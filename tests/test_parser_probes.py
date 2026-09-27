"""Regression tests wired from Astra's 2026-09-27 review probes
(~/vault/40-sessions/2026-09-27-astra-email-orders-to-table-review.md and
the accompanying -probes.py/-probes.json). Only import paths and pytest
plumbing were adapted from the probe script; every scenario below is the
exact same input the probe used. The assertions encode the *fixed*,
intended behaviour (the probe script itself only recorded the old, buggy
behaviour into JSON -- it made no claims about what's correct).

Money-grammar findings (bad_decimal, comma_decimal, bad_item_price):
a malformed amount must never crash the batch and must never be silently
reinterpreted (e.g. "1,25" must not become 125.0) -- it's a rejection,
categorised `invalid_amount`.

Field-validation findings (missing_currency, conflicting_currency,
invalid_date, missing_items): a recognised order with an invalid or
missing required field must not be published as an ordinary clean order
-- it's a rejection, categorised `invalid_order`.

MIME-selection finding (attachment_as_body): a complete HTML order body
must not be discarded in favour of an unrelated text/plain attachment.

Shop-scoping finding (same_id_different_shop): the same order id used by
two different shops must not suppress the second shop's legitimate order.

Publication findings (missing_source_cli, failed_publication): a missing
source folder must not be read as "zero messages" and must not overwrite
a previous good output; a failed publish (e.g. SQLite open failure) must
leave the previous good CSV/DB snapshot exactly as it was.
"""
import os
import subprocess
import sys
from dataclasses import asdict
from email.message import EmailMessage
from pathlib import Path

import pytest

from email_orders import imap_source, storage
from email_orders.cli import run_ingest
from email_orders.models import ParsedException, ParsedOrder
from email_orders.parser import parse_eml
from email_orders.shops import SHOPS

# Paths adapted from ~/vault/40-sessions/2026-09-27-astra-email-orders-to-table-
# rereview-probes.py (the 2026-09-27 re-review follow-up): that probe script
# opened the published files directly under the output directory; storage.py
# now publishes generations behind a `current` symlink (see storage.write_outputs),
# so every probe below reads through `storage.current_dir(...)` instead. The
# scenarios and assertions are otherwise the re-review's own follow-up cases.

SHOP_LIST = list(SHOPS.values())


def _body(total="$10.00 USD", date="2026-08-01", items="- Widget x1 @ $10.00 each"):
    return (
        f"Order ID: TS-9001\nOrder date: {date}\nCustomer: Synthetic\n"
        f"{items}\nOrder total: {total}\n"
    )


def _message(body: str, shop: int = 0) -> EmailMessage:
    m = EmailMessage()
    m["From"] = SHOP_LIST[shop]["from_addr"]
    m["Subject"] = "Synthetic order"
    m.set_content(body)
    return m


# -- Money grammar: rejected, not crashed, not silently reinterpreted --

