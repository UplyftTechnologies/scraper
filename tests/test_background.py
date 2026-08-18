import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pricing_scraper import background
from pricing_scraper.background import (
    HEARTBEAT_TIMEOUT_SECONDS,
    RunRequest,
    RunStopped,
    active_status,
    latest_status,
    read_status,
    request_stop,
    start_run,
    stop_requested,
    update_status_safely,
    write_status,
)


class BackgroundRunTests(unittest.TestCase):
    def test_status_round_trips_and_merges_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_status(
                "run-1",
                {"state": "running", "percent": 10, "site": "nykaa"},
                root=root,
            )
            write_status("run-1", {"percent": 60}, root=root)

            status = read_status("run-1", root=root)
            self.assertEqual(status["state"], "running")
            self.assertEqual(status["percent"], 60)
            self.assertEqual(status["site"], "nykaa")
            self.assertTrue(status["updated_at"])

    def test_unknown_run_has_no_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(read_status("missing", root=root))
            self.assertIsNone(latest_status(root))
            self.assertIsNone(active_status(root))

    def test_stop_request_is_visible_to_the_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_status("run-2", {"state": "running"}, root=root)
            self.assertFalse(stop_requested("run-2", root=root))

            request_stop("run-2", root=root)

            self.assertTrue(stop_requested("run-2", root=root))

    def test_second_run_is_refused_while_one_is_working(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # A live pid keeps the run active; concurrent runs would write the
            # same checkpoints and export files.
            write_status(
                "run-3",
                {
                    "state": "running",
                    "site": "tira",
                    "pid": os.getpid(),
                    "started_at": "2026-08-04T00:00:00+00:00",
                },
                root=root,
            )

            with self.assertRaises(RuntimeError) as caught:
                start_run(
                    RunRequest(
                        site="nykaa",
                        categories=["Serums"],
                        page_limit=1,
                        resume=True,
                        enrich_details=True,
                        config_path="config.yaml",
                    ),
                    root=root,
                )

            self.assertIn("already in progress", str(caught.exception))

    def test_a_dead_worker_does_not_block_the_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_status(
                "run-4",
                {
                    "state": "running",
                    "site": "tira",
                    # A pid that cannot be running: the worker died without
                    # writing a terminal status.
                    "pid": 2 ** 31 - 1,
                    "started_at": "2026-08-04T00:00:00+00:00",
                },
                root=root,
            )

            status = active_status(root)

            self.assertEqual(status["state"], "failed")
            self.assertIn("exited without finishing", status["error"])
            self.assertIsNone(active_status(root))

    def test_a_recycled_process_id_does_not_keep_a_dead_run_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_status(
                "run-5",
                {
                    "state": "running",
                    "site": "nykaa",
                    # The pid is alive, but it belongs to something that is not
                    # the worker: the operating system reused the number after
                    # the real worker died.
                    "pid": os.getpid(),
                    "started_at": "2026-08-04T00:00:00+00:00",
                },
                root=root,
            )

            with patch(
                "pricing_scraper.background._process_alive", return_value=False
            ):
                status = active_status(root)

            self.assertEqual(status["state"], "failed")
            self.assertIsNone(active_status(root))

    def test_a_run_that_stopped_reporting_is_retired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = datetime.now(timezone.utc) - timedelta(
                seconds=HEARTBEAT_TIMEOUT_SECONDS + 60
            )
            write_status(
                "run-6",
                {
                    "state": "running",
                    "site": "nykaa",
                    "pid": os.getpid(),
                    "started_at": "2026-08-04T00:00:00+00:00",
                },
                root=root,
            )
            # Age the heartbeat: the worker has not reported for far too long.
            write_status(
                "run-6", {"heartbeat": stale.isoformat()}, root=root, heartbeat=False
            )

            # Pin the boot time. _death_reason checks "did the machine reboot
            # since the heartbeat" first, so on a freshly booted machine that
            # check claims the run instead and the assertion below never sees
            # the reason it is testing.
            with patch(
                "pricing_scraper.background._system_boot_time",
                return_value=None,
            ):
                status = active_status(root)

            self.assertEqual(status["state"], "failed")
            self.assertIn("stopped reporting progress", status["error"])

    def test_a_run_started_before_the_last_boot_is_retired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_status(
                "run-7",
                {"state": "running", "site": "tira", "pid": os.getpid()},
                root=root,
            )
            just_booted = datetime.now(timezone.utc) + timedelta(seconds=5)

            with patch(
                "pricing_scraper.background._system_boot_time",
                return_value=just_booted,
            ):
                status = active_status(root)

            self.assertEqual(status["state"], "failed")
            self.assertIn("server restarted", status["error"])

    def test_requesting_a_stop_is_not_treated_as_a_sign_of_life(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = (
                datetime.now(timezone.utc)
                - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS + 60)
            ).isoformat()
            write_status(
                "run-8",
                {"state": "running", "site": "nykaa", "pid": os.getpid()},
                root=root,
            )
            write_status("run-8", {"heartbeat": stale}, root=root, heartbeat=False)

            request_stop("run-8", root=root)

            # The dashboard write must not refresh the worker's heartbeat, or an
            # abandoned run would look active for as long as someone keeps
            # pressing the button.
            self.assertEqual(read_status("run-8", root=root)["heartbeat"], stale)
            self.assertEqual(active_status(root)["state"], "failed")

    def test_a_retired_run_leaves_no_stop_file_behind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_status(
                "run-9",
                {"state": "running", "site": "nykaa", "pid": 2 ** 31 - 1},
                root=root,
            )
            request_stop("run-9", root=root)

            active_status(root)

            self.assertFalse(stop_requested("run-9", root=root))

    def test_a_stop_is_never_swallowed_by_failure_isolation(self):
        # The clients isolate per-product failures with broad except Exception
        # handlers; a stop that they could catch would look like a dead button.
        self.assertFalse(issubclass(RunStopped, Exception))
        with self.assertRaises(RunStopped):
            try:
                raise RunStopped("stop")
            except Exception:  # noqa: BLE001 - proving it is not caught here
                self.fail("RunStopped must not be caught as an Exception")

    def test_status_survives_a_locked_destination_file(self):
        # Windows refuses os.replace while another process has the status file
        # open, and the dashboard polls it constantly.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_status("run-5", {"state": "running", "percent": 5}, root=root)

            with patch(
                "pricing_scraper.background.os.replace",
                side_effect=PermissionError(5, "Access is denied"),
            ):
                write_status("run-5", {"percent": 50}, root=root)

            status = read_status("run-5", root=root)
            self.assertEqual(status["percent"], 50)
            self.assertEqual(status["state"], "running")

    def test_a_failed_status_write_never_aborts_the_run(self):
        # Losing a progress report must not abandon a collection that has
        # already spent hours on rate-limited requests.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "pricing_scraper.background.write_status",
                side_effect=OSError("disk full"),
            ):
                update_status_safely("run-6", {"percent": 20}, root=root)

    def test_latest_status_prefers_the_newest_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_status(
                "old",
                {"state": "success", "started_at": "2026-08-01T00:00:00+00:00"},
                root=root,
            )
            write_status(
                "new",
                {"state": "success", "started_at": "2026-08-03T00:00:00+00:00"},
                root=root,
            )

            self.assertEqual(latest_status(root)["run_id"], "new")


