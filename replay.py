"""Report bite detections in a saved video of a single cast. Sends no keys."""

import argparse
import math
from pathlib import Path

import cv2

from detector import BobberDetector, Calibration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--profile", type=Path, default=Path("calibration.json"))
    args = parser.parse_args()
    try:
        calibration = Calibration.load(args.profile)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if calibration is None:
        parser.error(f"Calibration profile not found: {args.profile}")
    detector = BobberDetector(calibration)
    video = cv2.VideoCapture(str(args.video))
    try:
        fps = video.get(cv2.CAP_PROP_FPS)
        if not video.isOpened() or not math.isfinite(fps) or fps <= 0:
            parser.error("Could not open the video with a valid frame rate.")
        frames = 0
        while True:
            ok, frame = video.read()
            if not ok:
                break
            if frame.shape[1::-1] != calibration.size:
                parser.error("Video dimensions must match the calibration profile.")
            seconds = frames / fps
            had_lock = detector.box is not None
            bite = detector.step(frame, seconds)
            if not had_lock and detector.box:
                print(f"{seconds:.2f}s  Bobber found")
            if bite:
                print(f"{seconds:.2f}s  Bite detected")
            frames += 1
        if not frames:
            parser.error("The video has no readable frames.")
        print(f"{frames} frames processed, {int(detector.fired)} bite detections.")
    finally:
        video.release()


if __name__ == "__main__":
    main()
