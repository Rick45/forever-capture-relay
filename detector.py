"""Calibration and visual detection, independent of camera and key transport."""

import base64
import binascii
from dataclasses import dataclass, replace
import json

import cv2
import numpy as np

from audio import AUDIO_WINDOW


DEFAULT_BITE_THRESHOLD = 0.66
DEFAULT_REEL_DELAY = (120, 300)
DEFAULT_CAST_DELAY = (250, 650)


def valid_score(value):
    return type(value) in (float, int) and 0 <= value <= 1


def validate_motion_thresholds(match, splash):
    if (type(match) not in (float, int) or not 0.4 <= match <= 0.98
            or type(splash) not in (float, int) or not 2 <= splash <= 60):
        raise ValueError("Match must be 0.4 to 0.98 and splash threshold 2 to 60.")


def validate_delay_range(minimum, maximum):
    if (type(minimum) is not int or type(maximum) is not int
            or not 0 <= minimum <= maximum <= 2**53 - 1):
        raise ValueError("Enter whole milliseconds with 0 ≤ minimum ≤ maximum.")
    return minimum, maximum


def decode_template(encoded):
    png = base64.b64decode(encoded, validate=True)
    template = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if template is None:
        raise ValueError("Invalid template image.")
    return template


def encode_template(template):
    ok, png = cv2.imencode(".png", template)
    if not ok:
        raise ValueError("Could not encode the calibration image.")
    return base64.b64encode(png.tobytes()).decode("ascii")


@dataclass(frozen=True)
class CastingBar:
    box: tuple
    template: np.ndarray

    @classmethod
    def validate(cls, size, box, template):
        if not isinstance(box, (list, tuple)) or len(box) != 4 or any(type(value) is not int for value in box):
            raise ValueError("Draw a box around the left end of the visible casting bar.")
        x, y, width, height = box
        if (not 12 <= width <= 160 or not 6 <= height <= 60 or x < 0 or y < 0
                or x + width > size[0] or y + height > size[1] or template.shape[:2] != (height, width)):
            raise ValueError("Select a casting bar box 12 to 160 pixels wide and 6 to 60 pixels tall.")
        if max(cv2.meanStdDev(template)[1].ravel()) < 8:
            raise ValueError("Include the edge of the casting bar so the selection has visible detail.")
        return cls(tuple(box), template)

    @classmethod
    def from_frame(cls, frame, box):
        if not isinstance(box, list) or len(box) != 4 or any(type(value) is not int for value in box):
            raise ValueError("Draw a box around the left end of the visible casting bar.")
        x, y, width, height = box
        return cls.validate(frame.shape[1::-1], box, frame[y:y+height, x:x+width].copy())

    def matches(self, frame):
        x, y, width, height = self.box
        # The fill and text can change across a wide saved selection.
        anchor_width = min(width, 24)
        patch = frame[y:y+height, x:x+anchor_width]
        anchor = self.template[:, :anchor_width]
        error = cv2.mean(cv2.absdiff(patch, anchor))
        score = cv2.matchTemplate(patch, anchor, cv2.TM_CCOEFF_NORMED)[0, 0]
        return bool(score >= 0.78 and max(error[:3]) <= 40)


class BarTracker:
    def __init__(self, bar):
        self.bar = bar
        self.reset()

    def reset(self):
        self.visible = None
        self.candidate = None
        self.candidate_since = 0.0
        self.samples = 0
        self.index = None

    def update(self, frame, index, now):
        if index == self.index:
            return
        self.index = index
        candidate = self.bar.matches(frame)
        if candidate != self.candidate:
            self.candidate, self.candidate_since, self.samples = candidate, now, 0
        self.samples += 1
        # A momentary mismatch at the end of the fill animation can look like absence.
        settle = 0.06 if candidate else 0.35
        if self.samples >= 3 and now - self.candidate_since >= settle:
            self.visible = candidate


