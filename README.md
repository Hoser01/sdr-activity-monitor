# SDR Activity Monitor

SDR Activity Monitor is an experimental, receive-only activity detector for amateur-radio
repeaters and simplex channels. One RTL-SDR captures a continuous wide slice of spectrum and
the software watches several configured frequencies at once. It records RF events, classifies
short kerchunks separately, can aggregate activity into Discord alerts, and presents live and
historical information in terminal and web dashboards.

This is a working experiment, not calibrated test equipment or a public-safety receiver. It is
being used to learn where wideband FFT detection works, where real RF environments defeat simple
rules, and which evidence makes automated classifications understandable. Thresholds in the
sample configuration are site-specific starting points, not universal recommendations.

## What “FFT-based” means

FFT stands for **Fast Fourier Transform**. An RTL-SDR produces a stream of time-domain complex
I/Q samples. The monitor groups those samples into blocks, applies a window, and uses an FFT to
turn each block into a spectrum: estimated signal energy organized by frequency.

Because every configured channel inside the captured bandwidth appears in the same spectrum,
one dongle can evaluate them simultaneously. For each channel, the monitor measures power in a
narrow frequency region and compares it with that channel’s learned local noise floor. The
important value is the **margin**:

```text
margin (dB) = current channel power − learned channel noise floor
```

When the margin remains above that channel’s trigger and passes duration, stability, and
interference checks, it can become a transmission. These measurements are relative receiver
values, not calibrated dBm. FFT detection identifies RF activity; it does not understand speech
or prove who transmitted.

## How it works

```text
RTL-SDR continuous I/Q stream
          │
          ├── windowed FFT ── channel power, center stability, overload/desense evidence
          │
          └── active-channel IQ ── FM discriminator ── optional CTCSS evidence
                                      │
                               channel state machines
                         candidate / transmitting / kerchunk
                                      │
                                    SQLite
                           ┌──────────┴──────────┐
                    alert aggregation      dashboard API
                           │                     │
                    Discord webhook       MapLibre web UI
```

The monitor maintains an independent baseline, threshold, and state machine per channel. It
uses multi-frame evidence and dropout hysteresis, suppresses correlated broadband changes,
guards other baselines during strong-signal desensitization, and retains completed observations
separately from alert delivery. Only active candidates incur CTCSS processing. SQLite stores the
operational record and supports concurrent dashboards through WAL mode.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the detailed design, storage model, receiver lifecycle,
recovery strategy, diagnostic privacy boundary, and possible future `rtl_power` integration.

## Requirements

- Debian or Ubuntu-style Linux system with systemd
- RTL-SDR-compatible receiver
- Python 3, NumPy, and PyYAML (installed by the installer)
- Frequencies that fit inside one configured RTL-SDR passband
- Root access for packages, system services, USB rules, and optional nginx/Certbot setup
- Optional Discord webhook
- Optional DNS record already pointing to the host for public HTTPS dashboard installation

The current service is designed for Linux servers and Raspberry Pi-class nodes. CPU load depends
on sample rate, FFT size, channel count, and hardware.

## Installation

Clone the repository and run the interactive installer:

```bash
git clone https://github.com/Hoser01/sdr-activity-monitor.git
cd sdr-activity-monitor
sudo ./scripts/install.sh
```

The installer asks for all deployment-specific information it needs:

- whether to install the dashboard and optionally publish it through HTTPS;
- dashboard DNS name and Let’s Encrypt email when public HTTPS is selected;
- optional Discord webhook;
- receiver node ID, name, location, and antenna description;
- RTL-SDR device index, center frequency, sample rate, FFT size, and gain;
- every channel’s type, name, callsign, frequency, bandwidth, trigger, alert policy, CTCSS
  tone, repeater offset, and optional tower coordinates.

It validates that all channels fit inside the selected passband before writing the configuration.
The secret webhook is stored in root-controlled `/etc/default/sdr-activity-monitor`, never in Git,
YAML, SQLite diagnostics, or the dashboard API.

The installer creates:

- `/opt/sdr-activity-monitor` — application and configuration;
- `sdr-activity-monitor.service` — receiver and detector;
- `sdr-dashboard-web.service` — localhost dashboard backend, when selected;
- `sdr-activity-monitor-calibration.timer` — recalibration every three hours at minute `:47`;
- `/etc/modprobe.d/sdr-activity-monitor.conf` — prevents DVB drivers from claiming the dongle;
- optional nginx reverse proxy and Let’s Encrypt certificate.

After installing, replug the receiver or reboot if Linux had already attached its DVB driver,
then start the services:

```bash
sudo systemctl start sdr-activity-monitor
sudo systemctl start sdr-dashboard-web        # when installed
sudo systemctl status sdr-activity-monitor
sudo journalctl -u sdr-activity-monitor -f
```

## Configuration and validation

The installed configuration is `/opt/sdr-activity-monitor/config/config.yaml`. The repository’s
`config/config.yaml` is the experiment’s Joplin-area example and a test fixture. Generate a new
configuration without doing a system installation with:

```bash
python3 scripts/configure.py --output /tmp/sdr-config.yaml
python3 -m app.monitor --config /tmp/sdr-config.yaml --check-config
```

After editing an installed configuration:

```bash
cd /opt/sdr-activity-monitor
python3 -m app.monitor --config config/config.yaml --check-config
sudo systemctl restart sdr-activity-monitor
```

Calibrate only while the monitored spectrum is quiet. A manual calibration lasts 15 seconds and
does not restart the receiver:

```bash
sudo systemctl kill --kill-who=main --signal=SIGHUP sdr-activity-monitor
```

The automatic timer runs at `00:47`, `03:47`, and every three hours thereafter. The odd minute is
an operational attempt to avoid common net starts, IDs, and top-of-hour activity; it is not a
guarantee that the band will be quiet.

## Dashboards and APIs

The web dashboard combines a MapLibre repeater map, live channel matrix, recent events, alert
delivery counts, and receiver diagnostics. Live values include channel power, noise floor,
margin, trigger, delta from trigger, state, and a signal visualization. Timestamps are rendered
in the browser’s local time.

The backend listens on `127.0.0.1:18090` by default:

- `/` — web application
- `/api/dashboard` — structured dashboard snapshot
- `/api/console` — plain-text terminal-style fallback
- `/api/diagnostics` — bounded diagnostic JSON without secrets, IQ, or audio

The terminal dashboard remains available:

```bash
sudo -u sdr-monitor python3 -m app.dashboard
```

## Lessons learned so far

These are the project author’s observations from operating the experiment. They describe this
installation and should be tested again at other sites.

1. **Baselines must remain current.** Stale noise-floor measurements create false activity, and
   conditions change throughout the day.
2. **Fifteen seconds has been sufficient for calibration.** It provides a useful baseline with a
   shorter monitoring pause than the original 45-second window.
3. **Recalibrate regularly and at an intentionally uncommon minute.** The experiment recalibrates
   every three hours at `:47` in an effort to avoid repeater tones, net starts, and top-of-hour use.
4. **Calibrate while quiet.** A real or nearby transmission during sampling can corrupt the learned
   noise floor.
5. **Margin is more useful than raw power.** Detection decisions need the signal’s level relative
   to that channel’s local noise floor.
6. **Thresholds must be tuned per channel.** Repeater outputs and simplex behave differently; even
   repeaters at the same receiver can require different sensitivity.
7. **Kerchunks are distinct observations.** Brief repeater access or tail events should be retained
   without automatically counting as completed voice transmissions or alert activity.
8. **CTCSS evidence reduces false attribution.** A matching tone can associate RF activity with a
   configured repeater, but missing tone evidence is still valid for simplex, carrier squelch,
   digital operation, weak signals, and repeater tails.
9. **Nearby transmitters affect more than one channel.** Front-end overload, desensitization, and
   spectral leakage can create simultaneous changes that look like unrelated activity.
10. **Correlated channel movement is often interference.** Similar broadband rises should not
    generate several independent station alerts.
11. **Persistent, stationary carriers need suppression.** Repetitive non-voice signals should be
    visible diagnostically without repeatedly alerting.
12. **Partial SDR reads are normal.** The capture path must assemble incomplete pipe reads instead
    of assuming each read contains a complete frame.
13. **Capture recovery must be automatic and observable.** Restarts, failures, recoveries, and the
    most recent successful frame need to be exposed.
14. **Event and alert lifecycles are separate.** Candidate, transmitting, kerchunk, completed,
    pending-alert, and delivered-alert states must not be conflated. Discord delivery success must
    update the authoritative alert database.
15. **Diagnostics make tuning explainable.** Power, noise, margin, trigger, delta, state, and
    signal-margin history help distinguish real activity from interference.
16. **Readable output matters operationally.** Fixed-width terminal columns and clearly separated
    delivered/pending counts prevent monitoring mistakes.
17. **Tests must follow configuration changes.** Threshold and behavior changes require matching
    test updates.
18. **Configuration alone is not proof.** Validate changes against live logs, post-calibration
    behavior, dashboard evidence, and what is heard over the air.

## Testing

Tests use synthesized I/Q and temporary databases; no RTL-SDR is required:

```bash
python3 -m unittest discover -s tests -v
```

They cover FFT detection, tone recognition, calibration, overload/desense rejection, stationary
carrier suppression, capture failure classification, partial reads, database behavior, alert
lifecycle behavior, and dashboard/API output.

## Current boundaries

- One `rtl_sdr` process owns one receiver and one continuous passband.
- This is activity detection, not audio recording or speech recognition.
- Raw IQ and demodulated audio are not persisted.
- CTCSS is supporting evidence rather than an absolute requirement.
- `rtl_power` is not currently part of the active pipeline.
- The dashboard is operational tooling and has no built-in user authentication; place access
  controls in front of it when exposure beyond a trusted audience requires them.

Contributions and field reports are welcome, especially reproducible examples that include the
configured trigger, noise floor, margin history, receiver conditions, and what was heard locally.