def test_bad_decimal_total_is_invalid_amount_not_a_crash():
    raw = _message(_body("12.3.4 USD")).as_bytes()
    result = parse_eml(raw, "bad_decimal", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "invalid_amount"


def test_comma_decimal_total_is_rejected_not_reinterpreted():
    raw = _message(_body("1,25 EUR")).as_bytes()
    result = parse_eml(raw, "comma_decimal", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "invalid_amount"


def test_bad_item_price_is_invalid_amount_not_a_crash():
    raw = _message(_body(items="- Widget x1 @ $1.2.3 each")).as_bytes()
    result = parse_eml(raw, "bad_item_price", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "invalid_amount"


# -- Field validation: rejected, not labelled an ordinary clean order --

def test_missing_currency_is_invalid_order_not_clean():
    raw = _message(_body("10.00")).as_bytes()
    result = parse_eml(raw, "missing_currency", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "invalid_order"


def test_conflicting_currency_is_invalid_order_not_silently_resolved():
    raw = _message(_body("$10.00 EUR")).as_bytes()
    result = parse_eml(raw, "conflicting_currency", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "invalid_order"


def test_invalid_calendar_date_is_invalid_order():
    raw = _message(_body(date="2026-99-99")).as_bytes()
    result = parse_eml(raw, "invalid_date", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "invalid_order"


def test_missing_items_is_invalid_order_not_item_count_zero():
    raw = _message(_body(items="")).as_bytes()
    result = parse_eml(raw, "missing_items", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "invalid_order"


# -- MIME selection: HTML body must not lose to an unrelated attachment --

def test_html_body_survives_unrelated_text_attachment():
    m = EmailMessage()
    m["From"] = SHOP_LIST[0]["from_addr"]
    m["Subject"] = "Synthetic order"
    m.set_content(_body().replace("\n", "<br>"), subtype="html")
    m.add_attachment("Unrelated attachment notes", subtype="plain", filename="notes.txt")
    result = parse_eml(m.as_bytes(), "html-with-text-attachment", "folder", {})
    assert isinstance(result, ParsedOrder), asdict(result) if isinstance(result, ParsedException) else result
    assert result.order_id == "TS-9001"
    assert result.total == 10.0
    assert result.currency == "USD"
    assert result.item_count == 1


def test_nested_multipart_alternative_inside_mixed_ignores_attachment():
    """multipart/mixed( multipart/alternative(plain, html), text-attachment )
    -- the real order must come from the alternative part; the sibling
    attachment (with different, unrelated order-looking text) must never
    be mixed in."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(_body(), "plain"))
    alt.attach(MIMEText(_body().replace("\n", "<br>"), "html"))

    outer = MIMEMultipart("mixed")
    outer["From"] = SHOP_LIST[0]["from_addr"]
    outer["Subject"] = "Synthetic order"
    outer.attach(alt)
    unrelated = MIMEText(
        "Order ID: ZZ-0000\nOrder date: 2020-01-01\nCustomer: Nobody\n"
        "- Ghost x9 @ $999.00 each\nOrder total: $8991.00 USD\n",
        "plain",
    )
    unrelated.add_header("Content-Disposition", "attachment", filename="unrelated.txt")
    outer.attach(unrelated)

    result = parse_eml(outer.as_bytes(), "nested-with-attachment", "folder", {})
    assert isinstance(result, ParsedOrder)
    assert result.order_id == "TS-9001"
    assert result.total == 10.0
    assert result.item_count == 1


def test_attachment_only_message_is_not_mistaken_for_an_order():
    """A message with no text/plain or text/html body at all (only a PDF
    attachment) must not crash and must not fabricate an order from
    attachment content it was never supposed to read for items/order-id."""
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart

    outer = MIMEMultipart("mixed")
    outer["From"] = SHOP_LIST[0]["from_addr"]
    outer["Subject"] = "Synthetic order"
    pdf_part = MIMEApplication(b"%PDF-1.4 not a real pdf but bytes", _subtype="pdf")
    pdf_part.add_header("Content-Disposition", "attachment", filename="receipt.pdf")
    outer.attach(pdf_part)

    result = parse_eml(outer.as_bytes(), "pdf-attachment-only", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category in ("irrelevant", "unparseable")


# -- Shop scoping: same order id, two different shops --

def test_same_order_id_different_shops_both_recorded():
    orders, exceptions = run_ingest(
        [
            ("shop-a", _message(_body(), 0).as_bytes()),
            ("shop-b", _message(_body(), 1).as_bytes()),
        ],
        "folder",
    )
    assert len(orders) == 2
    assert exceptions == []
    assert {o.shop for o in orders} == {SHOP_LIST[0]["display_name"], SHOP_LIST[1]["display_name"]}


def test_same_order_id_same_shop_is_still_a_duplicate():
    orders, exceptions = run_ingest(
        [
            ("shop-a-1", _message(_body(), 0).as_bytes()),
            ("shop-a-2", _message(_body(), 0).as_bytes()),
        ],
        "folder",
    )
    assert len(orders) == 1
    assert len(exceptions) == 1
    assert exceptions[0].category == "duplicate"


# -- Publication: missing source, and a failed publish, must not destroy
# -- the last good snapshot --

def test_missing_source_folder_cli_fails_and_preserves_previous_output(tmp_path):
    old = parse_eml(_message(_body()).as_bytes(), "old", "folder", {})
    storage.write_outputs([old], [], tmp_path)
    live = storage.current_dir(tmp_path)
    before_db = (live / "orders.db").read_bytes()
    before_csv_rows = (live / "orders.csv").read_text().splitlines()

    with pytest.raises(FileNotFoundError):
        imap_source.read_folder(tmp_path / "absent")

    proc = subprocess.run(
        [sys.executable, "-m", "email_orders", "ingest", "--source", "folder",
         "--input", str(tmp_path / "absent"), "--out", str(tmp_path)],
        cwd=Path(__file__).resolve().parent.parent,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0
    # The previous good snapshot must be untouched.
    assert (live / "orders.db").read_bytes() == before_db
    assert (live / "orders.csv").read_text().splitlines() == before_csv_rows


def test_failed_sqlite_publish_preserves_previous_good_snapshot(tmp_path, monkeypatch):
    old = parse_eml(_message(_body()).as_bytes(), "old", "folder", {})
    storage.write_outputs([old], [], tmp_path)
    live = storage.current_dir(tmp_path)
    before_target = os.readlink(live)
    # Compare the database's actual bytes/content, not just that a file
    # named orders.db exists -- an empty or truncated file would also
    # "exist".
    before_db_bytes = (live / "orders.db").read_bytes()
    before_csv_rows = (live / "orders.csv").read_text().splitlines()
    assert len(before_csv_rows) == 2  # header + 1 order

    def fail(*a, **k):
        raise OSError("injected sqlite-open failure")

    monkeypatch.setattr(storage.sqlite3, "connect", fail)
    with pytest.raises(OSError):
        storage.write_outputs([], [], tmp_path)

    # `current` was never repointed -- the failure happened while building
    # the new generation, before the one commit point (the symlink swap).
    assert os.readlink(live) == before_target
    assert (live / "orders.db").read_bytes() == before_db_bytes
    assert (live / "orders.csv").read_text().splitlines() == before_csv_rows
    # The half-built new generation is cleaned up, not left as a stray
    # directory that a later publish's cleanup pass would have to handle.
    assert list((tmp_path / storage.GENERATIONS_DIR).iterdir()) == [
        (tmp_path / storage.GENERATIONS_DIR / Path(before_target).name)
    ]


def test_failed_generation_switch_preserves_previous_publication(tmp_path, monkeypatch):
    """Publication's one commit point is the `os.replace` that repoints
    `current` onto the new, fully-written generation. Injecting a failure
    there (simulating a crash mid-rename) must leave the previous
    generation exactly as it was and `current` still resolving to it --
    this is the case a three-separate-file-replace scheme could not give:
    there is now exactly one commit point to fail at, not three."""
    old = parse_eml(_message(_body()).as_bytes(), "old", "folder", {})
    storage.write_outputs([old], [], tmp_path)
    live = storage.current_dir(tmp_path)
    before_target = os.readlink(live)
    before_db = (live / "orders.db").read_bytes()
    before_csv_rows = (live / "orders.csv").read_text().splitlines()

    real_replace = storage.os.replace

    def fail_on_symlink_swap(src, dst):
        if str(src).startswith(str(tmp_path / ".current-")):
            raise OSError("injected generation-switch failure")
        return real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", fail_on_symlink_swap)
    new = parse_eml(_message(_body(total="$20.00 USD")).as_bytes(), "new", "folder", {})
    with pytest.raises(OSError):
        storage.write_outputs([new], [], tmp_path)

    assert os.readlink(live) == before_target
    assert (live / "orders.db").read_bytes() == before_db
    assert (live / "orders.csv").read_text().splitlines() == before_csv_rows


# -- 2026-09-27 re-review follow-up (rereview.md findings 2 and 3) --
# Scenarios adapted (paths only, per the module docstring) from
# ~/vault/40-sessions/2026-09-27-astra-email-orders-to-table-rereview-probes.py.

def test_garbled_printed_date_is_invalid_order_not_a_null_date():
    """'Order date: not-a-date' must not become a clean order with a null
    date -- the label is printed, so its value must be a real ISO date or
    the order is rejected."""
    raw = _message(_body(date="not-a-date")).as_bytes()
    result = parse_eml(raw, "bad_date_text", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "invalid_order"


def test_item_currency_conflicting_with_total_is_invalid_order():
    """An item priced in euros under a dollar total must not be accepted
    with the item's currency silently dropped."""
    raw = _message(_body(items="- Widget x1 @ €10.00 each")).as_bytes()
    result = parse_eml(raw, "item_currency_conflict", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "invalid_order"


def test_one_malformed_item_line_rejects_the_whole_order():
    """One valid item plus one item line that doesn't match the supported
    format ('- Other xBAD @ $2.00 each') must not silently drop the
    malformed line and publish the order with only the valid item."""
    raw = _message(
        _body(items="- Widget x1 @ $10.00 each\n- Other xBAD @ $2.00 each")
    ).as_bytes()
    result = parse_eml(raw, "partial_items", "folder", {})
    assert isinstance(result, ParsedException)
    assert result.category == "invalid_order"


def test_attached_rfc822_message_never_overrides_the_real_html_body():
    """A complete HTML order plus an attached, unrelated .eml message must
    still parse from the real HTML body -- the attached email's own
    plain-text child must never be selected instead."""
    m = EmailMessage()
    m["From"] = SHOP_LIST[0]["from_addr"]
    m["Subject"] = "Synthetic order"
    m.set_content(_body().replace("\n", "<br>"), subtype="html")
    attached = EmailMessage()
    attached["From"] = "someone@example.invalid"
    attached["Subject"] = "Unrelated"
    attached.set_content("Unrelated attached email body")
    m.add_attachment(attached)

    result = parse_eml(m.as_bytes(), "attached_message_overrides_body", "folder", {})
    assert isinstance(result, ParsedOrder), asdict(result) if isinstance(result, ParsedException) else result
    assert result.order_id == "TS-9001"
    assert result.total == 10.0
    assert result.item_count == 1
