import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.monitor import (CTCSSDetector, ChannelState, Database, SpectrumAnalyzer,
                         classify_capture_failure, iq_blocks, detect_frontend_desense,
                         load_config, select_activity_channels)
from app.diagnostics import build_diagnostics
from app.api import dashboard_data


class PartialReader(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 7))


class MonitorTests(unittest.TestCase):
    def test_flatline_threshold_distinguishes_static_power(self):
        static = np.asarray([[-44.0, -45.0, -46.0]] * 30)
        live = static.copy()
        live[:, 1] += np.linspace(-0.1, 0.1, 30)
        self.assertLess(float(np.max(np.std(static, axis=0))), 0.005)
        self.assertGreater(float(np.max(np.std(live, axis=0))), 0.005)

    def test_iq_reader_accumulates_partial_pipe_reads(self):
        blocks = list(iq_blocks(PartialReader(bytes(range(32))), 8))
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].shape, (8,))

    def test_config_and_fft_detect_tone(self):
        cfg = load_config(Path("config/config.yaml"))
        self.assertEqual(cfg["assets"][0]["detection"]["threshold_above_noise_db"], 6)
        self.assertEqual(cfg["assets"][1]["detection"]["threshold_above_noise_db"], 6)
        self.assertEqual(cfg["assets"][2]["detection"]["threshold_above_noise_db"], 5)
        wb0iyc = next(asset for asset in cfg["assets"] if asset["id"] == "repeater-wb0iyc-joplin-147000")
        self.assertEqual(wb0iyc["detection"]["threshold_above_noise_db"], 4)
        self.assertEqual(wb0iyc["detection"]["kerchunk_max_ms"], 15000)
        group = cfg["groups"][0]
        analyzer = SpectrumAnalyzer(group["center_frequency"], group["sample_rate"], 16384, cfg["assets"])
        n = np.arange(16384)
        offset = cfg["assets"][1]["frequency"] - group["center_frequency"]
        iq = np.exp(2j * np.pi * offset * n / group["sample_rate"])
        powers = analyzer.powers(iq)
        self.assertGreater(powers["simplex-146520"], powers["channel-145350"] + 40)

    def test_detector_rejects_spike_and_accepts_tx(self):
        cfg = load_config(Path("config/config.yaml"))
        state = ChannelState(cfg["assets"][1], "LOCAL-01", "2m-main")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state.update(-60, start)
        state.update(-30, start + timedelta(milliseconds=100))
        self.assertIsNone(state.update(-60, start + timedelta(milliseconds=500)))
        result = None
        for millis in range(1000, 2300, 100):
            result = state.update(-30, start + timedelta(milliseconds=millis)) or result
        result = state.update(-60, start + timedelta(milliseconds=2700)) or result
        self.assertIsNotNone(result)
        self.assertGreater(result["duration_seconds"], 1.0)
        self.assertEqual(state.session.count, 1)
        self.assertEqual(__import__("json").loads(result["evidence_json"])["quality"], "confirmed")

    def test_startup_learning_does_not_create_transmission(self):
        cfg = load_config(Path("config/config.yaml"))
        state = ChannelState(cfg["assets"][1], "LOCAL-01", "2m-main")
        for power in (-80, -55, -45, -44, -44):
            state.learn(power)
        self.assertIsNone(state.candidate_start)
        self.assertIsNone(state.session)
        self.assertGreater(state.noise, -80)

    def test_startup_calibration_uses_robust_median(self):
        cfg = load_config(Path("config/config.yaml"))
        state = ChannelState(cfg["assets"][1], "LOCAL-01", "2m-main")
        for power in [-44.0] * 100 + [-80.0] * 20 + [-17.0] * 10:
            state.learn(power)
        state.finish_calibration()
        self.assertEqual(state.noise, -44.0)

    def test_manual_calibration_resets_detector_state(self):
        cfg = load_config(Path("config/config.yaml"))
        state = ChannelState(cfg["assets"][1], "LOCAL-01", "2m-main")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state.noise = -60.0
        state.update(-30.0, now)
        self.assertIsNotNone(state.candidate_start)
        state.begin_calibration()
        self.assertIsNone(state.noise)
        self.assertIsNone(state.candidate_start)
        self.assertEqual(len(state.calibration_powers), 0)

    def test_dominant_channel_kept_and_ghosts_suppressed(self):
        cfg = load_config(Path("config/config.yaml"))
        states = {a["id"]: ChannelState(a, "LOCAL-01", "2m-main") for a in cfg["assets"]}
        for state in states.values():
            state.noise = -50
        powers = {"channel-145350": -38, "simplex-146520": -15, "channel-147210": -37}
        eligible, interference = select_activity_channels(powers, states, 12)
        self.assertTrue(interference)
        self.assertEqual(eligible, {"simplex-146520"})

    def test_similar_broadband_rise_suppressed(self):
        cfg = load_config(Path("config/config.yaml"))
        states = {a["id"]: ChannelState(a, "LOCAL-01", "2m-main") for a in cfg["assets"]}
        for state in states.values():
            state.noise = -50
        powers = {"channel-145350": -35, "simplex-146520": -34, "channel-147210": -36}
        eligible, interference = select_activity_channels(powers, states, 12)
        self.assertTrue(interference)
        self.assertEqual(eligible, set())

    def test_overload_does_not_depress_other_channel_baseline(self):
        cfg = load_config(Path("config/config.yaml"))
        state = ChannelState(cfg["assets"][0], "LOCAL-01", "2m-main")
        state.noise = -44.0
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state.update(-80.0, now, eligible=False, learn_noise=False)
        self.assertEqual(state.noise, -44.0)
        self.assertIsNone(state.candidate_start)

    def test_multi_channel_downward_desense_detected(self):
        cfg = load_config(Path("config/config.yaml"))
        states = {a["id"]: ChannelState(a, "LOCAL-01", "2m-main") for a in cfg["assets"]}
        for state in states.values():
            state.noise = -60.0
        powers = {"channel-145350": -62, "simplex-146520": -70, "channel-147210": -66}
        self.assertTrue(detect_frontend_desense(powers, states, 4))
        powers["channel-147210"] = -62
        self.assertFalse(detect_frontend_desense(powers, states, 4))

    def test_database_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            names = {r[0] for r in db.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"assets", "nodes", "transmissions", "activity_sessions", "alerts", "receiver_health"} <= names)
            columns = {r[1] for r in db.db.execute("PRAGMA table_info(transmissions)")}
            self.assertIn("event_type", columns)
            self.assertIn("ctcss_tone_hz", columns)
            self.assertIn("evidence_json", columns)
            self.assertTrue({"receiver_status", "receiver_events"} <= names)
            db.receiver_state("LOCAL-01", "ready", "stream healthy", details={"capture_starts": 1})
            bundle = build_diagnostics(Path(directory) / "test.db")
            self.assertEqual(bundle["receiver_status"]["state"], "ready")
            self.assertIn("environment variables", bundle["privacy"]["omits"])
            db.close()

    def test_structured_dashboard_api_includes_map_and_access_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard.db"
            cfg = load_config(Path("config/config.yaml"))
            db = Database(path)
            db.sync_catalog(cfg)
            db.receiver_state("LOCAL-01", "ready", "stream healthy")
            db.close()
            payload = dashboard_data(path)
            self.assertTrue(payload["ok"])
            self.assertEqual(len(payload["stations"]), 5)
            wb0iyc = next(item for item in payload["stations"] if item["callsign"] == "WB0IYC")
            self.assertEqual(wb0iyc["access"]["analog_tone_hz"], 100.0)
            self.assertIsNotNone(wb0iyc["location"]["latitude"])
            simplex = next(item for item in payload["stations"] if item["type"] == "simplex")
            self.assertIsNone(simplex["location"]["latitude"])

    def test_ctcss_is_demodulated_from_existing_iq(self):
        sample_rate, center, channel, tone = 2_400_000, 146_280_000, 147_000_000, 100.0
        detector = CTCSSDetector(channel, center, sample_rate)
        sample_count = sample_rate
        for start in range(0, sample_count, 16384):
            indexes = np.arange(start, min(start + 16384, sample_count))
            seconds = indexes / sample_rate
            phase = (2 * np.pi * (channel - center) * seconds
                     + (700 / tone) * np.sin(2 * np.pi * tone * seconds))
            detector.feed(np.exp(1j * phase).astype(np.complex64))
        result = detector.result(100.0)
        self.assertEqual(result["status"], "detected")
        self.assertEqual(result["tone_hz"], 100.0)
        self.assertGreater(result["confidence"], 0.8)
        self.assertEqual(detector.result(None)["status"], "detected")

    def test_capture_failure_classification(self):
        self.assertEqual(classify_capture_failure("usb_claim_interface error -6", 1), "device_busy")
        self.assertEqual(classify_capture_failure("Permission denied", 1), "permission_error")
        self.assertEqual(classify_capture_failure("No supported devices found", 1), "device_missing")

    def test_repetitive_stationary_carrier_suppresses_alerts(self):
        cfg = load_config(Path("config/config.yaml"))
        state = ChannelState(cfg["assets"][1], "LOCAL-01", "2m-main")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = None
        for index in range(20):
            end = start + timedelta(seconds=index * 10 + 2)
            state.tx_start = end - timedelta(seconds=2)
            state.powers = [-50.0] * 4
            state.tx_noise = -60.0
            state.qualifying_frames = 4
            state.estimated_frequencies = [146_520_000.0] * 4
            result = state._finish_tx(end)
            state._reset_tx()
        self.assertEqual(result["alert_suppressed"], 1)
        self.assertEqual(result["suppression_reason"], "persistent_carrier_cooldown")

    def test_repeater_kerchunk_is_stored_but_not_counted(self):
        cfg = load_config(Path("config/config.yaml"))
        state = ChannelState(cfg["assets"][0], "LOCAL-01", "2m-main")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state.noise = -60
        result = None
        for millis in range(0, 2300, 100):
            result = state.update(-30, start + timedelta(milliseconds=millis)) or result
        result = state.update(-60, start + timedelta(milliseconds=2700)) or result
        self.assertIsNotNone(result)
        self.assertEqual(result["event_type"], "kerchunk")
        self.assertIsNone(state.session)

    def test_noise_floor_rises_much_slower_than_it_falls(self):
        cfg = load_config(Path("config/config.yaml"))
        state = ChannelState(cfg["assets"][0], "LOCAL-01", "2m-main")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state.noise = -60.0
        state.update(-57.0, now)
        upward_change = state.noise + 60.0
        state.noise = -60.0
        state.update(-63.0, now)
        downward_change = -60.0 - state.noise
        self.assertGreater(downward_change, upward_change * 20)


if __name__ == "__main__":
    unittest.main()
