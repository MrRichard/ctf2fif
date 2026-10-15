#!/usr/bin/env python3
"""Convert CTF MEG .ds directories to FIF, optionally injecting header fields.

Fields to inject are defined in a JSON file (see fields.json). Each entry:
    field     (required) short name of the field
    pattern   (required) regular expression the value must fully match
    prompt    (optional) text shown to the user
    target    (optional) where the value goes in the FIF header:
                  proj_name, experimenter,
                  subject_info.<key>   (his_id, first_name, last_name, hand, sex)
                  device_info.<key>    (type, model, serial, site)
              Entries without a target are stored together as a JSON blob in
              info["description"] (so "description" is reserved for that).
    required  (optional, default true) if false, an empty answer skips the field

Errors go to STDOUT and into a uniquely named report file for each run.
"""
import argparse
import json
import logging
import os
import re
import shutil
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path

DIRECT_TARGETS = {"proj_name", "experimenter"}
NESTED_TARGETS = {
    "subject_info": {"his_id", "first_name", "last_name", "hand", "sex"},
    "device_info": {"type", "model", "serial", "site"},
}
INT_KEYS = {"hand", "sex"}

log = logging.getLogger("ctf2fif")


def setup_logging(report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = report_dir / f"ctf2fif_report_{stamp}_{uuid.uuid4().hex[:8]}.log"

    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(report, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    sh = logging.StreamHandler(sys.stdout)  # errors go to STDOUT, per spec
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(fh)
    log.addHandler(sh)
    return report


def load_field_defs(path: Path) -> list:
    with open(path, encoding="utf-8") as f:
        defs = json.load(f)
    if not isinstance(defs, list):
        raise ValueError("fields file must contain a JSON list")
    seen = set()
    for i, d in enumerate(defs):
        for key in ("field", "pattern"):
            if key not in d:
                raise ValueError(f"entry {i}: missing '{key}'")
        if d["field"] in seen:
            raise ValueError(f"duplicate field name '{d['field']}'")
        seen.add(d["field"])
        try:
            d["_re"] = re.compile(d["pattern"])
        except re.error as e:
            raise ValueError(f"entry '{d['field']}': bad regex: {e}")
        target = d.get("target")
        if target:
            if target == "description":
                raise ValueError(f"entry '{d['field']}': 'description' is reserved")
            if "." in target:
                parent, _, key = target.partition(".")
                if key not in NESTED_TARGETS.get(parent, set()):
                    raise ValueError(f"entry '{d['field']}': unknown target '{target}'")
            elif target not in DIRECT_TARGETS:
                raise ValueError(f"entry '{d['field']}': unknown target '{target}'")
    return defs


def collect_values(defs: list, presets: dict, interactive: bool, label: str) -> dict:
    """Return {field: value}. Presets (--set) are validated; others prompted."""
    values = {}
    for d in defs:
        name = d["field"]
        required = d.get("required", True)
        if name in presets:
            val = presets[name]
            if not d["_re"].fullmatch(val):
                raise ValueError(f"--set {name}={val!r} does not match /{d['pattern']}/")
            values[name] = val
            continue
        if not interactive:
            if required:
                raise ValueError(f"required field '{name}' not supplied (use --set {name}=...)")
            continue
        prompt = d.get("prompt", name)
        suffix = "" if required else " [optional, Enter to skip]"
        while True:
            val = input(f"[{label}] {prompt}{suffix}: ").strip()
            if not val and not required:
                break
            if d["_re"].fullmatch(val):
                values[name] = val
                break
            print(f"  Invalid. Must match: {d['pattern']}")
    return values


def apply_fields(info, defs: list, values: dict) -> None:
    custom = {}
    for d in defs:
        name = d["field"]
        if name not in values:
            continue
        val = values[name]
        target = d.get("target")
        if not target:
            custom[name] = val
        elif "." in target:
            parent, _, key = target.partition(".")
            current = info.get(parent) or {}
            current = dict(current)
            if parent == "device_info":
                current.setdefault("type", "CTF")
            current[key] = int(val) if key in INT_KEYS else val
            info[parent] = current
        else:
            info[target] = val
    if custom:
        info["description"] = json.dumps(custom, sort_keys=True)


def convert(ds: Path, out: Path, defs: list, presets: dict,
            interactive: bool, overwrite: bool) -> Path:
    if not ds.is_dir() or ds.suffix.lower() != ".ds":
        raise ValueError(f"{ds} is not a CTF .ds directory")
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} exists (use --overwrite)")

    values = collect_values(defs, presets, interactive, ds.name)
    log.debug("Field values for %s: %s", ds.name, values)

    import mne  # imported late so --help and validation work without MNE installed

    raw = mne.io.read_raw_ctf(str(ds), preload=False, verbose="WARNING")
    apply_fields(raw.info, defs, values)

    # Do the write in a scratch dir inside the destination directory, then move
    # the finished file(s) into place and always remove the scratch dir.
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out.parent / f".ctf2fif-tmp-{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir()
    try:
        written = [Path(f) for f in raw.save(str(tmp_dir / out.name),
                                             overwrite=True, verbose="WARNING")]
        os.replace(written[0], out)
        for extra in written[1:]:
            os.replace(extra, out.parent / extra.name)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    log.debug("Wrote %s (scratch dir %s)", out, tmp_dir)
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Convert CTF .ds datasets to FIF.")
    p.add_argument("inputs", nargs="+", type=Path, help="one or more .ds directories")
    p.add_argument("-o", "--out", type=Path,
                   help="output .fif path (requires a single input); "
                        "default: <stem>_raw.fif in the current directory")
    p.add_argument("-f", "--fields", type=Path, help="JSON file defining header fields")
    p.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                   help="preset a field value (validated); repeatable")
    p.add_argument("--no-prompt", action="store_true",
                   help="never prompt; fail if a required field is missing")
    p.add_argument("--overwrite", action="store_true", help="overwrite existing FIF files")
    p.add_argument("--report-dir", type=Path, default=Path("reports"),
                   help="where report logs are written (default: ./reports)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    report = setup_logging(args.report_dir)
    log.info("Report: %s", report)

    try:
        defs = load_field_defs(args.fields) if args.fields else []
        presets = {}
        for item in args.set:
            if "=" not in item:
                raise ValueError(f"--set expects FIELD=VALUE, got {item!r}")
            k, _, v = item.partition("=")
            presets[k] = v
        unknown = set(presets) - {d["field"] for d in defs}
        if unknown:
            raise ValueError(f"--set for undefined field(s): {', '.join(sorted(unknown))}")
        if args.out is not None and len(args.inputs) != 1:
            raise ValueError("--out requires exactly one input")
        (args.out.parent if args.out else Path(".")).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.error("Setup failed: %s", e)
        log.debug("Traceback:\n%s", traceback.format_exc())
        return 2

    interactive = sys.stdin.isatty() and not args.no_prompt
    ok, failed = 0, 0
    for ds in args.inputs:
        out_path = args.out if args.out is not None else Path(f"{ds.stem}_raw.fif")
        try:
            out = convert(ds, out_path, defs, presets, interactive, args.overwrite)
            log.info("OK: %s -> %s", ds, out)
            ok += 1
        except KeyboardInterrupt:
            log.error("Interrupted by user")
            return 130
        except Exception as e:
            log.error("FAILED: %s: %s: %s", ds, type(e).__name__, e)
            log.debug("Traceback:\n%s", traceback.format_exc())
            failed += 1

    log.info("Done: %d converted, %d failed. Report: %s", ok, failed, report)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
