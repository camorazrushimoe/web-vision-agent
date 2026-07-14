"""
Browser Control Module
Manages browser interaction via xdotool (mouse, keyboard, screenshots).
No programmatic browser APIs — only X11 input emulation.
"""

import asyncio
import io
import logging
import os
import subprocess
import time
from typing import Optional

import numpy as np
from PIL import Image, ImageChops

logger = logging.getLogger("browser_control")

DISPLAY = os.environ.get("DISPLAY", ":99")


def _run_cmd(cmd: list[str], timeout: float = 5.0) -> str:
    """Run a shell command and return stdout."""
    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning(f"Command timed out: {' '.join(cmd)}")
        return ""
    except Exception as e:
        logger.error(f"Command failed: {' '.join(cmd)} — {e}")
        return ""


async def _async_run_cmd(cmd: list[str], timeout: float = 5.0) -> str:
    """Async wrapper for running shell commands."""
    return await asyncio.to_thread(_run_cmd, cmd, timeout)


# --- Mouse Control ---


async def move_mouse(x: int, y: int, smooth: bool = True):
    """Move mouse to coordinates. If smooth=True, use delay for human-like movement."""
    if smooth:
        await _async_run_cmd(["xdotool", "mousemove", "--delay", "50", str(x), str(y)])
    else:
        await _async_run_cmd(["xdotool", "mousemove", str(x), str(y)])
    logger.info(f"Mouse moved to ({x}, {y})")


async def click(button: int = 1):
    """Click mouse button. 1=left, 2=middle, 3=right."""
    await _async_run_cmd(["xdotool", "click", str(button)])
    logger.info(f"Clicked button {button}")


async def click_at(x: int, y: int, smooth: bool = True):
    """Move to coordinates and click."""
    await move_mouse(x, y, smooth=smooth)
    await asyncio.sleep(0.1)
    await click(1)


# --- Keyboard Control ---


async def type_text(text: str, delay_ms: int = 30):
    """Type text character by character with delay."""
    await _async_run_cmd(
        ["xdotool", "type", "--delay", str(delay_ms), text], timeout=30.0
    )
    logger.info(f"Typed: {text[:50]}...")


async def press_key(key: str):
    """Press a key or key combination (e.g., 'ctrl+l', 'Return', 'Escape')."""
    await _async_run_cmd(["xdotool", "key", key])
    logger.info(f"Pressed key: {key}")


# --- URL Management ---


async def get_current_url() -> str:
    """
    Get current URL from browser address bar.
    Uses Ctrl+L, Ctrl+A, Ctrl+C, then reads clipboard.
    """
    # Focus address bar
    await press_key("ctrl+l")
    await asyncio.sleep(0.3)

    # Select all
    await press_key("ctrl+a")
    await asyncio.sleep(0.1)

    # Copy to clipboard
    await press_key("ctrl+c")
    await asyncio.sleep(0.2)

    # Read clipboard
    url = await _async_run_cmd(["xclip", "-selection", "clipboard", "-o"])

    # Escape to unfocus address bar
    await press_key("Escape")
    await asyncio.sleep(0.2)

    logger.info(f"Current URL: {url}")
    return url


async def navigate_to_url(url: str):
    """Navigate browser to URL using keyboard."""
    logger.info(f"Navigating to: {url}")

    # Focus address bar
    await press_key("ctrl+l")
    await asyncio.sleep(0.3)

    # Select all (clear existing URL)
    await press_key("ctrl+a")
    await asyncio.sleep(0.1)

    # Type URL
    await type_text(url, delay_ms=10)
    await asyncio.sleep(0.1)

    # Press Enter
    await press_key("Return")


# --- Screenshot ---


async def take_screenshot() -> Image.Image:
    """Take a screenshot of the virtual display and return as PIL Image."""
    screenshot_path = "/tmp/screenshot.png"

    await _async_run_cmd(["scrot", "-o", screenshot_path], timeout=10.0)

    if not os.path.exists(screenshot_path):
        logger.error("Screenshot failed — file not created")
        # Return a blank image as fallback
        return Image.new("RGB", (1920, 1080), color=(0, 0, 0))

    img = Image.open(screenshot_path)
    img.load()  # Force load into memory
    return img


async def take_screenshot_bytes() -> bytes:
    """Take a screenshot and return as PNG bytes."""
    img = await take_screenshot()
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


# --- Page Load Detection ---


def pixel_difference(img1: Image.Image, img2: Image.Image) -> float:
    """
    Calculate percentage of pixels that differ between two images.
    Returns value 0.0 to 100.0.
    """
    if img1.size != img2.size:
        return 100.0

    arr1 = np.array(img1)
    arr2 = np.array(img2)

    # Calculate per-pixel difference (threshold: >10 units difference counts as changed)
    diff = np.abs(arr1.astype(int) - arr2.astype(int))
    changed_pixels = np.any(diff > 10, axis=2)  # Any channel differs by >10
    pct = (changed_pixels.sum() / changed_pixels.size) * 100.0

    return pct


async def wait_for_page_load(
    timeout: float = 12.0, stability_threshold: float = 1.0
) -> bool:
    """
    Wait for page to finish loading by comparing screenshots.
    Returns True if page stabilized, False if timeout reached.

    Args:
        timeout: Maximum seconds to wait
        stability_threshold: Percentage of pixel difference below which page is considered stable
    """
    logger.info("Waiting for page to load (smart detection)...")

    # Initial delay to let browser start loading
    await asyncio.sleep(2.0)

    screenshot1 = await take_screenshot()
    elapsed = 2.0

    while elapsed < timeout:
        await asyncio.sleep(1.0)
        elapsed += 1.0

        screenshot2 = await take_screenshot()
        diff = pixel_difference(screenshot1, screenshot2)

        logger.debug(
            f"Page load check: {diff:.2f}% pixels changed (elapsed: {elapsed:.0f}s)"
        )

        if diff < stability_threshold:
            logger.info(f"Page stabilized after {elapsed:.0f}s (diff: {diff:.2f}%)")
            return True

        screenshot1 = screenshot2

    logger.warning(f"Page did not stabilize within {timeout}s timeout")
    return False


# --- Scroll ---


async def scroll_down():
    """Scroll down one viewport (Page_Down)."""
    await press_key("Page_Down")
    await asyncio.sleep(1.0)
    logger.info("Scrolled down one page")


async def scroll_to_top():
    """Scroll to top of page (Home key)."""
    await press_key("ctrl+Home")
    await asyncio.sleep(0.5)
    logger.info("Scrolled to top")


# --- Browser Health ---


async def is_browser_running() -> bool:
    """Check if Chromium process is alive."""
    result = await _async_run_cmd(["pgrep", "-f", "chromium"])
    return len(result) > 0


async def restart_browser():
    """Kill and restart Chromium."""
    logger.warning("Restarting Chromium...")
    await _async_run_cmd(["pkill", "-f", "chromium"])
    await asyncio.sleep(2)

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    cmd = [
        "chromium",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--disable-extensions",
        "--disable-sync",
        "--disable-translate",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--window-size=1920,1080",
        "--window-position=0,0",
        "--start-maximized",
        "--user-data-dir=/tmp/chromium-profile",
        "about:blank",
    ]
    subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    await asyncio.sleep(3)
    logger.info("Chromium restarted")
