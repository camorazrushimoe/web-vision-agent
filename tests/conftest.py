"""
Shared fixtures for web-vision-agent tests.

All LLM calls and browser control functions are mocked so tests run
locally without Docker, Xvfb, Chromium, or real LLM servers.
"""

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image

import pytest

# Make app/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def make_image(color=(255, 255, 255)) -> Image.Image:
    """Return a small blank PIL image (used as a fake screenshot)."""
    return Image.new("RGB", (100, 100), color=color)


def make_different_image() -> Image.Image:
    """Return an image that is visually different from make_image()."""
    return Image.new("RGB", (100, 100), color=(0, 0, 0))


# ---------------------------------------------------------------------------
# browser_control mock fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_browser(monkeypatch):
    """
    Replace all browser_control async functions with mocks.
    Returns a namespace object so individual tests can override specific calls.
    """
    import browser_control

    monkeypatch.setattr(
        browser_control, "take_screenshot", AsyncMock(return_value=make_image())
    )
    monkeypatch.setattr(browser_control, "navigate_to_url", AsyncMock())
    monkeypatch.setattr(
        browser_control, "wait_for_page_load", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        browser_control,
        "get_current_url",
        AsyncMock(return_value="https://example.com/"),
    )
    monkeypatch.setattr(browser_control, "click_at", AsyncMock())
    monkeypatch.setattr(browser_control, "type_text", AsyncMock())
    monkeypatch.setattr(browser_control, "press_key", AsyncMock())
    monkeypatch.setattr(browser_control, "scroll_down", AsyncMock())
    monkeypatch.setattr(browser_control, "scroll_to_top", AsyncMock())
    monkeypatch.setattr(
        browser_control, "is_browser_running", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        browser_control,
        "pixel_difference",
        MagicMock(return_value=50.0),  # default: big diff = page changed
    )

    return browser_control


# ---------------------------------------------------------------------------
# llm_client mock fixtures
# ---------------------------------------------------------------------------


STRUCTURE_OK = {
    "primary_navigation": {"location": "top", "items": ["Home", "About"]},
    "secondary_navigation": {"location": "left", "items": []},
    "content_area": {"type": "article", "description": "Main content"},
    "summary": "A simple test page",
}

INPUT_FIELDS_OK = {
    "input_fields": [
        {
            "type": "search",
            "label": "site search",
            "description": "white search field at the top center, magnifying glass icon on the right",
        }
    ]
}

CONTENT_OK = {
    "content_type": "product_list",
    "content_summary": "List of products",
    "items_count": 2,
    "items": [
        {"index": 1, "label": "Product A", "description": "10 USD"},
        {"index": 2, "label": "Product B", "description": "20 USD"},
    ],
    "text_summary": "Page shows 2 products.",
    "clickable_elements": ["Product A card", "Product B card"],
}

COORDS_FOUND = {"found": True, "x": 500, "y": 80}
COORDS_NOT_FOUND = {"found": False, "reason": "element not visible"}


@pytest.fixture
def mock_llm(monkeypatch):
    """
    Replace all llm_client async functions with happy-path mocks.
    Returns the llm_client module so individual tests can override specific calls.
    """
    import llm_client

    monkeypatch.setattr(
        llm_client, "analyze_page_structure", AsyncMock(return_value=STRUCTURE_OK)
    )
    monkeypatch.setattr(
        llm_client, "detect_input_fields", AsyncMock(return_value=INPUT_FIELDS_OK)
    )
    monkeypatch.setattr(
        llm_client, "analyze_page_content", AsyncMock(return_value=CONTENT_OK)
    )
    monkeypatch.setattr(
        llm_client, "detect_popup", AsyncMock(return_value={"popup_detected": False})
    )
    monkeypatch.setattr(
        llm_client, "find_element_coordinates", AsyncMock(return_value=COORDS_FOUND)
    )
    monkeypatch.setattr(
        llm_client, "analyze_full_page", AsyncMock(return_value=STRUCTURE_OK)
    )

    return llm_client


# ---------------------------------------------------------------------------
# Convenience: collect all events from an async generator
# ---------------------------------------------------------------------------


async def collect(gen) -> list[dict]:
    """Drain an async generator and return all yielded dicts."""
    events = []
    async for event in gen:
        events.append(event)
    return events


def result_event(events: list[dict]) -> dict | None:
    """Return the first event with type='result', or None."""
    return next((e for e in events if e.get("type") == "result"), None)


def error_events(events: list[dict]) -> list[dict]:
    """Return all events that represent an error or failure state."""
    return [
        e
        for e in events
        if (
            "error" in e.get("stage", "")
            or "not_found" in e.get("stage", "")
            or "failed" in e.get("stage", "")
        )
    ]
