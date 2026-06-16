# https://r.jina.ai/https://www.kaggle.com/code/aisuko/zero-shot-image-classification-with-clip

import asyncio
import time
import re
import json
from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
import logging
import aiohttp
import os

# 双跳本地代理：711 HK 轮换（proxy/double_hop_proxy.py 监听 22002）
DEFAULT_PROXY = "http://127.0.0.1:22002"
os.environ['http_proxy'] = os.environ.get('http_proxy', DEFAULT_PROXY)
os.environ['https_proxy'] = os.environ.get('https_proxy', DEFAULT_PROXY)

# 统一的代理池（取消手动切换，按轮询使用配置中的代理）
logger_proxy = logging.getLogger("proxy_pool")
PROXY_SWITCH_ENABLED = False

class JinaProxyPool:
    def __init__(self):
        # 从环境变量或默认值获取代理地址
        proxy_url = os.environ.get('http_proxy') or os.environ.get('https_proxy') or DEFAULT_PROXY
        self._proxies: List[str] = [proxy_url]
        logger_proxy.info(f"Jina代理池加载完成，使用代理: {proxy_url}")
        self._idx = 0
        self._lock = asyncio.Lock()

    async def get_next(self) -> str:
        async with self._lock:
            proxy = self._proxies[self._idx % len(self._proxies)]
            self._idx += 1
            return proxy

    def get_all(self) -> List[str]:
        return list(self._proxies)

jina_proxy_pool = JinaProxyPool()

logger = logging.getLogger(__name__)

# 创建路由器，使用标签但不使用前缀，保持原始路径
router = APIRouter(tags=["Jina"])

# 并发控制配置（全局限制）
# 所有请求共享同一个并发限制，避免资源耗尽
# 建议值：50-100（根据服务器性能和代理数量调整）
MAX_CONCURRENT_REQUESTS = int(os.getenv("JINA_MAX_CONCURRENT", "50"))

# 全局共享的 Semaphore（所有请求共享同一个并发限制）
_global_semaphore: Optional[asyncio.Semaphore] = None

def get_global_semaphore() -> asyncio.Semaphore:
    """获取全局共享的 Semaphore（延迟初始化）"""
    global _global_semaphore
    if _global_semaphore is None:
        _global_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return _global_semaphore

class CrawlRequest(BaseModel):
    urls: List[str] = Field(..., description="要爬取的URL列表")
    timeout: Optional[int] = Field(30, description="单个URL超时时间(秒)")
    no_cache: Optional[bool] = Field(False, description="是否跳过Jina缓存，强制重新读取页面")
    ignore_imgs: Optional[bool] = Field(False, description="是否过滤图片链接")
    ignore_links: Optional[bool] = Field(False, description="是否过滤普通超链接")

class CrawlResponse(BaseModel):
    code: int
    msg: str
    data: Optional[Dict[str, Any]] = None

def extract_title_from_markdown(text: str) -> str:
    """从Markdown内容中提取标题"""
    if not text:
        return ""
    
    # 按行分割
    lines = text.split('\n')
    
    # 查找标题行
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # 匹配 "Title: " 开头的行
        if line.startswith('Title:'):
            title = line[6:].strip()  # 去掉 "Title: " 前缀
            return title
            
        # 匹配 "===============" 分隔符前的行
        if line == '===============':
            # 查找分隔符前的非空行
            for i in range(len(lines) - 1, -1, -1):
                prev_line = lines[i].strip()
                if prev_line and prev_line != '===============':
                    return prev_line
                    
        # 匹配一级标题 (# 开头)
        if line.startswith('# '):
            return line[2:].strip()
            
        # 匹配二级标题 (## 开头)
        if line.startswith('## '):
            return line[3:].strip()
    
    # 如果没有找到明确的标题，返回第一行非空内容
    for line in lines:
        line = line.strip()
        if line and not line.startswith('URL Source:') and not line.startswith('Markdown Content:'):
            return line
            
    return ""

async def crawl_single_url(url: str, timeout: int = 30, no_cache: bool = False, ignore_imgs: bool = False, ignore_links: bool = False, proxy_url: Optional[str] = None) -> Dict[str, Any]:
    """爬取单个URL"""
    start_time = time.time()
    try:
        # 设置超时
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        
        # 设置请求头（最小化配置，避免代理服务器拦截）
        headers = {}
        
        # 如果需要跳过缓存，添加 x-no-cache 头部
        if no_cache:
            headers['X-No-Cache'] = 'true'
        
        # 构建Jina代理URL
        jina_url = f"https://r.jina.ai/{url}"
        
        # 配置SSL上下文（允许通过代理进行SSL连接）
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        # 创建TCPConnector，配置SSL和代理设置
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(jina_url, headers=headers, timeout=timeout_obj, proxy=proxy_url) as response:
                # 获取响应信息
                status_code = response.status
                final_url = str(response.url)
                content_type = response.headers.get('content-type', '')
                
                # 读取响应内容
                text = await response.text()
                
                # 过滤图片链接
                if ignore_imgs:
                    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
                # 过滤普通超链接（不包括图片）
                if ignore_links:
                    text = re.sub(r'(?<!\!)\[.*?\]\(.*?\)', '', text)
                
                # 提取标题
                title = extract_title_from_markdown(text)
                
                execution_time = time.time() - start_time
                
                return {
                    "url": url,
                    "final_url": final_url,
                    "title": title,
                    "text": text,
                    "text_length": len(text),
                    "status_code": status_code,
                    "execution_time": round(execution_time, 2),
                    "encoding": response.get_encoding(),
                    "redirects": len(response.history),
                    "content_type": content_type,
                    "no_cache": no_cache,
                    "ignore_imgs": ignore_imgs,
                    "ignore_links": ignore_links,
                    "success": True
                }
                
    except Exception as e:
        execution_time = time.time() - start_time
        return {
            "url": url,
            "error": str(e),
            "execution_time": round(execution_time, 2),
            "timeout_used": timeout,
            "no_cache": no_cache,
            "ignore_imgs": ignore_imgs,
            "ignore_links": ignore_links,
            "success": False
        }

