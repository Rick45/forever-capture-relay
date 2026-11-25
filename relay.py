"""Read a Debian capture card and run the local capture dashboard."""

import argparse
import base64
from collections import deque
from dataclasses import replace
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network
from itertools import count
import json
import os
from pathlib import Path
import random
import re
import secrets
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import cv2
import numpy as np

from audio import AudioCapture
from detector import (
    BarTracker, BobberDetector, Calibration, CastingBar, valid_score, validate_delay_range, validate_motion_thresholds,
)
from detector_v2 import WowfishDetector, calibrate_interact_icon


CAPTURE_TIMEOUT = 2.0
BOBBER_TIMEOUT = 8.0
BAR_TIMEOUT = 5.0
CAST_TIMEOUT = 35.0
START_DELAY = 4.0
ACTION_LOG_LIMIT = 60


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class KeyHelper:
    def __init__(self, endpoint, secret):
        url = urlsplit(endpoint)
        if (url.scheme != "http" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("Use the Windows helper URL, for example http://192.168.1.20:8765.")
        IPv4Address(url.hostname)
        if url.port == 0:
            raise ValueError("Use a nonzero port for the Windows helper.")
        self.endpoint = endpoint.rstrip("/")
        self.secret = bytes.fromhex(secret)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def send(self, action):
        path = f"/{action}"
        timestamp = str(time.time_ns() // 1_000_000)
        nonce = secrets.token_hex(16)
        signature = hmac.new(self.secret, f"{path}\n{timestamp}\n{nonce}".encode(), hashlib.sha256).hexdigest()
        request = Request(self.endpoint + path, data=b"", method="POST", headers={
            "X-Capture-Time": timestamp, "X-Capture-Nonce": nonce, "X-Capture-Signature": signature,
        })
        try:
            with self.opener.open(request, timeout=1.5) as response:
                if response.status != 204:
                    raise RuntimeError("Unexpected response from the Windows helper.")
        except HTTPError as error:
            with error:
                detail = error.read(256).decode(errors="replace")
            raise RuntimeError(f"Windows helper refused {action} ({error.code}). {detail}") from error
        except OSError as error:
            raise RuntimeError(f"Could not reach the Windows helper for {action}. {error}") from error


class Capture:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.latest = (0, None, 0.0)
        self.problem = f"Opening {args.device}."
        self.closed = threading.Event()

    def snapshot(self):
        with self.lock:
            return (*self.latest, self.problem)

    def run(self):
        args = self.args
        index = 0
        last = None
        changed_at = 0.0
        while not self.closed.is_set():
            camera = cv2.VideoCapture(args.device, cv2.CAP_V4L2)
            try:
                if camera.isOpened():
                    if args.fourcc:
                        camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*args.fourcc))
                    camera.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
                    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
                    camera.set(cv2.CAP_PROP_FPS, args.fps)
                    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    while not self.closed.is_set():
                        ok, frame = camera.read()
                        if not ok or frame is None:
                            break
                        index += 1
                        if last is None or not np.array_equal(frame, last):
                            changed_at = time.monotonic()
                        last = frame
                        with self.lock:
                            self.latest = (index, frame, changed_at)
                            self.problem = ""
            except cv2.error as error:
                print(f"Capture error: {error}", flush=True)
            finally:
                camera.release()
            last = None
            with self.lock:
                self.latest = (index, None, 0.0)
                self.problem = f"No video from {args.device}. Checking the device again."
            self.closed.wait(1.0)


