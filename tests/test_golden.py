"""Golden-set regression tests (API-marked: run with WH_RUN_API_TESTS=1).

Uses real sale photos preserved in wh_data/golden/ (from the 2026-07-13 M0
validation run). Guards stage-1 recall against prompt/model changes:
photo 4960104/022.jpg contains the uncatalogued signed oil sketch that
motivated this whole build — if detection ever misses it, fail loudly.
"""

import os
from pathlib import Path

import pytest

GOLDEN = Path(__file__).resolve().parent.parent / "wh_data" / "golden"
API = pytest.mark.skipif(
    os.environ.get("WH_RUN_API_TESTS") != "1",
    reason="set WH_RUN_API_TESTS=1 (spends API tokens)")

# (relative path, minimum artworks stage-1 must find)
MUST_DETECT = [
    ("4960104/022.jpg", 1),   # the "Frederic Remington"-signed oil sketch
    ("4960104/000.jpg", 3),   # dense poster/memorabilia wall (truncation regression)
    ("4984500/010.jpg", 1),   # framed abstract in gold frame
]


@pytest.fixture(scope="module")
def client():
    import anthropic
    from wallhunter.config import anthropic_api_key
    os.environ.setdefault("ANTHROPIC_API_KEY", anthropic_api_key())
    return anthropic.Anthropic()


@API
@pytest.mark.parametrize("rel,min_works", MUST_DETECT)
def test_stage1_recall_floor(client, rel, min_works):
    from PIL import Image, ImageOps
    from wallhunter.config import STAGE1_MAX_EDGE, CostMeter
    from wallhunter.images import downscale_jpeg_b64
    from wallhunter.stage1 import _detect_one

    path = GOLDEN / rel
    if not path.exists():
        pytest.skip(f"golden photo missing: {rel}")
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    parsed, _cost = _detect_one(client, CostMeter(1.0),
                                downscale_jpeg_b64(img, STAGE1_MAX_EDGE))
    assert len(parsed.get("artworks", [])) >= min_works, (
        f"{rel}: expected >= {min_works} artworks, got {parsed.get('artworks')}")


@API
def test_stage1_signature_flag_on_remington_sketch(client):
    from PIL import Image, ImageOps
    from wallhunter.config import STAGE1_MAX_EDGE, CostMeter
    from wallhunter.images import downscale_jpeg_b64
    from wallhunter.stage1 import _detect_one

    path = GOLDEN / "4960104/022.jpg"
    if not path.exists():
        pytest.skip("golden photo missing")
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    parsed, _ = _detect_one(client, CostMeter(1.0),
                            downscale_jpeg_b64(img, STAGE1_MAX_EDGE))
    assert any(a.get("sig_visible") for a in parsed["artworks"]), (
        "signature flag lost on the signed oil sketch")
