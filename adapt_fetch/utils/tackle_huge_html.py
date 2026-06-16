import asyncio
import re
import logging

# 强制依赖：readability + html2text
from readability import Document  # type: ignore
import html2text  # type: ignore

logger = logging.getLogger(__name__)


def _mb(n_bytes: int) -> float:
    return n_bytes / (1024 * 1024)


def should_use_readability_for_huge_html(html_text: str, *, threshold_mb: float = 2.0) -> bool:
    try:
        sz = _mb(len(html_text) if isinstance(html_text, (str, bytes)) else 0)
        if sz >= threshold_mb:
            return True
        return False
    except Exception:
        return False


def split_html_safely(html_text: str, *, target_kb: int = 512, overlap_chars: int = 1024):
    """
    按安全边界切分HTML，不限制分块数量
    
    Args:
        html_text: HTML文本
        target_kb: 单块目标大小(KB)
        overlap_chars: 块之间重叠字符数
    
    Returns:
        List[(start, end)]: 分块的起止位置列表
    """
    try:
        if not html_text:
            return []
        target_bytes = max(64 * 1024, int(target_kb * 1024))
        overlap = max(0, overlap_chars)
        n = len(html_text)
        spans = []
        i = 0
        while i < n:  # 🔥 不再限制分块数量
            j = min(n, i + target_bytes)
            # 尝试对齐到下一个安全边界（</p>、</div>、空行）
            m = re.search(r"(</p>|</div>|\n\n)", html_text[j:j+4096])
            if m:
                j = j + m.end()
            spans.append((max(0, i - overlap), min(n, j + overlap)))
            i = j

        # 🔥 修复：不再合并相邻块！overlap 是故意设计的，用于避免边界截断
        # 直接返回所有分块，让每块之间保持轻微重叠
        logger.info(f"🔪 HTML切分完成: {n}字符({n/(1024*1024):.2f}MB) → {len(spans)}块 (target={target_kb}KB, overlap={overlap}字符)")
        return spans
    except Exception as e:
        logger.warning(f"HTML切分异常: {e}")
        return []


# 预编译base64图片正则
_RE_BASE64_IMG = re.compile(r'<img[^>]*src=["\']data:image/[^;]+;base64,[^"\']*["\'][^>]*>', re.IGNORECASE)
_RE_BASE64_URL = re.compile(r'url\(["\']?data:image/[^;]+;base64,[^)]+\)', re.IGNORECASE)
_RE_BASE64_DATA = re.compile(r'data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=]+', re.IGNORECASE)


def remove_base64_images(html: str) -> str:
    """
    移除HTML中的base64图片，避免占用过大空间
    
    使用预编译正则提升性能
    
    Args:
        html: HTML文本
    
    Returns:
        清理后的HTML
    """
    try:
        original_len = len(html)
        
        # 使用预编译正则
        html = _RE_BASE64_IMG.sub('<!-- base64-img-removed -->', html)
        html = _RE_BASE64_URL.sub('url(#base64-removed)', html)
        html = _RE_BASE64_DATA.sub('#base64-removed', html)
        
        # 统计清理效果
        cleaned_len = len(html)
        if cleaned_len < original_len:
            saved_mb = (original_len - cleaned_len) / (1024 * 1024)
            logger.debug(f"🖼️ 清理HTML中base64图片: 节省 {saved_mb:.2f}MB")
        
        return html
    except Exception as e:
        logger.warning(f"base64图片清理失败: {e}")
        return html


def remove_base64_from_markdown(markdown: str) -> str:
    """
    移除Markdown中的base64图片（html2text转换后的格式）
    
    Args:
        markdown: Markdown文本
    
    Returns:
        清理后的Markdown
    """
    try:
        original_len = len(markdown)
        
        # Markdown格式: ![alt](data:image/PNG;base64,...)
        markdown = re.sub(r'!\[([^\]]*)\]\(data:image/[^;]+;base64,[^\)]+\)', 
                         r'![图片已移除]', markdown, flags=re.IGNORECASE)
        
        # 纯链接格式: [text](data:image/...)
        markdown = re.sub(r'\[[^\]]*\]\(data:image/[^;]+;base64,[^\)]+\)', 
                         '[base64-link-removed]', markdown, flags=re.IGNORECASE)
        
        # 裸露的 data:image 链接
        markdown = re.sub(r'data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=]+', 
                         '#base64-removed', markdown, flags=re.IGNORECASE)
        
        cleaned_len = len(markdown)
        if cleaned_len < original_len:
            saved_kb = (original_len - cleaned_len) / 1024
            logger.debug(f"🖼️ 清理Markdown中base64: 节省 {saved_kb:.1f}KB")
        
        return markdown
    except Exception as e:
        logger.warning(f"Markdown base64清理失败: {e}")
        return markdown


