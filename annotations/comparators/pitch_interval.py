"""
Consistency comparison.

A note_pitch layer is (onset, offset, hz), and a comparison has to handle both
  · the interval: overlap ratio / IoU, or matching within an onset tolerance window
  · the pitch: a cents tolerance (Hz may never be compared directly)
  · still open: when the pitch is wrong but the timing is right, how inconsistent is
    that? The approach taken here is to report the two separately rather than collapsing
    them into a single score. Timing delta and pitch delta each get their own field, and
    the kind of mismatch is labelled separately too. How to weight them into one number
    is a scoring policy, and it does not belong buried inside a comparator.

Tolerance parameters are passed in by the caller and stored in LayerComparison.params,
because there is no single right scope for them: a sensible onset tolerance depends on
the material, the annotators and the question being asked. Rather than pick one and bury
it, each comparison records the tolerances it actually used, so a stored result stays
readable after the defaults have moved on.
"""
from __future__ import annotations

import math

from ..registry import register_comparator

#: The matching strategies _match() implements. Named here so an unknown value is refused
#: rather than silently falling through to one of them.
MATCH_STRATEGIES = ("onset", "iou")

NOTE_PITCH_DEFAULTS = {
    "onset_tolerance_sec": 0.10,
    "min_iou": 0.5,
    "pitch_tolerance_cents": 50.0,
    # Onset matching, because two independent annotators of the same performance agree far
    # more closely on where a note starts than on how long they held it. Stated here and
    # nowhere else: _match() requires the caller to have decided, so this default cannot
    # disagree with a second one buried in the matcher.
    "match_strategy": "onset",
}

SED_DEFAULTS = {
    "min_iou": 0.5,
    "require_same_label": True,
    # A sound event has no onset-only reading: two events of different lengths starting
    # together are not the same event.
    "match_strategy": "iou",
}


def _require(params, key: str) -> float:
    """A tolerance the chosen strategy cannot run without, taken only from the params."""
    value = params.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(
            f"{key} must be a number for this matching strategy, got {value!r}. "
            f"Merge the comparator's default_params before calling."
        )
    return float(value)


def _iou(a, b) -> float:
    inter = min(a["end_sec"], b["end_sec"]) - max(a["start_sec"], b["start_sec"])
    if inter <= 0:
        return 0.0
    union = max(a["end_sec"], b["end_sec"]) - min(a["start_sec"], b["start_sec"])
    return inter / union if union > 0 else 0.0


def _match(rows_a, rows_b, params):
    """
    Greedy matching. Score every candidate pair, then match from the highest score down,
    with each segment used at most once. Weaker than the Hungarian algorithm, but
    explainable — a reviewer can see why these two were paired.
    """
    # Nothing here is defaulted. Every comparator merges its own defaults before calling,
    # so a missing knob means a caller built the params by hand and left out something that
    # changes which pairs are considered at all. Silently supplying a value would make the
    # result unreadable: the params stored beside a comparison would not describe what was
    # actually done to produce it.
    strategy = params.get("match_strategy")
    if strategy not in MATCH_STRATEGIES:
        raise ValueError(
            f"match_strategy must be one of {MATCH_STRATEGIES}, got {strategy!r}. "
            f"Merge the comparator's default_params before calling."
        )
    tolerance = _require(params, "onset_tolerance_sec" if strategy == "onset" else "min_iou")

    cands = []
    for a in rows_a:
        for b in rows_b:
            if strategy == "onset":
                d = abs(a["start_sec"] - b["start_sec"])
                if d > tolerance:
                    continue
                score = 1.0 - d / max(tolerance, 1e-9)
            else:
                score = _iou(a, b)
                if score < tolerance:
                    continue
            cands.append((score, a, b))
    cands.sort(key=lambda t: -t[0])

    used_a, used_b, pairs = set(), set(), []
    for score, a, b in cands:
        if a["index"] in used_a or b["index"] in used_b:
            continue
        used_a.add(a["index"])
        used_b.add(b["index"])
        pairs.append((a, b, score))
    unmatched_a = [a for a in rows_a if a["index"] not in used_a]
    unmatched_b = [b for b in rows_b if b["index"] not in used_b]
    return pairs, unmatched_a, unmatched_b


