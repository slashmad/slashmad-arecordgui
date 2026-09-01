# Fedora RPM packaging

Build the binary and source RPM without root privileges:

```bash
bash packaging/build-rpm.sh
```

The build uses a temporary RPM tree and writes the finished packages to
`dist/`. Set `RPM_OUTPUT_DIR` to use another destination. The source archive is
created from an explicit allowlist, so Git metadata, local project memory,
caches, build directories, and recordings are never included.

The package installs the application command, desktop entry, AppStream
metadata, and a 512x512 transparent system icon. DNF owns the installed files
under `/usr`; recordings remain owned by the desktop user who runs the
application.
