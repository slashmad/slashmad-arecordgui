from __future__ import annotations

import os
import math
from pathlib import Path
import signal
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from .auto_stop import AnalysisUpdate, AutoStopConfig, GrowingWavAnalyzer
from .core import (
    CaptureDevice,
    CaptureProfile,
    capture_devices_with_default,
    capture_exit_was_clean,
    count_xruns,
    ensure_directory,
    free_bytes,
    parse_vu,
    parse_capture_devices,
    percent_to_dbfs,
    recording_path,
)


MIN_FREE_BYTES = 10 * 1024**3
AUTO_STOP_COUNTDOWN_SECONDS = 15
APPLICATION_ID = "se.slashmad.arecordgui"
ICON_FILENAME = "slashmad-arecordgui-icon.png"
SOURCE_LOGO_FILENAME = "slashmad-arecordgui-logo.png"
SYSTEM_ICON_PATH = (
    Path("/usr/share/icons/hicolor/512x512/apps") / f"{APPLICATION_ID}.png"
)
TITLE_ICON_SIZE = 40
TITLE_ICON_CSS = """
menubutton.title-icon-menu > button {
    min-width: 40px;
    min-height: 40px;
    padding: 0;
}
"""


def format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.2f} {unit}"
        number /= 1024
    raise AssertionError("unreachable")


def find_icon_path() -> Path | None:
    candidates = (
        Path(__file__).with_name(ICON_FILENAME),
        Path(__file__).resolve().parents[2] / ICON_FILENAME,
        SYSTEM_ICON_PATH,
        Path(__file__).with_name(SOURCE_LOGO_FILENAME),
        Path(__file__).resolve().parents[2] / SOURCE_LOGO_FILENAME,
    )
    return next((path for path in candidates if path.is_file()), None)


class RecorderWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application):
        super().__init__(application=application, title="slashmad arecord GUI")
        self.set_default_size(680, 500)
        self.profile = CaptureProfile()
        self.capture_devices: list[CaptureDevice] = list(
            capture_devices_with_default(())
        )
        self.process: subprocess.Popen[bytes] | None = None
        self.destination: Path | None = None
        self.completed_destination: Path | None = None
        self.media: Gtk.MediaFile | None = None
        self.auto_stop_analyzer: GrowingWavAnalyzer | None = None
        self.auto_stop_deadline: float | None = None
        self.stop_requested = False
        self.auto_stop_requested = False
        self.started_at = 0.0
        self.xruns = 0
        self.clipped = False
        self.stderr_tail = ""
        self.icon_path = find_icon_path()
        self.title_menu_button: Gtk.MenuButton | None = None

        title_icon_styles = Gtk.CssProvider()
        title_icon_styles.load_from_string(TITLE_ICON_CSS)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(),
            title_icon_styles,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.title_icon_styles = title_icon_styles

        title_bar = Gtk.HeaderBar()
        title_bar.set_show_title_buttons(True)
        title_bar.set_decoration_layout(":minimize,maximize,close")
        if self.icon_path:
            logo_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(self.icon_path),
                TITLE_ICON_SIZE,
                TITLE_ICON_SIZE,
                True,
            )
            logo = Gtk.Picture.new_for_pixbuf(logo_pixbuf)
            logo.set_can_shrink(False)
            self.title_menu_button = Gtk.MenuButton()
            self.title_menu_button.add_css_class("title-icon-menu")
            self.title_menu_button.set_child(logo)
            self.title_menu_button.set_has_frame(False)
            self.title_menu_button.set_tooltip_text("Application menu")
            self.title_menu_button.set_menu_model(self._create_title_menu())
            title_bar.pack_start(self.title_menu_button)
        title_label = Gtk.Label(label="slashmad arecord GUI")
        title_label.add_css_class("heading")
        title_bar.set_title_widget(title_label)
        self.set_titlebar(title_bar)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(18); root.set_margin_bottom(18)
        root.set_margin_start(18); root.set_margin_end(18)
        self.set_child(root)

        form = Gtk.Grid(column_spacing=10, row_spacing=10)
        root.append(form)
        form.attach(Gtk.Label(label="Device", xalign=1), 0, 0, 1, 1)
        self.device_model = Gtk.StringList.new(
            [device.display_label for device in self.capture_devices]
        )
        self.device_dropdown = Gtk.DropDown(model=self.device_model)
        self.device_dropdown.set_hexpand(True)
        form.attach(self.device_dropdown, 1, 0, 1, 1)
        self.refresh_devices_button = Gtk.Button(label="Refresh")
        form.attach(self.refresh_devices_button, 2, 0, 1, 1)
        form.attach(Gtk.Label(label="Name", xalign=1), 0, 1, 1, 1)
        self.name_entry = Gtk.Entry(text="Cassette001_SideA")
        form.attach(self.name_entry, 1, 1, 2, 1)
        form.attach(Gtk.Label(label="Folder", xalign=1), 0, 2, 1, 1)
        self.folder_entry = Gtk.Entry(text=str(Path.home() / "Music"))
        self.folder_entry.set_hexpand(True)
        form.attach(self.folder_entry, 1, 2, 1, 1)
        self.browse_button = Gtk.Button(label="Browse…")
        form.attach(self.browse_button, 2, 2, 1, 1)

        self.levels: list[Gtk.LevelBar] = []
        self.level_labels: list[Gtk.Label] = []
        for row, channel in enumerate(("L", "R"), start=3):
            form.attach(Gtk.Label(label=channel), 0, row, 1, 1)
            level = Gtk.LevelBar(min_value=0, max_value=100)
            level.set_hexpand(True)
            form.attach(level, 1, row, 1, 1)
            label = Gtk.Label(label="−∞ dBFS", width_chars=11, xalign=1)
            form.attach(label, 2, row, 1, 1)
            self.levels.append(level); self.level_labels.append(label)

        self.stats = Gtk.Label(label="Time 00:00:00  ·  Size 0 B  ·  XRUN 0", xalign=0)
        self.file_label = Gtk.Label(label="No file selected", xalign=0, ellipsize=3)
        self.status = Gtk.Label(label="Ready", xalign=0, wrap=True)
        root.append(self.file_label); root.append(self.stats); root.append(self.status)

        auto_stop = Gtk.Expander(label="Automatic tape-end stop (experimental)")
        auto_stop.set_expanded(False)
        root.append(auto_stop)
        auto_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        auto_box.set_margin_top(8)
        auto_stop.set_child(auto_box)

        self.auto_stop_check = Gtk.CheckButton(
            label="Enable automatic stop for the next recording"
        )
        auto_box.append(self.auto_stop_check)
        auto_grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        auto_box.append(auto_grid)
        auto_grid.attach(Gtk.Label(label="Not before", xalign=1), 0, 0, 1, 1)
        self.auto_stop_earliest = Gtk.SpinButton.new_with_range(0, 180, 1)
        self.auto_stop_earliest.set_value(30)
        auto_grid.attach(self.auto_stop_earliest, 1, 0, 1, 1)
        auto_grid.attach(Gtk.Label(label="minutes", xalign=0), 2, 0, 1, 1)
        auto_grid.attach(Gtk.Label(label="Low activity for", xalign=1), 0, 1, 1, 1)
        self.auto_stop_silence = Gtk.SpinButton.new_with_range(5, 600, 5)
        self.auto_stop_silence.set_value(60)
        auto_grid.attach(self.auto_stop_silence, 1, 1, 1, 1)
        auto_grid.attach(Gtk.Label(label="seconds", xalign=0), 2, 1, 1, 1)
        auto_grid.attach(Gtk.Label(label="Threshold", xalign=1), 0, 2, 1, 1)
        self.auto_stop_threshold = Gtk.SpinButton.new_with_range(-90, -20, 1)
        self.auto_stop_threshold.set_value(-60)
        auto_grid.attach(self.auto_stop_threshold, 1, 2, 1, 1)
        auto_grid.attach(Gtk.Label(label="dBFS", xalign=0), 2, 2, 1, 1)
        self.auto_stop_status = Gtk.Label(
            label="Auto-stop is off.", xalign=0, wrap=True
        )
        auto_box.append(self.auto_stop_status)
        self.disable_auto_stop_button = Gtk.Button(
            label="Disable auto-stop", sensitive=False
        )
        auto_box.append(self.disable_auto_stop_button)

        buttons = Gtk.Box(spacing=8)
        root.append(buttons)
        self.probe_button = Gtk.Button(label="Check device")
        self.record_button = Gtk.Button(label="● RECORD")
        self.record_button.add_css_class("suggested-action")
        self.stop_button = Gtk.Button(label="■ STOP", sensitive=False)
        self.play_button = Gtk.Button(label="▶ PLAY", sensitive=False)
        buttons.append(self.probe_button); buttons.append(self.record_button)
        buttons.append(self.stop_button); buttons.append(self.play_button)
        self.browse_button.connect("clicked", self._browse)
        self.device_dropdown.connect("notify::selected", self._device_selected)
        self.refresh_devices_button.connect("clicked", self._refresh_devices)
        self.probe_button.connect("clicked", self._probe)
        self.record_button.connect("clicked", self._start)
        self.stop_button.connect("clicked", self._stop)
        self.play_button.connect("clicked", self._play)
        self.auto_stop_check.connect("toggled", self._auto_stop_toggled)
        self.disable_auto_stop_button.connect("clicked", self._disable_auto_stop)
        self.connect("close-request", self._close_requested)
        self._auto_stop_toggled(self.auto_stop_check)
        self._refresh_devices(None)
        GLib.timeout_add(250, self._tick)

    def _create_title_menu(self) -> Gio.Menu:
        actions = (
            ("minimize", self._minimize_window),
            ("maximize-restore", self._maximize_restore_window),
            ("about", self._show_about),
            ("close", self._close_from_menu),
        )
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        menu = Gio.Menu()
        window_section = Gio.Menu()
        window_section.append("Minimize", "win.minimize")
        window_section.append("Maximize / Restore", "win.maximize-restore")
        menu.append_section(None, window_section)
        application_section = Gio.Menu()
        application_section.append("About", "win.about")
        application_section.append("Close", "win.close")
        menu.append_section(None, application_section)
        return menu

    def _minimize_window(
        self,
        _action: Gio.SimpleAction,
        _parameter: GLib.Variant | None,
    ) -> None:
        self.minimize()

    def _maximize_restore_window(
        self,
        _action: Gio.SimpleAction,
        _parameter: GLib.Variant | None,
    ) -> None:
        if self.is_maximized():
            self.unmaximize()
        else:
            self.maximize()

    def _show_about(
        self,
        _action: Gio.SimpleAction,
        _parameter: GLib.Variant | None,
    ) -> None:
        dialog = Gtk.AboutDialog(
            transient_for=self,
            modal=True,
            program_name="slashmad arecord GUI",
            version="0.1.0",
            comments="Direct ALSA recording frontend",
            license_type=Gtk.License.GPL_3_0,
        )
        if self.icon_path:
            dialog.set_logo(Gdk.Texture.new_from_filename(str(self.icon_path)))
        dialog.present()

    def _close_from_menu(
        self,
        _action: Gio.SimpleAction,
        _parameter: GLib.Variant | None,
    ) -> None:
        self.close()

    def _device_selected(
        self,
        dropdown: Gtk.DropDown,
        _parameter: object,
    ) -> None:
        if self.process:
            return
        selected = dropdown.get_selected()
        if selected >= len(self.capture_devices):
            return
        device = self.capture_devices[selected]
        self.profile = CaptureProfile(device=device.pcm)
        self.status.set_text(f"Capture device selected: {device.pcm}")

    def _refresh_devices(self, _button: Gtk.Button | None) -> None:
        if self.process:
            return
        selected_pcm = self.profile.device
        self.device_dropdown.set_sensitive(False)
        self.refresh_devices_button.set_sensitive(False)
        self.status.set_text("Scanning ALSA hardware capture devices…")
        threading.Thread(
            target=self._device_list_worker,
            args=(selected_pcm,),
            daemon=True,
        ).start()

    def _device_list_worker(self, selected_pcm: str) -> None:
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        try:
            result = subprocess.run(
                ["arecord", "-l"],
                capture_output=True,
                timeout=5,
                env=environment,
            )
            detected = parse_capture_devices(
                result.stdout.decode(errors="replace")
            )
            devices = capture_devices_with_default(detected)
            if detected:
                message = f"Found {len(detected)} ALSA hardware capture device(s)."
            else:
                message = (
                    "No ALSA hardware capture devices were reported; "
                    "BlackmagicIO remains the configured default."
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            devices = capture_devices_with_default(())
            message = f"Could not list ALSA devices: {exc}"
        GLib.idle_add(
            self._device_list_done,
            devices,
            selected_pcm,
            message,
        )

    def _device_list_done(
        self,
        devices: tuple[CaptureDevice, ...],
        selected_pcm: str,
        message: str,
    ) -> bool:
        if self.process:
            return GLib.SOURCE_REMOVE
        self.capture_devices = list(devices)
        self.device_model = Gtk.StringList.new(
            [device.display_label for device in self.capture_devices]
        )
        self.device_dropdown.set_model(self.device_model)
        selected = next(
            (
                index
                for index, device in enumerate(self.capture_devices)
                if device.pcm == selected_pcm
            ),
            0,
        )
        self.device_dropdown.set_selected(selected)
        self._device_selected(self.device_dropdown, None)
        self.device_dropdown.set_sensitive(True)
        self.refresh_devices_button.set_sensitive(True)
        self.status.set_text(message)
        return GLib.SOURCE_REMOVE

    def _set_controls(self, recording: bool) -> None:
        self.probe_button.set_sensitive(not recording)
        self.device_dropdown.set_sensitive(not recording)
        self.refresh_devices_button.set_sensitive(not recording)
        self.record_button.set_sensitive(not recording)
        self.name_entry.set_sensitive(not recording)
        self.folder_entry.set_sensitive(not recording)
        self.browse_button.set_sensitive(not recording)
        self.stop_button.set_sensitive(recording)
        playable = (
            not recording
            and self.completed_destination is not None
            and self.completed_destination.is_file()
        )
        self.play_button.set_sensitive(playable)
        self.auto_stop_check.set_sensitive(not recording)
        settings_sensitive = not recording and self.auto_stop_check.get_active()
        self.auto_stop_earliest.set_sensitive(settings_sensitive)
        self.auto_stop_silence.set_sensitive(settings_sensitive)
        self.auto_stop_threshold.set_sensitive(settings_sensitive)
        self.disable_auto_stop_button.set_sensitive(
            recording and self.auto_stop_analyzer is not None
        )

    def _auto_stop_toggled(self, button: Gtk.CheckButton) -> None:
        if self.process:
            return
        enabled = button.get_active()
        self.auto_stop_earliest.set_sensitive(enabled)
        self.auto_stop_silence.set_sensitive(enabled)
        self.auto_stop_threshold.set_sensitive(enabled)
        self.auto_stop_status.set_text(
            "Auto-stop is enabled for the next recording."
            if enabled else "Auto-stop is off."
        )

    def _browse(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Choose recording folder")
        initial = Path(self.folder_entry.get_text()).expanduser()
        while not initial.exists() and initial != initial.parent:
            initial = initial.parent
        if initial.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(initial)))
        dialog.select_folder(self, None, self._folder_selected)

    def _folder_selected(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error as exc:
            if not exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                self.status.set_text(f"Folder selection failed: {exc.message}")
            return
        path = folder.get_path()
        if path:
            self.folder_entry.set_text(path)
            self.status.set_text("Recording folder selected.")

    def _probe(self, _button: Gtk.Button) -> None:
        self.probe_button.set_sensitive(False)
        self.record_button.set_sensitive(False)
        self.device_dropdown.set_sensitive(False)
        self.refresh_devices_button.set_sensitive(False)
        profile = self.profile
        self.status.set_text(f"Checking ALSA device {profile.device}…")
        threading.Thread(
            target=self._probe_worker,
            args=(profile,),
            daemon=True,
        ).start()

    def _probe_worker(self, profile: CaptureProfile) -> None:
        try:
            result = subprocess.run(profile.probe_command(), capture_output=True, timeout=5)
            detail = result.stderr.decode(errors="replace").strip().splitlines()
            message = f"{profile.device} supports the capture profile." if result.returncode == 0 else (detail[-1] if detail else f"{profile.device} could not be opened.")
        except (OSError, subprocess.TimeoutExpired) as exc:
            message = f"Device check failed: {exc}"
        GLib.idle_add(self._probe_done, message)

    def _probe_done(self, message: str) -> bool:
        self.status.set_text(message)
        self.probe_button.set_sensitive(True)
        self.record_button.set_sensitive(True)
        self.device_dropdown.set_sensitive(True)
        self.refresh_devices_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _start(self, _button: Gtk.Button) -> None:
        try:
            folder = ensure_directory(Path(self.folder_entry.get_text()))
        except OSError as exc:
            self.status.set_text(f"Could not create the recording folder: {exc}")
            return
        if not os.access(folder, os.W_OK):
            self.status.set_text("The recording folder is not writable.")
            return
        available = free_bytes(folder)
        if available < MIN_FREE_BYTES:
            self.status.set_text(f"Not enough free space: {format_bytes(available)} (at least 10 GiB required).")
            return
        destination = recording_path(folder, self.name_entry.get_text())
        if destination.exists():
            self.status.set_text("The destination already exists; nothing was overwritten.")
            return
        self._stop_playback()
        self.completed_destination = None
        try:
            self.process = subprocess.Popen(
                self.profile.command(destination), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True,
            )
        except OSError as exc:
            self.status.set_text(f"Could not start arecord: {exc}")
            return
        self.destination = destination
        self.stop_requested = False
        self.auto_stop_requested = False
        self.started_at = time.monotonic(); self.xruns = 0; self.clipped = False; self.stderr_tail = ""
        self.file_label.set_text(str(destination))
        self.status.set_text("Recording directly from ALSA…")
        self._set_controls(True)
        if self.auto_stop_check.get_active():
            config = AutoStopConfig(
                earliest_seconds=self.auto_stop_earliest.get_value() * 60,
                silence_seconds=self.auto_stop_silence.get_value(),
                threshold_dbfs=self.auto_stop_threshold.get_value(),
            )
            self._start_auto_stop(destination, config)
        threading.Thread(target=self._read_stderr, args=(self.process,), daemon=True).start()
        threading.Thread(target=self._wait_process, args=(self.process,), daemon=True).start()

    def _start_auto_stop(self, destination: Path, config: AutoStopConfig) -> None:
        self._stop_auto_stop_analyzer()

        analyzer: GrowingWavAnalyzer

        def on_update(update: AnalysisUpdate) -> None:
            GLib.idle_add(self._handle_auto_stop_update, analyzer, update)

        def on_error(message: str) -> None:
            GLib.idle_add(self._handle_auto_stop_error, analyzer, message)

        analyzer = GrowingWavAnalyzer(
            destination,
            config,
            on_update,
            on_error,
            sample_rate=self.profile.rate,
        )
        self.auto_stop_analyzer = analyzer
        self.auto_stop_deadline = None
        self.auto_stop_status.set_text(
            "Auto-stop: waiting for tape activity; threshold "
            f"{config.threshold_dbfs:.0f} dBFS."
        )
        self.disable_auto_stop_button.set_sensitive(True)
        analyzer.start()

    def _handle_auto_stop_update(
        self,
        analyzer: GrowingWavAnalyzer,
        update: AnalysisUpdate,
    ) -> bool:
        if analyzer is not self.auto_stop_analyzer or not self.process:
            return GLib.SOURCE_REMOVE

        detector = update.detector
        config = analyzer.detector.config
        levels = (
            f"L {self._format_dbfs(update.left_dbfs)} · "
            f"R {self._format_dbfs(update.right_dbfs)}"
        )
        if detector.candidate_cancelled:
            self.auto_stop_deadline = None

        if detector.state == "waiting_for_activity":
            detail = "waiting for tape activity"
        elif detector.state == "waiting_for_window":
            minutes = config.earliest_seconds / 60
            detail = f"activity seen; detection starts at {minutes:g} min"
        elif detector.state == "listening":
            detail = "tape activity detected"
        elif detector.state == "silence":
            detail = (
                f"low activity {detector.silence_seconds:.0f}/"
                f"{config.silence_seconds:g} s"
            )
        else:
            if detector.candidate_started and self.auto_stop_deadline is None:
                self.auto_stop_deadline = (
                    time.monotonic() + AUTO_STOP_COUNTDOWN_SECONDS
                )
            remaining = self._auto_stop_countdown_remaining()
            detail = f"possible tape end; stopping in {remaining} s"

        self.auto_stop_status.set_text(f"Auto-stop: {detail} · {levels}")
        return GLib.SOURCE_REMOVE

    def _handle_auto_stop_error(
        self,
        analyzer: GrowingWavAnalyzer,
        message: str,
    ) -> bool:
        if analyzer is not self.auto_stop_analyzer:
            return GLib.SOURCE_REMOVE
        analyzer.stop()
        self.auto_stop_analyzer = None
        self.auto_stop_deadline = None
        self.disable_auto_stop_button.set_sensitive(False)
        self.auto_stop_status.set_text(
            f"Auto-stop disabled after an analysis error: {message}"
        )
        return GLib.SOURCE_REMOVE

    def _disable_auto_stop(self, _button: Gtk.Button) -> None:
        self._stop_auto_stop_analyzer()
        self.auto_stop_check.set_active(False)
        self.auto_stop_status.set_text(
            "Auto-stop was disabled; recording continues normally."
        )

    def _stop_auto_stop_analyzer(self) -> None:
        if self.auto_stop_analyzer:
            self.auto_stop_analyzer.stop()
            self.auto_stop_analyzer = None
        self.auto_stop_deadline = None
        self.disable_auto_stop_button.set_sensitive(False)

    @staticmethod
    def _format_dbfs(value: float) -> str:
        return "−∞ dBFS" if math.isinf(value) else f"{value:.1f} dBFS"

    def _auto_stop_countdown_remaining(self) -> int:
        if self.auto_stop_deadline is None:
            return AUTO_STOP_COUNTDOWN_SECONDS
        return max(0, math.ceil(self.auto_stop_deadline - time.monotonic()))

    def _read_stderr(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        while chunk := process.stderr.read(256):
            GLib.idle_add(self._handle_output, chunk.decode(errors="replace"))

    def _handle_output(self, text: str) -> bool:
        combined = self.stderr_tail + text
        self.stderr_tail = combined[-1000:]
        vu = parse_vu(combined)
        if vu:
            for index, percent in enumerate(vu):
                self.levels[index].set_value(percent)
                db = percent_to_dbfs(percent)
                self.level_labels[index].set_text("−∞ dBFS" if db == float("-inf") else f"{db:.1f} dBFS")
                self.clipped |= percent >= 100
        self.xruns += count_xruns(text)
        return GLib.SOURCE_REMOVE

    def _stop(self, _button: Gtk.Button) -> None:
        self._begin_stop(auto_stop_requested=False)

    def _begin_stop(self, *, auto_stop_requested: bool) -> None:
        if self.process and self.process.poll() is None:
            self._stop_auto_stop_analyzer()
            self.stop_button.set_sensitive(False)
            self.status.set_text(
                "Tape-end condition sustained; stopping and finalizing the WAV file…"
                if auto_stop_requested
                else "Stopping and finalizing the WAV file…"
            )
            os.killpg(self.process.pid, signal.SIGINT)
            self.stop_requested = True
            self.auto_stop_requested = auto_stop_requested

    def _wait_process(self, process: subprocess.Popen[bytes]) -> None:
        returncode = process.wait()
        GLib.idle_add(self._process_done, process, returncode)

    def _process_done(self, process: subprocess.Popen[bytes], returncode: int) -> bool:
        if process is not self.process:
            return GLib.SOURCE_REMOVE
        auto_stop_was_requested = self.auto_stop_requested
        self._stop_auto_stop_analyzer()
        if auto_stop_was_requested:
            self.auto_stop_status.set_text("Auto-stop: recording ended.")
        stopped_cleanly = capture_exit_was_clean(
            returncode,
            self.stop_requested,
        )
        completed_file = (
            self.destination
            if self.destination and self.destination.is_file()
            else None
        )
        if stopped_cleanly and completed_file:
            self.completed_destination = completed_file
            self.status.set_text("Recording saved." + (" CLIPPING was detected." if self.clipped else ""))
        elif stopped_cleanly:
            self.status.set_text("Recording failed: the completed WAV file is missing.")
        else:
            last = self.stderr_tail.replace("\r", "\n").strip().splitlines()
            detail = last[-1] if last else f"exit code {returncode}"
            self.status.set_text(f"Recording failed: {detail}")
        self.process = None
        self.stop_requested = False
        self.auto_stop_requested = False
        self._set_controls(False)
        return GLib.SOURCE_REMOVE

    def _play(self, _button: Gtk.Button) -> None:
        if not self.completed_destination or not self.completed_destination.is_file():
            self.play_button.set_sensitive(False)
            self.status.set_text("No completed recording is available for playback.")
            return
        if self.media and self.media.get_playing():
            self.media.pause()
            self.play_button.set_label("▶ PLAY")
            self.status.set_text("Playback paused.")
            return
        if self.media is None:
            self.media = Gtk.MediaFile.new_for_filename(str(self.completed_destination))
            self.media.connect("notify::ended", self._media_state_changed)
            self.media.connect("notify::error", self._media_state_changed)
        self.media.play()
        self.play_button.set_label("❚❚ PAUSE")
        self.status.set_text("Playing the completed recording…")

    def _media_state_changed(self, media: Gtk.MediaFile, _parameter: object) -> None:
        error = media.get_error()
        if error:
            self.play_button.set_label("▶ PLAY")
            self.status.set_text(f"Playback failed: {error.message}")
        elif media.get_ended():
            self.play_button.set_label("▶ PLAY")
            self.status.set_text("Playback finished.")

    def _stop_playback(self) -> None:
        if self.media:
            self.media.pause()
            self.media.clear()
            self.media = None
        self.play_button.set_label("▶ PLAY")

    def _tick(self) -> bool:
        if self.auto_stop_deadline is not None and self.process:
            remaining = self._auto_stop_countdown_remaining()
            if remaining <= 0:
                self._begin_stop(auto_stop_requested=True)
            else:
                self.auto_stop_status.set_text(
                    "Auto-stop: possible tape end; "
                    f"stopping in {remaining} s. Use Disable auto-stop to cancel."
                )
        if self.process:
            seconds = max(0, int(time.monotonic() - self.started_at))
            hours, rest = divmod(seconds, 3600); minutes, seconds = divmod(rest, 60)
            size = self.destination.stat().st_size if self.destination and self.destination.exists() else 0
            clip = "  ·  CLIP" if self.clipped else ""
            self.stats.set_text(f"Time {hours:02d}:{minutes:02d}:{seconds:02d}  ·  Size {format_bytes(size)}  ·  XRUN {self.xruns}{clip}")
        return GLib.SOURCE_CONTINUE

    def _close_requested(self, _window: Gtk.Window) -> bool:
        if self.process and self.process.poll() is None:
            self.status.set_text("Stop the recording before closing the window.")
            return True
        self._stop_auto_stop_analyzer()
        self._stop_playback()
        return False


class RecorderApplication(Gtk.Application):
    def __init__(self) -> None:
        GLib.set_application_name("slashmad arecord GUI")
        super().__init__(application_id=APPLICATION_ID)

    def do_activate(self) -> None:
        window = self.props.active_window or RecorderWindow(self)
        window.present()


def main() -> int:
    return RecorderApplication().run(None)