async def crawl_stream_generator(request: CrawlRequest):
    """SSE流式响应生成器 - 全局并发控制，自动轮换代理"""
    urls = request.urls
    timeout = request.timeout or 30
    no_cache = request.no_cache or False
    ignore_imgs = request.ignore_imgs or False
    ignore_links = request.ignore_links or False
    
    if not urls:
        yield f"data: {json.dumps({'type': 'error', 'code': 1, 'msg': 'URL列表不能为空'}, ensure_ascii=False)}\n\n"
        return

    # 发送开始消息
    yield f"data: {json.dumps({'type': 'start', 'total': len(urls)}, ensure_ascii=False)}\n\n"
    
    # 心跳包任务
    async def heartbeat_task():
        while True:
            await asyncio.sleep(3)
            yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
    
    # 爬取任务 - 全局并发控制（所有请求共享同一个并发限制）
    async def crawl_task():
        results = []
        failed = []
        
        # 使用全局共享的 Semaphore（所有请求共享同一个并发限制）
        semaphore = get_global_semaphore()
        
        async def crawl_with_semaphore(url: str) -> Dict[str, Any]:
            """带全局并发限制的爬取函数"""
            async with semaphore:  # 获取全局信号量
                proxy_url = await jina_proxy_pool.get_next()
                return await crawl_single_url(url, timeout, no_cache, ignore_imgs, ignore_links, proxy_url=proxy_url)
        
        # 创建所有任务（通过全局Semaphore自动控制并发）
        tasks = [crawl_with_semaphore(url) for url in urls]
        
        # 并发执行所有任务（全局Semaphore控制所有请求的总并发数量）
        all_results = await asyncio.gather(*tasks)
        
        for result in all_results:
            if result["success"]:
                results.append(result)
            else:
                failed.append(result)
        
        # 发送最终完成消息，包含所有数据
        final_result = {
            "code": 0,
            "msg": f"爬取完成，成功{len(results)}，失败{len(failed)}，共{len(urls)}。",
            "data": {
                "results": results,
                "failed": failed,
                "total": len(urls),
                "success": len(results),
                "failed_count": len(failed)
            }
        }
        yield f"data: {json.dumps({'type': 'complete', 'data': final_result}, ensure_ascii=False)}\n\n"
    
    # 创建心跳包和爬取任务
    heartbeat_gen = heartbeat_task()
    crawl_gen = crawl_task()
    
    # 竞争性地处理两个任务
    heartbeat_task_obj = asyncio.create_task(heartbeat_gen.__anext__())
    crawl_task_obj = asyncio.create_task(crawl_gen.__anext__())
    
    crawl_finished = False
    
    while not crawl_finished:
        done, pending = await asyncio.wait(
            [heartbeat_task_obj, crawl_task_obj],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        for task in done:
            try:
                result = task.result()
                yield result
                
                # 检查是否是完成消息
                if 'complete' in result:
                    crawl_finished = True
                    break
                
                # 重新启动相应的任务
                if task == heartbeat_task_obj:
                    heartbeat_task_obj = asyncio.create_task(heartbeat_gen.__anext__())
                elif task == crawl_task_obj:
                    crawl_task_obj = asyncio.create_task(crawl_gen.__anext__())
                    
            except StopAsyncIteration:
                # 任务完成
                if task == crawl_task_obj:
                    crawl_finished = True
                    break
    
    # 取消剩余任务并等待完成（避免 "Task was destroyed but it is pending" 警告）
    tasks_to_cancel = list(pending)
    if not heartbeat_task_obj.done():
        tasks_to_cancel.append(heartbeat_task_obj)
    if not crawl_task_obj.done():
        tasks_to_cancel.append(crawl_task_obj)
    
    for task in tasks_to_cancel:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration, Exception):
            # 🔥 捕获所有异常：CancelledError、StopAsyncIteration、以及其他可能的异常
            pass

@router.post("/jina_crawl")
async def crawl_urls(request: CrawlRequest = Body(...)):
    """
    爬取URL列表（流式响应）
    """
    try:
        return StreamingResponse(
            crawl_stream_generator(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    except Exception as e:
        logger.error(f"爬取请求失败: {e}")
        return JSONResponse(
            status_code=500,
            content=CrawlResponse(code=1, msg=f"爬取失败: {str(e)}").dict()
        ) 