"""Catalog root handling and settings (lightweight)."""

from __future__ import annotations

from pathlib import Path

from minicat.core.db import init_catalog


def resolve_catalog(root: str | Path) -> Path:
    """Ensure the path is a valid catalog (initialize if needed) and return it."""
    p = Path(root).expanduser().resolve()
    init_catalog(p)
    return p


def get_previews_dir(catalog_root: Path, kind: str = "thumbs") -> Path:
    """Return previews/thumbs or previews/boards inside the catalog."""
    sub = "boards" if kind == "boards" else "thumbs"
    d = catalog_root / "previews" / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_audio_dir(catalog_root: Path) -> Path:
    """Return the persistent transcription proxy audio folder inside the catalog.
    Every media file has exactly one processed audio file here:
      <catalog>/audio/000042.m4a
    24 kHz mono AAC @64 kbps with peak normalization to -3 dB (mono + volume norm only).
    Used for transcription (Gemini) and all AI listening (Journalist tone/emotion etc.).
    """
    d = catalog_root / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_transcriptions_dir(catalog_root: Path) -> Path:
    """Return the folder for plain text transcript files (.txt).
    Stored with zero-padded naming:
      000001.txt          → original transcript
      000001_fi.txt       → Finnish translation
    """
    d = catalog_root / "transcriptions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_subtitles_dir(catalog_root: Path) -> Path:
    """Return the folder for timed subtitle files (.srt).
    Stored with zero-padded naming consistent with previews:
      000001.srt          → original subtitles
      000001_fi.srt       → Finnish subtitles
    """
    d = catalog_root / "subtitles"
    d.mkdir(parents=True, exist_ok=True)
    return d
