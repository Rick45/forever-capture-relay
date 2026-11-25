# Capture Relay

Capture Relay reads an HDMI source in a Debian VM and serves a local preview and calibration dashboard. An optional Windows helper sends the configured keys only to the selected foreground process.

## Local development

There is no build step. On Debian, install the capture tools and Python dependencies, then run from the repository root:

```sh
sudo apt install python3-opencv python3-numpy v4l-utils alsa-utils
v4l2-ctl --list-devices
python3 -B relay.py --device /dev/video0
```

Use the capture card's actual device path, preferably its stable symlink under `/dev/v4l/by-id/`. Open [the local dashboard](http://127.0.0.1:8080/). Without `--host`, the relay runs without Windows input. Calibration is saved as `calibration.json` in the working directory by default.

For an isolated Python environment, install `requirements.txt` with `python -m pip install -r requirements.txt`; the system capture tools are still required. The Windows helper requires Node.js and Windows PowerShell and has no npm dependencies.

`python3 relay.py --help` lists all capture options. Defaults are 1280×720, 30 FPS, MJPG, and dashboard port 8080. Use `--width`, `--height`, `--fps`, `--fourcc`, `--port`, and `--profile` to override them. A calibration profile must match the captured frame dimensions. Enable HDMI audio with `--audio-device` or `CAPTURE_RELAY_AUDIO`.

## Repository layout

| File | Purpose |
| --- | --- |
| `relay.py` | Video capture, dashboard server, detection/action loop, and Windows helper requests |
| `detector.py` | Calibration profiles, casting-bar tracking, and V1 bobber/motion detection |
| `audio.py` | HDMI audio capture through ALSA `arecord` |
| `detector_v2.py`, `vendor/` | V2 interact-icon detection, audio fingerprints, reference assets, and upstream license |
| `dashboard.html` | Dashboard UI, styles, and browser code |
| `host.mjs`, `bridge.ps1` | Signed-command validation and Windows foreground checks/key input |
| `start-input.ps1` | Windows launcher using the account's encrypted credential |
| `capture-relay.service` | Debian systemd service definition |
| `test_relay.py`, `test_host.mjs` | Python and Windows helper checks |
| `replay.py` | V1 visual detection replay for a saved video of one cast |

## Current setup

The following addresses and paths describe the existing installation; use your own addresses for a new deployment.

Proxmox VM 108 is named `capture-relay`. It uses the UGREEN HDMI capture card on Proxmox USB port `6-2`. The guest service is `capture-relay.service`. Runtime files live in `/opt/capture-relay`, calibration lives in `/var/lib/capture-relay`, and the root-only environment file is `/etc/capture-relay/relay.env`.

