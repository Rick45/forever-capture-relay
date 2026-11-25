"""Read HDMI audio levels without recording or transmitting audio."""

from collections import deque
import math
import subprocess
import threading
import time

import numpy as np


AUDIO_WINDOW = 0.35
PCM_RATE = 48000


class AudioCapture:
    def __init__(self, device):
        self.device = device
        self.closed = threading.Event()
        self.sample = (0.0, None, None, None)
        self.pcm_lock = threading.Lock()
        self.pcm_index = 0
        self.pcm_buffer = deque(maxlen=75)

    def run(self):
        command = ["arecord", "-q", "-D", self.device, "-t", "raw", "-f", "S16_LE",
                   "-c", "2", "-r", str(PCM_RATE), "--period-time=20000", "--buffer-time=80000"]
        chunk_bytes = PCM_RATE * 2 * 2 // 50
        while not self.closed.is_set():
            try:
                with subprocess.Popen(command, stdout=subprocess.PIPE) as recorder:
                    try:
                        self.read_samples(recorder.stdout, chunk_bytes)
                    finally:
                        if recorder.poll() is None:
                            recorder.terminate()
            except OSError as error:
                print(f"HDMI audio unavailable: {error}", flush=True)
            with self.pcm_lock:
                self.pcm_buffer.clear()
                self.pcm_index += 1
            self.closed.wait(2)

    def read_samples(self, stream, chunk_bytes):
        baseline = None
        last_peak = None
        last_signal = None
        was_loud = False
        while not self.closed.is_set():
            chunk = stream.read(chunk_bytes)
            if len(chunk) != chunk_bytes:
                break
            audio = np.frombuffer(chunk, dtype="<i2").reshape(-1, 2).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(audio * audio, axis=0)).max())
            now = time.monotonic()
            if baseline is None:
                baseline = rms
            loud = rms >= max(0.0015, baseline * 3.0)
            if loud and not was_loud:
                last_peak = now
            if rms >= 0.0005:
                last_signal = now
            was_loud = loud
            baseline += 0.03 * (rms - baseline)
            self.sample = (now, round(20 * math.log10(max(0.0001, rms))), last_peak, last_signal)
            with self.pcm_lock:
                self.pcm_index += 1
                self.pcm_buffer.append((self.pcm_index, now, audio.mean(axis=1)))

    def pcm_cursor(self):
        with self.pcm_lock:
            return self.pcm_index

    def pcm_after(self, index):
        with self.pcm_lock:
            return [chunk for chunk in self.pcm_buffer if chunk[0] > index]

    def recent_peak(self, now):
        sample_time, _, peak_time, _ = self.sample
        if peak_time is not None and now - sample_time <= 0.3 and now - peak_time <= 2 * AUDIO_WINDOW:
            return peak_time
        return None
