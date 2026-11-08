"""
Quality checks. Each validator is a pure function: it takes a model instance and returns
a list of Findings.

The principle: a validator never writes to the database, never deletes data and never
modifies data. It only reports. Deciding whether an anomaly is acceptable is the
reviewer's job, not the checker's.

What the checks here settle without asking a human: sample rate, duration and channel
count, all read from the audio header; disagreements between the files on disk and the
metadata CSV; and whether two files that should describe the same recording actually do.
Anything that needs a judgement is reported and left to a reviewer.
"""
from __future__ import annotations

from pathlib import Path

from ..registry import Finding, register_validator

# --- Default thresholds for each check. All of them can be overridden by a
# --- ValidationRun's params, and a layer type can set its own defaults in
# --- registry.LayerTypeSpec.validator_params.
DEFAULTS = {
    # Allowed slack between the end of the last annotation and the reference length. Sized
    # for whole recordings of several minutes; a layer type holding short excerpts has to
    # override it, or the check can never fire at all.
    "coverage_gap_sec": 5.0,
    # An unannotated stretch in the middle of a recording longer than this is reported.
    "internal_gap_sec": 10.0,
    # How far a linked recording's own duration may sit from the length the metadata claims
    # for that recording. The same slack metadata_duration_agreement uses.
    "recording_duration_tolerance_sec": 1.0,
    # How much two segments may overlap before it counts as a disagreement about the data
    # rather than arithmetic in the source file. Zero by default, so a corpus that has not
    # thought about it gets every overlap as a warning; a layer type whose sources state an
    # onset and a duration separately should raise it, because their sum can land a
    # fraction of a millisecond past the next onset through independent rounding alone.
    # An overlap within the tolerance is still reported, as info — see segments_monotonic.
    "segment_overlap_tolerance_sec": 0.0,
}

#: Float-comparison epsilon for noticing an overlap at all. Not a tolerance: two segments
#: whose shared boundary differs only in the last bits of a float are abutting rather than
#: overlapping, and no corpus wants that reported. The tolerance above decides how a real
#: overlap is *graded*; this decides whether there is one to grade.
_TOUCHING_EPSILON_SEC = 1e-6


# ---------------------------------------------------------------------------
# Corpus level
# ---------------------------------------------------------------------------


@register_validator(
    "disk_metadata_consistency", "Items vs the metadata CSV", scope="corpus"
)
def disk_metadata_consistency(corpus, params=None) -> list[Finding]:
    """
    Whether every item the importer created is accounted for in the metadata CSV.

    When a corpus holds more items than its CSV has rows, the extra ones have no metadata
    of any kind: no identifier beyond the one the importer minted for them, no review
    decision, no quality label. Every script that works from the CSV is blind to them, and
    turning that blindness into a visible warning is the whole point of this check.

    It reads the database only. Which paths a corpus has on disk, and which of them became
    items, is the importer's knowledge. A check that goes looking for a directory named
    after one particular layout silently does nothing on every other corpus while still
    appearing to have checked; an importer that finds a candidate it does not import says
    so in its own report, where the layout is actually known.
    """
    out: list[Finding] = []
    items = list(corpus.items.all())

    for item in [i for i in items if not i.in_metadata]:
        out.append(
            Finding(
                code="item_missing_from_metadata",
                severity="warning",
                message=f"{item.display_name} exists on disk but has no row in the metadata "
                f"CSV, so it carries no review decision, no quality label, and is invisible "
                f"to any script that works from the CSV.",
                details={"disk_path": item.disk_path, "song_id": item.song_id},
                corpus_id=corpus.pk,
                item_id=item.pk,
            )
        )

    for item in [i for i in items if i.song_id_is_provisional]:
        out.append(
            Finding(
                code="provisional_song_id",
                severity="warning",
                message=f"{item.song_id} is a key the importer minted provisionally; it has "
                f"not been confirmed by a human.",
                details={"disk_path": item.disk_path},
                corpus_id=corpus.pk,
                item_id=item.pk,
            )
        )

    inferred = [i for i in items if i.metadata_match_method in {"folded", "diacritics_stripped"}]
    for item in inferred:
        out.append(
            Finding(
                code="metadata_match_inferred",
                severity="info",
                message=f"{item.song_id} was matched to its metadata row by the "
                f"“{item.metadata_match_method}” rule, i.e. inferred rather than matched "
                f"exactly.",
                details={"disk_path": item.disk_path},
                corpus_id=corpus.pk,
                item_id=item.pk,
            )
        )

    # metadata_row is null exactly when an item has no matching row, so the number of
    # matched rows is the one figure this check can state without re-reading the disk.
    in_metadata = sum(1 for i in items if i.in_metadata)
    out.append(
        Finding(
            code="metadata_coverage_summary",
            severity="info",
            message=f"{in_metadata} of {len(items)} items carry a metadata row.",
            details={"items": len(items), "with_metadata_row": in_metadata},
            corpus_id=corpus.pk,
        )
    )
    return out


