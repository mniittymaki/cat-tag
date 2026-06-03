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
        with ui.column().classes("items-center justify-center h-full text-center flex flex-col gap-y-2"):
            ui.label("No catalog loaded").classes("text-h6 text-grey-6")
        return

    sel_count = len(state.selected_ids)

    if sel_count > 0:
        # Single clip inspector only for exactly 1 selected item.
        # Multi-clip view (batch operations, proxies, etc.) opens at 2+ selected clips.
        if sel_count == 1:
            if not state.selected:
                sel = state.get_selected_videos()
                state.selected = sel[0] if sel else None
            if not state.selected:
                return
            v = state.selected

            # Root container: p-0 m-0 gap-0 + side px-2/px-3 per spec (maximizes usable text width)
            with ui.column().classes("w-full flex flex-col gap-y-2"):
                # CLIPNAME
                ui.label(v.filename).classes("text-sm font-semibold mb-1")

                # Storyboard — MUST remain fully visible and prominent (sizing untouched)
                _render_storyboard_section(v, state)

                # Action buttons
                ui.button("Rebuild Previews + Metadata", on_click=lambda state=state, v=v: _rebuild_action(state, v), icon="refresh").props("size=sm outline").classes("w-full mt-1 mb-1")

                # === CLIP DETAILS (detailed form - migrated) ===
                _render_clip_details(v, state, refresh_all_ui)

                # === TECHNICAL INFO ===
                _render_technical_info(v, state)

                # === TRANSCRIBE ===
                _render_transcribe_section(v, state)

                # === EXPORT + DELETE BUTTONS - Very bottom of single Clip view ===
                ui.separator().classes("my-2")

                # Export button (above deletes, as requested)
                ui.label("Export").classes("text-xs font-semibold text-zinc-500 mb-1")
                ui.button(
                    "EXPORT",
                    icon="download",
                    color="primary",
                    on_click=lambda state=state, v=v: _show_single_clip_export_dialog(state, v)
                ).props("size=md").classes("w-full mb-2")

                # Safe action: only removes from CAT+TAG catalog/library
                ui.button(
                    "DELETE FROM LIBRARY",
                    icon="delete",
                    color="negative",
                    on_click=lambda state=state, v=v: _delete_single_from_library(state, v)
                ).props("size=md outline").classes("w-full").tooltip(
                    "Remove this clip from the CAT+TAG catalog only. The original media file will remain on your disk."
                )

                # Dangerous action: removes from catalog + deletes actual file from disk
                ui.button(
                    "DELETE FROM DISK",
                    icon="delete_forever",
                    color="negative",
                    on_click=lambda state=state, v=v: _delete_single_from_disk(state, v)
                ).props("size=md").classes("w-full mt-1").tooltip(
                    "Permanently delete the original media file from your disk AND remove it from the CAT+TAG library. This cannot be undone."
                )

        else:
            with ui.column().classes("w-full flex flex-col gap-y-2"):
                _render_multi_selection_panel(state)

    elif state.selected_project:
        with ui.column().classes("w-full flex flex-col gap-y-2"):
            _render_project_inspector(state, state.selected_project)
    else:
        with ui.column().classes("w-full flex flex-col gap-y-2 px-2 items-center justify-center h-full text-center"):
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
            ui.label("Storyboard ready").classes("text-xs text-blue-400 mt-0.5")
        else:
            ui.label("No storyboard yet").classes("text-xs text-grey-6 italic mb-0.5")
            ui.button("Generate", icon="image", on_click=lambda v=v, state=state: _generate_storyboard_now(v, state)).props("size=sm outline dense").classes("mt-0.5")
    except Exception as sb_err:
        print(f"[Inspector] Storyboard error: {sb_err}")


