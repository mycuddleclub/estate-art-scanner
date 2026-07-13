"""
Vision analysis using Claude (Anthropic).

Two-stage approach:
1. Art filter: identify which photos contain original artwork (Haiku — cheap/fast)
2. Quality assessment: score collection quality and describe what's visible (Sonnet — capable)
"""

import base64
import logging
import re
import requests
import anthropic

logger = logging.getLogger(__name__)

# Haiku for cheap bulk filtering, Sonnet for quality assessment
FILTER_MODEL = "claude-haiku-4-5-20251001"
ASSESS_MODEL = "claude-sonnet-5"


def _download_image_b64(url: str) -> str | None:
    """Download image and return as base64 string."""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode("utf-8")
    except Exception as e:
        logger.warning(f"Failed to download image {url}: {e}")
        return None


def _image_block(b64: str) -> dict:
    """Build Anthropic image content block from base64."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": b64,
        },
    }


def filter_art_photos(thumbnail_urls: list[str], client: anthropic.Anthropic) -> list[str]:
    """
    Stage 1: Given thumbnail URLs, return only those containing visible
    original artwork. Uses Haiku for speed and cost efficiency.
    Processes in batches of 6.
    """
    if not thumbnail_urls:
        return []

    art_urls = []
    batch_size = 6

    for i in range(0, len(thumbnail_urls), batch_size):
        batch = thumbnail_urls[i:i + batch_size]

        images = []
        valid_urls = []
        for url in batch:
            b64 = _download_image_b64(url)
            if b64:
                images.append(_image_block(b64))
                valid_urls.append(url)

        if not images:
            continue

        content = []
        for idx, img in enumerate(images):
            content.append({"type": "text", "text": f"Image {idx + 1}:"})
            content.append(img)

        content.append({
            "type": "text",
            "text": (
                f"For each of the {len(images)} images above, answer YES or NO: "
                "does this photo show anything that could be an artwork — paintings, "
                "drawings, watercolors, fine prints, photographs, sculpture, studio "
                "ceramics/pottery, or textile art? Count artwork anywhere in the frame: "
                "hanging on walls in the background, leaning against furniture, propped "
                "in stacks, or partially visible at the edge. When uncertain, answer YES. "
                "Answer NO only if there is clearly no artwork at all, or the only "
                "candidates are mirrors, commercial posters, or obvious mass-produced decor. "
                "Reply in exactly this format with no other text: 1:YES 2:NO 3:YES"
            )
        })

        try:
            response = client.messages.create(
                model=FILTER_MODEL,
                max_tokens=60,
                messages=[{"role": "user", "content": content}],
            )
            answer = response.content[0].text.strip()
            logger.debug(f"Art filter response: {answer}")

            for match in re.finditer(r"(\d+):(YES|NO)", answer.upper()):
                idx = int(match.group(1)) - 1
                if 0 <= idx < len(valid_urls) and match.group(2) == "YES":
                    art_urls.append(valid_urls[idx])

        except Exception as e:
            logger.error(f"Art filter API call failed: {e}")
            continue

    logger.info(f"Art filter: {len(art_urls)}/{len(thumbnail_urls)} photos contain artwork")
    return art_urls


def assess_collection_quality(
    art_photo_urls: list[str],
    description: str,
    client: anthropic.Anthropic,
) -> dict:
    """
    Stage 2: Assess collection quality from confirmed art photos.
    Uses Sonnet for deeper analysis.

    Returns dict: score (1-10), summary, priority (HIGH/MEDIUM/LOW), alert_worthy (bool)
    """
    if not art_photo_urls:
        return {"score": 0, "summary": "No art photos", "priority": "LOW", "alert_worthy": False}

    urls_to_use = art_photo_urls[:20]
    content = []

    for idx, url in enumerate(urls_to_use):
        b64 = _download_image_b64(url)
        if b64:
            content.append({"type": "text", "text": f"Photo {idx + 1}:"})
            content.append(_image_block(b64))

    if not content:
        return {"score": 0, "summary": "Could not download photos", "priority": "LOW", "alert_worthy": False}

    clean_description = re.sub(r"<[^>]+>", " ", description).strip()[:500]

    content.append({
        "type": "text",
        "text": (
            "You are an expert art advisor scouting an estate sale for a collector who "
            "specializes in OVERLOOKED and UNDERRECOGNIZED art: works by documented but "
            "market-forgotten artists, folk/self-taught/outsider art, regional schools, "
            "works on paper, studio ceramics, and unfashionable periods. The best finds "
            "are usually uncatalogued — visible only in these photos, never mentioned in "
            "the listing text.\n\n"
            f"Sale description: \"{clean_description}\"\n\n"
            "Assess the artwork visible and respond with exactly these four sections:\n\n"
            "SCORE: [1-10] — score the SINGLE STRONGEST work visible, not the average. "
            "One serious original in a house of junk deserves a high score. "
            "(10 = at least one work with strong evidence of a documented or important "
            "artist; 7 = at least one confident, skilled original worth researching; "
            "4 = competent originals of uncertain merit; 1 = nothing but reproductions.)\n\n"
            "WHAT I SEE: Describe each distinct artwork — medium, approximate size, "
            "style/period, condition cues, overall quality. TRANSCRIBE any visible "
            "signature, label, inscription, or stamp, even partially ('signature lower "
            "right, possibly B—something'). Note works that appear only in backgrounds "
            "or stacks and whether the listing text mentions them. Note collector "
            "context: dense multi-work walls, art books, quality framing.\n\n"
            "RED FLAGS: Likely prints/giclees/posters presented as originals, "
            "mass-produced decor, condition problems.\n\n"
            "VERDICT: One sentence — is this worth immediate attention, and which "
            "specific work drives that judgment?\n\n"
            "Do NOT dismiss works for being unfashionable, naive, regional, or "
            "'decorative-looking' — that is exactly where sleepers hide. Distinguish "
            "what you can SEE from what you INFER, and say when photo quality limits "
            "your confidence."
        )
    })

    try:
        response = client.messages.create(
            model=ASSESS_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": content}],
        )
        summary = response.content[0].text.strip()

        score = 0
        match = re.search(r"SCORE:\s*(\d+)", summary, re.IGNORECASE)
        if match:
            score = min(10, max(0, int(match.group(1))))

        if score >= 7:
            priority, alert_worthy = "HIGH", True
        elif score >= 5:
            priority, alert_worthy = "MEDIUM", True
        else:
            priority, alert_worthy = "LOW", False

        logger.info(f"Quality score: {score}/10 ({priority})")
        return {"score": score, "summary": summary, "priority": priority, "alert_worthy": alert_worthy}

    except Exception as e:
        logger.error(f"Quality assessment failed: {e}")
        return {"score": 0, "summary": f"Assessment failed: {e}", "priority": "LOW", "alert_worthy": False}
