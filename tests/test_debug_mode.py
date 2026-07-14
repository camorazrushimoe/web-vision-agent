"""
Tests for Debug Mode capability.

Covers:
- DebugRecorder: step() saves files, writes log, returns SSE-ready dict
- DebugRecorder: annotated copy created only when coords provided
- DebugRecorder: original screenshot is never modified
- DebugRecorder: folder rotation — max 10 operations, oldest deleted
- DebugRecorder: step() with coords=None does not crash
- DebugRecorder: finish() writes final entry to log
- page_analyzer flows: recorder=None works identically to current behaviour
- page_analyzer flows: recorder is called at expected steps
- api.py: run_with_timeout() routes stage="debug" events as event: debug (not status)
- api.py: debug field "[DEBUG MODE ON]" injected into every SSE event when enabled
- GET /debug: returns null current_operation_id when idle
- GET /debug: returns current operation_id when busy
"""

import asyncio
import os
import shutil
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call
from pathlib import Path

import pytest
from PIL import Image

from conftest import (
    make_image,
    make_different_image,
    STRUCTURE_OK,
    INPUT_FIELDS_OK,
    COORDS_FOUND,
    collect,
    result_event,
    error_events,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_recorder(
    tmp_dir: str, operation_name: str = "search", ts: str = "20240715_143022"
):
    """Import and instantiate DebugRecorder pointing at a temp dir."""
    import debug_recorder as dr

    operation_id = f"{operation_name}_{ts}"
    return dr.DebugRecorder(
        operation_id=operation_id, operation_name=operation_name, base_dir=tmp_dir
    )


# ---------------------------------------------------------------------------
# DebugRecorder — basic step() behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_step_saves_original(tmp_path):
    """step() must save the original screenshot as a PNG file."""
    rec = make_recorder(str(tmp_path))
    screenshot = make_image()

    await rec.step("after_click", screenshot, coords=None, meta={})

    saved = list(tmp_path.rglob("*.png"))
    assert len(saved) >= 1


@pytest.mark.asyncio
async def test_recorder_step_with_coords_saves_annotated(tmp_path):
    """step() with coords creates both original and annotated copy."""
    rec = make_recorder(str(tmp_path))
    screenshot = make_image()

    await rec.step("after_click", screenshot, coords=(50, 50), meta={})

    pngs = list(tmp_path.rglob("*.png"))
    names = [p.name for p in pngs]
    # At least one annotated file
    assert any("annotated" in n for n in names), f"No annotated file found: {names}"
    # At least one non-annotated original
    assert any("annotated" not in n for n in names), f"No original file found: {names}"


@pytest.mark.asyncio
async def test_recorder_step_original_not_modified(tmp_path):
    """Original PIL Image passed to step() must not be modified."""
    rec = make_recorder(str(tmp_path))
    original = make_image(color=(255, 0, 0))
    original_pixels = list(original.getdata())

    await rec.step("after_click", original, coords=(50, 50), meta={})

    assert list(original.getdata()) == original_pixels, "Original image was mutated"


@pytest.mark.asyncio
async def test_recorder_step_no_coords_no_annotated(tmp_path):
    """step() with coords=None must NOT create an annotated file."""
    rec = make_recorder(str(tmp_path))
    screenshot = make_image()

    await rec.step("after_typing", screenshot, coords=None, meta={})

    pngs = list(tmp_path.rglob("*.png"))
    names = [p.name for p in pngs]
    assert not any("annotated" in n for n in names), (
        f"Unexpected annotated file: {names}"
    )


@pytest.mark.asyncio
async def test_recorder_step_returns_sse_dict(tmp_path):
    """step() must return a dict suitable for SSE debug event."""
    rec = make_recorder(str(tmp_path))
    screenshot = make_image()

    result = await rec.step(
        "after_click", screenshot, coords=(50, 50), meta={"key": "val"}
    )

    assert isinstance(result, dict)
    assert result.get("stage") == "debug"
    assert "step" in result
    assert "operation_id" in result
    assert result.get("debug") == "[DEBUG MODE ON]"


@pytest.mark.asyncio
async def test_recorder_step_no_screenshot_no_crash(tmp_path):
    """step() with screenshot=None must not raise."""
    rec = make_recorder(str(tmp_path))

    result = await rec.step(
        "on_error", screenshot=None, coords=None, meta={"error": "timeout"}
    )

    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# DebugRecorder — debug.log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_writes_to_log(tmp_path):
    """Each step() call appends a line to debug.log."""
    rec = make_recorder(str(tmp_path))
    screenshot = make_image()

    await rec.step("step_one", screenshot, coords=None, meta={"foo": "bar"})
    await rec.step("step_two", screenshot, coords=None, meta={})

    log_files = list(tmp_path.rglob("debug.log"))
    assert len(log_files) == 1

    content = log_files[0].read_text()
    assert "step_one" in content
    assert "step_two" in content


@pytest.mark.asyncio
async def test_recorder_finish_writes_to_log(tmp_path):
    """finish() must append a final line to debug.log."""
    rec = make_recorder(str(tmp_path))
    screenshot = make_image()
    await rec.step("before_action", screenshot, coords=None, meta={})

    rec.finish({"result_type": "page_reload"})

    log_files = list(tmp_path.rglob("debug.log"))
    content = log_files[0].read_text()
    assert "result_type" in content or "page_reload" in content


# ---------------------------------------------------------------------------
# DebugRecorder — folder rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_rotation_keeps_max_10(tmp_path):
    """After 11 operations, only 10 folders should remain."""
    import debug_recorder as dr

    for i in range(11):
        op_id = f"search_2024071{i:01d}_14302{i:01d}"
        rec = dr.DebugRecorder(
            operation_id=op_id, operation_name="search", base_dir=str(tmp_path)
        )
        screenshot = make_image()
        await rec.step("before_action", screenshot, coords=None, meta={})

    dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
    assert len(dirs) <= 10, f"Expected <= 10 dirs, got {len(dirs)}"


@pytest.mark.asyncio
async def test_recorder_rotation_deletes_oldest(tmp_path):
    """After overflow, the oldest folder is deleted."""
    import debug_recorder as dr

    created = []
    for i in range(11):
        op_id = f"op_{i:02d}_20240715_143{i:03d}"
        rec = dr.DebugRecorder(
            operation_id=op_id, operation_name="op", base_dir=str(tmp_path)
        )
        screenshot = make_image()
        await rec.step("before_action", screenshot, coords=None, meta={})
        created.append(op_id)
        # small sleep so mtime differs
        await asyncio.sleep(0.01)

    dirs = {d.name for d in tmp_path.iterdir() if d.is_dir()}
    # The very first operation should be gone
    assert created[0] not in dirs, f"Oldest dir {created[0]} was not deleted"
    # The last one should be present
    assert created[-1] in dirs, f"Newest dir {created[-1]} is missing"


# ---------------------------------------------------------------------------
# page_analyzer — recorder=None is backwards-compatible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_page_no_recorder_unchanged(mock_browser, mock_llm):
    """open_page(recorder=None) behaves identically to open_page() without param."""
    import page_analyzer

    events = await collect(
        page_analyzer.open_page("https://example.com", recorder=None)
    )
    res = result_event(events)

    assert res is not None
    assert res["type"] == "result"
    assert res["structure"] == STRUCTURE_OK


@pytest.mark.asyncio
async def test_search_page_no_recorder_unchanged(mock_browser, mock_llm):
    """search_page(recorder=None) behaves identically to current search_page()."""
    import page_analyzer

    events = await collect(page_analyzer.search_page("test", recorder=None))
    res = result_event(events)

    assert res is not None
    assert res["search_result"]["query"] == "test"


@pytest.mark.asyncio
async def test_click_element_no_recorder_unchanged(mock_browser, mock_llm):
    """click_element(recorder=None) behaves identically."""
    import page_analyzer

    events = await collect(page_analyzer.click_element("Sign in", recorder=None))
    res = result_event(events)

    assert res is not None
    assert res["clicked"] == "Sign in"


@pytest.mark.asyncio
async def test_scan_page_no_recorder_unchanged(mock_browser, mock_llm):
    """scan_page(recorder=None) behaves identically."""
    import page_analyzer

    events = await collect(page_analyzer.scan_page(recorder=None))
    res = result_event(events)

    assert res is not None
    assert res["type"] == "result"


@pytest.mark.asyncio
async def test_content_page_no_recorder_unchanged(mock_browser, mock_llm):
    """content_page(recorder=None) behaves identically."""
    import page_analyzer

    events = await collect(page_analyzer.content_page(recorder=None))
    res = result_event(events)

    assert res is not None
    assert res["type"] == "result"


# ---------------------------------------------------------------------------
# page_analyzer — recorder is called at expected steps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_page_recorder_called_on_click(mock_browser, mock_llm, tmp_path):
    """search_page with recorder: step() called after click with coords."""
    import debug_recorder as dr
    import page_analyzer

    rec = make_recorder(str(tmp_path), operation_name="search")
    rec.step = AsyncMock(
        return_value={
            "stage": "debug",
            "step": "after_click",
            "operation_id": rec.operation_id,
            "debug": "[DEBUG MODE ON]",
        }
    )

    await collect(page_analyzer.search_page("test", recorder=rec))

    # step must have been called at least once with after_click
    step_calls = [c for c in rec.step.call_args_list if c.args and "click" in c.args[0]]
    assert len(step_calls) >= 1, "recorder.step() was not called for after_click"


@pytest.mark.asyncio
async def test_search_page_recorder_called_on_typing(mock_browser, mock_llm, tmp_path):
    """search_page with recorder: step() called after typing."""
    import debug_recorder as dr
    import page_analyzer

    rec = make_recorder(str(tmp_path), operation_name="search")
    rec.step = AsyncMock(
        return_value={
            "stage": "debug",
            "step": "after_typing",
            "operation_id": rec.operation_id,
            "debug": "[DEBUG MODE ON]",
        }
    )

    await collect(page_analyzer.search_page("test", recorder=rec))

    step_calls = [c for c in rec.step.call_args_list if c.args and "typ" in c.args[0]]
    assert len(step_calls) >= 1, "recorder.step() was not called for after_typing"


@pytest.mark.asyncio
async def test_open_page_recorder_called_on_before_action(
    mock_browser, mock_llm, tmp_path
):
    """open_page with recorder: step('before_action') called at start."""
    import page_analyzer

    rec = make_recorder(str(tmp_path), operation_name="open")
    rec.step = AsyncMock(
        return_value={
            "stage": "debug",
            "step": "before_action",
            "operation_id": rec.operation_id,
            "debug": "[DEBUG MODE ON]",
        }
    )

    await collect(page_analyzer.open_page("https://example.com", recorder=rec))

    step_calls = [
        c for c in rec.step.call_args_list if c.args and "before" in c.args[0]
    ]
    assert len(step_calls) >= 1, "recorder.step('before_action') was not called"


# ---------------------------------------------------------------------------
# api.py — run_with_timeout() routes debug events correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_timeout_routes_debug_events():
    """Events with stage='debug' must be routed as event: debug, not event: status."""
    import api
    import json

    debug_event = {
        "stage": "debug",
        "step": "after_click",
        "operation_id": "search_123",
        "debug": "[DEBUG MODE ON]",
    }

    async def fake_generator():
        yield debug_event

    collected = []
    async for event in api.run_with_timeout(fake_generator(), "search"):
        collected.append(event)

    debug_events_routed = [e for e in collected if e.get("event") == "debug"]
    status_events_routed = [
        e
        for e in collected
        if e.get("event") == "status" and json.loads(e["data"]).get("stage") == "debug"
    ]

    assert len(debug_events_routed) >= 1, "Debug events were not routed as event: debug"
    assert len(status_events_routed) == 0, "Debug events leaked into event: status"


@pytest.mark.asyncio
async def test_run_with_timeout_status_events_unaffected():
    """Normal status events must still be routed as event: status."""
    import api

    async def fake_generator():
        yield {
            "stage": "clicking",
            "message": "Clicking...",
            "current_url": "https://x.com",
        }

    collected = []
    async for event in api.run_with_timeout(fake_generator(), "click"):
        collected.append(event)

    status_events = [e for e in collected if e.get("event") == "status"]
    assert len(status_events) >= 1


# ---------------------------------------------------------------------------
# api.py — debug field injected into SSE events when debug_enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_field_injected_when_enabled():
    """When debug_enabled=True, every SSE event data must contain 'debug' field."""
    import api
    import json

    # Enable debug mode
    api.state.debug_enabled = True

    async def fake_generator():
        yield {
            "stage": "loading",
            "message": "Loading...",
            "current_url": "https://x.com",
        }

    try:
        collected = []
        async for event in api.run_with_timeout(fake_generator(), "open"):
            collected.append(event)

        for ev in collected:
            data = json.loads(ev["data"])
            assert "debug" in data, f"'debug' field missing from event: {data}"
            assert data["debug"] == "[DEBUG MODE ON]"
    finally:
        api.state.debug_enabled = False


@pytest.mark.asyncio
async def test_debug_field_absent_when_disabled():
    """When debug_enabled=False, events must NOT contain 'debug' field."""
    import api
    import json

    api.state.debug_enabled = False

    async def fake_generator():
        yield {
            "stage": "loading",
            "message": "Loading...",
            "current_url": "https://x.com",
        }

    collected = []
    async for event in api.run_with_timeout(fake_generator(), "open"):
        collected.append(event)

    for ev in collected:
        data = json.loads(ev["data"])
        assert "debug" not in data, f"Unexpected 'debug' field in event: {data}"


# ---------------------------------------------------------------------------
# GET /debug — current_operation_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_debug_returns_null_operation_id_when_idle():
    """GET /debug must return current_operation_id=null when agent is idle."""
    import api
    from fastapi.testclient import TestClient

    # Ensure idle state
    api.state.busy = False
    api.state.current_operation = None
    api.state.debug_enabled = False

    client = TestClient(api.app)
    response = client.get("/debug")

    assert response.status_code == 200
    data = response.json()
    assert "debug_enabled" in data
    assert data["current_operation_id"] is None


@pytest.mark.asyncio
async def test_get_debug_returns_operation_id_when_busy():
    """GET /debug must return current_operation_id when agent is busy."""
    import api
    from fastapi.testclient import TestClient

    api.state.busy = True
    api.state.current_operation = "search"
    api.state.debug_enabled = True
    # Simulate active operation_id stored in state
    api.state.current_operation_id = "search_20240715_143022"

    try:
        client = TestClient(api.app)
        response = client.get("/debug")

        assert response.status_code == 200
        data = response.json()
        assert data["current_operation_id"] == "search_20240715_143022"
    finally:
        api.state.busy = False
        api.state.current_operation = None
        api.state.current_operation_id = None
        api.state.debug_enabled = False


# ---------------------------------------------------------------------------
# POST /debug — toggle
# ---------------------------------------------------------------------------


def test_post_debug_enable():
    """POST /debug with enabled=true sets state.debug_enabled=True."""
    import api
    from fastapi.testclient import TestClient

    api.state.debug_enabled = False
    client = TestClient(api.app)

    response = client.post("/debug", json={"enabled": True})

    assert response.status_code == 200
    assert api.state.debug_enabled is True

    # Cleanup
    api.state.debug_enabled = False


def test_post_debug_disable():
    """POST /debug with enabled=false sets state.debug_enabled=False."""
    import api
    from fastapi.testclient import TestClient

    api.state.debug_enabled = True
    client = TestClient(api.app)

    response = client.post("/debug", json={"enabled": False})

    assert response.status_code == 200
    assert api.state.debug_enabled is False
