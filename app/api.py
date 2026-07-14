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
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import browser_control
import page_analyzer
from debug_recorder import DebugRecorder, DEBUG_BASE_DIR

logger = logging.getLogger("api")

OPERATION_TIMEOUT = int(os.environ.get("OPERATION_TIMEOUT", "120"))


# --- State ---


class AgentState:
    """Tracks current agent state for concurrency control."""

    def __init__(self):
        self.busy = False
        self.current_operation: Optional[str] = None
        self.current_operation_id: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.last_analysis: Optional[dict] = None
        self.last_url: Optional[str] = None
        self.debug_enabled: bool = False
        self._lock = asyncio.Lock()

    async def acquire(self, operation: str, operation_id: str = "") -> bool:
        """Try to acquire the lock. Returns False if already busy."""
        async with self._lock:
            if self.busy:
                return False
            self.busy = True
            self.current_operation = operation
            self.current_operation_id = operation_id or None
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
            self.current_operation_id = None
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


class DebugToggleRequest(BaseModel):
    enabled: bool


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


def _make_recorder(operation_name: str) -> Optional[DebugRecorder]:
    """Create a DebugRecorder if debug mode is enabled, otherwise return None."""
    if not state.debug_enabled:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    operation_id = f"{operation_name}_{ts}"
    return DebugRecorder(operation_id=operation_id, operation_name=operation_name)


async def run_with_timeout(generator, operation: str, operation_id: str = ""):
    """
    Wrap an async generator with timeout and state management.
    Yields SSE events.
    """
    if not await state.acquire(operation, operation_id=operation_id):
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

                # Inject debug indicator into every event when debug mode is on
                if state.debug_enabled:
                    event = {**event, "debug": "[DEBUG MODE ON]"}

                if event_type == "result":
                    last_url = event.get("current_url", "")
                    last_analysis = event
                    yield {
                        "event": "result",
                        "data": json.dumps(event, ensure_ascii=False),
                    }
                elif event.get("stage") == "debug":
                    yield {
                        "event": "debug",
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
    recorder = _make_recorder("open")
    generator = page_analyzer.open_page(request.url, recorder=recorder)

    async def event_stream():
        async for event in run_with_timeout(
            generator, "open", operation_id=recorder.operation_id if recorder else ""
        ):
            yield event

    return EventSourceResponse(event_stream())


@app.post("/click")
async def click_element(request: ClickRequest):
    """Click on a named element, then analyze the new page."""
    check_busy()
    recorder = _make_recorder("click")
    generator = page_analyzer.click_element(request.target, recorder=recorder)

    async def event_stream():
        async for event in run_with_timeout(
            generator, "click", operation_id=recorder.operation_id if recorder else ""
        ):
            yield event

    return EventSourceResponse(event_stream())


@app.post("/scan")
async def scan_page():
    """Full page scan with scrolling."""
    check_busy()
    recorder = _make_recorder("scan")
    generator = page_analyzer.scan_page(recorder=recorder)

    async def event_stream():
        async for event in run_with_timeout(
            generator, "scan", operation_id=recorder.operation_id if recorder else ""
        ):
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
    recorder = _make_recorder("search")
    generator = page_analyzer.search_page(request.query, recorder=recorder)

    async def event_stream():
        async for event in run_with_timeout(
            generator, "search", operation_id=recorder.operation_id if recorder else ""
        ):
            yield event

    return EventSourceResponse(event_stream())


@app.post("/content")
async def content_page(request: ContentRequest):
    """Analyze the main content area of the current page."""
    check_busy()
    recorder = _make_recorder("content")
    generator = page_analyzer.content_page(
        full_page=request.full_page, recorder=recorder
    )

    async def event_stream():
        async for event in run_with_timeout(
            generator, "content", operation_id=recorder.operation_id if recorder else ""
        ):
            yield event

    return EventSourceResponse(event_stream())


# --- Debug Endpoints ---


@app.post("/debug")
async def debug_toggle(request: DebugToggleRequest):
    """Enable or disable debug mode."""
    state.debug_enabled = request.enabled
    return {
        "debug_enabled": state.debug_enabled,
        "message": f"Debug mode {'enabled' if state.debug_enabled else 'disabled'}",
    }


@app.get("/debug")
async def debug_status():
    """Get current debug mode state."""
    return {
        "debug_enabled": state.debug_enabled,
        "current_operation_id": state.current_operation_id if state.busy else None,
    }


@app.get("/debug/artifacts")
async def debug_artifacts():
    """List all recorded debug operation folders."""
    base = Path(DEBUG_BASE_DIR)
    if not base.exists():
        return {"operations": []}

    operations = []
    dirs = sorted(base.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
    for op_dir in dirs:
        if not op_dir.is_dir():
            continue
        files = sorted(f.name for f in op_dir.iterdir() if f.is_file())
        operations.append(
            {
                "operation_id": op_dir.name,
                "dir": str(op_dir),
                "files": files,
            }
        )

    return {"operations": operations}


@app.get("/debug/artifacts/{operation_id}/{filename}")
async def debug_artifact_file(operation_id: str, filename: str):
    """Download a specific debug artifact file (PNG or log)."""
    file_path = Path(DEBUG_BASE_DIR) / operation_id / filename

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")

    # Prevent path traversal
    try:
        file_path.relative_to(Path(DEBUG_BASE_DIR))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    if filename.endswith(".png"):
        return FileResponse(str(file_path), media_type="image/png")
    return FileResponse(str(file_path), media_type="text/plain")
