"""
Note/F0 CSV parsing.

The caller states the column contract; the parser never guesses it. Two layouts are
registered, because both occur in the wild and they are indistinguishable by shape — both
are three unlabelled numeric columns starting with a time in seconds:

    onset_offset_hz    onset_sec, offset_sec, freq_hz
    onset_hz_duration  onset_sec, pitch_hz, duration_sec   (vocadito's note annotations)

Reading one as the other does not fail at the row level. It produces rows that parse, sort
and import: with onset_hz_duration data read as onset_offset_hz, every "offset" is a
frequency in the hundreds and every "hz" is a duration of a few tenths of a second. The
result is a layer of plausible-looking garbage — notes a few hundred seconds long, pitched
at a third of a Hz — and nothing downstream can tell it apart from real data.

So a contract is not a hint, it is a claim that gets checked. Every row is tested against
what the contract says it should contain (a pitch inside the registered Hz range, a
positive duration, an offset at or after its onset), and a file that contradicts its
contract on more than CONTRACT_VIOLATION_LIMIT of its rows is refused outright — with the
contract that does fit named in the message, when one of the others does.

It returns plain dicts rather than model instances. The parser never touches the database,
which is what makes it testable on its own.
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

#: The plausible pitch range, matching the note_pitch payload field in layer_types.py. A
#: value outside it is the signal that the hz column is not a pitch at all.
MIN_HZ = 20.0
MAX_HZ = 8000.0

#: A contract is refused when more than this fraction of a file's numeric rows contradict
#: it. Not "any row": one stray outlier in real data must not fail an import. A
#: systematically wrong mapping contradicts nearly every row, so the two cases are far
#: apart and the threshold does not need to be delicate.
CONTRACT_VIOLATION_LIMIT = 0.2

#: Below this many rows there is no "stray outlier" reading available, so one violation is
#: already decisive.
SMALL_FILE_ROWS = 5


class F0ParseError(ValueError):
    """The file contradicts the column contract it was read under."""


@dataclass(frozen=True)
class ColumnContract:
    """What each column of a headerless file holds, by position."""

    key: str
    #: One field name per column: "onset" | "offset" | "hz" | "duration"
    columns: tuple[str, ...]
    label: str

    @property
    def fields(self) -> set[str]:
        return set(self.columns)


CONTRACTS: dict[str, ColumnContract] = {
    "onset_offset_hz": ColumnContract(
        key="onset_offset_hz",
        columns=("onset", "offset", "hz"),
        label="onset_sec, offset_sec, freq_hz",
    ),
    "onset_hz_duration": ColumnContract(
        key="onset_hz_duration",
        columns=("onset", "hz", "duration"),
        label="onset_sec, pitch_hz, duration_sec",
    ),
}

#: Header spellings that identify each field, for files that do carry a header row.
#: Matching is case-insensitive and ignores spaces, underscores and hyphens.
ALIASES: dict[str, set[str]] = {
    "onset": {"onset", "onsetsec", "start", "startsec", "time", "timesec", "begin"},
    "offset": {"offset", "offsetsec", "end", "endsec", "stop"},
    "hz": {"hz", "freq", "freqhz", "frequency", "frequencyhz", "pitch", "pitchhz", "f0", "f0hz"},
    "duration": {"duration", "durationsec", "dur", "length", "lengthsec"},
}

#: Pass this as the contract to read the field layout from the file's own header row.
FROM_HEADER = "header"


@dataclass
class F0ParseResult:
    rows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped_lines: list[tuple[int, str]] = field(default_factory=list)
    #: Which contract the rows were actually read under, for provenance.
    contract_key: str = ""
    #: Per-position field names as resolved, so provenance records the mapping and not
    #: merely its name.
    column_map: dict[str, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.rows)


def parse_f0_csv(
    source: str | Path | io.IOBase,
    *,
    contract: str | ColumnContract | Mapping[str, int | str],
    min_hz: float = MIN_HZ,
    max_hz: float = MAX_HZ,
) -> F0ParseResult:
    """
    Parse a note/F0 CSV under an explicit column contract.

    contract may be:
      · a key in CONTRACTS ("onset_offset_hz", "onset_hz_duration")
      · a ColumnContract
      · FROM_HEADER, to map the fields from the file's own header row by name
      · a mapping {field: column}, where a column is a 0-based index or a header name;
        field is one of onset / offset / hz / duration

    Raises F0ParseError when the file contradicts the contract, when the contract is
    unusable, or when a named column is absent from the header.
    """
    text = _read_text(source)
    rows = list(csv.reader(io.StringIO(text)))
    result = F0ParseResult()

    column_map, data_rows, header = _resolve_columns(contract, rows, result)
    result.column_map = column_map
    if header is not None:
        result.warnings.append(f"read the column layout from the header row: {','.join(header)}")

    needed = max(column_map.values()) + 1
    index = 0
    violations: list[str] = []
    #: Line numbers of rows with at least one violation, counted once each. One row can
    #: break the contract in two ways at once — an offset before its onset, which is swapped
    #: rather than skipped, and then a negative onset — and counting violation strings would
    #: report "2 of 1 rows (200%)".
    violating_rows: set[int] = set()
    numeric_rows = 0
    raw_values: list[list[float]] = []

    for lineno, raw in enumerate(data_rows, start=(2 if header is not None else 1)):
        cells = [c.strip() for c in raw]
        while cells and cells[-1] == "":
            cells.pop()
        if not cells:
            continue
        if len(cells) < needed:
            result.skipped_lines.append((lineno, ",".join(raw)))
            continue
        try:
            values = [float(cells[i]) for i in column_map.values()]
        except ValueError:
            # A parse failure on the first data line of a headerless file usually means
            # someone added a header row. Record it; do not swallow it silently.
            if lineno == 1:
                result.warnings.append(
                    f"line 1 is not numeric; treated as a header and skipped: {','.join(raw)}"
                )
            else:
                result.skipped_lines.append((lineno, ",".join(raw)))
            continue

        got = dict(zip(column_map.keys(), values))
        # float() reads "nan" and "inf" as numbers. Neither is a time or a pitch, every
        # comparison against one is false so the contract checks below let it through, and
        # a stored one cannot be written out as JSON again. One is enough to refuse the
        # file: a row count threshold is for values that are implausible, and these are
        # impossible.
        for name, value in got.items():
            if not math.isfinite(value):
                raise F0ParseError(
                    f"line {lineno}: the {name} column holds "
                    f"{cells[column_map[name]]!r}. Every time and pitch in an annotation "
                    f"file must be a finite number."
                )

        numeric_rows += 1
        # Kept unmapped so that, if this contract is refused, the first three columns can be
        # tried against the other registered contracts to name the one that does fit.
        try:
            raw_values.append([float(c) for c in cells[:3]])
        except ValueError:
            pass

        onset = got["onset"]
        if "duration" in got:
            duration = got["duration"]
            offset = onset + duration
            if duration <= 0:
                violations.append(f"line {lineno}: duration={duration:g} is not positive")
                violating_rows.add(lineno)
                result.skipped_lines.append((lineno, f"duration={duration:g} is not positive"))
                continue
        else:
            offset = got["offset"]
            if offset < onset:
                violations.append(f"line {lineno}: offset={offset:g} is before onset={onset:g}")
                violating_rows.add(lineno)
                result.warnings.append(f"line {lineno}: offset < onset, the two were swapped")
                onset, offset = offset, onset

        if onset < 0:
            violations.append(f"line {lineno}: onset={onset:g} is negative")
            violating_rows.add(lineno)
            result.skipped_lines.append((lineno, f"onset={onset:g} is negative"))
            continue

        hz = got["hz"]
        if hz == 0.0:
            # Frame-level F0 marks an unvoiced frame with 0. That is a real value, not a
            # contract violation, and it carries no note.
            result.skipped_lines.append((lineno, "hz=0 marks an unvoiced frame"))
            continue
        if not (min_hz <= hz <= max_hz):
            violations.append(
                f"line {lineno}: hz={hz:g} is outside the plausible pitch range "
                f"{min_hz:g}–{max_hz:g} Hz"
            )
            violating_rows.add(lineno)
            result.skipped_lines.append((lineno, f"hz={hz:g} is not a plausible pitch"))
            continue

        result.rows.append(
            {"index": index, "start_sec": onset, "end_sec": offset, "payload": {"hz": hz}}
        )
        index += 1

    _refuse_if_contract_contradicted(
        result,
        violations=violations,
        violating_rows=violating_rows,
        numeric_rows=numeric_rows,
        raw_values=raw_values,
        source=source,
        column_map=column_map,
        min_hz=min_hz,
        max_hz=max_hz,
    )

    for i in range(1, len(result.rows)):
        if result.rows[i]["start_sec"] < result.rows[i - 1]["start_sec"]:
            result.warnings.append(f"row {i}: onset is not increasing (source file is unsorted)")
            break
    return result


# ---------------------------------------------------------------------------
# Contract resolution
# ---------------------------------------------------------------------------


def _resolve_columns(contract, rows, result) -> tuple[dict[str, int], list[list[str]], list[str] | None]:
    """Returns ({field: column index}, data rows, header row or None)."""
    if isinstance(contract, str) and contract != FROM_HEADER:
        spec = CONTRACTS.get(contract)
        if spec is None:
            raise F0ParseError(
                f"Unknown column contract {contract!r}. Registered: {sorted(CONTRACTS)}, "
                f"or {FROM_HEADER!r} to read the layout from the file's header row, or a "
                f"mapping such as {{'onset': 0, 'hz': 1, 'duration': 2}}."
            )
        contract = spec

    if isinstance(contract, ColumnContract):
        result.contract_key = contract.key
        _require_fields(contract.fields, contract.key)
        return {f: i for i, f in enumerate(contract.columns)}, rows, None

    if contract == FROM_HEADER:
        if not rows:
            raise F0ParseError("The file is empty, so it has no header row to read.")
        header = rows[0]
        found = _fields_from_header(header)
        _require_fields(set(found), FROM_HEADER)
        result.contract_key = "header:" + ",".join(f"{k}={v}" for k, v in sorted(found.items()))
        return found, rows[1:], header

    if isinstance(contract, Mapping):
        named = {f: c for f, c in contract.items() if isinstance(c, str)}
        resolved: dict[str, int] = {f: c for f, c in contract.items() if isinstance(c, int)}
        header = None
        if named:
            if not rows:
                raise F0ParseError(
                    "The contract names columns by header, but the file is empty."
                )
            header = rows[0]
            lookup = {_normalise(h): i for i, h in enumerate(header)}
            for f, name in named.items():
                if _normalise(name) not in lookup:
                    raise F0ParseError(
                        f"The contract maps {f} to the column {name!r}, which is not in the "
                        f"header row: {header}"
                    )
                resolved[f] = lookup[_normalise(name)]
        unknown = set(resolved) - set(ALIASES)
        if unknown:
            raise F0ParseError(
                f"Unknown contract fields {sorted(unknown)}. Valid fields: {sorted(ALIASES)}."
            )
        _require_fields(set(resolved), "the mapping given")
        result.contract_key = "mapping:" + ",".join(f"{k}={v}" for k, v in sorted(resolved.items()))
        return resolved, (rows[1:] if header is not None else rows), header

    raise F0ParseError(
        f"A column contract is required; got {type(contract).__name__}. Pass a key from "
        f"{sorted(CONTRACTS)}, a ColumnContract, {FROM_HEADER!r}, or a field mapping."
    )


def _require_fields(fields: set[str], where: str) -> None:
    if "onset" not in fields or "hz" not in fields:
        raise F0ParseError(
            f"{where} must supply both an onset and an hz column; got {sorted(fields)}."
        )
    if "offset" not in fields and "duration" not in fields:
        raise F0ParseError(
            f"{where} must supply either an offset or a duration column so a segment has an "
            f"end; got {sorted(fields)}."
        )
    if "offset" in fields and "duration" in fields:
        raise F0ParseError(
            f"{where} supplies both an offset and a duration column. Two sources for one "
            f"end time can disagree, and there would be no way to say which the file meant."
        )


def _fields_from_header(header: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for i, cell in enumerate(header):
        norm = _normalise(cell)
        for fieldname, names in ALIASES.items():
            if norm in names and fieldname not in found:
                found[fieldname] = i
    if "offset" in found and "duration" in found:
        # Both present in a real header is not ambiguous — the file says it has both — but
        # _require_fields refuses that combination, so prefer the explicit end time.
        found.pop("duration")
    return found


def _normalise(s: str) -> str:
    return "".join(c for c in s.strip().lower() if c.isalnum())


def _refuse_if_contract_contradicted(
    result,
    *,
    violations: list[str],
    violating_rows: set[int],
    numeric_rows: int,
    raw_values,
    source,
    column_map: dict[str, int],
    min_hz: float,
    max_hz: float,
) -> None:
    if not violations or not numeric_rows:
        return
    # Rows, not violation strings: a row that breaks the contract twice is still one row,
    # and a rate derived from the strings can exceed 100%.
    failed = len(violating_rows)
    rate = failed / numeric_rows
    decisive = numeric_rows < SMALL_FILE_ROWS or rate > CONTRACT_VIOLATION_LIMIT
    if not decisive:
        result.warnings.extend(violations[:20])
        if len(violations) > 20:
            result.warnings.append(f"... and {len(violations) - 20} further row problems")
        return

    name = getattr(source, "name", None) or str(source)
    better = _best_fitting_contract(
        raw_values, exclude_map=column_map, min_hz=min_hz, max_hz=max_hz
    )
    hint = ""
    if better is not None:
        hint = (
            f" The data fits the {better.key!r} contract ({better.label}) instead; "
            f"re-read it under that contract if that is what this file holds."
        )
    raise F0ParseError(
        f"{name} contradicts the {result.contract_key!r} column contract on "
        f"{failed} of {numeric_rows} rows ({rate:.0%}). Refusing to import rather "
        f"than storing data read under the wrong mapping.{hint} First problems: "
        + "; ".join(violations[:3])
    )


def _best_fitting_contract(
    raw_values, *, exclude_map: dict[str, int], min_hz: float, max_hz: float
) -> ColumnContract | None:
    """
    Which registered contract the raw columns actually satisfy, if any.

    Excluded by resolved column map rather than by contract key. The same positional
    mapping can arrive named ("onset_offset_hz") or spelled out ({"onset": 0, ...}), and
    comparing keys misses the second form — so the hint could recommend re-reading the file
    under a mapping identical to the one that was just refused.

    The caller's own hz range is used, not the module defaults: judging a candidate by a
    wider range than the one that rejected the file is how a contract gets recommended
    whose pitch column the caller would also have thrown out.
    """
    best: tuple[float, ColumnContract] | None = None
    for spec in CONTRACTS.values():
        if len(spec.columns) != 3:
            continue
        spec_map = {f: i for i, f in enumerate(spec.columns)}
        if spec_map == exclude_map:
            continue
        ok = 0
        total = 0
        for vals in raw_values:
            if len(vals) < 3:
                continue
            total += 1
            got = dict(zip(spec.columns, vals))
            if got["onset"] < 0 or not (min_hz <= got["hz"] <= max_hz):
                continue
            if "duration" in got and got["duration"] <= 0:
                continue
            if "offset" in got and got["offset"] < got["onset"]:
                continue
            ok += 1
        if not total:
            continue
        rate = ok / total
        if rate > 1.0 - CONTRACT_VIOLATION_LIMIT and (best is None or rate > best[0]):
            best = (rate, spec)
    return best[1] if best else None


def _read_text(source) -> str:
    if isinstance(source, (str, Path)) and Path(source).exists():
        return Path(source).read_text(encoding="utf-8-sig")
    if hasattr(source, "read"):
        raw = source.read()
        return raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    return str(source)