def _render_clip_details(v, state, refresh_all_ui_fn):
    """Detailed editable metadata form - migrated from original."""
    with ui.expansion("CLIP DETAILS", icon="edit", value=True).classes("w-full inspector-expansion q-py-none min-h-[32px]"):
        with ui.column().classes("w-full gap-0 p-0 m-0"):
            # Project
            project_input = ui.input(label="Project", value=v.project or "", placeholder="Project name").props("dense outlined square").classes("w-full text-xs q-my-none")

            # Clients (read-only for now)
            client_names = db.get_clients_for_project(state.catalog_root, v.project) if v.project else []
            client_display = ", ".join([c.name for c in client_names]) if client_names else "(No Client)"
            ui.input(label="Client(s)", value=client_display).props("readonly dense outlined square").classes("w-full text-xs q-my-none text-grey-5")

            # Location
            location_input = ui.input(label="Location", value=v.location or "", placeholder="Enter location").props("dense outlined square").classes("w-full text-xs q-my-none")

            # Date
            with ui.row().classes("items-end gap-1 w-full py-0.5"):
                date_input = ui.input(label="Date", value=str(v.shoot_date) if v.shoot_date else None, placeholder="Select date").props("dense outlined square").classes("flex-1 text-xs q-my-none")
                with ui.menu() as date_menu:
                    ui.date().bind_value(date_input, 'value')
                with date_input.add_slot('append'):
                    ui.icon('event', size='sm').classes('cursor-pointer').on('click', date_menu.open)
                def clear_date():
                    date_input.set_value(None)
                    _save_clip_details(state, v, project_input, location_input, date_input, None, None, None, None, None, None, None, None)
                ui.button(icon="close", on_click=clear_date).props("size=xs flat dense round").classes("mb-0")

            # More fields (camera, operator, lens, technical)
            camera_input = ui.input(label="Camera", value=v.camera or "", placeholder="Camera model").props("dense outlined square").classes("w-full text-xs q-my-none")
            operator_input = ui.input(label="Operator", value=v.operator or "", placeholder="Camera operator").props("dense outlined square").classes("w-full text-xs q-my-none")
            lens_input = ui.input(label="Lens", value=v.lens or "", placeholder="Lens").props("dense outlined square").classes("w-full text-xs q-my-none")

            iso_input = ui.input(label="ISO", value=str(v.iso) if v.iso else "", placeholder="ISO").props("dense outlined square").classes("w-full text-xs q-my-none")
            aperture_input = ui.input(label="Aperture", value=f"{v.f_number}" if v.f_number else "", placeholder="f/ number").props("dense outlined square").classes("w-full text-xs q-my-none")
            shutter_input = ui.input(label="Shutter Speed", value=v.shutter_speed or "", placeholder="1/xx").props("dense outlined square").classes("w-full text-xs q-my-none")
            focal_input = ui.input(label="Focal Length", value=f"{v.focal_length}" if v.focal_length else "", placeholder="mm").props("dense outlined square").classes("w-full text-xs q-my-none")
            wb_input = ui.input(label="White Balance", value=v.white_balance or "", placeholder="White Balance").props("dense outlined square").classes("w-full text-xs q-my-none")
            gamma_input = ui.input(label="Gamma", value=v.gamma or "", placeholder="Gamma").props("dense outlined square").classes("w-full text-xs q-my-none")

            # Tags (simplified for this pass)
            ui.label("Tags").classes("text-xs text-zinc-500 font-medium mt-1 mb-0.5")
            with ui.row().classes("flex-wrap gap-1 mb-1"):
                current_tags = getattr(v, "tags", None) or []
                for tag in current_tags:
                    ui.chip(tag, color="grey-8").props("size=sm")

            # Save button
            def save_clip_details():
                _save_clip_details(state, v, project_input, location_input, date_input,
                                   camera_input, operator_input, lens_input,
                                   iso_input, aperture_input, shutter_input, focal_input, wb_input, gamma_input)
            ui.button("Save Details", on_click=save_clip_details, color="primary").props("size=xs").classes("w-full mt-1")

            # AI Tags button (bridge)
            has_trans = bool(getattr(v, "transcription_segments", None))
            has_story = bool(getattr(v, "storyboard_path", None))

            is_audio_only = False
            try:
                from minicat.cli.main import _is_audio_file
                is_audio_only = _is_audio_file(Path(v.path))
            except Exception:
                pass

            if is_audio_only and not has_trans:
                btn_text = "Transcribe First to Enable AI Tags"
                btn_disabled = True
            else:
                btn_text = "Suggest Tags with AI"
                btn_disabled = False

            btn = ui.button(
                btn_text,
                icon="auto_awesome",
                on_click=lambda v=v: _launch_ai_tags(v),
                color="primary"
            ).props("size=xs outline").classes("w-full mt-1")

            if btn_disabled:
                btn.props("disable")

            # --- Transcription proxy audio status (processed .m4a for transcription + AI listening) ---
            try:
                from minicat.core.video import get_cached_audio_path

                audio_p = get_cached_audio_path(v.id, state.catalog_root)
                audio_exists = audio_p.exists()

                if audio_exists:
                    with ui.column().classes("w-full mt-2 pt-1 border-t border-grey-8 gap-0"):
                        with ui.row().classes("items-center justify-between py-0.5"):
                            ui.label("Cached Audio (proxy)").classes("text-xs text-zinc-500 font-medium")
                            with ui.row().classes("gap-1"):
                                ui.button("Clear", icon="delete", size="xs", color="negative",
                                          on_click=lambda state=state, v=v: _clear_single_audio(state, v)).props("dense padding=xs")
                                ui.button("Rebuild", icon="refresh", size="xs",
                                          on_click=lambda state=state, v=v, refresh_all_ui_fn=refresh_all_ui_fn: _rebuild_single_audio(state, v, refresh_all_ui_fn)).props("dense padding=xs")

                        size_mb = audio_p.stat().st_size / (1024*1024)
                        ui.label(f"🎙️ {size_mb:.1f} MB (24 kHz AAC proxy • transcription + AI tools)").classes("text-xs text-grey-4")
            except Exception:
                pass