class CaptureLoop:
    def __init__(self, capture, helper, profile, audio=None):
        self.capture = capture
        self.helper = helper
        self.profile = profile
        self.audio = audio
        self.calibration = Calibration.load(profile)
        self.bar_tracker = None
        self.detector = None
        self.detection_version = "v1"
        self.preview_candidates = deque(maxlen=5)
        self.action_log = deque(maxlen=ACTION_LOG_LIMIT)
        self.action_ids = count(1)
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.stop_requested = threading.Event()
        self.frozen = None
        self.mode = "preview"
        self.message = "Calibrate the water and bobber, then try a manual cast in preview."
        self.phase_started = 0.0
        self.cast_delay_ms = 0
        self.reel_due = 0.0
        self.next_focus = 0.0
        self.seen_frame = 0
        self.detected = 0
        self.phase = "idle"
        self.starts = 0
        self.reels = 0
        self.retries = 0
        self.last_marker_at = 0.0
        self.reset_bar()
        self.reset_detector()
        self.record_action("Relay ready", {"Mode": "Preview only"})

    def record_action(self, action, values):
        self.action_log.appendleft({
            "id": next(self.action_ids), "time": time.time(), "action": action, "values": values,
        })

    def detector_values(self):
        values = {"Detector": self.detection_version.upper()}
        if isinstance(self.detector, WowfishDetector):
            values.update({"Icon threshold": self.detector.threshold, "Splash threshold": self.detector.bites.threshold})
        elif isinstance(self.detector, BobberDetector):
            values.update({"Bobber threshold": self.calibration.match, "Motion floor": self.calibration.splash})
        return values

    def reset_bar(self):
        bar = self.calibration.cast_bar if self.calibration else None
        self.bar_tracker = BarTracker(bar) if bar else None

    def reset_detector(self, reason=None):
        previous = self.detector
        if (reason and isinstance(previous, BobberDetector) and previous.candidate_report and not previous.fired
                and (previous.spike_at is not None or previous.visual_peak is not None)):
            previous.candidate_report["result"] = reason
        if self.calibration is None:
            self.detector = None
        elif self.detection_version == "v2":
            if self.calibration.interact_icon is None:
                self.detector = None
            elif isinstance(previous, WowfishDetector) and previous.calibration is self.calibration:
                previous.reset()
            else:
                self.detector = WowfishDetector(self.calibration, self.audio)
        else:
            self.detector = BobberDetector(self.calibration, require_audio=self.audio is not None,
                                          diagnostics=self.mode == "preview")

    def select_detection(self, version):
        with self.lock:
            if self.mode == "running":
                raise ValueError("Stop the run before changing detection versions.")
            if version == "v2" and self.audio is None:
                raise ValueError("V2 requires a configured HDMI audio input.")
            previous_version, previous_mode = self.detection_version, self.mode
            self.detection_version = version
            self.mode = "preview"
            try:
                self.reset_detector()
            except (ValueError, OSError, cv2.error):
                self.detection_version, self.mode = previous_version, previous_mode
                raise
            self.frozen = None
            self.preview_candidates.clear()
            self.phase = "idle"
            self.message = ("Cast by hand and calibrate the gold interact icon."
                            if version == "v2" and self.calibration and not self.detector
                            else "Preview only. Cast by hand to check detection.")
            self.record_action("Detector selected", self.detector_values())

    def audio_ready(self, now):
        if self.audio is None:
            return False
        sample_time, level, _, _ = self.audio.sample
        return level is not None and now - sample_time < 1.0

    def status(self):
        with self.lock:
            _, frame, changed_at, problem = self.capture.snapshot()
            detector = self.detector
            now = time.monotonic()
            live = frame is not None and now - changed_at < CAPTURE_TIMEOUT
            audio_state = "Off"
            audio_level = None
            if self.audio:
                sample_time, level, _, last_signal = self.audio.sample
                audio_state = "Waiting"
                if now - sample_time < 1:
                    audio_level = level
                    audio_state = "Live" if last_signal is not None and now - last_signal < 4 else "Silent"
            preview = None
            if self.mode == "preview" and isinstance(detector, BobberDetector):
                state = detector.preview_state
                if not live:
                    state = "Waiting for live video"
                elif frame.shape[1::-1] != self.calibration.size:
                    state = "Capture size changed. Recalibrate to continue."
                elif self.bar_tracker and self.bar_tracker.visible is not True:
                    state = "Waiting for the casting bar"
                elif detector.fired:
                    state = "Bite detected"
                preview = {
                    "state": state,
                    "motion_threshold": (round(detector.motion_threshold, 1)
                                         if detector.motion_threshold is not None else None),
                    "candidates": [report.copy() for report in reversed(self.preview_candidates)],
                }
            fingerprint = None
            thresholds = None
            if isinstance(detector, WowfishDetector):
                thresholds = {"match": detector.threshold, "splash": detector.bites.threshold}
                score = detector.bite or detector.bites.best_heard
                fingerprint = {
                    "state": detector.state if self.audio_ready(now) else "Waiting for live HDMI audio",
                    "clip": (detector.bite.clip if detector.bite else score.name) or None,
                    "score": round(score.score, 2) if score.score >= 0 else None,
                    "threshold": detector.bites.threshold,
                }
            elif isinstance(detector, BobberDetector):
                thresholds = {"match": self.calibration.match, "splash": self.calibration.splash}
            return {
                "mode": self.mode, "phase": self.phase, "message": self.message,
                "video": ("Live" if live else problem or "Capture stalled"),
                "ready": live, "helper": self.helper is not None,
                "calibrated": (self.calibration is not None and frame is not None
                               and frame.shape[1::-1] == self.calibration.size),
                "size": list(frame.shape[1::-1]) if frame is not None else None,
                "water": self.calibration.water if self.calibration else None,
                "icon_calibrated": bool(self.calibration and self.calibration.interact_icon),
                "bar": self.bar_tracker.bar.box if self.bar_tracker else None,
                "bar_visible": self.bar_tracker.visible if self.bar_tracker and live else None,
                "audio": audio_state, "audio_level": audio_level, "audio_ready": self.audio_ready(now),
                "match": self.calibration.match if self.calibration else 0.72,
                "splash": self.calibration.splash if self.calibration else 7,
                "thresholds": thresholds,
                "reel_delay": self.calibration.reel_delay if self.calibration else None,
                "cast_delay": self.calibration.cast_delay if self.calibration else None,
                "bobber": detector.box if detector else None,
                "confidence": round(detector.confidence, 2) if detector else None,
                "motion": round(detector.motion, 1) if isinstance(detector, BobberDetector) else None,
                "detections": self.detected, "casts": self.starts, "reels": self.reels, "retries": self.retries,
                "preview": preview, "detection_version": self.detection_version, "fingerprint": fingerprint,
                "action_log": list(self.action_log),
            }

    def freeze(self):
        with self.lock:
            if self.mode == "running":
                raise ValueError("Stop the run before calibrating.")
            _, frame, changed_at, _ = self.capture.snapshot()
            if frame is None or time.monotonic() - changed_at >= CAPTURE_TIMEOUT:
                raise ValueError("A live capture is required to calibrate.")
            self.frozen = (secrets.token_hex(16), frame)
            return self.frozen

    def calibrate(self, data):
        with self.lock:
            if self.mode == "running" or not self.frozen or data.get("frame") != self.frozen[0]:
                raise ValueError("Stop the run and freeze a fresh calibration image.")
            frame = self.frozen[1]
            if "bar" in data or "icon" in data:
                if self.calibration is None or self.calibration.size != frame.shape[1::-1]:
                    raise ValueError("Calibrate the water and bobber at the current resolution first.")
                if "icon" in data:
                    icon = calibrate_interact_icon(frame, self.calibration, data["icon"])
                    calibration = replace(self.calibration, interact_icon=icon)
                    values = {"Area": "Interact icon", "Icon size": f"{icon[0]} px", "Icon threshold": icon[1]}
                else:
                    calibration = replace(self.calibration, cast_bar=CastingBar.from_frame(frame, data["bar"]))
                    values = {"Area": "Casting bar", "Box (x, y, w, h)": calibration.cast_bar.box}
            else:
                calibration = Calibration.from_frame(frame, data.get("water"), data.get("bobber"),
                                                     data.get("match"), data.get("splash"))
                if self.calibration:
                    calibration = replace(calibration, bite_threshold=self.calibration.bite_threshold,
                                          reel_delay=self.calibration.reel_delay, cast_delay=self.calibration.cast_delay)
                    if self.calibration.size == calibration.size:
                        calibration = replace(calibration, cast_bar=self.calibration.cast_bar,
                                              interact_icon=self.calibration.interact_icon)
                values = {"Water box (x, y, w, h)": calibration.water, "Bobber box (x, y, w, h)": data["bobber"],
                          "Bobber threshold": calibration.match, "Motion floor": calibration.splash}
            calibration.save(self.profile)
            self.calibration = calibration
            self.frozen = None
            self.reset_bar()
            self.mode, self.phase = "preview", "idle"
            self.preview_candidates.clear()
            self.reset_detector()
            self.message = "Calibration saved. Try a manual cast in preview."
            self.record_action("Calibration saved", values)

    def update_thresholds(self, data):
        with self.lock:
            if data.get("version") != self.detection_version:
                raise ValueError("The selected detector changed. Check the thresholds and try again.")
            detector = self.detector
            if detector is None:
                raise ValueError("Calibrate the selected detector before adjusting its thresholds.")
            match, splash = data.get("match"), data.get("splash")
            if isinstance(detector, WowfishDetector):
                if not valid_score(match) or not valid_score(splash):
                    raise ValueError("Icon match and splash fingerprint thresholds must be between 0 and 1.")
                size, _ = self.calibration.interact_icon
                calibration = replace(self.calibration, interact_icon=(size, match), bite_threshold=splash)
            else:
                validate_motion_thresholds(match, splash)
                calibration = replace(self.calibration, match=match, splash=splash)
            calibration.save(self.profile)
            self.calibration = calibration
            if isinstance(detector, WowfishDetector):
                detector.update_thresholds(calibration)
            else:
                detector.calibration = calibration
                detector.spike_at = detector.spike_reference = detector.visual_peak = None
                detector.candidate_report = None
                self.preview_candidates.clear()
            self.record_action("Thresholds saved", self.detector_values())

    def update_delay(self, action, data):
        with self.lock:
            if self.calibration is None:
                raise ValueError("Save a calibration profile before changing the delay.")
            minimum, maximum = validate_delay_range(data.get("minimum"), data.get("maximum"))
            calibration = replace(self.calibration, **{f"{action}_delay": (minimum, maximum)})
            calibration.save(self.profile)
            self.calibration = calibration
            if self.detector is not None:
                self.detector.calibration = calibration
            self.record_action(f"{action.capitalize()} delay saved",
                               {"Minimum": f"{minimum} ms", "Maximum": f"{maximum} ms"})

    def control(self, action):
        if action == "stop":
            self.stop_requested.set()
        with self.lock:
            if action == "start":
                if self.mode == "running":
                    raise ValueError("A run is already in progress.")
                if not self.helper or not self.calibration:
                    raise ValueError("Starting requires calibration and a configured Windows helper.")
                if not self.bar_tracker:
                    raise ValueError("Calibrate the casting bar before starting.")
                if self.detection_version == "v2" and not self.calibration.interact_icon:
                    raise ValueError("Calibrate the gold interact icon before starting V2.")
                if self.detection_version == "v2" and not self.audio_ready(time.monotonic()):
                    raise ValueError("V2 requires live HDMI audio.")
                _, frame, changed_at, _ = self.capture.snapshot()
                if frame is None or time.monotonic() - changed_at >= CAPTURE_TIMEOUT:
                    raise ValueError("Starting requires live capture video.")
                if frame.shape[1::-1] != self.calibration.size:
                    raise ValueError("Capture size changed. Recalibrate before starting.")
                self.frozen = None
                self.mode, self.phase = "running", "starting"
                self.phase_started = time.monotonic()
                self.message = "Starting in 4 seconds. Bring the target window to the foreground."
                self.next_focus = 0.0
                self.stop_requested.clear()
                self.record_action("Run started", {**self.detector_values(), "Start delay": f"{START_DELAY:g} s"})
            elif action == "stop":
                values = {}
                if self.phase in ("cast_pending", "reel_pending"):
                    values[f"Pending {self.phase.removesuffix('_pending')}"] = "Cancelled"
                self.mode, self.phase = "paused", "idle"
                self.message = "Stopped. No more keys will be sent."
                self.record_action("Run stopped", values)
            else:
                self.mode, self.phase = "preview", "idle"
                self.preview_candidates.clear()
                self.message = "Preview only. Cast by hand to check detection."
                self.record_action("Preview selected", {"Detector": self.detection_version.upper()})
            self.reset_detector()

    def send_key(self, action):
        if self.closed.is_set() or self.stop_requested.is_set():
            return False
        _, frame, changed_at, problem = self.capture.snapshot()
        if frame is None or time.monotonic() - changed_at >= CAPTURE_TIMEOUT:
            raise RuntimeError(problem or "Capture stalled.")
        if frame.shape[1::-1] != self.calibration.size:
            raise RuntimeError("Capture size changed. Recalibrate before starting.")
        self.helper.send(action)
        return True

    def track_active_cast(self, now, index):
        if self.stop_requested.is_set():
            return False
        if self.phase == "starting":
            if now - self.phase_started < START_DELAY:
                return False
            self.phase = "ready_to_cast"
        if now >= self.next_focus:
            self.helper.send("focus")
            self.next_focus = time.monotonic() + 0.7

        visible = self.bar_tracker.visible
        elapsed = now - self.phase_started
        if self.phase == "reel_pending":
            if visible is False:
                self.record_action("Reel cancelled", {"Cast": self.starts, "Reason": "Casting bar disappeared"})
                self.phase = "ready_to_cast"
                self.reset_detector()
            else:
                if now >= self.reel_due and self.bar_tracker.candidate is True:
                    planned_ms = round((self.reel_due - self.phase_started) * 1000)
                    elapsed_ms = round((time.monotonic() - self.phase_started) * 1000)
                    if self.send_key("reel"):
                        self.reels += 1
                        self.phase, self.phase_started = "clearing_bar", time.monotonic()
                        self.message = "Bite detected. Reel sent, waiting to recast."
                        self.record_action("Reel sent", {"Cast": self.starts, "Reel": self.reels,
                                                         "Random delay": f"{planned_ms} ms",
                                                         "Wait before request": f"{elapsed_ms} ms"})
                return False
        if self.phase == "clearing_bar":
            if elapsed >= BAR_TIMEOUT and visible is True and self.bar_tracker.candidate is True:
                return self.clear_cast("Casting bar is still visible. Retrying the clear key.")
            if elapsed < 1.5 or visible is not False:
                return False
            self.phase = "ready_to_cast"
        if self.phase == "waiting_bar" and visible is not True:
            if elapsed < BAR_TIMEOUT or visible is not False or self.bar_tracker.candidate is not False:
                return False
            self.retries += 1
            self.record_action("Cast retry", {"Cast": self.starts, "Reason": "Casting bar did not appear"})
            self.phase = "ready_to_cast"
        if self.phase == "bite" and visible is False:
            self.phase = "ready_to_cast"
            self.reset_detector()
        if self.phase == "ready_to_cast" and visible is False and self.bar_tracker.candidate is False:
            self.cast_delay_ms = random.randint(*self.calibration.cast_delay)
            self.phase, self.phase_started = "cast_pending", time.monotonic()
            self.message = "Ready to cast. Casting shortly."
            self.record_action("Cast scheduled", {"Cast": self.starts + 1,
                                                  "Random delay": f"{self.cast_delay_ms} ms"})
        if self.phase == "cast_pending":
            if visible is True:
                self.record_action("Cast cancelled", {"Reason": "Casting bar appeared"})
            else:
                wait = time.monotonic() - self.phase_started
                if (visible is False and self.bar_tracker.candidate is False
                        and wait >= self.cast_delay_ms / 1000 and self.send_key("cast")):
                    self.starts += 1
                    self.phase, self.phase_started = "waiting_bar", time.monotonic()
                    self.seen_frame = index
                    self.message = "Cast sent. Waiting for the casting bar."
                    self.record_action("Cast sent", {"Cast": self.starts, "Detector": self.detection_version.upper(),
                                                     "Random delay": f"{self.cast_delay_ms} ms",
                                                     "Wait before request": f"{round(wait * 1000)} ms"})
                return False
        if visible is not True:
            return False
        if self.phase != "bite":
            self.reset_detector()
            self.phase, self.phase_started = "bite", now
            self.last_marker_at = now
            self.message = "Casting bar visible. Looking for the bobber."
        elif elapsed >= CAST_TIMEOUT:
            return self.clear_cast("Cast timed out. Clearing the cast and trying again.")
        elif now - self.last_marker_at >= BOBBER_TIMEOUT:
            return self.clear_cast("Bobber was not found. Clearing the cast and trying again.")
        return True

    def clear_cast(self, message):
        if self.send_key("reel"):
            self.retries += 1
            self.phase, self.phase_started = "clearing_bar", time.monotonic()
            self.reset_detector()
            self.message = message
            self.record_action("Clear key sent", {"Cast": self.starts, "Retry": self.retries, "Reason": message})
        return False

    def tick(self, now, index, frame, changed_at, problem):
        if frame is None or now - changed_at >= CAPTURE_TIMEOUT:
            if self.bar_tracker:
                self.bar_tracker.reset()
            if self.mode == "running":
                raise RuntimeError(problem or "Capture stalled.")
            if self.detector and self.detector.box:
                self.reset_detector("Video stalled")
            return
        if self.calibration and frame.shape[1::-1] != self.calibration.size:
            if self.bar_tracker:
                self.bar_tracker.reset()
            if self.mode == "running":
                raise RuntimeError("Capture size changed. Recalibrate before starting.")
            self.message = "Capture size changed. Recalibrate before starting."
            self.reset_detector("Capture size changed")
            return

        if self.bar_tracker:
            self.bar_tracker.update(frame, index, now)
        if not self.detector or self.mode == "paused":
            return
        v2 = isinstance(self.detector, WowfishDetector)
        if v2 and self.mode == "running" and not self.audio_ready(now):
            raise RuntimeError("V2 paused because HDMI audio stopped arriving.")
        if self.mode == "running":
            if not self.track_active_cast(now, index):
                return
        elif self.bar_tracker and not v2:
            if self.bar_tracker.visible is not True:
                if self.phase != "waiting_bar":
                    self.reset_detector("Casting bar disappeared")
                    self.phase = "waiting_bar"
                    self.message = "Preview only. Waiting for the casting bar."
                return
            if self.phase != "bite":
                self.phase = "bite"
                self.message = "Casting bar visible. Looking for the bobber."
            if self.detector.fired:
                return
        elif self.detector.fired:
            if now - self.phase_started < 1.5:
                return
            self.reset_detector()
            self.message = "Preview only. Ready for another manual cast."
        if self.detector.last_seen is not None and now - self.detector.last_seen >= 2.0:
            self.reset_detector("Bobber tracking lost")
            self.message = "Tracking lost. Looking for the bobber again."

        if index == self.seen_frame:
            return
        self.seen_frame = index
        had_lock = self.detector.box is not None
        if v2:
            bite = self.detector.step(frame, now, listen=self.audio_ready(now))
        else:
            audio_peak = self.audio.recent_peak(now) if self.audio else None
            bite = self.detector.step(frame, now, audio_peak)
            report = self.detector.candidate_report
            if report is not None and (not self.preview_candidates or self.preview_candidates[-1] is not report):
                report["audio_required"] = self.detector.require_audio
                self.preview_candidates.append(report)
        if self.detector.last_seen == now:
            self.last_marker_at = now
        if not had_lock and self.detector.box:
            self.message = (self.detector.state if v2 else "Bobber found. Watching for a splash.")
            label = "Icon score" if v2 else "Bobber score"
            self.record_action("Icon found" if v2 else "Bobber found",
                               {**self.detector_values(), label: round(self.detector.confidence, 3)})
        if bite:
            self.detected += 1
            self.phase_started = time.monotonic()
            values = {**self.detector_values(), "Mode": self.mode.capitalize()}
            if v2:
                values.update({"Icon score": round(self.detector.icon_score, 3), "Clip": self.detector.bite.clip,
                               "Splash score": round(self.detector.bite.score, 3)})
            else:
                motion, threshold = self.detector.confirmed_motion
                values.update({"Motion score": round(motion, 2), "Motion threshold": round(threshold, 2)})
                if self.detector.require_audio:
                    offset = round((audio_peak - self.detector.visual_peak) * 1000)
                    values["HDMI audio offset"] = f"{offset:+d} ms"
            if self.mode == "running":
                delay_ms = random.randint(*self.calibration.reel_delay)
                self.reel_due = self.phase_started + delay_ms / 1000
                self.phase = "reel_pending"
                self.message = "Bite detected. Reeling shortly."
                values.update({"Cast": self.starts, "Planned reel delay": f"{delay_ms} ms"})
            else:
                self.message = "Bite detected in preview."
            self.record_action("Bite confirmed", values)

    def run(self):
        while not self.closed.wait(0.012):
            with self.lock:
                try:
                    self.tick(time.monotonic(), *self.capture.snapshot())
                except (RuntimeError, cv2.error) as error:
                    self.record_action("Run paused", {"Cast": self.starts, "Reason": str(error)})
                    self.mode, self.phase = "paused", "idle"
                    self.message = str(error)
                    print(f"Paused: {error}", flush=True)


