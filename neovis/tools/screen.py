"""Screen capture — lets the agent (and the user on their phone) see the desktop.

SAFE: read-only. Returns the path to a PNG; the channel layer uploads the image.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from ..core.registry import tool


@tool(
    risk="safe",
    description="Take a screenshot of the primary monitor and save it as a PNG. "
    "Returns the file path and resolution. The user will receive the image.",
)
def capture_screen(save_dir: str = "") -> str:
    try:
        import mss
        import mss.tools
    except ImportError:
        return "ERROR: screen capture unavailable (mss not installed)."

    out_dir = Path(save_dir).expanduser() if save_dir else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"neovis_screen_{int(time.time())}.png"

    with mss.mss() as sct:
        monitor = sct.monitors[1]  # [0] is the virtual all-monitors box; [1] is primary
        shot = sct.grab(monitor)
        mss.tools.to_png(shot.rgb, shot.size, output=str(out_path))

    return f"Screenshot saved to {out_path} ({shot.width}x{shot.height})."