def _save_clip_details(state, v, project_input, location_input, date_input,
                       camera_input=None, operator_input=None, lens_input=None,
                       iso_input=None, aperture_input=None, shutter_input=None,
                       focal_input=None, wb_input=None, gamma_input=None):
    """Helper to persist edited clip details to DB and refresh UI (including list view)."""
    updates = {}

    # Project
    if project_input and project_input.value.strip() != (v.project or ""):
        updates["project"] = project_input.value.strip() or None

    # Location
    if location_input and location_input.value.strip() != (v.location or ""):
        updates["location"] = location_input.value.strip() or None

    # Shoot date
    if date_input:
        new_date_str = date_input.value
        if new_date_str:
            try:
                from datetime import date as date_cls
                new_date = date_cls.fromisoformat(new_date_str)
                if new_date != v.shoot_date:
                    updates["shoot_date"] = new_date
            except Exception:
                ui.notify("Invalid date format", color="negative")
                return
        elif v.shoot_date is not None:
            updates["shoot_date"] = None

    # Simple string fields
    for field_name, inp in [
        ("camera", camera_input),
        ("operator", operator_input),
        ("lens", lens_input),
        ("white_balance", wb_input),
        ("gamma", gamma_input),
        ("shutter_speed", shutter_input),
    ]:
        if inp:
            val = inp.value.strip() or None
            if val != getattr(v, field_name, None):
                updates[field_name] = val

    # Numeric / special fields
    numeric_fields = [
        ("iso", iso_input, int),
        ("f_number", aperture_input, float),
        ("focal_length", focal_input, float),
    ]
    for field_name, inp, cast in numeric_fields:
        if inp:
            val = inp.value.strip()
            current = getattr(v, field_name, None)
            if val == "":
                if current is not None:
                    updates[field_name] = None
            else:
                try:
                    parsed = cast(val)
                    if parsed != current:
                        updates[field_name] = parsed
                except (ValueError, TypeError):
                    pass

    if updates:
        try:
            db.update_video_fields(state.catalog_root, v.id, **updates)
        except Exception as e:
            ui.notify(f"Failed to save: {e}", color="negative")
            return

        refreshed = db.get_video_by_path(state.catalog_root, v.path)
        if refreshed:
            state.selected = refreshed

        from minicat.ui.app import refresh_all_ui
        refresh_all_ui(state)
        ui.notify("Clip details saved", color="positive")
    else:
        ui.notify("No changes to save", color="info")


def _render_technical_info(v, state):
    with ui.expansion("TECHNICAL INFO", icon="info", value=True).classes("w-full inspector-expansion q-py-none min-h-[32px]"):
        from minicat.ui.app import format_duration_timecode

        tech = []
        if v.duration is not None:
            tech.append(("Duration", format_duration_timecode(v.duration, v.fps)))
        if v.bit_rate: tech.append(("Bitrate", f"{v.bit_rate // 1000} kbps"))
        if v.width and v.height: tech.append(("Resolution", f"{v.width}×{v.height}"))
        if v.fps: tech.append(("Framerate", f"{v.fps:.2f} fps"))
        lang = getattr(v, "original_language", None)
        tech.append(("Original Language", lang.upper() if lang else "—"))

        # Timecode fields (always shown so the labels are visible)
        tech.append(("TC Start", getattr(v, "tc_start", None) or "—"))
        tech.append(("TC End", getattr(v, "tc_end", None) or "—"))

        for label, value in tech:
            # High-density property row per spec: side-by-side pairs, muted label, mono value right aligned
            with ui.row().classes("flex row no-wrap justify-between items-center q-py-xs py-0.5"):
                ui.label(label).classes("text-xs text-zinc-500 font-medium")
                ui.label(value).classes("text-xs text-zinc-200 font-mono text-right truncate")


