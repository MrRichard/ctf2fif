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

# QA comparison thresholds (see run_qa).
QA_PASS = 0.999          # median correlation required for PASS
QA_WARN = 0.99           # median correlation required for WARN
QA_LOW = 0.99            # per-channel correlation below this is "low"
QA_FLAT_REL = 1e-9       # channel std below this fraction of the median std is flat
QA_FLAT_ABS = 0.0        # absolute std floor for flat-channel detection

# Memory guards.
# CTF .ds files store data in trials; MNE sizes its read/write buffers after
# the trial length (res4 "nsamp"). A .ds written as a single long trial makes
# MNE load the whole recording as one buffer, which multiplies peak RAM by
# recording length and gets long files killed by the OOM killer ("Killed" with
# no error message). Capping buffers by BYTES keeps every fetch small no
# matter how the data was stored.
SAVE_CHUNK_BYTES = 256 * 1024 * 1024   # max float64 bytes per FIF write buffer
QA_CHUNK_BYTES = 128 * 1024 * 1024     # max float64 bytes per QA chunk (x2 sides)

log = logging.getLogger("ctf2fif")


def _chunk_samples(n_channels: int, budget_bytes: int) -> int:
    """Samples per fetch so one (n_channels, samples) float64 array stays <= budget."""
    return max(1, budget_bytes // (8 * max(1, n_channels)))


def _mem_available_bytes():
    """Bytes of RAM the kernel says are available (Linux only; None elsewhere)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _warn_if_memory_tight(needed_bytes: int, context: str) -> None:
    """Warn when a step is expected to approach the machine's available RAM.

    Without this the OOM killer just SIGKILLs the process (the shell prints a
    bare "Killed") and no traceback or report line is ever written.
    """
    avail = _mem_available_bytes()
    if avail is not None and needed_bytes > 0.75 * avail:
        log.warning(
            "%s is expected to need ~%.1f GB of RAM but only ~%.1f GB is "
            "available. If the process dies with a bare 'Killed' message, "
            "that is the kernel OOM killer; free memory or run under a "
            "memory cap (ulimit -v) to get a real traceback instead.",
            context, needed_bytes / (1 << 30), avail / (1 << 30),
        )


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

    # CTF data is int32 on disk; MNE reads whole trials into an int32 temp
    # before scaling to float64. Worst case that temp is the size of the
    # whole recording, so warn before we push against the RAM ceiling
    # (see _warn_if_memory_tight).
    n_ch, n_times = len(raw.ch_names), len(raw.times)
    _warn_if_memory_tight(
        4 * n_ch * n_times + SAVE_CHUNK_BYTES,
        f"Converting {ds.name} ({n_ch} channels, {n_times} samples)",
    )

    # Cap the writer's fetch buffer by bytes. MNE would otherwise size it
    # after the .ds trial length (res4 nsamp); a .ds stored as one long
    # trial then reads/writes the entire recording per buffer.
    buffer_sec = _chunk_samples(n_ch, SAVE_CHUNK_BYTES) / raw.info["sfreq"]

    # Do the write in a scratch dir inside the destination directory, then move
    # the finished file(s) into place and always remove the scratch dir.
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out.parent / f".ctf2fif-tmp-{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir()
    try:
        written = [Path(f) for f in raw.save(str(tmp_dir / out.name),
                                             overwrite=True,
                                             buffer_size_sec=buffer_sec,
                                             verbose="WARNING")]
        os.replace(written[0], out)
        for extra in written[1:]:
            os.replace(extra, out.parent / extra.name)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    log.debug("Wrote %s (scratch dir %s)", out, tmp_dir)
    return out


def run_qa(ctf_path: Path, fif_path: Path) -> dict:
    """Compare a source CTF .ds against a converted FIF file.

    Returns a result dict describing the PASS/WARN/FAIL status plus the
    numbers needed to build a human-reviewable report (see format_qa_report).
    The comparison itself streams in memory-bounded chunks -- see
    _compare_raws.
    """
    import mne

    result = {
        "status": "FAIL",
        "score": None,
        "reason": None,
        "error": None,
        "n_channels_ctf": None,
        "n_channels_fif": None,
        "channels_compared": 0,
        "sfreq_ctf": None,
        "sfreq_fif": None,
        "duration_sec": None,
        "resampled": False,
        "only_ctf": [],
        "only_fif": [],
        "flat_channels": [],
        "low_correlation_channels": [],
        "corr": None,
        "n_nan": 0,
        "worst_channels": [],
    }

    try:
        raw_ctf = mne.io.read_raw_ctf(str(ctf_path), preload=False, verbose="WARNING")
        raw_fif = mne.io.read_raw_fif(str(fif_path), preload=False, verbose="WARNING")
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["reason"] = "could not read input(s)"
        return result

    return _compare_raws(raw_ctf, raw_fif, result, label=ctf_path.name)


def _compare_raws(raw_ctf, raw_fif, result: dict, label: str = "") -> dict:
    """Populate the QA result dict by comparing two raw objects channel by channel.

    Both datasets are streamed in time chunks with byte-capped buffers, so
    peak RAM is bounded by the chunk size plus MNE's internal per-trial read
    buffer -- not by the length of the recording. A whole-recording CTF
    chunk would otherwise need ~5-6 float64 copies of the data at once and
    gets long files killed by the kernel OOM killer.

    If the CTF and FIF sampling rates differ the QA returns FAIL with a
    reason: this tool compares a conversion against its own source, so a
    rate mismatch can only mean the conversion (or the FIF) is wrong. The
    sampling rate is still recorded in the result for the report.
    """
    import numpy as np

    result["n_channels_ctf"] = len(raw_ctf.ch_names)
    result["n_channels_fif"] = len(raw_fif.ch_names)
    result["sfreq_ctf"] = float(raw_ctf.info["sfreq"])
    result["sfreq_fif"] = float(raw_fif.info["sfreq"])
    if len(raw_fif.times):
        result["duration_sec"] = round(float(raw_fif.times[-1]), 2)

    if result["sfreq_fif"] > 0 and abs(result["sfreq_ctf"] - result["sfreq_fif"]) > 0.01:
        result["reason"] = (
            f"sampling rates differ: CTF={result['sfreq_ctf']} Hz, "
            f"FIF={result['sfreq_fif']} Hz; a conversion by this tool should "
            "preserve the source rate"
        )
        return result

    fif_set = set(raw_fif.ch_names)
    ctf_set = set(raw_ctf.ch_names)
    common = [ch for ch in raw_ctf.ch_names if ch in fif_set]
    result["only_ctf"] = [ch for ch in raw_ctf.ch_names if ch not in fif_set]
    result["only_fif"] = [ch for ch in raw_fif.ch_names if ch not in ctf_set]
    result["channels_compared"] = len(common)

    if not common:
        result["reason"] = "no common channels between CTF and FIF"
        return result

    n_times = min(len(raw_ctf.times), len(raw_fif.times))
    if len(raw_ctf.times) != len(raw_fif.times):
        log.info(
            "QA: CTF has %d samples but FIF has %d; comparing the first %d",
            len(raw_ctf.times), len(raw_fif.times), n_times,
        )
    if n_times == 0:
        result["reason"] = "no overlapping time samples between CTF and FIF"
        return result

    n_ch_cmp = len(common)
    chunk = _chunk_samples(n_ch_cmp, QA_CHUNK_BYTES)
    _warn_if_memory_tight(
        4 * result["n_channels_ctf"] * len(raw_ctf.times) + 3 * n_ch_cmp * chunk * 8,
        f"QA of {label}" if label else "QA",
    )

    # Stream both datasets in chunks, accumulating per-channel Pearson sums.
    # Keeping only sums means peak RAM is O(chunk), never O(recording).
    n = 0
    sx = np.zeros(n_ch_cmp)
    sy = np.zeros(n_ch_cmp)
    sxx = np.zeros(n_ch_cmp)
    syy = np.zeros(n_ch_cmp)
    sxy = np.zeros(n_ch_cmp)
    for start in range(0, n_times, chunk):
        stop = min(start + chunk, n_times)
        x = raw_ctf.get_data(picks=common, start=start, stop=stop)
        y = raw_fif.get_data(picks=common, start=start, stop=stop)
        n += stop - start
        sx += x.sum(axis=1)
        sy += y.sum(axis=1)
        sxx += np.einsum("ij,ij->i", x, x)
        syy += np.einsum("ij,ij->i", y, y)
        sxy += np.einsum("ij,ij->i", x, y)
        del x, y

    mean_x = sx / n
    mean_y = sy / n
    var_x = sxx / n - mean_x * mean_x
    var_y = syy / n - mean_y * mean_y
    cov = sxy / n - mean_x * mean_y
    denom = np.sqrt(np.maximum(var_x, 0.0) * np.maximum(var_y, 0.0))
    corr = np.full(n_ch_cmp, np.nan)
    np.divide(cov, denom, out=corr, where=denom > 0)

    result["n_nan"] = int(np.isnan(corr).sum())
    valid = ~np.isnan(corr)
    if valid.any():
        good = corr[valid]
        result["score"] = round(float(np.median(good)), 6)
        result["corr"] = {
            "count": int(good.size),
            "min": round(float(good.min()), 6),
            "p05": round(float(np.percentile(good, 5)), 6),
            "median": round(float(np.median(good)), 6),
            "p95": round(float(np.percentile(good, 95)), 6),
            "max": round(float(good.max()), 6),
            "lt_low": int((good < QA_LOW).sum()),
            "lt_pass": int((good < QA_PASS).sum()),
        }
        result["low_correlation_channels"] = [
            common[i] for i in range(len(common)) if valid[i] and corr[i] < QA_LOW
        ]
        order = np.argsort(np.where(valid, corr, np.inf))
        result["worst_channels"] = [
            (common[i], round(float(corr[i]), 6)) for i in order[:10] if valid[i]
        ]

    # FIF channel std over the compared samples, from the accumulated sums.
    std = np.sqrt(np.maximum(var_y, 0.0))
    positive = std[std > 0]
    ref_std = float(np.median(positive)) if positive.size else 0.0
    flat_mask = std <= max(QA_FLAT_ABS, QA_FLAT_REL * ref_std)
    result["flat_channels"] = [common[i] for i in np.nonzero(flat_mask)[0]]

    if result["score"] is None:
        result["reason"] = "all channel correlations undefined (constant/flat data)"
    elif result["score"] >= QA_PASS and not result["low_correlation_channels"]:
        result["status"] = "PASS"
    elif result["score"] >= QA_WARN:
        result["status"] = "WARN"
    else:
        result["status"] = "FAIL"

    if (result["only_ctf"] or result["only_fif"]) and result["status"] != "FAIL":
        result["status"] = "FAIL"
        result["reason"] = "channel set mismatch between CTF and FIF"
    return result


def _fmt_list(items) -> str:
    return ", ".join(items) if items else "none"


def format_qa_report(result: dict, ctf_path, fif_path) -> str:
    """Render a QA result as a human-reviewable plain-text report."""
    lines = []
    add = lines.append
    add("CTF/FIF QA COMPARISON REPORT")
    add(f"Generated : {datetime.now().isoformat(timespec='seconds')}")
    add(f"CTF source: {ctf_path}")
    add(f"FIF input : {fif_path}")
    add("")
    add(f"RESULT: {result['status']}")
    if result.get("reason"):
        add(f"Reason: {result['reason']}")
    if result.get("error"):
        add(f"Error : {result['error']}")
    add("")
    add("Data properties")
    add(f"  Channels    : CTF={result['n_channels_ctf']} "
        f"FIF={result['n_channels_fif']} compared={result['channels_compared']}")
    add(f"  Sampling Hz : CTF={result['sfreq_ctf']} FIF={result['sfreq_fif']}")
    add(f"  Duration s  : {result['duration_sec']}")
    add(f"  Resampled CTF to FIF sfreq : {'yes' if result['resampled'] else 'no'}")
    add(f"  Channels only in CTF : {_fmt_list(result['only_ctf'])}")
    add(f"  Channels only in FIF : {_fmt_list(result['only_fif'])}")
    add("")
    add("Correlation (per common channel)")
    if result.get("corr"):
        c = result["corr"]
        add(f"  Count        : {c['count']}")
        add(f"  min          : {c['min']}")
        add(f"  p05          : {c['p05']}")
        add(f"  median       : {c['median']}")
        add(f"  p95          : {c['p95']}")
        add(f"  max          : {c['max']}")
        add(f"  undefined    : {result['n_nan']}")
        add(f"  < {QA_LOW}     : {c['lt_low']}")
        add(f"  < {QA_PASS}     : {c['lt_pass']}")
    else:
        add("  (not computed)")
        add(f"  undefined    : {result['n_nan']}")
    add("")
    add("Channel issues")
    add(f"  Flat/dead channels (FIF, near-zero variance) : "
        f"{_fmt_list(result['flat_channels'])}")
    add(f"  Low-correlation channels (<{QA_LOW}) : "
        f"{_fmt_list(result['low_correlation_channels'])}")
    add("")
    add("Worst channels by correlation")
    if result["worst_channels"]:
        for name, r in result["worst_channels"]:
            add(f"  {name:<24} {r}")
    else:
        add("  (none)")
    add("")
    add(f"Legend: PASS = score >= {QA_PASS} and no low-correlation channels; "
        f"WARN = score >= {QA_WARN}; FAIL = otherwise or structural mismatch.")
    return "\n".join(lines) + "\n"


def append_qa_report(report_path: Path, result: dict, ctf_path, fif_path) -> None:
    """Append a QA comparison block to the current run's report file."""
    block = format_qa_report(result, ctf_path, fif_path)
    rule = "=" * 72
    with open(report_path, "a", encoding="utf-8") as f:
        f.write(f"\n{rule}\n{block}{rule}\n")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Convert CTF .ds datasets to FIF.")
    p.add_argument("inputs", nargs="*", type=Path, help="one or more .ds directories")
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
    p.add_argument("--qa", action="store_true",
                   help="after each conversion, add a QA comparison of the input "
                        ".ds against its output .fif to the report")
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
        if not args.inputs:
            raise ValueError("no inputs given")
        if args.out is not None and len(args.inputs) != 1:
            raise ValueError("--out requires exactly one input")
        (args.out.parent if args.out else Path(".")).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.error("Setup failed: %s", e)
        log.debug("Traceback:\n%s", traceback.format_exc())
        return 2

    interactive = sys.stdin.isatty() and not args.no_prompt
    ok, failed, qa_failed = 0, 0, 0
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
            continue

        if args.qa:
            try:
                result = run_qa(ds, out)
                append_qa_report(report, result, ds, out)
                log.info("QA %s: %s (score=%s)", out, result["status"], result["score"])
                if result["status"] == "FAIL":
                    qa_failed += 1
            except Exception as e:
                log.error("QA failed for %s: %s: %s", out, type(e).__name__, e)
                log.debug("Traceback:\n%s", traceback.format_exc())
                qa_failed += 1

    log.info("Done: %d converted, %d failed, %d QA failures. Report: %s",
             ok, failed, qa_failed, report)
    return 1 if (failed or qa_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
