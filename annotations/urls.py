from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("corpora", views.CorpusViewSet, basename="corpus")
router.register("items", views.ItemViewSet, basename="item")
router.register("layers", views.LayerViewSet, basename="layer")
router.register("segments", views.SegmentViewSet, basename="segment")
router.register("corrections", views.LayerCorrectionViewSet, basename="correction")
router.register("reviews", views.ReviewViewSet, basename="review")
router.register("validation-runs", views.ValidationRunViewSet, basename="validationrun")
router.register("validation-issues", views.ValidationIssueViewSet, basename="validationissue")
router.register("comparisons", views.LayerComparisonViewSet, basename="comparison")
router.register("adjudications", views.AdjudicationViewSet, basename="adjudication")

urlpatterns = [
    path("", include(router.urls)),
    path("me/", views.me, name="me"),
    path("layer-types/", views.layer_types, name="layer-types"),
    path("comparator-defaults/", views.comparator_defaults, name="comparator-defaults"),
]
