#!/usr/bin/env python3
"""
双跳代理服务器（双监听）

唯一维护路径：本仓库 ``unified_backend/proxy/double_hop_proxy.py``。
请勿在其它目录保留副本。请在启动 ``start_unified.py`` 之前**另开终端**手动运行本脚本
（避免与已有监听进程争用 22002/22003）。启动命令：``python proxy/double_hop_proxy.py``。

路径（每条线路相同）：
  本机端口 → Clash 7897 (mixed-port，作为 711 双跳前置入口) → 711Proxy:10000 → 目标网站

两条线路区别仅在 711 认证用户名（轮换池不同）：
  • 127.0.0.1:22002 — 711 zone-custom + region-HK（香港轮换，延迟通常更低）
  • 127.0.0.1:22003 — 711 zone-custom 无 region（全球混播，出口更“干净”，适合易风控页面）

业务侧约定（unified_backend）：
  • 22002 → Jina、EasyScholar、Daily Papers(HuggingFace / Semantic Scholar)
  • 22003 → Google 网页搜索、Google Scholar
  • 7899 → PubMed（NCBI eutils）、CrossRef、ArXiv、Playwright、OpenAlex、token_router、easy_crawler
  • 直连 → bioRxiv / medRxiv API

说明：
  • PubMed 必须走 7899；711 对 *.ncbi.nlm.nih.gov 整域屏蔽，不可用。
  • Playwright / OpenAlex 当前真实实现走 7899，不走 22002/22003。

启动：
  python double_hop_proxy.py

测试：
  curl -sS -x http://127.0.0.1:22002 https://ipinfo.io/json
  curl -sS -x http://127.0.0.1:22003 https://ipinfo.io/json
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
from dataclasses import dataclass
from typing import Dict, Tuple

# ─────────────────────── 配置区（按需修改） ───────────────────────
LOCAL_HOST = "127.0.0.1"

# 第一跳：Clash HTTP 代理（mixed-port，默认与仓库内 PubMed / Playwright / easy_crawler 一致）
CLASH_HOST = "127.0.0.1"
CLASH_PORT = 7897

# 第二跳：711 入口（经 Clash CONNECT；认证在发往 711 的请求里携带）
UPSTREAM_HOST = "global.rotgb.711proxy.com"
UPSTREAM_PORT = 10000

CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 15.0


@dataclass(frozen=True)
class DoubleHopProfile:
    """一条本地监听 + 对应 711 账号（决定地区/池）。"""

    title: str
    local_port: int
    upstream_user: str
    upstream_pass: str


# 与业务代码约定保持一致；改端口或用户名时请同步各 router 常量
PROFILES: Tuple[DoubleHopProfile, ...] = (
    DoubleHopProfile(
        title="711 HK 轮换",
        local_port=22002,
        upstream_user=os.environ.get("DOUBLE_HOP_USER_HK", "your-711-user-zone-custom-region-HK"),
        upstream_pass=os.environ.get("DOUBLE_HOP_PASS", "your-711-password"),
    ),
    DoubleHopProfile(
        title="711 全球混播（无 region）",
        local_port=22003,
        upstream_user=os.environ.get("DOUBLE_HOP_USER_GLOBAL", "your-711-user-zone-custom"),
        upstream_pass=os.environ.get("DOUBLE_HOP_PASS", "your-711-password"),
    ),
)
# ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("double-hop")

_HOP_BY_HOP = {
    "proxy-connection",
    "connection",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
}


async def _read_until_header_end(
    reader: asyncio.StreamReader,
    timeout: float = READ_TIMEOUT,
    label: str = "",
) -> Tuple[bytes, bytes]:
    buf = bytearray()
    sep = b"\r\n\r\n"
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug(
                f"[{label}] 读取超时 ({timeout}s)，已收到 {len(buf)} 字节: {bytes(buf)[:300]!r}"
            )
            raise asyncio.TimeoutError(f"[{label}] 等待 HTTP 响应头超时 ({timeout}s)")
        if not chunk:
            logger.debug(
                f"[{label}] 连接关闭，共收到 {len(buf)} 字节: {bytes(buf)[:300]!r}"
            )
            raise ConnectionError(
                f"[{label}] 连接关闭，未读到完整 HTTP 头 (已收到 {len(buf)} 字节)"
            )
        buf.extend(chunk)
        idx = buf.find(sep)
        if idx != -1:
            return bytes(buf[: idx + 4]), bytes(buf[idx + 4 :])


def _parse_request_line(head: bytes) -> Tuple[str, str, str, Dict[str, str]]:
    text = head.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    parts = lines[0].split()
    method = parts[0] if len(parts) > 0 else "GET"
    path = parts[1] if len(parts) > 1 else "/"
    version = parts[2] if len(parts) > 2 else "HTTP/1.1"
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return method, path, version, headers


async def _pipe(
    src: asyncio.StreamReader,
    dst: asyncio.StreamWriter,
    label: str,
) -> None:
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except Exception as e:
        logger.debug(f"pipe [{label}] closed: {e}")
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def _relay(
    c_reader: asyncio.StreamReader,
    c_writer: asyncio.StreamWriter,
    u_reader: asyncio.StreamReader,
    u_writer: asyncio.StreamWriter,
) -> None:
    t1 = asyncio.create_task(_pipe(c_reader, u_writer, "client→711"))
    t2 = asyncio.create_task(_pipe(u_reader, c_writer, "711→client"))
    done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()


async def _open_clash_to_711() -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(CLASH_HOST, CLASH_PORT),
        timeout=CONNECT_TIMEOUT,
    )

    connect_req = (
        f"CONNECT {UPSTREAM_HOST}:{UPSTREAM_PORT} HTTP/1.1\r\n"
        f"Host: {UPSTREAM_HOST}:{UPSTREAM_PORT}\r\n"
        f"Proxy-Connection: Keep-Alive\r\n"
        f"\r\n"
    ).encode()
    logger.debug(f"→ Clash({CLASH_HOST}:{CLASH_PORT}) 发送: {connect_req!r}")
    writer.write(connect_req)
    await writer.drain()

    resp_head, _ = await _read_until_header_end(reader, label="Clash→711")
    logger.debug(f"← Clash 回应: {resp_head!r}")

    first_line = resp_head.split(b"\r\n", 1)[0]
    if b"200" not in first_line:
        writer.close()
        raise ConnectionError(
            f"Clash 拒绝 CONNECT 到 711：{first_line.decode('latin1', 'ignore')}"
        )

    logger.debug(f"✅ Clash→711 隧道已建立 ({UPSTREAM_HOST}:{UPSTREAM_PORT})")
    return reader, writer


def make_handle_client(profile: DoubleHopProfile):
    upstream_auth = base64.b64encode(
        f"{profile.upstream_user}:{profile.upstream_pass}".encode()
    ).decode()

    async def handle_client(
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
    ) -> None:
        peer = c_writer.get_extra_info("peername", ("?", 0))
        try:
            head_bytes, prebody = await _read_until_header_end(c_reader)
            method, path, version, headers = _parse_request_line(head_bytes)
            logger.info(
                f"[{profile.local_port} {profile.title}] {peer[0]}:{peer[1]} → {method} {path}"
            )

            try:
                u_reader, u_writer = await _open_clash_to_711()
            except Exception as e:
                logger.error(f"[{profile.local_port}] 建立双跳隧道失败: {e}")
                c_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await c_writer.drain()
                return

            if method.upper() == "CONNECT":
                target_connect = (
                    f"CONNECT {path} HTTP/1.1\r\n"
                    f"Host: {path}\r\n"
                    f"Proxy-Authorization: Basic {upstream_auth}\r\n"
                    f"Proxy-Connection: Keep-Alive\r\n"
                    f"\r\n"
                ).encode()
                logger.debug(f"→ 711 发送: {target_connect!r}")
                u_writer.write(target_connect)
                await u_writer.drain()

                resp_head, _ = await _read_until_header_end(u_reader, label="711→target")
                logger.debug(f"← 711 回应: {resp_head!r}")
                first_line = resp_head.split(b"\r\n", 1)[0]
                if b"200" not in first_line:
                    logger.error(
                        f"711 CONNECT 到目标失败: {first_line.decode('latin1', 'ignore')}"
                    )
                    c_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                    await c_writer.drain()
                    u_writer.close()
                    return

                c_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await c_writer.drain()

            else:
                filtered = {k: v for k, v in headers.items() if k not in _HOP_BY_HOP}
                filtered["connection"] = "close"
                filtered["proxy-authorization"] = f"Basic {upstream_auth}"

                req_lines = [f"{method} {path} {version}\r\n"]
                for k, v in filtered.items():
                    req_lines.append(f"{k}: {v}\r\n")
                req_lines.append("\r\n")

                u_writer.write("".join(req_lines).encode("iso-8859-1"))
                if prebody:
                    u_writer.write(prebody)
                await u_writer.drain()

            await _relay(c_reader, c_writer, u_reader, u_writer)

        except Exception as e:
            logger.warning(f"[{profile.local_port}] {peer[0]}:{peer[1]} error: {e}")
            try:
                c_writer.write(
                    b"HTTP/1.1 500 Internal Server Error\r\nConnection: close\r\n\r\n"
                )
                await c_writer.drain()
            except Exception:
                pass
        finally:
            try:
                c_writer.close()
            except Exception:
                pass

    return handle_client


async def _serve_profile(profile: DoubleHopProfile) -> None:
    handler = make_handle_client(profile)
    server = await asyncio.start_server(handler, LOCAL_HOST, profile.local_port)
    socks = server.sockets or []
    addrs = ", ".join(str(s.getsockname()) for s in socks)
    logger.info(
        f"监听 {addrs} | {profile.title} | 711 用户 {profile.upstream_user}"
    )
    async with server:
        await server.serve_forever()


async def main() -> None:
    logger.info("=" * 60)
    logger.info("双跳代理：多监听（Clash %s:%s → %s:%s）", CLASH_HOST, CLASH_PORT, UPSTREAM_HOST, UPSTREAM_PORT)
    for p in PROFILES:
        logger.info("  • :%s — %s", p.local_port, p.title)
    logger.info("=" * 60)
    await asyncio.gather(*(_serve_profile(p) for p in PROFILES))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("已停止。")
