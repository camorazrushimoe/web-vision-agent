"""
Tests for llm_client.py

Covers:
- _parse_json_response: clean JSON, markdown blocks, broken JSON, nested objects
- detect_input_fields: happy path, None from LLM, malformed JSON, missing key
- analyze_page_content_batch: happy path, None from LLM, empty screenshots, multiple
- merge_content_analyses: happy path, empty list, LLM failure
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image

from conftest import make_image, INPUT_FIELDS_OK, CONTENT_BATCH_OK, CONTENT_MERGE_OK


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
    from llm_client import detect_input_fields

    with patch("llm_client._call_llm", new=AsyncMock(return_value=None)):
        result = await detect_input_fields(make_image())
    assert result == {"input_fields": []}


@pytest.mark.asyncio
async def test_detect_input_fields_malformed_json():
    from llm_client import detect_input_fields

    with patch(
        "llm_client._call_llm", new=AsyncMock(return_value="Sorry, I cannot do that.")
    ):
        result = await detect_input_fields(make_image())
    assert result == {"input_fields": []}


@pytest.mark.asyncio
async def test_detect_input_fields_missing_key():
    from llm_client import detect_input_fields

    with patch(
        "llm_client._call_llm", new=AsyncMock(return_value='{"something_else": []}')
    ):
        result = await detect_input_fields(make_image())
    assert result == {"input_fields": []}


@pytest.mark.asyncio
async def test_detect_input_fields_empty_list():
    from llm_client import detect_input_fields

    with patch(
        "llm_client._call_llm", new=AsyncMock(return_value='{"input_fields": []}')
    ):
        result = await detect_input_fields(make_image())
    assert result == {"input_fields": []}


# ---------------------------------------------------------------------------
# analyze_page_content_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_page_content_batch_happy_path():
    from llm_client import analyze_page_content_batch

    labels = [{"region": "top", "index": 1}]
    with patch(
        "llm_client._call_llm", new=AsyncMock(return_value=json.dumps(CONTENT_BATCH_OK))
    ):
        result = await analyze_page_content_batch([make_image()], labels)
    assert result is not None
    assert result["page_type_hint"] == "product_list"
    assert "top" in result


@pytest.mark.asyncio
async def test_analyze_page_content_batch_llm_returns_none():
    from llm_client import analyze_page_content_batch

    with patch("llm_client._call_llm", new=AsyncMock(return_value=None)):
        result = await analyze_page_content_batch(
            [make_image()], [{"region": "top", "index": 1}]
        )
    assert result is None


@pytest.mark.asyncio
async def test_analyze_page_content_batch_empty_list():
    from llm_client import analyze_page_content_batch

    result = await analyze_page_content_batch([], [])
    assert result is None


@pytest.mark.asyncio
async def test_analyze_page_content_batch_multiple_screenshots():
    from llm_client import analyze_page_content_batch

    captured_messages = []

    async def fake_call_llm(base_url, model, messages, max_tokens):
        captured_messages.extend(messages)
        return json.dumps(CONTENT_BATCH_OK)

    screenshots = [make_image() for _ in range(3)]
    labels = [
        {"region": "top", "index": 1},
        {"region": "bottom", "index": 1},
        {"region": "middle", "index": 1},
    ]
    with patch("llm_client._call_llm", new=fake_call_llm):
        await analyze_page_content_batch(screenshots, labels)

    image_count = sum(
        1
        for msg in captured_messages
        for item in (msg.get("content") if isinstance(msg.get("content"), list) else [])
        if isinstance(item, dict) and item.get("type") == "image_url"
    )
    assert image_count == 3, f"Expected 3 images sent to LLM, got {image_count}"


# ---------------------------------------------------------------------------
# merge_content_analyses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_content_analyses_happy_path():
    from llm_client import merge_content_analyses

    partials = [CONTENT_BATCH_OK, CONTENT_BATCH_OK]
    with patch("llm_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__aenter__.return_value
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps(CONTENT_MERGE_OK)}}]
        }
        mock_response.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        result = await merge_content_analyses(partials)
    assert result is not None
    assert result["page_type"] == "product_list"
    assert "top_context" in result
    assert "bottom_context" in result


@pytest.mark.asyncio
async def test_merge_content_analyses_empty_list():
    from llm_client import merge_content_analyses

    result = await merge_content_analyses([])
    assert result is None


@pytest.mark.asyncio
async def test_merge_content_analyses_llm_fails():
    from llm_client import merge_content_analyses
    import httpx

    with patch("llm_client.httpx.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__aenter__.return_value
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        result = await merge_content_analyses([CONTENT_BATCH_OK])
    assert result is None
