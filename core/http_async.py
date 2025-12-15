# core/http_async.py

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

import aiohttp

log = logging.getLogger(__name__)

# More relaxed defaults to tolerate slow endpoints.
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(
    total=60,
    connect=10,
    sock_connect=10,
    sock_read=55,
)


@dataclass
class HttpConfig:
    base_url: str
    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0"
    max_retries: int = 5
    retry_backoff: float = 0.5
    retry_backoff_max: float = 5.0
    retry_jitter: float = 0.1
    timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT
    verify_ssl: bool = True
    limit: int = 100
    concurrency_limit: Optional[int] = None


class HttpClient:
    """
    Обёртка над aiohttp:
    - ретраи с эксп. backoff + jitter
    - JSON/текст ответы
    - ограничение параллелизма (semaphore)
    - управление cookies + fork() (копия кук в отдельный клиент)
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

        limit = self._cfg.concurrency_limit or self._cfg.limit
        self._semaphore = asyncio.Semaphore(max(1, int(limit)))

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
                ssl=self._cfg.verify_ssl,
                limit=self._cfg.limit,
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
        """Очистить cookie jar."""
        session = await self.ensure_session()
        session.cookie_jar.clear()

    async def cookies_snapshot(self) -> Dict[str, str]:
        """
        Плоская копия кук (name->value) для base_url.
        Удобно для fork().
        """
        session = await self.ensure_session()
        simple = session.cookie_jar.filter_cookies(self._cfg.base_url)
        return {k: morsel.value for k, morsel in simple.items()}

    async def fork(self) -> "HttpClient":
        """
        Изолированный клиент со СВОЕЙ aiohttp-сессией и копией текущих кук.
        Используй для «заморозки» сессии под отдельного бота.
        """
        snap = await self.cookies_snapshot()
        return HttpClient(self._cfg, cookies=snap, headers=dict(self._ext_headers))

    # ---------- core retry ----------

    async def _retry_request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
        **kwargs,
    ) -> Union[Dict[str, Any], str]:
        attempt = 0
        delay = self._cfg.retry_backoff
        last_exc: Optional[BaseException] = None

        session = await self.ensure_session()

        while attempt < self._cfg.max_retries:
            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json,
                    **kwargs,
                ) as resp:
                    if resp.status >= 400:
                        text = await resp.text()
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=f"HTTP {resp.status} body: {text[:500]}",
                            headers=resp.headers,
                        )

                    if resp.status == 204:
                        resp.release()
                        return {}

                    if expect_json:
                        return await resp.json(content_type=None)
                    return await resp.text()

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

                backoff = min(self._cfg.retry_backoff_max, delay)
                log.warning(
                    "HTTP attempt %s failed: %s; retry in %.2fs (method=%s url=%s)",
                    attempt,
                    repr(e),
                    backoff,
                    method,
                    url,
                )

                jitter_ratio = 1 + random.uniform(
                    -self._cfg.retry_jitter, self._cfg.retry_jitter
                )
                sleep_for = max(0.0, backoff * jitter_ratio)
                await asyncio.sleep(sleep_for)

                delay = min(self._cfg.retry_backoff_max, delay * 2)

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
        async with self._semaphore:
            return await self._retry_request(
                "GET",
                url,
                params=params,
                expect_json=expect_json,
                **kwargs,
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
        async with self._semaphore:
            return await self._retry_request(
                "POST",
                url,
                data=data,
                json=json,
                expect_json=expect_json,
                **kwargs,
            )
