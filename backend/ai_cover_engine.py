"""AI cover art generation via Google's Gemini API (image-generation models).

Requires GOOGLE_API_KEY in backend/.env. Uses the REST generateContent
endpoint directly (no SDK dependency, same pattern as the direct-HTTP
Anthropic call in server.py's /ai/blurb) so there's one fewer pinned SDK
version to track.

The prior model (imagen-4.0-generate-001, called via :predict) was
deprecated and shut down by Google on 2026-08-17 -- confirmed dead in
production via a live test call (404 model not found) on 2026-08-26.
Google's documented replacement is gemini-2.5-flash-image via
:generateContent, a different request/response shape entirely (contents/
parts instead of instances/parameters, inlineData instead of
bytesBase64Encoded) -- this file was rewritten for that shape, not just
had its model name swapped.

Separately: image generation requires a Google Cloud project with billing
enabled -- the free tier's quota for these models is 0 requests, not a
small allowance. A 429 RESOURCE_EXHAUSTED error whose message says
"limit: 0" means billing isn't on, not that usage was exceeded.
"""
import base64
import os
from typing import Optional

GOOGLE_IMAGE_MODEL = os.environ.get("GOOGLE_IMAGE_MODEL", "gemini-2.5-flash-image")
GOOGLE_GENAI_BASE = "https://generativelanguage.googleapis.com/v1beta"


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


def _extract_image_bytes(data: dict) -> bytes:
    """Shared response parsing for generateContent calls -- both cover
    generation and image-enhancement use this same response shape (a
    candidate's content.parts, one of which carries inlineData)."""
    candidates = data.get("candidates") or []
    if not candidates:
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            raise AICoverError(f"Google's image model declined this request ({block_reason}). Try a different description or image.")
        raise AICoverError("Image generation returned no results. Try a different description.")

    parts = (candidates[0].get("content") or {}).get("parts") or []
    for part in parts:
        inline = part.get("inlineData")
        if inline and inline.get("data"):
            try:
                return base64.b64decode(inline["data"])
            except Exception as e:
                raise AICoverError(f"Couldn't decode the generated image: {e}")

    raise AICoverError("Image generation response didn't include an image.")


async def generate_cover_image(prompt: str, api_key: str, aspect_ratio: str = "3:4") -> bytes:
    """Calls Gemini's generateContent endpoint and returns raw image bytes
    for the generated image. Raises AICoverError with a user-facing message
    on any failure (missing key handled by the caller before this is ever
    invoked). aspect_ratio isn't a request parameter on this endpoint the
    way it was on the old :predict API -- folded into the prompt text
    instead, since that's how this model shape controls composition."""
    if not api_key:
        raise AICoverError("AI Cover Generation isn't configured yet — add GOOGLE_API_KEY to backend/.env", 503)

    import httpx
    url = f"{GOOGLE_GENAI_BASE}/models/{GOOGLE_IMAGE_MODEL}:generateContent?key={api_key}"
    full_prompt = f"{prompt} Image aspect ratio: {aspect_ratio}."
    payload = {"contents": [{"parts": [{"text": full_prompt}]}]}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
    except Exception as e:
        raise AICoverError(f"Couldn't reach Google's image generation API: {e}")

    if resp.status_code == 429:
        raise AICoverError(
            "Google's image generation quota is exhausted. If this is a fresh setup, this usually means "
            "billing isn't enabled yet on the Google Cloud project for this API key -- the free tier's quota "
            "for image models is 0, not a small trial allowance.", 503,
        )
    if resp.status_code != 200:
        raise AICoverError(f"Image generation failed ({resp.status_code}): {resp.text[:300]}")

    return _extract_image_bytes(resp.json())


async def enhance_image(image_bytes: bytes, mime_type: str, api_key: str, instruction: str) -> bytes:
    """Feeds an existing image back into the same multimodal model along
    with an editing instruction and returns the resulting image bytes --
    used for the "AI Upscale" fix option (SparkPrep's approach: rather than
    a naive pixel-stretch, ask the model to enhance detail/resolution while
    preserving the original composition exactly)."""
    if not api_key:
        raise AICoverError("AI enhancement isn't configured yet — add GOOGLE_API_KEY to backend/.env", 503)

    import httpx
    url = f"{GOOGLE_GENAI_BASE}/models/{GOOGLE_IMAGE_MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [{
            "parts": [
                {"text": instruction},
                {"inlineData": {"mimeType": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}},
            ],
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
    except Exception as e:
        raise AICoverError(f"Couldn't reach Google's image API: {e}")

    if resp.status_code == 429:
        raise AICoverError(
            "Google's image quota is exhausted. If this is a fresh setup, this usually means billing isn't "
            "enabled yet on the Google Cloud project for this API key -- the free tier's quota for image "
            "models is 0, not a small trial allowance.", 503,
        )
    if resp.status_code != 200:
        raise AICoverError(f"Image enhancement failed ({resp.status_code}): {resp.text[:300]}")

    return _extract_image_bytes(resp.json())
