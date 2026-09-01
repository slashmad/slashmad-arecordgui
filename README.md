# slashmad-arecordgui

Project homepage: <https://github.com/slashmad/slashmad-arecordgui>

A deliberately small GTK4 frontend for direct ALSA recording with `arecord`.
The first profile preserves the known-good Blackmagic capture path:

```text
arecord -D hw:BlackmagicIO,0 -t wav -f S32_LE -r 48000 -c 2 --vumeter=stereo FILE.wav
```

The GUI does not introduce PipeWire, PulseAudio, JACK, resampling, or a second
recording engine. It refuses to overwrite an existing recording and sends
SIGINT when stopping so `arecord` can finish the WAV file cleanly.

## Audio quality and file size

Blackmagic specifies the Intensity Pro 4K audio path as 48 kHz and 24-bit. The
known-good direct ALSA profile requests S32_LE, so `arecord` stores each sample
in a 32-bit PCM word without compression or resampling. This preserves all
24 bits supplied by the card; the wider storage word does not create additional
analog resolution.

The resulting stereo WAV data rate is 3,072 kbit/s, or about 1.38 GB
(1.29 GiB) per hour. File size is therefore determined by recording duration,
channel count, sample rate, and storage word width; a larger WAV is not by
itself evidence of higher quality. See Blackmagic's
[Intensity Pro 4K technical specifications](https://www.blackmagicdesign.com/products/intensitypro4k/techspecs).

Type a destination folder or choose one with **Browse…**. Missing destination
folders are created automatically when recording starts. After a recording has
stopped cleanly, **Play** provides local Play/Pause through GTK's media backend.

## Capture device

The device selector lists hardware capture PCMs reported by `arecord -l` and
shows the exact `hw:CARD,DEVICE` address used for recording. BlackmagicIO remains
the first and default choice even when device enumeration is temporarily
unavailable. **Refresh** rescans the list after hardware changes, and **Check
device** verifies the selected device against the fixed 48 kHz, stereo, S32_LE
capture profile before recording.

Only direct ALSA hardware devices are offered. Selecting another card never
introduces a fallback to PipeWire, PulseAudio, JACK, `default`, or `plughw`.
The selected device is locked while recording. The compact title bar contains
only the application name and a transparent, title-bar-filling
`slashmad-arecordgui-icon.png` menu button at the far left. Its menu provides
Minimize, Maximize/Restore, About, and Close; Close uses the normal protected
window-close path and cannot bypass an active recording. The future installer
will register the same image as the desktop and system application icon under
the matching `se.slashmad.arecordgui` application ID. The original
`slashmad-arecordgui-logo.png` source image is retained unchanged.

## Experimental tape-end auto-stop

Expand **Automatic tape-end stop (experimental)** and enable it before starting
a recording. The detector reads newly appended samples from the WAV file; it
never sits between `arecord` and the recording and cannot modify the file.

- **Not before** prevents earlier gaps on the same side from stopping capture.
  A useful starting point is 30 minutes for a C60 side or 45 minutes for a C90
  side.
- **Low activity for** is the continuous time below the configured threshold
  required before a stop countdown begins. The default is 60 seconds.
- **Threshold** separates the noise of moving blank tape from the deck's output
  after mechanical stop. The conservative initial value is -60 dBFS.

The panel displays live left/right RMS levels. During the first disposable test,
compare a normal inter-recording gap, moving blank tape or leader, and the deck
after auto-stop. Set the threshold between the moving-tape and stopped-deck
levels. After sustained low activity, a visible 15-second countdown begins.
Signal returning cancels it immediately; **Disable auto-stop** cancels the
feature while recording continues.

Leave the feature off if moving tape and the stopped deck cannot be separated
reliably. Auto-stop never trims the WAV and uses the same clean SIGINT stop path
as the manual **Stop** button.

## Run on Fedora 44

```bash
sudo dnf install alsa-utils gtk4 python3-gobject
PYTHONPATH=src python3 -m arecordgui
```

## Build the Fedora RPM

The RPM build requires Fedora's Python RPM macros, desktop-file utilities,
AppStream tools, ImageMagick, and standard RPM build tools. Build as a regular
user; do not run the build with `sudo`:

```bash
bash packaging/build-rpm.sh
```

The binary RPM and source RPM are written to `dist/`. The package installs the
launcher, AppStream metadata, and system icon together with the Python command.
DNF uses root privileges only for files below `/usr`; recordings and newly
created recording folders remain owned by the user running the application.

Use **Check device** before the first recording. This invokes
`arecord --dump-hw-params` without capturing audio. A real capture requires the
Blackmagic ALSA device; the test suite does not.

## Test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Current scope

- selectable direct-ALSA hardware device with BlackmagicIO as the default;
- fixed capture format (48 kHz, stereo, signed 32-bit LE);
- destination folder browser, automatic folder creation, and collision-safe
  generated filename;
- free-space check (10 GiB minimum by default);
- stereo peak display derived from `arecord`'s VU output;
- recording elapsed time, current WAV size, clipping latch, and XRUN count;
- clean stop and visible capture-process errors;
- Play/Pause for the most recent cleanly completed WAV file;
- optional, fail-closed experimental tape-end auto-stop with live RMS display,
  an earliest-stop guard, sustained-low-activity timer, and cancelable countdown.

Automatic splitting and launching REAPER are intentionally deferred until the
single-file capture path has been exercised with the real hardware.
