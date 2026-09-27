"""Builds a deterministic corpus of synthetic order-confirmation .eml files.

Every shop, customer, product, order id and total here is invented for
this demo (see `shops.py`). Nothing is copied from any real mailbox,
client, or employer project. The corpus mixes:

- plain-text-only, HTML-only, and multipart (plain+HTML) bodies,
- quoted-printable, base64, and a non-UTF-8 charset (windows-1252),
- a forwarded message,
- a PDF attachment whose total is only readable from the PDF text,
- a duplicate order-id resend, a missing-total confirmation, two
  irrelevant (non-order) emails, and one structurally broken file.

Run standalone with `python -m email_orders generate --out data/eml`.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from email.charset import BASE64, QP, Charset
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import format_datetime
from io import BytesIO
from pathlib import Path

from reportlab.pdfgen import canvas

from .models import ExceptionSpec, GeneratedCorpus, LineItem, OrderSpec
from .shops import CUSTOMERS, IRRELEVANT_SUBJECTS, PRODUCTS, SHOPS

DEFAULT_SEED = 42


def _charset(name: str, encoding: str) -> Charset:
    cs = Charset(name)
    cs.body_encoding = QP if encoding == "quoted-printable" else BASE64
    cs.header_encoding = QP if encoding == "quoted-printable" else BASE64
    return cs


def _text_part(text: str, subtype: str, charset_name: str, encoding: str) -> MIMEText:
    # Pass the Charset object straight into the constructor so encoding
    # happens in one pass. Building a plain MIMEText(text, subtype) first
    # and calling .set_charset() afterwards double-encodes the payload
    # (the constructor's own default-charset pass already transfer-encodes
    # it once) and corrupts anything outside ASCII.
    return MIMEText(text, subtype, _charset(charset_name, encoding))


def _pick_items(rng: random.Random, n: int) -> list[LineItem]:
    chosen = rng.sample(PRODUCTS, n)
    items = []
    for name, price in chosen:
        qty = rng.randint(1, 2)
        items.append(LineItem(name=name, qty=qty, unit_price=price))
    return items


def _total(items: list[LineItem]) -> float:
    return round(sum(i.qty * i.unit_price for i in items), 2)


def _item_lines(items: list[LineItem], symbol: str) -> str:
    return "\n".join(
        f"  - {i.name} x{i.qty} @ {symbol}{i.unit_price:.2f} each" for i in items
    )


def _item_list_items(items: list[LineItem], symbol: str) -> str:
    # Leading "- " kept inside the <li> text itself (not just bullet CSS)
    # so that stripping tags down to plain text reproduces the exact same
    # "- name xN @ price each" line the plain-text bodies use, and one
    # regex in the parser covers both.
    return "\n".join(
        f"<li>- {i.name} x{i.qty} @ {symbol}{i.unit_price:.2f} each</li>" for i in items
    )


def _demo_header(msg, message_id: str) -> None:
    # Test-only traceability header: never used by the parser's business
    # logic, only by the test suite to reconcile parser output against
    # generator ground truth without guessing at message ordering.
    msg["X-Demo-Message-Id"] = message_id


def _base_headers(msg, from_addr: str, from_name: str, subject: str, when: datetime, to_addr: str = "you@customer-inbox.invalid") -> None:
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to_addr
    msg["Date"] = format_datetime(when)


@dataclass
class Built:
    message_id: str
    filename: str
    raw: bytes


def build_northbridge_order(message_id: str, order_id: str, when: datetime,
                             customer: str, items: list[LineItem],
                             include_total: bool) -> Built:
    shop = SHOPS["northbridge"]
    total = _total(items)
    date_str = when.date().isoformat()
    total_line_txt = f"\nOrder total: {shop['symbol']}{total:.2f} USD\n" if include_total else "\n"
    total_line_html = (
        f"<p><strong>Order total: {shop['symbol']}{total:.2f} USD</strong></p>"
        if include_total else ""
    )
    plain = (
        f"Hi {customer.split()[0]},\n\n"
        f"Thanks for your order from {shop['display_name']}!\n\n"
        f"Order ID: {order_id}\n"
        f"Order date: {date_str}\n"
        f"Customer: {customer}\n\n"
        f"Items:\n{_item_lines(items, shop['symbol'])}\n"
        f"{total_line_txt}\n"
        f"We'll email you when it ships.\n"
    )
    html = (
        f"<html><body><p>Hi {customer.split()[0]},</p>"
        f"<p>Thanks for your order from {shop['display_name']}!</p>"
        f"<p>Order ID: {order_id}<br>Order date: {date_str}<br>Customer: {customer}</p>"
        f"<ul>{_item_list_items(items, shop['symbol'])}</ul>"
        f"{total_line_html}"
        f"<p>We'll email you when it ships.</p></body></html>"
    )
    msg = MIMEMultipart("alternative")
    _base_headers(msg, shop["from_addr"], shop["display_name"],
                  f"Your {shop['display_name']} order confirmation — {order_id}", when)
    _demo_header(msg, message_id)
    msg.attach(_text_part(plain, "plain", "us-ascii", "quoted-printable"))
    msg.attach(_text_part(html, "html", "us-ascii", "quoted-printable"))
    return Built(message_id, f"{message_id}.eml", msg.as_bytes())


def build_lumen_html_order(message_id: str, order_id: str, when: datetime,
                            customer: str, items: list[LineItem]) -> Built:
    shop = SHOPS["lumen"]
    total = _total(items)
    date_str = when.date().isoformat()
    html = (
        f"<html><body><p>Hello {customer.split()[0]},</p>"
        f"<p>Your {shop['display_name']} order is confirmed.</p>"
        f"<p>Order number: {order_id}<br>Order date: {date_str}<br>Customer: {customer}</p>"
        f"<ul>{_item_list_items(items, shop['symbol'])}</ul>"
        f"<p><strong>Total charged: {shop['symbol']}{total:.2f}</strong></p>"
        f"<p>Merci for shopping with {shop['display_name']}.</p></body></html>"
    )
    msg = _text_part(html, "html", "utf-8", "base64")
    _base_headers(msg, shop["from_addr"], shop["display_name"],
                  f"Your {shop['display_name']} order confirmation", when)
    _demo_header(msg, message_id)
    return Built(message_id, f"{message_id}.eml", msg.as_bytes())


def build_lumen_forwarded_order(message_id: str, order_id: str, when: datetime,
                                 customer: str, items: list[LineItem]) -> Built:
    """Customer forwards the (plain-text rendition of the) confirmation."""
    shop = SHOPS["lumen"]
    total = _total(items)
    date_str = when.date().isoformat()
    original_from = f"{shop['display_name']} <{shop['from_addr']}>"
    forwarded_body = (
        f"Hi Sam,\n\ncan you check this order landed at the right address?\n\n"
        f"---------- Forwarded message ----------\n"
        f"From: {original_from}\n"
        f"Date: {format_datetime(when)}\n"
        f"Subject: Your {shop['display_name']} order confirmation\n"
        f"To: you@customer-inbox.invalid\n\n"
        f"Hello {customer.split()[0]},\n\n"
        f"Your {shop['display_name']} order is confirmed.\n\n"
        f"Order number: {order_id}\n"
        f"Order date: {date_str}\n"
        f"Customer: {customer}\n\n"
        f"Items:\n{_item_lines(items, shop['symbol'])}\n\n"
        f"Total charged: {shop['symbol']}{total:.2f}\n"
    )
    msg = _text_part(forwarded_body, "plain", "utf-8", "quoted-printable")
    _base_headers(msg, "personal.forwarder@customer-inbox.invalid", customer,
                  f"Fwd: Your {shop['display_name']} order confirmation", when)
    _demo_header(msg, message_id)
    return Built(message_id, f"{message_id}.eml", msg.as_bytes())


def build_cedarfern_order(message_id: str, order_id: str, when: datetime,
                           customer: str, items: list[LineItem],
                           resend_note: bool = False) -> Built:
    shop = SHOPS["cedarfern"]
    total = _total(items)
    date_str = when.date().isoformat()
    note = (
        "This is a resend of your confirmation — no action needed.\n\n"
        if resend_note else ""
    )
    plain = (
        f"{note}"
        f"Dear {customer},\n\n"
        f"Thank you for shopping at {shop['display_name']}. "
        f"Here’s your confirmation.\n\n"
        f"Order Ref: {order_id}\n"
        f"Order date: {date_str}\n"
        f"Customer: {customer}\n\n"
        f"Items:\n{_item_lines(items, shop['symbol'])}\n\n"
        f"Amount due: {shop['symbol']}{total:.2f}\n\n"
        f"Regards,\n{shop['display_name']}\n"
    )
    msg = _text_part(plain, "plain", "windows-1252", "quoted-printable")
    _base_headers(msg, shop["from_addr"], shop["display_name"],
                  f"{shop['display_name']} — order {order_id} confirmed", when)
    _demo_header(msg, message_id)
    return Built(message_id, f"{message_id}.eml", msg.as_bytes())


def _make_pdf_receipt(order_id: str, shop_display: str, customer: str,
                       items: list[LineItem], total: float, symbol: str) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(420, 520))
    y = 490
    c.setFont("Helvetica-Bold", 14)
    c.drawString(30, y, f"{shop_display} — Receipt")
    y -= 28
    c.setFont("Helvetica", 10)
    c.drawString(30, y, f"Order #: {order_id}")
    y -= 16
    c.drawString(30, y, f"Customer: {customer}")
    y -= 24
    for it in items:
        c.drawString(30, y, f"- {it.name} x{it.qty} @ {symbol}{it.unit_price:.2f} each")
        y -= 16
    y -= 10
    c.setFont("Helvetica-Bold", 11)
    c.drawString(30, y, f"Total paid: {symbol}{total:.2f}")
    c.showPage()
    c.save()
    return buf.getvalue()


def build_pico_pdf_order(message_id: str, order_id: str, when: datetime,
                          customer: str, items: list[LineItem]) -> Built:
    shop = SHOPS["pico"]
    total = _total(items)
    date_str = when.date().isoformat()
    plain = (
        f"Hi {customer.split()[0]},\n\n"
        f"Your {shop['display_name']} order is confirmed.\n\n"
        f"Order #: {order_id}\n"
        f"Order date: {date_str}\n"
        f"Customer: {customer}\n\n"
        f"Items:\n{_item_lines(items, shop['symbol'])}\n\n"
        f"Your total will appear on the attached receipt.\n"
    )
    msg = MIMEMultipart("mixed")
    _base_headers(msg, shop["from_addr"], shop["display_name"],
                  f"Your {shop['display_name']} order confirmation — {order_id}", when)
    _demo_header(msg, message_id)
    msg.attach(_text_part(plain, "plain", "us-ascii", "quoted-printable"))
    pdf_bytes = _make_pdf_receipt(order_id, shop["display_name"], customer, items, total, shop["symbol"])
    pdf_part = MIMEApplication(pdf_bytes, _subtype="pdf")
    pdf_part.add_header("Content-Disposition", "attachment", filename=f"receipt-{order_id}.pdf")
    msg.attach(pdf_part)
    return Built(message_id, f"{message_id}.eml", msg.as_bytes())


def build_pico_plain_order(message_id: str, order_id: str, when: datetime,
                            customer: str, items: list[LineItem]) -> Built:
    shop = SHOPS["pico"]
    total = _total(items)
    date_str = when.date().isoformat()
    plain = (
        f"Hi {customer.split()[0]},\n\n"
        f"Your {shop['display_name']} order is confirmed.\n\n"
        f"Order #: {order_id}\n"
        f"Order date: {date_str}\n"
        f"Customer: {customer}\n\n"
        f"Items:\n{_item_lines(items, shop['symbol'])}\n\n"
        f"Total paid: {shop['symbol']}{total:.2f}\n\n"
        f"Thanks for shopping with {shop['display_name']}.\n"
    )
    msg = _text_part(plain, "plain", "utf-8", "quoted-printable")
    _base_headers(msg, shop["from_addr"], shop["display_name"],
                  f"Your {shop['display_name']} order confirmation — {order_id}", when)
    _demo_header(msg, message_id)
    return Built(message_id, f"{message_id}.eml", msg.as_bytes())


def build_irrelevant(message_id: str, shop_display: str, from_addr: str,
                      subject: str, when: datetime) -> Built:
    plain = (
        f"{shop_display} newsletter\n\n"
        f"Check out what's new this season. Reply STOP to unsubscribe.\n"
    )
    msg = _text_part(plain, "plain", "utf-8", "quoted-printable")
    _base_headers(msg, from_addr, shop_display, subject, when)
    _demo_header(msg, message_id)
    return Built(message_id, f"{message_id}.eml", msg.as_bytes())


def build_garbage(message_id: str) -> Built:
    """Structurally broken input: no headers, no readable text encoding."""
    import os
    raw = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01" + os.urandom(180)
    return Built(message_id, f"{message_id}.eml", raw)


def generate(out_dir: str | Path, seed: int = DEFAULT_SEED) -> GeneratedCorpus:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    corpus = GeneratedCorpus()
    base_when = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)

    def day(n: int) -> datetime:
        from datetime import timedelta
        return base_when + timedelta(days=n)

    built: list[Built] = []

    # -- Northbridge: multipart alternative, quoted-printable, USD --
    c1 = _pick_items(rng, 2)
    b = build_northbridge_order("northbridge_ord_1001", "NB-1001", day(1),
                                 CUSTOMERS[0], c1, include_total=True)
    built.append(b)
    corpus.orders.append(OrderSpec(b.message_id, "NB-1001", "Northbridge Outfitters",
                                    day(1).date().isoformat(), CUSTOMERS[0], c1, "USD", _total(c1)))

    c2 = _pick_items(rng, 3)
    b = build_northbridge_order("northbridge_ord_1002", "NB-1002", day(2),
                                 CUSTOMERS[1], c2, include_total=True)
    built.append(b)
    corpus.orders.append(OrderSpec(b.message_id, "NB-1002", "Northbridge Outfitters",
                                    day(2).date().isoformat(), CUSTOMERS[1], c2, "USD", _total(c2)))

    # -- Northbridge: missing total (template bug) --
    c3 = _pick_items(rng, 2)
    b = build_northbridge_order("northbridge_missing_total", "NB-1003", day(3),
                                 CUSTOMERS[2], c3, include_total=False)
    built.append(b)
    corpus.exceptions.append(ExceptionSpec(b.message_id, "missing_total",
                                            "order NB-1003 body never prints a total"))

    # -- Lumen: HTML-only, base64, EUR --
    c4 = _pick_items(rng, 2)
    b = build_lumen_html_order("lumen_ord_2001", "LC-2001", day(4), CUSTOMERS[3], c4)
    built.append(b)
    corpus.orders.append(OrderSpec(b.message_id, "LC-2001", "Lumen & Co",
                                    day(4).date().isoformat(), CUSTOMERS[3], c4, "EUR", _total(c4)))

    # -- Lumen: forwarded plain-text rendition --
    c5 = _pick_items(rng, 2)
    b = build_lumen_forwarded_order("lumen_ord_2002_fwd", "LC-2002", day(5), CUSTOMERS[4], c5)
    built.append(b)
    corpus.orders.append(OrderSpec(b.message_id, "LC-2002", "Lumen & Co",
                                    day(5).date().isoformat(), CUSTOMERS[4], c5, "EUR", _total(c5)))

    # -- Cedar Fern: plain-only, windows-1252, GBP --
    c6 = _pick_items(rng, 2)
    b = build_cedarfern_order("cedarfern_ord_3001", "CF-3001", day(6), CUSTOMERS[5], c6)
    built.append(b)
    corpus.orders.append(OrderSpec(b.message_id, "CF-3001", "Cedar Fern Market",
                                    day(6).date().isoformat(), CUSTOMERS[5], c6, "GBP", _total(c6)))

    c7 = _pick_items(rng, 3)
    b = build_cedarfern_order("cedarfern_ord_3002", "CF-3002", day(7), CUSTOMERS[6], c7)
    built.append(b)
    corpus.orders.append(OrderSpec(b.message_id, "CF-3002", "Cedar Fern Market",
                                    day(7).date().isoformat(), CUSTOMERS[6], c7, "GBP", _total(c7)))

    # -- Cedar Fern: duplicate resend of CF-3002 --
    b = build_cedarfern_order("cedarfern_ord_3002_dup", "CF-3002", day(7), CUSTOMERS[6], c7,
                               resend_note=True)
    built.append(b)
    corpus.exceptions.append(ExceptionSpec(b.message_id, "duplicate",
                                            "resend of order CF-3002 (see cedarfern_ord_3002)"))

    # -- Pico & Bramble: PDF attachment carries the total --
    c8 = _pick_items(rng, 2)
    b = build_pico_pdf_order("pico_ord_4001", "PB-4001", day(8), CUSTOMERS[7], c8)
    built.append(b)
    corpus.orders.append(OrderSpec(b.message_id, "PB-4001", "Pico & Bramble",
                                    day(8).date().isoformat(), CUSTOMERS[7], c8, "USD", _total(c8)))

    c9 = _pick_items(rng, 2)
    b = build_pico_plain_order("pico_ord_4002", "PB-4002", day(9), CUSTOMERS[0], c9)
    built.append(b)
    corpus.orders.append(OrderSpec(b.message_id, "PB-4002", "Pico & Bramble",
                                    day(9).date().isoformat(), CUSTOMERS[0], c9, "USD", _total(c9)))

    # -- Irrelevant (non-order) emails --
    for idx, (shop_display, from_addr, subject) in enumerate(IRRELEVANT_SUBJECTS):
        b = build_irrelevant(f"irrelevant_{idx+1}", shop_display, from_addr, subject, day(10 + idx))
        built.append(b)
        corpus.exceptions.append(ExceptionSpec(b.message_id, "irrelevant",
                                                f"newsletter/marketing, not an order: {subject!r}"))

    # -- Structurally broken file --
    b = build_garbage("garbage_1")
    built.append(b)
    corpus.exceptions.append(ExceptionSpec(b.message_id, "unparseable",
                                            "not a valid RFC 822 message (no headers, undecodable body)"))

    for b in built:
        (out / b.filename).write_bytes(b.raw)
        corpus.files[b.message_id] = b.filename

    return corpus
