from datetime import datetime
from pathlib import Path
import math
import signal
import tempfile
import unittest

from arecordgui.core import (
    DEFAULT_CAPTURE_DEVICE,
    CaptureProfile,
    capture_devices_with_default,
    capture_exit_was_clean,
    count_xruns,
    ensure_directory,
    parse_vu,
    parse_capture_devices,
    percent_to_dbfs,
    recording_path,
    safe_stem,
)


class CoreTests(unittest.TestCase):
    def test_command_keeps_direct_hardware_profile(self):
        command = CaptureProfile().command(Path("/tmp/tape.wav"))
        self.assertEqual(command[:3], ["arecord", "-D", "hw:BlackmagicIO,0"])
        self.assertIn("S32_LE", command)
        self.assertIn("48000", command)
        self.assertEqual(command[-1], "/tmp/tape.wav")

    def test_capture_profile_accepts_another_direct_hardware_device(self):
        command = CaptureProfile(device="hw:USB,2").command(Path("/tmp/tape.wav"))
        self.assertEqual(command[:3], ["arecord", "-D", "hw:USB,2"])

    def test_capture_device_list_is_parsed_into_direct_hw_addresses(self):
        output = """\
**** List of CAPTURE Hardware Devices ****
card 2: BlackmagicIO [Blackmagic Intensity Pro 4K], device 0: Blackmagic Audio [Blackmagic Audio]
  Subdevices: 1/1
card 3: USB [USB Audio], device 1: USB Audio [USB Audio]
"""
        devices = parse_capture_devices(output)
        self.assertEqual(
            [(device.pcm, device.name) for device in devices],
            [
                (
                    "hw:BlackmagicIO,0",
                    "Blackmagic Intensity Pro 4K / Blackmagic Audio",
                ),
                ("hw:USB,1", "USB Audio"),
            ],
        )

    def test_blackmagic_is_always_the_first_default_device(self):
        detected = parse_capture_devices(
            "card 3: USB [USB Audio], device 1: USB Audio [USB Audio]"
        )
        devices = capture_devices_with_default(detected)
        self.assertEqual(devices[0], DEFAULT_CAPTURE_DEVICE)
        self.assertEqual(devices[1].pcm, "hw:USB,1")

    def test_detected_blackmagic_description_is_kept_and_marked_default(self):
        detected = parse_capture_devices(
            "card 2: BlackmagicIO [Intensity Pro 4K], "
            "device 0: Blackmagic Audio [Blackmagic Audio]"
        )
        devices = capture_devices_with_default(detected)
        self.assertEqual(devices[0].pcm, "hw:BlackmagicIO,0")
        self.assertEqual(
            devices[0].name,
            "Intensity Pro 4K / Blackmagic Audio (default)",
        )

    def test_recording_path_is_readable_and_timestamped(self):
        path = recording_path(Path("/audio"), "Cassette 001 / Side A", datetime(2026, 8, 31, 19, 35))
        self.assertEqual(path, Path("/audio/Cassette_001_Side_A_2026-08-31_193500.wav"))

    def test_safe_stem_has_nonempty_fallback(self):
        self.assertEqual(safe_stem(" /// "), "Cassette001_SideA")

    def test_ensure_directory_creates_missing_parents(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "recordings" / "cassettes"
            self.assertEqual(ensure_directory(destination), destination)
            self.assertTrue(destination.is_dir())

    def test_ensure_directory_rejects_an_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "recordings"
            destination.write_text("not a directory")
            with self.assertRaises(FileExistsError):
                ensure_directory(destination)

    def test_parse_stereo_vu_uses_latest_complete_value(self):
        self.assertEqual(parse_vu(" 12%| 34%\r 56%|78%"), (56, 78))
        self.assertIsNone(parse_vu("Recording WAVE"))

    def test_dbfs_conversion(self):
        self.assertEqual(percent_to_dbfs(100), 0.0)
        self.assertAlmostEqual(percent_to_dbfs(50), -6.0206, places=3)
        self.assertTrue(math.isinf(percent_to_dbfs(0)))

    def test_xrun_count_is_case_insensitive(self):
        self.assertEqual(count_xruns("overrun!!! (at least 1.0 ms long)\nXRUN"), 2)

    def test_requested_sigint_exit_one_is_clean(self):
        self.assertTrue(capture_exit_was_clean(1, stop_requested=True))
        self.assertTrue(capture_exit_was_clean(-signal.SIGINT, stop_requested=True))
        self.assertTrue(
            capture_exit_was_clean(128 + signal.SIGINT, stop_requested=True)
        )

    def test_unsolicited_failure_is_not_clean(self):
        self.assertTrue(capture_exit_was_clean(0, stop_requested=False))
        self.assertFalse(capture_exit_was_clean(1, stop_requested=False))
        self.assertFalse(capture_exit_was_clean(2, stop_requested=True))


if __name__ == "__main__":
    unittest.main()
