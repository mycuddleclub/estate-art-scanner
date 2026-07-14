"""Content-addressed image store, downscaling, cropping, perceptual hashing."""

import base64
import hashlib
import io

from PIL import Image, ImageOps

from .config import CROP_PAD_FRACTION, IMAGE_DIR

Image.MAX_IMAGE_PIXELS = 60_000_000  # sanity ceiling; sale photos are far smaller


def store_bytes(data: bytes) -> str:
    """Save bytes content-addressed; return the hash key."""
    h = hashlib.sha256(data).hexdigest()
    path = IMAGE_DIR / h[:2] / f"{h}.jpg"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return h


def path_for(file_hash: str):
    return IMAGE_DIR / file_hash[:2] / f"{file_hash}.jpg"


def load(file_hash: str) -> Image.Image:
    img = Image.open(path_for(file_hash))
    return ImageOps.exif_transpose(img).convert("RGB")


def downscale_jpeg_b64(img: Image.Image, max_edge: int, quality: int = 80) -> str:
    im = img.copy()
    im.thumbnail((max_edge, max_edge), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def crop_fraction_box(img: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    """Crop [left, top, w, h] fractions with padding, clamped to bounds."""
    x, y, w, h = box
    pad = CROP_PAD_FRACTION
    left = max(0.0, x - w * pad)
    top = max(0.0, y - h * pad)
    right = min(1.0, x + w * (1 + pad))
    bottom = min(1.0, y + h * (1 + pad))
    W, H = img.size
    px = (int(left * W), int(top * H), max(int(right * W), int(left * W) + 8),
          max(int(bottom * H), int(top * H) + 8))
    return img.crop(px)


def save_crop(img: Image.Image) -> tuple[str, int]:
    """Store a crop as JPEG; return (hash, pixel_area)."""
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return store_bytes(buf.getvalue()), img.size[0] * img.size[1]


def dhash(img: Image.Image) -> str:
    """64-bit difference hash as 16 hex chars (pure Pillow, no numpy)."""
    small = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            i = row * 9 + col
            bits = (bits << 1) | (1 if px[i] > px[i + 1] else 0)
    return f"{bits:016x}"


def hamming(h1: str, h2: str) -> int:
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")