def _render_transcribe_section(v, state):
    with ui.expansion("TRANSCRIBE", icon="mic", value=True).classes("w-full inspector-expansion q-py-none min-h-[32px]"):
        # Live job status from the decoupled workers (Layer 2)
        try:
            from minicat.core import workers as task_workers
            jobs = task_workers.get_transcription_jobs()
            active_jobs = [j for j in jobs if j.clip_id == v.id and j.status in ("queued", "running")]
            if active_jobs:
                job = active_jobs[0]
                ui.label(f"⏳ {job.message} ({job.status})").classes("text-xs text-orange-400 mb-1")
            else:
                error_jobs = [j for j in jobs if j.clip_id == v.id and j.status == "error"]
                if error_jobs:
                    job = error_jobs[0]
                    ui.label(f"❌ Failed: {job.message}").classes("text-xs text-red-400 mb-1")
        except Exception:
            pass

        has_trans = bool(getattr(v, "transcription_segments", None))
        if has_trans:
            ui.label(f"✓ Transcription ready ({len(v.transcription_segments)} segments)").classes("text-xs text-green-400")
        else:
            ui.label("No transcription yet").classes("text-xs text-grey-5")

        def _queue_transcribe(v=v):
            try:
                from minicat.ui.app import queue_for_transcription
                queue_for_transcription(v, do_translate=False)
                inspector_content.refresh()
            except Exception as e:
                ui.notify(f"Queue failed: {e}", color="negative")

        ui.button("TRANSCRIBE AUDIO WITH AI", icon="mic", on_click=lambda: _queue_transcribe(), color="primary").props("size=md").classes("w-full")
        ui.button("AI Journalist Cut", icon="content_cut", color="primary", on_click=lambda v=v: _launch_ai_journalist_cut(v)).props("size=md").classes("w-full mt-1")

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

    ui.label("TRANSCRIPTION").classes("text-xs font-semibold tracking-wider text-zinc-500 mt-1 mb-0.5")

    # Language selector
    available_langs = ["original"]
    translations = getattr(v, "translated_transcriptions", {}) or {}
    available_langs += sorted(translations.keys())

    current_lang = getattr(v, "_current_transcription_lang", "original")
    if current_lang not in available_langs:
        current_lang = "original"

    lang_options = {"original": "Original"}
    orig_lang = getattr(v, "original_language", None)
    if orig_lang:
        lang_options["original"] = f"Original ({orig_lang})"

    for lang in sorted(translations.keys()):
        lang_options[lang] = lang.upper()

    lang_select = ui.select(
        options=lang_options,
        value=current_lang,
        label="Language",
    ).props("dense outlined square").classes("w-full text-xs q-my-none")

    def change_lang():
        v._current_transcription_lang = lang_select.value
        inspector_content.refresh()

    lang_select.on_value_change(change_lang)

    # Segments display
    if current_lang == "original":
        segments_to_show = v.transcription_segments
    else:
        segments_to_show = translations.get(current_lang, [])

    import re
    from minicat.core.video import format_transcript_timecode

    with ui.column().classes("max-h-[200px] overflow-auto border border-zinc-700 rounded p-2 text-xs"):
        for seg in segments_to_show or []:
            # Show real production timecode (from clip's embedded TC + offset) instead of raw media seconds.
            # Matches the format in the authoritative .txt sidecars (e.g. "10:03:20:18 - 10:03:21:00 text")
            start = seg.get("start") or seg.get("source_in", 0)
            end = seg.get("end") or seg.get("source_out", 0)

            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                try:
                    fps = getattr(v, "fps", None)
                    base_tc = getattr(v, "tc_start", None)
                    full_tc = format_transcript_timecode(start, end, fps=fps, base_timecode=base_tc)
                    # Convert "[TC (s) → TC (s)]" → "TC - TC"
                    m = re.search(r'\[([^\(]+)\s*\([^)]+\)\s*→\s*([^\(]+)\s*\([^)]+\)\]', full_tc)
                    if m:
                        time_str = f"{m.group(1).strip()} - {m.group(2).strip()}"
                    else:
                        time_str = full_tc.strip("[]").replace(" → ", " - ")
                except Exception:
                    time_str = f"{start:.2f} → {end:.2f}"
            else:
                time_str = str(start)

            text = seg.get("text", "")
            with ui.column().classes("mb-1 gap-0"):
                ui.label(time_str).classes("font-mono text-xs font-bold")
                if text:
                    ui.markdown(text).classes("text-xs pl-2")

    # Translate controls (on-demand)
    with ui.row().classes("items-end gap-1 mt-1"):
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
        ).props("dense outlined square").classes("flex-1 text-xs q-my-none")

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
                clip_dur = getattr(v, "duration", None)
                translated = await asyncio.to_thread(
                    translate_transcription_segments,
                    source_segments,
                    target,
                    api_key,
                    model_name=get_gemini_model(),
                    max_duration=clip_dur,
                )
                if not v.translated_transcriptions:
                    v.translated_transcriptions = {}
                v.translated_transcriptions[target.lower()] = translated
                v._current_transcription_lang = target.lower()

                # Persist to DB
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

                # Also save translated SRT persistently in the catalog (like previews)
                try:
                    from minicat.core.video import save_transcription_txt, save_transcription_srt
                    fps = getattr(v, "fps", None)
                    base_tc = getattr(v, "tc_start", None)
                    save_transcription_txt(v.id, state.catalog_root, translated, lang=target.lower(), fps=fps, base_timecode=base_tc)
                    save_transcription_srt(v.id, state.catalog_root, translated, lang=target.lower(), fps=fps, base_timecode=base_tc)
                except Exception as srt_ex:
                    print(f"[Transcriptions] Failed to save translated SRT for clip {v.id}: {srt_ex}")

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
    from minicat.ui.app import format_duration_timecode

    selected = state.get_selected_videos()
    if not selected:
        return

    with ui.column().classes('flex flex-col gap-y-3 w-full'):
        ui.button(
            "Rebuild Previews + Metadata",
            icon="refresh",
            on_click=lambda state=state: _batch_rebuild(state)
        ).props("size=md").classes("w-full")

        # === Transcription proxy audio management (persistent <catalog>/audio/ 0000XX.m4a) ===
        with ui.row().classes('flex flex-row flex-wrap justify-between items-stretch gap-x-1 w-full'):
            ui.button(
                "Clear Cached Audio",
                icon="delete_sweep",
                on_click=lambda state=state: _batch_clear_audio_cache(state),
                color="negative"
            ).props("size=sm outline").classes("flex-1 text-xs py-1").tooltip(
                "Delete the single transcription proxy audio (.m4a) for selected clips (24 kHz mono AAC 64k + -3dB peak norm, used by transcription + AI Journalist listening)."
            )
            ui.button(
                "Rebuild Cached Audio",
                icon="refresh",
                on_click=lambda state=state: _batch_rebuild_audio_cache(state),
            ).props("size=sm").classes("flex-1 text-xs py-1").tooltip(
                "Clear and re-extract the processed 24 kHz mono AAC 64k transcription proxy (-3dB peak norm + mono only) for selected clips."
            )
            ui.button(
                "Purge legacy .wav",
                icon="cleaning_services",
                on_click=lambda state=state: _purge_legacy_audio_caches(state),
            ).props("size=sm outline").classes("flex-1 text-xs py-1").tooltip(
                "Remove any old 16 kHz .wav files still sitting in the catalog audio/ folder from before the processed AAC proxy upgrade. Safe — does not touch current .m4a files or force re-extraction."
            )

        # New: Copy selected media files + generate relinked XML
        ui.button(
            "Copy Clips + XML to Folder",
            icon="folder_copy",
            on_click=lambda state=state: _copy_clips_and_xml(state)
        ).props("size=md outline").classes("w-full").tooltip(
            "Copy the original media files to a chosen folder and create an XML that references the copied files."
        )

    count = len(selected)
    total_duration = sum((v.duration or 0) for v in selected)
    total_size = sum((v.size or 0) for v in selected)

    ui.label(f"{count} clips selected").classes("text-sm font-semibold my-1")

    # Clip Information: compact list of selected items (per-clip details for multiview)
    # Defensive import for stale bytecode after heavy inspector.py refactoring
    from minicat.ui.app import format_duration_timecode
    with ui.scroll_area().classes("w-full max-h-[120px] border border-zinc-700 rounded p-1 text-xs my-1"):
        for v in selected[:30]:
            fn = (v.filename or "")[:42]
            dstr = format_duration_timecode(v.duration or 0, 25)
            ui.label(f"{fn}  •  {dstr}").classes("font-mono leading-tight py-0.5")

    with ui.column().classes("gap-0 text-xs my-1"):
        # Defensive import (stale .pyc / partial reload safety after the inspector refactor)
        from minicat.ui.app import format_duration_timecode
        dur_str = format_duration_timecode(total_duration, 25)
        ui.label(f"Total Duration: {dur_str}")

        if total_size:
            if total_size >= 1024**3:
                gb = total_size / 1024**3
                ui.label(f"Total Size: {gb:.2f} GB")
            else:
                mb = total_size / 1024**2
                ui.label(f"Total Size: {mb:.2f} MB")
        projects = sorted({v.project for v in selected if v.project})
        if projects:
            ui.label(f"Projects: {', '.join(projects[:4])}{'…' if len(projects) > 4 else ''}")

        # Cached audio summary (single file per clip)
        try:
            from minicat.core.video import get_cached_audio_path
            total_mb = 0.0
            cached_count = 0
            for vv in selected:
                if vv.id:
                    p = get_cached_audio_path(vv.id, state.catalog_root)
                    if p.exists():
                        total_mb += p.stat().st_size / (1024*1024)
                        cached_count += 1
            if cached_count:
                ui.label(f"🎙️ Transcription proxy audio: {cached_count}/{len(selected)} clips ({total_mb:.1f} MB)").classes("text-xs text-amber-400 mt-1")
        except Exception:
            pass

    # --- Multi-clip AI Director (build a story across the selected clips) ---
    # Only enable if every selected clip has a transcription (required for verbatim cuts).
    transcribed = [v for v in selected if getattr(v, "transcription_segments", None)]
    missing_trans = len(selected) - len(transcribed)

    def _launch_multi_journalist():
        if missing_trans > 0:
            ui.notify(f"{missing_trans} clip(s) have no transcription. Transcribe them first for AI Director.", color="warning")
            return
        try:
            from minicat.ui.components.dialogs import show_multi_ai_journalist_cut_dialog
            show_multi_ai_journalist_cut_dialog(selected)
        except Exception as ex:
            ui.notify(f"AI Director not available yet: {ex}", color="negative")
            print(f"[AI Director] {ex}")

    with ui.row().classes("w-full gap-1 my-2"):
        btn_label = f"AI Director — Build Story ({len(selected)} clips)" if missing_trans == 0 else "AI Director (transcribe all clips first)"
        ui.button(
            btn_label,
            icon="movie_edit",
            on_click=_launch_multi_journalist,
            color="primary" if missing_trans == 0 else "grey-7"
        ).props("size=md").classes("flex-1 text-xs py-1").tooltip(
            "Use the AI Director to intercut verbatim moments across all these clips into complete narrative versions."
        )

    # Batch Edit Details - with smart behavior for Camera/Location
    with ui.column().classes("w-full gap-y-1"):
        ui.label("Batch Edit Details").classes("block text-xs text-zinc-400 mt-1 mb-0.5")

        # Compute common values across selection
        cameras = {getattr(v, "camera", None) for v in selected if getattr(v, "camera", None)}
        locations = {getattr(v, "location", None) for v in selected if getattr(v, "location", None)}

        common_camera = list(cameras)[0] if len(cameras) == 1 else None
        common_location = list(locations)[0] if len(locations) == 1 else None

        # Camera: show value + lock if identical across all selected clips
        if common_camera:
            cam_input = ui.input("Camera", value=common_camera).props("dense outlined square readonly").classes("w-full my-1 text-xs")
            ui.label("Same camera on all selected clips (locked)").classes("block text-xs text-zinc-400 mt-1 mb-0.5")
        else:
            cam_input = ui.input("Camera", placeholder="Leave empty to keep existing").props("dense outlined square").classes("w-full my-1 text-xs")

        # Location: pre-fill common value if all selected clips share the same one
        loc_input = ui.input(
            "Location",
            value=common_location or "",
            placeholder="Leave empty to keep existing" if not common_location else ""
        ).props("dense outlined square").classes("w-full my-1 text-xs")

        tag_input = ui.input("Add Tags (comma separated)", placeholder="e.g. interview, b-roll").props("dense outlined square").classes("w-full my-1 text-xs")

        def apply_batch_edit():
            updates = {}
            cam_val = (cam_input.value or "").strip() if cam_input else ""
            loc_val = (loc_input.value or "").strip() if loc_input else ""
            tags_str = (tag_input.value or "").strip() if tag_input else ""

            # Only update camera if the field was editable (i.e. values differed)
            if cam_val and not common_camera:
                updates["camera"] = cam_val
            if loc_val:
                updates["location"] = loc_val

            updated_count = 0
            if updates:
                for v in selected:
                    if v.id:
                        db.update_video_fields(state.catalog_root, v.id, **updates)
                        updated_count += 1

            # Handle tags (additive)
            if tags_str:
                new_tags = [t.strip() for t in tags_str.split(",") if t.strip()]
                for v in selected:
                    if v.id:
                        existing = list(getattr(v, "tags", []) or [])
                        added = False
                        for t in new_tags:
                            if t not in existing:
                                existing.append(t)
                                added = True
                        if added:
                            db.set_video_tags(state.catalog_root, v.id, existing)
                            updated_count += 1

            if updated_count:
                try:
                    from minicat.ui.app import refresh_all_ui
                    refresh_all_ui(state)
                except Exception:
                    # Fallback if central refresh not available
                    state.reload()
                    inspector_content.refresh()
                    try:
                        from minicat.ui.app import main_content
                        main_content.refresh()
                    except Exception:
                        pass
                ui.notify(f"Updated {len(selected)} clips", color="positive")
            else:
                ui.notify("No changes applied", color="info")

        ui.button("Apply to Selected", color="primary", on_click=apply_batch_edit).props("size=sm").classes("w-full mt-1")

    # === EXPORT (above delete buttons, as requested) ===
    ui.separator().classes("my-2")

    ui.label("Export").classes("block text-xs text-zinc-400 mt-1 mb-0.5")
    ui.button(
        "EXPORT",
        icon="download",
        color="primary",
        on_click=lambda state=state: _show_export_dialog_for_multi(state)
    ).props("size=md").classes("w-full my-1")

    # Safe action: only removes from CAT+TAG catalog/library
    ui.button(
        "DELETE FROM LIBRARY",
        icon="delete",
        color="negative",
        on_click=lambda state=state: _delete_from_library(state)
    ).props("size=md outline").classes("w-full my-1").tooltip(
        "Remove the selected clips from the CAT+TAG catalog only. The original media files will remain on your disk."
    )

    # Dangerous action: removes from catalog + deletes actual files from disk
    ui.button(
        "DELETE FROM DISK",
        icon="delete_forever",
        color="negative",
        on_click=lambda state=state: _delete_from_disk(state)
    ).props("size=md").classes("w-full my-1").tooltip(
        "Permanently delete the original media files from your disk AND remove them from the CAT+TAG library. This cannot be undone."
    )


