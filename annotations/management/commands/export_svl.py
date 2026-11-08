from pathlib import Path, PurePosixPath

from django.core.management.base import BaseCommand, CommandError

from annotations.exporters import (
    ExportError,
    file_uri,
    item_to_session,
    layer_to_svl,
    sort_layers_by_stream,
    suggested_filename,
    suggested_session_filename,
)
from annotations.models import AnnotationLayer, Item


class Command(BaseCommand):
    help = (
        "Export Sonic Visualiser files. With --session, write one .sv for the whole item; "
        "otherwise write one .svl per layer."
    )

    def add_arguments(self, parser):
        parser.add_argument("--item", help="song_id")
        parser.add_argument("--layer", type=int, help="layer id")
        parser.add_argument("--out", default=".", help="output directory")
        parser.add_argument(
            "--session", action="store_true", help="write a .sv session that opens on a double click"
        )
        parser.add_argument("--accepted-only", action="store_true")
        parser.add_argument(
            "--recording-root",
            default="",
            help=(
                "One-off override for where the original recordings live, used for this export "
                "only and never written to the corpus. Use link_recordings --root to set it "
                "persistently. Takes effect only when the item has no linked recording; "
                "--audio-root outranks it."
            ),
        )
        parser.add_argument(
            "--filename",
            default="",
            help=(
                "Recording filename inside each item's directory under --recording-root. "
                "Defaults to the corpus's own recording_filename; required when that is blank."
            ),
        )
        parser.add_argument(
            "--audio-root",
            default="",
            help=(
                "Directory to use in place of the corpus root when writing the audio path into "
                "a .sv session. The path must be valid on the machine that opens the file: the "
                "corpus root is normally the importer's own view, which inside a container is "
                "something like /data and does not exist on a reviewer's machine. Defaults to "
                "the corpus root, i.e. the previous behaviour."
            ),
        )

    def handle(self, *args, **o):
        out = Path(o["out"])
        out.mkdir(parents=True, exist_ok=True)
        try:
            if o["layer"]:
                layer = AnnotationLayer.objects.get(pk=o["layer"])
                path = out / suggested_filename(layer)
                path.write_text(layer_to_svl(layer), encoding="utf-8")
                self.stdout.write(self.style.SUCCESS(f"wrote {path}"))
                return
            if not o["item"]:
                raise CommandError("Specify --item or --layer")
            item = Item.objects.get(song_id=o["item"])
            layers = item.layers.all()
            if o["accepted_only"]:
                layers = layers.filter(status=AnnotationLayer.Status.ACCEPTED)
            if o["session"]:
                path = out / suggested_session_filename(item)
                # --recording-root is a one-off stand-in for a linked recording, so it
                # only applies when the item has none. --audio-root outranks both, matching
                # item_to_session()'s own precedence.
                audio_uri = None
                if not o["audio_root"] and o["recording_root"] and not item.recording_path:
                    # No fallback filename. Composing a path from a blank name would produce
                    # one that names a directory while looking like a file, and the session
                    # would fail to open with nothing to say the filename was never set.
                    filename = o["filename"] or item.corpus.recording_filename
                    if not filename:
                        raise CommandError(
                            "--recording-root needs a recording filename: pass --filename, or "
                            f"set recording_filename on the corpus {item.corpus.slug}."
                        )
                    root = o["recording_root"].rstrip("/")
                    audio_uri = file_uri(f"{root}/{PurePosixPath(item.disk_path).name}/{filename}")
                path.write_bytes(
                    item_to_session(
                        layers,
                        audio_uri=audio_uri,
                        audio_uri_base=o["audio_root"] or None,
                    )
                )
                self.stdout.write(self.style.SUCCESS(f"wrote {path}"))
                return
            for layer in sort_layers_by_stream(layers):
                path = out / suggested_filename(layer)
                path.write_text(layer_to_svl(layer), encoding="utf-8")
                self.stdout.write(f"wrote {path}")
        except (Item.DoesNotExist, AnnotationLayer.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        except ExportError as exc:
            raise CommandError(str(exc)) from exc
