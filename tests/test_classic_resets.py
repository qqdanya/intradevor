import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if "aiohttp" not in sys.modules:
    class _DummyTimeout:  # pragma: no cover - simple stub
        def __init__(self, *_, **__):
            pass

    class _DummySession:  # pragma: no cover - simple stub
        def __init__(self, *_, **__):
            self.closed = False
            self.cookie_jar = types.SimpleNamespace(
                update_cookies=lambda *a, **k: None,
                clear=lambda: None,
                filter_cookies=lambda url: {},
            )

        async def close(self):
            self.closed = True

    class _DummyConnector:  # pragma: no cover - simple stub
        def __init__(self, *_, **__):
            pass

    sys.modules["aiohttp"] = types.SimpleNamespace(
        ClientTimeout=_DummyTimeout,
        ClientSession=_DummySession,
        TCPConnector=_DummyConnector,
    )

if "bs4" not in sys.modules:
    bs4_stub = types.ModuleType("bs4")

    class _DummySoup:  # pragma: no cover - simple stub
        def __init__(self, *_, **__):
            pass

    bs4_stub.BeautifulSoup = _DummySoup
    sys.modules["bs4"] = bs4_stub

from strategies.martingale import MartingaleStrategy, MOSCOW_TZ
from strategies.fibonacci import FibonacciStrategy
from strategies.oscar_grind_2 import OscarGrind2Strategy


class ClassicResetBehaviourTests(IsolatedAsyncioTestCase):
    async def test_martingale_resets_direction_after_low_payout(self):
        await self._assert_classic_low_payout_forces_new_signal(
            MartingaleStrategy, "strategies.martingale"
        )

    async def test_fibonacci_resets_direction_after_low_payout(self):
        await self._assert_classic_low_payout_forces_new_signal(
            FibonacciStrategy, "strategies.fibonacci"
        )

    async def test_oscar_grind_resets_direction_after_low_payout(self):
        await self._assert_classic_low_payout_forces_new_signal(
            OscarGrind2Strategy, "strategies.oscar_grind_2"
        )

    async def test_sprint_does_not_reset_direction(self):
        calls = await self._run_low_payout_scenario(
            MartingaleStrategy,
            "strategies.martingale",
            trade_type="sprint",
        )
        self.assertEqual(calls, 1)

    async def _assert_classic_low_payout_forces_new_signal(
        self, cls, module_name
    ) -> None:
        calls = await self._run_low_payout_scenario(
            cls, module_name, trade_type="classic"
        )
        self.assertGreaterEqual(
            calls,
            2,
            "Classic mode should request a fresh signal after prolonged low payout.",
        )

    async def _run_low_payout_scenario(
        self,
        cls,
        module_name: str,
        *,
        trade_type: str,
    ) -> int:
        pct_sequence = [90, 60, 90]
        profits = [-10.0, 10.0]

        async def fake_get_current_percent(*_, **__):
            if pct_sequence:
                return pct_sequence.pop(0)
            return 90

        async def fake_place_trade(*_, **__):
            return "trade-id"

        async def fake_check_trade_result(*_, **__):
            if profits:
                return profits.pop(0)
            return 10.0

        async def fake_is_demo_account(*_, **__):
            return False

        async def fake_get_balance_info(*_, **__):
            return 10_000.0, "RUB", "10000 RUB"

        class TestStrategy(cls):
            async def _ensure_anchor_currency(self) -> bool:  # type: ignore[override]
                return True

            async def _ensure_anchor_account_mode(self) -> bool:  # type: ignore[override]
                return True

            async def sleep(self, seconds: float) -> None:  # type: ignore[override]
                return None

        strategy = TestStrategy(
            http_client=object(),
            user_id="u",
            user_hash="h",
            symbol="EURUSD",
            timeframe="M5",
            params={
                "trade_type": trade_type,
                "repeat_count": 1,
                "max_steps": 3,
                "min_percent": 80,
                "wait_on_low_percent": 0,
                "signal_timeout_sec": 1,
                "base_investment": 100,
            },
        )

        wait_calls = 0

        test_case = self

        async def fake_wait_signal(strategy_obj, *, timeout: float):
            nonlocal wait_calls
            wait_calls += 1
            if trade_type == "classic" and wait_calls == 2:
                # On the second request we expect stale timings to be cleared.
                test_case.assertIsNone(strategy_obj._next_expire_dt)
                test_case.assertIsNone(strategy_obj._last_signal_at_str)

            strategy_obj._next_expire_dt = datetime.now(MOSCOW_TZ) + timedelta(minutes=1)
            strategy_obj._last_signal_at_str = "ts"
            strategy_obj._last_signal_ver = (strategy_obj._last_signal_ver or 0) + 1
            return 1

        patches = [
            patch(f"{module_name}.get_current_percent", fake_get_current_percent),
            patch(f"{module_name}.place_trade", fake_place_trade),
            patch(f"{module_name}.check_trade_result", fake_check_trade_result),
            patch(f"{module_name}.is_demo_account", fake_is_demo_account),
            patch(f"{module_name}.get_balance_info", fake_get_balance_info),
            patch.object(cls, "wait_signal", fake_wait_signal),
        ]

        async with _apply_patches(patches):
            await strategy.run()

        return wait_calls


class _apply_patches:
    def __init__(self, patches):
        self._patches = patches

    async def __aenter__(self):
        for p in self._patches:
            p.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        for p in reversed(self._patches):
            p.stop()
        return False
