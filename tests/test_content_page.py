"""
Tests for page_analyzer.py — content_page flow.

Covers:
- full_page=False: single screenshot, result contains content fields
- full_page=True: multiple screenshots collected via scroll
- full_page=True: scroll stops early when page ends (pixel_diff < 1%)
- analyze_page_content returns None → error event, no AttributeError
- Result always contains all expected keys
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from conftest import make_image, CONTENT_OK, collect, result_event, error_events


# ---------------------------------------------------------------------------
# full_page=False (default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_single_screenshot(mock_browser, mock_llm):
    """full_page=False → one screenshot taken, result has content fields."""
    import page_analyzer

    events = await collect(page_analyzer.content_page(full_page=False))
    res = result_event(events)

    assert res is not None
    assert res["type"] == "result"
    assert res["content_type"] == "product_list"
    assert res["items_count"] == 2
    assert len(res["items"]) == 2
    assert res["full_page"] is False
    assert res["sections_analyzed"] == 1


@pytest.mark.asyncio
async def test_content_page_result_has_all_keys(mock_browser, mock_llm):
    """All expected keys must be present in the result."""
    import page_analyzer

    events = await collect(page_analyzer.content_page())
    res = result_event(events)

    for key in (
        "content_type",
        "content_summary",
        "items_count",
        "items",
        "text_summary",
        "clickable_elements",
        "sections_analyzed",
        "full_page",
    ):
        assert key in res, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# full_page=True — scroll behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_full_page_scrolls(mock_browser, mock_llm):
    """full_page=True → scroll_down called, multiple screenshots collected."""
    import browser_control
    import page_analyzer

    # Each take_screenshot returns a different image so scrolling continues
    mock_browser.take_screenshot = AsyncMock(
        side_effect=[
            make_image((255, 255, 255)),  # section 1
            make_image((200, 200, 200)),  # diff check 1 (different → continue)
            make_image((200, 200, 200)),  # section 2
            make_image((100, 100, 100)),  # diff check 2 (different → continue)
            make_image((100, 100, 100)),  # section 3
            make_image((50, 50, 50)),  # diff check 3 (different → continue)
            make_image((50, 50, 50)),  # section 4 (MAX_SCROLL_SECTIONS=4)
        ]
    )
    # pixel_difference always returns big diff → page keeps scrolling
    mock_browser.pixel_difference = MagicMock(return_value=30.0)

    events = await collect(page_analyzer.content_page(full_page=True))
    res = result_event(events)

    assert res is not None
    assert res["full_page"] is True
    assert res["sections_analyzed"] == 4
    assert mock_browser.scroll_down.call_count >= 1


@pytest.mark.asyncio
async def test_content_page_full_page_stops_at_end(mock_browser, mock_llm):
    """full_page=True: stops early when page doesn't scroll (pixel_diff < 1%)."""
    import page_analyzer

    mock_browser.pixel_difference = MagicMock(return_value=0.0)  # page end

    events = await collect(page_analyzer.content_page(full_page=True))
    res = result_event(events)

    assert res is not None
    assert res["full_page"] is True
    # Stopped after 1 section (took screenshot, tried to scroll, diff=0 → break)
    assert res["sections_analyzed"] == 1


@pytest.mark.asyncio
async def test_content_page_full_page_scrolls_to_top_after(mock_browser, mock_llm):
    """full_page=True must scroll back to top after collecting screenshots."""
    import page_analyzer

    await collect(page_analyzer.content_page(full_page=True))

    mock_browser.scroll_to_top.assert_called()


# ---------------------------------------------------------------------------
# Error: analyze_page_content returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_llm_returns_none(mock_browser, mock_llm):
    """analyze_page_content returns None → error event, no AttributeError."""
    import page_analyzer

    mock_llm.analyze_page_content = AsyncMock(return_value=None)

    events = await collect(page_analyzer.content_page())

    assert result_event(events) is None
    errs = error_events(events)
    assert len(errs) > 0
    assert errs[0]["stage"] == "analysis_failed"


@pytest.mark.asyncio
async def test_content_page_llm_partial_response(mock_browser, mock_llm):
    """LLM returns partial dict (missing keys) → result uses .get() defaults, no crash."""
    import page_analyzer

    mock_llm.analyze_page_content = AsyncMock(
        return_value={
            "content_type": "unknown"
            # all other keys missing
        }
    )

    events = await collect(page_analyzer.content_page())
    res = result_event(events)

    assert res is not None
    assert res["content_type"] == "unknown"
    assert res["items"] == []
    assert res["clickable_elements"] == []
    assert res["items_count"] == 0
