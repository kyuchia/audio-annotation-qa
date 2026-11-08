"""
Core models.

Corpus
  └─ Item (audio)
       └─ AnnotationLayer (type, version, author, parent)
            └─ Segment (start_sec, end_sec, payload JSON)

Four rules are fixed at the model layer.

1. Segment times are always stored in seconds, never as frame indices. Frames appear only
   when exporting for Sonic Visualiser, converted with that item's own sample_rate.
2. An Item's song_id, display_name and disk_path are three independent fields, none
   derived from another. A directory name on disk may have been transformed on its way
   there — characters outside ASCII dropped by one tool, transliterated by another — and
   the transformations are not invertible, so a value that was lost cannot be recomputed
   from a value that survived. Each is stored as observed.
3. A version lineage and a set of parallel annotations are different things. Version,
   parent and status are scoped to (item, layer_type, stream_key): a correction of an
   existing annotation is a new version within one stream, while two annotators working
   independently from the same audio get a stream each, both at version 1, neither the
   parent of the other.
4. A quality label is an aid to interpretation, not a deletion criterion. It lives on
   ReviewRecord rather than on Item, and the model layer deliberately offers no manager
   that filters by it — that is how a grading scheme quietly becomes a filter.
"""
from __future__ import annotations

import math

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from .registry import get_layer_type

ASCII_KEY = RegexValidator(
    r"^[a-z0-9][a-z0-9_\-]*$",
    "song_id must be a stable ASCII key: lowercase letters, digits, underscore, hyphen.",
)


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


