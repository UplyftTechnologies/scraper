import os
import tempfile
import unittest
from pathlib import Path

from pricing_scraper.background import (
    RunRequest,
    active_status,
    latest_status,
    read_status,
    request_stop,
    start_run,
    stop_requested,
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
