"""
LLM Client Module
Communicates with two LLM servers:
- Gemma 4 (vision analysis, popup detection)
- UI-TARS-2B (grounding — finding element coordinates)
"""

import base64
import io
import json
import logging
import os
import re
from typing import Optional

import httpx
from PIL import Image

logger = logging.getLogger("llm_client")

# Configuration
LLM_URL = os.environ.get("LLM_URL", "http://192.168.31.56:1234")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemma-4-e4b-it")
GROUNDING_URL = os.environ.get("GROUNDING_URL", "http://192.168.31.195:1234")
GROUNDING_MODEL = os.environ.get("GROUNDING_MODEL", "ui-tars-2b-sft")
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "30"))


def image_to_base64(img: Image.Image) -> str:
    """Convert PIL Image to base64 string."""
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


async def _call_llm(
    base_url: str,
    model: str,
    messages: list[dict],
    max_tokens: int = 1024,
) -> Optional[str]:
    """Make a chat completion request to LLM server."""
    url = f"{base_url}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }

    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return content
    except httpx.TimeoutException:
        logger.error(f"LLM request timed out ({base_url}, model={model})")
        return None
    except httpx.HTTPStatusError as e:
        logger.error(
            f"LLM HTTP error: {e.response.status_code} — {e.response.text[:200]}"
        )
        return None
    except Exception as e:
        logger.error(f"LLM request failed: {e}")
        return None


# --- Vision Analysis (Gemma 4) ---


async def analyze_page_structure(screenshot: Image.Image) -> Optional[dict]:
    """
    Analyze page structure using Gemma 4.
    Returns dict with primary_navigation, secondary_navigation, content_area, summary.
    """
    b64 = image_to_base64(screenshot)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a web page structure analyzer. Analyze the screenshot and identify:\n"
                "1) Primary navigation (main menu) — location and items\n"
                "2) Secondary navigation (sidebars, footer links) — location and items\n"
                "3) Content area (posts, products, news, etc) — type and description\n"
                "Be concise. Respond ONLY with valid JSON in this format:\n"
                '{"primary_navigation": {"location": "top/left/right", "items": ["item1", "item2"]}, '
                '"secondary_navigation": {"location": "...", "items": [...]}, '
                '"content_area": {"type": "...", "description": "..."}, '
                '"summary": "one sentence summary"}'
            ),
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
                {
                    "type": "text",
                    "text": "Analyze the structure of this web page. Identify the navigation areas and content area.",
                },
            ],
        },
    ]

    response = await _call_llm(LLM_URL, LLM_MODEL, messages, max_tokens=1024)
    if response is None:
        return None

    return _parse_json_response(response)


async def detect_popup(screenshot: Image.Image) -> Optional[dict]:
    """
    Check if there's a popup/modal/cookie banner on the page.
    Returns dict with popup_detected (bool) and close_button_text (str).
    """
    b64 = image_to_base64(screenshot)

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
                {
                    "type": "text",
                    "text": (
                        "Is there a popup, modal dialog, cookie banner, or overlay on this page "
                        "that blocks the main content? If yes, respond with JSON: "
                        '{"popup_detected": true, "close_button_text": "<text on the close/accept button>"}. '
                        'If no popup, respond: {"popup_detected": false}'
                    ),
                },
            ],
        },
    ]

    response = await _call_llm(LLM_URL, LLM_MODEL, messages, max_tokens=128)
    if response is None:
        return {"popup_detected": False}

    result = _parse_json_response(response)
    if result is None:
        return {"popup_detected": False}

    return result


