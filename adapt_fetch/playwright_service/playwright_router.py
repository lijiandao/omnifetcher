import asyncio
import os
import sys
import time
import aiohttp
import socket
import copy
import base64
import json
import re
import contextlib
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import logging
# 日志基础配置，级别调到 DEBUG
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
from urllib.parse import urlparse, urljoin, urlunparse
from adapt_fetch.utils.tackle_huge_html import (
    should_use_readability_for_huge_html,
    map_reduce_readability,
    clean_with_readability_single,
    remove_base64_images,
    remove_script_style_tags,
)

# 🔥 Readability 和 html2text 已统一到 tackle_huge_html 模块中
# 不再需要在这里单独导入

# 导入 helper 文件中的类和函数
from .playwright_router_helper import (
    PDFRedirectException, SmartModeDetector, CrawlRequest, CrawlResponse,
    get_edge_executable_path, get_browser_args,
    PIL_AVAILABLE, ensure_placeholder_tab, cleanup_orphan_about_blank_pages,
    REASON_PAGE_NOT_LOADED, REASON_PAGE_PARTIAL_LOAD, REASON_BLOCKED_VERIFICATION,
    REASON_BLOCKED_ACCESS_DENIED, REASON_BLOCKED_CLOUDFLARE, REASON_REQUIRE_JAVASCRIPT,
    REASON_TIMEOUT, REASON_NETWORK_ERROR, extract_text_from_html, evaluate_content_health,
    detect_reason_from_status, detect_reason_from_text, PLAYWRIGHT_HEADLESS,
)
# 导入并发策略函数
from adapt_fetch.utils.concurrent_strategies import (
    crawl_with_concurrent_strategy,
    crawl_with_concurrent_strategy_no_jina
)

# 导入爬虫工具
from adapt_fetch.easy_crawler.easy_crawler import EasyGetCrawler
# 导入 Clash 代理管理器
from adapt_fetch.proxy.change_proxy import build_clash_proxy_manager
# 导入 PDF 爬虫工具
from adapt_fetch.pdf_crawler.pdf_crawler import PDFCrawler
from adapt_fetch.easy_pdf_crawler.easy_pdf_crawler import EasyPDFCrawler
from adapt_fetch.jina.jina_router import crawl_single_url as jina_crawl_single_url, jina_proxy_pool

# 🔧 事件循环策略由 start_unified.py 统一控制
# 不再在此处硬编码设置，避免覆盖全局配置
# 如果需要检查当前策略，可以通过 asyncio.get_event_loop_policy() 获取

# 配置日志
logger = logging.getLogger(__name__)

try:
    from .playwright_router_helper import resolve_playwright_edge_user_data_dir
except ImportError:

    def resolve_playwright_edge_user_data_dir() -> str:
        """helper 未同步时兜底：固定 %LOCALAPPDATA%\\LightReadPlaywrightEdge（与 helper 内逻辑一致）。"""
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            path = os.path.join(base, "LightReadPlaywrightEdge")
        else:
            path = os.path.join(os.path.expanduser("~"), ".local", "share", "lightread-playwright-edge")
        os.makedirs(path, exist_ok=True)
        logger.info("📁 Playwright Edge 爬虫用户数据目录（helper 缺 resolve_* 时的本地兜底）: %s", path)
        return path


# 记录当前事件循环策略（仅用于日志确认，不设置策略）
if sys.platform == 'win32':
    try:
        policy = asyncio.get_event_loop_policy()
        policy_name = policy.__class__.__name__ if policy else "Unknown"
        logger.debug(f"🔁 PlaywrightRouter 检测到事件循环策略: {policy_name} (由 start_unified.py 统一控制)")
    except Exception:
        pass  # 忽略检查错误，不影响主流程

# 路由器级别变量
crawler = None


class TimeoutManager:
    """统一的超时管理器，确保在达到超时时间时立即关闭页面和停止操作"""
    
    def __init__(self):
        self.active_tasks = {}  # 存储活跃的任务和页面
        self.timeout_handlers = {}  # 存储超时处理器
    
    def register_task(self, task_id: str, page: Page, timeout_ms: int, related_tasks: list = None):
        """注册一个需要超时管理的任务"""
        if task_id in self.active_tasks:
            # 如果已存在，先清理旧的
            self.cleanup_task(task_id)
        
        self.active_tasks[task_id] = {
            'page': page,
            'start_time': time.time(),
            'timeout_ms': timeout_ms,
            'cancelled': False,
            'related_tasks': related_tasks or []  # 存储相关的异步任务
        }
        
        # 创建超时处理器
        async def timeout_handler():
            await asyncio.sleep(timeout_ms / 1000.0)
            if task_id in self.active_tasks and not self.active_tasks[task_id]['cancelled']:
                logger.warning(f"⏰ 任务 {task_id} 达到超时时间 {timeout_ms}ms，强制关闭页面")
                await self.force_cleanup_task(task_id)
        
        # 启动超时处理器
        timeout_task = asyncio.create_task(timeout_handler())
        self.timeout_handlers[task_id] = timeout_task
        
        logger.debug(f"📋 注册超时管理任务: {task_id} (超时: {timeout_ms}ms)")
    
    async def force_cleanup_task(self, task_id: str):
        """强制清理任务和关闭页面"""
        if task_id not in self.active_tasks:
            return
        
        task_info = self.active_tasks[task_id]
        page = task_info['page']
        related_tasks = task_info.get('related_tasks', [])
        
        try:
            task_info['cancelled'] = True
            
            # 🔥 取消所有相关的异步任务
            for task in related_tasks:
                if not task.done():
                    task.cancel()
                    logger.debug(f"🛑 取消相关异步任务: {task_id}")
            
            # 强制关闭页面
            if page and not page.is_closed():
                logger.info(f"🛑 强制关闭页面: {task_id}")
                await page.close()
                
        except Exception as e:
            logger.debug(f"强制关闭页面时出错 {task_id}: {e}")
        finally:
            # 清理记录
            self.cleanup_task(task_id)
    
    def cleanup_task(self, task_id: str):
        """清理任务记录"""
        if task_id in self.active_tasks:
            self.active_tasks[task_id]['cancelled'] = True
            del self.active_tasks[task_id]
        
        if task_id in self.timeout_handlers:
            timeout_task = self.timeout_handlers[task_id]
            if not timeout_task.done():
                timeout_task.cancel()
            del self.timeout_handlers[task_id]
        
        logger.debug(f"🧹 清理超时管理任务: {task_id}")
    
    def is_task_cancelled(self, task_id: str) -> bool:
        """检查任务是否已被取消"""
        if task_id not in self.active_tasks:
            return True
        return self.active_tasks[task_id]['cancelled']
    
    def get_remaining_time(self, task_id: str) -> float:
        """获取任务剩余时间（秒）"""
        if task_id not in self.active_tasks:
            return 0.0
        
        task_info = self.active_tasks[task_id]
        elapsed_ms = (time.time() - task_info['start_time']) * 1000
        remaining_ms = task_info['timeout_ms'] - elapsed_ms
        return max(0.0, remaining_ms / 1000.0)


