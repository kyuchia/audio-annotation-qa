"""
Reading audio headers.

The importer always reads sample rate, duration and channels from the audio header
itself, and trusts nothing a metadata file claims about them. A corpus whose files all
share one sample rate does so because one process produced them, not because the next
corpus will; treating a uniform batch as a guarantee is how the first mixed one lands
silently at the wrong frame positions. This is not a design option, it is the importer's
basic job.

Only the header is read, never the audio data, so a whole corpus scans in milliseconds.
"""
from __future__ import annotations

import contextlib
import wave
from dataclasses import dataclass
from pathlib import Path

#: Audio editors leave sidecar analysis files beside the audio (cached waveform peaks and
#: the like). They are not audio, so the importer ignores them by extension.
IGNORED_SUFFIXES = {".asd", ".reapeaks", ".peak", ".DS_Store"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db"}

#: When macOS writes to a non-HFS volume (an exFAT external drive), every file gets an
#: AppleDouble resource fork alongside it, named with a leading "._". It shares the real
#: file's name and extension, so any *.wav / *.csv glob picks it up, and reading it as an
#: audio header then blows up.
APPLEDOUBLE_PREFIX = "._"


def is_ignored_file(path: str | Path) -> bool:
    p = Path(path)
    if p.name.startswith(APPLEDOUBLE_PREFIX):
        return True
    return p.name in IGNORED_NAMES or p.suffix in IGNORED_SUFFIXES


@dataclass(frozen=True)
class MediaInfo:
    sample_rate: int
    duration_sec: float
    channels: int
    frames: int
    format: str
    reader: str


class AudioProbeError(RuntimeError):
    pass


def probe_audio(path: str | Path) -> MediaInfo:
    """Read the specs from the header. soundfile first, falling back to the stdlib wave module."""
    p = Path(path)
    if not p.exists():
        raise AudioProbeError(f"File does not exist: {p}")
    if is_ignored_file(p):
        raise AudioProbeError(f"This is a sidecar file that should be ignored, not audio: {p.name}")

    info = _probe_soundfile(p)
    if info is None:
        info = _probe_wave(p)
    if info is None:
        raise AudioProbeError(f"Could not read the header: {p}")
    if info.sample_rate <= 0 or info.frames <= 0:
        raise AudioProbeError(
            f"Header values are implausible: {p} sr={info.sample_rate} frames={info.frames}"
        )
    return info


def _probe_soundfile(p: Path) -> MediaInfo | None:
    try:
        import soundfile as sf
    except Exception:
        return None
    try:
        i = sf.info(str(p))
    except Exception:
        return None
    return MediaInfo(
        sample_rate=int(i.samplerate),
        duration_sec=float(i.frames) / float(i.samplerate),
        channels=int(i.channels),
        frames=int(i.frames),
        format=f"{i.format}/{i.subtype}",
        reader="soundfile",
    )


def _probe_wave(p: Path) -> MediaInfo | None:
    if p.suffix.lower() != ".wav":
        return None
    with contextlib.suppress(Exception):
        with wave.open(str(p), "rb") as w:
            sr = w.getframerate()
            n = w.getnframes()
            return MediaInfo(
                sample_rate=sr,
                duration_sec=n / float(sr) if sr else 0.0,
                channels=w.getnchannels(),
                frames=n,
                format=f"WAV/PCM_{w.getsampwidth() * 8}",
                reader="wave",
            )
    return None
