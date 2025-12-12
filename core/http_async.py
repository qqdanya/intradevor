# core/http_async.py
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Dict, Optional, Callable, Awaitable, Union

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(
    total=15, connect=5, sock_connect=5, sock_read=10
)


@dataclass
class HttpConfig:
    base_url: str
    user_agent: str = "Intradevor/1.0"
    max_retries: int = 3
    retry_backoff: float = 0.5  # 0.5, 1.0, 2.0...
    timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT
    verify_ssl: bool = True
    limit: int = 100  # max concurrent connections


class HttpClient:
    """
    Лёгкая обёртка над aiohttp с ретраями, JSON/текст ответами и «форком» с копией кук.
    """

    def __init__(
        self,
        cfg: HttpConfig,
        *,
        cookies: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self._cfg = cfg
        self._ext_headers = headers or {}
        self._init_cookies = cookies or {}
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "HttpClient":
        await self.ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "User-Agent": self._cfg.user_agent,
                "Accept": "application/json",
                **self._ext_headers,
            }
            connector = aiohttp.TCPConnector(
                ssl=self._cfg.verify_ssl,
                limit=self._cfg.limit,
                limit_per_host=self._cfg.limit,      # важно
                ttl_dns_cache=300,                   # ускоряет, меньше DNS задержек
                keepalive_timeout=30,                # держим соединения живыми
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                base_url=self._cfg.base_url,
                timeout=self._cfg.timeout,
                connector=connector,
                headers=headers,
                trust_env=True,
            )
            if self._init_cookies:
                self._session.cookie_jar.update_cookies(self._init_cookies)
        return self._session

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def update_cookies(self, cookies: Dict[str, str]) -> None:
        session = await self.ensure_session()
        session.cookie_jar.update_cookies(cookies)

    async def clear_cookies(self) -> None:
        session = await self.ensure_session()
        session.cookie_jar.clear()

    async def cookies_snapshot(self) -> Dict[str, str]:
        session = await self.ensure_session()
        simple = session.cookie_jar.filter_cookies(self._cfg.base_url)
        return {k: morsel.value for k, morsel in simple.items()}

    async def fork(self) -> "HttpClient":
        snap = await self.cookies_snapshot()
        return HttpClient(self._cfg, cookies=snap, headers=dict(self._ext_headers))

    async def _retry(
        self,
        request_cm: Callable[[], aiohttp.client._RequestContextManager],
        parse: Callable[[aiohttp.ClientResponse], Awaitable[Union[Dict[str, Any], str]]],
    ) -> Union[Dict[str, Any], str]:
        attempt = 0
        base_delay = self._cfg.retry_backoff
        last_exc: Optional[BaseException] = None

        while attempt < self._cfg.max_retries:
            try:
                # ВАЖНО: async with гарантирует освобождение соединения
                async with request_cm() as resp:
                    # полезно ретраить и 408/429 тоже (часто бывает под нагрузкой)
                    if resp.status in (408, 429) or resp.status >= 500:
                        text = await resp.text()
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=f"HTTP {resp.status} body: {text[:300]}",
                            headers=resp.headers,
                        )

                    if resp.status == 204:
                        return {}

                    return await parse(resp)

            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerTimeoutError,
                aiohttp.ClientResponseError,
                asyncio.TimeoutError,
            ) as e:
                last_exc = e
                attempt += 1
                if attempt >= self._cfg.max_retries:
                    break

                # backoff + jitter (чтобы не долбить сервер синхронно)
                delay = base_delay * (2 ** (attempt - 1))
                delay = delay * (0.85 + random.random() * 0.30)

                log.warning(
                    "HTTP attempt %s failed: %s; retry in %.2fs",
                    attempt,
                    repr(e),
                    delay,
                )
                await asyncio.sleep(delay)

        assert last_exc is not None
        raise last_exc

    async def get(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        **kwargs,
    ) -> Union[Dict[str, Any], str]:
        session = await self.ensure_session()
        if expect_json:
            return await self._retry(
                lambda: session.get(url, params=params, timeout=timeout, **kwargs),
                lambda r: r.json(content_type=None),
            )
        return await self._retry(
            lambda: session.get(url, params=params, timeout=timeout, **kwargs),
            lambda r: r.text(),
        )

    async def post(
        self,
        url: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        **kwargs,
    ) -> Union[Dict[str, Any], str]:
        session = await self.ensure_session()
        if expect_json:
            return await self._retry(
                lambda: session.post(url, data=data, json=json, timeout=timeout, **kwargs),
                lambda r: r.json(content_type=None),
            )
        return await self._retry(
            lambda: session.post(url, data=data, json=json, timeout=timeout, **kwargs),
            lambda r: r.text(),
        )
