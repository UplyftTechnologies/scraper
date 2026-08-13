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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

RUNS_DIRNAME = Path("data") / "runs"
ACTIVE_STATES = frozenset({"starting", "running"})
# A live worker rewrites its status on every page and every parent it finishes.
# The quietest stretch is the final export and database sync, so the timeout is
# generous; anything past it means nobody is working on the run.
HEARTBEAT_TIMEOUT_SECONDS = 1_800


class RunStopped(BaseException):
    """Raised inside the worker when the dashboard asks the run to stop.

    It derives from BaseException on purpose. The clients and the collection
    service isolate per-page and per-product failures with broad ``except
    Exception`` handlers, and a stop that those handlers could swallow would
    leave the dashboard's button looking broken.
    """


@dataclass(slots=True)
class RunRequest:
    """Everything the worker needs to reproduce a dashboard collection."""

    site: str
    categories: list[str]
    page_limit: int
    resume: bool
    enrich_details: bool
    config_path: str
    # "collect" runs a normal collection; "gtin" fills in missing barcodes only.
    mode: str = "collect"
    refresh_only_stale: bool = True
    gtin_all: bool = False
    gtin_limit: int = 0
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
    heartbeat: bool = True,
) -> dict[str, Any]:
    """Merge values into the run's status file and return the full status.

    The file is replaced atomically so a dashboard polling it never reads a
    half-written document.

    ``heartbeat`` records that the worker itself is alive. Dashboard-side
    writes pass ``False``: a stop request proves the browser is working, not
    that anyone is still collecting, and treating it as a sign of life would
    keep an abandoned run looking active.
    """
    status = read_status(run_id, root=root) or {"run_id": run_id}
    status.update(values)
    status["updated_at"] = _now()
    if heartbeat:
        status["heartbeat"] = status["updated_at"]
    payload = json.dumps(status, ensure_ascii=False, indent=2)
    path = _status_path(run_id, root)
    temporary = path.with_suffix(".tmp")
    # Flush to disk before renaming. A rename that reaches the disk ahead of
    # the contents leaves a NUL-filled file behind after a crash, which is how
    # a status or checkpoint file becomes unreadable.
    with temporary.open("wb", buffering=0) as handle:
        handle.write(payload.encode("utf-8"))
        os.fsync(handle.fileno())
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
        reason = _death_reason(status)
        if reason:
            run_id = str(status["run_id"])
            # The stop file would otherwise outlive the run and confuse the
            # next reader of this directory.
            _stop_path(run_id, root).unlink(missing_ok=True)
            return write_status(
                run_id,
                {
                    "state": "failed",
                    "error": f"{reason} Check the run log for the cause.",
                    "finished_at": _now(),
                },
                root=root,
            )
        return status
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _system_boot_time() -> datetime | None:
    """When this machine last booted, or None when it cannot be determined.

    Nothing that was running before the last boot is running now, which is the
    only reliable way to retire a run whose process id has been recycled.
    """
    now = datetime.now(timezone.utc)
    if sys.platform == "win32":
        try:
            import ctypes

            milliseconds = ctypes.windll.kernel32.GetTickCount64()
        except Exception:
            return None
        return now - timedelta(milliseconds=int(milliseconds))
    try:
        uptime = float(
            Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        )
    except (OSError, IndexError, ValueError):
        return None
    return now - timedelta(seconds=uptime)


def _death_reason(status: Mapping[str, Any]) -> str:
    """Explain why a run marked active cannot still be running, else "".

    A process id alone proves nothing: the operating system reuses ids, so a
    status left behind by a killed worker can point at an unrelated process
    that happens to be alive now. Three independent signals are checked.
    """
    heartbeat = _parse_timestamp(
        status.get("heartbeat") or status.get("updated_at")
    )
    boot_time = _system_boot_time()
    if heartbeat is not None and boot_time is not None and heartbeat < boot_time:
        return "The server restarted while this run was working."

    if heartbeat is not None:
        idle = (datetime.now(timezone.utc) - heartbeat).total_seconds()
        if idle > HEARTBEAT_TIMEOUT_SECONDS:
            return (
                "The worker stopped reporting progress "
                f"{int(idle // 60)} minutes ago."
            )

    pid = status.get("pid")
    if pid and not _process_alive(int(pid), run_id=str(status.get("run_id") or "")):
        return "The worker process exited without finishing."
    return ""


