"""
Debug Recorder Module
Manages per-operation debug artifacts: screenshots, annotated copies, debug.log.
"""

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("debug_recorder")

DEBUG_BASE_DIR = os.environ.get("DEBUG_BASE_DIR", "/tmp/web-vision-agent-debug")
MAX_DEBUG_OPERATIONS = 10


class DebugRecorder:
    """
    Records debug artifacts for a single agent operation.

    Creates a folder under DEBUG_BASE_DIR/{operation_id}/ and writes:
    - debug.log       — timestamped text log of every step
    - step_NN_<name>.png           — original screenshot (never modified)
    - step_NN_<name>_annotated.png — copy with click marker drawn on it

    Usage:
        recorder = DebugRecorder(operation_id="search_20240715_143022",
                                 operation_name="search")
        await recorder.step("before_action", screenshot, coords=None, meta={})
        await recorder.step("after_click", screenshot, coords=(450, 80), meta={})
        recorder.finish({"result_type": "page_reload"})
    """

    def __init__(
        self,
        operation_id: str,
        operation_name: str,
        base_dir: str = DEBUG_BASE_DIR,
    ):
        self.operation_id = operation_id
        self.operation_name = operation_name
        self.base_dir = Path(base_dir)
        self.op_dir = self.base_dir / operation_id
        self._step_counter = 0
        self._log_path = self.op_dir / "debug.log"

        self._init_dir()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_dir(self):
        """Create operation dir, rotate old dirs if needed."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._rotate()
        self.op_dir.mkdir(parents=True, exist_ok=True)
        self._log(f"operation={self.operation_name} id={self.operation_id}")

    def _rotate(self):
        """Delete oldest operation dirs when we have MAX_DEBUG_OPERATIONS or more."""
        dirs = sorted(
            [d for d in self.base_dir.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
        )
        while len(dirs) >= MAX_DEBUG_OPERATIONS:
            oldest = dirs.pop(0)
            try:
                shutil.rmtree(oldest)
                logger.debug(f"Debug rotation: removed {oldest.name}")
            except Exception as e:
                logger.warning(f"Failed to remove old debug dir {oldest}: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def step(
        self,
        name: str,
        screenshot: Optional[Image.Image],
        coords: Optional[tuple[int, int]],
        meta: dict,
    ) -> dict:
        """
        Record one debug step.

        - Saves original screenshot as step_NN_<name>.png  (never touched)
        - If coords given, saves annotated copy as step_NN_<name>_annotated.png
        - Appends a line to debug.log
        - Returns dict ready to be yielded as SSE event: debug
        """
        self._step_counter += 1
        step_num = f"{self._step_counter:02d}"
        base_name = f"step_{step_num}_{name}"

        original_file: Optional[str] = None
        annotated_file: Optional[str] = None

        if screenshot is not None:
            # Save original — never modify
            original_path = self.op_dir / f"{base_name}.png"
            screenshot.save(original_path, format="PNG")
            original_file = original_path.name
            logger.debug(f"[debug] saved {original_file}")

            # Save annotated copy if coords provided
            if coords is not None:
                annotated_path = self.op_dir / f"{base_name}_annotated.png"
                annotated = self._annotate(screenshot, coords, base_name)
                annotated.save(annotated_path, format="PNG")
                annotated_file = annotated_path.name
                logger.debug(f"[debug] saved {annotated_file}")

        # Log entry
        parts = [f"step={step_num}", f"name={name}"]
        if original_file:
            parts.append("screenshot saved")
        if annotated_file:
            parts.append("annotated saved")
        if coords:
            parts.append(f"coords=({coords[0]}, {coords[1]})")
        for k, v in meta.items():
            parts.append(f"{k}={v!r}")
        self._log(" · ".join(parts))

        # SSE event dict
        event: dict = {
            "stage": "debug",
            "step": name,
            "operation_id": self.operation_id,
            "debug": "[DEBUG MODE ON]",
        }
        if original_file:
            event["file"] = annotated_file or original_file
        if coords:
            event["coords"] = {"x": coords[0], "y": coords[1]}
        event.update(
            {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))}
        )

        return event

    def finish(self, result: dict):
        """Write final result entry to debug.log."""
        parts = []
        for k, v in result.items():
            parts.append(f"{k}={v!r}")
        self._log("finish · " + " · ".join(parts))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, message: str):
        """Append a timestamped line to debug.log."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"[{ts}] {message}\n"
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.warning(f"Failed to write debug.log: {e}")

    def _annotate(
        self,
        screenshot: Image.Image,
        coords: tuple[int, int],
        label: str,
    ) -> Image.Image:
        """
        Return a NEW image (copy of screenshot) with:
        - Red circle ~30px diameter at click point
        - Red crosshair in the center
        - Label text in the top-left corner
        """
        annotated = screenshot.copy()
        draw = ImageDraw.Draw(annotated)

        x, y = coords
        r = 15  # radius → diameter 30px

        # Circle
        draw.ellipse([(x - r, y - r), (x + r, y + r)], outline=(255, 0, 0), width=2)

        # Crosshair
        draw.line([(x - r, y), (x + r, y)], fill=(255, 0, 0), width=2)
        draw.line([(x, y - r), (x, y + r)], fill=(255, 0, 0), width=2)

        # Label — top-left corner
        text = label.replace("_", " ") + f"  ({x}, {y})"
        # Draw text with a dark background for readability
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

        text_x, text_y = 8, 8
        # Shadow / background
        draw.text((text_x + 1, text_y + 1), text, fill=(0, 0, 0), font=font)
        draw.text((text_x, text_y), text, fill=(255, 50, 50), font=font)

        return annotated
