import requests
import random
import configparser
import os
import sys
import json
import time
import asyncio
import yaml
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote
import logging
import aiohttp
import threading
from adapt_fetch.proxy.change_proxy_assist import (
    record_usage,
    select_with_weight,
)

# 使用全局日志配置（继承自 start_unified.py）
logger = logging.getLogger(__name__)

# 使用历史 JSON 统一落在 unified_backend/proxy_state/，避免污染 cwd
from pathlib import Path

_PROXY_STATE_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "proxy_state"

# 7899 专用 Clash API 固定配置。
# 这套给 PubMed / Playwright / OpenAlex 等 7899 链路使用，不再走 conf.ini。
LIGHTREAD_CLASH_7899_API_PORT = "19099"
LIGHTREAD_CLASH_7899_API_SECRET = ""
LIGHTREAD_CLASH_7899_SELECTOR = "GLOBAL"


def _usage_history_json_path(instance_name: str) -> str:
    return str(_PROXY_STATE_DIR / f"proxy_usage_{instance_name}.json")


# 移除所有代理设置，确保直接访问Clash API
for proxy_env in ['http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY']:
    if proxy_env in os.environ:
        del os.environ[proxy_env]

# === 统一HTTP请求助手 ===
from urllib.parse import urlparse, urlunparse

def _force_localhost(url: str) -> str:
    try:
        parsed = urlparse(url)
        # 强制使用127.0.0.1而不是localhost，避免hosts解析或代理拦截
        netloc = f"127.0.0.1:{parsed.port}" if parsed.port else "127.0.0.1"
        return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        return url

def _http_request(method: str, url: str, **kwargs):
    """封装requests调用，禁用环境代理与代理表，并强制127.0.0.1"""
    import requests as _req
    session = _req.Session()
    # 禁用环境变量代理
    try:
        session.trust_env = False
    except Exception:
        pass
    # 显式禁用代理
    session.proxies = {}

    final_url = _force_localhost(url)

    # 仅允许传递给 Request 的参数
    req_kwargs = {
        'headers': kwargs.get('headers'),
        'data': kwargs.get('data'),
        'json': kwargs.get('json'),
        'params': kwargs.get('params'),
    }
    # 提取超时，默认10秒
    timeout = kwargs.get('timeout', 10)

    # 构造并发送请求
    req = _req.Request(method=method.upper(), url=final_url, **req_kwargs)
    prepped = session.prepare_request(req)
    return session.send(prepped, timeout=timeout)

 
