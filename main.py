"""Open the dashboard with ``python main.py``, or run the CLI with arguments.

    python main.py                      # Streamlit dashboard on port 8501
    python main.py --site nykaa ...     # command-line collection
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_dashboard() -> int:
    """Start Streamlit using the current Python environment."""
    app_path = Path(__file__).resolve().with_name("streamlit_app.py")
    return subprocess.call(
        [sys.executable, "-m", "streamlit", "run", str(app_path)],
        cwd=app_path.parent,
    )


if __name__ == "__main__":
    if len(sys.argv) == 1:
        raise SystemExit(run_dashboard())

    from pricing_scraper.cli import main as cli_main

    raise SystemExit(cli_main())