@register_comparator(
    "note_pitch_interval_cents",
    "Note pitch: interval matching plus a cents tolerance",
    layer_types=("note_pitch",),
    default_params=NOTE_PITCH_DEFAULTS,
)
def compare_note_pitch(layer_a, layer_b, params=None) -> dict:
    p = {**NOTE_PITCH_DEFAULTS, **(params or {})}
    rows_a = layer_a.resolved_segments()
    rows_b = layer_b.resolved_segments()
    ref_a = layer_a.tuning_reference_hz
    ref_b = layer_b.tuning_reference_hz

    pairs, only_a, only_b = _match(rows_a, rows_b, p)
    disc: list[dict] = []
    onset_deltas, cents_deltas = [], []
    pitch_only = timing_only = both = 0

    for a, b, score in pairs:
        d_on = b["start_sec"] - a["start_sec"]
        d_off = b["end_sec"] - a["end_sec"]
        onset_deltas.append(abs(d_on))
        hz_a = (a["payload"] or {}).get("hz")
        hz_b = (b["payload"] or {}).get("hz")

        cents = None
        if isinstance(hz_a, (int, float)) and isinstance(hz_b, (int, float)) and hz_a > 0 and hz_b > 0:
            # The two layers may have different tuning references, so convert each to a
            # pitch relative to its own reference before comparing.
            cents = 1200.0 * math.log2((hz_b / ref_b) / (hz_a / ref_a))
            cents_deltas.append(abs(cents))

        pitch_bad = cents is not None and abs(cents) > p["pitch_tolerance_cents"]
        timing_bad = abs(d_on) > p["onset_tolerance_sec"] or _iou(a, b) < p["min_iou"]

        if pitch_bad and timing_bad:
            kind, both = "pitch_and_timing", both + 1
        elif pitch_bad:
            kind, pitch_only = "pitch_only", pitch_only + 1
        elif timing_bad:
            kind, timing_only = "timing_only", timing_only + 1
        else:
            continue

        disc.append(
            {
                "kind": kind,
                "a_index": a["index"],
                "b_index": b["index"],
                "a_start_sec": a["start_sec"],
                "b_start_sec": b["start_sec"],
                "onset_delta_sec": d_on,
                "offset_delta_sec": d_off,
                "iou": _iou(a, b),
                "a_hz": hz_a,
                "b_hz": hz_b,
                "pitch_delta_cents": cents,
                "octaves_apart": round(cents / 1200.0) if cents is not None else None,
            }
        )

    for a in only_a:
        disc.append(
            {
                "kind": "only_in_a",
                "a_index": a["index"],
                "b_index": None,
                "a_start_sec": a["start_sec"],
                "a_hz": (a["payload"] or {}).get("hz"),
            }
        )
    for b in only_b:
        disc.append(
            {
                "kind": "only_in_b",
                "a_index": None,
                "b_index": b["index"],
                "b_start_sec": b["start_sec"],
                "b_hz": (b["payload"] or {}).get("hz"),
            }
        )
    disc.sort(key=lambda d: d.get("a_start_sec") or d.get("b_start_sec") or 0.0)

    summary = {
        "segments_a": len(rows_a),
        "segments_b": len(rows_b),
        "matched": len(pairs),
        "only_in_a": len(only_a),
        "only_in_b": len(only_b),
        # Reported separately on purpose; never collapsed into a single "agreement rate"
        "pitch_only_mismatch": pitch_only,
        "timing_only_mismatch": timing_only,
        "both_mismatch": both,
        "median_abs_onset_delta_sec": _median(onset_deltas),
        "median_abs_pitch_delta_cents": _median(cents_deltas),
        "tuning_reference_a_hz": ref_a,
        "tuning_reference_b_hz": ref_b,
    }
    return {"summary": summary, "discrepancies": disc}


@register_comparator(
    "interval_label_iou",
    "Sound event: IoU matching plus label comparison",
    layer_types=("sound_event",),
    default_params=SED_DEFAULTS,
)
def compare_sound_events(layer_a, layer_b, params=None) -> dict:
    p = {**SED_DEFAULTS, **(params or {})}
    rows_a, rows_b = layer_a.resolved_segments(), layer_b.resolved_segments()
    pairs, only_a, only_b = _match(rows_a, rows_b, p)
    disc = []
    label_mismatch = 0
    for a, b, score in pairs:
        la = (a["payload"] or {}).get("label")
        lb = (b["payload"] or {}).get("label")
        if p["require_same_label"] and la != lb:
            label_mismatch += 1
            disc.append(
                {
                    "kind": "label_mismatch",
                    "a_index": a["index"],
                    "b_index": b["index"],
                    "a_start_sec": a["start_sec"],
                    "a_label": la,
                    "b_label": lb,
                    "iou": score,
                }
            )
    for a in only_a:
        disc.append({"kind": "only_in_a", "a_index": a["index"], "a_start_sec": a["start_sec"]})
    for b in only_b:
        disc.append({"kind": "only_in_b", "b_index": b["index"], "b_start_sec": b["start_sec"]})
    return {
        "summary": {
            "segments_a": len(rows_a),
            "segments_b": len(rows_b),
            "matched": len(pairs),
            "label_mismatch": label_mismatch,
            "only_in_a": len(only_a),
            "only_in_b": len(only_b),
        },
        "discrepancies": disc,
    }


def _median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