class ClashProxyManager:
    """Clash 代理管理器 - 支持多实例独立管理。

    ``config_path`` 指向一个 INI 文件（默认 conf.ini），其中 ``[clash api]`` 节包含：
    - ``port``    — Clash 外部控制端口
    - ``secret``  — Clash API 密钥（可为空）
    - ``selector``— 目标策略组名称（如 ``Cat Box``）

    ``selector_config_path`` 为可选的兼容参数（旧版曾用 rule_cfg.ini 分离 selector）；
    推荐所有字段都放在同一个 conf.ini 中，不再拆分。
    """

    def __init__(
        self,
        config_path: str = "conf.ini",
        instance_name: str = None,
        selector_config_path: Optional[str] = None,
        fixed_port: Optional[str] = None,
        fixed_secret: Optional[str] = None,
        fixed_selector: Optional[str] = None,
    ):
        self.port = None
        self.secret = None
        self.selector = None
        self.config_path = config_path
        self.selector_config_path = selector_config_path
        self.fixed_port = str(fixed_port).strip() if fixed_port is not None else None
        self.fixed_secret = "" if fixed_secret is None else str(fixed_secret)
        self.fixed_selector = fixed_selector.strip() if fixed_selector else None
        # 与当前 Clash /proxies 对齐后的策略组名（GET 成功后写入，避免 ini 与运行配置不一致）
        self._resolved_selector_name: Optional[str] = None
        # 实例名称，用于区分不同的代理管理器
        if instance_name is None:
            instance_name = os.path.splitext(os.path.basename(config_path))[0]
        self.instance_name = instance_name
        # 使用历史文件（抗重复）
        self.usage_history_file = _usage_history_json_path(self.instance_name)

        # 选择评分权重与策略
        try:
            self.weight_delay = float(os.getenv("PROXY_WEIGHT_DELAY", "0.7"))
        except Exception:
            self.weight_delay = 0.7
        try:
            self.cooldown_seconds = int(os.getenv("PROXY_RECENCY_COOLDOWN_SEC", "300"))
        except Exception:
            self.cooldown_seconds = 300
        try:
            self.selection_topk = max(1, int(os.getenv("PROXY_SELECTION_TOPK", "1")))
        except Exception:
            self.selection_topk = 1

        self._load_config()

    def _load_config(self):
        """加载 Clash API：port/secret 来自主配置；selector 可来自独立 ini。"""
        try:
            if self.fixed_port and self.fixed_selector:
                self.port = self.fixed_port
                self.secret = self.fixed_secret
                self.selector = self.fixed_selector
                self.base_url = f"http://127.0.0.1:{self.port}"
                logger.info(
                    f"✅ Clash API 加载完成 (实例: {self.instance_name}) 端口={self.port} "
                    f"selector={self.selector!r} (固定配置)"
                )
                return

            if not os.path.exists(self.config_path):
                logger.warning(f"配置文件 {self.config_path} 不存在 (实例: {self.instance_name})")
                return
            cf_main = configparser.ConfigParser()
            cf_main.read(self.config_path, encoding='utf-8')
            self.port = cf_main.get('clash api', 'port')
            self.secret = cf_main.get('clash api', 'secret')

            sel_path = self.selector_config_path or self.config_path
            if sel_path == self.config_path:
                self.selector = cf_main.get('clash api', 'selector')
            else:
                if not os.path.exists(sel_path):
                    logger.warning(
                        f"selector 配置 {sel_path} 不存在，回退主配置中的 selector (实例: {self.instance_name})"
                    )
                    self.selector = cf_main.get('clash api', 'selector')
                else:
                    cf_sel = configparser.ConfigParser()
                    cf_sel.read(sel_path, encoding='utf-8')
                    self.selector = cf_sel.get('clash api', 'selector')

            self.base_url = f"http://127.0.0.1:{self.port}"
            logger.info(
                f"✅ Clash API 加载完成 (实例: {self.instance_name}) 端口={self.port} "
                f"selector={self.selector!r} (API 文件: {self.config_path}"
                f"{f', selector 文件: {sel_path}' if sel_path != self.config_path else ''})"
            )
        except Exception as e:
            logger.error(f"❌ 加载Clash API配置失败 (实例: {self.instance_name}): {e}")

    def _proxies_selector_path(self) -> str:
        """REST 路径中的 selector 段（含空格等需编码）。"""
        return quote(self.selector or "", safe="")

    def _get_headers(self):
        headers = {'Content-Type': 'application/json'}
        if self.secret:
            headers['Authorization'] = f'Bearer {self.secret}'
        return headers

    def _get_auth_headers(self):
        if not self.secret:
            return {}
        return {'Authorization': f'Bearer {self.secret}'}

    def get_current_config(self) -> Dict[str, Any]:
        if not self.port:
            return {"code": 1, "msg": f"Clash API配置未加载 (实例: {self.instance_name})"}
        try:
            url = f'{self.base_url}/configs'
            headers = self._get_auth_headers()
            response = _http_request("GET", url, headers=headers, timeout=10)
            if response.status_code == 200:
                return {
                    "code": 0,
                    "msg": "获取配置成功",
                    "data": response.json(),
                    "instance": self.instance_name,
                    "config_file": self.config_path
                }
            return {"code": 1, "msg": f"获取配置失败，状态码: {response.status_code} (实例: {self.instance_name})"}
        except Exception as e:
            return {"code": 1, "msg": f"获取配置失败 (实例: {self.instance_name}): {str(e)}"}

    def set_mode_to_rule(self) -> Dict[str, Any]:
        if not self.port:
            return {"code": 1, "msg": f"Clash API配置未加载 (实例: {self.instance_name})"}
        try:
            url = f'{self.base_url}/configs'
            headers = self._get_headers()
            data = {"mode": "Rule"}
            response = _http_request("PATCH", url, json=data, headers=headers, timeout=10)
            if response.status_code == 204:
                return {"code": 0, "msg": f"成功切换到Rule模式 (实例: {self.instance_name})"}
            return {"code": 1, "msg": f"切换模式失败，状态码: {response.status_code} (实例: {self.instance_name})"}
        except Exception as e:
            return {"code": 1, "msg": f"切换模式失败 (实例: {self.instance_name}): {str(e)}"}

    def ensure_rule_mode(self) -> Dict[str, Any]:
        try:
            cfg = self.get_current_config()
            if cfg["code"] != 0:
                return {"code": 1, "msg": f"无法获取Clash配置 (实例: {self.instance_name}): {cfg['msg']}", "action": "skip", "instance": self.instance_name}
            current_mode = (cfg["data"].get("mode", "") or "").lower()
            if current_mode == "rule":
                return {"code": 0, "msg": f"代理模式已经是Rule模式 (实例: {self.instance_name})", "action": "no_change", "current_mode": current_mode, "instance": self.instance_name}
            sw = self.set_mode_to_rule()
            if sw["code"] == 0:
                return {"code": 0, "msg": f"从 {current_mode} 模式切换到Rule模式成功 (实例: {self.instance_name})", "action": "switched", "previous_mode": current_mode, "current_mode": "rule", "instance": self.instance_name}
            return {"code": 1, "msg": f"切换到Rule模式失败 (实例: {self.instance_name}): {sw['msg']}", "action": "failed", "current_mode": current_mode, "instance": self.instance_name}
        except Exception as e:
            return {"code": 1, "msg": f"确保Rule模式时发生异常 (实例: {self.instance_name}): {str(e)}", "action": "error", "instance": self.instance_name}

    async def test_proxy_delay(self, proxy_name: str) -> Tuple[str, int]:
        url = f'{self.base_url}/proxies/{proxy_name}/delay'
        params = {'timeout': 1000, 'url': 'http://www.gstatic.com/generate_204'}
        headers = self._get_headers()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        return proxy_name, data.get('delay', -1)
                    return proxy_name, -1
        except Exception:
            return proxy_name, -1

    async def batch_test_delays(self, proxy_names: List[str]) -> Dict[str, int]:
        tasks = [self.test_proxy_delay(name) for name in proxy_names]
        results = await asyncio.gather(*tasks)
        return {name: delay for name, delay in results}

    def select_best_proxy(self, available_proxies: List[str], current_proxy: str, delays: Dict[str, int]) -> str:
        if not available_proxies:
            return None
        candidates = [p for p in available_proxies if p != current_proxy and delays.get(p, -1) > 0]
        if not candidates:
            return None
        return select_with_weight(
            available_proxies=candidates,
            current_proxy=current_proxy,
            delays=delays,
            usage_history_path=self.usage_history_file,
            weight_delay=self.weight_delay,
            cooldown_seconds=self.cooldown_seconds,
            selection_topk=self.selection_topk,
        )

    async def switch_proxy_async(self, verbose: bool = False) -> Dict[str, Any]:
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: self.switch_proxy_smart(verbose=verbose))
        except Exception as e:
            return {"code": 1, "msg": f"异步代理切换异常 (实例: {self.instance_name}): {str(e)}", "data": {}, "instance": self.instance_name}

    def _test_clash_api_connection(self) -> bool:
        headers = self._get_auth_headers()
        try:
            url = f'{self.base_url}/configs'
            response = _http_request("GET", url, headers=headers, timeout=5)
            if response.status_code == 200:
                try:
                    response.json()
                    return True
                except json.JSONDecodeError:
                    return False
            return False
        except Exception:
            return False

    def _get_all_proxies_info(self):
        headers = self._get_auth_headers()
        url = f'{self.base_url}/proxies'
        try:
            response = _http_request("GET", url, headers=headers, timeout=10)
            if response.status_code != 200:
                return None
            return response.json()
        except Exception:
            return None

    def _get_selector_info(self):
        headers = self._get_auth_headers()
        url = f'{self.base_url}/proxies/{self._proxies_selector_path()}'
        try:
            response = _http_request("GET", url, headers=headers, timeout=10)
            if response.status_code != 200:
                return None
            return response.json()
        except Exception:
            return None

    def switch_proxy_smart(self, verbose: bool = True):
        try:
            if verbose:
                logger.info(f"🔄 开始智能代理切换 (实例: {self.instance_name})...")
            
            if not self.port or not self.selector:
                logger.error(f"❌ 配置文件加载失败 (实例: {self.instance_name})")
                return {"code": 1, "msg": f"配置文件加载失败 (实例: {self.instance_name})", "data": {}, "instance": self.instance_name}
            
            if not self._test_clash_api_connection():
                logger.error(f"❌ Clash API连接失败 (实例: {self.instance_name})")
                return {"code": 1, "msg": f"Clash API连接失败 (实例: {self.instance_name})", "data": {}, "instance": self.instance_name}
            
            selector_info = self._get_selector_info()
            if not selector_info:
                logger.error(f"❌ 获取选择器信息失败 (实例: {self.instance_name})")
                return {"code": 1, "msg": f"获取选择器信息失败 (实例: {self.instance_name})", "data": {}, "instance": self.instance_name}
            
            current_proxy = selector_info.get('now', '')
            all_proxies = selector_info.get('all', [])
            available = [p for p in all_proxies if p not in ['DIRECT', 'REJECT', '自动选择', '故障转移']]
            
            if verbose:
                logger.info(f"📊 当前代理: {current_proxy} (实例: {self.instance_name})")
                logger.info(f"📋 可用代理数: {len(available)} 个 (实例: {self.instance_name})")
            
            if len(available) <= 1:
                logger.warning(f"⚠️ 没有足够的可切换代理 (实例: {self.instance_name})")
                return {"code": 1, "msg": f"没有足够的可切换代理 (实例: {self.instance_name})", "data": {"current_proxy": current_proxy, "instance": self.instance_name}}
            
            if verbose:
                logger.info(f"🔍 开始测试 {len(available)} 个代理的延迟... (实例: {self.instance_name})")
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            delays = loop.run_until_complete(self.batch_test_delays(available))
            loop.close()
            
            # 输出延迟测试结果
            if verbose:
                valid_delays = {k: v for k, v in delays.items() if v > 0}
                if valid_delays:
                    sorted_delays = sorted(valid_delays.items(), key=lambda x: x[1])
                    logger.info(f"📈 延迟测试完成，前3名: (实例: {self.instance_name})")
                    for proxy, delay in sorted_delays[:3]:
                        logger.info(f"   - {proxy}: {delay}ms")
            
            selected = self.select_best_proxy(available, current_proxy, delays)
            if not selected:
                logger.warning(f"⚠️ 无法选择合适的代理 (实例: {self.instance_name})")
                return {"code": 1, "msg": f"无法选择合适的代理 (实例: {self.instance_name})", "data": {"current_proxy": current_proxy, "instance": self.instance_name}}
            
            if verbose:
                selected_delay = delays.get(selected, -1)
                logger.info(f"🎯 选中代理: {selected} (延迟: {selected_delay}ms) (实例: {self.instance_name})")
            
            url = f'{self.base_url}/proxies/{self._proxies_selector_path()}'
            headers = self._get_headers()
            data = {"name": selected}
            resp = _http_request("PUT", url, json=data, headers=headers, timeout=10)
            
            if resp.status_code == 204:
                try:
                    record_usage(self.usage_history_file, selected)
                except Exception:
                    pass
                if verbose:
                    logger.info(f"✅ 代理切换成功: {current_proxy} → {selected} (实例: {self.instance_name})")
                return {"code": 0, "msg": f"代理切换成功 (实例: {self.instance_name})", "data": {"previous_proxy": current_proxy, "current_proxy": selected, "instance": self.instance_name}}
            
            logger.error(f"❌ 切换代理失败，状态码: {resp.status_code} (实例: {self.instance_name})")
            return {"code": 1, "msg": f"切换代理失败，状态码: {resp.status_code} (实例: {self.instance_name})", "data": {"current_proxy": current_proxy, "response_text": resp.text, "instance": self.instance_name}}
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ 网络请求失败 (实例: {self.instance_name}): {str(e)}")
            return {"code": 1, "msg": f"网络请求失败 (实例: {self.instance_name}): {str(e)}", "data": {}, "instance": self.instance_name}
        except Exception as e:
            logger.error(f"❌ 未知错误 (实例: {self.instance_name}): {str(e)}")
            return {"code": 1, "msg": f"未知错误 (实例: {self.instance_name}): {str(e)}", "data": {}, "instance": self.instance_name}


