"""
Page Analyzer Module
High-level operations: open page, click element, scan full page.
Combines browser_control and llm_client.
"""

import asyncio
import logging
import os
from typing import AsyncGenerator, Optional

from PIL import Image

import browser_control
import llm_client
from debug_recorder import DebugRecorder

logger = logging.getLogger("page_analyzer")

MAX_POPUP_ATTEMPTS = int(os.environ.get("MAX_POPUP_ATTEMPTS", "3"))
MAX_SCROLL_SECTIONS = int(os.environ.get("MAX_SCROLL_SECTIONS", "4"))
PAGE_LOAD_TIMEOUT = float(os.environ.get("PAGE_LOAD_TIMEOUT", "12"))

CONTENT_MAX_TOP_SCREENS = int(os.environ.get("CONTENT_MAX_TOP_SCREENS", "2"))
CONTENT_MAX_BOTTOM_SCREENS = int(os.environ.get("CONTENT_MAX_BOTTOM_SCREENS", "2"))
CONTENT_MAX_MIDDLE_SCREENS = int(os.environ.get("CONTENT_MAX_MIDDLE_SCREENS", "4"))
CONTENT_LLM_BATCH_SIZE = int(os.environ.get("CONTENT_LLM_BATCH_SIZE", "3"))
CONTENT_GLOBAL_SCREEN_CAP = int(os.environ.get("CONTENT_GLOBAL_SCREEN_CAP", "12"))


# --- Status Event Helpers ---


def status_event(stage: str, message: str, current_url: str = "") -> dict:
    """Create a status event dict for SSE streaming."""
    event = {"stage": stage, "message": message}
    if current_url:
        event["current_url"] = current_url
    return event


def error_event(
    stage: str, message: str, current_url: str = "", details: str = ""
) -> dict:
    """Create an error event dict for SSE streaming."""
    event = {"stage": stage, "message": message}
    if current_url:
        event["current_url"] = current_url
    if details:
        event["details"] = details
    return event


# --- Debug Action Helpers ---
# These thin wrappers perform a browser action and immediately record a debug
# step if a recorder is provided. Using them replaces direct calls to
# browser_control so every action type is consistently logged.


async def _click_and_record(
    recorder: Optional[DebugRecorder],
    x: int,
    y: int,
    step_name: str,
    meta: dict = None,
):
    """Click at (x, y) and record a debug step with annotated screenshot."""
    await browser_control.click_at(x, y)
    if recorder:
        screenshot = await browser_control.take_screenshot()
        await recorder.step(step_name, screenshot, coords=(x, y), meta=meta or {})


async def _type_and_record(
    recorder: Optional[DebugRecorder],
    text: str,
    step_name: str,
    meta: dict = None,
):
    """Type text and record a debug step (no coords → no annotation)."""
    await browser_control.type_text(text)
    if recorder:
        screenshot = await browser_control.take_screenshot()
        await recorder.step(
            step_name, screenshot, coords=None, meta={"text": text, **(meta or {})}
        )


async def _wait_and_record(
    recorder: Optional[DebugRecorder],
    step_name: str,
    meta: dict = None,
    timeout: float = None,
) -> bool:
    """Wait for page load and record a debug step."""
    kw = {}
    if timeout is not None:
        kw["timeout"] = timeout
    result = await browser_control.wait_for_page_load(**kw)
    if recorder:
        screenshot = await browser_control.take_screenshot()
        await recorder.step(step_name, screenshot, coords=None, meta=meta or {})
    return result


# --- Popup Handling ---


