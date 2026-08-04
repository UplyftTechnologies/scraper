"""Run a collection in a detached process so it survives the browser closing.

Streamlit ties a script run to the browser session and drops the session two
minutes after the tab closes, which kills a long collection. Starting the work
as a separate process instead means the run keeps going as long as the server
is up, and any browser can reattach to it by reading its status file.

    python -m pricing_scraper.background <run_id>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

RUNS_DIRNAME = Path("data") / "runs"
ACTIVE_STATES = frozenset({"starting", "running"})


class RunStopped(RuntimeError):
    """Raised inside the worker when the dashboard asks the run to stop."""


@dataclass(slots=True)
class RunRequest:
    """Everything the worker needs to reproduce a dashboard collection."""

    site: str
    categories: list[str]
    page_limit: int
    resume: bool
    enrich_details: bool
    config_path: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def runs_dir(root: Path | None = None) -> Path:
    """Return the directory holding job, status, and log files."""
    base = (root or Path(__file__).resolve().parents[1]) / RUNS_DIRNAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _job_path(run_id: str, root: Path | None = None) -> Path:
    return runs_dir(root) / f"{run_id}.job.json"


def _status_path(run_id: str, root: Path | None = None) -> Path:
    return runs_dir(root) / f"{run_id}.status.json"


def _stop_path(run_id: str, root: Path | None = None) -> Path:
    return runs_dir(root) / f"{run_id}.stop"


def log_path(run_id: str, root: Path | None = None) -> Path:
    """Return the worker's captured stdout/stderr path."""
    return runs_dir(root) / f"{run_id}.log"


def read_log(
    run_id: str,
    *,
    lines: int = 200,
    root: Path | None = None,
    max_bytes: int = 256_000,
) -> str:
    """Return the tail of a run's log.

    Only the last max_bytes are read, so a run that has logged one line per
    request for hours still renders instantly.
    """
    path = log_path(run_id, root)
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # Drop the partial line the seek landed in.
            tail = handle.read()
    except OSError:
        return ""
    return "\n".join(tail.splitlines()[-max(1, lines):])


