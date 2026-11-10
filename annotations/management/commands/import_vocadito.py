"""
python manage.py import_vocadito \
    --slug vocadito-demo \
    --name "vocadito (demo subset)" \
    --root /data/vocadito \
    --validate

--root points at a vocadito tree: Audio/, Annotations/F0/, Annotations/Notes/,
Annotations/Lyrics/ and vocadito_metadata.csv. The demo subset under demo-data/vocadito/
has the same shape as the full release, so the same command imports either.

Both note annotations are imported, under the stream keys A1 and A2. They are parallel and
independent: neither becomes the parent of the other, each is version 1, and neither is
marked superseded. Frame-level F0 is not imported — services/vocadito.py explains why, and
the command repeats the reason in its output rather than leaving the files silently skipped.

Each excerpt annotated by both streams also gets its default comparison stored, through
the same service the API uses, so an imported corpus can be read without anyone first
pressing a button. Importing again finds those comparisons and leaves them as they are.

No tuning reference is passed. vocadito publishes none, and inventing a global one would
make the cents comparison wrong for every excerpt that does not happen to match it; the
comparison falls back to A4=440 for both layers, which cancels out when the two layers
being compared share it.
"""
from django.core.management.base import BaseCommand, CommandError

from annotations.models import Corpus
from annotations.services.compare import store_default_comparisons
from annotations.services.vocadito import METADATA_CSV, ingest_vocadito
from annotations.validators import run_for_corpus

#: Validators switched off for a vocadito corpus by default.
#:
#: recording_linked asks whether an item points at its original recording rather than at a
#: stand-in file. vocadito ships the recordings themselves, so the question has no
#: content here and the check would report the same non-finding once per excerpt — 40
#: identical info-level issues that bury the ones worth reading. Listed explicitly on the
#: corpus, so the silence is a recorded decision and `--keep-validators` undoes it.
DEFAULT_SKIP_VALIDATORS = ["recording_linked"]


class Command(BaseCommand):
    help = (
        "Import a vocadito tree: one Item per excerpt, with the A1 and A2 note annotations "
        "as parallel independent layers."
    )

    def add_arguments(self, parser):
        parser.add_argument("--slug", required=True)
        parser.add_argument("--name", default="")
        parser.add_argument("--root", required=True, help="a vocadito dataset root")
        parser.add_argument(
            "--metadata-csv",
            default="",
            help=f"defaults to {METADATA_CSV} at the dataset root",
        )
        parser.add_argument(
            "--skip-validators",
            default="",
            help="comma-separated validator keys to switch off for this corpus; defaults to "
            + ",".join(DEFAULT_SKIP_VALIDATORS),
        )
        parser.add_argument(
            "--keep-validators",
            action="store_true",
            help="run every validator, including the ones this corpus would skip by default",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--validate", action="store_true", help="run the validators immediately after import"
        )

    def handle(self, *args, **o):
        if o["keep_validators"]:
            skip = []
        elif o["skip_validators"]:
            skip = [k.strip() for k in o["skip_validators"].split(",") if k.strip()]
        else:
            skip = list(DEFAULT_SKIP_VALIDATORS)

        corpus, created = Corpus.objects.update_or_create(
            slug=o["slug"],
            defaults={
                "name": o["name"] or o["slug"],
                "root_path": o["root"],
                "metadata_csv_path": o["metadata_csv"],
                "skip_validators": skip,
            },
        )
        self.stdout.write(f"{'Created' if created else 'Updated'} corpus: {corpus}")
        if skip:
            self.stdout.write(f"  validators skipped for this corpus: {', '.join(skip)}")

        try:
            report = ingest_vocadito(corpus, dry_run=o["dry_run"])
        except (FileNotFoundError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"{report.tracks_seen} excerpts: {report.items_created} items created, "
                f"{report.items_updated} updated, {report.layers_created} layers, "
                f"{report.segments_created} segments"
            )
        )
        if report.layers_unchanged:
            self.stdout.write(
                f"  {report.layers_unchanged} layers already imported from an identical file, "
                f"so no new version was created"
            )
        self.stdout.write(
            f"{report.metadata_rows} metadata rows; "
            f"{len(report.tracks_without_metadata)} excerpts with no metadata row; "
            f"{len(report.unmatched_metadata_rows)} metadata rows with no audio"
        )
        self.stdout.write(
            f"{report.f0_files_present} frame-level F0 files and "
            f"{report.lyrics_files_present} lyrics files found, neither imported."
        )
        self.stdout.write(f"  {report.frame_f0}")
        self.stdout.write(
            "  Lyrics are plain text with one line per musical phrase; there is no "
            "registered layer type for them, and inventing one here would be a modelling "
            "decision rather than an import detail."
        )

        for w in report.warnings:
            self.stdout.write(self.style.WARNING(f"  ! {w}"))
        for f in report.probe_failures:
            self.stdout.write(self.style.ERROR(f"  x could not read the header: {f}"))
        for e in report.errors:
            self.stdout.write(self.style.ERROR(f"  x {e}"))

        if o["dry_run"]:
            self.stdout.write(self.style.WARNING("dry run: rolled back, the database is unchanged"))
            return

        comparisons = store_default_comparisons(corpus)
        self.stdout.write(
            self.style.SUCCESS(
                f"{comparisons.created} default comparisons stored"
                + (f", {comparisons.kept} already present" if comparisons.kept else "")
            )
        )
        for reason in comparisons.skipped:
            self.stdout.write(self.style.WARNING(f"  ! comparison skipped — {reason}"))

        if o["validate"]:
            run = run_for_corpus(corpus)
            self.stdout.write(
                self.style.SUCCESS(f"Validation run #{run.pk}: {run.summary.get('by_severity')}")
            )
