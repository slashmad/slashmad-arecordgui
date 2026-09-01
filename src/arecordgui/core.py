from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import re
import signal
import shutil


VU_RE = re.compile(r"(\d{1,3})%\s*\|\s*(\d{1,3})%")
XRUN_RE = re.compile(r"\b(?:xrun|overrun)\b", re.IGNORECASE)
CAPTURE_DEVICE_RE = re.compile(
    r"^\s*card\s+\d+:\s+(?P<card_id>\S+)\s+"
    r"\[(?P<card_name>[^]]*)\],\s+device\s+"
    r"(?P<device_number>\d+):\s+(?P<device_details>.+?)\s*$"
)


@dataclass(frozen=True)
class CaptureProfile:
    device: str = "hw:BlackmagicIO,0"
    sample_format: str = "S32_LE"
    rate: int = 48_000
    channels: int = 2

    def command(self, destination: Path) -> list[str]:
        return [
            "arecord",
            "-D", self.device,
            "-t", "wav",
            "-f", self.sample_format,
            "-r", str(self.rate),
            "-c", str(self.channels),
            "--vumeter=stereo",
            "--fatal-errors",
            "--", str(destination),
        ]

    def probe_command(self) -> list[str]:
        return [
            "arecord", "-D", self.device, "-f", self.sample_format,
            "-r", str(self.rate), "-c", str(self.channels),
            "--dump-hw-params", "-t", "raw", "-d", "1", "/dev/null",
        ]


@dataclass(frozen=True)
class CaptureDevice:
    pcm: str
    name: str

    @property
    def display_label(self) -> str:
        return f"{self.name} — {self.pcm}"


DEFAULT_CAPTURE_DEVICE = CaptureDevice(
    pcm="hw:BlackmagicIO,0",
    name="BlackmagicIO (default)",
)


def parse_capture_devices(output: str) -> tuple[CaptureDevice, ...]:
    """Parse the locale-neutral output from ``arecord -l``."""
    devices: list[CaptureDevice] = []
    seen: set[str] = set()
    for line in output.splitlines():
        match = CAPTURE_DEVICE_RE.match(line)
        if not match:
            continue
        pcm = (
            f"hw:{match.group('card_id')},"
            f"{match.group('device_number')}"
        )
        if pcm in seen:
            continue
        card_name = match.group("card_name").strip()
        device_name = re.sub(
            r"\s+\[[^]]*\]\s*$",
            "",
            match.group("device_details"),
        ).strip()
        name = card_name or match.group("card_id")
        if device_name and device_name.casefold() != name.casefold():
            name = f"{name} / {device_name}"
        devices.append(CaptureDevice(pcm=pcm, name=name))
        seen.add(pcm)
    return tuple(devices)


def capture_devices_with_default(
    detected: tuple[CaptureDevice, ...],
) -> tuple[CaptureDevice, ...]:
    """Keep Blackmagic first and available even if enumeration is unavailable."""
    default = next(
        (
            CaptureDevice(device.pcm, f"{device.name} (default)")
            for device in detected
            if device.pcm == DEFAULT_CAPTURE_DEVICE.pcm
        ),
        DEFAULT_CAPTURE_DEVICE,
    )
    return (default,) + tuple(
        device for device in detected if device.pcm != default.pcm
    )

def safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9ÅÄÖåäö_-]+", "_", value.strip())
    return cleaned.strip("_") or "Cassette001_SideA"


def recording_path(folder: Path, label: str, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    return folder / f"{safe_stem(label)}_{timestamp}.wav"


def ensure_directory(folder: Path) -> Path:
    folder = folder.expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")
    return folder


def free_bytes(folder: Path) -> int:
    return shutil.disk_usage(folder).free


def percent_to_dbfs(percent: int) -> float:
    if percent <= 0:
        return float("-inf")
    return 20.0 * math.log10(min(percent, 100) / 100.0)


def parse_vu(text: str) -> tuple[int, int] | None:
    matches = list(VU_RE.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    return min(int(match.group(1)), 100), min(int(match.group(2)), 100)


def count_xruns(text: str) -> int:
    return len(XRUN_RE.findall(text))


def capture_exit_was_clean(returncode: int, stop_requested: bool) -> bool:
    """Return whether arecord completed normally for this process lifecycle.

    alsa-utils arecord finalizes the WAV but returns EXIT_FAILURE (1) after its
    SIGINT handler runs.  That status is clean only when this application
    explicitly requested the signal; an unsolicited exit code 1 remains an
    error.
    """
    if returncode == 0:
        return True
    return stop_requested and returncode in (
        1,
        -signal.SIGINT,
        128 + signal.SIGINT,
    )
