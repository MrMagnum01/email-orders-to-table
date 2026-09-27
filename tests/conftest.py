import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from email_orders.generator import generate  # noqa: E402


@pytest.fixture()
def corpus(tmp_path):
    eml_dir = tmp_path / "eml"
    gt = generate(eml_dir, seed=42)
    return gt, eml_dir
