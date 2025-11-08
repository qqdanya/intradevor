# gui/trades_table_widget.py
from __future__ import annotations
from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QBrush
from core.money import format_amount, format_money


class TradesTableWidget(QTableWidget):
    """
    Таблица сделок.
    Колонки:
      [0] Время сигнала
      [1] Время ставки
      [2] Стратегия
      [3] Серия
      [4] Индикатор        <-- ОТ КОГО ПРИШЁЛ СИГНАЛ
      [5] Валютная пара
      [6] ТФ
      [7] Направление
      [8] Ставка
      [9] Время
      [10] Процент
      [11] P/L
      [12] Счёт
    """

    COLS = [
        "Время сигнала",
        "Время ставки",
        "Стратегия",
        "Серия",
        "Индикатор",
        "Валютная пара",
        "ТФ",
        "Направление",
        "Ставка",
        "Время",
        "Процент",
        "P/L",
        "Счет",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)

        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hdr.setStretchLastSection(True)

        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # trade_id -> row
        self._row_by_trade: dict[str, int] = {}
        # ожидание (trade_id -> {row, timer, expected_end_ts})
        self._pending_rows: dict[str, dict] = {}

    def add_pending(
        self,
        trade_id: str,
        signal_at: str,
        placed_at: str,
        symbol: str,
        timeframe: str,
        direction: int,  # 1=UP, 2=DOWN
        stake: float,
        duration: float,
        percent: int,
        account_mode: str,  # "ДЕМО"/"РЕАЛ"
        indicator: str = "-",  # НАЗВАНИЕ ИНДИКАТОРА
        strategy: str = "-",
        series: str | None = None,
        expected_end_ts: float | None = None,
        currency: str | None = None,
    ):
        """Добавляет строку ожидания с таймером."""
        from time import time as _now

        row = 0
        self.insertRow(row)

        if expected_end_ts is None:
            expected_end_ts = _now() + float(duration)

        def _fmt_left(sec: float) -> str:
            s = int(max(0, round(sec)))
            h, r = divmod(s, 3600)
            m, s = divmod(r, 60)
            if h > 0:
                return f"{h}:{m:02d}:{s:02d}"
            if m > 0:
                return f"{m}:{s:02d}"
            return f"{s} с"

        dir_text = "ВВЕРХ" if int(direction) == 1 else "ВНИЗ"
        left_now = max(0.0, expected_end_ts - _now())
        if currency:
            stake_txt = format_money(stake, currency)
        else:
            stake_txt = format_amount(stake)

        values = [
            signal_at,
            placed_at,
            strategy or "-",
            series or "—",
            indicator or "-",
            symbol,
            timeframe,
            dir_text,
            stake_txt,
            f"{int(round(duration / 60))} мин",
            f"{percent}%",
            f"Ожидание ({_fmt_left(left_now)})",
            account_mode,
        ]
        for col, val in enumerate(values):
            it = QTableWidgetItem(str(val))
            if col in (7, 11):  # выравнивание Направление, P/L по центру
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setItem(row, col, it)

        yellow = QBrush(QColor("#fff4c2"))
        for c in range(self.columnCount()):
            it = self.item(row, c)
            if it:
                it.setBackground(yellow)

        timer = QTimer(self)
        timer.setInterval(1000)

        def _tick():
            left = expected_end_ts - _now()
            info = self._pending_rows.get(trade_id)
            if not info:
                timer.stop()
                return
            cur_row = info.get("row")
            if not isinstance(cur_row, int) or cur_row >= self.rowCount():
                timer.stop()
                return
            item = self.item(cur_row, 11)
            if item:
                item.setText(f"Ожидание ({_fmt_left(left)})")
            if left <= 0:
                timer.stop()

        timer.timeout.connect(_tick)
        timer.start()

        # сдвигаем индексы ранее вставленных (мы вставили сверху)
        self._row_by_trade = {
            tid: (r + 1 if r >= row else r) for tid, r in self._row_by_trade.items()
        }
        for info in self._pending_rows.values():
            r = info.get("row")
            if isinstance(r, int) and r >= row:
                info["row"] = r + 1
        self._row_by_trade[trade_id] = row
        self._pending_rows[trade_id] = {
            "row": row,
            "timer": timer,
            "expected_end_ts": float(expected_end_ts),
            "series": series,
        }

    def set_result(
        self, trade_id: str, profit: float | None, currency: str = "",
    ):
        info = self._pending_rows.pop(trade_id, None)
        if info:
            timer = info.get("timer")
            if isinstance(timer, QTimer):
                try:
                    timer.stop()
                except Exception:
                    pass

        if trade_id not in self._row_by_trade:
            return
        row = self._row_by_trade[trade_id]

        pl_item = self.item(row, 11)
        if pl_item is None:
            pl_item = QTableWidgetItem()
            pl_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setItem(row, 11, pl_item)

        if profit is None:
            pl_item.setText("неизв.")
            return

        if currency:
            text = format_money(profit, currency, show_plus=True)
        else:
            text = format_amount(profit, show_plus=True)
        pl_item.setText(text)

        # лёгкая подсветка всей строки
        if profit > 0:
            row_bg = QColor(200, 255, 200)
        elif abs(profit) < 1e-9:
            row_bg = QColor(230, 230, 230)
        else:
            row_bg = QColor(255, 215, 215)

        for c in range(self.columnCount()):
            it = self.item(row, c)
            if it:
                it.setBackground(QBrush(row_bg))

    def remove_trade(self, trade_id: str):
        """Удаляет сделку из таблицы по её идентификатору."""
        info = self._pending_rows.pop(trade_id, None)
        if info:
            timer = info.get("timer")
            if isinstance(timer, QTimer):
                try:
                    timer.stop()
                except Exception:
                    pass

        row = self._row_by_trade.pop(trade_id, None)
        if row is None:
            return
        if 0 <= row < self.rowCount():
            self.removeRow(row)

        # пересчитываем индексы после удаления строки
        self._row_by_trade = {
            tid: (r - 1 if r > row else r) for tid, r in self._row_by_trade.items()
        }
        for info in self._pending_rows.values():
            r = info.get("row")
            if isinstance(r, int):
                if r == row:
                    info["row"] = None
                elif r > row:
                    info["row"] = r - 1
