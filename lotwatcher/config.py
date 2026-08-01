"""lotwatcher configuration — every-lot LA + HiBid ingestion on local models."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # estate-art-scanner repo
WH_DATA = ROOT / "wh_data"
DB_PATH = WH_DATA / "lotwatcher.db"
LA_PROFILE_DIR = WH_DATA / "la_profile"                # persistent browser profile
LOG_DIR = Path.home() / "logs"

# which platforms to watch: "hibid,la" (default) — run hibid-only until
# the LA account login is done in the persistent browser window
PLATFORMS = set(os.environ.get("LW_PLATFORMS", "hibid,la").split(","))

# --- LiveAuctioneers ---
LA_SEARCH_URL = "https://www.liveauctioneers.com/catalog/search/"
LA_MAX_LISTING_PAGES = int(os.environ.get("LW_LA_LISTING_PAGES", "60"))
LA_MAX_CATALOG_PAGES = int(os.environ.get("LW_LA_CATALOG_PAGES", "40"))
LA_PAGE_DELAY_S = (2.0, 4.5)          # polite jittered delay between page loads
LA_AUCTION_DELAY_S = (4.0, 9.0)

# --- HiBid ---
HIBID_GRAPHQL = "https://hibid.com/graphql"
# only take auctions ending at least this many days out (review runway);
# in steady state everything is scanned days ahead, so this mostly trims
# the initial backfill of about-to-end auctions
MIN_DAYS_OUT = float(os.environ.get("LW_MIN_DAYS_OUT", "3"))

HIBID_MAX_CATALOG_PAGES = int(os.environ.get("LW_HIBID_CATALOG_PAGES", "40"))

# --- funnel ---
STAGE1_MODEL = "qwen3.6-35b-a3b"
STAGE3_MODEL = "gpt-oss-120b"
LM_BASE = "http://localhost:1234/v1"
LMS_EXE = "/mnt/c/Users/willi/.lmstudio/bin/lms.exe"
STAGE1_WORKERS = int(os.environ.get("LW_STAGE1_WORKERS", "4"))
STAGE1_PROMISE_CUTOFF = float(os.environ.get("LW_PROMISE_CUTOFF", "5.0"))
# lower bar for lots that named a plausible artist (catches contemporary
# gallery artists not in the historical-skewed authority.db)
STAGE1_NAMED_CUTOFF = float(os.environ.get("LW_NAMED_CUTOFF", "3.0"))
MAX_AUCTIONS_PER_CYCLE = int(os.environ.get("LW_MAX_AUCTIONS", "60"))
MAX_LOTS_PER_AUCTION = int(os.environ.get("LW_MAX_LOTS", "1500"))

# display-time category filter (never filters detection — Daniel's rule)
HIDE_CATEGORIES = set(os.environ.get(
    "WH_HIDE_CATEGORIES",
    "jewelry,glass,metalware,furniture,decor,book,print").split(","))

# art-signal band (deep.py precedent, Daniel-approved design): titles that
# clearly signal art OR are too vague to rule out go to stage 1.
ART_SIGNAL = (
    "painting", "paint", "oil", "acrylic", "watercolor", "watercolour", "gouache",
    "canvas", "print", "lithograph", "litho", "etching", "engraving", "serigraph",
    "silkscreen", "screenprint", "woodcut", "woodblock", "linocut", "giclee",
    "drawing", "sketch", "pastel", "charcoal", "ink", "mixed media", "collage",
    "sculpture", "bronze", "carving", "carved", "bust", "statue", "figurine",
    "art", "artist", "signed", "illustration", "portrait", "landscape",
    "still life", "abstract", "folk", "outsider", "photograph", "photo",
    "poster", "tapestry", "textile", "quilt", "icon", "fresco", "mural",
    "aquatint", "monotype", "monoprint", "frame", "framed", "picture",
    "ceramic", "pottery", "studio", "kimono", "wall hanging",
)

# obviously-not-art hard negatives (only applied when NO art signal present)
HARD_NEGATIVE = (
    "vehicle", "truck", "tractor", "trailer", "mower", "atv", "utv", "forklift",
    "firearm", "ammo", "ammunition", "rifle", "pistol", "shotgun",
    "tool", "drill", "saw ", "wrench", "socket set", "compressor",
    "appliance", "refrigerator", "washer", "dryer", "hvac",
    "gift card", "coupon", "pallet of", "shelf of", "box lot of hardware",
)

def data_dirs():
    WH_DATA.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
