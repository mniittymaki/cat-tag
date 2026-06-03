"""
Background Task Controllers (Layer 2)

This module isolates all long-running background work (transcription, AI tagging,
proxy generation, etc.) from the NiceGUI presentation layer.

The UI should only:
- Call high-level functions like queue_transcription()
- Read job state via get_transcription_jobs()
- Optionally register lightweight callbacks for progress updates

No direct UI creation (ui.notify, ui.dialog, .refresh()) should live here.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# --- Domain models (pure data) ---

@dataclass
@dataclass
class AITaggingProgress:
    """Mutable object to report live progress from background AI tagging.
    Used by the import wizard to show progress without the background task
    touching the NiceGUI context directly.
    """
    total: int = 0
    completed: int = 0
    total_tags_added: int = 0
    is_complete: bool = False
    errors: int = 0


@dataclass
class NarrationVOProgress:
    """Mutable object to report live progress from background narration/VO audio generation
    (used for AI Director loaded stories and XML+VO exports).

    The background worker (asyncio task) MUST ONLY mutate fields here.
    A ui.timer created in the original NiceGUI handler/timer context polls this and
    performs all .text / .value / .close / notify / ui.update from the safe slot.
    This prevents "slot stack is empty" / "create UI from background task" errors.
    """
    value: float = 0.0
    status: str = "Preparing TTS for narration script..."
    detail: str = ""
    is_complete: bool = False
    result_path: str | None = None
    error: str | None = None


@dataclass
class TranscriptionJob:
    """Represents one transcription (+ optional translation) job in the queue."""
    clip_id: int
    filename: str
    do_translate: bool = False
    status: str = "queued"          # queued | running | done | error
    message: str = ""
    started_at: float | None = None


# --- Transcription Queue State ---

TRANSCRIPTION_JOBS: list[TranscriptionJob] = []
_transcription_worker_task: asyncio.Task | None = None

# Callback the UI can register to be notified when job status changes
# (kept minimal to avoid tight coupling)
_status_update_callbacks: list[Callable[[], None]] = []


def register_status_updater(callback: Callable[[], None]) -> None:
    """UI components can register a lightweight function to be called on progress changes."""
    if callback not in _status_update_callbacks:
        _status_update_callbacks.append(callback)


def _notify_status_update() -> None:
    for cb in list(_status_update_callbacks):
        try:
            cb()
        except Exception:
            pass


# --- Public API ---

def get_transcription_jobs() -> list[TranscriptionJob]:
    """Return a copy of current jobs (safe for UI consumption)."""
    return list(TRANSCRIPTION_JOBS)


def queue_transcription(
    clip_id: int,
    filename: str,
    do_translate: bool = False,
    catalog_root: Path | str | None = None,
) -> bool:
    """
    Add a clip to the background transcription queue.

    Returns True if queued successfully, False if already in queue.
    """
    # Prevent duplicate active jobs
    if any(j.clip_id == clip_id and j.status in ("queued", "running") for j in TRANSCRIPTION_JOBS):
        return False

    job = TranscriptionJob(
        clip_id=clip_id,
        filename=filename,
        do_translate=do_translate,
        status="queued",
        message="Queued",
    )
    TRANSCRIPTION_JOBS.append(job)
    _notify_status_update()
    _ensure_transcription_worker()
    return True


def clear_completed_jobs() -> int:
    """Remove done/error jobs. Returns number removed."""
    global TRANSCRIPTION_JOBS
    before = len(TRANSCRIPTION_JOBS)
    TRANSCRIPTION_JOBS = [j for j in TRANSCRIPTION_JOBS if j.status not in ("done", "error")]
    return before - len(TRANSCRIPTION_JOBS)


# --- Internal Worker Implementation ---

def _ensure_transcription_worker() -> None:
    global _transcription_worker_task
    if _transcription_worker_task is None or getattr(_transcription_worker_task, "done", lambda: True)():
        print("[Transcription] Starting background worker task...")
        _transcription_worker_task = asyncio.create_task(_transcription_worker())


async def _transcription_worker() -> None:
    """Single worker that processes the queue one clip at a time."""
    print("[Transcription Worker] Worker coroutine has started running.")

    # Lazy imports to avoid circular dependencies at module load
    from minicat.core import db
    from minicat.core.models import SearchFilters
    from minicat.core.video import ensure_cached_audio, save_transcription_srt, save_transcription_txt
    from minicat.ai.transcriber import (
        transcribe_audio_with_timestamps,
        translate_transcription_segments,
    )
    from minicat.core.settings import (
        get_gemini_api_key,
        get_gemini_model,
        get_preference,
    )

    while True:
        try:
            job = next((j for j in TRANSCRIPTION_JOBS if j.status == "queued"), None)
            if not job:
                await asyncio.sleep(0.6)
                continue
        except Exception as outer_e:
            print(f"[Transcription Worker] Error in job selection loop: {outer_e}")
            await asyncio.sleep(1)
            continue

        job.status = "running"
        job.started_at = time.time()
        job.message = "Starting..."
        _notify_status_update()

        try:
            # We need catalog context. For now we assume the job was queued with
            # a running AppState. In a future version this should be passed explicitly.
            # For the current extraction we keep the original behavior.
            from minicat.ui.app import get_state  # temporary bridge during refactor

            state = get_state()
            if not state:
                job.status = "error"
                job.message = "No catalog loaded"
                await asyncio.sleep(1)
                continue

            # Resolve clip
            clip = next((v for v in state.videos if v.id == job.clip_id), None)
            if not clip:
                try:
                    all_videos = db.search_videos(state.catalog_root, SearchFilters(), limit=20000)
                    clip = next((vv for vv in all_videos if vv.id == job.clip_id), None)
                except Exception as search_err:
                    print(f"[Transcription Worker] DB search failed: {search_err}")

            if not clip or not getattr(clip, "path", None):
                job.status = "error"
                job.message = "Clip not found"
                continue

            segment_count = await _run_transcription(
                clip=clip,
                job=job,
                catalog_root=state.catalog_root,
                ensure_audio_fn=ensure_cached_audio,
                transcribe_fn=transcribe_audio_with_timestamps,
                translate_fn=translate_transcription_segments,
                get_api_key=get_gemini_api_key,
                get_model=get_gemini_model,
                get_pref=get_preference,
            )

            if job.do_translate:
                job.message = "AI translating..."
                default_lang = get_preference("ai.default_translation_lang", "en")
                if default_lang and getattr(clip, "transcription_segments", None):
                    try:
                        clip_dur = getattr(clip, "duration", None)
                        translated = await asyncio.to_thread(
                            translate_transcription_segments,
                            clip.transcription_segments,
                            default_lang,
                            get_gemini_api_key(),
                            model_name=get_gemini_model(),
                            max_duration=clip_dur,
                        )
                        if not getattr(clip, "translated_transcriptions", None):
                            clip.translated_transcriptions = {}
                        clip.translated_transcriptions[default_lang] = translated
                        clip._current_transcription_lang = default_lang

                        # Save the translated TXT + SRT persistently (same as inspector translate path)
                        try:
                            from minicat.core.video import save_transcription_txt, save_transcription_srt
                            fps = getattr(clip, "fps", None)
                            base_tc = getattr(clip, "tc_start", None)
                            save_transcription_txt(clip.id, state.catalog_root, translated, lang=default_lang, fps=fps, base_timecode=base_tc)
                            save_transcription_srt(clip.id, state.catalog_root, translated, lang=default_lang, fps=fps, base_timecode=base_tc)
                        except Exception as save_ex:
                            print(f"[Transcription] Failed to save translated TXT/SRT for {clip.id}: {save_ex}")

                        # Preserve original language info
                        trans_data = {
                            "original": {
                                "language": getattr(clip, "original_language", None),
                                "segments": clip.transcription_segments
                            },
                            "translations": clip.translated_transcriptions
                        }
                        db.update_video_fields(
                            state.catalog_root,
                            clip.id,
                            transcription=json.dumps(trans_data, ensure_ascii=False),
                        )
                    except Exception as te:
                        print(f"[Transcription] Translation failed: {te}")

            job.status = "done"
            job.message = f"Done ({segment_count} segments)"

            # Request UI refresh via registered callbacks (preferred) or direct call as fallback
            _notify_status_update()

            # Temporary bridge: still call the old refresh mechanism during transition
            try:
                from minicat.ui.app import refresh_all_ui
                if state.selected and state.selected.id == job.clip_id:
                    refreshed = db.get_video_by_path(state.catalog_root, clip.path)
                    if refreshed:
                        state.selected = refreshed
                refresh_all_ui(state)
            except Exception:
                pass

        except Exception as e:
            job.status = "error"
            err_str = str(e)
            # Friendly messages for common bad media file cases (corrupted downloads, incomplete exports, etc.)
            if "moov atom" in err_str.lower() or "invalid data found when processing input" in err_str.lower():
                nice = "Source video file is corrupted or incomplete (moov atom missing). Replace with a valid copy and try again."
            elif "ffprobe failed" in err_str:
                nice = "Could not read video metadata (ffprobe failed). The file may be damaged or in an unsupported format."
            elif "ffmpeg audio extraction failed" in err_str:
                nice = "Failed to extract audio from the video. The file may be corrupted or unreadable."
            else:
                nice = err_str[:90]
            job.message = nice
            print(f"[Transcription Queue] Failed for {job.filename}: {e}")

        await asyncio.sleep(0.8)


async def _run_transcription(
    clip: Any,
    job: TranscriptionJob,
    catalog_root: Path | str,
    ensure_audio_fn: Any,
    transcribe_fn: Any,
    translate_fn: Any,
    get_api_key: Any,
    get_model: Any,
    get_pref: Any,
) -> int:
    """Internal pure transcription runner. No UI side effects."""
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("Gemini API key required in Settings")

    # Use persistent cached audio (never re-extract on repeated transcription attempts)
    # No artificial duration cap: transcribe the full clip so long interviews
    # (20+ min) are complete. Gemini + sanitize handle long audio; the prompt
    # was updated to not assume short files.
    if job:
        job.message = "Extracting transcription proxy audio (24 kHz mono AAC 64k + -3dB peak norm)..."

    audio_path = await asyncio.to_thread(
        ensure_audio_fn,
        clip.path,
        clip.id,
        catalog_root,
        None,  # full duration
    )

    if not audio_path:
        raise RuntimeError("Failed to obtain transcription audio (cached or extracted)")

    if job:
        job.message = "AI transcribing..."

    # ALWAYS CONFIRM THE FRAMERATE (but fps is confirmed at import time and is immutable thereafter).
    # Strategy:
    # - Prefer the fps stored on the clip (set at import via extract_metadata from the actual file).
    # - If the stored fps is missing/zero (legacy clip imported before reliable fps storage),
    #   probe the file now with confirm_video_framerate, use it, and backfill it into the DB
    #   exactly once. After that it is never changed.
    # - If stored fps exists, use it as authoritative. We may still live-probe for logging/verification
    #   but will not overwrite.
    from minicat.core.video import confirm_video_framerate
    stored_fps = getattr(clip, "fps", None)
    if stored_fps and float(stored_fps) > 0:
        fps = float(stored_fps)
        if fps > 120 or fps < 1:
            print(f"[Transcription] WARNING: stored fps {fps} on clip {getattr(clip, 'id', '?')} looks bogus, re-probing live...")
            fps = confirm_video_framerate(clip.path)
            # backfill the corrected value
            try:
                from minicat.core import db as _db
                _db.update_video_fields(catalog_root, clip.id, fps=fps)
                if hasattr(clip, "__dict__"):
                    clip.fps = fps
                elif hasattr(clip, "fps"):
                    setattr(clip, "fps", fps)
                print(f"[Transcription] Corrected bogus stored fps to {fps} and backfilled to DB")
            except Exception as fix_ex:
                print(f"[Transcription] Could not backfill corrected fps: {fix_ex}")
        else:
            print(f"[Transcription] Using framerate confirmed at import time: {fps} fps (stored on clip {getattr(clip, 'id', '?')}, immutable after import)")
        # Verify against live file for diagnostics (do not change stored value)
        try:
            live_fps = confirm_video_framerate(clip.path)
            if abs(live_fps - fps) > 0.05:
                print(f"[Transcription] NOTE: live probe reports {live_fps} but stored (import-time) value is {fps}. "
                      "Using stored value as canonical per 'confirmed at import and never changed' rule.")
            else:
                print(f"[Transcription] Live probe matches stored import-time value ({live_fps}).")
        except Exception as probe_ex:
            print(f"[Transcription] Live probe for verification failed (non-fatal): {probe_ex}")
    else:
        # Legacy clip: confirm now by probing the video file
        fps = confirm_video_framerate(clip.path)
        print(f"[Transcription] No framerate stored on legacy clip — confirmed now via live probe: {fps}")
        # Backfill so future transcriptions and the record treat it as "confirmed at import"
        try:
            from minicat.core import db as _db
            _db.update_video_fields(catalog_root, clip.id, fps=fps)
            # Update in-memory object too
            if hasattr(clip, "__dict__"):
                clip.fps = fps  # type: ignore[attr-defined]
            elif hasattr(clip, "fps"):
                setattr(clip, "fps", fps)
            print(f"[Transcription] Backfilled framerate {fps} into DB for clip {getattr(clip, 'id', '?')} "
                  "(one-time for legacy data; will be treated as immutable from now on).")
        except Exception as backfill_ex:
            print(f"[Transcription] Could not backfill fps to DB (will still use probed value for this run): {backfill_ex}")

    transcription_result = await asyncio.to_thread(
        transcribe_fn,
        audio_path,
        api_key,
        model_name=get_model(),
        fps=fps,
        total_duration=getattr(clip, "duration", None),
    )

    # New format returns dict with segments + language
    if isinstance(transcription_result, dict):
        segments = transcription_result.get("segments", [])
        detected_language = transcription_result.get("language")
    else:
        # Backward compat with old list return
        segments = transcription_result or []
        detected_language = None

    # Re-sanitize with actual audio duration for tight clamping (catches any remaining
    # out-of-range timestamps that the transcriber's internal sanitize missed).
    # Prefer the original video duration (we told Gemini about it) so we don't truncate
    # legitimate end-of-clip content.
    clip_dur = getattr(clip, "duration", None)
    try:
        import subprocess
        import json as _json
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(audio_path)],
            capture_output=True, text=True, timeout=10
        )
        adur = float(_json.loads(probe.stdout or "{}").get("format", {}).get("duration", 0) or 0)
    except Exception:
        adur = None
    max_dur = clip_dur or adur

    from minicat.ai.transcriber import sanitize_transcription_segments
    segments = sanitize_transcription_segments(segments, max_duration=max_dur)

    clip.transcription_segments = segments
    if detected_language:
        clip.original_language = detected_language

    # Save original SRT persistently (like previews and transcription proxy audio)
    # Pass the detected language so YLE styling is applied for Finnish
    save_lang = detected_language or "original"
    try:
        from minicat.core.video import save_transcription_txt, save_transcription_srt
        # Use the fps we authoritatively chose above (import-time value or one-time legacy backfill).
        # This respects "confirmed at import and never be changed".
        base_tc = getattr(clip, "tc_start", None)
        save_transcription_txt(clip.id, catalog_root, segments, lang=save_lang, fps=fps, base_timecode=base_tc)
        save_transcription_srt(clip.id, catalog_root, segments, lang=save_lang, fps=fps, base_timecode=base_tc)
    except Exception as srt_ex:
        print(f"[Transcriptions] Failed to save transcript/SRT for clip {clip.id}: {srt_ex}")

    # New storage format with language info
    trans_data = {
        "original": {
            "language": detected_language or getattr(clip, "original_language", None),
            "segments": segments
        },
        "translations": getattr(clip, "translated_transcriptions", {}) or {}
    }

    import json as _json
    from minicat.core import db as _db

    _db.update_video_fields(
        catalog_root,
        clip.id,
        transcription=_json.dumps(trans_data, ensure_ascii=False),
    )

    # Note: audio_path (the processed 24 kHz mono AAC 64k transcription proxy with -3dB peak norm)
    # is persistent in <catalog>/audio/ — we intentionally keep it for reuse by AI Journalist listening and future re-transcriptions.

    return len(segments)