async def analyze_full_page(screenshots: list[Image.Image]) -> Optional[dict]:
    """
    Analyze multiple screenshots (from full page scan) to get complete structure.
    """
    if not screenshots:
        return None

    # For now, send only the first screenshot with context about page length
    # Future: could concatenate or send multiple
    b64_images = [image_to_base64(img) for img in screenshots[:3]]

    content = []
    for i, b64 in enumerate(b64_images):
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )
        content.append(
            {
                "type": "text",
                "text": f"[Section {i + 1} of {len(b64_images)}]",
            }
        )

    content.append(
        {
            "type": "text",
            "text": (
                f"These are {len(b64_images)} sections of the same web page (scrolled top to bottom). "
                "Analyze the FULL page structure. Identify:\n"
                "1) Primary navigation\n"
                "2) Secondary navigation (including footer)\n"
                "3) All content sections found across the page\n"
                "Respond ONLY with valid JSON:\n"
                '{"primary_navigation": {"location": "...", "items": [...]}, '
                '"secondary_navigation": {"location": "...", "items": [...]}, '
                '"content_sections": [{"position": "top/middle/bottom", "type": "...", "description": "..."}], '
                '"summary": "..."}'
            ),
        }
    )

    messages = [{"role": "user", "content": content}]

    response = await _call_llm(LLM_URL, LLM_MODEL, messages, max_tokens=2048)
    if response is None:
        return None

    return _parse_json_response(response)


# --- Grounding (UI-TARS-2B) ---


async def find_element_coordinates(
    screenshot: Image.Image, target: str
) -> Optional[dict]:
    """
    Use UI-TARS-2B to find coordinates of a UI element.
    Returns dict with x, y, found (bool).

    UI-TARS returns coordinates in normalized format (0-1000).
    We convert to actual pixels based on screenshot dimensions.
    """
    b64 = image_to_base64(screenshot)
    width, height = screenshot.size

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
                {
                    "type": "text",
                    "text": f"Click on the element: '{target}'",
                },
            ],
        },
    ]

    response = await _call_llm(GROUNDING_URL, GROUNDING_MODEL, messages, max_tokens=128)
    if response is None:
        return {"found": False, "reason": "LLM did not respond"}

    logger.info(f"UI-TARS response for '{target}': {response}")

    # UI-TARS may respond in different formats. Try to parse coordinates.
    coords = _parse_coordinates(response, width, height)
    if coords:
        return {"found": True, "x": coords[0], "y": coords[1]}
    else:
        return {
            "found": False,
            "reason": f"Could not parse coordinates from: {response[:100]}",
        }


def _parse_coordinates(
    response: str, width: int, height: int
) -> Optional[tuple[int, int]]:
    """
    Parse coordinates from UI-TARS response.
    UI-TARS may return:
    - Normalized coords like (523, 147) meaning (x/1000*width, y/1000*height)
    - Action format like "click(523, 147)"
    - JSON like {"x": 523, "y": 147}
    - Pixel coords directly
    """
    # Try JSON format
    try:
        data = json.loads(response)
        if "x" in data and "y" in data:
            return (int(data["x"]), int(data["y"]))
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # Try to find coordinate patterns: (x, y) or click(x, y) or [x, y]
    patterns = [
        r"click\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)",
        r"\((\d+)\s*,\s*(\d+)\)",
        r"\[(\d+)\s*,\s*(\d+)\]",
        r'x["\s:=]+(\d+).*?y["\s:=]+(\d+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, response, re.IGNORECASE)
        if match:
            raw_x, raw_y = int(match.group(1)), int(match.group(2))

            # If values are > 1000, assume they are pixel coordinates
            if raw_x > 1000 or raw_y > 1000:
                return (min(raw_x, width - 1), min(raw_y, height - 1))

            # Otherwise assume normalized (0-1000) and convert to pixels
            pixel_x = int(raw_x * width / 1000)
            pixel_y = int(raw_y * height / 1000)
            return (pixel_x, pixel_y)

    return None


# --- Helpers ---


def _parse_json_response(response: str) -> Optional[dict]:
    """Try to parse JSON from LLM response, handling markdown code blocks."""
    # Strip markdown code blocks if present
    response = response.strip()
    if response.startswith("```"):
        # Remove first and last line (```json and ```)
        lines = response.split("\n")
        response = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    # Try direct JSON parse
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the response
    match = re.search(r"\{.*\}", response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    logger.warning(f"Could not parse JSON from LLM response: {response[:200]}")
    return None
