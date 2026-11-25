from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from audio import AudioCapture, PCM_RATE
from relay import CaptureLoop
from detector import BobberDetector, Calibration, CastingBar
from detector_v2 import calibrate_interact_icon, detection_assets
from wowfish.audio.clips import load_reference_clips
from wowfish.vision.calibrate import Calibration as IconScale
from wowfish.vision.matcher import IconMatcher
from wowfish.vision.template import template_for_size


class FakeCapture:
    snapshot_data = (0, None, 0.0, "")

    def snapshot(self):
        return self.snapshot_data


class FakeHelper:
    def __init__(self):
        self.sent = []
        self.refuse = False

    def send(self, action):
        if self.refuse:
            raise RuntimeError("The target window is not in front.")
        self.sent.append(action)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "calibration.json"
        self.still = np.full((150, 250, 3), 60, dtype=np.uint8)
        cv2.ellipse(self.still, (120, 66), (5, 4), 0, 0, 360, (22, 40, 215), -1)
        cv2.line(self.still, (120, 59), (120, 66), (205, 225, 240), 1)
        self.inactive = self.still.copy()
        cv2.rectangle(self.still, (152, 134), (232, 142), (20, 20, 20), -1)
        cv2.rectangle(self.still, (154, 136), (230, 140), (100, 190, 230), -1)
        self.template = self.still[57:73, 113:127].copy()
        self.splash = self.still.copy()
        cv2.circle(self.splash, (120, 65), 16, (255, 255, 255), -1)
        self.calibration = Calibration.from_frame(self.still, [20, 20, 200, 110], [113, 57, 14, 16], 0.72, 7)
        self.calibration = replace(self.calibration, cast_bar=CastingBar.from_frame(self.still, [152, 134, 25, 9]),
                                   cast_delay=(0, 0))
        self.calibration.save(self.path)
        self.capture = FakeCapture()
        self.helper = FakeHelper()
        self.loop = CaptureLoop(self.capture, self.helper, self.path)
        self.clock = 20.0
        timer = patch("relay.time.monotonic", side_effect=lambda: self.clock)
        timer.start()
        self.addCleanup(timer.stop)
        self.capture.snapshot_data = (0, self.still, self.clock, "")

    def feed(self, frame, delay=0.05):
        self.clock += delay
        self.capture.snapshot_data = (self.capture.snapshot_data[0] + 1, frame, self.clock, "")
        self.loop.tick(self.clock, *self.capture.snapshot())

    def bite(self):
        for index in range(20):
            self.feed(cv2.add(self.still, index % 3))
        self.feed(self.splash)
        self.feed(self.splash)
        self.feed(self.still)

    def clear_bar(self, delay=0.2):
        for _ in range(3):
            self.feed(self.inactive, delay)

    def test_profile_round_trip(self):
        profile = Calibration.load(self.path)
        self.assertEqual(profile.water, (20, 20, 200, 110))
        self.assertTrue(np.array_equal(profile.template, self.template))
        self.assertTrue(profile.cast_bar.matches(self.still))
        self.assertFalse(profile.cast_bar.matches(self.inactive))
        self.assertEqual(json.loads(self.path.read_text())["version"], 1)

    def test_wide_bar_calibration_matches_when_fill_shrinks(self):
        bar = CastingBar.from_frame(self.still, [152, 134, 80, 9])
        later = self.still.copy()
        later[136:141, 177:231] = 20
        self.assertTrue(bar.matches(later))
        self.assertFalse(bar.matches(self.inactive))

    def test_preview_ripples_and_bite_never_send_keys(self):
        self.clear_bar()
        self.assertIsNone(self.loop.detector.box)
        self.bite()
        self.assertEqual(self.loop.detected, 1)
        self.assertEqual(self.helper.sent, [])
        self.assertTrue(self.loop.detector.fired)
        self.feed(self.splash)
        self.feed(self.still)
        self.assertEqual(self.loop.detected, 1)

    def test_preview_reports_audio_timeout_and_matching_onset(self):
        peak = None
        audio = SimpleNamespace(sample=(0.0, None, None, None), recent_peak=lambda now: peak)
        self.loop = CaptureLoop(self.capture, self.helper, self.path, audio)
        self.clear_bar()
        self.assertEqual(self.loop.status()["preview"]["state"], "Waiting for the casting bar")
        self.bite()
        pending = self.loop.status()["preview"]["candidates"][0]
        self.assertEqual(pending["result"], "Waiting for HDMI audio")
        self.assertGreaterEqual(pending["motion"], pending["threshold"])
        self.feed(self.still, 0.36)
        rejected = self.loop.status()["preview"]["candidates"][0]
        self.assertEqual(rejected["result"], "Audio confirmation timed out")
        self.assertIsNone(rejected["audio_offset"])
        self.assertEqual(pending["result"], "Waiting for HDMI audio")

        self.loop.control("preview")
        for _ in range(20):
            self.feed(self.still)
        self.feed(self.splash)
        peak = self.clock - 0.02
        self.feed(self.splash)
        accepted = self.loop.status()["preview"]["candidates"][0]
        self.assertEqual(accepted["result"], "Bite detected")
        self.assertEqual(accepted["audio_offset"], -20)
        self.assertEqual(self.helper.sent, [])
        self.loop.control("start")
        self.assertIsNone(self.loop.status()["preview"])

    def test_full_cast_reel_recast_and_stop(self):
        self.loop.control("start")
        self.clear_bar()
        self.assertEqual(self.helper.sent, [])
        self.feed(self.inactive, 3.41)
        with patch("relay.random.randint", return_value=200) as delay:
            self.bite()
        delay.assert_called_once_with(120, 300)
        self.assertEqual(self.loop.phase, "reel_pending")
        self.assertEqual(self.loop.reels, 0)
        bite = self.loop.status()["action_log"][0]
        self.assertEqual(bite["action"], "Bite confirmed")
        self.assertEqual(bite["values"]["Planned reel delay"], "200 ms")
        self.assertGreaterEqual(bite["values"]["Motion score"], bite["values"]["Motion threshold"])
        self.loop.update_delay("reel", {"minimum": 400, "maximum": 400})
        self.assertEqual(self.loop.status()["reel_delay"], (400, 400))
        self.feed(self.still, self.loop.reel_due - self.clock - 0.01)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["cast"])
        self.feed(self.still, 0.02)
        self.assertEqual(self.loop.reels, 1)
        self.assertEqual(self.loop.status()["action_log"][0]["values"],
                         {"Cast": 1, "Reel": 1, "Random delay": "200 ms", "Wait before request": "210 ms"})
        self.clear_bar(delay=0.6)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["cast", "reel", "cast"])
        self.loop.control("stop")
        self.feed(self.still, 10)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["cast", "reel", "cast"])

    def test_stop_cancels_a_pending_reel(self):
        self.loop.control("start")
        self.clear_bar()
        self.feed(self.inactive, 3.41)
        self.bite()
        self.assertEqual(self.loop.phase, "reel_pending")
        self.loop.control("stop")
        self.feed(self.still, 1.0)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["cast"])
        self.assertEqual(self.loop.status()["action_log"][0]["values"]["Pending reel"], "Cancelled")

    def test_reel_delay_range_persists_and_controls_the_next_bite(self):
        old_profile = json.loads(self.path.read_text())
        del old_profile["reel_delay"]
        self.path.write_text(json.dumps(old_profile))
        self.loop = CaptureLoop(self.capture, self.helper, self.path)
        self.assertEqual(self.loop.status()["reel_delay"], (120, 300))
        self.loop.update_delay("reel", {"minimum": 25, "maximum": 55})
        with self.assertRaisesRegex(ValueError, "whole milliseconds"):
            self.loop.update_delay("reel", {"minimum": 56, "maximum": 55})
        self.assertEqual(Calibration.load(self.path).reel_delay, (25, 55))
        self.assertEqual(self.loop.status()["action_log"][0]["values"], {"Minimum": "25 ms", "Maximum": "55 ms"})
        self.loop.control("start")
        self.clear_bar()
        self.feed(self.inactive, 3.41)
        with patch("relay.random.randint", return_value=40) as delay:
            self.bite()
        delay.assert_called_once_with(25, 55)
        self.assertEqual(self.loop.status()["action_log"][0]["values"]["Random delay"], "40 ms")

    def test_cast_delay_range_persists_and_delays_the_request(self):
        old_profile = json.loads(self.path.read_text())
        del old_profile["cast_delay"]
        self.path.write_text(json.dumps(old_profile))
        self.loop = CaptureLoop(self.capture, self.helper, self.path)
        self.assertEqual(self.loop.status()["cast_delay"], (250, 650))
        self.loop.update_delay("cast", {"minimum": 200, "maximum": 400})
        self.assertEqual(Calibration.load(self.path).cast_delay, (200, 400))
        self.assertEqual(Calibration.load(self.path).reel_delay, (120, 300))
        self.loop.control("start")
        self.clear_bar()
        with patch("relay.random.randint", return_value=320) as delay:
            self.feed(self.inactive, 3.41)
        delay.assert_called_once_with(200, 400)
        self.assertEqual(self.loop.phase, "cast_pending")
        self.feed(self.inactive, 0.31)
        self.assertNotIn("cast", self.helper.sent)
        self.feed(self.inactive, 0.02)
        self.assertEqual(self.helper.sent.count("cast"), 1)
        cast = self.loop.status()["action_log"][0]
        self.assertEqual(cast["action"], "Cast sent")
        self.assertEqual(cast["values"]["Random delay"], "320 ms")
        self.assertEqual(cast["values"]["Wait before request"], "330 ms")

    def test_cast_delay_yields_to_a_manual_cast_or_stop(self):
        for cancel in ("manual", "stop"):
            with self.subTest(cancel=cancel):
                self.loop = CaptureLoop(self.capture, self.helper, self.path)
                self.loop.update_delay("cast", {"minimum": 500, "maximum": 500})
                self.loop.control("start")
                self.clear_bar()
                self.feed(self.inactive, 3.41)
                self.assertEqual(self.loop.phase, "cast_pending")
                if cancel == "manual":
                    for _ in range(3):
                        self.feed(self.still, 0.1)
                    self.assertEqual(self.loop.phase, "bite")
                else:
                    self.loop.control("stop")
                    self.assertEqual(self.loop.status()["action_log"][0]["values"]["Pending cast"], "Cancelled")
                self.feed(self.still, 0.4)
                self.assertNotIn("cast", self.helper.sent)

    def test_v2_calibrates_and_uses_bundled_splash_with_hdmi_audio(self):
        audio = AudioCapture("test")
        self.loop = CaptureLoop(self.capture, self.helper, self.path, audio)
        self.loop.select_detection("v2")
        with self.assertRaisesRegex(ValueError, "Calibrate the gold interact icon"):
            self.loop.control("start")

        templates, _ = detection_assets()
        gear = templates[0]
        icon_box = [104, 25, gear.size, gear.size]
        gear_frame = self.still.copy()
        patch = gear_frame[25:25 + gear.size, 104:104 + gear.size]
        patch[gear.mask > 0] = gear.bgr[gear.mask > 0]
        self.feed(gear_frame)
        frame_id, _ = self.loop.freeze()
        self.loop.calibrate({"frame": frame_id, "icon": icon_box})
        self.assertEqual(Calibration.load(self.path).interact_icon[0], gear.size)

        def feed_pcm(samples, frame):
            chunk_samples = PCM_RATE // 50
            for start in range(0, len(samples), chunk_samples):
                mono = samples[start:start + chunk_samples]
                mono = np.pad(mono, (0, chunk_samples - len(mono)))
                stereo = np.column_stack((mono, mono))
                pcm = (stereo.clip(-1, 1) * 32767).astype("<i2").tobytes()
                audio.read_samples(BytesIO(pcm), len(pcm))
                self.feed(frame, 0.02)

        clip = load_reference_clips(PCM_RATE)["fishbite"]
        feed_pcm(clip, self.still)
        self.assertEqual(self.loop.detected, 0)
        feed_pcm(np.zeros(PCM_RATE // 10, dtype=np.float32), gear_frame)
        self.assertIsNotNone(self.loop.detector.box)
        self.assertEqual(self.loop.detected, 0)
        feed_pcm(clip, gear_frame)
        self.assertEqual(self.loop.detected, 1)
        self.assertEqual(self.loop.status()["fingerprint"]["clip"], "fishbite")
        bite = self.loop.status()["action_log"][0]
        self.assertEqual(bite["action"], "Bite confirmed")
        self.assertEqual(bite["values"]["Clip"], "fishbite")
        self.assertGreaterEqual(bite["values"]["Splash score"], bite["values"]["Splash threshold"])
        self.assertNotIn("Planned reel delay", bite["values"])
        self.assertEqual(self.helper.sent, [])

        self.loop.control("start")
        with self.assertRaisesRegex(ValueError, "Stop the run"):
            self.loop.select_detection("v1")
        self.clock += 4.1
        feed_pcm(np.zeros(PCM_RATE // 2, dtype=np.float32), self.inactive)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["cast"])
        feed_pcm(np.zeros(PCM_RATE // 4, dtype=np.float32), gear_frame)
        feed_pcm(clip, gear_frame)
        self.feed(gear_frame, 0.31)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["cast", "reel"])
        with self.assertRaisesRegex(RuntimeError, "HDMI audio stopped"):
            self.feed(gear_frame, 1.1)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["cast", "reel"])
        self.loop.control("stop")
        self.loop.select_detection("v1")
        self.assertEqual(self.loop.status()["detection_version"], "v1")
        self.assertIsNotNone(self.loop.status()["preview"])

    def test_v2_preview_tracks_icon_without_casting_bar_or_live_audio(self):
        templates, _ = detection_assets()
        gear = templates[0]
        frame = self.inactive.copy()
        sample = frame[25:25 + gear.size, 104:104 + gear.size]
        sample[gear.mask > 0] = gear.bgr[gear.mask > 0]
        replace(self.calibration, interact_icon=(gear.size, 0.8)).save(self.path)
        audio = AudioCapture("test")
        self.loop = CaptureLoop(self.capture, self.helper, self.path, audio)
        self.loop.select_detection("v2")
        self.loop.control("preview")

        # Missing audio and an absent bar must not hide the visual match.
        self.feed(frame)
        self.assertFalse(self.loop.status()["audio_ready"])
        self.assertIsNotNone(self.loop.status()["bobber"])
        self.assertIn("Waiting for live HDMI audio", self.loop.detector.state)
        self.assertEqual(self.loop.detected, 0)
        self.assertEqual(self.helper.sent, [])
        with self.assertRaisesRegex(ValueError, "live HDMI audio"):
            self.loop.control("start")

        # Restoring audio still permits tracking with the bar absent.
        audio.sample = (self.clock, -20, None, self.clock)
        self.feed(frame, 0.3)
        self.feed(frame, 0.1)
        self.assertTrue(self.loop.status()["audio_ready"])
        self.assertIs(self.loop.status()["bar_visible"], False)
        self.assertIsNotNone(self.loop.status()["bobber"])
        self.assertIn("Listening for a splash", self.loop.detector.state)

        # Losing audio discards queued samples, keeping preview visual only.
        cursor = audio.pcm_cursor()
        with patch.object(audio, "pcm_after", side_effect=AssertionError("read stale audio")):
            self.feed(frame, 1.1)
        self.assertEqual(self.loop.detector.audio_cursor, cursor)
        self.assertIsNotNone(self.loop.status()["bobber"])
        self.assertEqual(self.loop.detected, 0)
        self.feed(self.inactive, 0.8)
        self.assertIsNone(self.loop.status()["bobber"])

    def test_v2_accepts_small_hdmi_gear_with_a_strong_template_match(self):
        box = [111, 55, 18, 18]
        scale = IconScale(16, 29, 0.831, 26, 26, gold=0.299, prominence=0.172)
        self.assertFalse(scale.valid)
        with patch("detector_v2.calibrate_icon_scale", return_value=scale):
            self.assertEqual(calibrate_interact_icon(self.still, self.calibration, box), (16, 0.73))
        with patch("detector_v2.calibrate_icon_scale", return_value=replace(scale, prominence=0.08)), \
                patch("detector_v2.IconMatcher") as matcher:
            matcher.return_value.match.return_value = SimpleNamespace(score=0.78)
            with self.assertRaisesRegex(ValueError, "Could not match the gold gear"):
                calibrate_interact_icon(self.still, self.calibration, box)
        with patch("detector_v2.calibrate_icon_scale", return_value=replace(scale, x=4)):
            with self.assertRaisesRegex(ValueError, "Could not match the gold gear"):
                calibrate_interact_icon(self.still, self.calibration, box)

    def test_v2_calibrates_compressed_gears_selected_by_visible_teeth(self):
        templates, _ = detection_assets()
        for size in (12, 16, 20, 24, 29, 42):
            with self.subTest(size=size):
                gear = template_for_size(templates, size)
                frame = np.full_like(self.still, (95, 75, 45))
                sample = frame[40:40+size, 100:100+size]
                sample[gear.mask > 0] = gear.bgr[gear.mask > 0]
                ys, xs = np.where(gear.mask > 0)
                box = [100 + int(xs.min()), 40 + int(ys.min()),
                       max(12, int(xs.max()-xs.min()+1)), max(12, int(ys.max()-ys.min()+1))]
                _, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                captured = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                detected_size, threshold = calibrate_interact_icon(captured, self.calibration, box)
                self.assertEqual(detected_size, size)
                # The saved size and threshold must also work in the runtime matcher.
                match = IconMatcher(template_for_size(templates, detected_size)).match(captured)
                self.assertTrue(match.accepted(threshold))
                self.assertLessEqual(abs(match.x - (100 + size // 2)), 1)

    def test_v2_checks_size_separation_at_the_selected_gear(self):
        box = [111, 55, 18, 18]
        scale = IconScale(20, 29, 0.85, 26, 26, gold=0.299, prominence=0.05)
        with patch("detector_v2.calibrate_icon_scale", return_value=scale), \
                patch("detector_v2.IconMatcher") as matcher:
            matcher.return_value.match.return_value = SimpleNamespace(score=0.65)
            self.assertEqual(calibrate_interact_icon(self.still, self.calibration, box), (20, 0.75))
            matcher.return_value.match.return_value = SimpleNamespace(score=0.80)
            with self.assertRaisesRegex(ValueError, "size is ambiguous.*match 0.85"):
                calibrate_interact_icon(self.still, self.calibration, box)

    def test_v2_rejects_water_grey_icons_and_gold_blobs(self):
        templates, _ = detection_assets()
        gear = template_for_size(templates, 24)
        for kind in ("water", "grey", "blob", "ring"):
            with self.subTest(kind=kind):
                frame = np.full_like(self.still, (95, 75, 45))
                if kind == "grey":
                    grey = cv2.cvtColor(gear.bgr, cv2.COLOR_BGR2GRAY)
                    sample = frame[40:64, 100:124]
                    sample[gear.mask > 0] = np.repeat(grey[:, :, None], 3, axis=2)[gear.mask > 0]
                elif kind in ("blob", "ring"):
                    cv2.circle(frame, (112, 52), 10, (40, 160, 220), -1 if kind == "blob" else 3)
                with self.assertRaisesRegex(ValueError, "Could not match the gold gear.*Best size"):
                    calibrate_interact_icon(frame, self.calibration, [100, 40, 24, 24])

    def test_thresholds_apply_to_the_selected_detector_and_persist(self):
        replace(self.calibration, interact_icon=(16, 0.73)).save(self.path)
        old_profile = json.loads(self.path.read_text())
        del old_profile["bite_threshold"]
        self.path.write_text(json.dumps(old_profile))
        audio = AudioCapture("test")
        audio.sample = (self.clock, -20, None, self.clock)
        self.loop = CaptureLoop(self.capture, self.helper, self.path, audio)
        self.loop.update_thresholds({"version": "v1", "match": 0.77, "splash": 8})
        self.assertEqual(self.loop.status()["thresholds"], {"match": 0.77, "splash": 8})

        self.loop.select_detection("v2")
        self.assertEqual(self.loop.status()["thresholds"], {"match": 0.73, "splash": 0.66})
        self.loop.control("start")
        detector = self.loop.detector
        self.loop.update_thresholds({"version": "v2", "match": 0.71, "splash": 0.62})
        self.assertEqual(self.loop.mode, "running")
        self.assertIs(self.loop.detector, detector)
        self.assertEqual(detector.search.threshold, 0.71)
        self.assertEqual(detector.bites.threshold, 0.62)
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            self.loop.update_thresholds({"version": "v2", "match": 0.8, "splash": 1.1})
        with self.assertRaisesRegex(ValueError, "selected detector changed"):
            self.loop.update_thresholds({"version": "v1", "match": 0.8, "splash": 8})
        profile = Calibration.load(self.path)
        self.assertEqual((profile.match, profile.splash), (0.77, 8))
        self.assertEqual((profile.interact_icon, profile.bite_threshold), ((16, 0.71), 0.62))
        self.assertEqual(self.helper.sent, [])

    def test_stalled_or_lost_video_blocks_keys(self):
        self.loop.control("start")
        self.clock += 5
        with self.assertRaisesRegex(RuntimeError, "Capture stalled"):
            self.loop.tick(self.clock, *self.capture.snapshot())
        with self.assertRaisesRegex(RuntimeError, "Unplugged"):
            self.loop.tick(self.clock, 1, None, 0, "Unplugged")
        self.assertEqual(self.helper.sent, [])

    def test_refused_focus_blocks_cast(self):
        self.loop.control("start")
        self.helper.refuse = True
        with self.assertRaisesRegex(RuntimeError, "The target window is not in front"):
            self.feed(self.still, 4.1)
        self.assertEqual(self.helper.sent, [])

    def test_cast_retries_with_delay_until_bar_appears(self):
        self.loop.control("start")
        self.clear_bar()
        self.feed(self.inactive, 3.6)
        self.assertIsNone(self.loop.detector.box)
        self.feed(self.inactive, 4.5)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["cast"])
        self.feed(self.inactive, 0.6)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["cast", "cast"])
        for _ in range(5):
            self.feed(self.still, 0.1)
        self.assertIsNotNone(self.loop.detector.box)
        self.assertEqual(self.loop.retries, 1)

    def test_missing_bobber_clears_cast_then_waits_for_bar_to_disappear(self):
        self.loop.control("start")
        no_bobber = self.still.copy()
        no_bobber[55:76, 110:130] = 60
        for _ in range(19):
            self.feed(no_bobber, 0.6)
        self.assertIsNone(self.loop.detector.box)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], [])
        self.feed(no_bobber, 1.0)
        self.assertIn("Bobber was not found", self.loop.message)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["reel"])
        self.assertEqual(self.loop.reels, 0)
        self.feed(self.inactive, 0.05)
        self.feed(no_bobber, 0.05)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["reel"])
        self.clear_bar(delay=0.6)
        self.assertEqual([key for key in self.helper.sent if key != "focus"], ["reel", "cast"])

    def test_existing_profile_allows_preview_and_requires_bar_calibration_for_input(self):
        replace(self.calibration, cast_bar=None).save(self.path)
        self.loop = CaptureLoop(self.capture, self.helper, self.path)
        with self.assertRaisesRegex(ValueError, "Calibrate the casting bar"):
            self.loop.control("start")
        self.bite()
        self.assertEqual(self.loop.detected, 1)

    def test_audio_and_visual_motion_must_coincide(self):
        for peak, expected in [(None, False), (0.5, False), (0.90, True)]:
            detector = BobberDetector(self.calibration, require_audio=True)
            for index in range(30):
                self.assertFalse(detector.step(self.still, index / 30, peak))
            self.assertFalse(detector.step(self.splash, 1.0, peak))
            self.assertEqual(detector.step(self.splash, 1.05, peak), expected)

        detector = BobberDetector(self.calibration, require_audio=True)
        for index in range(30):
            self.assertFalse(detector.step(self.still, index / 30))
        self.assertFalse(detector.step(self.splash, 1.0))
        self.assertFalse(detector.step(self.splash, 1.05))
        self.assertTrue(detector.step(self.splash, 1.18, 1.17))

    def test_camera_motion_is_not_a_local_splash(self):
        detector = BobberDetector(self.calibration)
        for index in range(30):
            self.assertFalse(detector.step(self.still, index / 30))
        self.assertIsNotNone(detector.box)
        self.assertFalse(detector.step(cv2.add(self.still, np.full_like(self.still, 60)), 1.1))
        self.assertFalse(detector.step(self.still, 1.2))

    def test_splash_survives_capture_compression(self):
        detector = BobberDetector(self.calibration)

        def encoded(frame):
            ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            self.assertTrue(ok)
            return cv2.imdecode(jpeg, cv2.IMREAD_COLOR)

        base = encoded(self.still)
        splash = encoded(self.splash)
        for index in range(30):
            self.assertFalse(detector.step(base, index / 30))
        self.assertIsNotNone(detector.box)
        self.assertFalse(detector.step(splash, 1.0))
        self.assertTrue(detector.step(splash, 1.05))
        self.assertFalse(detector.step(base, 1.1))
        self.assertFalse(detector.step(splash, 1.15))

    def test_small_splash_with_roomy_template_and_large_search_area(self):
        still = np.full((720, 1280, 3), 60, dtype=np.uint8)
        still[230:380, 420:670] = self.still
        calibration = Calibration.from_frame(still, [293, 38, 633, 445], [521, 280, 38, 33], 0.72, 7)
        detector = BobberDetector(calibration)
        for index in range(30):
            self.assertFalse(detector.step(still, index / 30))
        self.assertIsNotNone(detector.box)

        distant_motion = still.copy()
        cv2.circle(distant_motion, (650, 340), 7, (160, 160, 160), -1)
        self.assertFalse(detector.step(distant_motion, 1.0))
        self.assertFalse(detector.step(still, 1.05))

        splash = still.copy()
        cv2.circle(splash, (546, 296), 5, (115, 115, 115), -1)
        self.assertFalse(detector.step(splash, 1.10))
        self.assertTrue(detector.step(splash, 1.15))
        self.assertFalse(detector.step(still, 1.20))


if __name__ == "__main__":
    unittest.main()
