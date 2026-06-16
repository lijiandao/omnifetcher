"""
并发爬取策略模块

本模块包含不同的并发爬取策略函数，用于协调 EasyGet、Playwright 和 Jina 等爬虫的并发执行。
每个策略函数接收 crawler 实例作为参数，以访问必要的属性和方法。
"""

import asyncio
import time
import logging
from typing import Dict, Any, Optional

from adapt_fetch.playwright_service.playwright_router_helper import (
    CrawlRequest,
    cleanup_orphan_about_blank_pages,
    extract_text_from_html,
)

logger = logging.getLogger(__name__)


async def crawl_with_concurrent_strategy(
    crawler,
    url: str,
    config: CrawlRequest,
    detection_result: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """并发策略：同时尝试EasyGet、Jina和Playwright - 优雅取消版本
    
    Args:
        crawler: PlaywrightCrawler 实例
        url: 要爬取的 URL
        config: 爬取配置
        detection_result: 智能检测结果（可选）
    
    Returns:
        爬取结果字典
    """
    start_time = time.time()
    
    logger.info(f"🔄 并发策略启动: {url}")
    if detection_result:
        logger.info(f"📊 智能检测信息: {detection_result.get('recommended_mode', 'unknown')} - {detection_result.get('reason', 'no reason')}")
    
    # 第一步：约定线程安全的共享标志
    # 说明：该事件用于"HTTP类竞速成功信号"（EasyGet 或 Jina 任一成功即置位）
    easyget_success_flag = asyncio.Event()
    # 只有当 HTTP 类结果真正通过健康检测时才会写入，用于防止"误置位 flag"导致误取消 Playwright
    verified_http_success = {'result': None, 'source': None}
    playwright_page_ref = {'page': None}
    page_close_lock = asyncio.Lock()  # 确保page.close()只调用一次
    related_tasks = []  # 存储所有相关任务，用于统一取消
    page_close_event_triggered = {'flag': False}  # 页面关闭事件标志
    
    async def _close_page_gracefully(max_wait: float = 3.0, poll_interval: float = 0.2):
        """第三步：优雅关闭页面，避免竞态条件
        1. 当 EasyGet 已成功而 Playwright 页面尚未创建时，
           先轮询等待一小段时间（默认 ≤3s）直到页面对象就绪。
        2. 拿到页面后执行 stopLoading -> page.close() 的完整关闭流程。
        3. 即使等待超时仍未拿到页面，也会直接返回，防止死锁。
        """
        logger.info("🛑 执行优雅关闭页面…")
        
        async with page_close_lock:  # 确保只关闭一次
            page = playwright_page_ref.get('page')
            waited = 0.0
            # 如果此时页面尚未创建，短暂轮询等待
            while page is None and waited < max_wait:
                await asyncio.sleep(poll_interval)
                waited += poll_interval
                page = playwright_page_ref.get('page')

            if not page:
                logger.debug("[GracefulClose] 等待页面对象超时，未获取到 page，跳过关闭")
                return

            if page.is_closed():
                logger.debug("[GracefulClose] 页面已关闭，无需处理")
                return

            try:
                logger.info("🛑 执行优雅关闭页面…")
                # 先阻止网络流水线，减少 Edge 线程残留
                try:
                    client = await crawler._safe_new_cdp_session(page)
                    if client:
                        await client.send("Page.stopLoading")
                        await client.detach()
                    else:
                        logger.debug("_close_page_gracefully: 无法创建 CDP 会话，可能页面已关闭")
                except Exception:
                    pass  # 忽略 CDP 错误

                # 取消 still running 相关任务，避免悬挂
                for task in related_tasks:
                    if not task.done():
                        task.cancel()

                await page.close()
                logger.info("✅ 页面已优雅关闭")
            except Exception as e:
                logger.debug(f"优雅关闭页面时出错: {e}")
    
    async def easyget_task():
        """EasyGet任务 - 发出成功信号并触发优雅关闭"""
        try:
            logger.debug("🚀 启动EasyGet爬虫...")
            
            # 从配置获取 EasyGet 超时时间，默认5秒
            easyget_timeout = getattr(config, 'easyget_timeout', 5)
            max_redirects = int(config.max_redirects) if config.max_redirects is not None else 10
            
            logger.info(f"⏰ EasyGet并发模式超时: {easyget_timeout}秒")
            
            result = await crawler.easy_crawler.crawl_single_url(
                url=url,
                timeout=easyget_timeout,
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
            
            success = result.get('success', False)
            if not success:
                error_msg = result.get('error', '失败，未提供具体错误信息')
                logger.info(f"⚠️ EasyGet失败: {error_msg}，等待Playwright结果")
                # 🔥 返回包含错误信息的字典，而不是 None，便于后续提取错误详情
                return {
                    'success': False,
                    'url': url,
                    'actual_crawler': 'easyget',
                    'easyget_error': error_msg  # 🔥 直接放在 easyget_error
                }

            # --- PDF 检测：如果 EasyGet 已经成功处理了 PDF，直接返回 ---
            if result.get('is_pdf_page') and result.get('markdown'):
                logger.info("📄 EasyGet成功处理PDF，准备进行健康检测")
                # 直接返回 PDF 结果，添加执行信息
                result.update({
                    'execution_time': time.time() - start_time,
                    'actual_crawler': 'easyget_pdf',
                    'mode': 'concurrent→easyget_pdf',
                    'smart_detection': detection_result,
                    'easyget_timeout_used': easyget_timeout
                })
                # 健康检测：通过才宣告并发成功（避免"先成功后失败"打断 Playwright）
                try:
                    status_code = result.get('status_code')
                    min_text = getattr(config, 'text_limit', 100)
                    result = crawler._apply_health_detection_to_result(result, status_code, min_text)
                except Exception as _e:
                    logger.debug(f"并发策略 EasyGet(PDF) 健康检测异常: {_e}")
                if result.get('success'):
                    logger.info("✅ EasyGet(PDF) 通过健康检测，发出成功信号")
                    verified_http_success['result'] = result
                    verified_http_success['source'] = 'easyget_pdf'
                    easyget_success_flag.set()
                    await asyncio.sleep(0)
                    asyncio.create_task(_close_page_gracefully())
                else:
                    logger.info(f"⚠️ EasyGet(PDF) 健康检测未通过，继续等待Playwright (success={result.get('success')}, text_length={result.get('text_length')})")
                return result
            
            # --- PDF 检测：若响应为 PDF 但未处理，则交由 Playwright 处理 ---
            content_type_header = result.get('content_type', '').lower()
            final_url_lower = result.get('final_url', url).lower()
            if 'application/pdf' in content_type_header or final_url_lower.endswith('.pdf'):
                logger.info("📄 EasyGet检测到PDF响应但未处理，放弃EasyGet结果，等待Playwright继续处理")
                return None
            
            # 检查内容质量
            is_garbled = result.get('is_garbled', False)
            is_binary = result.get('is_binary', False)
            magic_type = result.get('magic_type', '')
            
            # 如果内容是乱码或二进制，且不是图片类型，则认为EasyGet失败
            if (is_garbled or is_binary) and 'image' not in magic_type.lower():
                logger.info(f"⚠️ EasyGet检测到乱码或二进制内容 (is_garbled={is_garbled}, is_binary={is_binary}, magic_type={magic_type})，等待Playwright结果")
                return None
            
            # 直接认为 EasyGet 已返回有效内容，发出成功信号
            logger.info("✅ EasyGet抓取到内容，准备进行健康检测（通过才宣告成功）")
            
            # 🔥 关键优化：检查EasyGet是否已经清理过HTML
            html_already_cleaned = result.get('html_cleaned', False)
            markdown_content = result.get('markdown', '')
            html_content = result.get('html', '')
            
            # 构建基础结果字典
            result_dict = {
                'url': url,
                'final_url': result.get('final_url', url),
                'success': True,
                'execution_time': time.time() - start_time,
                'actual_crawler': 'easyget',
                'mode': 'concurrent→easyget',
                'smart_detection': detection_result,
                'easyget_timeout_used': easyget_timeout
            }
            
            # 添加title字段（如果提取成功）
            if config.extract_title and result.get('title'):
                result_dict['title'] = result['title']
            
            # 根据是否已清理和用户配置决定返回内容
            if html_already_cleaned and markdown_content:
                # EasyGet已经清理过，直接使用，避免重复清理
                logger.info(f"🎯 EasyGet已完成HTML清理，直接使用markdown ({len(markdown_content)} 字符)")
                result_dict['markdown'] = markdown_content
                result_dict['text_length'] = len(markdown_content.strip())
                # 健康检测
                try:
                    status_code = result.get('status_code')
                    min_text = getattr(config, 'text_limit', 100)
                    logger.debug(f"🔍 EasyGet健康检测前: success={result_dict.get('success')}, text_length={result_dict.get('text_length')}, min_text={min_text}")
                    result_dict = crawler._apply_health_detection_to_result(result_dict, status_code, min_text)
                    logger.debug(f"🔍 EasyGet健康检测后: success={result_dict.get('success')}")
                except Exception as _e:
                    logger.error(f"❌ 并发策略 EasyGet(已清理) 健康检测异常: {_e}")
                    result_dict['success'] = False
                # 只有健康检测通过，才发出"并发成功"信号并关闭页面
                if result_dict.get('success'):
                    logger.info(f"✅ EasyGet(清理后)通过健康检测，发出成功信号 (text_length={result_dict.get('text_length')}, status_code={result.get('status_code')})")
                    verified_http_success['result'] = result_dict
                    verified_http_success['source'] = 'easyget_cleaned'
                    easyget_success_flag.set()
                    await asyncio.sleep(0)
                    asyncio.create_task(_close_page_gracefully())
                else:
                    logger.info(f"⚠️ EasyGet(清理后)健康检测未通过，继续等待Playwright (success={result_dict.get('success')}, text_length={result_dict.get('text_length')}, detection={result_dict.get('detection')})")
                return result_dict
            
            else:
                # 不需要清理或没有内容，直接返回html
                logger.info("📄 返回原始HTML（未启用清理或无内容）")
                result_dict['html'] = html_content
                result_dict['text_length'] = len(html_content.strip()) if html_content else 0
                # 健康检测
                try:
                    status_code = result.get('status_code')
                    min_text = getattr(config, 'text_limit', 100)
                    result_dict = crawler._apply_health_detection_to_result(result_dict, status_code, min_text)
                except Exception as _e:
                    logger.debug(f"并发策略 EasyGet(原始HTML) 健康检测异常: {_e}")
                if result_dict.get('success'):
                    logger.info(f"✅ EasyGet(原始HTML)通过健康检测，发出成功信号 (text_length={result_dict.get('text_length')}, status_code={result.get('status_code')})")
                    verified_http_success['result'] = result_dict
                    verified_http_success['source'] = 'easyget_raw_html'
                    easyget_success_flag.set()
                    await asyncio.sleep(0)
                    asyncio.create_task(_close_page_gracefully())
                else:
                    logger.info(f"⚠️ EasyGet(原始HTML)健康检测未通过，继续等待Playwright (success={result_dict.get('success')}, text_length={result_dict.get('text_length')}, detection={result_dict.get('detection')})")
                return result_dict
                
        except asyncio.CancelledError:
            logger.info("🛑 EasyGet任务已被取消（Playwright已成功）")
            raise
        except Exception as e:
            logger.warning(f"EasyGet任务异常: {e}")
            return None
    
    async def jina_task():
        """Jina 爬虫任务 - 通过 r.jina.ai 读取 Markdown 文本，作为HTTP类竞速的一员"""
        try:
            from adapt_fetch.jina.jina_router import crawl_single_url as jina_crawl_single_url, jina_proxy_pool
            
            logger.debug("🚀 启动Jina爬虫...")
            # 复用 easyget_timeout 作为 Jina 侧超时时间（秒）
            jina_timeout = getattr(config, 'easyget_timeout', 5)
            # 从代理池获取代理
            proxy_url = await jina_proxy_pool.get_next()
            res = await jina_crawl_single_url(
                url=url,
                timeout=jina_timeout,
                no_cache=False,
                ignore_imgs=False,
                ignore_links=False,
                proxy_url=proxy_url
            )
            if not isinstance(res, dict):
                logger.info("⚠️ Jina返回异常类型，等待其他结果")
                return None
            if not res.get('success'):
                err = res.get('error', '失败，未提供具体错误信息')
                logger.info(f"⚠️ Jina失败: {err}，等待其他结果")
                return {
                    'success': False,
                    'url': url,
                    'actual_crawler': 'jina',
                    'jina_error': err
                }
            # 成功：先健康检测，通过后才发出HTTP类成功信号（避免"先成功后失败"）
            logger.info("✅ Jina抓取到Markdown内容，准备进行健康检测（通过才宣告成功）")
            markdown_text = res.get('text', '') or ''
            title = res.get('title')
            final_url = res.get('final_url', url)
            status_code = res.get('status_code')
            result_dict = {
                'url': url,
                'final_url': final_url,
                'success': True,
                'execution_time': time.time() - start_time,
                'actual_crawler': 'jina',
                'mode': 'concurrent→jina',
                'smart_detection': detection_result,
                'markdown': markdown_text,
                'text_length': len(markdown_text.strip()),
                'status_code': status_code
            }
            if config.extract_title and title:
                result_dict['title'] = title
            # 健康检测（将 Jina 视作 HTTP 类）
            try:
                min_text = getattr(config, 'text_limit', 100)
                result_dict = crawler._apply_health_detection_to_result(result_dict, status_code, min_text)
            except Exception as _e:
                logger.debug(f"Jina 健康检测异常: {_e}")
            if result_dict.get('success'):
                logger.info(f"✅ Jina通过健康检测，发出成功信号 (text_length={result_dict.get('text_length')}, status_code={status_code})")
                verified_http_success['result'] = result_dict
                verified_http_success['source'] = 'jina'
                easyget_success_flag.set()
                await asyncio.sleep(0)
                asyncio.create_task(_close_page_gracefully())
            else:
                logger.info(f"⚠️ Jina健康检测未通过，继续等待Playwright (success={result_dict.get('success')}, text_length={result_dict.get('text_length')}, detection={result_dict.get('detection')})")
            return result_dict
        except asyncio.CancelledError:
            logger.info("🛑 Jina任务已被取消（其他路径已成功）")
            raise
        except Exception as e:
            logger.warning(f"Jina任务异常: {e}")
            return None
    
    async def playwright_task():
        """Playwright任务 - 全程监听EasyGet信号并优雅收尾"""
        page = None
        task_id = f"playwright_{url}_{int(time.time() * 1000)}"
        logger.info(f"🔍 Playwright任务启动: task_id={task_id}")
        
        async def check_cancellation():
            """检查是否应该取消当前操作"""
            if easyget_success_flag.is_set() and verified_http_success.get('result') is not None:
                src = verified_http_success.get('source') or 'http'
                logger.info(f"🛑 检测到HTTP成功信号({src})，Playwright主动退出")
                raise asyncio.CancelledError("HTTP已成功")
        
        try:
            logger.info("🎭 启动Playwright爬虫...")
            
            # 在每个主要操作前检查取消状态
            await check_cancellation()
            
            # 初始化Playwright
            await crawler.initialize(
                use_edge_user_data=config.use_edge_user_data,
                enable_javascript=config.enable_javascript,
                user_agent=config.user_agent,
                fast_mode=False
            )
            
            await check_cancellation()
            
            if not crawler.context:
                logger.warning("⚠️ 浏览器上下文为空，重新初始化...")
                await crawler.initialize(
                    use_edge_user_data=config.use_edge_user_data,
                    enable_javascript=config.enable_javascript,
                    user_agent=config.user_agent,
                    fast_mode=False
                )
                if not crawler.context:
                    raise Exception("浏览器上下文重新初始化失败")
            
            await check_cancellation()
            
            # 创建页面前检查 context 状态
            try:
                if not crawler.context:
                    logger.error(f"❌ Context 为 None，无法创建页面！task_id={task_id}")
                    raise Exception("Browser context is None")
                
                # 检查 context 是否已关闭
                try:
                    crawler.context.pages
                except Exception as check_err:
                    logger.error(f"❌ 检查Context状态失败: {check_err}, task_id={task_id}")
                    raise Exception(f"Browser context check failed: {check_err}")
                
                # 检查清理状态
                if crawler._is_closing:
                    logger.warning(f"⚠️ 检测到清理状态，跳过页面创建！task_id={task_id}")
                    return None
                
            except Exception as pre_check_err:
                logger.error(f"❌ 页面创建前检查失败: {pre_check_err}, task_id={task_id}")
                raise
            
            # 创建页面
            try:
                page = await crawler.context.new_page()
                playwright_page_ref['page'] = page
            except Exception as page_create_err:
                error_msg = str(page_create_err)
                logger.error(f"❌ 页面创建失败: {error_msg}, task_id={task_id}")
                raise
            
            # 第二步：绑定页面关闭回调，转化异常为普通返回值
            async def on_page_close():
                page_close_event_triggered['flag'] = True
                logger.warning(f"⚠️ 检测到页面关闭事件！task_id={task_id}")
            
            page.once('close', lambda: asyncio.create_task(on_page_close()))
            
            # 注册超时管理
            crawler.timeout_manager.register_task(task_id, page, config.timeout)
            
            await check_cancellation()
            
            # 设置资源阻塞
            if crawler.resource_blocking_enabled:
                await page.route("**/*", crawler._handle_resource_request)
            
            await check_cancellation()
            
            # 设置额外请求头
            if config.extra_headers:
                await page.set_extra_http_headers(config.extra_headers)
            
            await check_cancellation()
            
            # 导航到页面 - 设置timeout >= EasyGet超时避免早关页
            # 确保 Playwright 的 goto 超时 >= EasyGet 超时，避免页面过早关闭
            easyget_timeout_ms = getattr(config, 'easyget_timeout', 5) * 1000
            goto_timeout = max(easyget_timeout_ms, config.timeout)  # 至少等于 EasyGet 超时
            
            # --- 并发监听EasyGet成功信号，随时中断goto ---
            success_wait_task = asyncio.create_task(easyget_success_flag.wait())
            wait_state = config.wait_for_load_state or "commit"
            goto_task = asyncio.create_task(
                page.goto(
                    crawler._normalize_entry_url(url),
                    wait_until=wait_state,
                    timeout=goto_timeout
                )
            )
            logger.info(f"🔄 等待goto完成 (wait_until={wait_state}): {url}")
            done, pending = await asyncio.wait(
                {goto_task, success_wait_task},
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # 如果 HTTP 类成功信号率先到达，立即取消 goto 并退出
            if success_wait_task in done and easyget_success_flag.is_set() and verified_http_success.get('result') is not None:
                logger.info(f"🛑 检测到HTTP成功信号，取消Playwright goto 并退出 (wait_until={wait_state}): {url}")
                if not goto_task.done():
                    goto_task.cancel()
                    try:
                        await goto_task
                    except (asyncio.CancelledError, Exception):
                        pass
                raise asyncio.CancelledError("HTTP已成功，取消Playwright导航")

            # 只有当 goto_task 实际完成时，才宣告 goto 完成
            if goto_task in done:
                logger.info(f"✅ goto已经完成 (wait_until={wait_state}): {url}")
                try:
                    if goto_task.done() and not goto_task.cancelled():
                        exc = goto_task.exception()
                        if exc:
                            logger.warning(f"⚠️ [GOTO-EXCEPTION] goto_task有异常: {type(exc).__name__}: {str(exc)[:200]}")
                except Exception as check_err:
                    logger.debug(f"检查goto_task异常时出错: {check_err}")
            else:
                logger.warning(f"⚠️ goto 等待提前返回，但未命中成功信号；继续等待 goto: {url}")
            
            # 此时goto已完成（success_wait_task 可能仍在等待），获取响应
            try:
                response = await goto_task
            except asyncio.CancelledError as ce:
                logger.error(f"❌ [AWAIT-GOTO-CANCELLED] goto_task被取消!")
                if not easyget_success_flag.is_set():
                    logger.error("❌ [BUG-CONFIRMED] Playwright被取消但HTTP未成功！这是一个严重的并发bug")
                    return {
                        'url': url,
                        'success': False,
                        'execution_time': time.time() - start_time,
                        'actual_crawler': 'playwright',
                        'mode': 'concurrent→playwright',
                        'playwright_error': f"导航被意外取消（bug）: {str(ce)}"
                    }
                raise
            except Exception as ge:
                logger.error(f"❌ [AWAIT-GOTO-ERROR] goto_task异常: {type(ge).__name__}: {str(ge)[:200]}")
                raise
            
            # 取消未完成的success_wait_task防止泄漏
            if not success_wait_task.done():
                success_wait_task.cancel()
                try:
                    await success_wait_task
                except (asyncio.CancelledError, Exception):
                    pass
            
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
                    
                    failure_markdown = crawler._format_failure_markdown(url, playwright_error=error_msg)
                    ret_err = {
                        'url': url,
                        'success': False,
                        'execution_time': time.time() - start_time,
                        'actual_crawler': 'playwright',
                        'mode': 'concurrent→playwright',
                        'status_code': status_code,
                        'markdown': failure_markdown,
                        'text_length': len(failure_markdown),
                        'playwright_error': error_msg,
                        'smart_detection': detection_result
                    }
                    return ret_err
            
            # 检测是否为PDF
            final_url = page.url
            is_pdf = await crawler._detect_pdf_from_response(response, final_url, page)
            
            if is_pdf:
                logger.info(f"📄 检测到PDF页面: {final_url}")
                try:
                    remaining_ms = config.timeout
                    if task_id:
                        remaining_ms = int(crawler.timeout_manager.get_remaining_time(task_id) * 1000)
                        if remaining_ms <= 0:
                            remaining_ms = config.timeout
                    pdf_ret = await crawler.easy_pdf_crawler.download_pdf_via_request(
                        page=page,
                        url=final_url,
                        timeout=remaining_ms
                    )
                    pdf_ret.update({
                        'execution_time': time.time() - start_time,
                        'actual_crawler': 'pdf',
                        'mode': 'pdf',
                        'smart_detection': detection_result
                    })
                    return pdf_ret
                finally:
                    try:
                        if page and not page.is_closed():
                            await page.close()
                    except Exception:
                        pass
            
            # Web页面处理
            logger.info("🌐 确认为Web页面，继续处理...")
            
            await check_cancellation()
            
            # 创建页面处理任务，并添加到相关任务列表中
            logger.info("🔍 创建页面处理任务...")
            async def enhanced_page_process():
                """增强的页面处理，支持取消检查"""
                return await crawler._process_playwright_page_with_cancellation(page, url, config, task_id, check_cancellation)
            
            page_process_task = asyncio.create_task(enhanced_page_process())
            related_tasks.append(page_process_task)
            
            logger.info("🔍 等待页面处理任务完成...")
            page_result = await page_process_task
            logger.info(f"✅ 页面处理任务完成")
            
            # 添加执行信息
            page_result.update({
                'execution_time': time.time() - start_time,
                'actual_crawler': 'playwright',
                'mode': 'concurrent→playwright',
                'smart_detection': detection_result
            })
            
            # 健康检测（基于 response.status 与文本）
            try:
                status_code_for_health = response.status if response else None
                min_text = getattr(config, 'text_limit', 100)
                page_result = crawler._apply_health_detection_to_result(page_result, status_code_for_health, min_text)
            except Exception as _e:
                logger.debug(f"并发策略 Playwright 健康检测异常: {_e}")
            
            if page_result and page_result.get('success'):
                logger.info("✅ Playwright成功，准备返回结果")
            
            return page_result
                
        except asyncio.CancelledError as cancel_err:
            # 正常的取消，EasyGet已成功
            if easyget_success_flag.is_set():
                logger.info("✅ Playwright任务被HTTP成功信号取消")
            else:
                logger.warning("⚠️ Playwright任务被取消，但HTTP成功标志未设置！这可能是个bug")
            # 再次尝试优雅关闭页面，确保资源释放
            await _close_page_gracefully()
            return None
            
        except Exception as e:
            error_msg = str(e)
            
            # 检查是否是页面关闭异常
            if any(err_pattern in error_msg.lower() for err_pattern in [
                'target page, context or browser has been closed',
                'execution context was destroyed',
                'page has been closed', 'browser context is not open'
            ]):
                if page_close_event_triggered['flag']:
                    logger.info("✅ 页面关闭事件已触发，Playwright优雅退出")
                    return None
                else:
                    logger.warning(f"⚠️ 页面意外关闭: {error_msg}")
                    return None
            
            logger.error(f"❌ Playwright任务异常: {e}")
            return {
                'url': url,
                'success': False,
                'execution_time': time.time() - start_time,
                'actual_crawler': 'playwright',
                'mode': 'concurrent→playwright',
                'playwright_error': error_msg
            }
            
        finally:
            crawler.timeout_manager.cleanup_task(task_id)
            if page and not page.is_closed():
                try:
                    await page.close()
                    logger.debug("✅ Playwright页面已关闭（正常收尾）")
                except Exception as e:
                    logger.debug(f"关闭Playwright页面时出错: {e}")
    
    # 第二步：并发执行三个任务，用gather收拢异常
    try:
        # 创建任务
        easyget_coro = easyget_task()
        jina_coro = jina_task()
        playwright_coro = playwright_task()
        
        # 启动任务
        easyget_asyncio_task = asyncio.create_task(easyget_coro)
        jina_asyncio_task = asyncio.create_task(jina_coro)
        playwright_asyncio_task = asyncio.create_task(playwright_coro)
        
        # ⚡ 等待首个任务完成
        done, pending = await asyncio.wait(
            {easyget_asyncio_task, jina_asyncio_task, playwright_asyncio_task},
            return_when=asyncio.FIRST_COMPLETED
        )

        # 如果EasyGet率先完成且成功，立即返回结果
        if easyget_asyncio_task in done:
            easyget_result = await easyget_asyncio_task
            if easyget_result and isinstance(easyget_result, dict) and easyget_result.get('success'):
                logger.info("🎯 EasyGet先完成且成功，立刻返回结果；Playwright 清理转为后台异步处理")
                # 取消 Playwright 任务，但不阻塞当前返回；在后台等待其结束并做一次空白页清理
                if not playwright_asyncio_task.done():
                    playwright_asyncio_task.cancel()
                    async def _bg_wait_and_cleanup():
                        try:
                            try:
                                await playwright_asyncio_task
                            except (asyncio.CancelledError, Exception):
                                # 🔥 捕获所有异常，避免 "Task exception was never retrieved" 警告
                                pass
                        finally:
                            try:
                                crawler._placeholder_page = await cleanup_orphan_about_blank_pages(crawler.context, crawler.timeout_manager, crawler.placeholder_url, crawler._placeholder_page, crawler._handle_resource_request)
                            except Exception:
                                pass
                    asyncio.create_task(_bg_wait_and_cleanup())
                else:
                    # 若已完成，也做一次保险性的清理（后台进行）
                    async def _bg_cleanup_only():
                        try:
                            crawler._placeholder_page = await cleanup_orphan_about_blank_pages(crawler.context, crawler.timeout_manager, crawler.placeholder_url, crawler._placeholder_page, crawler._handle_resource_request)
                        except Exception:
                            pass
                    asyncio.create_task(_bg_cleanup_only())
                # 若 Jina 仍在运行也取消
                if not jina_asyncio_task.done():
                    jina_asyncio_task.cancel()
                return easyget_result

        # 如果Jina率先完成且成功，立即返回结果
        if jina_asyncio_task in done:
            jina_result_first = await jina_asyncio_task
            if jina_result_first and isinstance(jina_result_first, dict) and jina_result_first.get('success'):
                logger.info("🎯 Jina先完成且成功，立即返回，不等待其他任务")
                # 取消 EasyGet 与 Playwright
                if not easyget_asyncio_task.done():
                    easyget_asyncio_task.cancel()
                    try:
                        await easyget_asyncio_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        logger.warning(f"⚠️ 等待EasyGet取消时出错: {e}")
                if not playwright_asyncio_task.done():
                    playwright_asyncio_task.cancel()
                    async def _bg_wait_and_cleanup2():
                        try:
                            try:
                                await playwright_asyncio_task
                            except (asyncio.CancelledError, Exception):
                                # 🔥 捕获所有异常，避免 "Task exception was never retrieved" 警告
                                pass
                        finally:
                            try:
                                crawler._placeholder_page = await cleanup_orphan_about_blank_pages(crawler.context, crawler.timeout_manager, crawler.placeholder_url, crawler._placeholder_page, crawler._handle_resource_request)
                            except Exception:
                                pass
                    asyncio.create_task(_bg_wait_and_cleanup2())
                else:
                    async def _bg_cleanup_only2():
                        try:
                            crawler._placeholder_page = await cleanup_orphan_about_blank_pages(crawler.context, crawler.timeout_manager, crawler.placeholder_url, crawler._placeholder_page, crawler._handle_resource_request)
                        except Exception:
                            pass
                    asyncio.create_task(_bg_cleanup_only2())
                return jina_result_first

        # 如果Playwright率先完成且成功，立即返回结果并取消EasyGet
        if playwright_asyncio_task in done:
            playwright_result_first = await playwright_asyncio_task
            if playwright_result_first and isinstance(playwright_result_first, dict) and playwright_result_first.get('success'):
                logger.info("🎯 Playwright先完成且成功，立即返回，不等待EasyGet")
                if not easyget_asyncio_task.done():
                    logger.info("🛑 正在取消EasyGet任务...")
                    easyget_asyncio_task.cancel()
                    # 等待取消完成
                    try:
                        await easyget_asyncio_task
                    except asyncio.CancelledError:
                        logger.info("✅ EasyGet任务已成功取消")
                    except Exception as e:
                        logger.warning(f"⚠️ 等待EasyGet取消时出错: {e}")
                else:
                    logger.info("ℹ️ EasyGet任务已完成，无需取消")
                # 取消 Jina 任务
                if not jina_asyncio_task.done():
                    logger.info("🛑 正在取消Jina任务...")
                    jina_asyncio_task.cancel()
                    try:
                        await jina_asyncio_task
                    except asyncio.CancelledError:
                        logger.info("✅ Jina任务已成功取消")
                    except Exception as e:
                        logger.warning(f"⚠️ 等待Jina取消时出错: {e}")
                return playwright_result_first

        # 否则等待剩余任务全部完成（包括异常）
        logger.info("🔄 等待所有任务完成...")
        results = await asyncio.gather(
            easyget_asyncio_task,
            jina_asyncio_task,
            playwright_asyncio_task,
            return_exceptions=True
        )

        easyget_result = results[0] if len(results) > 0 else None
        jina_result = results[1] if len(results) > 1 else None
        playwright_result = results[2] if len(results) > 2 else None
        
        # 处理结果
        for result in [easyget_result, jina_result, playwright_result]:
            if result and isinstance(result, dict) and not isinstance(result, Exception) and result.get('success'):
                logger.info(f"🎯 使用 {result.get('actual_crawler', 'unknown')} 结果")

                # --- 智能学习 & 缓存 ---
                if hasattr(config, 'use_intellicache') and config.use_intellicache:
                    actual_mode = result.get('actual_crawler', 'unknown')
                    reason = f"并发最终成功: {actual_mode}"
                    # 缓存决策（支持 easyget / playwright / jina / pdf）
                    try:
                        decision_for_cache = actual_mode
                        crawler.smart_detector.cache_decision(url, decision_for_cache, reason, detection_result)
                    except Exception as _e:
                        logger.debug(f"缓存决策失败: {_e}")

                    # 学习预测准确性
                    predicted = detection_result.get('recommended_mode') if detection_result else 'concurrent'
                    try:
                        crawler.smart_detector.learn_from_result(url, predicted, result)
                    except Exception as _e:
                        logger.debug(f"学习处理异常: {_e}")

                return result 

        # 如果没有成功的结果，返回错误信息
        logger.error("❌ EasyGet、Jina和Playwright都失败了")

        # 构建错误详情（不包含内部引擎名称）
        easyget_error = "任务未返回结果"
        jina_error = "任务未返回结果"
        playwright_error = "任务未返回结果"

        if isinstance(easyget_result, Exception):
            easyget_error = str(easyget_result)
        elif easyget_result is None:
            easyget_error = "任务返回None（可能被取消或超时）"
        elif isinstance(easyget_result, dict):
            # 🔥 直接从 easyget_error 字段提取
            error_msg = easyget_result.get('easyget_error')
            if not error_msg:
                # 如果没有 error 字段，尝试从其他字段推断
                if not easyget_result.get('success', True):
                    # 🔥 优先检查页面内容是否包含 Cloudflare/验证关键词
                    markdown_text = easyget_result.get('markdown', '')
                    html_text = easyget_result.get('html', '')
                    combined_text = (markdown_text + ' ' + extract_text_from_html(html_text)).lower()
                    
                    # 检测 Cloudflare
                    if any(kw in combined_text for kw in ['cloudflare', 'just a moment', 'are you a robot', 'attention required']):
                        error_msg = "Cloudflare 人机验证拦截"
                    # 检测其他验证码
                    elif any(kw in combined_text for kw in ['captcha', 'robot check', 'human verification', 'are you human']):
                        error_msg = "需要人机验证"
                    # 然后才检查其他信息
                    else:
                        # 尝试从 detection 字段获取更详细的信息
                        detection = easyget_result.get('detection', {})
                        if isinstance(detection, dict):
                            text_length = detection.get('text_length', 0)
                            error_msg = easyget_result.get('msg') or f"质量检查失败(文本长度: {text_length})"
                        else:
                            error_msg = easyget_result.get('msg') or "失败，未提供具体错误信息"
                else:
                    error_msg = "返回了结果但被判定为失败"
            easyget_error = error_msg or "失败，未知原因"

        if isinstance(jina_result, Exception):
            jina_error = str(jina_result)
        elif jina_result is None:
            jina_error = "任务返回None（可能被取消或超时）"
        elif isinstance(jina_result, dict):
            # Jina 专属错误字段
            err = jina_result.get('jina_error') or jina_result.get('error') or jina_result.get('easyget_error')
            if not err and not jina_result.get('success', True):
                md_text = jina_result.get('markdown', '') or jina_result.get('text', '')
                html_text = jina_result.get('html', '')
                combined = (md_text + ' ' + extract_text_from_html(html_text)).lower()
                if any(kw in combined for kw in ['cloudflare', 'just a moment', 'are you a robot', 'attention required']):
                    err = "Cloudflare 人机验证拦截"
                elif any(kw in combined for kw in ['captcha', 'robot check', 'human verification', 'are you human']):
                    err = "需要人机验证"
            jina_error = err or "失败，未知原因"

        if isinstance(playwright_result, Exception):
            playwright_error = str(playwright_result) 
        elif playwright_result is None:
            playwright_error = "任务返回None（可能被取消或超时）"
        elif isinstance(playwright_result, dict):
            # 🔥 直接从 playwright_error 字段提取
            error_msg = playwright_result.get('playwright_error')
            if not error_msg:
                # 如果没有 error 字段，尝试从其他字段推断
                if not playwright_result.get('success', True):
                    # 🔥 优先检查页面内容是否包含 Cloudflare/验证关键词
                    markdown_text = playwright_result.get('markdown', '')
                    html_text = playwright_result.get('html', '')
                    combined_text = (markdown_text + ' ' + extract_text_from_html(html_text)).lower()
                    
                    # 检测 Cloudflare
                    if any(kw in combined_text for kw in ['cloudflare', 'just a moment', 'are you a robot', 'attention required']):
                        error_msg = "Cloudflare 人机验证拦截"
                    # 检测其他验证码
                    elif any(kw in combined_text for kw in ['captcha', 'robot check', 'human verification', 'are you human']):
                        error_msg = "需要人机验证"
                    # 然后才检查状态码
                    elif playwright_result.get('status_code') and playwright_result.get('status_code') >= 400:
                        status = playwright_result.get('status_code')
                        error_msg = f"页面访问失败，状态码: {status}"
                    else:
                        # 尝试从 detection 字段获取更详细的信息
                        detection = playwright_result.get('detection', {})
                        if isinstance(detection, dict):
                            text_length = detection.get('text_length', 0)
                            error_msg = playwright_result.get('msg') or f"质量检查失败(文本长度: {text_length})"
                        else:
                            error_msg = playwright_result.get('msg') or "失败，未提供具体错误信息"
                else:
                    error_msg = "返回了结果但被判定为失败"
            playwright_error = error_msg or "失败，未知原因"

        # 🔥 提取状态码（如果有）
        status_code = None
        # 先尝试从 playwright_result 字典中直接获取
        if isinstance(playwright_result, dict):
            status_code = playwright_result.get('status_code')
        # 如果没有，尝试从错误文本中提取
        if not status_code and playwright_error and isinstance(playwright_error, str):
            import re
            status_match = re.search(r'状态码[：:]\s*(\d+)', playwright_error)
            if status_match:
                try:
                    status_code = int(status_match.group(1))
                except Exception:
                    pass
        # 再尝试从 Jina 结果中获取状态码
        if not status_code and isinstance(jina_result, dict):
            status_code = jina_result.get('status_code') or status_code
        
        # 智能缓存失败（被拦截）
        if hasattr(config, 'use_intellicache') and config.use_intellicache:
            reason_blk = f"并发均失败: EasyGet({easyget_error}) | Jina({jina_error}) | Playwright({playwright_error})"
            try:
                crawler.smart_detector.cache_decision(url, 'blocked', reason_blk, {})
            except Exception as _e:
                logger.debug(f"缓存blocked失败: {_e}")

        failure_markdown = crawler._format_failure_markdown(url, easyget_error=easyget_error, playwright_error=playwright_error, jina_error=jina_error)
        return {
            'url': url,
            'success': False,
            'execution_time': time.time() - start_time,
            'markdown': failure_markdown,
            'text_length': len(failure_markdown),
            'easyget_error': easyget_error,
            'jina_error': jina_error,
            'playwright_error': playwright_error,
            'status_code': status_code  # 如果有提取到状态码，也返回
        }
        
    except Exception as e:
        logger.error(f"❌ 并发爬取异常: {e}")
        error_str = str(e)
        
        # 🔥 并发策略异常：由于无法区分是哪个引擎的异常，将错误信息同时放入三个字段
        failure_markdown = crawler._format_failure_markdown(url, easyget_error=error_str, playwright_error=error_str, jina_error=error_str)
        return {
            'url': url,
            'success': False,
            'execution_time': time.time() - start_time,
            'markdown': failure_markdown,
            'text_length': len(failure_markdown),
            'easyget_error': error_str,
            'jina_error': error_str,
            'playwright_error': error_str
        }


async def crawl_with_concurrent_strategy_no_jina(
    crawler,
    url: str,
    config: CrawlRequest,
    detection_result: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """并发策略（不包含Jina）：同时尝试EasyGet和Playwright - 优雅取消版本
    
    本函数是 crawl_with_concurrent_strategy 的简化版本，移除了 Jina 爬虫，
    只保留 EasyGet 和 Playwright 的并发竞速逻辑。
    
    Args:
        crawler: PlaywrightCrawler 实例
        url: 要爬取的 URL
        config: 爬取配置
        detection_result: 智能检测结果（可选）
    
    Returns:
        爬取结果字典
    """
    start_time = time.time()
    
    logger.info(f"🔄 并发策略启动（no_jina模式）: {url}")
    if detection_result:
        logger.info(f"📊 智能检测信息: {detection_result.get('recommended_mode', 'unknown')} - {detection_result.get('reason', 'no reason')}")
    
    # 第一步：约定线程安全的共享标志
    # 说明：该事件用于"HTTP类竞速成功信号"（EasyGet成功即置位）
    easyget_success_flag = asyncio.Event()
    # 只有当 HTTP 类结果真正通过健康检测时才会写入，用于防止"误置位 flag"导致误取消 Playwright
    verified_http_success = {'result': None, 'source': None}
    playwright_page_ref = {'page': None}
    page_close_lock = asyncio.Lock()  # 确保page.close()只调用一次
    related_tasks = []  # 存储所有相关任务，用于统一取消
    page_close_event_triggered = {'flag': False}  # 页面关闭事件标志
    
    async def _close_page_gracefully(max_wait: float = 3.0, poll_interval: float = 0.2):
        """第三步：优雅关闭页面，避免竞态条件
        1. 当 EasyGet 已成功而 Playwright 页面尚未创建时，
           先轮询等待一小段时间（默认 ≤3s）直到页面对象就绪。
        2. 拿到页面后执行 stopLoading -> page.close() 的完整关闭流程。
        3. 即使等待超时仍未拿到页面，也会直接返回，防止死锁。
        """
        logger.info("🛑 执行优雅关闭页面…")
        
        async with page_close_lock:  # 确保只关闭一次
            page = playwright_page_ref.get('page')
            waited = 0.0
            # 如果此时页面尚未创建，短暂轮询等待
            while page is None and waited < max_wait:
                await asyncio.sleep(poll_interval)
                waited += poll_interval
                page = playwright_page_ref.get('page')

            if not page:
                logger.debug("[GracefulClose] 等待页面对象超时，未获取到 page，跳过关闭")
                return

            if page.is_closed():
                logger.debug("[GracefulClose] 页面已关闭，无需处理")
                return

            try:
                logger.info("🛑 执行优雅关闭页面…")
                # 先阻止网络流水线，减少 Edge 线程残留
                try:
                    client = await crawler._safe_new_cdp_session(page)
                    if client:
                        await client.send("Page.stopLoading")
                        await client.detach()
                    else:
                        logger.debug("_close_page_gracefully: 无法创建 CDP 会话，可能页面已关闭")
                except Exception:
                    pass  # 忽略 CDP 错误

                # 取消 still running 相关任务，避免悬挂
                for task in related_tasks:
                    if not task.done():
                        task.cancel()

                await page.close()
                logger.info("✅ 页面已优雅关闭")
            except Exception as e:
                logger.debug(f"优雅关闭页面时出错: {e}")
    
    async def easyget_task():
        """EasyGet任务 - 发出成功信号并触发优雅关闭"""
        try:
            logger.debug("🚀 启动EasyGet爬虫...")
            
            # 从配置获取 EasyGet 超时时间，默认5秒
            easyget_timeout = getattr(config, 'easyget_timeout', 5)
            max_redirects = int(config.max_redirects) if config.max_redirects is not None else 10
            
            logger.info(f"⏰ EasyGet并发模式超时: {easyget_timeout}秒")
            
            result = await crawler.easy_crawler.crawl_single_url(
                url=url,
                timeout=easyget_timeout,
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
            
            success = result.get('success', False)
            if not success:
                error_msg = result.get('error', '失败，未提供具体错误信息')
                logger.info(f"⚠️ EasyGet失败: {error_msg}，等待Playwright结果")
                # 🔥 返回包含错误信息的字典，而不是 None，便于后续提取错误详情
                return {
                    'success': False,
                    'url': url,
                    'actual_crawler': 'easyget',
                    'easyget_error': error_msg  # 🔥 直接放在 easyget_error
                }

            # --- PDF 检测：如果 EasyGet 已经成功处理了 PDF，直接返回 ---
            if result.get('is_pdf_page') and result.get('markdown'):
                logger.info("📄 EasyGet成功处理PDF，准备进行健康检测")
                # 直接返回 PDF 结果，添加执行信息
                result.update({
                    'execution_time': time.time() - start_time,
                    'actual_crawler': 'easyget_pdf',
                    'mode': 'concurrent_no_jina→easyget_pdf',
                    'smart_detection': detection_result,
                    'easyget_timeout_used': easyget_timeout
                })
                # 健康检测：通过才宣告并发成功（避免"先成功后失败"打断 Playwright）
                try:
                    status_code = result.get('status_code')
                    min_text = getattr(config, 'text_limit', 100)
                    result = crawler._apply_health_detection_to_result(result, status_code, min_text)
                except Exception as _e:
                    logger.debug(f"并发策略 EasyGet(PDF) 健康检测异常: {_e}")
                if result.get('success'):
                    logger.info("✅ EasyGet(PDF) 通过健康检测，发出成功信号")
                    verified_http_success['result'] = result
                    verified_http_success['source'] = 'easyget_pdf'
                    easyget_success_flag.set()
                    await asyncio.sleep(0)
                    asyncio.create_task(_close_page_gracefully())
                else:
                    logger.info(f"⚠️ EasyGet(PDF) 健康检测未通过，继续等待Playwright (success={result.get('success')}, text_length={result.get('text_length')})")
                return result
            
            # --- PDF 检测：若响应为 PDF 但未处理，则交由 Playwright 处理 ---
            content_type_header = result.get('content_type', '').lower()
            final_url_lower = result.get('final_url', url).lower()
            if 'application/pdf' in content_type_header or final_url_lower.endswith('.pdf'):
                logger.info("📄 EasyGet检测到PDF响应但未处理，放弃EasyGet结果，等待Playwright继续处理")
                return None
            
            # 检查内容质量
            is_garbled = result.get('is_garbled', False)
            is_binary = result.get('is_binary', False)
            magic_type = result.get('magic_type', '')
            
            # 如果内容是乱码或二进制，且不是图片类型，则认为EasyGet失败
            if (is_garbled or is_binary) and 'image' not in magic_type.lower():
                logger.info(f"⚠️ EasyGet检测到乱码或二进制内容 (is_garbled={is_garbled}, is_binary={is_binary}, magic_type={magic_type})，等待Playwright结果")
                return None
            
            # 直接认为 EasyGet 已返回有效内容，发出成功信号
            logger.info("✅ EasyGet抓取到内容，准备进行健康检测（通过才宣告成功）")
            
            # 🔥 关键优化：检查EasyGet是否已经清理过HTML
            html_already_cleaned = result.get('html_cleaned', False)
            markdown_content = result.get('markdown', '')
            html_content = result.get('html', '')
            
            # 构建基础结果字典
            result_dict = {
                'url': url,
                'final_url': result.get('final_url', url),
                'success': True,
                'execution_time': time.time() - start_time,
                'actual_crawler': 'easyget',
                'mode': 'concurrent_no_jina→easyget',
                'smart_detection': detection_result,
                'easyget_timeout_used': easyget_timeout
            }
            
            # 添加title字段（如果提取成功）
            if config.extract_title and result.get('title'):
                result_dict['title'] = result['title']
            
            # 根据是否已清理和用户配置决定返回内容
            if html_already_cleaned and markdown_content:
                # EasyGet已经清理过，直接使用，避免重复清理
                logger.info(f"🎯 EasyGet已完成HTML清理，直接使用markdown ({len(markdown_content)} 字符)")
                result_dict['markdown'] = markdown_content
                result_dict['text_length'] = len(markdown_content.strip())
                # 健康检测
                try:
                    status_code = result.get('status_code')
                    min_text = getattr(config, 'text_limit', 100)
                    logger.debug(f"🔍 EasyGet健康检测前: success={result_dict.get('success')}, text_length={result_dict.get('text_length')}, min_text={min_text}")
                    result_dict = crawler._apply_health_detection_to_result(result_dict, status_code, min_text)
                    logger.debug(f"🔍 EasyGet健康检测后: success={result_dict.get('success')}")
                except Exception as _e:
                    logger.error(f"❌ 并发策略 EasyGet(已清理) 健康检测异常: {_e}")
                    result_dict['success'] = False
                # 只有健康检测通过，才发出"并发成功"信号并关闭页面
                if result_dict.get('success'):
                    logger.info(f"✅ EasyGet(清理后)通过健康检测，发出成功信号 (text_length={result_dict.get('text_length')}, status_code={result.get('status_code')})")
                    verified_http_success['result'] = result_dict
                    verified_http_success['source'] = 'easyget_cleaned'
                    easyget_success_flag.set()
                    await asyncio.sleep(0)
                    asyncio.create_task(_close_page_gracefully())
                else:
                    logger.info(f"⚠️ EasyGet(清理后)健康检测未通过，继续等待Playwright (success={result_dict.get('success')}, text_length={result_dict.get('text_length')}, detection={result_dict.get('detection')})")
                return result_dict
            
            else:
                # 不需要清理或没有内容，直接返回html
                logger.info("📄 返回原始HTML（未启用清理或无内容）")
                result_dict['html'] = html_content
                result_dict['text_length'] = len(html_content.strip()) if html_content else 0
                # 健康检测
                try:
                    status_code = result.get('status_code')
                    min_text = getattr(config, 'text_limit', 100)
                    result_dict = crawler._apply_health_detection_to_result(result_dict, status_code, min_text)
                except Exception as _e:
                    logger.debug(f"并发策略 EasyGet(原始HTML) 健康检测异常: {_e}")
                if result_dict.get('success'):
                    logger.info(f"✅ EasyGet(原始HTML)通过健康检测，发出成功信号 (text_length={result_dict.get('text_length')}, status_code={result.get('status_code')})")
                    verified_http_success['result'] = result_dict
                    verified_http_success['source'] = 'easyget_raw_html'
                    easyget_success_flag.set()
                    await asyncio.sleep(0)
                    asyncio.create_task(_close_page_gracefully())
                else:
                    logger.info(f"⚠️ EasyGet(原始HTML)健康检测未通过，继续等待Playwright (success={result_dict.get('success')}, text_length={result_dict.get('text_length')}, detection={result_dict.get('detection')})")
                return result_dict
                
        except asyncio.CancelledError:
            logger.info("🛑 EasyGet任务已被取消（Playwright已成功）")
            raise
        except Exception as e:
            logger.warning(f"EasyGet任务异常: {e}")
            return None
    
    async def playwright_task():
        """Playwright任务 - 全程监听EasyGet信号并优雅收尾"""
        page = None
        task_id = f"playwright_{url}_{int(time.time() * 1000)}"
        logger.info(f"🔍 Playwright任务启动: task_id={task_id}")
        
        async def check_cancellation():
            """检查是否应该取消当前操作"""
            if easyget_success_flag.is_set() and verified_http_success.get('result') is not None:
                src = verified_http_success.get('source') or 'http'
                logger.info(f"🛑 检测到HTTP成功信号({src})，Playwright主动退出")
                raise asyncio.CancelledError("HTTP已成功")
        
        try:
            logger.info("🎭 启动Playwright爬虫...")
            
            # 在每个主要操作前检查取消状态
            await check_cancellation()
            
            # 初始化Playwright
            await crawler.initialize(
                use_edge_user_data=config.use_edge_user_data,
                enable_javascript=config.enable_javascript,
                user_agent=config.user_agent,
                fast_mode=False
            )
            
            await check_cancellation()
            
            if not crawler.context:
                logger.warning("⚠️ 浏览器上下文为空，重新初始化...")
                await crawler.initialize(
                    use_edge_user_data=config.use_edge_user_data,
                    enable_javascript=config.enable_javascript,
                    user_agent=config.user_agent,
                    fast_mode=False
                )
                if not crawler.context:
                    raise Exception("浏览器上下文重新初始化失败")
            
            await check_cancellation()
            
            # 创建页面前检查 context 状态
            try:
                if not crawler.context:
                    logger.error(f"❌ Context 为 None，无法创建页面！task_id={task_id}")
                    raise Exception("Browser context is None")
                
                # 检查 context 是否已关闭
                try:
                    crawler.context.pages
                except Exception as check_err:
                    logger.error(f"❌ 检查Context状态失败: {check_err}, task_id={task_id}")
                    raise Exception(f"Browser context check failed: {check_err}")
                
                # 检查清理状态
                if crawler._is_closing:
                    logger.warning(f"⚠️ 检测到清理状态，跳过页面创建！task_id={task_id}")
                    return None
                
            except Exception as pre_check_err:
                logger.error(f"❌ 页面创建前检查失败: {pre_check_err}, task_id={task_id}")
                raise
            
            # 创建页面
            try:
                page = await crawler.context.new_page()
                playwright_page_ref['page'] = page
            except Exception as page_create_err:
                error_msg = str(page_create_err)
                logger.error(f"❌ 页面创建失败: {error_msg}, task_id={task_id}")
                raise
            
            # 第二步：绑定页面关闭回调，转化异常为普通返回值
            async def on_page_close():
                page_close_event_triggered['flag'] = True
                logger.warning(f"⚠️ 检测到页面关闭事件！task_id={task_id}")
            
            page.once('close', lambda: asyncio.create_task(on_page_close()))
            
            # 注册超时管理
            crawler.timeout_manager.register_task(task_id, page, config.timeout)
            
            await check_cancellation()
            
            # 设置资源阻塞
            if crawler.resource_blocking_enabled:
                await page.route("**/*", crawler._handle_resource_request)
            
            await check_cancellation()
            
            # 设置额外请求头
            if config.extra_headers:
                await page.set_extra_http_headers(config.extra_headers)
            
            await check_cancellation()
            
            # 导航到页面 - 设置timeout >= EasyGet超时避免早关页
            easyget_timeout_ms = getattr(config, 'easyget_timeout', 5) * 1000
            goto_timeout = max(easyget_timeout_ms, config.timeout)
            
            # --- 并发监听EasyGet成功信号，随时中断goto ---
            success_wait_task = asyncio.create_task(easyget_success_flag.wait())
            wait_state = config.wait_for_load_state or "commit"
            goto_task = asyncio.create_task(
                page.goto(
                    crawler._normalize_entry_url(url),
                    wait_until=wait_state,
                    timeout=goto_timeout
                )
            )
            logger.info(f"🔄 等待goto完成 (wait_until={wait_state}): {url}")
            done, pending = await asyncio.wait(
                {goto_task, success_wait_task},
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # 如果 HTTP 类成功信号率先到达，立即取消 goto 并退出
            if success_wait_task in done and easyget_success_flag.is_set() and verified_http_success.get('result') is not None:
                logger.info(f"🛑 检测到HTTP成功信号，取消Playwright goto 并退出 (wait_until={wait_state}): {url}")
                if not goto_task.done():
                    goto_task.cancel()
                    try:
                        await goto_task
                    except (asyncio.CancelledError, Exception):
                        pass
                raise asyncio.CancelledError("HTTP已成功，取消Playwright导航")

            if goto_task in done:
                logger.info(f"✅ goto已经完成 (wait_until={wait_state}): {url}")
                try:
                    if goto_task.done() and not goto_task.cancelled():
                        exc = goto_task.exception()
                        if exc:
                            logger.warning(f"⚠️ [GOTO-EXCEPTION] goto_task有异常: {type(exc).__name__}: {str(exc)[:200]}")
                except Exception as check_err:
                    logger.debug(f"检查goto_task异常时出错: {check_err}")
            else:
                logger.warning(f"⚠️ goto 等待提前返回，但未命中成功信号；继续等待 goto: {url}")
            
            try:
                response = await goto_task
            except asyncio.CancelledError as ce:
                logger.error(f"❌ [AWAIT-GOTO-CANCELLED] goto_task被取消!")
                if not easyget_success_flag.is_set():
                    logger.error("❌ [BUG-CONFIRMED] Playwright被取消但HTTP未成功！")
                    return {
                        'url': url,
                        'success': False,
                        'execution_time': time.time() - start_time,
                        'actual_crawler': 'playwright',
                        'mode': 'concurrent_no_jina→playwright',
                        'playwright_error': f"导航被意外取消（bug）: {str(ce)}"
                    }
                raise
            except Exception as ge:
                logger.error(f"❌ [AWAIT-GOTO-ERROR] goto_task异常: {type(ge).__name__}: {str(ge)[:200]}")
                raise
            
            if not success_wait_task.done():
                success_wait_task.cancel()
                try:
                    await success_wait_task
                except (asyncio.CancelledError, Exception):
                    pass
            
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
                    
                    failure_markdown = crawler._format_failure_markdown(url, playwright_error=error_msg)
                    return {
                        'url': url,
                        'success': False,
                        'execution_time': time.time() - start_time,
                        'actual_crawler': 'playwright',
                        'mode': 'concurrent_no_jina→playwright',
                        'status_code': status_code,
                        'markdown': failure_markdown,
                        'text_length': len(failure_markdown),
                        'playwright_error': error_msg,
                        'smart_detection': detection_result
                    }
            
            # 检测是否为PDF
            final_url = page.url
            is_pdf = await crawler._detect_pdf_from_response(response, final_url, page)
            
            if is_pdf:
                logger.info(f"📄 检测到PDF页面: {final_url}")
                try:
                    remaining_ms = config.timeout
                    if task_id:
                        remaining_ms = int(crawler.timeout_manager.get_remaining_time(task_id) * 1000)
                        if remaining_ms <= 0:
                            remaining_ms = config.timeout
                    pdf_ret = await crawler.easy_pdf_crawler.download_pdf_via_request(
                        page=page,
                        url=final_url,
                        timeout=remaining_ms
                    )
                    pdf_ret.update({
                        'execution_time': time.time() - start_time,
                        'actual_crawler': 'pdf',
                        'mode': 'pdf',
                        'smart_detection': detection_result
                    })
                    return pdf_ret
                finally:
                    try:
                        if page and not page.is_closed():
                            await page.close()
                    except Exception:
                        pass
            
            # Web页面处理
            logger.info("🌐 确认为Web页面，继续处理...")
            
            await check_cancellation()
            
            logger.info("🔍 创建页面处理任务...")
            async def enhanced_page_process():
                return await crawler._process_playwright_page_with_cancellation(page, url, config, task_id, check_cancellation)
            
            page_process_task = asyncio.create_task(enhanced_page_process())
            related_tasks.append(page_process_task)
            
            logger.info("🔍 等待页面处理任务完成...")
            page_result = await page_process_task
            logger.info(f"✅ 页面处理任务完成")
            
            page_result.update({
                'execution_time': time.time() - start_time,
                'actual_crawler': 'playwright',
                'mode': 'concurrent_no_jina→playwright',
                'smart_detection': detection_result
            })
            
            # 健康检测
            try:
                status_code_for_health = response.status if response else None
                min_text = getattr(config, 'text_limit', 100)
                page_result = crawler._apply_health_detection_to_result(page_result, status_code_for_health, min_text)
            except Exception as _e:
                logger.debug(f"并发策略 Playwright 健康检测异常: {_e}")
            
            if page_result and page_result.get('success'):
                logger.info("✅ Playwright成功，准备返回结果")
            
            return page_result
            
        except asyncio.CancelledError as cancel_err:
            if easyget_success_flag.is_set():
                logger.info("✅ Playwright任务被HTTP成功信号取消")
            else:
                logger.warning("⚠️ Playwright任务被取消，但HTTP成功标志未设置！")
            await _close_page_gracefully()
            return None
            
        except Exception as e:
            error_msg = str(e)
            
            # 检查是否是页面关闭异常
            if any(err_pattern in error_msg.lower() for err_pattern in [
                'target page, context or browser has been closed',
                'execution context was destroyed',
                'page has been closed', 'browser context is not open'
            ]):
                if page_close_event_triggered['flag']:
                    logger.info("✅ 页面关闭事件已触发，Playwright优雅退出")
                    return None
                else:
                    logger.warning(f"⚠️ 页面意外关闭: {error_msg}")
                    return None
            
            logger.error(f"❌ Playwright任务异常: {e}")
            return {
                'url': url,
                'success': False,
                'execution_time': time.time() - start_time,
                'actual_crawler': 'playwright',
                'mode': 'concurrent_no_jina→playwright',
                'playwright_error': error_msg
            }
            
        finally:
            crawler.timeout_manager.cleanup_task(task_id)
            if page and not page.is_closed():
                try:
                    await page.close()
                    logger.debug("✅ Playwright页面已关闭（正常收尾）")
                except Exception as e:
                    logger.debug(f"关闭Playwright页面时出错: {e}")
    
    # 第二步：并发执行两个任务，用gather收拢异常
    try:
        # 创建任务
        easyget_coro = easyget_task()
        playwright_coro = playwright_task()
        
        # 启动任务
        easyget_asyncio_task = asyncio.create_task(easyget_coro)
        playwright_asyncio_task = asyncio.create_task(playwright_coro)
        
        # ⚡ 等待首个任务完成
        done, pending = await asyncio.wait(
            {easyget_asyncio_task, playwright_asyncio_task},
            return_when=asyncio.FIRST_COMPLETED
        )

        # 如果EasyGet率先完成且成功，立即返回结果
        if easyget_asyncio_task in done:
            easyget_result = await easyget_asyncio_task
            if easyget_result and isinstance(easyget_result, dict) and easyget_result.get('success'):
                logger.info("🎯 EasyGet先完成且成功，立刻返回结果；Playwright 清理转为后台异步处理")
                # 取消 Playwright 任务，但不阻塞当前返回；在后台等待其结束并做一次空白页清理
                if not playwright_asyncio_task.done():
                    playwright_asyncio_task.cancel()
                    async def _bg_wait_and_cleanup():
                        try:
                            try:
                                await playwright_asyncio_task
                            except (asyncio.CancelledError, Exception):
                                # 🔥 捕获所有异常，避免 "Task exception was never retrieved" 警告
                                pass
                        finally:
                            try:
                                crawler._placeholder_page = await cleanup_orphan_about_blank_pages(crawler.context, crawler.timeout_manager, crawler.placeholder_url, crawler._placeholder_page, crawler._handle_resource_request)
                            except Exception:
                                pass
                    asyncio.create_task(_bg_wait_and_cleanup())
                else:
                    # 若已完成，也做一次保险性的清理（后台进行）
                    async def _bg_cleanup_only():
                        try:
                            crawler._placeholder_page = await cleanup_orphan_about_blank_pages(crawler.context, crawler.timeout_manager, crawler.placeholder_url, crawler._placeholder_page, crawler._handle_resource_request)
                        except Exception:
                            pass
                    asyncio.create_task(_bg_cleanup_only())
                return easyget_result

        # 如果Playwright率先完成且成功，立即返回结果并取消EasyGet
        if playwright_asyncio_task in done:
            playwright_result_first = await playwright_asyncio_task
            if playwright_result_first and isinstance(playwright_result_first, dict) and playwright_result_first.get('success'):
                logger.info("🎯 Playwright先完成且成功，立即返回，不等待EasyGet")
                if not easyget_asyncio_task.done():
                    logger.info("🛑 正在取消EasyGet任务...")
                    easyget_asyncio_task.cancel()
                    # 等待取消完成
                    try:
                        await easyget_asyncio_task
                    except asyncio.CancelledError:
                        logger.info("✅ EasyGet任务已成功取消")
                    except Exception as e:
                        logger.warning(f"⚠️ 等待EasyGet取消时出错: {e}")
                else:
                    logger.info("ℹ️ EasyGet任务已完成，无需取消")
                return playwright_result_first

        # 否则等待剩余任务全部完成（包括异常）
        logger.info("🔄 等待所有任务完成...")
        results = await asyncio.gather(
            easyget_asyncio_task,
            playwright_asyncio_task,
            return_exceptions=True
        )

        easyget_result = results[0] if len(results) > 0 else None
        playwright_result = results[1] if len(results) > 1 else None
        
        # 处理结果
        for result in [easyget_result, playwright_result]:
            if result and isinstance(result, dict) and not isinstance(result, Exception) and result.get('success'):
                logger.info(f"🎯 使用 {result.get('actual_crawler', 'unknown')} 结果")

                # --- 智能学习 & 缓存 ---
                if hasattr(config, 'use_intellicache') and config.use_intellicache:
                    actual_mode = result.get('actual_crawler', 'unknown')
                    reason = f"并发最终成功(no_jina): {actual_mode}"
                    # 缓存决策（支持 easyget / playwright / pdf）
                    try:
                        decision_for_cache = actual_mode
                        crawler.smart_detector.cache_decision(url, decision_for_cache, reason, detection_result)
                    except Exception as _e:
                        logger.debug(f"缓存决策失败: {_e}")

                    # 学习预测准确性
                    predicted = detection_result.get('recommended_mode') if detection_result else 'concurrent'
                    try:
                        crawler.smart_detector.learn_from_result(url, predicted, result)
                    except Exception as _e:
                        logger.debug(f"学习处理异常: {_e}")

                return result 

        # 如果没有成功的结果，返回错误信息
        logger.error("❌ EasyGet和Playwright都失败了")

        # 构建错误详情（不包含内部引擎名称）
        easyget_error = "任务未返回结果"
        playwright_error = "任务未返回结果"

        if isinstance(easyget_result, Exception):
            easyget_error = str(easyget_result)
        elif easyget_result is None:
            easyget_error = "任务返回None（可能被取消或超时）"
        elif isinstance(easyget_result, dict):
            # 🔥 直接从 easyget_error 字段提取
            error_msg = easyget_result.get('easyget_error')
            if not error_msg:
                # 如果没有 error 字段，尝试从其他字段推断
                if not easyget_result.get('success', True):
                    # 🔥 优先检查页面内容是否包含 Cloudflare/验证关键词
                    markdown_text = easyget_result.get('markdown', '')
                    html_text = easyget_result.get('html', '')
                    combined_text = (markdown_text + ' ' + extract_text_from_html(html_text)).lower()
                    
                    # 检测 Cloudflare
                    if any(kw in combined_text for kw in ['cloudflare', 'just a moment', 'are you a robot', 'attention required']):
                        error_msg = "Cloudflare 人机验证拦截"
                    # 检测其他验证码
                    elif any(kw in combined_text for kw in ['captcha', 'robot check', 'human verification', 'are you human']):
                        error_msg = "需要人机验证"
                    # 然后才检查其他信息
                    else:
                        # 尝试从 detection 字段获取更详细的信息
                        detection = easyget_result.get('detection', {})
                        if isinstance(detection, dict):
                            text_length = detection.get('text_length', 0)
                            error_msg = easyget_result.get('msg') or f"质量检查失败(文本长度: {text_length})"
                        else:
                            error_msg = easyget_result.get('msg') or "失败，未提供具体错误信息"
                else:
                    error_msg = "返回了结果但被判定为失败"
            easyget_error = error_msg or "失败，未知原因"

        if isinstance(playwright_result, Exception):
            playwright_error = str(playwright_result) 
        elif playwright_result is None:
            playwright_error = "任务返回None（可能被取消或超时）"
        elif isinstance(playwright_result, dict):
            # 🔥 直接从 playwright_error 字段提取
            error_msg = playwright_result.get('playwright_error')
            if not error_msg:
                # 如果没有 error 字段，尝试从其他字段推断
                if not playwright_result.get('success', True):
                    # 🔥 优先检查页面内容是否包含 Cloudflare/验证关键词
                    markdown_text = playwright_result.get('markdown', '')
                    html_text = playwright_result.get('html', '')
                    combined_text = (markdown_text + ' ' + extract_text_from_html(html_text)).lower()
                    
                    # 检测 Cloudflare
                    if any(kw in combined_text for kw in ['cloudflare', 'just a moment', 'are you a robot', 'attention required']):
                        error_msg = "Cloudflare 人机验证拦截"
                    # 检测其他验证码
                    elif any(kw in combined_text for kw in ['captcha', 'robot check', 'human verification', 'are you human']):
                        error_msg = "需要人机验证"
                    # 然后才检查状态码
                    elif playwright_result.get('status_code') and playwright_result.get('status_code') >= 400:
                        status = playwright_result.get('status_code')
                        error_msg = f"页面访问失败，状态码: {status}"
                    else:
                        # 尝试从 detection 字段获取更详细的信息
                        detection = playwright_result.get('detection', {})
                        if isinstance(detection, dict):
                            text_length = detection.get('text_length', 0)
                            error_msg = playwright_result.get('msg') or f"质量检查失败(文本长度: {text_length})"
                        else:
                            error_msg = playwright_result.get('msg') or "失败，未提供具体错误信息"
                else:
                    error_msg = "返回了结果但被判定为失败"
            playwright_error = error_msg or "失败，未知原因"

        # 🔥 提取状态码（如果有）
        status_code = None
        # 先尝试从 playwright_result 字典中直接获取
        if isinstance(playwright_result, dict):
            status_code = playwright_result.get('status_code')
        # 如果没有，尝试从错误文本中提取
        if not status_code and playwright_error and isinstance(playwright_error, str):
            import re
            status_match = re.search(r'状态码[：:]\s*(\d+)', playwright_error)
            if status_match:
                try:
                    status_code = int(status_match.group(1))
                except Exception:
                    pass
        
        # 智能缓存失败（被拦截）
        if hasattr(config, 'use_intellicache') and config.use_intellicache:
            reason_blk = f"并发均失败(no_jina): EasyGet({easyget_error}) | Playwright({playwright_error})"
            try:
                crawler.smart_detector.cache_decision(url, 'blocked', reason_blk, {})
            except Exception as _e:
                logger.debug(f"缓存blocked失败: {_e}")

        failure_markdown = crawler._format_failure_markdown(url, easyget_error=easyget_error, playwright_error=playwright_error)
        return {
            'url': url,
            'success': False,
            'execution_time': time.time() - start_time,
            'markdown': failure_markdown,
            'text_length': len(failure_markdown),
            'easyget_error': easyget_error,
            'playwright_error': playwright_error,
            'status_code': status_code  # 如果有提取到状态码，也返回
        }
        
    except Exception as e:
        logger.error(f"❌ 并发爬取异常(no_jina): {e}")
        error_str = str(e)
        
        # 🔥 并发策略异常：由于无法区分是哪个引擎的异常，将错误信息同时放入两个字段
        failure_markdown = crawler._format_failure_markdown(url, easyget_error=error_str, playwright_error=error_str)
        return {
            'url': url,
            'success': False,
            'execution_time': time.time() - start_time,
            'markdown': failure_markdown,
            'text_length': len(failure_markdown),
            'easyget_error': error_str,
            'playwright_error': error_str
        }
