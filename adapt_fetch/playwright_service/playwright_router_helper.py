import asyncio
import re
import sys
import signal
import os
import atexit
import copy
import time
import threading
import aiohttp
import socket
import json
import base64
import io
import requests
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional, Tuple
from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import logging
from urllib.parse import urlparse, urljoin
from pathlib import Path

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logging.getLogger(__name__).warning("PIL不可用，favicon处理功能受限")

# 导入爬虫工具
from adapt_fetch.easy_crawler.easy_crawler import EasyGetCrawler


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _smart_detector_config_read_path(config_file: str) -> Path:
    root = _package_root()
    cfg = root / "config" / config_file
    if cfg.is_file():
        return cfg
    legacy = root / config_file
    if legacy.is_file():
        return legacy
    return cfg


def _smart_detector_config_write_path(config_file: str) -> Path:
    cfg_dir = _package_root() / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    return cfg_dir / config_file


# 配置日志

logger = logging.getLogger(__name__)

# ====== 全局配置 ======
# Playwright 无头模式控制（内部管理，外部不应控制）
PLAYWRIGHT_HEADLESS = False  # False=有头模式（默认），True=无头模式


# ====== 统一的 REASON 常量与检测工具（新增） ======
# 说明：这些常量用于统一标识失败原因；请在各爬取路径命中时写入 result['reason']
REASON_PAGE_NOT_LOADED = "PAGE_NOT_LOADED"                 # 页面文本为空或未能加载
REASON_PAGE_PARTIAL_LOAD = "PAGE_PARTIAL_LOAD"             # 页面仅部分加载，文本长度不足阈值
REASON_BLOCKED_VERIFICATION = "BLOCKED_VERIFICATION"       # 被拦截要求验证（人机/验证码等）
REASON_BLOCKED_ACCESS_DENIED = "BLOCKED_ACCESS_DENIED"     # 访问被拒绝（如 401/403 等）
REASON_BLOCKED_CLOUDFLARE = "BLOCKED_CLOUDFLARE"           # Cloudflare/抗 DDoS 拦截
REASON_REQUIRE_JAVASCRIPT = "REQUIRE_JAVASCRIPT"           # 需要启用 JavaScript
REASON_TIMEOUT = "TIMEOUT"                                 # 请求或渲染超时
REASON_NETWORK_ERROR = "NETWORK_ERROR"                     # 网络错误（连接失败等）

#
# 常见拦截/验证关键词（中英文混排）
#
# ⚠️ 重要：这些关键词会参与“内容健康检测”，命中可能把结果标记为失败。
# 按你的要求：不再区分高/低置信度 —— **所有关键词只在“短文段”时才触发判定**，
# 避免正常长文里出现类似词汇时被误伤。
BLOCKED_KEYWORDS = [
    # Captcha / 人机验证
    'captcha',
    'recaptcha',
    'hcaptcha',
    'turnstile',
    'robot check',
    'are you a robot',
    'are you human',
    'human verification',
    'verify you are human',
    'security check',
    'security verification',
    'prove you are human',
    # Cloudflare challenge 常见片段（与 REASON_BLOCKED_CLOUDFLARE 不冲突，作为补充）
    'cf-chl',
    'challenge-platform',
    # 频控/拒绝类（同样只在短文段时命中）
    'access denied',
    'too many requests',
    'rate limit',
    'temporarily unavailable',
    'request blocked',
    'request was blocked',
    # 中文常见
    '验证码',
    '人机验证',
    '安全验证',
    '验证您是人类',
    '您的访问过于频繁',
    '访问被拒绝',
    '请求过于频繁',
]

def extract_text_from_html(html: str) -> str:
    """从 HTML 中粗略提取可见文本，用于健康度检测。"""
    try:
        if not html:
            return ''
        # 移除脚本与样式
        html_ = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html_ = re.sub(r'<style[^>]*>.*?</style>', '', html_, flags=re.DOTALL | re.IGNORECASE)
        # 移除所有标签
        text = re.sub(r'<[^>]+>', '', html_, flags=re.DOTALL)
        # 合并空白
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except Exception:
        return ''

def _contains_any_keywords(content: str, keywords: list) -> bool:
    if not content:
        return False
    content_lower = content.lower()
    return any(k in content_lower for k in keywords)

def detect_reason_from_text(text: str, html: Optional[str] = None) -> Optional[str]:
    """
    基于页面文本/HTML 检测拦截/验证类问题，返回对应 REASON；未命中返回 None。
    优先级：
      Cloudflare → REQUIRE_JAVASCRIPT → BLOCKED_VERIFICATION → TIMEOUT → PAGE_PARTIAL_LOAD
    """
    if not text:
        return None
    
    text_lower = text.lower()
    
    # 🔥 关键词命中：只在“短文段”时才判定（避免误伤正常长文）
    # 注：真正的网络/渲染超时尽量由异常/超时控制逻辑上报，而不是从正文猜测。
    short_block_text = len(text_lower) < 500
    html_lower = (html or '').lower() if html else ''

    if short_block_text:
        # Cloudflare/抗DDoS
        if _contains_any_keywords(text_lower, ['cloudflare', 'ddos protection', 'just a moment', 'attention required']) \
           or (html_lower and _contains_any_keywords(html_lower, ['cloudflare'])):
            return REASON_BLOCKED_CLOUDFLARE
        # 需要启用 JS
        if _contains_any_keywords(text_lower, ['please enable javascript', 'javascript is required', '请启用javascript', '需要启用javascript']):
            return REASON_REQUIRE_JAVASCRIPT
        # 其他验证/拦截（统一关键词列表）
        if _contains_any_keywords(text_lower, BLOCKED_KEYWORDS) or (html_lower and _contains_any_keywords(html_lower, BLOCKED_KEYWORDS)):
            return REASON_BLOCKED_VERIFICATION
    # 🔥 检测“超时”必须非常谨慎：
    # - 技术类文章/代码片段里经常出现 "timeout" 字样，不能据此判定为失败。
    # - 这里只在“看起来像错误页/报错文案”且文本较短时才判定为 TIMEOUT。
    #   （真正的网络/渲染超时应尽量由异常/超时控制逻辑上报，而不是从正文内容猜测。）
    try:
        short_text = len(text_lower) < 300
    except Exception:
        short_text = False
    if short_text:
        timeout_markers = [
            'navigation timeout', 'timeout exceeded', 'timed out',
            'net::err_timed_out', 'err_connection_timed_out',
            'timeouterror', 'request timed out',
            '请求超时', '连接超时', '操作超时'
        ]
        if _contains_any_keywords(text_lower, timeout_markers):
            return REASON_TIMEOUT
    # 🔥 新增：检测文本太短/部分加载相关错误
    if _contains_any_keywords(text_lower, ['too_short', 'too short', 'text_too_short', 'cleaned_text_too_short', 'plain_text_too_short', '文本太短', '内容过短', '部分加载']):
        return REASON_PAGE_PARTIAL_LOAD
    return None

def detect_reason_from_status(status_code: Optional[int]) -> Optional[str]:
    """根据 HTTP 状态码给出 REASON；未命中返回 None。"""
    if status_code is None:
        return None
    try:
        code = int(status_code)
    except Exception:
        return None
    if code in (401, 403):
        return REASON_BLOCKED_ACCESS_DENIED
    if code == 429:
        return REASON_BLOCKED_VERIFICATION
    return None

