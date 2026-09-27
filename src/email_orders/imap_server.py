"""A minimal, pure-Python, in-process IMAP4 test server.

This is self-written test fixture code (not a third-party dependency), so
it carries this project's own licence, not a separate one. It implements
just enough of RFC 3501 to serve a fixed mailbox of messages to a real
`imaplib` client: greeting, CAPABILITY, LOGIN, SELECT, SEARCH ALL, FETCH
(RFC822), NOOP and LOGOUT. It always binds to 127.0.0.1 and never talks to
any real mail service or credential -- the login/password it checks are
fixed test values passed in by the caller, never a secret.

Chosen over GreenMail (Apache-2.0, Java + a container) because a pure
in-process server needs no podman image pull, no JVM, and starts/stops in
milliseconds inside the test process -- simpler for a demo whose point is
the parsing, not the mail server. See README "IMAP server" section.
"""
from __future__ import annotations

import socketserver
import threading
from dataclasses import dataclass, field


@dataclass
class _Mailbox:
    # Ordered list of (name, raw_bytes); IMAP sequence numbers are 1-based
    # positions in this list, in insertion order.
    messages: list[tuple[str, bytes]] = field(default_factory=list)


class _Handler(socketserver.StreamRequestHandler):
    server: "SyntheticIMAPServer"

    def handle(self) -> None:
        self.wfile.write(b"* OK Synthetic Test IMAP Server ready\r\n")
        selected = False
        while True:
            line = self.rfile.readline()
            if not line:
                return
            line = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line.strip():
                continue
            parts = line.split()
            tag, cmd = parts[0], parts[1].upper() if len(parts) > 1 else ""
            args = [a.strip('"') for a in parts[2:]]

            if cmd == "CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1\r\n")
                self._ok(tag, "CAPABILITY completed")
            elif cmd == "LOGIN":
                user = args[0] if args else ""
                pw = args[1] if len(args) > 1 else ""
                if (user, pw) == (self.server.user, self.server.password):
                    self._ok(tag, "LOGIN completed")
                else:
                    self._write(f"{tag} NO LOGIN failed\r\n")
            elif cmd == "SELECT":
                n = len(self.server.mailbox.messages)
                self._write(f"* {n} EXISTS\r\n* 0 RECENT\r\n")
                selected = True
                self._ok(tag, "[READ-WRITE] SELECT completed")
            elif cmd in ("SEARCH", "UID") and (cmd == "SEARCH" or (args and args[0].upper() == "SEARCH")):
                ids = " ".join(str(i + 1) for i in range(len(self.server.mailbox.messages)))
                self._write(f"* SEARCH {ids}\r\n")
                self._ok(tag, "SEARCH completed")
            elif cmd == "FETCH":
                self._handle_fetch(tag, args)
            elif cmd == "NOOP":
                self._ok(tag, "NOOP completed")
            elif cmd == "LOGOUT":
                self._write("* BYE synthetic server closing\r\n")
                self._ok(tag, "LOGOUT completed")
                return
            else:
                self._write(f"{tag} BAD unrecognised command\r\n")

    def _handle_fetch(self, tag: str, args: list[str]) -> None:
        if not args:
            self._write(f"{tag} BAD FETCH needs a sequence set\r\n")
            return
        spec = args[0]
        ids: list[int] = []
        for chunk in spec.split(","):
            if ":" in chunk:
                lo, hi = chunk.split(":")
                ids.extend(range(int(lo), int(hi) + 1))
            else:
                ids.append(int(chunk))
        for seq in ids:
            if seq < 1 or seq > len(self.server.mailbox.messages):
                continue
            _, raw = self.server.mailbox.messages[seq - 1]
            header = f"* {seq} FETCH (RFC822 {{{len(raw)}}}\r\n".encode("ascii")
            self.wfile.write(header)
            self.wfile.write(raw)
            self.wfile.write(b")\r\n")
        self._ok(tag, "FETCH completed")

    def _ok(self, tag: str, text: str) -> None:
        self._write(f"{tag} OK {text}\r\n")

    def _write(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8", errors="replace"))


class SyntheticIMAPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, user: str, password: str, port: int = 0):
        # Bound to 127.0.0.1 only, on an OS-assigned free port unless one
        # is given -- never reachable from outside this machine.
        super().__init__(("127.0.0.1", port), _Handler)
        self.user = user
        self.password = password
        self.mailbox = _Mailbox()
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    def load_messages(self, messages: list[tuple[str, bytes]]) -> None:
        """`messages` is a list of (name, raw_bytes), in the order they
        should be assigned ascending IMAP sequence numbers."""
        self.mailbox.messages = list(messages)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # BaseServer.shutdown() blocks waiting for serve_forever()'s loop to
        # notice the shutdown flag -- if serve_forever() was never started
        # (start() was never called), that loop doesn't exist and
        # shutdown() would hang forever.
        if self._thread is not None:
            self.shutdown()
            self._thread.join(timeout=5)
        self.server_close()