# --- Helper for the new "Copy Clips + XML" feature ---
def _copy_clips_and_xml(state):
    """Thin wrapper so we can keep heavy logic in app.py."""
    from minicat.ui.app import _copy_selected_clips_with_xml
    _copy_selected_clips_with_xml(state)


def _show_export_dialog_for_multi(state):
    """Thin wrapper for the general multi-clip EXPORT dialog (quality, timecode, subtitles)."""
    from minicat.ui.app import _show_multi_clip_export_dialog
    _show_multi_clip_export_dialog(state)


def _render_project_inspector(state, name):
    """Project inspector details (moved here)."""
    with ui.column().classes("w-full flex flex-col gap-y-2"):
        ui.label(f"Project: {name}").classes("text-sm font-semibold mb-1")
        ui.label("Rich project metadata, client links, and clip list will be fully rendered here in follow-up passes.").classes("text-xs text-grey-6")
        # Placeholder for _show_rich_project_dialog trigger etc.


# Action helpers
def _rebuild_action(state, v):
    from minicat.ui.app import refresh_all_ui
    from minicat.core import db

    if _rebuild_clip_previews_and_metadata(state, v):
        # Re-fetch the clip so the inspector immediately shows new TC / metadata
        refreshed = db.get_video_by_path(state.catalog_root, v.path)
        if refreshed:
            state.selected = refreshed
        refresh_all_ui(state)
        ui.notify("Previews + metadata refreshed", color="positive")


def _generate_storyboard_now(v, state):
    # Simplified version
    ui.notify("Generating storyboard in background...", color="info")
    # Full implementation would go here


def _launch_ai_tags(v):
    # Moved into this file
    _launch_ai_tag_suggestions(v)


def _launch_ai_journalist_cut(v):
    """Launch the AI Journalist Cut dialog from the inspector."""
    ui_dialogs.show_ai_journalist_cut_dialog(v)


def _delete_from_library(state):
    """Safe: only removes clips from the CAT+TAG catalog (files stay on disk)."""
    from minicat.ui.app import _batch_delete_selected
    _batch_delete_selected(state)


def _delete_from_disk(state):
    """Dangerous: removes from catalog AND permanently deletes the actual media files from disk."""
    from minicat.ui.app import _batch_delete_media_and_disk
    _batch_delete_media_and_disk(state)


def _batch_clear_audio_cache(state):
    """Clear persistent transcription proxy audio (.m4a) for selection."""
    from minicat.ui.app import _batch_clear_audio_cache as _real
    _real(state)


def _batch_rebuild_audio_cache(state):
    """Rebuild (clear + re-extract) the processed transcription proxy audio for selection."""
    from minicat.ui.app import _batch_rebuild_audio_cache as _real
    _real(state)


def _purge_legacy_audio_caches(state):
    """Purge legacy .wav files (pre AAC proxy upgrade) from the catalog audio/ folder."""
    from minicat.ui.app import _purge_legacy_audio_wavs as _real
    _real(state)


