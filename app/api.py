"""Structured read-only API for the SDR dashboard application."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.diagnostics import build_diagnostics


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _transmission(row: sqlite3.Row) -> dict[str, Any]:
    evidence = _decode(row["evidence_json"], {})
    return {
        "id": row["observation_id"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "duration_seconds": row["duration_seconds"],
        "event_type": row["event_type"],
        "margin_db": row["signal_above_noise_db"],
        "ctcss_tone_hz": row["ctcss_tone_hz"],
        "ctcss_confidence": row["ctcss_confidence"],
        "evidence_quality": evidence.get("quality", "legacy"),
        "estimated_frequency_hz": row["estimated_frequency_hz"],
        "alert_suppressed": bool(row["alert_suppressed"]),
        "suppression_reason": row["suppression_reason"],
    }


def dashboard_data(db_path: Path) -> dict[str, Any]:
    """Return map, activity, station history, and diagnostics in one snapshot."""
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        assets = db.execute("SELECT * FROM assets ORDER BY frequency").fetchall()
        stations = []
        now = datetime.now(timezone.utc)
        for asset in assets:
            metadata = _decode(asset["metadata_json"], {})
            power = db.execute(
                "SELECT * FROM channel_power_samples WHERE asset_id=? ORDER BY id DESC LIMIT 1",
                (asset["asset_id"],)).fetchone()
            recent_rows = db.execute(
                """SELECT * FROM transmissions WHERE asset_id=?
                   ORDER BY start_time DESC LIMIT 10""", (asset["asset_id"],)).fetchall()
            history = [_transmission(row) for row in recent_rows]
            latest = history[0] if history else None
            sample_age = None
            live_state = "idle"
            if power:
                observed = datetime.fromisoformat(power["observed_at"])
                sample_age = max(0.0, (now - observed).total_seconds())
                if sample_age <= 3 and power["state"] == "transmitting":
                    live_state = "transmitting"
                elif sample_age <= 3 and power["state"] == "candidate":
                    live_state = "candidate"
            if live_state == "idle" and latest and latest["event_type"] == "kerchunk":
                ended = datetime.fromisoformat(latest["end_time"])
                if (now - ended).total_seconds() <= 60:
                    live_state = "kerchunk"
            location = metadata.get("location") or {}
            access = metadata.get("access") or {}
            stations.append({
                "id": asset["asset_id"],
                "name": asset["name"],
                "type": asset["type"],
                "callsign": metadata.get("callsign"),
                "frequency_hz": asset["frequency"],
                "input_frequency_hz": metadata.get("input_frequency"),
                "offset_hz": metadata.get("offset_hz"),
                "modes": metadata.get("modes") or ([metadata.get("mode")] if metadata.get("mode") else []),
                "access": {
                    "analog_tone_hz": access.get("analog_tone_hz"),
                    "p25_nac": access.get("p25_nac"),
                },
                "services": metadata.get("services") or {},
                "location": {
                    "city": location.get("city"), "state": location.get("state"),
                    "latitude": location.get("latitude"), "longitude": location.get("longitude"),
                },
                "notes": metadata.get("notes"),
                "threshold_db": (metadata.get("detection") or {}).get("threshold_above_noise_db"),
                "live": {
                    "state": live_state,
                    "observed_at": power["observed_at"] if power else None,
                    "sample_age_seconds": sample_age,
                    "power_db": power["power_db"] if power else None,
                    "noise_floor_db": power["noise_floor_db"] if power else None,
                    "margin_db": power["signal_above_noise_db"] if power else None,
                    "interference": bool(power["interference"]) if power else False,
                },
                "last_transmission": latest,
                "history": history,
            })
    diagnostics = build_diagnostics(db_path)
    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "stations": stations,
        "diagnostics": diagnostics,
    }
