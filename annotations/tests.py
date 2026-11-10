"""
Test suite.

Every fixture here is synthetic and built in code: tones at round frequencies, onsets on a
tenth of a second, WAV files written by the stdlib, so the suite says what the code does
rather than what one dataset happens to contain.

DemoImportComparisonTests is the one exception, and deliberately so: the landing page
quotes figures from the demo corpus, and a number printed on a public page needs a test
that fails when the data behind it changes. It is the only class that reads demo-data.

Two conventions worth knowing before reading:

  · A1 and A2 are stream keys standing for two independent annotators. They are never a
    version lineage, and several tests exist specifically to hold that line.
  · Pitches are given in Hz and compared in cents. A 100-cent disagreement is a semitone,
    which is the scale a pitch tolerance is set at.
"""
from __future__ import annotations

import bz2
import io
import math
import struct
import tempfile
import wave
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from .comparators.pitch_interval import (
    MATCH_STRATEGIES,
    NOTE_PITCH_DEFAULTS,
    SED_DEFAULTS,
    _match,
    compare_note_pitch,
)
from .exporters import item_to_session, layer_to_svl, sort_layers_by_stream
from .importers.f0_csv import CONTRACTS, FROM_HEADER, F0ParseError, parse_f0_csv
from .models import AnnotationLayer, Corpus, Item, LayerComparison, Segment
from .registry import LAYER_TYPES, VALIDATORS, get_layer_type
from .services.compare import run_comparison, store_default_comparisons
from .services.ingest import IngestError, create_layer_with_segments
from .services.vocadito import _refuse_duplicate_track_numbers, _probe
from .validators import run_for_corpus, run_for_item, run_for_layer

SAMPLE_RATE = 16000
DURATION_SEC = 20.0

#: A4 = 440, so these are exact equal-tempered pitches: A3, C4, E4, A4.
A3, C4, E4, A4 = 220.0, 261.6255653, 329.6275569, 440.0


def cents(a: float, b: float) -> float:
    return 1200.0 * math.log2(b / a)


def shift_cents(hz: float, by: float) -> float:
    return hz * (2.0 ** (by / 1200.0))


