"""Fetches raw .eml bytes from a folder, or from an IMAP mailbox.

The IMAP path talks to a real IMAP server over the wire via the standard
library's `imaplib` -- in this demo that server is always the local,
in-process `imap_server.SyntheticIMAPServer`, bound to 127.0.0.1. Nothing
here ever points at a real mailbox, and no credential is read from the
secret store: the demo's test IMAP account is a fixed, throwaway
user/password baked into `run_demo.py` / the CLI defaults, good for
nothing but talking to the local test server.
"""
from __future__ import annotations

import imaplib
from pathlib import Path


def read_folder(path: str | Path) -> list[tuple[str, bytes]]:
    """Returns (message_id, raw_bytes) for every `*.eml` file in `path`,
    sorted by filename so results are deterministic across runs."""
    folder = Path(path)
    out = []
    for f in sorted(folder.glob("*.eml")):
        out.append((f.stem, f.read_bytes()))
    return out


def read_imap(host: str, port: int, user: str, password: str,
              mailbox: str = "INBOX") -> list[tuple[str, bytes]]:
    """Returns (message_id, raw_bytes) for every message in `mailbox` on a
    real IMAP server reached over `imaplib`. `message_id` here is a
    sequence-based label (`imap-msg-<n>`), since IMAP itself doesn't expose
    the original filename -- exactly what a real Gmail/Outlook mailbox
    would give you too."""
    conn = imaplib.IMAP4(host, port)
    try:
        conn.login(user, password)
        conn.select(mailbox)
        typ, data = conn.search(None, "ALL")
        if typ != "OK":
            raise RuntimeError(f"IMAP SEARCH failed: {typ} {data}")
        ids = data[0].split()
        out = []
        for num in ids:
            typ, msg_data = conn.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or msg_data[0] is None:
                raise RuntimeError(f"IMAP FETCH failed for {num!r}: {typ}")
            raw = msg_data[0][1]
            out.append((f"imap-msg-{num.decode()}", raw))
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass
