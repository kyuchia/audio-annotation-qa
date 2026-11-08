"""
Sonic Visualiser export.

**Export only, no parser.** Sonic Visualiser is used here to open an annotation and look
at how it sits against the audio, not as a source of annotation. That keeps the direction
of truth unambiguous: the data lives in this system, and SV is a viewer onto it.

Two products:
  · .svl  a single layer, brought in with File -> Import Annotation Layer
  · .sv   a whole session (audio plus every layer), which opens on a double click and
          saves the manual work of loading the audio and importing each layer. This is
          what "open it and look at it in one step" means.

Frame indices appear only here. They are computed with that item's own sample_rate, and
with no sample_rate the export refuses rather than guessing or falling back to a default.

Two details that matter for whether SV can actually open the result:

  · The audio model carries mainModel="true". SV distinguishes the session's main audio
    model from a merely imported one by that attribute; without it the session opens with
    no main model, and so with no audio and no duration.
  · Ids are handed out from one counter shared by models, datasets and display layers, so
    no two elements anywhere in the file share an id. This is what SV's own writer does.
  · The session is compressed with bzip2, not gzip. This was verified by opening a real
    recording in SV, importing a MIDI file as a note layer, saving the session, and running
    file(1) on the result: "bzip2 compressed data, block size = 900k". A gzipped file is not
    rejected with a format error — SV fails to decompress it, falls back to reading the
    bytes as a plain-text annotation layer, and reports "Unable to load layer data ...
    without an audio file", which points at the wrong thing entirely.
"""
from __future__ import annotations

import bz2
import itertools
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

SV_DOCTYPE = "<!DOCTYPE sonic-visualiser>"

#: Display colours, assigned by position once the layers are sorted. Sonic Visualiser takes
#: a colour name, so the palette is its own; a session with more layers than colours wraps
#: round rather than leaving one unstyled.
_COLOUR_CYCLE = ("Purple", "Green", "Orange", "Blue", "Red", "Black")


class ExportError(RuntimeError):
    pass


def sort_layers_by_stream(layers):
    """Order layers by stream_key, then by layer type and version.

    Done here rather than by changing the model's Meta.ordering: display order is a
    presentation concern for this exporter, not a property of how layers are stored.

    Sorting on the key itself, with no table of preferred names: a corpus-specific running
    order can only be right for the corpus it was written for, and on every other one it
    degrades to "alphabetical, except for four names it has never seen" while still looking
    deliberate. Alphabetical throughout is at least the same rule everywhere, and it puts
    A1 before A2.
    """
    return sorted(
        layers,
        key=lambda l: ((l.stream_key or "").lower(), l.layer_type or "", l.version),
    )


def file_uri(path: str) -> str:
    """Build a file:// URI, percent-encoding the path but leaving the separators alone.

    Audio paths routinely contain spaces, parentheses and non-ASCII characters, and SV
    parses this attribute as a URL. XML-escaping alone is not enough: the value has to be
    a valid URI as well as valid XML, and the two escape different characters.
    """
    return "file://" + quote(path, safe="/")


def _require_sample_rate(item) -> int:
    if not item.sample_rate:
        raise ExportError(
            f"{item.song_id} has no sample rate (the audio header has not been read), so frame "
            f"indices cannot be computed."
        )
    return int(item.sample_rate)


def _points(layer, sample_rate: int) -> list[dict]:
    rows = layer.resolved_segments()
    pts = []
    for r in rows:
        hz = (r.get("payload") or {}).get("hz")
        label = (r.get("payload") or {}).get("label") or ""
        frame = int(round(r["start_sec"] * sample_rate))
        dur = max(1, int(round((r["end_sec"] - r["start_sec"]) * sample_rate)))
        pts.append(
            {
                "frame": frame,
                "duration": dur,
                "value": float(hz) if isinstance(hz, (int, float)) else 0.0,
                "label": str(label),
            }
        )
    return pts


def _model_kind(layer) -> tuple[str, str, str]:
    """Returns (model subtype, layer type, units)."""
    if layer.layer_type == "note_pitch":
        return "note", "notes", "Hz"
    return "region", "regions", ""


