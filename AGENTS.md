# Repository Guidelines

## Project Structure

This repository is Capture Relay: a Debian HDMI capture and detection service with a browser dashboard and an optional Windows input helper.

- `relay.py` owns video capture, the detection/action loop, the HTTP dashboard, and signed requests to Windows.
- `detector.py` owns calibration profiles, casting-bar tracking, and V1 bobber/motion detection. `audio.py` reads HDMI audio through ALSA.
- `detector_v2.py` integrates the bundled `vendor/wowfish/` icon and audio-fingerprint detectors. Keep `vendor/LICENSE` and the reference assets with that source.
- `dashboard.html` contains the dashboard markup, styles, and browser code; there is no frontend build step.
- `host.mjs` validates input requests and runs `bridge.ps1`, which checks the foreground process and sends keys. `start-input.ps1` loads the Windows account's encrypted credential and launches the helper.
- `capture-relay.service` defines the Debian service. `replay.py` checks V1 visual detection against saved footage. `test_relay.py` and `test_host.mjs` contain the automated checks.

## Build and Local Development

There is no build or packaging script. On Debian, install `python3-opencv`, `python3-numpy`, `v4l-utils`, and `alsa-utils`. For an isolated Python environment, install `requirements.txt`; ALSA capture still requires the system `arecord` executable. The Windows helper requires Node.js and Windows PowerShell.

Run `python3 -B relay.py --device /dev/video0` from the repository root for a loopback preview at `http://127.0.0.1:8080/`. Omit `--host` to run without input. Use `python3 relay.py --help` and `node host.mjs --help` for supported options. Capture and calibration dimensions must match.

The deployed Debian runtime lives in `/opt/capture-relay`, with calibration in `/var/lib/capture-relay` and environment settings in `/etc/capture-relay/relay.env`. Deploy the Python runtime files, `dashboard.html`, and `vendor/` together. The service runs as `capture-relay` with video/audio group access. See `README.md` for the existing installation's addresses and operator workflow; do not assume those addresses apply to a new installation.

Keep credentials, `.relay-secret`, calibration profiles, recordings, virtual environments, and generated files out of commits. Do not print or embed shared secrets or dashboard passwords in documentation or logs.

## Coding Style

Follow the surrounding code: four-space indentation, double-quoted Python and JavaScript strings, snake_case Python names, and camelCase JavaScript names. No formatter or linter is configured. Keep hardware-independent detection in the detector modules and input transport in the helper.

Preserve source-IP restrictions, HMAC verification, command expiry and nonce replay protection, dashboard authentication and request-token checks, and foreground/PID validation before input. Keep keydown and keyup paired. Preview mode must remain usable without the Windows helper, and calibration/threshold/delay changes must retain profile validation and persistence.

## Testing

Run `python3 -B -m unittest -v` from the repository root after changing Python detection, calibration, or loop behavior. It uses synthetic frames and fake capture/helper objects, without requiring capture hardware. Run `node .\test_host.mjs` on Windows after changing the helper or bridge; it checks signatures, expiry, replay rejection, and foreground handling.

For manual detection checks, use **Preview only**, calibrate at the active capture resolution, and verify casting-bar state, marker/icon tracking, bite detection, diagnostics, and action-log entries. V2 requires HDMI audio and interact-icon calibration. For input changes, verify focus loss pauses the run and use **Stop** to end it. Confirm saved thresholds and delay ranges survive a service restart. For dashboard changes, check calibration box selection and both detector versions.

Use `python3 replay.py clip.mp4 --profile calibration.json` for V1 visual replay of a single cast at matching dimensions. Replay sends no keys and does not exercise V2 audio fingerprints or the automated action loop. Documentation-only changes need a diff/whitespace review; hardware checks are only claimed when performed.

## Commits and Pull Requests

Use short, descriptive subjects such as `Fix casting-bar retry timing`. In pull requests, describe the behavior change, list the automated checks performed, and identify any hardware or Windows checks still outstanding. Include screenshots for visible dashboard changes and link relevant issues when available.
