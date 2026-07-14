"""
Web Vision Agent — HTTP API
FastAPI application with SSE streaming endpoints.
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import browser_control
import page_analyzer

logger = logging.getLogger("api")

OPERATION_TIMEOUT = int(os.environ.get("OPERATION_TIMEOUT", "120"))


# --- State ---


class AgentState:
    """Tracks current agent state for concurrency control."""

    def __init__(self):
        self.busy = False
        self.current_operation: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.last_analysis: Optional[dict] = None
        self.last_url: Optional[str] = None
        self._lock = asyncio.Lock()

    async def acquire(self, operation: str) -> bool:
        """Try to acquire the lock. Returns False if already busy."""
        async with self._lock:
            if self.busy:
                return False
            self.busy = True
            self.current_operation = operation
            self.started_at = datetime.now(timezone.utc)
            return True

    async def release(self, url: str = "", analysis: dict = None):
        """Release the lock after operation completes."""
        async with self._lock:
            self.busy = False
            if url:
                self.last_url = url
            if analysis:
                self.last_analysis = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "data": analysis,
                }
            self.current_operation = None
            self.started_at = None


state = AgentState()


# --- App ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("API server started")
    yield
    logger.info("API server shutting down")


app = FastAPI(
    title="Web Vision Agent",
    description="Browser automation with computer vision",
    version="1.0.0",
    lifespan=lifespan,
)


# --- Request Models ---


class OpenRequest(BaseModel):
    url: str


class ClickRequest(BaseModel):
    target: str


class SearchRequest(BaseModel):
    query: str


class ContentRequest(BaseModel):
    full_page: bool = False


# --- Helpers ---


def check_busy():
    """Raise 409 if agent is busy."""
    if state.busy:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "busy",
                "message": "Agent is currently processing another request. Please wait.",
                "current_operation": state.current_operation,
                "started_at": state.started_at.isoformat()
                if state.started_at
                else None,
            },
        )


async def run_with_timeout(generator, operation: str):
    """
    Wrap an async generator with timeout and state management.
    Yields SSE events.
    """
    if not await state.acquire(operation):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "busy",
                "message": "Agent is currently processing another request. Please wait.",
                "current_operation": state.current_operation,
                "started_at": state.started_at.isoformat()
                if state.started_at
                else None,
            },
        )

    last_url = ""
    last_analysis = None

    try:

        async def _produce_events():
            nonlocal last_url, last_analysis

            async for event in generator:
                event_type = event.get("type", "status")

                if event_type == "result":
                    last_url = event.get("current_url", "")
                    last_analysis = event
                    yield {
                        "event": "result",
                        "data": json.dumps(event, ensure_ascii=False),
                    }
                elif "stage" in event and "error" in event.get("stage", ""):
                    yield {
                        "event": "error",
                        "data": json.dumps(event, ensure_ascii=False),
                    }
                else:
                    yield {
                        "event": "status",
                        "data": json.dumps(event, ensure_ascii=False),
                    }

        # Run with timeout
        events = []
        deadline = time.time() + OPERATION_TIMEOUT

        async for event in _produce_events():
            if time.time() > deadline:
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {
                            "stage": "timeout",
                            "message": f"Operation timed out after {OPERATION_TIMEOUT} seconds",
                            "current_url": last_url,
                            "details": "Operation took too long. Agent returned to idle state. You can retry.",
                            "recovery": "Agent returned to idle state. You can retry the request.",
                        },
                        ensure_ascii=False,
                    ),
                }
                return
            yield event

    except Exception as e:
        logger.error(f"Operation {operation} failed: {e}", exc_info=True)
        yield {
            "event": "error",
            "data": json.dumps(
                {
                    "stage": "internal_error",
                    "message": str(e),
                    "current_url": last_url,
                },
                ensure_ascii=False,
            ),
        }
    finally:
        await state.release(url=last_url, analysis=last_analysis)


# --- Endpoints ---


@app.post("/open")
async def open_page(request: OpenRequest):
    """Open a URL, analyze page structure."""
    check_busy()

    generator = page_analyzer.open_page(request.url)

    async def event_stream():
        async for event in run_with_timeout(generator, "open"):
            yield event

    return EventSourceResponse(event_stream())


@app.post("/click")
async def click_element(request: ClickRequest):
    """Click on a named element, then analyze the new page."""
    check_busy()

    generator = page_analyzer.click_element(request.target)

    async def event_stream():
        async for event in run_with_timeout(generator, "click"):
            yield event

    return EventSourceResponse(event_stream())


@app.post("/scan")
async def scan_page():
    """Full page scan with scrolling."""
    check_busy()

    generator = page_analyzer.scan_page()

    async def event_stream():
        async for event in run_with_timeout(generator, "scan"):
            yield event

    return EventSourceResponse(event_stream())


@app.get("/screenshot")
async def get_screenshot():
    """Get current screenshot as PNG."""
    png_bytes = await browser_control.take_screenshot_bytes()
    return Response(content=png_bytes, media_type="image/png")


@app.get("/state")
async def get_state():
    """Get current agent state."""
    current_url = ""
    try:
        if not state.busy:
            current_url = await browser_control.get_current_url()
    except Exception:
        pass

    return {
        "current_url": current_url or state.last_url or "",
        "browser_status": "busy" if state.busy else "idle",
        "current_operation": state.current_operation,
        "last_analysis": state.last_analysis,
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    browser_running = await browser_control.is_browser_running()

    if not browser_running:
        return {
            "status": "degraded",
            "browser": "stopped",
            "display": "active",
            "message": "Browser is not running. It may need to be restarted.",
        }

    return {
        "status": "ok",
        "browser": "running",
        "display": "active",
    }


@app.post("/search")
async def search_page(request: SearchRequest):
    """Find the search field on the current page, type a query, submit, and analyze results."""
    check_busy()

    generator = page_analyzer.search_page(request.query)

    async def event_stream():
        async for event in run_with_timeout(generator, "search"):
            yield event

    return EventSourceResponse(event_stream())


@app.post("/content")
async def content_page(request: ContentRequest):
    """Analyze the main content area of the current page."""
    check_busy()

    generator = page_analyzer.content_page(full_page=request.full_page)

    async def event_stream():
        async for event in run_with_timeout(generator, "content"):
            yield event

    return EventSourceResponse(event_stream())