class Corpus(TimeStamped):
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    root_path = models.CharField(
        max_length=1024,
        help_text="Dataset root directory. Inside a container this is a path under the "
        "mounted data volume, e.g. /data/my-corpus.",
    )
    metadata_csv_path = models.CharField(
        max_length=1024,
        blank=True,
        help_text="Source of the claimed values. The importer does not trust the sample rate or "
        "duration in this file; it only uses them for consistency checks.",
    )
    recording_root = models.CharField(
        max_length=1024,
        blank=True,
        help_text="Directory holding the recordings **as seen by the machine that opens an "
        "exported session**, one subdirectory per item. This is the value written "
        "into Item.recording_path and from there into the session file, so it is the "
        "host-side path, not this process's. Blank means the recordings are not "
        "available, and exports fall back to whatever audio sits next to the "
        "annotations.",
    )
    recording_lookup_root = models.CharField(
        max_length=1024,
        blank=True,
        help_text="Where this process actually finds the recordings on disk, which inside a "
        "container is something like /recordings. Separate from recording_root "
        "because the process that checks a file exists and the process that later "
        "opens it are usually not the same one. Blank means look under "
        "recording_root.",
    )
    recording_filename = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Filename of the recording inside each item's subdirectory under the "
        "recording root, e.g. audio.wav.\n"
        "Blank by default rather than carrying a plausible guess. A default filename makes "
        "every corpus that has not configured one look as though it had, and the resulting "
        "miss is then reported as a file that is absent rather than as a setting nobody "
        "filled in — which sends the reader to the filesystem instead of to this field.",
    )
    skip_validators = models.JSONField(
        default=list,
        blank=True,
        help_text="Validator keys not to run for this corpus, e.g. [\"recording_linked\"]. "
        "Corpus- and item-scope validators otherwise run for every corpus, which is right "
        "when they describe something every corpus has. recording_linked does not: it "
        "exists for a corpus that ships a stand-in file instead of the recording its "
        "annotations were made against, and on a corpus whose own audio *is* that recording "
        "it reports the same non-finding once per item. That is noise, and noise is what "
        "stops people reading the findings that matter.\n"
        "Only the keys listed here are skipped, and they stay listed, so a silent check is "
        "still a visible decision rather than an absence. Layer-scope validators are "
        "selected by the layer type instead (registry.LayerTypeSpec.validators).",
    )

    class Meta:
        verbose_name_plural = "corpora"
        ordering = ["slug"]

    def __str__(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------


class Item(TimeStamped):
    corpus = models.ForeignKey(Corpus, on_delete=models.CASCADE, related_name="items")

    song_id = models.CharField(
        max_length=128,
        validators=[ASCII_KEY],
        help_text="Stable ASCII key. Not derived from disk_path or display_name, and they are "
        "not derived from it.",
    )
    song_id_is_provisional = models.BooleanField(
        default=False,
        help_text="Set when audio exists on disk but the metadata CSV has no row for it, so "
        "the importer minted a provisional key. It needs human confirmation before it "
        "counts as real; until then every export carries a warning.",
    )
    display_name = models.CharField(
        max_length=512,
        help_text="The name as it should be spelled, diacritics and all, even where the "
        "directory on disk has lost them.",
    )
    disk_path = models.CharField(
        max_length=1024,
        help_text="The item's audio directory, relative to the corpus root, exactly as it "
        "appears on disk — including any characters a tool upstream has already dropped. "
        "For a layout that keeps all audio in one directory this is that directory, and "
        "audio_filename is what identifies the item.",
    )
    audio_filename = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="The audio file's own name within disk_path. Set by the importer from what "
        "it actually found, never from a default: a layout that keeps every item's audio in "
        "one shared directory is distinguished by this name alone.",
    )

    # --- The next four fields are always read from the audio header by the importer. They
    # accept no manual input and do not trust metadata. ---
    sample_rate = models.PositiveIntegerField(null=True, blank=True)
    duration_sec = models.FloatField(null=True, blank=True)
    channels = models.PositiveSmallIntegerField(null=True, blank=True)
    audio_format = models.CharField(max_length=32, blank=True)
    media_probed_at = models.DateTimeField(null=True, blank=True)
    media_probe_error = models.TextField(blank=True)

    audio_is_proxy = models.BooleanField(
        default=False,
        help_text="True when the audio on disk is not the recording the annotations were made "
        "against, for instance a corpus that ships only a stand-in file. When "
        "true, coverage is measured against reference_duration_sec instead.",
    )
    reference_duration_sec = models.FloatField(
        null=True,
        blank=True,
        help_text="Length of the recording the annotations actually correspond to, as claimed "
        "by the corpus metadata. This is a claimed value, not a measured one, so it is "
        "used as the reference only when audio_is_proxy is true — precisely the case "
        "where that recording is not on hand to measure.",
    )
    recording_path = models.CharField(
        max_length=1024,
        blank=True,
        help_text="Absolute path to the original recording, expressed for the machine that "
        "opens the exported session. This holds a verified fact, not a derivation: it "
        "is written only after the file has been confirmed to exist, by "
        "link_recordings. Directory names under a corpus's recording root may happen to "
        "match those under its annotations, but that is how a given pair of trees was "
        "built, not a guarantee about any future one — so the path is resolved once, "
        "checked, and stored. Nothing downstream re-derives it.\n"
        "The path stored here must be the one the *reader* sees, not the one the "
        "*writer* used. A container path such as /recordings/... produces sessions that "
        "fail on every machine, including the one that wrote them, and the failure "
        "presents as a missing file rather than as a wrong path — which sends you "
        "looking at the drive instead of at the path. That is why link_recordings "
        "checks existence under the lookup root but stores the host root.\n"
        "Blank means not linked.",
    )
    # --- The recording's own specs, read from its header by link_recordings. These are
    # deliberately NOT the four fields above: those describe the file the annotations were
    # imported alongside, which is the pairing coverage_duration() reasons about. These
    # describe the original recording. Two different files, two different facts, two sets of
    # fields — one set that meant different things depending on what happened to be linked
    # is the mistake this avoids. ---
    recording_duration_sec = models.FloatField(
        null=True,
        blank=True,
        help_text="Measured duration of the linked recording. The session exporter uses it for "
        "the audio model's frame count, so that figure describes the file the session "
        "actually names.",
    )
    recording_sample_rate = models.PositiveIntegerField(null=True, blank=True)
    recording_channels = models.PositiveSmallIntegerField(null=True, blank=True)
    recording_linked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When recording_path was last confirmed against the filesystem. A path with "
        "no timestamp would say nothing about how stale the check is.",
    )
    metadata_row = models.JSONField(
        null=True,
        blank=True,
        help_text="The raw row from the metadata CSV. Null means this item is not in the CSV at "
        "all, i.e. disk and metadata disagree.",
    )
    metadata_match_method = models.CharField(
        max_length=32,
        blank=True,
        help_text="exact / folded / diacritics_stripped / manual. Anything other than exact "
        "means the match was inferred rather than certain.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["corpus", "song_id"]
        constraints = [
            models.UniqueConstraint(fields=["corpus", "song_id"], name="uniq_item_song_id"),
            # Not disk_path alone. A per-song layout gives each item its own directory, but
            # a flat per-type layout (Audio/vocadito_1.wav, Audio/vocadito_2.wav, ...) gives
            # them all the same one, and the file is what distinguishes them. What this
            # constraint was always for is "no two items may claim the same audio file",
            # which is the pair, and which holds for both layouts.
            models.UniqueConstraint(
                fields=["corpus", "disk_path", "audio_filename"], name="uniq_item_audio_path"
            ),
        ]
        indexes = [models.Index(fields=["corpus", "song_id"])]

    def __str__(self) -> str:
        return f"{self.song_id} — {self.display_name}"

    @property
    def in_metadata(self) -> bool:
        return self.metadata_row is not None

    @property
    def media_is_probed(self) -> bool:
        return self.sample_rate is not None and self.duration_sec is not None

    def coverage_duration(self) -> tuple[float | None, str]:
        """
        What coverage should be divided by. Returns (seconds, description of the source).

        When the audio on disk *is* the recording, the answer is the header: a measured
        value always wins. When it is a stand-in file while the annotation timeline
        corresponds to an original recording, dividing by the stand-in's length misreads
        the whole corpus as "annotation runs past the audio".
        """
        if self.audio_is_proxy and self.reference_duration_sec:
            return self.reference_duration_sec, "recording length claimed in metadata"
        if self.duration_sec:
            return self.duration_sec, "audio header"
        return None, "unknown"

    @property
    def has_recording(self) -> bool:
        return bool(self.recording_path)

    def playback_audio(self) -> tuple[str | None, str, str]:
        """
        Which audio file a viewer should actually open. Returns (path, kind, description),
        where kind is "recording" or "proxy".

        Deliberately shaped like coverage_duration(): both answer "which of two candidate
        sources are we using and why", and in both cases silently picking one and saying
        nothing is what makes the result impossible to argue with later. A reviewer opening
        a session against a stand-in will hear onsets that do not line up, and
        without being told which file they got, the obvious conclusion is that the
        annotation is wrong.

        recording_path wins when set, because it was verified to exist when it was written.
        Otherwise this falls back to the audio that sits beside the annotations.
        """
        if self.recording_path:
            return self.recording_path, "recording", "linked recording"
        base = (self.corpus.root_path or "").rstrip("/")
        # Both parts are required. With no filename there is no file to name, and composing
        # a path from an empty component yields one that points at a directory while looking
        # like a file — which fails later, somewhere that cannot say why.
        path = f"{base}/{self.disk_path}/{self.audio_filename}" if (base and self.audio_filename) else None
        kind = "proxy" if self.audio_is_proxy else "recording"
        description = (
            "stand-in file shipped beside the annotations"
            if self.audio_is_proxy
            else "audio stored beside the annotations"
        )
        return path, kind, description

    def seconds_to_frame(self, seconds: float) -> int:
        """The only place seconds may become frames. With no sample_rate it refuses to
        convert rather than guessing."""
        if not self.sample_rate:
            raise ValueError(
                f"{self.song_id} has no sample rate read yet, so frames cannot be computed"
            )
        return int(round(seconds * self.sample_rate))

    def record_media_info(self, info) -> None:
        self.sample_rate = info.sample_rate
        self.duration_sec = info.duration_sec
        self.channels = info.channels
        self.audio_format = info.format
        self.media_probed_at = timezone.now()
        self.media_probe_error = ""


# ---------------------------------------------------------------------------
# AnnotationLayer
# ---------------------------------------------------------------------------


class AnnotationLayer(TimeStamped):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        UNDER_REVIEW = "under_review", "Under review"
        ACCEPTED = "accepted", "Accepted"
        CHANGES_REQUESTED = "changes_requested", "Changes requested"
        REJECTED = "rejected", "Rejected"
        SUPERSEDED = "superseded", "Superseded by a newer version"

    class AuthorKind(models.TextChoices):
        PIPELINE = "pipeline", "Automated pipeline"
        HUMAN = "human", "Human"
        IMPORT = "import", "Imported from an existing file"
        SYSTEM = "system", "System-derived"

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="layers")
    layer_type = models.CharField(
        max_length=64, help_text="A key in registry.LAYER_TYPES, e.g. note_pitch."
    )
    stream_key = models.CharField(
        max_length=64,
        blank=True,
        help_text="A parallel track within the same item and layer type, e.g. one key per "
        "independent annotator (A1 / A2), or one per source. The name is "
        "deliberately generic: for an SED layer a stream might be a microphone number.\n"
        "Two streams are parallel, not a lineage. Version, parent and status are tracked "
        "per (layer_type, stream_key), so two annotators' layers are each version 1 with "
        "no parent, and neither supersedes the other.",
    )
    version = models.PositiveIntegerField(default=1)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
        help_text="Version lineage: pipeline v1 -> human-corrected v2.",
    )

    # Representations have a primary and a derived form. A quantised or re-encoded copy of
    # an annotation is a view of it, never the source of truth, and the distinction has to
    # survive in the data or the tidier-looking copy eventually gets used as the reference.
    derived_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="derivations",
        help_text="This layer is a derived representation of another one, e.g. a MIDI view "
        "quantised to whole semitones. A derived view is never the source of truth.",
    )
    derivation_kind = models.CharField(max_length=64, blank=True)

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="authored_layers",
    )
    author_kind = models.CharField(
        max_length=16, choices=AuthorKind.choices, default=AuthorKind.PIPELINE
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)

    params = models.JSONField(
        default=dict,
        blank=True,
        help_text="Layer-level parameters; the schema is defined by the layer_type. "
        "note_pitch uses it to store tuning_reference_hz.",
    )
    provenance = models.JSONField(
        default=dict,
        blank=True,
        help_text="Source file, pipeline version, parameters, import time, checksum of the "
        "original file.",
    )

    class Meta:
        ordering = ["item", "layer_type", "stream_key", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "layer_type", "stream_key", "version"],
                name="uniq_layer_version",
            )
        ]
        indexes = [models.Index(fields=["item", "layer_type", "stream_key"])]

    def __str__(self) -> str:
        stream = f"/{self.stream_key}" if self.stream_key else ""
        return f"{self.item.song_id}:{self.layer_type}{stream} v{self.version}"

    # -- Validation ---------------------------------------------------------

    def clean(self):
        spec = get_layer_type(self.layer_type)
        if spec is None:
            raise ValidationError(
                {"layer_type": f"Unregistered annotation type: {self.layer_type}"}
            )
        errs = spec.validate_params(self.params or {})
        if errs:
            raise ValidationError({"params": errs})
        if self.parent_id:
            if self.parent.item_id != self.item_id:
                raise ValidationError({"parent": "parent must belong to the same item"})
            if (self.parent.layer_type, self.parent.stream_key) != (
                self.layer_type,
                self.stream_key,
            ):
                raise ValidationError(
                    {"parent": "parent must be a layer of the same type and stream"}
                )
            if self.parent.version >= self.version:
                raise ValidationError({"version": "version must be greater than the parent's"})

    # -- Convenience properties ---------------------------------------------

    @property
    def type_spec(self):
        return get_layer_type(self.layer_type)

    @property
    def is_authoritative(self) -> bool:
        """A derived view is not authoritative. However tidy a MIDI view looks, it may not
        serve as the reference for a comparison."""
        return self.derived_from_id is None

    @property
    def tuning_reference_hz(self) -> float:
        return float((self.params or {}).get("tuning_reference_hz") or 440.0)

    def lineage(self) -> list["AnnotationLayer"]:
        chain, node, seen = [], self, set()
        while node and node.pk not in seen:
            seen.add(node.pk)
            chain.append(node)
            node = node.parent
        return list(reversed(chain))

    def next_version(self) -> int:
        latest = (
            AnnotationLayer.objects.filter(
                item=self.item, layer_type=self.layer_type, stream_key=self.stream_key
            )
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
        )
        return (latest or 0) + 1

    def active_corrections(self):
        return self.corrections.filter(is_active=True).order_by("order", "id")

    def latest_review(self):
        return self.reviews.order_by("-created_at").first()

    def resolved_segments(self) -> list[dict]:
        """A snapshot of the segments with the active corrections applied. The original
        Segment rows are never rewritten."""
        from .corrections import apply_corrections

        rows = [
            {
                "index": s.index,
                "start_sec": s.start_sec,
                "end_sec": s.end_sec,
                "payload": dict(s.payload or {}),
            }
            for s in self.segments.all()
        ]
        return apply_corrections(rows, list(self.active_corrections()))