async def dismiss_popups(
    max_attempts: int = None,
    recorder: Optional[DebugRecorder] = None,
) -> bool:
    """
    Check for popups/overlays and try to close them.
    Returns True if a popup was found and dismissed.
    """
    if max_attempts is None:
        max_attempts = MAX_POPUP_ATTEMPTS

    for attempt in range(max_attempts):
        screenshot = await browser_control.take_screenshot()
        popup_info = await llm_client.detect_popup(screenshot)

        if not popup_info or not popup_info.get("popup_detected"):
            if attempt > 0:
                logger.info(f"Popup dismissed after {attempt} attempt(s)")
            return attempt > 0  # True if we dismissed at least one

        close_text = popup_info.get("close_button_text", "")
        if not close_text:
            logger.warning("Popup detected but no close button text identified")
            return False

        logger.info(
            f"Popup detected (attempt {attempt + 1}), trying to close with: '{close_text}'"
        )

        # Find close button coordinates with UI-TARS
        coords = await llm_client.find_element_coordinates(screenshot, close_text)

        if not coords or not coords.get("found"):
            logger.warning(f"Could not find close button '{close_text}' on screen")
            return False

        # Click the close button (with debug recording)
        await _click_and_record(
            recorder,
            coords["x"],
            coords["y"],
            "after_click",
            meta={"context": "dismiss_popup", "close_text": close_text},
        )
        await asyncio.sleep(1.0)

    logger.warning(f"Failed to dismiss popup after {max_attempts} attempts")
    return False


# --- Open Page Flow ---


async def open_page(
    url: str,
    recorder: Optional[DebugRecorder] = None,
) -> AsyncGenerator[dict, None]:
    """
    Open a URL in the browser and analyze the page.
    Yields status events and finally a result event.
    """
    current_url = ""

    # Step 1: Navigate
    yield status_event("opening", f"Opening {url}...", current_url)
    await browser_control.navigate_to_url(url)

    # Step 2: Wait for page load
    yield status_event("loading", "Waiting for page to load...", url)
    stabilized = await _wait_and_record(
        recorder, "after_load", timeout=PAGE_LOAD_TIMEOUT
    )

    if not stabilized:
        logger.warning("Page did not fully stabilize, continuing anyway")

    # Step 3: Get current URL
    current_url = await browser_control.get_current_url()
    yield status_event("loaded", "Site loaded, checking for popups...", current_url)

    # Step 4: Dismiss popups
    await dismiss_popups(recorder=recorder)

    # Step 5: Take screenshot; record before_action if debug enabled
    yield status_event("analyzing", "Analyzing page structure with LLM...", current_url)
    screenshot = await browser_control.take_screenshot()

    if recorder:
        await recorder.step(
            "before_action", screenshot, coords=None, meta={"url": current_url}
        )

    # Analyze structure + input fields in parallel
    analysis, fields_result = await asyncio.gather(
        llm_client.analyze_page_structure(screenshot),
        llm_client.detect_input_fields(screenshot),
    )

    if analysis is None:
        if recorder:
            await recorder.step(
                "on_error", None, coords=None, meta={"error": "analysis_failed"}
            )
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze page structure",
            current_url,
            f"Check if LLM server at {llm_client.LLM_URL} is responding",
        )
        return

    # detect_input_fields failure is non-critical — use empty list
    input_fields = (fields_result or {}).get("input_fields", [])

    if recorder:
        recorder.finish({"result": "ok", "url": current_url})

    # Step 6: Return result
    yield {
        "type": "result",
        "current_url": current_url,
        "structure": analysis,
        "summary": analysis.get("summary", ""),
        "input_fields": input_fields,
    }


# --- Click Element Flow ---


