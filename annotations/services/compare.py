"""
Running a comparison and storing it.

The API and the importer both come here, so a comparison a reader runs from the item page
and one the importer stored at import time are the same object, computed the same way, to
the same recorded parameters. A second implementation would be two answers to one question,
and the first time they disagreed the page would have no way to say which was right.

The error type carries the field its message belongs to, so the viewset can report it the
way DRF reports the rest of the request without knowing why the comparison was refused.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import LayerComparison
from ..params import ParamError, comparator_params
from ..registry import comparator_for

#: The two streams an import pairs: one key per independent annotator.
DEFAULT_STREAM_PAIR = ("A1", "A2")


class ComparisonError(Exception):
    """A pair of layers that cannot be compared, with the reason and where it belongs."""

    def __init__(self, message: str, *, field: str = "detail"):
        super().__init__(message)
        self.field = field

    def as_errors(self) -> dict:
        # "params" is a field of the request, so its errors are a list, the shape DRF gives
        # a field error. "detail" is about the request as a whole, so it stays a string.
        return {self.field: [str(self)]} if self.field == "params" else {self.field: str(self)}


def default_params_for(layer_type: str) -> dict | None:
    """The parameters a comparison of this annotation type runs to when nobody chooses."""
    comparator = comparator_for(layer_type)
    return dict(comparator.default_params) if comparator else None


def run_comparison(layer_a, layer_b, params=None, *, created_by=None) -> LayerComparison:
    """Compare two layers and store the result, tolerances included."""
    if layer_a.layer_type != layer_b.layer_type:
        raise ComparisonError(
            f"Cannot compare different annotation types: {layer_a.layer_type} vs {layer_b.layer_type}"
        )
    comparator = comparator_for(layer_a.layer_type)
    if comparator is None:
        raise ComparisonError(f"No comparator is registered for {layer_a.layer_type}")
    try:
        supplied = comparator_params(params, comparator)
    except ParamError as exc:
        raise ComparisonError(str(exc), field="params") from exc
    merged = {**comparator.default_params, **supplied}
    try:
        result = comparator.fn(layer_a, layer_b, merged)
    except (ValueError, TypeError) as exc:
        # The boundary above checks every knob the comparator declares, so reaching here
        # means a comparator refused something that check does not cover yet. Still the
        # caller's input, so still the caller's error.
        raise ComparisonError(
            f"This comparison cannot be run with those parameters: {exc}", field="params"
        ) from exc
    return LayerComparison.objects.create(
        layer_a=layer_a,
        layer_b=layer_b,
        comparator_key=comparator.key,
        params=merged,  # store the tolerances, or nobody can later tell what standard
        # this difference was computed to
        summary=result["summary"],
        discrepancies=result["discrepancies"],
        created_by=created_by,
    )


def stored_comparison(layer_a, layer_b, params) -> LayerComparison | None:
    """
    The stored comparison of this pair at exactly these parameters, newest first.

    The parameters are compared in Python. A JSON field holds floats and a string, and the
    question being asked is whether this comparison has already been run to this standard,
    which is a question about values rather than about how a database happens to encode
    them.
    """
    for comparison in LayerComparison.objects.filter(layer_a=layer_a, layer_b=layer_b):
        if comparison.params == params:
            return comparison
    return None


@dataclass
class ComparisonReport:
    created: int = 0
    kept: int = 0
    skipped: list[str] = field(default_factory=list)


def store_default_comparisons(
    corpus,
    *,
    layer_type: str = "note_pitch",
    streams: tuple[str, str] = DEFAULT_STREAM_PAIR,
    version: int = 1,
    created_by=None,
) -> ComparisonReport:
    """
    Store the default comparison for every item in the corpus annotated by both streams.

    A reader who has just imported a corpus has two independent annotations of each
    excerpt and no comparison of them, so the one number the import exists to make
    available takes a manual step to see. Storing it here means the import leaves the
    corpus ready to read.

    Idempotent: an item whose pair already has a comparison at these exact parameters is
    left alone, so importing twice stores one comparison, and a comparison a reader ran to
    their own tolerances is never mistaken for this one.
    """
    report = ComparisonReport()
    params = default_params_for(layer_type)
    if params is None:
        report.skipped.append(f"no comparator is registered for {layer_type}")
        return report

    for item in corpus.items.order_by("song_id"):
        by_stream = {
            layer.stream_key: layer
            for layer in item.layers.filter(
                layer_type=layer_type, stream_key__in=streams, version=version
            )
        }
        pair = [by_stream.get(key) for key in streams]
        if any(layer is None for layer in pair):
            continue
        if stored_comparison(pair[0], pair[1], params) is not None:
            report.kept += 1
            continue
        try:
            run_comparison(pair[0], pair[1], None, created_by=created_by)
        except ComparisonError as exc:
            report.skipped.append(f"{item.song_id}: {exc}")
            continue
        report.created += 1
    return report