# ---------------------------------------------------------------------------
# Segment
# ---------------------------------------------------------------------------


class Segment(models.Model):
    layer = models.ForeignKey(AnnotationLayer, on_delete=models.CASCADE, related_name="segments")
    index = models.PositiveIntegerField(help_text="Position within this layer, starting at 0.")
    start_sec = models.FloatField(help_text="Seconds. Never a frame index.")
    end_sec = models.FloatField(
        help_text="Seconds. For a clip-level annotation this equals the audio length."
    )
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="The schema is defined by the layer_type (note_pitch -> {hz}).",
    )

    class Meta:
        ordering = ["layer", "index"]
        constraints = [
            models.UniqueConstraint(fields=["layer", "index"], name="uniq_segment_index"),
            models.CheckConstraint(
                condition=models.Q(end_sec__gte=models.F("start_sec")),
                name="segment_end_after_start",
            ),
        ]
        indexes = [models.Index(fields=["layer", "start_sec"])]

    def __str__(self) -> str:
        return f"[{self.index}] {self.start_sec:.3f}–{self.end_sec:.3f}"

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec

    def clean(self):
        spec = self.layer.type_spec
        if spec:
            errs = spec.validate_payload(self.payload or {})
            if errs:
                raise ValidationError({"payload": errs})


