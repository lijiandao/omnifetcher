import asyncio
import os
import time
import uuid
import json
import re
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse, unquote
from datetime import datetime
import logging
from playwright.async_api import Page, Download, Browser, BrowserContext
try:
    import PyPDF2
except ImportError:
    PyPDF2 = None
    
try:
    import fitz  # pymupdf
except ImportError:
    fitz = None
from io import BytesIO
import tempfile

# 配置日志
logger = logging.getLogger(__name__)

class EasyPDFCrawler:
    """轻量级PDF下载和文本提取器"""
    
    def __init__(self, download_dir: str = None, static_url_base: str = "/PDF"):
        """
        初始化PDF爬虫
        
        Args:
            download_dir: PDF文件下载目录
            static_url_base: 静态资源URL前缀
        """
        # 设置默认下载目录
        if download_dir is None:
            # 使用临时目录
            self.download_dir = os.path.join(tempfile.gettempdir(), "pdf_downloads")
        else:
            self.download_dir = download_dir
            
        self.static_url_base = static_url_base
        self.ensure_download_dir()
        
        # PDF魔数（文件头标识）
        self.PDF_MAGIC_NUMBERS = [
            b'%PDF-',  # 标准PDF文件头
        ]
        
        logger.info(f"📁 EasyPDF下载目录: {self.download_dir}")
        logger.info(f"🔗 静态URL基础路径: {self.static_url_base}")
        if not PyPDF2:
            logger.warning("⚠️ PyPDF2不可用，PDF文本提取功能受限")
    
    def ensure_download_dir(self):
        """确保下载目录存在"""
        os.makedirs(self.download_dir, exist_ok=True)
        logger.debug(f"📂 确保PDF下载目录存在: {self.download_dir}")
    
    def generate_pdf_filename(self, url: str, original_filename: str = None) -> str:
        """生成PDF文件名"""
        try:
            if original_filename:
                # 清理原始文件名
                clean_name = re.sub(r'[<>:"/\\|?*]', '_', original_filename)
                if clean_name.lower().endswith('.pdf'):
                    return clean_name
                else:
                    return f"{clean_name}.pdf"
            
            # 从URL生成文件名
            parsed = urlparse(url)
            path = parsed.path
            
            if path and path != '/':
                # 提取路径中的文件名
                filename = os.path.basename(path)
                if filename:
                    filename = unquote(filename)  # URL解码
                    # 清理文件名中的特殊字符
                    clean_filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                    if not clean_filename.lower().endswith('.pdf'):
                        clean_filename += '.pdf'
                    return clean_filename
            
            # 使用域名和时间戳生成文件名
            domain = parsed.netloc or 'unknown'
            domain = re.sub(r'[<>:"/\\|?*]', '_', domain)
            timestamp = int(time.time())
            return f"{domain}_{timestamp}.pdf"
            
        except Exception as e:
            logger.warning(f"生成文件名失败: {e}")
            timestamp = int(time.time())
            return f"pdf_{timestamp}.pdf"
    
    def detect_url_type(self, url: str) -> str:
        """检测URL类型"""
        url_lower = url.lower()
        
        # 直接PDF文件链接
        if (url_lower.endswith('.pdf') or 
            '.pdf?' in url_lower or 
            '.pdf#' in url_lower):
            return "direct_pdf"
        
        # 已知的PDF查看器网站
        pdf_viewer_sites = [
            'arxiv.org/pdf/',
            'dl.acm.org/doi/pdf/',
            'proceedings.mlr.press',
            'biorxiv.org/content',
            'medrxiv.org/content',
            'openreview.net',
            'aclanthology.org',
            'ieeexplore.ieee.org',
            'link.springer.com',
            'www.nature.com/articles'
        ]
        
        for site in pdf_viewer_sites:
            if site in url_lower:
                return "pdf_viewer"
        
        return "unknown"
    
    def extract_text_from_pdf(self, file_path: str) -> str:
        """从PDF文件提取文本内容"""
        try:
            text_content = ""
            
            # 首先尝试使用PyMuPDF (fitz)
            if fitz:
                try:
                    doc = fitz.open(file_path)
                    for page_num in range(len(doc)):
                        page = doc.load_page(page_num)
                        text_content += page.get_text()
                    doc.close()
                    logger.debug(f"✅ 使用PyMuPDF成功提取PDF文本: {len(text_content)} 字符")
                    return text_content
                except Exception as e:
                    logger.debug(f"PyMuPDF提取失败，尝试PyPDF2: {e}")
            else:
                logger.debug("PyMuPDF不可用，尝试PyPDF2")
            
            # 如果PyMuPDF失败或不可用，尝试PyPDF2
            if PyPDF2:
                try:
                    with open(file_path, 'rb') as file:
                        pdf_reader = PyPDF2.PdfReader(file)
                        for page in pdf_reader.pages:
                            text_content += page.extract_text()
                    logger.debug(f"✅ 使用PyPDF2成功提取PDF文本: {len(text_content)} 字符")
                    return text_content
                except Exception as e:
                    logger.debug(f"PyPDF2提取也失败: {e}")
            else:
                logger.warning("PyPDF2不可用")
            
            # 如果都失败了，返回错误信息
            logger.warning(f"❌ 无法从PDF提取文本: {file_path} (可能需要安装 PyMuPDF 或 PyPDF2)")
            return "❌ PDF文本提取失败 (缺少PDF处理库)"
            
        except Exception as e:
            logger.error(f"PDF文本提取异常: {e}")
            return f"❌ PDF文本提取异常: {str(e)}"
    
    async def download_pdf_with_page(self, page: Page, url: str, timeout: int = 30000) -> Dict[str, Any]:
        """使用Playwright页面下载PDF - 增强错误处理和页面状态检查"""
        start_time = time.time()
        
        try:
            logger.info(f"🧠 开始PDF下载: {url} (超时: {timeout}ms)")
            
            # 1. 检查页面状态
            if page.is_closed():
                raise Exception("页面已关闭，无法进行PDF下载")
            
            # 🔥 创建整体超时保护
            async def pdf_download_with_timeout():
                # 2. 设置下载处理
                download_info = {"download": None, "download_triggered": False}
            
                def handle_download(download: Download):
                    download_info["download"] = download
                    download_info["download_triggered"] = True
                    logger.info(f"🎯 检测到下载任务: {download.suggested_filename}")
                
                page.on("download", handle_download)
                
                try:
                    # 3. 检测URL类型并采用相应策略
                    url_type = self.detect_url_type(url)
                    logger.debug(f"🔍 检测到URL类型: {url_type}")
                    
                    # 4. 直接处理PDF下载，不重试
                    # 重置下载状态
                    download_info["download"] = None
                    download_info["download_triggered"] = False
                    
                    # 根据URL类型处理，使用动态调整的超时时间
                    actual_timeout = min(timeout, 30000)  # 最大30秒
                    
                    if url_type == "direct_pdf":
                        result = await self.handle_direct_pdf(page, url, download_info, actual_timeout)
                    elif url_type == "pdf_viewer":
                        result = await self.handle_pdf_viewer(page, url, download_info, actual_timeout)
                    else:
                        result = await self.handle_unknown_pdf(page, url, download_info, actual_timeout)
                    
                    if result["download_triggered"]:
                        # 处理下载的文件
                        return await self.process_download(
                            download_info["download"], url, start_time, url_type
                        )
                    else:
                        # 如果没有触发下载，直接抛出异常
                        raise Exception(f"未能触发PDF下载 (URL类型: {url_type})")
                        
                finally:
                    # 移除下载监听器
                    try:
                        page.remove_listener("download", handle_download)
                    except Exception as e:
                        logger.debug(f"移除下载监听器时出错: {e}")
            
            # 🔥 使用asyncio.wait_for来确保整体超时控制
            return await asyncio.wait_for(pdf_download_with_timeout(), timeout=timeout/1000.0)
            
        except Exception as e:
            download_time = time.time() - start_time
            error_msg = str(e)
            
            # 提供更友好的错误信息
            if "frame was detached" in error_msg.lower():
                friendly_error = f"页面连接中断错误: {error_msg}。可能原因：页面被提前关闭或浏览器上下文失效。建议：重新初始化浏览器后重试。"
            elif "net::err_aborted" in error_msg.lower():
                friendly_error = f"请求被中止错误: {error_msg}。可能原因：网络连接问题或服务器拒绝请求。建议：检查网络连接和代理设置。"
            elif "net::err_timed_out" in error_msg.lower():
                friendly_error = f"网络超时错误: {error_msg}。建议：检查网络连接或增加超时时间。"
            else:
                friendly_error = error_msg
            
            logger.error(f"❌ PDF下载失败: {url}: {friendly_error}")
            
            # 🔥 返回与正常爬虫一致的错误格式
            return {
                "success": False,
                "url": url,
                "final_url": url,
                "title": "PDF下载失败",
                "markdown": f"# PDF下载失败\n\n**错误信息:** {friendly_error}\n\n**原始URL:** {url}",
                "text_length": len(f"PDF下载失败: {friendly_error}"),
                "html_size": 0,
                "status_code": 0,
                "execution_time": round(download_time, 2),
                "mode": "pdf",
                "javascript_enabled": True,
                "crawler_type": "easy_pdf",
                "meta_info": {
                    "metas": {
                        "error": friendly_error,
                        "original_error": error_msg,
                        "pdf:url-type": locals().get('url_type', 'unknown'),
                        "pdf:retry-attempts": "1"
                    },
                    "links": 0,
                    "images": 0,
                    "scripts": 0,
                    "text_length": len(f"PDF下载失败: {friendly_error}"),
                    "processing_status": "pdf_download_failed",
                    "multimedia_blocked": True,
                    "content_cleaned": False
                },
                "icon": None,
                "files": [],
                "is_pdf_page": True,
                # 🔥 HTML清理信息（错误情况）
                "html_cleaning": {
                    "enabled": True,
                    "success": False,
                    "error": f"PDF下载失败: {friendly_error}",
                    "original_size": 0,
                    "cleaned_size": 0,
                    "size_reduction": 0,
                    "reduction_percentage": 0,
                    "processing_time": round(download_time, 2),
                    "multimedia_blocked": True,
                    "css_js_removed": True,
                    "content_focused": False,
                    "service_type": "pdf_download_failed",
                    "pdf_processing": False
                },
                # 🔥 资源阻止信息
                "resource_blocking": {
                    "images_blocked": True,
                    "videos_blocked": True,
                    "audio_blocked": True,
                    "css_blocked": True,
                    "js_blocked": True,
                    "fonts_blocked": True,
                    "ads_blocked": True,
                    "analytics_blocked": True
                },
                # 错误详情（保留原有信息）
                "error_details": {
                    "friendly_error": friendly_error,
                    "original_error": error_msg,
                    "download_time": round(download_time, 2),
                    "url_type": locals().get('url_type', 'unknown'),
                    "retry_attempts": 1
                }
            }
    
    async def handle_direct_pdf(self, page: Page, url: str, download_info: Dict, timeout: int) -> Dict[str, Any]:
        """处理直接PDF链接"""
        logger.debug("📥 处理直接PDF链接...")
        
        try:
            actual_timeout = min(timeout, 25000)
            logger.debug(f"🕐 设置导航超时: {actual_timeout}ms")
            
            # 直接导航到PDF URL
            response = await page.goto(url, timeout=actual_timeout, wait_until="domcontentloaded")
            
            if not response:
                raise Exception("页面导航失败，未收到响应")
            
            logger.debug(f"✅ 导航成功，状态码: {response.status}")
            
            # 等待自动下载触发
            for i in range(6):
                if download_info["download_triggered"]:
                    logger.debug("✅ 检测到自动下载")
                    return {"download_triggered": True}
                await asyncio.sleep(1)
            
            # 如果没有自动下载，尝试强制下载
            content_type = response.headers.get("content-type", "").lower()
            if "application/pdf" in content_type:
                logger.debug("📄 PDF在浏览器中显示，尝试Ctrl+S下载...")
                
                # 尝试Ctrl+S（一次）
                try:
                    await page.keyboard.down('Control')
                    await page.keyboard.press('s')
                    await page.keyboard.up('Control')
                    await asyncio.sleep(4)
                    
                    if download_info["download_triggered"]:
                        logger.debug("✅ Ctrl+S触发下载成功")
                        return {"download_triggered": True}
                except Exception as e:
                    logger.debug(f"Ctrl+S尝试失败: {e}")
            
            return {"download_triggered": download_info["download_triggered"]}
            
        except Exception as e:
            logger.error(f"处理直接PDF链接失败: {e}")
            raise
    
    async def handle_pdf_viewer(self, page: Page, url: str, download_info: Dict, timeout: int) -> Dict[str, Any]:
        """处理PDF查看器页面 - 增强ACM处理和错误恢复"""
        logger.debug("📄 处理PDF查看器页面...")
        
        try:
            # 1. 检查页面状态
            if page.is_closed():
                raise Exception("页面已关闭，无法处理PDF查看器")
            
            # 2. 设置超时和导航策略
            actual_timeout = min(timeout, 20000)
            logger.debug(f"🕐 设置导航超时: {actual_timeout}ms")
            
            # 3. ACM Digital Library特殊处理
            is_acm = "dl.acm.org" in url.lower()
            if is_acm:
                logger.debug("🎓 检测到ACM Digital Library，使用增强处理策略...")
                # ACM需要更长的超时时间
                actual_timeout = min(timeout, 25000)
            
            # 4. 导航到PDF查看器页面，不重试
            response = await page.goto(url, timeout=actual_timeout, wait_until="domcontentloaded")
            
            if not response:
                raise Exception("页面导航失败，未收到响应")
            
            logger.debug(f"✅ 导航成功，状态码: {response.status}")
            
            # 5. 智能等待页面加载完成
            try:
                if is_acm:
                    # ACM需要更长的网络等待时间
                    await page.wait_for_load_state("networkidle", timeout=10000)
                else:
                    await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception as e:
                logger.debug(f"网络等待超时，继续处理: {e}")
                # 不抛出异常，继续处理
            
            # 6. 检查是否已经自动触发下载
            if download_info["download_triggered"]:
                logger.debug("✅ 页面加载过程中自动触发了下载")
                return {"download_triggered": True}
            
            # 7. 特殊站点处理策略
            if is_acm:
                logger.debug("🎓 执行ACM Digital Library特殊处理...")
                # ACM需要额外等待时间确保页面完全加载
                await asyncio.sleep(3)
                
                # 检查页面是否有PDF内容
                try:
                    pdf_content_exists = await page.evaluate("""
                        () => {
                            // 检查是否存在PDF相关元素
                            const pdfElements = document.querySelectorAll('embed[type="application/pdf"], object[type="application/pdf"], iframe[src*=".pdf"]');
                            const pdfLinks = document.querySelectorAll('a[href*=".pdf"]');
                            return pdfElements.length > 0 || pdfLinks.length > 0;
                        }
                    """)
                    
                    if pdf_content_exists:
                        logger.debug("✅ 检测到PDF内容元素")
                    else:
                        logger.debug("⚠️ 未检测到明显的PDF内容元素")
                        
                except Exception as e:
                    logger.debug(f"PDF内容检测失败: {e}")
            
            # 8. 使用Ctrl+S触发下载，不重试
            try:
                # 执行Ctrl+S
                await page.keyboard.down('Control')
                await page.keyboard.press('s')
                await page.keyboard.up('Control')
                
                # 智能等待下载触发
                wait_time = 10 if is_acm else 6
                for i in range(wait_time):
                    if download_info["download_triggered"]:
                        logger.debug("✅ Ctrl+S成功触发下载")
                        return {"download_triggered": True}
                    await asyncio.sleep(1)
                    
                    # 在等待过程中也检查页面状态
                    if page.is_closed():
                        raise Exception("页面在等待下载过程中被关闭")
                
                logger.debug("⏰ Ctrl+S等待超时")
                
            except Exception as e:
                error_msg = str(e).lower()
                if "page" in error_msg and "clos" in error_msg:
                    raise Exception(f"Ctrl+S过程中页面被关闭: {e}")
                else:
                    logger.warning(f"⚠️ Ctrl+S失败: {e}")
            
            logger.debug("⚠️ Ctrl+S未能触发下载")
            return {"download_triggered": download_info["download_triggered"]}
            
        except Exception as e:
            logger.error(f"处理PDF查看器页面失败: {e}")
            raise
    
    async def handle_unknown_pdf(self, page: Page, url: str, download_info: Dict, timeout: int) -> Dict[str, Any]:
        """处理未知类型的PDF URL"""
        logger.debug("❓ 处理未知类型PDF URL...")
        
        try:
            # 先尝试按PDF查看器方式处理
            result = await self.handle_pdf_viewer(page, url, download_info, timeout)
            
            if result["download_triggered"]:
                return result
            
            # 如果没有成功，尝试按直接PDF方式处理
            logger.debug("🔄 尝试按直接PDF方式处理...")
            return await self.handle_direct_pdf(page, url, download_info, timeout)
            
        except Exception as e:
            logger.error(f"处理未知类型PDF失败: {e}")
            raise
    
    async def process_download(self, download: Download, url: str, start_time: float, url_type: str) -> Dict[str, Any]:
        """处理下载的文件"""
        try:
            # 生成文件名
            original_filename = download.suggested_filename
            filename = self.generate_pdf_filename(url, original_filename)
            file_path = os.path.join(self.download_dir, filename)
            
            # 保存文件
            logger.debug(f"💾 保存文件到: {file_path}")
            
            try:
                await download.save_as(file_path)
            except Exception as save_error:
                error_msg = str(save_error).lower()
                
                # 处理下载取消错误
                if "canceled" in error_msg or "cancelled" in error_msg:
                    logger.warning(f"⚠️ 下载被取消，尝试获取下载流: {url}")
                    
                    try:
                        stream = await download.create_read_stream()
                        if stream:
                            with open(file_path, 'wb') as f:
                                async for chunk in stream:
                                    f.write(chunk)
                            logger.debug("✅ 通过流成功保存文件")
                        else:
                            raise Exception("无法获取下载流")
                    except Exception as stream_error:
                        logger.error(f"❌ 流保存也失败: {stream_error}")
                        raise Exception(f"下载被取消且无法获取下载流: {stream_error}")
                else:
                    raise save_error
            
            # 验证文件
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                file_size = os.path.getsize(file_path)
                file_size_mb = file_size / (1024 * 1024)
                download_time = time.time() - start_time
                
                # 提取PDF文本内容
                logger.debug("📄 开始提取PDF文本内容...")
                # ⚠️ 重要：提取 PDF 文本是耗时的同步操作，放到线程池中执行以避免阻塞事件循环
                pdf_text = await asyncio.to_thread(self.extract_text_from_pdf, file_path)
                
                # 生成静态URL
                static_url = f"{self.static_url_base}/{filename}"
                
                logger.info(f"✅ PDF下载成功: {filename} ({file_size} bytes, {download_time:.2f}s)")
                
                # 🔥 转换PDF文本为Markdown格式（与正常爬虫保持一致）
                pdf_markdown = f"""# {filename}
**文件信息:**
- 文件大小: {file_size_mb:.2f}MB
- 下载时间: {download_time:.2f}s
- 下载链接: [{filename}]({static_url})
---
**PDF文本内容:**
{pdf_text}
"""
                
                return {
                    "success": True,
                    "url": url,
                    "final_url": url,
                    "title": filename,
                    "markdown": pdf_markdown,  # 🔥 只返回markdown格式，与正常爬虫保持一致
                    "text": pdf_text,  # ➕ 新增：提供原始提取文本，供上层逻辑使用
                    "text_length": len(pdf_markdown),  # 使用markdown长度
                    "html_size": file_size,  # 使用原始PDF文件大小
                    "status_code": 200,
                    "execution_time": round(download_time, 2),
                    "mode": "pdf",
                    "javascript_enabled": True,
                    "crawler_type": "easy_pdf",
                    "meta_info": {
                        "metas": {
                            "content-type": "application/pdf",
                            "pdf:file-size": f"{file_size_mb:.2f}MB",
                            "pdf:original-filename": original_filename or filename,
                            "pdf:url-type": url_type
                        },
                        "links": 1,  # 下载链接
                        "images": 0,
                        "scripts": 0,
                        "text_length": len(pdf_markdown),
                        "processing_status": "pdf_processed",
                        "multimedia_blocked": True,
                        "content_cleaned": True
                    },
                    "icon": None,  # PDF文件没有favicon
                    "files": [{
                        "filename": filename,
                        "file_path": file_path,
                        "static_url": static_url,
                        "file_size": file_size,
                        "file_size_mb": round(file_size_mb, 2),
                        "content_type": "application/pdf",
                        "original_filename": original_filename,
                        "url_type": url_type,
                        "download_time": round(download_time, 2)
                    }],
                    "is_pdf_page": True,
                    # 将files中的关键字段提升到根级别，方便上层逻辑直接获取
                    "static_url": static_url,
                    "file_path": file_path,
                    "filename": filename,
                    "file_size": file_size,
                    "file_size_mb": round(file_size_mb, 2),
                    # 🔥 HTML清理信息（与正常爬虫保持一致的格式）
                    "html_cleaning": {
                        "enabled": True,  # PDF处理等同于HTML清理
                        "success": True,
                        "error": None,
                        "original_size": file_size,
                        "cleaned_size": len(pdf_markdown),
                        "size_reduction": max(0, file_size - len(pdf_markdown)),
                        "reduction_percentage": round(max(0, (file_size - len(pdf_markdown)) / file_size * 100), 2) if file_size > 0 else 0,
                        "processing_time": round(download_time, 2),
                        "multimedia_blocked": True,
                        "css_js_removed": True,
                        "content_focused": True,
                        "service_type": "pdf_text_extraction",
                        "pdf_processing": True
                    },
                    # 🔥 资源阻止信息（与正常爬虫保持一致）
                    "resource_blocking": {
                        "images_blocked": True,
                        "videos_blocked": True,
                        "audio_blocked": True,
                        "css_blocked": True,
                        "js_blocked": True,
                        "fonts_blocked": True,
                        "ads_blocked": True,
                        "analytics_blocked": True
                    }
                }
            else:
                raise Exception("下载的文件不存在或为空")
                
        except Exception as e:
            logger.error(f"处理下载文件失败: {e}")
            raise
    
    def is_pdf_url(self, url: str, response_headers: Dict[str, str] = None) -> bool:
        """判断URL是否指向PDF文件"""
        try:
            # 1. 检查URL后缀
            url_lower = url.lower()
            if (url_lower.endswith('.pdf') or 
                '.pdf?' in url_lower or 
                '.pdf#' in url_lower):
                return True
            
            # 2. 检查响应头的Content-Type
            if response_headers:
                content_type = response_headers.get('content-type', '').lower()
                if 'application/pdf' in content_type:
                    return True
            
            # 3. 检查已知的PDF网站
            pdf_sites = [
                'arxiv.org/pdf/',
                'dl.acm.org/doi/pdf/',
                'proceedings.mlr.press',
                'biorxiv.org/content',
                'medrxiv.org/content'
            ]
            
            for site in pdf_sites:
                if site in url_lower:
                    return True
            
            return False
            
        except Exception as e:
            logger.debug(f"PDF URL检测失败: {e}")
            return False
    
    def cleanup_old_files(self, max_age_hours: int = 24):
        """清理过期的PDF文件"""
        try:
            logger.info(f"🧹 开始清理 {max_age_hours} 小时前的PDF文件...")
            
            current_time = time.time()
            cutoff_time = current_time - (max_age_hours * 3600)
            
            cleaned_count = 0
            cleaned_size = 0
            
            if os.path.exists(self.download_dir):
                for filename in os.listdir(self.download_dir):
                    if filename.endswith('.pdf'):
                        file_path = os.path.join(self.download_dir, filename)
                        try:
                            file_mtime = os.path.getmtime(file_path)
                            if file_mtime < cutoff_time:
                                file_size = os.path.getsize(file_path)
                                os.remove(file_path)
                                cleaned_size += file_size
                                cleaned_count += 1
                                logger.debug(f"🗑️ 删除过期PDF: {filename}")
                        except Exception as e:
                            logger.debug(f"删除文件失败 {filename}: {e}")
            
            if cleaned_count > 0:
                cleaned_size_mb = cleaned_size / (1024 * 1024)
                logger.info(f"🧹 清理完成: 删除 {cleaned_count} 个过期PDF文件, 释放 {cleaned_size_mb:.2f}MB 空间")
            else:
                logger.info("🧹 清理完成: 没有需要清理的文件")
            
        except Exception as e:
            logger.error(f"❌ 清理失败: {e}")

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





    async def perform_magic_number_check(self, page: Page, url: str) -> Dict[str, Any]:
        """
        对application/octet-stream类型的URL执行魔数校验
        
        Args:
            page: Playwright页面对象
            url: 要检测的URL
            
        Returns:
            Dict包含魔数校验结果
        """
        import time
        start_time = time.time()
        
        try:
            logger.info(f"🔍 开始魔数校验: {url}")
            
            # 记录GET请求开始时间
            get_request_start = time.time()
            # 发送GET请求获取文件头部数据（只需要前几个字节）
            response = await page.request.get(url, timeout=10000)
            get_request_time = time.time() - get_request_start
            
            if not response:
                total_time = time.time() - start_time
                logger.warning(f"⚠️ 魔数校验GET请求失败: {url} (耗时: {total_time:.3f}s)")
                return {
                    "success": False,
                    "error": "GET请求失败",
                    "is_pdf": False,
                    "magic_check_performed": True,
                    "timing": {
                        "total_time": round(total_time, 3),
                        "get_request_time": round(get_request_time, 3),
                        "body_read_time": 0,
                        "magic_check_time": 0
                    }
                }
            
            # 记录读取响应体开始时间
            body_read_start = time.time()
            # 获取响应体的前几个字节用于魔数检测
            body_bytes = await response.body()
            body_read_time = time.time() - body_read_start
            
            if not body_bytes:
                total_time = time.time() - start_time
                logger.warning(f"⚠️ 魔数校验响应体为空: {url} (耗时: {total_time:.3f}s)")
                return {
                    "success": False,
                    "error": "响应体为空",
                    "is_pdf": False,
                    "magic_check_performed": True,
                    "timing": {
                        "total_time": round(total_time, 3),
                        "get_request_time": round(get_request_time, 3),
                        "body_read_time": round(body_read_time, 3),
                        "magic_check_time": 0
                    }
                }
            
            # 记录魔数检测开始时间
            magic_check_start = time.time()
            # 只检查前20个字节就够了
            file_header = body_bytes[:20]
            is_pdf = self.is_pdf_by_magic_number(file_header)
            magic_check_time = time.time() - magic_check_start
            
            total_time = time.time() - start_time
            
            timing_info = {
                "total_time": round(total_time, 3),
                "get_request_time": round(get_request_time, 3),
                "body_read_time": round(body_read_time, 3),
                "magic_check_time": round(magic_check_time, 3)
            }
            
            result = {
                "success": True,
                "is_pdf": is_pdf,
                "is_web": not is_pdf,
                "magic_check_performed": True,
                "file_header": file_header.hex() if len(file_header) <= 20 else file_header[:20].hex(),
                "file_size": len(body_bytes),
                "detection_method": "magic_number_check",
                "timing": timing_info
            }
            
            if is_pdf:
                result["reason"] = "魔数校验确认为PDF文件"
                result["action"] = "执行PDF下载和文本提取"
                logger.info(f"✅ 魔数校验: {url} 确认为PDF文件 (总耗时: {timing_info['total_time']}s, GET请求: {timing_info['get_request_time']}s, 读取响应: {timing_info['body_read_time']}s, 魔数检测: {timing_info['magic_check_time']}s)")
            else:
                result["reason"] = "魔数校验确认不是PDF文件"
                result["action"] = "关闭页面拒绝下载"
                logger.info(f"❌ 魔数校验: {url} 不是PDF文件，拒绝下载 (总耗时: {timing_info['total_time']}s, GET请求: {timing_info['get_request_time']}s, 读取响应: {timing_info['body_read_time']}s, 魔数检测: {timing_info['magic_check_time']}s)")
            
            return result
            
        except Exception as e:
            total_time = time.time() - start_time
            logger.warning(f"⚠️ 魔数校验异常: {url}: {e} (耗时: {total_time:.3f}s)")
            return {
                "success": False,
                "error": str(e),
                "is_pdf": False,
                "is_web": True,
                "magic_check_performed": False,
                "detection_method": "magic_check_error",
                "timing": {
                    "total_time": round(total_time, 3),
                    "get_request_time": 0,
                    "body_read_time": 0,
                    "magic_check_time": 0
                }
            } 

    async def download_pdf_via_request(self, page: Page, url: str, timeout: int = 30000) -> Dict[str, Any]:
        """使用 Playwright 的 request API 直接下载 PDF，避免再次导航/新页面。"""
        start_time = time.time()
        try:
            logger.info(f"📥 通过request直接下载PDF: {url} (超时: {timeout}ms)")
            response = await page.request.get(url, timeout=timeout)
            if not response:
                return {
                    "success": False,
                    "url": url,
                    "final_url": url,
                    "title": "PDF下载失败",
                    "markdown": f"# PDF下载失败\n\n**错误信息:** 无响应\n\n**原始URL:** {url}",
                    "text_length": len("PDF下载失败: 无响应"),
                    "html_size": 0,
                    "status_code": 0,
                    "execution_time": round(time.time() - start_time, 2),
                    "mode": "pdf",
                    "javascript_enabled": True,
                    "crawler_type": "easy_pdf",
                    "is_pdf_page": True
                }
            status = response.status
            if status != 200:
                error_msg = f"HTTP状态异常: {status}"
                logger.warning(f"⚠️ {error_msg}")
                return {
                    "success": False,
                    "url": url,
                    "final_url": url,
                    "title": "PDF下载失败",
                    "markdown": f"# PDF下载失败\n\n**错误信息:** {error_msg}\n\n**原始URL:** {url}",
                    "text_length": len(f"PDF下载失败: {error_msg}"),
                    "html_size": 0,
                    "status_code": status,
                    "execution_time": round(time.time() - start_time, 2),
                    "mode": "pdf",
                    "javascript_enabled": True,
                    "crawler_type": "easy_pdf",
                    "is_pdf_page": True
                }
            body_bytes = await response.body()
            if not body_bytes:
                error_msg = "响应体为空"
                logger.warning(f"⚠️ {error_msg}")
                return {
                    "success": False,
                    "url": url,
                    "final_url": url,
                    "title": "PDF下载失败",
                    "markdown": f"# PDF下载失败\n\n**错误信息:** {error_msg}\n\n**原始URL:** {url}",
                    "text_length": len(f"PDF下载失败: {error_msg}"),
                    "html_size": 0,
                    "status_code": status,
                    "execution_time": round(time.time() - start_time, 2),
                    "mode": "pdf",
                    "javascript_enabled": True,
                    "crawler_type": "easy_pdf",
                    "is_pdf_page": True
                }
            # 魔数校验
            header = body_bytes[:20]
            if not self.is_pdf_by_magic_number(header):
                logger.warning("⚠️ 下载内容非PDF魔数，拒绝处理")
                return {
                    "success": False,
                    "url": url,
                    "final_url": url,
                    "title": "非PDF内容",
                    "markdown": f"# 不是PDF文件\n\n**URL:** {url}",
                    "text_length": len(f"不是PDF文件: {url}"),
                    "html_size": 0,
                    "status_code": status,
                    "execution_time": round(time.time() - start_time, 2),
                    "mode": "pdf",
                    "javascript_enabled": True,
                    "crawler_type": "easy_pdf",
                    "is_pdf_page": False
                }
            # 确保目录
            self.ensure_download_dir()
            # 尝试从 Content-Disposition 获取文件名
            suggested_name = None
            try:
                cd = response.headers.get("content-disposition", "")
                if "filename=" in cd:
                    m = re.search(r'filename\*?=\"?([^;\"]+)\"?', cd, re.IGNORECASE)
                    if m:
                        suggested_name = m.group(1)
            except Exception:
                suggested_name = None
            filename = self.generate_pdf_filename(url, suggested_name)
            file_path = os.path.join(self.download_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(body_bytes)
            file_size = len(body_bytes)
            file_size_mb = file_size / (1024 * 1024)
            pdf_text = await asyncio.to_thread(self.extract_text_from_pdf, file_path)
            static_url = f"{self.static_url_base}/{filename}"
            download_time = time.time() - start_time
            pdf_markdown = f"""# {filename}
**文件信息:**
- 文件大小: {file_size_mb:.2f}MB
- 下载时间: {download_time:.2f}s
- 下载链接: [{filename}]({static_url})
---
**PDF文本内容:**
{pdf_text}
"""
            return {
                "success": True,
                "url": url,
                "final_url": url,
                "title": filename,
                "markdown": pdf_markdown,
                "text": pdf_text,
                "text_length": len(pdf_markdown),
                "html_size": file_size,
                "status_code": status,
                "execution_time": round(download_time, 2),
                "mode": "pdf",
                "javascript_enabled": True,
                "crawler_type": "easy_pdf",
                "meta_info": {
                    "metas": {
                        "content-type": "application/pdf",
                        "pdf:file-size": f"{file_size_mb:.2f}MB",
                        "pdf:original-filename": suggested_name or filename,
                        "pdf:url-type": "request_download"
                    },
                    "links": 1,
                    "images": 0,
                    "scripts": 0,
                    "text_length": len(pdf_markdown),
                    "processing_status": "pdf_processed",
                    "multimedia_blocked": True,
                    "content_cleaned": True
                },
                "icon": None,
                "files": [{
                    "filename": filename,
                    "file_path": file_path,
                    "static_url": static_url,
                    "file_size": file_size,
                    "file_size_mb": round(file_size_mb, 2),
                    "content_type": "application/pdf",
                    "original_filename": suggested_name,
                    "url_type": "request_download",
                    "download_time": round(download_time, 2)
                }],
                "is_pdf_page": True,
                "static_url": static_url,
                "file_path": file_path,
                "filename": filename,
                "file_size": file_size,
                "file_size_mb": round(file_size_mb, 2),
                "html_cleaning": {
                    "enabled": True,
                    "success": True,
                    "error": None,
                    "original_size": file_size,
                    "cleaned_size": len(pdf_markdown),
                    "size_reduction": max(0, file_size - len(pdf_markdown)),
                    "reduction_percentage": round(max(0, (file_size - len(pdf_markdown)) / file_size * 100), 2) if file_size > 0 else 0,
                    "processing_time": round(download_time, 2),
                    "multimedia_blocked": True,
                    "css_js_removed": True,
                    "content_focused": True,
                    "service_type": "pdf_text_extraction",
                    "pdf_processing": True
                },
                "resource_blocking": {
                    "images_blocked": True,
                    "videos_blocked": True,
                    "audio_blocked": True,
                    "css_blocked": True,
                    "js_blocked": True,
                    "fonts_blocked": True,
                    "ads_blocked": True,
                    "analytics_blocked": True
                }
            }
        except Exception as e:
            logger.error(f"❌ request直接下载PDF失败: {e}")
            return {
                "success": False,
                "url": url,
                "final_url": url,
                "title": "PDF下载失败",
                "markdown": f"# PDF下载失败\n\n**错误信息:** {str(e)}\n\n**原始URL:** {url}",
                "text_length": len(f"PDF下载失败: {str(e)}"),
                "html_size": 0,
                "status_code": 0,
                "execution_time": round(time.time() - start_time, 2),
                "mode": "pdf",
                "javascript_enabled": True,
                "crawler_type": "easy_pdf",
                "is_pdf_page": True
            }