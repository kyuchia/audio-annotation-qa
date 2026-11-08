"""
python manage.py link_recordings --corpus my-corpus \\
    --root /recordings --host-root /Users/you/project/recordings

Links each Item to the recording its annotations were made against, so that an exported
session points at that audio rather than at a stand-in file shipped beside the annotations.

Two roots, because two different processes are involved and they do not see the same
filesystem. This command checks that a file exists, from wherever it happens to be running
— inside a container that is /recordings. Sonic Visualiser opens that file later, on the
host, where /recordings does not exist. So "where to look" and "what to store" are separate
inputs: existence is checked under --root, and --host-root is what gets written down.

Getting that wrong is not a loud failure. A container path in recording_path produces
sessions that fail on every machine including the one that wrote them, and SV reports it as
a missing file rather than a wrong path — which sends you looking at the drive instead of
at the path.

Each path is resolved once, checked, and stored. An item whose file is not found keeps a
blank recording_path and is reported by name: a wrong path stored silently is worse than no
path at all, because everything downstream would treat it as verified.

KNOWN LIMITATION — one directory per item.
    This command looks for <root>/<last component of the item's disk_path>/<filename>, so it
    assumes the recordings are laid out one directory per item, named as the annotations are.
    That does not fit a corpus whose audio lives in a single shared directory with the item
    distinguished by filename: every item there has the same disk_path, so every candidate
    path collapses onto one.

    Such a corpus is normally one whose own audio already *is* the recording, in which case
    there is nothing to link and both this command and the recording_linked validator have
    no work to do — switch the validator off with Corpus.skip_validators. A flat recording
    tree that genuinely needs linking is not supported yet; it wants a per-corpus rule for
    turning an item into a candidate path, rather than this one hardcoded assumption.

Nothing outside the database is written, and the recordings themselves are only ever opened
to read their header.
"""
from pathlib import Path, PurePosixPath

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from annotations.importers.audio_header import AudioProbeError, probe_audio
from annotations.models import Corpus

#: How far the recording's own duration may sit from the length metadata claims before it
#: is worth reporting. Same 1s slack metadata_duration_agreement uses.
DURATION_TOLERANCE_SEC = 1.0