class Dashboard(ThreadingHTTPServer):
    def __init__(self, port, loop, lan_address=None, lan_network=None, password=None):
        self.loop = loop
        self.lan_network = lan_network
        self.lan_address = lan_address
        self.authorization = "Basic " + base64.b64encode(f"relay:{password}".encode()).decode() if lan_address else None
        self.token = secrets.token_hex(32)
        self.page = Path(__file__).with_name("dashboard.html").read_bytes().replace(b"__CSRF_TOKEN__", self.token.encode())
        super().__init__(("0.0.0.0" if lan_address else "127.0.0.1", port), DashboardRequest)


class DashboardRequest(BaseHTTPRequestHandler):
    def reply(self, code, content, content_type="application/json", frame_id=None):
        if isinstance(content, dict):
            content = json.dumps(content).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if code == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="Capture Relay"')
        if frame_id:
            self.send_header("X-Frame-Id", frame_id)
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass

    def allowed(self):
        peer = IPv4Address(self.client_address[0])
        host = self.headers.get("Host")
        port = self.server.server_port
        if peer.is_loopback and host == f"127.0.0.1:{port}":
            return True
        if (self.server.lan_network is None or peer not in self.server.lan_network
                or host != f"{self.server.lan_address}:{port}"):
            self.reply(403, {"error": "Open the dashboard from the configured network address."})
            return False
        actual = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(actual, self.server.authorization.encode()):
            self.reply(401, {"error": "Sign in to open the dashboard."})
            return False
        return True

    def image(self, frame, frame_id=None):
        if frame is None:
            return self.reply(503, {"error": "No frame is available."})
        ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            return self.reply(500, {"error": "Could not encode the capture frame."})
        self.reply(200, jpeg.tobytes(), "image/jpeg", frame_id)

    def do_GET(self):
        if not self.allowed():
            return
        loop = self.server.loop
        if self.path == "/":
            self.reply(200, self.server.page, "text/html; charset=utf-8")
        elif self.path == "/status":
            self.reply(200, loop.status())
        elif self.path == "/frame":
            _, frame, _, _ = loop.capture.snapshot()
            self.image(frame)
        else:
            self.reply(404, {"error": "Page not found."})

    def do_POST(self):
        if not self.allowed():
            return
        if not hmac.compare_digest(self.headers.get("X-Dashboard-Token", "").encode(), self.server.token.encode()):
            return self.reply(403, {"error": "Reload the dashboard before issuing commands."})
        loop = self.server.loop
        try:
            if self.path == "/freeze":
                frame_id, frame = loop.freeze()
                return self.image(frame, frame_id)
            if self.path in ("/calibrate", "/thresholds", "/reel-delay", "/cast-delay"):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Expected calibration settings.")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected calibration settings.")
                if self.path == "/calibrate":
                    loop.calibrate(data)
                elif self.path == "/thresholds":
                    loop.update_thresholds(data)
                else:
                    loop.update_delay(self.path.removeprefix("/").removesuffix("-delay"), data)
            elif self.path in ("/start", "/stop", "/preview"):
                loop.control(self.path[1:])
            elif self.path in ("/detection/v1", "/detection/v2"):
                loop.select_detection(self.path.rsplit("/", 1)[1])
            else:
                return self.reply(404, {"error": "Unknown dashboard action."})
        except (ValueError, OSError, cv2.error) as error:
            return self.reply(400, {"error": str(error)})
        self.reply(200, loop.status())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/video0")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--fourcc", default="MJPG", help="Capture pixel format, or empty for driver default")
    parser.add_argument("--port", type=int, default=8080, help="Dashboard port")
    parser.add_argument("--profile", type=Path, default=Path("calibration.json"))
    parser.add_argument("--host", help="Windows input helper URL. Omit for preview only.")
    parser.add_argument("--audio-device", default=os.environ.get("CAPTURE_RELAY_AUDIO", ""),
                        help="ALSA input device for optional audio-assisted detection")
    args = parser.parse_args()
    if min(args.width, args.height, args.fps) <= 0 or len(args.fourcc) not in (0, 4) or not 1 <= args.port <= 65535:
        parser.error("Set positive capture dimensions and FPS, a valid port, and a four-character pixel format.")
    secret = os.environ.get("CAPTURE_RELAY_SECRET", "")
    if args.host and not re.fullmatch(r"[0-9a-fA-F]{64}", secret):
        parser.error("Set CAPTURE_RELAY_SECRET to the same 64-character hex secret used on Windows.")
    capture = Capture(args)
    audio = AudioCapture(args.audio_device) if args.audio_device else None
    try:
        lan_address = IPv4Address(os.environ["CAPTURE_RELAY_LAN_ADDRESS"]) if os.environ.get("CAPTURE_RELAY_LAN_ADDRESS") else None
        lan_network = None
        password = os.environ.get("CAPTURE_RELAY_DASHBOARD_PASSWORD", "")
        if lan_address:
            lan_network = IPv4Network(os.environ.get("CAPTURE_RELAY_LAN_NETWORK", ""))
            if not lan_network.is_private or lan_address not in lan_network or len(password) < 16:
                raise ValueError("LAN access requires a private network containing the LAN address and a dashboard password.")
        helper = KeyHelper(args.host, secret) if args.host else None
        loop = CaptureLoop(capture, helper, args.profile, audio)
        server = Dashboard(args.port, loop, lan_address, lan_network, password)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    threading.Thread(target=capture.run, daemon=True).start()
    if audio:
        threading.Thread(target=audio.run, daemon=True).start()
    worker = threading.Thread(target=loop.run, daemon=True)
    worker.start()
    print(f"Dashboard at http://{lan_address or '127.0.0.1'}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.closed.set()
        capture.closed.set()
        if audio:
            audio.closed.set()
        server.server_close()
        worker.join(timeout=2)


if __name__ == "__main__":
    main()
