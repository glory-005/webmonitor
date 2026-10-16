"""httpx 抓取器：连接复用 + 指数退避重试 + 中英文站点编码兜底。"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping

import httpx
from loguru import logger
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from webmoniter.fetch.base import FetchResult
from webmoniter.models import Target
from webmoniter.settings import Settings

#: 需要重试的状态码
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})

_CHARSET_HEADER_RE = re.compile(r"charset\s*=\s*[\"']?([\w\-]+)", re.IGNORECASE)
_CHARSET_META_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w\-]+)", re.IGNORECASE)


def decode_body(content: bytes, headers: Mapping[str, str]) -> str:
    """把响应字节解码成文本。

    httpx 的 response.text 在服务端没声明 charset 时按 UTF-8 处理，
    而国内不少站点是 GBK/GB2312 且不声明，所以这里自己按
    「响应头 → HTML meta → UTF-8 → GB18030」的顺序试。
    """
    candidates: list[str] = []
    header_match = _CHARSET_HEADER_RE.search(headers.get("content-type", ""))
    if header_match:
        candidates.append(header_match.group(1))
    meta_match = _CHARSET_META_RE.search(content[:4096])
    if meta_match:
        candidates.append(meta_match.group(1).decode("ascii", "ignore"))
    candidates.extend(("utf-8", "gb18030"))

    tried: set[str] = set()
    for encoding in candidates:
        encoding = encoding.strip().lower()
        if not encoding or encoding in tried:
            continue
        tried.add(encoding)
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    # 全部失败时用容错解码，宁可看到乱码也不要丢内容
    return content.decode("utf-8", errors="replace")


class HttpFetcher:
    """共享一个 AsyncClient，按 target 覆盖 header / Cookie。"""

    name = "http"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(settings.default_timeout, connect=10.0),
            headers={
                "User-Agent": settings.default_user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, target: Target) -> dict[str, str]:
        headers = dict(target.headers)
        if target.cookies_env:
            # .env 已被 webmoniter.settings 注入 os.environ，这里能直接读到
            cookie = os.environ.get(target.cookies_env)
            if cookie:
                headers["Cookie"] = cookie
            else:
                logger.warning(
                    "target {} 引用了 cookies_env={}，但环境变量为空，将按未登录状态抓取",
                    target.id,
                    target.cookies_env,
                )
        return headers

    async def _get(self, target: Target) -> httpx.Response:
        """带重试的 GET：网络错误与 5xx/429 都会重试。"""
        retryer = AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.8, max=8),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        )
        headers = self._headers(target)
        async for attempt in retryer:
            with attempt:
                response = await self._client.get(
                    target.url,
                    headers=headers,
                    timeout=httpx.Timeout(target.timeout, connect=min(10.0, target.timeout)),
                )
                if response.status_code in RETRY_STATUS:
                    response.raise_for_status()
                return response
        raise RuntimeError("unreachable")  # pragma: no cover - tenacity 会先抛

    async def fetch(self, target: Target) -> FetchResult:
        started = time.perf_counter()
        try:
            response = await self._get(target)
        except httpx.HTTPStatusError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            status = exc.response.status_code
            logger.warning("target {} 返回 HTTP {}（重试后仍失败）", target.id, status)
            return FetchResult(
                url=target.url, status=status, elapsed_ms=elapsed, error=f"HTTP {status}"
            )
        except Exception as exc:  # 网络/证书/超时等
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.warning("target {} 抓取异常: {}: {}", target.id, type(exc).__name__, exc)
            return FetchResult(
                url=target.url, elapsed_ms=elapsed, error=f"{type(exc).__name__}: {exc}"
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            return FetchResult(
                url=target.url,
                status=response.status_code,
                elapsed_ms=elapsed,
                error=f"HTTP {response.status_code}",
            )
        text = decode_body(response.content, response.headers)
        return FetchResult(url=str(response.url), text=text, status=response.status_code, elapsed_ms=elapsed)
