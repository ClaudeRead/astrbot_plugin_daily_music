"""安全 HTTP 工具：SSRF 防护 + 异步 JSON 请求。

仅允许 http/https，且目标主机解析结果不得落在环回/私有/保留等受限网段。
"""

import asyncio
import ipaddress
from urllib.parse import urlparse

import aiohttp

TIMEOUT = aiohttp.ClientTimeout(total=15)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AstrBotPlugin/1.0"}


def _ip_allowed(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def validate_url(url: str) -> None:
    """校验 URL scheme 与主机解析结果；不合规抛 ValueError。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"非法 URL：{url[:100]}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(parsed.hostname, port)
    except OSError as e:
        raise ValueError(f"主机解析失败：{parsed.hostname}") from e
    for info in infos:
        if not _ip_allowed(info[4][0]):
            raise ValueError(f"目标主机解析到受限网段：{parsed.hostname}")


async def fetch_json(url: str, params: dict | None = None, headers: dict | None = None):
    """GET 请求并解析 JSON；禁用重定向以免绕过主机校验。"""
    await validate_url(url)
    async with aiohttp.ClientSession(
        timeout=TIMEOUT, headers={**HEADERS, **(headers or {})}
    ) as session:
        async with session.get(url, params=params, allow_redirects=False) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)
