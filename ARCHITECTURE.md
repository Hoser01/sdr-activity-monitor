# SDR Activity Monitor Architecture

## Purpose

The SDR Activity Monitor is a receive-only, unattended Linux service for observing known
repeater and simplex frequencies with one RTL-SDR. It favors continuous coverage of a
single wide IQ passband, local evidence retention, and restrained Discord alerting.

## Primary decisions

1. **Continuous IQ is the authoritative input.** One long-running `rtl_sdr` process owns
   the dongle and supplies unsigned 8-bit IQ at 2.4 MS/s. This avoids revisit gaps and is
   better for short transmissions than repeated `rtl_power` sweeps.
2. **All configured channels inside the passband are evaluated together.** A windowed FFT
   yields channel power, interpolated carrier center, overload evidence, and desense guards.
3. **Relative power is used.** Values are not calibrated dBm. Detection uses a separately
   learned noise floor and a per-channel margin above that floor.
4. **Observations and alerts are different concerns.** Completed events are retained even
   when they are kerchunks, weak evidence, or alert-suppressed. Alert policy can therefore
   change without rewriting RF history.
5. **CTCSS uses the existing IQ stream.** In-memory downconversion, decimation, FM
   discrimination, and tone-bank correlation avoid a second process competing for the SDR.
   No PCM, IQ, or audio is persisted by this feature.
6. **SQLite is the operational record.** WAL mode supports concurrent read-only dashboards
   while the monitor writes transmissions, sessions, health, and diagnostic traces.
7. **Recovery is layered.** The monitor makes a bounded number of classified retries with
   exponential backoff. When that budget is exhausted, systemd restarts the process and the
   service's privileged pre-start hook resets the specific RTL-SDR USB device.

## Data flow

```text
RTL-SDR
  │ continuous rtl_sdr IQ
  ▼
IQ block reader ──► stream/flatline validation
  │
  ├──► windowed FFT
  │      ├──► per-channel power and carrier estimate
  │      ├──► broadband-overload selection
  │      └──► front-end desense guard
  │
  └──► active-channel IQ downconverter
         └──► FM discriminator ──► CTCSS tone evidence

Channel state machine
  ├── candidate/dropout hysteresis
  ├── adaptive noise floor
  ├── multi-frame evidence score
  ├── persistent-carrier alert guard
  └── completed transmission/session
                 │
                 ▼
              SQLite
       ┌─────────┴──────────┐
       ▼                    ▼
Discord/Spotwatchbot    read-only dashboard API
                            │
                         nginx/HTTPS
```

## Receiver lifecycle

The persisted receiver state is one of:

- `initializing`: process and database setup
- `starting`: launching the continuous helper
- `ready`: valid IQ frames are arriving
- `device_missing`: no supported RTL-SDR is visible
- `device_busy`: another process owns the interface
- `permission_error`: USB access is denied
- `stream_stalled`: the helper opened but stopped producing usable input
- `capture_failed` or `capture_terminated`: other bounded failures

State transitions are recorded separately from minute health samples. Reliability counters
include capture starts, failures, recoveries, missing/busy/permission failures, stalls, last
error, and last successful frame time.

## Detection and evidence

Each channel has its own trigger margin, minimum transmission duration, dropout merge time,
and noise-floor adaptation rates. An event becomes confirmed only after the configured number
of qualifying FFT frames and acceptable carrier-center stability. Unconfirmed and short events
remain available for diagnosis but do not contribute to activity-alert sessions.

The persistent-carrier guard looks for at least 20 highly repeatable observations over five
minutes, at least 20 seconds of aggregate activity, no more than 3 dB of margin spread, and no
more than 1 kHz of carrier-center variation. It suppresses repetitive alerts for one hour while
retaining observations. A material level or CTCSS change releases the guard.

## CTCSS

Only an active candidate channel incurs tone-processing work. The pipeline mixes that channel
to baseband, decimates to approximately 8 kHz, performs complex FM discrimination, and compares
the bounded in-memory result against the standard CTCSS tone bank. A detected tone requires a
minimum contrast and must match the configured tone when one is specified.

Tone evidence supports identification; its absence does not erase an RF event because repeater
tails, digital operation, weak signals, and receiver conditions can legitimately lack a tone.

## Storage

- `assets` and `nodes`: configured catalog
- `transmissions`: completed RF events plus evidence, CTCSS, and suppression fields
- `activity_sessions`: alert aggregation windows
- `alerts`: pending/delivered Discord records
- `receiver_status`: current lifecycle state
- `receiver_events`: bounded-transition audit source
- `receiver_health`: periodic receiver and reliability snapshots
- `channel_power_samples`: short-retention troubleshooting traces

Schema changes are additive and performed at startup, preserving existing rows.

## Dashboard and diagnostics

The primary dashboard is a responsive MapLibre web application backed by the structured,
read-only `/api/dashboard` snapshot. Repeater assets with coordinates appear as accessible tower
markers. Current transmissions are green; candidates and recently completed kerchunks are amber;
idle sites are slate blue; and a cyan/white focus ring identifies the selected station without
hiding its activity color. Simplex has a separate live control because it has no physical tower.
Station selection exposes access data, current RF measurements, and the ten most recent completed
events. A live matrix below the map shows power, noise, margin, trigger, delta, state, and relative
signal level for every configured channel.

Receiver lifecycle, FFT rate, capture reliability, sample counts, and alert delivery appear in the
same application. The earlier terminal renderer remains available through `/api/console` as a
low-dependency operational fallback. A localhost-only Python HTTP service owns all APIs and nginx
terminates HTTPS at `dashboard.lzarc.com`.

`/api/diagnostics` intentionally excludes environment variables, Discord credentials, raw IQ,
audio, browser data, and full transmission history.

## Optional `rtl_power` integration

`rtl_power` is not part of the active pipeline. If broad discovery is added later, it should be
an explicit **survey adapter**, not a second concurrent owner of the dongle:

1. Operator schedules or manually starts a survey window.
2. The continuous monitor closes `rtl_sdr` cleanly and records `survey_handoff`.
3. A bounded `rtl_power -1` adapter captures and strictly validates one requested span.
4. Survey results are stored in separate `survey_observations` tables and never masquerade as
   continuously monitored channel events.
5. The survey helper exits, the continuous stream restarts, and the coverage gap is recorded.

A second SDR can remove the handoff gap. In that design, a separate survey service owns SDR #2
and publishes validated discovery events to SQLite without changing the primary receiver.

## Security and operations

- The receiver and dashboard run as the unprivileged `sdr-monitor` account.
- systemd filesystem and privilege restrictions remain enabled.
- The dashboard backend binds to `127.0.0.1`; nginx is the only public listener.
- Discord credentials remain in the root-controlled environment file, never SQLite or APIs.
- Calibration is manually triggerable with `SIGHUP` and automatically scheduled every three
  hours at minute 47. Detection is paused during the 15-second calibration interval.

## Influences

The receiver-state vocabulary, bounded retry approach, evidence stability checks, persistent
carrier concept, and diagnostic privacy boundary were adapted from review of Apollo Passive
Receive 2.2.5. The implementations here are native to this continuous-IQ Python architecture;
Apollo's one-shot `rtl_power` capture model was deliberately not copied.
