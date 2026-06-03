"""Pydantic models for CAT+TAG (Video, Tag, SearchFilters, etc.)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator


class Segment(TypedDict, total=False):
    """Canonical typed segment used for transcription and AI journalist cuts.

    Time fields are normalized to float seconds at load time.
    Legacy data may contain string timestamps under 'start'/'end'.
    """

    source_in: float | str
    source_out: float | str
    start: float | str  # legacy key for raw transcription (normalized to float on load)
    end: float | str  # legacy key for raw transcription
    text: str
    reason: str  # only present on AI-selected cuts


class Video(BaseModel):
    id: int | None = None
    path: str
    filename: str
    size: int | None = None
    fingerprint: str | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    bit_rate: int | None = None
    audio_channels: int | None = None
    shoot_date: date | None = None
    project: str | None = None
    location: str | None = None
    camera: str | None = None
    operator: str | None = None
    lens: str | None = None
    notes: str | None = None
    thumbnail_path: str | None = None
    storyboard_path: str | None = None
    camera_xml_path: str | None = None
    iso: int | None = None
    f_number: float | None = None
    shutter_speed: str | None = None
    focal_length: float | None = None
    white_balance: str | None = None
    gamma: str | None = None
    color_primaries: str | None = None
    coding_equations: str | None = None
    import_date: datetime | None = None
    last_seen: datetime | None = None
    missing: bool = False
    tags: list[str] = Field(default_factory=list)
    transcription_segments: list[Segment] | None = (
        None  # Original language segments (normalized to float seconds)
    )
    original_language: str | None = (
        None  # Detected ISO code for the original transcription (e.g. "en", "fi")
    )
    translated_transcriptions: dict[str, list[Segment]] = Field(
        default_factory=dict
    )  # lang_code -> segments

    # Timecode information extracted at import time
    tc_start: str | None = None  # e.g. "01:23:45:12"  (starting timecode of the clip)
    tc_end: str | None = None  # e.g. "01:24:12:03"  (ending timecode of the clip)

    @model_validator(mode="after")
    def _normalize_transcription_timestamps(self) -> Video:
        """Ensure all transcription segments have float timestamps.

        This handles legacy data that was stored with string 'HH:MM:SS.mmm' values
        under the 'start'/'end' keys.
        """

        def _to_float(ts: Any) -> float:
            if ts is None:
                return 0.0
            if isinstance(ts, (int, float)):
                return float(ts)
            if isinstance(ts, str):
                t = ts.strip().replace(",", ".")
                parts = t.split(":")
                try:
                    if len(parts) == 3:
                        h, m, s = parts
                        return int(h) * 3600 + int(m) * 60 + float(s)
                    elif len(parts) == 2:
                        m, s = parts
                        return int(m) * 60 + float(s)
                    else:
                        return float(parts[0])
                except Exception:
                    return 0.0
            return 0.0

        def _normalize_list(segments: list[dict] | None) -> list[dict]:
            if not segments:
                return []
            normalized = []
            min_start = None
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                new_seg = dict(seg)
                # Normalize possible keys
                for old_key, new_key in [
                    ("start", "start"),
                    ("end", "end"),
                    ("source_in", "source_in"),
                    ("source_out", "source_out"),
                ]:
                    if old_key in new_seg:
                        new_seg[old_key] = _to_float(new_seg[old_key])
                # Track min for later shift-to-zero (only for obvious absolute/TOD timestamps >1h from transcriber)
                s0 = new_seg.get("start")
                if s0 is None:
                    s0 = new_seg.get("source_in")
                if isinstance(s0, (int, float)) and (min_start is None or s0 < min_start):
                    min_start = float(s0)
                normalized.append(new_seg)
            # Only shift if min_start looks like absolute/TOD time (e.g. >1 hour),
            # not normal media-relative silence before first speech (0.5- few seconds).
            # This keeps "source" times as true seconds from the beginning of the media file,
            # so they match the video timeline, embedded TC offsets, and what the user
            # sees when opening the file in Premiere.
            if min_start is not None and min_start > 3600:
                for new_seg in normalized:
                    for k in ("start", "end", "source_in", "source_out"):
                        if k in new_seg and isinstance(new_seg[k], (int, float)):
                            new_seg[k] = max(0.0, new_seg[k] - min_start)

            # Always sanitize on load: fix any remaining inversions from old bad transcriptions,
            # drop junk, ensure sorted. This makes old garbage data usable without re-transcribing.
            from minicat.ai.transcriber import sanitize_transcription_segments

            normalized = sanitize_transcription_segments(normalized)
            return normalized

        if self.transcription_segments:
            self.transcription_segments = _normalize_list(self.transcription_segments)  # type: ignore

        if self.translated_transcriptions:
            for lang, segs in list(self.translated_transcriptions.items()):
                self.translated_transcriptions[lang] = _normalize_list(segs)  # type: ignore

        return self


class Tag(BaseModel):
    id: int | None = None
    name: str
    created_at: datetime | None = None


class SearchFilters(BaseModel):
    text: str | None = None
    client: list[str] | None = None  # New: filter by client name(s)
    project: list[str] | None = None
    location: list[str] | None = None
    camera: list[str] | None = None
    date_from: date | None = None
    date_to: date | None = None
    tags: list[str] | None = None
    min_duration: float | None = None
    max_duration: float | None = None


class CatalogSettings(BaseModel):
    thumbnail_width: int = 480
    storyboard_cols: int = 4
    storyboard_rows: int = 3
    storyboard_cell_width: int = 240
    storyboard_cell_height: int = 135  # fixed cell size → all storyboards have identical dimensions
    jpeg_quality: int = 85


ProjectStatus = Literal["Pre-production", "Production", "Post-production", "Delivered", "Archived"]


class Client(BaseModel):
    """Rich Client entity. One client can have multiple projects."""

    id: int | None = None
    name: str
    contact_person: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    notes: str | None = None
    color: str | None = None  # Hex color for visual tagging in sidebar
    logo_path: str | None = None  # Local path to client logo image
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Computed
    project_count: int = 0


class Project(BaseModel):
    id: int | None = None
    name: str
    start_date: date | None = None
    end_date: date | None = None
    # Deprecated single client string - kept for backward compat during transition
    client: str | None = None
    # New: list of client names this project belongs to (many-to-many)
    clients: list[str] = Field(default_factory=list)
    director: str | None = None
    producer: str | None = None
    editor: str | None = None
    camera_operators: list[str] = Field(default_factory=list)
    location: str | None = None
    status: ProjectStatus = "Production"
    color: str | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Computed / convenience
    clip_count: int = 0
    total_duration: float = 0.0