# ---------------------------------------------------------------------------
# Item level
# ---------------------------------------------------------------------------


@register_validator("media_probed", "Audio specs have been read from the header", scope="item")
def media_probed(item, params=None) -> list[Finding]:
    if item.media_is_probed:
        return []
    return [
        Finding(
            code="media_not_probed",
            severity="error",
            message=f"{item.song_id} has no audio header read yet, so every operation that "
            f"needs a sample rate — the Sonic Visualiser export among them — will fail.",
            details={"error": item.media_probe_error},
            item_id=item.pk,
            corpus_id=item.corpus_id,
        )
    ]


@register_validator("metadata_duration_agreement", "Claimed duration vs header", scope="item")
def metadata_duration_agreement(item, params=None) -> list[Finding]:
    """
    The header is always authoritative. This check is not here to correct the header; it is
    here to notice that the metadata has gone stale.

    A corpus whose metadata carries no duration at all gets no finding rather than a
    fabricated one: there is nothing for the header to disagree with.
    """
    if not item.media_is_probed or not item.metadata_row:
        return []
    claimed = _first_number(item.metadata_row, ("duration_sec", "duration", "length_sec"))
    if claimed is None:
        return []
    diff = abs(claimed - item.duration_sec)
    if diff <= 1.0:
        return []
    if item.audio_is_proxy:
        return [
            Finding(
                code="proxy_audio_duration_differs",
                severity="info",
                message=f"{item.song_id}: {item.audio_filename} on disk is "
                f"{item.duration_sec:.2f}s long, while the metadata gives the recording as "
                f"{claimed:.2f}s. This file is a stand-in rather than the recording the "
                f"annotations were made against, so a difference is expected; coverage uses "
                f"the claimed recording length instead.",
                details={"proxy_sec": item.duration_sec, "recording_sec": claimed},
                item_id=item.pk,
                corpus_id=item.corpus_id,
            )
        ]
    return [
        Finding(
            code="metadata_duration_mismatch",
            severity="warning",
            message=f"{item.song_id}: the metadata claims {claimed:.2f}s but the audio header "
            f"says {item.duration_sec:.2f}s (a difference of {diff:.2f}s). The header wins.",
            details={"claimed_sec": claimed, "header_sec": item.duration_sec},
            item_id=item.pk,
            corpus_id=item.corpus_id,
        )
    ]