async def click_element(
    target: str,
    recorder: Optional[DebugRecorder] = None,
) -> AsyncGenerator[dict, None]:
    """
    Find an element by text and click it. Then analyze the new page.
    Yields status events and finally a result event.
    """
    # Step 1: Get current state
    current_url = await browser_control.get_current_url()
    yield status_event(
        "locating", f"Looking for element '{target}' on page...", current_url
    )

    # Step 2: Take screenshot; record before_action
    screenshot = await browser_control.take_screenshot()
    if recorder:
        await recorder.step(
            "before_action", screenshot, coords=None, meta={"target": target}
        )

    coords = await llm_client.find_element_coordinates(screenshot, target)

    if not coords or not coords.get("found"):
        reason = coords.get("reason", "Unknown") if coords else "LLM did not respond"
        if recorder:
            await recorder.step(
                "on_error",
                None,
                coords=None,
                meta={"error": "element_not_found", "reason": reason},
            )
        yield error_event(
            "element_not_found",
            f"Could not find element '{target}' on current page",
            current_url,
            reason,
        )
        return

    x, y = coords["x"], coords["y"]
    yield status_event(
        "clicking", f"Moving mouse to ({x}, {y}) and clicking...", current_url
    )

    # Step 3: Click (with debug recording)
    await _click_and_record(recorder, x, y, "after_click", meta={"target": target})

    # Step 4: Wait for navigation
    yield status_event("navigating", "Page is loading after click...", current_url)
    await _wait_and_record(recorder, "after_load", timeout=PAGE_LOAD_TIMEOUT)

    # Step 5: Get new URL
    new_url = await browser_control.get_current_url()
    yield status_event("loaded", "Page loaded, checking for popups...", new_url)

    # Step 6: Dismiss popups
    await dismiss_popups(recorder=recorder)

    # Step 7: Analyze new page (structure + input fields in parallel)
    yield status_event("analyzing", "Analyzing new page with LLM...", new_url)
    screenshot = await browser_control.take_screenshot()
    analysis, fields_result = await asyncio.gather(
        llm_client.analyze_page_structure(screenshot),
        llm_client.detect_input_fields(screenshot),
    )

    if analysis is None:
        if recorder:
            await recorder.step(
                "on_error", None, coords=None, meta={"error": "analysis_failed"}
            )
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze page after click",
            new_url,
            f"Check if LLM server at {llm_client.LLM_URL} is responding",
        )
        return

    # detect_input_fields failure is non-critical — use empty list
    input_fields = (fields_result or {}).get("input_fields", [])

    if recorder:
        recorder.finish({"result": "ok", "url": new_url})

    # Step 8: Return result
    yield {
        "type": "result",
        "current_url": new_url,
        "clicked": target,
        "click_coordinates": {"x": x, "y": y},
        "structure": analysis,
        "summary": analysis.get("summary", ""),
        "input_fields": input_fields,
    }


# --- Full Page Scan Flow ---


async def scan_page(
    recorder: Optional[DebugRecorder] = None,
) -> AsyncGenerator[dict, None]:
    """
    Scan the full page by scrolling and taking screenshots.
    Yields status events and finally a result event.
    """
    current_url = await browser_control.get_current_url()

    # Scroll to top first
    await browser_control.scroll_to_top()
    await asyncio.sleep(0.5)

    screenshots: list[Image.Image] = []
    page_ended = False

    for section in range(MAX_SCROLL_SECTIONS):
        yield status_event(
            "scanning",
            f"Taking screenshot {section + 1}/{MAX_SCROLL_SECTIONS}...",
            current_url,
        )

        screenshot = await browser_control.take_screenshot()
        screenshots.append(screenshot)

        # Record section screenshot
        if recorder:
            step_name = "before_action" if section == 0 else f"section_{section + 1}"
            await recorder.step(
                step_name, screenshot, coords=None, meta={"section": section + 1}
            )

        # Check if we're at the bottom (compare with scroll attempt)
        if section < MAX_SCROLL_SECTIONS - 1:
            await browser_control.scroll_down()
            await asyncio.sleep(1.0)

            # Take another screenshot to see if page actually scrolled
            check_screenshot = await browser_control.take_screenshot()
            diff = browser_control.pixel_difference(screenshot, check_screenshot)

            if diff < 1.0:
                # Page didn't scroll — we've reached the bottom
                page_ended = True
                logger.info(f"Page ended after {section + 1} sections")
                break
            else:
                # Page scrolled, the next iteration will capture this new viewport
                pass

    sections_scanned = len(screenshots)
    yield status_event(
        "scan_complete",
        f"Page {'ended' if page_ended else 'truncated'} after {sections_scanned} sections. Analyzing...",
        current_url,
    )

    # Analyze all screenshots together
    analysis = await llm_client.analyze_full_page(screenshots)

    if analysis is None:
        if recorder:
            await recorder.step(
                "on_error", None, coords=None, meta={"error": "analysis_failed"}
            )
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze full page scan",
            current_url,
            f"Sent {sections_scanned} screenshots. Check LLM server.",
        )
        return

    # Scroll back to top
    await browser_control.scroll_to_top()

    if recorder:
        final_screenshot = await browser_control.take_screenshot()
        await recorder.step(
            "after_load",
            final_screenshot,
            coords=None,
            meta={"sections": sections_scanned},
        )
        recorder.finish({"result": "ok", "sections": sections_scanned})

    yield {
        "type": "result",
        "current_url": current_url,
        "sections_scanned": sections_scanned,
        "page_ended": page_ended,
        "full_structure": analysis,
        "summary": analysis.get("summary", ""),
    }