class Fixtures(TestCase):
    """Shared builders. Holds no tests of its own."""

    def setUp(self):
        self.corpus = Corpus.objects.create(
            slug="demo", name="Demo corpus", root_path="/data/demo"
        )
        self.item = self.make_item("item_one")

    def make_item(self, song_id, **kwargs):
        defaults = dict(
            corpus=self.corpus,
            song_id=song_id,
            display_name=song_id.replace("_", " "),
            disk_path="audio",
            audio_filename=f"{song_id}.wav",
            sample_rate=SAMPLE_RATE,
            duration_sec=DURATION_SEC,
            channels=1,
            audio_format="WAV/PCM_16",
        )
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def rows(self, specs):
        """[(onset, duration, hz), ...] -> parser-shaped rows."""
        return [
            {
                "index": i,
                "start_sec": onset,
                "end_sec": onset + dur,
                "payload": {"hz": hz},
            }
            for i, (onset, dur, hz) in enumerate(specs)
        ]

    def scale_rows(self, n=8, start=1.0, step=1.0, dur=0.5, hz=A4):
        return self.rows([(start + i * step, dur, hz) for i in range(n)])

    def make_layer(self, rows, stream="A1", item=None, layer_type="note_pitch", **kwargs):
        return create_layer_with_segments(
            item=item or self.item,
            layer_type=layer_type,
            stream_key=stream,
            rows=rows,
            **kwargs,
        )

    def write_wav(self, path, seconds=DURATION_SEC, sample_rate=SAMPLE_RATE, channels=1):
        """A real WAV header with silent frames. Only the header is ever read back."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        frames = int(seconds * sample_rate)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(struct.pack("<h", 0) * frames * channels)
        return path


# ---------------------------------------------------------------------------
# parse_f0_csv: the column contract
# ---------------------------------------------------------------------------


class ColumnContractTests(TestCase):
    #: The same three notes, written out under each contract. Identical content, so a
    #: parser that honours the contract must produce identical rows from both.
    ONSET_OFFSET_HZ = "1.00,1.50,220.0\n2.00,2.25,330.0\n3.00,3.80,440.0\n"
    ONSET_HZ_DURATION = "1.00,220.0,0.50\n2.00,330.0,0.25\n3.00,440.0,0.80\n"

    def test_onset_offset_hz(self):
        r = parse_f0_csv(io.StringIO(self.ONSET_OFFSET_HZ), contract="onset_offset_hz")
        self.assertEqual(r.count, 3)
        self.assertEqual(r.contract_key, "onset_offset_hz")
        self.assertAlmostEqual(r.rows[0]["start_sec"], 1.00)
        self.assertAlmostEqual(r.rows[0]["end_sec"], 1.50)
        self.assertAlmostEqual(r.rows[0]["payload"]["hz"], 220.0)

    def test_onset_hz_duration_derives_the_end_time(self):
        r = parse_f0_csv(io.StringIO(self.ONSET_HZ_DURATION), contract="onset_hz_duration")
        self.assertEqual(r.count, 3)
        self.assertAlmostEqual(r.rows[2]["start_sec"], 3.00)
        self.assertAlmostEqual(r.rows[2]["end_sec"], 3.80)
        self.assertAlmostEqual(r.rows[2]["payload"]["hz"], 440.0)

    def test_both_contracts_describe_the_same_notes(self):
        a = parse_f0_csv(io.StringIO(self.ONSET_OFFSET_HZ), contract="onset_offset_hz")
        b = parse_f0_csv(io.StringIO(self.ONSET_HZ_DURATION), contract="onset_hz_duration")
        for ra, rb in zip(a.rows, b.rows):
            self.assertAlmostEqual(ra["start_sec"], rb["start_sec"], places=6)
            self.assertAlmostEqual(ra["end_sec"], rb["end_sec"], places=6)
            self.assertAlmostEqual(ra["payload"]["hz"], rb["payload"]["hz"], places=6)

    def test_wrong_contract_is_refused_not_imported(self):
        """
        The failure this is here to prevent: onset_hz_duration data read as
        onset_offset_hz parses cleanly at the row level, and yields notes hundreds of
        seconds long pitched at a fraction of a Hz.
        """
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(io.StringIO(self.ONSET_HZ_DURATION), contract="onset_offset_hz")
        msg = str(ctx.exception)
        self.assertIn("onset_offset_hz", msg)
        self.assertIn("plausible pitch range", msg)

    def test_the_refusal_names_the_contract_that_fits(self):
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(io.StringIO(self.ONSET_HZ_DURATION), contract="onset_offset_hz")
        self.assertIn("onset_hz_duration", str(ctx.exception))

    def test_the_other_direction_is_refused_too(self):
        """A duration read off an offset column is a 220-second note, not a 0.5s one."""
        with self.assertRaises(F0ParseError):
            parse_f0_csv(io.StringIO(self.ONSET_OFFSET_HZ), contract="onset_hz_duration")

    def test_one_stray_outlier_does_not_fail_a_good_file(self):
        """A systematically wrong mapping breaks every row; a bad row breaks one."""
        good = "".join(f"{i}.0,{i}.5,440.0\n" for i in range(1, 20))
        r = parse_f0_csv(io.StringIO(good + "20.0,20.5,0.3\n"), contract="onset_offset_hz")
        self.assertEqual(r.count, 19)
        self.assertTrue(any("not a plausible pitch" in s for _, s in r.skipped_lines))

    def test_unknown_contract_name_is_refused(self):
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(io.StringIO(self.ONSET_OFFSET_HZ), contract="whatever")
        self.assertIn("Unknown column contract", str(ctx.exception))

    def test_a_contract_with_no_end_time_is_refused(self):
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(io.StringIO(self.ONSET_OFFSET_HZ), contract={"onset": 0, "hz": 2})
        self.assertIn("offset or a duration", str(ctx.exception))

    def test_a_contract_with_two_end_times_is_refused(self):
        """Two sources for one end time can disagree, with no way to say which was meant."""
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(
                io.StringIO(self.ONSET_OFFSET_HZ),
                contract={"onset": 0, "offset": 1, "hz": 2, "duration": 1},
            )
        self.assertIn("both an offset and a duration", str(ctx.exception))

    def test_explicit_index_mapping(self):
        r = parse_f0_csv(
            io.StringIO("220.0,1.00,0.50\n330.0,2.00,0.25\n"),
            contract={"hz": 0, "onset": 1, "duration": 2},
        )
        self.assertEqual(r.count, 2)
        self.assertAlmostEqual(r.rows[0]["start_sec"], 1.00)
        self.assertAlmostEqual(r.rows[0]["payload"]["hz"], 220.0)

    def test_named_columns_from_a_header_row(self):
        csv = "start_sec,pitch_hz,duration_sec\n1.00,220.0,0.50\n2.00,330.0,0.25\n"
        r = parse_f0_csv(io.StringIO(csv), contract=FROM_HEADER)
        self.assertEqual(r.count, 2)
        self.assertEqual(r.column_map, {"onset": 0, "hz": 1, "duration": 2})
        self.assertAlmostEqual(r.rows[0]["end_sec"], 1.50)

    def test_a_named_column_absent_from_the_header_is_refused(self):
        csv = "start_sec,pitch_hz,duration_sec\n1.00,220.0,0.50\n"
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(io.StringIO(csv), contract={"onset": "start_sec", "hz": "pitch_hz",
                                                     "duration": "nope"})
        self.assertIn("nope", str(ctx.exception))

    def test_zero_hz_is_unvoiced_not_a_contract_violation(self):
        """Frame-level F0 marks an unvoiced frame with 0. It carries no note, and it is
        not evidence that the mapping is wrong."""
        csv = "1.00,1.50,440.0\n2.00,2.50,0.0\n3.00,3.50,440.0\n"
        r = parse_f0_csv(io.StringIO(csv), contract="onset_offset_hz")
        self.assertEqual(r.count, 2)
        self.assertTrue(any("unvoiced" in s for _, s in r.skipped_lines))

    def test_a_header_row_on_a_headerless_contract_is_reported(self):
        csv = "onset,offset,hz\n1.00,1.50,440.0\n"
        r = parse_f0_csv(io.StringIO(csv), contract="onset_offset_hz")
        self.assertEqual(r.count, 1)
        self.assertTrue(any("header" in w for w in r.warnings))

    def test_contract_is_required(self):
        with self.assertRaises(TypeError):
            parse_f0_csv(io.StringIO(self.ONSET_OFFSET_HZ))  # type: ignore[call-arg]

    def test_registered_contracts(self):
        self.assertEqual(sorted(CONTRACTS), ["onset_hz_duration", "onset_offset_hz"])


# ---------------------------------------------------------------------------
# Segment validation on the import path
# ---------------------------------------------------------------------------


class ImportPathValidationTests(Fixtures):
    """
    Segment.clean() states the payload rules, but bulk_create() does not call it and
    neither does any other write path that skips a ModelForm. create_layer_with_segments
    is the funnel every import passes through, so the check has to be there too.
    """

    def test_a_payload_below_the_registered_minimum_is_refused(self):
        with self.assertRaises(IngestError) as ctx:
            self.make_layer(self.rows([(1.0, 0.5, 3.0)]))
        self.assertIn("below the minimum", str(ctx.exception))

    def test_a_payload_above_the_registered_maximum_is_refused(self):
        with self.assertRaises(IngestError):
            self.make_layer(self.rows([(1.0, 0.5, 99000.0)]))

    def test_a_missing_required_payload_field_is_refused(self):
        rows = [{"index": 0, "start_sec": 1.0, "end_sec": 1.5, "payload": {}}]
        with self.assertRaises(IngestError) as ctx:
            self.make_layer(rows)
        self.assertIn("missing the required field hz", str(ctx.exception))

    def test_nothing_is_written_when_the_rows_are_refused(self):
        with self.assertRaises(IngestError):
            self.make_layer(self.rows([(1.0, 0.5, A4), (2.0, 0.5, 3.0)]))
        self.assertEqual(AnnotationLayer.objects.count(), 0)
        self.assertEqual(Segment.objects.count(), 0)

    def test_a_reversed_segment_is_refused(self):
        rows = [{"index": 0, "start_sec": 2.0, "end_sec": 1.0, "payload": {"hz": A4}}]
        with self.assertRaises(IngestError) as ctx:
            self.make_layer(rows)
        self.assertIn("before start_sec", str(ctx.exception))

    def test_an_unregistered_layer_type_is_refused(self):
        with self.assertRaises(IngestError) as ctx:
            self.make_layer(self.scale_rows(), layer_type="not_a_type")
        self.assertIn("Unregistered annotation type", str(ctx.exception))

    def test_invalid_layer_params_are_refused(self):
        with self.assertRaises(IngestError) as ctx:
            self.make_layer(self.scale_rows(), params={"tuning_reference_hz": 10.0})
        self.assertIn("params are invalid", str(ctx.exception))

    def test_a_clip_type_refuses_more_than_one_segment(self):
        rows = [
            {"index": 0, "start_sec": 0.0, "end_sec": 1.0, "payload": {"labels": ["a"]}},
            {"index": 1, "start_sec": 1.0, "end_sec": 2.0, "payload": {"labels": ["b"]}},
        ]
        with self.assertRaises(IngestError) as ctx:
            self.make_layer(rows, layer_type="audio_tagging")
        self.assertIn("clip-level", str(ctx.exception))

    def test_valid_rows_are_stored_in_seconds(self):
        layer = self.make_layer(self.rows([(1.25, 0.5, A4)]))
        seg = layer.segments.get()
        self.assertAlmostEqual(seg.start_sec, 1.25)
        self.assertAlmostEqual(seg.end_sec, 1.75)
        # Frames are a presentation concern; nothing here stores one.
        self.assertEqual(seg.payload, {"hz": A4})


# ---------------------------------------------------------------------------
# Parallel annotations vs version lineage
# ---------------------------------------------------------------------------


class AnnotatorIndependenceTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.a1 = self.make_layer(self.scale_rows(), stream="A1")
        self.a2 = self.make_layer(self.scale_rows(hz=C4), stream="A2")

    def test_each_annotator_gets_its_own_stream(self):
        self.assertEqual({self.a1.stream_key, self.a2.stream_key}, {"A1", "A2"})

    def test_both_are_version_one(self):
        self.assertEqual(self.a1.version, 1)
        self.assertEqual(self.a2.version, 1)

    def test_neither_is_the_parent_of_the_other(self):
        self.assertIsNone(self.a1.parent)
        self.assertIsNone(self.a2.parent)

    def test_neither_is_superseded(self):
        for layer in (self.a1, self.a2):
            layer.refresh_from_db()
            self.assertNotEqual(layer.status, AnnotationLayer.Status.SUPERSEDED)

    def test_a_third_annotator_changes_nothing_about_the_first_two(self):
        self.make_layer(self.scale_rows(hz=E4), stream="A3")
        for layer in (self.a1, self.a2):
            layer.refresh_from_db()
            self.assertEqual(layer.version, 1)
            self.assertIsNone(layer.parent)
            self.assertNotEqual(layer.status, AnnotationLayer.Status.SUPERSEDED)

    def test_within_one_stream_a_reimport_is_a_new_version(self):
        """The same mechanism that keeps streams apart still tracks lineage inside one."""
        v2 = self.make_layer(self.scale_rows(n=9), stream="A1")
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.parent_id, self.a1.pk)

    def test_a_new_version_supersedes_an_accepted_one_in_the_same_stream_only(self):
        self.a1.status = AnnotationLayer.Status.ACCEPTED
        self.a1.save(update_fields=["status"])
        self.a2.status = AnnotationLayer.Status.ACCEPTED
        self.a2.save(update_fields=["status"])

        self.make_layer(self.scale_rows(n=9), stream="A1")

        self.a1.refresh_from_db()
        self.a2.refresh_from_db()
        self.assertEqual(self.a1.status, AnnotationLayer.Status.SUPERSEDED)
        self.assertEqual(self.a2.status, AnnotationLayer.Status.ACCEPTED)

    def test_a_parent_from_another_stream_is_rejected_by_clean(self):
        from django.core.exceptions import ValidationError

        stray = AnnotationLayer(
            item=self.item, layer_type="note_pitch", stream_key="A2",
            version=2, parent=self.a1,
        )
        with self.assertRaises(ValidationError):
            stray.clean()

    def test_the_annotator_can_be_recorded_in_provenance(self):
        layer = self.make_layer(
            self.scale_rows(), stream="A4", provenance={"annotator": "A4"}
        )
        self.assertEqual(layer.provenance["annotator"], "A4")


# ---------------------------------------------------------------------------
# Comparator
# ---------------------------------------------------------------------------


class MatchStrategyTests(TestCase):
    ROWS = [{"index": 0, "start_sec": 1.0, "end_sec": 1.5, "payload": {"hz": 440.0}}]

    def test_a_missing_strategy_raises(self):
        """Not defaulted on purpose: a silent choice would not be recorded in the
        stored params, leaving the result unreadable."""
        with self.assertRaises(ValueError) as ctx:
            _match(self.ROWS, self.ROWS, {"onset_tolerance_sec": 0.1})
        self.assertIn("match_strategy", str(ctx.exception))

    def test_an_unknown_strategy_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _match(self.ROWS, self.ROWS, {"match_strategy": "telepathy"})
        self.assertIn("telepathy", str(ctx.exception))

    def test_both_registered_strategies_work(self):
        for strategy in MATCH_STRATEGIES:
            pairs, _, _ = _match(
                self.ROWS, self.ROWS,
                {"match_strategy": strategy, "onset_tolerance_sec": 0.1, "min_iou": 0.5},
            )
            self.assertEqual(len(pairs), 1, strategy)

    def test_each_comparator_default_names_a_registered_strategy(self):
        for defaults in (NOTE_PITCH_DEFAULTS, SED_DEFAULTS):
            self.assertIn(defaults["match_strategy"], MATCH_STRATEGIES)


class ComparatorTests(Fixtures):
    PARAMS = {
        "onset_tolerance_sec": 0.1,
        "min_iou": 0.5,
        "pitch_tolerance_cents": 50.0,
        "match_strategy": "onset",
    }

    def test_identical_annotations_produce_no_discrepancies(self):
        a = self.make_layer(self.scale_rows(), stream="A1")
        b = self.make_layer(self.scale_rows(), stream="A2")
        result = compare_note_pitch(a, b, self.PARAMS)
        self.assertEqual(result["summary"]["matched"], 8)
        self.assertEqual(result["discrepancies"], [])

    def test_pitch_only(self):
        """Same interval, a semitone apart in pitch."""
        a = self.make_layer(self.rows([(1.0, 0.5, A4)]), stream="A1")
        b = self.make_layer(self.rows([(1.0, 0.5, shift_cents(A4, 100))]), stream="A2")
        s = compare_note_pitch(a, b, self.PARAMS)["summary"]
        self.assertEqual(s["pitch_only_mismatch"], 1)
        self.assertEqual(s["timing_only_mismatch"], 0)
        self.assertEqual(s["both_mismatch"], 0)

    def test_timing_only(self):
        """Same pitch; the second annotation holds the note far longer, so the intervals
        overlap too little to count as the same interval."""
        a = self.make_layer(self.rows([(1.0, 0.5, A4)]), stream="A1")
        b = self.make_layer(self.rows([(1.05, 3.0, A4)]), stream="A2")
        s = compare_note_pitch(a, b, self.PARAMS)["summary"]
        self.assertEqual(s["timing_only_mismatch"], 1)
        self.assertEqual(s["pitch_only_mismatch"], 0)

    def test_both(self):
        a = self.make_layer(self.rows([(1.0, 0.5, A4)]), stream="A1")
        b = self.make_layer(self.rows([(1.05, 3.0, shift_cents(A4, 200))]), stream="A2")
        s = compare_note_pitch(a, b, self.PARAMS)["summary"]
        self.assertEqual(s["both_mismatch"], 1)

    def test_only_in_a_and_only_in_b(self):
        """Each annotator marks a note the other does not, far enough apart that neither
        falls inside the other's onset tolerance."""
        a = self.make_layer(self.rows([(1.0, 0.5, A4), (5.0, 0.5, A4)]), stream="A1")
        b = self.make_layer(self.rows([(1.0, 0.5, A4), (9.0, 0.5, A4)]), stream="A2")
        result = compare_note_pitch(a, b, self.PARAMS)
        s = result["summary"]
        self.assertEqual(s["matched"], 1)
        self.assertEqual(s["only_in_a"], 1)
        self.assertEqual(s["only_in_b"], 1)
        kinds = {d["kind"] for d in result["discrepancies"]}
        self.assertEqual(kinds, {"only_in_a", "only_in_b"})

    def test_every_category_is_reachable_in_one_comparison(self):
        a = self.make_layer(
            self.rows([
                (1.0, 0.5, A4),                       # agrees
                (3.0, 0.5, A4),                       # pitch only
                (5.0, 0.5, A4),                       # timing only
                (7.0, 0.5, A4),                       # both
                (9.0, 0.5, A4),                       # only in A
            ]),
            stream="A1",
        )
        b = self.make_layer(
            self.rows([
                (1.0, 0.5, A4),
                (3.0, 0.5, shift_cents(A4, 150)),
                (5.05, 3.0, A4),
                (7.05, 3.0, shift_cents(A4, 200)),
                (13.0, 0.5, A4),                      # only in B
            ]),
            stream="A2",
        )
        s = compare_note_pitch(a, b, self.PARAMS)["summary"]
        self.assertEqual(s["pitch_only_mismatch"], 1)
        self.assertEqual(s["timing_only_mismatch"], 1)
        self.assertEqual(s["both_mismatch"], 1)
        self.assertEqual(s["only_in_a"], 1)
        self.assertEqual(s["only_in_b"], 1)

    def test_results_are_symmetric_under_swapping_the_two_sides(self):
        """
        Neither annotation is the reference, so comparing B against A has to report the
        same disagreements with only the direction reversed. A comparator that privileged
        one side would pass the tests above and fail this one.
        """
        a = self.make_layer(
            self.rows([(1.0, 0.5, A4), (3.0, 0.5, A4), (9.0, 0.5, A4)]), stream="A1"
        )
        b = self.make_layer(
            self.rows([(1.0, 0.5, A4), (3.0, 0.5, shift_cents(A4, 150)), (13.0, 0.5, A4)]),
            stream="A2",
        )
        fwd = compare_note_pitch(a, b, self.PARAMS)["summary"]
        rev = compare_note_pitch(b, a, self.PARAMS)["summary"]

        self.assertEqual(fwd["matched"], rev["matched"])
        self.assertEqual(fwd["pitch_only_mismatch"], rev["pitch_only_mismatch"])
        self.assertEqual(fwd["timing_only_mismatch"], rev["timing_only_mismatch"])
        self.assertEqual(fwd["both_mismatch"], rev["both_mismatch"])
        # The asymmetric fields swap rather than staying put.
        self.assertEqual(fwd["only_in_a"], rev["only_in_b"])
        self.assertEqual(fwd["only_in_b"], rev["only_in_a"])
        self.assertEqual(fwd["segments_a"], rev["segments_b"])

    def test_pitch_delta_is_signed_and_reverses_with_the_sides(self):
        a = self.make_layer(self.rows([(1.0, 0.5, A4)]), stream="A1")
        b = self.make_layer(self.rows([(1.0, 0.5, shift_cents(A4, 150))]), stream="A2")
        fwd = compare_note_pitch(a, b, self.PARAMS)["discrepancies"][0]
        rev = compare_note_pitch(b, a, self.PARAMS)["discrepancies"][0]
        self.assertAlmostEqual(fwd["pitch_delta_cents"], 150.0, places=3)
        self.assertAlmostEqual(rev["pitch_delta_cents"], -150.0, places=3)

    def test_pitch_is_compared_in_cents_against_each_layers_own_reference(self):
        """
        Two annotations may have been made against different tuning references. The same
        written pitch at two references is the same note, and must not be reported as a
        pitch disagreement.
        """
        ref_b = shift_cents(A4, 30)
        a = self.make_layer(
            self.rows([(1.0, 0.5, A4)]), stream="A1", params={"tuning_reference_hz": A4}
        )
        b = self.make_layer(
            self.rows([(1.0, 0.5, shift_cents(A4, 30))]),
            stream="A2",
            params={"tuning_reference_hz": ref_b},
        )
        result = compare_note_pitch(a, b, self.PARAMS)
        self.assertEqual(result["summary"]["pitch_only_mismatch"], 0)
        self.assertAlmostEqual(result["summary"]["tuning_reference_a_hz"], A4)
        self.assertAlmostEqual(result["summary"]["tuning_reference_b_hz"], ref_b)

    def test_a_segment_is_matched_at_most_once(self):
        a = self.make_layer(self.rows([(1.0, 0.5, A4)]), stream="A1")
        b = self.make_layer(
            self.rows([(1.00, 0.5, A4), (1.02, 0.5, A4), (1.04, 0.5, A4)]), stream="A2"
        )
        s = compare_note_pitch(a, b, self.PARAMS)["summary"]
        self.assertEqual(s["matched"], 1)
        self.assertEqual(s["only_in_b"], 2)