@register_validator("recording_linked", "Original recording is linked", scope="item")
def recording_linked(item, params=None) -> list[Finding]:
    """
    Whether an exported session for this item would point at the recording the annotations
    were made against, or at a stand-in file shipped beside them.

    Worth a finding rather than being left implicit, because the failure is silent from the
    reviewer's side: a session opened against the wrong audio loads, plays and looks
    entirely normal, and the only symptom is that onsets do not line up — which reads as
    "the annotation is wrong" long before it reads as "this is the wrong audio file".

    The duration branch is the one that earns its keep. A file existing at the expected
    path proves only that something is there; a recording whose length disagrees with what
    the metadata claims for it is probably a different take, a different edit, or the wrong
    recording entirely.

    The branches past "is it linked" report independently rather than returning on the
    first hit: a wrong file can be wrong in more than one way at once, and being told only
    about its length when its sample rate is also off would send the reader after the
    smaller of the two problems.

    A corpus whose own audio is the recording has nothing here to find, and this check would
    report the same non-finding once per item. Switch it off for such a corpus with
    Corpus.skip_validators, which records the decision instead of hiding it.
    """
    p = {**DEFAULTS, **(params or {})}
    corpus = item.corpus
    # Looking is done under the lookup root, so that is the path worth naming in a miss;
    # what would be stored is the host root, which is a different string on purpose.
    lookup_root = (corpus.recording_lookup_root or corpus.recording_root or "").strip()

    if not lookup_root:
        return [
            Finding(
                code="recording_root_not_configured",
                severity="info",
                message=f"The corpus {corpus.slug} has no recording root, so exports for "
                f"{item.song_id} will point at whatever audio sits beside the annotations. "
                f"Run link_recordings --root to point it at the recordings instead.",
                details={"corpus_slug": corpus.slug},
                item_id=item.pk,
                corpus_id=item.corpus_id,
            )
        ]

    if not corpus.recording_filename:
        # No default filename to fall back on, deliberately: a guessed name turns "nobody
        # has configured this" into "the file is missing", which sends the reader to the
        # filesystem instead of to the corpus settings.
        return [
            Finding(
                code="recording_filename_not_configured",
                severity="info",
                message=f"The corpus {corpus.slug} has a recording root but no recording "
                f"filename, so there is no path to check for {item.song_id}. Set it on the "
                f"corpus, or pass link_recordings --filename.",
                details={"corpus_slug": corpus.slug, "recording_lookup_root": lookup_root},
                item_id=item.pk,
                corpus_id=item.corpus_id,
            )
        ]

    if not item.recording_path:
        tried = str(Path(lookup_root) / Path(item.disk_path).name / corpus.recording_filename)
        return [
            Finding(
                code="recording_not_linked",
                severity="warning",
                message=f"{item.song_id} has no linked recording, so its exported session will "
                f"point at the audio stored beside the annotations. Tried {tried}.",
                details={"tried_path": tried, "recording_lookup_root": lookup_root},
                item_id=item.pk,
                corpus_id=item.corpus_id,
            )
        ]

    out: list[Finding] = []

    # The specs below were measured by link_recordings when it verified the file. Reading
    # the stored values keeps this validator a pure database read, which is what lets a
    # corpus be checked from a process that cannot see the recordings at all.
    probed = item.recording_duration_sec
    claimed = item.reference_duration_sec
    if probed is not None and claimed and abs(probed - claimed) > p["recording_duration_tolerance_sec"]:
        out.append(
            Finding(
                code="recording_duration_mismatch",
                severity="warning",
                message=f"{item.song_id}: the linked recording measured {probed:.2f}s but the "
                f"metadata gives the recording as {claimed:.2f}s "
                f"(differs by {abs(probed - claimed):.2f}s). The linked file is probably "
                f"not the recording these annotations were aligned to.",
                details={
                    "recording_path": item.recording_path,
                    "probed_duration_sec": probed,
                    "reference_duration_sec": claimed,
                    "difference_sec": abs(probed - claimed),
                    "tolerance_sec": p["recording_duration_tolerance_sec"],
                },
                item_id=item.pk,
                corpus_id=item.corpus_id,
            )
        )

    rec_rate = item.recording_sample_rate
    if rec_rate and item.sample_rate and rec_rate != item.sample_rate:
        # Every frame index in an exported session is computed at item.sample_rate, because
        # that is the timeline the annotations were aligned to. Sonic Visualiser reads the
        # linked file at the file's own rate. A frame meant as t x item.sample_rate is then
        # read back as t x item.sample_rate / rec_rate seconds, so this is the factor by
        # which every annotation slides.
        ratio = item.sample_rate / rec_rate
        message = (
            f"{item.song_id}: the linked recording is {rec_rate} Hz but the annotations were "
            f"aligned against {item.sample_rate} Hz audio. Every frame index in an exported "
            f"session is computed at {item.sample_rate} Hz, and Sonic Visualiser reads the "
            f"linked file at its own rate, doing nothing by default to reconcile the two — it "
            f"simply plays at the wrong speed. So the session opens without complaint and "
            f"every annotation lands at the wrong time."
        )
        if abs(ratio - 1.0) > 1e-9:
            message += (
                f" The times are scaled by {ratio:g}, which is the number to recognise in the "
                f"symptom: an annotation intended for 60.0s appears at {60.0 * ratio:.1f}s."
            )
        message += (
            " That presents as bad alignment rather than as a configuration problem, so it "
            "gets debugged in the wrong place."
        )
        out.append(
            Finding(
                code="recording_sample_rate_mismatch",
                severity="warning",
                message=message,
                details={
                    "recording_path": item.recording_path,
                    "recording_sample_rate": rec_rate,
                    "annotation_sample_rate": item.sample_rate,
                    "ratio": ratio,
                },
                item_id=item.pk,
                corpus_id=item.corpus_id,
            )
        )

    rec_channels = item.recording_channels
    if rec_channels and item.channels and rec_channels != item.channels:
        # Info, not warning: unlike the sample rate, a channel-count difference does not move
        # anything on the timeline. Sonic Visualiser mixes down for display and playback, so
        # the annotations still land where they were meant to. It is reported because the two
        # files differing at all is worth knowing, not because anything is broken by it.
        out.append(
            Finding(
                code="recording_channel_count_differs",
                severity="info",
                message=f"{item.song_id}: the linked recording has {rec_channels} channels but "
                f"the audio the annotations were imported alongside has {item.channels}. "
                f"The timeline is unaffected — Sonic Visualiser mixes down and the "
                f"annotations still land where they were meant to — but the two files do "
                f"differ.",
                details={
                    "recording_path": item.recording_path,
                    "recording_channels": rec_channels,
                    "annotation_channels": item.channels,
                },
                item_id=item.pk,
                corpus_id=item.corpus_id,
            )
        )

    return out


