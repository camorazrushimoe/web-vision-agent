"""
Tests for llm_client.py

Covers:
- _parse_json_response: clean JSON, markdown blocks, broken JSON, nested objects
- detect_input_fields: happy path, None from LLM, malformed JSON, missing key
- analyze_page_content: happy path, None from LLM, screenshot capping at 3
"""

import json
import pytest
from unittest.mock import AsyncMock, patch
from PIL import Image

from conftest import make_image, INPUT_FIELDS_OK, CONTENT_OK


# ---------------------------------------------------------------------------
# _parse_json_response
# ---------------------------------------------------------------------------


def test_parse_json_clean():
    from llm_client import _parse_json_response

    result = _parse_json_response('{"key": "value"}')
    assert result == {"key": "value"}


def test_parse_json_markdown_block():
    from llm_client import _parse_json_response

    response = '```json\n{"key": "value"}\n```'
    result = _parse_json_response(response)
    assert result == {"key": "value"}


def test_parse_json_markdown_no_lang():
    from llm_client import _parse_json_response

    response = '```\n{"key": "value"}\n```'
    result = _parse_json_response(response)
    assert result == {"key": "value"}


def test_parse_json_embedded_in_text():
    from llm_client import _parse_json_response

    response = 'Here is the result: {"key": "value"} done.'
    result = _parse_json_response(response)
    assert result == {"key": "value"}


def test_parse_json_broken_returns_none():
    from llm_client import _parse_json_response

    result = _parse_json_response("this is not json at all")
    assert result is None


def test_parse_json_empty_string():
    from llm_client import _parse_json_response

    result = _parse_json_response("")
    assert result is None


def test_parse_json_nested():
    from llm_client import _parse_json_response

    raw = '{"input_fields": [{"type": "search", "label": "s", "description": "d"}]}'
    result = _parse_json_response(raw)
    assert result["input_fields"][0]["type"] == "search"


# ---------------------------------------------------------------------------
# detect_input_fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_input_fields_happy_path():
    """LLM returns valid input_fields — should return them as-is."""
    from llm_client import detect_input_fields

    with patch(
        "llm_client._call_llm", new=AsyncMock(return_value=json.dumps(INPUT_FIELDS_OK))
    ):
        result = await detect_input_fields(make_image())

    assert result is not None
    assert "input_fields" in result
    assert result["input_fields"][0]["type"] == "search"


@pytest.mark.asyncio
async def test_detect_input_fields_llm_returns_none():
    """LLM timeout/error → should return empty input_fields, not raise."""
    from llm_client import detect_input_fields

    with patch("llm_client._call_llm", new=AsyncMock(return_value=None)):
        result = await detect_input_fields(make_image())

    assert result == {"input_fields": []}


@pytest.mark.asyncio
async def test_detect_input_fields_malformed_json():
    """LLM returns garbage text → should return empty input_fields."""
    from llm_client import detect_input_fields

    with patch(
        "llm_client._call_llm", new=AsyncMock(return_value="Sorry, I cannot do that.")
    ):
        result = await detect_input_fields(make_image())

    assert result == {"input_fields": []}


@pytest.mark.asyncio
async def test_detect_input_fields_missing_key():
    """LLM returns valid JSON but without input_fields key → normalise to empty list."""
    from llm_client import detect_input_fields

    with patch(
        "llm_client._call_llm", new=AsyncMock(return_value='{"something_else": []}')
    ):
        result = await detect_input_fields(make_image())

    assert result == {"input_fields": []}


@pytest.mark.asyncio
async def test_detect_input_fields_empty_list():
    """LLM explicitly says no fields found → valid, return empty list."""
    from llm_client import detect_input_fields

    with patch(
        "llm_client._call_llm", new=AsyncMock(return_value='{"input_fields": []}')
    ):
        result = await detect_input_fields(make_image())

    assert result == {"input_fields": []}


# ---------------------------------------------------------------------------
# analyze_page_content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_page_content_happy_path():
    """Single screenshot, LLM returns valid content analysis."""
    from llm_client import analyze_page_content

    with patch(
        "llm_client._call_llm", new=AsyncMock(return_value=json.dumps(CONTENT_OK))
    ):
        result = await analyze_page_content([make_image()])

    assert result is not None
    assert result["content_type"] == "product_list"
    assert result["items_count"] == 2


@pytest.mark.asyncio
async def test_analyze_page_content_llm_returns_none():
    """LLM failure → should return None (caller handles it)."""
    from llm_client import analyze_page_content

    with patch("llm_client._call_llm", new=AsyncMock(return_value=None)):
        result = await analyze_page_content([make_image()])

    assert result is None


@pytest.mark.asyncio
async def test_analyze_page_content_caps_at_3_screenshots():
    """Even if 5 screenshots are passed, only 3 are sent to LLM."""
    from llm_client import analyze_page_content

    captured_messages = []

    async def fake_call_llm(base_url, model, messages, max_tokens):
        captured_messages.extend(messages)
        return json.dumps(CONTENT_OK)

    screenshots = [make_image() for _ in range(5)]
    with patch("llm_client._call_llm", new=fake_call_llm):
        await analyze_page_content(screenshots)

    # Count image_url entries in the message content
    image_count = sum(
        1
        for msg in captured_messages
        for item in (msg.get("content") if isinstance(msg.get("content"), list) else [])
        if isinstance(item, dict) and item.get("type") == "image_url"
    )
    assert image_count == 3, f"Expected 3 images sent to LLM, got {image_count}"


@pytest.mark.asyncio
async def test_analyze_page_content_empty_list():
    """Empty screenshots list → return None immediately."""
    from llm_client import analyze_page_content

    result = await analyze_page_content([])
    assert result is None
