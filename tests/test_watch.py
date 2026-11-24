"""Offline tests: no network, no API key. Run: python3 -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unifi_watch as uw  # noqa: E402

SAMPLE = Path(__file__).with_name("sample_api.json")


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


class FakeAPI:
    def __init__(self, frames):
        self.frames = list(frames)

    def snapshot(self):
        return self.frames.pop(0)


def site(online, isp):
    return {"online": online, "host_state": "connected" if online else "disconnected", "isp": isp,
            "wan_uptime": 99.0, "external_ip": None, "internet_issues": 0, "offline_devices": 0}


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(STATE_DIR=self.tmp.name, CONFIRM_POLLS="2", REPEAT_MINUTES="15",
                          PRIMARY_ISP="Beachfront=MediaCom", UNIFI_WATCH_ENV="/nonexistent")
        self.cfg = uw.Config()

    def tearDown(self):
        self.tmp.cleanup()

    def run_frames(self, frames):
        n = FakeNotifier()
        w = uw.Watcher(self.cfg, FakeAPI(frames), n)
        for _ in frames:
            w.step()
        return n, w

    def test_single_blip_is_debounced(self):
        n, _ = self.run_frames([{"Beachfront": site(True, "MediaCom")},
                                {"Beachfront": site(False, "MediaCom")},
                                {"Beachfront": site(True, "MediaCom")}])
        self.assertEqual(n.sent, [])

    def test_down_then_up_alerts_once_each(self):
        n, w = self.run_frames([{"Beachfront": site(True, "MediaCom")},
                                {"Beachfront": site(False, "MediaCom")},
                                {"Beachfront": site(False, "MediaCom")},
                                {"Beachfront": site(False, "MediaCom")},
                                {"Beachfront": site(True, "MediaCom")},
                                {"Beachfront": site(True, "MediaCom")}])
        self.assertEqual(len(n.sent), 2)
        self.assertIn("OFFLINE", n.sent[0])
        self.assertIn("back ONLINE", n.sent[1])
        events = [json.loads(l)["event"] for l in w.hist_path.read_text().splitlines()]
        self.assertEqual(events, ["DOWN", "UP"])

    def test_failover_and_failback(self):
        n, _ = self.run_frames([{"Beachfront": site(True, "MediaCom")},
                                {"Beachfront": site(True, "AT&T Wireless")},
                                {"Beachfront": site(True, "MediaCom")}])
        self.assertEqual(len(n.sent), 2)
        self.assertIn("FAILED OVER", n.sent[0])
        self.assertIn("back on primary", n.sent[1])

    def test_dashboard_written(self):
        self.run_frames([{"Beachfront": site(True, "MediaCom"), "Creekside": site(True, "AT&T Internet")}])
        page = (Path(self.tmp.name) / "status.html").read_text()
        self.assertIn("Beachfront", page)
        self.assertIn("Creekside", page)

    def test_snapshot_parses_sample_payload(self):
        api = uw.SiteManager(self.cfg, SAMPLE)
        snap = api.snapshot()
        self.assertTrue(snap["Beachfront"]["online"])
        self.assertEqual(snap["Beachfront"]["isp"], "AT&T Wireless")
        self.assertFalse(snap["HerrinGA Unifi Controller"]["online"])


if __name__ == "__main__":
    unittest.main()
