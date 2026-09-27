"""CLI: `generate`, `ingest` (folder or IMAP), and `demo` (both, one shot)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import imap_source
from .generator import DEFAULT_SEED, generate as generate_corpus
from .imap_server import SyntheticIMAPServer
from .models import ParsedOrder
from .parser import parse_eml
from .storage import write_outputs

TEST_IMAP_USER = "demo-tester"
TEST_IMAP_PASSWORD = "demo-only-not-a-secret"  # local test server only; never a real credential


def run_ingest(messages: list[tuple[str, bytes]], source: str):
    orders, exceptions = [], []
    seen_order_ids: dict[str, str] = {}
    for message_id, raw in messages:
        result = parse_eml(raw, message_id, source, seen_order_ids)
        if isinstance(result, ParsedOrder):
            orders.append(result)
        else:
            exceptions.append(result)
    return orders, exceptions


def cmd_generate(args: argparse.Namespace) -> None:
    corpus = generate_corpus(args.out, seed=args.seed)
    print(f"Generated {len(corpus.files)} .eml files in {args.out} "
          f"({len(corpus.orders)} orders, {len(corpus.exceptions)} deliberately-bad emails).")


def cmd_ingest(args: argparse.Namespace) -> None:
    try:
        if args.source == "folder":
            messages = imap_source.read_folder(args.input)
        else:
            messages = imap_source.read_imap(args.host, args.port, args.user, args.password, args.mailbox)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        # A missing/unreadable source is an ingest failure, not an empty
        # mailbox -- never write_outputs() here, or a previous good
        # snapshot would be silently replaced with zero orders/exceptions.
        print(f"error: could not read {args.source} source: {exc}", file=sys.stderr)
        sys.exit(2)
    orders, exceptions = run_ingest(messages, args.source)
    write_outputs(orders, exceptions, args.out)
    print(f"[{args.source}] {len(messages)} messages -> {len(orders)} orders, "
          f"{len(exceptions)} exceptions. Written to {args.out}/current/")


def cmd_demo(args: argparse.Namespace) -> None:
    out_root = Path(args.out)
    eml_dir = out_root / "eml"
    folder_out = out_root / "output-folder"
    imap_out = out_root / "output-imap"

    corpus = generate_corpus(eml_dir, seed=args.seed)
    print(f"generate: {len(corpus.files)} .eml files in {eml_dir}")

    folder_messages = imap_source.read_folder(eml_dir)
    f_orders, f_exceptions = run_ingest(folder_messages, "folder")
    write_outputs(f_orders, f_exceptions, folder_out)
    print(f"folder ingest: {len(folder_messages)} messages -> "
          f"{len(f_orders)} orders, {len(f_exceptions)} exceptions ({folder_out}/current/)")

    server = SyntheticIMAPServer(TEST_IMAP_USER, TEST_IMAP_PASSWORD)
    # Same messages, same order, served over a real (local-only) IMAP
    # connection this time instead of the filesystem.
    server.load_messages(folder_messages)
    server.start()
    try:
        imap_messages = imap_source.read_imap(
            "127.0.0.1", server.port, TEST_IMAP_USER, TEST_IMAP_PASSWORD, "INBOX",
        )
        i_orders, i_exceptions = run_ingest(imap_messages, "imap")
        write_outputs(i_orders, i_exceptions, imap_out)
        print(f"imap ingest (127.0.0.1:{server.port}): {len(imap_messages)} messages -> "
              f"{len(i_orders)} orders, {len(i_exceptions)} exceptions ({imap_out}/current/)")
    finally:
        server.stop()

    folder_totals = sorted((o.order_id, round(o.total, 2)) for o in f_orders)
    imap_totals = sorted((o.order_id, round(o.total, 2)) for o in i_orders)
    if folder_totals == imap_totals:
        print("OK: folder-sourced and IMAP-sourced orders match exactly.")
    else:
        print("MISMATCH between folder and IMAP sourced orders:", file=sys.stderr)
        print(f"  folder: {folder_totals}", file=sys.stderr)
        print(f"  imap:   {imap_totals}", file=sys.stderr)
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="email_orders")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write the synthetic .eml corpus")
    g.add_argument("--out", default="data/eml")
    g.add_argument("--seed", type=int, default=DEFAULT_SEED)
    g.set_defaults(func=cmd_generate)

    i = sub.add_parser("ingest", help="parse .eml messages into orders/exceptions tables")
    i.add_argument("--source", choices=["folder", "imap"], required=True)
    i.add_argument("--input", help="folder of .eml files (source=folder)")
    i.add_argument("--host", default="127.0.0.1")
    i.add_argument("--port", type=int)
    i.add_argument("--user", default=TEST_IMAP_USER)
    i.add_argument("--password", default=TEST_IMAP_PASSWORD)
    i.add_argument("--mailbox", default="INBOX")
    i.add_argument("--out", default="data/output")
    i.set_defaults(func=cmd_ingest)

    d = sub.add_parser("demo", help="generate + ingest via folder AND a local test IMAP server")
    d.add_argument("--out", default="data")
    d.add_argument("--seed", type=int, default=DEFAULT_SEED)
    d.set_defaults(func=cmd_demo)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
