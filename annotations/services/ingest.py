"""
Creating layers and their segments. Every import path goes through here — the management
commands and the API upload alike — rather than each having its own copy.

This module holds no knowledge of any particular dataset's disk layout. A dataset-specific
importer (services/vocadito.py) works out which files belong to which item and what the
columns mean, then hands the parsed rows to create_layer_with_segments().
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..models import AnnotationLayer, Segment
from ..registry import get_layer_type


class IngestError(ValueError):
    """The rows handed in cannot be stored as this layer type."""


#: How many offending rows a payload-validation failure names before it stops listing.
#: Enough to see a pattern; not the whole file.
MAX_REPORTED_PAYLOAD_ERRORS = 10


def create_layer_with_segments(
    *,
    item,
    layer_type: str,
    stream_key: str,
    rows: list[dict],
    author=None,
    author_kind=AnnotationLayer.AuthorKind.IMPORT,
    params: dict | None = None,
    provenance: dict | None = None,
    parent: AnnotationLayer | None = None,
    status=AnnotationLayer.Status.SUBMITTED,
) -> AnnotationLayer:
    """
    Create a new version of a layer. Always a new version: an import never overwrites an
    existing layer, because "somebody changed it and nobody knows who" is exactly the
    problem this system exists to solve.

    Version, parent and supersession are all scoped to (item, layer_type, stream_key), so
    two parallel streams are independent of each other: two annotators working from the
    same audio under different stream keys each get version 1 with no parent, and neither
    is marked SUPERSEDED. A lineage only forms within one stream key.

    The rows are validated against the layer type's payload schema before anything is
    written. Segment.clean() states the same rules, but bulk_create() does not call it and
    neither does any other write path that does not go through a ModelForm — so stating
    them there only is the same as not stating them. This is the funnel every import
    passes through, which makes it the place the check has to be.
    """
    spec = get_layer_type(layer_type)
    if spec is None:
        raise IngestError(f"Unregistered annotation type: {layer_type}")
    _validate_rows(spec, rows, layer_type=layer_type, stream_key=stream_key)

    param_errors = spec.validate_params(params or {})
    if param_errors:
        raise IngestError(
            f"{layer_type}/{stream_key or 'default'}: layer params are invalid: "
            + "; ".join(param_errors)
        )

    latest = (
        AnnotationLayer.objects.filter(item=item, layer_type=layer_type, stream_key=stream_key)
        .order_by("-version")
        .first()
    )
    version = (latest.version + 1) if latest else 1
    layer = AnnotationLayer.objects.create(
        item=item,
        layer_type=layer_type,
        stream_key=stream_key,
        version=version,
        parent=parent or latest,
        author=author,
        author_kind=author_kind,
        status=status,
        params=params or {},
        provenance=provenance or {},
    )
    Segment.objects.bulk_create(
        [
            Segment(
                layer=layer,
                index=r["index"],
                start_sec=r["start_sec"],
                end_sec=r["end_sec"],
                payload=r.get("payload") or {},
            )
            for r in rows
        ],
        batch_size=1000,
    )
    if latest and latest.status == AnnotationLayer.Status.ACCEPTED:
        latest.status = AnnotationLayer.Status.SUPERSEDED
        latest.save(update_fields=["status", "updated_at"])
    return layer


def _validate_rows(spec, rows: list[dict], *, layer_type: str, stream_key: str) -> None:
    problems: list[str] = []
    #: Rows with at least one problem, counted once each. The figure the message quotes has
    #: to be this and not len(rows): "604 rows do not satisfy the schema" when one row is
    #: out of range tells the operator the file or the contract is wrong, and sends them to
    #: re-examine 603 rows that are fine.
    failed_rows: set = set()
    stopped_early = False
    for position, r in enumerate(rows):
        where = f"row {r.get('index', position)}"
        before = len(problems)
        if r["end_sec"] < r["start_sec"]:
            problems.append(f"{where}: end_sec {r['end_sec']:g} is before start_sec {r['start_sec']:g}")
        for err in spec.validate_payload(r.get("payload") or {}):
            problems.append(f"{where}: {err}")
        if len(problems) > before:
            failed_rows.add(position)
        if len(problems) > MAX_REPORTED_PAYLOAD_ERRORS:
            # Listing stops, so the count below is a floor rather than a total. Said so in
            # the message instead of quietly reporting the partial count as final.
            stopped_early = True
            break

    clip_problem = spec.segment_scope == "clip" and len(rows) > 1
    if clip_problem:
        problems.append(
            f"{layer_type} is a clip-level type, so it takes at most one segment; got {len(rows)}"
        )
    if not problems:
        return

    if failed_rows:
        # The noun agrees with the total, the verb with the failing count: "1 of 20 rows
        # does not satisfy", "3 of 20 rows do not satisfy".
        noun = "row" if len(rows) == 1 else "rows"
        verb = "does" if len(failed_rows) == 1 else "do"
        count = f"{len(failed_rows)} of {len(rows)} {noun} {verb}"
        if stopped_early:
            count = f"at least {count}"
    else:
        count = "the rows do"

    raise IngestError(
        f"{layer_type}/{stream_key or 'default'}: {count} not satisfy the {layer_type} "
        f"schema. " + "; ".join(problems[:MAX_REPORTED_PAYLOAD_ERRORS])
    )


def validate_segment_rows(layer_type: str, rows: list[dict]) -> None:
    """The row check on its own, for callers that want it before building a layer."""
    spec = get_layer_type(layer_type)
    if spec is None:
        raise IngestError(f"Unregistered annotation type: {layer_type}")
    _validate_rows(spec, rows, layer_type=layer_type, stream_key="")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def float_or_none(raw):
    if raw in (None, "", "N/A"):
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        return None