# --- Content Page Flow ---


async def _capture_top_screens(
    max_screens: int,
    recorder: Optional[DebugRecorder],
    current_url: str,
) -> list[Image.Image]:
    """Capture top region screenshots scrolling downward from top of page."""
    await browser_control.scroll_to_top()

    screenshots: list[Image.Image] = []
    prev: Optional[Image.Image] = None

    for i in range(max_screens):
        screenshot = await browser_control.take_screenshot()
        screenshots.append(screenshot)

        if recorder:
            step = "before_action" if i == 0 else f"section_top_{i + 1}"
            await recorder.step(
                step, screenshot, coords=None, meta={"region": "top", "index": i + 1}
            )

        if prev is not None:
            diff = browser_control.pixel_difference(prev, screenshot)
            if diff < 1.0:
                logger.info(f"Top capture: page ended after {i + 1} screens")
                break

        prev = screenshot

        if i < max_screens - 1:
            await browser_control.scroll_down()

    return screenshots


async def _capture_bottom_screens(
    max_screens: int,
    recorder: Optional[DebugRecorder],
    current_url: str,
) -> tuple[list[Image.Image], int]:
    """
    Capture bottom region screenshots scrolling upward from bottom of page.
    Returns (screenshots, bottom_depth) where bottom_depth = number of Page_Up presses.
    bottom_depth is used to estimate page length.
    """
    await browser_control.scroll_to_bottom()

    screenshots: list[Image.Image] = []
    bottom_depth = 0
    prev: Optional[Image.Image] = None

    for i in range(max_screens):
        screenshot = await browser_control.take_screenshot()
        screenshots.append(screenshot)

        if recorder:
            await recorder.step(
                f"section_bottom_{i + 1}",
                screenshot,
                coords=None,
                meta={"region": "bottom", "index": i + 1},
            )

        if prev is not None:
            diff = browser_control.pixel_difference(prev, screenshot)
            if diff < 1.0:
                logger.info(f"Bottom capture: page ended after {i + 1} screens up")
                break

        prev = screenshot

        if i < max_screens - 1:
            await browser_control.scroll_up()
            bottom_depth += 1

    return screenshots, bottom_depth


async def _capture_middle_screens(
    top_count: int,
    page_length_est: int,
    covered: int,
    max_screens: int,
    global_cap: int,
    total_so_far: int,
    recorder: Optional[DebugRecorder],
) -> list[Image.Image]:
    """
    Capture middle region by positioning between top and bottom zones.
    Starts at viewport top_count (right after last top screenshot).
    """
    # Navigate to start of middle zone
    await browser_control.scroll_to_top()
    for _ in range(top_count):
        await browser_control.scroll_down()

    screenshots: list[Image.Image] = []
    middle_index = 0

    while True:
        if len(screenshots) >= max_screens:
            logger.info("Middle capture: max_middle_screens reached")
            break
        if total_so_far + len(screenshots) >= global_cap:
            logger.info("Middle capture: global screen cap reached")
            break
        if covered + len(screenshots) >= page_length_est:
            logger.info("Middle capture: overlap achieved, page fully covered")
            break

        screenshot = await browser_control.take_screenshot()
        screenshots.append(screenshot)
        middle_index += 1

        if recorder:
            await recorder.step(
                f"section_middle_{middle_index}",
                screenshot,
                coords=None,
                meta={"region": "middle", "index": middle_index},
            )

        await browser_control.scroll_down()

    return screenshots


