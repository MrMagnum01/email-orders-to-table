"""Parses raw .eml bytes into a `ParsedOrder` or a `ParsedException`.

Every message produces exactly one row in exactly one of the two output
tables. Nothing is dropped: a message that isn't a recognisable order
confirmation, can't be decoded at all, is missing its total, repeats an
order id already seen (for the same shop) in this run, or fails field
validation (see "Supported numeric grammar" and "Field validation"
below), is still recorded -- in the exceptions table, with a category and
a human-readable detail.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import html as html_lib
import io
import json
import math
import re
from datetime import date
from email.message import Message

from .models import ParsedException, ParsedOrder
from .shops import SHOPS

SYMBOL_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP"}

ORDER_ID_RE = re.compile(r"Order\s*(?:ID|Number|Ref|#)\s*:\s*([A-Za-z]{2,4}-\d{3,6})", re.IGNORECASE)
CUSTOMER_RE = re.compile(r"Customer\s*:\s*(.+)")
# Matches the whole printed value after "Order date:", however it's
# spelled -- not just a value already shaped like an ISO date. Used to
# tell "no date field printed at all" (fine; the order has no date) apart
# from "a date field is printed but its value isn't a real ISO date"
# (an `invalid_order`, not a null date) -- see `_parse_date`.
DATE_LABEL_RE = re.compile(r"Order date\s*:\s*(\S.*?)\s*$", re.MULTILINE)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# One item line, in the one line format every shop template uses. Also
# used, via `ITEM_BULLET_RE` below, to recognise a line that *looks* like
# an item bullet but doesn't fully match this shape -- see `_parse_items`.
ITEM_RE = re.compile(
    r"^\s*-\s*(.+?)\s+x(\d+)\s+@\s*([\$€£])?\s*([^\s]+)\s*(?:each)?\s*$",
    re.MULTILINE,
)
# Deliberately requires at least one whitespace char right after the
# leading "-" (matching the one space every template's real item lines
# use, "- name..."), so a decorative separator line made only of dashes
# (e.g. the forwarded-message template's "---------- Forwarded message
# ----------") is never mistaken for a malformed item bullet.
ITEM_BULLET_RE = re.compile(r"^\s*-\s+\S.*$", re.MULTILINE)
TOTAL_RE = re.compile(
    r"(?:Order total|Total charged|Amount due|Total paid)\s*:\s*"
    r"([\$€£])?\s*([^\s]+?)\s*(USD|EUR|GBP)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Supported numeric grammar for any money amount (a total or a unit
# price): plain digits, an optional single '.' decimal point followed by
# exactly two digits, no thousands separators of any kind ("," or " " or
# "."-as-thousands). This matches exactly what the generator ever prints
# (everything is formatted with Python's `:.2f`). Anything else --
# "12.3.4", "1,25", scientific notation, "inf"/"nan" -- is a rejection,
# not a guess: it becomes an `invalid_amount` exception rather than being
# silently reinterpreted or crashing the whole ingest run.
MONEY_RE = re.compile(r"^\d{1,9}\.\d{2}$")
MONEY_MAX = 1_000_000.0

# Charsets tried, in order, when a part's declared charset can't decode its
# payload. Kept short and explicit on purpose: this is a demo of handling
# the encodings the generator actually produces (see README), not a
# universal mojibake fixer.
FALLBACK_CHARSETS = ("utf-8",)


class MoneyError(ValueError):
    """Raised when a total or item price does not match `MONEY_RE`, or is
    out of the supported bounded range. Caught in `parse_eml` and turned
    into an `invalid_amount` exception row -- never left to propagate and
    abort the whole batch."""


class OrderFieldError(ValueError):
    """Raised for a recognised order whose required fields don't validate:
    contradictory total currency (symbol vs. code), no currency at all,
    a printed order-date value that isn't a real ISO calendar date, no
    item lines, an item line whose own currency symbol disagrees with the
    order's total currency, or an item bullet line that doesn't match the
    supported line format. Caught in `parse_eml` and turned into an
    `invalid_order` exception row -- never silently published as an
    ordinary clean order with a null, dropped or reinterpreted field."""


def _strip_html(s: str) -> str:
    s = re.sub(r"(?i)<(br|/p|/li|/tr|/div|/h[1-6])\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    return s


def _decode_part(part: Message) -> str | None:
    payload = part.get_payload(decode=True)
    if not payload:
        return None
    charset = part.get_content_charset() or "us-ascii"
    for cs in [charset, *[c for c in FALLBACK_CHARSETS if c != charset]]:
        try:
            return payload.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return None


def _pdf_text(payload: bytes) -> str:
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - pdfplumber is a pinned dependency
        return ""
    try:
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return ""


def _iter_body_candidate_parts(part: Message):
    """Recursively yields every non-multipart (leaf) part reachable from
    `part`, except that a part which is itself an *attachment container*
    -- `is_multipart()` and `Content-Disposition: attachment` -- is never
    descended into, so none of its own nested parts are yielded either.

    This is a hand-rolled walk rather than `Message.walk()` specifically
    because `Message.is_multipart()` (and so `walk()`) treats *any* part
    whose payload is a list as descendable -- which includes a
    `message/rfc822` attachment (an email attached to this one): its
    payload is a one-item list holding the embedded `Message`, so
    `walk()` happily descends into that attached email's own MIME tree
    and yields its body parts as if they belonged to the outer message.
    An attached email nearly always has its own decodable text/plain or
    text/html part, so that part -- not the real body -- would win the
    "first plain-text part" (or any part-order-dependent) selection.
    Pruning the whole subtree at the *container* boundary, rather than
    only skipping a leaf part whose own disposition is `attachment`, is
    what makes this correct for an attached message/rfc822 email or an
    attached multipart bundle, not just an attached single file.

    A genuine *leaf* attachment (not a container) -- e.g. a PDF receipt
    with `Content-Disposition: attachment` -- is still yielded here: this
    demo deliberately reads a PDF attachment's text as a fallback total
    source regardless of its disposition (see `_gather_text`), and a
    leaf can never itself hide further nested parts the way a container
    can. Per-content-type leaf rules (e.g. "a text/plain or text/html
    leaf whose disposition is `attachment` is not a body candidate") are
    applied by the caller, not here.
    """
    if part.is_multipart():
        if part.get_content_disposition() == "attachment":
            return
        for sub in part.get_payload():
            yield from _iter_body_candidate_parts(sub)
    else:
        yield part


def _gather_text(msg: Message) -> tuple[str | None, str, bool]:
    """Splits the message into (body_text, pdf_text, ok).

    `body_text` is the plain-text part if there is one, otherwise the
    HTML part stripped to text (never both -- a multipart/alternative
    message repeats the same content in each part, so using both would
    double every item line). `pdf_text` is the text extracted from any
    PDF attachment, kept separate since it's only ever used as a fallback
    source for the total, not for items (the item list already came from
    the body). `ok` is False only when the message has no text/plain,
    text/html or application/pdf part at all, or none of them could be
    decoded.

    Body candidates are gathered via `_iter_body_candidate_parts`, which
    prunes every attachment *container* (by Content-Disposition) at the
    boundary rather than only excluding attachment leaves -- see that
    function's docstring for why a leaf-only check misses an attached
    message/rfc822 email. On top of that, a text/plain or text/html leaf
    whose own Content-Disposition is `attachment` (e.g. an unrelated
    `notes.txt`) is never treated as a body candidate here either -- only
    the actual message body (no disposition, or `inline`) is. A PDF leaf
    is read regardless of its disposition (see above). Selection is
    structural throughout, not "whichever plain-text part comes first".
    """
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    pdf_chunks: list[str] = []
    found_content_part = False
    decoded_anything = False

    for part in _iter_body_candidate_parts(msg):
        ctype = part.get_content_type()
        disposition = part.get_content_disposition()  # 'attachment' | 'inline' | None
        if ctype in ("text/plain", "text/html") and disposition == "attachment":
            # A text attachment is not part of the message body -- it must
            # never win the body-text selection over the actual body.
            continue
        if ctype == "text/plain":
            found_content_part = True
            text = _decode_part(part)
            if text is not None:
                decoded_anything = True
                plain_chunks.append(text)
        elif ctype == "text/html":
            found_content_part = True
            text = _decode_part(part)
            if text is not None:
                decoded_anything = True
                html_chunks.append(_strip_html(text))
        elif ctype == "application/pdf":
            found_content_part = True
            payload = part.get_payload(decode=True)
            if payload:
                text = _pdf_text(payload)
                if text.strip():
                    decoded_anything = True
                    pdf_chunks.append(text)

    if not found_content_part or not decoded_anything:
        return None, "", False
    # Prefer the plain-text part when one exists (it's already the exact
    # "- name xN @ price each" line format); only fall back to the
    # HTML-stripped text when there is no plain part at all. Using both
    # would double every item line whenever a message carries both
    # (multipart/alternative bodies always do).
    body_text = "\n".join(plain_chunks) if plain_chunks else "\n".join(html_chunks)
    return body_text, "\n".join(pdf_chunks), True


def _identify_shop(msg: Message, text: str) -> str | None:
    from_addr = email.utils.parseaddr(msg.get("From", ""))[1].lower()
    for shop in SHOPS.values():
        if from_addr == shop["from_addr"].lower():
            return shop["display_name"]
    # Not sent from a known shop address (e.g. a customer forwarding a
    # confirmation from their own mailbox) -- fall back to recognising the
    # shop's own name inside the body text.
    for shop in SHOPS.values():
        if shop["display_name"] in text:
            return shop["display_name"]
    return None


def _validate_money(raw: str) -> float:
    """Validates and converts one money amount against the supported
    numeric grammar. Raises `MoneyError` -- never `ValueError` from a bare
    `float()` call -- for anything outside it."""
    if not MONEY_RE.match(raw):
        raise MoneyError(
            f"amount {raw!r} is not a supported decimal amount "
            "(expected plain digits with exactly two decimal places, "
            "e.g. '12.34'; no thousands separators of any kind)"
        )
    value = float(raw)
    if not math.isfinite(value) or value < 0 or value > MONEY_MAX:
        raise MoneyError(f"amount {raw!r} is out of the supported bounded range")
    return value


def _parse_total(text: str) -> tuple[float | None, str | None]:
    """Returns (total, currency) for the first recognised total line, or
    (None, None) if there is none. Raises `MoneyError` if the amount
    doesn't match the supported grammar, or if the printed currency
    symbol and the printed 3-letter code disagree (e.g. "$10.00 EUR")."""
    m = TOTAL_RE.search(text)
    if not m:
        return None, None
    symbol, amount, code = m.groups()
    value = _validate_money(amount)
    symbol_currency = SYMBOL_CURRENCY.get(symbol) if symbol else None
    code = code.upper() if code else None
    if code and symbol_currency and code != symbol_currency:
        raise OrderFieldError(
            f"total shows currency symbol {symbol!r} ({symbol_currency}) "
            f"but currency code {code!r}; contradictory currency"
        )
    currency = code or symbol_currency
    return value, currency


def _parse_items(text: str, order_currency: str | None) -> list[dict]:
    """Raises `MoneyError` if any item's unit price doesn't match the
    supported numeric grammar. Raises `OrderFieldError` if an item's
    printed currency symbol disagrees with the order's own currency (an
    item quietly priced in a different currency than the total is a
    contradictory order, not a detail to drop), or if any line that
    *looks* like an item bullet (starts with `- `) doesn't fully match
    the supported `- name xQty @ price each` shape -- accepting every
    line that happens to match while silently skipping the rest would
    quietly drop malformed items instead of flagging the order.
    """
    bullet_lines = ITEM_BULLET_RE.findall(text)
    items = []
    for name, qty, symbol, price in ITEM_RE.findall(text):
        value = _validate_money(price)
        item_currency = SYMBOL_CURRENCY.get(symbol) if symbol else None
        if item_currency and order_currency and item_currency != order_currency:
            raise OrderFieldError(
                f"item {name.strip()!r} is priced with currency symbol {symbol!r} "
                f"({item_currency}) but the order total is in {order_currency}; "
                "contradictory currency"
            )
        items.append({"name": name.strip(), "qty": int(qty), "unit_price": value})
    if len(items) != len(bullet_lines):
        raise OrderFieldError(
            f"found {len(bullet_lines)} item line(s) but only {len(items)} matched "
            "the supported '- name xQty @ price each' format; refusing to silently "
            "drop the rest"
        )
    return items


def _parse_date(text: str) -> str | None:
    """Returns the printed order date, or None if no "Order date:" label
    is printed at all (a missing date field is fine -- see
    `ParsedOrder.date`). Raises `OrderFieldError` if the label *is*
    printed but its value isn't a real ISO calendar date (`not-a-date`,
    `2026-99-99`, or anything else `date.fromisoformat` rejects) -- a
    printed-but-garbled date must never become a silent null date on an
    otherwise clean order.
    """
    m = DATE_LABEL_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    if not ISO_DATE_RE.match(raw):
        raise OrderFieldError(f"order date {raw!r} is not a valid ISO calendar date (YYYY-MM-DD)")
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise OrderFieldError(f"order date {raw!r} is not a valid calendar date") from None
    return raw


def parse_eml(raw: bytes, message_id: str, source: str,
              seen_order_ids: dict[tuple[str, str], str]) -> ParsedOrder | ParsedException:
    """Parses one raw .eml message into exactly one output row.

    `seen_order_ids` maps (shop, order_id) -> the message_id that first
    produced a *successfully validated* order for it in this run; it is
    mutated in place so duplicate detection works across an entire batch
    (folder or mailbox). Scoped per shop: two different shops using the
    same order-id scheme (e.g. both printing "TS-9001") are independent
    namespaces, not a collision -- see README "Limits". State is updated
    only after a message passes every validation below, so a message that
    fails validation never blocks a later, valid message with the same
    order id from being recorded.
    """
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    except Exception as exc:  # pragma: no cover - message_from_bytes is very lenient
        return ParsedException(message_id, "unparseable",
                                f"could not parse as RFC 822: {exc}", source)

    if not msg.get("From") and not msg.get("Subject"):
        return ParsedException(message_id, "unparseable",
                                "no From/Subject header found; not a recognisable email", source)

    body_text, pdf_text, ok = _gather_text(msg)
    if not ok or body_text is None:
        return ParsedException(message_id, "unparseable",
                                "no decodable text/plain, text/html or PDF content found", source)

    shop = _identify_shop(msg, body_text)
    order_id_m = ORDER_ID_RE.search(body_text)
    if shop is None or order_id_m is None:
        return ParsedException(message_id, "irrelevant",
                                "no recognised shop / order-id pattern; not an order confirmation",
                                source)

    order_id = order_id_m.group(1).upper()
    dup_key = (shop, order_id)
    if dup_key in seen_order_ids:
        return ParsedException(
            message_id, "duplicate",
            f"order id {order_id} already recorded from message "
            f"{seen_order_ids[dup_key]} (shop {shop})",
            source,
        )

    # The total is usually in the body; a couple of templates only print
    # it on a PDF receipt attachment, so that's searched second. Nothing
    # below mutates `seen_order_ids` until every check has passed.
    try:
        total, currency = _parse_total(body_text)
        if total is None and pdf_text:
            total, currency = _parse_total(pdf_text)
    except MoneyError as exc:
        return ParsedException(message_id, "invalid_amount", str(exc), source)
    except OrderFieldError as exc:
        return ParsedException(message_id, "invalid_order", str(exc), source)

    if total is None:
        return ParsedException(message_id, "missing_total",
                                f"recognised order {order_id} but found no total", source)

    if currency is None:
        return ParsedException(
            message_id, "invalid_order",
            f"order {order_id} total has no identifiable currency (no symbol or code)",
            source,
        )

    try:
        items = _parse_items(body_text, currency)
    except MoneyError as exc:
        return ParsedException(message_id, "invalid_amount", str(exc), source)
    except OrderFieldError as exc:
        return ParsedException(message_id, "invalid_order", str(exc), source)

    if not items:
        return ParsedException(
            message_id, "invalid_order",
            f"recognised order {order_id} has no parseable item lines", source,
        )

    try:
        date_value = _parse_date(body_text)
    except OrderFieldError as exc:
        return ParsedException(message_id, "invalid_order", str(exc), source)

    seen_order_ids[dup_key] = message_id
    customer_m = CUSTOMER_RE.search(body_text)

    return ParsedOrder(
        message_id=message_id,
        order_id=order_id,
        shop=shop,
        date=date_value,
        customer=customer_m.group(1).strip() if customer_m else None,
        items_json=json.dumps(items),
        item_count=len(items),
        total=total,
        currency=currency,
        source=source,
    )