# ---------------------------------------------------------------------------
# Registry and validator wiring
# ---------------------------------------------------------------------------


class RegistryTests(TestCase):
    def test_note_pitch_validator_set(self):
        self.assertEqual(
            set(get_layer_type("note_pitch").validators),
            {
                "segments_monotonic",
                "annotation_coverage",
                "annotation_internal_gaps",
                "media_probed",
                "metadata_duration_agreement",
            },
        )

    def test_note_pitch_sets_its_own_validator_thresholds(self):
        vp = get_layer_type("note_pitch").validator_params
        self.assertIn("coverage_gap_sec", vp["annotation_coverage"])
        self.assertIn("internal_gap_sec", vp["annotation_internal_gaps"])

    def test_every_validator_named_by_a_layer_type_is_registered(self):
        for key, spec in LAYER_TYPES.items():
            for v in spec.validators:
                self.assertIn(v, VALIDATORS, f"{key} names an unregistered validator {v}")

    def test_every_registered_validator_runs_somewhere(self):
        """
        A validator nobody can reach is dead code that still looks like coverage. Layer
        scope is selected by the layer type, so a layer validator earns its registration
        by being named; corpus and item validators run for every corpus.
        """
        named = {v for spec in LAYER_TYPES.values() for v in spec.validators}
        for key, spec in VALIDATORS.items():
            if spec.scope == "layer":
                self.assertIn(key, named, f"{key} is registered but no layer type uses it")

    def test_a_validator_restricted_to_a_type_is_named_by_that_type(self):
        for key, spec in VALIDATORS.items():
            if spec.layer_types:
                for lt in spec.layer_types:
                    self.assertIn(
                        key, get_layer_type(lt).validators,
                        f"{key} is restricted to {lt} but {lt} does not list it",
                    )


class ValidatorRunTests(Fixtures):
    def test_only_the_layer_types_own_validators_run(self):
        layer = self.make_layer(self.scale_rows())
        run = run_for_layer(layer)
        allowed = set(get_layer_type("note_pitch").validators)
        for issue in run.issues.all():
            self.assertTrue(
                any(issue.code.startswith(p) for p in ("coverage", "annotation", "segment")),
                issue.code,
            )
        self.assertTrue(allowed)

    def test_the_layer_types_threshold_is_used_when_the_caller_passes_none(self):
        """A tail gap above note_pitch's own threshold but below the shipped default."""
        gap_threshold = get_layer_type("note_pitch").validator_params["annotation_coverage"][
            "coverage_gap_sec"
        ]
        last_end = DURATION_SEC - (gap_threshold + 1.0)
        layer = self.make_layer(self.rows([(1.0, 0.5, A4), (last_end - 0.5, 0.5, A4)]))

        codes = {i.code for i in run_for_layer(layer).issues.all()}
        self.assertIn("annotation_tail_gap", codes)

    def test_a_run_parameter_overrides_the_layer_types_threshold(self):
        gap_threshold = get_layer_type("note_pitch").validator_params["annotation_coverage"][
            "coverage_gap_sec"
        ]
        last_end = DURATION_SEC - (gap_threshold + 1.0)
        layer = self.make_layer(self.rows([(1.0, 0.5, A4), (last_end - 0.5, 0.5, A4)]))

        run = run_for_layer(layer, params={"coverage_gap_sec": DURATION_SEC})
        self.assertNotIn("annotation_tail_gap", {i.code for i in run.issues.all()})

    def test_overlapping_segments_are_flagged(self):
        layer = self.make_layer(self.rows([(1.0, 1.0, A4), (1.5, 1.0, C4)]))
        issues = {i.code: i for i in run_for_layer(layer).issues.all()}
        self.assertIn("segments_overlap", issues)
        details = issues["segments_overlap"].details
        self.assertEqual(details["total"], 1)
        self.assertAlmostEqual(details["largest_overlap_sec"], 0.5, places=6)
        self.assertEqual(details["pairs"][0]["a_index"], 0)
        self.assertEqual(details["pairs"][0]["b_index"], 1)

    def test_abutting_segments_do_not_overlap(self):
        layer = self.make_layer(self.rows([(1.0, 0.5, A4), (1.5, 0.5, C4)]))
        codes = {i.code for i in run_for_layer(layer).issues.all()}
        self.assertNotIn("segments_overlap", codes)
        self.assertNotIn("segments_overlap_within_tolerance", codes)


