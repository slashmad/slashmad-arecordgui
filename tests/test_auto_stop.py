from __future__ import annotations

import math
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import wave

from arecordgui.auto_stop import (
    AutoStopConfig,
    EndOfTapeDetector,
    GrowingWavAnalyzer,
    stereo_s32le_rms_dbfs,
    wave_data_offset,
)


def stereo_frames(left: int, right: int, count: int) -> bytes:
    return struct.pack("<ii", left, right) * count


def streaming_wav_header(sample_rate: int) -> bytes:
    byte_rate = sample_rate * 2 * 4
    fmt = struct.pack("<HHIIHH", 1, 2, sample_rate, byte_rate, 8, 32)
    return (
        b"RIFF" + struct.pack("<I", 0x7FFFFFFF) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", 0x7FFFF000)
    )


class WavAnalysisTests(unittest.TestCase):
    def test_wave_data_offset_accepts_extra_chunks_and_padding(self):
        junk = b"JUNK" + struct.pack("<I", 3) + b"abc" + b"\0"
        fmt = b"fmt " + struct.pack("<I", 16) + bytes(16)
        header = b"RIFF" + struct.pack("<I", 1000) + b"WAVE" + junk + fmt
        header += b"data" + struct.pack("<I", 900)
        self.assertEqual(wave_data_offset(header), len(header))

    def test_wave_data_offset_waits_for_an_incomplete_header(self):
        self.assertIsNone(wave_data_offset(b"RIFF"))
        partial = b"RIFF" + bytes(4) + b"WAVEfmt " + struct.pack("<I", 16)
        self.assertIsNone(wave_data_offset(partial))

    def test_wave_data_offset_rejects_non_wav_data(self):
        with self.assertRaises(ValueError):
            wave_data_offset(b"NOPE" + bytes(4) + b"WAVE")

    def test_stereo_rms_is_measured_per_channel(self):
        left, right = stereo_s32le_rms_dbfs(
            stereo_frames(1 << 30, 1 << 29, 100)
        )
        self.assertAlmostEqual(left, -6.0206, places=3)
        self.assertAlmostEqual(right, -12.0412, places=3)

    def test_zero_pcm_is_negative_infinity(self):
        left, right = stereo_s32le_rms_dbfs(stereo_frames(0, 0, 10))
        self.assertTrue(math.isinf(left) and left < 0)
        self.assertTrue(math.isinf(right) and right < 0)

    def test_incomplete_pcm_frame_is_rejected(self):
        with self.assertRaises(ValueError):
            stereo_s32le_rms_dbfs(bytes(7))


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.detector = EndOfTapeDetector(AutoStopConfig(
            earliest_seconds=10,
            silence_seconds=4,
            threshold_dbfs=-50,
            hysteresis_db=3,
        ))

    def test_silence_cannot_arm_detector_before_activity(self):
        update = self.detector.process(-80, -80, 20, 20)
        self.assertEqual(update.state, "waiting_for_activity")
        self.assertFalse(update.candidate_started)

    def test_hysteresis_band_alone_does_not_prove_activity(self):
        update = self.detector.process(-49, -49, 20, 20)
        self.assertEqual(update.state, "waiting_for_activity")
        self.assertFalse(update.activity_seen)

    def test_long_gap_before_earliest_time_is_ignored(self):
        self.detector.process(-20, -20, 1, 1)
        update = self.detector.process(-80, -80, 8, 9)
        self.assertEqual(update.state, "waiting_for_window")
        self.assertEqual(update.silence_seconds, 0)

    def test_only_post_window_silence_counts(self):
        self.detector.process(-20, -20, 1, 1)
        update = self.detector.process(-80, -80, 4, 12)
        self.assertEqual(update.silence_seconds, 2)
        self.assertFalse(update.candidate_started)
        update = self.detector.process(-80, -80, 2, 14)
        self.assertTrue(update.candidate_started)
        self.assertEqual(update.state, "candidate")

    def test_activity_on_either_channel_cancels_candidate(self):
        self.detector.process(-20, -20, 1, 1)
        self.detector.process(-80, -80, 4, 14)
        update = self.detector.process(-80, -20, 1, 15)
        self.assertTrue(update.candidate_cancelled)
        self.assertEqual(update.state, "listening")
        self.assertEqual(update.silence_seconds, 0)

    def test_hysteresis_does_not_chatter(self):
        self.detector.process(-20, -20, 1, 1)
        self.detector.process(-60, -60, 1, 11)
        update = self.detector.process(-49, -49, 1, 12)
        self.assertEqual(update.state, "silence")
        update = self.detector.process(-46, -46, 1, 13)
        self.assertEqual(update.state, "listening")


class GrowingWavAnalyzerTests(unittest.TestCase):
    def test_pcm_appended_after_start_is_analyzed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capture.wav"
            path.write_bytes(
                streaming_wav_header(10)
                + stereo_frames(1 << 29, 1 << 29, 10)
            )
            activity = threading.Event()
            candidate = threading.Event()
            errors: list[str] = []

            def on_update(update) -> None:
                if update.detector.activity_seen:
                    activity.set()
                if update.detector.candidate_started:
                    candidate.set()

            analyzer = GrowingWavAnalyzer(
                path,
                AutoStopConfig(
                    earliest_seconds=1,
                    silence_seconds=2,
                    threshold_dbfs=-50,
                ),
                on_update,
                errors.append,
                sample_rate=10,
                analysis_frames=10,
                poll_seconds=0.01,
            )
            analyzer.start()
            self.assertTrue(activity.wait(1), "initial tape activity was not seen")
            with path.open("ab") as wav_file:
                wav_file.write(stereo_frames(0, 0, 30))
                wav_file.flush()
            self.assertTrue(candidate.wait(1), "appended silence was not analyzed")
            analyzer.stop()
            self.assertEqual(errors, [])

    def test_completed_wav_is_analyzed_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capture.wav"
            active = stereo_frames(1 << 29, 1 << 29, 10)
            quiet = stereo_frames(0, 0, 30)
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(2)
                wav_file.setsampwidth(4)
                wav_file.setframerate(10)
                wav_file.writeframes(active + quiet)
            original = path.read_bytes()

            candidate = threading.Event()
            errors: list[str] = []
            analyzer = GrowingWavAnalyzer(
                path,
                AutoStopConfig(
                    earliest_seconds=1,
                    silence_seconds=2,
                    threshold_dbfs=-50,
                ),
                lambda update: candidate.set() if update.detector.candidate_started else None,
                errors.append,
                sample_rate=10,
                analysis_frames=10,
                poll_seconds=0.01,
            )
            analyzer.start()
            self.assertTrue(candidate.wait(1), "analyzer did not detect sustained silence")
            analyzer.stop()

            self.assertEqual(errors, [])
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_wav_disables_only_the_analyzer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capture.wav"
            path.write_bytes(b"not a wav file")
            failed = threading.Event()
            errors: list[str] = []

            def on_error(message: str) -> None:
                errors.append(message)
                failed.set()

            analyzer = GrowingWavAnalyzer(
                path,
                AutoStopConfig(),
                lambda _update: None,
                on_error,
                analysis_frames=10,
                poll_seconds=0.01,
            )
            analyzer.start()
            self.assertTrue(failed.wait(1), "analyzer error was not reported")
            self.assertIn("Not a RIFF/WAVE file", errors[0])


if __name__ == "__main__":
    unittest.main()