def evaluate_content_health(text: str,
                            status_code: Optional[int] = None,
                            min_text_length: int = 100) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """
    评估内容健康度，返回 (ok, reason, details)：
      - ok=False 时，reason 为上面定义的固定常量之一
      - details 便于调试的补充信息（长度、状态码、命中项等）
    判定顺序：
      1) 状态码映射（401/403/429）
      2) 文本级别拦截/验证关键词（Cloudflare/验证码/需要JS等）
      3) 文本长度为 0 → PAGE_NOT_LOADED
      4) 文本长度 < min_text_length → PAGE_PARTIAL_LOAD
    """
    text = (text or '').strip()
    text_len = len(text)
    details = {'text_length': text_len, 'min_text_length': min_text_length, 'status_code': status_code}

    # 1) 状态码优先
    reason = detect_reason_from_status(status_code)
    if reason:
        details['hit'] = 'status_code'
        return False, reason, details

    # 2) 关键词检测
    kw_reason = detect_reason_from_text(text)
    if kw_reason:
        details['hit'] = 'blocked_keywords'
        return False, kw_reason, details

    # 3) 文本为空
    if text_len == 0:
        details['hit'] = 'empty_text'
        return False, REASON_PAGE_NOT_LOADED, details

    # 4) 文本不足阈值
    if text_len < int(min_text_length):
        details['hit'] = 'partial_text'
        return False, REASON_PAGE_PARTIAL_LOAD, details

    # 正常
    return True, None, details


class PDFRedirectException(Exception):
    """PDF重定向异常，用于从网页处理流程中断并返回PDF处理结果"""
    def __init__(self, pdf_result):
        self.pdf_result = pdf_result
        super().__init__(f"PDF重定向: {pdf_result.get('url', 'unknown')}")


class CrawlRequest(BaseModel):
    """爬虫请求模型"""
    urls: List[str] = Field(..., description="要爬取的URL列表")
    timeout: Optional[int] = Field(30000, description="页面加载超时时间(毫秒)", ge=5000, le=120000)
    wait_for_selector: Optional[str] = Field(None, description="等待特定选择器出现")
    wait_for_load_state: Optional[str] = Field("commit", description="等待加载状态: commit, domcontentloaded, load, networkidle")
    user_agent: Optional[str] = Field(None, description="自定义User-Agent")
    enable_javascript: Optional[bool] = Field(True, description="是否启用JavaScript")
    extra_headers: Optional[Dict[str, str]] = Field(default_factory=dict, description="额外的请求头")
    concurrent_limit: Optional[int] = Field(3, description="抢占式最大并发量（队列未空时始终占满该并发）", ge=1, le=20)
    use_edge_user_data: Optional[bool] = Field(
        True,
        description=(
            "历史兼容字段。Playwright 已固定使用独立目录："
            "%LOCALAPPDATA%\\LightReadPlaywrightEdge（Linux: ~/.local/share/lightread-playwright-edge），"
            "不再与系统 Edge User Data 共用；传参不影响该路径。"
        ),
    )
    # headless 字段已废弃，由内部全局变量 PLAYWRIGHT_HEADLESS 控制
    scroll_pages: Optional[bool] = Field(True, description="是否滚动页面加载完整内容")
    scroll_count: Optional[int] = Field(3, description="滚动次数，0表示不滚动", ge=0, le=10)
    scroll_interval: Optional[float] = Field(0.2, description="滚动间隔时间(秒)", ge=0.1, le=1.0)
    mode: Optional[str] = Field("normal", description="爬取模式: normal(并发EasyGet+Playwright+Jina), fast(仅EasyGet), playwright(仅Playwright), jina(仅Jina), no_jina(并发EasyGet+Playwright)")
    crawl_type: Optional[str] = Field("auto", description="爬取类型: pdf(PDF专用), web(网页专用), auto(自动检测)")
    use_intellicache: Optional[bool] = Field(True, description="是否使用智能缓存（True=允许使用缓存，False=跳过缓存如不存在缓存一样）")
    follow_redirects: Optional[bool] = Field(True, description="是否跟随重定向(快速模式)")
    max_redirects: Optional[int] = Field(10, description="最大重定向次数(快速模式)", ge=1, le=30)
    verify_ssl: Optional[bool] = Field(False, description="是否验证SSL证书(快速模式)")
    encoding: Optional[str] = Field(None, description="强制指定编码，留空自动检测(快速模式)")
    use_edge_cookies: Optional[bool] = Field(False, description="是否使用Edge浏览器的cookies(快速模式)")
    edge_profile: Optional[str] = Field("Default", description="Edge配置文件名称(快速模式)")
    target_domains: Optional[List[str]] = Field(default_factory=list, description="指定要导入cookies的域名列表(快速模式)")
    custom_cookies: Optional[Dict[str, str]] = Field(default_factory=dict, description="自定义cookies(快速模式)")
    pdf_download_timeout: Optional[int] = Field(10000, description="PDF下载超时时间(毫秒)", ge=5000, le=30000)
    pdf_max_size_mb: Optional[int] = Field(10, description="PDF最大下载大小(MB)", ge=1, le=500)
    htmlclean: Optional[Dict[str, Any]] = Field(None, description="HTML清理配置参数，用于指挥URL解析工作")
    htmlclean_enabled: Optional[bool] = Field(True, description="是否启用HTML清理")
    smart_wait_min_chars: Optional[int] = Field(None, description="文本稳定判断的最小字符数，留空自动根据网站类型调整", ge=100, le=2000)
    smart_wait_idle_ms: Optional[int] = Field(None, 
          description="文本稳定的空闲时间(毫秒)，留空自动根据网站类型调整", ge=0, le=2000)
    smart_wait_timeout_ms: Optional[int] = Field(
        5000,
        description="最大等待时间(毫秒)，留空时使用全局timeout参数",
        ge=3000, le=20000
    )
    smart_wait_delta_chars: Optional[int] = Field(
        100,
        description="判定稳定时，idleMs时间窗口内文本变化的最大字符数阈值",
        ge=0, le=1000
    )
    smart_stable_limit_times: Optional[int] = Field(
        3,
        description="连续稳定次数阈值，必须连续N次检查都稳定才算真正稳定",
        ge=1, le=20
    )
    smart_wait_zero_text_threshold: Optional[int] = Field(
        12,
        description="连续检测到零文本的次数阈值，达到此值时判定为内容无法检测",
        ge=2, le=20
    )
    smart_wait_enabled: Optional[bool] = Field(True, description="是否启用智能等待功能")
    extract_title: Optional[bool] = Field(True, description="是否提取页面标题")
    extract_icon: Optional[bool] = Field(True, description="是否提取页面favicon")
    freeze_page_after_wait: Optional[bool] = Field(True, description="智能等待完成后是否冻结页面以优化HTML提取速度（True=冻结页面提升HTML提取速度，False=保持页面活跃状态）")
    pdf_detection_result: Optional[Dict[str, Any]] = Field(None, description="内部使用：PDF检测结果", exclude=True)
    text_limit: Optional[int] = Field(
        150,
        description="健康检测所需的最小正文字符数；过低易放过空页，过高易误报 PAGE_PARTIAL_LOAD（如公众号清理后偏短）",
        ge=30,
        le=1000,
    )
    auto_download_pdf: Optional[bool] = Field(True, description="是否自动下载PDF文件")
    use_readability: Optional[bool] = Field(False, description="是否使用 Readability 算法清理 HTML（True=使用快速的 Readability，False=使用精确的 HTML 清理服务）")
    easyget_timeout: Optional[int] = Field(5, description="并发模式下 EasyGet 的超时时间（秒），默认5秒", ge=1, le=30)
    
    # 🔥 分块并行清理(MapReduce)配置 - 强制策略，简化参数
    # 注：走 HTML 清理服务（/process_html）时，同名字段会由 playwright_router 写入请求 options。
    chunked_threshold_mb: Optional[float] = Field(8.0, description="触发MapReduce分块并行清理的HTML大小阈值(MB)，超过则自动分块加速", ge=0.5, le=50.0)
    chunk_target_kb: Optional[int] = Field(512, description="单个分块的目标大小(KB)，会按此大小切分HTML (512KB=0.5MB, 2048KB=2MB)", ge=64, le=2048)
    chunk_overlap_chars: Optional[int] = Field(1024, description="相邻分块之间的重叠字符数，避免边界截断内容", ge=0, le=8192)
    chunk_concurrency: Optional[int] = Field(4, description="分块并行清理的并发度，同时处理的块数 (-1表示无限制，所有块一次性并发)", ge=-1, le=64)

    @field_validator('urls')
    @classmethod
    def validate_urls(cls, v):
        if not v:
            raise ValueError('URLs列表不能为空')
        for url in v:
            if not url.strip():
                raise ValueError('URL不能为空字符串')
        return v

    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v):
        if v not in ['normal', 'fast', 'playwright', 'jina', 'no_jina']:
            raise ValueError('mode must be one of: normal, fast, playwright, jina, no_jina')
        return v

    @field_validator('crawl_type')
    @classmethod
    def validate_crawl_type(cls, v):
        if v not in ['pdf', 'web', 'auto']:
            raise ValueError('crawl_type must be one of: pdf, web, auto')
        return v


