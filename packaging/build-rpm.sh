#!/usr/bin/bash
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
package_name=slashmad-arecordgui
package_version=0.1.0
source_date_epoch=${SOURCE_DATE_EPOCH:-1788220800}
output_dir=${RPM_OUTPUT_DIR:-"$project_root/dist"}

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/${package_name}-rpm.XXXXXX")
trap 'rm -rf -- "$work_dir"' EXIT

top_dir="$work_dir/rpmbuild"
mkdir -p "$top_dir/BUILD" "$top_dir/BUILDROOT" "$top_dir/RPMS" \
    "$top_dir/SOURCES" "$top_dir/SPECS" "$top_dir/SRPMS"

archive="$top_dir/SOURCES/${package_name}-${package_version}.tar.gz"
(
    cd "$project_root"
    tar --sort=name \
        --mtime="@$source_date_epoch" \
        --owner=0 --group=0 --numeric-owner \
        --pax-option=delete=atime,delete=ctime \
        --transform="s,^,${package_name}-${package_version}/," \
        -cf - \
        LICENSE \
        README.md \
        pyproject.toml \
        slashmad-arecordgui-icon.png \
        slashmad-arecordgui-logo.png \
        data/se.slashmad.arecordgui.desktop \
        data/se.slashmad.arecordgui.metainfo.xml \
        src/arecordgui/__init__.py \
        src/arecordgui/__main__.py \
        src/arecordgui/app.py \
        src/arecordgui/auto_stop.py \
        src/arecordgui/core.py \
        tests/test_auto_stop.py \
        tests/test_core.py
) | gzip -n > "$archive"

rpmbuild -ba \
    --define "_topdir $top_dir" \
    --define "_sourcedir $top_dir/SOURCES" \
    "$project_root/packaging/slashmad-arecordgui.spec"

mkdir -p "$output_dir"
install -m 0644 "$top_dir"/RPMS/noarch/*.rpm "$output_dir/"
install -m 0644 "$top_dir"/SRPMS/*.src.rpm "$output_dir/"

printf 'Built RPM artifacts in %s\n' "$output_dir"
