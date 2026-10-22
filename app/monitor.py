#!/usr/bin/env python3
"""Wideband RTL-SDR activity monitor.

One rtl_sdr process produces unsigned 8-bit interleaved IQ.  The monitor
computes a windowed FFT for each block and feeds per-channel power into an
adaptive detector.  Only completed, validated transmissions are persisted.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import numpy as np
import yaml

LOG = logging.getLogger("sdr_activity_monitor")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime | None) -> str | None:
    return ts.isoformat(timespec="milliseconds") if ts else None


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)
    if not isinstance(cfg, dict):
        raise ValueError("configuration root must be a mapping")
    for required in ("node", "receiver", "groups", "assets"):
        if required not in cfg:
            raise ValueError(f"missing configuration key: {required}")
    group_ids = {group["id"] for group in cfg["groups"]}
    active = cfg["receiver"]["active_group"]
    if active not in group_ids:
        raise ValueError(f"active_group {active!r} is not defined")
    enabled = [a for a in cfg["assets"] if a.get("monitoring", {}).get("enabled", True)]
    if not enabled:
        raise ValueError("at least one asset must be enabled")
    group = next(g for g in cfg["groups"] if g["id"] == active)
    center = int(group["center_frequency"])
    rate = int(group["sample_rate"])
    low, high = center - rate / 2, center + rate / 2
    for asset in enabled:
        if asset["monitoring"]["group"] == active:
            freq = int(asset["frequency"])
            if not low < freq < high:
                raise ValueError(f"asset {asset['id']} lies outside active capture window")
    return cfg


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
              asset_id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
              frequency INTEGER NOT NULL, metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nodes (
              node_id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata_json TEXT NOT NULL,
              last_seen TEXT, health_status TEXT
            );
            CREATE TABLE IF NOT EXISTS transmissions (
              observation_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, asset_id TEXT NOT NULL,
              frequency INTEGER NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
              duration_seconds REAL NOT NULL, peak_signal_db REAL NOT NULL,
              average_signal_db REAL NOT NULL, noise_floor_db REAL NOT NULL,
              signal_above_noise_db REAL NOT NULL, monitor_group TEXT NOT NULL,
              event_type TEXT NOT NULL DEFAULT 'transmission',
              FOREIGN KEY(asset_id) REFERENCES assets(asset_id)
            );
            CREATE INDEX IF NOT EXISTS ix_tx_asset_start ON transmissions(asset_id, start_time);
            CREATE TABLE IF NOT EXISTS activity_sessions (
              session_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, asset_id TEXT NOT NULL,
              start_time TEXT NOT NULL, end_time TEXT, transmission_count INTEGER NOT NULL,
              total_airtime_seconds REAL NOT NULL, alerted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS alerts (
              alert_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, asset_id TEXT NOT NULL,
              created_at TEXT NOT NULL, delivered INTEGER NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS receiver_health (
              id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL,
              observed_at TEXT NOT NULL, status TEXT NOT NULL, samples_received INTEGER NOT NULL,
              fft_frames INTEGER NOT NULL, details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channel_power_samples (
              id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
              asset_id TEXT NOT NULL, power_db REAL NOT NULL, noise_floor_db REAL,
              signal_above_noise_db REAL, state TEXT NOT NULL,
              interference INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_power_asset_time
              ON channel_power_samples(asset_id, observed_at DESC);
            CREATE TABLE IF NOT EXISTS receiver_status (
              node_id TEXT PRIMARY KEY, state TEXT NOT NULL, message TEXT NOT NULL,
              updated_at TEXT NOT NULL, consecutive_failures INTEGER NOT NULL DEFAULT 0,
              next_retry_at TEXT, details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS receiver_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL,
              observed_at TEXT NOT NULL, previous_state TEXT, state TEXT NOT NULL,
              reason TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS ix_receiver_events_time
              ON receiver_events(observed_at DESC);
            """
        )
        tx_columns = {row[1] for row in self.db.execute("PRAGMA table_info(transmissions)")}
        if "event_type" not in tx_columns:
            self.db.execute(
                "ALTER TABLE transmissions ADD COLUMN event_type TEXT NOT NULL DEFAULT 'transmission'")
        additions = {
            "estimated_frequency_hz": "REAL",
            "ctcss_tone_hz": "REAL",
            "ctcss_confidence": "REAL",
            "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
            "alert_suppressed": "INTEGER NOT NULL DEFAULT 0",
            "suppression_reason": "TEXT",
        }
        tx_columns = {row[1] for row in self.db.execute("PRAGMA table_info(transmissions)")}
        for column, definition in additions.items():
            if column not in tx_columns:
                self.db.execute(f"ALTER TABLE transmissions ADD COLUMN {column} {definition}")
        self.db.commit()

    def sync_catalog(self, cfg: dict[str, Any]) -> None:
        node = cfg["node"]
        self.db.execute(
            "INSERT INTO nodes(node_id,name,metadata_json,last_seen,health_status) VALUES(?,?,?,?,?) "
            "ON CONFLICT(node_id) DO UPDATE SET name=excluded.name,metadata_json=excluded.metadata_json",
            (node["id"], node["name"], json.dumps(node), iso(utc_now()), "starting"),
        )
        for asset in cfg["assets"]:
            self.db.execute(
                "INSERT INTO assets(asset_id,name,type,frequency,metadata_json) VALUES(?,?,?,?,?) "
                "ON CONFLICT(asset_id) DO UPDATE SET name=excluded.name,type=excluded.type,"
                "frequency=excluded.frequency,metadata_json=excluded.metadata_json",
                (asset["id"], asset["name"], asset["type"], asset["frequency"], json.dumps(asset)),
            )
        self.db.commit()

    def transmission(self, tx: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO transmissions(observation_id,node_id,asset_id,frequency,start_time,end_time,"
            "duration_seconds,peak_signal_db,average_signal_db,noise_floor_db,signal_above_noise_db,"
            "monitor_group,event_type,estimated_frequency_hz,ctcss_tone_hz,ctcss_confidence,evidence_json,"
            "alert_suppressed,suppression_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(tx[k] for k in ("observation_id", "node_id", "asset_id", "frequency", "start_time",
                                   "end_time", "duration_seconds", "peak_signal_db", "average_signal_db",
                                   "noise_floor_db", "signal_above_noise_db", "monitor_group", "event_type",
                                   "estimated_frequency_hz", "ctcss_tone_hz", "ctcss_confidence",
                                   "evidence_json", "alert_suppressed", "suppression_reason")),
        )
        self.db.commit()

    def receiver_state(self, node_id: str, state: str, message: str,
                       failures: int = 0, next_retry_at: datetime | None = None,
                       details: dict[str, Any] | None = None) -> None:
        now = iso(utc_now())
        previous = self.db.execute(
            "SELECT state FROM receiver_status WHERE node_id=?", (node_id,)).fetchone()
        previous_state = previous[0] if previous else None
        payload = json.dumps(details or {})
        self.db.execute(
            "INSERT INTO receiver_status(node_id,state,message,updated_at,consecutive_failures,next_retry_at,details_json) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET state=excluded.state,"
            "message=excluded.message,updated_at=excluded.updated_at,consecutive_failures=excluded.consecutive_failures,"
            "next_retry_at=excluded.next_retry_at,details_json=excluded.details_json",
            (node_id, state, message, now, failures, iso(next_retry_at), payload))
        if previous_state != state:
            self.db.execute(
                "INSERT INTO receiver_events(node_id,observed_at,previous_state,state,reason,details_json) "
                "VALUES(?,?,?,?,?,?)", (node_id, now, previous_state, state, message, payload))
        self.db.execute("UPDATE nodes SET last_seen=?,health_status=? WHERE node_id=?",
                        (now, state, node_id))
        self.db.commit()

    def session(self, state: "ChannelState", closed: bool = False) -> None:
        s = state.session
        if not s:
            return
        self.db.execute(
            "INSERT INTO activity_sessions VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
            "end_time=excluded.end_time,transmission_count=excluded.transmission_count,"
            "total_airtime_seconds=excluded.total_airtime_seconds,alerted=excluded.alerted",
            (s.id, state.node_id, state.asset["id"], iso(s.started), iso(s.last_activity) if closed else None,
             s.count, s.airtime, int(s.alerted)),
        )
        self.db.commit()

    def alert(self, session_id: str, asset_id: str, delivered: bool, error: str | None) -> None:
        self.db.execute("INSERT INTO alerts VALUES(?,?,?,?,?,?)",
                        (str(uuid.uuid4()), session_id, asset_id, iso(utc_now()), int(delivered), error))
        self.db.commit()

    def health(self, node_id: str, status: str, samples: int, frames: int, details: dict[str, Any]) -> None:
        now = iso(utc_now())
        self.db.execute("UPDATE nodes SET last_seen=?,health_status=? WHERE node_id=?", (now, status, node_id))
        self.db.execute("INSERT INTO receiver_health(node_id,observed_at,status,samples_received,fft_frames,details_json) VALUES(?,?,?,?,?,?)",
                        (node_id, now, status, samples, frames, json.dumps(details)))
        self.db.commit()

    def power_samples(self, observed_at: datetime, powers: dict[str, float],
                      states: dict[str, "ChannelState"], interference: bool) -> None:
        stamp = iso(observed_at)
        rows = []
        for asset_id, power in powers.items():
            state = states[asset_id]
            status = "transmitting" if state.tx_start else "candidate" if state.candidate_start else "idle"
            above = power - state.noise if state.noise is not None else None
            rows.append((stamp, asset_id, power, state.noise, above, status, int(interference)))
        self.db.executemany(
            "INSERT INTO channel_power_samples(observed_at,asset_id,power_db,noise_floor_db,"
            "signal_above_noise_db,state,interference) VALUES(?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def prune_power_samples(self, cutoff: datetime) -> int:
        cursor = self.db.execute("DELETE FROM channel_power_samples WHERE observed_at < ?", (iso(cutoff),))
        self.db.commit()
        return cursor.rowcount

    def close(self) -> None:
        self.db.close()


@dataclass
class Session:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started: datetime = field(default_factory=utc_now)
    last_activity: datetime = field(default_factory=utc_now)
    count: int = 0
    airtime: float = 0.0
    alerted: bool = False


STANDARD_CTCSS_TONES = np.asarray([
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9,
    171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5,
    203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
], dtype=np.float64)


class CTCSSDetector:
    """Bounded in-memory CTCSS detector fed from the existing IQ stream."""

    def __init__(self, channel_hz: int, center_hz: int, sample_rate: int,
                 target_rate: int = 8000, max_seconds: float = 8.0):
        self.offset_hz = channel_hz - center_hz
        self.sample_rate = sample_rate
        self.decimation = max(1, sample_rate // target_rate)
        self.output_rate = sample_rate / self.decimation
        self.max_samples = int(self.output_rate * max_seconds)
        self.sample_index = 0
        self.previous: complex | None = None
        self.audio: deque[float] = deque(maxlen=self.max_samples)

    def reset(self) -> None:
        self.previous = None
        self.audio.clear()

    def feed(self, iq: np.ndarray) -> None:
        indexes = self.sample_index + np.arange(iq.size)
        oscillator = np.exp(-2j * np.pi * self.offset_hz * indexes / self.sample_rate)
        self.sample_index += iq.size
        mixed = iq * oscillator
        usable = mixed.size - (mixed.size % self.decimation)
        if usable < self.decimation:
            return
        baseband = mixed[:usable].reshape(-1, self.decimation).mean(axis=1)
        if self.previous is not None:
            baseband = np.concatenate((np.asarray([self.previous]), baseband))
        self.previous = complex(baseband[-1])
        if baseband.size < 2:
            return
        discriminator = np.angle(baseband[1:] * np.conj(baseband[:-1]))
        self.audio.extend(float(value) for value in discriminator)

    def result(self, expected_tone_hz: float | None = None) -> dict[str, Any]:
        samples = np.asarray(self.audio, dtype=np.float64)
        duration = samples.size / self.output_rate
        if duration < 0.5:
            return {"status": "insufficient", "tone_hz": None, "confidence": 0.0,
                    "snr_db": None, "sample_seconds": duration}
        samples -= float(np.mean(samples))
        samples *= np.hanning(samples.size)
        times = np.arange(samples.size) / self.output_rate
        powers = []
        for tone in STANDARD_CTCSS_TONES:
            projection = np.sum(samples * np.exp(-2j * np.pi * tone * times))
            powers.append(float(abs(projection) ** 2))
        values = np.asarray(powers)
        best_index = int(np.argmax(values))
        best_tone = float(STANDARD_CTCSS_TONES[best_index])
        floor = float(np.median(values)) + 1e-30
        snr_db = 10 * np.log10((values[best_index] + 1e-30) / floor)
        expected_match = expected_tone_hz is None or abs(best_tone - expected_tone_hz) <= 0.6
        detected = snr_db >= 8.0 and expected_match
        confidence = max(0.0, min(1.0, (snr_db - 5.0) / 15.0)) if expected_match else 0.0
        return {"status": "detected" if detected else "not_detected",
                "tone_hz": best_tone if detected else None,
                "candidate_tone_hz": best_tone, "confidence": confidence,
                "snr_db": float(snr_db), "sample_seconds": duration,
                "expected_tone_hz": expected_tone_hz}


class ChannelState:
    """Adaptive channel detector with candidate and dropout hysteresis."""

    def __init__(self, asset: dict[str, Any], node_id: str, group_id: str):
        self.asset, self.node_id, self.group_id = asset, node_id, group_id
        self.det = asset["detection"]
        self.alert_cfg = asset["alert"]
        self.noise: float | None = None
        self.candidate_start: datetime | None = None
        self.tx_start: datetime | None = None
        self.last_above: datetime | None = None
        self.powers: list[float] = []
        self.tx_noise = 0.0
        self.session: Session | None = None
        self.last_alert: datetime | None = None
        self.calibration_powers: deque[float] = deque(maxlen=10000)
        self.estimated_frequencies: list[float] = []
        self.qualifying_frames = 0
        self.completed_events: deque[dict[str, Any]] = deque(maxlen=100)
        self.persistent_until: datetime | None = None
        self.persistent_reference_margin: float | None = None
        self.persistent_reference_tone: float | None = None

    def learn(self, power: float, alpha: float = 0.05) -> None:
        """Learn a baseline while deliberately preventing event creation."""
        self.calibration_powers.append(power)
        if self.noise is None:
            self.noise = power
        else:
            self.noise += alpha * (power - self.noise)

    def finish_calibration(self) -> None:
        """Use a low percentile so brief signals cannot bias startup upward."""
        if self.calibration_powers:
            percentile = float(self.det.get("startup_noise_percentile", 25))
            self.noise = float(np.percentile(np.asarray(self.calibration_powers), percentile))
            self.calibration_powers.clear()

    def begin_calibration(self) -> None:
        """Discard the learned floor and suppress any in-progress detection."""
        self.noise = None
        self.calibration_powers.clear()
        self._reset_tx()

    def update(self, power: float, now: datetime, eligible: bool = True,
               learn_noise: bool = True, estimated_frequency_hz: float | None = None,
               ctcss_result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if self.noise is None:
            self.noise = power
            return None
        threshold = self.noise + float(self.det["threshold_above_noise_db"])
        above = eligible and power > threshold
        if above:
            if self.candidate_start is None:
                self.candidate_start = now
                self.powers = []
                self.estimated_frequencies = []
                self.qualifying_frames = 0
                self.tx_noise = self.noise
            self.last_above = now
            self.powers.append(power)
            self.qualifying_frames += 1
            if estimated_frequency_hz is not None:
                self.estimated_frequencies.append(estimated_frequency_hz)
            elapsed_ms = (now - self.candidate_start).total_seconds() * 1000
            if self.tx_start is None and elapsed_ms >= int(self.det["minimum_tx_ms"]):
                self.tx_start = self.candidate_start
                LOG.info("TX start asset=%s power=%.1f noise=%.1f", self.asset["id"], power, self.noise)
        elif self.candidate_start is not None:
            dropout_ms = (now - (self.last_above or now)).total_seconds() * 1000
            if dropout_ms >= int(self.det["dropout_merge_ms"]):
                result = self._finish_tx(self.last_above or now, ctcss_result) if self.tx_start else None
                self._reset_tx()
                return result
        # Learn quickly before a signal, very slowly while elevated.
        if learn_noise:
            if above or power > self.noise:
                # Real signals usually raise power. Learn upward extremely
                # slowly so sustained carriers/IDs cannot become the baseline.
                alpha = float(self.det.get("noise_rise_alpha", 0.00001))
            else:
                alpha = float(self.det.get("noise_alpha", 0.005))
            self.noise += alpha * (min(power, threshold) - self.noise)
        return None

    def _finish_tx(self, end: datetime, ctcss_result: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.tx_start and self.powers
        duration = max(0.0, (end - self.tx_start).total_seconds())
        avg, peak = float(np.mean(self.powers)), max(self.powers)
        kerchunk_ms = int(self.det.get("kerchunk_max_ms", 0))
        event_type = "kerchunk" if kerchunk_ms > 0 and duration * 1000 < kerchunk_ms else "transmission"
        center = float(np.median(self.estimated_frequencies)) if self.estimated_frequencies else float(self.asset["frequency"])
        center_variation = (float(np.percentile(self.estimated_frequencies, 90)
                                  - np.percentile(self.estimated_frequencies, 10))
                            if self.estimated_frequencies else 0.0)
        minimum_frames = int(self.det.get("minimum_evidence_frames", 3))
        max_center_variation = float(self.det.get("maximum_center_variation_hz", 5000))
        evidence_quality = ("confirmed" if self.qualifying_frames >= minimum_frames
                            and center_variation <= max_center_variation else "weak")
        tone = ctcss_result or {"status": "unavailable", "tone_hz": None, "confidence": 0.0}
        margin = avg - self.tx_noise
        history_cutoff = end - timedelta(minutes=5)
        while self.completed_events and self.completed_events[0]["end"] < history_cutoff:
            self.completed_events.popleft()
        event_summary = {"end": end, "duration": duration, "margin": margin,
                         "center": center, "tone": tone.get("tone_hz")}
        self.completed_events.append(event_summary)
        recent = list(self.completed_events)
        if len(recent) >= 20 and sum(item["duration"] for item in recent) >= 20:
            margins = [item["margin"] for item in recent]
            centers = [item["center"] for item in recent]
            if max(margins) - min(margins) <= 3 and max(centers) - min(centers) <= 1000:
                self.persistent_until = end + timedelta(minutes=60)
                self.persistent_reference_margin = float(np.median(margins))
                detected_tones = [item["tone"] for item in recent if item["tone"] is not None]
                self.persistent_reference_tone = (float(np.median(detected_tones))
                                                  if detected_tones else None)
        suppressed = bool(self.persistent_until and end < self.persistent_until)
        if suppressed and self.persistent_reference_margin is not None:
            material_change = abs(margin - self.persistent_reference_margin) >= 6
            tone_change = (tone.get("tone_hz") is not None and self.persistent_reference_tone is not None
                           and abs(float(tone["tone_hz"]) - self.persistent_reference_tone) > 0.6)
            if material_change or tone_change:
                suppressed = False
                self.persistent_until = None
        if event_type == "transmission" and evidence_quality == "confirmed":
            if self.session is None:
                self.session = Session(started=self.tx_start, last_activity=end)
            self.session.last_activity = end
            self.session.count += 1
            self.session.airtime += duration
        tx = {
            "observation_id": str(uuid.uuid4()), "node_id": self.node_id,
            "asset_id": self.asset["id"], "frequency": int(self.asset["frequency"]),
            "start_time": iso(self.tx_start), "end_time": iso(end), "duration_seconds": duration,
            "peak_signal_db": peak, "average_signal_db": avg, "noise_floor_db": self.tx_noise,
            "signal_above_noise_db": avg - self.tx_noise, "monitor_group": self.group_id,
            "event_type": event_type, "estimated_frequency_hz": center,
            "ctcss_tone_hz": tone.get("tone_hz"), "ctcss_confidence": tone.get("confidence", 0.0),
            "evidence_json": json.dumps({"quality": evidence_quality,
                                          "qualifying_frames": self.qualifying_frames,
                                          "center_variation_hz": center_variation,
                                          "ctcss": tone}),
            "alert_suppressed": int(suppressed),
            "suppression_reason": "persistent_carrier_cooldown" if suppressed else None,
        }
        LOG.info("%s end asset=%s duration=%.2fs peak=%.1fdB above_noise=%.1fdB",
                 event_type.upper(), self.asset["id"], duration, peak, avg - self.tx_noise)
        return tx

    def _reset_tx(self) -> None:
        self.candidate_start = self.tx_start = self.last_above = None
        self.powers = []
        self.estimated_frequencies = []
        self.qualifying_frames = 0

    def should_alert(self, now: datetime) -> bool:
        s, policy = self.session, self.alert_cfg
        if not s or s.alerted or (self.persistent_until and now < self.persistent_until):
            return False
        age = (now - s.started).total_seconds()
        cooldown_ok = self.last_alert is None or (now - self.last_alert).total_seconds() >= int(policy["cooldown_seconds"])
        return (s.count >= int(policy["minimum_transmissions"]) and
                s.airtime >= float(policy["minimum_airtime_seconds"]) and
                age <= int(policy["window_seconds"]) and cooldown_ok)

    def session_expired(self, now: datetime) -> bool:
        return bool(self.session and (now - self.session.last_activity).total_seconds() >= int(self.det["session_timeout_seconds"]))


def select_activity_channels(powers: dict[str, float], states: dict[str, ChannelState],
                             dominance_db: float) -> tuple[set[str], bool]:
    """Choose plausible channels and identify non-dominant broadband rises.

    One clearly dominant channel is retained. Similar simultaneous rises are
    treated as front-end overload/interference rather than multiple transmitters.
    """
    candidates: list[tuple[float, str]] = []
    for asset_id, power in powers.items():
        state = states[asset_id]
        if state.noise is None:
            continue
        excess = power - state.noise
        if excess > float(state.det["threshold_above_noise_db"]):
            candidates.append((excess, asset_id))
    if len(candidates) < 2:
        return {asset_id for _, asset_id in candidates}, False
    candidates.sort(reverse=True)
    if candidates[0][0] - candidates[1][0] >= dominance_db:
        return {candidates[0][1]}, True
    return set(), True


def detect_frontend_desense(powers: dict[str, float], states: dict[str, ChannelState],
                            drop_db: float, minimum_channels: int = 2) -> bool:
    """Detect a strong out-of-window signal making several channels go quiet."""
    depressed = sum(
        1 for asset_id, power in powers.items()
        if states[asset_id].noise is not None and power <= states[asset_id].noise - drop_db
    )
    return depressed >= minimum_channels


class SpectrumAnalyzer:
    def __init__(self, center: int, sample_rate: int, fft_size: int, assets: list[dict[str, Any]]):
        self.center, self.sample_rate, self.fft_size = center, sample_rate, fft_size
        self.window = np.hanning(fft_size).astype(np.float32)
        self.assets = assets
        self.freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, 1.0 / sample_rate)) + center
        self.masks: dict[str, np.ndarray] = {}
        for asset in assets:
            half_bw = int(asset.get("bandwidth", 15000)) / 2
            indexes = np.flatnonzero(np.abs(self.freqs - int(asset["frequency"])) <= half_bw)
            if indexes.size == 0:
                raise ValueError(f"FFT has no bins for {asset['id']}")
            self.masks[asset["id"]] = indexes

    def powers(self, iq: np.ndarray) -> dict[str, float]:
        return {key: value["power_db"] for key, value in self.analyze(iq).items()}

    def analyze(self, iq: np.ndarray) -> dict[str, dict[str, float]]:
        spectrum = np.fft.fftshift(np.fft.fft(iq * self.window))
        psd = (np.abs(spectrum) ** 2) / (self.window.sum() ** 2)
        result: dict[str, dict[str, float]] = {}
        for asset in self.assets:
            indexes = self.masks[asset["id"]]
            values = psd[indexes]
            peak = float(np.max(values))
            core = values >= peak / 4.0
            weights = values[core]
            frequencies = self.freqs[indexes][core]
            center = (float(np.sum(frequencies * weights) / np.sum(weights))
                      if float(np.sum(weights)) > 0 else float(asset["frequency"]))
            result[asset["id"]] = {
                "power_db": 10.0 * np.log10(float(np.mean(values)) + 1e-20),
                "estimated_frequency_hz": center,
            }
        return result


def iq_blocks(stream: BinaryIO, fft_size: int) -> Iterator[np.ndarray]:
    need = fft_size * 2
    while True:
        raw = bytearray()
        while len(raw) < need:
            chunk = stream.read(need - len(raw))
            if not chunk:
                return
            raw.extend(chunk)
        vals = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        yield ((vals[0::2] - 127.5) + 1j * (vals[1::2] - 127.5)) / 127.5


def send_discord(webhook: str, state: ChannelState) -> tuple[bool, str | None]:
    s, asset = state.session, state.asset
    assert s
    elapsed = int((s.last_activity - s.started).total_seconds())
    content = (f"📻 **RF Activity Detected**\nChannel: {asset['name']} "
               f"({int(asset['frequency']) / 1e6:.3f} MHz)\nActivity: {s.count} transmissions\n"
               f"Airtime: {s.airtime:.1f} seconds\nWindow: {elapsed} seconds\nReceiver: {state.node_id}")
    request = urllib.request.Request(webhook, data=json.dumps({"content": content}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Discord returned HTTP {response.status}")
        return True, None
    except (urllib.error.URLError, RuntimeError) as exc:
        return False, str(exc)


def classify_capture_failure(message: str, return_code: int | None) -> str:
    """Map rtl_sdr failures to actionable, stable receiver states."""
    text = (message or "").lower()
    if "usb_claim_interface error -6" in text or "resource busy" in text or "device or resource busy" in text:
        return "device_busy"
    if "permission denied" in text or "usb_open error -3" in text:
        return "permission_error"
    if "no supported devices found" in text or "usb_open error -4" in text:
        return "device_missing"
    if "lost at least" in text or "short read" in text or "stalled" in text or "flatlined" in text:
        return "stream_stalled"
    if return_code is not None and return_code < 0:
        return "capture_terminated"
    return "capture_failed"


class Monitor:
    def __init__(self, cfg: dict[str, Any], config_path: Path):
        self.cfg, self.config_path = cfg, config_path
        self.running = True
        storage = Path(cfg["storage"]["database"])
        self.db = Database(storage)
        self.db.sync_catalog(cfg)
        active_id = cfg["receiver"]["active_group"]
        self.group = next(g for g in cfg["groups"] if g["id"] == active_id)
        self.assets = [a for a in cfg["assets"] if a.get("monitoring", {}).get("enabled", True)
                       and a["monitoring"]["group"] == active_id]
        self.states = {a["id"]: ChannelState(a, cfg["node"]["id"], active_id) for a in self.assets}
        self.analyzer = SpectrumAnalyzer(int(self.group["center_frequency"]), int(self.group["sample_rate"]),
                                         int(cfg["receiver"].get("fft_size", 16384)), self.assets)
        self.ctcss_detectors = {
            asset["id"]: CTCSSDetector(int(asset["frequency"]), int(self.group["center_frequency"]),
                                       int(self.group["sample_rate"]))
            for asset in self.assets
        }
        self.samples = self.frames = 0
        self.started = time.monotonic()
        detection = cfg.get("interference_rejection", {})
        self.warmup_seconds = int(detection.get("startup_calibration_seconds", 45))
        self.dominance_db = float(detection.get("dominance_db", 12))
        self.warmup_until = self.started + self.warmup_seconds
        self.calibration_finished = False
        self.calibration_requested = False
        self.last_interference_log = 0.0
        self.interference_frames = 0
        self.strong_signal_guard_db = float(detection.get("strong_signal_guard_db", 20))
        self.overload_recovery_seconds = float(detection.get("overload_recovery_seconds", 2))
        self.desense_drop_db = float(detection.get("desense_drop_db", 4))
        self.baseline_holdoff_until = 0.0
        diagnostics = cfg.get("diagnostics", {})
        self.trace_interval = float(diagnostics.get("power_trace_interval_seconds", 1))
        self.trace_retention_hours = int(diagnostics.get("power_trace_retention_hours", 72))
        self.flatline_window = int(diagnostics.get("flatline_window_seconds", 30))
        self.flatline_min_std_db = float(diagnostics.get("flatline_min_std_db", 0.005))
        self.power_history: deque[tuple[float, ...]] = deque(maxlen=self.flatline_window)
        self.node_id = cfg["node"]["id"]
        self.receiver_state = "initializing"
        self.metrics = {"capture_starts": 0, "capture_failures": 0, "capture_recoveries": 0,
                        "stream_stalls": 0, "device_missing": 0, "device_busy": 0,
                        "permission_errors": 0, "forced_terminations": 0,
                        "consecutive_failures": 0, "last_error": None,
                        "last_successful_frame_at": None}
        recovery = cfg.get("recovery", {})
        self.max_restart_attempts = int(recovery.get("max_restart_attempts", 3))
        self.max_retry_seconds = int(recovery.get("max_retry_seconds", 60))
        self.db.receiver_state(self.node_id, "initializing", "Monitor is initializing")

    def command(self) -> list[str]:
        receiver, group = self.cfg["receiver"], self.group
        cmd = [receiver.get("rtl_sdr_path", "/usr/bin/rtl_sdr"), "-f", str(group["center_frequency"]),
               "-s", str(group["sample_rate"])]
        gain = receiver.get("gain", "auto")
        if str(gain).lower() != "auto":
            cmd.extend(["-g", str(gain)])
        cmd.append("-")
        if receiver.get("device") is not None:
            cmd[1:1] = ["-d", str(receiver["device"])]
        return cmd

    def run(self) -> None:
        retry = int(self.cfg["receiver"].get("retry_seconds", 10))
        while self.running:
            cmd = self.command()
            self.metrics["capture_starts"] += 1
            self._transition("starting", "Starting continuous RTL-SDR capture")
            LOG.info("starting capture center=%s rate=%s assets=%s", self.group["center_frequency"],
                     self.group["sample_rate"], ",".join(a["id"] for a in self.assets))
            try:
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            except OSError as exc:
                self.metrics["capture_failures"] += 1
                self.metrics["last_error"] = str(exc)
                self._transition("receiver_unavailable", str(exc))
                raise
            consume_error: Exception | None = None
            try:
                assert process.stdout
                self._consume(process.stdout)
            except Exception as exc:  # converted into a classified bounded retry below
                consume_error = exc
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    _, stderr = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); _, stderr = process.communicate()
                message = stderr.decode(errors="replace").strip()[-1000:]
                if consume_error:
                    message = f"{message} | {consume_error}".strip(" |")
                if message:
                    LOG.warning("rtl_sdr stopped: %s", message.replace("\n", " | "))
                self.db.health(self.node_id, "disconnected", self.samples, self.frames,
                               {"capture_exit": process.returncode, "message": message})
            if self.running:
                state = classify_capture_failure(message, process.returncode)
                self.metrics["capture_failures"] += 1
                self.metrics["consecutive_failures"] += 1
                self.metrics["last_error"] = message or f"rtl_sdr exited {process.returncode}"
                metric_key = {"device_busy": "device_busy", "permission_error": "permission_errors",
                              "device_missing": "device_missing", "stream_stalled": "stream_stalls"}.get(state)
                if metric_key:
                    self.metrics[metric_key] += 1
                failures = int(self.metrics["consecutive_failures"])
                delay = min(self.max_retry_seconds, retry * (2 ** min(3, failures - 1)))
                next_retry = utc_now() + timedelta(seconds=delay)
                self._transition(state, self.metrics["last_error"], failures, next_retry)
                if failures >= self.max_restart_attempts:
                    raise RuntimeError(
                        f"RTL-SDR capture failed {failures} times; handing recovery to systemd")
                LOG.warning("bounded receiver retry state=%s attempt=%d/%d delay=%ss",
                            state, failures, self.max_restart_attempts, delay)
                deadline = time.monotonic() + delay
                while self.running and time.monotonic() < deadline:
                    time.sleep(min(0.5, deadline - time.monotonic()))

    def _consume(self, stream: BinaryIO) -> None:
        health_interval = int(self.cfg.get("health", {}).get("log_interval_seconds", 60))
        next_health = time.monotonic() + health_interval
        next_trace = time.monotonic() + self.trace_interval
        next_prune = time.monotonic() + 3600
        for iq in iq_blocks(stream, self.analyzer.fft_size):
            if not self.running:
                break
            self.samples += iq.size; self.frames += 1
            now = utc_now()
            analysis = self.analyzer.analyze(iq)
            powers = {key: value["power_db"] for key, value in analysis.items()}
            monotonic_now = time.monotonic()
            self.metrics["last_successful_frame_at"] = iso(now)
            if self.receiver_state != "ready":
                if self.metrics["consecutive_failures"]:
                    self.metrics["capture_recoveries"] += 1
                self.metrics["consecutive_failures"] = 0
                self._transition("ready", "Continuous IQ stream is healthy")
            if self.calibration_requested:
                for state in self.states.values():
                    state.begin_calibration()
                self.power_history.clear()
                self.warmup_until = monotonic_now + self.warmup_seconds
                self.calibration_finished = False
                self.calibration_requested = False
                LOG.info("manual calibration started duration=%ss", self.warmup_seconds)
            if monotonic_now < self.warmup_until:
                for asset_id, power in powers.items():
                    self.states[asset_id].learn(power)
                continue
            if not self.calibration_finished:
                for state in self.states.values():
                    state.finish_calibration()
                self.calibration_finished = True
                LOG.info("startup calibration complete noise_floors=%s",
                         {k: round(v.noise or 0, 1) for k, v in self.states.items()})
            eligible, interference = select_activity_channels(powers, self.states, self.dominance_db)
            desense = detect_frontend_desense(powers, self.states, self.desense_drop_db)
            strong_signal = any(
                state.noise is not None and powers[asset_id] - state.noise >= self.strong_signal_guard_db
                for asset_id, state in self.states.items())
            if strong_signal or desense:
                self.baseline_holdoff_until = monotonic_now + self.overload_recovery_seconds
            overload_active = interference or desense or monotonic_now < self.baseline_holdoff_until
            if overload_active:
                self.interference_frames += 1
            if interference:
                if monotonic_now - self.last_interference_log >= 10:
                    ranked = sorted(((power - (self.states[k].noise or power), k)
                                     for k, power in powers.items()), reverse=True)
                    LOG.warning("broadband/overload suppression strongest=%s excess=%.1fdB eligible=%s",
                                ranked[0][1], ranked[0][0], ",".join(sorted(eligible)) or "none")
                    self.last_interference_log = monotonic_now
            elif desense and monotonic_now - self.last_interference_log >= 10:
                LOG.warning("front-end desense suppression: multiple channels dropped >=%.1fdB",
                            self.desense_drop_db)
                self.last_interference_log = monotonic_now
            for asset_id, power in powers.items():
                state = self.states[asset_id]
                # A strong nearby signal can make other channels appear much
                # quieter (AGC/desense). Do not learn those depressed values as
                # new baselines; doing so causes false events when RF recovers.
                baseline_guard = monotonic_now < self.baseline_holdoff_until and asset_id not in eligible
                event_eligible = asset_id in eligible and not desense
                detector = self.ctcss_detectors[asset_id]
                was_candidate = state.candidate_start is not None
                if event_eligible and not was_candidate:
                    detector.reset()
                if event_eligible or was_candidate:
                    detector.feed(iq)
                expected_tone = state.asset.get("access", {}).get("analog_tone_hz")
                expected_tone_value = float(expected_tone) if expected_tone is not None else None
                tone_result = detector.result(expected_tone_value) if was_candidate else None
                tx = state.update(power, now, event_eligible,
                                  learn_noise=(not interference or asset_id in eligible) and not baseline_guard,
                                  estimated_frequency_hz=analysis[asset_id]["estimated_frequency_hz"],
                                  ctcss_result=tone_result)
                if tx:
                    self.db.transmission(tx); self.db.session(state)
                    if state.should_alert(now):
                        self._alert(state)
                    detector.reset()
                elif was_candidate and state.candidate_start is None:
                    detector.reset()
                if state.session_expired(now):
                    self.db.session(state, closed=True)
                    LOG.info("session closed asset=%s tx=%d airtime=%.1fs", asset_id,
                             state.session.count, state.session.airtime)
                    state.session = None
            if monotonic_now >= next_trace:
                self.db.power_samples(now, powers, self.states, overload_active)
                self.power_history.append(tuple(powers[k] for k in sorted(powers)))
                if len(self.power_history) == self.power_history.maxlen:
                    variation = np.std(np.asarray(self.power_history), axis=0)
                    if float(np.max(variation)) < self.flatline_min_std_db:
                        raise RuntimeError(
                            "RF power stream flatlined "
                            f"(max channel std={float(np.max(variation)):.6f} dB)")
                next_trace = monotonic_now + self.trace_interval
            if monotonic_now >= next_prune:
                removed = self.db.prune_power_samples(now - timedelta(hours=self.trace_retention_hours))
                if removed:
                    LOG.info("pruned diagnostic power samples count=%d", removed)
                next_prune = monotonic_now + 3600
            if time.monotonic() >= next_health:
                fps = self.frames / max(0.001, time.monotonic() - self.started)
                details = {"center_frequency": self.group["center_frequency"],
                           "sample_rate": self.group["sample_rate"], "fft_frames_per_second": fps,
                           "interference_frames": self.interference_frames,
                           "noise_floors": {k: v.noise for k, v in self.states.items()},
                           "reliability": dict(self.metrics)}
                self.db.health(self.node_id, "healthy", self.samples, self.frames, details)
                LOG.info("health samples=%d frames=%d fps=%.2f", self.samples, self.frames, fps)
                next_health = time.monotonic() + health_interval

    def _transition(self, state: str, message: str, failures: int | None = None,
                    next_retry_at: datetime | None = None) -> None:
        self.receiver_state = state
        self.db.receiver_state(self.node_id, state, message,
                               int(self.metrics["consecutive_failures"] if failures is None else failures),
                               next_retry_at, dict(self.metrics))

    def _alert(self, state: ChannelState) -> None:
        env_name = self.cfg.get("discord", {}).get("webhook_env", "SDR_MONITOR_DISCORD_WEBHOOK")
        webhook = os.environ.get(env_name)
        if not webhook:
            self.db.alert(state.session.id, state.asset["id"], False, "pending_external_delivery")
            state.session.alerted = True
            state.last_alert = utc_now()
            self.db.session(state)
            LOG.info("queued external alert asset=%s session=%s", state.asset["id"], state.session.id)
            return
        delivered, error = send_discord(webhook, state)
        self.db.alert(state.session.id, state.asset["id"], delivered, error)
        if delivered:
            state.session.alerted = True; state.last_alert = utc_now(); self.db.session(state)
            LOG.info("Discord alert delivered asset=%s", state.asset["id"])
        else:
            LOG.error("Discord alert failed asset=%s error=%s", state.asset["id"], error)

    def stop(self, *_: Any) -> None:
        self.running = False

    def request_calibration(self, *_: Any) -> None:
        """Request a fresh noise-floor calibration from the signal handler."""
        self.calibration_requested = True

    def close(self) -> None:
        for state in self.states.values():
            if state.session:
                self.db.session(state, closed=True)
        self.db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Wideband RTL-SDR activity monitor")
    parser.add_argument("--config", type=Path, default=Path("/opt/sdr-activity-monitor/config/config.yaml"))
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        if args.check_config:
            print(f"configuration valid: {len(cfg['assets'])} assets")
            return 0
        logging.basicConfig(level=getattr(logging, cfg.get("logging", {}).get("level", "INFO").upper()),
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        monitor = Monitor(cfg, args.config)
        signal.signal(signal.SIGTERM, monitor.stop); signal.signal(signal.SIGINT, monitor.stop)
        signal.signal(signal.SIGHUP, monitor.request_calibration)
        try:
            monitor.run()
        finally:
            monitor.close()
        return 0
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        print(f"configuration/startup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
