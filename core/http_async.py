# core/http_async.py
from __future__ import annotations
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import aiohttp

# Подберите таймауты под ваш бэкенд
_DEFAULT_CONNECT = 5
_DEFAULT_READ = 20
_DEFAULT_TOTAL = 30


class HttpError(RuntimeError):
    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}: {text[:200]}")
        self.status = status
        self.text = text


class HttpClient:
    """
    Обёртка над aiohttp.ClientSession.
    Храните один экземпляр на всё приложение и передавайте в стратегии.
    """

    def __init__(
        self,
        base_url: str,
        *,
        cookies: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        verify_ssl: bool = True,
    ):
        timeout = aiohttp.ClientTimeout(
            total=_DEFAULT_TOTAL, connect=_DEFAULT_CONNECT, sock_read=_DEFAULT_READ
        )
        connector = aiohttp.TCPConnector(ssl=verify_ssl)
        self._session = aiohttp.ClientSession(
            timeout=timeout, connector=connector, headers=headers
        )
        self._base_url = base_url.rstrip("/") + "/"
        if cookies:
            self._session.cookie_jar.update_cookies(cookies)

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    async def close(self) -> None:
        if not self._session.closed:
            await self._session.close()

    async def post(
        self,
        path: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        json: Any = None,
        expect_json: bool = True,
    ) -> Any:
        url = urljoin(self._base_url, path.lstrip("/"))
        async with self._session.post(url, data=data, json=json) as resp:
            if resp.status >= 400:
                raise HttpError(resp.status, await resp.text())
            return await resp.json() if expect_json else await resp.text()

    async def get(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
    ) -> Any:
        url = urljoin(self._base_url, path.lstrip("/"))
        async with self._session.get(url, params=params) as resp:
            if resp.status >= 400:
                raise HttpError(resp.status, await resp.text())
            return await resp.json() if expect_json else await resp.text()
