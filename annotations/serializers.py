from __future__ import annotations

from django.db.models import Count, IntegerField, Max, Min, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from rest_framework import serializers

from .models import (
    Adjudication,
    AnnotationLayer,
    Corpus,
    Item,
    LayerComparison,
    LayerCorrection,
    ReviewRecord,
    Segment,
    ValidationIssue,
    ValidationRun,
)
from .importers.f0_csv import CONTRACTS, FROM_HEADER
from .registry import LAYER_TYPES, get_layer_type


class CorpusSerializer(serializers.ModelSerializer):
    item_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Corpus
        fields = [
            "id", "slug", "name", "description", "root_path", "metadata_csv_path",
            "item_count", "created_at", "updated_at",
        ]


class SegmentSerializer(serializers.ModelSerializer):
    duration_sec = serializers.FloatField(read_only=True)

    class Meta:
        model = Segment
        fields = ["id", "index", "start_sec", "end_sec", "duration_sec", "payload"]

    def validate(self, attrs):
        if attrs.get("end_sec", 0) < attrs.get("start_sec", 0):
            raise serializers.ValidationError("end_sec may not be earlier than start_sec")
        return attrs


class LayerCorrectionSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    created_by_name = serializers.CharField(source="created_by.username", read_only=True)

    class Meta:
        model = LayerCorrection
        fields = [
            "id", "layer", "kind", "kind_display", "params", "reason", "order",
            "is_active", "created_by", "created_by_name", "created_at",
        ]
        read_only_fields = ["created_by"]

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError(
                "A correction must carry a reason, or nobody later can judge whether it should "
                "still be kept."
            )
        return value


class ReviewRecordSerializer(serializers.ModelSerializer):
    reviewer_name = serializers.CharField(source="reviewer.username", read_only=True)
    decision_display = serializers.CharField(source="get_decision_display", read_only=True)

    class Meta:
        model = ReviewRecord
        fields = [
            "id", "layer", "reviewer", "reviewer_name", "decision", "decision_display",
            "timing_usable", "quality_class", "metrics", "notes", "created_at",
        ]
        read_only_fields = ["reviewer"]


class LayerSerializer(serializers.ModelSerializer):
    segment_count = serializers.IntegerField(read_only=True)
    item_song_id = serializers.CharField(source="item.song_id", read_only=True)
    layer_type_label = serializers.SerializerMethodField()
    is_authoritative = serializers.BooleanField(read_only=True)
    tuning_reference_hz = serializers.FloatField(read_only=True)
    latest_review = serializers.SerializerMethodField()
    open_issue_counts = serializers.SerializerMethodField()
    active_correction_count = serializers.SerializerMethodField()
    time_span = serializers.SerializerMethodField()

    class Meta:
        model = AnnotationLayer
        fields = [
            "id", "item", "item_song_id", "layer_type", "layer_type_label", "stream_key",
            "version", "parent", "derived_from", "derivation_kind", "author", "author_kind",
            "status", "params", "provenance", "segment_count", "is_authoritative",
            "tuning_reference_hz", "latest_review", "open_issue_counts",
            "active_correction_count", "time_span", "created_at", "updated_at",
        ]

    def get_layer_type_label(self, obj):
        spec = get_layer_type(obj.layer_type)
        return spec.label if spec else obj.layer_type

    def get_latest_review(self, obj):
        review = obj.latest_review()
        return ReviewRecordSerializer(review).data if review else None

    def get_open_issue_counts(self, obj):
        rows = (
            obj.issues.unacknowledged()
            .current()
            .values("severity")
            .annotate(n=Count("id"))
        )
        return {r["severity"]: r["n"] for r in rows}

    def get_active_correction_count(self, obj):
        return obj.corrections.filter(is_active=True).count()

    def get_time_span(self, obj):
        """For drawing the coverage rail on the frontend. Does not return every segment."""
        agg = obj.segments.aggregate(first=Min("start_sec"), last=Max("end_sec"))
        return {"first_sec": agg["first"], "last_sec": agg["last"]}

    def validate(self, attrs):
        layer_type = attrs.get("layer_type", getattr(self.instance, "layer_type", None))
        spec = get_layer_type(layer_type)
        if spec is None:
            raise serializers.ValidationError(
                {"layer_type": f"Unregistered annotation type: {layer_type}"}
            )
        errs = spec.validate_params(attrs.get("params", {}) or {})
        if errs:
            raise serializers.ValidationError({"params": errs})
        return attrs


# What the item list treats as something to look at. An info finding states a fact about
# the data — how much of a recording an annotation covers, how many items carry a metadata
# row — and an item carrying only those has nothing outstanding. The status column has
# always counted these two severities; anything else selecting items has to agree with it,
# or the same page says both "clean" and "open issues" about the same row.
NEEDS_ATTENTION = ("error", "warning")


def item_issues(item_ref, severities=None):
    """Every unacknowledged finding about an item as it stands now: the item's own, and
    those its validators attached to one of its layers."""
    qs = (
        ValidationIssue.objects.filter(Q(item=item_ref) | Q(layer__item=item_ref))
        .unacknowledged()
        .current()
    )
    return qs.filter(severity__in=severities) if severities else qs


def open_issue_count(severity):
    """
    One severity's count for the row being serialized.

    A Count() over the joined issues would count every run's copy of the same finding, so
    the count is taken from the current-findings queryset instead, grouped by the one
    severity it was filtered to.
    """
    rows = (
        item_issues(OuterRef("pk"), (severity,))
        .order_by()
        .values("severity")
        .annotate(n=Count("id"))
        .values("n")
    )
    return Coalesce(Subquery(rows[:1], output_field=IntegerField()), 0)


