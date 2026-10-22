"""Sanitized operational diagnostics for the SDR Activity Monitor."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def build_diagnostics(db_path: Path) -> dict[str, Any]:
    """Build a bounded support bundle without credentials or raw IQ/audio."""
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        status = db.execute("SELECT * FROM receiver_status ORDER BY updated_at DESC LIMIT 1").fetchone()
        health = db.execute("SELECT * FROM receiver_health ORDER BY id DESC LIMIT 1").fetchone()
        events = _rows(db.execute(
            "SELECT observed_at,previous_state,state,reason FROM receiver_events ORDER BY id DESC LIMIT 25"))
        channels = _rows(db.execute(
            """SELECT a.asset_id,a.name,a.frequency,
                      json_extract(a.metadata_json,'$.detection.threshold_above_noise_db') threshold_db,
                      p.observed_at,p.noise_floor_db,p.signal_above_noise_db,p.state,p.interference
               FROM assets a LEFT JOIN channel_power_samples p ON p.id=(
                 SELECT id FROM channel_power_samples WHERE asset_id=a.asset_id ORDER BY id DESC LIMIT 1)
               ORDER BY a.frequency"""))
        counts = dict(db.execute(
            """SELECT COUNT(*) transmissions,
                      SUM(CASE WHEN ctcss_tone_hz IS NOT NULL THEN 1 ELSE 0 END) ctcss_detections,
                      SUM(alert_suppressed) suppressed_events
               FROM transmissions""").fetchone())
        alerts = dict(db.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN delivered=1 THEN 1 ELSE 0 END) delivered,
                      SUM(CASE WHEN delivered=0 THEN 1 ELSE 0 END) pending
               FROM alerts""").fetchone())
    health_payload = dict(health) if health else None
    if health_payload:
        health_payload["details"] = json.loads(health_payload.pop("details_json"))
    status_payload = dict(status) if status else None
    if status_payload:
        status_payload["details"] = json.loads(status_payload.pop("details_json"))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "privacy": {
            "contains": ["receiver state", "bounded state history", "channel thresholds",
                         "aggregate transmission and alert counts"],
            "omits": ["Discord webhook", "environment variables", "raw IQ", "audio",
                      "full transmission history", "browser data"],
        },
        "receiver_status": status_payload,
        "latest_health": health_payload,
        "recent_receiver_events": events,
        "channels": channels,
        "counts": counts,
        "alerts": alerts,
    }
