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

    # Step 5: Take screenshot and analyze
    yield status_event("analyzing", "Analyzing page structure with LLM...", current_url)
    screenshot = await browser_control.take_screenshot()
    analysis = await llm_client.analyze_page_structure(screenshot)

    if analysis is None:
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze page structure",
            current_url,
            f"Check if LLM server at {llm_client.LLM_URL} is responding",
        )
        return

    # Step 6: Return result
    yield {
        "type": "result",
        "current_url": current_url,
        "structure": analysis,
        "summary": analysis.get("summary", ""),
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
            "locating",
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

    # Step 7: Analyze new page
    yield status_event("analyzing", "Analyzing new page with LLM...", new_url)
    screenshot = await browser_control.take_screenshot()
    analysis = await llm_client.analyze_page_structure(screenshot)

    if analysis is None:
        yield error_event(
            "analysis_failed",
            "LLM failed to analyze page after click",
            new_url,
            f"Check if LLM server at {llm_client.LLM_URL} is responding",
        )
        return

    # Step 8: Return result
    yield {
        "type": "result",
        "current_url": new_url,
        "clicked": target,
        "click_coordinates": {"x": x, "y": y},
        "structure": analysis,
        "summary": analysis.get("summary", ""),
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