def _batch_rebuild(state):
    """Rebuild previews + metadata for the current multi-selection (delegates to canonical impl)."""
    from minicat.ui.app import _batch_rebuild_previews_and_metadata
    _batch_rebuild_previews_and_metadata(state)


def _delete_single_from_library(state, v):
    """Safe single-clip version: only removes from CAT+TAG catalog."""
    from minicat.ui.app import _delete_from_library_single
    _delete_from_library_single(state, v)


def _delete_single_from_disk(state, v):
    """Dangerous single-clip version: removes from catalog + deletes file from disk (with strong confirmation)."""
    from minicat.ui.app import _delete_from_disk_single
    _delete_from_disk_single(state, v)


def _show_single_clip_export_dialog(state, v):
    """Export dialog for single clip (Clio view)."""
    from minicat.ui.app import _show_single_clip_export_dialog as _impl
    _impl(state, v)


def _clear_single_audio(state, v):
    """Clear the single transcription proxy audio file for a clip."""
    if not v.id:
        return
    from minicat.core.video import clear_cached_audio
    deleted = clear_cached_audio(v.id, state.catalog_root)
    try:
        inspector_content.refresh()
    except Exception:
        pass
    ui.notify(f"Cleared cached audio file" if deleted else "No cached audio to clear", color="positive")


