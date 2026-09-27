"""Shared dataclasses for the order-confirmation pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LineItem:
    name: str
    qty: int
    unit_price: float


@dataclass
class OrderSpec:
    """Ground truth for one synthetic order, as the generator built it.

    `message_id` ties this back to the .eml file that carries it, so tests
    can reconcile generator truth against parser output exactly.
    """

    message_id: str
    order_id: str
    shop: str
    date: str  # ISO 8601 date, e.g. 2026-08-14
    customer: str
    items: list[LineItem]
    currency: str
    total: float
    # If set, this order is a deliberate duplicate of another message_id's
    # order_id (used to test duplicate-order-id detection).
    duplicate_of: str | None = None


@dataclass
class ExceptionSpec:
    """Ground truth for one deliberately-bad synthetic email."""

    message_id: str
    category: str  # unparseable | missing_total | duplicate | irrelevant
    note: str = ""


@dataclass
class GeneratedCorpus:
    orders: list[OrderSpec] = field(default_factory=list)
    exceptions: list[ExceptionSpec] = field(default_factory=list)
    # message_id -> relative .eml filename
    files: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedOrder:
    message_id: str
    order_id: str | None
    shop: str | None
    date: str | None
    customer: str | None
    items_json: str
    item_count: int
    total: float | None
    currency: str | None
    source: str  # folder | imap


@dataclass
class ParsedException:
    message_id: str
    category: str
    detail: str
    source: str  # folder | imap