class CrawlResponse(BaseModel):
    """爬虫响应模型"""
    code: int
    msg: str
    data: Optional[Dict[str, Any]] = None


def get_edge_executable_path() -> Optional[str]:
    """获取Edge浏览器可执行文件路径"""
    # Windows路径
    windows_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    
    # Linux路径
    linux_paths = [
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
        "/opt/microsoft/msedge/msedge",
    ]
    
    paths = windows_paths if sys.platform == "win32" else linux_paths
    
    for path in paths:
        if os.path.exists(path):
            return path
    return None


def get_edge_user_data_dir() -> Optional[str]:
    """获取Edge用户数据目录"""
    if sys.platform == "win32":
        user_data_dir = os.path.expanduser(r"~\AppData\Local\Microsoft\Edge\User Data")
    else:
        user_data_dir = os.path.expanduser("~/.config/microsoft-edge")
    
    return user_data_dir if os.path.exists(user_data_dir) else None


def get_fixed_playwright_edge_user_data_dir() -> str:
    """
    Playwright 爬虫专用 Edge 用户数据目录（与系统日常 Edge 隔离）。
    Windows: %LOCALAPPDATA%\\LightReadPlaywrightEdge
    Linux:   ~/.local/share/lightread-playwright-edge
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "LightReadPlaywrightEdge")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "lightread-playwright-edge")


def resolve_playwright_edge_user_data_dir() -> str:
    """始终使用写死在 get_fixed_playwright_edge_user_data_dir 中的目录。"""
    path = get_fixed_playwright_edge_user_data_dir()
    os.makedirs(path, exist_ok=True)
    logger.info("📁 Playwright Edge 爬虫用户数据目录（固定）: %s", path)
    return path


def get_browser_args(use_user_data: bool = True, fast_mode: bool = False, for_persistent_context: bool = False, smart_detector: 'SmartModeDetector' = None) -> List[str]:
    """
    获取浏览器启动参数
    
    Args:
        use_user_data: 是否使用用户数据目录
        fast_mode: 是否为快速模式
        for_persistent_context: 是否为持久化上下文
        smart_detector: 智能检测器实例，用于获取配置参数
        
    Returns:
        浏览器启动参数列表
    """
    # 基础参数
    args = [
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-blink-features=AutomationControlled',
        '--disable-dev-shm-usage',
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
        '--disable-features=TranslateUI,VizDisplayCompositor',
        '--disable-ipc-flooding-protection',
        '--enable-features=NetworkService,NetworkServiceLogging',
        '--force-color-profile=srgb',
        '--metrics-recording-only',
        '--use-mock-keychain',
    ]
    
    # Fast模式使用EasyGet（HTTP请求），不需要浏览器参数，直接返回基础参数
    if fast_mode:
        logger.debug(f"🚀 Fast模式：使用EasyGet，不加载浏览器配置参数")
    else:
        # Normal模式：从配置文件读取浏览器配置参数
        browser_config = {}
        if smart_detector and hasattr(smart_detector, 'config'):
            browser_config = smart_detector.config.get('browser_config', {})
            logger.debug(f"🔧 从配置文件加载浏览器配置: {len(browser_config)}个参数组")
        
        # 从配置文件读取各种参数组
        performance_args = browser_config.get('performance_args', [])
        anti_detection_args = browser_config.get('anti_detection_args', [])
        stability_args = browser_config.get('stability_args', [])
        user_experience_args = browser_config.get('user_experience_args', [])
        network_args = browser_config.get('network_args', [])
        extra_args = browser_config.get('extra_args', [])
        memory_saving_args = browser_config.get('memory_saving_args', [])
        
        # 添加normal模式专用参数
        for arg_group in [performance_args, anti_detection_args, stability_args, user_experience_args, network_args]:
            args.extend(arg_group)
        
        # 自定义额外参数
        if extra_args:
            args.extend(extra_args)
            logger.debug(f"🔧 添加自定义浏览器参数: {extra_args}")

        # 节省内存参数：优先使用配置文件；若未提供则使用一组安全默认值
        if memory_saving_args:
            args.extend(memory_saving_args)
            logger.debug(f"🧹 使用配置中的节省内存参数: {memory_saving_args}")
        else:
            default_mem_args = [
                '--disable-extensions',  # 关闭扩展与其后台页
                '--disable-component-extensions-with-background-pages',
                '--disable-sync',
                '--disable-background-networking',
                '--enable-low-end-device-mode',  # 触发低端设备优化策略
                # 关闭备用渲染器与部分缓存，减少“闲置”进程与缓存占用
                '--disable-features=SpareRendererForSitePerProcess',
                '--disable-features=BackForwardCache',
                '--disable-features=NetworkServiceCodeCache'
            ]
            args.extend(default_mem_args)
            logger.debug("🧹 已添加默认节省内存参数")
        
        logger.debug(f"📊 Normal模式参数统计: 性能({len(performance_args)}) 反检测({len(anti_detection_args)}) 稳定性({len(stability_args)}) 体验({len(user_experience_args)}) 网络({len(network_args)}) 自定义({len(extra_args)})")
    
    # 用户数据目录（仅非persistent context时添加）
    if use_user_data and not for_persistent_context:
        user_data_dir = get_edge_user_data_dir()
        if user_data_dir:
            args.append(f'--user-data-dir={user_data_dir}')

    # ⚠️ 兼容性修复：在持久化上下文(persistent context)模式下，如果仍然包含
    # '--disable-web-security' 参数，Edge/Chromium 会因为默认用户数据目录
    # 与该开关组合而直接崩溃退出，日志中出现
    # "Web security may only be disabled if '--user-data-dir' is also specified with a non-default value."
    # 因此，当 for_persistent_context 为 True 时，强制移除该参数以避免浏览器
    # 无法启动的问题。
    if for_persistent_context:
        args = [arg for arg in args if arg != '--disable-web-security']

    # 代理设置：persistent context 下必须由 Playwright 的 proxy= 指定，禁止再叠 CLI --proxy-server。
    # 否则与 launch_persistent_context(proxy=...) 双写，Edge 易出现 ERR_PROXY_CONNECTION_FAILED。
    if for_persistent_context:
        logger.debug("🌐 persistent context：代理仅使用 Playwright proxy 参数，跳过 --proxy-server")
    elif smart_detector and hasattr(smart_detector, 'config'):
        proxy_config = smart_detector.config.get('proxy_config', {})
        if proxy_config:
            proxy_server = proxy_config.get('proxy_server')
            proxy_bypass = proxy_config.get('proxy_bypass_list')

            if proxy_server:
                if "://" not in str(proxy_server):
                    proxy_server = f"http://{proxy_server}"
                args.extend([
                    f'--proxy-server={proxy_server}',
                    f'--proxy-bypass-list={proxy_bypass or "localhost,127.0.0.1"}',
                    '--force-direct-connection-for-localhost',
                ])
                logger.debug("🌐 配置代理: %s (绕过: %s)", proxy_server, proxy_bypass)
            else:
                logger.debug("🌐 配置文件中未设置代理服务器")
        else:
            logger.debug("🌐 配置文件中未找到代理配置")
    else:
        logger.debug("🌐 未提供智能检测器实例，跳过代理配置")
    
    # 去重并返回
    # 使用有序集合去重，保持参数顺序
    seen = set()
    deduplicated_args = []
    for arg in args:
        if arg not in seen:
            seen.add(arg)
            deduplicated_args.append(arg)
    
    logger.debug(f"🚀 浏览器启动参数准备完成: {len(deduplicated_args)}个参数 (快速模式: {fast_mode})")
    return deduplicated_args


# -------------- 轻量标签页管家：占位与孤儿空白页清理 --------------
async def ensure_placeholder_tab(context: BrowserContext, placeholder_url: str, placeholder_page: Optional[Page], resource_blocker=None) -> Optional[Page]:
    """确保存在一个占位页，返回占位页对象。
    
    Args:
        context: 浏览器上下文
        placeholder_url: 占位页URL
        placeholder_page: 已有的占位页（可选）
        resource_blocker: 资源拦截回调函数（可选），用于节省带宽和内存
    """
    try:
        if not context:
            return placeholder_page

        if placeholder_page and (not placeholder_page.is_closed()):
            return placeholder_page

        # 已有占位URL的页面
        for p in list(context.pages):
            try:
                if p and (not p.is_closed()) and (p.url or '').startswith(placeholder_url):
                    # 🔥 为已存在的占位页补充资源拦截器（如果还没注册）
                    if resource_blocker:
                        try:
                            await p.route("**/*", resource_blocker)
                            logger.debug("✅ 已为现有占位页注册资源拦截器")
                        except Exception as _route_e:
                            logger.debug(f"为现有占位页注册资源拦截失败（可能已注册）: {_route_e}")
                    return p
            except Exception:
                continue

        # 新建占位页
        page = await context.new_page()
        
        # 🔥 注册资源拦截器（在导航之前，避免加载不必要的资源）
        if resource_blocker:
            try:
                await page.route("**/*", resource_blocker)
                logger.debug("✅ 已为新占位页注册资源拦截器，节省资源加载")
            except Exception as _route_e:
                logger.debug(f"为新占位页注册资源拦截失败: {_route_e}")
        
        try:
            await page.goto(placeholder_url, wait_until="domcontentloaded", timeout=8000)
        except Exception as _e:
            logger.debug(f"占位页导航失败（忽略）: {_e}")

        # 若被关闭，自动补回
        try:
            page.once('close', lambda: asyncio.create_task(ensure_placeholder_tab(context, placeholder_url, None, resource_blocker)))
        except Exception:
            pass

        return page
    except Exception as e:
        logger.debug(f"ensure_placeholder_tab 异常: {e}")
        return placeholder_page


async def cleanup_orphan_about_blank_pages(context: BrowserContext, timeout_manager, placeholder_url: str, placeholder_page: Optional[Page], resource_blocker=None) -> Optional[Page]:
    """关闭不属于活跃任务的 about:blank 页面，并确保至少有一个占位页。
    
    Args:
        context: 浏览器上下文
        timeout_manager: 超时管理器
        placeholder_url: 占位页URL
        placeholder_page: 已有的占位页（可选）
        resource_blocker: 资源拦截回调函数（可选）
    """
    try:
        if not context:
            return placeholder_page

        try:
            active_pages = {info.get('page') for info in getattr(timeout_manager, 'active_tasks', {}).values()}
        except Exception:
            active_pages = set()

        for p in list(context.pages):
            try:
                if not p or p.is_closed():
                    continue
                # ⚠️ 跳过占位页（通过引用比较，避免占位页导航失败时被误关闭）
                if placeholder_page and p == placeholder_page:
                    continue
                if p in active_pages:
                    continue
                url_now = p.url or ''
                if url_now.startswith('about:'):
                    await p.close()
            except Exception as _e:
                logger.debug(f"关闭 about:blank 页失败: {_e}")

        # 如果没有任何存活页，补一个占位页
        try:
            alive = [x for x in context.pages if x and not x.is_closed()]
            if len(alive) == 0:
                placeholder_page = await ensure_placeholder_tab(context, placeholder_url, None, resource_blocker)
        except Exception:
            pass

        return placeholder_page
    except Exception as e:
        logger.debug(f"cleanup_orphan_about_blank_pages 异常: {e}")
        return placeholder_page


# 🔥 智能模式检测器
class SmartModeDetector:
    """智能模式检测器 - 基于URL/指纹黑名单和HTML分析，支持配置文件和学习更新"""
    
    def __init__(self, config_file: str = "smart_detector_config.json"):
        self.config_file = config_file
        self.config = {}
        self.cache_data = {}
        self.domain_cache = {}
        self.spa_regex = None
        self.last_save_time = time.time()
        
        # 加载配置
        self._load_config()
        self._load_cache()
        
        # 编译正则表达式
        self._compile_patterns()
        
        logger.info(f"🧠 智能检测器初始化完成 - 域名黑名单: {len(self.spa_domains)}个, 框架模式: {len(self.spa_patterns)}个, 缓存: {len(self.domain_cache)}个")
    
    def get_random_user_agent(self) -> str:
        """从配置文件中获取随机 User-Agent"""
        import random
        try:
            user_agents = self.config.get('user_agents', [])
            if user_agents:
                selected_ua = random.choice(user_agents)
                logger.debug(f"🎲 随机选择 User-Agent: {selected_ua}")
                return selected_ua
            else:
                # 如果配置文件中没有 user_agents，使用默认值
                default_ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                logger.warning(f"⚠️ 配置文件中没有 user_agents，使用默认: {default_ua}")
                return default_ua
        except Exception as e:
            logger.error(f"❌ 获取随机 User-Agent 失败: {e}")
            # 异常时返回默认值
            return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    
    def _load_config(self):
        """加载配置文件"""
        try:
            config_path = _smart_detector_config_read_path(self.config_file)
            if config_path.is_file():
                with open(config_path, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
                logger.info(f"✅ 加载智能检测器配置: {config_path}")
            else:
                logger.warning(f"⚠️ 配置文件不存在，使用默认配置: {config_path}")
                self._create_default_config()
        except Exception as e:
            logger.error(f"❌ 加载配置文件失败: {e}")
            self._create_default_config()
        
        # 提取配置项
        self.spa_domains = set(self.config.get('spa_domains', []))
        self.spa_patterns = self.config.get('spa_framework_patterns', [])
        self.spa_root_selectors = self.config.get('spa_root_selectors', [])
        self.spa_meta_tags = self.config.get('spa_meta_tags', [])
        
        # 🔥 PDF检测配置
        pdf_config = self.config.get('pdf_detection', {})
        self.pdf_domains = set(pdf_config.get('known_pdf_domains', []))
        self.pdf_path_patterns = pdf_config.get('pdf_path_patterns', [])
        self.pdf_query_indicators = pdf_config.get('pdf_query_indicators', [])
        self.pdf_learning_enabled = pdf_config.get('learning_enabled', True)
        self.pdf_auto_learn_threshold = pdf_config.get('auto_learn_threshold', 3)
        
        # 检测参数
        detection_config = self.config.get('detection_thresholds', {})
        self.body_text_threshold = detection_config.get('body_text_threshold', 500)
        self.script_count_threshold = detection_config.get('script_count_threshold', 10)
        self.confidence_threshold = detection_config.get('confidence_threshold', 60)
        
        # SPA检测规则
        spa_rules = detection_config.get('spa_detection_rules', {})
        self.min_criteria_match = spa_rules.get('min_criteria_match', 2)
        self.criteria_weights = spa_rules.get('criteria', {})
        
        # 缓存配置
        self.auto_save_interval = self.config.get('auto_save_interval', 3600)
        self.max_cache_size = self.config.get('max_cache_size', 10000)
    
    def _load_cache(self):
        """加载缓存文件"""
        try:
            # 统一文件后，仅需确保 config 中带有 domains 字段
            self.domain_cache = self.config.setdefault('domains', {})
            # 若历史独立缓存文件仍存在，尝试合并后删除
            legacy_path = None
            for base in (Path(__file__).resolve().parent, _package_root()):
                cand = base / "smart_detector_cache.json"
                if cand.is_file():
                    legacy_path = cand
                    break
            if legacy_path is not None:
                try:
                    with open(str(legacy_path), 'r', encoding='utf-8') as f:
                        legacy_cache = json.load(f)
                    legacy_domains = legacy_cache.get('domains', {})
                    if legacy_domains:
                        self.domain_cache.update(legacy_domains)
                        logger.info(f"🔄 合并旧缓存 smart_detector_cache.json -> config (合并 {len(legacy_domains)} 条)")
                    os.remove(legacy_path)
                except Exception as merge_e:
                    logger.warning(f"合并旧缓存失败: {merge_e}")
            logger.info(f"✅ 缓存域名加载完成 ({len(self.domain_cache)} 个)")
        except Exception as e:
            logger.error(f"❌ 加载缓存文件失败: {e}")
            self._create_default_cache()
    
    def _create_default_config(self):
        """创建默认配置"""
        self.config = {
            "version": "1.0.0",
            "detection_thresholds": {
                "body_text_threshold": 500,
                "script_count_threshold": 10,
                "confidence_threshold": 60
            },
            "spa_domains": [],
            "spa_framework_patterns": [],
            "spa_root_selectors": [],
            "spa_meta_tags": [],
            # 🔥 PDF检测配置
            "pdf_detection": {
                "known_pdf_domains": [
                    # 🔥 从pdf_crawler.py整合的已知PDF查看器网站
                    "arxiv.org",
                    "proceedings.mlr.press",
                    "biorxiv.org",
                    "medrxiv.org",
                    "openreview.net",
                    "aclanthology.org",
                    "dl.acm.org",
                    "ieeexplore.ieee.org",
                    "link.springer.com",
                    "www.nature.com",
                    # 学术会议和期刊
                    "proceedings.neurips.cc",
                    "proceedings.icml.cc",
                    "proceedings.aaai.org",
                    "jmlr.org",
                    "www.ijcai.org",
                    # 其他常见学术网站
                    "scholar.google.com",
                    "semanticscholar.org",
                    "researchgate.net",
                    "academia.edu"
                ],
                "pdf_path_patterns": [
                    # 学术网站常见模式
                    "/pdf/",
                    "/content/pdf/",
                    "/paper_files/paper/",
                    "/proceedings/",
                    # 期刊网站常见模式
                    "/article/pdf/",
                    "/download/pdf/",
                    "/viewPDF/",
                    "/getPDF/",
                    # 其他常见模式
                    "pdf?",
                    "download=pdf",
                    "format=pdf",
                    "type=pdf"
                ],
                "pdf_query_indicators": [
                    "pdf",
                    "download",
                    "attachment"
                ],
                "learning_enabled": True,
                "auto_learn_threshold": 3  # 连续3次成功识别为PDF后自动学习
            },
            # 🔥 注意：浏览器配置参数、代理配置、User-Agent等配置
            # 应该在 smart_detector_config.json 文件中定义，不在Python代码中硬编码
            # 如果配置文件不存在或缺少配置，程序会使用空的默认值
        }
    
    def _create_default_cache(self):
        """创建默认缓存"""
        # 单文件模式：只初始化 config 中的 domains
        self.config.setdefault('domains', {})
        self.domain_cache = self.config['domains']
    
    # ------------------------------------------------------------------
    # 新增: 域名分层工具，支持父域泛化  foo.bar.example.com ->
    # ['foo.bar.example.com', 'bar.example.com', 'example.com']
    # ------------------------------------------------------------------
    def _get_domain_variants(self, domain: str):
        parts = domain.split('.')
        return ['.'.join(parts[i:]) for i in range(len(parts)-1)]  # 至少保留二级域名

    # ------------------------------------------------------------------
    # 新增: 更新分数工具
    # ------------------------------------------------------------------
    def _update_score(self, domain_key: str, decision: str, delta: int):
        entry = self.domain_cache.get(domain_key)
        if not entry:
            if delta > 0:
                # 新建
                self.domain_cache[domain_key] = {
                    'decision': decision,
                    'score': delta,
                    'reason': 'auto_learn',
                    'timestamp': time.time()
                }
            return
        # 若决策改变则重置分数为 1
        if entry.get('decision') != decision:
            entry['decision'] = decision
            entry['score'] = 1
        else:
            entry['score'] = entry.get('score', 1) + delta
        # 移除得分<=0
        if entry['score'] <= 0:
            self.domain_cache.pop(domain_key, None)

    def _compile_patterns(self):
        """编译正则表达式模式"""
        try:
            if self.spa_patterns:
                self.spa_regex = re.compile('|'.join(self.spa_patterns), re.IGNORECASE)
                logger.debug(f"✅ 编译SPA框架正则模式: {len(self.spa_patterns)}个")
            else:
                logger.warning("⚠️ 没有SPA框架模式可编译")
        except Exception as e:
            logger.error(f"❌ 编译正则表达式失败: {e}")
            self.spa_regex = None
    
    def _save_config(self):
        """保存配置文件"""
        try:
            # 更新配置中的域名列表（学习新的域名）
            self.config['spa_domains'] = list(self.spa_domains)
            
            # 🔥 更新PDF配置
            pdf_config = self.config.setdefault('pdf_detection', {})
            pdf_config['known_pdf_domains'] = list(self.pdf_domains)
            pdf_config['pdf_path_patterns'] = self.pdf_path_patterns
            pdf_config['pdf_query_indicators'] = self.pdf_query_indicators
            pdf_config['learning_enabled'] = self.pdf_learning_enabled
            pdf_config['auto_learn_threshold'] = self.pdf_auto_learn_threshold
            
            self.config['last_updated'] = time.strftime('%Y-%m-%dT%H:%M:%SZ')
            
            config_path = _smart_detector_config_write_path(self.config_file)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            logger.debug(f"💾 保存智能检测器配置: {config_path}")
        except Exception as e:
            logger.error(f"❌ 保存配置文件失败: {e}")
    
    def _auto_save_if_needed(self):
        """根据时间间隔自动保存"""
        if time.time() - self.last_save_time > self.auto_save_interval:
            self._save_config()
    
    def learn_from_result(self, url: str, predicted_mode: str, actual_performance: Dict[str, Any]):
        """从实际使用结果中学习，更新检测准确性"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower().lstrip('www.')

            success = actual_performance.get('success', False)
            actual_crawler_name = actual_performance.get('actual_crawler')
            # 将 Jina 作为独立类别参与学习；未知类别回退为 'playwright'
            if actual_crawler_name in ('easyget', 'playwright', 'jina'):
                actual_mode = actual_crawler_name
            else:
                actual_mode = 'playwright'

            # +1 分奖励正确策略，-1 分惩罚错误策略
            if success:
                self._update_score(domain, actual_mode, 1)
            else:
                self._update_score(domain, actual_mode, -1)

            self._auto_save_if_needed()
        except Exception as e:
            logger.warning(f"⚠️ 学习处理异常: {e}")
    
    def learn_pdf_pattern(self, url: str, success: bool, detection_method: str):
        """学习PDF URL模式"""
        try:
            if not self.pdf_learning_enabled or not success:
                return
                
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            if domain.startswith('www.'):
                domain = domain[4:]
            
            # 获取或创建PDF学习统计
            pdf_learning = self.config.setdefault('pdf_learning_stats', {})
            domain_stats = pdf_learning.setdefault(domain, {
                'success_count': 0,
                'total_attempts': 0,
                'learned': False,
                'patterns_found': [],
                'first_seen': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'last_seen': time.strftime('%Y-%m-%dT%H:%M:%SZ')
            })
            
            # 更新统计
            domain_stats['total_attempts'] += 1
            if success:
                domain_stats['success_count'] += 1
            domain_stats['last_seen'] = time.strftime('%Y-%m-%dT%H:%M:%SZ')
            
            # 检查是否达到学习阈值
            if (domain_stats['success_count'] >= self.pdf_auto_learn_threshold and 
                not domain_stats['learned'] and 
                domain not in self.pdf_domains):
                
                # 自动学习新的PDF域名
                self.pdf_domains.add(domain)
                domain_stats['learned'] = True
                
                # 更新配置
                pdf_config = self.config.setdefault('pdf_detection', {})
                pdf_config['known_pdf_domains'] = list(self.pdf_domains)
                
                logger.info(f"📚 自动学习新PDF域名: {domain} (成功{domain_stats['success_count']}次)")
                
                # 分析URL模式
                path = parsed.path
                if path and path != '/':
                    # 提取可能的路径模式
                    path_parts = path.split('/')
                    for i, part in enumerate(path_parts):
                        if 'pdf' in part.lower():
                            pattern = '/'.join(path_parts[:i+1]) + '/'
                            if pattern not in self.pdf_path_patterns:
                                self.pdf_path_patterns.append(pattern)
                                pdf_config['pdf_path_patterns'] = self.pdf_path_patterns
                                domain_stats['patterns_found'].append(pattern)
                                logger.info(f"📚 学习新PDF路径模式: {pattern}")
                
                # 保存配置
                self._save_config()
            
        except Exception as e:
            logger.warning(f"⚠️ PDF学习处理异常: {e}")
    
    def is_spa_domain(self, url: str) -> bool:
        """检查URL是否属于已知的SPA域名"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            
            # 移除www前缀
            if domain.startswith('www.'):
                domain = domain[4:]
            
            # 检查完整域名匹配
            if domain in self.spa_domains:
                return True
            
            # 检查子域名匹配
            for spa_domain in self.spa_domains:
                if domain.endswith('.' + spa_domain):
                    return True
            
            return False
        except:
            return False
    
    def is_pdf_url(self, url: str) -> bool:
        """检查URL是否是PDF文件URL - 使用配置文件中的模式"""
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()
            domain = parsed.netloc.lower()
            
            # 移除www前缀
            if domain.startswith('www.'):
                domain = domain[4:]
            
            # 1. 直接检查文件扩展名
            if path.endswith('.pdf'):
                return True
            
            # 2. 特殊处理arxiv.org - 只有/pdf/路径才认为是PDF
            if domain == 'arxiv.org':
                if '/pdf/' in path:
                    return True
                # arxiv.org的其他路径（如/html/、/abs/等）不是PDF
                return False
            
            # 3. 检查已知的PDF域名
            if domain in self.pdf_domains:
                return True
            
            # 检查子域名匹配
            for pdf_domain in self.pdf_domains:
                if domain.endswith('.' + pdf_domain):
                    return True
            
            # 4. 检查配置中的PDF URL模式
            full_url = url.lower()
            for pattern in self.pdf_path_patterns:
                if pattern in full_url:
                    return True
            
            # 5. 检查查询参数中的PDF指示
            query_params = parsed.query.lower()
            if query_params:
                for indicator in self.pdf_query_indicators:
                    if indicator in query_params and 'pdf' in query_params:
                        return True
            
            return False
        except:
            return False
    
    def detect_spa_from_html(self, html: str, url: str) -> Dict[str, Any]:
        """从HTML内容检测是否为SPA应用 - 基于Stack Overflow最佳实践优化"""
        try:
            html_lower = html.lower()
            
            # 🔥 优化的SPA检测算法 - 基于Stack Overflow经验
            criteria_matched = []
            total_score = 0
            
            # 1. 正文字符数检测（去标签后）< 500字 - 权重40
            body_match = re.search(r'<body[^>]*>(.*?)</body>', html_lower, re.DOTALL)
            body_content = body_match.group(1) if body_match else html_lower
            
            # 移除脚本和样式标签内容
            body_content = re.sub(r'<script[^>]*>.*?</script>', '', body_content, flags=re.DOTALL)
            body_content = re.sub(r'<style[^>]*>.*?</style>', '', body_content, flags=re.DOTALL)
            body_content = re.sub(r'<[^>]+>', '', body_content)  # 移除HTML标签
            body_text = body_content.strip()
            body_length = len(body_text)
            
            empty_body_weight = self.criteria_weights.get('empty_body', {}).get('weight', 40)
            if body_length < self.body_text_threshold:
                criteria_matched.append(f"正文过短({body_length}<{self.body_text_threshold}字符)")
                total_score += empty_body_weight
            
            # 2. <script>标签数量 >= 10 - 权重35
            script_count = len(re.findall(r'<script[^>]*>', html_lower))
            high_script_weight = self.criteria_weights.get('high_script_count', {}).get('weight', 35)
            if script_count >= self.script_count_threshold:
                criteria_matched.append(f"脚本数量过多({script_count}>={self.script_count_threshold}个)")
                total_score += high_script_weight
            
            # 3. 前端框架脚本检测 - 权重30
            spa_scripts = []
            framework_weight = self.criteria_weights.get('spa_framework_scripts', {}).get('weight', 30)
            if self.spa_regex:
                spa_scripts = self.spa_regex.findall(html)
                if spa_scripts:
                    criteria_matched.append(f"检测到{len(spa_scripts)}个前端框架脚本")
                    total_score += framework_weight
            
            # 4. 单一容器节点检测 (div id="root", app-root等) - 权重25
            spa_root_found = False
            root_container_weight = self.criteria_weights.get('spa_root_container', {}).get('weight', 25)
            for selector in self.spa_root_selectors:
                if selector.lower() in html_lower:
                    spa_root_found = True
                    criteria_matched.append(f"发现SPA根容器: {selector}")
                    total_score += root_container_weight
                    break
            
            # 5. SPA相关meta标签 - 权重10
            meta_score = 0
            meta_weight = self.criteria_weights.get('meta_indicators', {}).get('weight', 10)
            for meta_tag in self.spa_meta_tags:
                if meta_tag.lower() in html_lower:
                    meta_score += 1
            if meta_score > 2:
                criteria_matched.append(f"SPA相关meta标签({meta_score}个)")
                total_score += meta_weight
            
            # 🔥 基于Stack Overflow经验的判断逻辑：命中两项即可判定疑似JS重度
            is_spa = len(criteria_matched) >= self.min_criteria_match
            confidence = min(total_score, 100)  # 置信度不超过100%
            
            return {
                'is_spa': is_spa,
                'confidence': confidence,
                'criteria_matched': criteria_matched,
                'criteria_count': len(criteria_matched),
                'min_required': self.min_criteria_match,
                'reasons': criteria_matched
            }
            
        except Exception as e:
            logger.warning(f"HTML SPA检测异常: {e}")
            return {
                'is_spa': False,
                'confidence': 0,
                'criteria_matched': [],
                'criteria_count': 0,
                'reasons': [f"检测异常: {e}"]
            }
    
    def get_cached_decision(self, url: str) -> Optional[str]:
        """获取缓存的决策结果"""
        try:
            parsed = urlparse(url)
            domain_full = parsed.netloc.lower()
            if domain_full.startswith('www.'):
                domain_full = domain_full[4:]

            # 构造查询顺序：完整域 -> 父域 -> 顶级域
            for dom in self._get_domain_variants(domain_full):
                cached = self.domain_cache.get(dom)
                if cached:
                    return cached

            # 未命中
            return None
        except:
            return None
    
    def cache_decision(self, url: str, decision: str, reason: str, detection_details: Dict[str, Any] = None):
        """缓存决策结果"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower().lstrip('www.')
            
            # 🔥 检查是否为PDF URL，如果是则缓存为PDF类型
            if self.is_pdf_url(url):
                # 对于PDF URL，我们需要更精确的缓存策略
                # 使用URL模板（去掉文件名部分）作为缓存键
                url_template = self._get_url_template(url)
                cache_key = f"{domain}:{url_template}" if url_template != parsed.path else domain
                
                self.domain_cache[cache_key] = {
                    'decision': 'pdf',
                    'reason': f"PDF URL检测: {reason}",
                    'timestamp': time.time(),
                    'url_sample': url,
                    'url_template': url_template,
                    'is_pdf': True,
                    'detection_details': detection_details or {}
                }
                
                logger.info(f"🗃️ 缓存PDF决策: {cache_key} -> pdf ({reason})")
                
                # 🔥 PDF学习：如果是成功的PDF处理，进行学习
                if 'success' in reason.lower() or 'PDF URL检测' in reason:
                    self.learn_pdf_pattern(url, True, 'pdf_detection')
            else:
                # 普通网页：更新分数
                self._update_score(domain, decision, 1)
                logger.info(f"🗃️ 缓存决策: {domain} ({decision}) (+1)")
            
            # 定期保存
            self._auto_save_if_needed()
            
        except Exception as e:
            logger.warning(f"缓存决策失败: {e}")
    
    def _get_url_template(self, url: str) -> str:
        """获取URL模板，去掉文件名部分"""
        try:
            parsed = urlparse(url)
            path = parsed.path
            
            # 如果路径以.pdf结尾，去掉文件名部分
            if path.lower().endswith('.pdf'):
                # 找到最后一个/的位置
                last_slash = path.rfind('/')
                if last_slash != -1:
                    return path[:last_slash + 1]  # 保留最后的/
                else:
                    return path
            
            # 对于其他PDF模式，返回原路径
            return path
            
        except:
            return ""
    
    async def smart_detect_mode(self, url: str, easy_crawler: EasyGetCrawler, use_intellicache: bool = True) -> Dict[str, Any]:
        """🔥 智能检测应该使用的爬虫模式 - 简化版EasyGet优先策略"""
        start_time = time.time()
        
        try:
            # 1. 检查缓存 - 如果缓存中明确标记必须使用Playwright，直接返回
            if use_intellicache:
                cached = self.get_cached_decision(url)
                if cached and cached.get('decision') == 'pdf':
                    logger.info(f"📋 缓存决策: {url} -> PDF ({cached['reason']})")
                    return {
                        'recommended_mode': 'pdf',
                        'reason': f"缓存决策: {cached['reason']}",
                        'detection_method': 'cache_hit_pdf',
                        'confidence': 100,
                        'execution_time': time.time() - start_time,
                        'cache_hit': True,
                        'detection_details': cached.get('detection_details', {})
                    }
                elif cached and cached.get('decision') == 'playwright':
                    logger.info(f"📋 缓存决策: {url} -> Playwright ({cached['reason']})")
                    return {
                        'recommended_mode': 'playwright',
                        'reason': f"缓存决策: {cached['reason']}",
                        'detection_method': 'cache_hit',
                        'confidence': 100,
                        'execution_time': time.time() - start_time,
                        'cache_hit': True,
                        'detection_details': cached.get('detection_details', {})
                    }
                elif cached and cached.get('decision') == 'easyget':
                    logger.info(f"📋 缓存决策: {url} -> EasyGet ({cached['reason']})")
                    return {
                        'recommended_mode': 'easyget',
                        'reason': f"缓存决策: {cached['reason']}",
                        'detection_method': 'cache_hit',
                        'confidence': 100,
                        'execution_time': time.time() - start_time,
                        'cache_hit': True,
                        'detection_details': cached.get('detection_details', {})
                    }
                elif cached and cached.get('decision') == 'jina':
                    logger.info(f"📋 缓存决策: {url} -> Jina ({cached['reason']})")
                    return {
                        'recommended_mode': 'jina',
                        'reason': f"缓存决策: {cached['reason']}",
                        'detection_method': 'cache_hit',
                        'confidence': 100,
                        'execution_time': time.time() - start_time,
                        'cache_hit': True,
                        'detection_details': cached.get('detection_details', {})
                    }
            else:
                logger.info(f"🚫 智能缓存已禁用，跳过缓存检查: {url}")
            
            # 2. 检查是否为PDF URL - 直接返回PDF模式
            if self.is_pdf_url(url):
                if use_intellicache:
                    self.cache_decision(url, 'pdf', 'URL模式匹配')
                logger.info(f"📄 PDF URL检测: {url} -> PDF模式")
                return {
                    'recommended_mode': 'pdf',
                    'reason': 'URL模式匹配为PDF文件',
                    'detection_method': 'pdf_url_pattern',
                    'confidence': 95,
                    'execution_time': time.time() - start_time,
                    'cache_hit': False
                }
            
            # 3. 检查传统域名黑名单 - 已知的SPA网站直接使用Playwright
            if self.is_spa_domain(url):
                if use_intellicache:
                    self.cache_decision(url, 'playwright', '域名黑名单匹配')
                return {
                    'recommended_mode': 'playwright',
                    'reason': '域名在SPA黑名单中',
                    'detection_method': 'domain_blacklist',
                    'confidence': 95,
                    'execution_time': time.time() - start_time,
                    'cache_hit': False
                }
            
            # 🔥 4. 新策略：直接尝试EasyGet爬取，根据结果决定是否转用Playwright
            logger.info(f"🧠 EasyGet优先尝试: {url}")
            
            # 使用EasyGet直接尝试爬取，设置较短的超时时间
            try:
                easyget_result = await easy_crawler.crawl_single_url(
                    url=url,
                    timeout=10,  # 10秒超时
                    extract_title=False,  # 检测时不需要提取title
                    extract_icon=False,   # 检测时不需要提取icon
                    htmlclean_enabled=False  # 检测时不需要清理HTML，加快速度
                )
                
                # 分析EasyGet结果
                success = easyget_result.get('success', False)
                status_code = easyget_result.get('status_code', 0)
                html_content = easyget_result.get('html', '')
                execution_time = easyget_result.get('execution_time', 0)
                
                # 从HTML中提取文本内容
                text_content = ''
                if html_content:
                    try:
                        # 简单提取文本：移除标签
                        text_content = re.sub(r'<[^>]+>', '', html_content)
                        text_content = text_content.strip()
                    except Exception as e:
                        logger.debug(f"提取文本失败: {e}")
                        text_content = ''
                
                text_length = len(text_content) if text_content else 0
                
                # 检测常见的拦截/错误模式
                blocked_indicators = [
                    '403 Forbidden', '404 Not Found', '503 Service Unavailable',
                    'Access Denied', 'Blocked', 'Captcha', 'Human Verification',
                    'Please enable JavaScript', 'JavaScript is required',
                    'This site is protected', 'CloudFlare', 'DDoS protection',
                    '验证码', '人机验证', '访问被拒绝', '页面不存在',
                    '服务不可用', '需要启用JavaScript', '请启用JavaScript'
                ]
                
                is_blocked = any(indicator in text_content or indicator in html_content 
                               for indicator in blocked_indicators) if text_content and html_content else False
                
                # 判断EasyGet是否有效
                min_text_length = 100  # 最小文字长度阈值
                easyget_effective = (
                    success and
                    status_code == 200 and 
                    text_length >= min_text_length and
                    not is_blocked
                )
                
                if easyget_effective:
                    # EasyGet有效，推荐使用EasyGet
                    reason = f"EasyGet有效(文字{text_length}字符，状态{status_code}，耗时{execution_time:.1f}s)"
                    if use_intellicache:
                        self.cache_decision(url, 'easyget', reason, {
                            'text_length': text_length,
                            'status_code': status_code,
                            'blocked': is_blocked,
                            'execution_time': execution_time,
                            'easyget_test_success': True
                        })
                    
                    logger.info(f"✅ EasyGet测试成功: {url} -> EasyGet ({reason})")
                    return {
                        'recommended_mode': 'easyget',
                        'reason': reason,
                        'detection_method': 'easyget_test_success',
                        'confidence': 85,
                        'execution_time': time.time() - start_time,
                        'cache_hit': False,
                        'easyget_test_result': {
                            'success': success,
                            'text_length': text_length,
                            'status_code': status_code,
                            'blocked': is_blocked,
                            'execution_time': execution_time
                        }
                    }
                else:
                    # EasyGet无效，需要使用Playwright
                    failure_reasons = []
                    if not success:
                        failure_reasons.append("请求失败")
                    if status_code != 200:
                        failure_reasons.append(f"HTTP错误({status_code})")
                    if text_length < min_text_length:
                        failure_reasons.append(f"文字过少({text_length}字符)")
                    if is_blocked:
                        failure_reasons.append("检测到拦截/验证")
                    
                    failure_reason = "EasyGet失败: " + ", ".join(failure_reasons)
                    if use_intellicache:
                        self.cache_decision(url, 'playwright', failure_reason, {
                            'text_length': text_length,
                            'status_code': status_code,
                            'blocked': is_blocked,
                            'execution_time': execution_time,
                            'easyget_test_success': False,
                            'failure_reasons': failure_reasons
                        })
                    
                    logger.info(f"🔄 EasyGet测试失败，转用Playwright: {url} -> ({failure_reason})")
                    return {
                        'recommended_mode': 'playwright',
                        'reason': failure_reason,
                        'detection_method': 'easyget_test_failed',
                        'confidence': 80,
                        'execution_time': time.time() - start_time,
                        'cache_hit': False,
                        'easyget_test_result': {
                            'success': success,
                            'text_length': text_length,
                            'status_code': status_code,
                            'blocked': is_blocked,
                            'execution_time': execution_time,
                            'failure_reasons': failure_reasons
                        }
                    }
            except Exception as e:
                # EasyGet异常，使用Playwright
                reason = f"EasyGet异常: {str(e)}"
                if use_intellicache:
                    self.cache_decision(url, 'playwright', reason)
                
                logger.info(f"🔄 EasyGet异常，转用Playwright: {url} -> ({reason})")
                return {
                    'recommended_mode': 'playwright',
                    'reason': reason,
                    'detection_method': 'easyget_exception',
                    'confidence': 70,
                    'execution_time': time.time() - start_time,
                    'cache_hit': False,
                    'error': str(e)
                }
        except Exception as e:
            # 整体异常情况默认使用Playwright
            reason = f"检测异常: {str(e)}"
            if use_intellicache:
                self.cache_decision(url, 'playwright', reason)
            
            logger.warning(f"⚠️ 智能检测异常，使用Playwright: {url} -> {reason}")
            return {
                'recommended_mode': 'playwright',
                'reason': reason,
                'detection_method': 'exception_fallback',
                'confidence': 0,
                'execution_time': time.time() - start_time,
                'cache_hit': False,
                'error': str(e)
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """获取检测器统计信息"""
        cache_stats = self.cache_data.get('stats', {})
        learning_stats = self.config.get('learning_stats', {})
        
        return {
            'config_version': self.config.get('version', '未知'),
            'spa_domains_count': len(self.spa_domains),
            'spa_patterns_count': len(self.spa_patterns),
            'cache_size': len(self.domain_cache),
            'cache_stats': cache_stats,
            'learning_stats': learning_stats,
            # 🔥 PDF检测统计
            'pdf_detection_stats': {
                'pdf_domains_count': len(self.pdf_domains),
                'pdf_path_patterns_count': len(self.pdf_path_patterns),
                'pdf_query_indicators_count': len(self.pdf_query_indicators),
                'learning_enabled': self.pdf_learning_enabled,
                'auto_learn_threshold': self.pdf_auto_learn_threshold,
                'learned_domains': self.config.get('pdf_learning_stats', {})
            },
            'thresholds': {
                'body_text_threshold': self.body_text_threshold,
                'script_count_threshold': self.script_count_threshold,
                'confidence_threshold': self.confidence_threshold,
                'min_criteria_match': self.min_criteria_match
            }
        }
