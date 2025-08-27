# gui/trades_table_widget.py
from __future__ import annotations
from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QBrush
from core.money import format_amount


class TradesTableWidget(QTableWidget):
    """
    Таблица сделок.
    Колонки:
      [0] Время сигнала
      [1] Время ставки
      [2] Стратегия
      [3] Индикатор        <-- ОТ КОГО ПРИШЁЛ СИГНАЛ
      [4] Валютная пара
      [5] ТФ
      [6] Направление
      [7] Ставка
      [8] Время
      [9] Процент
      [10] P/L
      [11] Счёт
    """

    COLS = [
        "Время сигнала",
        "Время ставки",
        "Стратегия",
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

        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # trade_id -> row
        self._row_by_trade: dict[str, int] = {}

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
    ):
        row = 0
        self.insertRow(row)

        dir_text = "ВВЕРХ" if int(direction) == 1 else "ВНИЗ"
        values = [
            signal_at,
            placed_at,
            strategy or "-",
            indicator or "-",
            symbol,
            timeframe,
            dir_text,
            format_amount(stake),
            f"{int(round(duration / 60))} мин",
            f"{percent}%",
            "ожидание…",
            account_mode,
        ]
        for col, val in enumerate(values):
            it = QTableWidgetItem(str(val))
            if col in (6, 10):  # выравнивание Направление, P/L по центру
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setItem(row, col, it)

        # сдвигаем индексы ранее вставленных (мы вставили сверху)
        self._row_by_trade = {
            tid: (r + 1 if r >= row else r) for tid, r in self._row_by_trade.items()
        }
        self._row_by_trade[trade_id] = row

    def set_result(
        self, trade_id: str, profit: float | None, currency_suffix: str = ""
    ):
        if trade_id not in self._row_by_trade:
            return
        row = self._row_by_trade[trade_id]

        pl_item = self.item(row, 10)
        if pl_item is None:
            pl_item = QTableWidgetItem()
            pl_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setItem(row, 10, pl_item)

        if profit is None:
            pl_item.setText("неизв.")
            return

        text = format_amount(profit, show_plus=True)
        if currency_suffix:
            text += f" {currency_suffix}"
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
