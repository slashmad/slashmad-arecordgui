from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
from pathlib import Path
import struct
import sys
import threading
import time
from typing import Literal
from collections.abc import Callable


FRAME_BYTES = 8  # stereo, signed 32-bit little-endian
FULL_SCALE = 1 << 31


def wave_data_offset(header: bytes) -> int | None:
    """Return the first PCM byte offset, or None while the header is incomplete."""
    if len(header) < 12:
        return None
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError("Not a RIFF/WAVE file")

    offset = 12
    while offset + 8 <= len(header):
        chunk_id = header[offset:offset + 4]
        chunk_size = struct.unpack_from("<I", header, offset + 4)[0]
        data_offset = offset + 8
        if chunk_id == b"data":
            return data_offset
        next_offset = data_offset + chunk_size + (chunk_size & 1)
        if next_offset > len(header):
            return None
        offset = next_offset
    return None


def stereo_s32le_rms_dbfs(pcm: bytes) -> tuple[float, float]:
    if len(pcm) % FRAME_BYTES:
        raise ValueError("PCM data must contain complete stereo S32_LE frames")
    if not pcm:
        return float("-inf"), float("-inf")

    samples = array("i")
    samples.frombytes(pcm)
    if samples.itemsize != 4:
        raise RuntimeError("This platform does not provide 32-bit signed integers")
    if sys.byteorder != "little":
        samples.byteswap()

    frames = len(samples) // 2
    left_square = sum(sample * sample for sample in samples[0::2]) / frames
    right_square = sum(sample * sample for sample in samples[1::2]) / frames

    def to_dbfs(mean_square: float) -> float:
        if mean_square <= 0:
            return float("-inf")
        return 20.0 * math.log10(math.sqrt(mean_square) / FULL_SCALE)

    return to_dbfs(left_square), to_dbfs(right_square)


@dataclass(frozen=True)
class AutoStopConfig:
    earliest_seconds: float = 30 * 60
    silence_seconds: float = 60
    threshold_dbfs: float = -60.0
    hysteresis_db: float = 3.0

    def __post_init__(self) -> None:
        if self.earliest_seconds < 0:
            raise ValueError("Earliest stop time cannot be negative")
        if self.silence_seconds <= 0:
            raise ValueError("Silence duration must be positive")
        if not -100.0 <= self.threshold_dbfs <= -10.0:
            raise ValueError("Silence threshold must be between -100 and -10 dBFS")
        if self.hysteresis_db < 0:
            raise ValueError("Hysteresis cannot be negative")


DetectorState = Literal[
    "waiting_for_activity",
    "waiting_for_window",
    "listening",
    "silence",
    "candidate",
]


@dataclass(frozen=True)
class DetectorUpdate:
    state: DetectorState
    silence_seconds: float
    activity_seen: bool
    candidate_started: bool = False
    candidate_cancelled: bool = False


class EndOfTapeDetector:
    def __init__(self, config: AutoStopConfig):
        self.config = config
        self.activity_seen = False
        self.silence_seconds = 0.0
        self.candidate = False
        # Start fail-closed: a level inside the hysteresis band is not enough
        # to prove that tape activity has occurred.
        self._quiet = True

    def process(
        self,
        left_dbfs: float,
        right_dbfs: float,
        duration_seconds: float,
        elapsed_end_seconds: float,
    ) -> DetectorUpdate:
        if duration_seconds <= 0:
            raise ValueError("Analysis duration must be positive")

        level = max(left_dbfs, right_dbfs)
        if level <= self.config.threshold_dbfs:
            self._quiet = True
        elif level >= self.config.threshold_dbfs + self.config.hysteresis_db:
            self._quiet = False

        previous_candidate = self.candidate
        if not self._quiet:
            self.activity_seen = True
            self.silence_seconds = 0.0
            self.candidate = False
        elif self.activity_seen and elapsed_end_seconds > self.config.earliest_seconds:
            elapsed_start = elapsed_end_seconds - duration_seconds
            eligible_start = max(elapsed_start, self.config.earliest_seconds)
            self.silence_seconds += max(0.0, elapsed_end_seconds - eligible_start)
            self.candidate = self.silence_seconds >= self.config.silence_seconds
        else:
            self.silence_seconds = 0.0
            self.candidate = False

        if self.candidate:
            state: DetectorState = "candidate"
        elif not self.activity_seen:
            state = "waiting_for_activity"
        elif elapsed_end_seconds < self.config.earliest_seconds:
            state = "waiting_for_window"
        elif self._quiet:
            state = "silence"
        else:
            state = "listening"

        return DetectorUpdate(
            state=state,
            silence_seconds=self.silence_seconds,
            activity_seen=self.activity_seen,
            candidate_started=self.candidate and not previous_candidate,
            candidate_cancelled=previous_candidate and not self.candidate,
        )


@dataclass(frozen=True)
class AnalysisUpdate:
    left_dbfs: float
    right_dbfs: float
    audio_seconds: float
    detector: DetectorUpdate


class GrowingWavAnalyzer:
    """Analyze appended PCM without participating in the capture write path."""

    def __init__(
        self,
        path: Path,
        config: AutoStopConfig,
        on_update: Callable[[AnalysisUpdate], None],
        on_error: Callable[[str], None],
        *,
        sample_rate: int = 48_000,
        analysis_frames: int = 24_000,
        poll_seconds: float = 0.1,
    ):
        if sample_rate <= 0 or analysis_frames <= 0 or poll_seconds <= 0:
            raise ValueError("Analyzer timing values must be positive")
        self.path = path
        self.detector = EndOfTapeDetector(config)
        self.on_update = on_update
        self.on_error = on_error
        self.sample_rate = sample_rate
        self.analysis_frames = analysis_frames
        self.poll_seconds = poll_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("WAV analyzer is already running")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="growing-wav-analyzer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _wait_for_data(self) -> bool:
        return self._stop_event.wait(self.poll_seconds)

    def _run(self) -> None:
        try:
            while not self.path.exists():
                if self._wait_for_data():
                    return
            with self.path.open("rb", buffering=0) as wav_file:
                self._analyze_file(wav_file)
        except (OSError, ValueError, RuntimeError) as exc:
            if not self._stop_event.is_set():
                self.on_error(str(exc))

    def _analyze_file(self, wav_file) -> None:
        pending = bytearray()
        data_offset: int | None = None
        while data_offset is None:
            block = wav_file.read(4096)
            if block:
                pending.extend(block)
                if len(pending) > 1024 * 1024:
                    raise ValueError("WAV header exceeds the 1 MiB safety limit")
                data_offset = wave_data_offset(pending)
            elif self._wait_for_data():
                return

        pcm = pending[data_offset:]
        frames_analyzed = 0
        analysis_bytes = self.analysis_frames * FRAME_BYTES

        while not self._stop_event.is_set():
            while len(pcm) < analysis_bytes:
                block = wav_file.read(analysis_bytes - len(pcm))
                if block:
                    pcm.extend(block)
                elif self._wait_for_data():
                    return
                else:
                    break
            if len(pcm) < analysis_bytes:
                continue

            analysis_pcm = bytes(pcm[:analysis_bytes])
            del pcm[:analysis_bytes]
            left, right = stereo_s32le_rms_dbfs(analysis_pcm)
            frames_analyzed += self.analysis_frames
            audio_seconds = frames_analyzed / self.sample_rate
            update = self.detector.process(
                left,
                right,
                self.analysis_frames / self.sample_rate,
                audio_seconds,
            )
            self.on_update(AnalysisUpdate(left, right, audio_seconds, update))