# ---------------------------------------------------------------------------
# Corrections: traceable, and they never touch the original file
# ---------------------------------------------------------------------------


class LayerCorrection(TimeStamped):
    """
    A per-layer correction for a known, explainable defect in an imported annotation — a
    whole layer transposed by a tool that exported it, a constant timing offset, a run of
    segments that should not be there.

    Deliberately not implemented as "edit the Segments and save them back". The data as
    imported stays identical to the file on disk, and a correction is a switchable overlay
    carrying a reason and an author, applied only when the data is read or exported. That
    is what makes it reversible, reviewable, and visible as a correction rather than
    indistinguishable from the source.
    """

    class Kind(models.TextChoices):
        OCTAVE_SHIFT = "octave_shift", "Octave shift"
        CENTS_SHIFT = "cents_shift", "Cents shift"
        TIME_OFFSET = "time_offset", "Global time offset"
        DROP_SEGMENTS = "drop_segments", "Drop segments"
        TRIM_TAIL = "trim_tail", "Trim tail"

    layer = models.ForeignKey(
        AnnotationLayer, on_delete=models.CASCADE, related_name="corrections"
    )
    kind = models.CharField(max_length=32, choices=Kind.choices)
    params = models.JSONField(default=dict, blank=True)
    reason = models.TextField(
        help_text="Why this correction exists. Without it, nobody later can judge whether it "
        "should still be kept."
    )
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["layer", "order", "id"]

    def __str__(self) -> str:
        return f"{self.layer} · {self.get_kind_display()}"


