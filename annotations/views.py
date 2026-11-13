from __future__ import annotations

from django.db.models import Count, Exists, OuterRef, Q
from django.http import HttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from rest_framework.response import Response

from .comparators.pitch_interval import NOTE_PITCH_DEFAULTS, SED_DEFAULTS
from .exporters import ExportError, item_to_session, layer_to_svl, suggested_filename, suggested_session_filename
from .importers import parse_f0_csv
from .importers.f0_csv import F0ParseError
from .params import ParamError, validator_params
from .models import (
    NEEDS_ATTENTION,
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
from .services.compare import ComparisonError, run_comparison
from .serializers import (
    item_issues,
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
        if corpus := _text_param(p, "corpus"):
            cond = Q(corpus__slug=corpus)
            if _is_id(corpus):
                cond |= Q(corpus_id=int(corpus))
            qs = qs.filter(cond)
        if p.get("in_metadata") == "false":
            qs = qs.filter(metadata_row__isnull=True)
        if p.get("in_metadata") == "true":
            qs = qs.filter(metadata_row__isnull=False)
        if p.get("has_open_issues") == "true":
            # Exists over the current findings: a join would match an item on a finding
            # that a later run has already superseded. The severities are the status
            # column's, so the filter selects exactly the rows that column marks.
            qs = qs.filter(Exists(item_issues(OuterRef("pk"), NEEDS_ATTENTION)))
        if p.get("unprobed") == "true":
            qs = qs.filter(sample_rate__isnull=True)
        if q := _text_param(p, "q"):
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
        """
        What is wrong with this item now, which is the only question this endpoint is
        asked. Older runs stay in the database and stay readable one run at a time, at
        /api/validation-runs/<id>/issues/.
        """
        item = self.get_object()
        qs = ValidationIssue.objects.filter(Q(item=item) | Q(layer__item=item)).current()
        if request.query_params.get("open") == "true":
            qs = qs.unacknowledged()
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
            qs = qs.filter(id__in=_id_list_param(request.query_params, "layers"))
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
        if (item := _id_param(p, "item")) is not None:
            qs = qs.filter(item=item)
        for field in ("layer_type", "stream_key", "status"):
            if value := _text_param(p, field):
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
        # Saving the review writes the layer's status with it, in one transaction
        # (ReviewRecord.save), so the two cannot disagree.
        review = ser.save(reviewer=_user(request))
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


class SegmentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Reads only. Stored segments are the file as it was imported and are never modified in
    place: a change arrives as a new layer version (upload-annotation) or as a correction
    laid over the layer.
    """

    queryset = Segment.objects.select_related("layer")
    serializer_class = SegmentSerializer
    pagination_class = SegmentPagination

    def get_queryset(self):
        qs = super().get_queryset()
        if (layer := _id_param(self.request.query_params, "layer")) is not None:
            qs = qs.filter(layer_id=layer)
        return qs


class LayerCorrectionViewSet(viewsets.ModelViewSet):
    queryset = LayerCorrection.objects.select_related("layer")
    serializer_class = LayerCorrectionSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        qs = super().get_queryset()
        if (layer := _id_param(self.request.query_params, "layer")) is not None:
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
        if severity := _text_param(request.query_params, "severity"):
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
        for field in ("run", "item", "layer"):
            if (value := _id_param(p, field)) is not None:
                qs = qs.filter(**{field: value})
        for field in ("severity", "code"):
            if value := _text_param(p, field):
                qs = qs.filter(**{field: value})
        if p.get("open") == "true":
            qs = qs.unacknowledged()
        # Naming a run is asking for history, and history is exactly what the rows of a
        # superseded run are. Every other question is about the data as it stands.
        if not p.get("run"):
            qs = qs.current()
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

    def get_queryset(self):
        """
        ?item= and ?layer= ask which comparisons were run on something, which is how a
        page showing one item finds the comparison already stored for it. Either side of
        the pair counts: A against B and B against A are the same question asked once.
        """
        qs = self.queryset
        p = self.request.query_params
        for field, sides in (
            ("item", ("layer_a__item", "layer_b__item")),
            ("layer", ("layer_a", "layer_b")),
        ):
            value = p.get(field)
            if not value:
                continue
            if not _is_id(value):
                return qs.none()
            qs = qs.filter(Q(**{sides[0]: value}) | Q(**{sides[1]: value})).distinct()
        return qs

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            obj = run_comparison(
                ser.validated_data["layer_a"],
                ser.validated_data["layer_b"],
                ser.validated_data.get("params"),
                created_by=_user(request),
            )
        except ComparisonError as exc:
            # Every refusal the service raises is about what the caller asked for, so it
            # is a 400 — a 500 would blame the server for a value the caller chose.
            return Response(exc.as_errors(), status=status.HTTP_400_BAD_REQUEST)
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


@api_view(["GET"])
@permission_classes([])
def me(request):
    """
    Who is asking, as the session sees them.

    Open to everyone and answered for everyone: a visitor with no session is a fact about
    this request, so it is a 200 with the fact in it. Refusing the question would leave the
    frontend to discover the answer by attempting a write and reading the refusal, which is
    how a reader ends up pressing a button that was never going to work.

    This reports the session. It grants nothing: every endpoint keeps its own permission,
    and a request that lies about this gets exactly as far as it did before.
    """
    user = getattr(request, "user", None)
    signed_in = bool(user and user.is_authenticated)
    return Response(
        {"authenticated": signed_in, "username": user.get_username() if signed_in else None}
    )


def _is_id(raw: str) -> bool:
    """Whether a query value is a row id. ASCII digits only: str.isdigit() also accepts
    characters such as a superscript two, which int() and the database then refuse."""
    return raw.isascii() and raw.isdigit()


def _shown(raw: str) -> str:
    text = repr(raw)
    return text if len(text) <= 40 else f"{text[:37]}..."


def _id_param(params, name: str) -> int | None:
    """
    A query parameter that filters by a row id, or None when the request leaves it out.

    Anything else is a 400 that names the parameter. Handing the raw string to the ORM
    raises inside the query, and the visitor is then told the server failed over a value
    they typed.
    """
    raw = params.get(name)
    if not raw:
        return None
    if not _is_id(raw):
        raise ValidationError(
            {name: [f"{name} takes a whole number id, such as {name}=4; got {_shown(raw)}."]}
        )
    return int(raw)


def _id_list_param(params, name: str) -> list[int]:
    """A comma-separated list of row ids, checked the same way."""
    raw = params.get(name) or ""
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if not parts or any(not _is_id(part) for part in parts):
        raise ValidationError(
            {name: [f"{name} takes ids separated by commas, such as {name}=7,8; got {_shown(raw)}."]}
        )
    return [int(part) for part in parts]


def _text_param(params, name: str) -> str | None:
    """A query parameter matched as text. The database stores text without NUL characters
    and refuses a query holding one, so that single case is answered here."""
    raw = params.get(name)
    if raw and "\x00" in raw:
        raise ValidationError({name: [f"{name} takes plain text."]})
    return raw


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
