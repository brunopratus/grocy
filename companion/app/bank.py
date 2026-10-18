"""Phase 5 (interface only): link grocery card transactions from the bank app to receipts.

The household's bank app (repo brunopratus/bank, also on Railway) holds the card
transactions. Plan:

  1. ``BankTransactionSource.grocery_transactions(start, end)`` fetches card payments whose
     merchant looks like a grocery store (Maxima, Rimi, Lidl, Norfa, IKI, market).
  2. ``match_receipts`` pairs each transaction with a stored receipt by date (+/- 1 day),
     amount (exact to the cent) and merchant -> shopping location.
  3. A report shows spending per week and per store next to the nutrition targets.

TODO (phase 5): implement an HTTP source against the bank app's API once its endpoint and
auth are decided; nothing here talks to the bank yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass
class CardTransaction:
    id: str
    booked: date
    amount_eur: float  # negative = money out
    merchant: str


@dataclass
class ReceiptRef:
    id: str
    purchased: date
    total_eur: float
    store: str


@dataclass
class Match:
    transaction: CardTransaction
    receipt: ReceiptRef | None
    confidence: float


class BankTransactionSource(Protocol):
    async def grocery_transactions(self, start: date, end: date) -> list[CardTransaction]: ...


def match_receipts(transactions: list[CardTransaction], receipts: list[ReceiptRef]) -> list[Match]:
    """TODO (phase 5): date/amount/merchant matching."""
    raise NotImplementedError("Phase 5: bank link is not implemented yet")