async def content_page(
    max_top_screens: int = CONTENT_MAX_TOP_SCREENS,
    max_bottom_screens: int = CONTENT_MAX_BOTTOM_SCREENS,
    max_middle_screens: int = CONTENT_MAX_MIDDLE_SCREENS,
    llm_batch_size: int = CONTENT_LLM_BATCH_SIZE,
    overlap_stop_enabled: bool = True,
    recorder: Optional[DebugRecorder] = None,
) -> AsyncGenerator[dict, None]:
    """
    Analyze the full content of the current page using bidirectional screenshot capture.
    Captures top, bottom, and optionally middle regions.
    Sends screenshots in batches to LLM and merges results.
    Yields status events and finally a result event.
    """
    current_url = await browser_control.get_current_url()

    # --- Phase 1: Capture top region ---
    yield status_event("capturing_top", "Capturing top region (1)...", current_url)
    top_screenshots = await _capture_top_screens(max_top_screens, recorder, current_url)
    top_count = len(top_screenshots)
    logger.info(f"Content: captured {top_count} top screenshots")

    # --- Phase 2: Capture bottom region ---
    yield status_event(
        "capturing_bottom", "Capturing bottom region (1)...", current_url
    )
    bottom_screenshots, bottom_depth = await _capture_bottom_screens(
        max_bottom_screens, recorder, current_url
    )
    bottom_count = len(bottom_screenshots)
    logger.info(
        f"Content: captured {bottom_count} bottom screenshots, bottom_depth={bottom_depth}"
    )

    # --- Phase 3: Decide if middle is needed ---
    page_length_est = top_count + bottom_depth
    covered = top_count + bottom_count
    middle_screenshots: list[Image.Image] = []

    if overlap_stop_enabled and covered < page_length_est:
        total_so_far = top_count + bottom_count
        remaining = page_length_est - covered
        logger.info(
            f"Content: page_length_est={page_length_est}, covered={covered}, "
            f"need {remaining} middle viewport(s)"
        )
        yield status_event(
            "capturing_middle",
            f"Capturing middle region (up to {min(remaining, max_middle_screens)} screens)...",
            current_url,
        )
        middle_screenshots = await _capture_middle_screens(
            top_count=top_count,
            page_length_est=page_length_est,
            covered=covered,
            max_screens=max_middle_screens,
            global_cap=CONTENT_GLOBAL_SCREEN_CAP,
            total_so_far=total_so_far,
            recorder=recorder,
        )
        logger.info(f"Content: captured {len(middle_screenshots)} middle screenshots")
    else:
        logger.info(
            "Content: skipping middle capture (page covered or overlap_stop disabled)"
        )

    # Scroll back to top
    await browser_control.scroll_to_top()

    # --- Phase 4: Build labeled screenshot list for batching ---
    # Order: top (1..N) → bottom (1..M, reversed so [0] = deepest bottom) → middle
    labeled: list[tuple[Image.Image, dict]] = []
    for i, img in enumerate(top_screenshots):
        labeled.append((img, {"region": "top", "index": i + 1}))
    for i, img in enumerate(reversed(bottom_screenshots)):
        labeled.append((img, {"region": "bottom", "index": i + 1}))
    for i, img in enumerate(middle_screenshots):
        labeled.append((img, {"region": "middle", "index": i + 1}))

    total_screenshots = len(labeled)

    # --- Phase 5: Send to LLM in batches ---
    batches = [
        labeled[i : i + llm_batch_size]
        for i in range(0, total_screenshots, llm_batch_size)
    ]
    partial_results: list[dict] = []

    for batch_idx, batch in enumerate(batches):
        yield status_event(
            "analyzing",
            f"Sending batch {batch_idx + 1}/{len(batches)} to LLM...",
            current_url,
        )
        imgs = [item[0] for item in batch]
        lbls = [item[1] for item in batch]
        result = await llm_client.analyze_page_content_batch(imgs, lbls)
        if result is not None:
            partial_results.append(result)
        else:
            logger.warning(
                f"Content: batch {batch_idx + 1} returned None (timeout?), skipping"
            )

    if not partial_results:
        if recorder:
            await recorder.step(
                "on_error", None, coords=None, meta={"error": "all_batches_failed"}
            )
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze page content — all batches timed out",
            current_url,
            f"Check if LLM server at {llm_client.LLM_URL} is responding",
        )
        return

    # --- Phase 6: Merge or return directly ---
    if len(partial_results) == 1:
        # Single batch — use batch result directly, reshape to final format
        r = partial_results[0]
        analysis = {
            "page_type": r.get("page_type_hint", "unknown"),
            "content_summary": "",
            "top_context": r.get(
                "top", {"navigation_items": [], "hero_or_title": "", "ctas": []}
            ),
            "middle_context": {
                "themes": r.get("middle", {}).get("themes", []),
                "key_sections": r.get("middle", {}).get("key_sections", []),
                "items_count_estimate": 0,
            },
            "bottom_context": r.get(
                "bottom", {"footer_links": [], "copyright_or_org": ""}
            ),
        }
    else:
        yield status_event(
            "merging",
            f"Merging {len(partial_results)} batch results...",
            current_url,
        )
        analysis = await llm_client.merge_content_analyses(partial_results)

        if analysis is None:
            if recorder:
                await recorder.step(
                    "on_error", None, coords=None, meta={"error": "merge_failed"}
                )
            yield error_event(
                "merge_failed",
                "LLM failed to merge batch results",
                current_url,
                f"Check if LLM server at {llm_client.LLM_URL} is responding",
            )
            return

    if recorder:
        final_screenshot = await browser_control.take_screenshot()
        await recorder.step(
            "after_load",
            final_screenshot,
            coords=None,
            meta={"screenshots_taken": total_screenshots, "batches_sent": len(batches)},
        )
        recorder.finish({"result": "ok", "screenshots_taken": total_screenshots})

    yield {
        "type": "result",
        "current_url": current_url,
        "page_type": analysis.get("page_type", "unknown"),
        "content_summary": analysis.get("content_summary", ""),
        "top_context": analysis.get(
            "top_context", {"navigation_items": [], "hero_or_title": "", "ctas": []}
        ),
        "middle_context": analysis.get(
            "middle_context",
            {"themes": [], "key_sections": [], "items_count_estimate": 0},
        ),
        "bottom_context": analysis.get(
            "bottom_context", {"footer_links": [], "copyright_or_org": ""}
        ),
        "screenshots_taken": total_screenshots,
        "batches_sent": len(batches),
    }