# ---------------------------------------------------------------------------
# Layer level
# ---------------------------------------------------------------------------


@register_validator("segments_monotonic", "Segment times increase and do not overlap", scope="layer")
def segments_monotonic(layer, params=None) -> list[Finding]:
    """
    Segment times must increase, and a segment must not begin before the previous one ends.

    Two causes produce an overlap, they look identical in the data, and they want opposite
    responses. One is a genuine conflict: the annotator has two events sounding at once in
    a layer that holds a single voice. The other is arithmetic: where a source file states
    an onset and a *duration*, each rounded on its own, their sum can land a fraction of a
    millisecond past the next onset without anybody having annotated anything twice.

    Only the magnitude separates them, so the magnitude decides the severity.
    segment_overlap_tolerance_sec is the dividing line, set per layer type because it is a
    property of how that type's sources are written. Above it, a warning. At or below it,
    an info finding — reported rather than dropped, because "there is an overlap and it is
    the size rounding would produce" is a different statement from silence, and a tolerance
    quietly swallowing findings is how a real conflict eventually goes unnoticed.
    """
    p = {**DEFAULTS, **(params or {})}
    tolerance = p["segment_overlap_tolerance_sec"]
    rows = layer.resolved_segments()
    out: list[Finding] = []

    for r in rows:
        if r["end_sec"] < r["start_sec"]:
            out.append(
                Finding(
                    code="segment_reversed",
                    severity="error",
                    message=f"Segment {r['index']} has its offset before its onset.",
                    details=r,
                    layer_id=layer.pk,
                    item_id=layer.item_id,
                )
            )

    # Stored order is by index, which is the order the rows arrived in; it is not
    # necessarily time order. Reported as its own finding, and the overlap walk below runs
    # on a time-sorted copy: comparing a row against its index-predecessor on an unsorted
    # layer makes every out-of-order pair look like an overlap, which names the wrong
    # defect and invents a magnitude for it.
    out.extend(_report_time_order(layer, rows))
    ordered = sorted(rows, key=lambda r: (r["start_sec"], r["index"]))

    prev = None
    overlaps = []
    for r in ordered:
        if prev is not None and r["start_sec"] < prev["end_sec"] - _TOUCHING_EPSILON_SEC:
            overlaps.append(
                {
                    "a_index": prev["index"],
                    "b_index": r["index"],
                    "a_end_sec": prev["end_sec"],
                    "b_start_sec": r["start_sec"],
                    "overlap_sec": prev["end_sec"] - r["start_sec"],
                }
            )
        prev = r

    beyond = [o for o in overlaps if o["overlap_sec"] > tolerance]
    within = [o for o in overlaps if o["overlap_sec"] <= tolerance]

    if beyond:
        largest = max(o["overlap_sec"] for o in beyond)
        out.append(
            Finding(
                code="segments_overlap",
                severity="warning",
                message=f"{len(beyond)} "
                f"{'pair' if len(beyond) == 1 else 'pairs'} of segments overlap in time, "
                f"which a single-voice layer normally should not. The largest overlap is "
                f"{largest:.6g}s"
                + (
                    f", beyond this layer type's {tolerance:.6g}s rounding tolerance."
                    if tolerance
                    else "."
                ),
                details={
                    "pairs": beyond[:20],
                    "total": len(beyond),
                    "largest_overlap_sec": largest,
                    "tolerance_sec": tolerance,
                },
                layer_id=layer.pk,
                item_id=layer.item_id,
            )
        )

    if within:
        largest = max(o["overlap_sec"] for o in within)
        out.append(
            Finding(
                code="segments_overlap_within_tolerance",
                severity="info",
                message=f"{len(within)} "
                f"{'pair' if len(within) == 1 else 'pairs'} of segments overlap by at most "
                f"{largest:.6g}s, which is within this layer type's rounding tolerance of "
                f"{tolerance:.6g}s. An overlap this small is what independent rounding of an "
                f"onset and a duration in the source produces, not two events annotated as "
                f"sounding at once.",
                details={
                    "pairs": within[:20],
                    "total": len(within),
                    "largest_overlap_sec": largest,
                    "tolerance_sec": tolerance,
                    "within_tolerance": True,
                },
                layer_id=layer.pk,
                item_id=layer.item_id,
            )
        )
    return out


