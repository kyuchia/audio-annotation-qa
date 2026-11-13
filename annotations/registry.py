"""
The registry of annotation types, validators and comparators.

The design goal: payloads are pluggable, and comparators and validators register against
a layer type rather than being hard-coded into the models. The models know only that
"there is a layer_type string" and that "the payload is JSON".

Adding an annotation type means one more register_layer_type() call in layer_types.py.
Models, serializers and admin all stay untouched.
"""
from __future__ import annotations

import math
import numbers
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------
# Payload schema: deliberately thin. jsonschema is not pulled in, because the whole
# requirement is "what type should this key be, is it required, is its range sane".
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PayloadField:
    name: str
    kind: str  # "number" | "string" | "bool" | "list" | "any"
    required: bool = True
    minimum: float | None = None
    maximum: float | None = None
    help_text: str = ""

    def check(self, value: Any) -> list[str]:
        errs: list[str] = []
        if self.kind == "number":
            if not isinstance(value, numbers.Real) or isinstance(value, bool):
                return [f"{self.name}: expected a number, got {type(value).__name__}"]
            # nan compares false against every bound, so it would pass both range checks.
            if not math.isfinite(value):
                return [f"{self.name}: expected a finite number, got {value}"]
            if self.minimum is not None and value < self.minimum:
                errs.append(f"{self.name}: {value} is below the minimum {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                errs.append(f"{self.name}: {value} is above the maximum {self.maximum}")
        elif self.kind == "string" and not isinstance(value, str):
            errs.append(f"{self.name}: expected a string")
        elif self.kind == "bool" and not isinstance(value, bool):
            errs.append(f"{self.name}: expected a boolean")
        elif self.kind == "list" and not isinstance(value, (list, tuple)):
            errs.append(f"{self.name}: expected an array")
        return errs


@dataclass(frozen=True)
class LayerTypeSpec:
    key: str
    label: str
    #: A clip-level annotation (audio tagging) has no multiple segments along a timeline
    segment_scope: str = "interval"  # "interval" | "clip"
    payload_fields: tuple[PayloadField, ...] = ()
    #: Fields on layer.params (e.g. tuning_reference_hz), not per-segment
    param_fields: tuple[PayloadField, ...] = ()
    default_comparator: str | None = None
    validators: tuple[str, ...] = ()
    #: Per-validator threshold overrides for this layer type, {validator key: params}.
    #: A validator's own DEFAULTS are written for whole pieces; a layer type whose data
    #: has a different scale (short excerpts, say) states its thresholds here rather than
    #: every caller having to remember to pass them. A ValidationRun's own params still
    #: win, so an override here is a default and not a ceiling.
    validator_params: dict = field(default_factory=dict)
    description: str = ""

    def validate_payload(self, payload: dict) -> list[str]:
        errs: list[str] = []
        if not isinstance(payload, dict):
            return ["payload must be an object"]
        for f in self.payload_fields:
            if f.name not in payload:
                if f.required:
                    errs.append(f"payload is missing the required field {f.name}")
                continue
            errs.extend(f.check(payload[f.name]))
        return errs

    def validate_params(self, params: dict) -> list[str]:
        errs: list[str] = []
        if not isinstance(params, dict):
            return ["params must be an object"]
        for f in self.param_fields:
            if f.name not in params:
                if f.required:
                    errs.append(f"params is missing the required field {f.name}")
                continue
            errs.extend(f.check(params[f.name]))
        return errs


LAYER_TYPES: dict[str, LayerTypeSpec] = {}


def register_layer_type(spec: LayerTypeSpec) -> LayerTypeSpec:
    LAYER_TYPES[spec.key] = spec
    return spec


def get_layer_type(key: str) -> LayerTypeSpec | None:
    return LAYER_TYPES.get(key)


def layer_type_choices() -> list[tuple[str, str]]:
    return [(k, v.label) for k, v in sorted(LAYER_TYPES.items())]


# --------------------------------------------------------------------------
# Validator registration
# --------------------------------------------------------------------------

Scope = str  # "corpus" | "item" | "layer"


@dataclass
class ValidatorSpec:
    key: str
    label: str
    scope: Scope
    fn: Callable[..., Iterable["Finding"]]
    layer_types: tuple[str, ...] | None = None  # None = applies to every type
    default_params: dict = field(default_factory=dict)

    def applies_to_layer_type(self, layer_type: str) -> bool:
        return self.layer_types is None or layer_type in self.layer_types


@dataclass
class Finding:
    """What a validator returns. Deliberately not a model instance: validators never
    touch the database."""

    code: str
    severity: str  # "error" | "warning" | "info"
    message: str
    details: dict = field(default_factory=dict)
    corpus_id: int | None = None
    item_id: int | None = None
    layer_id: int | None = None


VALIDATORS: dict[str, ValidatorSpec] = {}


def register_validator(
    key: str,
    label: str,
    scope: Scope,
    layer_types: tuple[str, ...] | None = None,
    default_params: dict | None = None,
):
    def deco(fn):
        VALIDATORS[key] = ValidatorSpec(
            key=key,
            label=label,
            scope=scope,
            fn=fn,
            layer_types=layer_types,
            default_params=default_params or {},
        )
        return fn

    return deco


def validators_for(scope: Scope, layer_type: str | None = None) -> list[ValidatorSpec]:
    out = [v for v in VALIDATORS.values() if v.scope == scope]
    if layer_type is not None:
        out = [v for v in out if v.applies_to_layer_type(layer_type)]
    return sorted(out, key=lambda v: v.key)


# --------------------------------------------------------------------------
# Comparator registration
# --------------------------------------------------------------------------


@dataclass
class ComparatorSpec:
    key: str
    label: str
    fn: Callable[..., dict]
    layer_types: tuple[str, ...]
    default_params: dict = field(default_factory=dict)


COMPARATORS: dict[str, ComparatorSpec] = {}


def register_comparator(
    key: str, label: str, layer_types: tuple[str, ...], default_params: dict | None = None
):
    def deco(fn):
        COMPARATORS[key] = ComparatorSpec(
            key=key,
            label=label,
            fn=fn,
            layer_types=layer_types,
            default_params=default_params or {},
        )
        return fn

    return deco


def comparator_for(layer_type: str) -> ComparatorSpec | None:
    spec = get_layer_type(layer_type)
    if spec and spec.default_comparator:
        return COMPARATORS.get(spec.default_comparator)
    for c in COMPARATORS.values():
        if layer_type in c.layer_types:
            return c
    return None
