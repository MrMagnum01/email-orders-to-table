# email-orders-to-table

Parses order-confirmation emails into a structured orders table: order id,
shop, date, customer, items, total and currency. Anything that isn't a
clean order confirmation -- unreadable, missing its total, a repeat send,
or just not an order at all -- is written to a separate `exceptions.csv`
instead of being silently dropped.

**Everything in this repo is synthetic.** The included generator produces
13 fake order-confirmation emails (`.eml` files) from 4 fictional shops
with a fixed random seed, so the whole pipeline can be demoed and tested
without any real mailbox or client data. No code, data, shop template, or
name here is copied from any client or employer project.

## Upwork job types this demonstrates

- "Extract data from emails to spreadsheet"
- "Email parsing" / "email data extraction"
- "Gmail/Outlook automation" (the *reading-a-mailbox* part specifically --
  see "IMAP server" below for exactly what that does and doesn't mean here)

This demo reads from a **local test IMAP server only** -- it never connects
to Gmail, Outlook, or any real mailbox, and no real credential is used
anywhere in it.

## What it does

1. `generate` -- builds a deterministic corpus of synthetic
   order-confirmation `.eml` files across 4 fictional shops, varying:
   - **body format**: multipart (plain + HTML), HTML-only, and
     plain-text-only messages;
   - **encoding**: quoted-printable, base64, and a non-UTF-8 charset
     (windows-1252, e.g. a `£` amount and a curly apostrophe);
   - a **forwarded message** (a customer forwards their confirmation, so
     the shop's own address is gone from `From:` and the content is
     wrapped in a forwarding preamble);
   - a **PDF attachment** whose total is only readable from the PDF's own
     text, not the email body;
   - a **duplicate** resend of an existing order id;
   - an order confirmation with its **total line missing** (template bug);
   - two **irrelevant** emails (a newsletter and a marketing blast, one
     from a real shop address, one not);
   - one **structurally broken** file (no headers, undecodable body).
2. `ingest --source folder|imap` -- reads every message from a directory
   of `.eml` files, or from a mailbox on an IMAP server, and writes:
   - `orders.csv` + `orders.db` (SQLite) -- one row per order: message id,
     order id, shop, date, customer, items (JSON), item count, total,
     currency, and which source it came from.
   - `exceptions.csv` (also in `orders.db`) -- one row per problem:
     category (`unparseable` / `missing_total` / `duplicate` /
     `invalid_amount` / `invalid_order` / `irrelevant`) and a
     human-readable detail.
3. `demo` -- runs the whole thing once: generates the corpus, ingests it
   from the folder, then starts the local test IMAP server (loaded with
   the same messages), ingests it again over real IMAP, and confirms both
   sources produce exactly the same orders before shutting the server
   down.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## One-command run

```bash
./run_demo.sh
```

Writes the synthetic corpus to `data/eml/`, then both output sets to
`data/output-folder/` and `data/output-imap/` (identical, from the two
different sources). `data/` is git-ignored -- it's generated, not source.

## CLI, step by step

```bash
export PYTHONPATH=src

python -m email_orders generate --out data/eml --seed 42
python -m email_orders ingest --source folder --input data/eml --out data/output-folder
python -m email_orders demo --out data --seed 42   # folder + IMAP in one go
```

## Tests

```bash
source .venv/bin/activate
export PYTHONPATH=src
pytest tests -v
```

30/30 passing:
- **Known-total reconciliation** (`test_generator_ground_truth.py`) -- the
  generator's own record of every order and total it wrote is compared
  against the parser's output, exactly -- including every item's name,
  quantity and unit price, not just the item count -- plus a check that
  every one of the 13 messages lands in exactly one of the two tables
  (nothing dropped, nothing double-counted) and that all four
  generator-corpus exception categories appear.
- **Failure-class tests** (`test_parser_failures.py`) -- one hand-built
  input per bad-input class (empty bytes, header-less binary junk, missing
  total, duplicate order id, irrelevant-from-a-known-shop, irrelevant from
  an unknown sender), independent of the generator's own corpus, plus one
  good-path sanity check.
- **Probe-derived regression tests** (`test_parser_probes.py`) -- one test
  per finding from the 2026-09-27 independent review: malformed/comma
  decimals and bad item prices are rejected as `invalid_amount`, not
  crashed on or silently reinterpreted; missing/contradictory currency, an
  invalid calendar date, and zero item lines are rejected as
  `invalid_order`, not published as an ordinary clean order; a complete
  HTML body is not displaced by an unrelated text attachment, including
  through nested multipart and an attachment-only message; the same order
  id used by two different shops is two legitimate orders, not one order
  plus a spurious duplicate; a missing source folder fails the CLI rather
  than silently ingesting as empty, and a failed publish (e.g. a SQLite
  open failure) never destroys the previous good CSV/SQLite snapshot.
- **Pipeline tests** (`test_pipeline.py`) -- the local IMAP server binds to
  127.0.0.1 only and rejects a wrong login; IMAP-sourced and
  folder-sourced results match exactly; CSV and SQLite outputs agree with
  each other and with the in-memory results; a subprocess smoke test runs
  the actual `demo` CLI end to end.

## IMAP server

Per the brief, this needed either GreenMail (Apache-2.0, Java, run in a
container) or a pure-Python in-process test server if simpler. **This repo
uses a small, self-written, pure-Python in-process IMAP4 server**
(`src/email_orders/imap_server.py`) instead of GreenMail: it needs no
container image pull and no JVM, starts and stops in milliseconds inside
the test process, and the demo's point is the parsing, not the mail
server. It implements just enough of RFC 3501 for a real `imaplib` client
to log in, select a mailbox, search, and fetch full messages -- always
bound to `127.0.0.1`, checked against a fixed throwaway test
login/password (`demo-tester` / a hardcoded non-secret string), and never
pointed at any real mailbox or credential. `demo`/`ingest --source imap`
never read anything from the secret store.

## How parsing works

- Each message's readable text is gathered from whichever it has: the
  plain-text part if present, otherwise the HTML part with tags stripped
  to text; a PDF attachment (if any) is extracted separately via
  `pdfplumber` and used only as a fallback source for the total. Body-text
  candidates are selected structurally by MIME disposition: a text/plain
  or text/html part whose `Content-Disposition` is `attachment` (e.g. an
  unrelated `notes.txt`) is never treated as the body, even if it's the
  only text/plain part in the message and even inside nested multipart
  structures.
- **Shop identification**: by the `From:` address first; if that doesn't
  match a known shop (e.g. a forwarded message, now sent from the
  customer's own address), by the shop's display name appearing anywhere
  in the body text.
- **Fields** (order id, date, customer, total) are pulled by regex against
  a handful of label variants used across the 4 shop templates ("Order
  ID:" / "Order number:" / "Order Ref:" / "Order #:", and similarly for
  the total line) -- deliberately not just one fixed label, since four
  different shop templates use four different words for the same field.
- **Items** are parsed from a single `- name xQty @ price each` line
  format, shared by every template's plain-text rendition (and reproduced
  inside the `<li>` items of HTML-only bodies) -- see "Limits" below.
- **Supported numeric grammar**: a total or unit price must be plain
  digits with exactly two decimal places (`12.34`) -- no thousands
  separators, no comma decimal points, no scientific notation. Anything
  else is rejected as `invalid_amount` rather than crashing the ingest run
  or being silently reinterpreted (`1,25` is not guessed to mean `1.25`;
  it's rejected).
- **Field validation**: a recognised order must have an identifiable,
  non-contradictory currency (a printed symbol and a printed 3-letter code
  that disagree, e.g. `$10.00 EUR`, are rejected, not resolved in favour
  of one of them), a real calendar date if a date is printed at all, and
  at least one parseable item line. Any of these failing is flagged
  `invalid_order`, never published as an ordinary clean order with a null
  or zero-valued field.
- **Duplicate detection** is per ingest run and scoped per shop: the first
  message to claim an order id *for a given shop* wins the `orders` row;
  a later message with the same order id from the *same* shop is flagged
  `duplicate`. The same order id used by two different shops is two
  independent, legitimate orders -- this demo's shops don't share an
  order-id namespace, and nothing here assumes a client's shops would
  either. Duplicate state is only recorded after a message passes every
  other validation, so a message that fails validation never blocks a
  later, valid message with the same order id.
- **Missing total**: if no total is found in the body or any PDF
  attachment, the message is flagged `missing_total`, not guessed at or
  dropped.
- **Irrelevant**: if no known shop can be identified, or no order-id
  pattern is found, it's flagged `irrelevant` rather than force-fit into
  an order row.
- **Unparseable**: a message with no `From`/`Subject` header at all, or no
  text/HTML/PDF content that could be decoded, is flagged `unparseable`.
- **Publication**: `ingest` reads the whole source before writing anything;
  a missing/unreadable folder or a failed IMAP fetch fails the CLI (exit
  code 2) rather than being read as "zero messages". Output is staged in a
  temporary directory and published (one atomic file replace per output)
  only once every output has been written successfully -- a failure
  partway through publishing (e.g. the SQLite file can't be opened) leaves
  the previous run's `orders.csv` / `exceptions.csv` / `orders.db`
  untouched instead of replacing them with an empty or partial snapshot.

## Limits

- Item parsing assumes one shared line format (`- name xQty @ price
  each`) across every shop template. A real client's shop emails would
  very likely need their own item-line pattern studied and added; this
  demo shows the field-extraction and exception-handling approach, not a
  universal email-layout parser.
- Only one attachment type is handled: PDF, via text extraction (no OCR).
  Any other attachment type is currently just ignored by the parser (its
  body text, if any, is still used) rather than raising its own exception
  category -- a real engagement would very likely need that distinction
  made explicit.
- Currency is read from the printed 3-letter code or the `$`/`€`/`£`
  symbol; there is no FX conversion anywhere in this project.
- Duplicate-order-id detection is scoped to one `ingest` run's input
  (a folder, or a mailbox snapshot at fetch time) *and* to one shop within
  that run, not a persistent cross-run registry and not a cross-shop
  global namespace.
- The IMAP server is a minimal test fixture (`LOGIN`, `SELECT`, `SEARCH
  ALL`, `FETCH (RFC822)`, `LOGOUT`), not a general-purpose mail server --
  it exists to prove the ingestion code speaks real IMAP, not to replace
  GreenMail for anything beyond this demo.

## Project layout

```
src/email_orders/
  shops.py        fictional shop/customer/product name pools
  models.py       shared dataclasses (generator ground truth + parser output)
  generator.py    builds the deterministic synthetic .eml corpus
  parser.py       parses one message into an order row or an exception row
  imap_server.py  minimal in-process IMAP4 test server (self-written, see above)
  imap_source.py  reads messages from a folder, or fetches them over IMAP
  storage.py      writes orders/exceptions to CSV and SQLite
  cli.py          `generate` / `ingest` / `demo` subcommands
tests/            pytest suite (ground truth, failure classes, pipeline + IMAP + CLI)
LICENSES.md       every open-source library used and its licence
```

## Role

AI-assisted build: an AI coding agent (Claude) wrote this repository's
code, tests and generator from a brief set by this profile's owner, who
specified the requirements (parse order emails to a table; cover multiple
formats/encodings/attachment types; include a local test IMAP path),
reviewed the resulting design, and verified its behaviour by running and
extending the automated test suite. No personal, pre-existing
email-parsing engagement is claimed -- this is a synthetic capability
demo, not a record of past client work.
