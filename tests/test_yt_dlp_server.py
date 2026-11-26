import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services import yt_dlp_server


class _BackgroundThread:
    created = None

    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon
        _BackgroundThread.created = self

    def start(self):
        return None


class TransferSyncTests(unittest.TestCase):
    def setUp(self):
        yt_dlp_server.jobs.clear()
        _BackgroundThread.created = None

    def tearDown(self):
        yt_dlp_server.jobs.clear()

    def test_foreground_timeout_keeps_job_syncing_and_schedules_monitor(self):
        job_id = "job"
        yt_dlp_server.jobs[job_id] = {
            "status": "running",
            "phase": "downloading",
            "destination": "/srv/downloads/example",
        }
        status = {
            "config": {"enabled": True, "bucket_name": "bucket"},
            "runtime": {"running": False, "last_result": None},
        }

        with patch.object(
            yt_dlp_server,
            "sync_agent_request",
            side_effect=[status, {"started": True}, status],
        ), patch.object(yt_dlp_server.threading, "Thread", _BackgroundThread), patch.object(
            yt_dlp_server.time, "time", side_effect=[0, 61, 61]
        ):
            result = yt_dlp_server.wait_for_sync_and_cleanup(
                job_id,
                [],
                Path("/srv/downloads/example"),
                threading.Event(),
                wait_seconds=60,
            )

        self.assertFalse(result)
        self.assertEqual(yt_dlp_server.jobs[job_id]["status"], "running")
        self.assertEqual(yt_dlp_server.jobs[job_id]["phase"], "syncing")
        self.assertEqual(yt_dlp_server.jobs[job_id]["sync_status"], "waiting")
        self.assertIsNone(yt_dlp_server.jobs[job_id]["error"])
        self.assertIsNotNone(_BackgroundThread.created)


if __name__ == "__main__":
    unittest.main()
