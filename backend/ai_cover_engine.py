"""AI cover art generation and image enhancement via OpenAI's Image API.

Requires OPENAI_API_KEY in backend/.env. Uses OpenAI's REST endpoints
directly (no SDK dependency, same pattern as the direct-HTTP Anthropic
call in server.py's /ai/blurb) so there's one fewer pinned SDK version to
track.

Switched from Google's Gemini/Imagen API on 2026-08-26 -- that path hit
two separate blockers in production: the Imagen model had been deprecated
and shut down by Google, and even after fixing the model name, image
generation requires a Google Cloud project with billing enabled (the free
tier's quota is 0 requests, not a small allowance), which ran into a
card-decline issue at signup. OpenAI's billing is a plain card checkout
with no cloud-project setup step.

One thing to verify once a real key is in hand: OpenAI's docs mention GPT
Image models may require "API Organization Verification" (an identity
check) in the developer dashboard before they'll actually respond, on top
of billing being enabled -- if generation calls fail with an
organization/verification-related error, that's what's being asked for.
"""
import base64
import os
from typing import Optional

OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
OPENAI_API_BASE = "https://api.openai.com/v1"

# OpenAI's images endpoints take a fixed set of sizes, not an arbitrary
# aspect ratio string -- map SparkPrep's cover aspect ratios to the closest
# supported size (all covers here are portrait, so this only needs to
# distinguish "roughly square" from "clearly portrait").
_SIZE_BY_ASPECT = {
    "1:1": "1024x1024",
    "3:4": "1024x1536",
    "2:3": "1024x1536",
}
DEFAULT_SIZE = "1024x1536"


class AICoverError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def build_cover_prompt(user_prompt: str, title: Optional[str], genre: Optional[str], is_full_wrap: bool) -> str:
    """Wraps the author's freeform description with print-cover-specific
    guidance. AI image models render embedded text unreliably (misspelled/
    garbled titles are common), so we explicitly tell it to leave the
    canvas clear of text -- the title/author/spine text still gets added
    by SparkPrep's own compositor (front_cover slot + existing text
    overlay tooling), not baked into the AI image itself."""
    parts = [
        f"A professional, print-ready book cover illustration. {user_prompt.strip()}",
    ]
    if genre:
        parts.append(f"Genre: {genre}.")
    parts.append(
        "Full-bleed artwork filling the entire frame, no borders or margins. "
        "Do not render any words, letters, titles, or typography anywhere in the image -- "
        "leave the composition clear for text to be added separately. "
        "High resolution, sharp focus, commercial book-cover quality lighting and composition."
    )
    return " ".join(parts)


def _handle_error_response(resp) -> None:
    if resp.status_code == 401:
        raise AICoverError("OpenAI rejected this API key. Double-check OPENAI_API_KEY is set correctly.", 503)
    if resp.status_code == 429:
        raise AICoverError(
            "OpenAI rate-limited or quota-exhausted this request. If this is a fresh account, make sure "
            "billing/credit is actually added, not just an API key created.", 503,
        )
    if resp.status_code == 403:
        raise AICoverError(
            "OpenAI blocked this request (403) -- GPT Image models can require completing 'API Organization "
            "Verification' in the OpenAI developer dashboard before they'll respond, separate from billing.", 503,
        )
    if resp.status_code != 200:
        raise AICoverError(f"OpenAI image request failed ({resp.status_code}): {resp.text[:300]}")


async def generate_cover_image(prompt: str, api_key: str, aspect_ratio: str = "3:4") -> bytes:
    """Calls OpenAI's image generation endpoint and returns raw PNG bytes.
    Raises AICoverError with a user-facing message on any failure (missing
    key handled by the caller before this is ever invoked)."""
    if not api_key:
        raise AICoverError("AI Cover Generation isn't configured yet — add OPENAI_API_KEY to backend/.env", 503)

    import httpx
    url = f"{OPENAI_API_BASE}/images/generations"
    payload = {
        "model": OPENAI_IMAGE_MODEL,
        "prompt": prompt,
        "size": _SIZE_BY_ASPECT.get(aspect_ratio, DEFAULT_SIZE),
        "n": 1,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except Exception as e:
        raise AICoverError(f"Couldn't reach OpenAI's image generation API: {e}")

    _handle_error_response(resp)

    data = resp.json()
    items = data.get("data") or []
    if not items or not items[0].get("b64_json"):
        raise AICoverError("Image generation returned no results. Try a different description.")
    try:
        return base64.b64decode(items[0]["b64_json"])
    except Exception as e:
        raise AICoverError(f"Couldn't decode the generated image: {e}")


async def enhance_image(image_bytes: bytes, mime_type: str, api_key: str, instruction: str) -> bytes:
    """Feeds an existing image into OpenAI's image *edits* endpoint along
    with an editing instruction and returns the resulting image bytes --
    used for the "AI Upscale" fix option (SparkPrep's approach: rather than
    a naive pixel-stretch, ask the model to enhance detail/resolution while
    preserving the original composition exactly). Unlike generations, edits
    is multipart/form-data since it takes a real file upload, not JSON."""
    if not api_key:
        raise AICoverError("AI enhancement isn't configured yet — add OPENAI_API_KEY to backend/.env", 503)

    import httpx
    url = f"{OPENAI_API_BASE}/images/edits"
    headers = {"Authorization": f"Bearer {api_key}"}
    ext = "png" if "png" in mime_type else "jpg"
    files = {"image": (f"source.{ext}", image_bytes, mime_type)}
    data = {"model": OPENAI_IMAGE_MODEL, "prompt": instruction, "n": "1"}
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, headers=headers, data=data, files=files)
    except Exception as e:
        raise AICoverError(f"Couldn't reach OpenAI's image API: {e}")

    _handle_error_response(resp)

    result = resp.json()
    items = result.get("data") or []
    if not items or not items[0].get("b64_json"):
        raise AICoverError("Image enhancement returned no results.")
    try:
        return base64.b64decode(items[0]["b64_json"])
    except Exception as e:
        raise AICoverError(f"Couldn't decode the enhanced image: {e}")
