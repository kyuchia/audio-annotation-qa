"""
Django admin — the researcher's own acceptance interface.

This is the main reason for choosing Django over FastAPI: the problem needs an admin back
office, role permissions and migrations that evolve. So the admin is not a freebie that
came along for the ride. It is a first-class user interface, and it is worth the time to
set up list_filter, inlines and actions properly.

The Vue side is for annotators and reviewers; the admin is where a researcher accepts
data and puts out fires.
"""
from __future__ import annotations

from django.contrib import admin, messages
from django.db.models import Count, Max, Min
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .exporters import ExportError, item_to_session, suggested_session_filename
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
from .services.vocadito import ingest_vocadito
from .validators import run_for_corpus, run_for_item, run_for_layer


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


@admin.register(Corpus)
class CorpusAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "item_count", "issue_badge", "root_path")
    search_fields = ("slug", "name")
    actions = ("action_ingest", "action_validate")

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_items=Count("items"))

    @admin.display(description="Items", ordering="_items")
    def item_count(self, obj):
        return obj._items

    @admin.display(description="Open issues")
    def issue_badge(self, obj):
        qs = ValidationIssue.objects.filter(item__corpus=obj, acknowledged=False)
        errors = qs.filter(severity="error").count()
        warnings = qs.filter(severity="warning").count()
        return _severity_badge(errors, warnings)

    @admin.action(description="Rescan a vocadito tree and import (reads headers, ignores .asd)")
    def action_ingest(self, request, queryset):
        for corpus in queryset:
            try:
                report = ingest_vocadito(corpus)
            except (FileNotFoundError, ValueError) as exc:
                self.message_user(request, f"{corpus}: {exc}", messages.ERROR)
                continue
            self.message_user(
                request,
                f"{corpus}: {report.items_created} items created, "
                f"{report.items_updated} updated, {report.layers_created} layers and "
                f"{report.segments_created} segments imported."
                + (
                    f" {len(report.probe_failures)} headers could not be read."
                    if report.probe_failures
                    else ""
                ),
                messages.WARNING if report.probe_failures else messages.SUCCESS,
            )

    @admin.action(description="Run every validator")
    def action_validate(self, request, queryset):
        for corpus in queryset:
            run = run_for_corpus(corpus, user=request.user)
            self.message_user(
                request,
                format_html(
                    '{} validated: {} findings. <a href="{}">View</a>',
                    corpus.name,
                    run.summary.get("total", 0),
                    reverse("admin:annotations_validationissue_changelist") + f"?run__id__exact={run.pk}",
                ),
            )


# ---------------------------------------------------------------------------
# Item
# ---------------------------------------------------------------------------


