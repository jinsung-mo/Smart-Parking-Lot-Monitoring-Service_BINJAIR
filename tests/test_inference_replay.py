import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.serve_inference_replay import create_replay_app

ROOT = Path(__file__).resolve().parents[1]


class InferenceReplayTest(unittest.TestCase):
    def setUp(self):
        self.app = create_replay_app(ROOT / "docs/demo/b5-transition.json", ROOT / "build/web")
        self.client = self.app.test_client()

    def test_before_start_uses_actual_first_observation(self):
        data = self.client.get("/api/status").json["parking_lot_A"]
        self.assertEqual(data["available_spaces"], 2)
        self.assertEqual(next(s["status"] for s in data["spaces"] if s["id"] == "B5"), "empty")
        self.assertEqual(len(data["spaces"]), 20)

    def test_replays_transition_and_clamps_at_end(self):
        with patch("scripts.serve_inference_replay.time.monotonic", return_value=1000):
            self.client.post("/replay/start")
        with patch("scripts.serve_inference_replay.time.monotonic", return_value=1018.4):
            before = self.client.get("/api/status").json["parking_lot_A"]
        with patch("scripts.serve_inference_replay.time.monotonic", return_value=1018.6):
            after = self.client.get("/api/status").json["parking_lot_A"]
        self.assertEqual(before["available_spaces"], 2)
        self.assertEqual(after["available_spaces"], 1)
        self.assertEqual(after["occupancy_rate"], 95)
        with patch("scripts.serve_inference_replay.time.monotonic", return_value=999999):
            final = self.client.get("/api/status").json["parking_lot_A"]
        self.assertEqual(final, after)

    def test_restart_resets_log_and_playback(self):
        self.client.get("/api/status")
        self.assertTrue(self.client.get("/replay/log").json)
        self.client.post("/replay/start")
        self.assertEqual(self.client.get("/replay/log").json, [])
        self.assertEqual(self.client.get("/api/status").json["parking_lot_A"]["available_spaces"], 2)

    def test_local_proxy_cors_and_no_cache(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
