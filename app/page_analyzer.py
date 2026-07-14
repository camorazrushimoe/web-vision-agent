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

logger = logging.getLogger("page_analyzer")

MAX_POPUP_ATTEMPTS = int(os.environ.get("MAX_POPUP_ATTEMPTS", "3"))
MAX_SCROLL_SECTIONS = int(os.environ.get("MAX_SCROLL_SECTIONS", "4"))
PAGE_LOAD_TIMEOUT = float(os.environ.get("PAGE_LOAD_TIMEOUT", "12"))


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


# --- Popup Handling ---


async def dismiss_popups(max_attempts: int = None) -> bool:
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

        # Click the close button
        await browser_control.click_at(coords["x"], coords["y"])
        await asyncio.sleep(1.0)

    logger.warning(f"Failed to dismiss popup after {max_attempts} attempts")
    return False


# --- Open Page Flow ---


async def open_page(url: str) -> AsyncGenerator[dict, None]:
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
    stabilized = await browser_control.wait_for_page_load(timeout=PAGE_LOAD_TIMEOUT)

    if not stabilized:
        logger.warning("Page did not fully stabilize, continuing anyway")

    # Step 3: Get current URL
    current_url = await browser_control.get_current_url()
    yield status_event("loaded", "Site loaded, checking for popups...", current_url)

    # Step 4: Dismiss popups
    await dismiss_popups()

    # Step 5: Take screenshot and analyze (structure + input fields in parallel)
    yield status_event("analyzing", "Analyzing page structure with LLM...", current_url)
    screenshot = await browser_control.take_screenshot()
    analysis, fields_result = await asyncio.gather(
        llm_client.analyze_page_structure(screenshot),
        llm_client.detect_input_fields(screenshot),
    )

    if analysis is None:
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze page structure",
            current_url,
            f"Check if LLM server at {llm_client.LLM_URL} is responding",
        )
        return

    # detect_input_fields failure is non-critical — use empty list
    input_fields = (fields_result or {}).get("input_fields", [])

    # Step 6: Return result
    yield {
        "type": "result",
        "current_url": current_url,
        "structure": analysis,
        "summary": analysis.get("summary", ""),
        "input_fields": input_fields,
    }


# --- Click Element Flow ---


async def click_element(target: str) -> AsyncGenerator[dict, None]:
    """
    Find an element by text and click it. Then analyze the new page.
    Yields status events and finally a result event.
    """
    # Step 1: Get current state
    current_url = await browser_control.get_current_url()
    yield status_event(
        "locating", f"Looking for element '{target}' on page...", current_url
    )

    # Step 2: Take screenshot and find element
    screenshot = await browser_control.take_screenshot()
    coords = await llm_client.find_element_coordinates(screenshot, target)

    if not coords or not coords.get("found"):
        reason = coords.get("reason", "Unknown") if coords else "LLM did not respond"
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

    # Step 3: Click
    await browser_control.click_at(x, y)

    # Step 4: Wait for navigation
    yield status_event("navigating", "Page is loading after click...", current_url)
    stabilized = await browser_control.wait_for_page_load(timeout=PAGE_LOAD_TIMEOUT)

    # Step 5: Get new URL
    new_url = await browser_control.get_current_url()
    yield status_event("loaded", "Page loaded, checking for popups...", new_url)

    # Step 6: Dismiss popups
    await dismiss_popups()

    # Step 7: Analyze new page (structure + input fields in parallel)
    yield status_event("analyzing", "Analyzing new page with LLM...", new_url)
    screenshot = await browser_control.take_screenshot()
    analysis, fields_result = await asyncio.gather(
        llm_client.analyze_page_structure(screenshot),
        llm_client.detect_input_fields(screenshot),
    )

    if analysis is None:
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze page after click",
            new_url,
            f"Check if LLM server at {llm_client.LLM_URL} is responding",
        )
        return

    # detect_input_fields failure is non-critical — use empty list
    input_fields = (fields_result or {}).get("input_fields", [])

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


async def scan_page() -> AsyncGenerator[dict, None]:
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
                # We don't add check_screenshot here — it will be captured in next iteration's main screenshot
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
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze full page scan",
            current_url,
            f"Sent {sections_scanned} screenshots. Check LLM server.",
        )
        return

    # Scroll back to top
    await browser_control.scroll_to_top()

    yield {
        "type": "result",
        "current_url": current_url,
        "sections_scanned": sections_scanned,
        "page_ended": page_ended,
        "full_structure": analysis,
        "summary": analysis.get("summary", ""),
    }


