from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Position:
    symbol: str
    shares: int
    entry_price: float
    peak_price: float
    opened_at: str


class PaperBroker:
    def __init__(self, path: Path, cash: float):
        self.path = path
        self.cash = cash
        self.position: Position | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "action", "symbol", "shares", "price", "cash", "note"])

    def buy(self, symbol: str, shares: int, price: float, note: str) -> str:
        cost = shares * price
        if self.position is not None:
            return "Paper buy skipped: already holding a position."
        if shares < 1 or cost > self.cash:
            return "Paper buy skipped: insufficient cash."
        self.cash -= cost
        self.position = Position(symbol, shares, price, price, datetime.now().isoformat())
        self._record("BUY", symbol, shares, price, note)
        return f"Paper BUY {shares} {symbol} at ${price:.2f}; cash ${self.cash:.2f}."

    def mark(self, price: float) -> None:
        if self.position is not None:
            self.position.peak_price = max(self.position.peak_price, price)

    def sell(self, price: float, note: str) -> str:
        if self.position is None:
            return "Paper sell skipped: no open position."
        pos = self.position
        self.cash += pos.shares * price
        self.position = None
        self._record("SELL", pos.symbol, pos.shares, price, note)
        pnl = (price - pos.entry_price) * pos.shares
        return f"Paper SELL {pos.shares} {pos.symbol} at ${price:.2f}; P/L ${pnl:.2f}; cash ${self.cash:.2f}."

    def _record(self, action: str, symbol: str, shares: int, price: float, note: str) -> None:
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([datetime.now().isoformat(), action, symbol, shares, f"{price:.4f}", f"{self.cash:.2f}", note])
