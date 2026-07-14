"""
Tests for page_analyzer.py — search_page flow.

Covers:
- Happy path: result contains search_result with correct fields
- result_type=page_reload when URL changes
- result_type=content_updated when URL same but pixel_diff > 10%
- result_type=no_change when nothing changed
- No search field found → error event
- UI-TARS can't locate field → error event
- Submit button not found → falls back to Enter key
- url_before captured before any browser actions
- analyze_page_structure returns None after search → error event
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, call

from conftest import (
    make_image,
    make_different_image,
    STRUCTURE_OK,
    INPUT_FIELDS_OK,
    COORDS_FOUND,
    COORDS_NOT_FOUND,
    collect,
    result_event,
    error_events,
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_page_happy_path(mock_browser, mock_llm):
    """Full search flow: find field, type, click button, page reloads."""
    import browser_control
    import page_analyzer

    # URL changes after search
    mock_browser.get_current_url = AsyncMock(
        side_effect=[
            "https://example.com/",  # url_before
            "https://example.com/?q=test",  # url_after
        ]
    )

    events = await collect(page_analyzer.search_page("test"))
    res = result_event(events)

    assert res is not None
    sr = res["search_result"]
    assert sr["query"] == "test"
    assert sr["url_before"] == "https://example.com/"
    assert sr["url_after"] == "https://example.com/?q=test"
    assert sr["url_changed"] is True
    assert sr["result_type"] == "page_reload"


# ---------------------------------------------------------------------------
# result_type logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_result_type_content_updated(mock_browser, mock_llm):
    """URL doesn't change but pixel diff is high → content_updated (AJAX search)."""
    import browser_control
    import page_analyzer

    # Same URL before and after
    mock_browser.get_current_url = AsyncMock(return_value="https://example.com/")
    # Large pixel difference
    mock_browser.pixel_difference = MagicMock(return_value=45.0)

    events = await collect(page_analyzer.search_page("test"))
    res = result_event(events)

    assert res is not None
    assert res["search_result"]["result_type"] == "content_updated"
    assert res["search_result"]["url_changed"] is False


@pytest.mark.asyncio
async def test_search_result_type_no_change(mock_browser, mock_llm):
    """URL same and pixel diff small → no_change (search didn't work)."""
    import browser_control
    import page_analyzer

    mock_browser.get_current_url = AsyncMock(return_value="https://example.com/")
    mock_browser.pixel_difference = MagicMock(return_value=2.0)

    events = await collect(page_analyzer.search_page("test"))
    res = result_event(events)

    assert res is not None
    assert res["search_result"]["result_type"] == "no_change"


# ---------------------------------------------------------------------------
# url_before is captured BEFORE any browser actions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_url_before_captured_first(mock_browser, mock_llm):
    """url_before must be the very first get_current_url call."""
    import browser_control
    import page_analyzer

    call_order = []

    async def track_url():
        call_order.append("get_current_url")
        return "https://example.com/"

    async def track_screenshot():
        call_order.append("take_screenshot")
        return make_image()

    mock_browser.get_current_url = AsyncMock(side_effect=track_url)
    mock_browser.take_screenshot = AsyncMock(side_effect=track_screenshot)

    await collect(page_analyzer.search_page("test"))

    # url must be read before screenshot
    assert call_order.index("get_current_url") < call_order.index("take_screenshot")


# ---------------------------------------------------------------------------
# Error: no search field found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_no_search_field(mock_browser, mock_llm):
    """Gemma finds no search fields → search_field_not_found error."""
    import page_analyzer

    mock_llm.detect_input_fields = AsyncMock(return_value={"input_fields": []})

    events = await collect(page_analyzer.search_page("test"))

    assert result_event(events) is None
    errs = error_events(events)
    assert len(errs) > 0
    assert errs[0]["stage"] == "search_field_not_found"


@pytest.mark.asyncio
async def test_search_only_form_fields_no_search(mock_browser, mock_llm):
    """Only form-type fields found, no search field → error."""
    import page_analyzer

    mock_llm.detect_input_fields = AsyncMock(
        return_value={
            "input_fields": [
                {"type": "form", "label": "contact form", "description": "..."}
            ]
        }
    )

    events = await collect(page_analyzer.search_page("test"))

    assert result_event(events) is None
    errs = error_events(events)
    assert errs[0]["stage"] == "search_field_not_found"


@pytest.mark.asyncio
async def test_search_detect_fields_returns_none(mock_browser, mock_llm):
    """detect_input_fields returns None (LLM error) → error event."""
    import page_analyzer

    mock_llm.detect_input_fields = AsyncMock(return_value=None)

    events = await collect(page_analyzer.search_page("test"))

    assert result_event(events) is None
    errs = error_events(events)
    assert errs[0]["stage"] == "search_field_not_found"


# ---------------------------------------------------------------------------
# Error: UI-TARS can't locate the field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_uitars_cannot_locate_field(mock_browser, mock_llm):
    """UI-TARS returns found=False → search_field_not_found error."""
    import page_analyzer

    mock_llm.find_element_coordinates = AsyncMock(return_value=COORDS_NOT_FOUND)

    events = await collect(page_analyzer.search_page("test"))

    assert result_event(events) is None
    errs = error_events(events)
    assert errs[0]["stage"] == "search_field_not_found"


# ---------------------------------------------------------------------------
# Fallback: submit button not found → Enter key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_submit_button_not_found_uses_enter(mock_browser, mock_llm):
    """Submit button not found → should press Return, not crash."""
    import browser_control
    import page_analyzer

    # First find_element_coordinates call: field found (coords)
    # Second call: submit button not found
    mock_llm.find_element_coordinates = AsyncMock(
        side_effect=[
            COORDS_FOUND,  # search field
            COORDS_NOT_FOUND,  # submit button
        ]
    )

    events = await collect(page_analyzer.search_page("test"))
    res = result_event(events)

    # Should still succeed
    assert res is not None

    # Return key should have been pressed
    press_calls = [str(c) for c in mock_browser.press_key.call_args_list]
    assert any("Return" in c for c in press_calls), (
        f"Expected press_key('Return') but got: {press_calls}"
    )


# ---------------------------------------------------------------------------
# Error: analyze_page_structure None after search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_structure_none_after_search(mock_browser, mock_llm):
    """LLM fails on result page analysis → error event, no AttributeError."""
    import page_analyzer

    mock_llm.analyze_page_structure = AsyncMock(return_value=None)

    events = await collect(page_analyzer.search_page("test"))

    assert result_event(events) is None
    errs = error_events(events)
    assert len(errs) > 0
    assert errs[0]["stage"] == "analysis_failed"