# --- Content Page Flow ---


async def content_page(full_page: bool = False) -> AsyncGenerator[dict, None]:
    """
    Analyze the main content area of the current page.
    If full_page=True, scrolls through up to MAX_SCROLL_SECTIONS before analyzing.
    Yields status events and finally a result event.
    """
    current_url = await browser_control.get_current_url()

    yield status_event("screenshot", "Taking screenshot...", current_url)

    screenshots: list[Image.Image] = []

    if full_page:
        # Scroll to top first
        await browser_control.scroll_to_top()
        await asyncio.sleep(0.5)

        page_ended = False
        for section in range(MAX_SCROLL_SECTIONS):
            yield status_event(
                "scanning",
                f"Capturing section {section + 1}/{MAX_SCROLL_SECTIONS}...",
                current_url,
            )

            screenshot = await browser_control.take_screenshot()
            screenshots.append(screenshot)

            if section < MAX_SCROLL_SECTIONS - 1:
                await browser_control.scroll_down()
                await asyncio.sleep(1.0)

                check = await browser_control.take_screenshot()
                diff = browser_control.pixel_difference(screenshot, check)
                if diff < 1.0:
                    page_ended = True
                    logger.info(
                        f"Content scan: page ended after {section + 1} sections"
                    )
                    break

        # Scroll back to top after scanning
        await browser_control.scroll_to_top()
        yield status_event(
            "analyzing",
            f"Analyzing {len(screenshots)} page section(s) with vision model...",
            current_url,
        )
    else:
        screenshot = await browser_control.take_screenshot()
        screenshots.append(screenshot)
        yield status_event(
            "analyzing", "Analyzing page content with vision model...", current_url
        )

    analysis = await llm_client.analyze_page_content(screenshots)

    if analysis is None:
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze page content",
            current_url,
            f"Check if LLM server at {llm_client.LLM_URL} is responding",
        )
        return

    yield {
        "type": "result",
        "current_url": current_url,
        "sections_analyzed": len(screenshots),
        "full_page": full_page,
        "content_type": analysis.get("content_type", "unknown"),
        "content_summary": analysis.get("content_summary", ""),
        "items_count": analysis.get("items_count", 0),
        "items": analysis.get("items", []),
        "text_summary": analysis.get("text_summary", ""),
        "clickable_elements": analysis.get("clickable_elements", []),
    }


# --- Search Page Flow ---


async def search_page(query: str) -> AsyncGenerator[dict, None]:
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

    # Step 2: Detect search field via Gemma 4
    yield status_event("detecting", "Looking for search field on page...", url_before)
    fields_result = await llm_client.detect_input_fields(screenshot_before)
    input_fields = (fields_result or {}).get("input_fields", [])

    search_field = next((f for f in input_fields if f.get("type") == "search"), None)

    if search_field is None:
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
        yield error_event(
            "search_field_not_found",
            "Could not locate search field coordinates on screen",
            url_before,
            coords.get("reason", "UI-TARS could not find the element")
            if coords
            else "No response from grounding model",
        )
        return

    # Step 4: Click the search field
    x, y = coords["x"], coords["y"]
    yield status_event(
        "clicking", f"Clicking search field at ({x}, {y})...", url_before
    )
    await browser_control.click_at(x, y)
    await asyncio.sleep(0.3)

    # Step 5: Clear existing text, then type query
    await browser_control.press_key("ctrl+a")
    await asyncio.sleep(0.1)
    await browser_control.type_text(query)

    # Step 6: Screenshot to verify text was entered
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
        await browser_control.click_at(sx, sy)
    else:
        yield status_event(
            "submitting", "Search button not found, pressing Enter...", url_before
        )
        await browser_control.press_key("Return")

    # Step 8: Wait for page to stabilize BEFORE reading URL
    yield status_event("waiting", "Waiting for search results to load...", url_before)
    await browser_control.wait_for_page_load(timeout=PAGE_LOAD_TIMEOUT)

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
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze search results page",
            url_after,
            f"Check if LLM server at {llm_client.LLM_URL} is responding",
        )
        return

    input_fields_after = (fields_after or {}).get("input_fields", [])

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
