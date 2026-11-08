"""
Persists Findings as ValidationRuns and ValidationIssues.

The validators themselves never touch the database (see checks.py); all writing is
concentrated here in one place.

Which validators run is decided in three places, each owning the question it can answer:

  · the layer type (registry.LayerTypeSpec.validators) selects the layer-scope checks, and
    its validator_params sizes their thresholds for that type's data
  · the corpus (Corpus.skip_validators) switches off corpus- and item-scope checks that
    have no content for it, since those otherwise run for every corpus
  · the caller's params override any threshold for one run

A skipped validator is named in the run's summary rather than simply absent, because "this
check was not run" and "this check found nothing" are different results and a reader cannot
tell them apart from an empty list.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import Corpus, Item, AnnotationLayer, ValidationIssue, ValidationRun
from ..registry import get_layer_type, validators_for


def run_for_layer(layer, *, params=None, run=None, user=None) -> ValidationRun:
    skipped: set[str] = set()
    return _run(
        scope=ValidationRun.Scope.LAYER,
        targets={"layer": layer},
        collect=lambda: _layer_findings(layer, params),
        run=run,
        user=user,
        skipped=skipped,
    )


def run_for_item(item, *, params=None, run=None, user=None) -> ValidationRun:
    skip = _skip_keys(item.corpus)
    skipped: set[str] = set()

    def collect():
        findings = []
        for v in _enabled(validators_for("item"), skip, skipped):
            findings.extend(v.fn(item, params))
        for layer in item.layers.all():
            findings.extend(_layer_findings(layer, params))
        return findings

    return _run(ValidationRun.Scope.ITEM, {"item": item}, collect, run, user, skipped)


def run_for_corpus(corpus, *, params=None, user=None) -> ValidationRun:
    skip = _skip_keys(corpus)
    skipped: set[str] = set()

    def collect():
        findings = []
        for v in _enabled(validators_for("corpus"), skip, skipped):
            findings.extend(v.fn(corpus, params))
        item_validators = list(_enabled(validators_for("item"), skip, skipped))
        for item in corpus.items.prefetch_related("layers"):
            for v in item_validators:
                findings.extend(v.fn(item, params))
            for layer in item.layers.all():
                findings.extend(_layer_findings(layer, params))
        return findings

    return _run(ValidationRun.Scope.CORPUS, {"corpus": corpus}, collect, None, user, skipped)


def _skip_keys(corpus) -> set[str]:
    raw = getattr(corpus, "skip_validators", None) or []
    return {str(k) for k in raw}


def _enabled(specs, skip: set[str], skipped: set[str]):
    for v in specs:
        if v.key in skip:
            skipped.add(v.key)
            continue
        yield v


def _layer_findings(layer, params):
    spec = get_layer_type(layer.layer_type)
    keys = set(spec.validators) if spec else set()
    overrides = (spec.validator_params if spec else {}) or {}
    findings = []
    for v in validators_for("layer", layer.layer_type):
        if keys and v.key not in keys:
            continue
        # The layer type's thresholds sit under the caller's: an override passed to this run
        # is a deliberate instruction for this run and has to win.
        merged = {**overrides.get(v.key, {}), **(params or {})}
        findings.extend(v.fn(layer, merged or None))
    return findings


@transaction.atomic
def _run(scope, targets, collect, run, user, skipped) -> ValidationRun:
    if run is None:
        run = ValidationRun.objects.create(
            scope=scope,
            corpus=targets.get("corpus"),
            item=targets.get("item"),
            layer=targets.get("layer"),
            triggered_by=user,
        )
    findings = list(collect())
    ValidationIssue.objects.bulk_create(
        [
            ValidationIssue(
                run=run,
                code=f.code,
                severity=f.severity,
                message=f.message,
                details=f.details,
                corpus_id=f.corpus_id,
                item_id=f.item_id,
                layer_id=f.layer_id,
            )
            for f in findings
        ],
        batch_size=500,
    )
    counts: dict[str, int] = {}
    by_code: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
        by_code[f.code] = by_code.get(f.code, 0) + 1
    run.validator_keys = sorted({f.code for f in findings})
    run.summary = {
        "by_severity": counts,
        "by_code": by_code,
        "total": len(findings),
        # Named, not omitted: an empty finding list for a skipped check would otherwise read
        # as a clean result.
        "skipped_validators": sorted(skipped),
    }
    run.finished_at = timezone.now()
    run.save(update_fields=["validator_keys", "summary", "finished_at", "updated_at"])
    return run


def run_for_target(obj, *, params=None, user=None) -> ValidationRun:
    if isinstance(obj, Corpus):
        return run_for_corpus(obj, params=params, user=user)
    if isinstance(obj, Item):
        return run_for_item(obj, params=params, user=user)
    if isinstance(obj, AnnotationLayer):
        return run_for_layer(obj, params=params, user=user)
    raise TypeError(f"Unsupported validation target: {type(obj).__name__}")
