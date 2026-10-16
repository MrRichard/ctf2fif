# ctf2fif

Convert CTF MEG `.ds` directories to FIF, optionally injecting header fields.

This tool wraps [MNE-Python](https://mne.tools/) to read one or more CTF `.ds`
datasets and write them out as `.fif` files. It can also collect a set of
user-defined metadata values (validated against regular expressions) and write
them into the FIF header.

## Requirements

- Python 3.8+
- [MNE-Python](https://mne.tools/)

Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

Convert a single dataset:

```bash
python ctf2fif.py /path/to/data.ds
```

Convert multiple datasets at once:

```bash
python ctf2fif.py /path/to/*.ds
```

The default output is `<stem>_raw.fif` in the current directory. Use `-o/--out`
(only with a single input) to choose a destination:

```bash
python ctf2fif.py data.ds -o results/data_raw.fif
```

### Header fields

Field definitions are read from a JSON file (see `fields.json`) passed with
`-f/--fields`. Each entry describes a field to inject:

| Key        | Required | Description                                                                 |
| ---------- | -------- | --------------------------------------------------------------------------- |
| `field`    | yes      | Short name of the field.                                                    |
| `pattern`  | yes      | Regular expression the value must fully match.                              |
| `prompt`   | no       | Text shown when prompting the user.                                         |
| `target`   | no       | Where the value goes in the FIF header (see below).                         |
| `required` | no       | Defaults to `true`; when `false`, an empty answer skips the field.          |

Supported `target` values:

- `proj_name`, `experimenter`
- `subject_info.<key>` — `his_id`, `first_name`, `last_name`, `hand`, `sex`
- `device_info.<key>` — `type`, `model`, `serial`, `site`

Fields without a `target` are stored together as a JSON blob in
`info["description"]`.

```bash
python ctf2fif.py data.ds -f fields.json
```

Preset values non-interactively (validated against each field's pattern):

```bash
python ctf2fif.py data.ds -f fields.json --set subject_id=M123324354 --set project=Study1
```

### Options

| Option          | Description                                                       |
| --------------- | ----------------------------------------------------------------- |
| `-o`, `--out`   | Output `.fif` path (requires a single input).                     |
| `-f`, `--fields`| JSON file defining header fields.                                 |
| `--set`         | Preset a field value (`FIELD=VALUE`); repeatable.                 |
| `--no-prompt`   | Never prompt; fail if a required field is missing.                |
| `--overwrite`   | Overwrite existing FIF files.                                     |
| `--report-dir`  | Directory for report logs (default: `./reports`).                 |
| `--qa`          | Add a QA comparison of each input `.ds` against its output `.fif` to the run report. |

## QA comparison

With `--qa`, each conversion is followed by a QA check that correlates the
converted FIF against its CTF source, channel by channel (matched by name).
The full comparison is appended to the same report file as the conversion, so
the run's log is a single, self-contained, human-reviewable record.

```bash
python ctf2fif.py sub-001.ds -o sub-001_raw.fif --qa
```

Each comparison block records channel counts, sampling rate, duration,
correlation statistics (min/p05/median/p95/max), any flat/dead or
low-correlation channels, a worst-channels table, and a status:

- **PASS** — median correlation ≥ 0.999 and no channel below 0.99.
- **WARN** — median correlation ≥ 0.99.
- **FAIL** — otherwise, or if the CTF and FIF channel sets differ.

A QA `FAIL` counts toward the run's failure exit code (a summary line is also
printed to STDOUT).

## Reports

Every run writes a uniquely named log file to `./reports` (configurable with
`--report-dir`). Errors are also printed to STDOUT. When `--qa` is used, the
QA comparison blocks are appended to that same log file.

## License

MIT — see [LICENSE.md](LICENSE.md).

Copyright (c) 2026 WFU Information Technology and Analysis Group (ITAG).