@dataclass(frozen=True)
class Calibration:
    size: tuple
    water: tuple
    template: np.ndarray
    match: float
    splash: float
    cast_bar: CastingBar | None = None
    interact_icon: tuple | None = None
    bite_threshold: float = DEFAULT_BITE_THRESHOLD
    reel_delay: tuple[int, int] = DEFAULT_REEL_DELAY
    cast_delay: tuple[int, int] = DEFAULT_CAST_DELAY

    @staticmethod
    def validate(size, water, template, match, splash):
        def integers(values, count):
            return (isinstance(values, (list, tuple)) and len(values) == count
                    and all(type(value) is int for value in values))

        if not integers(size, 2) or not integers(water, 4):
            raise ValueError("Invalid calibration dimensions.")
        width, height = size
        x, y, w, h = water
        if x < 0 or y < 0 or w < 32 or h < 32 or x + w > width or y + h > height:
            raise ValueError("Draw a water box inside the captured image.")
        th, tw = template.shape[:2]
        if not (8 <= tw <= min(w, 160) and 8 <= th <= min(h, 160)):
            raise ValueError("Draw a tight bobber box between 8 and 160 pixels per side.")
        if max(cv2.meanStdDev(template)[1].ravel()) < 4:
            raise ValueError("The bobber box needs more visible detail.")
        validate_motion_thresholds(match, splash)
        return Calibration(tuple(size), tuple(water), template, match, splash)

    @classmethod
    def from_frame(cls, frame, water, bobber, match, splash):
        height, width = frame.shape[:2]
        if not isinstance(bobber, list) or len(bobber) != 4 or any(type(n) is not int for n in bobber):
            raise ValueError("Draw a bobber box first.")
        x, y, w, h = bobber
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise ValueError("Draw a bobber box inside the captured image.")
        result = cls.validate((width, height), water, frame[y:y+h, x:x+w].copy(), match, splash)
        wx, wy, ww, wh = result.water
        if x < wx or y < wy or x + w > wx + ww or y + h > wy + wh:
            raise ValueError("Draw the bobber box inside the water box.")
        return result

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["version"] != 1:
                raise ValueError("Unsupported profile version.")
            profile = cls.validate(data["size"], data["water"], decode_template(data["template"]),
                                   data["match"], data["splash"])
            if "cast_bar" in data:
                bar = data["cast_bar"]
                profile = replace(profile, cast_bar=CastingBar.validate(profile.size, bar["box"],
                                                                       decode_template(bar["template"])))
            if "interact_icon" in data:
                icon_size, threshold = data["interact_icon"]
                if type(icon_size) is not int or not 12 <= icon_size <= 128 or not valid_score(threshold):
                    raise ValueError("Invalid interact-icon calibration.")
                profile = replace(profile, interact_icon=(icon_size, threshold))
            bite_threshold = data.get("bite_threshold", DEFAULT_BITE_THRESHOLD)
            if not valid_score(bite_threshold):
                raise ValueError("Invalid splash fingerprint threshold.")
            profile = replace(profile, bite_threshold=bite_threshold)
            reel_delay = validate_delay_range(*data.get("reel_delay", DEFAULT_REEL_DELAY))
            cast_delay = validate_delay_range(*data.get("cast_delay", DEFAULT_CAST_DELAY))
            profile = replace(profile, reel_delay=reel_delay, cast_delay=cast_delay)
            return profile
        except FileNotFoundError:
            return None
        except (KeyError, TypeError, ValueError, binascii.Error, cv2.error) as error:
            raise ValueError(f"Invalid calibration profile {path}: {error}") from error

    def save(self, path):
        temporary = path.with_suffix(path.suffix + ".tmp")
        data = {
            "version": 1, "size": self.size, "water": self.water,
            "match": self.match, "splash": self.splash,
            "template": encode_template(self.template),
        }
        if self.cast_bar:
            data["cast_bar"] = {"box": self.cast_bar.box, "template": encode_template(self.cast_bar.template)}
        if self.interact_icon:
            data["interact_icon"] = self.interact_icon
        data["bite_threshold"] = self.bite_threshold
        data["reel_delay"] = self.reel_delay
        data["cast_delay"] = self.cast_delay
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(path)


