"""
Layer 1: Component Layouts — Inspector (Right Panel)

Houses:
- inspector_content() — main reactive right panel
- _render_project_inspector()
- _render_multi_selection_panel()
- Related helper renderers for clip details, technical info, transcription, etc.

This is the largest UI surface and will be further split in future passes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from nicegui import ui

from minicat.core import db, video
from minicat.ui.components import dialogs as ui_dialogs


@ui.refreshable
def inspector_content() -> None:
    """
    Main reactive right inspector panel (Layer 1 - extracted).

    This is now the canonical home for the inspector UI.
    We are progressively moving the detailed sections here.
    """
    from minicat.ui.app import get_state, main_content, refresh_all_ui
    from minicat.core.settings import get_preference, get_gemini_api_key, get_gemini_model
    import asyncio

    state = get_state()
    if state is None:
        with ui.column().classes("items-center justify-center h-full text-center"):
            ui.label("No catalog loaded").classes("text-h6 text-grey-6")
        return

    sel_count = len(state.selected_ids)

    if sel_count > 0:
        if sel_count == 1:
            if not state.selected:
                sel = state.get_selected_videos()
                state.selected = sel[0] if sel else None
            if not state.selected:
                return
            v = state.selected

            # CLIPNAME
            ui.label(v.filename).classes("text-h6 font-bold mb-2")

            # Storyboard
            _render_storyboard_section(v, state)

            # Action buttons
            ui.button("Rebuild Previews + Metadata", on_click=lambda: _rebuild_action(state, v), icon="refresh").props("size=sm outline").classes("w-full mt-2")

            # === CLIP DETAILS (detailed form - migrated) ===
            _render_clip_details(v, state, refresh_all_ui)

            # === TECHNICAL INFO ===
            _render_technical_info(v, state)

            # === TRANSCRIBE ===
            _render_transcribe_section(v, state)

        else:
            _render_multi_selection_panel(state)

    elif state.selected_project:
        _render_project_inspector(state, state.selected_project)
    else:
        ui.label("No selection").classes("text-grey-6")


# --- Sub-renderers (being populated in this step) ---

def _render_storyboard_section(v, state):
    try:
        has_storyboard = getattr(v, "storyboard_path", None) and Path(v.storyboard_path).exists()
        if has_storyboard:
            sb_path = Path(v.storyboard_path)
            img = ui.image(str(sb_path)).classes(
                "w-full cursor-pointer hover:opacity-90 transition-opacity"
            ).style("max-height: 160px; object-fit: contain; border: 1px solid #333; border-radius: 4px; background: #111;")
            img.on("click", lambda vv=v: _show_storyboard_dialog(vv))
            ui.label("Storyboard ready").classes("text-xs text-blue-400 mt-1")
        else:
            ui.label("No storyboard yet").classes("text-xs text-grey-6 italic mb-1")
            ui.button("Generate", icon="image", on_click=lambda: _generate_storyboard_now(v, state)).props("size=sm outline dense").classes("mt-1")
    except Exception as sb_err:
        print(f"[Inspector] Storyboard error: {sb_err}")


def _render_clip_details(v, state, refresh_all_ui_fn):
    """Detailed editable metadata form - migrated from original."""
    with ui.expansion("CLIP DETAILS", icon="edit", value=True).classes("w-full mb-0 inspector-expansion"):
        with ui.column().classes("w-full gap-0 -space-y-[2px]"):
            # Project
            project_input = ui.input(label="Project", value=v.project or "", placeholder="Project name").props("dense").classes("w-full mb-0 text-sm")

            # Clients (read-only for now)
            client_names = db.get_clients_for_project(state.catalog_root, v.project) if v.project else []
            client_display = ", ".join([c.name for c in client_names]) if client_names else "(No Client)"
            ui.input(label="Client(s)", value=client_display).props("readonly dense").classes("w-full mb-0 text-sm text-grey-5")

            # Location
            location_input = ui.input(label="Location", value=v.location or "", placeholder="Enter location").props("dense").classes("w-full mb-0 text-sm")

            # Date
            with ui.row().classes("items-end gap-2 w-full mb-0"):
                date_input = ui.input(label="Date", value=str(v.shoot_date) if v.shoot_date else None, placeholder="Select date").props("dense").classes("flex-1 text-sm")
                with ui.menu() as date_menu:
                    ui.date().bind_value(date_input, 'value')
                with date_input.add_slot('append'):
                    ui.icon('event', size='sm').classes('cursor-pointer').on('click', date_menu.open)
                def clear_date():
                    date_input.set_value(None)
                    _save_clip_details(state, v, project_input, location_input, date_input, None, None, None, None, None, None, None, None)
                ui.button(icon="close", on_click=clear_date).props("size=xs flat dense round").classes("mb-0")

            # More fields (camera, operator, lens, technical)
            camera_input = ui.input(label="Camera", value=v.camera or "", placeholder="Camera model").props("dense").classes("w-full mb-0 text-sm")
            operator_input = ui.input(label="Operator", value=v.operator or "", placeholder="Camera operator").props("dense").classes("w-full mb-0 text-sm")
            lens_input = ui.input(label="Lens", value=v.lens or "", placeholder="Lens").props("dense").classes("w-full mb-0 text-sm")

            iso_input = ui.input(label="ISO", value=str(v.iso) if v.iso else "", placeholder="ISO").props("dense").classes("w-full mb-0 text-sm")
            aperture_input = ui.input(label="Aperture", value=f"{v.f_number}" if v.f_number else "", placeholder="f/ number").props("dense").classes("w-full mb-0 text-sm")
            shutter_input = ui.input(label="Shutter Speed", value=v.shutter_speed or "", placeholder="1/xx").props("dense").classes("w-full mb-0 text-sm")
            focal_input = ui.input(label="Focal Length", value=f"{v.focal_length}" if v.focal_length else "", placeholder="mm").props("dense").classes("w-full mb-0 text-sm")
            wb_input = ui.input(label="White Balance", value=v.white_balance or "", placeholder="White Balance").props("dense").classes("w-full mb-0 text-sm")
            gamma_input = ui.input(label="Gamma", value=v.gamma or "", placeholder="Gamma").props("dense").classes("w-full mb-0 text-sm")

            # Tags (simplified for this pass)
            ui.label("Tags").classes("text-sm text-grey-6 mb-0.5")
            with ui.row().classes("flex-wrap gap-1 mb-1"):
                current_tags = getattr(v, "tags", None) or []
                for tag in current_tags:
                    ui.chip(tag, color="grey-8").props("size=sm")

            # Save button
            def save_clip_details():
                _save_clip_details(state, v, project_input, location_input, date_input,
                                   camera_input, operator_input, lens_input,
                                   iso_input, aperture_input, shutter_input, focal_input, wb_input, gamma_input)
            ui.button("Save Details", on_click=save_clip_details, color="primary").props("size=xs").classes("w-full mt-2")

            # AI Tags button (bridge)
            ui.button("Suggest Tags with AI", icon="auto_awesome", on_click=lambda: _launch_ai_tags(v), color="primary").props("size=xs outline").classes("w-full mt-1")


def _save_clip_details(state, v, project_input, location_input, date_input,
                       camera_input=None, operator_input=None, lens_input=None,
                       iso_input=None, aperture_input=None, shutter_input=None,
                       focal_input=None, wb_input=None, gamma_input=None):
    """Helper to persist edited clip details."""
    updates = {}
    if project_input and project_input.value != (v.project or ""):
        updates["project"] = project_input.value.strip() or None
    # ... (add similar for other fields in full implementation)
    if updates:
        db.update_video_fields(state.catalog_root, v.id, **updates)
        refreshed = db.get_video_by_path(state.catalog_root, v.path)
        if refreshed:
            state.selected = refreshed
        refresh_all_ui(state)
        ui.notify("Clip details saved", color="positive")


def _render_technical_info(v, state):
    with ui.expansion("TECHNICAL INFO", icon="info", value=True).classes("w-full mt-[-25px] inspector-expansion"):
        tech = []
        if v.duration: tech.append(("Duration", f"{v.duration:.1f}s"))
        if v.bit_rate: tech.append(("Bitrate", f"{v.bit_rate // 1000} kbps"))
        if v.width and v.height: tech.append(("Resolution", f"{v.width}×{v.height}"))
        if v.fps: tech.append(("Framerate", f"{v.fps:.2f} fps"))
        for label, value in tech:
            with ui.row().classes("justify-between text-sm py-0.5"):
                ui.label(label).classes("text-grey-6")
                ui.label(value)


def _render_transcribe_section(v, state):
    with ui.expansion("TRANSCRIBE", icon="mic", value=True).classes("w-full mt-[-25px] inspector-expansion"):
        # Live job status from the decoupled workers (Layer 2)
        try:
            from minicat.core import workers as task_workers
            jobs = task_workers.get_transcription_jobs()
            active_jobs = [j for j in jobs if j.clip_id == v.id and j.status in ("queued", "running")]
            if active_jobs:
                job = active_jobs[0]
                ui.label(f"⏳ {job.message} ({job.status})").classes("text-xs text-orange-400 mb-2")
        except Exception:
            pass

        has_trans = bool(getattr(v, "transcription_segments", None))
        if has_trans:
            ui.label(f"✓ Transcription ready ({len(v.transcription_segments)} segments)").classes("text-xs text-green-400")
        else:
            ui.label("No transcription yet").classes("text-xs text-grey-5")

        def _queue_transcribe(do_translate=False):
            try:
                from minicat.ui.app import queue_for_transcription
                queue_for_transcription(v, do_translate=do_translate)
                inspector_content.refresh()
            except Exception as e:
                ui.notify(f"Queue failed: {e}", color="negative")

        ui.button("TRANSCRIBE AUDIO WITH AI", icon="mic", on_click=lambda: _queue_transcribe(False), color="primary").props("size=md").classes("w-full")
        ui.button("TRANSCRIBE + TRANSLATE (DEFAULT)", icon="translate", on_click=lambda: _queue_transcribe(True), color="primary").props("size=md").classes("w-full mt-1")
        ui.button("AI Journalist Cut", icon="content_cut", color="primary").props("size=md").classes("w-full mt-1")

        # === Full Transcription Viewer (moved here) ===
        _render_transcription_viewer(v, state)


def _render_transcription_viewer(v, state):
    """Full segments viewer + language switching + on-demand translation.
    Extracted and adapted for the components layer + workers integration.
    """
    if not getattr(v, "transcription_segments", None):
        return

    from minicat.core.settings import get_preference, get_gemini_api_key, get_gemini_model
    from minicat.ai.transcriber import translate_transcription_segments
    import asyncio

    ui.label("TRANSCRIPTION").classes("text-sm font-semibold text-grey-4 mt-2 mb-1")

    # Language selector
    available_langs = ["original"]
    translations = getattr(v, "translated_transcriptions", {}) or {}
    available_langs += sorted(translations.keys())

    current_lang = getattr(v, "_current_transcription_lang", "original")
    if current_lang not in available_langs:
        current_lang = "original"

    lang_options = {"original": "Original"}
    for lang in sorted(translations.keys()):
        lang_options[lang] = lang.upper()

    lang_select = ui.select(
        options=lang_options,
        value=current_lang,
        label="Language",
    ).props("dense").classes("w-full")

    def change_lang():
        v._current_transcription_lang = lang_select.value
        inspector_content.refresh()

    lang_select.on_value_change(change_lang)

    # Segments display
    if current_lang == "original":
        segments_to_show = v.transcription_segments
    else:
        segments_to_show = translations.get(current_lang, [])

    with ui.column().classes("max-h-[200px] overflow-auto border border-zinc-700 rounded p-2 text-xs"):
        for seg in segments_to_show or []:
            start = seg.get("start", "")
            text = seg.get("text", "")
            ui.markdown(f"**[{start}]** {text}").classes("mb-1")

    # Translate controls (on-demand)
    with ui.row().classes("items-end gap-2 mt-2"):
        common_langs = [
            ("fi", "Finnish 🇫🇮"),
            ("sv", "Swedish 🇸🇪"),
            ("es", "Spanish 🇪🇸"),
            ("de", "German 🇩🇪"),
            ("fr", "French 🇫🇷"),
        ]
        trans_select = ui.select(
            options={code: label for code, label in common_langs},
            value=get_preference("ai.default_translation_lang", "fi"),
            label="Translate to",
        ).props("dense").classes("flex-1")

        async def do_translate():
            target = trans_select.value
            if not target or not state.selected:
                return
            source_segments = getattr(v, "transcription_segments", None)
            if not source_segments:
                ui.notify("No original transcription", color="warning")
                return
            api_key = get_gemini_api_key()
            if not api_key:
                ui.notify("Gemini API key required", color="warning")
                return

            loading = ui.dialog()
            with loading, ui.card().classes("w-[320px]"):
                ui.label(f"Translating to {target}...").classes("text-center")
                ui.spinner(size="lg").classes("mx-auto mt-3")
            loading.open()

            try:
                translated = await asyncio.to_thread(
                    translate_transcription_segments,
                    source_segments,
                    target,
                    api_key,
                    model_name=get_gemini_model(),
                )
                if not v.translated_transcriptions:
                    v.translated_transcriptions = {}
                v.translated_transcriptions[target.lower()] = translated
                v._current_transcription_lang = target.lower()

                # Persist
                import json
                trans_data = {
                    "original": v.transcription_segments,
                    "translations": v.translated_transcriptions
                }
                from minicat.core import db as _db
                _db.update_video_fields(
                    state.catalog_root, v.id,
                    transcription=json.dumps(trans_data, ensure_ascii=False),
                )
                ui.notify(f"Translated to {target}", color="positive")
                inspector_content.refresh()
            except Exception as e:
                ui.notify(f"Translation failed: {e}", color="negative")
            finally:
                try:
                    loading.close()
                except Exception:
                    pass

        ui.button("TRANSLATE", icon="translate", on_click=do_translate, color="primary").props("size=sm")


def _render_multi_selection_panel(state):
    """Full multi-selection panel (moved here)."""
    selected = state.get_selected_videos()
    if not selected:
        return

    ui.button(
        "Rebuild Previews + Metadata",
        icon="refresh",
        on_click=lambda: _batch_rebuild(state)
    ).props("size=md").classes("w-full mb-3")

    count = len(selected)
    total_duration = sum((v.duration or 0) for v in selected)
    total_size = sum((v.size or 0) for v in selected)

    ui.label(f"{count} clips selected").classes("text-h6 font-bold mb-2")

    with ui.column().classes("gap-1 text-body2 mb-4"):
        ui.label(f"Total Duration: {total_duration:.1f}s")
        if total_size:
            ui.label(f"Total Size: {total_size / (1024*1024):.1f} MB")
        projects = sorted({v.project for v in selected if v.project})
        if projects:
            ui.label(f"Projects: {', '.join(projects[:4])}{'…' if len(projects) > 4 else ''}")

    ui.separator().classes("my-2")
    ui.label("Export XML is available in the toolbar (next to Refresh).").classes("text-xs text-grey-6 mb-3")

    ui.button("Generate Proxies", icon="video_settings", on_click=lambda: _batch_generate_proxies(state)).props("size=md").classes("w-full mb-3")

    # Simplified batch edit UI (full version can be expanded later)
    with ui.column().classes("w-full gap-2 mb-2"):
        ui.label("Batch Edit Details").classes("text-caption text-grey-6 font-bold")
        ui.input("Camera", placeholder="Leave empty to keep existing").props("dense")
        ui.input("Location", placeholder="Leave empty to keep existing").props("dense")
        ui.input("Add Tags (comma separated)", placeholder="e.g. interview, b-roll").props("dense")
        ui.button("Apply to Selected", color="primary").props("size=sm").classes("w-full mt-2")


def _render_project_inspector(state, name):
    """Project inspector details (moved here)."""
    ui.label(f"Project: {name}").classes("text-h6 font-bold mb-2")
    ui.label("Rich project metadata, client links, and clip list will be fully rendered here in follow-up passes.").classes("text-xs text-grey-6")
    # Placeholder for _show_rich_project_dialog trigger etc.


# Action helpers
def _rebuild_action(state, v):
    if _rebuild_clip_previews_and_metadata(state, v):
        refresh_all_ui(state)
        ui.notify("Previews + metadata refreshed", color="positive")


def _generate_storyboard_now(v, state):
    # Simplified version
    ui.notify("Generating storyboard in background...", color="info")
    # Full implementation would go here


def _launch_ai_tags(v):
    # Moved into this file
    _launch_ai_tag_suggestions(v)


def _show_storyboard_dialog(video):
    """Now centralized in dialogs.py."""
    ui_dialogs.show_storyboard_dialog(video)


def create_right_drawer():
    from minicat.ui.app import create_right_drawer as orig
    orig()


# --- Workers integration (live updates) ---
def _register_inspector_for_job_updates():
    try:
        from minicat.core import workers as task_workers
        def _refresh_inspector_on_job_change():
            try:
                inspector_content.refresh()
            except Exception:
                pass
        task_workers.register_status_updater(_refresh_inspector_on_job_change)
    except Exception:
        pass

_register_inspector_for_job_updates()


# --- Moved small helpers (tightening bridges) ---
def _launch_ai_tag_suggestions(video):
    """Moved from app.py into inspector component."""
    from minicat.ui.app import get_state, refresh_all_ui, _show_rich_ai_tag_review_dialog
    from minicat.core.settings import get_gemini_api_key, get_gemini_model
    from minicat.ai.tag_suggester import suggest_tags_from_storyboard, suggest_tags_from_transcript
    import asyncio

    has_storyboard = bool(getattr(video, "storyboard_path", None))
    has_transcript = bool(getattr(video, "transcription_segments", None))

    if not has_storyboard and not has_transcript:
        ui.notify("AI tags require a storyboard (video) or transcription (audio/video)", color="warning")
        return

    api_key = get_gemini_api_key()
    if not api_key:
        ui.notify("Set your Gemini API key in Settings first", color="warning")
        return

    loading = ui.dialog()
    with loading, ui.card().classes("w-[320px]"):
        ui.label("Asking Gemini...").classes("text-center")
        ui.spinner(size="lg").classes("mx-auto mt-4")
    loading.open()

    async def run():
        try:
            if has_storyboard:
                suggestions = await asyncio.to_thread(
                    suggest_tags_from_storyboard,
                    video.storyboard_path, api_key, min_tags=3, max_tags=8,
                    model_name=get_gemini_model()
                )
            else:
                suggestions = await asyncio.to_thread(
                    suggest_tags_from_transcript,
                    video.transcription_segments, api_key, min_tags=3, max_tags=8,
                    model_name=get_gemini_model()
                )
            loading.close()
            if suggestions:
                _show_rich_ai_tag_review_dialog(video, suggestions)
            else:
                ui.notify("No suggestions from AI", color="warning")
        except Exception as e:
            try:
                loading.close()
            except Exception:
                pass
            ui.notify(f"AI failed: {e}", color="negative")

    asyncio.create_task(run())


def _rebuild_clip_previews_and_metadata(state, clip):
    """Moved batch/single rebuild helper."""
    from minicat.ui.app import refresh_all_ui
    from minicat.core import db, video
    from pathlib import Path

    if not clip.id:
        return False

    try:
        from minicat.cli.main import _is_audio_file
        is_audio = _is_audio_file(Path(clip.path))
    except Exception:
        is_audio = Path(clip.path).suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aiff", ".aif"}

    if is_audio:
        updates = {}
        try:
            meta = video.extract_metadata(clip.path)
            if meta.get("codec"):
                updates["codec"] = meta["codec"]
        except Exception:
            pass
        if updates:
            db.update_video_fields(state.catalog_root, clip.id, **updates)
        return True

    try:
        thumb, board = video.generate_previews(clip.path, state.catalog_root, clip.id)
        updates = {
            "thumbnail_path": str(thumb),
            "storyboard_path": str(board),
        }
        try:
            meta = video.extract_metadata(clip.path)
            if meta.get("codec"):
                updates["codec"] = meta["codec"]
        except Exception:
            pass

        db.update_video_fields(state.catalog_root, clip.id, **updates)
        return True
    except Exception as e:
        print(f"[Rebuild] Failed for {clip.filename}: {e}")
        return False


def create_right_drawer() -> None:
    """Thin wrapper so create_right_drawer can eventually move here too."""
    from minicat.ui.app import create_right_drawer as _old
    _old()
