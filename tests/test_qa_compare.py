"""Tests for the streaming QA comparison (_compare_raws).

The comparison streams both datasets in byte-capped time chunks and
accumulates per-channel sums. These tests verify that the streamed math
matches the direct full-load computation (the pre-streaming implementation,
kept here as the reference) and that status/reason semantics hold.

Run from the repo root:  python -m pytest tests/
"""
import numpy as np
import pytest

import mne

import ctf2fif
from ctf2fif import _compare_raws, QA_LOW, QA_PASS

SFREQ = 100.0
N_CH = 20
N_TIMES = 20_000  # 200 s

# Force tiny chunks (4000 B / (8 B * 20 ch) = 25 samples) so the accumulation
# crosses ~800 chunk boundaries.
TINY_CHUNK_BYTES = 4000


def make_result() -> dict:
    """A fresh empty result dict with the keys format_qa_report expects."""
    return {
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


def raw_from(data, names=None, sfreq=SFREQ):
    names = names if names is not None else [f"EEG{i:03d}" for i in range(data.shape[0])]
    return mne.io.RawArray(data, mne.create_info(names, sfreq, "eeg"))


def full_load_corr(a, b):
    """Reference implementation: the pre-streaming centered-array correlation.

    Rows with zero variance yield NaN instead of raising.
    """
    a = a - a.mean(axis=1, keepdims=True)
    b = b - b.mean(axis=1, keepdims=True)
    denom = np.sqrt((a * a).sum(axis=1) * (b * b).sum(axis=1))
    with np.errstate(invalid="ignore", divide="ignore"):
        return (a * b).sum(axis=1) / denom


@pytest.fixture
def rng():
    return np.random.default_rng(42)


@pytest.fixture
def tiny_chunks(monkeypatch):
    monkeypatch.setattr(ctf2fif, "QA_CHUNK_BYTES", TINY_CHUNK_BYTES)


def test_chunk_helper():
    # 4000 B / (8 B per float64 * 20 ch) = 25 samples; never zero
    assert ctf2fif._chunk_samples(20, 4000) == 25
    assert ctf2fif._chunk_samples(1, 1) == 1


def test_streaming_matches_full_load(rng, tiny_chunks):
    a = rng.standard_normal((N_CH, N_TIMES)) * 1e-12
    b = a * 2.0 + rng.standard_normal((N_CH, N_TIMES)) * 1e-14
    ra, rb = raw_from(a), raw_from(b)

    res = _compare_raws(ra, rb, make_result())

    ref = full_load_corr(a, b)
    assert res["status"] == "PASS"  # corr ~1: only float rounding differs
    assert res["n_nan"] == int(np.isnan(ref).sum()) == 0
    stats = res["corr"]
    for key, ref_val in (
        ("min", ref.min()), ("median", np.median(ref)), ("max", ref.max()),
        ("p05", np.percentile(ref, 5)), ("p95", np.percentile(ref, 95)),
    ):
        # result stats are rounded to 6 decimals
        assert stats[key] == pytest.approx(ref_val, abs=1e-6), key
    assert stats["count"] == N_CH
    assert stats["lt_low"] == 0 and stats["lt_pass"] == 0
    assert abs(res["score"] - float(np.median(ref))) < 1e-6


def test_pass_identical(rng, tiny_chunks):
    a = rng.standard_normal((N_CH, N_TIMES)) * 1e-12
    res = _compare_raws(raw_from(a), raw_from(a.copy()), make_result())
    assert res["status"] == "PASS"
    assert res["score"] >= QA_PASS
    assert res["corr"]["median"] == 1.0
    assert res["n_nan"] == 0
    assert res["flat_channels"] == []
    assert res["low_correlation_channels"] == []
    assert res["only_ctf"] == [] and res["only_fif"] == []
    assert res["duration_sec"] == round((N_TIMES - 1) / SFREQ, 2)


def test_flat_channel_both_sides(rng, tiny_chunks):
    a = rng.standard_normal((N_CH, N_TIMES)) * 1e-12
    a[7] = 0.0  # exact zeros -> zero variance -> NaN correlation
    res = _compare_raws(raw_from(a), raw_from(a.copy()), make_result())
    assert res["n_nan"] == 1
    assert res["flat_channels"] == ["EEG007"]
    assert "EEG007" not in res["low_correlation_channels"]
    assert res["status"] == "PASS"  # score from the 19 valid channels
    assert res["corr"]["count"] == N_CH - 1


def test_flat_channel_fif_only(rng, tiny_chunks):
    a = rng.standard_normal((N_CH, N_TIMES)) * 1e-12
    b = a.copy()
    b[4] = 0.0  # constant on the FIF side only
    res = _compare_raws(raw_from(a), raw_from(b), make_result())
    assert res["flat_channels"] == ["EEG004"]
    assert "EEG004" not in res["low_correlation_channels"]
    assert res["status"] == "PASS"


def test_channel_set_mismatch_fail(rng, tiny_chunks):
    a = rng.standard_normal((N_CH, N_TIMES)) * 1e-12
    b = a[:15].copy()
    res = _compare_raws(raw_from(a), raw_from(b), make_result())
    assert res["channels_compared"] == 15
    assert res["only_ctf"] == [f"EEG{i:03d}" for i in range(15, 20)]
    assert res["only_fif"] == []
    assert res["status"] == "FAIL"
    assert res["reason"] == "channel set mismatch between CTF and FIF"


def test_sfreq_mismatch_fail(rng):
    a = rng.standard_normal((N_CH, N_TIMES)) * 1e-12
    res = _compare_raws(
        raw_from(a, sfreq=100.0), raw_from(a.copy(), sfreq=125.0), make_result()
    )
    assert res["status"] == "FAIL"
    assert res["score"] is None
    assert res["resampled"] is False
    assert res["reason"].startswith("sampling rates differ")
    assert "CTF=100.0" in res["reason"] and "FIF=125.0" in res["reason"]


def test_no_common_channels(rng):
    a = rng.standard_normal((N_CH, N_TIMES)) * 1e-12
    res = _compare_raws(
        raw_from(a), raw_from(a.copy(), names=[f"MEG{i:03d}" for i in range(N_CH)]),
        make_result(),
    )
    assert res["status"] == "FAIL"
    assert res["reason"] == "no common channels between CTF and FIF"
    assert res["channels_compared"] == 0


def test_shorter_fif_compares_prefix(rng, tiny_chunks):
    a = rng.standard_normal((N_CH, N_TIMES)) * 1e-12
    res = _compare_raws(raw_from(a), raw_from(a[:, :15_000].copy()), make_result())
    assert res["status"] == "PASS"
    assert res["duration_sec"] == round(14_999 / SFREQ, 2)


def test_low_correlation_fails(rng, tiny_chunks):
    a = rng.standard_normal((N_CH, N_TIMES)) * 1e-12
    b = a + rng.standard_normal((N_CH, N_TIMES)) * 5e-12  # SNR ~0.2 -> corr ~0.2
    res = _compare_raws(raw_from(a), raw_from(b), make_result())
    assert res["status"] == "FAIL"
    assert res["score"] < QA_LOW
    assert res["n_nan"] == 0


def test_warn_band(rng, tiny_chunks):
    # noise ~0.1x signal -> expected corr ~ 1/sqrt(1+0.01) ~ 0.995 (WARN band)
    a = rng.standard_normal((N_CH, N_TIMES)) * 1e-12
    b = a + rng.standard_normal((N_CH, N_TIMES)) * 1e-13
    res = _compare_raws(raw_from(a), raw_from(b), make_result())
    assert QA_LOW <= res["score"] <= QA_PASS
    assert res["status"] == "WARN"