class LayerInline(admin.TabularInline):
    model = AnnotationLayer
    extra = 0
    fields = ("layer_link", "layer_type", "stream_key", "version", "status", "author_kind", "segment_count")
    readonly_fields = fields
    show_change_link = False
    can_delete = False
    ordering = ("layer_type", "stream_key", "-version")

    def has_add_permission(self, request, obj):
        return False

    @admin.display(description="layer")
    def layer_link(self, obj):
        url = reverse("admin:annotations_annotationlayer_change", args=[obj.pk])
        return format_html('<a href="{}">#{}</a>', url, obj.pk)

    @admin.display(description="Segments")
    def segment_count(self, obj):
        return obj.segments.count()


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = (
        "song_id", "display_name", "metadata_state", "media_spec", "layer_count", "issue_badge",
    )
    list_filter = (
        "corpus",
        "song_id_is_provisional",
        "metadata_match_method",
        "sample_rate",
        "channels",
    )
    search_fields = ("song_id", "display_name", "disk_path")
    inlines = [LayerInline]
    actions = ("action_validate", "action_export_session")
    readonly_fields = (
        "sample_rate", "duration_sec", "channels", "audio_format",
        "media_probed_at", "media_probe_error", "metadata_row_pretty",
    )
    fieldsets = (
        ("Identity (three independent fields, none derived from another)", {
            "fields": ("corpus", "song_id", "song_id_is_provisional", "display_name",
                       "disk_path", "audio_filename"),
            "description": "A directory name on disk may have lost characters on its way "
                           "there, and the tool that dropped them and the tool that "
                           "transliterated them do not agree, so neither name can be "
                           "recomputed from the other. Maintain all three fields separately. "
                           "For a layout that keeps every item's audio in one directory, it "
                           "is audio_filename that identifies the item.",
        }),
        ("Audio specs (read-only, sourced from the file header)", {
            "fields": ("sample_rate", "duration_sec", "channels", "audio_format",
                       "media_probed_at", "media_probe_error"),
            "description": "These four are always read from the audio header. They accept no "
                           "manual input, and the values metadata claims are not trusted.",
        }),
        ("metadata", {"fields": ("metadata_match_method", "metadata_row_pretty", "notes")}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("corpus").annotate(
            _layers=Count("layers", distinct=True)
        )

    @admin.display(description="metadata")
    def metadata_state(self, obj):
        if not obj.in_metadata:
            return format_html('<b style="color:#9a6700">on disk, not in CSV</b>')
        if obj.metadata_match_method in {"folded", "diacritics_stripped"}:
            return format_html(
                '<span title="inferred match">{} (inferred)</span>', obj.metadata_match_method
            )
        return obj.metadata_match_method or "—"

    @admin.display(description="Specs")
    def media_spec(self, obj):
        if not obj.media_is_probed:
            return format_html('<b style="color:#b3261e">not read</b>')
        return f"{obj.sample_rate} Hz · {obj.channels}ch · {obj.duration_sec:.2f}s"

    @admin.display(description="layers", ordering="_layers")
    def layer_count(self, obj):
        return obj._layers

    @admin.display(description="Open issues")
    def issue_badge(self, obj):
        qs = obj.issues.filter(acknowledged=False)
        return _severity_badge(qs.filter(severity="error").count(), qs.filter(severity="warning").count())

    @admin.display(description="Raw metadata row")
    def metadata_row_pretty(self, obj):
        if not obj.metadata_row:
            return "(this item is not in the metadata CSV)"
        rows = "".join(
            f"<tr><th style='text-align:left;padding-right:1em'>{k}</th><td>{v}</td></tr>"
            for k, v in obj.metadata_row.items()
        )
        return mark_safe(f"<table>{rows}</table>")

    @admin.action(description="Run validators")
    def action_validate(self, request, queryset):
        total = 0
        for item in queryset:
            total += run_for_item(item, user=request.user).summary.get("total", 0)
        self.message_user(request, f"{total} findings produced.")

    @admin.action(description="Export an SV session (.sv)")
    def action_export_session(self, request, queryset):
        from django.http import HttpResponse

        if queryset.count() != 1:
            self.message_user(request, "Only one item can be exported at a time.", messages.ERROR)
            return None
        item = queryset.first()
        try:
            blob = item_to_session(item.layers.all())
        except ExportError as exc:
            self.message_user(request, str(exc), messages.ERROR)
            return None
        resp = HttpResponse(blob, content_type="application/x-sonic-visualiser")
        resp["Content-Disposition"] = f'attachment; filename="{suggested_session_filename(item)}"'
        return resp


# ---------------------------------------------------------------------------
# AnnotationLayer
# ---------------------------------------------------------------------------


class SegmentInline(admin.TabularInline):
    model = Segment
    extra = 0
    fields = ("index", "start_sec", "end_sec", "payload")
    ordering = ("index",)
    max_num = 0  # Read-only: segments are import output. To change them, open a new version
    # or add a correction.
    can_delete = False
    readonly_fields = fields

    def has_add_permission(self, request, obj):
        return False


class CorrectionInline(admin.TabularInline):
    model = LayerCorrection
    extra = 0
    fields = ("kind", "params", "reason", "order", "is_active", "created_by")
    readonly_fields = ("created_by",)


class ReviewInline(admin.TabularInline):
    model = ReviewRecord
    extra = 0
    fields = ("decision", "timing_usable", "quality_class", "metrics", "notes", "reviewer", "created_at")
    readonly_fields = ("reviewer", "created_at")


@admin.register(AnnotationLayer)
class AnnotationLayerAdmin(admin.ModelAdmin):
    list_display = (
        "__str__", "status", "author_kind", "segment_count", "span", "authoritative",
        "review_state", "issue_badge",
    )
    list_filter = ("layer_type", "status", "author_kind", "stream_key", "item__corpus")
    search_fields = ("item__song_id", "item__display_name", "stream_key")
    autocomplete_fields = ("item", "parent", "derived_from")
    inlines = [CorrectionInline, ReviewInline, SegmentInline]
    actions = ("action_validate",)
    readonly_fields = ("lineage_view", "provenance")

    fieldsets = (
        (None, {"fields": ("item", "layer_type", "stream_key", "version", "status")}),
        ("Version lineage", {"fields": ("parent", "lineage_view")}),
        ("Primary and derived representation", {
            "fields": ("derived_from", "derivation_kind"),
            "description": "The CSV is authoritative and the MIDI is a derived view. MIDI can "
                           "only hold whole semitones, so a derived layer is never the "
                           "reference for a comparison.",
        }),
        ("Parameters and provenance", {"fields": ("author", "author_kind", "params", "provenance")}),
    )

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("item")
            .annotate(_segments=Count("segments", distinct=True),
                      _first=Min("segments__start_sec"), _last=Max("segments__end_sec"))
        )

    @admin.display(description="segments", ordering="_segments")
    def segment_count(self, obj):
        return obj._segments

    @admin.display(description="Annotated span")
    def span(self, obj):
        if obj._first is None:
            return "—"
        tail = ""
        if obj.item.duration_sec:
            gap = obj.item.duration_sec - obj._last
            if gap > 5:
                tail = format_html(' <b style="color:#9a6700">{}s missing at end</b>', f"{gap:.1f}")
        return format_html("{:.2f}–{:.2f}s{}", obj._first, obj._last, tail)

    @admin.display(description="Authoritative", boolean=True)
    def authoritative(self, obj):
        return obj.is_authoritative

    @admin.display(description="Latest review")
    def review_state(self, obj):
        r = obj.latest_review()
        if not r:
            return "—"
        bits = [r.get_decision_display()]
        if r.timing_usable is not None:
            bits.append("timing usable" if r.timing_usable else "timing not usable")
        if r.quality_class:
            bits.append(f"class {r.quality_class}")
        return " · ".join(bits)

    @admin.display(description="Open issues")
    def issue_badge(self, obj):
        qs = obj.issues.filter(acknowledged=False)
        return _severity_badge(qs.filter(severity="error").count(), qs.filter(severity="warning").count())

    @admin.display(description="Lineage")
    def lineage_view(self, obj):
        if not obj.pk:
            return "—"
        parts = []
        for node in obj.lineage():
            label = f"v{node.version} ({node.get_author_kind_display()})"
            if node.pk == obj.pk:
                parts.append(f"<b>{label}</b>")
            else:
                url = reverse("admin:annotations_annotationlayer_change", args=[node.pk])
                parts.append(f'<a href="{url}">{label}</a>')
        return mark_safe(" -&gt; ".join(parts))

    @admin.action(description="Run validators")
    def action_validate(self, request, queryset):
        total = sum(run_for_layer(l, user=request.user).summary.get("total", 0) for l in queryset)
        self.message_user(request, f"{total} findings produced.")

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in instances:
            if isinstance(obj, (LayerCorrection,)) and obj.created_by_id is None:
                obj.created_by = request.user
            if isinstance(obj, ReviewRecord) and obj.reviewer_id is None:
                obj.reviewer = request.user
            obj.save()
        formset.save_m2m()
        for obj in formset.deleted_objects:
            obj.delete()


# ---------------------------------------------------------------------------
# Validation results and comparisons
# ---------------------------------------------------------------------------


@admin.register(ValidationRun)
class ValidationRunAdmin(admin.ModelAdmin):
    list_display = ("id", "scope", "target", "total", "by_severity", "created_at", "finished_at")
    list_filter = ("scope", "created_at")
    readonly_fields = ("summary", "validator_keys")

    @admin.display(description="Target")
    def target(self, obj):
        return obj.corpus or obj.item or obj.layer

    @admin.display(description="Total")
    def total(self, obj):
        return obj.summary.get("total", 0)

    @admin.display(description="Breakdown")
    def by_severity(self, obj):
        d = obj.summary.get("by_severity", {})
        return " · ".join(f"{k} {v}" for k, v in d.items()) or "—"


@admin.register(ValidationIssue)
class ValidationIssueAdmin(admin.ModelAdmin):
    list_display = ("severity_tag", "code", "short_message", "item", "layer", "acknowledged")
    list_filter = ("severity", "code", "acknowledged", "item__corpus")
    search_fields = ("message", "code", "item__song_id")
    readonly_fields = ("run", "code", "severity", "message", "details", "corpus", "item", "layer")
    actions = ("action_acknowledge",)

    @admin.display(description="Severity", ordering="severity")
    def severity_tag(self, obj):
        colour = {"error": "#b3261e", "warning": "#9a6700", "info": "#5a6672"}[obj.severity]
        return format_html('<b style="color:{}">{}</b>', colour, obj.get_severity_display())

    @admin.display(description="Message")
    def short_message(self, obj):
        return obj.message if len(obj.message) <= 110 else obj.message[:110] + "…"

    @admin.action(description="Mark as seen (acceptable)")
    def action_acknowledge(self, request, queryset):
        n = queryset.update(acknowledged=True)
        self.message_user(request, f"{n} marked as handled. The original records are kept.")


@admin.register(LayerComparison)
class LayerComparisonAdmin(admin.ModelAdmin):
    list_display = ("__str__", "comparator_key", "headline", "created_at")
    readonly_fields = ("summary", "discrepancies", "params")

    @admin.display(description="Result")
    def headline(self, obj):
        s = obj.summary or {}
        return (
            f"matched {s.get('matched', 0)} / "
            f"only in A {s.get('only_in_a', 0)} / only in B {s.get('only_in_b', 0)} / "
            f"pitch mismatches {s.get('pitch_only_mismatch', 0)} / "
            f"timing mismatches {s.get('timing_only_mismatch', 0)}"
        )


@admin.register(ReviewRecord)
class ReviewRecordAdmin(admin.ModelAdmin):
    list_display = ("layer", "decision", "timing_usable", "quality_class", "reviewer", "created_at")
    list_filter = ("decision", "timing_usable", "quality_class")
    search_fields = ("layer__item__song_id", "notes")


@admin.register(Adjudication)
class AdjudicationAdmin(admin.ModelAdmin):
    list_display = ("item", "layer_type", "stream_key", "chosen", "adjudicator", "created_at")
    filter_horizontal = ("candidates",)


@admin.register(LayerCorrection)
class LayerCorrectionAdmin(admin.ModelAdmin):
    list_display = ("layer", "kind", "params", "is_active", "created_by", "created_at")
    list_filter = ("kind", "is_active")


def _severity_badge(errors: int, warnings: int):
    if not errors and not warnings:
        return format_html('<span style="color:#1f6f4a">clean</span>')
    parts = []
    if errors:
        parts.append(f'<b style="color:#b3261e">{errors} errors</b>')
    if warnings:
        parts.append(f'<span style="color:#9a6700">{warnings} warnings</span>')
    return mark_safe(" · ".join(parts))


admin.site.site_header = "Audio annotation management platform"
admin.site.site_title = "Annotation management"
admin.site.index_title = "Data review"
