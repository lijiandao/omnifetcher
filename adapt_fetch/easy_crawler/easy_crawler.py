import asyncio
import aiohttp
import ssl
import time
import re
import chardet
import sqlite3
import os
import json
import base64
import copy
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urljoin, urlparse
import logging
from bs4 import BeautifulSoup
import gzip
from io import BytesIO
from datetime import datetime, timedelta
import shutil
import tempfile
from PIL import Image
import socket
# -------- ftfy 乱码检测支持 --------
try:
    from ftfy.badness import is_bad as is_mojibake  # type: ignore
except Exception:
    def is_mojibake(_: str) -> bool:  # type: ignore
        return False

# -------- selectolax 快速HTML解析 --------
try:
    from selectolax.parser import HTMLParser  # type: ignore
    SELECTOLAX_AVAILABLE = True
except Exception:
    SELECTOLAX_AVAILABLE = False


# -------- 可打印字符比例算法 ----------
import string

def _printable_ratio(txt: str) -> float:
    """计算字符串中 ASCII 可打印字符的比例"""
    if not txt:
        return 1.0
    printable = set(string.printable)
    printable_count = sum(1 for c in txt if c in printable)
    return printable_count / len(txt)


def is_probably_binary(text: str, *, min_len: int = 50, ratio_threshold: float = 0.90) -> bool:
    """新乱码 / 二进制判定逻辑
    规则: 文本长度 >= min_len 且可打印字符比例 < ratio_threshold 即认为非正常文本
    """
    if not text or len(text) < min_len:
        return False
    ratio = _printable_ratio(text)
    return ratio < ratio_threshold

# 配置日志
logger = logging.getLogger(__name__)

# 统一的大HTML处理工具（强制）
from adapt_fetch.utils.tackle_huge_html import (
    should_use_readability_for_huge_html,
    map_reduce_readability,
    clean_with_readability_single,
    remove_base64_images,
    remove_base64_from_markdown,
    remove_script_style_tags,
)

# 关闭 readability.readability 的冗长 DEBUG 日志
try:
    logging.getLogger('readability.readability').setLevel(logging.WARNING)
except Exception:
    pass

# Windows加密相关
try:
    import win32crypt
    WINDOWS_CRYPTO_AVAILABLE = True
except ImportError:
    WINDOWS_CRYPTO_AVAILABLE = False
    logger.warning("win32crypt不可用，无法解密Windows存储的cookies")

# AES-GCM支持 (用于新版Chrome/Edge)
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    AES_GCM_AVAILABLE = True
except ImportError:
    AES_GCM_AVAILABLE = False
    logger.warning("cryptography不可用，无法解密v10+版本的cookies")


# 常见二进制文件魔数字典
MAGIC_SIGNATURES = {
    b"%PDF-": "PDF",
    b"\x89PNG": "PNG",
    b"GIF87a": "GIF",
    b"GIF89a": "GIF",
    b"\xFF\xD8\xFF": "JPEG",
    b"PK\x03\x04": "ZIP",
    b"\x1F\x8B": "GZIP",
    b"\x42\x4D": "BMP",
    b"\x00\x00\x01\x00": "ICO",
}


def is_binary_magic(raw: bytes) -> bool:
    """根据文件头魔数判断是否为常见二进制格式。"""
    if not raw:
        return False
    for sig in MAGIC_SIGNATURES:
        if raw.startswith(sig):
            return True
    return False


def should_fail_easyget(raw: bytes, plain_text: str) -> bool:
    """双检测逻辑：魔数命中或乱码判定为真时，让 EasyGet 失败，Playwright 接管。"""
    is_binary = is_binary_magic(raw[:8])
    probably_binary = is_probably_binary(plain_text)
    return is_binary or probably_binary

