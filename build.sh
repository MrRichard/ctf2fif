#!/usr/bin/env bash
# Build a standalone Linux executable of ctf2fif with PyInstaller.
#
# Usage:
#   ./build.sh            # single-file executable -> dist/ctf2fif
#   ONEFILE=0 ./build.sh  # one-dir bundle -> dist/ctf2fif/ctf2fif
#   COMPACT=1 ./build.sh  # extra size trimming (strips binaries, drops mne tests)
#
# Environment:
#   PY        Python interpreter to build with (default: .venv/bin/python)
#   ONEFILE   set to 0 to produce a one-dir bundle instead of a single file
#   COMPACT   set to 1 to strip binaries and exclude test-only imports
set -euo pipefail

cd "$(dirname "$0")"

PY="${PY:-.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
    echo "error: python interpreter not found at '$PY' (set PY=...)" >&2
    exit 1
fi

if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
    echo "error: PyInstaller not installed for '$PY'" >&2
    echo "       install build deps:  uv pip install --python '$PY' -r requirements-build.txt" >&2
    exit 1
fi

mkdir -p build dist

args=(
    --noconfirm
    --clean
    --onefile
    --name=ctf2fif
    --distpath=dist
    --workpath=build
    --specpath=build
    --collect-all=mne
    --copy-metadata=mne
)

if [[ "${ONEFILE:-1}" == "0" ]]; then
    args+=(
        --onedir
    )
fi

if [[ "${COMPACT:-0}" == "1" ]]; then
    # Strip native libraries and keep mne's test tree out of the bundle.
    args+=(
        --strip
        --exclude-module=mne.tests
    )
fi

echo "==> Building ctf2fif with PyInstaller $("$PY" -c 'import PyInstaller; print(PyInstaller.__version__)')"
"$PY" -m PyInstaller "${args[@]}" ctf2fif.py

cp fields.json dist/fields.json

if [[ "${ONEFILE:-1}" == "0" ]]; then
    echo "==> Built: dist/ctf2fif/ctf2fif"
else
    echo "==> Built: dist/ctf2fif"
fi