# ---------------------------------------------------------------------------
# Review workflow (the primary one)
# ---------------------------------------------------------------------------


class ReviewRecord(TimeStamped):
    """
    A review outcome: a decision, who made it, when, and the evidence behind it.

    Outcomes like these tend to end up as extra columns in the annotation metadata itself,
    where they have no schema, no history and no author — so a decision cannot be
    questioned later, because nothing records what it was based on or who reached it.
    """

    class Decision(models.TextChoices):
        ACCEPT = "accept", "Accept"
        ACCEPT_WITH_NOTES = "accept_with_notes", "Accept with notes"
        REQUEST_CHANGES = "request_changes", "Request changes"
        REJECT = "reject", "Reject"

    layer = models.ForeignKey(AnnotationLayer, on_delete=models.CASCADE, related_name="reviews")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    decision = models.CharField(max_length=24, choices=Decision.choices)

    timing_usable = models.BooleanField(
        null=True,
        blank=True,
        help_text="Listening judgement: whether this annotation's timing is close enough to "
        "the audio to be usable. A human verdict, which is why it is nullable — undecided "
        "and 'not usable' are different answers."
    )
    quality_class = models.CharField(
        max_length=32,
        blank=True,
        help_text="Quality label. Deliberately has no choices: this vocabulary is "
        "corpus-specific and it changes: each corpus brings its own words for it, and "
        "pinning them to the model means a migration every time a new corpus arrives.\n"
        "Warning: this is an aid to interpretation, not a deletion criterion. Any query "
        "that filters on this field must say so explicitly at the call site; the system "
        "never filters on it by default.",
    )
    metrics = models.JSONField(
        default=dict,
        blank=True,
        help_text="The quantitative evidence behind the decision: whatever figures the "
        "reviewer actually looked at, such as coverage, segment counts, or comparison "
        "results. Only a decision with evidence can ever be re-examined.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["layer", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.layer} · {self.get_decision_display()}"


# ---------------------------------------------------------------------------
# Consensus workflow (an extension point, not yet part of the main flow)
# ---------------------------------------------------------------------------


class Adjudication(TimeStamped):
    """
    The workflow where several people annotate independently and someone then adjudicates
    between them.

    Modelled but not yet wired up: there is no workflow and no UI for it, and nothing in
    the system creates one. It is kept because it needs no change to the existing
    structure — an adjudication is only several parallel AnnotationLayers plus a choice
    among them and the reasoning for it.
    """

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="adjudications")
    layer_type = models.CharField(max_length=64)
    stream_key = models.CharField(max_length=64, blank=True)
    candidates = models.ManyToManyField(AnnotationLayer, related_name="adjudicated_in")
    chosen = models.ForeignKey(
        AnnotationLayer,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="chosen_in_adjudications",
    )
    adjudicator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    agreement = models.JSONField(default=dict, blank=True)
    rationale = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"Adjudication {self.item.song_id}:{self.layer_type}/{self.stream_key}"