class EdgeCookieReader:
    """Edge浏览器Cookie读取器"""
    
    def __init__(self):
        # ✅ 修复：支持Linux系统，添加系统检测
        import platform
        system = platform.system().lower()
        
        if system == "windows":
            self.edge_user_data_paths = [
                os.path.expanduser(r"~\AppData\Local\Microsoft\Edge\User Data"),
                os.path.expanduser(r"~\AppData\Local\Microsoft\Edge Dev\User Data"),
                os.path.expanduser(r"~\AppData\Roaming\Microsoft\Edge\User Data"),
            ]
        elif system == "linux":
            self.edge_user_data_paths = [
                os.path.expanduser("~/.config/microsoft-edge"),
                os.path.expanduser("~/.config/microsoft-edge-dev"),
                os.path.expanduser("~/.config/google-chrome"),  # 作为备选
                os.path.expanduser("~/.config/chromium"),      # 作为备选
            ]
        elif system == "darwin":  # macOS
            self.edge_user_data_paths = [
                os.path.expanduser("~/Library/Application Support/Microsoft Edge"),
                os.path.expanduser("~/Library/Application Support/Microsoft Edge Dev"),
                os.path.expanduser("~/Library/Application Support/Google Chrome"),  # 作为备选
            ]
        else:
            self.edge_user_data_paths = []
            logger.warning(f"不支持的操作系统: {system}")
        
        self.system = system
        self._master_key_cache = None
    
    def get_edge_user_data_dir(self) -> Optional[str]:
        """获取Edge用户数据目录"""
        for path in self.edge_user_data_paths:
            if os.path.exists(path):
                logger.info(f"找到浏览器用户数据目录: {path}")
                return path
        
        # ✅ 改进：如果没找到，提供更详细的信息
        logger.warning(f"未找到浏览器用户数据目录 (系统: {self.system})")
        return None
    
    def get_cookie_db_path(self, profile: str = "Default") -> Optional[str]:
        """获取cookies数据库路径"""
        user_data_dir = self.get_edge_user_data_dir()
        if not user_data_dir:
            return None
        
        # 尝试不同的配置文件名称
        possible_profiles = [profile, "Default", "Profile 1"]
        
        for prof in possible_profiles:
            # 1. 尝试新版路径 (Network/Cookies)
            network_cookie_path = os.path.join(user_data_dir, prof, "Network", "Cookies")
            if os.path.exists(network_cookie_path):
                logger.info(f"找到cookies数据库(新版): {network_cookie_path}")
                return network_cookie_path
                
            # 2. 尝试旧版路径
            cookie_db_path = os.path.join(user_data_dir, prof, "Cookies")
            if os.path.exists(cookie_db_path):
                logger.info(f"找到cookies数据库: {cookie_db_path}")
                return cookie_db_path
        
        logger.warning(f"未找到cookies数据库，尝试的配置文件: {possible_profiles}")
        return None
    
    def get_master_key(self) -> Optional[bytes]:
        """获取解密用的master key (仅Windows)"""
        if not WINDOWS_CRYPTO_AVAILABLE:
            return None
            
        try:
            user_data_dir = self.get_edge_user_data_dir()
            if not user_data_dir:
                return None
                
            local_state_path = os.path.join(user_data_dir, "Local State")
            if not os.path.exists(local_state_path):
                logger.warning(f"Local State文件不存在: {local_state_path}")
                return None
                
            with open(local_state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
                
            encrypted_key = base64.b64decode(state["os_crypt"]["encrypted_key"])
            # Remove DPAPI prefix (5 bytes)
            encrypted_key = encrypted_key[5:]
            
            master_key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
            return master_key
        except Exception as e:
            logger.error(f"获取master key失败: {e}")
            return None

    def decrypt_cookie_value(self, encrypted_value: bytes) -> str:
        """解密cookie值 (支持 v10 AES-GCM 和 旧版 DPAPI)"""
        # Linux系统下的处理
        if self.system != "windows" or not WINDOWS_CRYPTO_AVAILABLE:
            # 在Linux系统下，cookies通常不加密，直接返回value字段
            return ""
        
        try:
            # 1. 尝试 AES-GCM 解密 (v10 前缀) - 新版 Edge/Chrome
            if encrypted_value.startswith(b'v10'):
                # 获取 master key (可以使用缓存)
                if self._master_key_cache is None:
                    self._master_key_cache = self.get_master_key()
                
                if self._master_key_cache and AES_GCM_AVAILABLE:
                    try:
                        nonce = encrypted_value[3:15]
                        ciphertext = encrypted_value[15:]
                        aesgcm = AESGCM(self._master_key_cache)
                        decrypted_data = aesgcm.decrypt(nonce, ciphertext, None)
                        return decrypted_data.decode('utf-8', errors='ignore')
                    except Exception as e:
                        logger.debug(f"AES-GCM解密失败: {e}，尝试降级方法")
            
            # 2. 尝试旧版 DPAPI 解密 - 旧版 Edge/Chrome
            # 移除前缀（通常是v10，如果GCM失败则回退到这里尝试）
            data_to_decrypt = encrypted_value
            if encrypted_value.startswith(b'v10'):
                data_to_decrypt = encrypted_value[3:]
            
            # 使用Windows DPAPI解密
            decrypted_value = win32crypt.CryptUnprotectData(data_to_decrypt, None, None, None, 0)[1]
            return decrypted_value.decode('utf-8', errors='ignore')
        except Exception as e:
            logger.debug(f"解密cookie失败: {e}")
            return ""
    
    def read_edge_cookies(self, profile: str = "Default", target_domains: List[str] = None) -> Dict[str, List[Dict]]:
        """读取浏览器的cookies"""
        cookie_db_path = self.get_cookie_db_path(profile)
        if not cookie_db_path:
            logger.warning("无法找到cookies数据库文件")
            return {}
        
        # 创建临时文件副本（避免文件锁定问题）
        temp_db_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as temp_file:
                temp_db_path = temp_file.name
                shutil.copy2(cookie_db_path, temp_db_path)
            
            # 连接到临时数据库
            conn = sqlite3.connect(temp_db_path)
            cursor = conn.cursor()
            
            # 构建查询语句
            if target_domains:
                domain_conditions = " OR ".join([f"host_key LIKE '%{domain}%'" for domain in target_domains])
                query = f"""
                SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite
                FROM cookies 
                WHERE ({domain_conditions})
                ORDER BY host_key, name
                """
            else:
                query = """
                SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite
                FROM cookies 
                ORDER BY host_key, name
                """
            
            cursor.execute(query)
            rows = cursor.fetchall()
            
            cookies_by_domain = {}
            
            for row in rows:
                host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite = row
                
                # 解密cookie值
                if encrypted_value and self.system == "windows":
                    decrypted_value = self.decrypt_cookie_value(encrypted_value)
                    final_value = decrypted_value if decrypted_value else value
                else:
                    # Linux系统直接使用value字段
                    final_value = value
                
                # 跳过空值cookies
                if not final_value:
                    continue
                
                # 转换过期时间
                if expires_utc:
                    try:
                        # Chrome时间戳是从1601年1月1日开始的微秒数
                        chrome_epoch = datetime(1601, 1, 1)
                        expire_time = chrome_epoch + timedelta(microseconds=expires_utc)
                        expires = expire_time.timestamp()
                    except:
                        expires = None
                else:
                    expires = None
                
                cookie_info = {
                    'name': name,
                    'value': final_value,
                    'domain': host_key,
                    'path': path or '/',
                    'expires': expires,
                    'secure': bool(is_secure),
                    'httponly': bool(is_httponly),
                    'samesite': samesite
                }
                
                if host_key not in cookies_by_domain:
                    cookies_by_domain[host_key] = []
                cookies_by_domain[host_key].append(cookie_info)
            
            conn.close()
            
            total_cookies = sum(len(cookies) for cookies in cookies_by_domain.values())
            logger.info(f"成功读取 {len(cookies_by_domain)} 个域名的cookies，总计 {total_cookies} 个cookie")
            return cookies_by_domain
            
        except Exception as e:
            logger.error(f"读取浏览器cookies失败: {e}")
            return {}
        finally:
            # 清理临时文件
            if temp_db_path and os.path.exists(temp_db_path):
                try:
                    os.unlink(temp_db_path)
                except Exception:
                    pass
    
    def cookies_to_aiohttp_format(self, cookies_by_domain: Dict[str, List[Dict]]) -> Dict[str, str]:
        """将cookies转换为aiohttp可用的格式"""
        formatted_cookies = {}
        
        for domain, cookies in cookies_by_domain.items():
            cookie_strings = []
            for cookie in cookies:
                cookie_strings.append(f"{cookie['name']}={cookie['value']}")
            
            if cookie_strings:
                formatted_cookies[domain] = "; ".join(cookie_strings)
        
        return formatted_cookies

class EasyGetCrawler:
    """基于aiohttp的传统网络爬虫工具类"""
    
    def __init__(self, pdf_handler=None):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cookie_reader = EdgeCookieReader()
        self.edge_cookies: Dict[str, str] = {}  # 存储从浏览器读取的cookies
        self.pdf_handler = pdf_handler  # PDF处理器（可选，用于处理PDF内容）
        self.default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
    
    async def initialize(self):
        """初始化HTTP会话"""
        if self.session and not self.session.closed:
            return
        
        # 创建SSL上下文
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        # 连接器配置 - 增加连接池大小以支持更高并发
        connector = aiohttp.TCPConnector(
            ssl=ssl_context,
            limit=500,  # 增加总连接池大小（原100）
            limit_per_host=100,  # 增加每个主机的连接数（原30）
            keepalive_timeout=30,
            enable_cleanup_closed=True,
            # 增加套接字重用和性能优化
            use_dns_cache=True,
            ttl_dns_cache=300,
            family=socket.AF_INET,  # 强制使用IPv4以提高连接速度
        )
        
        # ✅ 修复：不设置session级别的默认超时，完全由请求级别控制
        self.session = aiohttp.ClientSession(
            connector=connector,
            headers=self.default_headers,
            cookie_jar=aiohttp.CookieJar(unsafe=True),  # 允许不安全的cookie
            connector_owner=True,
            trust_env=False  # 不使用环境变量的代理设置
        )
        
        logger.info("✅ EasyGet HTTP会话初始化完成")
    
    def load_edge_cookies(self, profile: str = "Default", target_domains: List[str] = None) -> Dict[str, Any]:
        """加载浏览器cookies"""
        try:
            # 读取cookies
            cookies_by_domain = self.cookie_reader.read_edge_cookies(profile, target_domains)
            
            if not cookies_by_domain:
                system_info = f"系统: {self.cookie_reader.system}"
                logger.warning(f"未读取到任何cookies ({system_info})")
                return {"success": False, "message": f"未读取到任何cookies ({system_info})"}
            
            # 转换为aiohttp格式
            self.edge_cookies = self.cookie_reader.cookies_to_aiohttp_format(cookies_by_domain)
            
            # 统计信息
            total_cookies = sum(len(cookies) for cookies in cookies_by_domain.values())
            domains_count = len(cookies_by_domain)
            
            logger.info(f"✅ 成功加载 {domains_count} 个域名的 {total_cookies} 个cookie")
            
            return {
                "success": True,
                "message": f"成功加载cookies",
                "domains_count": domains_count,
                "total_cookies": total_cookies,
                "domains": list(cookies_by_domain.keys()),
                "system": self.cookie_reader.system
            }
            
        except Exception as e:
            error_msg = f"加载浏览器cookies失败: {str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}
    
    def get_cookies_for_url(self, url: str, custom_cookies: Dict[str, str] = None) -> str:
        """获取指定URL的cookies"""
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        
        # 收集所有匹配的cookies
        cookie_strings = []
        
        # 1. 从浏览器cookies中查找
        for edge_domain, edge_cookie_string in self.edge_cookies.items():
            # 检查域名匹配（支持子域名）
            if (edge_domain == domain or 
                domain.endswith('.' + edge_domain.lstrip('.')) or 
                edge_domain.endswith('.' + domain.lstrip('.'))):
                cookie_strings.append(edge_cookie_string)
        
        # 2. 添加自定义cookies
        if custom_cookies:
            for custom_domain, custom_cookie_string in custom_cookies.items():
                if (custom_domain == domain or 
                    domain.endswith('.' + custom_domain.lstrip('.')) or 
                    custom_domain.endswith('.' + domain.lstrip('.'))):
                    cookie_strings.append(custom_cookie_string)
        
        # 合并所有cookies
        final_cookies = "; ".join(cookie_strings)
        
        return final_cookies
    
    def detect_encoding(self, content: bytes, headers: Dict[str, str]) -> str:
        """检测内容编码"""
        # 1. 从响应头获取编码
        content_type = headers.get('content-type', '').lower()
        charset_match = re.search(r'charset=([^;\s]+)', content_type)
        if charset_match:
            encoding = charset_match.group(1).strip('\'"')
            try:
                content.decode(encoding)
                return encoding
            except (UnicodeDecodeError, LookupError):
                pass
        
        # 2. 从HTML meta标签获取编码
        content_str = content[:1024].decode('utf-8', errors='ignore')
        meta_charset = re.search(r'<meta[^>]+charset=[\'"]?([^\'"\s>]+)', content_str, re.IGNORECASE)
        if meta_charset:
            encoding = meta_charset.group(1)
            try:
                content.decode(encoding)
                return encoding
            except (UnicodeDecodeError, LookupError):
                pass
        
        # 3. 使用chardet自动检测
        try:
            detected = chardet.detect(content[:4096])
            if detected and detected['confidence'] > 0.7:
                encoding = detected['encoding']
                if encoding:
                    content.decode(encoding)
                    return encoding
        except (UnicodeDecodeError, LookupError):
            pass
        
        # 4. 常见编码尝试
        for encoding in ['utf-8', 'gbk', 'gb2312', 'gb18030', 'big5', 'latin1']:
            try:
                content.decode(encoding)
                return encoding
            except UnicodeDecodeError:
                continue
        
        # 5. 默认返回utf-8
        return 'utf-8'
    
    def decompress_content(self, content: bytes, encoding_header: str) -> bytes:
        """解压缩内容"""
        if 'gzip' in encoding_header.lower():
            try:
                return gzip.decompress(content)
            except Exception:
                pass
        return content
    
    # PDF魔数（文件头标识）
    PDF_MAGIC_NUMBERS = [
        b'%PDF-',  # 标准PDF文件头
    ]
    
    def is_pdf_by_magic_number(self, file_data: bytes) -> bool:
        """
        通过魔数（文件头）检测是否为PDF文件
        
        Args:
            file_data: 文件的二进制数据（至少前几个字节）
            
        Returns:
            bool: 是否为PDF文件
        """
        try:
            if not file_data:
                return False
            
            # 检查PDF魔数
            for magic in self.PDF_MAGIC_NUMBERS:
                if file_data.startswith(magic):
                    logger.debug(f"✅ 魔数检测: 发现PDF文件头 {magic}")
                    return True
            
            logger.debug(f"❌ 魔数检测: 不是PDF文件，文件头: {file_data[:10]}")
            return False
            
        except Exception as e:
            logger.warning(f"⚠️ 魔数检测失败: {e}")
            return False

    def is_mojibake_content(self, content: bytes) -> bool:
        """
        检测内容是否包含乱码
        
        Args:
            content: 文件的二进制数据
            
        Returns:
            bool: 是否包含乱码
        """
        try:
            if not content:
                return False
            
            # 使用ftfy进行乱码检测
            return is_mojibake(content.decode('utf-8', errors='ignore'))
            
        except Exception as e:
            logger.warning(f"⚠️ 乱码检测失败: {e}")
            return False


    
    async def fetch_single_favicon(self, favicon_url: str, base_url: str, timeout: int = 2) -> Optional[str]:
        """获取单个favicon并转换为base64编码
        
        Args:
            favicon_url: favicon的URL
            base_url: 网站基础URL
            timeout: 单个favicon的超时时间
            
        Returns:
            str: base64编码的favicon图片，失败返回None
        """
        try:
            # 转换为绝对URL
            absolute_favicon_url = urljoin(base_url, favicon_url)
            logger.debug(f"🔍 [EasyGet] 尝试获取favicon: {absolute_favicon_url}")
            
            # 使用session下载favicon
            async with self.session.get(
                absolute_favicon_url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
                headers={'Accept': 'image/*,*/*;q=0.8'},
                proxy="http://127.0.0.1:7899"
            ) as response:
                if response.status == 200:
                    favicon_data = await response.read()
                    
                    # 检查是否是有效的图片数据
                    if len(favicon_data) > 0 and len(favicon_data) < 50000:  # 限制大小50KB
                        # 检测MIME类型
                        if favicon_data.startswith(b'\x89PNG'):
                            mime_type = 'image/png'
                        elif favicon_data.startswith(b'\xff\xd8\xff'):
                            mime_type = 'image/jpeg'
                        elif favicon_data.startswith(b'GIF'):
                            mime_type = 'image/gif'
                        elif favicon_data.startswith(b'\x00\x00\x01\x00'):
                            mime_type = 'image/x-icon'
                        else:
                            mime_type = 'image/png'  # 默认
                        
                        favicon_base64 = base64.b64encode(favicon_data).decode('utf-8')
                        data_uri = f"data:{mime_type};base64,{favicon_base64}"
                        logger.debug(f"✅ [EasyGet] 成功获取favicon: {absolute_favicon_url} ({len(favicon_data)} bytes)")
                        return data_uri
                        
        except Exception as e:
            logger.debug(f"[EasyGet] 获取favicon失败 {absolute_favicon_url}: {e}")
            return None
        
        return None

    async def get_favicon_base64_concurrent(self, url: str, html_content: str = None, timeout: int = 3) -> Optional[str]:
        """并发获取网站favicon并转换为base64编码
        
        Args:
            url: 网站URL
            html_content: HTML内容（可选）
            timeout: 总超时时间
            
        Returns:
            str: base64编码的favicon图片，失败返回None
        """
        try:
            parsed_url = urlparse(url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
            
            favicon_urls = []
            
            # 1. 如果有HTML内容，从中解析favicon链接
            if html_content:
                try:
                    soup = BeautifulSoup(html_content[:10000], 'html.parser')  # 只解析前10K字符
                    
                    favicon_selectors = [
                        'link[rel="icon"]',
                        'link[rel="shortcut icon"]', 
                        'link[rel="apple-touch-icon"]',
                        'link[rel="apple-touch-icon-precomposed"]'
                    ]
                    
                    for selector in favicon_selectors:
                        elements = soup.select(selector)
                        for el in elements:
                            href = el.get('href')
                            if href:
                                favicon_urls.append(href)
                                
                except Exception as e:
                    logger.debug(f"[EasyGet] 解析HTML中的favicon失败: {e}")
            
            # 2. 添加默认favicon路径
            if not favicon_urls:
                favicon_urls.append('/favicon.ico')
            
            # 3. 限制最多尝试3个favicon URL
            favicon_urls = favicon_urls[:3]
            
            # 4. 并发尝试下载所有favicon
            if favicon_urls:
                # 创建并发任务，每个任务有独立的短超时
                single_timeout = min(2, timeout // len(favicon_urls))
                tasks = [
                    self.fetch_single_favicon(favicon_url, base_url, single_timeout) 
                    for favicon_url in favicon_urls
                ]
                
                # 并发执行，只要有一个成功就返回
                try:
                    # 使用asyncio.as_completed来获取第一个成功的结果
                    for coro in asyncio.as_completed(tasks, timeout=timeout):
                        try:
                            result = await coro
                            if result:  # 如果获取成功，立即返回
                                # 取消其他还在执行的任务
                                for task in tasks:
                                    if hasattr(task, 'cancel') and not task.done():
                                        task.cancel()
                                return result
                        except Exception:
                            continue  # 忽略单个任务的失败
                            
                except asyncio.TimeoutError:
                    pass
            
            return None
            
        except Exception as e:
            logger.debug(f"[EasyGet] 并发获取favicon异常 {url}: {e}")
            return None
    
    def extract_page_info(self, html_content: str, base_url: str, extract_title: bool = True) -> Dict[str, Any]:
        """提取页面标题信息（去除多余的 meta 信息）"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            result = {}
            # 仅在需要时提取标题
            if extract_title:
                title_tag = soup.find('title')
                title = title_tag.get_text().strip() if title_tag else ''
                result['title'] = title

            return result
        except Exception as e:
            logger.warning(f"提取页面信息时出错: {str(e)}")
            return {
                'title': '',
                'error': str(e)
            }
    
    async def extract_page_info_async(self, html_content: str, base_url: str, extract_title: bool = True) -> Dict[str, Any]:
        """异步版本，使用线程池避免阻塞事件循环"""
        return await asyncio.to_thread(self.extract_page_info, html_content, base_url, extract_title)

    async def get_favicon_base64(self, url: str, html_content: str = None, timeout: int = 3) -> Optional[str]:
        """获取网站图标的base64编码 - ✅ 修复：添加超时控制"""
        try:
            # ✅ 添加总体超时控制
            favicon_start_time = time.time()
            
            parsed_url = urlparse(url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
            
            favicon_urls = []
            
            # 1. 从HTML中解析favicon链接（限制解析时间）
            if html_content:
                try:
                    # 限制HTML解析时间
                    soup = BeautifulSoup(html_content[:10000], 'html.parser')  # 只解析前10K字符
                    
                    favicon_selectors = [
                        'link[rel="icon"]',
                        'link[rel="shortcut icon"]', 
                        'link[rel="apple-touch-icon"]',
                    ]
                    
                    for selector in favicon_selectors:
                        links = soup.select(selector)
                        for link in links[:2]:  # 最多尝试2个
                            href = link.get('href')
                            if href:
                                favicon_url = urljoin(base_url, href)
                                if favicon_url not in favicon_urls:
                                    favicon_urls.append(favicon_url)
                                    
                except Exception as e:
                    logger.debug(f"解析HTML中的favicon失败: {e}")
            
            # 2. 添加默认favicon路径
            if not favicon_urls:
                favicon_urls.append(urljoin(base_url, '/favicon.ico'))
            
            # 3. 快速尝试下载favicon（最多尝试2个）
            for i, favicon_url in enumerate(favicon_urls[:2]):
                # 检查剩余时间
                elapsed = time.time() - favicon_start_time
                if elapsed >= timeout:
                    break
                
                try:
                    remaining_timeout = max(1, timeout - elapsed)
                    
                    # ✅ 关键修复：使用严格的超时控制
                    async with self.session.get(
                        favicon_url,
                        timeout=aiohttp.ClientTimeout(total=remaining_timeout),
                        headers={'Accept': 'image/*,*/*;q=0.8'}
                    ) as response:
                        
                        if response.status == 200:
                            content = await response.read()
                            
                            if len(content) > 0 and len(content) < 100000:  # 限制大小
                                # 简单编码，不进行复杂处理
                                if content.startswith(b'\x89PNG'):
                                    mime_type = 'image/png'
                                elif content.startswith(b'\xff\xd8\xff'):
                                    mime_type = 'image/jpeg'
                                elif content.startswith(b'GIF'):
                                    mime_type = 'image/gif'
                                elif content.startswith(b'\x00\x00\x01\x00'):
                                    mime_type = 'image/x-icon'
                                else:
                                    mime_type = 'image/png'
                                
                                base64_str = base64.b64encode(content).decode('utf-8')
                                data_uri = f"data:{mime_type};base64,{base64_str}"
                                
                                return data_uri
                                
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.debug(f"下载favicon失败 {favicon_url}: {e}")
                    continue
            
            return None
            
        except Exception as e:
            logger.debug(f"获取favicon时出错: {e}")
            return None
    
    async def _call_html_cleaner_service_async(self, html_content: str, htmlclean_config: dict = None) -> dict:
        """异步调用HTML清理服务API"""
        try:
          
            # 默认清理配置
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
            
            # 如果提供了配置，与默认配置合并
            final_config = copy.deepcopy(default_config)
            if htmlclean_config:
                final_config.update({k: v for k, v in htmlclean_config.items() if k != "options"})
                if "options" in htmlclean_config:
                    final_config["options"].update(htmlclean_config["options"])
            
            payload = {
                "html": html_content,
                **{k: v for k, v in final_config.items() if k != "options"},
                "options": final_config["options"]
            }

            logger.debug(f"🧹 [EasyGet] 异步调用HTML清理服务: http://127.0.0.1:8900/process_html")

            # 创建超时配置
            timeout = aiohttp.ClientTimeout(total=30)
            
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
                            logger.error("❌ [EasyGet] HTML清理服务返回无法解析的JSON")
                            return {"success": False, "error": "返回非法JSON"}
                        
                        if result.get('code') == 0:
                            markdown = result.get('fit_markdown') or result.get('raw_markdown')
                            if markdown:
                                logger.debug(f"✅ [EasyGet] HTML清理成功，生成markdown长度: {len(markdown)}")
                                return {"success": True, "fit_markdown": markdown}
                            else:
                                logger.warning("❌ [EasyGet] HTML清理服务未返回有效的markdown内容")
                                return {"success": False, "error": "未返回有效的markdown内容"}
                        else:
                            error_msg = result.get('msg', '未知错误')
                            logger.error(f"❌ [EasyGet] HTML清理服务返回错误: {error_msg}")
                            return {"success": False, "error": f"服务返回错误: {error_msg}"}
                    else:
                        error_msg = f"HTTP {response.status}"
                        logger.error(f"❌ [EasyGet] HTML清理服务请求失败: {error_msg}")
                        return {"success": False, "error": f"请求失败: {error_msg}"}
                        
        except asyncio.TimeoutError:
            logger.error("❌ [EasyGet] HTML清理服务请求超时")
            return {"success": False, "error": "请求超时"}
        except aiohttp.ClientConnectorError:
            logger.error("❌ [EasyGet] 无法连接到HTML清理服务")
            return {"success": False, "error": "无法连接到清理服务"}
        except Exception:
            logger.exception("❌ [EasyGet] HTML清理服务调用异常")
            return {"success": False, "error": "服务调用异常"}
    
    async def crawl_single_url(self, url: str, timeout: int = 30, user_agent: str = None,
                              extra_headers: Dict[str, str] = None, follow_redirects: bool = True,
                              max_redirects: int = 10, verify_ssl: bool = False,
                              encoding: str = None,
                              use_edge_cookies: bool = False, edge_profile: str = "Default",
                              target_domains: List[str] = None, custom_cookies: Dict[str, str] = None,
                              extract_title: bool = True, extract_icon: bool = True,
                              htmlclean_enabled: bool = True,
                              text_limit: int = 100,
                              chunked_threshold_mb: float = 8.0,
                              chunk_target_kb: int = 512,
                              chunk_overlap_chars: int = 1024,
                              chunk_concurrency: int = 4,
                            ) -> Dict[str, Any]:
        """爬取单个URL - ✅ 根据htmlclean_enabled决定返回markdown或html"""
        start_time = time.time()
        
        try:
            logger.info(f"📄 [EasyGet] 开始爬取: {url} - 超时设置: {timeout}秒 - HTML清理: {htmlclean_enabled}")
            
            # ✅ 使用asyncio.wait_for确保整个操作的超时控制
            return await asyncio.wait_for(
                self._do_crawl_single_url(url, timeout, user_agent, extra_headers, 
                                        follow_redirects, max_redirects, verify_ssl,
                                        encoding, use_edge_cookies, edge_profile,
                                        target_domains, custom_cookies, start_time, 
                                        extract_title, extract_icon, htmlclean_enabled,
                                        text_limit, chunked_threshold_mb, chunk_target_kb,
                                        chunk_overlap_chars, chunk_concurrency),
                timeout=timeout
            )
            
        except asyncio.CancelledError:
            execution_time = time.time() - start_time
            logger.warning(f"🛑 [EasyGet] 任务被取消 {url} - 耗时: {execution_time:.2f}秒")
            raise  # 重新抛出，让上层知道任务被取消
            
        except asyncio.TimeoutError:
            execution_time = time.time() - start_time
            error_msg = f"请求超时 ({timeout}秒)"
            logger.error(f"❌ [EasyGet] 爬取超时 {url}: {error_msg} - 实际耗时: {execution_time:.2f}秒")
            
            return {
                "url": url,
                "error": error_msg,
                "execution_time": round(execution_time, 2),
                "timeout_used": timeout,
                "mode": "fast",
                "crawler_type": "easyget_http",
                "success": False
            }
            
        except Exception as e:
            execution_time = time.time() - start_time
            error_msg = str(e)
            logger.error(f"❌ [EasyGet] 爬取失败 {url}: {error_msg} - 耗时: {execution_time:.2f}秒")
            
            return {
                "url": url,
                "error": error_msg,
                "execution_time": round(execution_time, 2),
                "timeout_used": timeout,
                "mode": "fast",
                "crawler_type": "easyget_http",
                "success": False
            }
    
    async def _do_crawl_single_url(self, url: str, timeout: int, user_agent: str,
                                  extra_headers: Dict[str, str], follow_redirects: bool,
                                  max_redirects: int, verify_ssl: bool, encoding: str,
                                  use_edge_cookies: bool, edge_profile: str,
                                  target_domains: List[str], custom_cookies: Dict[str, str],
                                  start_time: float, extract_title: bool = True, extract_icon: bool = True,
                                  htmlclean_enabled: bool = True, 
                                  text_limit: int = 100,
                                  chunked_threshold_mb: float = 8.0,
                                  chunk_target_kb: int = 512,
                                  chunk_overlap_chars: int = 1024,
                                  chunk_concurrency: int = 4) -> Dict[str, Any]:
        """内部爬取方法 - 根据htmlclean_enabled决定处理逻辑"""
        
        # 🔥 修复：确保session已经初始化
        if self.session is None or self.session.closed:
            logger.warning(f"⚠️ [EasyGet] Session未初始化或已关闭，重新初始化...")
            await self.initialize()
            
        # 🔥 二次检查：如果仍然没有有效session，返回错误
        if self.session is None or self.session.closed:
            logger.error(f"❌ [EasyGet] Session初始化失败: {url}")
            return {
                "url": url,
                "error": "HTTP会话初始化失败",
                "execution_time": round(time.time() - start_time, 2),
                "success": False,
                "final_url": url
            }
        
        # 如果需要使用浏览器cookies且还未加载，先加载
        if use_edge_cookies and not self.edge_cookies:
            # ⚠️ 重要：读取浏览器 Cookie 涉及磁盘 I/O 与 SQLite 操作，放到线程池中执行
            await asyncio.to_thread(self.load_edge_cookies, edge_profile, target_domains)
        
        # 准备请求头
        headers = self.default_headers.copy()
        if user_agent:
            headers['User-Agent'] = user_agent
        if extra_headers:
            headers.update(extra_headers)
        
        # 获取cookies
        cookies_string = ""
        if use_edge_cookies or custom_cookies:
            cookies_string = self.get_cookies_for_url(url, custom_cookies)
            if cookies_string:
                headers['Cookie'] = cookies_string
        
        # ✅ 简化超时配置：只设置总体超时，简单有效
        request_timeout = aiohttp.ClientTimeout(total=timeout)
        
        # 发送请求
        proxy_url = "http://127.0.0.1:7899"
        proxy_start_time = time.time()
        
        try:
            logger.debug(f"🌐 [EasyGet] 开始HTTP请求: {url} - 代理: {proxy_url}")
            
            async with self.session.get(
                url,
                headers=headers,
                timeout=request_timeout,
                allow_redirects=follow_redirects,
                max_redirects=max_redirects,
                ssl=False if not verify_ssl else None,
                proxy=proxy_url
            ) as response:
                proxy_time = time.time() - proxy_start_time
                logger.debug(f"✅ [EasyGet] HTTP响应获取成功: {url}")
                read_start_time = time.time()
                
                # 获取响应信息
                status_code = response.status
                final_url = str(response.url)
                response_headers = dict(response.headers)
                
                # 读取响应内容（不限制大小，交给MapReduce处理）
                logger.debug(f"[EasyGet] 开始读取响应: {url}")
                content = await response.read()
                read_elapsed = time.time() - read_start_time
                
                # 记录大小信息
                actual_size_mb = len(content) / (1024 * 1024)
                if actual_size_mb > 10.0:
                    logger.info(f"[EasyGet] 下载大文件: {actual_size_mb:.2f}MB")
                
                logger.debug(f"[EasyGet] 响应读取完成")
                
                # 解压缩内容
                decompress_start = time.time()
                content_encoding = response_headers.get('content-encoding', '')
                if content_encoding:
                    content = self.decompress_content(content, content_encoding)
                decompress_elapsed = time.time() - decompress_start
 
                # 检测编码
                detect_start = time.time()
                if encoding:
                    detected_encoding = encoding
                else:
                    detected_encoding = self.detect_encoding(content, response_headers)
                detect_elapsed = time.time() - detect_start
                
                # 解码内容
                decode_start = time.time()
                try:
                    html_content = content.decode(detected_encoding, errors='ignore')
                except Exception as e:
                    logger.warning(f"解码失败，使用utf-8: {e}")
                    html_content = content.decode('utf-8', errors='ignore')
                decode_elapsed = time.time() - decode_start

                # 🔍 先进行魔数和二进制检测
                is_binary_magic_result = False
                garbled = False
                
                try:
                    # 1) 检测魔数
                    magic_t0 = time.time()
                    is_binary_magic_result = is_binary_magic(content[:8])
                    logger.debug(f"[EasyGet] 魔数检测: is_binary={is_binary_magic_result}")
                    
                    # 如果是 PDF 且有 PDF 处理器，立即处理并返回（避免耗时的文本解析）
                    if is_binary_magic_result:
                        # 直接检查是否为 PDF（修复：不能用完整8字节查字典）
                        is_pdf = content[:5] == b'%PDF-'
                        
                        if is_pdf and self.pdf_handler:
                            logger.info("[EasyGet-PDF] 检测到PDF，使用PDF处理器提取文本...")
                            try:
                                # 保存 PDF 文件
                                pdf_filename = self.pdf_handler.generate_pdf_filename(url, None)
                                pdf_path = os.path.join(self.pdf_handler.download_dir, pdf_filename)
                                
                                self.pdf_handler.ensure_download_dir()
                                with open(pdf_path, 'wb') as f:
                                    f.write(content)
                                
                                file_size = len(content)
                                file_size_mb = file_size / (1024 * 1024)
                                logger.debug(f"[EasyGet-PDF] PDF已保存")
                                
                                # 提取文本（放到线程池避免阻塞）
                                pdf_text = await asyncio.to_thread(
                                    self.pdf_handler.extract_text_from_pdf, pdf_path
                                )
                                
                                static_url = f"{self.pdf_handler.static_url_base}/{pdf_filename}"
                                execution_time = time.time() - start_time
                                
                                pdf_markdown = f"""# {pdf_filename}
**文件信息:**
- 文件大小: {file_size_mb:.2f}MB
- 下载时间: {execution_time:.2f}s
- 下载链接: [{pdf_filename}]({static_url})
---
**PDF文本内容:**
{pdf_text}
"""
                                
                                logger.info(f"✅ [EasyGet-PDF] PDF处理成功: {url} ({len(pdf_text)} 字符文本)")
                                
                                return {
                                    "url": url,
                                    "final_url": final_url,
                                    "title": pdf_filename,
                                    "markdown": pdf_markdown,
                                    "text": pdf_text,
                                    "text_length": len(pdf_markdown),
                                    "html_size": file_size,
                                    "status_code": status_code,
                                    "execution_time": round(execution_time, 2),
                                    "mode": "fast",
                                    "javascript_enabled": False,
                                    "crawler_type": "easyget_pdf",
                                    "success": True,
                                    "is_pdf_page": True,
                                    "file_path": pdf_path,
                                    "static_url": static_url,
                                    "filename": pdf_filename,
                                    "file_size": file_size,
                                    "file_size_mb": round(file_size_mb, 2),
                                }
                                
                            except Exception as pdf_error:
                                logger.warning(f"⚠️ [EasyGet-PDF] PDF处理失败: {pdf_error}，交给Playwright处理")
                                # PDF处理失败，继续正常流程
                    
                    # 2) 纯文本提取（用于乱码检测；避免重型解析阻塞）
                    text_t0 = time.time()
                    plain_text = ''
                    if SELECTOLAX_AVAILABLE and len(html_content) <= 1500000:
                        # 小文档走 selectolax
                        tree = HTMLParser(html_content)
                        plain_text = tree.body.text(separator=' ', strip=True) if getattr(tree, 'body', None) else ''
                    else:
                        # 大文档走正则，并抽样前 800KB
                        sample_html = html_content[:800*1024]
                        plain_text = re.sub(r'<[^>]+>', ' ', sample_html)
                        plain_text = re.sub(r'\s+', ' ', plain_text).strip()
                    plain_text = plain_text.replace('\n', '')
                    text_elapsed = time.time() - text_t0
                    logger.debug(f"[EasyGet] 纯文本提取完成")
                    garbled = is_probably_binary(plain_text) if plain_text else False

                    # === 双重检测: 魔数/乱码触发失败 ===
                    if should_fail_easyget(content, plain_text):
                        logger.info("[EasyGet-Failover] 检测到二进制魔数或乱码，标记EasyGet失败，交给Playwright 处理")
                        execution_time = time.time() - start_time
                        return {
                            "url": url,
                            "final_url": final_url,
                            "status_code": status_code,
                            "execution_time": round(execution_time, 2),
                            "content_type": response_headers.get('content-type', ''),
                            "mode": "fast",
                            "crawler_type": "easyget_http",
                            "success": False,
                            "error": "quality_check_failed(binary_or_garbled_detected)",
                            "is_garbled": garbled,
                            "is_binary": is_binary_magic_result,
                            "magic_type": MAGIC_SIGNATURES.get(content[:8], '') if is_binary_magic_result else ''
                        }

                except Exception as log_err:
                    logger.warning(f"[二进制/乱码检测] 检测过程出错: {log_err}，使用默认值继续处理")
                
                # 🔥 新逻辑：使用 Readability 快速清理 HTML 并判断内容长度
                cleaned_markdown = None  # 保存清理后的markdown，避免重复清理
                cleaned_html = None  # 保存清理后的 HTML
                
                if htmlclean_enabled:
                    # 🔥 强制策略：只要触发MapReduce就走Readability（忽略外部use_readability配置）
                    clean_start = time.time()
                    
                    try:
                        # 检测是否为超大HTML（统一阈值判断）
                        use_mapreduce = should_use_readability_for_huge_html(html_content, threshold_mb=chunked_threshold_mb)
                        
                        if use_mapreduce:
                            # 超大HTML → MapReduce 分块并行清理
                            html_size_mb = len(html_content) / (1024 * 1024)
                            logger.info(f"[EasyGet] MapReduce处理 ({html_size_mb:.2f}MB)")
                            
                            mr = await map_reduce_readability(
                                html_content,
                                concurrency=chunk_concurrency,
                                target_kb=chunk_target_kb,
                                overlap_chars=chunk_overlap_chars,
                                to_markdown=True,
                            )
                            
                            if mr and mr.get("success"):
                                cleaned_markdown = mr.get("markdown")
                                cleaned_text_length = int(mr.get("text_length", 0))
                                clean_time = time.time() - clean_start
                                logger.debug(f"[EasyGet] MapReduce完成")
                            else:
                                # MapReduce失败，抛异常让外层捕获
                                raise RuntimeError(f"MapReduce失败: {mr.get('error')}")
                        else:
                            # 普通HTML → 单体 Readability 清理（使用统一接口）
                            clean_result = await clean_with_readability_single(html_content, to_markdown=True)
                            
                            if clean_result.get('success'):
                                cleaned_markdown = clean_result.get('markdown')
                                cleaned_text_length = clean_result.get('text_length')
                                clean_time = clean_result.get('clean_time')
                                logger.debug(f"[EasyGet] Readability清理完成")
                            else:
                                raise RuntimeError(f"单体Readability失败: {clean_result.get('error')}")
                        
                        # 质量检查
                        if cleaned_text_length < text_limit:
                            logger.info(f"[EasyGet] 清理后纯文本不足，交给Playwright")
                            execution_time = time.time() - start_time
                            return {
                                "url": url,
                                "final_url": final_url,
                                "status_code": status_code,
                                "execution_time": round(execution_time, 2),
                                "content_type": response_headers.get('content-type', ''),
                                "mode": "fast",
                                "crawler_type": "easyget_http",
                                "success": False,
                                "error": "quality_check_failed(cleaned_text_too_short)",
                                "text_length": cleaned_text_length,
                                "text_limit": text_limit,
                                "is_garbled": garbled,
                                "is_binary": is_binary_magic_result
                            }
                        
                        logger.debug(f"[内容质量检查] 质量检查通过")
                                
                    except Exception as clean_err:
                        logger.warning(f"[EasyGet-Failover] HTML清理异常: {clean_err}，交给Playwright处理")
                        execution_time = time.time() - start_time
                        return {
                            "url": url,
                            "final_url": final_url,
                            "status_code": status_code,
                            "execution_time": round(execution_time, 2),
                            "content_type": response_headers.get('content-type', ''),
                            "mode": "fast",
                            "crawler_type": "easyget_http",
                            "success": False,
                            "error": f"quality_check_failed(html_cleaning_exception: {str(clean_err)})",
                            "is_garbled": garbled,
                            "is_binary": is_binary_magic_result
                        }
                else:
                    # 未启用HTML清理，用body纯文本长度判断（保留旧逻辑）
                    tree = HTMLParser(html_content)
                    plain_text = tree.body.text(separator=' ', strip=True) if tree.body else ''
                    plain_text = plain_text.replace('\n', '')
                    plain_text_length = len(plain_text)
                    
                    if plain_text_length < text_limit:
                        logger.info(f"[EasyGet] 纯文本长度不足，交给Playwright处理")
                        execution_time = time.time() - start_time
                        return {
                            "url": url,
                            "final_url": final_url,
                            "status_code": status_code,
                            "execution_time": round(execution_time, 2),
                            "content_type": response_headers.get('content-type', ''),
                            "mode": "fast",
                            "crawler_type": "easyget_http",
                            "success": False,
                            "error": "quality_check_failed(plain_text_too_short)",
                            "text_length": plain_text_length,
                            "text_limit": text_limit,
                            "is_garbled": garbled,
                            "is_binary": is_binary_magic_result
                        }
                    
                    logger.debug(f"[内容质量检查] 质量检查通过")

                # 🔥 预处理HTML：移除base64图片和script/style标签，减少解析负担
                cleaned_html_for_title = remove_script_style_tags(remove_base64_images(html_content))
                
                # 提取页面信息
                page_info = await self.extract_page_info_async(
                    cleaned_html_for_title,
                    final_url,
                    extract_title
                )
                
                # 获取favicon（根据extract_icon参数决定）
                favicon_base64 = None
                if extract_icon:
                    remaining_time = timeout - (time.time() - start_time)
                    if remaining_time > 1:  # 至少剩余1秒才尝试获取favicon
                        favicon_timeout = min(3, int(remaining_time - 0.5))
                        favicon_base64 = await self.get_favicon_base64_concurrent(
                            final_url, html_content, favicon_timeout
                        )
                
                execution_time = time.time() - start_time
                
                # 构建基础结果
                result = {
                    "url": url,
                    "final_url": final_url,
                    "status_code": status_code,
                    "execution_time": round(execution_time, 2),
                    "proxy_time": round(proxy_time, 2),
                    "encoding": detected_encoding,
                    "redirects": len(response.history) if hasattr(response, 'history') else 0,
                    "content_type": response_headers.get('content-type', ''),
                    "mode": "fast",
                    "crawler_type": "easyget_http",
                    "javascript_enabled": False,
                    "cookies_used": bool(cookies_string),
                    "cookies_source": "browser+custom" if use_edge_cookies and custom_cookies else 
                                    "browser" if use_edge_cookies else 
                                    "custom" if custom_cookies else "none",
                    "timeout_used": timeout,
                    "success": True,
                    "is_garbled": garbled,
                    "is_binary": is_binary_magic_result,
                    "magic_type": MAGIC_SIGNATURES.get(content[:8], '') if is_binary_magic_result else ''
                }
                
                # 添加可选字段
                if extract_title:
                    result["title"] = page_info.get('title', '')
                
                if extract_icon and favicon_base64:
                    result["icon"] = favicon_base64
                
                # 🔥 关键修改：根据是否已经清理过来决定返回内容
                if cleaned_markdown is not None:
                    # 已经清理过，直接返回markdown，避免上层重复清理
                    result["markdown"] = cleaned_markdown
                    result["text_length"] = len(cleaned_markdown.strip())
                    result["html_cleaned"] = True  # 标记已清理
                else:
                    # 未清理，返回原始html，由上层决定是否清理
                    result["html"] = html_content
                    result["text_length"] = len(html_content.strip())
                    result["html_cleaned"] = False  # 标记未清理
                
                logger.info(f"✅ [EasyGet] 爬取成功: {url} ({status_code})")
                
                return result
        
        except asyncio.TimeoutError as e:
            # 真正的asyncio超时
            execution_time = time.time() - start_time
            logger.error(f"❌ [EasyGet] 真正的asyncio超时: {url} - 耗时: {execution_time:.2f}秒")
            raise  # 重新抛出给上层的asyncio.wait_for处理
            
        except aiohttp.ClientConnectorError as e:
            # 代理连接错误
            execution_time = time.time() - start_time
            error_msg = f"代理连接失败: {str(e)}"
            logger.error(f"❌ [EasyGet] 代理连接错误 {url}: {error_msg} - 耗时: {execution_time:.2f}秒")
            
            return {
                "url": url,
                "error": error_msg,
                "execution_time": round(execution_time, 2),
                "timeout_used": timeout,
                "mode": "fast",
                "crawler_type": "easyget_http",
                "success": False,
                "error_type": "proxy_connection_error"
            }
            
        except aiohttp.ClientConnectionError as e:
            # aiohttp内部超时
            execution_time = time.time() - start_time
            error_msg = f"HTTP请求超时: {str(e)}"
            logger.error(f"❌ [EasyGet] aiohttp超时 {url}: {error_msg} - 耗时: {execution_time:.2f}秒")
            
            return {
                "url": url,
                "error": error_msg,
                "execution_time": round(execution_time, 2),
                "timeout_used": timeout,
                "mode": "fast",
                "crawler_type": "easyget_http",
                "success": False,
                "error_type": "http_timeout"
            }
            
        except aiohttp.ClientError as e:
            # 其他aiohttp客户端错误
            execution_time = time.time() - start_time
            error_msg = f"HTTP客户端错误: {str(e)}"
            logger.error(f"❌ [EasyGet] HTTP客户端错误 {url}: {error_msg} - 耗时: {execution_time:.2f}秒")
            
            return {
                "url": url,
                "error": error_msg,
                "execution_time": round(execution_time, 2),
                "timeout_used": timeout,
                "mode": "fast",
                "crawler_type": "easyget_http",
                "success": False,
                "error_type": "http_client_error"
            }
            
        except Exception as e:
            # 其他未知错误
            execution_time = time.time() - start_time
            error_msg = f"未知错误: {str(e)}"
            logger.error(f"❌ [EasyGet] 未知错误 {url}: {error_msg} - 耗时: {execution_time:.2f}秒")
            
            return {
                "url": url,
                "error": error_msg,
                "execution_time": round(execution_time, 2),
                "timeout_used": timeout,
                "mode": "fast",
                "crawler_type": "easyget_http",
                "success": False,
                "error_type": "unknown_error"
            }
    
    async def crawl_urls_concurrent(self, urls: List[str], concurrent_limit: int = 10, **kwargs) -> Dict[str, Any]:
        """并发爬取多个URL - ✅ 修复并发控制"""
        start_time = time.time()
        
        # 动态调整并发限制
        effective_concurrent_limit = min(concurrent_limit, len(urls), 20)  # 降低最大并发
        
        # 创建信号量限制并发数
        semaphore = asyncio.Semaphore(effective_concurrent_limit)
        
        async def crawl_with_semaphore(url: str, index: int):
            async with semaphore:
                result = await self.crawl_single_url(url, **kwargs)
                return result
        
        # ✅ 真正的并发执行
        logger.info(f"🚀 [EasyGet] 开始并发爬取 {len(urls)} 个URL，并发限制: {effective_concurrent_limit}")
        
        # 创建所有任务
        tasks = [crawl_with_semaphore(url, i) for i, url in enumerate(urls)]
        
        # 并发执行所有任务
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理结果
        successful_results = []
        failed_results = []
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                failed_results.append({
                    "url": urls[i],
                    "error": str(result),
                    "mode": "fast",
                    "crawler_type": "easyget_http",
                    "success": False
                })
            elif result.get("success"):
                successful_results.append(result)
            else:
                failed_results.append(result)
        
        total_time = time.time() - start_time
        
        logger.info(f"🏁 [EasyGet] 并发爬取完成 - 成功: {len(successful_results)}, 失败: {len(failed_results)}, 总耗时: {total_time:.2f}秒")
        
        return {
            "results": successful_results,
            "failed_urls": failed_results,
            "total_urls": len(urls),
            "success_count": len(successful_results),
            "failed_count": len(failed_results),
            "execution_time": round(total_time, 2),
            "concurrent_limit": effective_concurrent_limit,
            "mode": "fast",
            "crawler_type": "easyget_http"
        }
    
    async def cleanup(self):
        """清理资源"""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("✅ EasyGet HTTP会话已关闭") 