class BobberDetector:
    def __init__(self, calibration, require_audio=False, diagnostics=False):
        self.calibration = calibration
        self.require_audio = require_audio
        self.diagnostics = diagnostics
        self.candidate_report = None
        self.motion_threshold = None
        self.preview_state = "Looking for the bobber"
        self.box = None
        self.confidence = 0.0
        self.motion = 0.0
        self.candidate = None
        self.confirmations = 0
        self.locked_at = None
        self.last_seen = None
        self.previous_gray = None
        self.baseline = 0.0
        self.spike_at = None
        self.spike_reference = None
        self.visual_peak = None
        self.confirmed_motion = None
        self.fired = False
        self.motion_window = max(7, min(17, (calibration.size[1] // 72) | 1))

    def motion_near_box(self, previous, gray):
        diff = cv2.absdiff(previous, gray)
        x, y, _, _ = self.calibration.water
        bx, by, tw, th = self.box
        center_x = bx - x + tw // 2
        center_y = by - y + th // 2
        radius = max(12, min(24, max(tw, th) // 2 + 5))
        left, top = max(0, center_x-radius), max(0, center_y-radius)
        right, lower_edge = center_x+radius+1, center_y+radius+1
        local = diff[top:lower_edge, left:right]
        # An impact can occupy only a small patch next to the marker.
        averaged = cv2.blur(local, (self.motion_window, self.motion_window), borderType=cv2.BORDER_REPLICATE)
        surrounding = float(diff.mean())
        return max(0.0, float(averaged.max()) - surrounding), surrounding

    def step(self, frame, now, audio_peak=None):
        if self.fired:
            return False
        cal = self.calibration
        x, y, w, h = cal.water
        th, tw = cal.template.shape[:2]
        search = frame[y:y+h, x:x+w]
        search_x, search_y = x, y
        if self.box:
            bx, by, _, _ = self.box
            margin = max(14, min(48, max(tw, th)))
            search_x, search_y = max(x, bx-margin), max(y, by-margin)
            right = min(x+w, bx+tw+margin)
            lower_edge = min(y+h, by+th+margin)
            search = frame[search_y:lower_edge, search_x:right]
        scores = cv2.matchTemplate(search, cal.template, cv2.TM_CCOEFF_NORMED)
        _, score, _, (left, top) = cv2.minMaxLoc(scores)
        candidate = (search_x + left, search_y + top)
        self.confidence = float(score)
        if self.box is None:
            if score < cal.match:
                self.confirmations = 0
                return False
            if self.confirmations and max(abs(a-b) for a, b in zip(candidate, self.candidate)) <= 6:
                self.confirmations += 1
            else:
                self.confirmations = 1
            self.candidate = candidate
            if self.confirmations < 3:
                return False
            self.box = (*candidate, tw, th)
            self.locked_at = now
        if score >= cal.match:
            self.last_seen = now

        # Measure motion around the last known position even if a splash hides the bobber.
        water_gray = cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(water_gray, (3, 3), 0)
        if self.previous_gray is not None:
            self.motion, surrounding = self.motion_near_box(self.previous_gray, gray)
            threshold = max(cal.splash, self.baseline * 3.0 + 2.0)
            can_detect = now - self.locked_at >= 0.65 and now - self.last_seen <= 0.45 and surrounding < 12
            if self.diagnostics:
                self.motion_threshold = threshold
                if now - self.locked_at < 0.65:
                    self.preview_state = "Waiting for the bobber to settle"
                elif now - self.last_seen > 0.45:
                    self.preview_state = "Bobber match lost"
                elif surrounding >= 12:
                    self.preview_state = "Motion across the water"
                else:
                    self.preview_state = "Watching for local motion"
                if self.spike_at is not None or self.visual_peak is not None:
                    self.note_audio_peak(audio_peak)
            if self.spike_at is not None:
                if not can_detect or now - self.spike_at > 0.22:
                    if self.diagnostics:
                        self.candidate_report["result"] = (self.preview_state if not can_detect
                                                           else "Motion did not persist")
                    self.spike_at = None
                    self.spike_reference = None
                else:
                    sustained_motion, sustained_surrounding = self.motion_near_box(self.spike_reference, gray)
                    if self.motion >= threshold or (sustained_surrounding < 12 and sustained_motion >= threshold):
                        motion = self.motion if self.motion >= threshold else sustained_motion
                        self.confirmed_motion = (motion, threshold)
                        self.visual_peak = self.spike_at
                        self.spike_at = self.spike_reference = None
                        if self.diagnostics:
                            self.candidate_report["result"] = "Waiting for HDMI audio"
            if self.visual_peak is not None:
                if not can_detect or now - self.visual_peak > AUDIO_WINDOW:
                    if self.diagnostics:
                        self.candidate_report["result"] = (self.preview_state if not can_detect
                                                           else "Audio confirmation timed out")
                    self.visual_peak = None
                elif not self.require_audio or (audio_peak is not None and abs(audio_peak - self.visual_peak) <= AUDIO_WINDOW):
                    if self.diagnostics:
                        self.candidate_report["result"] = "Bite detected"
                    self.fired = True
                    return True
            if can_detect and self.visual_peak is None and self.spike_at is None and self.motion >= threshold:
                self.spike_at = now
                self.spike_reference = self.previous_gray
                if self.diagnostics:
                    self.candidate_report = {
                        "time": now, "result": "Checking whether motion persists",
                        "match": round(self.confidence, 2), "motion": round(self.motion, 1),
                        "threshold": round(threshold, 1), "audio_offset": None,
                    }
                    self.note_audio_peak(audio_peak)
            elif surrounding < 12 and self.motion < threshold:
                self.baseline = 0.90 * self.baseline + 0.10 * self.motion
        self.previous_gray = gray
        if score >= cal.match and self.spike_at is None:
            self.box = (*candidate, tw, th)
        return False

    def note_audio_peak(self, audio_peak):
        if audio_peak is not None:
            report = self.candidate_report
            offset = round((audio_peak - report["time"]) * 1000)
            if report["audio_offset"] is None or abs(offset) < abs(report["audio_offset"]):
                report["audio_offset"] = offset
