"""End-to-end tests: the local test IMAP server, CSV/SQLite output, and a
subprocess smoke test of the CLI itself.
"""
import csv
import sqlite3
import subprocess
import sys
from pathlib import Path

from email_orders.cli import TEST_IMAP_PASSWORD, TEST_IMAP_USER, run_ingest
from email_orders.imap_server import SyntheticIMAPServer
from email_orders.imap_source import read_folder, read_imap
from email_orders.storage import current_dir, write_outputs

SRC = Path(__file__).resolve().parent.parent / "src"


def test_imap_server_binds_to_localhost_only():
    server = SyntheticIMAPServer(TEST_IMAP_USER, TEST_IMAP_PASSWORD)
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.port > 0
    finally:
        server.stop()


def test_imap_source_matches_folder_source(corpus):
    gt, eml_dir = corpus
    folder_messages = read_folder(eml_dir)

    server = SyntheticIMAPServer(TEST_IMAP_USER, TEST_IMAP_PASSWORD)
    server.load_messages(folder_messages)
    server.start()
    try:
        imap_messages = read_imap("127.0.0.1", server.port, TEST_IMAP_USER,
                                   TEST_IMAP_PASSWORD, "INBOX")
    finally:
        server.stop()

    assert len(imap_messages) == len(folder_messages)

    f_orders, f_exceptions = run_ingest(folder_messages, "folder")
    i_orders, i_exceptions = run_ingest(imap_messages, "imap")

    f_totals = sorted((o.order_id, o.total, o.currency) for o in f_orders)
    i_totals = sorted((o.order_id, o.total, o.currency) for o in i_orders)
    assert f_totals == i_totals
    assert len(f_exceptions) == len(i_exceptions)


def test_imap_login_rejects_wrong_credentials():
    import imaplib

    server = SyntheticIMAPServer(TEST_IMAP_USER, TEST_IMAP_PASSWORD)
    server.start()
    try:
        conn = imaplib.IMAP4("127.0.0.1", server.port)
        try:
            typ, _ = conn.login("wrong-user", "wrong-password")
        except imaplib.IMAP4.error:
            typ = "NO"
        assert typ != "OK"
    finally:
        server.stop()


def test_storage_writes_matching_csv_and_sqlite(tmp_path, corpus):
    gt, eml_dir = corpus
    messages = read_folder(eml_dir)
    orders, exceptions = run_ingest(messages, "folder")
    out_dir = tmp_path / "output"
    write_outputs(orders, exceptions, out_dir)
    live = current_dir(out_dir)

    with open(live / "orders.csv", newline="", encoding="utf-8") as fh:
        csv_orders = list(csv.DictReader(fh))
    with open(live / "exceptions.csv", newline="", encoding="utf-8") as fh:
        csv_exceptions = list(csv.DictReader(fh))
    assert len(csv_orders) == len(orders)
    assert len(csv_exceptions) == len(exceptions)

    conn = sqlite3.connect(live / "orders.db")
    try:
        db_orders = conn.execute("SELECT order_id, total, currency FROM orders").fetchall()
        db_exceptions = conn.execute("SELECT message_id, category FROM exceptions").fetchall()
    finally:
        conn.close()
    assert len(db_orders) == len(orders)
    assert len(db_exceptions) == len(exceptions)

    csv_order_ids = {r["order_id"] for r in csv_orders}
    db_order_ids = {r[0] for r in db_orders}
    assert csv_order_ids == db_order_ids


def test_cli_demo_subprocess_smoke(tmp_path):
    out = tmp_path / "data"
    proc = subprocess.run(
        [sys.executable, "-m", "email_orders", "demo", "--out", str(out), "--seed", "42"],
        cwd=Path(__file__).resolve().parent.parent,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK: folder-sourced and IMAP-sourced orders match exactly." in proc.stdout
    assert (current_dir(out / "output-folder") / "orders.csv").exists()
    assert (current_dir(out / "output-folder") / "exceptions.csv").exists()
    assert (current_dir(out / "output-folder") / "orders.db").exists()
    assert (current_dir(out / "output-imap") / "orders.csv").exists()
