"""
Web Vision Agent — Entrypoint
Starts Xvfb, Chromium, optionally VNC, then launches FastAPI server.
"""

import os
import signal
import subprocess
import sys
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("entrypoint")

# Configuration from environment
DISPLAY = os.environ.get("DISPLAY", ":99")
DISPLAY_RESOLUTION = os.environ.get("DISPLAY_RESOLUTION", "1920x1080x24")
API_PORT = int(os.environ.get("API_PORT", "8080"))
VNC_ENABLED = os.environ.get("VNC_ENABLED", "false").lower() == "true"

processes: list[subprocess.Popen] = []


def cleanup(signum=None, frame=None):
    """Kill all child processes on exit."""
    logger.info("Shutting down...")
    for proc in reversed(processes):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    sys.exit(0)


signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT, cleanup)


def start_xvfb():
    """Start Xvfb virtual display."""
    display_num = DISPLAY.replace(":", "")
    cmd = [
        "Xvfb",
        DISPLAY,
        "-screen",
        "0",
        DISPLAY_RESOLUTION,
        "-ac",
        "+extension",
        "GLX",
        "+render",
        "-noreset",
    ]
    logger.info(
        f"Starting Xvfb on display {DISPLAY} with resolution {DISPLAY_RESOLUTION}"
    )
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    processes.append(proc)
    time.sleep(1)

    if proc.poll() is not None:
        logger.error("Xvfb failed to start!")
        sys.exit(1)

    logger.info("Xvfb started successfully")
    return proc


def start_chromium():
    """Start Chromium browser maximized."""
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
    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    logger.info("Starting Chromium browser")
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    processes.append(proc)
    time.sleep(3)

    if proc.poll() is not None:
        logger.error("Chromium failed to start!")
        sys.exit(1)

    logger.info("Chromium started successfully")
    return proc


def start_vnc():
    """Start x11vnc server for remote viewing."""
    cmd = [
        "x11vnc",
        "-display",
        DISPLAY,
        "-forever",
        "-nopw",
        "-shared",
        "-rfbport",
        "5900",
    ]
    logger.info("Starting VNC server on port 5900")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    processes.append(proc)
    time.sleep(1)

    if proc.poll() is not None:
        logger.warning("VNC server failed to start (non-critical)")
    else:
        logger.info("VNC server started successfully")

    return proc


def start_api_server():
    """Start FastAPI server with uvicorn."""
    cmd = [
        "python3",
        "-m",
        "uvicorn",
        "api:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(API_PORT),
        "--log-level",
        "info",
    ]
    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    logger.info(f"Starting API server on port {API_PORT}")
    proc = subprocess.Popen(cmd, env=env)
    processes.append(proc)
    return proc


def main():
    logger.info("=" * 50)
    logger.info("Web Vision Agent starting...")
    logger.info("=" * 50)

    # 1. Start virtual display
    start_xvfb()

    # 2. Start browser
    start_chromium()

    # 3. Optionally start VNC
    if VNC_ENABLED:
        start_vnc()
    else:
        logger.info("VNC disabled (set VNC_ENABLED=true to enable)")

    # 4. Start API server (blocking)
    api_proc = start_api_server()

    logger.info("=" * 50)
    logger.info("Web Vision Agent is ready!")
    logger.info(f"API: http://0.0.0.0:{API_PORT}")
    if VNC_ENABLED:
        logger.info("VNC: port 5900")
    logger.info("=" * 50)

    # Wait for API server to exit
    try:
        api_proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
