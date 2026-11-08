from __future__ import annotations

from django.db.models import Count, Q
from django.http import HttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from rest_framework.response import Response

from .comparators.pitch_interval import NOTE_PITCH_DEFAULTS, SED_DEFAULTS
from .exporters import ExportError, item_to_session, layer_to_svl, suggested_filename, suggested_session_filename
from .importers import parse_f0_csv
from .importers.f0_csv import F0ParseError
from .params import ParamError, comparator_params, validator_params
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
from .registry import comparator_for
from .serializers import (
    AdjudicationSerializer,
    CorpusSerializer,
    F0UploadSerializer,
    ItemDetailSerializer,
    ItemSerializer,
    LayerComparisonSerializer,
    LayerCorrectionSerializer,
    LayerSerializer,
    LayerTypeSerializer,
    ReviewRecordSerializer,
    SegmentSerializer,
    ValidationIssueSerializer,
    ValidationRunSerializer,
)
from .services.ingest import IngestError, create_layer_with_segments
from .services.vocadito import ingest_vocadito
from .validators import run_for_corpus, run_for_item, run_for_layer


class DefaultPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 2000


class SegmentPagination(PageNumberPagination):
    """A layer can hold thousands of segments, and the frontend needs them all at once to
    draw the coverage rail, so the page size cap is loosened."""

    page_size = 500
    page_size_query_param = "page_size"
    max_page_size = 20000