class PlaywrightCrawler:
    """Playwright爬虫类 - 完整功能版本"""
    
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.playwright = None
        self.edge_executable_path = None
        self.edge_user_data_dir = None
        self._is_closing = False
        self._current_config = None
        self._initialization_count = 0
        self._using_persistent_context = False
        
        # 🔥 新增：统一的超时管理器
        self.timeout_manager = TimeoutManager()
        
        # 占位页配置（用于持久化上下文在无任务时保持一个轻量标签页，避免窗口被关闭）
        self.placeholder_url = "https://zhuanlan.zhihu.com/p/3210586096"
        self._placeholder_page: Optional[Page] = None

        # 内存维护：周期性触发 GC/清理缓存/清理 about:blank
        self._memory_trim_interval_sec = 45  # 秒
        self._memory_task: Optional[asyncio.Task] = None

        # 设置PDF下载目录和静态URL前缀
        # Windows桌面路径
        desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
        self.pdf_download_dir = os.path.join(desktop_path, "STATIC", "RESOURCE", "PDF")
        self.static_url_base = "/PDF"
        
        # 确保下载目录存在
        os.makedirs(self.pdf_download_dir, exist_ok=True)
        logger.info(f"📁 PDF下载目录: {self.pdf_download_dir}")
        logger.info(f"🔗 静态URL前缀: {self.static_url_base}")
        
        # 爬虫实例 - 先初始化PDF处理器，再传给EasyGet
        self.pdf_crawler = PDFCrawler(download_dir=self.pdf_download_dir, static_url_base=self.static_url_base)
        self.easy_pdf_crawler = EasyPDFCrawler(download_dir=self.pdf_download_dir, static_url_base=self.static_url_base)
        self.easy_crawler = EasyGetCrawler(pdf_handler=self.easy_pdf_crawler)  # 传入PDF处理器
        self.proxy_manager = build_clash_proxy_manager(
            "playwright_crawl_7899",
        )
        self.smart_detector = SmartModeDetector()
        
        # 资源阻塞和JavaScript配置
        self.resource_blocking_enabled = True
        self._enable_javascript = True
        # 初始化进行中的标志，配合异步等待避免并发重复初始化
        self._initializing = False
        
        # ⚠️ 新增：初始化锁，防止并发情况下重复启动浏览器导致 "Target page, context or browser has been closed" 错误
        self._init_lock = asyncio.Lock()
        
        logger.info("✅ PlaywrightCrawler 初始化完成（包含统一超时管理器）")
        logger.info("🔥 X网站特殊优化：x.com/twitter.com/twimg.com/t.co 域名资源全部放行，智能等待支持连接错误恢复")
        
    async def initialize(self, use_edge_user_data: bool = True, 
                        enable_javascript: bool = True, user_agent: str = None, fast_mode: bool = False):
        """初始化浏览器 - 强制使用 persistent context"""
        # --- 并发保护：若已有协程在进行初始化，则等待其完成，避免重复启动浏览器 ---
        if self._initializing:
            while self._initializing:
                await asyncio.sleep(0.05)
            # 等待结束后，如果浏览器上下文已可用，则直接返回
            if self.context:
                return

        # 标记正在初始化
        self._initializing = True

        try:
            self._initialization_count += 1
            self._enable_javascript = enable_javascript
            logger.info(f"🔄 第 {self._initialization_count} 次初始化请求 - 快速模式: {fast_mode}")
            
            # 初始化 EasyGet 爬虫（容错：防止外部依赖抛出 KeyError 等导致整体失败）
            try:
                await self.easy_crawler.initialize()
            except KeyError as e:
                logger.warning(f"EasyGet 初始化失败，缺少键: {e}，将继续仅使用 Playwright 路径")
            except Exception as e:
                logger.warning(f"EasyGet 初始化失败: {e}，将继续仅使用 Playwright 路径")
            
            # 如果是快速模式，只初始化EasyGet，不需要Playwright
            if fast_mode:
                logger.info("⚡ 快速模式：将使用 EasyGet HTTP 爬虫，跳过 Playwright 初始化")
                return

            # 检查是否已经在清理中，如果是则快速退出
            if self._is_closing:
                logger.info("🛑 检测到清理状态，跳过浏览器初始化")
                return

            # ✅ 修复：检查现有浏览器状态，如果不健康则清理重建
            try:
                if self.context:
                    # 检查上下文是否仍然有效
                    try:
                        # 尝试访问上下文的页面列表来验证可用性
                        pages = self.context.pages
                        if pages is None:
                            logger.warning("⚠️ 上下文页面列表不可访问，需要重新初始化")
                            await self._force_cleanup_browser()
                        else:
                            return
                    except Exception as e:
                        logger.warning(f"⚠️ 检查现有上下文失败: {e}，需要重新初始化")
                        await self._force_cleanup_browser()
            except Exception as e:
                logger.error(f"❌ 检查现有浏览器状态时出错: {e}")

            # 启动Playwright和浏览器
            if not self.playwright:
                self.playwright = await async_playwright().start()
                
                # 再次检查清理状态
                if self._is_closing:
                    logger.info("🛑 Playwright启动后检测到清理状态")
                    return
            
            # 获取Edge路径和配置
            if not self.edge_executable_path:
                self.edge_executable_path = get_edge_executable_path()

            # 每次新建持久化上下文前解析 user_data（固定独立目录，见 playwright_router_helper）
            self.edge_user_data_dir = resolve_playwright_edge_user_data_dir()
            
            # 获取 User-Agent
            if not user_agent or user_agent.strip() == "":
                # 从智能检测器获取随机 User-Agent
                user_agent = self.smart_detector.get_random_user_agent()
                logger.info(f"🎲 使用随机 User-Agent: {user_agent}")
            else:
                logger.info(f"📝 使用指定 User-Agent: {user_agent}")
            
            # 准备浏览器参数（强制 persistent context）
            browser_args = get_browser_args(True, fast_mode, for_persistent_context=True, smart_detector=self.smart_detector)
            
            # 在启动浏览器前再次检查清理状态
            if self._is_closing:
                logger.info("🛑 浏览器启动前检测到清理状态")
                return
            
            # 强制使用持久化上下文启动浏览器
            # headless 模式由全局变量 PLAYWRIGHT_HEADLESS 控制，外部不应管理
            try:
                if self.edge_executable_path:
                    self.context = await self.playwright.chromium.launch_persistent_context(
                        user_data_dir=self.edge_user_data_dir,
                        executable_path=self.edge_executable_path,
                        headless=PLAYWRIGHT_HEADLESS,
                        args=browser_args,
                        user_agent=user_agent,
                        accept_downloads=True,
                        ignore_https_errors=True,
                        java_script_enabled=enable_javascript,
                        proxy={"server": "http://127.0.0.1:7899"}
                    )
                    logger.info(f"✅ Edge 持久化上下文启动成功（代理: http://127.0.0.1:7899，headless={PLAYWRIGHT_HEADLESS}）")
                else:
                    logger.warning("⚠️ 未找到Edge浏览器，使用默认Chromium持久化上下文")
                    self.context = await self.playwright.chromium.launch_persistent_context(
                        user_data_dir=self.edge_user_data_dir,
                        headless=PLAYWRIGHT_HEADLESS,
                        args=browser_args,
                        user_agent=user_agent,
                        accept_downloads=True,
                        ignore_https_errors=True,
                        java_script_enabled=enable_javascript,
                        proxy={"server": "http://127.0.0.1:7899"}
                    )
                    logger.info(f"✅ Chromium 持久化上下文启动成功（代理: http://127.0.0.1:7899，headless={PLAYWRIGHT_HEADLESS}）")
                
                # 浏览器启动后检查清理状态
                if self._is_closing:
                    logger.warning("🛑 浏览器启动完成后检测到清理状态，立即清理")
                    if self.context:
                        try:
                            await self.context.close()
                        except Exception as e:
                            logger.error(f"❌ 清理新创建的上下文时出错: {e}")
                        self.context = None
                    return
                
                self._using_persistent_context = True
                
                # 保存当前配置
                self._current_config = {
                    'use_edge_user_data': use_edge_user_data,
                    'edge_user_data_dir': self.edge_user_data_dir,
                    'headless': PLAYWRIGHT_HEADLESS,  # 使用全局变量
                    'enable_javascript': enable_javascript,
                    'user_agent': user_agent,
                    'fast_mode': fast_mode
                }
                
                logger.info("🎉 Playwright爬虫初始化完成（强制 persistent context）")
                
                # ⚠️ 重要：先创建占位页，再关闭 about:blank 页面
                # 否则关闭最后一个页面会导致 Edge 浏览器窗口自动关闭
                
                # 第一步：确保存在一个占位标签页，防止全部关闭后窗口消失
                try:
                    self._placeholder_page = await ensure_placeholder_tab(self.context, self.placeholder_url, self._placeholder_page, self._handle_resource_request)
                    if not self._placeholder_page:
                        logger.warning("⚠️ 占位页创建失败，返回值为 None")
                except Exception as _e:
                    logger.error(f"❌ 创建占位页失败: {_e}")
                
                # 第二步：关闭其他 about:blank 页面（占位页除外）
                try:
                    about_blank_count = 0
                    for _p in list(self.context.pages):
                        # 跳过占位页
                        if _p == self._placeholder_page:
                            continue
                        if _p and (not _p.is_closed()) and ((_p.url or '').startswith('about:')):
                            await _p.close()
                            about_blank_count += 1
                    if about_blank_count > 0:
                        logger.debug(f"🧹 初始化后清理了 {about_blank_count} 个 about:blank 页")
                except Exception as _e:
                    logger.debug(f"初始化后清理 about:blank 失败: {_e}")
                # 启动后台内存保洁器
                try:
                    self._ensure_memory_maintenance_started()
                except Exception as _e:
                    logger.debug(f"启动内存保洁器失败: {_e}")
                
            except Exception as browser_error:
                # 检查是否是清理导致的错误
                if self._is_closing:
                    logger.warning("🛑 浏览器启动过程中检测到清理状态，忽略启动错误")
                    return
                else:
                    logger.error(f"❌ 浏览器启动失败: {browser_error}")
                    # ✅ 修复：如果是浏览器已存在的错误，尝试强制清理后重试
                    error_msg = str(browser_error).lower()
                    if "target page, context or browser has been closed" in error_msg or "target closed" in error_msg:
                        logger.warning(f"⚠️ 检测到浏览器状态冲突，强制清理后重试: {browser_error}")
                        await self._force_cleanup_browser()
                        
                        # 重试一次
                        try:
                            if self.edge_executable_path:
                                self.context = await self.playwright.chromium.launch_persistent_context(
                                    user_data_dir=self.edge_user_data_dir,
                                    executable_path=self.edge_executable_path,
                                    headless=PLAYWRIGHT_HEADLESS,
                                    args=browser_args,
                                    user_agent=user_agent,
                                    accept_downloads=True,
                                    ignore_https_errors=True,
                                    java_script_enabled=enable_javascript,
                                    proxy={"server": "http://127.0.0.1:7899"}
                                )
                            else:
                                self.context = await self.playwright.chromium.launch_persistent_context(
                                    user_data_dir=self.edge_user_data_dir,
                                    headless=PLAYWRIGHT_HEADLESS,
                                    args=browser_args,
                                    user_agent=user_agent,
                                    accept_downloads=True,
                                    ignore_https_errors=True,
                                    java_script_enabled=enable_javascript,
                                    proxy={"server": "http://127.0.0.1:7899"}
                                )
                            
                            self._using_persistent_context = True
                            self._current_config = {
                                'use_edge_user_data': use_edge_user_data,
                                'edge_user_data_dir': self.edge_user_data_dir,
                                'headless': PLAYWRIGHT_HEADLESS,
                                'enable_javascript': enable_javascript,
                                'user_agent': user_agent,
                                'fast_mode': fast_mode
                            }
                            logger.info("✅ 重试后Playwright爬虫初始化成功")
                            
                        except Exception as retry_error:
                            logger.error(f"❌ 重试初始化也失败: {retry_error}")
                            raise retry_error
                    else:
                        # 其他错误直接抛出
                        raise browser_error
            
        except Exception as e:
            # 检查是否是清理相关的错误
            if self._is_closing:
                logger.info("🛑 初始化过程中检测到清理状态，忽略初始化错误")
                return
            
            logger.error(f"❌ 初始化Playwright爬虫失败: {e}")
            raise e
        finally:
            # 清理并发保护标志
            self._initializing = False

    async def _force_cleanup_browser(self):
        """强制清理浏览器资源（内部方法）"""
        try:
            # 清理上下文
            if self.context:
                try:
                    await self.context.close()
                except Exception as e:
                    logger.error(f"❌ 强制关闭上下文时出错: {e}")
                finally:
                    self.context = None

            # 清理浏览器（如果是普通模式）
            if self.browser and not self._using_persistent_context:
                try:
                    await self.browser.close()
                    logger.debug("✅ 强制关闭浏览器")
                except Exception as e:
                    logger.debug(f"强制关闭浏览器时出错: {e}")
                finally:
                    self.browser = None

            # 重置状态
            self._using_persistent_context = False
            self._current_config = None
            
            logger.info("✅ 强制清理浏览器资源完成")
            
        except Exception as e:
            logger.warning(f"❌ 强制清理浏览器资源时出错: {e}")

    async def _handle_resource_request(self, route):
        """处理资源请求 - 优化版：快速阻止 + 减少延迟"""
        try:
            url = route.request.url
            resource_type = route.request.resource_type
            
            # 🔥 X网站特殊处理：不拦截任何资源（包括CDN）
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.lower()
            # X网站及其CDN域名白名单
            x_domains = ['x.com', 'twitter.com', 'twimg.com', 't.co']
            is_x_site = any(x_domain in domain for x_domain in x_domains)
            
            if is_x_site:
                await route.continue_()
                return
            
            # 获取当前JavaScript启用状态
            enable_js = self._enable_javascript
            
            # 快速阻止：优先处理最常见的资源类型
            if resource_type in {'image', 'media', 'font', 'websocket', 'eventsource', 'manifest', 'other'}:
                await route.abort()
                return
            
            # CSS处理：JavaScript启用时允许CSS，禁用时阻止CSS
            if resource_type == 'stylesheet':
                if enable_js:
                    await route.continue_()
                    return
                else:
                    await route.abort()
                    return
            
            # 优化的URL检查：使用更快的字符串检查
            url_lower = url.lower()
            
            # 快速扩展名检查：CSS根据JS状态决定，其他一律阻止
            blocked_extensions = ['.jpg', '.png', '.gif', '.woff', '.woff2']
            if any(url_lower.endswith(ext) for ext in blocked_extensions):
                await route.abort()
                return
            
            # CSS扩展名检查：JavaScript启用时允许，禁用时阻止
            if url_lower.endswith('.css'):
                if enable_js:
                    await route.continue_()
                    return
                else:
                    await route.abort()
                    return
            
            # 处理JavaScript：简化逻辑
            if resource_type == 'script' or url_lower.endswith(('.js', '.min.js')):
                if enable_js:
                    # 快速广告/跟踪检查
                    if any(keyword in url_lower for keyword in ['analytics', 'ads', 'tracking', 'facebook.com', 'googlesyndication']):
                        await route.abort()
                        return
                    else:
                        await route.continue_()
                        return
                else:
                    await route.abort()
                    return
            
            # 快速关键词阻止（减少检查项）- 精确过滤
            if resource_type not in {'document', 'xhr', 'fetch'}:
                blocked_keywords = ['analytics', 'ads', 'tracking', 'audio', 'facebook.com', 'googlesyndication']
                # 阻止广告和跟踪相关资源，但不阻止内容相关的资源
                if any(keyword in url_lower for keyword in blocked_keywords):
                    await route.abort()
                    return
            
            # 允许关键资源通过
            if resource_type in {'document', 'xhr', 'fetch'}:
                await route.continue_()
            else:
                # 默认阻止其他类型
                await route.abort()
                
        except Exception as e:
            # 如果处理出错，快速阻止请求
            try:
                await route.abort()
            except:
                pass  # 忽略abort错误

    def _format_failure_markdown(self, url: str, easyget_error: Optional[str] = None, playwright_error: Optional[str] = None, jina_error: Optional[str] = None) -> str:
        """生成统一的失败 markdown 格式：URL + 失败原因（从错误信息中提取）"""
        parts = [f"# 爬取失败\n"]
        parts.append(f"**URL:** {url}\n")
        
        # 构建错误信息描述
        error_parts = []
        if easyget_error:
            error_parts.append(f"EasyGet: {easyget_error}")
        if playwright_error:
            error_parts.append(f"Playwright: {playwright_error}")
        if jina_error:
            error_parts.append(f"Jina: {jina_error}")
        
        if error_parts:
            parts.append(f"**失败原因:** {' | '.join(error_parts)}")
        else:
            parts.append("**失败原因:** 未知错误")
        
        return "\n".join(parts)
    
    def _normalize_entry_url(self, raw_url: str) -> str:
        """在发起导航前对URL进行协议及主机规范化，避免已知站点的HTTP失败"""
        if not raw_url:
            return raw_url

        try:
            parsed = urlparse(raw_url)
            scheme = parsed.scheme or 'http'
            netloc = parsed.netloc

            # Playwright 在访问 Zhihu 的 HTTP 入口时会收到 403，统一升级为 HTTPS
            if netloc.endswith('zhihu.com'):
                scheme = 'https'

                # zhihu.com 裸域会 301 到 www，直接提前补全
                if netloc == 'zhihu.com':
                    netloc = 'www.zhihu.com'

            if scheme == parsed.scheme and netloc == parsed.netloc:
                return raw_url

            normalized = parsed._replace(scheme=scheme, netloc=netloc)
            normalized_url = urlunparse(normalized)
            logger.debug(f"🔁 入口URL规范化: {raw_url} -> {normalized_url}")
            return normalized_url
        except Exception as e:
            logger.debug(f"URL规范化失败，继续使用原始URL: {raw_url} ({e})")
            return raw_url

    async def _call_html_cleaner_service_async(
        self,
        html_content: str,
        htmlclean_config: dict,
        crawl_config: Optional[CrawlRequest] = None,
    ) -> dict:
        """异步调用 HTML 清理服务（/process_html）。

        crawl_config 若提供，会将其中的 MapReduce / 分流参数写入请求体 options，对应
        crawl_demo_router：accelerated_threshold_mb、concurrency、target_kb、overlap_chars。
        """
        clean_start_time = time.time()
        try:
          
            # 1. 默认配置
            default_config = {
                "approach": "prune",
                "options": {
                    "ignore_links": False,
                    "ignore_images": False,
                    "escape_html": True,
                    "include_sup_sub": True,
                    "threshold": 0.5,
                    "threshold_type": "fixed"
                }
            }
            # 2. 递归合并
            final_config = copy.deepcopy(default_config)
            if htmlclean_config:
                final_config.update({k: v for k, v in htmlclean_config.items() if k != "options"})
                # 合并options
                if "options" in htmlclean_config:
                    final_config["options"].update(htmlclean_config["options"])

            # 3. CrawlRequest 顶层 MapReduce / 分流参数 → 写入 process_html options（覆盖同名字段）
            if crawl_config is not None:
                final_config["options"]["accelerated_threshold_mb"] = crawl_config.chunked_threshold_mb
                final_config["options"]["concurrency"] = crawl_config.chunk_concurrency
                final_config["options"]["target_kb"] = crawl_config.chunk_target_kb
                final_config["options"]["overlap_chars"] = crawl_config.chunk_overlap_chars
            
            # 4. approach 特殊处理
            approach = final_config.get("approach", "prune")
            if approach == "md25":
                if "user_query" not in final_config["options"]:
                    final_config["options"]["user_query"] = "main content"
                    final_config["options"]["bm25_threshold"] = 1.2
            
            payload = {
                "html": html_content,
                **{k: v for k, v in final_config.items() if k != "options"},
                "options": final_config["options"]
            }

            logger.debug(f"🧹 异步调用HTML清理服务: http://127.0.0.1:8900/process_html")

        
            # 创建超时配置
            timeout = aiohttp.ClientTimeout(total=60)
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    "http://127.0.0.1:8900/process_html",
                    json=payload,
                    headers={'Content-Type': 'application/json'},
                ) as response:
                    if response.status == 200:
                        try:
                            result = await response.json()
                        except Exception:
                            clean_time = time.time() - clean_start_time
                            logger.error(f"❌ HTML清理服务返回无法解析的JSON (耗时: {clean_time:.2f}s)")
                            return {"success": False, "error": "返回非法JSON", "clean_time": clean_time}
                        
                        if result.get('code') == 0:
                            markdown = result.get('fit_markdown') or result.get('raw_markdown')
                            if markdown:
                                clean_time = time.time() - clean_start_time
                                logger.info(f"✅ HTML清理成功，生成markdown长度: {len(markdown)} 字符，耗时: {clean_time:.2f}s")
                                return {"success": True, "fit_markdown": markdown, "clean_time": clean_time}
                            else:
                                clean_time = time.time() - clean_start_time
                                logger.warning(f"❌ HTML清理服务未返回有效的markdown内容 (耗时: {clean_time:.2f}s)")
                                return {"success": False, "error": "未返回有效的markdown内容", "clean_time": clean_time}
                        else:
                            clean_time = time.time() - clean_start_time
                            error_msg = result.get('msg', '未知错误')
                            logger.error(f"❌ HTML清理服务返回错误: {error_msg} (耗时: {clean_time:.2f}s)")
                            return {"success": False, "error": f"服务返回错误: {error_msg}", "clean_time": clean_time}
                    else:
                        clean_time = time.time() - clean_start_time
                        error_msg = f"HTTP {response.status}"
                        logger.error(f"❌ HTML清理服务请求失败: {error_msg} (耗时: {clean_time:.2f}s)")
                        return {"success": False, "error": f"请求失败: {error_msg}", "clean_time": clean_time}
                        
        except asyncio.TimeoutError:
            clean_time = time.time() - clean_start_time
            logger.error(f"❌ HTML清理服务请求超时 (耗时: {clean_time:.2f}s)")
            return {"success": False, "error": "请求超时", "clean_time": clean_time}
        except aiohttp.ClientConnectorError:
            clean_time = time.time() - clean_start_time
            logger.error(f"❌ 无法连接到HTML清理服务 (耗时: {clean_time:.2f}s)")
            return {"success": False, "error": "无法连接到清理服务", "clean_time": clean_time}
        except Exception:
            clean_time = time.time() - clean_start_time
            logger.exception(f"❌ HTML清理服务调用异常 (耗时: {clean_time:.2f}s)")
            return {"success": False, "error": "服务调用异常", "clean_time": clean_time}

    async def get_favicon_base64(self, page: Page, base_url: str) -> Optional[str]:
        """获取网站favicon并转换为base64编码"""
        favicon_start = time.time()
        try:
            logger.debug(f"🎨 开始获取favicon: {base_url}")
            
            async def get_favicon_with_timeout():
                # 1. 尝试从页面HTML中查找favicon链接
                favicon_urls = await page.evaluate("""
                    () => {
                        const links = [];
                        const selectors = [
                            'link[rel="icon"]',
                            'link[rel="shortcut icon"]', 
                            'link[rel="apple-touch-icon"]'
                        ];
                        
                        for (const selector of selectors) {
                            const elements = document.querySelectorAll(selector);
                            for (const el of elements) {
                                const href = el.getAttribute('href');
                                if (href) {
                                    links.push(href);
                                }
                            }
                        }
                        
                        return links.slice(0, 2);  // 最多只尝试2个
                    }
                """)
                
                # 2. 如果没找到，尝试默认的favicon.ico路径
                if not favicon_urls:
                    parsed_url = urlparse(base_url)
                    default_favicon = f"{parsed_url.scheme}://{parsed_url.netloc}/favicon.ico"
                    favicon_urls = [default_favicon]
                
                # 3. 快速尝试下载favicon
                for i, favicon_url in enumerate(favicon_urls[:2]):
                    try:
                        absolute_favicon_url = urljoin(base_url, favicon_url)
                        logger.debug(f"🔍 尝试获取favicon ({i+1}/2): {absolute_favicon_url}")
                        
                        response = await page.request.get(absolute_favicon_url, timeout=1000)
                        
                        if response.status == 200:
                            favicon_data = await response.body()
                            
                            if len(favicon_data) > 0:
                                # 简单处理，不进行复杂的图片处理
                                if favicon_data.startswith(b'\x89PNG'):
                                    mime_type = 'image/png'
                                elif favicon_data.startswith(b'\xff\xd8\xff'):
                                    mime_type = 'image/jpeg'
                                elif favicon_data.startswith(b'GIF'):
                                    mime_type = 'image/gif'
                                elif favicon_data.startswith(b'\x00\x00\x01\x00'):
                                    mime_type = 'image/x-icon'
                                else:
                                    mime_type = 'image/png'
                                
                                favicon_base64 = base64.b64encode(favicon_data).decode('utf-8')
                                data_uri = f"data:{mime_type};base64,{favicon_base64}"
                                
                                favicon_time = time.time() - favicon_start
                                logger.debug(f"✅ 成功获取favicon: {absolute_favicon_url} ({len(favicon_data)} bytes, {favicon_time:.2f}s)")
                                return data_uri
                                
                    except Exception as e:
                        logger.debug(f"获取favicon失败: {absolute_favicon_url}: {e}")
                        continue
                
                return None
            
            # favicon获取总超时2秒
            return await asyncio.wait_for(get_favicon_with_timeout(), timeout=2.0)
            
        except asyncio.TimeoutError:
            favicon_time = time.time() - favicon_start
            logger.debug(f"⏰ Favicon获取超时: {base_url} ({favicon_time:.2f}s)")
            return None
        except Exception as e:
            favicon_time = time.time() - favicon_start
            logger.debug(f"❌ Favicon获取异常: {base_url}: {e} ({favicon_time:.2f}s)")
            return None

    def _extract_title_from_html(self, html_content: str) -> str:
        """使用正则表达式从HTML中提取title (同步)"""
        try:
            title_match = re.search(r'<title[^>]*>(.*?)</title>', html_content, re.IGNORECASE | re.DOTALL)
            if title_match:
                title = title_match.group(1).strip()
                import html as _html
                title = _html.unescape(title)
                title = re.sub(r'\s+', ' ', title).strip()
                return title if title else '无标题'
            return '无标题'
        except Exception as e:
            logger.debug(f"从HTML提取title失败: {e}")
            return '无标题'

    async def _extract_title_from_html_async(self, html_content: str) -> str:
        """异步版本，避免在事件循环中解析大型 HTML 阻塞"""
        return await asyncio.to_thread(self._extract_title_from_html, html_content)

    async def _process_playwright_page_with_cancellation(self, page: Page, url: str, config: CrawlRequest, task_id: str = None, check_cancellation=None) -> Dict[str, Any]:
        """页面处理逻辑 - 支持取消检查"""
        if check_cancellation:
            await check_cancellation()
        
        return await self._process_playwright_page(page, url, config, task_id, check_cancellation)
    
    async def _process_playwright_page(self, page: Page, url: str, config: CrawlRequest, task_id: str = None, check_cancellation=None) -> Dict[str, Any]:
        """处理Playwright页面的公共逻辑"""
        try:
            # 🔥 检查超时状态
            if task_id and self.timeout_manager.is_task_cancelled(task_id):
                logger.info(f"🛑 任务 {task_id} 在页面处理开始时已被取消")
                error_msg = '任务已超时'
                failure_markdown = self._format_failure_markdown(url, playwright_error=error_msg)
                return {
                    'url': url,
                    'success': False,
                    'markdown': failure_markdown,
                    'playwright_error': error_msg  # 🔥 Playwright页面处理，放在 playwright_error
                }
            
            # 👉 新增：EasyGet 取消检查
            if check_cancellation:
                await check_cancellation()
            
            # 等待特定选择器
            if config.wait_for_selector:
                try:
                    # 🔥 使用剩余时间作为选择器等待时间
                    wait_timeout = 5000
                    if task_id:
                        remaining_time_ms = self.timeout_manager.get_remaining_time(task_id) * 1000
                        wait_timeout = min(5000, int(remaining_time_ms)) if remaining_time_ms > 0 else 5000
                    
                    await page.wait_for_selector(config.wait_for_selector, timeout=wait_timeout)
                except Exception as e:
                    logger.warning(f"⚠️ 等待选择器超时: {config.wait_for_selector}: {e}")
            
            # 🔥 再次检查超时状态
            if task_id and self.timeout_manager.is_task_cancelled(task_id):
                logger.info(f"🛑 任务 {task_id} 在选择器等待后被取消")
                error_msg = '任务已超时'
                failure_markdown = self._format_failure_markdown(url, playwright_error=error_msg)
                return {
                    'url': url,
                    'success': False,
                    'markdown': failure_markdown,
                    'playwright_error': error_msg  # 🔥 Playwright页面处理，放在 playwright_error
                }
            
            # 👉 再次检查 EasyGet 取消
            if check_cancellation:
                await check_cancellation()
            
            logger.info("🔍 开始建立CDP会话并停止页面加载")
            client_task = asyncio.create_task(self._safe_new_cdp_session(page))
            
            # 智能等待
            if hasattr(config, 'smart_wait_enabled') and config.smart_wait_enabled and config.enable_javascript:
                # 🔥 在智能等待前检查超时状态
                if task_id and self.timeout_manager.is_task_cancelled(task_id):
                    logger.info(f"🛑 任务 {task_id} 在智能等待前被取消")
                    error_msg = '任务已超时'
                    failure_markdown = self._format_failure_markdown(url, playwright_error=error_msg)
                    return {
                        'url': url,
                        'success': False,
                        'markdown': failure_markdown,
                        'playwright_error': error_msg  # 🔥 Playwright页面处理，放在 playwright_error
                    }

                # 👉 在智能等待前检查 EasyGet 取消
                if check_cancellation:
                    await check_cancellation()

                await self._wait_for_text_stable(page, url, config, task_id, check_cancellation)
            logger.info("✅ 智能等待完成")
            # 页面滚动
            if config.scroll_pages:
                await self.scroll_page(page, config.scroll_count, config.scroll_interval)
            logger.info("❄️ 页面正在冻结当中")
            # 创建并行任务：页面冻结 + HTML内容获取
            async def freeze_page_task():
                """页面冻结任务"""
                if config.freeze_page_after_wait:
                    try:
                        client = await client_task
                        if client:
                            logger.info("✅ CDP会话建立完成")
                            await client.send("Page.stopLoading")  # 立即停止剩余网络流水线
                            logger.info("❄️ 页面已冻结")
                        else:
                            logger.debug("CDP 会话创建失败或页面已关闭，跳过冻结")
                        return True
                    except Exception as e:
                        logger.error(f"页面冻结失败: {e}")
                        return False
                else:
                    logger.info("⏭️ 未启用页面冻结")
                    return True
            
            async def get_html_content_task():
                """获取HTML内容任务"""
                logger.info("🔍 开始获取HTML内容")
                logger.info("⏱️   cleaned & compressed HTML fetch start")
                t0 = time.time()

                # 使用 Chrome CDP DOM 方式抓取；若节点失效则优雅回退
                content = ''
                client = await self._safe_new_cdp_session(page)
                if client:
                    try:
                        # 可选启用DOM域（更稳健）
                        try:
                            await client.send("DOM.enable")
                        except Exception as _e:
                            logger.debug(f"DOM.enable 忽略异常: {_e}")
                        # 第一次尝试
                        doc = await client.send("DOM.getDocument", {"depth": -1, "pierce": False})
                        root_id = doc.get("root", {}).get("nodeId")
                        if root_id:
                            try:
                                outer = await client.send("DOM.getOuterHTML", {"nodeId": root_id})
                                content = outer.get("outerHTML", '') or ''
                            except Exception as e_outer:
                                # 节点可能在获取期间被替换，重试一次
                                if "Could not find node with given id" in str(e_outer):
                                    logger.debug("DOM.getOuterHTML 节点失效，重试获取 Document")
                                    doc = await client.send("DOM.getDocument", {"depth": -1, "pierce": False})
                                    root_id = doc.get("root", {}).get("nodeId")
                                    if root_id:
                                        try:
                                            outer = await client.send("DOM.getOuterHTML", {"nodeId": root_id})
                                            content = outer.get("outerHTML", '') or ''
                                        except Exception as e_outer2:
                                            logger.debug(f"DOM.getOuterHTML 重试仍失败，回退 page.content(): {e_outer2}")
                                else:
                                    logger.debug(f"DOM.getOuterHTML 异常，回退 page.content(): {e_outer}")
                        # 如果仍为空则回退
                        if not content:
                            content = await page.content()
                    except Exception as e_cdp:
                        logger.debug(f"CDP DOM 抓取异常，回退 page.content(): {e_cdp}")
                        content = await page.content()
                else:
                    # 无CDP会话：直接回退
                    content = await page.content()
                logger.info("⏱️   CDP DOM.getOuterHTML done %.2fs", time.time()-t0)
                
                # 🔥 简化：不再预先截断，交给 MapReduce 统一处理
                html_size_mb = len(content) / (1024 * 1024)
                if html_size_mb > 10.0:
                    logger.info(f"📊 获取到超大HTML: {html_size_mb:.2f}MB，将由 MapReduce 自动分块处理")
                
                logger.info("✅ HTML内容获取完成")
                return content
            
            # 并行执行页面冻结和HTML内容获取
            content_task = asyncio.create_task(get_html_content_task())
            freeze_task = asyncio.create_task(freeze_page_task())
            
            # 🔥 更新超时管理器，传入相关任务以便超时时取消
            if task_id and task_id in self.timeout_manager.active_tasks:
                self.timeout_manager.active_tasks[task_id]['related_tasks'] = [content_task, freeze_task]
            
            try:
                html_content = await content_task  # 只等待html内容获取完成
            except Exception as e:
                # 如果是页面关闭错误，取消所有相关任务
                if "Target page, context or browser has been closed" in str(e):
                    logger.info("🛑 Playwright页面已关闭，任务被取消")
                    # 取消所有进行中的任务
                    for task in [content_task, freeze_task]:
                        if not task.done():
                            task.cancel()
                    error_msg = '页面已关闭'
                    failure_markdown = self._format_failure_markdown(url, playwright_error=error_msg)
                    return {
                        'url': url,
                        'success': False,
                        'markdown': failure_markdown,
                        'playwright_error': error_msg  # 🔥 Playwright页面处理，放在 playwright_error
                    }
                else:
                    logger.error(f"❌ 页面处理失败: {e}")
                    raise
            
            # CDP会话清理是等到 await page.close()） 自动完成的；
            final_url = page.url
            # freeze_task 让其后台继续执行，不阻塞主流程
            logger.info(f"📋 HTML内容已获取，冻结任务状态: {freeze_task.done()}")
            
            logger.info(f"📋 并行任务完成 - 冻结结果: {freeze_task.done()}, HTML长度: {len(html_content)}")
            
            # 创建并行任务列表
            parallel_tasks = []

            # 1. HTML清理任务（取决于htmlclean_enabled开关）
            markdown_task = None
            if getattr(config, "htmlclean_enabled", True):
                # 启用HTML清理，转换为markdown
                markdown_task = asyncio.create_task(
                    self._clean_html_to_markdown(html_content, config)
                )
                parallel_tasks.append(('markdown', markdown_task))
            else:
                # 未启用HTML清理，直接返回html
                logger.debug("HTML清理已被禁用 (htmlclean_enabled=False)，将直接返回HTML内容")
            
            # 2. Title提取任务 (异步)
            if config.extract_title:
                # 🔥 预处理HTML：移除base64图片和script/style标签，减少解析负担
                cleaned_html_for_title = remove_script_style_tags(remove_base64_images(html_content))
                title_task = asyncio.create_task(
                    self._extract_title_from_html_async(cleaned_html_for_title)
                )
                parallel_tasks.append(('title', title_task))
            
            # 3. Favicon提取任务
            favicon_task = None
            if config.extract_icon:
                favicon_task = asyncio.create_task(
                    self.get_favicon_base64(page, final_url)
                )
                parallel_tasks.append(('favicon', favicon_task))
            
            # 等待所有并行任务完成
            results = {}
            for task_name, task in parallel_tasks:
                try:
                    results[task_name] = await task
                except Exception as e:
                    logger.warning(f"{task_name}任务失败: {e}")
                    results[task_name] = None
            
            # 组装结果
            result = {
                'url': url,
                'final_url': final_url,
                'success': True
            }
            
            # 🔥 根据是否启用HTML清理决定返回内容
            if getattr(config, "htmlclean_enabled", True):
                # 启用HTML清理，返回markdown，text_length是markdown长度
                markdown_content = results.get('markdown', {}).get('markdown', '')
                result.update({
                    'markdown': markdown_content,
                    'text_length': len(markdown_content.strip()),
                    'html_cleaning': results.get('markdown', {}).get('cleaning_info', {})
                })
            else:
                # 未启用HTML清理，返回html，text_length是html长度
                result.update({
                    'html': html_content,
                    'text_length': len(html_content.strip()) if html_content else 0
                })
            
            # 添加可选字段
            if config.extract_title:
                extracted_title = results.get('title')
                if extracted_title:
                    result['title'] = extracted_title
            
            if results.get('favicon'):
                result['favicon'] = results['favicon']
            
            return result
            
        except Exception as e:
            logger.error(f"❌ 页面处理失败: {e}")
            raise

    async def scroll_page(self, page: Page, scroll_count: int = 3, scroll_interval: float = 1.0) -> int:
        """滚动页面以加载动态内容"""
        try:
            # 如果滚动次数为0，直接返回当前页面高度
            if scroll_count == 0:
                logger.info("⏭️ 滚动次数为0，跳过滚动操作")
                try:
                    current_height = await page.evaluate("document.body ? document.body.scrollHeight : 0")
                except Exception as e:
                    logger.warning(f"⚠️ 获取页面高度失败: {str(e)}")
                    current_height = 0
                return current_height
            
            total_height = 0
            logger.info(f"🔄 开始滚动页面加载动态内容 - 滚动次数: {scroll_count}, 滚动间隔: {scroll_interval}秒")
            
            for i in range(scroll_count):
                # 获取当前页面高度，添加空值检查
                try:

                    prev_height = await page.evaluate("document.body ? document.body.scrollHeight : 0")
                except Exception as e:
                    logger.warning(f"⚠️ 获取页面高度失败: {str(e)}")
                    prev_height = 0
                
                # 优化滚动：使用更快的滚动方式，添加空值检查
                try:
                    await page.evaluate("document.body && window.scrollTo(0, document.body.scrollHeight)")
                except Exception as e:
                    logger.warning(f"⚠️ 滚动页面失败: {str(e)}")
                
                # 优化等待：先短暂等待，然后检查高度变化
                await asyncio.sleep(min(scroll_interval, 0.1))  # 最少等待0.1秒
                
                # 检查页面高度是否改变，添加空值检查
                try:
                    new_height = await page.evaluate("document.body ? document.body.scrollHeight : 0")
                except Exception as e:
                    logger.warning(f"⚠️ 获取页面高度失败: {str(e)}")
                    new_height = prev_height  # 如果获取失败，使用上一次的高度
                total_height = new_height
                
                logger.debug(f"第{i+1}次滚动: {prev_height} -> {new_height} (等待{scroll_interval}秒)")
                
                # 如果高度没有变化，说明已经加载完成
                if new_height == prev_height:
                    logger.info(f"✅ 页面高度未变化，提前停止滚动 (共滚动{i+1}次)")
                    break
                else:
                    # 如果高度有变化，继续等待剩余时间
                    remaining_wait = scroll_interval - 0.1
                    if remaining_wait > 0:
                        await asyncio.sleep(remaining_wait)
            
            # 滚动回顶部
            await page.evaluate("window.scrollTo(0, 0)")
            logger.info(f"📄 滚动完成，页面最终高度: {total_height}px")
            return total_height
            
        except Exception as e:
            logger.warning(f"❌ 滚动页面时出错: {str(e)}")
            return 0

    async def _wait_for_text_stable(self, page: Page, url: str, config: CrawlRequest, task_id: str = None, check_cancellation=None) -> Dict[str, Any]:
        """使用异步轮询等待页面文本内容稳定，可被取消"""
        wait_start = time.time()

        try:
            # 🔥 首先检查 EasyGet 取消/超时
            if check_cancellation:
                await check_cancellation()
            if task_id and self.timeout_manager.is_task_cancelled(task_id):
                logger.info(f"🛑 任务 {task_id} 在智能等待开始时已被取消")
                return {'success': False, 'wait_time': 0, 'error': '任务已超时'}

            # 动态参数配置
            min_chars = config.smart_wait_min_chars if config.smart_wait_min_chars is not None else 500
            idle_ms = config.smart_wait_idle_ms if config.smart_wait_idle_ms is not None else 600
            configured_timeout = config.smart_wait_timeout_ms if config.smart_wait_timeout_ms is not None else config.timeout
            if task_id:
                remaining_time_ms = self.timeout_manager.get_remaining_time(task_id) * 1000
                timeout_ms = min(configured_timeout, int(remaining_time_ms)) if remaining_time_ms > 0 else configured_timeout
            else:
                timeout_ms = configured_timeout
            delta_chars = config.smart_wait_delta_chars if config.smart_wait_delta_chars is not None else 10
            # 新增参数：连续稳定次数与零文本阈值
            stable_limit_times = getattr(config, 'smart_stable_limit_times', None)
            stable_limit_times = stable_limit_times if isinstance(stable_limit_times, int) and stable_limit_times > 0 else 3
            zero_text_threshold = getattr(config, 'smart_wait_zero_text_threshold', None)
            zero_text_threshold = zero_text_threshold if isinstance(zero_text_threshold, int) and zero_text_threshold > 0 else 12
            param_source = "用户配置" if any([
                getattr(config, 'smart_wait_min_chars', None),
                getattr(config, 'smart_wait_idle_ms', None),
                getattr(config, 'smart_wait_timeout_ms', None),
                getattr(config, 'smart_wait_delta_chars', None),
                getattr(config, 'smart_stable_limit_times', None),
                getattr(config, 'smart_wait_zero_text_threshold', None)
            ]) else "默认参数"

            # 检测是否是X网站（x.com / twitter.com）
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.lower()
            is_x_site = 'x.com' in domain or 'twitter.com' in domain
            
            logger.info(f"🔍 轮询检查参数({param_source}): 最小字符={min_chars}, 轮询间隔={idle_ms}ms, 超时={timeout_ms}ms, 变化阱值={delta_chars}字符, 连续稳定次数={stable_limit_times}, 零文本阈值={zero_text_threshold}")

            # --- 新实现：在浏览器侧用 MutationObserver + rAF 监控文本增长 ---
            logger.info("🧪 使用 MutationObserver 在浏览器端等待文本稳定…")
            if is_x_site:
                logger.info("🔥 X网站特殊优化已启用：检测到连接错误提示时将自动等待恢复")

            # 调用浏览器端脚本
            try:
                result_js = await page.evaluate(
                    """
                    (params) => {
                        const { minChars, deltaChars, timeoutMs, idleMs, stableLimitTimes, zeroTextThreshold, enableXSiteOptimization } = params;
                        return new Promise(resolve => {
                            const start = performance.now();
                            const getTextLength = () => (document.body ? (document.body.textContent || '').length : 0);
                            const getPageText = () => (document.body ? (document.body.textContent || '') : '');
                            let lastLen = getTextLength();
                            const initialLen = lastLen;
                            let checkCount = 0;
                            let stableCount = 0;
                            let zeroTextCount = (lastLen === 0) ? 1 : 0;
                            let connectivityWaitCount = 0;

                            // 选择观察目标；若 document.body 尚未就绪，则使用 document.documentElement
                            const targetNode = document.body || document.documentElement || document;
                            const observer = new MutationObserver(() => {}); // 仅用于触发微任务
                            observer.observe(targetNode, {subtree: true, childList: true, characterData: true});

                            const check = () => {
                                checkCount += 1;
                                const len = getTextLength();
                                const pageText = getPageText();
                                const delta = Math.abs(len - lastLen);

                                // 🔥 特殊优化：X网站连接错误检测（仅在启用时）
                                if (enableXSiteOptimization) {
                                    const hasConnectivityIssue = pageText.includes('Seems like you lost connectivity') || 
                                                                pageText.includes("We'll keep retrying") ||
                                                                pageText.includes('似乎失去了连接') ||
                                                                pageText.includes('我们会继续重试');
                                    
                                    if (hasConnectivityIssue) {
                                        connectivityWaitCount += 1;
                                        // 重置稳定计数，因为页面处于错误状态
                                        stableCount = 0;
                                        // 继续等待，不计入稳定状态
                                        lastLen = len;
                                        setTimeout(() => { requestAnimationFrame(check); }, Math.max(0, idleMs || 0));
                                        return;
                                    }
                                }

                                // 连续零文本计数
                                if (len === 0) {
                                    zeroTextCount += 1;
                                } else {
                                    zeroTextCount = 0;
                                }

                                // 稳定计数（排除纯零文本情况和第一次检查）
                                // 第一次检查时 checkCount=1，不应该计入稳定，因为没有"之前的状态"可以比较
                                if (checkCount > 1) {
                                    if (delta <= deltaChars && len > 0) {
                                        stableCount += 1;
                                    } else {
                                        stableCount = 0;
                                    }
                                }

                                // 达到零文本阈值：返回特殊原因
                                if (zeroTextCount >= zeroTextThreshold) {
                                    observer.disconnect();
                                    return resolve({reason: 'zero_text_threshold', waitTime: performance.now() - start, initialLen, finalLen: len, checkCount, stableCount, zeroTextCount, connectivityWaitCount});
                                }

                                // 必须同时达到最小字符数且连续稳定才退出
                                if (len >= minChars && stableCount >= stableLimitTimes) {
                                    observer.disconnect();
                                    return resolve({reason: 'stable', waitTime: performance.now() - start, initialLen, finalLen: len, checkCount, stableCount, zeroTextCount, connectivityWaitCount});
                                }

                                if (performance.now() - start > timeoutMs) {
                                    observer.disconnect();
                                    return resolve({reason: 'forced_timeout', waitTime: performance.now() - start, initialLen, finalLen: len, checkCount, stableCount, zeroTextCount, connectivityWaitCount});
                                }

                                lastLen = len;
                                setTimeout(() => { requestAnimationFrame(check); }, Math.max(0, idleMs || 0));
                            };

                            setTimeout(() => { requestAnimationFrame(check); }, Math.max(0, idleMs || 0));
                        });
                    };
                    """,
                    {
                        "minChars": min_chars,
                        "deltaChars": delta_chars,
                        "timeoutMs": timeout_ms,
                        "idleMs": idle_ms,
                        "stableLimitTimes": stable_limit_times,
                        "zeroTextThreshold": zero_text_threshold,
                        "enableXSiteOptimization": is_x_site
                    }
                )

                wait_time = result_js.get('waitTime', 0) / 1000  # 转成秒
                growth_ratio = (result_js.get('finalLen', 0) - result_js.get('initialLen', 0)) / max(result_js.get('initialLen', 1), 1)
                connectivity_wait_count = result_js.get('connectivityWaitCount', 0)

                # 构建日志信息
                log_msg = f"🧪 智能等待完成，reason={result_js.get('reason')}, checks={result_js.get('checkCount')}, stable_count={result_js.get('stableCount')}, zero_text_count={result_js.get('zeroTextCount')}"
                if is_x_site and connectivity_wait_count > 0:
                    log_msg += f", connectivity_wait={connectivity_wait_count}次"
                log_msg += f", wait_time={wait_time:.2f}s"
                logger.info(log_msg)

                return {
                    'success': True,
                    'wait_time': wait_time,
                    'initial_length': result_js.get('initialLen'),
                    'final_length': result_js.get('finalLen'),
                    'check_count': result_js.get('checkCount'),
                    'stable_count': result_js.get('stableCount'),
                    'zero_text_count': result_js.get('zeroTextCount'),
                    'connectivity_wait_count': connectivity_wait_count,
                    'growth_ratio': growth_ratio,
                    'reason': result_js.get('reason'),
                    'config_used': {
                        'min_chars': min_chars,
                        'idle_ms': idle_ms,
                        'timeout_ms': timeout_ms,
                        'delta_chars': delta_chars,
                        'stable_limit_times': stable_limit_times,
                        'zero_text_threshold': zero_text_threshold,
                        'param_source': param_source
                    }
                }

            except Exception as e:
                wait_time = time.time() - wait_start
                logger.warning(f"❌ 浏览器端文本稳定检查异常: {str(e)} (耗时{wait_time:.2f}s)")
                return {
                    'success': False,
                    'wait_time': wait_time,
                    'error': str(e)
                }

        except asyncio.CancelledError:
            # 传播取消
            raise
        except Exception as e:
            wait_time = time.time() - wait_start
            logger.warning(f"❌ 轮询检查异常: {str(e)} (耗时{wait_time:.2f}s)")
            return {
                'success': False,
                'wait_time': wait_time,
                'error': str(e)
            }

    async def crawl_single_url(self, url: str, config: CrawlRequest) -> Dict[str, Any]:
        """爬取单个URL - 重新设计的并发策略"""
        start_time = time.time()
        
        try:
            # 检查是否为PDF专用模式
            if config.crawl_type == "pdf":
                return await self._handle_pdf_crawl(url, config)
            
            # --- Normal / Fast / Playwright / Jina / No_Jina 模式分发 ---
            if config.mode == "fast":
                # 仅 EasyGet
                return await self._crawl_with_easyget_only(url, config)

            if config.mode == "playwright":
                # Playwright 专用模式
                asyncio.create_task(self._rotate_proxy_for_request(reason=f"url={url}"))
                return await self._crawl_with_playwright_only(url, config)

            if config.mode == "jina":
                # 仅 Jina
                asyncio.create_task(self._rotate_proxy_for_request(reason=f"url={url}"))
                return await self._crawl_with_jina_only(url, config)

            if config.mode == "no_jina":
                # 并发 EasyGet + Playwright（不包含 Jina）
                asyncio.create_task(self._rotate_proxy_for_request(reason=f"url={url}"))
                return await crawl_with_concurrent_strategy_no_jina(self, url, config)

            # 其余视为 normal 模式（并发策略 + 智能缓存）
            # 每次抓取前尝试异步切换一次代理（不阻塞主流程，不等待）
            asyncio.create_task(self._rotate_proxy_for_request(reason=f"url={url}"))

            if hasattr(config, 'use_intellicache') and config.use_intellicache:
                # 先查询 / 计算智能决策
                detection_result = await self.smart_detector.smart_detect_mode(url, self.easy_crawler, True)
                recommended_mode = detection_result.get('recommended_mode')

                if recommended_mode == 'pdf':
                    logger.info("🎯 缓存/检测结果: PDF -> 直接 Playwright PDF 流程")
                    return await self._handle_pdf_crawl(url, config)
                elif recommended_mode == 'easyget':
                    logger.info("🎯 缓存/检测结果: WEB-EasyGet -> 直接 EasyGet")
                    return await self._crawl_with_easyget_only(url, config, detection_result)
                elif recommended_mode == 'playwright':
                    logger.info("🎯 缓存/检测结果: WEB-Playwright -> 直接 Playwright")
                    return await self._crawl_with_playwright_only(url, config)
                elif recommended_mode == 'jina':
                    logger.info("🎯 缓存/检测结果: WEB-Jina -> 直接 Jina")
                    return await self._crawl_with_jina_only(url, config, detection_result)
                elif recommended_mode == 'blocked':
                    # 之前判定为拦截，直接返回错误信息
                    reason_str = detection_result.get('reason', 'URL 被拦截，无法抓取')
                    logger.warning(f"🚫 URL 已缓存为拦截状态: {reason_str}")
                    failure_markdown = self._format_failure_markdown(url, playwright_error=reason_str)
                    return {
                        'url': url,
                        'success': False,
                        'execution_time': time.time() - start_time,
                        'markdown': failure_markdown,
                        'text_length': len(failure_markdown),
                        'playwright_error': reason_str  # 🔥 智能检测通常涉及浏览器，放在 playwright_error
                    }
                else:
                    logger.info("ℹ️ 智能检测未命中，转入并发策略")
                    # 未命中或未知 -> 并发策略，并传入 detection_result 供学习
                    return await crawl_with_concurrent_strategy(self, url, config, detection_result)

            # use_intellicache = False 直接并发
            logger.info("ℹ️ use_intellicache=False，直接并发 EasyGet + Playwright + Jina")
            return await crawl_with_concurrent_strategy(self, url, config, None)
                
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"❌ 爬取URL失败 {url}: {e}")
            error_str = str(e)
            failure_markdown = self._format_failure_markdown(url, easyget_error=error_str, playwright_error=error_str, jina_error=error_str)
            # 🔥 顶层异常：由于不确定是哪个爬虫，将错误信息同时放入三个字段
            return {
                'url': url,
                'success': False,
                'execution_time': execution_time,
                'markdown': failure_markdown,
                'easyget_error': error_str,
                'playwright_error': error_str,
                'jina_error': error_str
            }

    async def _crawl_with_concurrent_strategy(self, url: str, config: CrawlRequest, detection_result: dict = None) -> Dict[str, Any]:
        """并发策略：同时尝试EasyGet、Jina和Playwright - 优雅取消版本
        
        注意：此方法是对 concurrent_strategies.crawl_with_concurrent_strategy 的包装，
        实际实现已移至 concurrent_strategies.py 模块以减少文件长度。
        """
        return await crawl_with_concurrent_strategy(self, url, config, detection_result)

    async def _crawl_with_concurrent_strategy_no_jina(self, url: str, config: CrawlRequest, detection_result: dict = None) -> Dict[str, Any]:
        """并发策略（不包含Jina）：同时尝试EasyGet和Playwright - 优雅取消版本
        
        注意：此方法是对 concurrent_strategies.crawl_with_concurrent_strategy_no_jina 的包装，
        实际实现已移至 concurrent_strategies.py 模块以减少文件长度。
        """
        return await crawl_with_concurrent_strategy_no_jina(self, url, config, detection_result)

    async def _handle_pdf_crawl(self, url: str, config: CrawlRequest) -> Dict[str, Any]:
        """处理PDF爬取"""
        start_time = time.time()
        page = None
        task_id = f"pdf_{url}_{int(time.time() * 1000)}"
        
        try:
            logger.info(f"📄 开始PDF专用爬取: {url}")
            
            # 确保浏览器已初始化
            await self.initialize(
                use_edge_user_data=config.use_edge_user_data,
                enable_javascript=config.enable_javascript,
                user_agent=config.user_agent,
                fast_mode=False
            )
            
            page = await self.context.new_page()
            
            # 🔥 注册超时管理
            self.timeout_manager.register_task(task_id, page, config.timeout)
            
            try:
                # 设置额外请求头
                if config.extra_headers:
                    await page.set_extra_http_headers(config.extra_headers)
                
                # 🔥 检查超时状态
                if self.timeout_manager.is_task_cancelled(task_id):
                    logger.info(f"🛑 PDF任务 {task_id} 在下载前已被取消")
                    error_msg = '任务已超时'
                    failure_markdown = self._format_failure_markdown(url, playwright_error=error_msg)
                    return {
                        'url': url,
                        'success': False,
                        'markdown': failure_markdown,
                        'playwright_error': error_msg  # 🔥 PDF爬取使用Playwright，放在 playwright_error
                    }
                
                # 使用剩余时间限制PDF下载超时
                remaining_time_ms = self.timeout_manager.get_remaining_time(task_id) * 1000
                effective_timeout = min(config.timeout, int(remaining_time_ms)) if remaining_time_ms > 0 else config.timeout
                
                # 获取自动下载PDF设置，默认为True
                auto_download_pdf = getattr(config, 'auto_download_pdf', True)
                logger.info(f"📄 PDF自动下载设置: {'启用' if auto_download_pdf else '禁用'}")
                
                # 使用EasyPDFCrawler下载PDF，传递auto_download参数
                if auto_download_pdf:
                    # 直接使用EasyPDFCrawler下载PDF
                    result = await self.easy_pdf_crawler.download_pdf_with_page(
                        page=page,
                        url=url,
                        timeout=effective_timeout
                    )
                else:
                    # 使用PDFCrawler只检测不下载
                    result = await self.pdf_crawler.process_pdf_page(
                        page=page,
                        url=url,
                        auto_download=False,
                        download_timeout=effective_timeout
                    )
                
                # 处理结果
                if result.get("success"):
                    if auto_download_pdf:
                        # 提取PDF文本内容并转换为markdown格式
                        text_content = result.get('text', '')  # 这里text字段仅作中间变量，不返回
                        pdf_url = result.get('final_url', url)
                        title = result.get('title', '未知PDF文档')
                        
                        # 获取PDF文件路径和静态URL
                        file_path = result.get('file_path', '')
                        static_url = result.get('static_url', '')
                        filename = result.get('filename', title)
                        
                        # 生成markdown格式的内容 - 只包含PDF的实际文本内容
                        if text_content:
                            markdown_content = text_content
                        else:
                            markdown_content = "*PDF文本提取失败或内容为空*"
                        
                        return {
                            'url': url,
                            'final_url': pdf_url,
                            'title': title,
                            'success': True,
                            'execution_time': time.time() - start_time,
                            'actual_crawler': 'pdf',
                            'mode': 'pdf',
                            'markdown': markdown_content,
                            'text_length': len(markdown_content.strip()),
                            'crawl_type_used': 'pdf',
                            'pdf_processing': True,
                            'pdf_downloaded': True,
                            'file_path': file_path,  # 添加文件路径
                            'static_url': static_url,  # 添加静态URL
                            'filename': filename,  # 添加文件名
                            'file_size': result.get('file_size', 0),  # 添加文件大小
                            'file_size_mb': result.get('file_size_mb', 0)  # 添加文件大小(MB)
                        }
                    else:
                        # 只检测不下载的结果
                        return {
                            'url': url,
                            'success': True,
                            'execution_time': time.time() - start_time,
                            'actual_crawler': 'pdf',
                            'mode': 'pdf',
                            'markdown': "*PDF页面检测成功，但未下载内容*",
                            'text_length': len("*PDF页面检测成功，但未下载内容*"),
                            'crawl_type_used': 'pdf',
                            'pdf_processing': False,
                            'pdf_downloaded': False
                        }
                else:
                    error_msg = result.get('error', '未知错误')
                    logger.error(f"❌ PDF处理失败: {url}")
                    failure_markdown = self._format_failure_markdown(url, playwright_error=error_msg)
                    return {
                        'url': url,
                        'success': False,
                        'execution_time': time.time() - start_time,
                        'markdown': failure_markdown,
                        'text_length': len(failure_markdown),
                        'crawl_type_used': 'pdf',
                        'pdf_processing': False,
                        'pdf_downloaded': False,
                        'playwright_error': error_msg  # 🔥 PDF爬取使用Playwright，放在 playwright_error
                    }
                
            finally:
                # 🔥 清理超时管理器中的任务记录
                self.timeout_manager.cleanup_task(task_id)
                
                if page:
                    try:
                        if not page.is_closed():
                            await page.close()
                    except Exception as e:
                        logger.debug(f"关闭PDF页面时出错: {e}")
            
        except Exception as e:
            logger.error(f"❌ PDF爬取异常: {url}: {e}")
            error_str = str(e)
            failure_markdown = self._format_failure_markdown(url, playwright_error=error_str)
            return {
                'url': url,
                'success': False,
                'execution_time': time.time() - start_time,
                'markdown': failure_markdown,
                'text_length': len(failure_markdown),
                'mode': 'pdf',
                'actual_crawler': 'pdf',
                'crawl_type_used': 'pdf',
                'pdf_downloaded': False,
                'playwright_error': error_str  # 🔥 PDF爬取异常使用Playwright，放在 playwright_error
            }

    async def _crawl_with_easyget_only(self, url: str, config: CrawlRequest, detection_result=None) -> Dict[str, Any]:
        """仅使用EasyGet爬虫"""
        start_time = time.time()
        
        try:
            logger.info(f"⚡ EasyGet专用模式: {url}")
            
            # 确保参数类型正确，避免 dict 和 int 比较错误
            timeout_seconds = int(config.timeout) // 1000 if isinstance(config.timeout, int) else 30
            max_redirects = int(config.max_redirects) if config.max_redirects is not None else 10
            
            # 修正参数传递 - 使用正确的参数名和格式  
            
            result = await self.easy_crawler.crawl_single_url(
                url=url,
                timeout=timeout_seconds,
                user_agent=config.user_agent,
                extra_headers=config.extra_headers or {},
                follow_redirects=config.follow_redirects,
                max_redirects=max_redirects,
                verify_ssl=config.verify_ssl,
                encoding=config.encoding,
                use_edge_cookies=config.use_edge_cookies,
                edge_profile=config.edge_profile,
                target_domains=config.target_domains or [],
                custom_cookies=config.custom_cookies or {},
                extract_title=config.extract_title,
                extract_icon=config.extract_icon,
                htmlclean_enabled=config.htmlclean_enabled,
                text_limit=config.text_limit,
                chunked_threshold_mb=config.chunked_threshold_mb,
                chunk_target_kb=config.chunk_target_kb,
                chunk_overlap_chars=config.chunk_overlap_chars,
                chunk_concurrency=config.chunk_concurrency
            )
            
            # 🔥 修复: 确保result不为None，如果是则创建一个包含错误信息的结果对象
            if result is None:
                logger.error(f"❌ EasyGet返回了None结果: {url}")
                result = {
                    'success': False,
                    'error': 'EasyGet返回了空结果',
                    'final_url': url
                }
            
            # 🔥 新增：检查是否是PDF页面，如果是直接返回完整结果，保留PDF相关字段
            if result.get('is_pdf_page') and result.get('success'):
                logger.info(f"📄 EasyGet成功处理PDF，直接返回完整结果（包含file_path和static_url）")
                # 直接返回PDF结果，只添加必要的执行信息
                result.update({
                    'execution_time': time.time() - start_time,
                    'actual_crawler': 'easyget_pdf',
                    'mode': 'easyget_only→pdf',
                    'smart_detection': detection_result
                })
                return result
            
            # 🔥 构建返回结果，根据htmlclean_enabled决定返回格式
            return_result = {
                'url': url,
                'final_url': result.get('final_url', url),
                'success': result.get('success', False),
                'execution_time': time.time() - start_time,
                'actual_crawler': 'easyget',
                'mode': 'easyget_only',
                'smart_detection': detection_result
            }

            # 添加title字段（如果提取成功）
            if config.extract_title and result.get('title'):
                return_result['title'] = result['title']
            
            if config.htmlclean_enabled:
                # 调用内部清理服务，将 html 转换为 markdown
                html_content = result.get('html', '')
                use_readability = getattr(config, 'use_readability', False)
                cleaner_name = 'Readability' if use_readability else 'HTML清理服务'
                logger.info(f"🧹 使用{cleaner_name}清理HTML...")
                try:
                    clean_ret = await self._clean_html_to_markdown(html_content, config)
                    if clean_ret.get('markdown'):
                        markdown_content = clean_ret['markdown']
                    else:
                        markdown_content = html_content  # 回退原始 html
                except Exception as _e:
                    logger.warning(f"{cleaner_name}失败或异常: {_e}")
                    markdown_content = html_content
                return_result.update({
                    'markdown': markdown_content,
                    'text_length': len(markdown_content.strip()) if markdown_content else 0
                })
            else:
                # EasyGet返回html，直接返回html，text_length是html长度
                html_content = result.get('html', '')
                return_result.update({
                    'html': html_content,
                    'text_length': len(html_content.strip()) if html_content else 0
                })
            # 健康检测：命中则置为失败并写入 reason
            try:
                status_code = result.get('status_code')
                min_text = getattr(config, 'text_limit', 100)
                return_result = self._apply_health_detection_to_result(return_result, status_code, min_text)
            except Exception as _e:
                logger.debug(f"EasyGet专用模式健康检测异常: {_e}")
            return return_result
            
        except Exception as e:
            logger.error(f"❌ EasyGet爬取失败: {e}")
            error_str = str(e)
            failure_markdown = self._format_failure_markdown(url, easyget_error=error_str)
            return {
                'url': url,
                'success': False,
                'execution_time': time.time() - start_time,
                'markdown': failure_markdown,
                'text_length': len(failure_markdown),
                'easyget_error': error_str  # 🔥 EasyGet专用模式失败，放在 easyget_error
            }

    async def _crawl_with_playwright_only(self, url: str, config: CrawlRequest) -> Dict[str, Any]:
        """仅使用Playwright爬虫"""
        start_time = time.time()
        page = None
        task_id = f"playwright_only_{url}_{int(time.time() * 1000)}"
        
        try:
            logger.info(f"🎭 Playwright专用模式: {url}")
            
            # 初始化Playwright
            await self.initialize(
                use_edge_user_data=config.use_edge_user_data,
                enable_javascript=config.enable_javascript,
                user_agent=config.user_agent,
                fast_mode=False
            )
            
            page = await self.context.new_page()
            
            # 🔥 注册超时管理
            self.timeout_manager.register_task(task_id, page, config.timeout)
            
            # 设置资源阻塞
            if self.resource_blocking_enabled:
                await page.route("**/*", self._handle_resource_request)
            
            # 设置额外请求头
            if config.extra_headers:
                await page.set_extra_http_headers(config.extra_headers)
            
            # 🔥 检查超时状态
            if self.timeout_manager.is_task_cancelled(task_id):
                logger.info(f"🛑 任务 {task_id} 在导航前已被取消")
                error_msg = '任务已超时'
                failure_markdown = self._format_failure_markdown(url, playwright_error=error_msg)
                return {
                    'url': url,
                    'success': False,
                    'markdown': failure_markdown,
                    'playwright_error': error_msg  # 🔥 Playwright专用模式，放在 playwright_error
                }
            
            # 导航到页面 - 使用剩余时间作为超时
            remaining_time_ms = self.timeout_manager.get_remaining_time(task_id) * 1000
            effective_timeout = min(config.timeout, int(remaining_time_ms)) if remaining_time_ms > 0 else config.timeout
            wait_state = config.wait_for_load_state or "commit"
            
            logger.info(f"🔄 开始导航页面 (wait_until={wait_state}, timeout={effective_timeout}ms): {url}")
            response = await page.goto(
                url, 
                wait_until=wait_state,
                timeout=effective_timeout
            )
            logger.info(f"✅ 页面导航完成: {url}")
            
            # 检查页面响应状态码
            if response:
                status_code = response.status
                if status_code >= 400:
                    error_msg = f"页面访问失败，状态码: {status_code}"
                    if status_code == 404:
                        error_msg += " (页面不存在)"
                    elif status_code == 403:
                        error_msg += " (访问被禁止)"
                    elif status_code >= 500:
                        error_msg += " (服务器错误)"
                    
                    logger.warning(f"⚠️ {error_msg}: {url}")
                    await page.close()
                    
                    failure_markdown = self._format_failure_markdown(url, playwright_error=error_msg)
                    ret_err = {
                        'url': url,
                        'success': False,
                        'execution_time': time.time() - start_time,
                        'status_code': status_code,
                        'markdown': failure_markdown,
                        'text_length': len(failure_markdown),
                        'playwright_error': error_msg  # 🔥 Playwright专用模式，放在 playwright_error
                    }
                    return ret_err
            
            # 检测是否为PDF
            final_url = page.url
            is_pdf = await self._detect_pdf_from_response(response, final_url, page)
            
            if is_pdf:
                logger.info(f"📄 检测到PDF页面: {final_url}")
                # 直接使用当前页面通过 request 下载 PDF，避免再次打开新页面
                try:
                    remaining_ms = int(self.timeout_manager.get_remaining_time(task_id) * 1000)
                    if remaining_ms <= 0:
                        remaining_ms = config.timeout
                    pdf_ret = await self.easy_pdf_crawler.download_pdf_via_request(
                        page=page,
                        url=final_url,
                        timeout=remaining_ms
                    )
                    return pdf_ret
                finally:
                    try:
                        if page and not page.is_closed():
                            await page.close()
                    except Exception:
                        pass
            
            # Web页面处理
            logger.info("🌐 确认为Web页面，继续处理...")
            
            # 🔥 检查超时状态
            if self.timeout_manager.is_task_cancelled(task_id):
                logger.info(f"🛑 任务 {task_id} 在页面处理前已被取消")
                error_msg = '任务已超时'
                failure_markdown = self._format_failure_markdown(url, playwright_error=error_msg)
                return {
                    'url': url,
                    'success': False,
                    'markdown': failure_markdown,
                    'playwright_error': error_msg  # 🔥 Playwright专用模式，放在 playwright_error
                }
            
            # 使用公共页面处理逻辑
            page_result = await self._process_playwright_page(page, url, config, task_id)
            
            # 添加执行信息
            page_result.update({
                'execution_time': time.time() - start_time,
                'actual_crawler': 'playwright',
                'mode': 'playwright',
                'text_length': len((page_result.get('markdown', '') or '').strip())
            })
            # 健康检测
            try:
                status_code_for_health = response.status if response else None
                min_text = getattr(config, 'text_limit', 100)
                page_result = self._apply_health_detection_to_result(page_result, status_code_for_health, min_text)
            except Exception as _e:
                logger.debug(f"Playwright专用模式 健康检测异常: {_e}")
            
            return page_result
            
        except Exception as e:
            logger.error(f"❌ Playwright爬取失败: {e}")
            error_str = str(e)
            failure_markdown = self._format_failure_markdown(url, playwright_error=error_str)
            return {
                'url': url,
                'success': False,
                'execution_time': time.time() - start_time,
                'markdown': failure_markdown,
                'text_length': len(failure_markdown),
                'playwright_error': error_str  # 🔥 Playwright专用模式失败，放在 playwright_error
            }
        finally:
            # 🔥 清理超时管理器中的任务记录
            self.timeout_manager.cleanup_task(task_id)
            
            if page:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception as e:
                    logger.debug(f"关闭页面时出错: {e}")

    async def _crawl_with_jina_only(self, url: str, config: CrawlRequest, detection_result=None) -> Dict[str, Any]:
        """仅使用Jina爬虫（r.jina.ai）"""
        start_time = time.time()
        try:
            # 计算超时（秒）
            try:
                timeout_seconds = int(config.timeout) // 1000 if isinstance(config.timeout, int) else 30
                if timeout_seconds <= 0:
                    timeout_seconds = 30
            except Exception:
                timeout_seconds = 30
            # 从代理池获取代理
            proxy_url = await jina_proxy_pool.get_next()
            # 发起 Jina 抓取
            res = await jina_crawl_single_url(
                url=url,
                timeout=timeout_seconds,
                no_cache=False,
                ignore_imgs=False,
                ignore_links=False,
                proxy_url=proxy_url
            )
            if not isinstance(res, dict):
                raise RuntimeError("Jina 返回了非法结果类型")
            if not res.get('success'):
                err = res.get('error', '失败，未提供具体错误信息')
                failure_markdown = self._format_failure_markdown(url, jina_error=err)
                return {
                    'url': url,
                    'success': False,
                    'execution_time': time.time() - start_time,
                    'markdown': failure_markdown,
                    'text_length': len(failure_markdown),
                    'actual_crawler': 'jina',
                    'mode': 'jina',
                    'jina_error': err
                }
            # 成功 -> 组装结果
            markdown_text = res.get('text', '') or ''
            title = res.get('title')
            final_url = res.get('final_url', url)
            status_code = res.get('status_code')
            result = {
                'url': url,
                'final_url': final_url,
                'title': title if (getattr(config, 'extract_title', True) and title) else None,
                'success': True,
                'execution_time': time.time() - start_time,
                'actual_crawler': 'jina',
                'mode': 'jina',
                'smart_detection': detection_result,
                'markdown': markdown_text,
                'text_length': len(markdown_text.strip()),
                'status_code': status_code
            }
            # 健康检测（Jina 归入 HTTP 类）
            try:
                min_text = getattr(config, 'text_limit', 100)
                result = self._apply_health_detection_to_result(result, status_code, min_text)
            except Exception as _e:
                logger.debug(f"Jina-only 健康检测异常: {_e}")
            return result
        except Exception as e:
            error_str = str(e)
            failure_markdown = self._format_failure_markdown(url, jina_error=error_str)
            return {
                'url': url,
                'success': False,
                'execution_time': time.time() - start_time,
                'markdown': failure_markdown,
                'text_length': len(failure_markdown),
                'actual_crawler': 'jina',
                'mode': 'jina',
                'jina_error': error_str
            }

    async def _detect_pdf_from_response(self, response, final_url: str, page: Page) -> bool:
        """复杂的PDF检测流程"""
        try:
            # 1. 检查URL扩展名
            if final_url.lower().endswith('.pdf'):
                logger.debug("✅ PDF检测: URL以.pdf结尾")
                return True
            
            # 2. 检查Content-Type header
            content_type = response.headers.get('content-type', '').lower()
            if 'application/pdf' in content_type:
                logger.debug("✅ PDF检测: Content-Type为application/pdf")
                return True
            
            # 3. 检查Content-Disposition header
            content_disposition = response.headers.get('content-disposition', '').lower()
            if '.pdf' in content_disposition or 'application/pdf' in content_disposition:
                logger.debug("✅ PDF检测: Content-Disposition包含PDF信息")
                return True
            
            # 4. 检查页面内容是否包含PDF embed/object
            try:
                has_pdf_embed = await page.evaluate("""
                    () => {
                        // 检查PDF嵌入元素
                        const pdfElements = document.querySelectorAll('embed[type="application/pdf"], object[type="application/pdf"], iframe[src*=".pdf"]');
                        return pdfElements.length > 0;
                    }
                """)
                
                if has_pdf_embed:
                    logger.debug("✅ PDF检测: 页面包含PDF嵌入元素")
                    return True
            except Exception as e:
                logger.debug(f"PDF嵌入元素检测失败: {e}")
            
            # 5. 使用智能检测器的PDF URL检测
            if self.smart_detector.is_pdf_url(final_url):
                logger.debug("✅ PDF检测: 智能检测器确认为PDF URL")
                return True
            
            # 6. 检查状态码和其他指标
            if response.status == 200:
                # 对于application/octet-stream，进行魔数检测
                if 'application/octet-stream' in content_type:
                    try:
                        magic_result = await self.easy_pdf_crawler.perform_magic_number_check(page, final_url)
                        if magic_result.get('success') and magic_result.get('is_pdf'):
                            logger.debug("✅ PDF检测: 魔数校验确认为PDF")
                            return True
                    except Exception as e:
                        logger.debug(f"魔数检测失败: {e}")
            
            logger.debug("❌ PDF检测: 所有检测都表明不是PDF")
            return False
            
        except Exception as e:
            logger.warning(f"PDF检测异常: {e}")
            return False


    async def _clean_html_to_markdown(self, html_content: str, config: CrawlRequest) -> Dict[str, Any]:
        """HTML清理转markdown - 根据配置选择清理方法"""
        total_clean_start = time.time()
        try:
            if not html_content or len(html_content) < 10:
                total_clean_time = time.time() - total_clean_start
                logger.warning(f"⚠️ HTML内容为空或过短 (耗时: {total_clean_time:.3f}s)")
                return {
                    'markdown': '# 内容为空\n\n未获取到有效的HTML内容',
                    'cleaning_info': {
                        'enabled': True,
                        'success': False,
                        'error': 'HTML内容为空或过短',
                        'clean_time': total_clean_time
                    }
                }
            
            # 🔥 优先级1：检测超大HTML，强制MapReduce（忽略use_readability配置）
            huge_html = should_use_readability_for_huge_html(
                html_content,
                threshold_mb=getattr(config, 'chunked_threshold_mb', 8.0)
            )
            
            if huge_html:
                # 超大HTML → 强制 MapReduce Readability（不管use_readability设置）
                html_size_mb = len(html_content) / (1024 * 1024)
                logger.info(f"🧩 检测到超大HTML({html_size_mb:.2f}MB)，强制MapReduce Readability")
                
                mr = await map_reduce_readability(
                    html_content,
                    concurrency=getattr(config, 'chunk_concurrency', 4),
                    target_kb=getattr(config, 'chunk_target_kb', 512),
                    overlap_chars=getattr(config, 'chunk_overlap_chars', 1024),
                    to_markdown=True,
                )
                total_clean_time = time.time() - total_clean_start
                
                if mr.get('success'):
                    markdown_content = mr.get('markdown', '')
                    return {
                        'markdown': markdown_content,
                        'cleaning_info': {
                            'enabled': True,
                            'success': True,
                            'cleaner': 'readability-mapreduce',
                            'original_size': len(html_content),
                            'cleaned_size': len(markdown_content),
                            'slices': mr.get('slices', 0),
                            'reduction_percentage': max(0, round((len(html_content)-len(markdown_content))/max(1,len(html_content))*100,2)),
                            'total_clean_time': total_clean_time,
                        }
                    }
                else:
                    logger.warning(f"⚠️ MapReduce失败: {mr.get('error')}, 回退单体Readability")
                    # MapReduce失败，回退到单体Readability（使用统一接口）
                    readability_result = await clean_with_readability_single(html_content, to_markdown=True)
                    total_clean_time = time.time() - total_clean_start
                    if readability_result.get('success'):
                        markdown_content = readability_result.get('markdown', '')
                        return {
                            'markdown': markdown_content,
                            'cleaning_info': {
                                'enabled': True,
                                'success': True,
                                'cleaner': readability_result.get('cleaner', 'readability-single'),
                                'original_size': readability_result.get('original_size', len(html_content)),
                                'cleaned_size': readability_result.get('cleaned_size', len(markdown_content)),
                                'reduction_percentage': readability_result.get('reduction_percentage', 0),
                                'total_clean_time': total_clean_time,
                            }
                        }
                    else:
                        # Readability也失败，抛异常
                        raise RuntimeError(f"Readability清理失败: {readability_result.get('error')}")
            
            # 🔥 优先级2：普通HTML，根据use_readability配置决定
            use_readability = getattr(config, 'use_readability', False)
            
            if use_readability:
                # 普通HTML → 单体 Readability（使用统一接口）
                logger.info(f"🚀 使用单体 Readability 清理，原始长度: {len(html_content)} 字符")
                readability_result = await clean_with_readability_single(html_content, to_markdown=True)
                total_clean_time = time.time() - total_clean_start
                
                if readability_result.get('success'):
                    markdown_content = readability_result.get('markdown', '')
                    
                    return {
                        'markdown': markdown_content,
                        'cleaning_info': {
                            'enabled': True,
                            'success': True,
                            'cleaner': 'readability-lxml',
                            'original_size': readability_result.get('original_size', len(html_content)),
                            'cleaned_size': readability_result.get('cleaned_size', len(markdown_content)),
                            'reduction_percentage': readability_result.get('reduction_percentage', 0),
                            'total_clean_time': total_clean_time,
                            'service_clean_time': readability_result.get('clean_time', 0)
                        }
                    }
                else:
                    # Readability 失败，回退到 HTML 清理服务
                    logger.warning(f"⚠️ Readability 清理失败: {readability_result.get('error')}, 回退到 HTML 清理服务")
                    use_readability = False  # 标记回退
            
            # 使用 HTML 清理服务（默认或回退方案）
            if not use_readability:
                # 使用配置的清理参数，或使用默认参数
                htmlclean_config = config.htmlclean if config.htmlclean else {
                    "approach": "prune",
                    "options": {
                        "ignore_links": False,
                        "ignore_images": False,
                        "escape_html": True,
                        "include_sup_sub": True,
                        "threshold": 0.5,
                        "threshold_type": "fixed"
                    }
                }
                
                logger.info(f"🧹 使用 HTML 清理服务，原始内容长度: {len(html_content)} 字符")
                
                # 调用HTML清理服务
                clean_result = await self._call_html_cleaner_service_async(html_content, htmlclean_config, config)
                
                total_clean_time = time.time() - total_clean_start
                service_clean_time = clean_result.get('clean_time', 0)
                
                if clean_result.get('success'):
                    markdown_content = clean_result.get('fit_markdown', '')
                    
                    # 如果清理后内容为空，返回原始内容的简化版本
                    if not markdown_content or len(markdown_content.strip()) < 10:
                        logger.warning(f"⚠️ HTML清理服务返回空内容，使用备用方案 (总耗时: {total_clean_time:.3f}s, 服务耗时: {service_clean_time:.3f}s)")
                        markdown_content = f"# 页面内容\n\n清理服务返回空内容，原始HTML长度: {len(html_content)} 字符"
                    else:
                        reduction_pct = round((len(html_content) - len(markdown_content)) / len(html_content) * 100, 2) if len(html_content) > 0 else 0
                        logger.info(f"✅ HTML清理完成: {len(html_content)} → {len(markdown_content)} 字符 (压缩率: {reduction_pct}%, 总耗时: {total_clean_time:.3f}s, 服务耗时: {service_clean_time:.3f}s)")
                    
                    return {
                        'markdown': markdown_content,
                        'cleaning_info': {
                            'enabled': True,
                            'success': True,
                            'cleaner': 'html_clean_service',
                            'original_size': len(html_content),
                            'cleaned_size': len(markdown_content),
                            'reduction_percentage': round((len(html_content) - len(markdown_content)) / len(html_content) * 100, 2) if len(html_content) > 0 else 0,
                            'total_clean_time': total_clean_time,
                            'service_clean_time': service_clean_time
                        }
                    }
                else:
                    error_msg = clean_result.get('error', '未知错误')
                    logger.error(f"❌ HTML清理失败: {error_msg} (总耗时: {total_clean_time:.3f}s, 服务耗时: {service_clean_time:.3f}s)")
                    
                    # 清理失败时的备用方案
                    fallback_markdown = f"# 内容清理失败\n\n**错误:** {error_msg}\n\n**原始HTML长度:** {len(html_content)} 字符\n\n*请检查HTML清理服务是否正常运行*"
                    
                    return {
                        'markdown': fallback_markdown,
                        'cleaning_info': {
                            'enabled': True,
                            'success': False,
                            'cleaner': 'html_clean_service',
                            'error': error_msg,
                            'original_size': len(html_content),
                            'cleaned_size': len(fallback_markdown),
                            'total_clean_time': total_clean_time,
                            'service_clean_time': service_clean_time
                        }
                    }
                
        except Exception as e:
            total_clean_time = time.time() - total_clean_start
            logger.error(f"❌ HTML清理异常: {e} (总耗时: {total_clean_time:.3f}s)")
            
            # 异常时的备用方案
            fallback_markdown = f"# 内容清理异常\n\n**异常信息:** {str(e)}\n\n**原始HTML长度:** {len(html_content)} 字符"
            
            return {
                'markdown': fallback_markdown,
                'cleaning_info': {
                    'enabled': True,
                    'success': False,
                    'error': str(e),
                    'original_size': len(html_content),
                    'cleaned_size': len(fallback_markdown),
                    'total_clean_time': total_clean_time
                }
            }

    async def crawl_urls_concurrent(self, urls: List[str], config: CrawlRequest) -> Dict[str, Any]:
        """并发爬取多个URL - 抢占式最大并发（工作池）"""
        start_time = time.time()
        
        try:
            # 1) 计算抢占式最大并发量（使用 concurrent_limit；队列未空时始终占满该并发）
            limit = getattr(config, 'concurrent_limit', None)
            raw_concurrency = limit if isinstance(limit, int) and limit > 0 else 3
            # 边界：至少1，最多100
            concurrency = min(100, max(1, raw_concurrency))
            
            total = len(urls or [])
            if total == 0:
                return {
                    'results': [],
                    'summary': {
                        'total_urls': 0,
                        'successful': 0,
                        'failed': 0,
                        'execution_time': 0.0,
                        'concurrent_limit': concurrency
                    }
                }
            
            # 2) 基于 Queue 的工作池：保持待处理不空时始终占满并发
            queue = asyncio.Queue()
            for idx, u in enumerate(urls):
                queue.put_nowait((idx, u))
            
            results_buffer: List[Optional[Dict[str, Any]]] = [None] * total
            
            async def worker(worker_id: int):
                while True:
                    try:
                        idx, u = await queue.get()
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        break
                    try:
                        ret = await self.crawl_single_url(u, config)
                    except Exception as e:
                        ret = {
                            'url': u,
                            'success': False,
                            'error': str(e),
                            'execution_time': 0
                        }
                    results_buffer[idx] = ret
                    try:
                        queue.task_done()
                    except Exception:
                        pass
            
            worker_count = min(concurrency, total)
            workers = [asyncio.create_task(worker(i)) for i in range(worker_count)]
            
            # 等待所有任务完成；在任何时刻，当某个 worker 完成就会立即抢占下一条URL
            await queue.join()
            
            # 停止所有 worker
            for w in workers:
                w.cancel()
            # 🔥 修复：必须捕获CancelledError
            try:
                await asyncio.gather(*workers, return_exceptions=True)
            except (asyncio.CancelledError, Exception):
                pass
            
            # 3) 汇总结果
            processed_results: List[Dict[str, Any]] = []
            for i in range(total):
                r = results_buffer[i]
                if r is None:
                    processed_results.append({
                        'url': urls[i],
                        'success': False,
                        'error': '任务未完成',
                        'execution_time': 0
                    })
                else:
                    processed_results.append(r)
            
            execution_time = time.time() - start_time
            successful_count = sum(1 for r in processed_results if r.get('success', False))
            failed_count = total - successful_count
            
            return {
                'results': processed_results,
                'summary': {
                    'total_urls': total,
                    'successful': successful_count,
                    'failed': failed_count,
                    'execution_time': execution_time,
                    'concurrent_limit': concurrency
                }
            }
        
        except Exception as e:
            logger.error(f"❌ 并发爬取失败: {e}")
            return {
                'results': [],
                'summary': {
                    'total_urls': len(urls or []),
                    'successful': 0,
                    'failed': len(urls or []),
                    'execution_time': time.time() - start_time,
                    'error': str(e)
                }
            }

    async def cleanup_resources(self):
        """清理资源"""
        if self._is_closing:
            return

        self._is_closing = True
        logger.info("🧹 开始清理Playwright爬虫资源...")

        try:
            # 停止后台内存保洁器
            if self._memory_task and (not self._memory_task.done()):
                try:
                    self._memory_task.cancel()
                    try:
                        await self._memory_task
                    except Exception:
                        pass
                finally:
                    self._memory_task = None
            # 清理上下文
            if self.context:
                try:
                    await self.context.close()
                except Exception as e:
                    logger.warning(f"❌ 关闭上下文失败: {e}")
                finally:
                    self.context = None

            # 清理浏览器
            if self.browser and not self._using_persistent_context:
                try:
                    await self.browser.close()
                    logger.debug("✅ 浏览器已关闭")
                except Exception as e:
                    logger.warning(f"❌ 关闭浏览器失败: {e}")
                finally:
                    self.browser = None

            # 停止Playwright
            if self.playwright:
                try:
                    await self.playwright.stop()
                    logger.debug("✅ Playwright已停止")
                except Exception as e:
                    logger.warning(f"❌ 停止Playwright失败: {e}")
                finally:
                    self.playwright = None

            # 清理EasyGet爬虫
            if hasattr(self, 'easy_crawler') and self.easy_crawler:
                try:
                    await self.easy_crawler.cleanup()
                    logger.debug("✅ EasyGet爬虫已清理")
                except Exception as e:
                    logger.warning(f"❌ 清理EasyGet爬虫失败: {e}")

            # 重置状态
            self._using_persistent_context = False
            self._current_config = None
            
            logger.info("✅ Playwright爬虫资源清理完成")
            
        except Exception as e:
            logger.error(f"❌ 清理资源时发生异常: {e}")
        finally:
            self._is_closing = False

    # === 内存维护：定期 GC 与缓存清理 ===
    def _ensure_memory_maintenance_started(self):
        """确保后台内存保洁任务已启动。"""
        try:
            if (not self._memory_task) or self._memory_task.done():
                self._memory_task = asyncio.create_task(self._memory_maintenance_loop())
                logger.debug("🧹 内存保洁器已启动")
        except Exception as e:
            logger.debug(f"启动内存保洁器异常: {e}")

    async def _memory_maintenance_loop(self):
        """后台循环：周期性对所有页面触发 V8 GC、清理浏览器缓存并移除孤儿空白页。"""
        try:
            while True:
                try:
                    if not self.context or self._is_closing:
                        break
                    await self._trim_memory_once()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"内存保洁器循环异常: {e}")
                await asyncio.sleep(self._memory_trim_interval_sec)
        finally:
            logger.debug("🧹 内存保洁器已停止")

    async def _trim_memory_once(self):
        """对当前上下文执行一次内存整理。"""
        if not self.context:
            return
        try:
            # 1) 清理孤儿 about:blank 页面并确保占位页
            try:
                self._placeholder_page = await cleanup_orphan_about_blank_pages(self.context, self.timeout_manager, self.placeholder_url, self._placeholder_page, self._handle_resource_request)
            except Exception as _e:
                logger.debug(f"清理关于:blank 页面异常: {_e}")

            # 2) 对活跃页面触发 GC / 清缓存
            for p in list(self.context.pages):
                try:
                    if not p or p.is_closed():
                        continue
                    client = await self._safe_new_cdp_session(p)
                    if not client:
                        continue
                    # 触发 V8 GC
                    try:
                        await client.send("HeapProfiler.enable")
                        await client.send("HeapProfiler.collectGarbage")
                    except Exception as _e:
                        logger.debug(f"Heap GC 失败: {_e}")
                    # 清理浏览器缓存（仅清 HTTP 缓存，不影响 Cookie）
                    try:
                        await client.send("Network.clearBrowserCache")
                    except Exception as _e:
                        logger.debug(f"清理浏览器缓存失败: {_e}")
                except Exception as e:
                    logger.debug(f"页面内存整理异常: {e}")
        except Exception as e:
            logger.debug(f"一次内存整理异常: {e}")

    # === 新增: 安全创建 CDP 会话的工具函数 ===
    async def _safe_new_cdp_session(self, page: Page):
        """安全地为给定页面创建 CDP 会话。
        如果页面已经被关闭或创建会话失败，将返回 None 而不是抛出异常。"""
        try:
            if page.is_closed():
                return None
            return await page.context.new_cdp_session(page)
        except Exception as e:
            logger.debug(f"_safe_new_cdp_session failed: {e}")
            return None

    def _apply_health_detection_to_result(self, result: Dict[str, Any], status_code: Optional[int], min_text_length: int) -> Dict[str, Any]:
        """
        统一对结果进行健康度检测：
          - 命中则 success=False，并将错误信息写入对应的 easyget_error 或 playwright_error
        """
        try:
            # 优先使用 markdown，否则基于 html 提取可见文本
            text = result.get('markdown')
            if not text:
                html = result.get('html', '')
                text = extract_text_from_html(html)
            ok, reason, details = evaluate_content_health(text or '', status_code, min_text_length)
            if not ok:
                result['success'] = False
                # 附加检测详情（不覆盖已有字段）
                if 'detection' not in result:
                    result['detection'] = details
                else:
                    try:
                        result['detection'].update(details)
                    except Exception:
                        result['detection'] = details
                
                # 🔥 根据 actual_crawler 或 mode 判断是哪个爬虫，将错误信息放入对应的错误字段
                error_msg = f"内容健康检测失败: {reason}"
                actual_crawler = result.get('actual_crawler', '')
                mode = result.get('mode', '')
                
                # 判断是 EasyGet / Jina / Playwright
                if actual_crawler == 'easyget' or mode in ('fast', 'easyget', 'concurrent→easyget'):
                    if not result.get('easyget_error'):
                        result['easyget_error'] = error_msg
                elif actual_crawler == 'jina' or mode in ('jina', 'concurrent→jina'):
                    if not result.get('jina_error'):
                        result['jina_error'] = error_msg
                elif actual_crawler == 'playwright' or mode in ('playwright', 'concurrent→playwright'):
                    # Playwright 相关
                    if not result.get('playwright_error'):
                        result['playwright_error'] = error_msg
                else:
                    # 无法判断时，同时放入三个字段
                    if not result.get('easyget_error'):
                        result['easyget_error'] = error_msg
                    if not result.get('playwright_error'):
                        result['playwright_error'] = error_msg
                    if not result.get('jina_error'):
                        result['jina_error'] = error_msg
        except Exception as _e:
            # 🔥 关键修复：健康检测异常时，强制标记为失败，防止误判成功
            logger.error(f"❌ 健康检测异常（已强制标记为失败）: {_e}")
            result['success'] = False
            result['detection'] = {'error': str(_e)}
            
            # 将错误信息写入对应的错误字段
            error_msg = f"健康检测异常: {str(_e)}"
            actual_crawler = result.get('actual_crawler', '')
            mode = result.get('mode', '')
            if actual_crawler == 'easyget' or mode in ('fast', 'easyget', 'concurrent→easyget'):
                result['easyget_error'] = error_msg
            elif actual_crawler == 'jina' or mode in ('jina', 'concurrent→jina'):
                result['jina_error'] = error_msg
            elif actual_crawler == 'playwright' or mode in ('playwright', 'concurrent→playwright'):
                result['playwright_error'] = error_msg
            else:
                result['easyget_error'] = error_msg
                result['playwright_error'] = error_msg
                result['jina_error'] = error_msg
                
        return result

    async def _rotate_proxy_for_request(self, reason: str = ""):
        """在每次抓取前尝试异步切换Clash代理（fire-and-forget）。
        
        Args:
            reason: 切换原因
        
        - 仅调度异步切换到事件循环，不等待其完成
        - 任何异常都会被吞掉，仅记录调试日志
        """
        try:
            if hasattr(self, 'proxy_manager') and self.proxy_manager:
                # fire-and-forget 模式
                asyncio.create_task(self.proxy_manager.switch_proxy_async(verbose=True))
                logger.info("🔁 已触发按请求代理切换%s（不等待）", f"（{reason}）" if reason else "")
        except Exception as e:
            logger.warning(f"⚠️ 按请求代理切换异常: {e}")



    # （方法已迁移至 helper，保留处已移除）