def write_status(
    run_id: str,
    values: Mapping[str, Any],
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """Merge values into the run's status file and return the full status.

    The file is replaced atomically so a dashboard polling it never reads a
    half-written document.
    """
    status = read_status(run_id, root=root) or {"run_id": run_id}
    status.update(values)
    status["updated_at"] = _now()
    payload = json.dumps(status, ensure_ascii=False, indent=2)
    path = _status_path(run_id, root)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return status
        except PermissionError:
            # Windows refuses to replace a file another process has open, and
            # the dashboard polls this file constantly. Retry, then fall back.
            time.sleep(0.05 * (attempt + 1))
    path.write_text(payload, encoding="utf-8")
    temporary.unlink(missing_ok=True)
    return status


def update_status_safely(
    run_id: str,
    values: Mapping[str, Any],
    *,
    root: Path | None = None,
) -> None:
    """Write status without ever interrupting the collection.

    A status file is only a progress report. Losing one must never abandon a
    run that has already spent hours on rate-limited requests.
    """
    try:
        write_status(run_id, values, root=root)
    except OSError as exc:
        print(f"status_write_failed run={run_id} error={exc}", file=sys.stderr)


def read_status(
    run_id: str,
    *,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """Read one run's status, or None when it has never been written."""
    path = _status_path(run_id, root)
    for attempt in range(3):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            # The worker may be mid-replace; a moment later the file is whole.
            time.sleep(0.05 * (attempt + 1))
            continue
        return payload if isinstance(payload, dict) else None
    return None


def all_statuses(root: Path | None = None) -> list[dict[str, Any]]:
    """Return every known run status, newest first."""
    statuses: list[dict[str, Any]] = []
    for path in runs_dir(root).glob("*.status.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            statuses.append(payload)
    statuses.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    return statuses


def latest_status(root: Path | None = None) -> dict[str, Any] | None:
    """Return the most recently started run, finished or not."""
    statuses = all_statuses(root)
    return statuses[0] if statuses else None


def active_status(root: Path | None = None) -> dict[str, Any] | None:
    """Return the run that is still working, if there is one.

    A run whose process is gone without a terminal status is reported as
    failed rather than blocking the next run forever.
    """
    for status in all_statuses(root):
        if status.get("state") not in ACTIVE_STATES:
            continue
        pid = status.get("pid")
        if pid and not _process_alive(int(pid)):
            return write_status(
                str(status["run_id"]),
                {
                    "state": "failed",
                    "error": (
                        "The worker process exited without finishing. Check "
                        "the run log for the cause."
                    ),
                    "finished_at": _now(),
                },
                root=root,
            )
        return status
    return None


def _process_alive(pid: int) -> bool:
    """Report whether a process id is still running, portably."""
    if sys.platform == "win32":
        try:
            output = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return True
        return str(pid) in output
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_run(request: RunRequest, *, root: Path | None = None) -> dict[str, Any]:
    """Launch the collection in a detached process and return its status.

    Only one run may be active at a time: concurrent runs would write the same
    checkpoints and export files.
    """
    running = active_status(root)
    if running is not None:
        raise RuntimeError(
            f"A {running.get('site', 'collection')} run is already in "
            "progress. Wait for it to finish or stop it first."
        )
    base = root or Path(__file__).resolve().parents[1]
    _job_path(request.run_id, root).write_text(
        json.dumps(asdict(request), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _stop_path(request.run_id, root).unlink(missing_ok=True)
    status = write_status(
        request.run_id,
        {
            "run_id": request.run_id,
            "site": request.site,
            "state": "starting",
            "stage": "queued",
            "percent": 0,
            "message": "Starting the collection process...",
            "categories": list(request.categories),
            "page_limit": request.page_limit,
            "listing_products": 0,
            "detail_parents": 0,
            "sku_rows": 0,
            "started_at": _now(),
            "finished_at": "",
            "error": "",
        },
        root=root,
    )

    # Detach so the worker outlives the Streamlit session that started it.
    creationflags = 0
    start_new_session = False
    if sys.platform == "win32":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        start_new_session = True

    with log_path(request.run_id, root).open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "pricing_scraper.background", request.run_id],
            cwd=str(base),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    return write_status(request.run_id, {"pid": process.pid}, root=root)


def request_stop(run_id: str, *, root: Path | None = None) -> None:
    """Ask a running collection to stop at its next progress checkpoint."""
    _stop_path(run_id, root).write_text(_now(), encoding="utf-8")
    write_status(
        run_id,
        {"message": "Stop requested; finishing the current request..."},
        root=root,
    )


def stop_requested(run_id: str, *, root: Path | None = None) -> bool:
    """Report whether the dashboard asked this run to stop."""
    return _stop_path(run_id, root).exists()


def _execute(run_id: str, *, root: Path | None = None) -> int:
    """Run one collection to completion inside the detached worker process."""
    from pricing_scraper.config import (
        apply_environment_overrides,
        load_config,
    )
    from pricing_scraper.dashboard_service import (
        collect_amazon,
        collect_nykaa,
        collect_tira,
    )
    from pricing_scraper.exporter import load_products_csv

    job = json.loads(_job_path(run_id, root).read_text(encoding="utf-8"))
    site = str(job["site"])
    write_status(
        run_id,
        {"state": "running", "message": "Loading configuration..."},
        root=root,
    )

    config = load_config(Path(str(job["config_path"])))
    apply_environment_overrides(config)

    counters = {
        "listing_products": 0,
        "detail_parents": 0,
        "sku_rows": 0,
        "percent": 0,
    }

    def report(stage: str, current: int, total: int, message: str) -> None:
        if stop_requested(run_id, root=root):
            raise RunStopped("The dashboard asked this run to stop.")
        if stage == "listing_products":
            counters["listing_products"] = current
        elif stage in {"details", "detail_parents"}:
            counters["detail_parents"] = current
        elif stage in {"detail_products", "sku_rows"}:
            counters["sku_rows"] = current
        if total:
            # Stages that report no total (running counts such as SKU rows)
            # keep the last real percentage instead of resetting the bar.
            counters["percent"] = int(min(100, max(0, (current / total) * 100)))
        update_status_safely(
            run_id,
            {"stage": stage, "message": message, **counters},
            root=root,
        )

    collector = {
        "nykaa": collect_nykaa,
        "tira": collect_tira,
        "amazon": collect_amazon,
    }[site]
    result = collector(
        config,
        list(job["categories"]),
        int(job["page_limit"]),
        resume=bool(job["resume"]),
        enrich_details=bool(job["enrich_details"]),
        progress_callback=report,
    )
    exported = len(load_products_csv(result.export.csv_path))
    write_status(
        run_id,
        {
            "state": "success" if result.completed else "incomplete",
            "stage": "finished",
            "percent": 100,
            "message": (
                "Scraping complete."
                if result.completed
                else "Scraping paused; the checkpoint was saved."
            ),
            "listing_products": result.listing_products,
            "detail_parents": result.detail_parents,
            "sku_rows": len(result.products),
            "exported_rows": exported,
            "completed": result.completed,
            "next_page": result.next_page,
            "stop_reasons": list(result.stop_reasons),
            "failures": result.failures,
            "blocks": result.blocks,
            "requests": result.requests,
            "csv_path": str(result.export.csv_path),
            "excel_path": str(result.export.excel_path),
            "products_written": result.export.products_written,
            "database_enabled": result.export.database_enabled,
            "database_products_written": result.export.database_products_written,
            "database_price_points_written": (
                result.export.database_price_points_written
            ),
            "database_error": result.export.database_error,
            "finished_at": _now(),
        },
        root=root,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Worker entry point: ``python -m pricing_scraper.background <run_id>``."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: python -m pricing_scraper.background <run_id>")
        return 2
    run_id = arguments[0]
    try:
        return _execute(run_id)
    except RunStopped as exc:
        write_status(
            run_id,
            {
                "state": "stopped",
                "message": str(exc),
                "finished_at": _now(),
            },
        )
        return 0
    except BaseException as exc:  # noqa: BLE001 - the status file is the report
        write_status(
            run_id,
            {
                "state": "failed",
                "message": "The collection failed.",
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": _now(),
            },
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
