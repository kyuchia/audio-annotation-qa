"""
Applies LayerCorrections to a snapshot of the segments. Pure functions, no database.

The original Segments always match the file as it was at import time; a correction is
an overlay applied at read time. So "this whole layer came out two octaves low" is one
octave_shift correction, not a rewrite of every payload row — which would leave no way to
tell the corrected data from the data as it arrived.
"""
from __future__ import annotations

from typing import Iterable


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
