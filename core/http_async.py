# core/http_async.py
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Union

import aiohttp

log = logging.getLogger(__name__)

# Быстрые fail-fast таймауты: для трейдинга лучше быстро упасть и (возможно) 1-2 раза повторить,
# чем зависать десятки секунд и пропускать окно входа.
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(
    total=10,
    connect=3,
    sock_connect=3,
    sock_read=6,
)


@dataclass
class HttpConfig:
    base_url: str
    user_agent: str = "Mozilla/5.0 (X11; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0"

    # Короткие ретраи (1-2) вместо долгих ожиданий
    max_retries: int = 2
    retry_backoff: float = 0.2
    retry_backoff_max: float = 0.7
    retry_jitter: float = 0.1

    # Межзапросная задержка (если API не любит “пулемёт” — можно поставить 0.05..0.15)
    request_spacing: float = 0.0

    timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT
    verify_ssl: bool = True

    # Лимиты соединений (TCPConnector)
    limit: int = 20
    limit_per_host: int = 10
    dns_cache_ttl: int = 300

    # Ограничение параллелизма на уровне клиента (семафор).
    # Для торговых API часто лучше 1..3.
    concurrency_limit: Optional[int] = 2

    # Ретраим только безопасные методы по умолчанию (POST/PUT/PATCH — НЕ ретраим, чтобы не удвоить действие)
    retry_methods: FrozenSet[str] = frozenset({"GET", "HEAD", "OPTIONS"})

    # Ретраибл статусы (типично: временные проблемы/рейткеп/таймаут)
    retry_statuses: FrozenSet[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

    # Ограничиваем Retry-After сверху, чтобы “не ждать долго”
    retry_after_cap: float = 1.0


class HttpClient:
    """
    Обёртка над aiohttp:
    - быстрые ретраи только там, где это имеет смысл
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

        # Троттлинг последовательных запросов (если нужно)
        self._throttle_lock = asyncio.Lock()
        self._last_request_at: float = 0.0

        # Ограничение параллелизма
        sem_limit = self._cfg.concurrency_limit or self._cfg.limit
        self._semaphore = asyncio.Semaphore(max(1, int(sem_limit)))

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
                limit=max(1, int(self._cfg.limit)),
                limit_per_host=max(1, int(self._cfg.limit_per_host)),
                ttl_dns_cache=max(0, int(self._cfg.dns_cache_ttl)),
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                base_url=self._cfg.base_url,
                timeout=self._cfg.timeout,
                connector=connector,
                headers=headers,
                trust_env=False,
            )
            if self._init_cookies:
                self._session.cookie_jar.update_cookies(self._init_cookies)
        return self._session

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- cookies ----------

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

    # ---------- helpers ----------

    def _is_retryable_method(self, method: str) -> bool:
        return method.upper() in self._cfg.retry_methods

    def _is_retryable_status(self, status: int) -> bool:
        return status in self._cfg.retry_statuses

    def _parse_retry_after(self, value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            # most APIs send seconds
            sec = float(value.strip())
            return max(0.0, min(sec, float(self._cfg.retry_after_cap)))
        except Exception:
            # ignore HTTP-date format for simplicity (не хотим ждать долго)
            return None

    async def _throttle(self) -> None:
        if self._cfg.request_spacing <= 0:
            return
        async with self._throttle_lock:
            now = asyncio.get_running_loop().time()
            elapsed = now - self._last_request_at
            wait_for = max(0.0, self._cfg.request_spacing - elapsed)
            if wait_for:
                await asyncio.sleep(wait_for)
                now = asyncio.get_running_loop().time()
            self._last_request_at = now

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
        timeout: Optional[aiohttp.ClientTimeout] = None,
        **kwargs,
    ) -> Union[Dict[str, Any], str]:
        session = await self.ensure_session()

        attempt = 0
        delay = float(self._cfg.retry_backoff)
        last_exc: Optional[BaseException] = None

        method_u = method.upper()
        can_retry = self._is_retryable_method(method_u)

        while True:
            try:
                await self._throttle()

                req_kwargs = dict(kwargs)
                if timeout is not None:
                    req_kwargs["timeout"] = timeout

                async with session.request(
                    method_u,
                    url,
                    params=params,
                    data=data,
                    json=json,
                    **req_kwargs,
                ) as resp:
                    # 204 No Content
                    if resp.status == 204:
                        resp.release()
                        return {}

                    # Ошибки HTTP
                    if resp.status >= 400:
                        text = await resp.text()

                        # Быстро выходим на НЕретраибл кодах/методах
                        if (not can_retry) or (not self._is_retryable_status(resp.status)):
                            raise aiohttp.ClientResponseError(
                                request_info=resp.request_info,
                                history=resp.history,
                                status=resp.status,
                                message=f"HTTP {resp.status} body: {text[:500]}",
                                headers=resp.headers,
                            )

                        # Ретраибл ответ: выбросим ошибку, чтобы попасть в retry-ветку ниже
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=f"RETRYABLE HTTP {resp.status} body: {text[:500]}",
                            headers=resp.headers,
                        )

                    # Успешный ответ
                    if expect_json:
                        return await resp.json(content_type=None)
                    return await resp.text()

            except aiohttp.ClientResponseError as e:
                last_exc = e

                # Если статус не ретраибл или метод не ретраибл — сразу наверх
                if (not can_retry) or (e.status is None) or (not self._is_retryable_status(int(e.status))):
                    raise

                attempt += 1
                if attempt > self._cfg.max_retries:
                    break

                # Если есть Retry-After (часто на 429) — уважаем, но капаем сверху
                retry_after = None
                if getattr(e, "headers", None):
                    retry_after = self._parse_retry_after(e.headers.get("Retry-After"))

                backoff = min(self._cfg.retry_backoff_max, delay)
                jitter_ratio = 1.0 + random.uniform(-self._cfg.retry_jitter, self._cfg.retry_jitter)
                sleep_for = max(0.0, backoff * jitter_ratio)

                if retry_after is not None:
                    sleep_for = max(sleep_for, retry_after)

                log.warning(
                    "HTTP attempt %s/%s failed: %s; retry in %.2fs (method=%s url=%s)",
                    attempt,
                    self._cfg.max_retries,
                    repr(e),
                    sleep_for,
                    method_u,
                    url,
                )

                await asyncio.sleep(sleep_for)
                delay = min(self._cfg.retry_backoff_max, delay * 2)

            except (
                aiohttp.ClientConnectionError,
                aiohttp.ServerTimeoutError,
                asyncio.TimeoutError,
            ) as e:
                last_exc = e

                # На сетевых/таймаут-ошибках ретраим только безопасные методы
                if not can_retry:
                    raise

                attempt += 1
                if attempt > self._cfg.max_retries:
                    break

                backoff = min(self._cfg.retry_backoff_max, delay)
                jitter_ratio = 1.0 + random.uniform(-self._cfg.retry_jitter, self._cfg.retry_jitter)
                sleep_for = max(0.0, backoff * jitter_ratio)

                log.warning(
                    "HTTP attempt %s/%s failed: %s; retry in %.2fs (method=%s url=%s)",
                    attempt,
                    self._cfg.max_retries,
                    repr(e),
                    sleep_for,
                    method_u,
                    url,
                )

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
        timeout: Optional[aiohttp.ClientTimeout] = None,
        **kwargs,
    ) -> Union[Dict[str, Any], str]:
        async with self._semaphore:
            return await self._retry_request(
                "GET",
                url,
                params=params,
                expect_json=expect_json,
                timeout=timeout,
                **kwargs,
            )

    async def post(
        self,
        url: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        # ВАЖНО: по умолчанию POST НЕ ретраится (см retry_methods), чтобы не задвоить действие.
        **kwargs,
    ) -> Union[Dict[str, Any], str]:
        async with self._semaphore:
            return await self._retry_request(
                "POST",
                url,
                data=data,
                json=json,
                expect_json=expect_json,
                timeout=timeout,
                **kwargs,
            )

    async def post_idempotent(
        self,
        url: str,
        *,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        **kwargs,
    ) -> Union[Dict[str, Any], str]:
        """
        Если у тебя есть эндпоинт POST, который безопасно повторять (идемпотентный),
        используй этот метод: он временно разрешает ретраи для POST.
        """
        old = self._cfg.retry_methods
        try:
            self._cfg.retry_methods = frozenset(set(old) | {"POST"})
            async with self._semaphore:
                return await self._retry_request(
                    "POST",
                    url,
                    data=data,
                    json=json,
                    expect_json=expect_json,
                    timeout=timeout,
                    **kwargs,
                )
        finally:
            self._cfg.retry_methods = old