def _process_alive(pid: int, *, run_id: str = "") -> bool:
    """Report whether the worker for this run is still running, portably.

    The process must also look like the worker: a recycled process id running
    something else entirely does not keep a dead run alive.
    """
    if sys.platform == "win32":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        output = (completed.stdout or "").strip()
        # Only a clear answer may retire a run. Under heavy load tasklist can
        # exit non-zero or return nothing at all, and reading that as "the
        # process is gone" marks a healthy worker failed - which then lets a
        # second run of the same site start and collide with its checkpoints.
        if completed.returncode != 0 or not output:
            return True
        if str(pid) not in output:
            return False
        image = output.split(None, 1)[0].casefold()
        # The worker is always started with sys.executable.
        return "python" in image
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if run_id:
        try:
            command = Path(f"/proc/{pid}/cmdline").read_text(encoding="utf-8")
        except OSError:
            return True
        if command:
            return run_id in command
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
    write_status(
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

    try:
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
    except OSError as exc:
        # The status was already written as "starting". Left that way, a worker
        # that never launched would look like a live run and refuse every new
        # one until the heartbeat timeout expired half an hour later.
        write_status(
            request.run_id,
            {
                "state": "failed",
                "message": "The collection process could not be started.",
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": _now(),
            },
            root=root,
        )
        raise RuntimeError(
            f"Could not start the collection process: {exc}"
        ) from exc
    return write_status(request.run_id, {"pid": process.pid}, root=root)


def request_stop(run_id: str, *, root: Path | None = None) -> None:
    """Ask a running collection to stop at its next progress checkpoint."""
    _stop_path(run_id, root).write_text(_now(), encoding="utf-8")
    write_status(
        run_id,
        {
            "message": "Stop requested; finishing the current request...",
            "stop_requested_at": _now(),
        },
        root=root,
        heartbeat=False,
    )


def stop_requested(run_id: str, *, root: Path | None = None) -> bool:
    """Report whether the dashboard asked this run to stop."""
    return _stop_path(run_id, root).exists()


def _execute_gtin_only(
    run_id: str,
    job: Mapping[str, Any],
    config: dict[str, Any],
    site: str,
    *,
    report: Any,
    sleeper: Any,
    root: Path | None,
) -> int:
    """Run the barcode-only mode inside the same detached worker."""
    from pricing_scraper.gtin_scrape import collect_gtins

    result = collect_gtins(
        config,
        site,
        only_missing=not bool(job.get("gtin_all", False)),
        limit=max(0, int(job.get("gtin_limit", 0) or 0)),
        progress_callback=report,
        sleeper=sleeper,
    )
    export = result.export
    write_status(
        run_id,
        {
            "state": "success",
            "stage": "finished",
            "percent": 100,
            "message": result.summary(),
            "listing_products": result.stored_products,
            "detail_parents": result.targeted,
            "sku_rows": result.found,
            "completed": True,
            "gtin_found": result.found,
            "gtin_targeted": result.targeted,
            "gtin_coverage": round(result.coverage, 1),
            "failures": result.failures,
            "blocks": 0,
            "requests": result.requests,
            "stop_reasons": [],
            "csv_path": str(export.csv_path) if export else "",
            "excel_path": str(export.excel_path) if export else "",
            "products_written": export.products_written if export else 0,
            "database_enabled": export.database_enabled if export else False,
            "database_products_written": (
                export.database_products_written if export else 0
            ),
            "database_price_points_written": (
                export.database_price_points_written if export else 0
            ),
            "database_error": export.database_error if export else "",
            "finished_at": _now(),
        },
        root=root,
    )
    return 0


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

    def interruptible_sleep(seconds: float) -> None:
        """Sleep in slices so a stop is noticed inside a long retry backoff.

        Rate limiting and soft-block backoff can wait a minute at a time. Left
        as one uninterruptible sleep, the dashboard's stop appears to be
        ignored for as long as that wait lasts.
        """
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            if stop_requested(run_id, root=root):
                raise RunStopped("The dashboard asked this run to stop.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))

    if str(job.get("mode") or "collect") == "gtin":
        return _execute_gtin_only(
            run_id,
            job,
            config,
            site,
            report=report,
            sleeper=interruptible_sleep,
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
        refresh_only_stale=bool(job.get("refresh_only_stale", True)),
        progress_callback=report,
        sleeper=interruptible_sleep,
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
