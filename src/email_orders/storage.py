"""Writes parsed orders/exceptions to CSV and to a SQLite database.

Both outputs come from the same in-memory rows, so they never disagree.
SQLite (stdlib `sqlite3`) is used rather than DuckDB to keep the
dependency list at zero for this step -- no extra library to license or
install.
"""
from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .models import ParsedException, ParsedOrder

OUTPUT_FILES = ("orders.csv", "exceptions.csv", "orders.db")

ORDER_FIELDS = ["message_id", "order_id", "shop", "date", "customer",
                "items_json", "item_count", "total", "currency", "source"]
EXCEPTION_FIELDS = ["message_id", "category", "detail", "source"]


def write_csv(orders: list[ParsedOrder], exceptions: list[ParsedException], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "orders.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(ORDER_FIELDS)
        for o in orders:
            w.writerow([getattr(o, f) for f in ORDER_FIELDS])
    with open(out_dir / "exceptions.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(EXCEPTION_FIELDS)
        for e in exceptions:
            w.writerow([getattr(e, f) for f in EXCEPTION_FIELDS])


def write_sqlite(orders: list[ParsedOrder], exceptions: list[ParsedException], db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE orders (
                message_id TEXT PRIMARY KEY, order_id TEXT, shop TEXT, date TEXT,
                customer TEXT, items_json TEXT, item_count INTEGER,
                total REAL, currency TEXT, source TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE exceptions (
                message_id TEXT, category TEXT, detail TEXT, source TEXT
            )"""
        )
        conn.executemany(
            f"INSERT INTO orders VALUES ({','.join('?' * len(ORDER_FIELDS))})",
            [tuple(getattr(o, f) for f in ORDER_FIELDS) for o in orders],
        )
        conn.executemany(
            f"INSERT INTO exceptions VALUES ({','.join('?' * len(EXCEPTION_FIELDS))})",
            [tuple(getattr(e, f) for f in EXCEPTION_FIELDS) for e in exceptions],
        )
        conn.commit()
    finally:
        conn.close()


def write_outputs(orders: list[ParsedOrder], exceptions: list[ParsedException], out_dir: str | Path) -> None:
    """Writes orders.csv, exceptions.csv and orders.db as one atomic
    "publication": everything is written into a staging directory first,
    and only if all three writes succeed are they published over the
    previous outputs, one `os.replace` (atomic on POSIX same-filesystem
    renames) per file. If any staged write fails -- including a failure
    partway through `write_sqlite` -- the staging directory is discarded
    and the previous good outputs (if any) are left exactly as they were.
    A run's outputs are never partially overwritten with an inconsistent
    or empty snapshot.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=out_dir))
    try:
        write_csv(orders, exceptions, staging)
        write_sqlite(orders, exceptions, staging / "orders.db")
        for name in OUTPUT_FILES:
            os.replace(staging / name, out_dir / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
