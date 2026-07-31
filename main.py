"""Launch the dashboard by default, or run the CLI when arguments are passed."""

import sys


if __name__ == "__main__":
    if len(sys.argv) == 1:
        from dashboard import main as dashboard_main

        raise SystemExit(dashboard_main())

    from pricing_scraper.cli import main as cli_main

    raise SystemExit(cli_main())
