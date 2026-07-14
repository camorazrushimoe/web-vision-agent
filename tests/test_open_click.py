"""
Tests for page_analyzer.py — open_page and click_element flows.

Covers:
- Happy path: result event contains structure + input_fields
- detect_input_fields returns None → input_fields=[] in result, no crash
- analyze_page_structure returns None → error event yielded, no AttributeError
- input_fields is always present in result (even empty)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from conftest import (
    make_image,
    STRUCTURE_OK,
    INPUT_FIELDS_OK,
    COORDS_FOUND,
    collect,
    result_event,
    error_events,
)


# ---------------------------------------------------------------------------
# open_page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_page_happy_path(mock_browser, mock_llm):
    """open_page should yield a result with structure and input_fields."""
    import page_analyzer

    events = await collect(page_analyzer.open_page("https://example.com"))
    res = result_event(events)

    assert res is not None
    assert res["type"] == "result"
    assert res["structure"] == STRUCTURE_OK
    assert res["input_fields"] == INPUT_FIELDS_OK["input_fields"]
    assert res["current_url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_open_page_input_fields_none(mock_browser, mock_llm):
    """detect_input_fields returns None → result still arrives, input_fields=[]."""
    import page_analyzer

    mock_llm.detect_input_fields = AsyncMock(return_value=None)

    events = await collect(page_analyzer.open_page("https://example.com"))
    res = result_event(events)

    assert res is not None
    assert res["input_fields"] == []


@pytest.mark.asyncio
async def test_open_page_structure_none_yields_error(mock_browser, mock_llm):
    """analyze_page_structure returns None → error event, no AttributeError."""
    import page_analyzer

    mock_llm.analyze_page_structure = AsyncMock(return_value=None)

    events = await collect(page_analyzer.open_page("https://example.com"))

    assert result_event(events) is None
    errs = error_events(events)
    assert len(errs) > 0
    assert errs[0]["stage"] == "analysis_failed"


@pytest.mark.asyncio
async def test_open_page_always_has_input_fields_key(mock_browser, mock_llm):
    """input_fields key must always be present in result."""
    import page_analyzer

    events = await collect(page_analyzer.open_page("https://example.com"))
    res = result_event(events)

    assert "input_fields" in res


# ---------------------------------------------------------------------------
# click_element
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_element_happy_path(mock_browser, mock_llm):
    """click_element should find element, click, and return result with input_fields."""
    import page_analyzer

    events = await collect(page_analyzer.click_element("Sign in"))
    res = result_event(events)

    assert res is not None
    assert res["clicked"] == "Sign in"
    assert "click_coordinates" in res
    assert "input_fields" in res
    assert res["input_fields"] == INPUT_FIELDS_OK["input_fields"]


@pytest.mark.asyncio
async def test_click_element_not_found(mock_browser, mock_llm):
    """Element not found → error event, no result."""
    import page_analyzer

    mock_llm.find_element_coordinates = AsyncMock(
        return_value={"found": False, "reason": "not visible"}
    )

    events = await collect(page_analyzer.click_element("Nonexistent button"))

    assert result_event(events) is None
    errs = error_events(events)
    assert len(errs) > 0
    assert errs[0]["stage"] == "element_not_found"


@pytest.mark.asyncio
async def test_click_element_input_fields_none(mock_browser, mock_llm):
    """detect_input_fields None after click → result still arrives, input_fields=[]."""
    import page_analyzer

    # First call (finding the element) returns coords, second call (after click) returns None for fields
    mock_llm.detect_input_fields = AsyncMock(return_value=None)

    events = await collect(page_analyzer.click_element("Sign in"))
    res = result_event(events)

    assert res is not None
    assert res["input_fields"] == []


@pytest.mark.asyncio
async def test_click_element_structure_none_yields_error(mock_browser, mock_llm):
    """analyze_page_structure None after click → error event, no AttributeError."""
    import page_analyzer

    mock_llm.analyze_page_structure = AsyncMock(return_value=None)

    events = await collect(page_analyzer.click_element("Sign in"))

    assert result_event(events) is None
    errs = error_events(events)
    assert len(errs) > 0
