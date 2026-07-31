"""Launch the Streamlit dashboard with ``python dashboard.py``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Start Streamlit using the current Python environment."""
    app_path = Path(__file__).resolve().with_name("streamlit_app.py")
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
        ],
        cwd=app_path.parent,
    )


if __name__ == "__main__":
    raise SystemExit(main())
