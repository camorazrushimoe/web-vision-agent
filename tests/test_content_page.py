"""
Tests for page_analyzer.py — content_page flow (content-analysis-v2).

Covers:
- Happy path: result contains all expected top/middle/bottom context fields
- Top/bottom region capture: correct scroll functions called
- Short page: middle capture skipped
- overlap_stop_enabled=False: middle always skipped
- Single batch (≤ llm_batch_size): merge step skipped, result reshaped directly
- Multiple batches: merge step called
- One batch returns None: continues with rest, no analysis_failed
- All batches return None: error event analysis_failed
- merge returns None: error event merge_failed
- scroll_to_top called after capture phase (at least twice)
- recorder=None accepted without error
- SSE stages emitted: capturing_top, capturing_bottom, analyzing
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from conftest import (
    make_image,
    make_different_image,
    CONTENT_BATCH_OK,
    CONTENT_MERGE_OK,
    collect,
    result_event,
    error_events,
)


# ---------------------------------------------------------------------------
# Happy path — result structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_result_has_all_keys(mock_browser, mock_llm):
    import page_analyzer

    events = await collect(page_analyzer.content_page())
    res = result_event(events)
    assert res is not None
    assert res["type"] == "result"
    for key in (
        "current_url",
        "page_type",
        "content_summary",
        "top_context",
        "middle_context",
        "bottom_context",
        "screenshots_taken",
        "batches_sent",
    ):
        assert key in res, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_content_page_top_context_structure(mock_browser, mock_llm):
    import page_analyzer

    events = await collect(page_analyzer.content_page())
    res = result_event(events)
    top = res["top_context"]
    assert "navigation_items" in top
    assert "hero_or_title" in top
    assert "ctas" in top


@pytest.mark.asyncio
async def test_content_page_bottom_context_structure(mock_browser, mock_llm):
    import page_analyzer

    events = await collect(page_analyzer.content_page())
    res = result_event(events)
    bottom = res["bottom_context"]
    assert "footer_links" in bottom
    assert "copyright_or_org" in bottom


@pytest.mark.asyncio
async def test_content_page_middle_context_structure(mock_browser, mock_llm):
    import page_analyzer

    events = await collect(page_analyzer.content_page())
    res = result_event(events)
    middle = res["middle_context"]
    assert "themes" in middle
    assert "key_sections" in middle
    assert "items_count_estimate" in middle


# ---------------------------------------------------------------------------
# Scroll functions called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_calls_scroll_to_top(mock_browser, mock_llm):
    import page_analyzer

    await collect(page_analyzer.content_page())
    mock_browser.scroll_to_top.assert_called()


@pytest.mark.asyncio
async def test_content_page_calls_scroll_to_bottom(mock_browser, mock_llm):
    import page_analyzer

    await collect(page_analyzer.content_page())
    mock_browser.scroll_to_bottom.assert_called()


@pytest.mark.asyncio
async def test_content_page_top_screenshots_taken(mock_browser, mock_llm):
    import page_analyzer

    mock_browser.pixel_difference = MagicMock(return_value=50.0)
    events = await collect(page_analyzer.content_page(max_top_screens=2))
    res = result_event(events)
    assert res is not None
    assert res["screenshots_taken"] >= 2


# ---------------------------------------------------------------------------
# Short page — middle skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_short_page_no_middle(mock_browser, mock_llm):
    import page_analyzer

    mock_browser.pixel_difference = MagicMock(return_value=0.0)
    events = await collect(page_analyzer.content_page())
    res = result_event(events)
    assert res is not None
    stage_events = [e.get("stage") for e in events if "stage" in e]
    assert "capturing_middle" not in stage_events


# ---------------------------------------------------------------------------
# overlap_stop_enabled=False → no middle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_overlap_stop_disabled_no_middle(mock_browser, mock_llm):
    import page_analyzer

    mock_browser.pixel_difference = MagicMock(return_value=50.0)
    events = await collect(page_analyzer.content_page(overlap_stop_enabled=False))
    res = result_event(events)
    assert res is not None
    stage_events = [e.get("stage") for e in events if "stage" in e]
    assert "capturing_middle" not in stage_events


# ---------------------------------------------------------------------------
# Single batch — merge skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_single_batch_no_merge(mock_browser, mock_llm):
    import page_analyzer

    mock_browser.pixel_difference = MagicMock(return_value=0.0)
    events = await collect(page_analyzer.content_page(llm_batch_size=10))
    res = result_event(events)
    assert res is not None
    mock_llm.merge_content_analyses.assert_not_called()
    stage_events = [e.get("stage") for e in events if "stage" in e]
    assert "merging" not in stage_events


@pytest.mark.asyncio
async def test_content_page_single_batch_result_reshaped(mock_browser, mock_llm):
    import page_analyzer

    mock_browser.pixel_difference = MagicMock(return_value=0.0)
    events = await collect(page_analyzer.content_page(llm_batch_size=10))
    res = result_event(events)
    assert res is not None
    assert res["page_type"] == CONTENT_BATCH_OK["page_type_hint"]
    assert res["top_context"] == CONTENT_BATCH_OK["top"]
    assert res["bottom_context"] == CONTENT_BATCH_OK["bottom"]


# ---------------------------------------------------------------------------
# Multiple batches — merge called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_multiple_batches_merge_called(mock_browser, mock_llm):
    import page_analyzer

    mock_browser.pixel_difference = MagicMock(return_value=50.0)
    events = await collect(
        page_analyzer.content_page(
            max_top_screens=2, max_bottom_screens=2, llm_batch_size=1
        )
    )
    res = result_event(events)
    assert res is not None
    mock_llm.merge_content_analyses.assert_called_once()
    assert res["page_type"] == CONTENT_MERGE_OK["page_type"]
    assert res["content_summary"] == CONTENT_MERGE_OK["content_summary"]


@pytest.mark.asyncio
async def test_content_page_multiple_batches_merging_stage_emitted(
    mock_browser, mock_llm
):
    import page_analyzer

    mock_browser.pixel_difference = MagicMock(return_value=50.0)
    events = await collect(
        page_analyzer.content_page(
            max_top_screens=2, max_bottom_screens=2, llm_batch_size=1
        )
    )
    stage_events = [e.get("stage") for e in events if "stage" in e]
    assert "merging" in stage_events


# ---------------------------------------------------------------------------
# Error: one batch returns None — continues with rest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_one_batch_none_continues(mock_browser, mock_llm):
    import page_analyzer

    mock_browser.pixel_difference = MagicMock(return_value=0.0)
    mock_llm.analyze_page_content_batch = AsyncMock(
        side_effect=[None, CONTENT_BATCH_OK]
    )
    events = await collect(
        page_analyzer.content_page(
            max_top_screens=1, max_bottom_screens=1, llm_batch_size=1
        )
    )
    errs = error_events(events)
    analysis_failed = [e for e in errs if e.get("stage") == "analysis_failed"]
    assert len(analysis_failed) == 0


# ---------------------------------------------------------------------------
# Error: all batches return None → analysis_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_all_batches_none_error(mock_browser, mock_llm):
    import page_analyzer

    mock_llm.analyze_page_content_batch = AsyncMock(return_value=None)
    events = await collect(page_analyzer.content_page())
    assert result_event(events) is None
    errs = error_events(events)
    assert len(errs) > 0
    assert errs[0]["stage"] == "analysis_failed"


# ---------------------------------------------------------------------------
# Error: merge returns None → merge_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_merge_returns_none_error(mock_browser, mock_llm):
    import page_analyzer

    mock_browser.pixel_difference = MagicMock(return_value=50.0)
    mock_llm.merge_content_analyses = AsyncMock(return_value=None)
    events = await collect(
        page_analyzer.content_page(
            max_top_screens=2, max_bottom_screens=2, llm_batch_size=1
        )
    )
    assert result_event(events) is None
    errs = error_events(events)
    assert len(errs) > 0
    assert errs[0]["stage"] == "merge_failed"


# ---------------------------------------------------------------------------
# scroll_to_top called after capture (at least 2 times)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_scroll_to_top_after_capture(mock_browser, mock_llm):
    import page_analyzer

    await collect(page_analyzer.content_page())
    assert mock_browser.scroll_to_top.call_count >= 2


# ---------------------------------------------------------------------------
# recorder=None accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_recorder_none_accepted(mock_browser, mock_llm):
    import page_analyzer

    events = await collect(page_analyzer.content_page(recorder=None))
    res = result_event(events)
    assert res is not None
    assert res["type"] == "result"


# ---------------------------------------------------------------------------
# SSE stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_page_emits_capturing_top_stage(mock_browser, mock_llm):
    import page_analyzer

    events = await collect(page_analyzer.content_page())
    stage_events = [e.get("stage") for e in events if "stage" in e]
    assert "capturing_top" in stage_events


@pytest.mark.asyncio
async def test_content_page_emits_capturing_bottom_stage(mock_browser, mock_llm):
    import page_analyzer

    events = await collect(page_analyzer.content_page())
    stage_events = [e.get("stage") for e in events if "stage" in e]
    assert "capturing_bottom" in stage_events


@pytest.mark.asyncio
async def test_content_page_emits_analyzing_stage(mock_browser, mock_llm):
    import page_analyzer

    events = await collect(page_analyzer.content_page())
    stage_events = [e.get("stage") for e in events if "stage" in e]
    assert "analyzing" in stage_events
