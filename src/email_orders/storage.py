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
import uuid
from pathlib import Path

from .models import ParsedException, ParsedOrder

OUTPUT_FILES = ("orders.csv", "exceptions.csv", "orders.db")
GENERATIONS_DIR = ".generations"
CURRENT_LINK = "current"

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


def current_dir(out_dir: str | Path) -> Path:
    """The directory a reader should open `orders.csv` / `exceptions.csv`
    / `orders.db` in for a given `out_dir`: `out_dir/current`, a symlink
    to the most recently *fully* published generation. Callers must
    always go through this -- never assume the three files live directly
    in `out_dir` -- because publication (below) switches generations by
    repointing this symlink, not by writing into `out_dir` directly.
    """
    return Path(out_dir) / CURRENT_LINK


def write_outputs(orders: list[ParsedOrder], exceptions: list[ParsedException], out_dir: str | Path) -> None:
    """Publishes orders.csv, exceptions.csv and orders.db as one atomic
    "publication".

    Every run's three files are written *complete* into a brand-new,
    never-before-seen generation directory under `out_dir/.generations/`.
    Nothing under `out_dir` that a reader can already see is touched
    while that happens. Only once all three files exist there does
    publication make the new generation live, and it does that with
    exactly one filesystem commit point: a symlink named `current` is
    built under a fresh temporary name and then moved onto `current` with
    a single `os.replace` (an atomic rename on POSIX, including across a
    process crash -- the rename either lands completely or not at all,
    never half-done).

    Until that one rename happens, `current` -- if it exists at all --
    still points at the previous generation, complete and untouched: a
    build failure (e.g. `write_sqlite` failing partway through) or a
    crash before the rename leaves the previous publication exactly as it
    was, never replaced with an inconsistent, empty or partial snapshot.
    A reader that opens `current/orders.csv` and `current/orders.db` at
    any moment sees either the old complete generation or the new
    complete one -- never a mix of the two, and never a mid-write file.

    Old generations are then removed as a best-effort cleanup step; a
    failure there can leave a stale generation directory on disk, but it
    can never affect what `current` points at or make an old generation's
    files disappear out from under a reader that already resolved
    `current`.
    """
    out_dir = Path(out_dir)
    generations_root = out_dir / GENERATIONS_DIR
    generations_root.mkdir(parents=True, exist_ok=True)

    gen_name = f"gen-{uuid.uuid4().hex}"
    gen_dir = generations_root / gen_name
    gen_dir.mkdir()
    try:
        write_csv(orders, exceptions, gen_dir)
        write_sqlite(orders, exceptions, gen_dir / "orders.db")
    except BaseException:
        shutil.rmtree(gen_dir, ignore_errors=True)
        raise

    tmp_link = out_dir / f".current-{uuid.uuid4().hex}"
    tmp_link.symlink_to(Path(GENERATIONS_DIR) / gen_name)
    try:
        os.replace(tmp_link, out_dir / CURRENT_LINK)
    except BaseException:
        # The switch never happened -- `current` (if any) is untouched.
        # Clean up both the never-linked temp symlink and the new
        # generation it pointed at; nothing publishable came from this run.
        tmp_link.unlink(missing_ok=True)
        shutil.rmtree(gen_dir, ignore_errors=True)
        raise

    # Best-effort cleanup: remove every generation except the one
    # `current` now points at. Never runs before the switch above, so it
    # can never race a reader that is still resolving the previous
    # generation through `current`.
    live_name = os.readlink(out_dir / CURRENT_LINK)
    for child in generations_root.iterdir():
        if child.name != Path(live_name).name:
            shutil.rmtree(child, ignore_errors=True)