def _report_time_order(layer, rows) -> list[Finding]:
    """
    Whether the segments are stored in the order they occur in the audio.

    Index order is the order the rows arrived in, so an unsorted source file produces a
    layer whose indices and timeline disagree. That is worth a finding of its own: it is a
    property of the file rather than of the annotation, every pairwise check has to sort
    around it, and anything that reads the segments in stored order — an export, a diff
    against another layer, a person scrolling the list — sees them jumbled.
    """
    out_of_order = [
        {
            "index": cur["index"],
            "start_sec": cur["start_sec"],
            "after_index": prev["index"],
            "after_start_sec": prev["start_sec"],
        }
        for prev, cur in zip(rows, rows[1:])
        if cur["start_sec"] < prev["start_sec"]
    ]
    if not out_of_order:
        return []
    first = out_of_order[0]
    return [
        Finding(
            code="segments_out_of_time_order",
            severity="warning",
            message=f"{len(out_of_order)} "
            f"{'segment' if len(out_of_order) == 1 else 'segments'} are stored out of time "
            f"order: segment {first['index']} starts at {first['start_sec']:.3f}s, before "
            f"segment {first['after_index']} at {first['after_start_sec']:.3f}s. The source "
            f"file was not sorted by onset. Nothing is lost — every check here sorts by time "
            f"first — but stored order and the timeline disagree, so anything that reads the "
            f"segments in order sees them jumbled.",
            details={
                "out_of_order": out_of_order[:20],
                "count": len(out_of_order),
                "total_segments": len(rows),
            },
            layer_id=layer.pk,
            item_id=layer.item_id,
        )
    ]


@register_validator("annotation_coverage", "Annotation coverage vs reference length", scope="layer")
def annotation_coverage(layer, params=None) -> list[Finding]:
    """
    How much of the reference length this layer annotates, and whether it stops short of the
    end or runs past it.

    The severity of a tail gap depends on whether the real audio is on hand. With the
    recording the annotations were made against, a gap at the end is a genuine finding:
    something is missing from it. With only a stand-in, the gap carries no independent
    information — a file generated from the annotations ends exactly where they do, so the
    gap is fully determined by the claimed length minus the stand-in's length, and it
    cannot be told apart from ordinary lead-out without the real recording.
    """
    p = {**DEFAULTS, **(params or {})}
    item = layer.item
    rows = layer.resolved_segments()
    reference, ref_source = item.coverage_duration()
    if not rows or not reference:
        return []
    last_end = max(r["end_sec"] for r in rows)
    first_start = min(r["start_sec"] for r in rows)
    tail_gap = reference - last_end
    out: list[Finding] = []
    if tail_gap > p["coverage_gap_sec"]:
        proxy = bool(item.audio_is_proxy)
        message = (
            f"The annotation ends at {last_end:.2f}s against a reference length of "
            f"{reference:.2f}s (source: {ref_source}), leaving {tail_gap:.2f}s unannotated "
            f"at the end."
        )
        if proxy:
            message += (
                " The audio on disk is a stand-in rather than the recording the annotations "
                "were made against, so this gap cannot be distinguished from ordinary "
                "lead-out without the real audio."
            )
        out.append(
            Finding(
                code="annotation_tail_gap",
                severity="info" if proxy else "warning",
                message=message,
                details={
                    "last_annotation_sec": last_end,
                    "reference_duration_sec": reference,
                    "reference_source": ref_source,
                    "gap_sec": tail_gap,
                    "threshold_sec": p["coverage_gap_sec"],
                    "audio_is_proxy": proxy,
                    "item_notes": item.notes or "",
                },
                layer_id=layer.pk,
                item_id=item.pk,
            )
        )
    if last_end > reference + 0.5:
        out.append(
            Finding(
                code="annotation_exceeds_audio",
                severity="error",
                message=f"The annotation ends at {last_end:.2f}s, past the reference length of "
                f"{reference:.2f}s (source: {ref_source}).",
                details={
                    "overshoot_sec": last_end - reference,
                    "reference_duration_sec": reference,
                    "reference_source": ref_source,
                },
                layer_id=layer.pk,
                item_id=item.pk,
            )
        )
    covered = sum(r["end_sec"] - r["start_sec"] for r in rows)
    out.append(
        Finding(
            code="coverage_summary",
            severity="info",
            message=f"Covers {covered:.1f}s of {reference:.1f}s "
            f"({100 * covered / reference:.0f}%), spanning "
            f"{first_start:.2f}–{last_end:.2f}s across {len(rows)} segments.",
            details={
                "covered_sec": covered,
                "ratio": covered / reference,
                "reference_source": ref_source,
                "segment_count": len(rows),
                "item_notes": item.notes or "",
            },
            layer_id=layer.pk,
            item_id=item.pk,
        )
    )
    return out


