"""Dedicated failure-class tests: one (or more) hand-built input per bad
class the parser must recognise, independent of the generator's own
corpus. These call `parse_eml` directly.
"""
from email.mime.text import MIMEText

from email_orders.models import ParsedException, ParsedOrder
from email_orders.parser import parse_eml


def _plain(body: str, subject="Your Test Shop order confirmation",
           from_addr="orders@test-shop.invalid") -> bytes:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"Test Shop <{from_addr}>"
    msg["To"] = "you@customer-inbox.invalid"
    return msg.as_bytes()


def test_empty_bytes_is_unparseable():
    result = parse_eml(b"", "m1", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "unparseable"


def test_headerless_binary_junk_is_unparseable():
    raw = b"\x00\x01\x02\x03 this is not an email at all \xff\xfe\xfd" * 3
    result = parse_eml(raw, "m2", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "unparseable"


def test_missing_total_is_flagged_not_dropped():
    # Sender matches a known shop address, so `_identify_shop` succeeds and
    # this isolates the "recognised order, no total anywhere" failure mode.
    body = (
        "Hi Sam,\n\n"
        "Thanks for your order from Northbridge Outfitters!\n\n"
        "Order ID: TS-9001\n"
        "Order date: 2026-08-01\n"
        "Customer: Sam Pillai\n\n"
        "Items:\n"
        "  - Widget x1 @ $10.00 each\n\n"
        "We'll email you when it ships.\n"
    )
    raw = _plain(body, from_addr="orders@northbridge-outfitters.invalid")
    result = parse_eml(raw, "m3", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "missing_total"


def test_duplicate_order_id_is_flagged_on_second_occurrence():
    body = (
        "Hi Sam,\n\nThanks for your order from Northbridge Outfitters!\n\n"
        "Order ID: TS-9002\nOrder date: 2026-08-02\nCustomer: Sam Pillai\n\n"
        "Items:\n  - Widget x1 @ $10.00 each\n\n"
        "Order total: $10.00 USD\n"
    )
    raw = _plain(body, from_addr="orders@northbridge-outfitters.invalid")
    seen: dict[str, str] = {}
    first = parse_eml(raw, "m4a", "folder", seen)
    second = parse_eml(raw, "m4b", "folder", seen)
    assert isinstance(first, ParsedOrder)
    assert isinstance(second, ParsedException)
    assert second.category == "duplicate"
    assert "TS-9002" in second.detail
    assert "m4a" in second.detail


def test_irrelevant_email_from_known_shop_is_not_an_order():
    body = "Our warehouse is closed for the holiday. No orders ship this week.\n"
    raw = _plain(body, subject="Holiday shipping notice",
                 from_addr="orders@northbridge-outfitters.invalid")
    result = parse_eml(raw, "m5", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "irrelevant"


def test_unknown_sender_with_no_order_pattern_is_irrelevant():
    body = "Please take our two-minute delivery satisfaction survey.\n"
    raw = _plain(body, subject="How did we do?", from_addr="feedback@some-courier.invalid")
    result = parse_eml(raw, "m6", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "irrelevant"


def test_valid_order_still_parses_correctly():
    body = (
        "Hi Sam,\n\nThanks for your order from Northbridge Outfitters!\n\n"
        "Order ID: TS-9003\nOrder date: 2026-08-03\nCustomer: Sam Pillai\n\n"
        "Items:\n  - Widget x2 @ $5.00 each\n\n"
        "Order total: $10.00 USD\n"
    )
    raw = _plain(body, from_addr="orders@northbridge-outfitters.invalid")
    result = parse_eml(raw, "m7", "folder", {})
    assert isinstance(result, ParsedOrder)
    assert result.order_id == "TS-9003"
    assert result.total == 10.0
    assert result.currency == "USD"
    assert result.customer == "Sam Pillai"
    assert result.item_count == 1