class RepeatedRunTests(Fixtures):
    """
    Findings accumulate in the database and the pages that read them show the latest
    answer. Every assertion here goes through the API, because the four places that read
    findings — the item page's list, the item list's counts, the "Open issues" filter and
    a layer's own tally — each had their own query and each had to be taught the rule.
    """

    def setUp(self):
        super().setUp()
        self.a1 = self.make_layer(self.scale_rows(n=4), stream="A1")
        self.a2 = self.make_layer(self.scale_rows(n=4, hz=C4), stream="A2")
        self.client = APIClient()

    def open_issues(self, item=None):
        item = item or self.item
        r = self.client.get(f"/api/items/{item.pk}/issues/?open=true")
        self.assertEqual(r.status_code, 200)
        return r.data

    def test_a_second_run_of_the_same_validators_leaves_the_list_the_same_length(self):
        run_for_item(self.item)
        after_one = self.open_issues()
        self.assertTrue(after_one, "the fixture should produce findings to count")

        second = run_for_item(self.item)
        after_two = self.open_issues()

        self.assertEqual(len(after_two), len(after_one))
        self.assertEqual({i["code"] for i in after_two}, {i["code"] for i in after_one})
        self.assertEqual({i["run"] for i in after_two}, {second.pk})

    def test_a_second_run_leaves_the_item_lists_counts_the_same(self):
        unprobed = self.make_item("item_unprobed", sample_rate=None)
        run_for_corpus(self.corpus)
        first = self.counts_for(unprobed)
        self.assertGreater(first["open_error_count"], 0, "an unread header is an error")

        run_for_corpus(self.corpus)

        self.assertEqual(self.counts_for(unprobed), first)

    def counts_for(self, item):
        r = self.client.get(f"/api/items/?q={item.song_id}")
        row = r.data["results"][0]
        return {k: row[k] for k in ("open_error_count", "open_warning_count")}

    def test_a_second_run_leaves_each_layers_own_tally_the_same(self):
        run_for_item(self.item)
        first = [l["open_issue_counts"] for l in self.detail_layers()]

        run_for_item(self.item)

        self.assertEqual([l["open_issue_counts"] for l in self.detail_layers()], first)

    def detail_layers(self):
        r = self.client.get(f"/api/items/{self.item.pk}/")
        return sorted(r.data["layers"], key=lambda l: l["id"])

    def test_the_open_issues_filter_names_an_item_once_after_two_runs(self):
        self.item.metadata_row = {"duration_sec": DURATION_SEC + 5.0}
        self.item.save(update_fields=["metadata_row"])
        run_for_item(self.item)
        run_for_item(self.item)

        r = self.client.get("/api/items/?has_open_issues=true")

        self.assertEqual([i["song_id"] for i in r.data["results"]], [self.item.song_id])

    def test_a_layer_run_supersedes_that_layer_alone(self):
        item_run = run_for_item(self.item)
        layer_run = run_for_layer(self.a1)

        by_layer = {}
        for issue in self.open_issues():
            by_layer.setdefault(issue["layer"], set()).add(issue["run"])

        self.assertEqual(by_layer[self.a1.pk], {layer_run.pk})
        self.assertEqual(by_layer[self.a2.pk], {item_run.pk})

    def test_a_finding_that_has_been_fixed_stops_being_listed(self):
        """
        The rule is the latest run that covered the layer, which is what lets a finding
        disappear. Taking the latest run that mentioned the layer would leave this one
        standing after the data was corrected.
        """
        overlapping = self.make_layer(self.rows([(1.0, 1.0, A4), (1.5, 1.0, C4)]), stream="B1")
        run_for_layer(overlapping)
        self.assertIn("segments_overlap", {i["code"] for i in self.open_issues()})

        Segment.objects.filter(layer=overlapping, index=1).update(start_sec=2.0, end_sec=3.0)
        run_for_layer(overlapping)

        codes = {i["code"] for i in self.open_issues() if i["layer"] == overlapping.pk}
        self.assertNotIn("segments_overlap", codes)

    def test_a_superseded_run_stays_readable_by_run(self):
        """History is the reason the rows are kept, so asking for a run by id still
        answers with everything that run reported."""
        first = run_for_item(self.item)
        run_for_item(self.item)

        r = self.client.get(f"/api/validation-issues/?run={first.pk}")

        self.assertEqual(r.data["count"], first.issues.count())
        self.assertEqual({i["run"] for i in r.data["results"]}, {first.pk})


class OpenIssueFilterTests(Fixtures):
    """
    "Open issues" and the status column answer one question between them, so they select
    the same items. An info finding reports a measurement rather than a fault, and an item
    carrying only those reads clean in both places.
    """

    def setUp(self):
        super().setUp()
        # An annotation that covers the recording. A tail gap past note_pitch's threshold
        # is a warning, and this class is about what an item with no warnings looks like.
        self.make_layer(self.scale_rows(n=19, start=0.5, step=1.0, dur=0.9), stream="A1")
        self.client = APIClient()

    def listed(self):
        """The song ids the "Open issues" chip would show."""
        r = self.client.get("/api/items/?has_open_issues=true")
        self.assertEqual(r.status_code, 200)
        return [i["song_id"] for i in r.data["results"]]

    def status_of(self, item):
        """The two counts the status column draws its pills from."""
        row = self.client.get(f"/api/items/?q={item.song_id}").data["results"][0]
        return row["open_error_count"], row["open_warning_count"]

    def claim_duration(self, item, seconds):
        item.metadata_row = {"duration_sec": seconds}
        item.save(update_fields=["metadata_row"])

    def severities(self, item):
        r = self.client.get(f"/api/items/{item.pk}/issues/?open=true")
        return {i["severity"] for i in r.data}

    def test_an_item_with_only_info_findings_reads_clean_and_is_left_out(self):
        run_for_item(self.item)

        self.assertEqual(self.severities(self.item), {"info"})
        self.assertEqual(self.status_of(self.item), (0, 0))
        self.assertEqual(self.listed(), [])

    def test_the_info_findings_stay_on_the_item_page(self):
        """The filter decides which items to show, and changes nothing about what an item
        reports once it is open."""
        run_for_item(self.item)

        r = self.client.get(f"/api/items/{self.item.pk}/issues/?open=true")

        self.assertEqual({i["severity"] for i in r.data}, {"info"})
        self.assertIn("coverage_summary", {i["code"] for i in r.data})

    def test_an_item_with_a_current_warning_is_listed(self):
        self.claim_duration(self.item, DURATION_SEC + 5.0)
        run_for_item(self.item)

        self.assertEqual(self.status_of(self.item), (0, 1))
        self.assertEqual(self.listed(), [self.item.song_id])

    def test_an_item_whose_warning_was_fixed_in_a_later_run_is_left_out(self):
        self.claim_duration(self.item, DURATION_SEC + 5.0)
        run_for_item(self.item)
        self.assertEqual(self.listed(), [self.item.song_id])

        self.claim_duration(self.item, DURATION_SEC)
        run_for_item(self.item)

        self.assertEqual(self.status_of(self.item), (0, 0))
        self.assertEqual(self.listed(), [])

    def test_an_error_lists_the_item(self):
        unprobed = self.make_item("item_unprobed", sample_rate=None)
        run_for_item(unprobed)

        self.assertEqual(self.status_of(unprobed), (1, 0))
        self.assertEqual(self.listed(), [unprobed.song_id])


class StoredComparisonApiTests(Fixtures):
    """The item page opens on the comparison already in the database, which it finds by
    asking for the ones run on this item."""

    def setUp(self):
        super().setUp()
        self.a1 = self.make_layer(self.scale_rows(n=4), stream="A1")
        self.a2 = self.make_layer(self.scale_rows(n=4, hz=C4), stream="A2")
        self.client = APIClient()

    def test_a_stored_comparison_is_found_by_item(self):
        run_comparison(self.a1, self.a2)

        r = self.client.get(f"/api/comparisons/?item={self.item.pk}")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)
        self.assertIn("matched", r.data["results"][0]["summary"])

    def test_the_newest_comparison_comes_first(self):
        older = run_comparison(self.a1, self.a2)
        newer = run_comparison(self.a1, self.a2, {"onset_tolerance_sec": 0.2})

        r = self.client.get(f"/api/comparisons/?item={self.item.pk}")

        self.assertEqual([c["id"] for c in r.data["results"]], [newer.pk, older.pk])

    def test_a_comparison_on_another_item_is_left_out(self):
        other = self.make_item("item_two")
        b1 = self.make_layer(self.scale_rows(n=4), stream="A1", item=other)
        b2 = self.make_layer(self.scale_rows(n=4, hz=C4), stream="A2", item=other)
        run_comparison(b1, b2)
        mine = run_comparison(self.a1, self.a2)

        r = self.client.get(f"/api/comparisons/?item={self.item.pk}")

        self.assertEqual([c["id"] for c in r.data["results"]], [mine.pk])

    def test_a_comparison_list_needs_no_authentication(self):
        run_comparison(self.a1, self.a2)

        r = APIClient().get(f"/api/comparisons/?item={self.item.pk}")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)

    def test_an_item_that_is_no_id_at_all_matches_nothing(self):
        run_comparison(self.a1, self.a2)

        r = self.client.get("/api/comparisons/?item=all")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 0)