if __name__ == "__main__":
    unittest.main()


class ProcessLivenessTests(unittest.TestCase):
    """A run may only be retired on a clear answer, never on a failed probe."""

    def _run(self, *, returncode=0, stdout=""):
        from unittest.mock import patch

        completed = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=""
        )
        with patch("pricing_scraper.background.sys.platform", "win32"):
            with patch(
                "pricing_scraper.background.subprocess.run", return_value=completed
            ):
                return background._process_alive(4321, run_id="r1")

    def test_a_listed_python_process_is_alive(self):
        self.assertTrue(
            self._run(stdout="python.exe   4321 Console   1   120,000 K\n")
        )

    def test_a_clear_no_match_retires_the_run(self):
        self.assertTrue(
            not self._run(
                stdout="INFO: No tasks are running which match the specified criteria.\n"
            )
        )

    def test_empty_output_is_treated_as_alive(self):
        """Under load tasklist can return nothing; that is not proof of death.

        Reading it as death marks a healthy worker failed, which then lets a
        second run of the same site start and collide with its checkpoints.
        """
        self.assertTrue(self._run(stdout=""))

    def test_a_failed_probe_is_treated_as_alive(self):
        self.assertTrue(self._run(returncode=1, stdout=""))

    def test_a_recycled_pid_running_something_else_is_not_the_worker(self):
        self.assertTrue(
            not self._run(stdout="notepad.exe   4321 Console   1   8,000 K\n")
        )
