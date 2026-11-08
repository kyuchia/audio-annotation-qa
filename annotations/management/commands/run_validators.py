from django.core.management.base import BaseCommand, CommandError

from annotations.models import AnnotationLayer, Corpus, Item
from annotations.validators import run_for_target


class Command(BaseCommand):
    help = "Run the validators over a corpus, item or layer; results are written to a ValidationRun."

    def add_arguments(self, parser):
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument("--corpus", help="corpus slug")
        g.add_argument("--item", help="song_id")
        g.add_argument("--layer", type=int, help="layer id")
        parser.add_argument("--show", action="store_true", help="print every issue found")

    def handle(self, *args, **o):
        if o["corpus"]:
            target = Corpus.objects.filter(slug=o["corpus"]).first()
        elif o["item"]:
            target = Item.objects.filter(song_id=o["item"]).first()
        else:
            target = AnnotationLayer.objects.filter(pk=o["layer"]).first()
        if target is None:
            raise CommandError("No such target")

        run = run_for_target(target)
        self.stdout.write(self.style.SUCCESS(f"Validation run #{run.pk}: {run.summary}"))
        if not o["show"]:
            return
        styles = {"error": self.style.ERROR, "warning": self.style.WARNING, "info": lambda s: s}
        for issue in run.issues.select_related("item", "layer").order_by("severity", "code"):
            where = issue.layer or issue.item or issue.corpus
            self.stdout.write(styles[issue.severity](f"[{issue.code}] {where} — {issue.message}"))