class OverlapToleranceTests(Fixtures):
    """
    Both sides of segment_overlap_tolerance_sec.

    The tolerance exists because a note source states an onset and a duration, so a
    segment's end time is the sum of two independently rounded numbers and can land
    microseconds past the next onset with nothing wrong in the annotation. The threshold
    has to separate that from a real conflict without hiding either.
    """

    TOLERANCE = 0.001  # note_pitch's own, asserted below rather than assumed

    def overlapping_layer(self, overlap_sec, stream="A1"):
        """Two segments whose shared boundary is crossed by exactly overlap_sec."""
        return self.make_layer(
            [
                {"index": 0, "start_sec": 1.0, "end_sec": 1.5 + overlap_sec,
                 "payload": {"hz": A4}},
                {"index": 1, "start_sec": 1.5, "end_sec": 2.0, "payload": {"hz": C4}},
            ],
            stream=stream,
        )

    def codes_and_issues(self, layer, params=None):
        return {i.code: i for i in run_for_layer(layer, params=params).issues.all()}

    def test_note_pitch_sets_the_tolerance_this_class_assumes(self):
        vp = get_layer_type("note_pitch").validator_params["segments_monotonic"]
        self.assertEqual(vp["segment_overlap_tolerance_sec"], self.TOLERANCE)

    # -- at or below the tolerance: info -----------------------------------

    def test_an_overlap_below_the_tolerance_is_info(self):
        """The magnitude the demo corpus actually carries: tens of microseconds."""
        layer = self.overlapping_layer(2.2676e-05)
        issues = self.codes_and_issues(layer)
        self.assertIn("segments_overlap_within_tolerance", issues)
        self.assertNotIn("segments_overlap", issues)
        self.assertEqual(issues["segments_overlap_within_tolerance"].severity, "info")

    def test_the_info_finding_states_the_magnitude_and_the_tolerance(self):
        layer = self.overlapping_layer(2.2676e-05)
        issue = self.codes_and_issues(layer)["segments_overlap_within_tolerance"]
        self.assertIn("2.2676e-05s", issue.message)
        self.assertIn("rounding tolerance", issue.message)
        self.assertAlmostEqual(issue.details["largest_overlap_sec"], 2.2676e-05, places=9)
        self.assertEqual(issue.details["tolerance_sec"], self.TOLERANCE)
        self.assertTrue(issue.details["within_tolerance"])

    def test_an_overlap_exactly_at_the_tolerance_is_info(self):
        """"At or below" includes the boundary itself."""
        layer = self.overlapping_layer(self.TOLERANCE)
        issues = self.codes_and_issues(layer)
        self.assertIn("segments_overlap_within_tolerance", issues)
        self.assertNotIn("segments_overlap", issues)

    # -- above the tolerance: warning --------------------------------------

    def test_an_overlap_just_above_the_tolerance_is_a_warning(self):
        layer = self.overlapping_layer(self.TOLERANCE * 2)
        issues = self.codes_and_issues(layer)
        self.assertIn("segments_overlap", issues)
        self.assertNotIn("segments_overlap_within_tolerance", issues)
        self.assertEqual(issues["segments_overlap"].severity, "warning")
        self.assertEqual(issues["segments_overlap"].details["tolerance_sec"], self.TOLERANCE)

    def test_a_large_overlap_is_a_warning(self):
        layer = self.overlapping_layer(0.5)
        issues = self.codes_and_issues(layer)
        self.assertIn("segments_overlap", issues)
        self.assertEqual(issues["segments_overlap"].severity, "warning")

    def test_the_warning_names_the_tolerance_it_exceeded(self):
        layer = self.overlapping_layer(0.5)
        self.assertIn(
            "rounding tolerance",
            self.codes_and_issues(layer)["segments_overlap"].message,
        )

    # -- both sides at once -------------------------------------------------

    def test_the_two_kinds_are_reported_separately_in_one_layer(self):
        """A rounding artifact in the same layer must not mask a real conflict."""
        layer = self.make_layer(
            [
                {"index": 0, "start_sec": 1.0, "end_sec": 1.50002, "payload": {"hz": A4}},
                {"index": 1, "start_sec": 1.5, "end_sec": 2.5, "payload": {"hz": C4}},
                {"index": 2, "start_sec": 2.0, "end_sec": 3.0, "payload": {"hz": E4}},
            ]
        )
        issues = self.codes_and_issues(layer)
        self.assertIn("segments_overlap_within_tolerance", issues)
        self.assertIn("segments_overlap", issues)
        self.assertEqual(issues["segments_overlap_within_tolerance"].details["total"], 1)
        self.assertEqual(issues["segments_overlap"].details["total"], 1)
        self.assertAlmostEqual(
            issues["segments_overlap"].details["largest_overlap_sec"], 0.5, places=6
        )

    # -- the generic default is unchanged ----------------------------------

    def test_the_shipped_default_tolerance_is_zero(self):
        from .validators.checks import DEFAULTS

        self.assertEqual(DEFAULTS["segment_overlap_tolerance_sec"], 0.0)

    def test_with_the_tolerance_at_zero_a_tiny_overlap_is_a_warning(self):
        """
        The generic behaviour has to be exactly what it was before the tolerance existed:
        any detected overlap is a warning. A layer type that has not thought about its
        sources should not inherit someone else's leniency.
        """
        layer = self.overlapping_layer(2.2676e-05)
        issues = self.codes_and_issues(layer, params={"segment_overlap_tolerance_sec": 0.0})
        self.assertIn("segments_overlap", issues)
        self.assertNotIn("segments_overlap_within_tolerance", issues)

    def test_at_zero_the_warning_message_omits_the_tolerance_clause(self):
        layer = self.overlapping_layer(0.5)
        issues = self.codes_and_issues(layer, params={"segment_overlap_tolerance_sec": 0.0})
        self.assertNotIn("rounding tolerance", issues["segments_overlap"].message)

    def test_a_run_parameter_overrides_the_layer_types_tolerance(self):
        layer = self.overlapping_layer(0.5)
        issues = self.codes_and_issues(layer, params={"segment_overlap_tolerance_sec": 1.0})
        self.assertIn("segments_overlap_within_tolerance", issues)
        self.assertNotIn("segments_overlap", issues)

    def test_boundary_float_noise_is_not_an_overlap_at_all(self):
        """Below the touching epsilon there is nothing to grade, at any tolerance."""
        layer = self.overlapping_layer(1e-9)
        issues = self.codes_and_issues(layer, params={"segment_overlap_tolerance_sec": 0.0})
        self.assertNotIn("segments_overlap", issues)
        self.assertNotIn("segments_overlap_within_tolerance", issues)

    def test_an_internal_gap_is_info_and_carries_the_items_notes(self):
        self.item.notes = "A long rest here is part of the material."
        self.item.save(update_fields=["notes"])
        threshold = get_layer_type("note_pitch").validator_params["annotation_internal_gaps"][
            "internal_gap_sec"
        ]
        layer = self.make_layer(
            self.rows([(1.0, 0.5, A4), (2.0, 0.5, A4), (2.0 + threshold + 2.0, 0.5, A4)])
        )
        issues = {i.code: i for i in run_for_layer(layer).issues.all()}
        self.assertIn("annotation_internal_gap", issues)
        self.assertEqual(issues["annotation_internal_gap"].severity, "info")
        self.assertIn("part of the material", issues["annotation_internal_gap"].details["item_notes"])

    def test_annotation_past_the_reference_length_is_an_error(self):
        layer = self.make_layer(self.rows([(1.0, 0.5, A4), (DURATION_SEC + 5.0, 0.5, A4)]))
        codes = {i.code for i in run_for_layer(layer).issues.all()}
        self.assertIn("annotation_exceeds_audio", codes)

    def test_an_unprobed_item_is_an_error_at_item_scope(self):
        unprobed = self.make_item("no_header", sample_rate=None, duration_sec=None)
        self.make_layer(self.scale_rows(), item=unprobed)
        codes = {i.code for i in run_for_corpus(self.corpus).issues.all()}
        self.assertIn("media_not_probed", codes)


class SkipValidatorsTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.make_layer(self.scale_rows())

    def test_an_item_validator_runs_by_default(self):
        run = run_for_corpus(self.corpus)
        self.assertIn("recording_root_not_configured", {i.code for i in run.issues.all()})
        self.assertEqual(run.summary["skipped_validators"], [])

    def test_a_skipped_validator_produces_no_findings(self):
        self.corpus.skip_validators = ["recording_linked"]
        self.corpus.save(update_fields=["skip_validators"])
        run = run_for_corpus(self.corpus)
        self.assertNotIn("recording_root_not_configured", {i.code for i in run.issues.all()})

    def test_a_skipped_validator_is_named_in_the_summary(self):
        """
        "Not run" and "found nothing" are different results, and an empty finding list
        cannot tell them apart — so the skip is recorded rather than merely obeyed.
        """
        self.corpus.skip_validators = ["recording_linked"]
        self.corpus.save(update_fields=["skip_validators"])
        run = run_for_corpus(self.corpus)
        self.assertEqual(run.summary["skipped_validators"], ["recording_linked"])

    def test_skipping_a_corpus_scope_validator(self):
        self.corpus.skip_validators = ["disk_metadata_consistency"]
        self.corpus.save(update_fields=["skip_validators"])
        run = run_for_corpus(self.corpus)
        self.assertNotIn("metadata_coverage_summary", {i.code for i in run.issues.all()})
        self.assertEqual(run.summary["skipped_validators"], ["disk_metadata_consistency"])

    def test_skipping_leaves_the_other_validators_alone(self):
        self.corpus.skip_validators = ["recording_linked"]
        self.corpus.save(update_fields=["skip_validators"])
        codes = {i.code for i in run_for_corpus(self.corpus).issues.all()}
        self.assertIn("coverage_summary", codes)
        self.assertIn("metadata_coverage_summary", codes)


# ---------------------------------------------------------------------------
# Sonic Visualiser export
# ---------------------------------------------------------------------------


class ExportTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.a1 = self.make_layer(self.scale_rows(n=4), stream="A1")
        self.a2 = self.make_layer(self.scale_rows(n=4, hz=C4), stream="A2")

    def test_svl_frames_use_the_items_own_sample_rate(self):
        xml = layer_to_svl(self.a1)
        self.assertIn(f'sampleRate="{SAMPLE_RATE}"', xml)
        self.assertIn(f'frame="{int(round(1.0 * SAMPLE_RATE))}"', xml)

    def test_svl_refuses_without_a_sample_rate(self):
        from .exporters import ExportError

        self.item.sample_rate = None
        self.item.save(update_fields=["sample_rate"])
        with self.assertRaises(ExportError):
            layer_to_svl(AnnotationLayer.objects.get(pk=self.a1.pk))

    def test_a_session_is_bzip2_compressed(self):
        blob = item_to_session([self.a1, self.a2])
        # Not merely "decompresses": the magic number is what SV dispatches on, and a
        # gzipped session fails in a way whose error names the audio, not the format.
        self.assertTrue(blob.startswith(b"BZh"))
        bz2.decompress(blob)

    def test_a_session_contains_a_note_layer_for_each_annotator(self):
        xml = bz2.decompress(item_to_session([self.a1, self.a2])).decode()
        self.assertIn('name="A1 v1"', xml)
        self.assertIn('name="A2 v1"', xml)
        self.assertEqual(xml.count('type="notes"'), 2)

    def test_a_session_names_its_main_audio_model(self):
        xml = bz2.decompress(item_to_session([self.a1, self.a2])).decode()
        self.assertIn('mainModel="true"', xml)
        self.assertIn('type="wavefile"', xml)

    def test_the_two_annotators_get_different_colours(self):
        xml = bz2.decompress(item_to_session([self.a1, self.a2])).decode()
        import re

        colours = re.findall(r'type="notes"[^>]*colourName="([^"]+)"', xml)
        self.assertEqual(len(colours), 2)
        self.assertNotEqual(colours[0], colours[1])

    def test_layers_sort_by_stream_key(self):
        ordered = sort_layers_by_stream([self.a2, self.a1])
        self.assertEqual([l.stream_key for l in ordered], ["A1", "A2"])

    def test_a_session_refuses_layers_from_different_items(self):
        from .exporters import ExportError

        other = self.make_item("item_two")
        stray = self.make_layer(self.scale_rows(n=4), stream="A1", item=other)
        with self.assertRaises(ExportError):
            item_to_session([self.a1, stray])


# ---------------------------------------------------------------------------
# Audio header probing
# ---------------------------------------------------------------------------


class AudioHeaderTests(Fixtures):
    def test_specs_come_from_the_header(self):
        from .importers import probe_audio

        with tempfile.TemporaryDirectory() as tmp:
            wav = self.write_wav(Path(tmp) / "a.wav", seconds=3.0, sample_rate=8000, channels=2)
            info = probe_audio(wav)
        self.assertEqual(info.sample_rate, 8000)
        self.assertEqual(info.channels, 2)
        self.assertAlmostEqual(info.duration_sec, 3.0, places=3)

    def test_sidecar_and_resource_fork_files_are_ignored(self):
        from .importers import is_ignored_file

        self.assertTrue(is_ignored_file("audio/take.wav.asd"))
        self.assertTrue(is_ignored_file("audio/._take.wav"))
        self.assertTrue(is_ignored_file("audio/.DS_Store"))
        self.assertFalse(is_ignored_file("audio/take.wav"))


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


