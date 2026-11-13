"""
Validation of caller-supplied tuning knobs at the API boundary.

Validators and comparators are given a flat dict of thresholds. Internally those are
trusted: a validator compares `tail_gap > p["coverage_gap_sec"]` without asking whether
the value is a number, and `_match` raises outright on a match_strategy it does not
recognise. That is the right shape for internal code — a wrong value is a programming
error and should be loud.

It is the wrong shape for a value that arrived as JSON from a client. A string where a
float belongs raises TypeError deep inside a comparison; an unknown strategy raises
ValueError inside the matcher; a params value that is not an object at all fails at the
`{**defaults, **params}` merge. All three surface as a 500, which blames the server for
input the client chose.

So the boundary checks the shape first and the internals keep their raises. Each knob is
checked against the type of its own default, which means this file needs no table of its
own to drift out of step with the real ones: the registry and the validator module already
state every accepted key and its type, and that is what gets read.
"""
from __future__ import annotations

import math
import numbers

#: Keys accepted on top of a validator's declared defaults. A ValidationRun's params are
#: merged into every validator that runs, so a key belonging to one check is legitimately
#: present when another runs. The union of every validator's defaults is the accepted set.
_UNKNOWN_KEY_HINT = (
    "Pass only the thresholds this endpoint documents; an unrecognised key is almost "
    "always a typo, and silently ignoring it would leave the caller believing a threshold "
    "had been applied."
)


class ParamError(ValueError):
    """Caller-supplied params are the wrong shape. Translate to 400, never 500."""


def clean_params(raw, *, allowed: dict, where: str) -> dict:
    """
    Check `raw` against `allowed`, a {key: default value} dict whose values define the
    expected types. Returns the params to use (an empty dict when nothing was supplied).

    Raises ParamError with a message naming the offending key, what was expected and what
    arrived. The caller is a person debugging a request, so the message has to say which of
    their keys was wrong rather than that something, somewhere, was not a float.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ParamError(
            f"{where} must be a JSON object mapping a threshold name to a value; got "
            f"{_describe(raw)}."
        )

    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ParamError(
            f"{where} does not accept {unknown}. Accepted: {sorted(allowed)}. "
            f"{_UNKNOWN_KEY_HINT}"
        )

    cleaned: dict = {}
    for key, value in raw.items():
        cleaned[key] = _check_one(key, value, allowed[key], where)
    return cleaned


def _check_one(key: str, value, default, where: str):
    # bool before Real: in Python a bool *is* an int, so a numeric knob would accept True
    # and quietly compare as 1.
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ParamError(
                f"{where}: {key} must be true or false; got {_describe(value)}."
            )
        return value

    if isinstance(default, numbers.Real):
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise ParamError(
                f"{where}: {key} must be a number; got {_describe(value)}."
            )
        # 1e999 is valid JSON and arrives as infinity; a threshold of infinity or nan
        # switches its check off while the run still records that it was applied.
        if not math.isfinite(value):
            raise ParamError(
                f"{where}: {key} must be a finite number; got {_describe(value)}."
            )
        return float(value)

    if isinstance(default, str):
        if not isinstance(value, str):
            raise ParamError(
                f"{where}: {key} must be a string; got {_describe(value)}."
            )
        return value

    # Nothing else is a threshold today. Refusing rather than passing it through keeps an
    # unvalidated value from reaching code that assumes it was checked here.
    raise ParamError(
        f"{where}: {key} is not a parameter this endpoint can accept from a request."
    )


def check_choice(key: str, value, choices, where: str) -> None:
    """A string knob whose accepted values are a fixed set, e.g. match_strategy."""
    if value not in choices:
        raise ParamError(
            f"{where}: {key} must be one of {sorted(choices)}; got {value!r}."
        )


def validator_params(raw) -> dict:
    """Params for a ValidationRun, checked against every validator threshold there is."""
    from .validators.checks import DEFAULTS

    return clean_params(raw, allowed=DEFAULTS, where="params")


def comparator_params(raw, comparator) -> dict:
    """
    Params for a comparison, checked against the comparator's own declared defaults.

    match_strategy gets a membership check as well as a type check: it decides which pairs
    are considered at all, and _match refuses an unrecognised value rather than guessing,
    so an unchecked string here would become a 500 further in.
    """
    from .comparators.pitch_interval import MATCH_STRATEGIES

    cleaned = clean_params(raw, allowed=comparator.default_params, where="params")
    if "match_strategy" in cleaned:
        check_choice("match_strategy", cleaned["match_strategy"], MATCH_STRATEGIES, "params")
    return cleaned


def _describe(value) -> str:
    """Name the offending value without echoing a large payload back at the caller."""
    if isinstance(value, (dict, list, tuple)):
        return f"a JSON {'object' if isinstance(value, dict) else 'array'}"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"the boolean {str(value).lower()}"
    text = repr(value)
    return text if len(text) <= 40 else f"{text[:37]}..."