# 预编译正则表达式，避免每次调用时重新编译
# 使用非回溯模式：[^<]*(?:<(?!/tag)[^<]*)* 避免灾难性回溯
_RE_SCRIPT = re.compile(r'<script[^>]*>(?:[^<]|<(?!/script>))*</script>', re.IGNORECASE)
_RE_STYLE = re.compile(r'<style[^>]*>(?:[^<]|<(?!/style>))*</style>', re.IGNORECASE)
_RE_NOSCRIPT = re.compile(r'<noscript[^>]*>(?:[^<]|<(?!/noscript>))*</noscript>', re.IGNORECASE)
# 移除没有闭合标签的 script/style（自闭合或残缺）
_RE_SCRIPT_OPEN = re.compile(r'<script[^>]*/>', re.IGNORECASE)
_RE_STYLE_OPEN = re.compile(r'<style[^>]*/>', re.IGNORECASE)


def remove_script_style_tags(html: str) -> str:
    """
    强制移除所有 <script> 和 <style> 标签及其内容
    
    使用预编译正则和非回溯模式，避免灾难性回溯
    
    Args:
        html: HTML文本
    
    Returns:
        清理后的HTML
    """
    try:
        original_len = len(html)
        
        # 使用预编译正则，性能更好
        html = _RE_SCRIPT.sub('', html)
        html = _RE_STYLE.sub('', html)
        html = _RE_NOSCRIPT.sub('', html)
        
        # 清理自闭合的 script/style 标签
        html = _RE_SCRIPT_OPEN.sub('', html)
        html = _RE_STYLE_OPEN.sub('', html)
        
        cleaned_len = len(html)
        if cleaned_len < original_len:
            saved_kb = (original_len - cleaned_len) / 1024
            logger.debug(f"🧹 清理script/style标签: 节省 {saved_kb:.1f}KB")
        
        return html
    except Exception as e:
        logger.warning(f"script/style标签清理失败: {e}")
        return html


async def _readability_to_markdown_pipeline(html: str, to_markdown: bool = True) -> str:
    """
    精简管道：Readability → html2text → Markdown
    
    注意：MapReduce 调用前已执行预清理（移除 script/style/base64），这里不再重复过滤
    
    Args:
        html: 已预清理的HTML文本块（MapReduce已去除script/style/base64）
        to_markdown: 是否转换为Markdown（False则返回cleaned_html）
    
    Returns:
        清理后的markdown或html
    """
    def _work():
        # 步骤1: Readability 提取主要内容
        doc = Document(html)
        cleaned_html = doc.summary()
        
        # 步骤2: 转换为 Markdown
        if to_markdown:
            conv = html2text.HTML2Text()
            conv.ignore_links = False
            conv.ignore_images = False
            conv.ignore_emphasis = False
            conv.body_width = 0
            conv.unicode_snob = True
            conv.skip_internal_links = False
            markdown = conv.handle(cleaned_html)
            return markdown
        else:
            return cleaned_html
    
    return await asyncio.to_thread(_work)