class ItemSerializer(serializers.ModelSerializer):
    corpus_slug = serializers.CharField(source="corpus.slug", read_only=True)
    in_metadata = serializers.BooleanField(read_only=True)
    media_is_probed = serializers.BooleanField(read_only=True)
    has_recording = serializers.BooleanField(read_only=True)
    layer_count = serializers.IntegerField(read_only=True)
    open_error_count = serializers.IntegerField(read_only=True)
    open_warning_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Item
        fields = [
            "id", "corpus", "corpus_slug", "song_id", "song_id_is_provisional",
            "display_name", "disk_path", "audio_filename",
            "sample_rate", "duration_sec", "channels", "audio_format",
            "media_probed_at", "media_probe_error", "media_is_probed",
            "audio_is_proxy", "reference_duration_sec",
            "recording_path", "has_recording", "recording_linked_at",
            "recording_duration_sec", "recording_sample_rate", "recording_channels",
            "metadata_row", "metadata_match_method", "in_metadata", "notes",
            "layer_count", "open_error_count", "open_warning_count",
            "created_at", "updated_at",
        ]
        # The four fields read from the header are not writable through the API: their only
        # source is the audio file itself. audio_is_proxy and reference_duration_sec join
        # them because the importer is what decides both.
        read_only_fields = [
            "sample_rate", "duration_sec", "channels", "audio_format", "media_probed_at",
            "media_probe_error", "audio_is_proxy", "reference_duration_sec",
            "recording_path", "recording_linked_at", "recording_duration_sec",
            "recording_sample_rate", "recording_channels",
        ]

    @staticmethod
    def annotate(qs):
        return qs.annotate(
            layer_count=Count("layers", distinct=True),
            open_error_count=open_issue_count("error"),
            open_warning_count=open_issue_count("warning"),
        )


class ItemDetailSerializer(ItemSerializer):
    layers = serializers.SerializerMethodField()

    class Meta(ItemSerializer.Meta):
        fields = ItemSerializer.Meta.fields + ["layers"]

    def get_layers(self, obj):
        qs = obj.layers.annotate(segment_count=Count("segments")).order_by(
            "layer_type", "stream_key", "-version"
        )
        return LayerSerializer(qs, many=True, context=self.context).data


class ValidationIssueSerializer(serializers.ModelSerializer):
    item_song_id = serializers.CharField(source="item.song_id", read_only=True)
    layer_label = serializers.CharField(source="layer.__str__", read_only=True)

    class Meta:
        model = ValidationIssue
        fields = [
            "id", "run", "code", "severity", "message", "details",
            "corpus", "item", "item_song_id", "layer", "layer_label",
            "acknowledged", "acknowledged_note", "created_at",
        ]
        read_only_fields = [
            "run", "code", "severity", "message", "details", "corpus", "item", "layer",
        ]


class ValidationRunSerializer(serializers.ModelSerializer):
    issue_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ValidationRun
        fields = [
            "id", "scope", "corpus", "item", "layer", "validator_keys",
            "summary", "issue_count", "created_at", "finished_at",
        ]


class LayerComparisonSerializer(serializers.ModelSerializer):
    layer_a_label = serializers.CharField(source="layer_a.__str__", read_only=True)
    layer_b_label = serializers.CharField(source="layer_b.__str__", read_only=True)

    class Meta:
        model = LayerComparison
        fields = [
            "id", "layer_a", "layer_a_label", "layer_b", "layer_b_label",
            "comparator_key", "params", "summary", "discrepancies", "created_at",
        ]
        read_only_fields = ["comparator_key", "summary", "discrepancies"]


class AdjudicationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Adjudication
        fields = [
            "id", "item", "layer_type", "stream_key", "candidates", "chosen",
            "adjudicator", "agreement", "rationale", "created_at",
        ]


class LayerTypeSerializer(serializers.Serializer):
    """Saves the frontend from hard-coding annotation types: the type table is a projection
    of the backend registry."""

    key = serializers.CharField()
    label = serializers.CharField()
    segment_scope = serializers.CharField()
    description = serializers.CharField()
    payload_fields = serializers.SerializerMethodField()
    param_fields = serializers.SerializerMethodField()
    validators = serializers.ListField(child=serializers.CharField())
    default_comparator = serializers.CharField(allow_null=True)

    def get_payload_fields(self, obj):
        return [f.__dict__ for f in obj.payload_fields]

    def get_param_fields(self, obj):
        return [f.__dict__ for f in obj.param_fields]

    @staticmethod
    def all():
        return LayerTypeSerializer(list(LAYER_TYPES.values()), many=True).data


class F0UploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    stream_key = serializers.CharField(max_length=64)
    layer_type = serializers.CharField(max_length=64, default="note_pitch")
    #: Required, with no default. Three unlabelled numeric columns beginning with a time in
    #: seconds can be (onset, offset, hz) or (onset, pitch, duration), and the file does not
    #: say which. A default here would mean every uploader who did not think about it got
    #: one particular reading of their file, which is how the wrong one gets stored.
    column_contract = serializers.ChoiceField(choices=sorted(CONTRACTS) + [FROM_HEADER])
    tuning_reference_hz = serializers.FloatField(required=False)
    parent = serializers.PrimaryKeyRelatedField(
        queryset=AnnotationLayer.objects.all(), required=False, allow_null=True
    )
    note = serializers.CharField(required=False, allow_blank=True)

    def validate_layer_type(self, value):
        if get_layer_type(value) is None:
            raise serializers.ValidationError(f"Unregistered annotation type: {value}")
        return value
