#!/usr/bin/env bash
# One-command demo run: generate the synthetic .eml corpus, then ingest it
# both from the filesystem and from a local, in-process test IMAP server,
# and confirm the two sources agree. Assumes the venv is already created
# and activated (see README "Setup"), or falls back to system python3.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

OUT="${1:-data}"

python3 -m email_orders demo --out "$OUT" --seed 42

echo
echo "Done. See $OUT/output-folder/ and $OUT/output-imap/ for"
echo "orders.csv, exceptions.csv and orders.db (SQLite)."