From a computer on the same LAN, open [Capture Relay](http://192.168.1.214:8080/). Sign in as `relay` with the password in `%LOCALAPPDATA%\CaptureRelay\dashboard-password.txt` on the Windows PC used for setup. This HTTP connection is for the local network.

The previous loopback URL also works over an SSH tunnel. From Windows, open a PowerShell window and keep this tunnel running.

```powershell
ssh -N -o ExitOnForwardFailure=yes -o HostKeyAlgorithms=ssh-ed25519 -L 8080:127.0.0.1:8080 -i "$env:USERPROFILE/.ssh/homeserver_migration_ed25519" ricardo@192.168.1.214
```

On the first connection, verify the guest fingerprint `SHA256:N70kxfD/k6G/G+WsB5U3Gjsyy7wQh755kWP7g+AplKE`.

Open [Capture Relay over SSH](http://127.0.0.1:8080). If the guest address changes, get the current address from the VM's **Summary** page and update `CAPTURE_RELAY_LAN_ADDRESS` in the guest environment file.

## Enable input

Open the target application. In Windows Task Manager, find its numeric PID under **Details**. Use the same Windows account and privilege level as the target application. Run the helper with the PID shown in Task Manager.

```powershell
Set-Location "$env:LOCALAPPDATA\CaptureRelay"
.\start-input.ps1 -BindAddress 192.168.1.160 -VmAddress 192.168.1.214 -TargetPid 1234 -Cast F11 -Reel F11
```

The launcher uses the local account's encrypted credential. The default action keys are `1` and `F`. Set `-Cast` and `-Reel` to a letter, digit, or function key from `F1` to `F12`. You can use the same key for each action. Press Ctrl+C to stop the helper. Restart the helper with the new PID if you restart the target application.

The Windows helper listens on TCP 8765 and accepts signed commands from the specified guest address. If Windows Firewall blocks it, allow that port on the private profile with the remote address scoped to the guest. If the Windows address changes, update `CAPTURE_RELAY_HOST` in the guest environment file, restart the guest service, and use the new bind address. Keep the guest and Windows clocks synchronized.

## Calibrate the preview

1. In Windows Display settings, duplicate the source display to the HDMI output connected to the capture card. Check that the dashboard shows that display.
2. Trigger the first action manually. Once the marker is visible and settled, select **Freeze frame to calibrate**. Draw a box around the expected landing area and a second, tight box around the marker. Save the calibration.
3. Cast by hand and select **Calibrate casting bar** while it is visible. Draw a tight box around the left end, including its border and avoiding text. Save the box. A new bar calibration keeps the saved water and marker boxes.
4. In **Preview only**, perform several runs manually. Check that **Casting bar** switches between **visible** and **absent**, the outline follows the marker while the bar is visible, and the detection count rises once per event. Open **Detection diagnostics** below the preview to see the current detection stage and the five most recent splash candidates, including why they passed or failed. Audio offsets are based on when the relay observed each cue. Tighten the boxes or adjust the match and splash thresholds as needed.
5. Select **Start** and bring the target window to the foreground within four seconds. If the casting bar stays absent, the relay retries after five seconds. If the bar is visible but the marker stays missing for eight seconds, the relay clears the current cast and tries again once the bar disappears. Select **Stop** to end the run.

When HDMI audio is configured, V1 requires a sharp sound and visible motion near the marker within 350 milliseconds. Route the target application's sound to the display audio output feeding the card. The dashboard shows **HDMI audio silent** when the card is delivering silence. Use **Preview only** to confirm a real splash registers once before starting.

## Use the V2 detector

1. Enable the gold gear over the bobber in WoW with `/console SoftTargetIconGameObject 1`. Set the helper's `-Reel` key to the game's **Interact With Target** binding.
2. Select **V2 · Interact icon and splash fingerprint** in the dashboard's **Detector** menu. V2 uses your saved water area and requires HDMI audio. The selector applies to the relay session and can be changed between runs.
3. Cast manually, select **Calibrate interact icon** while the gold gear is visible, and draw a box around all its teeth with a little surrounding water. Leave the bobber and text outside. The box must be 12 to 128 capture pixels per side. Save it at the resolution and UI scale you use for fishing. The casting bar calibration is required for **Start**.
4. In **Preview only**, cast again. The dashboard should highlight the gold icon even when HDMI audio is unavailable or the casting bar is absent. Live HDMI audio is still required to confirm a bite: the dashboard then names the matched splash clip and shows its score. **Start** still requires live audio and casting-bar calibration.

Icon calibration searches beyond the drawn box size to account for the sprite's transparent margins and shadow. It also checks size separation at the selected gear so nearby gold objects do not suppress that check. If calibration fails, the error includes the best size, shape match, gold score, and size separation, with guidance for the failing check. Redraw on the same frozen frame when the box is wrong; cancel and freeze again if the icon is grey or blurry. If the icon remains unclear, increase its on-screen size and recalibrate. The **Icon match** slider affects detection after calibration; lowering it does not bypass a failed calibration.

Use **Detection thresholds** to tune the selected detector after calibration. V2 exposes **Icon match** and **Splash fingerprint** scores from 0 to 1. V1 exposes **Bobber match** and **Splash motion**. **Save thresholds** applies the values to the current session and saves them in the calibration profile.

Use **Reel delay** to set the minimum and maximum in whole milliseconds. The default is 120–300 ms. Set both fields to the same value for a fixed delay. The saved range applies to future bites in either detector version.

Use **Cast delay** to set the random wait before an automated cast or recast. The default is 250–650 ms. This delay starts once the relay is ready to cast, after the usual startup or recast wait. Both delay ranges are saved with the calibration profile.

The **Action log** beside the preview shows the last 60 events from the current relay session, newest first. A confirmed bite lists the detector scores and thresholds, plus the planned delay when a reel is scheduled. The reel entry gives the selected random delay and the actual time from confirmation to the request. The log starts fresh after a service restart.

V2 runs the icon search and audio fingerprint detector from [wow-fishing-forever](https://github.com/rsamf/wow-fishing-forever/tree/7e37aca73e018077087fe8bf34c05fd012babda4) on the relay's HDMI feeds. The bundled source and reference assets are under `vendor/` with the upstream license. Deploy `detector_v2.py` and `vendor/` alongside the other runtime files in `/opt/capture-relay`.

## Verify or reinstall

On Debian 13, install `python3-opencv`, `python3-numpy`, `v4l-utils`, and `alsa-utils`. Check the capture device with `v4l2-ctl --list-devices` and use its video symlink under `/dev/v4l/by-id/`. The service expects the device path in `CAPTURE_RELAY_DEVICE`, the Windows URL in `CAPTURE_RELAY_HOST`, and a shared 64-character hex key in `CAPTURE_RELAY_SECRET`. Set `CAPTURE_RELAY_AUDIO` to the ALSA input name to enable audio. Set `CAPTURE_RELAY_LAN_ADDRESS`, `CAPTURE_RELAY_LAN_NETWORK`, and a separate dashboard password in `CAPTURE_RELAY_DASHBOARD_PASSWORD` to serve the dashboard on the LAN.

The service runs as `capture-relay` with video/audio group access. Install `relay.py`, `detector.py`, `detector_v2.py`, `audio.py`, `dashboard.html`, and the complete `vendor/` directory in `/opt/capture-relay`. Its `StateDirectory` creates `/var/lib/capture-relay`, and its working directory puts the default calibration profile there.

Device and helper environment variables are expanded by the service unit; when running `relay.py` directly, use `--device` and `--host`. Without LAN settings, the dashboard binds to `127.0.0.1`. LAN access requires a private IPv4 network in CIDR notation containing the configured address and allowed clients, plus a dashboard password of at least 16 characters. It listens on all IPv4 interfaces but checks the configured network, dashboard Host header, and credentials. HTTP does not encrypt the dashboard password or preview; use the SSH tunnel when encryption is needed. Keep the environment file root-only and keep secrets and calibration files out of Git.

On Windows, install `start-input.ps1`, `host.mjs`, and `bridge.ps1` together under `%LOCALAPPDATA%\CaptureRelay`. The launcher expects a `.relay-secret` file encrypted by the same Windows account using PowerShell `ConvertFrom-SecureString`, containing the shared key used by Debian. The credential is local to that account and machine. Run `node .\host.mjs --help` for direct-launch options, including a custom helper port.

After updating runtime files, restart and inspect the service:

```sh
sudo systemctl restart capture-relay.service
sudo systemctl status capture-relay.service
sudo journalctl -u capture-relay.service -n 100 --no-pager
```

If the unit file changed, run `sudo systemctl daemon-reload` before restarting.

## Development checks

From the repository root, run the Python tests with the dependencies installed:

```sh
python3 -B -m unittest -v
```

These checks use synthetic frames and fake capture/helper objects, so capture hardware is not required. On Windows, run the helper checks:

```powershell
node .\test_host.mjs
```

They check signature, expiry, replay rejection, and foreground handling. For hardware verification, use **Preview only** to check casting-bar state, marker/icon tracking, audio, and one detection per bite before testing **Start** and **Stop** with the selected target window in front. Confirm saved calibration, thresholds, and delay ranges survive a service restart.

Use `python3 replay.py clip.mp4 --profile calibration.json` to check V1 visual detection on a recording of a single cast at the calibration dimensions. Replay sends no keys and does not test V2 audio fingerprints or the automated action loop.
