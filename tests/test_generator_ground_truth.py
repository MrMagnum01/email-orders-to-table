"""Known-total reconciliation: the generator records the true orders and
totals it wrote into the .eml corpus, and the parser's output must match
them exactly -- order for order, cent for cent, and every deliberately-bad
message must land in exceptions under the category the generator intended.
"""
from email_orders.cli import run_ingest
from email_orders.imap_source import read_folder


def test_orders_match_generator_ground_truth(corpus):
    gt, eml_dir = corpus
    messages = read_folder(eml_dir)
    orders, exceptions = run_ingest(messages, "folder")

    assert len(messages) == len(gt.files) == len(orders) + len(exceptions)

    by_order_id = {o.order_id: o for o in orders}
    expected_order_ids = {spec.order_id for spec in gt.orders}
    assert set(by_order_id) == expected_order_ids

    for spec in gt.orders:
        got = by_order_id[spec.order_id]
        assert got.shop == spec.shop
        assert got.date == spec.date
        assert got.customer == spec.customer
        assert got.currency == spec.currency
        assert got.total == spec.total, f"{spec.order_id}: {got.total} != {spec.total}"
        assert got.item_count == len(spec.items)


def test_exceptions_match_generator_intent(corpus):
    gt, eml_dir = corpus
    messages = read_folder(eml_dir)
    orders, exceptions = run_ingest(messages, "folder")

    by_message_id = {e.message_id: e for e in exceptions}
    expected = {spec.message_id: spec.category for spec in gt.exceptions}
    assert set(by_message_id) == set(expected)
    for message_id, expected_category in expected.items():
        assert by_message_id[message_id].category == expected_category, message_id


def test_every_message_lands_exactly_once(corpus):
    """Nothing is silently skipped or double-counted."""
    gt, eml_dir = corpus
    messages = read_folder(eml_dir)
    orders, exceptions = run_ingest(messages, "folder")

    order_message_ids = {o.message_id for o in orders}
    exception_message_ids = {e.message_id for e in exceptions}
    assert not (order_message_ids & exception_message_ids)
    assert order_message_ids | exception_message_ids == set(gt.files)


def test_all_four_exception_categories_present(corpus):
    gt, eml_dir = corpus
    messages = read_folder(eml_dir)
    _, exceptions = run_ingest(messages, "folder")
    categories = {e.category for e in exceptions}
    assert categories == {"unparseable", "missing_total", "duplicate", "irrelevant"}
