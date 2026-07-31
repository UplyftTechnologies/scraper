"""Launch the independent read-only product viewer on port 8502."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Start the product viewer without touching the scraper dashboard."""
    parser = argparse.ArgumentParser(
        description="Launch the read-only product catalogue viewer.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8502,
        help="Viewer port (default: 8502).",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")

    root = Path(__file__).resolve().parent
    app_path = root / "product_viewer_app.py"
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.address",
            "127.0.0.1",
            "--server.port",
            str(args.port),
        ],
        cwd=root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