# --- Search Page Flow ---


async def search_page(
    query: str,
    recorder: Optional[DebugRecorder] = None,
) -> AsyncGenerator[dict, None]:
    """
    Find a search field on the current page, type a query, submit it,
    and analyze the result.
    Yields status events and finally a result event.
    """
    # Step 0: Save url_before BEFORE any actions
    url_before = await browser_control.get_current_url()
    yield status_event("starting", f"Preparing to search for '{query}'...", url_before)

    # Step 1: Screenshot before search (for pixel diff comparison later)
    screenshot_before = await browser_control.take_screenshot()

    if recorder:
        await recorder.step(
            "before_action", screenshot_before, coords=None, meta={"query": query}
        )

    # Step 2: Detect search field via Gemma 4
    yield status_event("detecting", "Looking for search field on page...", url_before)
    fields_result = await llm_client.detect_input_fields(screenshot_before)
    input_fields = (fields_result or {}).get("input_fields", [])

    search_field = next((f for f in input_fields if f.get("type") == "search"), None)

    if search_field is None:
        if recorder:
            await recorder.step(
                "on_error", None, coords=None, meta={"error": "search_field_not_found"}
            )
        yield error_event(
            "search_field_not_found",
            "No search field detected on this page",
            url_before,
            "Gemma 4 found no search-type input fields in the current screenshot",
        )
        return

    # Step 3: Find pixel coordinates via UI-TARS using the rich description
    field_description = search_field.get("description") or search_field.get(
        "label", "search input field"
    )
    yield status_event(
        "locating",
        f"Locating search field: '{search_field.get('label', '')}'...",
        url_before,
    )

    coords = await llm_client.find_element_coordinates(
        screenshot_before, field_description
    )

    if not coords or not coords.get("found"):
        if recorder:
            await recorder.step(
                "on_error",
                None,
                coords=None,
                meta={"error": "search_field_coords_not_found"},
            )
        yield error_event(
            "search_field_not_found",
            "Could not locate search field coordinates on screen",
            url_before,
            coords.get("reason", "UI-TARS could not find the element")
            if coords
            else "No response from grounding model",
        )
        return

    # Step 4: Click the search field (with debug recording)
    x, y = coords["x"], coords["y"]
    yield status_event(
        "clicking", f"Clicking search field at ({x}, {y})...", url_before
    )
    await _click_and_record(
        recorder, x, y, "after_click", meta={"context": "search_field"}
    )
    await asyncio.sleep(0.3)

    # Step 5: Clear existing text, then type query
    await browser_control.press_key("ctrl+a")
    await asyncio.sleep(0.1)
    await _type_and_record(recorder, query, "after_typing", meta={"query": query})

    # Step 6: Screenshot to verify text was entered (already taken inside _type_and_record)
    screenshot_typed = await browser_control.take_screenshot()
    yield status_event(
        "typing",
        f"Typed query '{query}', verifying input...",
        url_before,
    )

    # Step 7: Find submit button using the same screenshot with text typed
    submit_coords = await llm_client.find_element_coordinates(
        screenshot_typed, "submit button or search icon near the search field"
    )

    if submit_coords and submit_coords.get("found"):
        sx, sy = submit_coords["x"], submit_coords["y"]
        yield status_event(
            "submitting", f"Clicking search button at ({sx}, {sy})...", url_before
        )
        await _click_and_record(
            recorder, sx, sy, "after_click", meta={"context": "submit_button"}
        )
    else:
        yield status_event(
            "submitting", "Search button not found, pressing Enter...", url_before
        )
        await browser_control.press_key("Return")

    # Step 8: Wait for page to stabilize BEFORE reading URL
    yield status_event("waiting", "Waiting for search results to load...", url_before)
    await _wait_and_record(recorder, "after_load", timeout=PAGE_LOAD_TIMEOUT)

    # Step 9: Read URL only after page has loaded
    url_after = await browser_control.get_current_url()

    # Step 10: Final screenshot for analysis and pixel diff
    screenshot_after = await browser_control.take_screenshot()

    # Step 11: Determine result_type
    url_changed = url_after != url_before
    pixel_diff = browser_control.pixel_difference(screenshot_before, screenshot_after)

    if url_changed:
        result_type = "page_reload"
    elif pixel_diff > 10.0:
        result_type = "content_updated"
    else:
        result_type = "no_change"

    yield status_event(
        "analyzing",
        f"Search done ({result_type}), analyzing results...",
        url_after,
    )

    # Step 12: Analyze result page (structure + input fields in parallel)
    analysis, fields_after = await asyncio.gather(
        llm_client.analyze_page_structure(screenshot_after),
        llm_client.detect_input_fields(screenshot_after),
    )

    if analysis is None:
        if recorder:
            await recorder.step(
                "on_error", None, coords=None, meta={"error": "analysis_failed"}
            )
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze search results page",
            url_after,
            f"Check if LLM server at {llm_client.LLM_URL} is responding",
        )
        return

    input_fields_after = (fields_after or {}).get("input_fields", [])

    if recorder:
        recorder.finish(
            {
                "result_type": result_type,
                "url_changed": url_changed,
                "pixel_diff_pct": round(pixel_diff, 1),
            }
        )

    yield {
        "type": "result",
        "current_url": url_after,
        "search_result": {
            "query": query,
            "url_before": url_before,
            "url_after": url_after,
            "url_changed": url_changed,
            "result_type": result_type,
            "pixel_diff_pct": round(pixel_diff, 1),
            "summary": analysis.get("summary", ""),
        },
        "structure": analysis,
        "input_fields": input_fields_after,
    }