class Command(BaseCommand):
    help = (
        "Find each item's original recording, verify it exists, and store the path and the "
        "recording's own specs on the item."
    )

    def add_arguments(self, parser):
        parser.add_argument("--corpus", required=True, help="corpus slug")
        parser.add_argument(
            "--root",
            default="",
            help="where to look for the recordings, as seen by this process; inside a "
            "container that is typically /recordings",
        )
        parser.add_argument(
            "--host-root",
            default="",
            help="what to store, as seen by the machine that opens the exported session. "
            "Defaults to --root. Pass it whenever this process does not share a "
            "filesystem with that machine: a path that only resolves here produces "
            "sessions Sonic Visualiser cannot open anywhere.",
        )
        parser.add_argument(
            "--filename",
            default="",
            help="recording filename inside each item's directory (defaults to the corpus's "
            "own recording_filename; required when that is blank)",
        )
        parser.add_argument(
            "--relink",
            action="store_true",
            help="re-check items that already have a recording_path; without this they are skipped",
        )
        parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")

    def handle(self, *args, **o):
        corpus = Corpus.objects.filter(slug=o["corpus"]).first()
        if corpus is None:
            raise CommandError(f"No corpus with slug {o['corpus']}")

        dry_run = o["dry_run"]
        host_root_given = bool(o["host_root"])

        corpus_fields = []
        if o["root"]:
            # --root alone sets both: with no --host-root the caller is asserting that this
            # process and the reader see the same path.
            corpus.recording_lookup_root = o["root"]
            corpus.recording_root = o["host_root"] or o["root"]
            corpus_fields += ["recording_lookup_root", "recording_root"]
        elif o["host_root"]:
            corpus.recording_root = o["host_root"]
            corpus_fields.append("recording_root")
        if o["filename"]:
            corpus.recording_filename = o["filename"]
            corpus_fields.append("recording_filename")
        if corpus_fields and not dry_run:
            corpus.save(update_fields=[*set(corpus_fields), "updated_at"])

        # A re-run with no flags still has to know where to look, which is what
        # recording_lookup_root is for; it falls back to the host root when they are the same.
        lookup_root = corpus.recording_lookup_root or corpus.recording_root
        host_root = corpus.recording_root or corpus.recording_lookup_root
        if not lookup_root:
            raise CommandError(
                "This corpus has no recording root. Pass --root to set one, e.g. "
                "--root /recordings"
            )

        filename = corpus.recording_filename
        if not filename:
            # Deliberately not defaulted. A guessed filename turns "nobody configured this"
            # into "every file is missing", which sends the reader to the filesystem rather
            # than to the one setting that is actually blank.
            raise CommandError(
                "This corpus has no recording filename. Pass --filename to set one, e.g. "
                "--filename audio.wav"
            )
        root = Path(lookup_root)
        if not root.is_dir():
            self.stdout.write(
                self.style.WARNING(f"lookup root does not exist or is not a directory: {root}")
            )

        linked = already = missing = 0
        misses: list[str] = []
        mismatches: list[str] = []

        for item in corpus.items.order_by("song_id"):
            if item.recording_path and not o["relink"]:
                already += 1
                continue

            # Only the last component of disk_path names the item's own directory. The rest
            # of disk_path locates it under the corpus root, which is a different tree from
            # the recording root and has no counterpart there.
            song_dir = Path(item.disk_path).name
            candidate = root / song_dir / filename
            # The stored path is built from the host root, and with POSIX separators: it
            # describes a path on another machine, so this machine's os.sep is irrelevant.
            stored = str(PurePosixPath(host_root.rstrip("/")) / song_dir / filename)

            if not candidate.is_file():
                missing += 1
                misses.append(f"{item.song_id}: tried {candidate}")
                if item.recording_path and not dry_run:
                    # A relink that no longer finds the file must clear the stored path and
                    # its specs. Leaving them would keep asserting facts that are no longer
                    # true.
                    item.recording_path = ""
                    item.recording_duration_sec = None
                    item.recording_sample_rate = None
                    item.recording_channels = None
                    item.recording_linked_at = timezone.now()
                    item.save(
                        update_fields=[
                            "recording_path",
                            "recording_duration_sec",
                            "recording_sample_rate",
                            "recording_channels",
                            "recording_linked_at",
                            "updated_at",
                        ]
                    )
                continue

            try:
                info = probe_audio(candidate)
            except AudioProbeError as exc:
                missing += 1
                misses.append(f"{item.song_id}: found {candidate} but could not read it — {exc}")
                continue

            # The recording's specs go in their own fields. They are deliberately NOT written
            # to sample_rate / duration_sec / channels: those describe the file the
            # annotations were imported alongside, and coverage_duration() reasons about
            # exactly that pairing. A recording longer than the file the annotations were
            # imported alongside would silently move every coverage figure if it overwrote
            # them.
            claimed = item.reference_duration_sec
            if claimed and abs(info.duration_sec - claimed) > DURATION_TOLERANCE_SEC:
                mismatches.append(
                    f"{item.song_id}: recording is {info.duration_sec:.2f}s but metadata gives "
                    f"the recording as {claimed:.2f}s "
                    f"(differs by {abs(info.duration_sec - claimed):.2f}s)"
                )

            linked += 1
            if not dry_run:
                item.recording_path = stored
                item.recording_duration_sec = info.duration_sec
                item.recording_sample_rate = info.sample_rate
                item.recording_channels = info.channels
                item.recording_linked_at = timezone.now()
                item.save(
                    update_fields=[
                        "recording_path",
                        "recording_duration_sec",
                        "recording_sample_rate",
                        "recording_channels",
                        "recording_linked_at",
                        "updated_at",
                    ]
                )

        total = corpus.items.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"{linked} linked, {already} already linked, {missing} missing, {total} total"
            )
        )
        self.stdout.write(f"looked under {root} · stored as {host_root} · filename {filename}")

        if linked and not host_root_given and host_root == lookup_root:
            example = str(PurePosixPath(host_root.rstrip("/")) / "<song>" / filename)
            self.stdout.write(
                self.style.WARNING(
                    f"  ! --host-root was not given, so paths are stored exactly as looked up: "
                    f"{example}. That path must resolve on the machine that opens the exported "
                    f"session, not just on this one. If this process is containerised it "
                    f"almost certainly does not, and Sonic Visualiser will report the file as "
                    f"missing rather than as wrongly addressed. Re-run with --relink "
                    f"--host-root <path on that machine> to correct it."
                )
            )

        for m in misses:
            self.stdout.write(self.style.WARNING(f"  ! no recording — {m}"))
        for m in mismatches:
            self.stdout.write(
                self.style.WARNING(
                    f"  ! duration disagrees — {m}. The linked file may not be the recording "
                    f"these annotations were aligned to."
                )
            )
        if dry_run:
            self.stdout.write(self.style.WARNING("dry run: nothing was written"))