class CorpusViewSet(viewsets.ModelViewSet):
    queryset = Corpus.objects.annotate(item_count=Count("items"))
    serializer_class = CorpusSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    @action(detail=True, methods=["post"], url_path="ingest-vocadito")
    def ingest_vocadito(self, request, pk=None):
        """
        Scan a vocadito tree under the corpus's root_path: create Items, read headers,
        import the A1 and A2 note annotations. Supports dry_run.

        Named for the dataset rather than offered as a generic "ingest" with a layout
        parameter. A layout table invites the caller to pick the wrong entry for their
        tree, and the importer then matches files that happen to fit while meaning
        something else. A dataset-specific endpoint cannot be given the wrong dataset.
        """
        corpus = self.get_object()
        try:
            report = ingest_vocadito(corpus, dry_run=bool(request.data.get("dry_run")))
        except (FileNotFoundError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(report.as_dict())

    @action(detail=True, methods=["post"])
    def validate(self, request, pk=None):
        try:
            params = validator_params(request.data.get("params"))
        except ParamError as exc:
            return Response({"params": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        run = run_for_corpus(self.get_object(), params=params or None, user=_user(request))
        return Response(ValidationRunSerializer(run).data, status=status.HTTP_201_CREATED)


class ItemViewSet(viewsets.ModelViewSet):
    serializer_class = ItemSerializer
    pagination_class = DefaultPagination
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = ItemSerializer.annotate(Item.objects.select_related("corpus"))
        p = self.request.query_params
        if corpus := p.get("corpus"):
            cond = Q(corpus__slug=corpus)
            if corpus.isdigit():
                cond |= Q(corpus_id=int(corpus))
            qs = qs.filter(cond)
        if p.get("in_metadata") == "false":
            qs = qs.filter(metadata_row__isnull=True)
        if p.get("in_metadata") == "true":
            qs = qs.filter(metadata_row__isnull=False)
        if p.get("has_open_issues") == "true":
            qs = qs.filter(issues__acknowledged=False).distinct()
        if p.get("unprobed") == "true":
            qs = qs.filter(sample_rate__isnull=True)
        if q := p.get("q"):
            qs = qs.filter(Q(song_id__icontains=q) | Q(display_name__icontains=q))
        return qs.order_by("song_id")

    def get_serializer_class(self):
        return ItemDetailSerializer if self.action == "retrieve" else ItemSerializer

    @action(detail=True, methods=["post"])
    def validate(self, request, pk=None):
        try:
            params = validator_params(request.data.get("params"))
        except ParamError as exc:
            return Response({"params": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        run = run_for_item(self.get_object(), params=params or None, user=_user(request))
        return Response(ValidationRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="issues")
    def issues(self, request, pk=None):
        qs = ValidationIssue.objects.filter(item=self.get_object())
        if request.query_params.get("open") == "true":
            qs = qs.filter(acknowledged=False)
        return Response(ValidationIssueSerializer(qs, many=True).data)

    @action(detail=True, methods=["post"], url_path="upload-annotation")
    def upload_annotation(self, request, pk=None):
        """
        Upload an annotation file (F0 CSV for now), parse it into structured data and open
        it as a new version of a layer. Always a new version, never an overwrite:
        "somebody changed it and nobody knows who" is the problem this system exists to
        solve.
        """
        item = self.get_object()
        form = F0UploadSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data

        try:
            parsed = parse_f0_csv(data["file"], contract=data["column_contract"])
        except F0ParseError as exc:
            # 400, not a partial import. The file parsed as numbers but contradicts the
            # column contract the uploader chose, so storing any of it would store data
            # read under the wrong mapping.
            return Response(
                {"detail": str(exc), "column_contract": data["column_contract"]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not parsed.rows:
            return Response(
                {"detail": "The file has no usable data rows", "skipped": parsed.skipped_lines[:20]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        params = {}
        if data.get("tuning_reference_hz"):
            params["tuning_reference_hz"] = data["tuning_reference_hz"]

        try:
            layer = create_layer_with_segments(
                item=item,
                layer_type=data["layer_type"],
                stream_key=data["stream_key"],
                rows=parsed.rows,
                author=_user(request),
                author_kind=AnnotationLayer.AuthorKind.HUMAN,
                params=params,
                parent=data.get("parent"),
                provenance={
                    "uploaded_filename": getattr(data["file"], "name", ""),
                    "note": data.get("note", ""),
                    "parser": "parse_f0_csv",
                    "column_contract": parsed.contract_key,
                    "column_map": parsed.column_map,
                    "parser_warnings": parsed.warnings,
                    "skipped_lines": len(parsed.skipped_lines),
                },
            )
        except IngestError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        run = run_for_layer(layer, user=_user(request))
        return Response(
            {
                "layer": LayerSerializer(
                    AnnotationLayer.objects.annotate(segment_count=Count("segments")).get(pk=layer.pk)
                ).data,
                "parser_warnings": parsed.warnings,
                "validation": ValidationRunSerializer(run).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="export-session")
    def export_session(self, request, pk=None):
        """A whole SV session (.sv). It opens on a double click, with no manual loading of
        the audio and no importing each layer by hand."""
        item = self.get_object()
        qs = item.layers.all()
        if ids := request.query_params.get("layers"):
            qs = qs.filter(id__in=[int(i) for i in ids.split(",") if i.strip().isdigit()])
        elif request.query_params.get("accepted_only") == "true":
            qs = qs.filter(status=AnnotationLayer.Status.ACCEPTED)
        else:
            qs = _latest_per_stream(qs)
        try:
            blob = item_to_session(qs)
        except ExportError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        resp = HttpResponse(blob, content_type="application/x-sonic-visualiser")
        resp["Content-Disposition"] = f'attachment; filename="{suggested_session_filename(item)}"'
        return resp


class LayerViewSet(viewsets.ModelViewSet):
    serializer_class = LayerSerializer
    pagination_class = DefaultPagination
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = AnnotationLayer.objects.select_related("item").annotate(
            segment_count=Count("segments")
        )
        p = self.request.query_params
        for field in ("item", "layer_type", "stream_key", "status"):
            if value := p.get(field):
                qs = qs.filter(**{field: value})
        if p.get("authoritative_only") == "true":
            qs = qs.filter(derived_from__isnull=True)
        return qs.order_by("item__song_id", "layer_type", "stream_key", "-version")

    def perform_create(self, serializer):
        serializer.save(author=_user(self.request))

    @action(detail=True, methods=["get"])
    def segments(self, request, pk=None):
        """
        ?resolved=true returns the snapshot with corrections applied — the version used for
        export and comparison. The default returns the original rows. Both have to be
        reachable, or there is no way to see what a correction actually did.
        """
        layer = self.get_object()
        if request.query_params.get("resolved") == "true":
            return Response(layer.resolved_segments())
        paginator = SegmentPagination()
        qs = layer.segments.all()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(SegmentSerializer(page, many=True).data)

    @action(detail=True, methods=["post"])
    def validate(self, request, pk=None):
        try:
            params = validator_params(request.data.get("params"))
        except ParamError as exc:
            return Response({"params": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        run = run_for_layer(self.get_object(), params=params or None, user=_user(request))
        return Response(ValidationRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"])
    def reviews(self, request, pk=None):
        layer = self.get_object()
        if request.method == "GET":
            return Response(ReviewRecordSerializer(layer.reviews.all(), many=True).data)
        ser = ReviewRecordSerializer(data={**request.data, "layer": layer.pk})
        ser.is_valid(raise_exception=True)
        review = ser.save(reviewer=_user(request))
        # The review outcome drives the layer status, so the two cannot disagree
        mapping = {
            ReviewRecord.Decision.ACCEPT: AnnotationLayer.Status.ACCEPTED,
            ReviewRecord.Decision.ACCEPT_WITH_NOTES: AnnotationLayer.Status.ACCEPTED,
            ReviewRecord.Decision.REQUEST_CHANGES: AnnotationLayer.Status.CHANGES_REQUESTED,
            ReviewRecord.Decision.REJECT: AnnotationLayer.Status.REJECTED,
        }
        layer.status = mapping[review.decision]
        layer.save(update_fields=["status", "updated_at"])
        return Response(ReviewRecordSerializer(review).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def lineage(self, request, pk=None):
        chain = self.get_object().lineage()
        return Response(LayerSerializer(chain, many=True, context={"request": request}).data)

    @action(detail=True, methods=["get"], url_path="export-svl")
    def export_svl(self, request, pk=None):
        layer = self.get_object()
        try:
            xml = layer_to_svl(layer)
        except ExportError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        resp = HttpResponse(xml, content_type="application/xml")
        resp["Content-Disposition"] = f'attachment; filename="{suggested_filename(layer)}"'
        return resp


class SegmentViewSet(viewsets.ModelViewSet):
    queryset = Segment.objects.select_related("layer")
    serializer_class = SegmentSerializer
    pagination_class = SegmentPagination
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        if layer := self.request.query_params.get("layer"):
            qs = qs.filter(layer_id=layer)
        return qs


class LayerCorrectionViewSet(viewsets.ModelViewSet):
    queryset = LayerCorrection.objects.select_related("layer")
    serializer_class = LayerCorrectionSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        if layer := self.request.query_params.get("layer"):
            qs = qs.filter(layer_id=layer)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=_user(self.request))


class ReviewViewSet(viewsets.ModelViewSet):
    queryset = ReviewRecord.objects.select_related("layer", "reviewer")
    serializer_class = ReviewRecordSerializer
    pagination_class = DefaultPagination
    permission_classes = [IsAuthenticatedOrReadOnly]

    def perform_create(self, serializer):
        serializer.save(reviewer=_user(self.request))


class ValidationRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ValidationRun.objects.annotate(issue_count=Count("issues"))
    serializer_class = ValidationRunSerializer
    pagination_class = DefaultPagination

    @action(detail=True, methods=["get"])
    def issues(self, request, pk=None):
        qs = self.get_object().issues.select_related("item", "layer")
        if severity := request.query_params.get("severity"):
            qs = qs.filter(severity=severity)
        if request.query_params.get("open") == "true":
            qs = qs.filter(acknowledged=False)
        return Response(ValidationIssueSerializer(qs, many=True).data)


class ValidationIssueViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ValidationIssueSerializer
    pagination_class = DefaultPagination

    def get_queryset(self):
        qs = ValidationIssue.objects.select_related("item", "layer")
        p = self.request.query_params
        for field in ("run", "item", "layer", "severity", "code"):
            if value := p.get(field):
                qs = qs.filter(**{field: value})
        if p.get("open") == "true":
            qs = qs.filter(acknowledged=False)
        return qs

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        """Mark as seen and judged acceptable. The issue is not deleted: deleting it would
        leave no record."""
        issue = self.get_object()
        issue.acknowledged = True
        issue.acknowledged_note = request.data.get("note", "")
        issue.save(update_fields=["acknowledged", "acknowledged_note"])
        return Response(ValidationIssueSerializer(issue).data)


class LayerComparisonViewSet(viewsets.ModelViewSet):
    queryset = LayerComparison.objects.select_related("layer_a", "layer_b")
    serializer_class = LayerComparisonSerializer
    pagination_class = DefaultPagination
    permission_classes = [IsAuthenticatedOrReadOnly]

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        a = ser.validated_data["layer_a"]
        b = ser.validated_data["layer_b"]
        if a.layer_type != b.layer_type:
            return Response(
                {"detail": f"Cannot compare different annotation types: {a.layer_type} vs {b.layer_type}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        comparator = comparator_for(a.layer_type)
        if comparator is None:
            return Response(
                {"detail": f"No comparator is registered for {a.layer_type}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            supplied = comparator_params(ser.validated_data.get("params"), comparator)
        except ParamError as exc:
            return Response({"params": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        params = {**comparator.default_params, **supplied}
        try:
            result = comparator.fn(a, b, params)
        except (ValueError, TypeError) as exc:
            # The boundary above checks every knob the comparator declares, so reaching
            # here means a comparator refused something that check does not cover yet.
            # Still the client's input, so still a 400 — a 500 would blame the server for
            # a value the caller chose.
            return Response(
                {"params": [f"This comparison cannot be run with those parameters: {exc}"]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        obj = LayerComparison.objects.create(
            layer_a=a,
            layer_b=b,
            comparator_key=comparator.key,
            params=params,  # store the tolerances, or nobody can later tell what standard
            # this difference was computed to
            summary=result["summary"],
            discrepancies=result["discrepancies"],
            created_by=_user(request),
        )
        return Response(self.get_serializer(obj).data, status=status.HTTP_201_CREATED)


class AdjudicationViewSet(viewsets.ModelViewSet):
    queryset = Adjudication.objects.select_related("item")
    serializer_class = AdjudicationSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]


@api_view(["GET"])
@permission_classes([])
def layer_types(request):
    """The frontend's annotation-type table reads the registry directly rather than
    hard-coding it."""
    return Response(LayerTypeSerializer.all())


@api_view(["GET"])
@permission_classes([])
def comparator_defaults(request):
    return Response({"note_pitch": NOTE_PITCH_DEFAULTS, "sound_event": SED_DEFAULTS})


def _user(request):
    u = getattr(request, "user", None)
    return u if (u and u.is_authenticated) else None


def _latest_per_stream(qs):
    seen, keep = set(), []
    for layer in qs.order_by("layer_type", "stream_key", "-version"):
        key = (layer.layer_type, layer.stream_key)
        if key in seen:
            continue
        seen.add(key)
        keep.append(layer)
    return keep