class CorrectionTests(Fixtures):
    def test_a_correction_is_an_overlay_and_leaves_the_segments_alone(self):
        from .models import LayerCorrection

        layer = self.make_layer(self.rows([(1.0, 0.5, A4)]))
        LayerCorrection.objects.create(
            layer=layer,
            kind=LayerCorrection.Kind.OCTAVE_SHIFT,
            params={"octaves": 1},
            reason="The export tool put this layer an octave low.",
        )
        self.assertAlmostEqual(layer.resolved_segments()[0]["payload"]["hz"], A4 * 2)
        # The stored row still matches the file as imported.
        self.assertAlmostEqual(layer.segments.get().payload["hz"], A4)

    def test_an_inactive_correction_is_not_applied(self):
        from .models import LayerCorrection

        layer = self.make_layer(self.rows([(1.0, 0.5, A4)]))
        LayerCorrection.objects.create(
            layer=layer,
            kind=LayerCorrection.Kind.OCTAVE_SHIFT,
            params={"octaves": 1},
            reason="Switched off for now.",
            is_active=False,
        )
        self.assertAlmostEqual(layer.resolved_segments()[0]["payload"]["hz"], A4)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class ApiTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.a1 = self.make_layer(self.scale_rows(n=4), stream="A1")
        self.a2 = self.make_layer(self.scale_rows(n=4, hz=C4), stream="A2")
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("reviewer", password="irrelevant")

    # -- reads -------------------------------------------------------------

    def test_listing_items_needs_no_authentication(self):
        r = self.client.get("/api/items/")
        self.assertEqual(r.status_code, 200)

    def test_listing_layer_types_needs_no_authentication(self):
        r = self.client.get("/api/layer-types/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("note_pitch", [t["key"] for t in r.data])

    def test_comparator_defaults_state_the_match_strategy(self):
        r = self.client.get("/api/comparator-defaults/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["note_pitch"]["match_strategy"], "onset")

    # -- who is asking -----------------------------------------------------

    def test_me_answers_a_visitor_with_no_session(self):
        """A 200 with the fact in it: having no session is an answer, and not a refusal."""
        r = self.client.get("/api/me/")
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.data["authenticated"], False)
        self.assertIsNone(r.data["username"])

    def test_me_names_the_signed_in_reader(self):
        self.client.force_authenticate(self.user)
        r = self.client.get("/api/me/")
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.data["authenticated"], True)
        self.assertEqual(r.data["username"], "reviewer")

    def test_me_grants_nothing(self):
        """Asking who you are leaves every permission where it was."""
        self.client.get("/api/me/")
        r = self.client.post(f"/api/layers/{self.a1.id}/validate/", {}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_item_detail_lists_both_annotations(self):
        r = self.client.get(f"/api/items/{self.item.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual({l["stream_key"] for l in r.data["layers"]}, {"A1", "A2"})

    def test_an_unknown_item_is_404_not_500(self):
        self.assertEqual(self.client.get("/api/items/999999/").status_code, 404)

    def test_header_fields_are_read_only(self):
        self.client.force_authenticate(self.user)
        r = self.client.patch(
            f"/api/items/{self.item.pk}/", {"sample_rate": 1}, format="json"
        )
        self.assertEqual(r.status_code, 200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.sample_rate, SAMPLE_RATE)

    # -- writes require auth ------------------------------------------------

    def test_creating_a_comparison_requires_authentication(self):
        r = self.client.post(
            "/api/comparisons/",
            {"layer_a": self.a1.pk, "layer_b": self.a2.pk},
            format="json",
        )
        self.assertEqual(r.status_code, 403)

    def test_uploading_requires_authentication(self):
        upload = SimpleUploadedFile("notes.csv", b"1.0,1.5,440.0\n", content_type="text/csv")
        r = self.client.post(
            f"/api/items/{self.item.pk}/upload-annotation/",
            {"file": upload, "stream_key": "A3", "column_contract": "onset_offset_hz"},
        )
        self.assertEqual(r.status_code, 403)

    def test_validating_requires_authentication(self):
        r = self.client.post(f"/api/items/{self.item.pk}/validate/", {}, format="json")
        self.assertEqual(r.status_code, 403)

    # -- upload -------------------------------------------------------------

    def test_upload_requires_a_column_contract(self):
        self.client.force_authenticate(self.user)
        upload = SimpleUploadedFile("notes.csv", b"1.0,440.0,0.5\n", content_type="text/csv")
        r = self.client.post(
            f"/api/items/{self.item.pk}/upload-annotation/",
            {"file": upload, "stream_key": "A3"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("column_contract", r.data)

    def test_upload_with_the_right_contract_creates_a_layer(self):
        self.client.force_authenticate(self.user)
        upload = SimpleUploadedFile(
            "notes.csv", b"1.0,440.0,0.5\n2.0,220.0,0.5\n", content_type="text/csv"
        )
        r = self.client.post(
            f"/api/items/{self.item.pk}/upload-annotation/",
            {"file": upload, "stream_key": "A3", "column_contract": "onset_hz_duration"},
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["layer"]["segment_count"], 2)
        self.assertEqual(r.data["layer"]["stream_key"], "A3")
        self.assertEqual(r.data["layer"]["version"], 1)
        self.assertEqual(
            r.data["layer"]["provenance"]["column_contract"], "onset_hz_duration"
        )

    def test_upload_with_the_wrong_contract_is_refused_and_stores_nothing(self):
        self.client.force_authenticate(self.user)
        before = AnnotationLayer.objects.count()
        upload = SimpleUploadedFile(
            "notes.csv", b"1.0,440.0,0.5\n2.0,220.0,0.5\n", content_type="text/csv"
        )
        r = self.client.post(
            f"/api/items/{self.item.pk}/upload-annotation/",
            {"file": upload, "stream_key": "A3", "column_contract": "onset_offset_hz"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("contradicts", r.data["detail"])
        self.assertEqual(AnnotationLayer.objects.count(), before)

    def test_upload_into_a_new_stream_does_not_supersede_the_others(self):
        self.client.force_authenticate(self.user)
        upload = SimpleUploadedFile("notes.csv", b"1.0,440.0,0.5\n", content_type="text/csv")
        self.client.post(
            f"/api/items/{self.item.pk}/upload-annotation/",
            {"file": upload, "stream_key": "A3", "column_contract": "onset_hz_duration"},
        )
        for layer in (self.a1, self.a2):
            layer.refresh_from_db()
            self.assertEqual(layer.version, 1)
            self.assertNotEqual(layer.status, AnnotationLayer.Status.SUPERSEDED)

    def test_upload_runs_the_validators(self):
        self.client.force_authenticate(self.user)
        upload = SimpleUploadedFile("notes.csv", b"1.0,440.0,0.5\n", content_type="text/csv")
        r = self.client.post(
            f"/api/items/{self.item.pk}/upload-annotation/",
            {"file": upload, "stream_key": "A3", "column_contract": "onset_hz_duration"},
        )
        self.assertEqual(r.status_code, 201)
        self.assertIn("summary", r.data["validation"])

    # -- comparison ---------------------------------------------------------

    def test_creating_a_comparison_returns_201_and_persists_the_tolerances(self):
        self.client.force_authenticate(self.user)
        r = self.client.post(
            "/api/comparisons/",
            {"layer_a": self.a1.pk, "layer_b": self.a2.pk},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["comparator_key"], "note_pitch_interval_cents")

        stored = LayerComparison.objects.get(pk=r.data["id"])
        # Stored, because otherwise nobody can later say what standard this difference
        # was computed to.
        for key, value in NOTE_PITCH_DEFAULTS.items():
            self.assertEqual(stored.params[key], value, key)
        self.assertEqual(stored.summary["segments_a"], 4)
        self.assertEqual(stored.summary["segments_b"], 4)

    def test_a_comparison_records_the_disagreements(self):
        self.client.force_authenticate(self.user)
        r = self.client.post(
            "/api/comparisons/",
            {"layer_a": self.a1.pk, "layer_b": self.a2.pk},
            format="json",
        )
        # A1 is all A4 and A2 is all C4, which is far more than the pitch tolerance.
        self.assertEqual(r.data["summary"]["pitch_only_mismatch"], 4)
        self.assertEqual(len(r.data["discrepancies"]), 4)

    def test_a_caller_supplied_tolerance_is_stored_with_the_comparison(self):
        self.client.force_authenticate(self.user)
        r = self.client.post(
            "/api/comparisons/",
            {
                "layer_a": self.a1.pk,
                "layer_b": self.a2.pk,
                "params": {"pitch_tolerance_cents": 2000.0},
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["params"]["pitch_tolerance_cents"], 2000.0)
        # At that tolerance the two annotations agree.
        self.assertEqual(r.data["summary"]["pitch_only_mismatch"], 0)

    def test_comparing_across_layer_types_is_rejected(self):
        self.client.force_authenticate(self.user)
        tagged = self.make_layer(
            [{"index": 0, "start_sec": 0.0, "end_sec": DURATION_SEC,
              "payload": {"labels": ["x"]}}],
            stream="clip",
            layer_type="audio_tagging",
        )
        r = self.client.post(
            "/api/comparisons/",
            {"layer_a": self.a1.pk, "layer_b": tagged.pk},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

    # -- review -------------------------------------------------------------

    def test_a_review_decision_moves_the_layer_status(self):
        self.client.force_authenticate(self.user)
        r = self.client.post(
            f"/api/layers/{self.a1.pk}/reviews/",
            {"decision": "accept", "quality_class": "whatever this corpus calls it"},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.status, AnnotationLayer.Status.ACCEPTED)

    def test_the_quality_label_is_free_text(self):
        """The field defines no choices on purpose: the vocabulary belongs to the corpus."""
        self.client.force_authenticate(self.user)
        label = "needs-a-second-listen"
        r = self.client.post(
            f"/api/layers/{self.a1.pk}/reviews/",
            {"decision": "accept_with_notes", "quality_class": label},
            format="json",
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["quality_class"], label)

    def test_a_correction_must_carry_a_reason(self):
        self.client.force_authenticate(self.user)
        r = self.client.post(
            "/api/corrections/",
            {"layer": self.a1.pk, "kind": "octave_shift", "params": {"octaves": 1},
             "reason": "   "},
            format="json",
        )
        self.assertEqual(r.status_code, 400)

    # -- export -------------------------------------------------------------

    def test_the_session_endpoint_returns_a_bzip2_session(self):
        r = self.client.get(f"/api/items/{self.item.pk}/export-session/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"BZh"))
        xml = bz2.decompress(r.content).decode()
        self.assertIn('name="A1 v1"', xml)
        self.assertIn('name="A2 v1"', xml)

    def test_the_svl_endpoint_names_the_stream_in_its_filename(self):
        r = self.client.get(f"/api/layers/{self.a1.pk}/export-svl/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(
            f'filename="{self.item.song_id}_A1_v1.svl"', r["Content-Disposition"]
        )


# ---------------------------------------------------------------------------
# Caller-supplied params at the API boundary
# ---------------------------------------------------------------------------


class ParamValidationTests(TestCase):
    """
    The unit level. Internals trust their thresholds and raise on a bad one, which is right
    for a programming error and wrong for a value that arrived as JSON — so the shape is
    checked before it gets that far.
    """

    def test_none_means_no_params(self):
        from .params import clean_params

        self.assertEqual(clean_params(None, allowed={"a": 1.0}, where="params"), {})

    def test_a_non_object_is_refused(self):
        from .params import ParamError, clean_params

        for bad in ("hello", [1, 2], 3, True):
            with self.assertRaises(ParamError, msg=repr(bad)) as ctx:
                clean_params(bad, allowed={"a": 1.0}, where="params")
            self.assertIn("must be a JSON object", str(ctx.exception))

    def test_an_unknown_key_is_refused_and_the_accepted_set_is_named(self):
        from .params import ParamError, clean_params

        with self.assertRaises(ParamError) as ctx:
            clean_params({"coverage_gap": 1.0}, allowed={"coverage_gap_sec": 5.0}, where="params")
        msg = str(ctx.exception)
        self.assertIn("coverage_gap", msg)
        self.assertIn("coverage_gap_sec", msg)

    def test_a_string_where_a_number_belongs_is_refused(self):
        from .params import ParamError, clean_params

        with self.assertRaises(ParamError) as ctx:
            clean_params({"a": "abc"}, allowed={"a": 1.0}, where="params")
        self.assertIn("must be a number", str(ctx.exception))

    def test_a_bool_is_not_accepted_as_a_number(self):
        """In Python a bool is an int, so an unguarded numeric check takes True as 1."""
        from .params import ParamError, clean_params

        with self.assertRaises(ParamError):
            clean_params({"a": True}, allowed={"a": 1.0}, where="params")

    def test_a_number_where_a_bool_belongs_is_refused(self):
        from .params import ParamError, clean_params

        with self.assertRaises(ParamError) as ctx:
            clean_params({"flag": 1}, allowed={"flag": True}, where="params")
        self.assertIn("true or false", str(ctx.exception))

    def test_an_int_is_accepted_for_a_float_knob(self):
        from .params import clean_params

        self.assertEqual(clean_params({"a": 2}, allowed={"a": 1.0}, where="params"), {"a": 2.0})

    def test_every_validator_threshold_is_accepted(self):
        from .params import validator_params
        from .validators.checks import DEFAULTS

        self.assertEqual(validator_params(dict(DEFAULTS)), {k: float(v) for k, v in DEFAULTS.items()})

    def test_a_bad_match_strategy_is_refused_before_the_comparator_sees_it(self):
        from .params import ParamError, comparator_params
        from .registry import comparator_for

        comparator = comparator_for("note_pitch")
        with self.assertRaises(ParamError) as ctx:
            comparator_params({"match_strategy": "telepathy"}, comparator)
        self.assertIn("telepathy", str(ctx.exception))

    def test_a_valid_match_strategy_passes(self):
        from .params import comparator_params
        from .registry import comparator_for

        cleaned = comparator_params({"match_strategy": "iou"}, comparator_for("note_pitch"))
        self.assertEqual(cleaned["match_strategy"], "iou")


class ParamApiTests(Fixtures):
    """
    The boundary itself: every endpoint that takes params must answer 400, never 500.

    A 500 says the server broke. For a value the client chose, that sends whoever is
    debugging to the server logs instead of to their own request body.
    """

    def setUp(self):
        super().setUp()
        self.a1 = self.make_layer(self.scale_rows(n=4), stream="A1")
        self.a2 = self.make_layer(self.scale_rows(n=4, hz=C4), stream="A2")
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("reviewer", password="irrelevant")
        self.client.force_authenticate(self.user)

    def validate_urls(self):
        return (
            f"/api/corpora/{self.corpus.pk}/validate/",
            f"/api/items/{self.item.pk}/validate/",
            f"/api/layers/{self.a1.pk}/validate/",
        )

    # -- validator params ---------------------------------------------------

    def test_a_string_threshold_is_400_on_every_validate_endpoint(self):
        for url in self.validate_urls():
            r = self.client.post(url, {"params": {"coverage_gap_sec": "abc"}}, format="json")
            self.assertEqual(r.status_code, 400, url)
            self.assertIn("must be a number", str(r.data), url)

    def test_a_non_object_params_is_400_on_every_validate_endpoint(self):
        for url in self.validate_urls():
            for bad in ("hello", [1, 2]):
                r = self.client.post(url, {"params": bad}, format="json")
                self.assertEqual(r.status_code, 400, f"{url} {bad!r}")
                self.assertIn("must be a JSON object", str(r.data))

    def test_an_unknown_threshold_is_400(self):
        for url in self.validate_urls():
            r = self.client.post(url, {"params": {"nonsense": 1.0}}, format="json")
            self.assertEqual(r.status_code, 400, url)

    def test_a_valid_threshold_still_works(self):
        for url in self.validate_urls():
            r = self.client.post(url, {"params": {"coverage_gap_sec": 1.0}}, format="json")
            self.assertEqual(r.status_code, 201, url)

    def test_omitting_params_still_works(self):
        for url in self.validate_urls():
            self.assertEqual(self.client.post(url, {}, format="json").status_code, 201, url)

    # -- comparison params --------------------------------------------------

    def comparison(self, params):
        return self.client.post(
            "/api/comparisons/",
            {"layer_a": self.a1.pk, "layer_b": self.a2.pk, "params": params},
            format="json",
        )

    def test_a_bad_match_strategy_is_400_not_500(self):
        r = self.comparison({"match_strategy": "telepathy"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("telepathy", str(r.data))

    def test_a_null_match_strategy_is_400_not_500(self):
        r = self.comparison({"match_strategy": None})
        self.assertEqual(r.status_code, 400)

    def test_a_non_object_comparison_params_is_400_not_500(self):
        for bad in ("hello", [1, 2]):
            r = self.comparison(bad)
            self.assertEqual(r.status_code, 400, repr(bad))
            self.assertIn("must be a JSON object", str(r.data))

    def test_a_string_tolerance_is_400_not_500(self):
        r = self.comparison({"onset_tolerance_sec": "wide"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("must be a number", str(r.data))

    def test_an_unknown_comparison_param_is_400(self):
        self.assertEqual(self.comparison({"nonsense": 1}).status_code, 400)

    def test_a_valid_comparison_param_still_works(self):
        r = self.comparison({"match_strategy": "iou", "min_iou": 0.25})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["params"]["match_strategy"], "iou")
        self.assertEqual(r.data["params"]["min_iou"], 0.25)


# ---------------------------------------------------------------------------
# Regression tests for the reviewed defects
# ---------------------------------------------------------------------------


class IngestErrorMessageTests(Fixtures):
    def test_the_message_counts_failing_rows_not_every_row(self):
        """
        "20 rows do not satisfy the schema" for one bad row tells the operator the file or
        the contract is wrong, and sends them to re-examine 19 rows that are fine.
        """
        rows = self.rows([(float(n), 0.5, A4) for n in range(20)])
        rows[7]["payload"]["hz"] = 3.0
        with self.assertRaises(IngestError) as ctx:
            self.make_layer(rows)
        msg = str(ctx.exception)
        self.assertIn("1 of 20 rows does not satisfy", msg)
        self.assertNotIn("20 rows do not satisfy", msg)
        self.assertIn("row 7", msg)

    def test_several_failing_rows_are_counted(self):
        rows = self.rows([(float(n), 0.5, A4) for n in range(20)])
        for n in (2, 5, 9):
            rows[n]["payload"]["hz"] = 3.0
        with self.assertRaises(IngestError) as ctx:
            self.make_layer(rows)
        self.assertIn("3 of 20 rows do not satisfy", str(ctx.exception))

    def test_a_long_run_of_failures_is_reported_as_a_floor(self):
        """Listing stops early, so the count is explicitly a lower bound, not a total."""
        rows = self.rows([(float(n), 0.5, 3.0) for n in range(40)])
        with self.assertRaises(IngestError) as ctx:
            self.make_layer(rows)
        self.assertIn("at least", str(ctx.exception))


class DuplicateTrackNumberTests(TestCase):
    def test_two_files_with_the_same_track_number_are_refused(self):
        """
        Zero padding is enough: int("08") == int("8"), so both files would become the item
        vocadito_8 and the second would collide on uniq_item_song_id partway through the
        import — an IntegrityError that rolls everything back and names a constraint rather
        than the two files.
        """
        tracks = [(8, Path("Audio/vocadito_08.wav")), (8, Path("Audio/vocadito_8.wav"))]
        with self.assertRaises(ValueError) as ctx:
            _refuse_duplicate_track_numbers(tracks)
        msg = str(ctx.exception)
        self.assertIn("vocadito_08.wav", msg)
        self.assertIn("vocadito_8.wav", msg)
        self.assertIn("track 8", msg)

    def test_distinct_track_numbers_pass(self):
        tracks = [(8, Path("Audio/vocadito_8.wav")), (9, Path("Audio/vocadito_9.wav"))]
        self.assertIsNone(_refuse_duplicate_track_numbers(tracks))


class ProbeFailureTests(Fixtures):
    def test_a_failed_probe_clears_stale_specs_so_media_probed_reports_it(self):
        """
        An item probed once, then its audio replaced by something unreadable. Keeping the
        old specs would leave the item asserting media_is_probed while media_probe_error is
        set — so the validator stays silent and the exporter computes frames from a rate
        that describes a file that no longer exists.
        """
        from .services.vocadito import VocaditoReport

        self.assertTrue(self.item.media_is_probed)
        report = VocaditoReport()
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "not-audio.wav"
            bad.write_bytes(b"this is not a wav file")
            _probe(self.item, bad, report)

        self.assertFalse(self.item.media_is_probed)
        self.assertIsNone(self.item.sample_rate)
        self.assertIsNone(self.item.duration_sec)
        self.assertTrue(self.item.media_probe_error)
        self.assertEqual(len(report.probe_failures), 1)

        self.item.save()
        codes = {i.code for i in run_for_item(self.item).issues.all()}
        self.assertIn("media_not_probed", codes)


class SegmentTimeOrderTests(Fixtures):
    """
    Stored order is index order, which is the order the rows arrived in. An unsorted source
    file makes it disagree with the timeline, and a pairwise walk over the stored order then
    reports the wrong thing.
    """

    def unsorted_layer(self):
        # Three notes, stored out of time order: index 1 begins before index 0.
        return self.make_layer(
            [
                {"index": 0, "start_sec": 10.0, "end_sec": 10.5, "payload": {"hz": A4}},
                {"index": 1, "start_sec": 1.0, "end_sec": 1.5, "payload": {"hz": C4}},
                {"index": 2, "start_sec": 15.0, "end_sec": 15.5, "payload": {"hz": E4}},
            ]
        )

    def test_an_unsorted_layer_is_reported_as_an_ordering_finding(self):
        issues = {i.code: i for i in run_for_layer(self.unsorted_layer()).issues.all()}
        self.assertIn("segments_out_of_time_order", issues)
        self.assertEqual(issues["segments_out_of_time_order"].severity, "warning")
        self.assertEqual(issues["segments_out_of_time_order"].details["count"], 1)

    def test_an_unsorted_layer_is_not_reported_as_an_overlap(self):
        """
        The defect this replaces: walking index order, index 1 starts at 1.0s while index 0
        ends at 10.5s, so the pair looked like a 9.5s overlap that nobody annotated.
        """
        codes = {i.code for i in run_for_layer(self.unsorted_layer()).issues.all()}
        self.assertNotIn("segments_overlap", codes)
        self.assertNotIn("segments_overlap_within_tolerance", codes)

    def test_a_sorted_layer_reports_no_ordering_finding(self):
        layer = self.make_layer(self.rows([(1.0, 0.5, A4), (2.0, 0.5, C4), (3.0, 0.5, E4)]))
        codes = {i.code for i in run_for_layer(layer).issues.all()}
        self.assertNotIn("segments_out_of_time_order", codes)

    def test_a_real_overlap_is_still_found_in_an_unsorted_layer(self):
        """Sorting must not cost the check its actual job."""
        layer = self.make_layer(
            [
                {"index": 0, "start_sec": 10.0, "end_sec": 10.5, "payload": {"hz": A4}},
                {"index": 1, "start_sec": 1.0, "end_sec": 2.0, "payload": {"hz": C4}},
                {"index": 2, "start_sec": 1.5, "end_sec": 2.5, "payload": {"hz": E4}},
            ]
        )
        issues = {i.code: i for i in run_for_layer(layer).issues.all()}
        self.assertIn("segments_out_of_time_order", issues)
        self.assertIn("segments_overlap", issues)
        self.assertAlmostEqual(
            issues["segments_overlap"].details["largest_overlap_sec"], 0.5, places=6
        )

    def test_gaps_are_computed_on_time_sorted_segments(self):
        """
        Walking index order gives negative gaps, and a negative gap never exceeds the
        threshold — so a real gap went unreported and the layer looked fully covered.
        """
        threshold = get_layer_type("note_pitch").validator_params["annotation_internal_gaps"][
            "internal_gap_sec"
        ]
        far = 2.0 + threshold + 2.0
        layer = self.make_layer(
            [
                {"index": 0, "start_sec": far, "end_sec": far + 0.5, "payload": {"hz": A4}},
                {"index": 1, "start_sec": 1.0, "end_sec": 1.5, "payload": {"hz": C4}},
                {"index": 2, "start_sec": 2.0, "end_sec": 2.5, "payload": {"hz": E4}},
            ]
        )
        issues = {i.code: i for i in run_for_layer(layer).issues.all()}
        self.assertIn("annotation_internal_gap", issues)
        self.assertAlmostEqual(
            issues["annotation_internal_gap"].details["longest_sec"], far - 2.5, places=6
        )


class ContractRefusalMessageTests(TestCase):
    def test_the_rate_counts_rows_not_violations(self):
        """
        One row can break the contract twice — an offset before its onset, which is swapped
        rather than skipped, and then a negative onset — so counting violation strings
        produced "2 of 1 rows (200%)".
        """
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(io.StringIO("-5.0,-9.0,440.0\n"), contract="onset_offset_hz")
        msg = str(ctx.exception)
        self.assertIn("1 of 1 rows (100%)", msg)
        self.assertNotIn("2 of 1", msg)
        self.assertNotIn("200%", msg)

    def test_the_rate_never_exceeds_one_hundred_percent(self):
        body = "".join("-5.0,-9.0,440.0\n" for _ in range(10))
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(io.StringIO(body), contract="onset_offset_hz")
        self.assertIn("10 of 10 rows (100%)", str(ctx.exception))

    def test_the_hint_cannot_suggest_the_mapping_that_just_failed(self):
        """
        The caller's mapping is positionally identical to onset_offset_hz but spelled out,
        so excluding by contract key missed it; and the hint judged candidates by the module
        hz range rather than the caller's, so pitches the caller rejected looked fine. Both
        together let the message recommend re-reading the file under the very mapping that
        had just been refused.
        """
        body = "".join(f"{i}.0,{i}.5,80.0\n" for i in range(1, 11))
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(
                io.StringIO(body),
                contract={"onset": 0, "offset": 1, "hz": 2},
                min_hz=200.0,
                max_hz=400.0,
            )
        msg = str(ctx.exception)
        self.assertNotIn("onset_offset_hz", msg)
        self.assertNotIn("The data fits", msg)

    def test_the_hint_still_names_a_genuinely_different_contract(self):
        body = "".join(f"{i}.0,440.0,0.5\n" for i in range(1, 11))
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(io.StringIO(body), contract="onset_offset_hz")
        self.assertIn("onset_hz_duration", str(ctx.exception))

    def test_the_callers_hz_range_is_what_judges_a_row(self):
        body = "".join(f"{i}.0,{i}.5,440.0\n" for i in range(1, 11))
        with self.assertRaises(F0ParseError) as ctx:
            parse_f0_csv(
                io.StringIO(body), contract="onset_offset_hz", min_hz=20.0, max_hz=300.0
            )
        self.assertIn("20–300 Hz", str(ctx.exception))


class MatcherToleranceTests(TestCase):
    ROWS = [{"index": 0, "start_sec": 1.0, "end_sec": 1.5, "payload": {"hz": 440.0}}]

    def test_a_missing_onset_tolerance_raises_rather_than_defaulting(self):
        """The matcher holds no second copy of a default the stored params would not show."""
        with self.assertRaises(ValueError) as ctx:
            _match(self.ROWS, self.ROWS, {"match_strategy": "onset"})
        self.assertIn("onset_tolerance_sec", str(ctx.exception))

    def test_a_missing_min_iou_raises_for_the_iou_strategy(self):
        with self.assertRaises(ValueError) as ctx:
            _match(self.ROWS, self.ROWS, {"match_strategy": "iou"})
        self.assertIn("min_iou", str(ctx.exception))

    def test_a_non_numeric_tolerance_raises(self):
        with self.assertRaises(ValueError):
            _match(self.ROWS, self.ROWS,
                   {"match_strategy": "onset", "onset_tolerance_sec": "wide"})

    def test_the_onset_strategy_does_not_need_min_iou(self):
        pairs, _, _ = _match(
            self.ROWS, self.ROWS, {"match_strategy": "onset", "onset_tolerance_sec": 0.1}
        )
        self.assertEqual(len(pairs), 1)

    def test_both_shipped_defaults_satisfy_their_own_strategy(self):
        for defaults in (NOTE_PITCH_DEFAULTS, SED_DEFAULTS):
            pairs, _, _ = _match(self.ROWS, self.ROWS, dict(defaults))
            self.assertEqual(len(pairs), 1, defaults["match_strategy"])


class DemoImportComparisonTests(TestCase):
    """
    The import leaves the demo corpus ready to read.

    The landing page quotes three numbers from vocadito_21 and reads them from a stored
    comparison, so the import has to store one, storing it twice has to be the same as
    storing it once, and the numbers have to be these ones. The figures are checked here
    because a page states them to a reader who cannot check them.
    """

    root = Path(settings.BASE_DIR) / "demo-data" / "vocadito"

    def _import(self):
        call_command(
            "import_vocadito",
            slug="vocadito-demo",
            name="vocadito (demo subset)",
            root=str(self.root),
            stdout=io.StringIO(),
        )
        return Corpus.objects.get(slug="vocadito-demo")

    def test_import_stores_one_default_comparison_per_item_and_stays_that_way(self):
        corpus = self._import()
        items = list(corpus.items.order_by("song_id"))
        self.assertEqual(len(items), 5)
        for item in items:
            stored = LayerComparison.objects.filter(layer_a__item=item)
            self.assertEqual(stored.count(), 1, f"{item.song_id} should hold one comparison")
            self.assertEqual(stored.first().params, NOTE_PITCH_DEFAULTS)

        # Importing again finds them and leaves them alone.
        corpus = self._import()
        self.assertEqual(LayerComparison.objects.count(), 5)
        for item in corpus.items.all():
            self.assertEqual(LayerComparison.objects.filter(layer_a__item=item).count(), 1)

    def test_vocadito_21_compares_57_40_5(self):
        corpus = self._import()
        item = corpus.items.get(song_id="vocadito_21")
        comparison = LayerComparison.objects.get(layer_a__item=item)
        self.assertEqual(comparison.layer_a.stream_key, "A1")
        self.assertEqual(comparison.layer_b.stream_key, "A2")
        self.assertEqual(comparison.summary["matched"], 57)
        self.assertEqual(comparison.summary["only_in_a"], 40)
        self.assertEqual(comparison.summary["only_in_b"], 5)

    def test_a_comparison_run_to_other_tolerances_is_left_in_place(self):
        """The import adds its own and keeps someone else's: they answer different questions."""
        corpus = self._import()
        item = corpus.items.get(song_id="vocadito_21")
        a = item.layers.get(stream_key="A1")
        b = item.layers.get(stream_key="A2")
        LayerComparison.objects.filter(layer_a=a).delete()
        run_comparison(a, b, {"onset_tolerance_sec": 0.3})

        report = store_default_comparisons(corpus)
        self.assertEqual(report.created, 1)
        self.assertEqual(LayerComparison.objects.filter(layer_a=a).count(), 2)
        self.assertEqual(
            LayerComparison.objects.filter(layer_a=a, params=NOTE_PITCH_DEFAULTS).count(), 1
        )

