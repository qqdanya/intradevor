# core/http_async.py
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Callable, Awaitable, Union

import aiohttp

log = logging.getLogger(__name__)


def _float_env(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        return int(raw) if raw is not None else default
    except ValueError:
        return default


DEFAULT_TIMEOUT = aiohttp.ClientTimeout(
    total=_float_env("HTTP_TIMEOUT_TOTAL", 30),
    connect=_float_env("HTTP_TIMEOUT_CONNECT", 10),
    sock_connect=_float_env("HTTP_TIMEOUT_SOCK_CONNECT", 10),
    sock_read=_float_env("HTTP_TIMEOUT_SOCK_READ", 20),
)

DEFAULT_MAX_RETRIES = _int_env("HTTP_MAX_RETRIES", 5)
DEFAULT_RETRY_BACKOFF = _float_env("HTTP_RETRY_BACKOFF", 1.0)


@dataclass
class HttpConfig:
    base_url: str
    user_agent: str = "Intradevor/1.0"
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff: float = DEFAULT_RETRY_BACKOFF  # 1.0, 2.0, 4.0...
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

    # ---------- session lifecycle ----------

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
                ssl=self._cfg.verify_ssl, limit=self._cfg.limit
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

    # ---------- cookies ----------

    async def update_cookies(self, cookies: Dict[str, str]) -> None:
        """Горячо обновить куки в текущей сессии."""
        session = await self.ensure_session()
        session.cookie_jar.update_cookies(cookies)

    async def clear_cookies(self) -> None:
        session = await self.ensure_session()
        session.cookie_jar.clear()

    async def cookies_snapshot(self) -> Dict[str, str]:
        """
        Плоская копия кук (name->value) для base_url.
        """
        session = await self.ensure_session()
        simple = session.cookie_jar.filter_cookies(self._cfg.base_url)
        return {k: morsel.value for k, morsel in simple.items()}

    async def fork(self) -> "HttpClient":
        """
        Изолированный клиент со СВОЕЙ aiohttp-сессией и копией текущих кук.
        Используй для «заморозки» сессии под бота.
        """
        snap = await self.cookies_snapshot()
        return HttpClient(self._cfg, cookies=snap, headers=dict(self._ext_headers))

    # ---------- core retry ----------

    async def _retry(
        self,
        func: Callable[[], Awaitable[aiohttp.ClientResponse]],
        parse: Callable[
            [aiohttp.ClientResponse], Awaitable[Union[Dict[str, Any], str]]
        ],
    ) -> Union[Dict[str, Any], str]:
        attempt = 0
        delay = self._cfg.retry_backoff
        last_exc: Optional[BaseException] = None
        while attempt < self._cfg.max_retries:
            try:
                resp = await func()
                if resp.status >= 500:
                    text = await resp.text()
                    raise aiohttp.ClientResponseError(
                        request_info=resp.request_info,
                        history=resp.history,
                        status=resp.status,
                        message=f"Server error body: {text[:500]}",
                        headers=resp.headers,
                    )
                if resp.status == 204:
                    return {}  # пустой JSON
                return await parse(resp)
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerTimeoutError,
                aiohttp.ClientResponseError,
            ) as e:
                last_exc = e
                attempt += 1
                if attempt >= self._cfg.max_retries:
                    break
                log.warning(
                    "HTTP attempt %s failed: %s; retry in %.2fs",
                    attempt,
                    repr(e),
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
        assert last_exc is not None
        raise last_exc

    # ---------- requests ----------

    async def get(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
        **kwargs,
    ) -> Union[Dict[str, Any], str]:
        session = await self.ensure_session()
        if expect_json:
            return await self._retry(
                lambda: session.get(url, params=params, **kwargs),
                lambda r: r.json(content_type=None),
            )
        else:
            return await self._retry(
                lambda: session.get(url, params=params, **kwargs), lambda r: r.text()
            )

    async def post(
        self,
        url: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
        **kwargs,
    ) -> Union[Dict[str, Any], str]:
        session = await self.ensure_session()
        if expect_json:
            return await self._retry(
                lambda: session.post(url, data=data, json=json, **kwargs),
                lambda r: r.json(content_type=None),
            )
        else:
            return await self._retry(
                lambda: session.post(url, data=data, json=json, **kwargs),
                lambda r: r.text(),
            )
