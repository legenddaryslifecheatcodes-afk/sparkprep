"""AI cover art generation via Google's Gemini API (Imagen image models).

Requires GOOGLE_API_KEY in backend/.env. Uses the REST predict endpoint
directly (no SDK dependency, same pattern as the direct-HTTP Anthropic
call in server.py's /ai/blurb) so there's one fewer pinned SDK version to
track. The model name is deliberately an env-configurable constant --
Google's image-model naming changes fairly often, so verify
GOOGLE_IMAGE_MODEL against the current Google AI Studio / Gemini API docs
before relying on this in production.
"""
import base64
import os
from typing import Optional

GOOGLE_IMAGE_MODEL = os.environ.get("GOOGLE_IMAGE_MODEL", "imagen-4.0-generate-001")
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


async def generate_cover_image(prompt: str, api_key: str, aspect_ratio: str = "3:4") -> bytes:
    """Calls the Imagen predict endpoint and returns raw PNG/JPEG bytes for
    the first generated image. Raises AICoverError with a user-facing
    message on any failure (missing key handled by the caller before this
    is ever invoked)."""
    if not api_key:
        raise AICoverError("AI Cover Generation isn't configured yet — add GOOGLE_API_KEY to backend/.env", 503)

    import httpx
    url = f"{GOOGLE_GENAI_BASE}/models/{GOOGLE_IMAGE_MODEL}:predict?key={api_key}"
    payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {"sampleCount": 1, "aspectRatio": aspect_ratio},
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
    except Exception as e:
        raise AICoverError(f"Couldn't reach Google's image generation API: {e}")

    if resp.status_code != 200:
        raise AICoverError(f"Image generation failed ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    predictions = data.get("predictions") or []
    if not predictions:
        raise AICoverError("Image generation returned no results. Try a different description.")

    b64_image = predictions[0].get("bytesBase64Encoded")
    if not b64_image:
        raise AICoverError("Image generation response was missing image data.")

    try:
        return base64.b64decode(b64_image)
    except Exception as e:
        raise AICoverError(f"Couldn't decode the generated image: {e}")
