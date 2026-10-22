#!/usr/bin/env python3
"""Interactive configuration wizard for SDR Activity Monitor."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


def ask(prompt: str, default: Any = None, required: bool = False) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return str(default)
        if not required:
            return ""
        print("A value is required.")


def ask_bool(prompt: str, default: bool = True) -> bool:
    marker = "Y/n" if default else "y/N"
    value = input(f"{prompt} [{marker}]: ").strip().lower()
    return default if not value else value in {"y", "yes", "1", "true"}


def ask_int(prompt: str, default: int) -> int:
    while True:
        try:
            return int(ask(prompt, default))
        except ValueError:
            print("Enter a whole number.")


def ask_float(prompt: str, default: float | None = None) -> float | None:
    while True:
        value = ask(prompt, default)
        if value == "":
            return None
        try:
            return float(value)
        except ValueError:
            print("Enter a number, or leave it blank when optional.")


def hz_from_mhz(prompt: str, default: float | None = None) -> int:
    value = ask_float(prompt, default)
    if value is None:
        raise ValueError(f"{prompt} is required")
    return round(value * 1_000_000)


def channel(index: int, group: str) -> dict[str, Any]:
    print(f"\nChannel {index}")
    kind = ask("Type (repeater/simplex)", "repeater").lower()
    while kind not in {"repeater", "simplex"}:
        kind = ask("Type must be repeater or simplex", "repeater").lower()
    frequency = hz_from_mhz("Output/frequency in MHz", 146.520)
    callsign = ask("Callsign (optional)")
    name = ask("Display name", callsign or f"Channel {frequency / 1e6:.3f}")
    asset: dict[str, Any] = {
        "id": f"channel-{frequency}",
        "type": kind,
        "name": name,
        "frequency": frequency,
        "modes": ["FM"],
        "bandwidth": ask_int("Channel bandwidth in Hz", 15000),
        "location": {"city": None, "state": None, "latitude": None, "longitude": None},
        "monitoring": {"enabled": True, "group": group},
        "detection": {
            "threshold_above_noise_db": ask_float("Trigger margin above noise in dB", 6.0),
            "minimum_tx_ms": ask_int("Minimum transmission duration in ms", 750),
            "minimum_evidence_frames": 3,
            "maximum_center_variation_hz": 5000,
            "dropout_merge_ms": 300,
            "session_timeout_seconds": 300,
            "noise_alpha": 0.0005,
            "noise_rise_alpha": 0.00001,
            "startup_noise_percentile": 25,
        },
        "alert": {
            "minimum_transmissions": ask_int("Transmissions required for an alert", 5 if kind == "repeater" else 3),
            "minimum_airtime_seconds": ask_int("Airtime required for an alert", 15 if kind == "repeater" else 10),
            "window_seconds": ask_int("Alert aggregation window in seconds", 600 if kind == "repeater" else 300),
            "cooldown_seconds": 1800,
        },
    }
    if callsign:
        asset["callsign"] = callsign.upper()
    tone = ask_float("Expected CTCSS tone in Hz (optional)")
    if tone is not None:
        asset["access"] = {"analog_tone_hz": tone}
    if kind == "repeater":
        offset_khz = ask_int("Repeater offset in kHz (for example -600 or 600)", -600)
        asset["offset_hz"] = offset_khz * 1000
        asset["input_frequency"] = frequency + asset["offset_hz"]
        asset["detection"]["kerchunk_max_ms"] = ask_int("Maximum kerchunk duration in ms", 3500)
        asset["location"] = {
            "city": ask("Tower city (optional)") or None,
            "state": ask("Tower state/region (optional)") or None,
            "latitude": ask_float("Tower latitude (optional)"),
            "longitude": ask_float("Tower longitude (optional)"),
        }
    return asset


def build_config() -> dict[str, Any]:
    print("SDR Activity Monitor configuration\n")
    node_id = ask("Node ID", "LOCAL-01", required=True)
    group = "primary"
    center = hz_from_mhz("SDR center frequency in MHz", 146.280)
    sample_rate = ask_int("SDR sample rate in samples/second", 2_400_000)
    count = ask_int("Number of channels to monitor", 3)
    assets = [channel(number, group) for number in range(1, count + 1)]
    low, high = center - sample_rate / 2, center + sample_rate / 2
    outside = [a["name"] for a in assets if not low <= a["frequency"] <= high]
    if outside:
        raise ValueError("channels outside the configured passband: " + ", ".join(outside))
    return {
        "node": {
            "id": node_id,
            "name": ask("Node name", node_id),
            "location": {
                "latitude": ask_float("Receiver latitude (optional)"),
                "longitude": ask_float("Receiver longitude (optional)"),
            },
            "antenna": ask("Antenna description (optional)") or "Not specified",
        },
        "receiver": {
            "driver": "rtl_sdr",
            "device": ask_int("RTL-SDR device index", 0),
            "active_group": group,
            "fft_size": ask_int("FFT size", 16384),
            "gain": ask_float("Tuner gain in dB", 32.8),
            "retry_seconds": 10,
            "rtl_sdr_path": "/usr/bin/rtl_sdr",
        },
        "groups": [{"id": group, "center_frequency": center, "sample_rate": sample_rate, "dwell_seconds": 0}],
        "assets": assets,
        "discord": {"webhook_env": "SDR_MONITOR_DISCORD_WEBHOOK"},
        "interference_rejection": {
            "startup_calibration_seconds": 15,
            "dominance_db": 12,
            "strong_signal_guard_db": 20,
            "overload_recovery_seconds": 2,
            "desense_drop_db": 4,
        },
        "diagnostics": {
            "power_trace_interval_seconds": 1,
            "power_trace_retention_hours": 72,
            "flatline_window_seconds": 30,
            "flatline_min_std_db": 0.005,
        },
        "storage": {"database": "/opt/sdr-activity-monitor/data/activity.db"},
        "health": {"log_interval_seconds": 60},
        "recovery": {"max_restart_attempts": 3, "max_retry_seconds": 60},
        "logging": {"level": "INFO"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.output.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite {args.output}; use --force")
    try:
        config = build_config()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nConfiguration cancelled.")
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
