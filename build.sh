#!/usr/bin/env bash
# Build a standalone Linux executable of ctf2fif with Nuitka.
#
# Usage:
#   ./build.sh            # standalone folder -> dist/ctf2fif.dist/
#   ONEFILE=1 ./build.sh  # single self-extracting file -> dist/ctf2fif.bin
#   COMPACT=1 ./build.sh  # extra size trimming (drops mne tests, uses LTO)
#
# Environment:
#   PY        Python interpreter to build with (default: .venv/bin/python)
#   ONEFILE   set to 1 to produce a single-file executable
#   COMPACT   set to 1 to enable LTO and strip test-only imports
#   JOBS      parallel compile jobs (default: nproc)
set -euo pipefail

cd "$(dirname "$0")"

PY="${PY:-.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
    echo "error: python interpreter not found at '$PY' (set PY=...)" >&2
    exit 1
fi

if ! "$PY" -c "import nuitka" 2>/dev/null; then
    echo "error: nuitka not installed for '$PY'" >&2
    echo "       install build deps:  uv pip install --python '$PY' -r requirements-build.txt" >&2
    exit 1
fi

args=(
    --standalone
    --assume-yes-for-downloads
    --enable-plugin=numpy
    --include-package=mne
    --include-package-data=mne
    --include-data-file=fields.json=fields.json
    --output-dir=dist
    --output-filename=ctf2fif
    --show-progress
    --jobs="${JOBS:-$(nproc)}"
)

if [[ "${ONEFILE:-0}" == "1" ]]; then
    args+=(--onefile)
fi

if [[ "${COMPACT:-0}" == "1" ]]; then
    # LTO lets gcc optimize across compiled modules; dropping mne's test
    # tree keeps a large amount of code and data out of the build.
    args+=(
        --lto=yes
        --nofollow-import-to=mne.tests
    )
fi

echo "==> Building ctf2fif with $("$PY" -m nuitka --version | head -1)"
"$PY" -m nuitka "${args[@]}" ctf2fif.py

if [[ "${ONEFILE:-0}" == "1" ]]; then
    # Nuitka names the onefile executable <output-filename>.bin on Linux.
    built="$(find dist -maxdepth 1 -name 'ctf2fif*' -type f -perm -u+x | head -1)"
    echo "==> Built: ${built:-dist/ctf2fif*}"
else
    echo "==> Built: dist/ctf2fif.dist/ctf2fif"
fi
