"""Configuration: paths, models, prices, cost caps, API key resolution."""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WH_DATA_DIR", REPO_ROOT / "wh_data"))
DB_PATH = DATA_DIR / "wallhunter.db"
IMAGE_DIR = DATA_DIR / "images"
REPORT_DIR = DATA_DIR / "reports"

STAGE1_MODEL = os.environ.get("WH_STAGE1_MODEL", "claude-haiku-4-5-20251001")
STAGE2_MODEL = os.environ.get("WH_STAGE2_MODEL", "claude-sonnet-5")

# USD per million tokens (input, output) — used with actual response usage
MODEL_PRICES = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
}

RUN_COST_CAP_USD = float(os.environ.get("WH_RUN_COST_CAP_USD", "5"))
RATE_LIMIT_SECONDS = float(os.environ.get("WH_RATE_LIMIT_SECONDS", "0.5"))
STAGE1_MAX_EDGE = 1024      # px, longest side sent to detection
STAGE2_CROP_MAX_EDGE = 1024
STAGE2_CONTEXT_MAX_EDGE = 512
DEDUPE_MAX_HAMMING = 8      # dhash distance at/below which detections merge
CROP_PAD_FRACTION = 0.05

TIER_A_MIN = 7.5
TIER_B_MIN = 5.0

_KEY_FALLBACKS = [
    REPO_ROOT / ".env",
    Path.home() / "Desktop/williams-art-engine/.env",
    Path.home() / "art-scout/.env",
]


def anthropic_api_key() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    for env_path in _KEY_FALLBACKS:
        if env_path.exists():
            load_dotenv(env_path, override=False)
            if os.environ.get("ANTHROPIC_API_KEY"):
                return os.environ["ANTHROPIC_API_KEY"]
    raise SystemExit(
        "ANTHROPIC_API_KEY not set and not found in any fallback .env "
        f"({', '.join(str(p) for p in _KEY_FALLBACKS)})"
    )


def ensure_dirs():
    for d in (DATA_DIR, IMAGE_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)


class CostCapExceeded(Exception):
    pass


class CostMeter:
    """Accumulates real usage-based cost and enforces the per-run cap."""

    def __init__(self, cap_usd: float = RUN_COST_CAP_USD):
        self.cap = cap_usd
        self.total = 0.0
        self.calls = 0

    def add(self, model: str, usage) -> float:
        inp, outp = MODEL_PRICES.get(model, (5.0, 25.0))  # unknown model: assume pricey
        cost = (usage.input_tokens * inp + usage.output_tokens * outp) / 1_000_000
        self.total += cost
        self.calls += 1
        if self.total >= self.cap:
            raise CostCapExceeded(
                f"run cost ${self.total:.2f} reached cap ${self.cap:.2f} after {self.calls} calls"
            )
        return cost
