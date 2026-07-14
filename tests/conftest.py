"""Test config: isolate WH data in a temp dir BEFORE wallhunter imports."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="wh_test_")
os.environ["WH_DATA_DIR"] = _TMP

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402


@pytest.fixture()
def conn():
    from wallhunter import db
    c = db.connect()
    yield c
    # wipe between tests (shared temp DB)
    for table in ("events", "work_detections", "works", "detections",
                  "photos", "sales", "runs"):
        c.execute(f"DELETE FROM {table}")
    c.commit()
    c.close()


@pytest.fixture()
def tiny_jpeg() -> bytes:
    import io
    from PIL import Image
    img = Image.new("RGB", (64, 48), (120, 90, 60))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()