# ---------------------------------------------------------------------------
# Validation results
# ---------------------------------------------------------------------------


class ValidationRun(TimeStamped):
    class Scope(models.TextChoices):
        CORPUS = "corpus", "Whole corpus"
        ITEM = "item", "Single item"
        LAYER = "layer", "Single layer"

    scope = models.CharField(max_length=16, choices=Scope.choices)
    corpus = models.ForeignKey(
        Corpus, null=True, blank=True, on_delete=models.CASCADE, related_name="validation_runs"
    )
    item = models.ForeignKey(Item, null=True, blank=True, on_delete=models.CASCADE)
    layer = models.ForeignKey(AnnotationLayer, null=True, blank=True, on_delete=models.CASCADE)
    validator_keys = models.JSONField(default=list, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        target = self.corpus or self.item or self.layer
        return f"Validation run #{self.pk} · {target}"


class ValidationIssue(models.Model):
    class Severity(models.TextChoices):
        ERROR = "error", "Error"
        WARNING = "warning", "Warning"
        INFO = "info", "Info"

    run = models.ForeignKey(ValidationRun, on_delete=models.CASCADE, related_name="issues")
    code = models.CharField(max_length=64)
    severity = models.CharField(max_length=8, choices=Severity.choices)
    message = models.TextField()
    details = models.JSONField(default=dict, blank=True)

    corpus = models.ForeignKey(Corpus, null=True, blank=True, on_delete=models.CASCADE)
    item = models.ForeignKey(
        Item, null=True, blank=True, on_delete=models.CASCADE, related_name="issues"
    )
    layer = models.ForeignKey(
        AnnotationLayer, null=True, blank=True, on_delete=models.CASCADE, related_name="issues"
    )

    acknowledged = models.BooleanField(
        default=False,
        help_text="A human has looked at this and judged it acceptable. The issue is not "
        "deleted, only marked as handled."
    )
    acknowledged_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["severity", "code", "id"]
        indexes = [
            models.Index(fields=["run", "severity"]),
            models.Index(fields=["item"]),
            models.Index(fields=["layer"]),
        ]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}"


# ---------------------------------------------------------------------------
# Consistency comparison results
# ---------------------------------------------------------------------------


class LayerComparison(TimeStamped):
    layer_a = models.ForeignKey(
        AnnotationLayer, on_delete=models.CASCADE, related_name="comparisons_as_a"
    )
    layer_b = models.ForeignKey(
        AnnotationLayer, on_delete=models.CASCADE, related_name="comparisons_as_b"
    )
    comparator_key = models.CharField(max_length=64)
    params = models.JSONField(
        default=dict,
        blank=True,
        help_text="Tolerance parameters (onset in seconds, pitch in cents). Stored, because "
        "otherwise nobody can later tell what standard this difference was computed to.",
    )
    summary = models.JSONField(default=dict, blank=True)
    discrepancies = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Comparison {self.layer_a} <-> {self.layer_b}"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def hz_to_cents(hz: float, reference_hz: float = 440.0) -> float:
    return 1200.0 * math.log2(hz / reference_hz)


def cents_between(hz_a: float, hz_b: float) -> float:
    return 1200.0 * math.log2(hz_a / hz_b)


def hz_to_midi(hz: float, reference_hz: float = 440.0) -> float:
    """A4 = MIDI 69. When the layer's tuning reference is not 440 it has to be passed in,
    or every pitch converted comes out skewed by the same amount."""
    return 69.0 + 12.0 * math.log2(hz / reference_hz)