def build_clash_proxy_manager(
    instance_name: str = "lightread_clash_7899",
) -> ClashProxyManager:
    """返回固定指向 7899/19099 的 ClashProxyManager。"""
    return ClashProxyManager(
        config_path="<hardcoded-lightread-7899>",
        instance_name=instance_name,
        fixed_port=LIGHTREAD_CLASH_7899_API_PORT,
        fixed_secret=LIGHTREAD_CLASH_7899_API_SECRET,
        fixed_selector=LIGHTREAD_CLASH_7899_SELECTOR,
    )

class ISPProxyManager:
    """ISP代理管理器 - 支持多实例独立管理"""

    def __init__(self, config_path: str = "ISP_cfg.yaml", instance_name: str = None):
        self.port = None
        self.secret = None
        self.selector = None
        self.config_path = config_path
        # 实例名称，用于区分不同的代理管理器
        if instance_name is None:
            # 根据配置文件名自动生成实例名
            instance_name = os.path.splitext(os.path.basename(config_path))[0]
        self.instance_name = instance_name
        # 使用历史：记录每个节点上次使用时间与次数，用于抗重复
        self.usage_history_file = _usage_history_json_path(self.instance_name)
        # 单权重：延迟权重，recency 权重为 (1 - weight_delay)
        try:
            self.weight_delay = float(os.getenv("PROXY_WEIGHT_DELAY", "0.7"))
        except Exception:
            self.weight_delay = 0.7
        # 冷却窗口：在该窗口时间内刚使用过的节点会被惩罚
        try:
            self.cooldown_seconds = int(os.getenv("PROXY_RECENCY_COOLDOWN_SEC", "300"))
        except Exception:
            self.cooldown_seconds = 300
        # 在得分排序后，支持从前K名中随机挑选，增加分散性
        try:
            self.selection_topk = max(1, int(os.getenv("PROXY_SELECTION_TOPK", "1")))
        except Exception:
            self.selection_topk = 1
        
        # 定义排除的代理列表 - 仅用于conf.ini配置
        self.use_proxy_filter = os.path.basename(config_path) == "conf.ini"
        self.excluded_proxies = {
            'special': ['DIRECT', 'REJECT', '账号邮箱看最新的地址', 'NETV2', '自动选择', '故障转移'],
            'keywords': ['香港', 'HK', 'Hong Kong', 'HongKong']  # 添加香港相关的关键词
        } if self.use_proxy_filter else {
            'special': ['DIRECT', 'REJECT', '账号邮箱看最新的地址', 'NETV2', '自动选择', '故障转移'],
            'keywords': []  # 不使用关键词过滤
        }
        
        self._load_config()

        logger.info(f"🎯 创建ISP代理管理器实例: {self.instance_name}")
        logger.info(f"📁 配置文件: {self.config_path}")
        logger.info(f"📄 使用历史文件: {self.usage_history_file}")

        if self.use_proxy_filter:
            logger.info(f"⚙️ 已启用代理过滤规则（针对conf.ini）")
    
    def _load_config(self):
        """加载ISP代理配置"""
        try:
            if not os.path.exists(self.config_path):
                logger.warning(f"配置文件 {self.config_path} 不存在 (实例: {self.instance_name})")
                return

            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            # 加载代理配置
            proxy_config = config.get('proxy', {})
            self.server = proxy_config.get('server')
            self.port = proxy_config.get('port')
            self.username = proxy_config.get('username')
            self.password = proxy_config.get('password')

            # 加载代理池配置
            pool_config = config.get('pool', {})
            self.switch_interval = pool_config.get('switch_interval', 60)
            self.test_timeout = pool_config.get('test_timeout', 10)
            self.max_retries = pool_config.get('max_retries', 3)
            self.auto_rotate = pool_config.get('auto_rotate', True)

            # 加载代理列表
            self.proxies = config.get('proxies', [])

            logger.info(f"✅ ISP代理配置加载完成 (实例: {self.instance_name})")
            logger.info(f"📡 代理服务器: {self.server}:{self.port}")
            logger.info(f"👤 用户名: {self.username}")

        except Exception as e:
            logger.error(f"❌ 加载ISP代理配置失败 (实例: {self.instance_name}): {e}")

    def _get_current_proxy(self) -> Dict[str, Any]:
        """获取当前代理配置 - Playwright 直接使用 ISP 代理"""
        if not self.server or not self.port or not self.username or not self.password:
            logger.error("ISP代理配置不完整")
            return {}

        proxy_url = f"http://{self.username}:{self.password}@{self.server}:{self.port}"
        
        # Playwright 代理配置：直接使用 ISP 代理，而不是本地 Clash
        # 允许通过环境变量 PLAYWRIGHT_PROXY_SERVER 覆盖（如果需要特殊配置）
        override_pw_server = os.getenv("PLAYWRIGHT_PROXY_SERVER", "").strip()
        if override_pw_server:
            # 如果设置了环境变量，使用环境变量指定的代理
            if not (override_pw_server.startswith("http://") or override_pw_server.startswith("https://")):
                override_pw_server = f"http://{override_pw_server}"
            pw_server = override_pw_server
            pw_username = None
            pw_password = None
        else:
            # 默认：直接使用 ISP 代理
            pw_server = f"http://{self.server}:{self.port}"
            pw_username = self.username
            pw_password = self.password

        proxies: Dict[str, Any] = {
            "http": proxy_url,
            "https": proxy_url,
            "playwright": {
                "server": pw_server,
                **({"username": pw_username} if pw_username else {}),
                **({"password": pw_password} if pw_password else {}),
                "bypass": "127.0.0.1,localhost,::1"
            }
        }

        try:
            logger.info("使用ISP代理 (requests): %s@%s:%s", self.username, self.server, self.port)
            logger.info("使用ISP代理 (playwright): %s (用户: %s)", pw_server, pw_username or "无")
        except Exception:
            pass
        return proxies

    def _get_headers(self):
        """获取请求头（用于非GET，如PUT/PATCH）"""
        return {
            'Authorization': f'Bearer {self.secret}',
            'Content-Type': 'application/json'
        }
    
    def _get_auth_headers(self):
        """仅认证头（用于GET请求）"""
        return {
            'Authorization': f'Bearer {self.secret}'
        }
    
    def get_current_config(self) -> Dict[str, Any]:
        """获取当前ISP代理配置"""
        try:
            config = {
                "server": self.server,
                "port": self.port,
                "username": self.username,
                "password": "***",  # 隐藏密码
                "switch_interval": self.switch_interval,
                "test_timeout": self.test_timeout,
                "max_retries": self.max_retries,
                "auto_rotate": self.auto_rotate,
                "proxies_count": len(self.proxies),
                "active_proxies": [p['name'] for p in self.proxies if p.get('active', False)]
            }

            return {
                "code": 0,
                "msg": "获取ISP代理配置成功",
                "data": config,
                "instance": self.instance_name,
                "config_file": self.config_path
            }
        except Exception as e:
            return {
                "code": 1,
                "msg": f"获取ISP代理配置失败 (实例: {self.instance_name}): {str(e)}"
            }
    
    def test_proxy_connection(self) -> Dict[str, Any]:
        """测试ISP代理连接"""
        if not self.server or not self.port or not self.username or not self.password:
            return {"code": 1, "msg": f"ISP代理配置未加载 (实例: {self.instance_name})"}

        try:
            import requests
            session = requests.Session()
            session.trust_env = False
            session.proxies = {}

            proxy_url = f"http://{self.username}:{self.password}@{self.server}:{self.port}"
            proxies = {"http": proxy_url, "https": proxy_url}

            # 测试代理连接
            test_url = "http://myip.lunaproxy.io"
            response = session.get(test_url, proxies=proxies, timeout=10)

            if response.status_code == 200:
                logger.info(f"✅ ISP代理连接测试成功 (实例: {self.instance_name})")
                return {
                    "code": 0,
                    "msg": f"ISP代理连接测试成功 (实例: {self.instance_name})"
                }
            else:
                logger.error(f"❌ ISP代理连接测试失败 (实例: {self.instance_name}): 状态码 {response.status_code}")
                return {
                    "code": 1,
                    "msg": f"ISP代理连接测试失败，状态码: {response.status_code} (实例: {self.instance_name})"
                }

        except Exception as e:
            logger.error(f"❌ ISP代理连接测试异常 (实例: {self.instance_name}): {e}")
            return {
                "code": 1,
                "msg": f"ISP代理连接测试异常 (实例: {self.instance_name}): {str(e)}"
            }
    
    def ensure_proxy_available(self) -> Dict[str, Any]:
        """确保ISP代理可用"""
        try:
            # 测试代理连接
            test_result = self.test_proxy_connection()

            if test_result["code"] == 0:
                logger.info(f"✅ ISP代理可用 (实例: {self.instance_name})")
                return {
                    "code": 0,
                    "msg": f"ISP代理可用 (实例: {self.instance_name})",
                    "action": "available",
                    "instance": self.instance_name
                }
            else:
                logger.error(f"❌ ISP代理不可用 (实例: {self.instance_name}): {test_result['msg']}")
                return {
                    "code": 1,
                    "msg": f"ISP代理不可用 (实例: {self.instance_name}): {test_result['msg']}",
                    "action": "unavailable",
                    "instance": self.instance_name
                }

        except Exception as e:
            error_msg = f"检查ISP代理可用性时发生异常 (实例: {self.instance_name}): {str(e)}"
            logger.error(f"❌ {error_msg}")
            return {
                "code": 1,
                "msg": error_msg,
                "action": "error",
                "instance": self.instance_name
            }
    
    

    async def test_proxy_delay(self, proxy_name: str = "isp_proxy") -> Tuple[str, int]:
        """测试ISP代理连接速度

        Args:
            proxy_name: 代理名称

        Returns:
            Tuple[str, int]: (代理名称, 延迟ms)，如果测试失败返回延迟为-1
        """
        if not self.server or not self.port or not self.username or not self.password:
            logger.error(f"ISP代理配置不完整 (实例: {self.instance_name})")
            return proxy_name, -1

        try:
            import requests
            session = requests.Session()
            session.trust_env = False
            session.proxies = {}

            proxy_url = f"http://{self.username}:{self.password}@{self.server}:{self.port}"
            proxies = {"http": proxy_url, "https": proxy_url}

            # 测试延迟
            test_url = "http://www.gstatic.com/generate_204"
            start_time = time.time()

            response = session.get(test_url, proxies=proxies, timeout=10)
            delay = int((time.time() - start_time) * 1000)

            if response.status_code == 204:
                logger.info(f"ISP代理 {proxy_name} 延迟: {delay}ms (实例: {self.instance_name})")
                return proxy_name, delay
            else:
                logger.warning(f"ISP代理 {proxy_name} 延迟测试失败，状态码: {response.status_code} (实例: {self.instance_name})")
                return proxy_name, -1

        except Exception as e:
            logger.error(f"ISP代理 {proxy_name} 延迟测试异常: {e} (实例: {self.instance_name})")
            return proxy_name, -1

    async def batch_test_delays(self, proxy_names: List[str]) -> Dict[str, int]:
        """测试ISP代理的延迟

        Args:
            proxy_names: 代理节点名称列表

        Returns:
            Dict[str, int]: {代理名称: 延迟ms}
        """
        # 对于ISP代理，只测试一个代理
        if proxy_names:
            _, delay = await self.test_proxy_delay(proxy_names[0])
            return {proxy_names[0]: delay}
        else:
            return {}

    # ========= 使用历史（抗重复）由 assist 模块提供 =========

    def select_best_proxy(self, available_proxies: List[str], current_proxy: str, delays: Dict[str, int]) -> str:
        """选择ISP代理（只有一个代理）"""
        if not available_proxies:
            return None
        # 对于ISP代理，只有一个代理，直接返回
        return available_proxies[0] if available_proxies else None

    async def switch_proxy_async(self, verbose: bool = False) -> Dict[str, Any]:
        """异步测试ISP代理连接"""
        try:
            if verbose:
                logger.info(f"🔄 开始异步ISP代理测试 (实例: {self.instance_name})...")

            # 测试ISP代理连接
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: self.test_proxy_connection())

            if verbose:
                if result["code"] == 0:
                    logger.info(f"✅ 异步ISP代理测试成功 (实例: {self.instance_name}): {result.get('msg', '')}")
                else:
                    logger.warning(f"⚠️ 异步ISP代理测试失败 (实例: {self.instance_name}): {result.get('msg', '')}")

            return result

        except Exception as e:
            error_msg = f"异步ISP代理测试异常 (实例: {self.instance_name}): {str(e)}"
            logger.error(f"❌ {error_msg}")
            return {
                "code": 1,
                "msg": error_msg,
                "data": {},
                "instance": self.instance_name
            }

    def switch_proxy_smart(self, verbose=True):
        """
        测试ISP代理连接 - 简化版本

        Args:
            verbose (bool): 是否输出详细日志

        Returns:
            dict: 返回结果，格式为 {code: 0/1, msg: str, data: dict, instance: str}
                  code 0表示成功，1表示失败
        """
        try:
            if verbose:
                logger.info(f"=== ISP代理连接测试开始 (实例: {self.instance_name}) ===")

            # 检查配置
            if not self.server or not self.port or not self.username or not self.password:
                return {
                    "code": 1,
                    "msg": f"ISP代理配置加载失败 (实例: {self.instance_name})",
                    "data": {},
                    "instance": self.instance_name
                }

            if verbose:
                logger.info(f"ISP代理配置 (实例: {self.instance_name}) - 服务器: {self.server}:{self.port}")

            # 测试ISP代理连接
            test_result = self.test_proxy_connection()

            if test_result["code"] == 0:
                if verbose:
                    logger.info(f"✓ ISP代理连接测试成功 (实例: {self.instance_name})！")
                return {
                    "code": 0,
                    "msg": f"ISP代理连接测试成功 (实例: {self.instance_name})",
                    "data": {
                        "proxy": f"{self.username}@{self.server}:{self.port}",
                        "instance": self.instance_name
                    }
                }
            else:
                logger.error(f"❌ ISP代理连接测试失败 (实例: {self.instance_name}): {test_result['msg']}")
                return {
                    "code": 1,
                    "msg": f"ISP代理连接测试失败 (实例: {self.instance_name}): {test_result['msg']}",
                    "data": {},
                    "instance": self.instance_name
                }

        except Exception as e:
            return {
                "code": 1,
                "msg": f"ISP代理测试异常 (实例: {self.instance_name}): {str(e)}",
                "data": {},
                "instance": self.instance_name
            }

    def _test_clash_api_connection(self):
        """测试Clash API连接"""
        headers = self._get_auth_headers()

        # 重试机制 - 最多重试3次
        max_retries = 1
        for attempt in range(max_retries):
            try:
                # 测试基本连接
                test_url = f'{self.base_url}/configs'

                # 打印API请求信息 (不受verbose控制，总是显示)
                logger.info(f"🔗 测试API连接 (实例: {self.instance_name}) - 第{attempt + 1}次尝试:")
                logger.info(f"   URL: {test_url}")
                logger.info(f"   Method: GET")
                logger.info(f"   Headers: {headers}")
                try:
                    import requests as _r
                    logger.info(f"   requests 版本: {_r.__version__}")
                except Exception:
                    pass

                response = _http_request(
                    "GET",
                    test_url,
                    headers=headers,
                    timeout=5,
                )
            
                if response.status_code == 401:
                    logger.error(f"错误 (实例: {self.instance_name}): API认证失败，请检查secret配置")
                    logger.error(f"响应内容: {response.text}")
                    return False
                elif response.status_code == 200:
                    # 成功响应，尝试解析JSON
                    try:
                        response.json()
                        logger.info(f"✅ API连接测试成功 (实例: {self.instance_name})")
                        return True
                    except json.JSONDecodeError:
                        logger.error(f"警告 (实例: {self.instance_name}): API响应不是有效的JSON格式")
                        return False
                else:
                    logger.error(f"错误 (实例: {self.instance_name}): API连接失败，状态码: {response.status_code}")
                    logger.error(f"响应内容: {response.text}")
                    logger.error(f"响应头: {dict(response.headers)}")

                    # 如果不是最后一次重试，继续重试
                    if attempt < max_retries - 1:
                        logger.info(f"等待1秒后进行第{attempt + 2}次重试...")
                        time.sleep(1)
                        continue
                    else:
                        logger.error(f"❌ 所有{max_retries}次重试都失败了")
                        return False

            except requests.exceptions.ConnectionError:
                logger.error(f"错误 (实例: {self.instance_name}): 无法连接到Clash API，请确保Clash服务正在运行")
                if attempt < max_retries - 1:
                    logger.info(f"等待1秒后进行第{attempt + 2}次重试...")
                    time.sleep(1)
                    continue
                return False
            except requests.exceptions.Timeout:
                logger.error(f"错误 (实例: {self.instance_name}): API请求超时")
                if attempt < max_retries - 1:
                    logger.info(f"等待1秒后进行第{attempt + 2}次重试...")
                    time.sleep(1)
                    continue
                return False
            except Exception as e:
                logger.error(f"错误 (实例: {self.instance_name}): API连接测试失败: {e}")
                if attempt < max_retries - 1:
                    logger.info(f"等待1秒后进行第{attempt + 2}次重试...")
                    time.sleep(1)
                    continue
                return False

        # 所有重试都失败
        logger.error(f"❌ API连接测试在{max_retries}次重试后仍然失败")
        return False

    def _get_all_proxies_info(self):
        """获取所有代理信息"""
        headers = self._get_auth_headers()

        url = f'{self.base_url}/proxies'

        # 打印API请求信息 (不受verbose控制，总是显示)
        logger.info(f"🔗 获取代理信息API请求 (实例: {self.instance_name}):")
        logger.info(f"   URL: {url}")
        logger.info(f"   Method: GET")
        logger.info(f"   Headers: {headers}")

        try:
            response = _http_request(
                "GET",
                url,
                headers=headers,
                timeout=10,
            )

            if response.status_code != 200:
                logger.error(f"错误 (实例: {self.instance_name}): 获取代理信息失败，状态码: {response.status_code}")
                logger.error(f"响应内容: {response.text}")
                logger.error(f"响应头: {dict(response.headers)}")
                return None
            
            try:
                return response.json()
            except json.JSONDecodeError as e:
                logger.error(f"错误 (实例: {self.instance_name}): 代理信息响应不是有效的JSON: {e}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"错误 (实例: {self.instance_name}): 请求代理信息失败: {e}")
            return None

    def _get_selector_info(self):
        """获取指定选择器的信息"""
        headers = self._get_auth_headers()

        url = f'{self.base_url}/proxies/{quote(self.selector or "", safe="")}'

        # 打印API请求信息 (不受verbose控制，总是显示)
        logger.info(f"🔗 获取选择器信息API请求 (实例: {self.instance_name}):")
        logger.info(f"   URL: {url}")
        logger.info(f"   Method: GET")
        logger.info(f"   Headers: {headers}")

        try:
            response = _http_request(
                "GET",
                url,
                headers=headers,
                timeout=10,
            )

            if response.status_code == 404:
                logger.error(f"错误 (实例: {self.instance_name}): 选择器 '{self.selector}' 不存在")
                return None
            elif response.status_code != 200:
                logger.error(f"错误 (实例: {self.instance_name}): 获取选择器信息失败，状态码: {response.status_code}")
                logger.error(f"响应内容: {response.text}")
                logger.error(f"响应头: {dict(response.headers)}")
                return None
            
            try:
                return response.json()
            except json.JSONDecodeError as e:
                logger.error(f"错误 (实例: {self.instance_name}): 选择器信息响应不是有效的JSON: {e}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"错误 (实例: {self.instance_name}): 请求选择器信息失败: {e}")
            return None

# ===== 精简的兼容函数（仅保留主函数入口） =====

def switch_proxy_smart(verbose=True):
    """
    智能切换代理到下一个可用节点 - 主入口函数。
    port / secret / selector 全部来自 conf.ini（路径可通过 GOOGLE_SEARCH_CLASH_CFG 覆盖）。
    """
    conf_path = os.getenv("GOOGLE_SEARCH_CLASH_CFG", "conf.ini")
    proxy_manager = ClashProxyManager(conf_path)
    return proxy_manager.switch_proxy_smart(verbose)

async def async_main():
    """异步主函数"""
    conf_path = os.getenv("GOOGLE_SEARCH_CLASH_CFG", "conf.ini")
    proxy_manager = ClashProxyManager(conf_path)
    result = await proxy_manager.switch_proxy_smart(verbose=True)
    
    if result["code"] == 0:
        print(f"\n✓ {result['msg']}")
    else:
        print(f"\n✗ {result['msg']}")
        sys.exit(1)

def main():
    """主函数 - 创建事件循环并运行异步主函数"""
    loop = asyncio.get_event_loop()
    loop.run_until_complete(async_main())

if __name__ == '__main__':
    main()



# ======================
# 代理池固定间隔轮询续约
# ======================

# 环境变量配置
PROXY_AUTO_RENEW_ENABLED = os.getenv("PROXY_AUTO_RENEW_ENABLED", "true").lower() == "true"
# 单一续约间隔：优先 PROXY_AUTO_RENEW_INTERVAL，其次兼容旧名 PROXY_AUTO_RENEW_INTERVAL_GLOBAL
_auto_interval_raw = os.getenv("PROXY_AUTO_RENEW_INTERVAL") or os.getenv("PROXY_AUTO_RENEW_INTERVAL_GLOBAL")
PROXY_AUTO_RENEW_INTERVAL = int(_auto_interval_raw or "300")
# 兼容旧常量名（与 PROXY_AUTO_RENEW_INTERVAL 相同；不再单独维护 rule 间隔）
PROXY_AUTO_RENEW_INTERVAL_GLOBAL = PROXY_AUTO_RENEW_INTERVAL
PROXY_AUTO_RENEW_JITTER_SEC = int(os.getenv("PROXY_AUTO_RENEW_JITTER_SEC", "20"))  # 抖动，避免齐步


class ProxyAutoRefresher:
    """后台轮询续约器：固定间隔执行测速与智能切换。

    - 使用 ClashProxyManager.switch_proxy_smart()，内部已包含延迟测试与选择逻辑
    - 守护线程实现，独立于事件循环
    - 可随时 start/stop
    """

    def __init__(self, manager: ClashProxyManager, interval_seconds: int, name: str):
        self.manager = manager
        self.interval_seconds = max(10, int(interval_seconds))
        self.name = name
        self._thread: threading.Thread = None
        self._stop_event = threading.Event()

    def _loop(self):
        logger.info(f"🛠️ 代理轮询续约线程启动: {self.name} (间隔: {self.interval_seconds}s)")
        # 首次运行先等待一个小抖动，避免服务同时启动拥挤
        initial_sleep = random.uniform(0, min(self.interval_seconds * 0.2, 5))
        if initial_sleep > 0:
            time.sleep(initial_sleep)

        while not self._stop_event.is_set():
            try:
                # 执行一次智能切换（含测速/筛选/切换）；verbose 关以减少日志噪声
                result = self.manager.switch_proxy_smart(verbose=False)
                code = result.get("code", 1) if isinstance(result, dict) else 1
                if code == 0:
                    data = result.get("data", {}) if isinstance(result, dict) else {}
                    prev_p = data.get("previous_proxy")
                    curr_p = data.get("current_proxy")
                    logger.info(
                        f"✅ [{self.name}] 续约完成: {prev_p} -> {curr_p} (实例: {self.manager.instance_name})"
                    )
                else:
                    logger.warning(f"⚠️ [{self.name}] 续约失败: {result}")
            except Exception as e:
                logger.error(f"❌ [{self.name}] 续约异常: {e}")

            # 休眠（加入抖动，避免整点齐步）
            if self._stop_event.is_set():
                break
            sleep_seconds = self.interval_seconds + random.uniform(0, max(PROXY_AUTO_RENEW_JITTER_SEC, 0))
            for _ in range(int(sleep_seconds)):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

        logger.info(f"🛑 代理轮询续约线程退出: {self.name}")

    def start(self):
        if self._thread and self._thread.is_alive():
            logger.info(f"[{self.name}] 续约线程已在运行，跳过启动")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name=f"proxy-renew-{self.name}", daemon=True)
        self._thread.start()

    def stop(self):
        if not self._thread:
            return
        self._stop_event.set()
        try:
            self._thread.join(timeout=5)
        except Exception:
            pass
        finally:
            self._thread = None


# 全局单例管理
_auto_started = False
_start_lock = threading.Lock()
_renew_refresher: ProxyAutoRefresher = None


def start_auto_renew(
    interval_seconds: int = None,
    global_interval: int = None,
    rule_interval: int = None,
) -> Dict[str, Any]:
    """启动一条代理轮询续约线程。

    port / secret / selector 全部来自 conf.ini（路径可通过 GOOGLE_SEARCH_CLASH_CFG 覆盖）。
    ``global_interval`` / ``rule_interval`` 仅为兼容旧调用：均映射到同一 ``interval_seconds``。
    """
    global _auto_started, _renew_refresher

    with _start_lock:
        if (
            _renew_refresher
            and _renew_refresher._thread
            and _renew_refresher._thread.is_alive()
        ):
            logger.info("代理自动续约已在运行，跳过重复启动")
            return {
                "code": 0,
                "msg": "auto renew already running",
                "intervals": {
                    "interval_seconds": _renew_refresher.interval_seconds,
                    "jitter": PROXY_AUTO_RENEW_JITTER_SEC,
                },
            }

        if _renew_refresher:
            _renew_refresher.stop()
            _renew_refresher = None
        _auto_started = False

        if interval_seconds is not None:
            sec = int(interval_seconds)
        elif global_interval is not None:
            sec = int(global_interval)
        elif rule_interval is not None:
            sec = int(rule_interval)
        else:
            sec = PROXY_AUTO_RENEW_INTERVAL

        conf_path = os.getenv("GOOGLE_SEARCH_CLASH_CFG", "conf.ini")
        manager = ClashProxyManager(conf_path, instance_name="proxy_auto_renew")
        _renew_refresher = ProxyAutoRefresher(manager, sec, name="renew")
        _renew_refresher.start()
        _auto_started = True

        logger.info(
            f"🔁 代理自动续约已启动: interval={sec}s (conf={conf_path}), "
            f"jitter={PROXY_AUTO_RENEW_JITTER_SEC}s"
        )

        return {
            "code": 0,
            "msg": "auto renew started",
            "intervals": {
                "interval_seconds": sec,
                "jitter": PROXY_AUTO_RENEW_JITTER_SEC,
            },
        }


def stop_auto_renew() -> Dict[str, Any]:
    """停止代理轮询续约线程。"""
    global _auto_started, _renew_refresher

    with _start_lock:
        if _renew_refresher:
            _renew_refresher.stop()
            _renew_refresher = None
        _auto_started = False
        logger.info("🛑 已停止代理自动续约")
        return {"code": 0, "msg": "auto renew stopped"}


def _maybe_start_auto_renew_on_import():
    """模块导入时，按开关自动启动续约（守护线程）。"""
    if os.getenv("DISABLE_PROXY_AUTO_RENEW", "false").lower() == "true":
        # 兼容旧开关
        return
    if not PROXY_AUTO_RENEW_ENABLED:
        return
    try:
        start_auto_renew()
    except Exception as e:
        logger.warning(f"自动启动代理续约失败: {e}")


# 仅在作为模块导入时自动启动（避免 __main__ 调用时干扰）
if __name__ != '__main__':
    _maybe_start_auto_renew_on_import()
