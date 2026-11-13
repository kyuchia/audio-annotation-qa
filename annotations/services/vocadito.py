"""
The vocadito importer.

vocadito ships a flat, per-annotation-type tree rather than one directory per excerpt:

    Audio/vocadito_{n}.wav                       mono, 44.1 kHz, 9–39 s
    Annotations/F0/vocadito_{n}_f0.csv           time_sec, freq_hz   (0 = unvoiced)
    Annotations/Notes/vocadito_{n}_notesA1.csv   onset_sec, pitch_hz, duration_sec
    Annotations/Notes/vocadito_{n}_notesA2.csv   onset_sec, pitch_hz, duration_sec
    Annotations/Lyrics/vocadito_{n}_lyrics.txt
    vocadito_metadata.csv                        track_id, singer_id, average_pitch, language

Three things about that layout shape this importer:

1. The item key is the track number, which appears in the filename and in the metadata
   CSV's track_id. Both sides hold the same integer, so matching is an exact key lookup —
   no name folding, and the diacritic-deletion problem that per-directory layouts have
   does not arise here.

2. Every excerpt's audio lives in one shared directory, so the audio *file* is what
   identifies an item, not a directory of its own. disk_path is "Audio" for all of them and
   audio_filename carries the track; see the uniq_item_audio_path constraint on Item.

3. The audio is the recording the annotations were made against, not a stand-in
   file, so audio_is_proxy is false and coverage is measured against the header. The
   metadata CSV has no duration column at all, which is the honest state of affairs: there
   is no claimed duration to check the header against, and metadata_duration_agreement
   correctly says nothing rather than inventing a reference.

A1 and A2 are parallel, independent annotations of the same audio by two annotators. They
are imported under separate stream keys, which is what keeps them independent: version,
parent and status are all scoped per stream key, so each is version 1 with no parent and
neither is ever marked SUPERSEDED by the other. Disagreement between them is annotator
disagreement, not an error by either one, and nothing here treats one as the correction of
the other. The annotator is also written into provenance, so the identity survives
independently of the stream key's spelling.

Frame-level F0 is deliberately **not** imported; see FRAME_F0_RATIONALE below.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from ..importers import is_ignored_file, parse_f0_csv, probe_audio
from ..importers.audio_header import AudioProbeError
from ..importers.f0_csv import F0ParseError
from ..models import AnnotationLayer, Corpus, Item
from .ingest import IngestError, create_layer_with_segments, sha256_file

AUDIO_DIR = "Audio"
F0_DIR = "Annotations/F0"
NOTES_DIR = "Annotations/Notes"
LYRICS_DIR = "Annotations/Lyrics"
METADATA_CSV = "vocadito_metadata.csv"

#: The two independent annotators. Order is listing order only; it implies no precedence,
#: no lineage, and no preferred reference for a comparison.
ANNOTATORS = ("A1", "A2")

#: The note files are (onset_sec, pitch_hz, duration_sec), per Annotations/README.txt. Named
#: here rather than passed by the caller: it is a fact about this dataset, and a caller who
#: had to supply it could supply the wrong one.
NOTES_CONTRACT = "onset_hz_duration"

#: The frame-level F0 contract, for reference. Not used: see FRAME_F0_RATIONALE.
F0_CONTRACT = {"onset": 0, "hz": 1}

AUDIO_RE = re.compile(r"^vocadito_(\d+)\.wav$", re.IGNORECASE)

#: Why the per-frame F0 is left out of the import.
#:
#: It is a different kind of annotation from everything the Segment model is built for. A
#: note layer is a few dozen intervals; an F0 track is one evenly spaced sample every
#: 256/44100 s — 1,500 to 6,700 rows per excerpt, around 120,000 for the full 40 — each a
#: single instant rather than an interval. Storing them as segments would be dishonest in
#: three separate ways at once:
#:
#:   · Every segment would be zero-length, so annotation_coverage would compute 0% coverage
#:     for a file that is in fact densely annotated, and segments_monotonic would be
#:     measuring nothing.
#:   · note_pitch means "a note was sung here at this pitch". A frame says "the pitch
#:     estimate at this instant was this", which is not the same claim: the 40% of frames
#:     marked 0 are unvoiced, and a contour crosses between notes without any note boundary.
#:   · The note comparator matches onsets within a tolerance. Against a 0.0058 s grid that
#:     pairs frames with whichever neighbour happens to be nearest, and the A1-vs-A2
#:     comparison this corpus exists to show would be swamped by it.
#:
#: Frame-level pitch wants its own layer type — a contour, either clip-scope with the track
#: in the payload or a dedicated dense representation — and choosing between those is a
#: modelling decision, not an import detail. So the files are left on disk, reported as
#: present and unimported, and the registry gains no half-type to accommodate them.
FRAME_F0_RATIONALE = (
    "Frame-level F0 is not imported: it is a dense contour on a 256/44100 s grid "
    "(1.5k-6.7k rows per excerpt), not a set of note intervals, and storing it as "
    "zero-length segments would make coverage and onset matching report nonsense. It "
    "wants its own layer type."
)


@dataclass
class VocaditoReport:
    items_created: int = 0
    items_updated: int = 0
    layers_created: int = 0
    layers_unchanged: int = 0
    segments_created: int = 0
    tracks_seen: int = 0
    metadata_rows: int = 0
    f0_files_present: int = 0
    lyrics_files_present: int = 0
    unmatched_metadata_rows: list[str] = field(default_factory=list)
    tracks_without_metadata: list[str] = field(default_factory=list)
    probe_failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    frame_f0: str = FRAME_F0_RATIONALE

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def load_metadata(csv_path: str | Path) -> dict[int, dict]:
    """vocadito_metadata.csv keyed by track_id. Raises when the key column is absent."""
    path = Path(csv_path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [dict(r) for r in csv.DictReader(fh)]
    if not rows:
        return {}
    if "track_id" not in rows[0]:
        raise ValueError(
            f"{path.name} has no track_id column, so its rows cannot be matched to the "
            f"audio files. Columns present: {sorted(rows[0])}"
        )
    out: dict[int, dict] = {}
    for row in rows:
        try:
            out[int(str(row["track_id"]).strip())] = row
        except (TypeError, ValueError):
            continue
    return out


@transaction.atomic
def ingest_vocadito(corpus: Corpus, *, dry_run: bool = False, **_ignored) -> VocaditoReport:
    report = VocaditoReport()
    root = Path(corpus.root_path)
    audio_root = root / AUDIO_DIR
    notes_root = root / NOTES_DIR
    if not audio_root.is_dir():
        raise FileNotFoundError(f"Audio directory not found: {audio_root}")
    if not notes_root.is_dir():
        raise FileNotFoundError(f"Note annotation directory not found: {notes_root}")

    metadata_path = Path(corpus.metadata_csv_path) if corpus.metadata_csv_path else root / METADATA_CSV
    metadata: dict[int, dict] = {}
    if metadata_path.exists():
        metadata = load_metadata(metadata_path)
        report.metadata_rows = len(metadata)
    else:
        report.warnings.append(
            f"no metadata CSV at {metadata_path}; every item will carry a provisional song_id"
        )

    tracks = _discover_tracks(audio_root)
    report.tracks_seen = len(tracks)
    if not tracks:
        raise FileNotFoundError(f"No vocadito_N.wav files under {audio_root}")
    # Before anything is written. Two filenames can carry the same track number — zero
    # padding is enough, since int("08") == int("8") — and the number is what the song_id
    # is built from, so the second one would collide on the uniq_item_song_id constraint
    # several items into the import. That surfaces as an IntegrityError that rolls the whole
    # transaction back and names a constraint rather than the two files, so the check runs
    # here where both filenames are still in hand.
    _refuse_duplicate_track_numbers(tracks)

    for number, wav in tracks:
        row = metadata.get(number)
        song_id = f"vocadito_{number}"
        if row is None:
            report.tracks_without_metadata.append(song_id)
            report.warnings.append(
                f"{song_id}: vocadito_metadata.csv has no row with track_id={number}, so "
                f"singer and language are unknown and the song_id is provisional"
            )

        item, created = Item.objects.update_or_create(
            corpus=corpus,
            disk_path=AUDIO_DIR,
            audio_filename=wav.name,
            defaults={
                "song_id": song_id,
                "song_id_is_provisional": row is None,
                "display_name": _display_name(number, row),
                "metadata_row": row,
                # The track number in the filename and metadata's track_id are the same
                # integer, so this is a key lookup rather than an inference from a name.
                "metadata_match_method": "exact" if row is not None else "none",
                # The audio is the recording these annotations were made against, so
                # coverage is measured against the header and nothing is a stand-in.
                "audio_is_proxy": False,
                # vocadito_metadata.csv has no duration column, so there is no claimed
                # length. Left null rather than filled from the header, which would turn a
                # measurement into a fake claim and make the agreement check circular.
                "reference_duration_sec": None,
            },
        )
        report.items_created += int(created)
        report.items_updated += int(not created)

        _probe(item, wav, report)
        item.save()

        if (root / F0_DIR / f"vocadito_{number}_f0.csv").exists():
            report.f0_files_present += 1
        if (root / LYRICS_DIR / f"vocadito_{number}_lyrics.txt").exists():
            report.lyrics_files_present += 1

        for annotator in ANNOTATORS:
            _ingest_notes(item, notes_root, number, annotator, report)

    for number, row in sorted(metadata.items()):
        if not any(n == number for n, _ in tracks):
            report.unmatched_metadata_rows.append(f"track_id={number}")
            report.warnings.append(
                f"vocadito_metadata.csv lists track_id={number} but no "
                f"{AUDIO_DIR}/vocadito_{number}.wav exists"
            )

    if dry_run:
        transaction.set_rollback(True)
    return report


def _refuse_duplicate_track_numbers(tracks: list[tuple[int, Path]]) -> None:
    by_number: dict[int, list[str]] = {}
    for number, path in tracks:
        by_number.setdefault(number, []).append(path.name)
    clashes = {n: names for n, names in by_number.items() if len(names) > 1}
    if not clashes:
        return
    detail = "; ".join(
        f"track {n} is claimed by {' and '.join(sorted(names))}" for n, names in sorted(clashes.items())
    )
    raise ValueError(
        f"Two or more audio files carry the same track number, so they would all become the "
        f"item vocadito_N and collide: {detail}. The track number in the filename is this "
        f"corpus's identity, so the files have to be distinguishable by it — rename or remove "
        f"the duplicates before importing."
    )


def _discover_tracks(audio_root: Path) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for p in sorted(audio_root.iterdir()):
        if not p.is_file() or is_ignored_file(p):
            continue
        m = AUDIO_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return sorted(found, key=lambda t: t[0])


def _display_name(number: int, row: dict | None) -> str:
    """vocadito excerpts have no titles, so the name states what is actually known."""
    if not row:
        return f"vocadito {number}"
    singer = (row.get("singer_id") or "").strip()
    language = (row.get("language") or "").strip()
    bits = [b for b in (singer, language) if b and b.lower() != "none"]
    return f"vocadito {number}" + (f" ({', '.join(bits)})" if bits else "")


def _probe(item: Item, wav: Path, report: VocaditoReport) -> None:
    """
    Audio specs always come from the header, never from metadata.

    A failure clears the four header fields as well as recording the error. They may hold
    readings from an earlier run against a file that has since been replaced or truncated,
    and leaving them would make the item assert two contradictory things at once:
    media_is_probed true, so the media_probed validator stays quiet and the exporter
    computes frame indices, while media_probe_error says the file could not be read. The
    honest state after a failed probe is "no specs", which is what media_probed is there to
    report.
    """
    try:
        item.record_media_info(probe_audio(wav))
    except AudioProbeError as exc:
        item.sample_rate = None
        item.duration_sec = None
        item.channels = None
        item.audio_format = ""
        item.media_probe_error = str(exc)
        item.media_probed_at = timezone.now()
        report.probe_failures.append(f"{item.song_id}: {exc}")


def _ingest_notes(
    item: Item, notes_root: Path, number: int, annotator: str, report: VocaditoReport
) -> None:
    path = notes_root / f"vocadito_{number}_notes{annotator}.csv"
    if not path.exists():
        report.warnings.append(f"{item.song_id}: no {annotator} note file at {path}")
        return

    digest = sha256_file(path)
    if _already_imported(item, annotator, digest):
        # Re-running the importer on unchanged files must not mint new versions: a version
        # number is a claim that something changed, and inflating it on every run makes the
        # lineage meaningless. A changed file does get a new version, which is the point.
        report.layers_unchanged += 1
        return

    try:
        parsed = parse_f0_csv(path, contract=NOTES_CONTRACT)
    except F0ParseError as exc:
        report.errors.append(f"{item.song_id}/{annotator}: {exc}")
        return
    if not parsed.rows:
        report.warnings.append(f"{item.song_id}/{annotator}: the note CSV has no usable rows")
        return
    for w in parsed.warnings:
        report.warnings.append(f"{item.song_id}/{annotator}: {w}")

    try:
        create_layer_with_segments(
            item=item,
            layer_type="note_pitch",
            # The stream key keeps the two annotations parallel rather than sequential.
            stream_key=annotator,
            rows=parsed.rows,
            author_kind=AnnotationLayer.AuthorKind.IMPORT,
            params={},
            provenance={
                "source_file": str(path),
                "source_sha256": digest,
                "parser": "parse_f0_csv",
                "column_contract": parsed.contract_key,
                "column_map": parsed.column_map,
                "skipped_lines": len(parsed.skipped_lines),
                "imported_at": timezone.now().isoformat(),
                "representation": "csv_hz_authoritative",
                "dataset": "vocadito",
                # Recorded here as well as in stream_key, so the annotator's identity does
                # not depend on how the stream key happens to be spelled.
                "annotator": annotator,
                "annotator_role": "independent",
                "parallel_annotators": list(ANNOTATORS),
                "independence_note": (
                    f"{' and '.join(ANNOTATORS)} annotated this excerpt separately. "
                    f"Neither revises the other: they are parallel layers, each version 1 "
                    f"with no parent, and a disagreement between them is annotator "
                    f"disagreement rather than an error by either one."
                ),
            },
        )
    except IngestError as exc:
        report.errors.append(f"{item.song_id}/{annotator}: {exc}")
        return

    report.layers_created += 1
    report.segments_created += len(parsed.rows)


def _already_imported(item: Item, annotator: str, digest: str) -> bool:
    return (
        item.layers.filter(
            layer_type="note_pitch",
            stream_key=annotator,
            provenance__source_sha256=digest,
        )
        .exists()
    )