def layer_to_svl(layer, *, model_id: int = 1, dataset_id: int = 0) -> str:
    """A single layer as .svl (uncompressed XML)."""
    item = layer.item
    sr = _require_sample_rate(item)
    subtype, _, units = _model_kind(layer)
    pts = _points(layer, sr)

    if model_id == dataset_id:
        raise ExportError("model_id and dataset_id must differ; SV ids are unique per file")

    name = f"{layer.layer_type}/{layer.stream_key or 'default'} v{layer.version}"
    values = [p["value"] for p in pts if p["value"] > 0]
    vmin = min(values) if values else 0.0
    vmax = max(values) if values else 0.0
    start = pts[0]["frame"] if pts else 0
    end = max((p["frame"] + p["duration"] for p in pts), default=0)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        SV_DOCTYPE,
        "<sv>",
        "  <data>",
        (
            f'    <model id="{model_id}" name={quoteattr(name)} sampleRate="{sr}" '
            f'start="{start}" end="{end}" type="sparse" dimensions="3" resolution="1" '
            f'notifyOnAdd="true" dataset="{dataset_id}" subtype="{subtype}" '
            f'valueQuantization="0" minimum="{vmin:.6f}" maximum="{vmax:.6f}" '
            f'units={quoteattr(units)}/>'
        ),
        f'    <dataset id="{dataset_id}" dimensions="3">',
    ]
    for p in pts:
        lines.append(
            f'      <point frame="{p["frame"]}" value="{p["value"]:.6f}" '
            f'duration="{p["duration"]}" level="1" label={quoteattr(p["label"])}/>'
        )
    lines += ["    </dataset>", "  </data>", "</sv>", ""]
    return "\n".join(lines)


#: Appended to the audio model's name when the session points at a stand-in file
#: rather than the original recording. SV shows the model name in its layer list, so this
#: is visible on opening the session — rather than being something the reviewer works out
#: several minutes in, once the onsets sound wrong.
STAND_IN_MARKER = " [stand-in audio]"


def _session_audio(item, audio_uri, audio_uri_base) -> tuple[str, str]:
    """Resolve the session's audio URI. Returns (uri, kind).

    kind is "recording" when the URI names the linked original recording, "proxy" when it
    names a stand-in file, and "override" when the caller supplied the location.

    Precedence, most specific first:
      1. audio_uri        — the caller stated the whole URI; used verbatim
      2. audio_uri_base   — the caller stated a root; the item's own layout hangs off it
      3. recording_path   — a path verified to exist when link_recordings wrote it
      4. the audio beside the annotations, which for some releases is a stand-in

    Levels 1 and 2 are one-off overrides for writing a session that will be opened on a
    different machine, so they outrank the stored path rather than the other way round. Both
    come back as "override" rather than as one of the two known kinds: an override cannot
    tell us what it points at, and the caller who supplied it knows what they supplied.
    Callers therefore get neither the stand-in marker nor the recording's frame count for an
    override — both would be claims this function is not in a position to make.
    """
    if audio_uri:
        return audio_uri, "override"
    if audio_uri_base:
        base = audio_uri_base.rstrip("/")
        # An audio_uri_base points at the annotations' own layout, so the file it names is
        # whatever sits beside them — the same file duration_sec describes.
        return file_uri(f"{base}/{item.disk_path}/{item.audio_filename}"), "override"

    path, kind, _ = item.playback_audio()
    if not path:
        raise ExportError(
            f"{item.song_id} has no audio to point at: it has no linked recording, and its "
            f"corpus has no root_path to locate the audio stored beside the annotations."
        )
    return file_uri(path), kind


def _audio_frames(item, kind: str, sample_rate: int) -> int:
    """Frame count for the audio model, describing the file the URI actually names.

    The item carries two durations because it can name two different files. duration_sec
    measures the audio the annotations were imported alongside; recording_duration_sec
    measures the linked original. Reading the wrong one understates or overstates the file
    the session points at, by however much the two files differ — which is exactly the
    lead-in and lead-out a stand-in generated from the annotations does not have.

    No filesystem access: link_recordings already probed the recording, so the figure is
    stored rather than re-measured at export time. That keeps export a pure database read,
    which matters because the recordings are not necessarily mounted wherever an export runs.
    """
    if kind == "recording" and item.recording_duration_sec:
        seconds = item.recording_duration_sec
    else:
        seconds = item.duration_sec or 0.0
    return int(round(seconds * sample_rate))