def _rebuild_single_audio(state, v, refresh_all_ui_fn):
    """Rebuild the single processed transcription proxy audio for a clip."""
    if not v.id:
        return
    from minicat.core.video import rebuild_cached_audio_for_clip
    try:
        ok = rebuild_cached_audio_for_clip(v.path, v.id, state.catalog_root)
        try:
            inspector_content.refresh()
        except Exception:
            pass
        ui.notify("Rebuilt transcription proxy audio for this clip" if ok else "Audio proxy rebuild failed", color="positive" if ok else "negative")
    except Exception as ex:
        ui.notify(f"Audio rebuild failed: {ex}", color="negative")


def _show_storyboard_dialog(video):
    """Now centralized in dialogs.py."""
    ui_dialogs.show_storyboard_dialog(video)


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

    # Capture the current NiceGUI client so we can safely create UI from the background task
    client = ui.context.client

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

            with client:
                loading.close()
                if suggestions:
                    _show_rich_ai_tag_review_dialog(video, suggestions)
                else:
                    ui.notify("No suggestions from AI", color="warning")
        except Exception as e:
            try:
                with client:
                    loading.close()
            except Exception:
                pass
            try:
                with client:
                    ui.notify(f"AI failed: {e}", color="negative")
            except Exception:
                pass

    asyncio.create_task(run())


def _rebuild_clip_previews_and_metadata(state, clip):
    """Moved batch/single rebuild helper."""
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
            # Timecode (tc_start / tc_end) is refreshed as part of the standard Rebuild
            if meta.get("tc_start"):
                updates["tc_start"] = meta["tc_start"]
            if meta.get("tc_end"):
                updates["tc_end"] = meta["tc_end"]
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
            # Timecode (tc_start / tc_end) is refreshed as part of the standard Rebuild
            if meta.get("tc_start"):
                updates["tc_start"] = meta["tc_start"]
            if meta.get("tc_end"):
                updates["tc_end"] = meta["tc_end"]
        except Exception:
            pass

        db.update_video_fields(state.catalog_root, clip.id, **updates)
        return True
    except Exception as e:
        print(f"[Rebuild] Failed for {clip.filename}: {e}")
        return False
