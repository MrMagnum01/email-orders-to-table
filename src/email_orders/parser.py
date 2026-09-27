"""Parses raw .eml bytes into a `ParsedOrder` or a `ParsedException`.

Every message produces exactly one row in exactly one of the two output
tables. Nothing is dropped: a message that isn't a recognisable order
confirmation, can't be decoded at all, is missing its total, or repeats an
order id already seen in this run, is still recorded — in the exceptions
table, with a category and a human-readable detail.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import html as html_lib
import io
import json
import re
from email.message import Message

from .models import ParsedException, ParsedOrder
from .shops import SHOPS

SYMBOL_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP"}

ORDER_ID_RE = re.compile(r"Order\s*(?:ID|Number|Ref|#)\s*:\s*([A-Za-z]{2,4}-\d{3,6})", re.IGNORECASE)
CUSTOMER_RE = re.compile(r"Customer\s*:\s*(.+)")
DATE_RE = re.compile(r"Order date\s*:\s*(\d{4}-\d{2}-\d{2})")
ITEM_RE = re.compile(
    r"^\s*-\s*(.+?)\s+x(\d+)\s+@\s*([\$€£])?\s*([\d.]+)\s*(?:each)?\s*$",
    re.MULTILINE,
)
TOTAL_RE = re.compile(
    r"(?:Order total|Total charged|Amount due|Total paid)\s*:\s*"
    r"([\$€£])?\s*([\d.,]+)\s*(USD|EUR|GBP)?",
    re.IGNORECASE,
)

# Charsets tried, in order, when a part's declared charset can't decode its
# payload. Kept short and explicit on purpose: this is a demo of handling
# the encodings the generator actually produces (see README), not a
# universal mojibake fixer.
FALLBACK_CHARSETS = ("utf-8",)


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
    """
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    pdf_chunks: list[str] = []
    found_content_part = False
    decoded_anything = False

    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
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


def _parse_total(text: str) -> tuple[float | None, str | None]:
    m = TOTAL_RE.search(text)
    if not m:
        return None, None
    symbol, amount, code = m.groups()
    value = float(amount.replace(",", ""))
    currency = code or SYMBOL_CURRENCY.get(symbol)
    return value, currency


def _parse_items(text: str) -> list[dict]:
    return [
        {"name": name.strip(), "qty": int(qty), "unit_price": float(price)}
        for name, qty, _symbol, price in ITEM_RE.findall(text)
    ]


def parse_eml(raw: bytes, message_id: str, source: str,
              seen_order_ids: dict[str, str]) -> ParsedOrder | ParsedException:
    """Parses one raw .eml message into exactly one output row.

    `seen_order_ids` maps order_id -> the message_id that first produced a
    successful order for it in this run; it is mutated in place so
    duplicate detection works across an entire batch (folder or mailbox).
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
    if order_id in seen_order_ids:
        return ParsedException(
            message_id, "duplicate",
            f"order id {order_id} already recorded from message {seen_order_ids[order_id]}",
            source,
        )

    # The total is usually in the body; a couple of templates only print
    # it on a PDF receipt attachment, so that's searched second.
    total, currency = _parse_total(body_text)
    if total is None and pdf_text:
        total, currency = _parse_total(pdf_text)
    if total is None:
        return ParsedException(message_id, "missing_total",
                                f"recognised order {order_id} but found no total", source)

    seen_order_ids[order_id] = message_id
    customer_m = CUSTOMER_RE.search(body_text)
    date_m = DATE_RE.search(body_text)
    items = _parse_items(body_text)

    return ParsedOrder(
        message_id=message_id,
        order_id=order_id,
        shop=shop,
        date=date_m.group(1) if date_m else None,
        customer=customer_m.group(1).strip() if customer_m else None,
        items_json=json.dumps(items),
        item_count=len(items),
        total=total,
        currency=currency,
        source=source,
    )
