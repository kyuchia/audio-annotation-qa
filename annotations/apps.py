from django.apps import AppConfig


class AnnotationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "annotations"
    verbose_name = "Audio annotation management"

    def ready(self):
        # Importing is registering: layer types, validators and comparators all enter
        # the registry here.
        from . import layer_types  # noqa: F401
        from . import validators  # noqa: F401
        from . import comparators  # noqa: F401