async def clean_with_readability_single(html_content: str, to_markdown: bool = True) -> dict:
    """
    单体 Readability 清理（统一接口，供 EasyGet 和 Playwright 共用）
    
    Args:
        html_content: HTML内容
        to_markdown: 是否转换为Markdown
    
    Returns:
        清理结果字典
    """
    import time
    clean_start = time.time()
    
    try:
        # 单体清理：先做预清理，再调用管道
        html_cleaned = remove_base64_images(html_content)
        html_cleaned = remove_script_style_tags(html_cleaned)
        
        # 调用管道函数处理已清理的HTML
        result_content = await _readability_to_markdown_pipeline(html_cleaned, to_markdown=to_markdown)
        clean_time = time.time() - clean_start
        
        # 提取纯文本长度
        text_only = re.sub(r"<[^>]+>", " ", result_content)
        text_only = re.sub(r"\s+", " ", text_only).strip()
        
        reduction_pct = max(0, round((len(html_content) - len(result_content)) / max(1, len(html_content)) * 100, 2))
        
        return {
            'success': True,
            'markdown': result_content if to_markdown else '',
            'cleaned_html': result_content if not to_markdown else '',
            'text_length': len(text_only),
            'clean_time': clean_time,
            'original_size': len(html_content),
            'cleaned_size': len(result_content),
            'reduction_percentage': reduction_pct,
            'cleaner': 'readability-single'
        }
    except Exception as e:
        clean_time = time.time() - clean_start
        logger.error(f"❌ Readability单体清理失败: {e}")
        return {
            'success': False,
            'error': str(e),
            'clean_time': clean_time,
            'cleaner': 'readability-single'
        }


async def map_reduce_readability(html_text: str, *,
                                 concurrency: int = 4,
                                 target_kb: int = 512,
                                 overlap_chars: int = 1024,
                                 to_markdown: bool = True) -> dict:
    """
    MapReduce 方式并行清理超大 HTML
    
    Args:
        html_text: HTML文本
        concurrency: 并发度，-1表示无限制（所有块一次性并发）
        target_kb: 单块目标大小(KB)
        overlap_chars: 块之间重叠字符数
        to_markdown: 是否转换为Markdown
    
    Returns:
        清理结果字典
    """
    if not html_text:
        return {"success": False, "error": "empty_html"}

    # 🔍 在分块前先进行一次轻量预清理，避免大段<script>/<style>被拆成半截
    html_for_split = html_text
    try:
        pre_cleaned = remove_script_style_tags(remove_base64_images(html_text))
        if pre_cleaned:
            if len(pre_cleaned) != len(html_text):
                logger.debug(
                    f"🧼 MapReduce预清理: {len(html_text)}→{len(pre_cleaned)} 字符 (移除script/style/base64)"
                )
            html_for_split = pre_cleaned
    except Exception as e:
        logger.warning(f"MapReduce预清理阶段异常: {e}")

    spans = split_html_safely(html_for_split, target_kb=target_kb, overlap_chars=overlap_chars)
    if not spans:
        spans = [(0, len(html_for_split))]
    
    logger.info(f"🧩 MapReduce分块: {len(spans)}块, 并发度: {'无限制' if concurrency == -1 else concurrency}")

    # 🔥 完整管道：每个块独立完成 HTML→Readability→html2text→Markdown
    if concurrency == -1:
        # 无限并发：所有块一次性并发
        async def run_pipeline_unlimited(idx, s, e):
            result = await _readability_to_markdown_pipeline(html_for_split[s:e], to_markdown=to_markdown)
            return idx, result
        tasks = [run_pipeline_unlimited(i, s, e) for i, (s, e) in enumerate(spans)]
    else:
        # 有限并发：使用信号量控制
        sem = asyncio.Semaphore(max(1, concurrency))
        async def run_pipeline_limited(idx, s, e):
            async with sem:
                result = await _readability_to_markdown_pipeline(html_for_split[s:e], to_markdown=to_markdown)
                return idx, result
        tasks = [run_pipeline_limited(i, s, e) for i, (s, e) in enumerate(spans)]
    
    # 并发执行所有管道
    parts = await asyncio.gather(*tasks, return_exceptions=True)

    # Reduce阶段：排序并拼接结果
    ordered = []
    for p in parts:
        if isinstance(p, tuple):
            ordered.append(p)
    ordered.sort(key=lambda x: x[0])
    
    # 拼接所有块
    final_content = "\n\n".join([p[1] for p in ordered if isinstance(p[1], str) and p[1].strip()])
    
    logger.info(f"✅ MapReduce完成: {len(ordered)}块已拼接, 最终长度{len(final_content)/(1024*1024):.2f}MB")

    # 提取纯文本统计
    text_only = re.sub(r"<[^>]+>", " ", final_content)
    text_only = re.sub(r"\s+", " ", text_only).strip()

    return {
        "success": True,
        "slices": len(spans),
        "cleaned_html": final_content if not to_markdown else "",
        "markdown": final_content if to_markdown else "",
        "text_length": len(text_only)
    }