def item_to_session(layers, *, audio_uri: str | None = None, audio_uri_base: str | None = None) -> bytes:
    """
    A whole SV session (.sv, which is bzip2-compressed XML).

    All the layers must belong to the same item. The first model is the audio; each layer
    then contributes one model plus one display layer.

    Which audio the session points at is decided by _session_audio() above — a linked
    original recording when there is one, otherwise the file beside the annotations. When
    that fallback is a stand-in file, the audio model's name says so, and the audio
    model's frame count is taken from whichever of the two files the URI actually names.

    audio_uri_base replaces the corpus root when building the audio path. The path written
    into the file has to be valid on the machine that opens the session, and the corpus
    root is the importer's view of the world — inside a container that is something like
    /data, which does not exist on the reviewer's machine. Pass audio_uri to override the
    whole URI instead; it is then used verbatim, encoding included.
    """
    layers = sort_layers_by_stream(layers)
    if not layers:
        raise ExportError("No layers to export")
    item = layers[0].item
    if any(l.item_id != item.pk for l in layers):
        raise ExportError("Layers exported as one session must belong to the same item")
    sr = _require_sample_rate(item)

    uri, audio_kind = _session_audio(item, audio_uri, audio_uri_base)
    audio_name = item.display_name + (STAND_IN_MARKER if audio_kind == "proxy" else "")
    total_frames = _audio_frames(item, audio_kind, sr)

    # One id space for models, datasets and display layers, matching SV's own writer.
    ids = itertools.count()
    audio_model_id = next(ids)
    waveform_layer_id = next(ids)

    data = [
        "  <data>",
        (
            f'    <model id="{audio_model_id}" name={quoteattr(audio_name)} '
            f'sampleRate="{sr}" start="0" end="{total_frames}" type="wavefile" '
            f'file={quoteattr(uri)} mainModel="true"/>'
        ),
    ]
    display = [
        "  <display>",
        (
            f'    <window width="1200" height="700"/>'
        ),
        '    <view type="pane" centre="0" zoom="512" followPan="1" followZoom="1" tracking="page">',
        f'      <layer id="{waveform_layer_id}" type="waveform" name="Waveform" '
        f'model="{audio_model_id}" colourName="Blue" scale="0" channelMode="0"/>',
    ]

    for position, layer in enumerate(layers):
        model_id = next(ids)
        dataset_id = next(ids)
        layer_id = next(ids)
        subtype, sv_layer_type, units = _model_kind(layer)
        pts = _points(layer, sr)
        values = [p["value"] for p in pts if p["value"] > 0]
        vmin = min(values) if values else 0.0
        vmax = max(values) if values else 0.0
        start = pts[0]["frame"] if pts else 0
        end = max((p["frame"] + p["duration"] for p in pts), default=0)
        name = f"{layer.stream_key or layer.layer_type} v{layer.version}"
        if layer.corrections.filter(is_active=True).exists():
            name += " (corrections applied)"

        data.append(
            f'    <model id="{model_id}" name={quoteattr(name)} sampleRate="{sr}" '
            f'start="{start}" end="{end}" type="sparse" dimensions="3" resolution="1" '
            f'notifyOnAdd="true" dataset="{dataset_id}" subtype="{subtype}" '
            f'valueQuantization="0" minimum="{vmin:.6f}" maximum="{vmax:.6f}" '
            f'units={quoteattr(units)}/>'
        )
        data.append(f'    <dataset id="{dataset_id}" dimensions="3">')
        for p in pts:
            data.append(
                f'      <point frame="{p["frame"]}" value="{p["value"]:.6f}" '
                f'duration="{p["duration"]}" level="1" label={quoteattr(p["label"])}/>'
            )
        data.append("    </dataset>")

        display.append(
            f'      <layer id="{layer_id}" type="{sv_layer_type}" name={quoteattr(name)} '
            f'model="{model_id}" verticalScale="0" scaleMinimum="{vmin:.6f}" '
            f'scaleMaximum="{vmax:.6f}" colourName="{_colour(position)}"/>'
        )

    data.append("  </data>")
    display += ["    </view>", "  </display>"]

    xml = "\n".join(
        ['<?xml version="1.0" encoding="UTF-8"?>', SV_DOCTYPE, "<sv>", *data, *display,
         "  <selections/>", "</sv>", ""]
    )
    # bzip2, not gzip. See the module docstring: SV cannot read a gzipped session, and
    # the error it gives when handed one names the audio, not the compression.
    return bz2.compress(xml.encode("utf-8"))


def _colour(position: int) -> str:
    """A distinct colour per layer in a session, so two streams never come out identical.

    Keyed on position in the sorted layer list rather than on the stream key's spelling. A
    name-to-colour table only covers the names it was written with, and every corpus
    outside that list gets one colour for all of its layers — which is precisely the case
    where telling them apart matters.
    """
    return _COLOUR_CYCLE[position % len(_COLOUR_CYCLE)]


def suggested_filename(layer) -> str:
    stream = layer.stream_key or "layer"
    return f"{layer.item.song_id}_{stream}_v{layer.version}.svl"


def suggested_session_filename(item) -> str:
    return f"{item.song_id}.sv"
