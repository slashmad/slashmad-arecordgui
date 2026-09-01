Name:           slashmad-arecordgui
Version:        0.1.0
Release:        2%{?dist}
Summary:        Direct ALSA recording frontend

License:        GPL-3.0-or-later
URL:            https://github.com/slashmad/slashmad-arecordgui
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils
BuildRequires:  appstream
BuildRequires:  ImageMagick

Requires:       alsa-utils
Requires:       python3-gobject
Requires:       gtk4
Requires:       gstreamer1-plugins-base

%description
slashmad arecord GUI is a GTK4 frontend for recording uncompressed stereo
audio through an explicitly selected direct ALSA hardware PCM. BlackmagicIO
remains the default capture device.

%prep
%autosetup

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files arecordgui

install -Dpm 0644 data/se.slashmad.arecordgui.desktop \
    %{buildroot}%{_datadir}/applications/se.slashmad.arecordgui.desktop
install -Dpm 0644 data/se.slashmad.arecordgui.metainfo.xml \
    %{buildroot}%{_metainfodir}/se.slashmad.arecordgui.metainfo.xml
install -dm 0755 \
    %{buildroot}%{_datadir}/icons/hicolor/512x512/apps
magick slashmad-arecordgui-icon.png \
    -filter Lanczos -resize 512x512 -strip \
    %{buildroot}%{_datadir}/icons/hicolor/512x512/apps/se.slashmad.arecordgui.png
chmod 0644 \
    %{buildroot}%{_datadir}/icons/hicolor/512x512/apps/se.slashmad.arecordgui.png

%check
PYTHONPATH=src %{python3} -m unittest discover -s tests -v
desktop-file-validate \
    %{buildroot}%{_datadir}/applications/se.slashmad.arecordgui.desktop
appstreamcli validate --no-net --strict \
    %{buildroot}%{_metainfodir}/se.slashmad.arecordgui.metainfo.xml

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/slashmad-arecordgui
%{_datadir}/applications/se.slashmad.arecordgui.desktop
%{_metainfodir}/se.slashmad.arecordgui.metainfo.xml
%{_datadir}/icons/hicolor/512x512/apps/se.slashmad.arecordgui.png

%changelog
* Tue Sep 01 2026 slashmad - 0.1.0-2
- Avoid a duplicate installed title-bar icon
- Expose the application icon and project URLs through AppStream

* Tue Sep 01 2026 slashmad - 0.1.0-1
- Initial Fedora package
