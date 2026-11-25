"""Run wowfish's icon and splash matchers on the relay's HDMI feeds."""

from functools import lru_cache
from pathlib import Path
import sys

from audio import PCM_RATE

# Keep the pinned upstream package intact, including its absolute imports.
sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))

from wowfish.audio import AudioChunk
from wowfish.audio.clips import build_fingerprints
from wowfish.audio.detector import BiteDetector
from wowfish.audio.wav import resample
from wowfish.vision.calibrate import OFF_SCALE, calibrate_icon_scale
from wowfish.vision.matcher import GOLD_MIN, MIN_TEMPLATE_PX, IconMatcher
from wowfish.vision.search import IconSearch
from wowfish.vision.template import load_icon_templates, template_for_size


@lru_cache(maxsize=1)
def detection_assets():
    return load_icon_templates(), build_fingerprints()


def icon_search_bounds(calibration):
    x, y, width, height = calibration.water
    frame_width, frame_height = calibration.size
    # The interact icon appears above the bobber's position in the water.
    return (max(0, x - 32), max(0, y - 120),
            min(frame_width, x + width + 32), min(frame_height, y + height + 32))


def calibrate_interact_icon(frame, calibration, box):
    if (not isinstance(box, list) or len(box) != 4
            or any(type(value) is not int for value in box)):
        raise ValueError("Draw a tight box around the gold interact icon.")
    x, y, width, height = box
    frame_height, frame_width = frame.shape[:2]
    if (not MIN_TEMPLATE_PX <= width <= 128 or not MIN_TEMPLATE_PX <= height <= 128
            or x < 0 or y < 0 or x + width > frame_width or y + height > frame_height):
        raise ValueError("Select a gold icon box 12 to 128 pixels per side inside the frame.")
    left, top, right, bottom = icon_search_bounds(calibration)
    if x < left or y < top or x + width > right or y + height > bottom:
        raise ValueError("The gold icon must be within the calibrated water area or just above it.")

    size = max(width, height)
    # A user selects the visible teeth, while the sprite also includes transparent
    # margins and a shadow. Allow for both a tight selection and extra water.
    sizes = range(max(MIN_TEMPLATE_PX, min(width, height) // 2), min(128, size * 2) + 1)
    padding = sizes.stop // 2 + 2
    crop_x, crop_y = max(0, x - padding), max(0, y - padding)
    crop = frame[crop_y:y+height+padding, crop_x:x+width+padding]
    templates, _ = detection_assets()
    scale = calibrate_icon_scale(crop, templates, sizes=sizes, probe_sizes=(sizes.start, size, sizes.stop - 1),
                                 anchors=1)
    selected = x <= crop_x + scale.x < x + width and y <= crop_y + scale.y < y + height
    # Upstream compares off-scale matches anywhere in the crop. A nearby gold
    # object can suppress a real gear's prominence. Check those sizes at the
    # selected gear instead, with just two pixels of capture jitter.
    local_prominence = scale.prominence
    if selected and not scale.valid and scale.score >= 0.80 and scale.gold >= GOLD_MIN:
        off_scores = []
        center_x, center_y = crop_x + scale.x, crop_y + scale.y
        for factor in OFF_SCALE:
            side = max(MIN_TEMPLATE_PX, round(scale.icon_size_px * factor))
            if side == scale.icon_size_px:
                continue
            half = (side + 1) // 2 + 2
            sample = frame[max(0, center_y-half):center_y+half,
                           max(0, center_x-half):center_x+half]
            match = IconMatcher(template_for_size(templates, side)).match(sample)
            if match.score >= 0:
                off_scores.append(match.score)
        if off_scores:
            local_prominence = max(local_prominence, scale.score - max(off_scores))
    # HDMI scaling softens the size peak even for gears above 18 pixels.
    hdmi_gear = (scale.score >= 0.80 and scale.gold >= GOLD_MIN and local_prominence >= 0.14)
    if not selected or not (scale.valid or hdmi_gear):
        if not selected:
            reason = "The best candidate is outside your box. Redraw around only the gear."
        elif scale.gold < GOLD_MIN:
            reason = "The selection is not gold enough. Freeze with the gold gear visible, rather than the grey icon."
        elif scale.score < 0.80:
            reason = "The gear shape is unclear. Freeze a sharper frame and include all the teeth with a little surrounding water."
        else:
            reason = "The gear size is ambiguous. Exclude the bobber, text, and other gold objects; try a sharper frame or larger UI scale."
        raise ValueError(
            "Could not match the gold gear inside the box. " + reason
            + f" (Best size {scale.icon_size_px}px, match {scale.score:.2f}, "
              f"gold {scale.gold:.2f}, size separation {local_prominence:.2f}.)")
    return scale.icon_size_px, scale.suggested_threshold()


class WowfishDetector:
    def __init__(self, calibration, audio):
        self.calibration = calibration
        self.audio = audio
        templates, fingerprints = detection_assets()
        self.bites = BiteDetector(fingerprints, threshold=calibration.bite_threshold)
        size, self.threshold = calibration.interact_icon
        self.left, self.top, self.right, self.bottom = icon_search_bounds(calibration)
        margin = max(0.10, size * 1.5 / min(self.right - self.left, self.bottom - self.top))
        matcher = IconMatcher(template_for_size(templates, size))
        self.search = IconSearch(matcher, self.threshold, margin=margin)
        self.reset()

    def update_thresholds(self, calibration):
        self.calibration = calibration
        _, self.threshold = calibration.interact_icon
        self.search.threshold = self.threshold
        if self.bites.threshold != calibration.bite_threshold:
            self.bites.threshold = calibration.bite_threshold
            self.bites.reset()
            self.audio_cursor = self.audio.pcm_cursor()

    def reset(self):
        self.box = None
        self.confidence = 0.0
        self.icon_score = None
        self.last_seen = None
        self.fired = False
        self.state = "Looking for the gold interact icon"
        self.audio_cursor = self.audio.pcm_cursor()
        self.search.reset()
        self.bites.reset()
        self.bite = None

    def locate_icon(self, frame):
        crop = frame[self.top:self.bottom, self.left:self.right]
        _, match = self.search.step(crop)
        self.confidence = match.score
        if match.accepted(self.threshold):
            return match.x + self.left, match.y + self.top, match.size
        return None

    def step(self, frame, now, *, listen=True):
        if self.fired:
            return False
        icon = self.locate_icon(frame)
        if icon is not None:
            x, y, size = icon
            self.icon_score = self.confidence
            if self.box is None:
                self.audio_cursor = self.audio.pcm_cursor()
            self.state = "Gold icon found. Listening for a splash fingerprint."
            self.box = (x - size // 2, y - size // 2, size, size)
            self.last_seen = now
        if not listen:
            # Keep visual preview useful while audio is unavailable. Discard old
            # samples so reconnecting audio cannot confirm a stale splash.
            self.bites.reset()
            self.audio_cursor = self.audio.pcm_cursor()
        if self.box is None:
            return False
        if now - self.last_seen >= 0.75:
            self.reset()
            return False

        if not listen:
            self.state = "Gold icon found. Waiting for live HDMI audio."
            return False

        sample_rate = self.bites.p.sample_rate
        for index, captured_at, samples in self.audio.pcm_after(self.audio_cursor):
            if index != self.audio_cursor + 1:
                self.bites.reset()
            self.audio_cursor = index
            chunk = AudioChunk(resample(samples, PCM_RATE, sample_rate), sample_rate, captured_at)
            events = self.bites.feed(chunk)
            if events:
                self.bite = events[0]
                self.fired = True
                self.state = "Splash fingerprint matched"
                return True
        return False
