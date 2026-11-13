"""
Applies LayerCorrections to a snapshot of the segments. Pure functions, no database.

The original Segments always match the file as it was at import time; a correction is
an overlay applied at read time. So "this whole layer came out two octaves low" is one
octave_shift correction, not a rewrite of every payload row — which would leave no way to
tell the corrected data from the data as it arrived.
"""
from __future__ import annotations

import math
import numbers
from typing import Iterable

#: What each kind of correction takes: {kind: {param: (requirement, lowest, highest)}}.
#: The bounds on the two pitch shifts are what keeps the factor a float can hold; a time
#: is bounded only by being a real number of seconds.
_NUMBER_PARAMS = {
    "octave_shift": {"octaves": (-10, 10)},
    "cents_shift": {"cents": (-12000, 12000)},
    "time_offset": {"seconds": (None, None)},
    "trim_tail": {"after_sec": (0, None)},
    "drop_segments": {},
}
#: The kinds that take a list of segment indices, and whether they need one.
_INDICES_REQUIRED = {"octave_shift": False, "cents_shift": False, "drop_segments": True}


def correction_param_errors(kind: str, params) -> list[str]:
    """
    Why these params cannot be applied by this kind of correction, as sentences for the
    person who typed them. An empty list means apply_corrections() will run on them.

    A correction is applied on every read of its layer, so this is asked once, when the
    correction is written, by the API serializer and by LayerCorrection.clean() alike.
    """
    numbers_for = _NUMBER_PARAMS.get(kind)
    if numbers_for is None:
        return [f"{kind} is outside the kinds of correction this system applies."]
    if not isinstance(params, dict):
        return [f"{kind} takes its params as a JSON object, for example {_example(kind)}."]

    accepted = list(numbers_for) + (["indices"] if kind in _INDICES_REQUIRED else [])
    errs: list[str] = []
    unknown = sorted(str(k) for k in set(params) - set(accepted))
    if unknown:
        errs.append(
            f"{kind} takes {' and '.join(accepted)}; remove {', '.join(unknown)}."
        )

    for name, (low, high) in numbers_for.items():
        value = params.get(name)
        ok = (
            isinstance(value, numbers.Real)
            and not isinstance(value, bool)
            and math.isfinite(value)
            and (low is None or value >= low)
            and (high is None or value <= high)
        )
        if not ok:
            errs.append(
                f"{kind} needs {name} as {_range(low, high)}, for example {_example(kind)}; "
                f"got {_show(params, name)}."
            )

    if kind in _INDICES_REQUIRED and ("indices" in params or _INDICES_REQUIRED[kind]):
        value = params.get("indices")
        ok = (
            isinstance(value, list)
            and all(isinstance(i, int) and not isinstance(i, bool) and i >= 0 for i in value)
            and (value or not _INDICES_REQUIRED[kind])
        )
        if not ok:
            errs.append(
                f"{kind} needs indices as a list of segment index numbers, for example "
                f"[0, 4]; got {_show(params, 'indices')}."
            )
    return errs


def _range(low, high) -> str:
    if low is not None and high is not None:
        return f"a number from {low} to {high}"
    if low is not None:
        return f"a number of {low} or more"
    return "a finite number"


def _example(kind: str) -> str:
    return {
        "octave_shift": '{"octaves": 1}',
        "cents_shift": '{"cents": -50}',
        "time_offset": '{"seconds": 0.25}',
        "trim_tail": '{"after_sec": 30}',
        "drop_segments": '{"indices": [0, 4]}',
    }[kind]


def _show(params: dict, name: str) -> str:
    if name not in params:
        return "nothing"
    text = repr(params[name])
    return text if len(text) <= 40 else f"{text[:37]}..."


def apply_corrections(rows: list[dict], corrections: Iterable) -> list[dict]:
    out = [dict(r, payload=dict(r.get("payload") or {})) for r in rows]
    for c in corrections:
        out = _apply_one(out, c.kind, c.params or {})
    return out


def _apply_one(rows: list[dict], kind: str, params: dict) -> list[dict]:
    if kind == "octave_shift":
        factor = 2.0 ** float(params.get("octaves", 0))
        return _scale_hz(rows, factor, params)
    if kind == "cents_shift":
        factor = 2.0 ** (float(params.get("cents", 0)) / 1200.0)
        return _scale_hz(rows, factor, params)
    if kind == "time_offset":
        d = float(params.get("seconds", 0.0))
        for r in rows:
            r["start_sec"] += d
            r["end_sec"] += d
        return rows
    if kind == "drop_segments":
        drop = set(params.get("indices") or [])
        return [r for r in rows if r["index"] not in drop]
    if kind == "trim_tail":
        after = float(params.get("after_sec", float("inf")))
        kept = []
        for r in rows:
            if r["start_sec"] >= after:
                continue
            if r["end_sec"] > after:
                r = dict(r, end_sec=after, payload=dict(r["payload"], trimmed=True))
            kept.append(r)
        return kept
    return rows


def _scale_hz(rows: list[dict], factor: float, params: dict) -> list[dict]:
    only = params.get("indices")
    only = set(only) if only else None
    for r in rows:
        if only is not None and r["index"] not in only:
            continue
        hz = r["payload"].get("hz")
        if isinstance(hz, (int, float)):
            r["payload"]["hz"] = hz * factor
            r["payload"]["hz_corrected"] = True
    return rows