# 创建路由器
router = APIRouter()

# 初始化和清理函数
async def initialize_crawler():
    """初始化爬虫服务"""
    global crawler
    try:
        if not crawler:
            crawler = PlaywrightCrawler()
            logger.info("✅ Playwright爬虫服务对象已创建，开始预启动浏览器…")
            try:
                # 预初始化：提前启动浏览器持久化上下文，避免并发请求时重复启动
                await crawler.initialize()
                logger.info("🎉 预启动浏览器成功，Playwright环境就绪")
            except Exception as pre_init_err:
                logger.warning(f"⚠️ 浏览器预初始化失败，但继续保留爬虫实例: {pre_init_err}")
        else:
            logger.info("✅ Playwright爬虫服务已存在，跳过初始化")
    except Exception as e:
        logger.error(f"❌ 初始化Playwright爬虫服务失败: {e}")
        raise

async def cleanup_crawler():
    """清理爬虫资源"""
    global crawler
    try:
        if crawler:
            await crawler.cleanup_resources()
            crawler = None
            logger.info("✅ Playwright爬虫资源清理完成")
    except Exception as e:
        logger.error(f"❌ 清理Playwright爬虫资源失败: {e}")

# 路由定义

@router.post("/crawl")
async def crawl_urls(config: CrawlRequest = Body(...)):
    """批量爬取URL接口"""
    global crawler
    if not crawler:
        raise HTTPException(status_code=503, detail="爬虫服务未初始化")
    try:
        result = await crawler.crawl_urls_concurrent(config.urls, config)
        # 🔥 若任一爬取项失败（success=False），则顶层 code=1
        code_top = 0
        msg_top = "爬取完成"
        try:
            results_list = (result or {}).get('results', [])
            for item in results_list:
                if isinstance(item, dict):
                    # 只检查 success=False
                    if not item.get('success', True):
                        code_top = 1
                        msg_top = "部分URL爬取失败"
                        break
        except Exception:
            pass
        return CrawlResponse(
            code=code_top,
            msg=msg_top,
            data=result
        )
    except Exception as e:
        logger.error(f"❌ 爬取失败: {e}")
        return CrawlResponse(
            code=1,
            msg=f"爬取失败: {str(e)}",
            data=None
        )
