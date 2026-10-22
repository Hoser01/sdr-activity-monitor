#!/usr/bin/env python3
"""ANSI terminal dashboard for the SDR Activity Monitor SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


LOCAL_TIMEZONE = ZoneInfo("America/Chicago")


def meter(value: float, low: float = -5, high: float = 30, width: int = 24) -> str:
    fraction = max(0.0, min(1.0, (value - low) / (high - low)))
    filled = round(fraction * width)
    return "█" * filled + "·" * (width - filled)


def age(timestamp: str | None) -> str:
    if not timestamp:
        return "never"
    then = datetime.fromisoformat(timestamp)
    now = datetime.now(then.tzinfo)
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def display_time(timestamp: str) -> str:
    """Render database timestamps at a stable width for dashboard tables."""
    return datetime.fromisoformat(timestamp).astimezone(LOCAL_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def format_tone(row: sqlite3.Row) -> str:
    tone = row["ctcss_tone_hz"]
    confidence = row["ctcss_confidence"]
    if tone is None:
        return "—"
    return f"{tone:.1f}/{(confidence or 0) * 100:.0f}%"


def evidence_quality(row: sqlite3.Row) -> str:
    try:
        return json.loads(row["evidence_json"] or "{}").get("quality", "legacy")
    except (TypeError, ValueError):
        return "unknown"


def render(db_path: Path) -> str:
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        health = db.execute("SELECT * FROM receiver_health ORDER BY id DESC LIMIT 1").fetchone()
        receiver = db.execute(
            "SELECT * FROM receiver_status ORDER BY updated_at DESC LIMIT 1").fetchone()
        channels = db.execute(
            """SELECT a.asset_id,a.name,a.frequency,
                      json_extract(a.metadata_json,'$.detection.threshold_above_noise_db') threshold_db,
                      p.observed_at,p.power_db,
                      p.noise_floor_db,p.signal_above_noise_db,p.state,p.interference
               FROM assets a LEFT JOIN channel_power_samples p ON p.id=(
                 SELECT id FROM channel_power_samples WHERE asset_id=a.asset_id ORDER BY id DESC LIMIT 1)
               ORDER BY a.frequency""").fetchall()
        transmissions = db.execute(
            """SELECT a.name,t.start_time,t.duration_seconds,t.signal_above_noise_db,t.event_type,
                      t.ctcss_tone_hz,t.ctcss_confidence,t.alert_suppressed,t.evidence_json,
                      json_extract(a.metadata_json,'$.detection.threshold_above_noise_db') threshold_db
               FROM transmissions t JOIN assets a ON a.asset_id=t.asset_id
               ORDER BY t.start_time DESC LIMIT 8""").fetchall()
        last_transmissions = db.execute(
            """SELECT a.name,a.frequency,t.start_time,t.duration_seconds,
                      t.signal_above_noise_db,t.event_type,t.ctcss_tone_hz,t.ctcss_confidence,
                      t.alert_suppressed,t.evidence_json,
                      json_extract(a.metadata_json,'$.detection.threshold_above_noise_db') threshold_db
               FROM assets a
               LEFT JOIN transmissions t ON t.observation_id=(
                 SELECT observation_id FROM transmissions
                 WHERE asset_id=a.asset_id ORDER BY start_time DESC LIMIT 1)
               ORDER BY a.frequency"""
        ).fetchall()
        sessions = db.execute(
            "SELECT COUNT(*) count FROM activity_sessions WHERE end_time IS NULL").fetchone()["count"]
        alert_counts = db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN delivered=1 THEN 1 ELSE 0 END) AS delivered,
                      SUM(CASE WHEN delivered=0 THEN 1 ELSE 0 END) AS pending
               FROM alerts"""
        ).fetchone()

    lines = ["SDR Activity Monitor", "=" * 80]
    if receiver:
        receiver_details = json.loads(receiver["details_json"])
        retry = (f"  Retry: {display_time(receiver['next_retry_at'])}"
                 if receiver["next_retry_at"] else "")
        lines.append(
            f"State: {receiver['state'].upper():<20}  Updated: {age(receiver['updated_at']):<10} "
            f"Failures: {receiver['consecutive_failures']}  Capture starts: "
            f"{receiver_details.get('capture_starts', 0)}  Recoveries: "
            f"{receiver_details.get('capture_recoveries', 0)}{retry}")
        lines.append(f"Status: {receiver['message'][:120]}")
    if health:
        details = json.loads(health["details_json"])
        displayed_status = receiver["state"] if receiver else health["status"]
        lines.append(
            f"Receiver: {displayed_status.upper():<12}  Health sample: {age(health['observed_at']):<10} "
            f"FFT: {details.get('fft_frames_per_second', 0):.1f}/s  "
            f"Samples: {health['samples_received']:,}  Interference frames: {details.get('interference_frames', 0):,}")
    else:
        lines.append("Receiver: waiting for first health report")
    delivered = alert_counts["delivered"] or 0
    pending = alert_counts["pending"] or 0
    lines.extend([f"Open sessions: {sessions}  Discord alerts: {delivered} delivered, {pending} pending", "",
                  "Channel                 Power     Noise    Margin  Trigger       Δ   State         Signal margin"])
    for row in channels:
        if row["power_db"] is None:
            lines.append(f"{row['name']:<23} waiting for diagnostic sample")
            continue
        margin = row["signal_above_noise_db"] or 0.0
        threshold = float(row["threshold_db"] or 0.0)
        difference = margin - threshold
        warning = " OVERLOAD" if row["interference"] else ""
        lines.append(
            f"{row['name']:<23} {row['power_db']:>6.1f} dB {row['noise_floor_db']:>6.1f} dB "
            f"{margin:>6.1f} dB {threshold:>5.1f} dB {difference:>+6.1f} "
            f"{row['state']:<12} [{meter(margin)}]{warning}")
    time_width = 23
    channel_width = max(
        24,
        *(len(row["name"]) for row in transmissions),
        *(len(row["name"]) for row in last_transmissions),
    )
    table_width = time_width + channel_width + 84
    lines.extend(["", "Last transmission by station", "-" * table_width])
    lines.append(
        f"{'Station':<{channel_width}}  {'Time (Local)':<{time_width}}  "
        f"{'Duration':>8}  {'Type':<12}  {'Margin':>8}  {'Trigger':>8}  {'Δ':>7}  "
        f"{'CTCSS':>9}  {'Evidence':<9}  {'Alert':<10}"
    )
    lines.append("-" * table_width)
    for row in last_transmissions:
        if row["start_time"] is None:
            lines.append(f"{row['name']:<{channel_width}}  {'Never':<{time_width}}")
            continue
        lines.append(
            f"{row['name']:<{channel_width}}  "
            f"{display_time(row['start_time']):<{time_width}}  "
            f"{row['duration_seconds']:>7.1f}s  "
            f"{row['event_type']:<12}  "
            f"{row['signal_above_noise_db']:>5.1f} dB  "
            f"{float(row['threshold_db']):>5.1f} dB  "
            f"{row['signal_above_noise_db'] - float(row['threshold_db']):>+6.1f}  "
            f"{format_tone(row):>9}  {evidence_quality(row):<9}  "
            f"{'suppressed' if row['alert_suppressed'] else 'eligible':<10}"
        )
    lines.extend(["", "Recent completed transmissions", "-" * table_width])
    if not transmissions:
        lines.append("None recorded")
    else:
        lines.append(
            f"{'Time (Local)':<{time_width}}  {'Channel':<{channel_width}}  "
            f"{'Duration':>8}  {'Type':<12}  {'Margin':>8}  {'Trigger':>8}  {'Δ':>7}  "
            f"{'CTCSS':>9}  {'Evidence':<9}  {'Alert':<10}"
        )
        lines.append("-" * table_width)
        for row in transmissions:
            lines.append(
                f"{display_time(row['start_time']):<{time_width}}  "
                f"{row['name']:<{channel_width}}  "
                f"{row['duration_seconds']:>7.1f}s  "
                f"{row['event_type']:<12}  "
                f"{row['signal_above_noise_db']:>5.1f} dB  "
                f"{float(row['threshold_db']):>5.1f} dB  "
                f"{row['signal_above_noise_db'] - float(row['threshold_db']):>+6.1f}  "
                f"{format_tone(row):>9}  {evidence_quality(row):<9}  "
                f"{'suppressed' if row['alert_suppressed'] else 'eligible':<10}"
            )
    lines.append("\nDiagnostics: /api/diagnostics (sanitized, no credentials/IQ/audio)")
    lines.append("Ctrl+C exits the dashboard; monitoring continues in the background.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live SDR activity terminal dashboard")
    parser.add_argument("--database", type=Path,
                        default=Path("/opt/sdr-activity-monitor/data/activity.db"))
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        while True:
            print("\033[2J\033[H" + render(args.database), flush=True)
            if args.once:
                return 0
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
