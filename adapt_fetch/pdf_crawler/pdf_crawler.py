import asyncio
import os
import time
import uuid
import shutil
import json
import threading
import schedule
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse, urljoin
from datetime import datetime, timedelta
import logging
from playwright.async_api import Page, Download, Browser, BrowserContext, Response
import aiofiles

# 配置日志
logger = logging.getLogger(__name__)

class PDFCrawler:
    """PDF自动下载爬虫类 - 通过网络请求检测和页面点击下载"""
    
    def __init__(self, download_dir: str = None, static_url_base: str = "/PDF"):
        """
        初始化PDF爬虫
        
        Args:
            download_dir: PDF文件下载目录，默认为桌面STATIC路径
            static_url_base: 静态资源URL前缀
        """
        # 设置默认下载目录
        if download_dir is None:
            # Windows桌面路径
            desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
            self.download_dir = os.path.join(desktop_path, "STATIC", "RESOURCE", "PDF")
        else:
            self.download_dir = download_dir
            
        self.static_url_base = static_url_base
        self.ensure_download_dir()
        
        # 初始化文件缓存管理
        self.cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "file_resource_cache")
        self.cache_file = os.path.join(self.cache_dir, "pdf_files_cache.json")
        self.ensure_cache_dir()
        
        # 启动定时清理任务
        self.start_cleanup_scheduler()
        
        logger.info(f"📁 PDF爬虫初始化完成，下载目录: {self.download_dir}")
        logger.info(f"🔗 静态URL前缀: {self.static_url_base}")
        logger.info(f"📋 缓存文件: {self.cache_file}")
    
    def ensure_download_dir(self):
        """确保下载目录存在"""
        os.makedirs(self.download_dir, exist_ok=True)
        logger.debug(f"📂 确保PDF下载目录存在: {self.download_dir}")
    
    def ensure_cache_dir(self):
        """确保缓存目录存在"""
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # 如果缓存文件不存在，创建空的缓存文件
        if not os.path.exists(self.cache_file):
            self.save_cache({})
        
        logger.debug(f"📂 确保缓存目录存在: {self.cache_dir}")
    
    def load_cache(self) -> Dict[str, Any]:
        """加载PDF文件缓存信息"""
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.debug(f"加载缓存文件失败，返回空缓存: {e}")
            return {}
    
    def save_cache(self, cache_data: Dict[str, Any]):
        """保存PDF文件缓存信息"""
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            logger.debug(f"缓存信息已保存到: {self.cache_file}")
        except Exception as e:
            logger.error(f"保存缓存文件失败: {e}")
    
    def add_file_to_cache(self, filename: str, file_path: str, url: str, file_size: int):
        """将文件信息添加到缓存"""
        try:
            cache = self.load_cache()
            
            cache[filename] = {
                "file_path": file_path,
                "url": url,
                "file_size": file_size,
                "download_time": datetime.now().isoformat(),
                "last_access": datetime.now().isoformat()
            }
            
            self.save_cache(cache)
            logger.debug(f"文件已添加到缓存: {filename}")
        except Exception as e:
            logger.error(f"添加文件到缓存失败: {e}")
    
    def start_cleanup_scheduler(self):
        """启动定时清理任务"""
        try:
            # 设置每天晚上12点清理
            schedule.every().day.at("00:00").do(self.cleanup_old_files_scheduled)
            
            # 在后台线程中运行调度器
            def run_scheduler():
                while True:
                    schedule.run_pending()
                    time.sleep(60)  # 每分钟检查一次
            
            scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
            scheduler_thread.start()
            
            logger.info("🕛 定时清理任务已启动，每天晚上12:00清理12小时前的PDF文件")
        except Exception as e:
            logger.error(f"启动定时清理任务失败: {e}")
    
    def cleanup_old_files_scheduled(self):
        """定时清理任务（12小时前的文件）"""
        try:
            logger.info("🧹 开始定时清理PDF文件...")
            
            cache = self.load_cache()
            current_time = datetime.now()
            cutoff_time = current_time - timedelta(hours=12)
            
            files_to_remove = []
            cleaned_count = 0
            cleaned_size = 0
            
            for filename, file_info in cache.items():
                try:
                    download_time = datetime.fromisoformat(file_info["download_time"])
                    
                    if download_time < cutoff_time:
                        file_path = file_info["file_path"]
                        
                        # 删除物理文件
                        if os.path.exists(file_path):
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            cleaned_size += file_size
                            cleaned_count += 1
                            logger.debug(f"🗑️ 删除过期PDF: {filename}")
                        
                        files_to_remove.append(filename)
                        
                except Exception as e:
                    logger.error(f"处理文件 {filename} 时出错: {e}")
                    files_to_remove.append(filename)
            
            # 从缓存中移除已删除的文件
            for filename in files_to_remove:
                cache.pop(filename, None)
            
            # 保存更新后的缓存
            self.save_cache(cache)
            
            if cleaned_count > 0:
                cleaned_size_mb = cleaned_size / (1024 * 1024)
                logger.info(f"🧹 定时清理完成: 删除 {cleaned_count} 个过期PDF文件, 释放 {cleaned_size_mb:.2f}MB 空间")
            else:
                logger.info("🧹 定时清理完成: 没有需要清理的文件")
                
        except Exception as e:
            logger.error(f"❌ 定时清理失败: {e}")
    
    def generate_pdf_filename(self, url: str, original_filename: str = None, custom_filename: str = None) -> str:
        """
        生成PDF文件名
        
        Args:
            url: 原始URL
            original_filename: 原始文件名
            custom_filename: 用户自定义文件名
            
        Returns:
            生成的文件名
        """
        # 如果用户提供了自定义文件名，优先使用
        if custom_filename:
            safe_name = "".join(c for c in custom_filename if c.isalnum() or c in "-_")[:50]
            if not safe_name.endswith('.pdf'):
                safe_name += '.pdf'
            return safe_name
        
        # 生成唯一ID
        unique_id = str(uuid.uuid4())[:8]
        
        # 从URL提取有意义的名称
        parsed_url = urlparse(url)
        
        if original_filename:
            # 使用原始文件名
            name = os.path.splitext(original_filename)[0]
        elif "arxiv.org" in parsed_url.netloc:
            # ArXiv特殊处理 - 只有/pdf/路径才生成arxiv文件名
            if "/pdf/" in parsed_url.path:
                arxiv_id = parsed_url.path.split("/pdf/")[-1].split(".")[0]
                name = f"arxiv_{arxiv_id}"
            else:
                # 对于非PDF路径，使用通用命名
                path_parts = [p for p in parsed_url.path.split("/") if p]
                if path_parts:
                    name = path_parts[-1].split(".")[0]
                    if not name or len(name) < 3:
                        name = "arxiv_paper"
                else:
                    name = "arxiv_paper"
        elif "biorxiv.org" in parsed_url.netloc or "medrxiv.org" in parsed_url.netloc:
            # bioRxiv/medRxiv处理
            if "/content/" in parsed_url.path:
                paper_info = parsed_url.path.split("/content/")[-1].split("/")[0]
                name = f"biorxiv_{paper_info}"
            else:
                name = "biorxiv_paper"
        elif "proceedings.mlr.press" in parsed_url.netloc:
            # PMLR处理
            if "/v" in parsed_url.path and "/" in parsed_url.path:
                parts = parsed_url.path.strip("/").split("/")
                if len(parts) >= 3:
                    volume = parts[0]  # v139
                    paper_id = parts[1]  # dong21a
                    name = f"pmlr_{volume}_{paper_id}"
                else:
                    name = "pmlr_paper"
            else:
                name = "pmlr_paper"
        else:
            # 通用处理：从URL路径提取
            path_parts = [p for p in parsed_url.path.split("/") if p]
            if path_parts:
                name = path_parts[-1].split(".")[0]
                if not name or len(name) < 3:
                    name = parsed_url.netloc.replace(".", "_")
            else:
                name = parsed_url.netloc.replace(".", "_")
        
        # 清理文件名，移除特殊字符
        safe_name = "".join(c for c in name if c.isalnum() or c in "-_")[:50]
        if not safe_name:
            safe_name = "pdf_file"
        
        return f"{safe_name}_{unique_id}.pdf"
    
    def detect_url_type(self, url: str) -> str:
        """
        检测URL类型
        
        Args:
            url: 要检测的URL
            
        Returns:
            URL类型: 'direct_pdf', 'pdf_viewer', 'unknown'
        """
        url_lower = url.lower()
        
        # 直接PDF文件链接
        if (url_lower.endswith('.pdf') or 
            '.pdf?' in url_lower or 
            '.pdf#' in url_lower):
            return "direct_pdf"
        
        # 已知的PDF查看器网站
        pdf_viewer_sites = [
            'arxiv.org/pdf/',  # 只有arxiv.org/pdf/路径才是PDF
            'proceedings.mlr.press',
            'biorxiv.org/content',
            'medrxiv.org/content',
            'openreview.net',
            'aclanthology.org',
            'dl.acm.org/doi/pdf/',  # ACM Digital Library
            'ieeexplore.ieee.org',  # IEEE Xplore
            'link.springer.com',    # Springer
            'www.nature.com/articles'  # Nature
        ]
        
        for site in pdf_viewer_sites:
            if site in url_lower:
                return "pdf_viewer"
        
        return "unknown"
    
    async def process_pdf_page(self, page: Page, url: str,
                             auto_download: bool = True,
                             download_timeout: int = 10000, max_size_mb: int = 10) -> Dict[str, Any]:
        """
        处理PDF页面（向后兼容方法）
        
        Args:
            page: Playwright页面对象
            url: PDF页面URL
            auto_download: 是否自动下载PDF
            download_timeout: 下载超时时间
            max_size_mb: 最大文件大小（已废弃）
            
        Returns:
            处理结果字典
        """
        try:
            # 默认直接下载并提取PDF文本
            from adapt_fetch.easy_pdf_crawler.easy_pdf_crawler import EasyPDFCrawler
            easy_pdf_crawler = EasyPDFCrawler(self.download_dir, self.static_url_base)
            
            if auto_download:
                logger.info(f"🔄 自动下载PDF模式已启用，开始下载: {url}")
                return await easy_pdf_crawler.download_pdf_with_page(page, url, download_timeout)
            else:
                logger.info(f"ℹ️ 自动下载PDF模式已禁用，仅检测PDF: {url}")
                # 只检测PDF，不下载
                return {
                    "success": True,
                    "url": url,
                    "is_pdf_page": True,
                    "files": [],
                    "message": "PDF页面检测成功，未下载"
                }
        except Exception as e:
            logger.error(f"处理PDF页面失败: {e}")
            return {
                "success": False,
                "url": url,
                "error": str(e),
                "is_pdf_page": False,
                "files": []
            }
    
    def cleanup_old_files(self, max_age_hours: int = 12):
        """
        手动清理过期的PDF文件
        
        Args:
            max_age_hours: 文件最大保留小时数，默认12小时
        """
        try:
            logger.info(f"🧹 开始手动清理 {max_age_hours} 小时前的PDF文件...")
            
            cache = self.load_cache()
            current_time = datetime.now()
            cutoff_time = current_time - timedelta(hours=max_age_hours)
            
            files_to_remove = []
            cleaned_count = 0
            cleaned_size = 0
            
            for filename, file_info in cache.items():
                try:
                    download_time = datetime.fromisoformat(file_info["download_time"])
                    
                    if download_time < cutoff_time:
                        file_path = file_info["file_path"]
                        
                        # 删除物理文件
                        if os.path.exists(file_path):
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            cleaned_size += file_size
                            cleaned_count += 1
                            logger.debug(f"🗑️ 删除过期PDF: {filename}")
                        
                        files_to_remove.append(filename)
                        
                except Exception as e:
                    logger.error(f"处理文件 {filename} 时出错: {e}")
                    files_to_remove.append(filename)
            
            # 从缓存中移除已删除的文件
            for filename in files_to_remove:
                cache.pop(filename, None)
            
            # 保存更新后的缓存
            self.save_cache(cache)
            
            if cleaned_count > 0:
                cleaned_size_mb = cleaned_size / (1024 * 1024)
                logger.info(f"🧹 手动清理完成: 删除 {cleaned_count} 个过期PDF文件, 释放 {cleaned_size_mb:.2f}MB 空间")
            else:
                logger.info("🧹 手动清理完成: 没有需要清理的文件")
            
        except Exception as e:
            logger.error(f"❌ 手动清理失败: {e}")
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """
        获取缓存统计信息
        
        Returns:
            缓存统计信息字典
        """
        try:
            cache = self.load_cache()
            
            total_files = len(cache)
            total_size = sum(file_info.get("file_size", 0) for file_info in cache.values())
            
            # 按下载时间排序
            sorted_files = []
            for filename, file_info in cache.items():
                try:
                    download_time = datetime.fromisoformat(file_info["download_time"])
                    sorted_files.append({
                        "filename": filename,
                        "download_time": download_time,
                        "file_size": file_info.get("file_size", 0),
                        "url": file_info.get("url", ""),
                        "static_url": f"{self.static_url_base}/{filename}"
                    })
                except:
                    continue
            
            sorted_files.sort(key=lambda x: x["download_time"], reverse=True)
            
            return {
                "cache_file": self.cache_file,
                "download_dir": self.download_dir,
                "static_url_base": self.static_url_base,
                "total_files": total_files,
                "total_size": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "files": sorted_files[:20]  # 只返回最新的20个文件
            }
            
        except Exception as e:
            logger.error(f"❌ 获取缓存统计信息失败: {e}")
            return {
                "error": str(e),
                "cache_file": self.cache_file,
                "download_dir": self.download_dir,
                "static_url_base": self.static_url_base
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取PDF下载统计信息
        
        Returns:
            统计信息字典
        """
        try:
            stats = {
                "download_dir": self.download_dir,
                "static_url_base": self.static_url_base,
                "total_files": 0,
                "total_size_mb": 0.0,
                "file_list": []
            }
            
            if os.path.exists(self.download_dir):
                for filename in os.listdir(self.download_dir):
                    file_path = os.path.join(self.download_dir, filename)
                    if os.path.isfile(file_path) and filename.endswith('.pdf'):
                        file_size = os.path.getsize(file_path)
                        file_mtime = os.path.getmtime(file_path)
                        
                        stats["total_files"] += 1
                        stats["total_size_mb"] += file_size / (1024 * 1024)
                        stats["file_list"].append({
                            "filename": filename,
                            "size_mb": round(file_size / (1024 * 1024), 2),
                            "modified_time": file_mtime,
                            "static_url": f"{self.static_url_base}/{filename}"
                        })
                
                # 按修改时间排序
                stats["file_list"].sort(key=lambda x: x["modified_time"], reverse=True)
                stats["total_size_mb"] = round(stats["total_size_mb"], 2)
            
            return stats
            
        except Exception as e:
            logger.error(f"❌ 获取统计信息失败: {e}")
            return {
                "error": str(e),
                "download_dir": self.download_dir,
                "static_url_base": self.static_url_base
            } 