@register_validator(
    "annotation_internal_gaps", "Unannotated stretches mid-recording", scope="layer"
)
def annotation_internal_gaps(layer, params=None) -> list[Finding]:
    """
    annotation_coverage watches the gap at the end; this one watches the middle.

    The severity is always info rather than warning, and the item's notes are carried into
    the details. A long silence that is part of the material and a stretch the annotator
    missed are identical in the data — the only thing separating them is knowledge about
    the recording itself — so whoever reads the finding needs that context on the same
    screen, and the decision belongs to them rather than to the checker.
    """
    p = {**DEFAULTS, **(params or {})}
    rows = layer.resolved_segments()
    if len(rows) < 3:
        return []
    # Time order, not stored order. On an unsorted layer a pairwise walk over index order
    # produces negative gaps, and a negative gap never exceeds the threshold — so the real
    # gaps go unreported and the layer looks fully covered. segments_monotonic is what
    # reports the ordering itself.
    rows = sorted(rows, key=lambda r: (r["start_sec"], r["index"]))
    gaps = []
    for prev, cur in zip(rows, rows[1:]):
        gap = cur["start_sec"] - prev["end_sec"]
        if gap > p["internal_gap_sec"]:
            gaps.append(
                {
                    "after_index": prev["index"],
                    "from_sec": prev["end_sec"],
                    "to_sec": cur["start_sec"],
                    "gap_sec": gap,
                }
            )
    if not gaps:
        return []
    longest = max(g["gap_sec"] for g in gaps)
    return [
        Finding(
            code="annotation_internal_gap",
            severity="info",
            message=f"{len(gaps)} unannotated "
            f"{'stretch' if len(gaps) == 1 else 'stretches'} longer than "
            f"{p['internal_gap_sec']:.4g}s mid-recording (the longest is {longest:.1f}s). A "
            f"silence in the material and a missed annotation look identical in the data, so "
            f"a human has to decide which this is.",
            details={
                "gaps": gaps[:20],
                "count": len(gaps),
                "longest_sec": longest,
                "threshold_sec": p["internal_gap_sec"],
                # The context needed to make that call, kept with the finding.
                "item_notes": layer.item.notes or "",
            },
            layer_id=layer.pk,
            item_id=layer.item_id,
        )
    ]


@register_validator(
    "clip_single_segment", "A clip-level annotation should have one segment", scope="layer",
    layer_types=("audio_tagging",),
)
def clip_single_segment(layer, params=None) -> list[Finding]:
    n = layer.segments.count()
    if n <= 1:
        return []
    return [
        Finding(
            code="clip_layer_multi_segment",
            severity="error",
            message=f"A clip-level annotation has {n} segments; it should have 0 or 1.",
            details={"count": n},
            layer_id=layer.pk,
            item_id=layer.item_id,
        )
    ]


# ---------------------------------------------------------------------------


def _first_number(row: dict, keys):
    for k in keys:
        if k in row:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return None
