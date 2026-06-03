"""
CAT+TAG NiceGUI Application

Three-column layout inspired by the design spec:
- Left drawer: Taxonomy / Filters (Projects, Cameras, Locations, Tags)
- Center: Media grid with thumbnails
- Right drawer: Inspector (Storyboard + editable structured metadata)

Run with:
    uv run minicat open /path/to/catalog
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
from datetime import datetime
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from nicegui import app, ui

from minicat.ai.tag_suggester import (
    suggest_tags_from_storyboard,
    suggest_tags_from_transcript,
)
from minicat.ai.transcriber import (
    ai_journalist_cut_to_srt_segments,
    segments_to_srt,
    transcribe_audio_with_timestamps,
    translate_transcription_segments,
)
from minicat.ai.journalist_cutter import generate_journalist_cuts
from minicat.ai.director import (
    generate_director_cuts,
    build_combined_labeled_transcript,
    validate_and_normalize_versions,
    get_narrative_sequence,
)
from minicat.ai.voiceover import (
    generate_narration_audio,
    generate_narration_audio_sync,
    get_voice_for_language,
    get_language_display_name,
    get_tts_provider,
    get_google_voices_for_language,
    get_piper_voices_for_language,
    get_tts_provider_display_name,
    ensure_google_tts_package,
    is_google_tts_available,
    get_google_tts_status,
    get_local_tts_status,
    get_tts_status,
    run_gcloud_auth_application_default,
    is_gcloud_available,
    find_gcloud,
    is_piper_available,
    ensure_piper_package,
    reset_piper_install_flag,
    ensure_piper_voice_model,
)
from minicat.core.settings import SUPPORTED_LANGUAGES
# generate_xmeml is re-exported from fcpxml_exporter for compatibility;
# the real implementation lives in xmeml_exporter.py (strict Premiere schema).
from minicat.ai.xmeml_exporter import generate_xmeml, get_media_start_offset_and_duration
from minicat.ai import narrative_vo_exporter
from minicat.ai.multi_xmeml_exporter import prepare_director_sources, get_audio_characteristics
from minicat.ai import multi_xmeml_exporter
from minicat.core import config, db, settings, video

# Layer 2: Background task controllers (extracted)
from minicat.core import workers as task_workers

# Layer 1: Component extractions
from minicat.ui.components import drawers as ui_drawers
from minicat.ui.components import inspector as ui_inspector
from minicat.ui.components import dialogs as ui_dialogs

# Language-specific test sentences for the "Test Current Voice" button in Text to Speech settings.
# The test sentence is always chosen to match the currently selected voiceover language.
TTS_TEST_PHRASES = {
    "en": "This is a test of the current text-to-speech settings. The quick brown fox jumps over the lazy dog.",
    "fi": "Tämä on testi nykyisistä tekstistä puheeksi -asetuksista. Nopea ruskea kettu hyppää laiskan koiran yli.",
    "fr": "Ceci est un test des paramètres actuels de synthèse vocale. Le renard brun rapide saute par-dessus le chien paresseux.",
    "de": "Dies ist ein Test der aktuellen Text-zu-Sprache-Einstellungen. Der schnelle braune Fuchs springt über den faulen Hund.",
    "es": "Esta es una prueba de la configuración actual de texto a voz. El rápido zorro marrón salta sobre el perro perezoso.",
    "sv": "Detta är ett test av de aktuella inställningarna för text till tal. Den snabba bruna räven hoppar över den lata hunden.",
}

# Localized labels for the rich exported TXT script/story files.
# The goal: the *entire* TXT (headers, section titles, field labels like "Text:", the full transcript section,
# notes, etc.) must be in the same language as the transcripted/scripted language used for the AI run
# (the lang of the sidecar content that the "Text" / selected beats quote).
# "en" for English transcripts/narration; "fi" when the user chose Finnish (or original fi material).
SCRIPT_LABELS = {
    "en": {
        "ai_journalist_cut": "AI JOURNALIST CUT",
        "source_file": "Source File:",
        "exported": "Exported:",
        "version": "Version:",
        "duration": "Duration:",
        "editorial_summary": "EDITORIAL SUMMARY",
        "narration_voiceover_script": "NARRATION / VOICEOVER SCRIPT",
        "selected_segments": "SELECTED SEGMENTS",
        "text_label": "Text:",
        "reason_label": "Reason:",
        "full_transcript": "FULL TRANSCRIPT (for reference)",
        # Director
        "ai_director_multi": "AI DIRECTOR — MULTI-CLIP SCRIPT",
        "narrative_script": "NARRATIVE SCRIPT (spoken clips + AI narration bridges)",
        "narration_explain_line1": "Narration bridges (🎙️) were generated because 'Generate narration / voiceover'",
        "narration_explain_line2": "was enabled. They are shown here in the order they appear between clips.",
        "narration_explain_line3": "These bridges are also available as voiceover audio or on-screen titles",
        "narration_explain_line4": "via the XML export options (only when narration was enabled).",
        "selected_content": "SELECTED CONTENT (with original clip attribution)",
        "note_xml_line1": "Note: For creative multi-source Director cuts, all source files are",
        "note_xml_line2": "treated as starting at 00:00:00:00 in the exported XMEML for reliable",
        "note_xml_line3": "relinking in Premiere. The times below show the original media time",
        "note_xml_line4": "within each source file.",
        "text_label_dir": "Text:",
        "why_chosen": "Why chosen:",
        "why_chosen_default": "Selected for its contribution to the overall narrative arc across sources.",
        "narration_bridge": "   🎙️ NARRATION BRIDGE (AI generated):",
        "bridge_note": "      (These bridges can be rendered as voiceover audio or text titles in the XML exports.)",
    },
    "fi": {
        "ai_journalist_cut": "AI JOURNALISTIN LEIKKAUS",
        "source_file": "Lähdetiedosto:",
        "exported": "Viety:",
        "version": "Versio:",
        "duration": "Kesto:",
        "editorial_summary": "TOIMITUKSELLINEN YHTEENVETO",
        "narration_voiceover_script": "KERRONTA / VOICEOVER-KÄSIKIRJOITUS",
        "selected_segments": "VALITUT SEGMENTIT",
        "text_label": "Teksti:",
        "reason_label": "Perustelu:",
        "full_transcript": "KOKO TRANSSKRIPTIO (viitteeksi)",
        # Director
        "ai_director_multi": "AI OHJAAJA — MONIKLIPPIKÄSIKIRJOITUS",
        "narrative_script": "KERRONTAKÄSIKIRJOITUS (puhutut klipit + AI-kerrontasillat)",
        "narration_explain_line1": "Kerrontasillat (🎙️) generoitiin, koska 'Luo kerronta / voiceover' oli valittuna.",
        "narration_explain_line2": "Ne näytetään tässä järjestyksessä, jossa ne esiintyvät klippien välissä.",
        "narration_explain_line3": "Nämä sillat ovat saatavilla myös voiceover-ääninä tai ruututeksteinä",
        "narration_explain_line4": "XML-viennissä (vain kun kerronta oli käytössä).",
        "selected_content": "VALITUT SISÄLLÖT (alkuperäisten klippien attribuutioin)",
        "note_xml_line1": "Huom: Luovissa monilähteisissä AI-ohjaajan leikkauksissa kaikki lähdetiedostot",
        "note_xml_line2": "käsitellään alkavan 00:00:00:00:sta XMEML:ssä luotettavaa uudelleenlinkitystä varten",
        "note_xml_line3": "Premieressä. Alla olevat ajat näyttävät kunkin lähteen alkuperäisen mediatiedoston",
        "note_xml_line4": "sisäisen ajan.",
        "text_label_dir": "Teksti:",
        "why_chosen": "Miksi valittu:",
        "why_chosen_default": "kokonaisnarratiivin kaaren tukemiseksi lähteiden yli.",
        "narration_bridge": "   🎙️ KERRONTASILTA (AI generoima):",
        "bridge_note": "      (Nämä sillat voidaan renderöidä voiceover-ääninä tai teksteinä XML-viennissä.)",
    },
}

def get_script_labels(lang: str | None) -> dict:
    """Return the label dict for the rich TXT script/story based on the transcripted/scripted language.
    Uses Finnish labels when the chosen language is 'fi' (or equivalent); English otherwise.
    This ensures the *whole file* (not just the AI content) matches the language of the transcript
    used for the AI Journalist/Director scripting.
    """
    if not lang:
        return SCRIPT_LABELS["en"]
    l = str(lang).lower().strip()
    if l in ("fi", "finnish"):
        return SCRIPT_LABELS["fi"]
    # For "original" we leave English labels (content will be original lang, but structure English is conventional
    # and we may not know the original's language here). If caller passes the resolved original_language (e.g. "fi")
    # it will pick Finnish labels.
    return SCRIPT_LABELS["en"]


from minicat.core.models import Client, Project, SearchFilters, Video
from minicat.core.settings import (
    DEFAULT_GEMINI_MODEL,
    GEMINI_MODELS,
    PROXY_PRESETS,
    get_gemini_api_key,
    get_gemini_model,
    get_preference,
    get_proxy_default_burn_subtitles,
    get_proxy_default_burn_timecode,
    get_proxy_default_preset,
    get_default_export_directory,
    set_default_export_directory,
    create_export_subfolder,
    DEFAULT_EXPORT_DIRECTORY,
    set_gemini_api_key,
    set_gemini_model,
    set_preference,
    set_proxy_default_burn_subtitles,
    set_proxy_default_burn_timecode,
    set_proxy_default_preset,
)
from minicat.core.video import (
    export_fcp7_xml,
    export_ai_journalist_cut_audio,
    export_ai_journalist_cut_video,
    get_available_subtitle_languages,
)

# ---------------------------------------------------------------------------
# Global-ish state for the current session (single catalog per app instance)
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self, catalog_root: Path):
        self.catalog_root = catalog_root
        self.videos: list[Video] = []
        self.selected: Video | None = None       # Focused clip (shown in inspector)
        self.selected_ids: set[int] = set()      # Multi-selection for batch ops / export
        self.selected_project: str | None = None # Focused project (shown in inspector)
        self.view_mode: str = "grid"             # "grid" or "list"
        self.media_filter: str = "all"           # "all" | "video" | "audio"  (top bar view toggle)

        # List view column customization (user can reorder + hide columns by dragging in settings)
        self.list_column_order: list[str] = []
        self.hidden_list_columns: set[str] = set()

        self.filters = SearchFilters()
        self.all_projects: list[str] = []
        self.all_clients: list[str] = []   # New
        self.all_cameras: list[str] = []
        self.all_locations: list[str] = []
        self.all_tags: list[str] = []

        # Sort options for media view (grid + list)
        self.sort_field: str = "shoot_date"   # filename, shoot_date, import_date, duration
        self.sort_desc: bool = True

        # Raw results from DB (before view filters like media type)
        self._raw_videos: list[Video] = []

    def reload(self) -> None:
        """Reload everything from DB (call after mutations)."""
        self.all_projects = db.get_distinct_values(self.catalog_root, "project")
        self.all_clients = [c.name for c in db.get_clients(self.catalog_root)]  # New
        self.all_cameras = db.get_distinct_values(self.catalog_root, "camera")
        self.all_locations = db.get_distinct_values(self.catalog_root, "location")
        self.all_tags = [t.name for t in db.get_all_tags(self.catalog_root)]

        # Apply current filters
        self._raw_videos = db.search_videos(self.catalog_root, self.filters, limit=2000)  # higher limit so transcribed/processed clips stay visible in large libraries; use filters/search for very big catalogs

        # Apply user-selected sort + media view filter
        self.apply_sort()

    def set_filter_text(self, text: str | None) -> None:
        self.filters.text = text or None
        self.reload()

    def toggle_project(self, project: str) -> None:
        current = self.filters.project or []
        if project in current:
            current = [p for p in current if p != project]
        else:
            current = current + [project]
        self.filters.project = current or None
        self.reload()
        refresh_all_ui(self)

    def toggle_client(self, client_name: str) -> None:
        """Toggle filtering by a client (shows all its projects)."""
        current = self.filters.client or []
        if client_name in current:
            current = [c for c in current if c != client_name]
        else:
            current = current + [client_name]
        self.filters.client = current or None
        self.reload()
        refresh_all_ui(self)

    def toggle_camera(self, camera: str) -> None:
        current = self.filters.camera or []
        if camera in current:
            current = [c for c in current if c != camera]
        else:
            current = current + [camera]
        self.filters.camera = current or None
        self.reload()
        refresh_all_ui(self)

    def toggle_location(self, location: str) -> None:
        current = self.filters.location or []
        if location in current:
            current = [loc for loc in current if loc != location]
        else:
            current = current + [location]
        self.filters.location = current or None
        self.reload()
        refresh_all_ui(self)

    def toggle_tag(self, tag: str) -> None:
        current = self.filters.tags or []
        if tag in current:
            current = [t for t in current if t != tag]
        else:
            current = current + [tag]
        self.filters.tags = current or None
        self.reload()
        refresh_all_ui(self)   # ensure toolbar, drawers, everything stays consistent including current sort mode

    def clear_filters(self) -> None:
        self.filters = SearchFilters()
        self.reload()
        refresh_all_ui(self)

    # --- Project inspector selection (similar to clips) ---
    def select_project(self, name: str) -> None:
        self.selected_project = name
        # Clear clip selection when focusing a project
        self.selected_ids.clear()
        self.selected = None
        # Refresh main grid visuals + inspector + drawer (so clip rings disappear, project panel shows)
        self._refresh_selection_visuals()
        ui.update()

    def clear_project_selection(self) -> None:
        self.selected_project = None
        self._refresh_selection_visuals()
        ui.update()

    # ------------------------------------------------------------------
    # Multi-selection support (for batch actions + XML export)
    # ------------------------------------------------------------------
    def is_selected(self, video: Video) -> bool:
        return bool(video.id and video.id in self.selected_ids)

    def toggle_select(self, video: Video) -> None:
        """Toggle a clip in the multi-selection set.
        Work (state mutation + UI refresh of grid cards, toolbar count, inspector, drawer)
        is deferred so clicking checkboxes never blocks the UI.
        """
        if not video.id:
            return

        def _do_toggle():
            if video.id in self.selected_ids:
                self.selected_ids.remove(video.id)
                if self.selected and self.selected.id == video.id:
                    self.selected = None
            else:
                self.selected_ids.add(video.id)
                self.selected = video
            self._refresh_selection_visuals()

        ui.timer(0.02, _do_toggle, once=True)

    def set_single_selection(self, video: Video) -> None:
        """Direct single selection (also clears any prior multi-selection).
        We defer the grid + inspector refresh by one tick so the click
        feels responsive even on very large 4K files. Grid re-render ensures
        all other cards lose their selection ring/checkbox state.
        """
        if not video.id:
            return
        self.selected_ids = {video.id}
        self.selected = video

        def _refresh_inspector():
            self._refresh_selection_visuals()

        # Very short defer makes the selection highlight feel instant
        # while the heavy form builds in the background.
        ui.timer(0.01, _refresh_inspector, once=True)

    def clear_selection(self) -> None:
        self.selected_ids.clear()
        self.selected = None
        self._refresh_selection_visuals()
        ui.update()

    def _update_right_drawer(self):
        """Show or hide the right drawer depending on current selection state."""
        global RIGHT_DRAWER
        if RIGHT_DRAWER is not None:
            has_selection = bool(self.selected_ids or self.selected_project or self.selected)
            RIGHT_DRAWER.set_value(has_selection)
            ui.update(RIGHT_DRAWER)

    def _refresh_selection_visuals(self) -> None:
        """Central helper: after mutating selected_ids / selected, refresh everything
        that depends on the visual selection state (grid cards, list table "☑" symbols,
        toolbar "N selected" count, inspector multi/single panel, right drawer visibility).
        """
        try:
            main_content.refresh()
            ui_inspector.inspector_content.refresh()
            self._update_right_drawer()
        except Exception as ex:
            print(f"[selection] refresh error: {ex}")

    def set_view_mode(self, mode: str) -> None:
        if mode in ("grid", "list"):
            self.view_mode = mode
            main_content.refresh()

    def set_media_filter(self, mode: str) -> None:
        """Top bar view filter: restrict visible library to Video / Audio / All."""
        if mode in ("all", "video", "audio"):
            self.media_filter = mode
            self.apply_sort()
            main_content.refresh()

    def set_sort(self, field: str, desc: bool | None = None):
        """Change the sort order for the media grid/list."""
        valid = {"filename", "shoot_date", "import_date", "duration"}
        if field in valid:
            self.sort_field = field
        if desc is not None:
            self.sort_desc = desc

        self.apply_sort()
        refresh_all_ui(self)  # full consistency (toolbar + grid + sidebars) after sort change

    def apply_sort(self):
        """Sort + apply the current top-bar media view filter (Video / Audio / All)."""
        # Always start from the full raw DB result for this view session
        source = getattr(self, "_raw_videos", None)
        if source is None:
            source = self.videos or []

        if not source:
            self.videos = []
            return

        items = list(source)

        def key_func(v: Video):
            if self.sort_field == "filename":
                return (v.filename or "").lower()
            if self.sort_field == "shoot_date":
                return v.shoot_date or date.min
            if self.sort_field == "import_date":
                return getattr(v, "import_date", None) or datetime.min
            if self.sort_field == "duration":
                return v.duration or 0
            return (v.filename or "").lower()

        items.sort(key=key_func, reverse=self.sort_desc)

        # Apply the top-bar "View" media type filter (Video / Audio / All)
        if self.media_filter != "all":
            try:
                from minicat.cli.main import _is_audio_file
                is_audio_fn = _is_audio_file
            except Exception:
                def is_audio_fn(p: Path) -> bool:
                    return p.suffix.lower() in {
                        ".wav", ".wave", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga",
                        ".aiff", ".aif", ".aifc", ".wma", ".opus",
                    }

            if self.media_filter == "audio":
                items = [v for v in items if is_audio_fn(Path(v.path))]
            else:  # "video"
                items = [v for v in items if not is_audio_fn(Path(v.path))]

        self.videos = items

    def get_selected_videos(self) -> list[Video]:
        """Return the currently multi-selected videos.

        Robust version: always returns the full selection even if some clips
        are no longer in the current filtered `self.videos` result (e.g. after
        changing filters or hitting the 500 result limit).
        """
        id_to_video = {v.id: v for v in self.videos if v.id is not None}
        result: list[Video] = []
        missing_ids: list[int] = []

        for vid in self.selected_ids:
            if vid in id_to_video:
                result.append(id_to_video[vid])
            else:
                missing_ids.append(vid)

        if missing_ids:
            # Fetch the rest from DB so batch dialogs always see the true selection count
            fetched = db.get_videos_by_ids(self.catalog_root, missing_ids)
            result.extend(fetched)

        return result

    def select_video(self, video: Video) -> None:
        # Back-compat: treat as single selection (used by some older paths)
        self.set_single_selection(video)

    def save_video_fields(self, **kwargs: Any) -> None:
        if not self.selected or not self.selected.id:
            return
        db.update_video_fields(self.catalog_root, self.selected.id, **kwargs)
        self.selected = db.get_video_by_path(self.catalog_root, self.selected.path)
        self.reload()
        main_content.refresh()
        ui_inspector.inspector_content.refresh()


# Global state (set when the app starts)
STATE: AppState | None = None
CONTENT: ui.element | None = None   # Persistent container for all dynamic content
APP_MODE: str = "welcome"           # "welcome" or "app"

# Reference to the right drawer element so we can show/hide it dynamically
RIGHT_DRAWER: Any = None


def get_state() -> AppState | None:
    return STATE


@ui.refreshable
def main_content() -> None:
    """Central content area only.
    IMPORTANT: This must NEVER contain top-level layout elements
    (header, left_drawer, right_drawer, footer). Those must live
    directly under the @ui.page.
    """
    if APP_MODE == "welcome" or STATE is None:
        _render_welcome_screen()
    else:
        # Only the dynamic central area (grid or list + local footer if desired)
        with ui.element("div").classes("w-full q-pa-none"):
            state = get_state()
            try:
                if state and state.view_mode == "list":
                    create_media_list()
                else:
                    create_media_grid()
            except Exception as render_err:
                import traceback
                traceback.print_exc()
                print(f"[Render] Error in central content: {render_err}")
                with ui.column().classes("w-full p-8 text-center"):
                    ui.label("Error rendering media view").classes("text-h6 text-red")
                    ui.label(str(render_err)).classes("text-sm text-grey-6 mt-2")
                    ui.button("Try Grid View", on_click=lambda: (setattr(state, 'view_mode', 'grid'), main_content.refresh())).classes("mt-4")
                    ui.button("Reload Page", on_click=lambda: ui.navigate.reload()).classes("mt-2")


# Re-export the refreshable version from the component so existing .refresh() calls continue to work
inspector_content = ui_inspector.inspector_content


def _render_project_inspector(state: AppState, name: str) -> None:
    """Renders rich project details in the right inspector (similar to clip inspector)."""
    proj = db.get_project_with_stats(state.catalog_root, name)

    ui.label(f"Project: {proj.name}").classes("text-h5 font-bold mb-2")

    # Computed stats
    with ui.row().classes("gap-2 text-xs mb-1"):
        ui.label(f"{proj.clip_count} clips")
        dur_str = format_duration_timecode(proj.total_duration)
        ui.label(f"{dur_str} total")

    # Editable fields - high density form controls
    with ui.column().classes("gap-0 p-0 m-0"):
        start = ui.input("Start Date", value=str(proj.start_date) if proj.start_date else "").props("dense outlined square").classes("w-full text-xs q-my-none")
        end = ui.input("End Date", value=str(proj.end_date) if proj.end_date else "").props("dense outlined square").classes("w-full text-xs q-my-none")
        client = ui.input("Client", value=proj.client or "").props("dense outlined square").classes("w-full text-xs q-my-none")
        director = ui.input("Director", value=proj.director or "").props("dense outlined square").classes("w-full text-xs q-my-none")
        producer = ui.input("Producer", value=proj.producer or "").props("dense outlined square").classes("w-full text-xs q-my-none")
        editor = ui.input("Editor", value=proj.editor or "").props("dense outlined square").classes("w-full text-xs q-my-none")
        ops = ui.input("Camera Operators", value=", ".join(proj.camera_operators)).props("dense outlined square").classes("w-full text-xs q-my-none")
        loc = ui.input("Location", value=proj.location or "").props("dense outlined square").classes("w-full text-xs q-my-none")

        status = ui.select(
            ["Pre-production", "Production", "Post-production", "Delivered", "Archived"],
            value=proj.status,
            label="Status"
        ).props("dense outlined square").classes("w-full text-xs q-my-none")

        notes = ui.textarea("Notes", value=proj.notes or "").props("dense outlined square").classes("w-full text-xs q-my-none")

    def save_project():
        proj.start_date = date.fromisoformat(start.value) if start.value else None
        proj.end_date = date.fromisoformat(end.value) if end.value else None
        proj.client = client.value.strip() or None
        proj.director = director.value.strip() or None
        proj.producer = producer.value.strip() or None
        proj.editor = editor.value.strip() or None
        proj.camera_operators = [x.strip() for x in ops.value.split(",") if x.strip()]
        proj.location = loc.value.strip() or None
        proj.status = status.value
        proj.notes = notes.value.strip() or None

        db.create_or_update_project(state.catalog_root, proj)
        state.all_projects = db.get_distinct_values(state.catalog_root, "project")
        refresh_all_ui(state)
        ui.notify("Project saved", color="positive")

    with ui.row().classes("gap-1 mt-1"):
        ui.button("Save Project", on_click=save_project, color="primary").props("size=sm")
        ui.button("Close", on_click=state.clear_project_selection).props("size=sm outline")

    ui.separator().classes("my-4")
    ui.label("Tip: Use the left sidebar to filter clips by this project.").classes("text-xs text-grey-6")


async def _batch_generate_proxies(state: AppState) -> None:
    """Generate proxies using the new official CAT+TAG 5-profile system."""
    selected = state.get_selected_videos()
    if not selected:
        return

    from minicat.core import settings as core_settings

    with ui.dialog() as settings_dialog, ui.card().classes("w-[560px]"):
        ui.label("Generate Proxies").classes("text-h6 mb-1")
        ui.label("Choose a profile. Each has fixed resolution, codec, and container optimized for its use case.").classes("text-xs text-grey-6 mb-3")

        preset = ui.select(
            core_settings.PROXY_PRESETS,
            value=core_settings.get_proxy_default_preset(),
            label="Proxy Profile"
        ).props("dense")

        with ui.row().classes("justify-end gap-2 mt-6 w-full"):
            ui.button("Cancel", on_click=settings_dialog.close).props("flat")
            ui.button("Generate Proxies", color="primary", on_click=lambda: (
                settings_dialog.close(),
                asyncio.create_task(_do_batch_proxy_generation(state, selected, preset.value))
            ))

    settings_dialog.open()


async def _do_batch_proxy_generation(
    state: AppState,
    selected: list,
    preset: str,
):
    """Actual async proxy generation using one of the 5 official profiles."""
    progress_dialog = ui.dialog()
    with progress_dialog, ui.card().classes("w-[520px]"):
        ui.label("Generating Proxies").classes("text-h5 mb-2")
        ui.label(preset).classes("text-sm text-grey-5 mb-2")

        progress_bar = ui.linear_progress(value=0, show_value=True).classes("w-full mt-2")
        status_label = ui.label("Starting...").classes("text-sm mt-2")
        file_label = ui.label("").classes("text-xs text-grey-6")

        close_btn = ui.button("Close", on_click=progress_dialog.close).props("flat").classes("mt-4 w-full")
        close_btn.visible = False

    progress_dialog.open()
    await asyncio.sleep(0.05)

    # Pre-flight ffmpeg check
    try:
        from minicat.core.video import find_ffmpeg
        find_ffmpeg()
    except RuntimeError:
        progress_dialog.close()
        show_ffmpeg_required_dialog()
        return

    success = 0
    failed = 0
    total = len(selected)

    for i, clip in enumerate(selected, 1):
        progress_bar.value = i / total
        status_label.text = f"Processing {i} / {total}"
        file_label.text = clip.filename
        ui.update(progress_bar, status_label, file_label)
        await asyncio.sleep(0.01)

        try:
            proxy_dir = state.catalog_root / "proxies" / (clip.project or "Uncategorized")
            proxy_dir.mkdir(parents=True, exist_ok=True)

            # Use correct extension for the chosen profile
            from minicat.core.video import PROXY_PROFILE_DEFS
            profile = PROXY_PROFILE_DEFS.get(preset, PROXY_PROFILE_DEFS["Apple ProRes Proxy (Standard NLE Workflow)"])
            ext = profile.get("ext", ".mov")
            proxy_name = Path(clip.path).stem + "_proxy" + ext
            proxy_out = proxy_dir / proxy_name

            await asyncio.to_thread(
                video.create_proxy,
                source_path=clip.path,
                output_path=proxy_out,
                preset=preset,
                subtle_watermark=True,
                watermark_text="CAT+TAG",
                watermark_size=18,
                watermark_opacity=0.4,
                burn_original_timecode=True,
            )

            success += 1
        except Exception as err:
            failed += 1
            print(f"[Proxy] Failed for {clip.filename}: {err}")

    progress_bar.value = 1.0
    status_label.text = "Finished"
    file_label.text = ""
    ui.update(progress_bar, status_label, file_label)

    close_btn.visible = True
    ui.update(close_btn)

    refresh_all_ui(state)

    msg = f"Generated proxies for {success} clips"
    if failed:
        msg += f", {failed} failed"
    ui.notify(msg, color="positive" if failed == 0 else "warning", duration=6)


# NOTE: The multi-selection panel rendering was moved to
# minicat/ui/components/inspector.py (_render_multi_selection_panel)
# for a single source of truth. The old duplicate implementation has been removed.


# ---------------------------------------------------------------------------
# Export Dialog helpers (single clip + multiclip)
# ---------------------------------------------------------------------------

def _show_single_clip_export_dialog(state: AppState, video: Video) -> None:
    """Opens the export dialog for a single clip (Clio/Inspector view)."""
    _show_export_dialog(state, [video], is_single=True)


def _show_multi_clip_export_dialog(state: AppState) -> None:
    """Opens the export dialog for multiple selected clips."""
    selected = state.get_selected_videos()
    if not selected:
        ui.notify("No clips selected", color="warning")
        return
    _show_export_dialog(state, selected, is_single=False)


def _show_export_dialog(state: AppState, videos: list[Video], is_single: bool = True) -> None:
    """Main export dialog with quality, timecode, and subtitle options."""
    from minicat.core import settings as core_settings

    if not videos:
        return

    video = videos[0]  # for single-clip subtitle discovery etc.

    with ui.dialog() as dialog, ui.card().classes("w-[520px]"):
        ui.label("Export" + ("" if is_single else f" ({len(videos)} clips)")).classes("text-h5 mb-2")

        # 1. Quality
        ui.label("Quality").classes("text-base font-semibold mt-3 mb-1")
        quality_options = ["Original (highest quality)"] + core_settings.PROXY_PRESETS
        quality = ui.select(
            quality_options,
            value=quality_options[0],
            label="Export Source"
        ).props("dense").classes("w-full")

        # 2. Timecode
        burn_tc = ui.checkbox("Burn timecode on video", value=True).classes("mt-3")

        # 3. Subtitles
        ui.label("Subtitles").classes("text-base font-semibold mt-4 mb-1")

        # Discover available subtitles (only meaningful for single clip for now)
        available_subs = []
        if is_single and video.id:
            try:
                available_subs = get_available_subtitle_languages(video.id, state.catalog_root)
            except Exception:
                available_subs = []

        sub_options = [("none", "None")] + [(lang, display) for lang, display in available_subs]
        sub_display_map = {lang: display for lang, display in sub_options}
        sub_value_map = {display: lang for lang, display in sub_options}

        subtitle_choice = ui.select(
            [display for _, display in sub_options],
            value="None",
            label="Burn subtitles (choose language)"
        ).props("dense").classes("w-full")

        export_subs_separate = ui.checkbox(
            "Also export chosen subtitles as separate .srt file (not burned)",
            value=False
        ).classes("mt-2")

        # Action buttons
        with ui.row().classes("justify-end gap-2 mt-6 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            def do_export():
                chosen_quality = quality.value
                burn_timecode = burn_tc.value
                chosen_sub_display = subtitle_choice.value
                chosen_sub_lang = sub_value_map.get(chosen_sub_display, "none")
                export_sub_file = export_subs_separate.value

                dialog.close()

                # Kick off the actual export work
                asyncio.create_task(
                    _perform_export(
                        state,
                        videos,
                        quality=chosen_quality,
                        burn_timecode=burn_timecode,
                        subtitle_lang=chosen_sub_lang,
                        export_subtitle_file=export_sub_file,
                    )
                )

            ui.button("Export", icon="download", color="primary", on_click=do_export)

    dialog.open()


async def _perform_export(
    state: AppState,
    videos: list[Video],
    *,
    quality: str,
    burn_timecode: bool,
    subtitle_lang: str,
    export_subtitle_file: bool,
) -> None:
    """Does the actual work of exporting based on user choices."""
    from minicat.core.video import get_subtitle_srt_path
    import shutil

    export_dir = get_default_export_directory()
    export_dir.mkdir(parents=True, exist_ok=True)

    success = 0
    failed = 0

    for v in videos:
        try:
            # Determine source file
            if quality == "Original (highest quality)":
                source_path = Path(v.path)
                quality_tag = "Original"
            else:
                # Try to find the matching proxy
                proxy_dir = state.catalog_root / "proxies" / (v.project or "Uncategorized")
                proxy_name = Path(v.path).stem + "_proxy.mov"   # most profiles use .mov
                # For H.264/HEVC profiles the extension might be .mp4 — we try common names
                candidates = [
                    proxy_dir / (Path(v.path).stem + "_proxy.mov"),
                    proxy_dir / (Path(v.path).stem + "_proxy.mp4"),
                ]
                source_path = next((c for c in candidates if c.exists()), Path(v.path))
                quality_tag = quality.split()[0] if quality else "Proxy"

            # Base output name
            stem = Path(v.path).stem
            out_name = f"{stem}_export_{quality_tag}"
            if burn_timecode:
                out_name += "_TC"

            # Handle subtitles
            sub_path = None
            if subtitle_lang and subtitle_lang != "none" and v.id:
                candidate = get_subtitle_srt_path(v.id, state.catalog_root, subtitle_lang)
                if candidate.exists():
                    sub_path = candidate

            # === Actual file creation ===
            final_out = export_dir / f"{out_name}.mp4"

            if quality == "Original (highest quality)" and not burn_timecode and not sub_path:
                # Simple copy
                shutil.copy2(source_path, final_out)
            else:
                # We need to render (burn timecode and/or subtitles)
                # For now we reuse the proxy machinery when possible, or do a simple burn pass.
                # This is a pragmatic first implementation.
                from minicat.core.video import burn_subtitles_to_video

                # If we need timecode burn or we're on original + burns, we currently route through
                # a proxy-style render. A more complete dedicated exporter can be added later.
                # For simplicity in this first version we always produce an H.264 file when burns are requested.

                # Build a temporary path for the base render if needed
                work_path = final_out

                # Very basic path: if user chose a proxy profile and it exists, use it as base.
                # If burns are needed, we run burn_subtitles_to_video on top (timecode is not yet
                # integrated into the general burn path — we can enhance later).

                if burn_timecode or sub_path:
                    # For a complete solution we would call a unified renderer here.
                    # As a solid starting point we notify the user and still produce the best file we can.
                    ui.notify(
                        "Advanced burn (timecode + subtitles) on Original/Proxy is being added. "
                        "Current version copies the best available source.",
                        color="info",
                        duration=6,
                    )
                    shutil.copy2(source_path, final_out)
                else:
                    shutil.copy2(source_path, final_out)

            # Export subtitles as separate file if requested
            if export_subtitle_file and sub_path and sub_path.exists():
                sub_out = export_dir / f"{out_name}.srt"
                shutil.copy2(sub_path, sub_out)

            success += 1

        except Exception as ex:
            failed += 1
            print(f"[Export] Failed for {v.filename}: {ex}")

    if success:
        ui.notify(f"Exported {success} file(s) to {export_dir}", color="positive")
    if failed:
        ui.notify(f"{failed} export(s) failed (see console)", color="negative")


def _batch_delete_selected(state: AppState) -> None:
    """Delete all currently multi-selected items from the DB (with confirmation)."""
    selected = state.get_selected_videos()
    if not selected:
        return

    count = len(selected)

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(f"Remove {count} clips from catalog?").classes("text-h6")
        ui.label("Original files on disk will NOT be deleted.").classes("text-sm text-grey-6")

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            def do_batch_delete():
                deleted = 0
                for v in selected:
                    if v.id:
                        # Clean up all generated files (previews, transcription proxy audio, transcripts, subtitles, proxies)
                        try:
                            from minicat.core.video import cleanup_all_generated_files_for_clip
                            cleanup_all_generated_files_for_clip(v.id, state.catalog_root, original_filename=v.filename)
                        except Exception as cleanup_ex:
                            print(f"[Delete from Library] Artifact cleanup failed for {v.id}: {cleanup_ex}")

                        if db.delete_video(state.catalog_root, v.id):
                            deleted += 1
                dialog.close()
                state.clear_selection()
                refresh_all_ui(state)
                ui.notify(f"Removed {deleted} clips from catalog (all generated files cleaned up)", color="positive")
                _schedule_orphan_cleanup(state)
            ui.button(f"Remove {count} Clips", on_click=do_batch_delete, color="negative")

    dialog.open()


def _schedule_orphan_cleanup(state: AppState) -> None:
    """Fire a background orphan sweep (files + DB safety) right after a delete action.
    This is defense-in-depth: explicit per-clip clean + delete_video already do the work;
    this catches any edge cases or legacy ghosts immediately without blocking UI.
    """
    try:
        from minicat.core.video import cleanup_orphaned_catalog_files
        import asyncio as _aio
        clip_ids = {v.id for v in (getattr(state, "videos", []) or []) if getattr(v, "id", None)}
        _aio.create_task(_aio.to_thread(cleanup_orphaned_catalog_files, state.catalog_root, clip_ids))
    except Exception as _ex:
        print(f"[Cleanup] Post-delete orphan sweep failed to schedule: {_ex}")


# ---------------------------------------------------------------------------
# Delete helpers: Library (catalog only) vs Disk (catalog + physical files)
# "DELETE FROM LIBRARY" = safe, only removes from CAT+TAG
# "DELETE FROM DISK"    = dangerous, removes from catalog + deletes original files
# ---------------------------------------------------------------------------

def _delete_media_file_from_disk(state: AppState, video: Video) -> bool:
    """Internal helper: deletes the physical file from disk + removes from catalog.
    Returns True if the DB entry was removed (file deletion may have failed).
    """
    if not video.id:
        return False

    file_path = Path(video.path).expanduser().resolve()

    try:
        if file_path.exists():
            file_path.unlink()
    except Exception as ex:
        ui.notify(f"Could not delete file from disk: {ex}", color="negative")
        print(f"[Delete from Disk] Failed to unlink {file_path}: {ex}")

    # Always remove from DB even if file deletion failed
    db.delete_video(state.catalog_root, video.id)

    # Thorough cleanup of all generated artifacts (previews, audio, transcripts, subtitles, proxies)
    try:
        from minicat.core.video import cleanup_all_generated_files_for_clip
        cleanup_all_generated_files_for_clip(video.id, state.catalog_root, original_filename=video.filename)
    except Exception as ex:
        print(f"[Delete from Disk] Artifact cleanup failed for {video.id}: {ex}")

    _schedule_orphan_cleanup(state)
    return True


# --- MULTI-CLIP DISK DELETION (with very strong confirmation) ---
def _batch_delete_media_and_disk(state: AppState) -> None:
    """Permanently deletes clips from the CAT+TAG library AND the original files from disk."""
    selected = state.get_selected_videos()
    if not selected:
        return

    count = len(selected)

    with ui.dialog() as dialog, ui.card().classes("w-[440px]"):
        ui.label("⚠️ DELETE FROM DISK — Are you 100% sure?").classes("text-h6 text-negative")
        ui.label(f"This will permanently delete the original media file(s) for {count} clip(s) from your computer.").classes("text-sm")
        ui.label("The clips will also be removed from the CAT+TAG catalog.").classes("text-sm mt-1")
        ui.label("THIS ACTION CANNOT BE UNDONE.").classes("text-sm font-bold mt-2 text-negative")

        with ui.column().classes("mt-2 text-xs text-grey-6"):
            for v in selected[:5]:
                ui.label(f"• {v.filename}")
            if count > 5:
                ui.label(f"... and {count - 5} more")

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            def do_delete_from_disk():
                deleted_count = 0
                for v in selected:
                    if _delete_media_file_from_disk(state, v):
                        deleted_count += 1
                dialog.close()
                state.clear_selection()
                refresh_all_ui(state)
                ui.notify(f"Deleted {deleted_count} clip(s) from library and disk", color="negative")
            ui.button(f"YES, DELETE {count} FILE(S) FROM DISK", on_click=do_delete_from_disk, color="negative")

    dialog.open()


# --- SINGLE CLIP DISK DELETION (with very strong confirmation) ---
def _delete_from_disk_single(state: AppState, video: Video) -> None:
    """Permanently deletes one clip from the CAT+TAG library AND the original file from disk."""
    if not video.id:
        return

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("⚠️ DELETE FROM DISK — Are you 100% sure?").classes("text-h6 text-negative")
        ui.label(f"This will permanently delete the original media file from your computer:\n{video.filename}").classes("text-sm")
        ui.label("It will also be removed from the CAT+TAG catalog.").classes("text-sm mt-1")
        ui.label("THIS ACTION CANNOT BE UNDONE.").classes("text-sm font-bold mt-2 text-negative")

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            def do_delete():
                _delete_media_file_from_disk(state, video)
                dialog.close()
                state.clear_selection()
                refresh_all_ui(state)
                ui.notify("Clip deleted from library and disk", color="negative")
            ui.button("YES, DELETE FROM DISK", on_click=do_delete, color="negative")

    dialog.open()


# ---------------------------------------------------------------------------
# NEW: Copy selected clips + generate relinked XML (for Multiview)
# ---------------------------------------------------------------------------
async def _copy_selected_clips_with_xml(state: AppState) -> None:
    """Copy the original media files of the current multi-selection to a user-chosen folder
    and generate an XML inside that folder with all file paths updated to the new location.
    """
    selected = state.get_selected_videos()
    if not selected:
        ui.notify("No clips selected", color="warning")
        return

    count = len(selected)

    # --- 1. Ask user for destination folder ---
    try:
        if webview and webview.windows:
            dest_folder = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG | webview.DIALOG_DIRECTORY,
                directory=str(Path.home()),
                allow_multiple=False,
            )
            if not dest_folder:
                return
            dest = Path(dest_folder[0] if isinstance(dest_folder, (list, tuple)) else dest_folder)
        else:
            # Fallback — use the user's (or new default) export directory
            dest = get_default_export_directory()
    except Exception:
        dest = get_default_export_directory()

    # Confirmation
    with ui.dialog() as confirm, ui.card().classes("w-[480px]"):
        ui.label("Copy Clips + Generate XML").classes("text-h6 mb-2")
        ui.label(f"This will copy {count} media file(s) to:").classes("text-sm")
        ui.label(str(dest)).classes("text-sm font-mono break-all")
        ui.label("An XML file will also be created in the same folder, with all media paths pointing to the copies.").classes("text-sm mt-2")

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=confirm.close).props("flat")
            async def do_copy():
                confirm.close()
                await _perform_copy_and_xml(state, selected, dest)
            ui.button("Copy + Generate XML", icon="folder_copy", on_click=do_copy, color="primary")

    confirm.open()


async def _perform_copy_and_xml(state: AppState, selected: list[Video], dest_folder: Path) -> None:
    """Actual work: copy files + generate XML. Runs with progress UI."""
    dest_folder = dest_folder.expanduser().resolve()
    dest_folder.mkdir(parents=True, exist_ok=True)

    # Progress dialog
    prog = ui.dialog()
    with prog, ui.card().classes("w-[520px]"):
        ui.label("Copying Media + Generating XML").classes("text-h6 mb-2")
        progress = ui.linear_progress(value=0, show_value=True).classes("w-full")
        status = ui.label("Preparing...").classes("text-sm mt-2")
        file_label = ui.label("").classes("text-xs text-grey-6")
        summary = ui.label("").classes("text-sm mt-3")

    prog.open()
    await asyncio.sleep(0.05)

    copied_files: list[tuple[Video, Path]] = []
    errors = []

    total = len(selected)

    for i, v in enumerate(selected):
        percent = (i + 1) / total
        progress.value = percent
        status.text = f"Copying {i+1}/{total}"
        file_label.text = v.filename
        ui.update(progress, status, file_label)

        src = Path(v.path).expanduser().resolve()
        if not src.exists():
            errors.append(f"{v.filename} (file not found)")
            continue

        # Handle duplicate filenames
        dest_name = src.name
        counter = 1
        while (dest_folder / dest_name).exists():
            stem = src.stem
            suffix = src.suffix
            dest_name = f"{stem} ({counter}){suffix}"
            counter += 1

        dest_file = dest_folder / dest_name

        try:
            await asyncio.to_thread(shutil.copy2, src, dest_file)
            copied_files.append((v, dest_file))
        except Exception as ex:
            errors.append(f"{v.filename}: {ex}")

    # --- Generate XML with updated paths ---
    if copied_files:
        status.text = "Generating XML..."
        ui.update(status)

        xml_name = f"Selected_Clips_{datetime.now().strftime('%Y%m%d_%H%M')}.xml"
        xml_path = dest_folder / xml_name

        # Create lightweight Video copies with new paths
        updated_videos: list[Video] = []
        for orig_v, new_path in copied_files:
            new_v = orig_v.model_copy(deep=False)
            new_v.path = str(new_path)
            new_v.filename = new_path.name
            updated_videos.append(new_v)

        try:
            export_fcp7_xml(
                updated_videos,
                xml_path,
                sequence_name=f"CAT+TAG - {len(updated_videos)} Clips",
            )
        except Exception as ex:
            errors.append(f"XML generation failed: {ex}")
            xml_path = None

    # Final summary
    status.text = "Finished"
    summary_text = f"Copied {len(copied_files)} / {total} files to:\n{dest_folder}"
    if 'xml_path' in locals() and xml_path and xml_path.exists():
        summary_text += f"\n\nXML created: {xml_path.name}"
    if errors:
        summary_text += f"\n\nErrors: {len(errors)}"

    summary.text = summary_text
    summary.classes(add="whitespace-pre-line")
    ui.update(summary)

    await asyncio.sleep(0.6)
    prog.close()

    if copied_files:
        ui.notify(f"Copied {len(copied_files)} clips + XML to {dest_folder.name}", color="positive", duration=8)
        # Reveal folder
        try:
            import platform, subprocess
            if platform.system() == "Darwin":
                subprocess.run(["open", str(dest_folder)])
            elif platform.system() == "Windows":
                os.startfile(str(dest_folder))
        except Exception:
            pass
    else:
        ui.notify("No files were copied", color="warning")


# --- SINGLE CLIP LIBRARY ONLY (safe) ---
def _delete_from_library_single(state: AppState, video: Video) -> None:
    """Removes one clip from the CAT+TAG catalog only (file stays on disk)."""
    if not video.id:
        return

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Delete from Library?").classes("text-h6")
        ui.label(f"This will only remove the clip from the CAT+TAG catalog:\n{video.filename}").classes("text-sm")
        ui.label("The original media file will remain on your disk.").classes("text-sm text-grey-6 mt-1")

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            def do_delete():
                # Thorough cleanup of all generated artifacts before removing from DB
                try:
                    from minicat.core.video import cleanup_all_generated_files_for_clip
                    cleanup_all_generated_files_for_clip(video.id, state.catalog_root, original_filename=video.filename)
                except Exception as cleanup_ex:
                    print(f"[Delete from Library] Artifact cleanup failed: {cleanup_ex}")

                db.delete_video(state.catalog_root, video.id)
                dialog.close()
                state.clear_selection()
                refresh_all_ui(state)
                ui.notify("Removed from CAT+TAG library (all generated files cleaned up)", color="positive")
                _schedule_orphan_cleanup(state)
            ui.button("DELETE FROM LIBRARY", on_click=do_delete, color="negative")

    dialog.open()


def _rebuild_clip_previews_and_metadata(state: AppState, clip: Video) -> bool:
    """Rebuilds storyboard/thumbnail + refreshes technical metadata (codec, shoot_date) for one clip.
    Returns True on success.
    """
    if not clip.id:
        return False

    # Guard: audio files have no video previews; just refresh metadata
    try:
        from minicat.cli.main import _is_audio_file
        is_audio = _is_audio_file(Path(clip.path))
    except Exception:
        is_audio = Path(clip.path).suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aiff", ".aif"}

    if is_audio:
        updates: dict[str, Any] = {}
        try:
            meta = video.extract_metadata(clip.path)
            if meta.get("codec"):
                updates["codec"] = meta["codec"]
            if not clip.shoot_date and meta.get("creation_date"):
                updates["shoot_date"] = meta["creation_date"]
        except Exception as meta_err:
            print(f"[Metadata] Could not extract for {clip.filename}: {meta_err}")
        if updates:
            db.update_video_fields(state.catalog_root, clip.id, **updates)
        return True

    try:
        # Generate previews (video only)
        thumb, board = video.generate_previews(
            clip.path, state.catalog_root, clip.id
        )

        updates: dict[str, Any] = {
            "thumbnail_path": str(thumb),
            "storyboard_path": str(board),
        }

        # Refresh technical fields from the actual media file (ffprobe)
        # This includes timecode (tc_start / tc_end) as part of the standard rebuild.
        try:
            meta = video.extract_metadata(clip.path)
            if meta.get("codec"):
                updates["codec"] = meta["codec"]
            if not clip.shoot_date and meta.get("creation_date"):
                updates["shoot_date"] = meta["creation_date"]
            # Timecode start / end (always refreshed during rebuild)
            if meta.get("tc_start"):
                updates["tc_start"] = meta["tc_start"]
            if meta.get("tc_end"):
                updates["tc_end"] = meta["tc_end"]
        except Exception as meta_err:
            print(f"[Metadata] Could not extract for {clip.filename}: {meta_err}")

        # Also re-extract from camera sidecar XML if present (often richer)
        try:
            xml_meta = video.extract_camera_xml_metadata(clip.path, enrich_with_exiftool=True)
            if xml_meta:
                if xml_meta.get("camera") and not clip.camera:
                    updates["camera"] = xml_meta["camera"]
                if xml_meta.get("lens") and not clip.lens:
                    updates["lens"] = xml_meta["lens"]
                if xml_meta.get("shoot_date") and not clip.shoot_date:
                    updates["shoot_date"] = xml_meta["shoot_date"]
                if xml_meta.get("source_xml"):
                    updates["camera_xml_path"] = xml_meta["source_xml"]
                # Rich technical fields
                for field in ("iso", "f_number", "shutter_speed", "focal_length", "white_balance"):
                    if xml_meta.get(field) and not getattr(clip, field, None):
                        updates[field] = xml_meta[field]
        except Exception as xml_err:
            print(f"[XML] Could not re-extract for {clip.filename}: {xml_err}")

        db.update_video_fields(state.catalog_root, clip.id, **updates)
        return True

    except Exception as e:
        print(f"[Rebuild] Failed for {clip.filename}: {e}")
        return False


def _batch_rebuild_previews_and_metadata(state: AppState) -> None:
    """Rebuild previews + metadata for all currently selected clips."""
    selected = state.get_selected_videos()
    if not selected:
        return

    count = len(selected)
    success = 0
    failed = 0

    notif = ui.notification(f"Rebuilding {count} clips...", type="ongoing", close_button=False)

    for i, clip in enumerate(selected, 1):
        notif.message = f"Rebuilding {i}/{count}: {clip.filename}"
        if _rebuild_clip_previews_and_metadata(state, clip):
            success += 1
        else:
            failed += 1

    notif.dismiss()

    # Refresh UI (full, so sidebar counts + distinct values update if metadata changed)
    refresh_all_ui(state)

    msg = f"Rebuilt {success} clips"
    if failed:
        msg += f", {failed} failed"
    ui.notify(msg, color="positive" if failed == 0 else "warning", duration=6)


# ---------------------------------------------------------------------------
# Transcription proxy audio management (persistent <catalog>/audio/ for transcription + AI listening)
# ---------------------------------------------------------------------------

def _get_audio_cache_info(clip: Video, catalog_root: Path) -> dict:
    """Return info about the single transcription proxy audio file for one clip."""
    from minicat.core.video import get_cached_audio_path
    p = get_cached_audio_path(clip.id, catalog_root)
    if p.exists():
        size = p.stat().st_size
        return {"path": p, "size": size, "size_mb": size / (1024 * 1024)}
    return None


def _clear_audio_cache_for_clips(clips: list[Video], catalog_root: Path) -> tuple[int, int]:
    """Clear the single transcription proxy audio file for a list of clips. Returns (deleted_files, clips_affected)."""
    from minicat.core.video import clear_cached_audio
    total_deleted = 0
    affected = 0
    for clip in clips:
        if clip.id:
            deleted = clear_cached_audio(clip.id, catalog_root)
            if deleted:
                total_deleted += deleted
                affected += 1
    return total_deleted, affected


def _rebuild_audio_cache_for_clips(state: AppState, clips: list[Video]) -> None:
    """Rebuild (clear + re-extract with full processing) the transcription proxy audio for selected clips."""
    from minicat.core.video import rebuild_cached_audio_for_clip
    if not clips:
        return

    count = len(clips)
    success = 0
    notif = ui.notification(f"Rebuilding transcription proxy audio for {count} clips...", type="ongoing", close_button=False)

    for i, clip in enumerate(clips, 1):
        notif.message = f"Audio proxy {i}/{count}: {clip.filename}"
        try:
            ok = rebuild_cached_audio_for_clip(clip.path, clip.id, state.catalog_root)
            if ok:
                success += 1
        except Exception as ex:
            print(f"[Audio Cache Rebuild] Failed for {clip.filename}: {ex}")

    notif.dismiss()
    refresh_all_ui(state)
    ui.notify(f"Rebuilt transcription proxy audio for {success}/{count} clips", color="positive", duration=6)


def _batch_clear_audio_cache(state: AppState) -> None:
    """UI action: clear cached transcription proxy audio for current selection (with confirmation)."""
    selected = state.get_selected_videos()
    if not selected:
        return

    def do_clear():
        deleted, affected = _clear_audio_cache_for_clips(selected, state.catalog_root)
        refresh_all_ui(state)
        ui.notify(f"Cleared cached audio for {affected} clip(s)", color="positive")

    with ui.dialog() as confirm, ui.card().classes("w-[420px]"):
        ui.label("Clear Cached Audio?").classes("text-h6 mb-2")
        ui.label(
            f"This will delete the persistent transcription proxy audio (.m4a) "
            f"for {len(selected)} selected clip(s) from the catalog's audio/ folder.\n\n"
            "The original media files are not affected. "
            "The processed audio (noise-reduced, leveled 24 kHz AAC) will be re-extracted automatically the next time you transcribe or use AI Journalist."
        ).classes("text-sm mb-4")
        with ui.row().classes("gap-2 w-full"):
            ui.button("Cancel", on_click=confirm.close).props("outline")
            ui.button("Yes, Clear Cached Audio", color="negative", on_click=lambda: (confirm.close(), do_clear()))


def _batch_rebuild_audio_cache(state: AppState) -> None:
    """UI action: rebuild processed transcription proxy audio for current selection."""
    selected = state.get_selected_videos()
    if not selected:
        return
    _rebuild_audio_cache_for_clips(state, selected)


def _purge_legacy_audio_wavs(state: AppState) -> None:
    """UI action: remove any obsolete .wav transcription proxies left from before the 24 kHz AAC upgrade.
    This cleans the catalog's audio/ folder without affecting modern .m4a proxies or requiring a rebuild.
    """
    from minicat.core.video import purge_legacy_wav_caches
    try:
        count = purge_legacy_wav_caches(state.catalog_root)
        refresh_all_ui(state)
        if count > 0:
            ui.notify(f"Purged {count} legacy .wav file(s) from catalog audio/ folder", color="positive", duration=6)
        else:
            ui.notify("No legacy .wav audio caches found to purge", color="info")
    except Exception as ex:
        ui.notify(f"Legacy purge failed: {ex}", color="negative")


def _render_active_filters(state: AppState) -> None:
    """Shows the currently active project (and other filters) prominently above the media grid/list."""
    if not state:
        return

    chips = []

    if state.filters.project:
        projects = state.filters.project
        if len(projects) == 1:
            chips.append(("Project", projects[0]))
        else:
            chips.append(("Projects", ", ".join(projects)))

    if state.filters.camera:
        cameras = state.filters.camera
        chips.append(("Camera", ", ".join(cameras) if len(cameras) > 1 else cameras[0]))

    if state.filters.location:
        locations = state.filters.location
        chips.append(("Location", ", ".join(locations) if len(locations) > 1 else locations[0]))

    if state.filters.text:
        chips.append(("Search", state.filters.text))

    if not chips and not state.filters.tags:
        return

    with ui.row().classes("items-center gap-2 mb-2 flex-wrap"):
        ui.label("Active filters:").classes("text-caption text-grey-6 mr-1")

        for label, value in chips:
            chip_text = f"{label}: {value}"
            ui.chip(chip_text, removable=True).props("size=sm outline").on(
                "remove", lambda lbl=label: _remove_filter(state, lbl)
            )

        # Render each active tag as its own removable chip (so user can remove one by one)
        if state.filters.tags:
            for tag in state.filters.tags:
                def make_tag_remover(t=tag):
                    def remove_one():
                        if state.filters.tags:
                            new_tags = [x for x in state.filters.tags if x != t]
                            state.filters.tags = new_tags or None
                        state.reload()
                        refresh_all_ui(state)
                    return remove_one

                ui.chip(f"Tag: {tag}", removable=True, color="primary").props("size=sm outline").on(
                    "remove", make_tag_remover()
                )

        ui.button("Clear all", icon="clear_all", on_click=state.clear_filters).props("size=xs flat dense")


# ---------------------------------------------------------------------------
# UI Components
# ---------------------------------------------------------------------------

def _open_catalog_dialog() -> None:
    """Open a different CAT+TAG catalog from the top bar.
    Any folder is accepted: it will be created/initialized as a catalog if needed.
    Default catalog location is ~/CAT+TAG .
    """
    state = get_state()

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Open Another Catalog").classes("text-h6 mb-3")

        ui.label("Select or enter a folder to use (or create) as your CAT+TAG catalog.\nDefault is ~/CAT+TAG .").classes("text-sm text-grey-6 mb-2")

        selected_path = ui.label("(No folder selected)").classes("text-xs text-grey-5 mb-3")

        def choose_folder():
            try:
                import webview
                if webview.windows:
                    result = webview.windows[0].create_file_dialog(
                        webview.FileDialog.FOLDER,
                        directory=str(Path.home()),
                        allow_multiple=False,
                    )
                    if result:
                        chosen = Path(result[0])
                        selected_path.text = str(chosen)
                        selected_path.classes(replace="text-xs text-primary mb-3")
                        # Force UI update for the label in case
                        try:
                            selected_path.update()
                        except Exception:
                            pass
            except Exception:
                ui.notify("Please enter the path manually below", color="info")

        ui.button("Choose Folder...", icon="folder", on_click=choose_folder).props("outline").classes("w-full mb-3")

        manual_path = ui.input("Or paste catalog folder path").props("dense").classes("w-full mb-2")

        def do_open():
            chosen_text = (selected_path.text or "").strip()
            manual_val = (manual_path.value or "").strip()
            path_str = chosen_text if chosen_text and "No folder" not in chosen_text else manual_val
            if not path_str:
                ui.notify("Please select or enter a folder", color="warning")
                return

            try:
                p = Path(path_str).expanduser().resolve()
                p.mkdir(parents=True, exist_ok=True)
                dialog.close()
                _switch_to_catalog(p)
            except Exception as ex:
                ui.notify(f"Failed to use folder: {ex}", color="negative")

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Open Catalog", icon="folder_open", on_click=do_open, color="primary")

    dialog.open()


def create_header() -> None:
    """Ultra-dense NLE-style topbar header with view controls and import/export actions."""
    with ui.header(elevated=False).props('bordered').classes('bg-[#161618] text-zinc-300 border-b border-zinc-800/40 q-py-xs q-px-sm items-center justify-between gap-x-3 shadow-none'):

        # === LEFT: Application Title + Open Catalog (icon only) ===
        with ui.row().classes("items-center no-wrap gap-x-2"):
            ui.button(
                icon="folder_open",
                on_click=_open_catalog_dialog
            ).props("flat dense size=sm").tooltip("Open Catalog")

            ui.html(
                '<span class="text-[15px] font-semibold tracking-tight text-white">CAT</span>'
                '<span class="text-[15px] font-bold text-primary">+</span>'
                '<span class="text-[15px] font-semibold tracking-tight text-white">TAG</span>'
            )

            ui.chip("Local", icon="lock", color="primary").props("outline size=xs dense").classes("text-[10px]")

        # === CENTER: Omnibar Search (smooth, border-minimal NLE style) ===
        with ui.row().classes("flex-1 justify-center items-center no-wrap px-2"):
            with ui.row().classes(
                "items-center bg-zinc-900/90 rounded-full border border-zinc-800/60 "
                "focus-within:border-zinc-700/70 w-[540px] max-w-[50vw] pr-1"
            ):
                ui.icon("search").classes("text-[15px] text-zinc-500 ml-3 mr-1")
                search_input = ui.input(
                    placeholder="Search library..."
                ).props('dense dark borderless clearable').classes('flex-1 text-xs')

            def _apply_search():
                val = (search_input.value or "").strip()
                state = get_state()
                if state:
                    state.set_filter_text(val or None)

            search_input.on("update:model-value", lambda e: _apply_search())
            globals()['_header_search_input'] = search_input

        # === RIGHT: View Controls + Import/Export + Utilities ===
        with ui.row().classes("items-center no-wrap gap-x-1 text-xs"):

            # Grid / List density view controls
            ui.button(
                icon="grid_view",
                on_click=lambda: _header_set_view_mode("grid")
            ).props("flat dense size=xs").classes("text-zinc-400").tooltip("Grid View")

            ui.button(
                icon="list",
                on_click=lambda: _header_set_view_mode("list")
            ).props("flat dense size=xs").classes("text-zinc-400").tooltip("List View")

            ui.separator().props("vertical").classes("mx-0.5 h-4")

            # Import Folder
            ui.button(
                "Import",
                icon="folder_open",
                on_click=lambda: trigger_import()
            ).props("flat dense size=xs").classes("text-zinc-300").tooltip("Import Folder")

            # Export XML
            ui.button(
                "XML",
                icon="upload",
                on_click=lambda: _header_export_xml()
            ).props("flat dense size=xs").classes("text-zinc-300").tooltip("Export XML (selected or all visible)")

            # Load saved AI Director story (in top bar only; available after using "Save Story" from AI Director results)
            ui.button(
                "Load Story",
                icon="upload_file",
                on_click=_show_load_ai_director_story_dialog
            ).props("flat dense size=xs").classes("text-zinc-300").tooltip("Load saved AI Director story (project/cut) for XML + Voiceover export")

            ui.separator().props("vertical").classes("mx-0.5 h-4")

            # Catalog Root (compact)
            state = get_state()
            if state and getattr(state, 'catalog_root', None):
                catalog_text = str(state.catalog_root)
                parts = catalog_text.rstrip('/').split('/')
                short_path = '/'.join(parts[-2:]) if len(parts) > 1 else catalog_text
                ui.label(short_path).classes("text-[10px] font-mono text-zinc-500").tooltip(catalog_text)

            ui.separator().props("vertical").classes("mx-0.5 h-4")

            # Utility actions
            ui.button(icon="refresh", on_click=lambda: refresh_all_ui(get_state()) if get_state() else None).props("flat dense size=xs").classes("text-zinc-400").tooltip("Refresh")

            ui.button(icon="settings", on_click=_open_settings_dialog).props("flat dense size=xs").classes("text-zinc-400").tooltip("Settings")

            ui.button(icon="help_outline", on_click=_open_help_dialog).props("flat dense size=xs").classes("text-zinc-400").tooltip("Help")


# --- Layer 3: Simple Refresh Registry ---
_refresh_registry: list = []


def register_refresh_callback(fn):
    """New components register here for cleaner updates (reduces global side effects)."""
    if fn not in _refresh_registry:
        _refresh_registry.append(fn)


def refresh_all_ui(state: AppState) -> None:
    """Central helper with registry support (Layer 3)."""
    state.reload()

    # Registry-based refresh (preferred going forward)
    for fn in list(_refresh_registry):
        try:
            fn()
        except Exception:
            pass

    # Legacy direct refreshes (kept during migration for components that still use @ui.refreshable)
    main_content.refresh()
    ui_inspector.inspector_content.refresh()

    # Unified sidebar refresh (after drawers unification)
    try:
        ui_drawers.left_drawer_content.refresh()
        ui_drawers.rich_tags_section.refresh()
    except Exception:
        pass

    # Sync search
    try:
        search_input = globals().get('_header_search_input')
        if search_input:
            current_text = state.filters.text or ""
            if getattr(search_input, 'value', None) != current_text:
                search_input.value = current_text
    except Exception:
        pass


# --- Layer 1: Delegated to ui/components/drawers.py ---

def create_left_drawer() -> None:
    """Delegated to extracted drawers component."""
    state = get_state()
    if state is not None:
        ui_drawers.create_left_drawer(state)


def create_right_drawer() -> None:
    """Inspector / Storyboard + Metadata Editor (updates on selection).
    The drawer is only shown when there is something to inspect (clip or project selected).
    """
    global RIGHT_DRAWER
    state = get_state()

    visible = False
    if state is not None:
        if state.selected_ids or state.selected_project or state.selected:
            visible = True

    with ui.right_drawer(value=visible, elevated=False).classes("q-pa-none border-l border-zinc-800/40").props("width=320 bordered") as drawer:
        RIGHT_DRAWER = drawer
        with ui.scroll_area().classes('px-4 py-2 flex flex-col gap-y-3 w-full h-full'):
            inspector_content()


def _save_tags_from_input(state: AppState, value: str) -> None:
    if not state.selected or not state.selected.id:
        return
    tags = [t.strip() for t in value.split(",") if t.strip()]
    db.set_video_tags(state.catalog_root, state.selected.id, tags)
    state.selected = db.get_video_by_path(state.catalog_root, state.selected.path)
    state.reload()
    main_content.refresh()
    ui_inspector.inspector_content.refresh()


def format_duration_timecode(seconds: float | None, fps: float | None = None) -> str:
    """Convert seconds to timecode string HH:MM:SS:FF (always 4 parts, FF defaults to 00)."""
    if seconds is None or seconds < 0:
        return "00:00:00:00"
    total_seconds = int(seconds)
    hh = total_seconds // 3600
    mm = (total_seconds % 3600) // 60
    ss = total_seconds % 60

    if fps and fps > 0:
        ff = int(round((seconds - total_seconds) * fps))
        ff = max(0, min(int(fps) - 1, ff))
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
    else:
        return f"{hh:02d}:{mm:02d}:{ss:02d}:00"


def format_duration_mmss(seconds: float | None) -> str:
    """Simple MM:SS for grid card overlays."""
    if seconds is None or seconds <= 0:
        return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def get_resolution_label(width: int | None, height: int | None) -> str:
    """Return 4K UHD / HD / 720 / SD based on vertical resolution."""
    if not width or not height:
        return "—"
    try:
        w, h = int(width), int(height)
        vert = min(w, h)
    except (TypeError, ValueError):
        return "—"

    if vert >= 2160:
        return "4K UHD"
    elif vert >= 1080:
        return "HD"
    elif vert >= 720:
        return "720"
    else:
        return "SD"


def _show_storyboard_dialog(video: Video) -> None:
    """Open a modal with the full storyboard image for a clip."""
    if not video.storyboard_path or not Path(video.storyboard_path).exists():
        ui.notify("No storyboard available for this clip", color="warning")
        return
    with ui.dialog() as dialog, ui.card().classes("w-[92vw] max-w-[1200px] q-pa-none overflow-hidden"):
        with ui.row().classes("items-center justify-between px-4 py-2 bg-[#111]"):
            ui.label(video.filename).classes("text-base font-medium")
            ui.button(icon="close", on_click=dialog.close).props("flat dense round")
        # Cap the displayed size so huge 4K-derived storyboards don't kill the UI
        ui.image(str(video.storyboard_path)).classes("w-full").style("max-height: 70vh; object-fit: contain;")
        with ui.row().classes("px-4 py-2 bg-[#111] justify-between"):
            def ask_ai_from_storyboard():
                if not video.storyboard_path:
                    ui.notify("No storyboard available", color="warning")
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

                try:
                    suggestions = suggest_tags_from_storyboard(
                        video.storyboard_path,
                        api_key,
                        min_tags=3,
                        max_tags=8,
                        model_name=get_gemini_model(),
                    )
                except Exception as e:
                    loading.close()
                    error_msg = str(e)
                    if "404" in error_msg and "no longer available" in error_msg.lower():
                        error_msg = "Selected Gemini model is outdated. Please choose a current model in Settings."
                    ui.notify(f"AI failed: {error_msg}", color="negative", multi_line=True)
                    return
                finally:
                    loading.close()

                if not suggestions:
                    ui.notify("No suggestions from AI", color="warning")
                    return

                # Rich multi-select + editable review dialog (same experience as the inspector)
                with ui.dialog() as sug_dialog, ui.card().classes("w-[520px]"):
                    ui.label("AI Suggested Tags").classes("text-h6 mb-2")
                    ui.label("Review, edit, and select the tags you want to add").classes("text-xs text-grey-6 mb-3")

                    tag_items = [{"text": tag, "selected": True} for tag in suggestions]

                    def get_selected_count():
                        return sum(1 for item in tag_items if item.get("selected") and item.get("text"))

                    items_container = ui.column().classes("w-full gap-2 mb-2")

                    def refresh_items():
                        items_container.clear()
                        with items_container:
                            for idx, item in enumerate(tag_items):
                                text = item.get("text") or ""
                                with ui.row().classes("items-center gap-2 w-full py-0.5"):
                                    cb = ui.checkbox(
                                        text or "(empty tag)",
                                        value=bool(item.get("selected"))
                                    ).props("dense")

                                    def make_cb_handler(i=idx):
                                        def handler(e):
                                            tag_items[i]["selected"] = bool(getattr(e, "value", False))
                                            update_add_button()
                                        return handler
                                    cb.on_value_change(make_cb_handler())

                                    def edit_item(i=idx):
                                        async def do_edit():
                                            try:
                                                new_val = await ui.input_dialog(
                                                    "Edit tag text",
                                                    value=tag_items[i].get("text", ""),
                                                )
                                                if new_val is not None:
                                                    cleaned = (new_val or "").strip().lower()
                                                    if cleaned:
                                                        tag_items[i]["text"] = cleaned
                                                        refresh_items()
                                                        update_add_button()
                                            except Exception as ex:
                                                ui.notify(f"Could not edit: {ex}", color="negative")
                                        asyncio.create_task(do_edit())

                                    ui.button(icon="edit", on_click=edit_item).props("flat dense size=sm color=grey-7")

                                    def delete_item(i=idx):
                                        if 0 <= i < len(tag_items):
                                            del tag_items[i]
                                            refresh_items()
                                            update_add_button()

                                    ui.button(icon="delete", on_click=delete_item).props("flat dense size=sm color=grey-7")

                            # Add your own tag
                            with ui.row().classes("items-center gap-2 w-full mt-2 pt-2 border-t border-grey-8"):
                                custom_input = ui.input(placeholder="Add your own tag...").props("dense").classes("flex-1")
                                def add_custom():
                                    val = (custom_input.value or "").strip().lower()
                                    if val:
                                        tag_items.append({"text": val, "selected": True})
                                        custom_input.value = ""
                                        refresh_items()
                                        update_add_button()
                                ui.button("Add", on_click=add_custom, color="primary").props("dense size=sm")

                    refresh_items()

                    button_row = ui.row().classes("justify-end gap-2 mt-2 w-full")

                    def update_add_button():
                        button_row.clear()
                        with button_row:
                            ui.button("Cancel", on_click=sug_dialog.close).props("flat")
                            count = get_selected_count()
                            add_btn = ui.button(
                                f"Add Selected ({count})",
                                on_click=apply_tags,
                                color="primary"
                            )
                            if count == 0:
                                add_btn.props("disable")

                    async def apply_tags():
                        if not video.id:
                            sug_dialog.close()
                            return
                        selected = [item["text"] for item in tag_items if item.get("selected") and item.get("text")]
                        if not selected:
                            sug_dialog.close()
                            return

                        current_state = get_state()

                        # Close immediately for responsive feel
                        sug_dialog.close()
                        ui.notify("Saving tags...", color="info", duration=1.5)

                        def _save_tags_sync():
                            """Run all DB work in a thread so we don't block the UI.
                            Includes retry logic for 'database is locked' (very common during imports + manual edits).
                            Returns (write_succeeded: bool, state)
                            """
                            if not current_state:
                                return (False, None)

                            current = set(getattr(video, 'tags', None) or [])
                            new_tags = current | set(selected)

                            # Aggressive retry for manual "Add Selected" (user is actively waiting)
                            write_succeeded = False
                            for attempt in range(15):  # more attempts for manual action
                                try:
                                    db.set_video_tags(current_state.catalog_root, video.id, list(new_tags))
                                    write_succeeded = True
                                    break
                                except Exception as db_err:
                                    err_lower = str(db_err).lower()
                                    if "locked" in err_lower or "busy" in err_lower:
                                        import time
                                        time.sleep(0.12 * (attempt + 1))  # slightly longer backoff
                                        continue
                                    raise

                            if not write_succeeded:
                                print(f"[AI Tags] Warning: Could not write tags for {video.filename} after retries")

                            fresh_video = db.get_video_by_path(current_state.catalog_root, video.path)
                            if fresh_video:
                                video.tags = fresh_video.tags
                                current_state.selected = fresh_video
                                current_state.selected_ids = {fresh_video.id} if fresh_video.id else set()
                            return (write_succeeded, current_state)

                        # Offload the DB work
                        result = await asyncio.to_thread(_save_tags_sync)
                        write_succeeded = result[0] if isinstance(result, (list, tuple)) else False
                        saved_state = result[1] if isinstance(result, (list, tuple)) else None

                        # Schedule final UI with honest result
                        def _finish_ui():
                            try:
                                if saved_state:
                                    saved_state.reload()
                                    refresh_all_ui(saved_state)

                                try:
                                    dialog.close()
                                except Exception:
                                    pass

                                if write_succeeded:
                                    ui.notify(
                                        f"Added {len(selected)} tag(s). The clip is selected — look at the inspector and the small tag chips under the card.",
                                        color="positive", duration=6
                                    )
                                else:
                                    ui.notify(
                                        "Failed to save tags after many attempts (database busy). Wait 5–10 seconds and try again.",
                                        color="negative", duration=10
                                    )
                            except Exception as e:
                                print(f"[AI Tags] Error in final UI: {e}")

                        ui.timer(0.02, _finish_ui, once=True)

                    def select_all():
                        for item in tag_items:
                            item["selected"] = True
                        refresh_items()
                        update_add_button()

                    def deselect_all():
                        for item in tag_items:
                            item["selected"] = False
                        refresh_items()
                        update_add_button()

                    update_add_button()

                    with ui.row().classes("justify-between w-full mt-3"):
                        ui.button("Select All", on_click=select_all, color="primary").props("outline size=sm")
                        ui.button("Deselect All", on_click=deselect_all).props("flat size=sm")

                sug_dialog.open()

            ui.button("Ask AI for tags", icon="auto_awesome", on_click=ask_ai_from_storyboard).props("size=sm outline")

            ui.button("Rebuild Previews", on_click=lambda: (_rebuild_and_refresh_storyboard(video, dialog))).props("size=sm outline")
    dialog.open()


def _rebuild_and_refresh_storyboard(video: Video, dialog) -> None:
    """Rebuild previews for one clip and refresh both grid and open dialog."""
    state = get_state()
    if not state or not video.id:
        return
    if _rebuild_clip_previews_and_metadata(state, video):
        # Update the selected video reference
        refreshed = db.get_video_by_path(state.catalog_root, video.path)
        if refreshed:
            state.selected = refreshed
        state.reload()
        main_content.refresh()
        dialog.close()
        ui.notify("Storyboard + thumbnail rebuilt", color="positive")
        # Re-open with fresh data
        _show_storyboard_dialog(state.selected or video)
    else:
        ui.notify("Failed to rebuild", color="negative")


def _play_current_video():
    """Play the currently selected video (prefers proxy if available)."""
    current_state = get_state()
    if not current_state or not current_state.selected:
        ui.notify("No clip selected", color="warning")
        return
    if platform.system() == "Darwin" and get_preference("use_quicklook", True):
        ui.notify("Opening preview...", color="info", duration=2)
    else:
        ui.notify("Opening in default player...", color="info", duration=2)
    _play_video(current_state.selected.path)


def _play_current_from_card(video: Video) -> None:
    """Play handler for the small play button on media grid cards."""
    if not video or not video.path:
        ui.notify("Invalid clip", color="warning")
        return
    if platform.system() == "Darwin" and get_preference("use_quicklook", True):
        ui.notify("Opening preview...", color="info", duration=2)
    else:
        ui.notify("Opening in default player...", color="info", duration=2)
    _play_video(video.path)


# ---------------------------------------------------------------------------
# Header action helpers (safe to call from static header buttons via get_state)
# ---------------------------------------------------------------------------

def _header_set_view_mode(mode: str) -> None:
    """Set grid/list view from the top bar (icon-only buttons)."""
    state = get_state()
    if state and mode in ("grid", "list"):
        state.view_mode = mode
        main_content.refresh()


def _set_media_view_filter(mode: str) -> None:
    """Top bar 'View' filter: All / Video / Audio."""
    state = get_state()
    if state:
        state.set_media_filter(mode)


# ---------------------------------------------------------------------------
# List view column configuration (for drag-to-reorder + visibility)
# ---------------------------------------------------------------------------

def _get_all_list_columns() -> list[dict]:
    """Returns the canonical list of all available columns for the list view."""
    return [
        {"name": "selected", "label": "", "field": "selected", "align": "center", "sortable": False},
        {"name": "filename", "label": "Filename", "field": "filename", "align": "left", "sortable": False},
        {"name": "date", "label": "Shoot Date", "field": "date", "align": "left", "sortable": False},
        {"name": "length", "label": "Length", "field": "length", "align": "left", "sortable": False},
        {"name": "codec", "label": "Codec", "field": "codec", "align": "left"},
        {"name": "resolution", "label": "Resolution", "field": "resolution", "align": "left"},
        {"name": "camera", "label": "Camera", "field": "camera", "align": "left"},
        {"name": "operator", "label": "Operator", "field": "operator", "align": "left"},
        {"name": "lens", "label": "Lens", "field": "lens", "align": "left"},
        {"name": "iso", "label": "ISO", "field": "iso", "align": "left"},
        {"name": "aperture", "label": "Aperture", "field": "aperture", "align": "left"},
        {"name": "shutter", "label": "Shutter", "field": "shutter", "align": "left"},
        {"name": "focal", "label": "Focal", "field": "focal", "align": "left"},
        {"name": "wb", "label": "WB", "field": "wb", "align": "left"},
        {"name": "gamma", "label": "Gamma", "field": "gamma", "align": "left"},
        {"name": "color_primaries", "label": "Color Primaries", "field": "color_primaries", "align": "left"},
        {"name": "coding_equations", "label": "Coding Eqs", "field": "coding_equations", "align": "left"},
        {"name": "location", "label": "Location", "field": "location", "align": "left"},
        {"name": "tags", "label": "Tags", "field": "tags", "align": "left"},
        {"name": "tc_start", "label": "TC Start", "field": "tc_start", "align": "left"},
        {"name": "tc_end", "label": "TC End", "field": "tc_end", "align": "left"},
    ]


# Default columns shown in the list view (user can customize via the column picker)
DEFAULT_VISIBLE_LIST_COLUMNS = [
    "selected",
    "filename",
    "date",
    "length",
    "codec",
    "resolution",
    "camera",
    "lens",
    "gamma",
    "location",
    "tags",
]


def _show_list_column_customizer(state: AppState) -> None:
    """Dialog to reorder columns (with up/down) and toggle visibility in list view."""
    all_columns = _get_all_list_columns()
    all_names = [c["name"] for c in all_columns]

    if not state.list_column_order:
        # Set smart defaults on first use
        default_visible = DEFAULT_VISIBLE_LIST_COLUMNS.copy()
        # Append any columns not in the default list (future-proofing)
        for name in all_names:
            if name not in default_visible:
                default_visible.append(name)
        state.list_column_order = default_visible
        state.hidden_list_columns = set(all_names) - set(DEFAULT_VISIBLE_LIST_COLUMNS)

    current_order = [name for name in state.list_column_order if name in all_names]
    for name in all_names:
        if name not in current_order:
            current_order.append(name)

    visible = {name for name in current_order if name not in state.hidden_list_columns}

    with ui.dialog() as dialog, ui.card().classes("w-[540px]"):
        ui.label("Customize List Columns").classes("text-h6 mb-1")
        ui.label("Use the arrows to reorder. Check the box to show or hide a column.").classes("text-xs text-grey-6 mb-3")

        column_list = ui.column().classes("w-full gap-1")

        def refresh_column_list():
            column_list.clear()
            with column_list:
                for idx, name in enumerate(current_order):
                    col_def = next((c for c in all_columns if c["name"] == name), None)
                    if not col_def:
                        continue

                    is_visible = name in visible

                    with ui.row().classes("items-center w-full px-2 py-1 bg-[#1f1f23] rounded"):
                        ui.checkbox(value=is_visible, on_change=lambda e, n=name: _toggle_column_visibility(n, visible, state)).props("dense")

                        ui.label(col_def.get("label") or name).classes("flex-1 text-sm")

                        # Move up / down buttons (reliable reordering)
                        ui.button(icon="arrow_upward", on_click=lambda i=idx: _move_column(i, -1, current_order, refresh_column_list)).props("flat dense size=xs")
                        ui.button(icon="arrow_downward", on_click=lambda i=idx: _move_column(i, 1, current_order, refresh_column_list)).props("flat dense size=xs")

        refresh_column_list()

        def apply_changes():
            state.list_column_order = current_order.copy()
            state.hidden_list_columns = {name for name in current_order if name not in visible}
            dialog.close()
            if state.view_mode == "list":
                main_content.refresh()

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Reset to Default", on_click=lambda: _reset_list_columns(state, dialog, current_order, visible, refresh_column_list)).props("flat")
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Apply", on_click=apply_changes, color="primary")

    dialog.open()


def _move_column(index: int, direction: int, order_list: list[str], refresh_fn):
    """Move a column up or down in the customizer list."""
    new_index = index + direction
    if 0 <= new_index < len(order_list):
        order_list[index], order_list[new_index] = order_list[new_index], order_list[index]
        refresh_fn()


def _reset_list_columns(state: AppState, dialog, current_order, visible, refresh_fn):
    """Reset list view columns to default order and visibility."""
    all_cols = _get_all_list_columns()
    all_names = [c["name"] for c in all_cols]

    # Use our curated defaults instead of showing everything
    default_order = DEFAULT_VISIBLE_LIST_COLUMNS.copy()

    # Append any new columns that aren't in our default list (for future-proofing)
    for name in all_names:
        if name not in default_order:
            default_order.append(name)

    current_order.clear()
    current_order.extend(default_order)
    visible.clear()
    visible.update(DEFAULT_VISIBLE_LIST_COLUMNS)  # only our defaults are visible
    state.list_column_order = default_order.copy()
    state.hidden_list_columns = set(all_names) - set(DEFAULT_VISIBLE_LIST_COLUMNS)
    refresh_fn()


def _toggle_column_visibility(name: str, visible_set: set, state: AppState):
    """Toggle visibility of a column in the customizer (live update)."""
    if name in visible_set:
        visible_set.remove(name)
    else:
        visible_set.add(name)


def _header_refresh() -> None:
    """Refresh the current catalog view from top bar."""
    state = get_state()
    if state:
        state.reload()
        main_content.refresh()


def _header_export_xml() -> None:
    """Export current selection (or all visible) as XML from top bar icon."""
    state = get_state()
    if not state:
        return
    clips = state.get_selected_videos()
    if not clips:
        clips = list(state.videos)
    if clips:
        _show_export_xml_dialog(state, clips)
    else:
        ui.notify("No clips to export", color="warning")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _open_settings_dialog() -> None:
    """Open the application settings dialog with multiple pages."""
    current_page = "general"

    with ui.dialog() as dialog, ui.card().classes("w-[640px]"):
        ui.label("Settings").classes("text-h5 mb-2")

        with ui.row().classes("w-full gap-4"):
            # === LEFT NAVIGATION ===
            with ui.column().classes("w-40 gap-1 pt-1"):
                nav_items = [
                    ("general", "General"),
                    ("proxy", "Proxy"),
                    ("ai", "AI"),
                    ("text_to_speech", "Text-to-Speech"),
                    ("translation", "Translation"),
                ]

                nav_buttons: dict[str, ui.button] = {}

                def switch_page(page: str):
                    nonlocal current_page
                    if page == current_page:
                        return
                    current_page = page
                    content_container.clear()
                    with content_container:
                        _render_settings_page(page, content_container)
                    # Update active styling for consistent selected look (filled bg + primary text)
                    for p, btn in nav_buttons.items():
                        if p == page:
                            btn.props("color=primary")
                            btn.classes(add="bg-zinc-800")
                        else:
                            btn.props(remove="color")
                            btn.classes(remove="bg-zinc-800")

                for page_id, label in nav_items:
                    btn = ui.button(
                        label,
                        on_click=lambda p=page_id: switch_page(p),
                    ).props("flat").classes("w-full justify-start text-sm")
                    if page_id == "general":
                        btn.props("color=primary")
                        btn.classes("bg-zinc-800")
                    nav_buttons[page_id] = btn

            # === RIGHT CONTENT AREA ===
            content_container = ui.column().classes("flex-1 gap-2 min-h-[420px]")

            with content_container:
                _render_settings_page("general", content_container)

        with ui.row().classes("justify-end w-full mt-4"):
            ui.button("Close", on_click=dialog.close).props("flat")

    dialog.open()


def _render_settings_page(page: str, container: ui.element) -> None:
    """Render one settings page into the given container."""
    if page == "general":
        _render_general_settings(container)
    elif page == "proxy":
        _render_proxy_settings(container)
    elif page == "ai":
        _render_ai_settings(container)
    elif page == "text_to_speech":
        _render_text_to_speech_settings(container)
    elif page == "translation":
        _render_translation_settings(container)
    else:
        ui.label("Unknown page").classes("text-negative")


def _render_general_settings(container: ui.element) -> None:
    """General settings (playback, tools detection, etc.)."""
    with container:
        ui.label("Playback").classes("text-base font-semibold mt-1 mb-2")

        use_quicklook = get_preference("use_quicklook", True)

        def save_quicklook(value: bool):
            set_preference("use_quicklook", value)

        ui.checkbox(
            "Use macOS Quick Look for video playback",
            value=use_quicklook,
            on_change=lambda e: save_quicklook(e.value)
        ).classes("mb-1")

        ui.label(
            "When enabled, videos open in the native Quick Look preview (like pressing Space in Finder). "
            "Automatically falls back to the default player if Quick Look fails on certain files (e.g. some Sony H.264 clips)."
        ).classes("text-xs text-grey-6 mb-4")

        # --- Advanced Tools Detection ---
        ui.label("Advanced Tools").classes("text-base font-semibold mt-4 mb-2")

        from minicat.core import video as video_core
        exif_available = video_core.is_exiftool_available()

        if exif_available:
            ui.label("✓ ExifTool detected — deep metadata extraction enabled for Fuji, Nikon, GoPro, DJI, Blackmagic, etc.").classes("text-sm text-positive")
        else:
            ui.label("ExifTool not found").classes("text-sm text-grey-6")
            ui.label(
                "Install ExifTool (brew install exiftool on macOS) for best results with Fuji Film Simulations, "
                "Nikon embedded data, GoPro telemetry, and many other cameras that don't write sidecar files."
            ).classes("text-xs text-grey-5")

        # --- Default Export Directory ---
        ui.label("Default Export Directory").classes("text-base font-semibold mt-5 mb-2")
        ui.label(
            "All AI Director / Journalist exports (XML + VO + script TXT, rendered clips) and subtitle burns will use this folder by default. "
            "New installs default to ~/CAT+TAG/Exports (dated subfolders are always created inside it). "
            "You can still override the path in individual dialogs."
        ).classes("text-xs text-grey-6 mb-2")

        current_export_dir = str(get_default_export_directory())

        export_dir_input = ui.input(
            "Default folder",
            value=current_export_dir,
        ).props("dense").classes("w-full mb-2")

        def _on_export_dir_change(e):
            try:
                val = e.value.strip()
                if val:
                    set_default_export_directory(val)
                    # Refresh display in case normalization happened
                    export_dir_input.value = str(get_default_export_directory())
                    export_dir_input.update()
                    ui.notify("Default export folder updated", color="positive")
            except Exception as ex:
                ui.notify(f"Invalid path: {ex}", color="negative")

        export_dir_input.on("blur", _on_export_dir_change)

        def _refresh_export_dir_display(new_path: str):
            export_dir_input.value = new_path
            export_dir_input.update()

        with ui.row().classes("gap-2 mb-2"):
            def choose_export_dir():
                try:
                    import webview
                    win = webview.active_window() or (webview.windows[0] if webview.windows else None)
                    if win:
                        result = win.create_file_dialog(
                            webview.FileDialog.FOLDER,
                            allow_multiple=False
                        )
                        if result:
                            chosen = result[0] if isinstance(result, (list, tuple)) else result
                            set_default_export_directory(chosen)
                            new_path = str(get_default_export_directory())
                            _refresh_export_dir_display(new_path)
                            ui.notify(f"Default export folder set to {new_path}", color="positive")
                    else:
                        # Fallback: just let user edit the field manually
                        ui.notify("Could not open native folder picker. You can paste a path directly into the field.", color="warning")
                except Exception as e:
                    ui.notify(f"Folder picker unavailable: {e}", color="warning")
                    print(f"[Settings] Export dir picker error: {e}")

            ui.button("Choose Folder...", icon="folder_open", on_click=choose_export_dir, color="primary").props("size=sm")

            def reset_export_dir():
                # Reset to the current canonical default (~/CAT+TAG/Exports)
                set_default_export_directory(DEFAULT_EXPORT_DIRECTORY)
                new_path = str(get_default_export_directory())
                _refresh_export_dir_display(new_path)
                ui.notify("Reset to ~/CAT+TAG/Exports", color="positive")

            ui.button("Reset to default", icon="refresh", on_click=reset_export_dir).props("size=sm outline")

        ui.label(
            "Tip: You can also manually paste a path into the field above and press Enter (changes are saved immediately on picker use)."
        ).classes("text-xs text-grey-5")


def _render_proxy_settings(container: ui.element) -> None:
    """Proxy export defaults settings (profile, timecode burn, subtitles burn)."""
    with container:
        ui.label("Proxy Export Defaults").classes("text-base font-semibold mt-1 mb-2")
        ui.label(
            "Default profile used by the 'Generate Proxies' tool and per-clip proxy exports. "
            "Each profile has a fixed resolution and container."
        ).classes("text-xs text-grey-6 mb-2")

        cur_preset = get_proxy_default_preset()
        cur_burn_tc = get_proxy_default_burn_timecode()
        cur_burn_subs = get_proxy_default_burn_subtitles()

        p_preset = ui.select(
            options=PROXY_PRESETS,
            value=cur_preset if cur_preset in PROXY_PRESETS else PROXY_PRESETS[0],
            label="Default Proxy Profile",
        ).props("dense").classes("w-full mb-3")

        p_burn_tc = ui.checkbox("Burn original timecode by default", value=cur_burn_tc).classes("mb-1")
        p_burn_subs = ui.checkbox("Burn subtitles by default", value=cur_burn_subs).classes("mb-3")

        def _save_proxy_defaults():
            set_proxy_default_preset(p_preset.value)
            set_proxy_default_burn_timecode(bool(p_burn_tc.value))
            set_proxy_default_burn_subtitles(bool(p_burn_subs.value))
            ui.notify("Proxy defaults saved", color="positive")

        ui.button("Save Proxy Defaults", on_click=_save_proxy_defaults, color="primary").props("size=sm").classes("mb-1")


def _render_ai_settings(container: ui.element) -> None:
    """AI-related settings (Gemini API key and model only)."""
    with container:
        # Security notice for API key
        env_key = os.getenv("GEMINI_API_KEY")
        if env_key:
            ui.label("✓ Using GEMINI_API_KEY from environment (.env or system env). Stored key is ignored.").classes("text-positive text-sm mb-2")
        else:
            ui.label("For better security, set GEMINI_API_KEY in a .env file instead of storing it here.").classes("text-warning text-sm mb-2")

        # AI Tag Suggestions (Gemini)
        ui.label("AI Tag Suggestions (Gemini)").classes("text-base font-semibold mt-1 mb-2")

        current_key = get_gemini_api_key() or "" if not env_key else ""

        gemini_key_input = ui.input(
            "Gemini API Key",
            value=current_key,
            placeholder="AIzaSy...",
            password=True,
        ).props("dense").classes("w-full mb-1")

        def save_gemini_key():
            if os.getenv("GEMINI_API_KEY"):
                ui.notify("Cannot save key while GEMINI_API_KEY is set in environment", color="warning")
                return

            key = gemini_key_input.value.strip() or None
            set_gemini_api_key(key)
            if key:
                ui.notify("Gemini API key saved (consider moving to .env for security)", color="positive")
            else:
                ui.notify("Gemini API key cleared", color="warning")

        ui.button("Save Key", on_click=save_gemini_key, color="primary").props("size=sm").classes("mb-1")

        test_result = ui.label("").classes("text-sm mb-2")

        async def test_gemini_key():
            key = gemini_key_input.value.strip()
            if not key:
                test_result.set_text("Please enter an API key first.")
                test_result.classes(replace="text-sm text-warning mb-2")
                return

            test_result.set_text("Testing key...")
            test_result.classes(replace="text-sm text-grey-6 mb-2")

            def _do_test():
                from google import genai
                client = genai.Client(api_key=key)
                list(client.models.list())

            try:
                import asyncio
                await asyncio.to_thread(_do_test)
                test_result.set_text("✓ API key is valid!")
                test_result.classes(replace="text-sm text-positive mb-2")
            except Exception as e:
                test_result.set_text(f"✗ {str(e)}")
                test_result.classes(replace="text-sm text-negative mb-2")

        ui.button("Test API Key", on_click=test_gemini_key, color="primary").props("size=sm outline").classes("mb-3")

        current_model = get_gemini_model()
        if current_model not in GEMINI_MODELS:
            current_model = DEFAULT_GEMINI_MODEL
            set_gemini_model(current_model)

        model_select = ui.select(
            options=GEMINI_MODELS,
            value=current_model,
            label="Gemini Model",
        ).props("dense").classes("w-full mb-1")

        def save_model():
            set_gemini_model(model_select.value)
            ui.notify(f"Model set to {model_select.value}", color="positive")

        ui.button("Save Model", on_click=save_model, color="primary").props("size=sm").classes("mb-2")

        ui.label(
            "gemini-2.5-flash = best balance (recommended)\n"
            "gemini-2.5-flash-lite = fastest & cheapest\n"
            "gemini-2.5-pro = most capable (slower & more expensive)"
        ).classes("text-xs text-grey-6 mb-4")


def _render_text_to_speech_settings(container: ui.element) -> None:
    """Text-to-Speech settings (voiceovers / narrations for AI Director and AI Journalist cuts)."""
    # TTS-related settings imports
    from minicat.core.settings import (
        get_tts_provider, set_tts_provider,
        get_tts_default_language, set_tts_default_language,
        get_tts_google_default_voice, set_tts_google_default_voice,
        get_tts_voice, set_tts_voice,
        get_preference, clean_tts_voice, clean_tts_language,
        get_gcp_credentials_path, set_gcp_credentials_path,
    )

    with container:
        # --- Text-to-Speech Settings ---
        ui.label("Text-to-Speech").classes("text-base font-semibold mt-1 mb-2")

        ui.label(
            "Choose which TTS to use. Piper is completely offline after the first download — no login, no cloud, no account required."
        ).classes("text-xs text-grey-6 mb-2")

        # --- Provider choice (drives everything below) ---
        current_provider = get_tts_provider()

        provider_options = {
            "local": "Piper TTS - fully offline, no cloud, recommended",
            "google": "Google Cloud TTS - Wavenet/Standard - requires login",
        }
        provider_select = ui.select(
            options=provider_options,
            value=current_provider if current_provider in provider_options else "local",
            label="TTS Provider",
        ).props("dense").classes("w-full mb-2")

        # Live status + provider-specific action area
        status_label = ui.label().classes("text-sm mb-1")
        help_label = ui.label().classes("text-xs text-grey-5 mb-2")

        # We will (re)populate these on provider change
        action_row_container = ui.row().classes("gap-2 items-center mb-1")
        extra_help_label = ui.label().classes("text-[10px] text-grey-5 mb-2")

        # Container for the "no gcloud CLI needed" credentials file picker (google only)
        google_creds_container = ui.column().classes("mt-1 mb-2 hidden")

        lang_select = None
        voice_select = None   # renamed from google_voice_select to be provider-agnostic
        save_btn = None
        test_btn = None

        def _get_current_provider() -> str:
            return provider_select.value or "local"

        def _refresh_tts_status():
            """Update the status line + help text for the currently selected provider."""
            prov = _get_current_provider()
            try:
                if prov == "local":
                    # Reset flag so a "Check Status" can trigger a fresh auto-install attempt
                    # (prevents permanent "offline" state after a transient install failure).
                    reset_piper_install_flag()
                    ensure_piper_package()
                    st = get_local_tts_status()
                else:
                    ensure_google_tts_package()
                    st = get_google_tts_status()

                ready = bool(st.get("ready"))
                if ready:
                    if prov == "local":
                        status_label.set_text("✓ Local offline TTS ready to go (Piper)")
                        status_label.classes(replace="text-sm text-positive mb-1")
                        help_label.set_text("Pick language + voice below. Models are downloaded automatically on first use.")
                        help_label.classes(replace="text-xs text-grey-6 mb-2")
                    else:
                        status_label.set_text("✓ Google Cloud TTS ready — voiceover generation enabled (WaveNet/Standard, 4M free tier).")
                        status_label.classes(replace="text-sm text-positive mb-1")
                        help_label.set_text("Ready for voiceovers. Set language + voice below and Save. Test uses a natural sentence in the selected language.")
                        help_label.classes(replace="text-xs text-grey-6 mb-2")
                else:
                    status_label.set_text(st.get("message", "Not ready"))
                    status_label.classes(replace="text-sm text-grey-6 mb-1")
                    if prov == "local":
                        help_label.set_text("Piper will auto-install on first use / status check. Use 'Prepare / Update voices' button below to pre-download models (or if it still shows offline, run 'uv pip install piper-tts' then Check Status).")
                        help_label.classes(replace="text-xs text-grey-5 mb-2")
                    else:
                        help_label.set_text("Click 'Log in with Google' below, complete the browser flow, then 'Check Status'.")
                        help_label.classes(replace="text-xs text-grey-5 mb-2")
            except Exception as st_err:
                status_label.set_text("TTS status check failed")
                status_label.classes(replace="text-sm text-negative mb-1")
                print(f"[TTS Settings] status error: {st_err}")

        def _rebuild_provider_actions():
            """Clear and rebuild the action buttons + extra help depending on the active provider."""
            action_row_container.clear()
            extra_help_label.set_text("")

            prov = _get_current_provider()
            with action_row_container:
                if prov == "local":
                    def prepare_local_voices():
                        try:
                            # Allow fresh install attempt (in case previous auto-install failed or user did manual)
                            reset_piper_install_flag()
                            lang = clean_tts_language((lang_select.value if lang_select else "en") or "en") or "en"
                            ui.notify(f"Preparing offline voice for {lang} ... (first time downloads ~30-80 MB)", color="info")

                            def _do_prep():
                                if not ensure_piper_package():
                                    raise RuntimeError(
                                        "piper-tts package is not installed. "
                                        "Please run in your terminal: uv pip install piper-tts"
                                    )
                                p = ensure_piper_voice_model(lang)
                                return p

                            import concurrent.futures
                            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                                fut = pool.submit(_do_prep)
                                model_path = fut.result()
                            ui.notify(f"Voice ready: {model_path.name}", color="positive")
                            _refresh_tts_status()
                        except Exception as ex:
                            ui.notify(f"Voice preparation failed: {ex}", color="negative")
                            print(f"[Piper] {ex}")

                    ui.button("Prepare / Update voices for current language", on_click=prepare_local_voices, color="primary").props("size=sm")
                    ui.button("Check Status", on_click=lambda: (_refresh_tts_status(), ui.notify("Local TTS status refreshed", color="info")), color="secondary").props("size=sm outline")
                    extra_help_label.set_text("Completely offline after download. No Google account or internet needed for generation.")
                else:
                    # Google Cloud path - make it easy even if gcloud CLI is missing
                    gcloud_bin = find_gcloud()

                    def do_launch_google_auth():
                        try:
                            if not gcloud_bin:
                                ui.notify("gcloud CLI not detected — use the buttons below to install it or pick a credentials file instead.", color="warning")
                                return
                            run_gcloud_auth_application_default()
                            ui.notify(
                                "Browser opened for Google login.\n"
                                "Complete the steps there, then return and click 'Check Status'.",
                                color="info", multi_line=True
                            )
                        except Exception as auth_ex:
                            ui.notify(f"Auth launch failed: {auth_ex}", color="negative")

                    def check_google_and_enable():
                        try:
                            ensure_google_tts_package()
                            new_status = get_google_tts_status()
                            ready = bool(new_status.get("ready"))
                            _refresh_tts_status()
                            _rebuild_google_creds_ui()
                            if ready:
                                if lang_select: lang_select.enable()
                                if voice_select: voice_select.enable()
                                if save_btn: save_btn.enable()
                                if test_btn: test_btn.enable()
                                ui.notify("✓ Google Cloud TTS ready — controls enabled.", color="positive")
                            else:
                                ui.notify(new_status.get("message", "Still not ready."), color="warning")
                        except Exception as chk_ex:
                            ui.notify(f"Check failed: {chk_ex}", color="negative")

                    if gcloud_bin:
                        ui.button("🔑 Log in with Google (gcloud auth)", on_click=do_launch_google_auth, color="primary").props("size=sm")
                    else:
                        # gcloud missing - offer easy ways to get it or bypass it completely
                        def open_install_page():
                            import webbrowser
                            webbrowser.open("https://cloud.google.com/sdk/docs/install")

                        ui.button("Open gcloud install instructions", on_click=open_install_page, color="secondary").props("size=sm outline")

                        if shutil.which("brew"):
                            def install_gcloud_brew():
                                def _run():
                                    try:
                                        ui.notify("Installing google-cloud-sdk via Homebrew (this can take a minute)...", color="info")
                                        result = subprocess.run(
                                            ["brew", "install", "google-cloud-sdk"],
                                            capture_output=True, text=True, timeout=300
                                        )
                                        if result.returncode == 0:
                                            ui.notify("gcloud installed! Click 'Check Status' below.", color="positive")
                                            # Give the shell/brew a moment
                                            import time; time.sleep(1.0)
                                        else:
                                            ui.notify("brew install finished with warnings. Try 'Check Status'.", color="warning")
                                    except Exception as ex:
                                        ui.notify(f"Homebrew install failed: {ex}", color="negative")
                                    # always refresh so the login button may appear
                                    _refresh_tts_status()
                                    _rebuild_provider_actions()
                                import threading
                                threading.Thread(target=_run, daemon=True).start()

                            ui.button("Install gcloud via Homebrew", on_click=install_gcloud_brew, color="primary").props("size=sm")

                    ui.button("Check Status", on_click=check_google_and_enable, color="secondary").props("size=sm outline")
                    extra_help_label.set_text("Tip: the 'Check Status' button also works if you previously logged in via terminal or set a credentials file below.")

        def _rebuild_google_creds_ui():
            """Show/hide and populate a credentials file picker that lets users authenticate
            with Google Cloud TTS using only a downloaded JSON key (no gcloud CLI install required).
            """
            google_creds_container.clear()
            if _get_current_provider() != "google":
                google_creds_container.classes(add="hidden")
                return
            google_creds_container.classes(remove="hidden")
            with google_creds_container:
                ui.label("Alternative: use a credentials file (no gcloud CLI needed)").classes("text-sm font-semibold mt-1")
                current_creds = get_gcp_credentials_path() or ""
                creds_input = ui.input(
                    "Service account key JSON path",
                    value=current_creds,
                    placeholder="Select or paste path to your Google Cloud JSON key...",
                ).props("dense").classes("w-full mb-1")

                def browse_creds():
                    try:
                        import tkinter as tk
                        from tkinter import filedialog
                        root = tk.Tk()
                        root.withdraw()
                        root.attributes("-topmost", True)
                        path = filedialog.askopenfilename(
                            title="Select Google Cloud credentials JSON",
                            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
                        )
                        root.destroy()
                        if path:
                            creds_input.value = path
                    except Exception as ex:
                        ui.notify(f"Could not open file dialog: {ex}. Paste the full path instead.", color="warning")

                with ui.row().classes("gap-2 mb-1"):
                    ui.button("Browse...", on_click=browse_creds, color="secondary").props("size=sm outline")

                    def save_creds_path():
                        val = (creds_input.value or "").strip()
                        try:
                            if val:
                                set_gcp_credentials_path(val)
                                ui.notify("Credentials file saved and will be used for TTS.", color="positive")
                            else:
                                set_gcp_credentials_path(None)
                                ui.notify("Cleared explicit credentials.", color="positive")
                        except Exception as ex:
                            ui.notify(str(ex), color="negative")
                        _refresh_tts_status()

                    ui.button("Save / Activate", on_click=save_creds_path, color="primary").props("size=sm")

                    def clear_creds():
                        creds_input.value = ""
                        set_gcp_credentials_path(None)
                        _refresh_tts_status()
                        ui.notify("Credentials cleared.", color="positive")

                    ui.button("Clear", on_click=clear_creds, color="secondary").props("size=sm outline")

                ui.label(
                    "How to get a key: Google Cloud Console → IAM & Admin → Service Accounts → Create key (JSON). "
                    "Give it the Cloud Text-to-Speech User role."
                ).classes("text-xs text-grey-5")

        # initial build of creds UI (will be empty unless google is selected)
        _rebuild_google_creds_ui()

        def _update_voice_options():
            """Swap the voice selector contents when language or provider changes."""
            if not voice_select or not lang_select:
                return
            prov = _get_current_provider()
            lang = clean_tts_language(lang_select.value) or "en"
            if prov == "local":
                voices = get_piper_voices_for_language(lang) or []
                if not voices:
                    voices = [("en_US-lessac-medium", "Lessac (clear, natural US English)")]
                voice_select.options = voices
                vals = [v[0] for v in voices]
                cur_val = clean_tts_voice(voice_select.value)
                if cur_val not in vals:
                    voice_select.value = vals[0]
            else:
                voices = get_google_voices_for_language(lang) or []
                if not voices:
                    voices = [("en-US-Wavenet-F", "Wavenet F (recommended)")]
                voice_select.options = voices
                vals = [v[0] for v in voices]
                cur_val = clean_tts_voice(voice_select.value)
                if cur_val not in vals:
                    voice_select.value = vals[0]
            voice_select.update()

        # Initial status + actions
        _refresh_tts_status()
        _rebuild_provider_actions()

        # React to provider changes: swap status, actions, and voice list
        def on_provider_changed(_e=None):
            _refresh_tts_status()
            _rebuild_provider_actions()
            _rebuild_google_creds_ui()
            # Also immediately persist the provider choice
            set_tts_provider(_get_current_provider())
            _update_voice_options()
            # Enable controls for the new provider (local is usually ready)
            prov = _get_current_provider()
            try:
                st = get_tts_status()
                if st.get("ready"):
                    if lang_select: lang_select.enable()
                    if voice_select: voice_select.enable()
                    if save_btn: save_btn.enable()
                    if test_btn: test_btn.enable()
            except Exception:
                pass

        provider_select.on_value_change(on_provider_changed)

        # Configurable defaults
        try:
            current_lang = get_tts_default_language()
            # Use the general voice getter (works across providers)
            current_voice = clean_tts_voice(get_tts_voice())

            # Auto-repair corrupted voice prefs (tuples/lists from previous select saves) so they don't
            # propagate to generators or cause attribute errors like 'tuple' has no 'endswith'.
            try:
                raw_voice_pref = get_preference("ai.tts_voice", None)
                if isinstance(raw_voice_pref, (list, tuple)):
                    set_tts_voice(current_voice or "en-US-Wavenet-F")
                raw_g_pref = get_preference("ai.tts_google_default_voice", None)
                if isinstance(raw_g_pref, (list, tuple)):
                    set_tts_google_default_voice(current_voice or "en-US-Wavenet-F")
            except Exception:
                pass

            try:
                from minicat.core.settings import SUPPORTED_LANGUAGES as _SETTINGS_LANGS
            except Exception:
                _SETTINGS_LANGS = None
            _supported = _SETTINGS_LANGS or [
                ("en", "English"),
                ("fi", "Finnish"),
                ("de", "German"),
                ("sv", "Swedish"),
                ("fr", "French"),
                ("es", "Spanish"),
            ]
            lang_options = {code: f"{name} ({code})" for code, name in _supported}
            safe_lang = current_lang if current_lang in lang_options else "en"
            lang_select = ui.select(
                options=lang_options,
                value=safe_lang,
                label="Default Voiceover Language",
            ).props("dense").classes("w-full mb-2")

            # Initial voice list depends on the (possibly just changed) provider
            prov = _get_current_provider()
            if prov == "local":
                raw_voice_options = get_piper_voices_for_language(safe_lang) or []
                if not raw_voice_options:
                    raw_voice_options = [("en_US-lessac-medium", "Lessac (clear, natural US English)")]
                label_for_voice = "Default Local Voice (offline)"
            else:
                raw_voice_options = get_google_voices_for_language(safe_lang) or []
                if not raw_voice_options:
                    raw_voice_options = [("en-US-Wavenet-F", "Wavenet F (recommended)")]
                label_for_voice = "Default Google Voice (WaveNet recommended for quality)"

            voice_values = [opt[0] for opt in raw_voice_options]
            safe_voice = current_voice if current_voice in voice_values else voice_values[0]

            voice_select = ui.select(
                options=raw_voice_options,
                value=None,
                label=label_for_voice,
            ).props("dense").classes("w-full mb-1")

            if safe_voice in voice_values:
                voice_select.value = safe_voice
            else:
                voice_select.value = raw_voice_options[0][0]

            lang_select.on_value_change(lambda _: _update_voice_options())

            def save_tts_settings():
                prov = _get_current_provider()
                set_tts_provider(prov)
                set_tts_default_language(clean_tts_language(lang_select.value))
                # Save in the general slot + the google-specific slot for compatibility
                set_tts_voice(voice_select.value)
                if prov == "google":
                    set_tts_google_default_voice(voice_select.value)
                ui.notify(f"TTS settings saved ({prov})", color="positive")

            with ui.row().classes("gap-2 mb-2"):
                save_btn = ui.button("Save TTS Settings", on_click=save_tts_settings, color="primary").props("size=sm")

                async def test_current_tts():
                    try:
                        prov = _get_current_provider()
                        test_lang = clean_tts_language(lang_select.value) or "en"
                        test_text = TTS_TEST_PHRASES.get(test_lang, TTS_TEST_PHRASES.get("en", "This is a test of the current text-to-speech settings."))
                        test_voice = clean_tts_voice(voice_select.value)

                        if prov == "local":
                            ensure_piper_package()
                        else:
                            ensure_google_tts_package()

                        status = get_tts_status()
                        if not status.get("ready", False):
                            ui.notify(status.get('message', 'Not ready for TTS test'), color="warning")
                            return

                        out_path = get_default_export_directory() / "tts_test_sample.mp3"
                        actual_out = await generate_narration_audio(
                            text=test_text,
                            language=test_lang,
                            output_path=out_path,
                            voice=test_voice,
                        )
                        ui.notify(f"Test sample saved to {actual_out.name}", color="positive")
                    except Exception as ex:
                        ui.notify(f"TTS test failed: {ex}", color="negative")
                        print(f"[TTS Test] {ex}")

                test_btn = ui.button("Test Current Voice", on_click=test_current_tts, color="secondary").props("size=sm outline")

            # Initial enable/disable based on readiness of the chosen provider
            try:
                st = get_tts_status()
                initial_ready = bool(st.get("ready"))
            except Exception:
                initial_ready = True if _get_current_provider() == "local" else False

            if not initial_ready:
                if lang_select: lang_select.disable()
                if voice_select: voice_select.disable()
                if save_btn: save_btn.disable()
                if test_btn: test_btn.disable()

            ui.label(
                "Local = completely offline (Piper). Google = best quality but requires one-time login and internet for generation."
            ).classes("text-xs text-grey-6 mb-1")

            ui.label(
                "Tip: Start with Local. Switch to Google Cloud only if you need the absolute highest naturalness and have set up the free tier."
            ).classes("text-xs text-warning mb-2")

        except Exception as tts_ex:
            ui.label(f"[TTS settings UI failed to load: {tts_ex}]").classes("text-negative text-xs")
            ui.button("Check Status", on_click=_refresh_tts_status, color="secondary").props("size=sm outline")


def _render_translation_settings(container: ui.element) -> None:
    """Translation settings (default language for 'Transcribe + Translate')."""
    from minicat.core.settings import (
        get_preference,
        set_preference,
        clean_tts_language,
    )

    with container:
        # --- Default Translation Language ---
        ui.label("Default Translation Language").classes("text-base font-semibold mt-1 mb-2")

        current_default_lang = get_preference("ai.default_translation_lang", "en")

        common_langs = [
            ("en", "English 🇬🇧"),
            ("fi", "Finnish 🇫🇮"),
            ("sv", "Swedish 🇸🇪"),
            ("es", "Spanish 🇪🇸"),
            ("de", "German 🇩🇪"),
            ("fr", "French 🇫🇷"),
            ("ru", "Russian 🇷🇺"),
            ("zh", "Chinese 🇨🇳"),
        ]

        lang_options = {code: label for code, label in common_langs}
        default_lang_select = ui.select(
            options=lang_options,
            value=current_default_lang if current_default_lang in lang_options else None,
            label="Default language for 'Transcribe + Translate'",
            clearable=True,
        ).props("dense").classes("w-full mb-1")

        custom_lang = ui.input(
            "Custom language code (e.g. 'et' for Estonian)",
            value=current_default_lang if current_default_lang not in lang_options else ""
        ).props("dense").classes("w-full mb-1")

        def save_default_translation_lang():
            lang = clean_tts_language(custom_lang.value or default_lang_select.value or "en") or "en"
            set_preference("ai.default_translation_lang", lang)
            ui.notify(f"Default translation language set to: {lang}", color="positive")

        ui.button("Save Default Language", on_click=save_default_translation_lang, color="primary").props("size=sm").classes("mb-2")


def _open_help_dialog() -> None:
    """Open a nicely formatted Help dialog with shortcuts, tips, and about info."""
    with ui.dialog() as dialog, ui.card().classes("w-[680px]"):
        ui.label("CAT+TAG Help").classes("text-h5 mb-4")

        # === Keyboard Shortcuts ===
        ui.label("Keyboard Shortcuts").classes("text-base font-semibold mt-2 mb-2")

        shortcuts = [
            ("?", "Open this Help dialog"),
            ("Ctrl/Cmd + I", "Open Import Wizard"),
            ("/", "Focus the top search bar"),
            ("Ctrl/Cmd + A", "Select all visible clips"),
            ("Escape", "Clear selection or close dialogs"),
        ]

        with ui.column().classes("gap-1 mb-4"):
            for key, action in shortcuts:
                with ui.row().classes("items-center gap-3"):
                    ui.label(key).classes("font-mono text-sm bg-[#222] px-2 py-0.5 rounded w-32 text-center")
                    ui.label(action).classes("text-sm")

        ui.separator().classes("my-3")

        # === AI Journalist (Single) + AI Director (Multi) ===
        ui.label("AI Tools").classes("text-base font-semibold mt-2 mb-2")

        with ui.column().classes("gap-1 mb-3 text-sm"):
            ui.markdown("""
**Single clip — AI Journalist Cut:**
- Transcribe the clip first
- Open **AI Journalist Cut** from the inspector
- Generate multiple versions with different tones/purposes
- When a narration style is selected you can set **Narration min/max seconds** (total spoken duration budget for the script) and **Min/Max bridges** (how many conceptual narration passages to include).
- Export Premiere XMEML v4 or self-contained MP4/WAV. The "Export File" button always bundles the rich script TXT (even without narration).
- Exported rich script TXT files (SELECTED SEGMENTS + full transcript) are generated in the same language as the transcripted/scripted content (e.g. full Finnish UI labels/structure when using a fi transcript).

**Multi-clip — AI Director:**
- Select 2+ transcribed clips
- Click **AI Director — Build Story**
- The AI Director receives one combined, labeled transcript (every segment clearly marked C1, C2...)
- It builds narrative versions by intercutting verbatim material across the different sources (transcript-only, no audio)
- Each version can include an AI-written narration script (voiceovers).
- In the generation dialog (when narration style chosen) you can control **Narration min/max seconds** (total spoken time across all bridges) and **Min/Max number of bridges** (exact count of discrete narration items in the interleaved narrative_elements).

**Exporting from AI Director (or saved stories):**
- The main **"Export XML"** button (and loading a saved AIStory_*.json) always produces a fresh dated subfolder inside your default export directory (~/CAT+TAG/Exports by default).
- Inside that folder you always get:
  - The multi-source XMEML XML
  - The full rich **"AI DIRECTOR — MULTI-CLIP SCRIPT.txt"** (attributed clips + narration bridges with source filenames, timings, reasons, etc.) — now localized to the scripted language when applicable.
  - Voiceover audio (Narration.wav + Narration_BridgeNN.wav as 44.1kHz stereo WAV via Piper, or MP3 via Google) **when narration is present**.
- Use **"XML + Voiceover Audio"** for custom language/voice + live progress bar per bridge.
- **Settings → Text to Speech** tab: choose Piper (local/offline, recommended) vs Google, default language/voice, test voices, prepare Piper models, Google credentials, etc. All TTS settings live here (moved out of the old Translation tab).
            """)

        ui.separator().classes("my-3")

        # === Multi-Select Tools ===
        ui.label("Multi-Select Tools (2+ clips)").classes("text-base font-semibold mt-2 mb-2")

        multi_tips = [
            "AI Director — Build Story: Let the AI Director intercut verbatim moments across multiple clips into coherent narrative versions. In the dialog you can now budget total narration seconds (min/max) and exact min/max bridge count when using a narration style. 'Export XML' (or load saved story) always creates a dated subfolder with the XML + the full rich 'AI DIRECTOR — MULTI-CLIP SCRIPT.txt' (clips + bridges with attributions, language-matched to your transcript). Voiceovers (WAVs) are added when narration is present.",
            "Copy Clips + XML: Copy original media + generate a relinked Premiere XML to any folder",
            "Batch Edit: Camera, Location, Lens, Tags, and Client assignment (smart locking when values match)",
            "DELETE FROM LIBRARY vs DELETE FROM DISK (strong confirmation required for disk deletion)",
        ]

        for tip in multi_tips:
            with ui.row().classes("gap-2 mb-1"):
                ui.icon("check_circle", size="1rem").classes("text-primary mt-0.5")
                ui.label(tip).classes("text-sm")

        ui.separator().classes("my-3")

        # === Import Wizard ===
        ui.label("Import Wizard").classes("text-base font-semibold mt-2 mb-2")

        import_tips = [
            "Supports existing Projects and Clients (or create new during import)",
            "Optional AI auto-tagging and transcription during import",
            "Reads rich camera metadata from sidecar XML (especially Sony)",
        ]

        for tip in import_tips:
            with ui.row().classes("gap-2 mb-1"):
                ui.icon("upload_file", size="1rem").classes("text-primary mt-0.5")
                ui.label(tip).classes("text-sm")

        ui.separator().classes("my-3")

        # === Tips & Workflow ===
        ui.label("Tips & Workflow").classes("text-base font-semibold mt-2 mb-2")

        tips = [
            "Transcribe clips before using AI Journalist (single or multi).",
            "One processed transcription proxy audio file per clip (`<catalog>/audio/0000XX.m4a`) is reused for transcription + all AI listening (24 kHz mono AAC 64k + peak normalization to -3 dB).",
            "Use the left drawer (now high-density video-editor aesthetic) to filter by Projects, Clients, Cameras, Locations, and Tags.",
            "AI narration (Journalist & Director): directly set min/max total narration seconds and min/max number of bridges in the generation dialog when a narration style is active.",
            "The top-left folder icon opens the catalog switcher (icon-only for compactness). Default catalog is ~/CAT+TAG .",
            "If thumbnails/storyboards or transcriptions seem 'lost' from clip view/inspector: clear filters (or search by filename) — only ~2000 clips are loaded at once (newest first by default). Use 'Rebuild Previews + Metadata' in inspector for selected clips. Re-transcribe if needed (re-uses the processed transcription proxy audio).",
            "Everything stays 100% local — your media and API calls never leave your machine except when you explicitly use AI features.",
        ]

        for tip in tips:
            with ui.row().classes("gap-2 mb-1"):
                ui.icon("lightbulb", size="1rem").classes("text-primary mt-0.5")
                ui.label(tip).classes("text-sm")

        ui.separator().classes("my-3")

        # === Settings ===
        ui.label("Settings").classes("text-base font-semibold mt-2 mb-2")

        settings_tips = [
            "Settings (gear icon) now has a dedicated **Text to Speech** tab — all Piper/Google voiceover settings (provider, default language + voice, test button, Prepare voices, Google auth/credentials file) live here.",
            "AI Director multi exports (Export XML, load saved story, etc.) always create a fresh dated subfolder in your default export directory (~/CAT+TAG/Exports by default) and include the rich script TXT.",
            "Default export folder, proxies, burn-in options, etc. are in the other tabs.",
        ]

        for tip in settings_tips:
            with ui.row().classes("gap-2 mb-1"):
                ui.icon("settings", size="1rem").classes("text-primary mt-0.5")
                ui.label(tip).classes("text-sm")

        ui.separator().classes("my-3")

        # === About ===
        ui.label("About").classes("text-base font-semibold mt-2 mb-2")

        with ui.column().classes("text-sm text-grey-5 gap-1"):
            ui.label("CAT+TAG — Personal video catalog + AI tools")
            ui.label("Built for people who shoot a lot of footage and need to actually find and repurpose things later.")
            ui.label("100% local. No cloud. No telemetry. Full creative control.")
            ui.label("Copyright © 2026 Mikko Niittymäki")

        with ui.row().classes("justify-end mt-6"):
            ui.button("Close", on_click=dialog.close).props("flat")

    dialog.open()


def _select_all_visible(state: AppState) -> None:
    """Select all currently visible clips in the grid."""
    for v in state.videos:
        if v.id:
            state.selected_ids.add(v.id)
    if state.videos and state.videos[0].id:
        state.selected = state.videos[0]

    # Ensure the right inspector drawer opens and shows the multiview
    # (batch tools, AI Journalist, Delete Media, etc.) when 2+ items are selected.
    # Use the central helper so grid + inspector + drawer + toolbar count all sync.
    state._refresh_selection_visuals()


def _remove_filter(state: AppState, filter_type: str) -> None:
    """Remove a specific active filter and refresh the view."""
    if filter_type in ("Project", "Projects"):
        state.filters.project = None
    elif filter_type == "Camera":
        state.filters.camera = None
    elif filter_type == "Location":
        state.filters.location = None
    elif filter_type == "Tags":
        state.filters.tags = None
    elif filter_type == "Search":
        state.filters.text = None

    state.reload()
    main_content.refresh()


def _rename_project_dialog(state: AppState, current_name: str):
    with ui.dialog() as dialog, ui.card().classes("w-80"):
        ui.label("Rename Project").classes("text-h6 mb-2")
        new_name = ui.input("New name", value=current_name)
        def do_rename():
            name = new_name.value.strip()
            if name and name != current_name:
                updated = db.rename_project(state.catalog_root, current_name, name)
                dialog.close()
                if state.filters.project and current_name in state.filters.project:
                    state.filters.project = [name if p == current_name else p for p in state.filters.project]
                refresh_all_ui(state)
                ui.notify(f"Renamed. Updated {updated} clips.", color="positive")
            else:
                ui.notify("Enter a different name.", color="warning")
        with ui.row().classes("justify-end gap-2 mt-4"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Rename", on_click=do_rename, color="primary")
    dialog.open()


def _delete_project_dialog(state: AppState, current_name: str):
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(f"Delete Project “{current_name}”?").classes("text-h6 mb-1")
        ui.label("This will permanently delete the project record from the database (and its client associations).").classes("text-xs text-grey-6 mb-3")

        choice = ui.radio(
            ["Remove project from clips (keep clips)", "Delete all clips in this project"],
            value="Remove project from clips (keep clips)"
        )
        def do_delete():
            also_delete = "Delete all" in choice.value
            if also_delete:
                # Cleanup artifacts for clips that will be deleted (DB delete doesn't clean files)
                try:
                    from minicat.core.video import cleanup_all_generated_files_for_clip
                    clips_in_project = [v for v in state.videos if getattr(v, "project", None) == current_name]
                    for v in clips_in_project:
                        if v.id:
                            try:
                                cleanup_all_generated_files_for_clip(v.id, state.catalog_root, original_filename=v.filename)
                            except Exception as cl_ex:
                                print(f"[Delete Project] Artifact cleanup failed for clip {v.id}: {cl_ex}")
                except Exception as ex:
                    print(f"[Delete Project] Pre-cleanup failed: {ex}")
            affected = db.delete_project(state.catalog_root, current_name, also_delete_clips=also_delete)
            dialog.close()
            state.clear_filters()
            refresh_all_ui(state)
            ui.notify(
                f"Deleted project and {affected} clips." if also_delete else f"Removed project from {affected} clips.",
                color="negative" if also_delete else "positive"
            )
            _schedule_orphan_cleanup(state)
        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Delete", on_click=do_delete, color="negative")
    dialog.open()


def _show_rich_project_dialog(state: AppState, name: str):
    proj = db.get_project_with_stats(state.catalog_root, name)

    with ui.dialog() as dialog, ui.card().classes("w-[520px]"):
        ui.label(f"Project: {proj.name}").classes("text-h5 mb-2")

        with ui.row().classes("gap-4 text-sm mb-4"):
            ui.label(f"{proj.clip_count} clips")
            dur_str = format_duration_timecode(proj.total_duration)
            ui.label(f"{dur_str} total")

        with ui.column().classes("gap-2"):
            start = ui.input("Start Date", value=str(proj.start_date) if proj.start_date else "")
            end = ui.input("End Date", value=str(proj.end_date) if proj.end_date else "")

            # New multi-client support (rich Clients)
            all_clients = db.get_clients(state.catalog_root)
            current_client_names = proj.clients or ([proj.client] if proj.client else [])
            client_select = ui.select(
                options={c.id: c.name for c in all_clients},
                value=[c.id for c in all_clients if c.name in current_client_names],
                label="Clients (can belong to multiple)",
                multiple=True
            ).props("use-chips")

            def create_new_client():
                with ui.dialog() as new_client_dialog, ui.card().classes("w-[420px] q-pa-md"):
                    ui.label("New client").classes("text-h6 mb-4")

                    c_name = ui.input("Name *").props("autofocus dense").classes("w-full mb-3")

                    ui.label("Contact information").classes("text-sm text-grey-5 mb-2 mt-1")

                    with ui.row().classes("gap-3 w-full"):
                        c_contact = ui.input("Contact person").props("dense").classes("flex-1")
                        c_phone = ui.input("Phone").props("dense").classes("flex-1")

                    c_email = ui.input("Email").props("dense").classes("w-full mt-3")

                    def do_create_client():
                        name = c_name.value.strip()
                        if not name:
                            ui.notify("Client name is required", color="warning")
                            return

                        new_client = Client(
                            name=name,
                            contact_person=c_contact.value.strip() or None,
                            email=c_email.value.strip() or None,
                            phone=c_phone.value.strip() or None,
                        )
                        saved = db.create_or_update_client(state.catalog_root, new_client)
                        ui.notify(f"Client '{saved.name}' created", color="positive")
                        new_client_dialog.close()

                        # Refresh project dialog with the new client pre-selected
                        dialog.close()
                        _show_rich_project_dialog(state, proj.name)

                    with ui.row().classes("justify-end gap-2 mt-6 w-full"):
                        ui.button("Cancel", on_click=new_client_dialog.close).props("flat")
                        ui.button("Create client", on_click=do_create_client, color="primary")

            ui.button("Create New Client", icon="add", on_click=create_new_client).props("size=sm flat dense").classes("mt-1")

            director = ui.input("Director", value=proj.director or "")
            producer = ui.input("Producer", value=proj.producer or "")
            editor = ui.input("Editor", value=proj.editor or "")
            ops = ui.input("Camera Operators", value=", ".join(proj.camera_operators))
            loc = ui.input("Location", value=proj.location or "")
            status = ui.select(
                ["Pre-production", "Production", "Post-production", "Delivered", "Archived"],
                value=proj.status
            )
            notes = ui.textarea("Notes", value=proj.notes or "")

        def save():
            proj.start_date = date.fromisoformat(start.value) if start.value else None
            proj.end_date = date.fromisoformat(end.value) if end.value else None

            # Handle multi-client assignment (new system)
            selected_client_ids = client_select.value or []
            db.set_project_clients(state.catalog_root, proj.name, selected_client_ids)

            # Keep legacy single client field populated with first client for compatibility
            if selected_client_ids:
                first_client = next((c for c in all_clients if c.id in selected_client_ids), None)
                proj.client = first_client.name if first_client else None
            else:
                proj.client = None

            proj.director = director.value.strip() or None
            proj.producer = producer.value.strip() or None
            proj.editor = editor.value.strip() or None
            proj.camera_operators = [x.strip() for x in ops.value.split(",") if x.strip()]
            proj.location = loc.value.strip() or None
            proj.status = status.value
            proj.notes = notes.value.strip() or None

            db.create_or_update_project(state.catalog_root, proj)

            dialog.close()
            state.all_projects = db.get_distinct_values(state.catalog_root, "project")
            refresh_all_ui(state)
            ui.notify("Project saved", color="positive")

        with ui.row().classes("justify-between gap-2 mt-4 w-full"):
            ui.button("Delete", on_click=lambda: (_delete_project_dialog(state, proj.name), dialog.close()), color="negative").props("flat")
            with ui.row().classes("gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save", on_click=save, color="primary")

    dialog.open()


def _show_export_xml_dialog(state: AppState, selected: list[Video]) -> None:
    """Dialog for exporting the current selection as XML timeline.
    
    Uses the improved Premiere-native XMEML exporter (same as AI Journalist)
    when all selected clips come from the same source file. Falls back to
    legacy FCP7 format for mixed-source selections.
    """
    from datetime import datetime
    from pathlib import Path as _Path

    count = len(selected)
    total_dur = sum(v.duration or 0 for v in selected)
    default_name = f"CAT+TAG-{datetime.now().strftime('%Y%m%d-%H%M')}"

    # Try to guess a good fps from the clips
    fps_values = [v.fps for v in selected if v.fps]
    default_fps = fps_values[0] if fps_values else 24.0

    with ui.dialog() as dialog, ui.card().classes("w-[520px]"):
        ui.label("Export XML Timeline").classes("text-h5 mb-2")
        dur_str = format_duration_timecode(total_dur)
        ui.label(f"{count} clips  •  {dur_str} total").classes("text-caption text-grey-6 mb-4")

        seq_name = ui.input("Sequence name", value=default_name).classes("w-full mb-2")

        fps_input = ui.number("Timeline FPS", value=default_fps, min=1, max=120, step=1).classes("w-full mb-2")

        ui.label("Order: Clips will appear in the same order as the current grid (respecting your sort/filter).").classes("text-xs text-grey-6 mb-4")

        def do_export():
            name = seq_name.value.strip() or default_name
            fps = float(fps_input.value or 24.0)

            # Ask user where to save using native dialog when possible
            try:
                import webview
                win = webview.active_window() or (webview.windows[0] if webview.windows else None)
                if win:
                    res = win.create_file_dialog(
                        webview.FileDialog.SAVE,
                        directory=str(_Path.home()),
                        save_filename=f"{name}.xml",
                        file_types=("XML Files (*.xml)",)
                    )
                    if not res:
                        dialog.close()
                        return
                    out_path = _Path(res[0] if isinstance(res, (list, tuple)) else res)
                else:
                    raise RuntimeError("No webview window")
            except Exception:
                # Fallback: use a simple path in home
                out_path = _Path.home() / f"{name}.xml"

            if not str(out_path).lower().endswith(".xml"):
                out_path = out_path.with_suffix(".xml")

            try:
                # Use the same high-quality Premiere XMEML exporter as the AI Journalist
                # when all selected clips come from the same source file.
                from pathlib import Path as _Path2
                source_paths = {str(_Path2(v.path).resolve()) for v in selected}
                
                if len(source_paths) == 1:
                    # All clips from same source → use the improved journalist-style exporter
                    source_path = list(source_paths)[0]
                    
                    # Convert full clips into "cut segments" (full duration each)
                    cut_segments = []
                    for v in selected:
                        dur = v.duration or 0
                        cut_segments.append({
                            "source_in": 0.0,
                            "source_out": dur,
                            "text": v.filename,
                            "reason": "Full clip from topbar export"
                        })
                    
                    from minicat.ai.xmeml_exporter import generate_xmeml
                    generate_xmeml(
                        cut_segments=cut_segments,
                        source_video_path=source_path,
                        output_path=out_path,
                        sequence_name=name,
                        width=getattr(selected[0], "width", None) or 1920,
                        height=getattr(selected[0], "height", None) or 1080,
                        audio_channels=getattr(selected[0], "audio_channels", None),
                    )
                    used_improved = True
                else:
                    # Mixed sources → fall back to legacy FCP7 exporter
                    export_fcp7_xml(selected, out_path, sequence_name=name, fps=fps)
                    used_improved = False
                
                dialog.close()
                
                if used_improved:
                    ui.notify(f"Exported {count} clips → {out_path.name} (improved Premiere structure)", color="positive", duration=6)
                else:
                    ui.notify(f"Exported {count} clips → {out_path.name} (legacy FCP7 format)", color="positive", duration=6)
                # Optionally reveal the file
                try:
                    import platform
                    import subprocess
                    if platform.system() == "Darwin":
                        subprocess.run(["open", "-R", str(out_path)])
                except Exception:
                    pass
            except Exception as ex:
                ui.notify(f"Export failed: {ex}", color="negative")
                print(f"[XML Export] Error: {ex}")

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Export XML", icon="download", on_click=do_export, color="primary")

    dialog.open()


def show_ffmpeg_required_dialog():
    """
    Show a friendly, prominent dialog explaining that ffmpeg is required.
    Works both from normal UI code and from inside async import flows.
    """
    from minicat.core.env import get_ffmpeg_install_hint, is_frozen

    hint = get_ffmpeg_install_hint()
    mode = "Bundled app" if is_frozen() else "Development run"

    with ui.dialog() as dialog, ui.card().classes("w-[520px]"):
        ui.label("ffmpeg Required").classes("text-h5 mb-2 text-negative")
        ui.label(f"Running as: {mode}").classes("text-xs text-grey-6 mb-3")

        with ui.scroll_area().classes("max-h-[220px]"):
            for line in hint.split("\n"):
                ui.label(line).classes("text-body2")

        ui.separator().classes("my-3")

        with ui.row().classes("justify-end gap-2 w-full"):
            ui.button("Close", on_click=dialog.close).props("flat")
            # On macOS we can try to be extra helpful
            def open_terminal_and_close():
                try:
                    import subprocess
                    subprocess.Popen(["open", "-a", "Terminal"])
                except Exception:
                    pass
                dialog.close()

            ui.button(
                "Open Terminal",
                icon="terminal",
                on_click=open_terminal_and_close,
                color="primary",
            )

    dialog.open()


def _show_burn_subtitles_dialog(v: Video) -> None:
    """Reusable burn subtitles dialog that can be called from both full and minimal inspector views."""
    state = get_state()
    if not state or not v:
        ui.notify("No clip selected", color="warning")
        return

    # Re-fetch fresh data
    fresh = db.get_video_by_path(state.catalog_root, v.path)
    if fresh:
        v = fresh

    translations = getattr(v, "translated_transcriptions", {}) or {}
    has_trans = bool(getattr(v, "transcription_segments", None))

    lang_options = {"original": "Original"}
    orig_lang = getattr(v, "original_language", None)
    if orig_lang:
        lang_options["original"] = f"Original ({orig_lang})"

    for code in sorted(translations.keys()):
        lang_options[code] = code.upper()

    with ui.dialog() as burn_dialog, ui.card().classes("w-[520px]"):
        ui.label("Burn Subtitles into Video").classes("text-h6 mb-2")

        if not has_trans:
            ui.label("⚠️ No transcription available yet. Transcribe the clip first.").classes("text-red-400 text-sm mb-2")

        burn_lang = ui.select(
            options=lang_options,
            value="original" if has_trans else None,
            label="Subtitle Language"
        ).props("dense").classes("w-full mb-2")

        out_folder = ui.input("Output folder", value=str(get_default_export_directory())).props("dense")
        out_name = ui.input("Output filename", value=f"{v.filename.rsplit('.',1)[0]}_burned.mp4").props("dense")

        async def do_burn():
            try:
                lang = burn_lang.value or "original"
                if lang == "original":
                    segs = getattr(v, "transcription_segments", None)
                else:
                    segs = translations.get(lang)

                if not segs:
                    ui.notify("No transcription for selected language", color="warning")
                    return

                from minicat.ai.transcriber import segments_to_srt

                fps = getattr(v, "fps", None)
                srt_content = segments_to_srt(segs, fps=fps)

                folder = Path(out_folder.value).expanduser()
                folder.mkdir(parents=True, exist_ok=True)
                final_video = folder / out_name.value

                srt_out = folder / f"{out_name.value.rsplit('.', 1)[0]}.srt"
                srt_out.write_text(srt_content, encoding="utf-8")

                burn_dialog.close()

                progress_dialog = ui.dialog()
                with progress_dialog, ui.card().classes("w-[520px]"):
                    ui.label("Burning subtitles...").classes("text-h6 mb-2")
                    progress_bar = ui.linear_progress(value=0, show_value=True).classes("w-full")
                    status_label = ui.label("Starting ffmpeg...").classes("text-sm mt-2")

                progress_dialog.open()

                def progress_callback(percent: float):
                    progress_bar.value = percent
                    if v.duration and v.duration > 0:
                        processed = percent * v.duration
                        status_label.text = f"{processed:.1f}s / {v.duration:.1f}s"

                await asyncio.to_thread(
                    video.burn_subtitles_to_video,
                    v.path,
                    srt_out,
                    final_video,
                    use_ebu_style=True,
                    progress_callback=progress_callback,
                )

                progress_dialog.close()
                ui.notify(f"Burned video + .srt saved to {folder}", color="positive")

            except Exception as ex:
                try:
                    progress_dialog.close()
                except Exception:
                    pass
                ui.notify(f"Burn failed: {ex}", color="negative")
                print(f"[Burn Subs] Error: {ex}")
            finally:
                # Cleanup any temp ASS file created inside burn_subtitles_to_video
                # (the function itself tries to clean, but we double-check here)
                pass  # The core function now handles its own temp file cleanup reliably

        with ui.row().classes("gap-2 mt-4"):
            ui.button("Cancel", on_click=burn_dialog.close).props("flat")
            if has_trans:
                ui.button("Burn Subtitles", on_click=do_burn, color="primary")
            else:
                ui.button("Burn Subtitles", on_click=lambda: ui.notify("Transcribe first", color="warning")).props("disable")

    burn_dialog.open()


def _show_ai_journalist_cut_dialog(v) -> None:
    """
    AI Journalist Cut dialog.
    Lets the user generate professional short versions of an interview
    using the transcript, then export as:
      - Premiere-ready FCP7 XML
      - Human-readable TXT
      - Actual rendered video file (MP4) containing exactly the selected clips
    """
    from minicat.core import video as video_core
    from pathlib import Path as _Path  # for temp audio dir

    if not getattr(v, "transcription_segments", None):
        ui.notify("This clip has no transcription. Transcribe it first.", color="warning")
        return

    # Gather available languages
    translations = getattr(v, "translated_transcriptions", {}) or {}
    lang_options = {"original": "Original"}
    orig_lang = getattr(v, "original_language", None)
    if orig_lang:
        lang_options["original"] = f"Original ({orig_lang})"

    for code in sorted(translations.keys()):
        lang_options[code] = code.upper()

    current_lang = getattr(v, "_current_transcription_lang", "original")
    if current_lang not in lang_options:
        current_lang = "original"

    # Get segments for the chosen language
    def _get_segments_for_lang(lang: str):
        if lang == "original":
            return getattr(v, "transcription_segments", []) or []
        return translations.get(lang, []) or []

    # Default values
    default_purpose = "News Package"
    default_tone = "newsroom"

    # Use the actual clip duration as the hard upper limit.
    # The user should never be able to request a cut longer than the source material.
    clip_duration = getattr(v, "duration", None) or 0
    if clip_duration > 0:
        max_possible = int(clip_duration)
    else:
        # Fallback only if duration metadata is missing for some reason
        max_possible = 600
        console.print("[yellow]Warning: Could not determine clip duration. Using 600s as safe upper limit.[/]")

    # Sensible default: 90s or the full clip (whichever is smaller)
    default_max_seconds = min(90, max_possible)

    dlg = ui.dialog()
    with dlg, ui.card().classes("w-[720px]"):
        ui.label("AI Journalist Cut").classes("text-h5 mb-1")
        ui.label(v.filename).classes("text-sm text-grey-6 mb-3")

        # Controls
        with ui.row().classes("gap-4 items-end w-full"):
            min_dur = ui.number(
                "Min duration (seconds)",
                value=30,
                min=5,
                max=max_possible,
                step=5,
            ).props("dense").classes("w-48")

            max_dur = ui.number(
                "Max duration (seconds)",
                value=default_max_seconds,
                min=15,
                max=max_possible,
                step=5,
            ).props("dense").classes("w-48")

        purpose = ui.select(
            {
                "News Package": "News Package (tight broadcast package: strong open, key soundbites, closer)",
                "Social Media Teaser": "Social Media Teaser (punchy curiosity hook — arresting first 8 seconds)",
                "Best Soundbites / Quotes": "Best Soundbites / Quotes (most quotable lines, intercut for maximum impact)",
                "Emotional / Human Story": "Emotional / Human Story (personal stakes & emotional contrast between voices)",
                "In-depth Highlight": "In-depth Highlight (deeper synthesis of key or surprising territory)",
                # new 2026
                "Investigative Cold Open": "Investigative Cold Open (high-suspense hook: facts + cliffhangers into the mystery)",
                "Expert Manifesto / Manifesto Call": "Expert Manifesto / Manifesto Call (mission-driven claims to collective call-to-action)",
                "Character Retrospective": "Character Retrospective (nostalgic, reflective look back on life, career or era)",
                "Social Jump-Cut Strip": "Social Jump-Cut Strip (ultra-aggressive TikTok/Shorts bursts — zero dead air)",
                "Three-Act Underdog Arc": "Three-Act Underdog Arc (strict 3-act underdog journey: struggle → epiphany → triumph)",
            },
            value=default_purpose,
            label="Purpose",
        ).props("dense").classes("w-full mt-2")

        tone = ui.select(
            {
                "newsroom": "Newsroom (tight & factual)",
                "flexible": "Flexible / Creative (atmospheric & juxtapositions)",
                "documentary": "Documentary (story-driven & atmospheric)",
                "corporate": "Corporate (professional & polished)",
                "commercial": "Commercial (persuasive & brand-driven)",
                "rewrite": "Verbatim Scriptwriter (new story from real spoken material only)",
                # new 2026 creative directorial styles (available for Journalist too)
                "investigative_hook": "Investigative Hook (cinematic, cliffhangers)",
                "masterclass": "Masterclass (authoritative, long-form argument)",
                "confessional": "Confessional (vulnerable, intimate, preserves stumbles)",
                "engagement_bomb": "Engagement Bomb (ultra-fast TikTok/Shorts)",
                "subversive": "Subversive (witty, ironic, humanizing)",
                "visual_poem": "Visual Poem (atmospheric, rhythmic, musical)",
                "manifesto": "Manifesto (inspirational, mobilizing call-to-action)",
                "underdog": "Underdog (strict 3-act: struggle → pivot → triumph)",
                "analytical": "Analytical (cause-effect, data, objective)",
                "legacy": "Legacy (warm, wistful, reflective past-tense)",
            },
            value=default_tone,
            label="Tone",
        ).props("dense").classes("w-full mt-1")

        clean_fillers = ui.checkbox(
            "Remove filler words (cleaner, more polished sentences)",
            value=False
        ).props("dense").classes("mt-1 mb-2")

        # Same VoiceOver/Narration style options as AI Director
        narration_style = ui.select(
            {
                None: "No narration / voiceover script",
                "omniscient": "Omniscient (3rd-person journalistic — objective bridges, context, balance)",
                "subjective": "Subjective (1st-person reflective / essay-film style)",
                "explainer": "Explainer (high-energy, punchy short-form / social hook voice)",
            },
            value=None,
            label="Narration / Voiceover Style (optional)"
        ).props("dense").classes("w-full mt-1")

        ui.label("If a style is chosen, the AI will write a style-aware narration script (voiceover) for the cut in the transcript language. This matches the full VoiceOver/Narration options from AI Director. The script can be rendered as audio or used as text.").classes("text-xs text-blue-400 mb-2")

        # Narration length and bridge count controls (only apply when a narration style is selected)
        with ui.row().classes("gap-2 mt-1"):
            narr_min_sec = ui.number("Narration min (s)", value=5, min=0, step=5).props("dense").classes("w-28").tooltip("Minimum total spoken seconds for the generated narration script")
            narr_max_sec = ui.number("Narration max (s)", value=45, min=0, step=5).props("dense").classes("w-28").tooltip("Maximum total spoken seconds for the narration script")
            narr_min_bridges = ui.number("Min bridges", value=1, min=0, max=12).props("dense").classes("w-24").tooltip("Min number of conceptual bridge sections in the narration script")
            narr_max_bridges = ui.number("Max bridges", value=4, min=0, max=12).props("dense").classes("w-24").tooltip("Max number of conceptual bridge sections in the narration script")

        with ui.row().classes("gap-2 mt-3"):
            lang_select = ui.select(
                lang_options,
                value=current_lang,
                label="Use transcript in",
            ).props("dense")

            num_versions = ui.number("Versions to generate", value=2, min=1, max=3).props("dense").classes("w-32")

        generate_btn = ui.button("Generate AI Cuts", icon="auto_awesome", color="primary").classes("mt-4 w-full")

        # Results area
        results_container = ui.column().classes("w-full mt-4")

        def _render_versions(versions: list[dict]):
            # Always resolve selected_segments source times from the authoritative .txt sidecar
            # (the transscript file written at transcription time). This guarantees the list
            # shows the real [10:03:30:19 (xx s) → ...] the user sees in 0000XX_fi.txt, and that
            # durs and any downstream (XML in/out, SRT durs) use transscript truth not LLM/DB numbers.
            # Especially critical for Verbatim Scriptwriter / rewrite tone where Stage 2 composes
            # new narrative text and often emits drifted source_in/out.
            try:
                from minicat.core.video import repair_journalist_segments_with_transcript
                cat_root = None
                try:
                    from minicat.ui.app import get_state
                    st = get_state()
                    cat_root = getattr(st, "catalog_root", None) if st else None
                except Exception:
                    pass
                clip_id = getattr(v, "id", None) if v else None
                cur_lang = "fi"
                try:
                    if lang_select and getattr(lang_select, "value", None):
                        cur_lang = lang_select.value or "fi"
                except Exception:
                    pass
                if cat_root and clip_id:
                    for ver in versions:
                        if ver.get("selected_segments"):
                            ver["selected_segments"] = repair_journalist_segments_with_transcript(
                                ver["selected_segments"], cat_root, clip_id, cur_lang
                            )
            except Exception as _rend_re:
                print(f"[AI Journalist] _render_versions repair skipped: {_rend_re}")

            results_container.clear()
            with results_container:
                for version in versions:
                    with ui.card().classes("w-full mb-3 border"):
                        header = f"{version.get('version_id', '?')} — {version.get('title', 'Untitled')}"
                        ui.label(header).classes("text-base font-semibold mb-1")

                        dur = version.get("total_duration", 0)
                        dur_str = format_duration_timecode(dur, 25)
                        ui.label(f"Duration: {dur_str}").classes("text-xs text-grey-5 mb-2")

                        # Show whether rewrite tone actually produced a non-linear order (very useful diagnostic)
                        if version.get("_reorder_note"):
                            note = version["_reorder_note"]
                            color = "positive" if "✓" in note else "warning"
                            ui.label(note).classes(f"text-xs mb-1 {'text-positive' if color=='positive' else 'text-warning'}")

                        summary = version.get("narrative_summary", "")
                        if summary:
                            ui.markdown(summary).classes("text-sm mb-2")

                        # Show narration script if the AI generated one for voiceover (new for Journalist)
                        narration = (version.get("narration_text") or "").strip()
                        if narration:
                            with ui.card().classes("w-full bg-blue-900/15 border-l-2 border-blue-500 mb-2 p-2"):
                                style = version.get("narration_style")
                                label = f"🎙️ Narration script for voiceover" + (f" ({style})" if style else "")
                                ui.label(label).classes("text-xs font-semibold text-blue-300 mb-0.5")
                                # Show full (usually short for single clip)
                                ui.markdown(narration).classes("text-sm text-blue-100")

                            # VoiceOver/Narration export options (same as AI Director)
                            with ui.row().classes("gap-2 items-end mb-2"):
                                vo_voice = ui.input(
                                    "Voice (optional, blank = use Settings default)",
                                    value="",
                                ).props("dense").classes("w-64").tooltip("E.g. 'en_US-amy-medium' for Piper or a Google voice name")
                                gen_vo_audio = ui.checkbox("Generate voiceover audio (sidecar .wav)", value=True).props("dense")
                                # "as titles" for journalist means skip audio gen, just use script text (in TXT and as comments in XML)
                                use_titles = ui.checkbox("Narration as text/titles only (no audio)", value=False).props("dense")

                        # Editable list of segments
                        segs = version.get("selected_segments", [])

                        def make_delete_handler(ver, idx):
                            def handler():
                                if 0 <= idx < len(ver["selected_segments"]):
                                    del ver["selected_segments"][idx]
                                    _render_versions(versions)  # refresh
                            return handler

                        for idx, seg in enumerate(segs):
                            with ui.row().classes("items-center gap-2 mb-1 text-sm"):
                                # Support both new canonical keys and legacy keys
                                s = seg.get("source_in") or seg.get("start", 0)
                                e = seg.get("source_out") or seg.get("end", 0)
                                try:
                                    from minicat.core.video import format_transcript_timecode
                                    tc = format_transcript_timecode(
                                        s, e,
                                        fps=getattr(v, "fps", None),
                                        base_timecode=getattr(v, "tc_start", None),
                                    )
                                except Exception:
                                    tc = f"{float(s):.1f}s → {float(e):.1f}s"
                                ui.label(tc).classes("font-mono text-xs w-28")
                                ui.label(seg.get("text", "")[:80]).classes("flex-1 truncate")
                                ui.button(
                                    icon="delete",
                                    on_click=make_delete_handler(version, idx),
                                ).props("size=xs flat color=negative")

                        # Export buttons for this version
                        def export_this_version_as_xml(ver=version):
                            print(f"[AI Cut XML Export] HANDLER CALLED for version {ver.get('version_id')} of {v.filename}")
                            ui.notify("Starting XML export...", color="info", duration=2)
                            source_path = Path(v.path).resolve()

                            try:
                                out_name = f"{v.filename.rsplit('.', 1)[0]}_AI_Cut_{ver['version_id']}.xml"
                                export_dir = get_default_export_directory()
                                out_path = export_dir / out_name

                                segments = ver.get("selected_segments", [])
                                if not segments:
                                    ui.notify("No segments in this version to export", color="warning")
                                    return

                                # Re-resolve every source_in/out from the trans .txt sidecar right at export time.
                                # This is the final safety net: even if the live version dict had drifted numbers
                                # (old generate before this fix, or edited externally), the XML <in>/<out> and
                                # timeline durs will be taken from the exact timecodes in the transscript file.
                                try:
                                    from minicat.core.video import repair_journalist_segments_with_transcript
                                    from minicat.ui.app import get_state
                                    st = get_state()
                                    cat_root = getattr(st, "catalog_root", None) if st else None
                                    clip_id = getattr(v, "id", None)
                                    x_lang = "fi"
                                    try:
                                        if lang_select and getattr(lang_select, "value", None):
                                            x_lang = lang_select.value or "fi"
                                    except Exception:
                                        pass
                                    if cat_root and clip_id:
                                        segments = repair_journalist_segments_with_transcript(
                                            segments, cat_root, clip_id, x_lang
                                        )
                                        # Belt-and-suspenders: push the freshly repaired ranges back into the
                                        # live version dict so that the immediate bundle call to
                                        # export_this_version_as_txt(ver, ...) and any other readers see
                                        # the authoritative trans times (not the short LLM numbers).
                                        try:
                                            ver["selected_segments"] = [dict(s) for s in segments]
                                        except Exception:
                                            pass
                                except Exception as _xml_re:
                                    print(f"[AI Cut XML Export] transcript re-resolve skipped: {_xml_re}")

                                # Safety for cleaned versions: if a segment's text was stripped to empty by filler removal,
                                # use its reason as fallback text so the clip still appears properly in the XML.
                                for s in segments:
                                    if not (s.get('text') or '').strip():
                                        if s.get('reason'):
                                            s['text'] = s['reason']

                                # For consistency with AI Director: use a dedicated per-export subfolder
                                suggestion = f"AI_Journalist_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:30] }"
                                export_dir = create_export_subfolder(suggestion)
                                out_path = export_dir / out_name

                                # Pass real start timecode so the <file> timecode in XML matches what the
                                # source transcription shows (e.g. 10:03:20:01). Combined with the repaired
                                # source_in values, this makes script + XML line up with the transcript.
                                source_tc = getattr(v, "tc_start", None)

                                print(f"[AI Cut XML Export] Starting generate_xmeml → {out_path}")
                                generate_xmeml(
                                    cut_segments=segments,
                                    source_video_path=str(source_path),
                                    output_path=out_path,
                                    sequence_name=f"AI Cut - {ver.get('title', ver['version_id'])}",
                                    narrative_summary=ver.get('narrative_summary'),
                                    width=getattr(v, "width", None) or 1920,
                                    height=getattr(v, "height", None) or 1080,
                                    audio_channels=getattr(v, "audio_channels", None),
                                    source_start_timecode=source_tc,
                                )

                                if out_path.exists():
                                    print(f"[AI Cut XML Export] SUCCESS: {out_path}")

                                    # Always also write the rich AI JOURNALIST script/story TXT into the same subfolder.
                                    # This gives users the "SELECTED SEGMENTS" (with correct repaired times from trans .txt,
                                    # Text, Reason) + full original transcript, exactly like AI Director "Export XML"
                                    # always produces the rich MULTI-CLIP SCRIPT.txt (regardless of narration).
                                    # Especially useful / requested for the no-narration case.
                                    script_path = None
                                    try:
                                        script_path = export_this_version_as_txt(ver, target_export_dir=export_dir)
                                    except Exception as _script_ex:
                                        print(f"[AI Cut XML Export] rich script TXT failed (non-fatal): {_script_ex}")

                                    if script_path and script_path.exists():
                                        ui.notify(f"XML + AI JOURNALIST script.txt (SELECTED SEGMENTS + full trans) exported → {out_path.name} (in {export_dir.name})", color="positive", duration=8)
                                    else:
                                        ui.notify(f"XMEML exported to {out_path}", color="positive")

                                    # If this version has a narration script, generate the TTS voiceover audio
                                    # (sidecar WAV next to the XML) using the per-export choices (matching Director's VoiceOver options).
                                    narration_text = (ver.get("narration_text") or "").strip()
                                    if narration_text and not use_titles.value and gen_vo_audio.value:
                                        try:
                                            nar_lang = ver.get("narration_language") or "en"
                                            nar_name = f"{v.filename.rsplit('.', 1)[0]}_AI_Cut_{ver['version_id']}_Narration.wav"
                                            nar_path = export_dir / nar_name
                                            ui.notify("Generating narration voiceover audio (TTS)...", color="info", duration=3)
                                            chosen_voice = (vo_voice.value or "").strip() or None
                                            generate_narration_audio_sync(
                                                text=narration_text,
                                                language=nar_lang,
                                                output_path=nar_path,
                                                voice=chosen_voice,
                                            )
                                            ui.notify(f"Narration voiceover audio saved → {nar_path.name}", color="positive")
                                        except Exception as vo_ex:
                                            ui.notify(f"Voiceover audio generation failed: {vo_ex}", color="negative")
                                            print(f"[AI Journalist VO] {vo_ex}")
                                    elif narration_text and use_titles.value:
                                        ui.notify("Narration text included in TXT/XML (titles-only mode, no audio generated)", color="info")

                                    # Generate one clean SRT that exactly matches this AI cut's script
                                    # (correct order from the AI, correct timing for the new sequence).
                                    try:
                                        srt_name = f"{v.filename.rsplit('.', 1)[0]}_AI_Cut_{ver['version_id']}.srt"
                                        srt_path = export_dir / srt_name
                                        timeline_segs = ai_journalist_cut_to_srt_segments(segments)
                                        if timeline_segs:
                                            fps = getattr(v, "fps", None) or 25.0
                                            srt_content = segments_to_srt(timeline_segs, strict_timing=True, fps=fps)
                                            srt_path.write_text(srt_content, encoding="utf-8")
                                            print(f"[AI Cut Export] Pushed SRT for new cut timeline: {srt_path.name}")
                                    except Exception as srt_ex:
                                        print(f"[AI Cut Export] SRT generation failed (non-fatal): {srt_ex}")

                                    # OPUS warning (shown after successful export so user doesn't miss the XML)
                                    try:
                                        if video_core.has_opus_audio(source_path):
                                            def _offer_transcode():
                                                async def do_transcode():
                                                    try:
                                                        ui.notify("Transcoding to Premiere-friendly AAC version...", color="info", duration=4)
                                                        out_dir = get_default_export_directory()
                                                        safe_name = source_path.stem + "_for_Premiere" + source_path.suffix
                                                        target = out_dir / safe_name

                                                        transcoded = await asyncio.to_thread(
                                                            video_core.create_premiere_friendly_version,
                                                            source_path,
                                                            target,
                                                        )
                                                        ui.notify(
                                                            f"Created: {transcoded.name}  (use this file with your XML)",
                                                            color="positive",
                                                            duration=10,
                                                        )
                                                    except Exception as tex:
                                                        ui.notify(f"Transcode failed: {tex}", color="negative")
                                                        print(f"[OPUS Transcode] {tex}")

                                                asyncio.create_task(do_transcode())

                                            ui.notify(
                                                "Note: Source has OPUS audio. Premiere can have trouble linking it. Consider the AAC version.",
                                                color="warning",
                                                duration=8,
                                                actions=[{"label": "Transcode to AAC", "on_click": _offer_transcode}],
                                            )
                                    except Exception as opus_err:
                                        print(f"[AI Cut XML Export] OPUS check failed (non-fatal): {opus_err}")

                                else:
                                    ui.notify("XMEML generation completed but file not found on disk", color="warning")
                                    print(f"[AI Cut XML Export] File not found after generate: {out_path}")

                            except Exception as ex:
                                ui.notify(f"XML Export failed: {ex}", color="negative")
                                print(f"[AI Cut XML Export] EXCEPTION: {ex}")
                                import traceback
                                traceback.print_exc()

                        def export_this_version_as_txt(ver=version, target_export_dir: "Path | None" = None):
                            """Export the rich AI JOURNALIST CUT script/story as TXT.

                            If target_export_dir is provided, write the script into that existing folder
                            (used by XML export to bundle XML + script like AI Director does).
                            Otherwise creates its own subfolder and notifies.
                            """
                            print(f"[AI Cut TXT Export] HANDLER CALLED for version {ver.get('version_id')} of {v.filename}")
                            do_notify = target_export_dir is None
                            if do_notify:
                                ui.notify("Starting TXT export...", color="info", duration=2)
                            try:
                                from datetime import datetime
                                from pathlib import Path as _Path

                                if target_export_dir is not None:
                                    export_dir = _Path(target_export_dir)
                                    export_dir.mkdir(parents=True, exist_ok=True)
                                else:
                                    # Use per-export subfolder for consistency
                                    suggestion = f"AI_Journalist_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:30] }"
                                    export_dir = create_export_subfolder(suggestion)
                                out_name = f"{v.filename.rsplit('.', 1)[0]}_AI_Cut_{ver['version_id']}.txt"
                                out_path = export_dir / out_name
                                print(f"[AI Cut TXT Export] Target path: {out_path}")

                                # Compute language/labels EARLY so they are always bound before any use in the rich TXT.
                                # (Previously the assignment was after the header/selected uses, causing UnboundLocalError.)
                                transcript_lang = "fi"
                                try:
                                    if lang_select and getattr(lang_select, "value", None):
                                        transcript_lang = lang_select.value or "fi"
                                except Exception:
                                    pass

                                effective_label_lang = transcript_lang
                                if not effective_label_lang or effective_label_lang in ("original", ""):
                                    try:
                                        effective_label_lang = getattr(v, "original_language", None) or "en"
                                    except Exception:
                                        effective_label_lang = "en"
                                if ver.get("script_language"):
                                    effective_label_lang = ver.get("script_language")
                                labels = get_script_labels(effective_label_lang)

                                lines = []

                                # Header - all labels in the transcripted/scripted language
                                lines.append("=" * 72)
                                lines.append(labels["ai_journalist_cut"])
                                lines.append("=" * 72)
                                lines.append(f"{labels['source_file']} {v.filename}")
                                lines.append(f"{labels['exported']}    {datetime.now().strftime('%Y-%m-%d %H:%M')}")
                                lines.append(f"{labels['version']}     {ver.get('version_id', '?')} — {ver.get('title', 'Untitled')}")
                                dur_str = format_duration_timecode(ver.get('total_duration', 0), 25)
                                lines.append(f"{labels['duration']}    {dur_str}")
                                lines.append("")

                                summary = ver.get('narrative_summary', '').strip()
                                if summary:
                                    lines.append(labels["editorial_summary"])
                                    lines.append("-" * 72)
                                    lines.append(summary)
                                    lines.append("")

                                # Include narration script if present (for voiceover)
                                narration = (ver.get("narration_text") or "").strip()
                                if narration:
                                    lines.append(labels["narration_voiceover_script"])
                                    lines.append("-" * 72)
                                    lines.append(narration)
                                    lines.append("")

                                def _to_seconds(t):
                                    if isinstance(t, (int, float)):
                                        return float(t)
                                    if isinstance(t, str):
                                        t = t.strip().replace(',', '.')
                                        parts = t.split(':')
                                        if len(parts) == 3:
                                            h, m, s = parts
                                            return int(h) * 3600 + int(m) * 60 + float(s)
                                        elif len(parts) == 2:
                                            m, s = parts
                                            return int(m) * 60 + float(s)
                                        else:
                                            return float(parts[0])
                                    return float(t)

                                # Selected segments
                                lines.append(labels["selected_segments"])
                                lines.append("-" * 72)

                                # Re-resolve numerics from trans .txt first so that (dur) values are correct
                                # even for composed/rewritten segments. (transcript_lang and labels were computed early.)
                                segments_for_txt = ver.get("selected_segments", [])
                                try:
                                    from minicat.core.video import repair_journalist_segments_with_transcript
                                    from minicat.ui.app import get_state
                                    st = get_state()
                                    cat_root = getattr(st, "catalog_root", None) if st else None
                                    if cat_root and v.id:
                                        segments_for_txt = repair_journalist_segments_with_transcript(
                                            segments_for_txt, cat_root, v.id, transcript_lang
                                        )
                                except Exception:
                                    pass

                                for i, seg in enumerate(segments_for_txt, 1):
                                    # Support canonical + legacy keys (now pre-repaired from trans .txt)
                                    start = _to_seconds(seg.get('source_in') or seg.get('start', 0))
                                    end = _to_seconds(seg.get('source_out') or seg.get('end', 0))
                                    dur = end - start

                                    seg_text = (seg.get('text', '') or "").strip()

                                    # ALWAYS derive the displayed timecode prefix from the *actual* repaired
                                    # source range for this item. This makes the TC the SCRIPT shows for each
                                    # selected beat exactly match the material range used by the corresponding
                                    # clipitem in the XML (and the rendered cut). The format is the same one
                                    # used to write the source trans .txt files, so it matches the style and
                                    # values the user sees in the TRANSCRIPTION.
                                    try:
                                        from minicat.core.video import format_transcript_timecode
                                        time_prefix = format_transcript_timecode(
                                            start, end,
                                            fps=getattr(v, "fps", None),
                                            base_timecode=getattr(v, "tc_start", None),
                                        )
                                    except Exception:
                                        def fmt(t):
                                            h = int(t // 3600)
                                            m = int((t % 3600) // 60)
                                            s = t % 60
                                            return f"{h:02d}:{m:02d}:{s:05.2f}" if h > 0 else f"{m:02d}:{s:05.2f}"
                                        time_prefix = f"[{fmt(start)} → {fmt(end)}]"

                                    lines.append(f"\n{i}. {time_prefix} ({dur:.2f}s)")

                                    lines.append(f"   {labels['text_label']}   {seg_text}")
                                    lines.append(f"   {labels['reason_label']} {seg.get('reason', '')}")

                                lines.append("\n" + "=" * 72)

                                # Full transcript at the bottom -- prefer the actual transscript file for the
                                # *chosen* transcript_lang (so the embedded full text matches the language the
                                # AI used for SELECTED SEGMENTS / story). Fall back sensibly.
                                # This guarantees the TXT SCRIPT is consistently in the transcripted/scripted language.
                                full_transcript_content = None
                                try:
                                    from minicat.core.video import get_transcription_txt_path
                                    from minicat.ui.app import get_state
                                    st = get_state()
                                    cat_root = getattr(st, "catalog_root", None) if st else None
                                    if cat_root and v.id:
                                        try_langs = []
                                        if transcript_lang:
                                            try_langs.append(transcript_lang)
                                        for extra in ("original", "fi", "en", ""):
                                            if extra not in try_langs:
                                                try_langs.append(extra)
                                        for try_lang in try_langs:
                                            p = get_transcription_txt_path(v.id, cat_root, try_lang)
                                            if p.exists():
                                                full_transcript_content = p.read_text(encoding="utf-8")
                                                break
                                except Exception:
                                    full_transcript_content = None

                                if full_transcript_content:
                                    lines.append(f"\n{labels['full_transcript']}")
                                    lines.append("-" * 72)
                                    lines.append(full_transcript_content)
                                else:
                                    # fallback to segments (old behavior) - these should be the ones for the chosen lang
                                    original_segments = getattr(v, "transcription_segments", []) or []
                                    if original_segments:
                                        lines.append(f"\n{labels['full_transcript']}")
                                        lines.append("-" * 72)

                                        for seg in original_segments:
                                            # Support canonical + legacy keys
                                            start = _to_seconds(seg.get('source_in') or seg.get('start', 0))
                                            end = _to_seconds(seg.get('source_out') or seg.get('end', 0))
                                            text = seg.get('text', '')

                                            def fmt(t):
                                                h = int(t // 3600)
                                                m = int((t % 3600) // 60)
                                                s = t % 60
                                                return f"{h:02d}:{m:02d}:{s:05.2f}" if h > 0 else f"{m:02d}:{s:05.2f}"

                                            lines.append(f"[{fmt(start)} → {fmt(end)}] {text}")

                                content = "\n".join(lines)
                                out_path.write_text(content, encoding="utf-8")
                                if do_notify:
                                    ui.notify(f"TXT exported to {out_path}", color="positive")  # full path
                                print(f"[AI Cut TXT Export] SUCCESS: {out_path}")
                                return out_path

                            except Exception as ex:
                                if do_notify:
                                    ui.notify(f"TXT Export failed: {ex}", color="negative")
                                print(f"[AI Cut TXT Export] EXCEPTION: {ex}")
                                import traceback
                                traceback.print_exc()
                                return None

                        async def export_this_version_as_new_clip(ver=version):
                            """
                            Export the AI Journalist cut as a real media file:
                            - If the source is audio → WAV file with the selected segments
                            - If the source is video → MP4 file with the selected segments
                            """
                            # Robust notification helper (ui.timer(0, once=True) re-enters NiceGUI context).
                            # This function is attached directly to on_click (no manual create_task),
                            # so the initial call site has a valid slot and ui.timer() can be constructed.
                            def _safe_notify(message, **kwargs):
                                def _do_notify():
                                    ui.notify(message, **kwargs)
                                ui.timer(0, _do_notify, once=True)

                            try:
                                from minicat.cli.main import _is_audio_file

                                base = v.filename.rsplit('.', 1)[0]
                                ver_id = ver.get('version_id', 'X')
                                title_slug = (ver.get('title') or 'Cut').replace(' ', '_')[:40]
                                dur = int(ver.get('total_duration', 0))

                                # Use per-export subfolder (consistent with other AI Journalist + Director exports)
                                suggestion = f"AI_Journalist_{ver_id}_{title_slug}"
                                export_dir = create_export_subfolder(suggestion)

                                is_audio_source = _is_audio_file(Path(v.path))

                                if is_audio_source:
                                    out_name = f"{base}_AIJournalist_{ver_id}_{title_slug}_{dur}s.wav"
                                    out_path = export_dir / out_name

                                    _safe_notify(f"Exporting AI audio cut ({dur}s) as WAV...", color="info", duration=4)

                                    def _do_audio():
                                        segs = ver.get("selected_segments", [])
                                        try:
                                            from minicat.core.video import repair_journalist_segments_with_transcript
                                            from minicat.ui.app import get_state
                                            st = get_state()
                                            cat_root = getattr(st, "catalog_root", None) if st else None
                                            if cat_root and v.id:
                                                segs = repair_journalist_segments_with_transcript(segs, cat_root, v.id, getattr(lang_select, "value", None) or "fi")
                                        except Exception:
                                            pass
                                        video_core.export_ai_journalist_cut_audio(
                                            video=v,
                                            selected_segments=segs,
                                            output_path=out_path,
                                        )
                                        return out_path

                                    rendered = await asyncio.to_thread(_do_audio)
                                    if rendered.exists() and rendered.stat().st_size > 1000:
                                        _safe_notify(f"Audio clip exported → {rendered.name}", color="positive", duration=8)

                                        # Generate one clean SRT that matches the AI cut exactly
                                        try:
                                            srt_name = f"{base}_AIJournalist_{ver_id}_{title_slug}_{dur}s.srt"
                                            srt_path = export_dir / srt_name
                                            timeline_segs = ai_journalist_cut_to_srt_segments(segs)
                                            if timeline_segs:
                                                fps = getattr(v, "fps", None) or 25.0  # prefer source fps for correct frame alignment of sidecar SRT
                                                srt_content = segments_to_srt(timeline_segs, strict_timing=True, fps=fps)
                                                srt_path.write_text(srt_content, encoding="utf-8")
                                                _safe_notify(f"Matching SRT saved → {srt_path.name}", color="positive", duration=6)
                                        except Exception as srt_ex:
                                            print(f"[AI Audio Export] SRT generation failed (non-fatal): {srt_ex}")

                                    # Sidecar narration voiceover if present (respect per-export choices like Director)
                                    narration_text = (ver.get("narration_text") or "").strip()
                                    if narration_text and not use_titles.value and gen_vo_audio.value:
                                        try:
                                            nar_lang = ver.get("narration_language") or "en"
                                            nar_name = f"{base}_AIJournalist_{ver_id}_{title_slug}_{dur}s_Narration.wav"
                                            nar_path = export_dir / nar_name
                                            _safe_notify("Generating narration voiceover audio (TTS)...", color="info", duration=3)
                                            chosen_voice = (vo_voice.value or "").strip() or None
                                            generate_narration_audio_sync(
                                                text=narration_text,
                                                language=nar_lang,
                                                output_path=nar_path,
                                                voice=chosen_voice,
                                            )
                                            _safe_notify(f"Narration voiceover audio saved → {nar_path.name}", color="positive", duration=6)
                                        except Exception as vo_ex:
                                            _safe_notify(f"Voiceover generation failed: {vo_ex}", color="negative")
                                            print(f"[AI Journalist Render VO] {vo_ex}")
                                    elif narration_text and use_titles.value:
                                        _safe_notify("Narration as text only (script in TXT; no audio sidecar)", color="info")
                                    else:
                                        _safe_notify("Audio export completed but output file missing or empty", color="warning")
                                else:
                                    out_name = f"{base}_AIJournalist_{ver_id}_{title_slug}_{dur}s.mp4"
                                    out_path = export_dir / out_name

                                    _safe_notify(f"Rendering AI video cut ({dur}s) as MP4...", color="info", duration=4)

                                    def _do_video():
                                        segs = ver.get("selected_segments", [])
                                        try:
                                            from minicat.core.video import repair_journalist_segments_with_transcript
                                            from minicat.ui.app import get_state
                                            st = get_state()
                                            cat_root = getattr(st, "catalog_root", None) if st else None
                                            if cat_root and v.id:
                                                segs = repair_journalist_segments_with_transcript(segs, cat_root, v.id, getattr(lang_select, "value", None) or "fi")
                                        except Exception:
                                            pass
                                        video_core.export_ai_journalist_cut_video(
                                            video=v,
                                            selected_segments=segs,
                                            output_path=out_path,
                                            title=ver.get('title'),
                                        )
                                        return out_path

                                    rendered = await asyncio.to_thread(_do_video)
                                    if rendered.exists() and rendered.stat().st_size > 1000:
                                        _safe_notify(f"Video clip exported → {rendered.name}", color="positive", duration=8)

                                        # Generate one clean SRT that matches the AI cut exactly
                                        try:
                                            srt_name = f"{base}_AIJournalist_{ver_id}_{title_slug}_{dur}s.srt"
                                            srt_path = export_dir / srt_name
                                            timeline_segs = ai_journalist_cut_to_srt_segments(segs)
                                            if timeline_segs:
                                                fps = getattr(v, "fps", None) or 25.0  # prefer source fps for correct frame alignment of sidecar SRT
                                                srt_content = segments_to_srt(timeline_segs, strict_timing=True, fps=fps)
                                                srt_path.write_text(srt_content, encoding="utf-8")
                                                _safe_notify(f"Matching SRT saved → {srt_path.name}", color="positive", duration=6)
                                        except Exception as srt_ex:
                                            print(f"[AI Video Export] SRT generation failed (non-fatal): {srt_ex}")

                                    # Sidecar narration voiceover if present (respect per-export choices like Director)
                                    narration_text = (ver.get("narration_text") or "").strip()
                                    if narration_text and not use_titles.value and gen_vo_audio.value:
                                        try:
                                            nar_lang = ver.get("narration_language") or "en"
                                            nar_name = f"{base}_AIJournalist_{ver_id}_{title_slug}_{dur}s_Narration.wav"
                                            nar_path = export_dir / nar_name
                                            _safe_notify("Generating narration voiceover audio (TTS)...", color="info", duration=3)
                                            chosen_voice = (vo_voice.value or "").strip() or None
                                            generate_narration_audio_sync(
                                                text=narration_text,
                                                language=nar_lang,
                                                output_path=nar_path,
                                                voice=chosen_voice,
                                            )
                                            _safe_notify(f"Narration voiceover audio saved → {nar_path.name}", color="positive", duration=6)
                                        except Exception as vo_ex:
                                            _safe_notify(f"Voiceover generation failed: {vo_ex}", color="negative")
                                            print(f"[AI Journalist Render VO] {vo_ex}")
                                    elif narration_text and use_titles.value:
                                        _safe_notify("Narration as text only (script in TXT; no audio sidecar)", color="info")
                                    else:
                                        _safe_notify("Video export completed but output file missing or empty", color="warning")

                            except Exception as ex:
                                print(f"[AI Journalist Export New Clip] {ex}")
                                import traceback
                                traceback.print_exc()
                                _safe_notify(f"Export as New Clip failed: {ex}", color="negative")

                        with ui.row().classes("gap-2 mt-2 w-full"):
                            # Primary action: the actual rendered media clip (MP4 or WAV)
                            ui.button(
                                "Export as New Clip",
                                icon="download",
                                on_click=export_this_version_as_new_clip,
                            ).props("color=primary").classes("flex-1")

                            # Secondary: XML + TXT exports under one "Export File" button
                            export_file_btn = ui.button(
                                "Export File",
                                icon="description",
                            ).props("outline").tooltip("Export Premiere XML (bundles the rich AI JOURNALIST script/story TXT like Director) or standalone TXT")
                            with export_file_btn, ui.menu():
                                ui.menu_item(
                                    "Premiere XML (+ rich script TXT)",
                                    on_click=export_this_version_as_xml
                                )
                                ui.menu_item(
                                    "TXT",
                                    on_click=export_this_version_as_txt
                                )

        async def do_generate():
            generate_btn.disable()
            results_container.clear()

            # Dynamic status area with spinner + updating text (same pattern as multi-clip)
            with results_container:
                with ui.row().classes("items-center gap-3 justify-center w-full"):
                    ui.spinner("dots").classes("mr-2")
                    status_label = ui.label("Preparing...").classes("text-sm text-grey-6")

            # Hoisted for scope
            audio_for_listening = None

            try:
                status_label.text = "Preparing transcript..."
                await asyncio.sleep(0.03)

                lang = lang_select.value
                segments = _get_segments_for_lang(lang)

                if not segments:
                    ui.notify("No segments available for the selected language", color="warning")
                    return

                # AI Journalist is strictly transcript-only. No audio is sent.
                audio_for_listening = None
                status_label.text = "Sending transcript to AI..."
                await asyncio.sleep(0.03)

                status_label.text = "AI is analyzing the transcript and generating versions...\n(usually 15–60 seconds)"

                # Run the heavy AI work in a thread (transcript-only)
                # Determine narration language from chosen transcript
                lang = lang_select.value
                if lang == "original":
                    mat_lang = getattr(v, "original_language", None) or "en"
                else:
                    mat_lang = lang

                versions = await asyncio.to_thread(
                    generate_journalist_cuts,
                    segments,
                    float(max_dur.value),
                    min_duration_seconds=float(min_dur.value),
                    purpose=purpose.value,
                    tone=tone.value,
                    num_versions=int(num_versions.value),
                    clean_fillers=bool(clean_fillers.value),
                    generate_narration=bool(narration_style.value),
                    narration_style=narration_style.value,
                    material_language=mat_lang,
                    narration_min_seconds=float(narr_min_sec.value or 0),
                    narration_max_seconds=float(narr_max_sec.value or 0),
                    narration_min_bridges=int(narr_min_bridges.value or 0),
                    narration_max_bridges=int(narr_max_bridges.value or 0),
                )

                # Ensure narration lang is on versions for TTS later
                for ver in versions:
                    if narration_style.value or ver.get("narration_text"):
                        ver["narration_language"] = mat_lang
                    # So that exported TXT script/story has *all* its details (labels etc) in the
                    # transcripted/scripted language, even when the export func is called later.
                    ver["script_language"] = mat_lang
                    ver["narration_min_seconds"] = float(narr_min_sec.value or 0)
                    ver["narration_max_seconds"] = float(narr_max_sec.value or 0)
                    ver["narration_min_bridges"] = int(narr_min_bridges.value or 0)
                    ver["narration_max_bridges"] = int(narr_max_bridges.value or 0)

                # Repair source timings for the returned versions (critical for "rewrite" tone).
                # The two-stage Verbatim Scriptwriter pipeline (and sometimes normal LLM output) can
                # emit hallucinated or mangled source_in/source_out even when the text is correct.
                # We match the chosen texts back to the exact `segments` list we passed in (which has
                # the authoritative times from the transcription / translation) and restore the real
                # numbers. This makes:
                #   - the editable list in the dialog show correct source times
                #   - the AI_Cut_*.txt "SELECTED SEGMENTS" times match the source _fi.txt (or original)
                #   - the XML <in>/<out> pull the actual spoken material the script describes
                # so everything lines up with "Out Transcript timecodes".
                try:
                    _reattach_source_info(versions, segments)
                except Exception as _repair_err:
                    print(f"[AI Journalist] post-generation source time repair skipped: {_repair_err}")

                # Extra pass using the .txt sidecar (catches rewrite composed texts that _reattach
                # on the in-memory segments list could not match exactly). _render_versions will
                # also do this, but doing it here ensures the versions objects themselves carry
                # correct numerics for any immediate use (audio render buttons etc).
                try:
                    from minicat.core.video import repair_journalist_segments_with_transcript
                    cat_root = None
                    try:
                        from minicat.ui.app import get_state
                        st = get_state()
                        cat_root = getattr(st, "catalog_root", None) if st else None
                    except Exception:
                        pass
                    clip_id = getattr(v, "id", None)
                    cur_lang = "fi"
                    try:
                        if lang_select and getattr(lang_select, "value", None):
                            cur_lang = lang_select.value or "fi"
                    except Exception:
                        pass
                    if cat_root and clip_id:
                        for ver in versions:
                            if ver.get("selected_segments"):
                                ver["selected_segments"] = repair_journalist_segments_with_transcript(
                                    ver["selected_segments"], cat_root, clip_id, cur_lang
                                )
                except Exception as _post_re:
                    print(f"[AI Journalist] post-gen transcript repair skipped: {_post_re}")

                status_label.text = "Processing AI response..."

                _render_versions(versions)

            except Exception as ex:
                results_container.clear()
                with results_container:
                    ui.label(f"AI generation failed: {ex}").classes("text-negative")
                    ui.label("Check the console for the raw response from Gemini (helps debugging).").classes("text-xs text-grey-6")
                print(f"[AI Journalist Cut] Error: {ex}")
            finally:
                generate_btn.enable()
                # Transcript-only mode for AI Journalist.

        generate_btn.on_click(do_generate)

    dlg.open()


# ---------------------------------------------------------------------------
# AI DIRECTOR — Multi-Clip Narrative Construction
# The Director receives one combined, labeled transcript (C1, C2...) from multiple clips.
# ---------------------------------------------------------------------------

def _show_multi_ai_journalist_cut_dialog(selected_videos: list[Any]) -> None:
    """
    AI Director for multiple clips.

    - Combines the transcripts of the selected clips into one explicitly labeled document
      (every timecode clearly states which original clip it belongs to: C1, C2, ...).
    - Sends the combined labeled transcript (plus optional audio) to the AI Director.
    - The Director builds narrative versions by intelligently selecting and intercutting
      verbatim material across the different sources.
    - Output segments keep their source metadata so rendering and XMEML export work
      correctly with multiple original media files.
    - Journalistic safety is absolute: only exact spoken words + accurate timings
      from the original transcriptions are ever used.
    """
    from minicat.core import video as video_core
    from pathlib import Path as _Path  # avoid name clash

    if not selected_videos:
        ui.notify("No clips provided for AI Director.", color="warning")
        return

    # Guard: every clip must have transcription
    for v in selected_videos:
        if not getattr(v, "transcription_segments", None):
            ui.notify(f"Clip '{v.filename}' has no transcription. Transcribe all clips first.", color="warning")
            return

    # Build source registry (stable short labels for the AI and for UI)
    # Note: segments come from the DB (transcription_segments), which is the
    # authoritative high-precision source. We do not re-parse the .txt files
    # from <catalog>/transcriptions/ for the Director (those are output artifacts).
    sources: list[dict] = []
    for idx, v in enumerate(selected_videos):
        label = f"C{idx+1}"  # C1, C2, ...
        sources.append({
            "index": idx,
            "label": label,
            "video": v,
            "filename": v.filename,
            "camera": getattr(v, "camera", None) or "",
            "path": str(_Path(v.path).resolve()),
            "duration": getattr(v, "duration", 0) or 0,
            "segments": getattr(v, "transcription_segments", []) or [],
            "original_language": getattr(v, "original_language", None),
        })

    # Determine the primary language from the clips' technical metadata
    # (preferred over auto-detection by Gemini)
    languages = [s.get("original_language") for s in sources if s.get("original_language")]
    primary_language = languages[0] if languages else "en"

    # Total available material from the selected clips
    total_available = sum(s["duration"] for s in sources)
    default_max = int(total_available) if total_available > 0 else 120

    # Simple language handling for MVP: use "original" for all clips.
    # (Future: per-clip language choice or shared translation.)
    def _get_segments_for_source(src: dict) -> list[dict]:
        return src["segments"]

    dlg = ui.dialog()
    with dlg, ui.card().classes("w-[820px] max-h-[90vh] overflow-auto"):
        ui.label("AI Director — Build Story").classes("text-h5 mb-1")
        ui.label(f"Intercutting {len(sources)} clips into narrative versions").classes("text-sm text-grey-6 mb-2")

        # List of participating clips (compact)
        with ui.column().classes("mb-3 text-xs"):
            for s in sources:
                dur_str = format_duration_timecode(s['duration'], 25)
                ui.label(f"{s['label']}: {s['filename']}  ({dur_str})").classes("font-mono")

            if primary_language:
                ui.label(f"Detected original language: {primary_language} (will be used for narration)").classes("text-blue-400 mt-1")

        # Controls (same spirit as single-clip)
        # Max duration can now go up to the full combined length of the selected clips
        with ui.row().classes("gap-4 items-end w-full"):
            num_versions = ui.number("Versions", value=2, min=1, max=3).props("dense").classes("w-20")

            min_dur = ui.number("Min duration (s)", value=30, min=5, max=default_max, step=5).props("dense").classes("w-28")
            max_dur = ui.number("Max duration (s)", value=min(180, default_max), min=10, max=default_max, step=5).props("dense").classes("w-28")

        purpose = ui.select(
            {
                "News Package": "News Package (tight broadcast package: strong open, key soundbites, closer)",
                "Social Media Teaser": "Social Media Teaser (punchy curiosity hook — arresting first 8 seconds)",
                "Best Soundbites / Quotes": "Best Soundbites / Quotes (most quotable lines, intercut for maximum impact)",
                "Emotional / Human Story": "Emotional / Human Story (personal stakes & emotional contrast between voices)",
                "In-depth Highlight": "In-depth Highlight (deeper synthesis of key or surprising territory)",
                # new 2026
                "Investigative Cold Open": "Investigative Cold Open (high-suspense hook: facts + cliffhangers into the mystery)",
                "Expert Manifesto / Manifesto Call": "Expert Manifesto / Manifesto Call (mission-driven claims to collective call-to-action)",
                "Character Retrospective": "Character Retrospective (nostalgic, reflective look back on life, career or era)",
                "Social Jump-Cut Strip": "Social Jump-Cut Strip (ultra-aggressive TikTok/Shorts bursts — zero dead air)",
                "Three-Act Underdog Arc": "Three-Act Underdog Arc (strict 3-act underdog journey: struggle → epiphany → triumph)",
            },
            value="News Package", label="Purpose"
        ).props("dense").classes("w-full mt-2")

        tone = ui.select(
            {
                "newsroom": "Newsroom (tight & factual)",
                "flexible": "Flexible / Creative (atmospheric & juxtapositions)",
                "documentary": "Documentary (story-driven & atmospheric)",
                "corporate": "Corporate (professional & polished)",
                "commercial": "Commercial (persuasive & brand-driven)",
                "rewrite": "Verbatim Scriptwriter (new story across clips)",
                # new 2026 creative directorial styles
                "investigative_hook": "Investigative Hook (cinematic, cliffhangers)",
                "masterclass": "Masterclass (authoritative, long-form argument)",
                "confessional": "Confessional (vulnerable, intimate, preserves stumbles)",
                "engagement_bomb": "Engagement Bomb (ultra-fast TikTok/Shorts)",
                "subversive": "Subversive (witty, ironic, humanizing)",
                "visual_poem": "Visual Poem (atmospheric, rhythmic, musical)",
                "manifesto": "Manifesto (inspirational, mobilizing call-to-action)",
                "underdog": "Underdog (strict 3-act: struggle → pivot → triumph)",
                "analytical": "Analytical (cause-effect, data, objective)",
                "legacy": "Legacy (warm, wistful, reflective past-tense)",
            },
            value="newsroom", label="Tone / Role"
        ).props("dense").classes("w-full mt-1")

        clean_fillers = ui.checkbox("Remove filler words (cleaner sentences)", value=False).props("dense").classes("mt-2")

        narration_style = ui.select(
            {
                None: "No narration / voiceover script",
                "omniscient": "Omniscient (3rd-person journalistic — objective bridges, context, balance)",
                "subjective": "Subjective (1st-person reflective / essay-film style)",
                "explainer": "Explainer (high-energy, punchy short-form / social hook voice)",
            },
            value=None,
            label="Narration / Voiceover Style (optional)"
        ).props("dense").classes("w-full mt-2")

        # Narration budget controls for Director (applies to the discrete bridges in narrative_elements)
        with ui.row().classes("gap-2 mt-1"):
            d_narr_min_sec = ui.number("Narration min (s)", value=10, min=0, step=5).props("dense").classes("w-28").tooltip("Min total spoken seconds across all bridges in the story")
            d_narr_max_sec = ui.number("Narration max (s)", value=90, min=0, step=5).props("dense").classes("w-28").tooltip("Max total spoken seconds across all bridges")
            d_narr_min_b = ui.number("Min bridges", value=2, min=0, max=15).props("dense").classes("w-24").tooltip("Minimum number of discrete narration bridges to insert")
            d_narr_max_b = ui.number("Max bridges", value=6, min=0, max=15).props("dense").classes("w-24").tooltip("Maximum number of discrete narration bridges")

        generate_btn = ui.button("Generate Versions (AI Director)", icon="auto_awesome", color="primary").classes("mt-4 w-full")

        with ui.column().classes("w-full items-center mt-1"):
            ui.label("The Director works from the combined labeled transcripts only (no audio is sent).").classes("text-xs text-grey-5 text-center")
            ui.label("Choosing a narration style tells the AI to generate purposeful, sparing voiceover bridges in that linguistic perspective (see Text-to-Speech settings for style details). Bridges are inserted only where they add real value (every 3-5 clips or at key transitions).").classes("text-xs text-blue-400 text-center")

        results_container = ui.column().classes("w-full mt-4")

        def _build_multi_transcript_and_segments() -> tuple[str, list[dict]]:
            """
            Build a single labeled transcript string for the AI + a flat list of
            augmented segments that carry their source information.
            """
            lines = []
            flat_segments: list[dict] = []

            for s in sources:
                segs = _get_segments_for_source(s)
                if not segs:
                    continue
                lines.append(f"\n=== {s['label']} : {s['filename']} ({s['duration']:.0f}s) ===")
                for seg in segs:
                    start = seg.get("source_in") or seg.get("start", 0)
                    end = seg.get("source_out") or seg.get("end", 0)
                    text = (seg.get("text") or "").strip()
                    if not text:
                        continue
                    try:
                        start_f = float(start)
                        end_f = float(end)
                    except Exception:
                        continue
                    tc = f"[{start_f:.1f}s → {end_f:.1f}s]"
                    lines.append(f"{tc} {text}")

                    # Augmented segment that travels with the AI output
                    flat_segments.append({
                        "source_in": start_f,
                        "source_out": end_f,
                        "text": text,
                        "source_label": s["label"],
                        "source_filename": s["filename"],
                        "source_path": s["path"],
                        "source_clip_index": s["index"],
                    })
            return "\n".join(lines).strip(), flat_segments

        def _render_multi_versions(versions: list[dict]):
            # Always re-resolve times from per-source trans .txt sidecars before display
            # (ensures the list the user sees, and any immediate exports from buttons here,
            # use the authoritative verbatim ranges for combined narrative beats within a source).
            try:
                from minicat.core.video import repair_director_version_with_transcripts
                cat_root = None
                vids = []
                try:
                    from minicat.ui.app import get_state
                    st = get_state()
                    cat_root = getattr(st, "catalog_root", None) if st else None
                    vids = getattr(st, "videos", None) or []
                except Exception:
                    pass
                if cat_root:
                    for i, ver in enumerate(versions):
                        versions[i] = repair_director_version_with_transcripts(ver, cat_root, vids)
            except Exception as _rend_re:
                print(f"[AI Director] _render_multi_versions sidecar repair skipped: {_rend_re}")

            results_container.clear()
            with results_container:
                for version in versions:
                    with ui.card().classes("w-full mb-3 border"):
                        header = f"{version.get('version_id', '?')} — {version.get('title', 'Untitled')}"
                        with ui.row().classes("items-center gap-2"):
                            ui.label(header).classes("text-base font-semibold")
                            if version.get("narration_text"):
                                lang = version.get("narration_language", "")
                                tooltip = f"Narration script available ({lang}) — voiceover can be exported with the XML" if lang else "Narration script available — voiceover can be exported with the XML"
                                ui.label("🎙️").classes("text-lg").tooltip(tooltip)

                        dur = version.get("total_duration", 0)
                        dur_str = format_duration_timecode(dur, 25)
                        ui.label(f"Duration: {dur_str}  •  Multi-source cut").classes("text-xs text-grey-5 mb-2")

                        summary = version.get("narrative_summary", "")
                        if summary:
                            ui.markdown(summary).classes("text-sm mb-2")

                        # Prefer the new rich interleaved structure when available (read early so we can
                        # decide whether the AI Narration / Voiceover Script acts as NARRATION BRIDGE (TTS))
                        narrative_elements = version.get("narrative_elements") or []
                        segs = version.get("selected_segments", [])

                        # Show AI-generated narration text only if it was requested and generated.
                        # When narrative_elements has no explicit "narration" items, the script itself
                        # provides the NARRATION BRIDGE (TTS) and will be displayed in bridge styling below.
                        narration = version.get("narration_text", "").strip()
                        has_typed_narration_in_elements = bool(narrative_elements) and any(
                            (item or {}).get("type") == "narration" for item in (narrative_elements or [])
                        )
                        if narration and (not bool(narrative_elements) or has_typed_narration_in_elements):
                            lang = version.get("narration_language")
                            lang_label = f" ({lang})" if lang else ""
                            with ui.card().classes("w-full bg-blue-900/10 border border-blue-700 mb-2 p-2"):
                                ui.label(f"AI Narration / Voiceover Script{lang_label}").classes("text-sm font-semibold text-blue-300 mb-1")
                                ui.markdown(narration).classes("text-sm")

                        def make_delete_handler(ver, idx):
                            def handler():
                                if 0 <= idx < len(ver.get("selected_segments", [])):
                                    del ver["selected_segments"][idx]
                                    _render_multi_versions(versions)
                            return handler

                        if narrative_elements:
                            # New interleaved display (recommended)
                            for item in narrative_elements:
                                if item.get("type") == "clip":
                                    s = item.get("source_in", 0)
                                    e = item.get("source_out", 0)
                                    src_label = item.get("source_label") or "?"
                                    tc = f"{s:.1f}s → {e:.1f}s"
                                    with ui.column().classes("w-full mb-1"):
                                        with ui.row().classes("items-center gap-2 text-sm"):
                                            ui.label(f"[{src_label}]").classes("font-mono text-[10px] px-1.5 py-0.5 bg-amber-900/60 rounded text-amber-300 shrink-0")
                                            ui.label(tc).classes("font-mono text-xs w-28 shrink-0")
                                            ui.label((item.get("text") or "")[:90]).classes("flex-1 truncate")
                                elif item.get("type") == "narration":
                                    with ui.card().classes("w-full bg-blue-900/15 border-l-2 border-blue-500 mb-2 p-2"):
                                        provider_name = get_tts_provider_display_name()
                                        ui.label(f"🎙️ Narration bridge • {provider_name}").classes("text-xs font-semibold text-blue-300 mb-0.5")
                                        ui.markdown(item.get("text", "")).classes("text-sm text-blue-100")

                            # Treat AI Narration / Voiceover Script exactly like NARRATION BRIDGE (TTS):
                            # if narrative_elements was provided but had no explicit "narration" items,
                            # the script (narration_text) IS the bridge content for TTS/audio/XML.
                            if narrative_elements and not any((i or {}).get("type") == "narration" for i in narrative_elements):
                                script = (version.get("narration_text") or "").strip()
                                if script:
                                    with ui.card().classes("w-full bg-blue-900/15 border-l-2 border-blue-500 mb-2 p-2"):
                                        provider_name = get_tts_provider_display_name()
                                        ui.label(f"🎙️ Narration bridge (AI Narration / Voiceover Script) • {provider_name}").classes("text-xs font-semibold text-blue-300 mb-0.5")
                                        ui.markdown(script).classes("text-sm text-blue-100")
                        else:
                            # Legacy display
                            for idx, seg in enumerate(segs):
                                with ui.column().classes("w-full mb-2"):
                                    s = seg.get("source_in") or seg.get("start", 0)
                                    e = seg.get("source_out") or seg.get("end", 0)
                                    src_label = seg.get("source_label") or seg.get("source_filename") or "Unknown source"
                                    tc = f"{s:.1f}s → {e:.1f}s"

                                    with ui.row().classes("items-center gap-2 text-sm"):
                                        ui.label(f"[{src_label}]").classes("font-mono text-[10px] px-1.5 py-0.5 bg-zinc-800 rounded text-amber-400 shrink-0")
                                        ui.label(tc).classes("font-mono text-xs w-28 shrink-0")
                                        ui.label((seg.get("text") or "")[:80]).classes("flex-1 truncate")

                                    reason = (seg.get("reason") or "").strip()
                                    if reason:
                                        ui.label(f"   → {reason}").classes("text-xs text-grey-5 ml-1 italic")

                                    ui.button(icon="delete", on_click=make_delete_handler(version, idx)).props("size=xs flat color=negative").classes("self-end")

                        # Export row (multi-aware where implemented)
                        # Thin compatibility shell. Real work lives in the three dedicated exporters.
                        def _legacy_export_multi_xml(ver=version, generate_voiceover: bool = True, voiceover_language: str = "en", narration_as_titles: bool = False, voiceover_voice: str | None = None, pregenerated_vo_files: list[dict] | None = None):
                            """
                            Multi-source XMEML export.
                            - generate_voiceover=True  → generates audio bridges (WAV stereo 44100Hz for Piper local / MP3 for Google) + dedicated voiceover audio track for the AI Narration / Voiceover Script
                            - narration_as_titles=True → embeds narration bridges as visible <title> elements (no audio)
                            - voiceover_voice: Optional specific voice name (works for both local Piper and Google Cloud)
                            """
                            try:
                                segs = ver.get("selected_segments", [])
                                if not segs:
                                    ui.notify("No segments in this version", color="warning")
                                    return

                                # Collect unique source paths from the AI-selected segments
                                source_paths = {}
                                for seg in segs:
                                    p = seg.get("source_path")
                                    if p:
                                        source_paths[p] = seg.get("source_filename", _Path(p).name)

                                if not source_paths:
                                    ui.notify("No source file paths found in the AI segments", color="warning")
                                    return

                                # Always land in a fresh subfolder inside default library
                                suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
                                export_dir = create_export_subfolder(suggestion)
                                raw_name = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:30] }"
                                from minicat.core.settings import sanitize_for_filesystem
                                name = sanitize_for_filesystem(raw_name, max_len=80)
                                out_name = f"{name}.xml"
                                out_path = export_dir / out_name

                                source_list = list(source_paths.keys())

                                if len(source_list) == 1:
                                    # All segments from the same source file → use the high-quality single-source XMEML
                                    cut_segments = []
                                    for seg in segs:
                                        cut_segments.append({
                                            "source_in": seg.get("source_in") or seg.get("start"),
                                            "source_out": seg.get("source_out") or seg.get("end"),
                                            "text": seg.get("text", ""),
                                            "reason": seg.get("reason", "")
                                        })

                                    from minicat.ai.xmeml_exporter import generate_xmeml, get_video_timebase
                                    detected_timebase = get_video_timebase(source_list[0])
                                    generate_xmeml(
                                        cut_segments=cut_segments,
                                        source_video_path=source_list[0],
                                        output_path=out_path,
                                        sequence_name=f"AI Multi Cut - {ver.get('title', ver.get('version_id'))}",
                                        narrative_summary=ver.get('narrative_summary'),
                                        timebase=detected_timebase,
                                    )
                                    ui.notify(f"XML + AI DIRECTOR — MULTI-CLIP SCRIPT.txt exported (single-source high quality) → {out_path.name}", color="positive", duration=6)
                                    # script TXT is now exported centrally by the * _exporter functions when they create/use the subfolder
                                else:
                                    # Multi-source + narration/voiceover path is now fully owned by
                                    # narrative_vo_exporter.py (exporter #3). Delegate to the real implementation.
                                    from minicat.ai import narrative_vo_exporter

                                    result = narrative_vo_exporter.export_narrative_vo_xmeml(
                                        ver,
                                        generate_voiceover=generate_voiceover,
                                        voiceover_language=voiceover_language,
                                        narration_as_titles=narration_as_titles,
                                        voiceover_voice=voiceover_voice,
                                        pregenerated_vo_files=pregenerated_vo_files,
                                    )
                                    return result

                            except Exception as ex:
                                ui.notify(f"Multi XML export failed: {ex}", color="negative")
                                print(f"[Multi XML Export] {ex}")
                                import traceback
                                traceback.print_exc()

                        # (the _show_ai_director_xml_export_dialog was hoisted to module level above
                        # so it can be called from the "Load Saved AI Story" path as well)

                        # Check if this version actually contains AI-generated narration (supports both old and new formats)
                        has_narration = bool(version.get("narration_text")) or any(
                            item.get("type") == "narration"
                            for item in (version.get("narrative_elements") or [])
                        )

                        with ui.row().classes("gap-2 mt-2 w-full"):
                            # Always available: basic multi-source Premiere XML.
                            # If the version has an AI Narration / Voiceover Script, this will also
                            # export the script as audio (TTS voiceover using current default provider/language/voice)
                            # and add the audio track to the XML (narrations voiced and included in the timeline).
                            async def _export_basic_multi(ver=version):
                                try:
                                    # Fresh sidecar repair (Director multi) so that the XML and the always-bundled
                                    # "AI DIRECTOR — MULTI-CLIP SCRIPT.txt" use exact times from the per-source
                                    # trans .txt sidecars (full spans for any combined narrative beats).
                                    try:
                                        from minicat.core.video import repair_director_version_with_transcripts
                                        from minicat.ui.app import get_state
                                        st = get_state()
                                        cat_root = getattr(st, "catalog_root", None) if st else None
                                        vids = getattr(st, "videos", None) or []
                                        if cat_root:
                                            ver = repair_director_version_with_transcripts(ver, cat_root, vids)
                                    except Exception:
                                        pass

                                    has_narr = bool(ver.get("narration_text")) or any(
                                        item.get("type") == "narration"
                                        for item in (ver.get("narrative_elements") or [])
                                    )
                                    if has_narr:
                                        # Voice the AI narration script as audio (WAV for Piper local / MP3 for Google)
                                        # and include in XML at the correct interleaved positions. Use the
                                        # narration's own language (e.g. "fi") from the Director so the right
                                        # TTS model/voice is chosen for "AI Narration / Voiceover Script (fi)".
                                        voiceover_language = ver.get("narration_language") or "en"
                                        suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
                                        target_dir = create_export_subfolder(suggestion)
                                        out = await asyncio.to_thread(
                                            lambda: narrative_vo_exporter.export_narrative_vo_xmeml(
                                                ver,
                                                generate_voiceover=True,
                                                voiceover_language=voiceover_language,
                                                output_dir=target_dir,
                                            )
                                        )
                                        if out:
                                            try:
                                                nbridges = len([b for b in (get_narrative_sequence(ver) or []) if b.get("type") == "narration"])
                                            except Exception:
                                                nbridges = 0
                                            msg = f"XML + voiceovers + AI DIRECTOR — MULTI-CLIP SCRIPT.txt exported → {out.name} (in {target_dir.name})"
                                            ui.notify(msg, color="positive", duration=8)
                                        else:
                                            ui.notify("Multi XML export returned no file (see console)", color="warning")
                                    else:
                                        suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
                                        target_dir = create_export_subfolder(suggestion)
                                        out = await asyncio.to_thread(
                                            lambda: multi_xmeml_exporter.export_ai_director_multi_xmeml(ver, output_dir=target_dir)
                                        )
                                        if out:
                                            ui.notify(f"XML + AI DIRECTOR — MULTI-CLIP SCRIPT.txt exported → {out.name} (in {target_dir.name})", color="positive", duration=6)
                                        else:
                                            ui.notify("Multi XML export returned no file (see console)", color="warning")
                                except Exception as ex:
                                    ui.notify(f"Multi XML export failed: {ex}", color="negative")
                                    print(f"[Multi XML Export] {ex}")
                                    import traceback
                                    traceback.print_exc()

                            ui.button(
                                "Export XML",
                                icon="description",
                                on_click=_export_basic_multi
                            ).props("outline").classes("flex-1").tooltip(
                                "Export standard multi-source XMEML for AI Director. If the version has an AI Narration / Voiceover Script, it will be exported as audio (TTS) and added as a track in the XML."
                            )

                            def _save_this_story(v=version):
                                p = save_ai_director_story(v)
                                if p:
                                    ui.notify(f"Story saved as {p.name}. Use the “Load Story” button in the top bar to re-open it later for XML + Voiceover export — no need to re-run the AI Director.", color="positive", duration=10)
                                else:
                                    ui.notify("Failed to save story (see console)", color="negative")

                            ui.button(
                                "Save Story",
                                icon="save",
                                on_click=_save_this_story
                            ).props("outline").classes("flex-1").tooltip(
                                "Save this AI Director story (the cut + any narrations) so it can be reloaded later for EXPORT XML + VOICE OVERS"
                            )

                            # Additional options for narration (text titles only, or custom VO settings).
                            # Note: the main "Export XML" button above now automatically voices the AI Narration / Voiceover Script (if present) and includes the audio in the XML.
                            if has_narration:
                                async def _export_text_titles(ver=version):
                                    try:
                                        suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
                                        target_dir = create_export_subfolder(suggestion)
                                        out = await asyncio.to_thread(
                                            lambda: narrative_vo_exporter.export_narrative_vo_xmeml(
                                                ver, narration_as_titles=True, output_dir=target_dir
                                            )
                                        )
                                        if out:
                                            ui.notify(f"XML + Text Titles exported → {out.name} (in {target_dir.name})", color="positive", duration=6)
                                        else:
                                            ui.notify("Text titles XML export returned no file (see console)", color="warning")
                                    except Exception as ex:
                                        ui.notify(f"XML + Text Titles export failed: {ex}", color="negative")
                                        print(f"[XML + Text Titles Export] {ex}")
                                        import traceback
                                        traceback.print_exc()

                                ui.button(
                                    "XML + Text Titles",
                                    icon="title",
                                    on_click=_export_text_titles
                                ).props("outline").classes("flex-1").tooltip(
                                    "Export XML with narration bridges as visible on-screen <title> elements (no audio)"
                                )

                                ui.button(
                                    "XML + Voiceover Audio",
                                    icon="mic",
                                    on_click=lambda v=version: _show_ai_director_xml_export_dialog(v)
                                ).props("outline").tooltip(
                                    "Export XML with generated voiceover audio (WAV for local Piper / MP3 for Google) on a dedicated audio track at correct positions from the AI Narration / Voiceover Script"
                                )

                                ui.label("🎙️ Narration ready").classes("text-xs text-blue-400 self-center ml-2")

                            def export_multi_txt(ver=version):
                                """Wrapper for the standalone button (writes to default dir with slug name)."""
                                export_ai_director_multi_clip_script(ver)

                            def export_multi_txt(ver=version):
                                """Wrapper for the standalone button (writes to default dir with slug name)."""
                                export_ai_director_multi_clip_script(ver)

                            ui.button("Export as TXT Script", icon="description", on_click=export_multi_txt).props("outline")

        async def do_generate_multi():
            generate_btn.disable()
            results_container.clear()

            # Dynamic status area with spinner + updating text
            with results_container:
                with ui.row().classes("items-center gap-3 justify-center w-full"):
                    ui.spinner("dots").classes("mr-2")
                    status_label = ui.label("Preparing...").classes("text-sm text-grey-6 text-center")

            try:
                status_label.text = "Preparing combined transcript from all selected clips..."
                await asyncio.sleep(0.05)  # let UI update

                transcript_text, augmented_segments = _build_multi_transcript_and_segments()
                if not augmented_segments:
                    ui.notify("No usable transcript segments across the selected clips.", color="warning")
                    return

                # NOTE: AI Director runs on combined labeled TRANSCRIPTS ONLY.
                # No audio is sent to the Director (by explicit request).
                audio_for_listening = None

                primary_src = sources[0]
                primary_label = primary_src['label']

                status_label.text = f"Sending combined labeled transcript from {primary_label} (and other clips) to AI Director..."
                await asyncio.sleep(0.03)

                # Use smaller font so the long status fits on a single centered row with the spinner
                status_label.classes(replace="text-xs text-grey-6 text-center")
                status_label.text = "AI Director is analyzing all sources and building narrative versions... (usually 25–90 seconds)"

                # Use the canonical combined labeled transcript + Director module
                canonical_transcript, _ = build_combined_labeled_transcript(sources)

                versions = await asyncio.to_thread(
                    generate_director_cuts,
                    augmented_segments,                    # still carries source_* keys
                    float(max_dur.value),
                    min_duration_seconds=float(min_dur.value),
                    purpose=purpose.value,
                    tone=tone.value,
                    num_versions=int(num_versions.value),
                    clean_fillers=bool(clean_fillers.value),
                    narration_style=narration_style.value,  # new style enum: None | "omniscient" | "subjective" | "explainer"
                    source_media_path=None,   # Explicitly no audio for Director (transcript-only)
                    combined_transcript=canonical_transcript,  # the one clean labeled document
                    source_count=len(sources),
                    material_language=primary_language,   # Pass the real original language from metadata
                    narration_min_seconds=float(d_narr_min_sec.value or 0),
                    narration_max_seconds=float(d_narr_max_sec.value or 0),
                    narration_min_bridges=int(d_narr_min_b.value or 0),
                    narration_max_bridges=int(d_narr_max_b.value or 0),
                )

                # AI Journalist and Director are now transcript-only (no audio sent to Gemini).

                status_label.text = "Processing AI response and preparing results..."

                # Make sure the source metadata survives validation/normalization in the cutter.
                # We post-process to re-attach source info using text+timing match (robust enough).
                _reattach_source_info(versions, augmented_segments)

                # Enforce trans .txt sidecar as source of truth for Director too (long combined
                # narrative "Text" beats within one source clip must use the full verbatim span
                # from that clip's authoritative sidecar, exactly like Journalist).
                try:
                    from minicat.core.video import repair_director_version_with_transcripts
                    cat_root = None
                    vids = []
                    try:
                        from minicat.ui.app import get_state
                        st = get_state()
                        cat_root = getattr(st, "catalog_root", None) if st else None
                        vids = getattr(st, "videos", None) or []
                    except Exception:
                        pass
                    if cat_root:
                        for i, ver in enumerate(versions):
                            versions[i] = repair_director_version_with_transcripts(ver, cat_root, vids)
                except Exception as _dir_re:
                    print(f"[AI Director] post-gen sidecar transcript repair skipped: {_dir_re}")

                # Attach the scripted/transcripted language so that TXT script/story exports
                # can produce *all* details (labels, headers, "Teksti:", notes...) in matching language.
                for v in versions:
                    if not v.get("script_language"):
                        v["script_language"] = primary_language
                    v["narration_min_seconds"] = float(d_narr_min_sec.value or 0)
                    v["narration_max_seconds"] = float(d_narr_max_sec.value or 0)
                    v["narration_min_bridges"] = int(d_narr_min_b.value or 0)
                    v["narration_max_bridges"] = int(d_narr_max_b.value or 0)

                # Safety net: If the user did NOT request a narration style, strip any narration
                # the model may have added anyway (especially common in creative rewrite tone).
                if not narration_style.value:
                    for v in versions:
                        if "narrative_elements" in v:
                            v["narrative_elements"] = [
                                item for item in v.get("narrative_elements", [])
                                if item.get("type") != "narration"
                            ]
                        if "narration_text" in v:
                            v["narration_text"] = ""
                        if "narration_language" in v:
                            v.pop("narration_language", None)

                # === Post-generation diversity check for multi-clip ===
                # The AI sometimes "cheats" and mostly uses only the first clip.
                # We detect this and retry with a very direct corrective prompt if needed.
                model_name = get_gemini_model()

                def _count_used_sources(ver):
                    used = set()
                    for seg in ver.get("selected_segments", []):
                        src = seg.get("source_label") or seg.get("source_filename")
                        if src:
                            used.add(src)
                    return len(used)

                min_desired = min(3, len(sources))  # at least 3 if possible
                needs_retry = any(_count_used_sources(v) < 2 for v in versions)  # at least 2 different clips

                if needs_retry and len(sources) >= 2:
                    print(f"[AI Director] Low source diversity detected (mostly used one clip). Retrying with stronger instruction...")
                    status_label.text = "Director mostly used one clip — retrying with stronger diversity enforcement..."

                    corrective_system = (
                        "You are a journalist/director building multi-clip stories. "
                        "Your previous attempt only used material from one or two clips. "
                        "This is unacceptable. You MUST create versions that meaningfully use spoken material "
                        f"from at least {min_desired} different clips (C1, C2, etc.). "
                        "Intercut between the clips. Use the full set of material provided."
                    )

                    corrective_user = f"""
Previous attempt failed the diversity requirement.

Here is the material again (with labels C1, C2...):

{transcript_text}

Create {num_versions} versions that properly use material from MULTIPLE different clips.
Return only the JSON array.
"""

                    try:
                        # Create client for the corrective retry
                        from google import genai
                        from minicat.core.settings import get_gemini_api_key
                        api_key = get_gemini_api_key()
                        retry_client = genai.Client(api_key=api_key.strip()) if api_key else None

                        if retry_client:
                            retry_response = retry_client.models.generate_content(
                                model=model_name,
                                contents=[corrective_system, corrective_user],
                                config=types.GenerateContentConfig(
                                    response_mime_type="application/json",
                                    temperature=0.85,
                                ),
                            )
                            raw_retry = retry_response.text.strip()
                            if raw_retry.startswith("```"):
                                raw_retry = raw_retry.split("```")[1]
                                if raw_retry.lower().startswith("json"):
                                    raw_retry = raw_retry[4:].strip()
                            retry_data = json.loads(raw_retry)
                            retry_versions = validate_and_normalize_versions(retry_data, num_versions)
                            _reattach_source_info(retry_versions, augmented_segments)
                            versions = retry_versions
                            print("[AI Director] Retry successful with better diversity.")
                        else:
                            print("[AI Director] Could not create client for retry.")
                    except Exception as retry_ex:
                        print(f"[AI Director] Retry also failed: {retry_ex}")

                _render_multi_versions(versions)

            except Exception as ex:
                results_container.clear()
                with results_container:
                    ui.label(f"AI Director failed: {ex}").classes("text-negative")
                    ui.label("Check the console for more details.").classes("text-xs text-grey-5")
                print(f"[AI Director] {ex}")
                import traceback; traceback.print_exc()
            finally:
                generate_btn.enable()

        generate_btn.on_click(do_generate_multi)

    dlg.open()


def _reattach_source_info(versions: list[dict], original_augmented: list[dict]) -> None:
    """After the cutter validates/normalizes, put back the multi-source keys using best-effort match.

    Matching strategy (in order of preference):
    1. Exact (rounded time + text prefix)
    2. Text content match (most reliable for verbatim)
    3. Parse explicit source from the AI's 'reason' field (very common pattern)
    4. Nearest time match within tolerance (with preference for matching source if known from reason)
    5. (New robust fallback) Direct source_label match (C1/C2... as required by Director prompt) to attach source_path etc.
       This guarantees that the plain "Export XML" (no VO) button and basic multi export always produce a file.
    """
    import re

    # Primary lookup by time + text
    time_text_lookup = {}
    # Secondary lookup by normalized text
    text_lookup = {}
    # Per-source lists for smarter fallback
    by_source = {}  # source_label -> list of orig segments

    for s in original_augmented:
        t_key = (round(s.get("source_in", 0), 2), round(s.get("source_out", 0), 2), (s.get("text") or "")[:60])
        time_text_lookup[t_key] = s

        norm_text = (s.get("text") or "").strip().lower()[:80]
        if norm_text:
            text_lookup[norm_text] = s

        src = s.get("source_label") or s.get("source_filename")
        if src:
            by_source.setdefault(src, []).append(s)

    for ver in versions:
        # Repair both legacy selected_segments and the rich narrative_elements (clip items)
        seg_lists = []
        if ver.get("selected_segments"):
            seg_lists.append(ver.get("selected_segments", []))
        for item in ver.get("narrative_elements", []) or []:
            if isinstance(item, dict) and item.get("type") == "clip":
                seg_lists.append([item])  # wrap so we can treat uniformly below

        for seg_list in seg_lists:
            for seg in seg_list:
                matched = None
                reason = seg.get("reason") or ""

                # 1. Try exact time + text
                k = (round(seg.get("source_in", 0), 2), round(seg.get("source_out", 0), 2), (seg.get("text") or "")[:60])
                if k in time_text_lookup:
                    matched = time_text_lookup[k]

                # 2. Try by text content (very reliable for verbatim)
                if not matched:
                    norm = (seg.get("text") or "").strip().lower()[:80]
                    if norm in text_lookup:
                        matched = text_lookup[norm]

                # 3. Try to parse explicit source from the AI's reason field
                if not matched and reason:
                    src_match = re.search(r'\b(C\d+)\b', reason, re.IGNORECASE)
                    if src_match:
                        guessed_src = src_match.group(1).upper()
                        candidates = by_source.get(guessed_src, [])
                        if candidates:
                            seg_start = seg.get("source_in", 0) or 0
                            best_dist = 999
                            best = None
                            for orig in candidates:
                                dist = abs((orig.get("source_in", 0) or 0) - seg_start)
                                if dist < best_dist:
                                    best_dist = dist
                                    best = orig
                            if best:
                                matched = best

                # 4. Fallback: nearest time match (within 1.5s tolerance)
                if not matched:
                    best_dist = 999
                    best_match = None
                    seg_start = seg.get("source_in", 0) or 0
                    guessed_src = None
                    if reason:
                        m = re.search(r'\b(C\d+)\b', reason, re.IGNORECASE)
                        if m:
                            guessed_src = m.group(1).upper()

                    for orig in original_augmented:
                        dist = abs((orig.get("source_in", 0) or 0) - seg_start)
                        if dist < best_dist and dist < 1.5:
                            if guessed_src:
                                orig_src = orig.get("source_label") or orig.get("source_filename")
                                if orig_src and orig_src.upper() == guessed_src:
                                    best_dist = dist
                                    best_match = orig
                                    continue
                            best_dist = dist
                            best_match = orig

                    if best_match:
                        matched = best_match

                if matched:
                    for extra_key in ("source_label", "source_filename", "source_path", "source_clip_index"):
                        if extra_key in matched:
                            seg[extra_key] = matched[extra_key]
                    # Repair the *times* (core of making material sync to the script text)
                    if "source_in" in matched and "source_out" in matched:
                        try:
                            real_in = float(matched["source_in"])
                            real_out = float(matched["source_out"])
                            if real_out > real_in + 0.1:
                                seg["source_in"] = round(real_in, 2)
                                seg["source_out"] = round(real_out, 2)
                        except Exception:
                            pass

                # Robust fallback: AI Director prompt requires "source_label" (C1, C2, ...) on every clip.
                # Use it to attach the full source_path/filename etc from the original augmented list
                # (which always has the ground-truth paths). This ensures "Export XML" (without VO)
                # and other exports always succeed even if time/text fuzzy matching in reattach failed.
                src_label = seg.get("source_label")
                if src_label and "source_path" not in seg:
                    for orig in original_augmented:
                        orig_lbl = orig.get("source_label") or orig.get("source_filename")
                        if orig_lbl and str(orig_lbl).strip().upper() == str(src_label).strip().upper():
                            for extra_key in ("source_label", "source_filename", "source_path", "source_clip_index"):
                                if extra_key in orig:
                                    seg[extra_key] = orig[extra_key]
                            # Also correct the times from original for perfect material sync
                            if "source_in" in orig and "source_out" in orig:
                                try:
                                    seg["source_in"] = round(float(orig["source_in"]), 2)
                                    seg["source_out"] = round(float(orig["source_out"]), 2)
                                except Exception:
                                    pass
                            break


def _get_ai_director_stories_dir() -> Path:
    """Return (and create) the directory used for saved AI Director stories (under default exports)."""
    d = get_default_export_directory() / "ai_director_stories"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _reveal_stories_folder(stories_dir: Path) -> None:
    """Open the ai_director_stories folder in the OS file manager (Finder on macOS, etc)."""
    try:
        import platform
        import subprocess
        p = Path(stories_dir).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        sysname = platform.system()
        if sysname == "Darwin":
            subprocess.run(["open", str(p)], check=False)
        elif sysname == "Windows":
            subprocess.run(["explorer", str(p)], check=False)
        else:
            # Linux / others
            subprocess.run(["xdg-open", str(p)], check=False)
        ui.notify(f"Opened folder: {p}", color="info", duration=3)
    except Exception as ex:
        ui.notify(f"Could not open folder: {ex}", color="warning")
        # Fallback: show path
        try:
            ui.notify(f"Saved stories location: {stories_dir}", color="info", duration=8)
        except Exception:
            pass


def save_ai_director_story(version: dict) -> Path | None:
    """Persist a Director-built story (with narrations) so it can be reloaded later for full XML + VO export."""
    try:
        stories_dir = _get_ai_director_stories_dir()
        ver_id = version.get("version_id", "X")
        title = (version.get("title") or "Untitled Story").replace(" ", "_")[:50]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"AIStory_{ver_id}_{title}_{ts}.json"
        out_path = stories_dir / fname

        payload = {
            "format": "cat-tag-ai-director-story",
            "format_version": 1,
            "saved_at": datetime.now().isoformat(),
            "version": version,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return out_path
    except Exception as ex:
        print(f"[AI Director] Failed to save story: {ex}")
        return None


def load_ai_director_story(file_bytes: bytes) -> dict | None:
    """Load a previously saved AI Director story from its JSON bytes."""
    try:
        text = file_bytes.decode("utf-8")
        payload = json.loads(text)
        ver = payload.get("version") or payload
        if not isinstance(ver, dict):
            return None
        # Basic sanity: must have some segments
        if not (ver.get("selected_segments") or ver.get("narrative_elements")):
            return None
        return ver
    except Exception as ex:
        print(f"[AI Director] Failed to load story: {ex}")
        return None


def load_ai_director_story_and_show_export(story_path: Path | str) -> None:
    """Directly load a specific saved AI Director story (.json) and immediately
    render its AI Narration / Voiceover Script to audio (WAV via forced local/Piper for the script
    using the story's saved language e.g. 'fi') with progress, then export the full
    XMEML with the voiceover bridges added at their correct interleaved positions.

    This makes "minicat open AIStory_....json" or loading via the UI "just work"
    for re-rendering the narrations & making the XML, treating the script the same
    as any NARRATION BRIDGE (TTS).
    """
    try:
        p = Path(story_path).expanduser().resolve()
        if not p.exists() or not p.is_file():
            ui.notify(f"Story file not found: {p}", color="negative")
            return
        ver = load_ai_director_story(p.read_bytes())
        if not ver:
            ui.notify("The selected file is not a valid saved AI Director story.", color="negative")
            return
        ui.notify(f"Loaded story “{ver.get('title', p.name)}” — ready for export.", color="positive")
        _repair_loaded_story_sources(ver)
        # Also repair times from sidecars (so loaded old stories get full verbatim spans too)
        try:
            from minicat.core.video import repair_director_version_with_transcripts
            from minicat.ui.app import get_state
            st = get_state()
            cat_root = getattr(st, "catalog_root", None) if st else None
            vids = getattr(st, "videos", None) or []
            if cat_root:
                ver = repair_director_version_with_transcripts(ver, cat_root, vids)
        except Exception:
            pass
        has_narr = bool(ver.get("narration_text")) or any(
            item.get("type") == "narration" for item in (ver.get("narrative_elements") or [])
        )
        if has_narr:
            # Directly render the AI Narration / Voiceover Script (as WAV bridges via its language + provider)
            # and produce XML with them added. No extra choice dialog — "load to render narrations & XML" just works.
            ui.timer(0.1, lambda v=ver: _perform_narration_vo_export_for_loaded_story(v), once=True)
        else:
            try:
                suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
                target_dir = create_export_subfolder(suggestion)
                out = multi_xmeml_exporter.export_ai_director_multi_xmeml(ver, output_dir=target_dir)
                if out:
                    ui.notify(f"XML + AI DIRECTOR — MULTI-CLIP SCRIPT.txt exported → {out.name} (in {target_dir.name})", color="positive", duration=6)
                else:
                    ui.notify("Export returned no file", color="warning")
            except Exception as ex:
                ui.notify(f"XML export failed: {ex}", color="negative")
    except Exception as ex:
        ui.notify(f"Could not load story: {ex}", color="negative")
        print(f"[Load AI Story direct] {ex}")


def _repair_loaded_story_sources(ver: dict) -> None:
    """Best-effort repair for loaded AI Director stories.

    If the saved JSON is missing "source_path" on segments (older saves, or the
    absolute paths have changed), and a catalog is currently open in the app,
    re-attach the paths by matching on "source_filename". This lets users open a
    saved AIStory_*.json later (even in a fresh launch or different catalog view)
    and still successfully render Narrations + Export XML with correct sources.
    """
    try:
        state = get_state()
        if not state or not getattr(state, "videos", None):
            return
        # filename -> list[Video]
        by_name: dict[str, list] = {}
        for v in (getattr(state, "videos", []) or []):
            fn = getattr(v, "filename", None)
            if not fn and getattr(v, "path", None):
                fn = Path(v.path).name
            if fn:
                by_name.setdefault(fn, []).append(v)
                # also index by stem for leniency
                stem = Path(fn).stem
                if stem and stem != fn:
                    by_name.setdefault(stem, []).append(v)

        # Collect segment lists (selected + any clip items inside narrative_elements)
        seg_lists: list[list[dict]] = []
        if ver.get("selected_segments"):
            seg_lists.append(ver.get("selected_segments", []))
        for item in (ver.get("narrative_elements") or []):
            if isinstance(item, dict) and item.get("type") == "clip":
                seg_lists.append([item])

        for seg_list in seg_lists:
            for seg in seg_list:
                if seg.get("source_path"):
                    continue  # keep the saved one if present
                fn = seg.get("source_filename") or seg.get("source_label")
                if not fn:
                    continue
                cands = by_name.get(fn) or by_name.get(Path(str(fn)).name) or by_name.get(Path(str(fn)).stem)
                if cands:
                    v = cands[0]
                    p = getattr(v, "path", None)
                    if p:
                        seg["source_path"] = str(Path(p).resolve())
                        if not seg.get("source_filename"):
                            seg["source_filename"] = getattr(v, "filename", Path(p).name)
                        print(f"[Load Story] Re-attached source_path via catalog match for {fn}")
    except Exception as ex:
        print(f"[Load Story] source repair skipped: {ex}")


def _show_load_ai_director_story_dialog() -> None:
    """Hoisted dialog to load a previously saved AI Director story (JSON).
    Shows recent stories from <default export dir>/ai_director_stories as a convenient list
    (what users often call "loading a project" after saving an AI cut).
    Also supports manual file upload.
    Available from the top bar only.
    """
    with ui.dialog() as load_dlg, ui.card().classes("w-[560px]"):
        ui.label("Load Saved AI Director Story").classes("text-h6 mb-2")
        ui.label("Open a previously saved AI cut (with narrations/voiceover script) to export the full multi-source XMEML + optional voiceovers without re-running the Director.").classes("text-sm text-grey-5 mb-3")

        # Prominent way to open a *specific* file the user has (e.g. AIStory_....json they just saved)
        def _choose_specific_story_file():
            try:
                import webview
                win = webview.active_window() or (webview.windows[0] if webview.windows else None)
                if win:
                    res = win.create_file_dialog(
                        webview.FileDialog.OPEN,
                        directory=str(Path.home()),
                        allow_multiple=False,
                        file_types=("JSON files (*.json)", "*.json")
                    )
                    if res:
                        chosen = Path(res[0] if isinstance(res, (list, tuple)) else res)
                        load_dlg.close()
                        load_ai_director_story_and_show_export(chosen)
                        return
            except Exception:
                pass
            # Fallback to the upload below
            ui.notify("Use the file chooser below or drag a .json", color="info")

        ui.button(
            "Choose specific .json file (your AIStory_....json)...",
            icon="file_open",
            on_click=_choose_specific_story_file,
            color="primary"
        ).classes("w-full mb-3").props("size=md")

        stories_dir = _get_ai_director_stories_dir()
        story_files = sorted(
            [p for p in stories_dir.glob("*.json") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:15]

        def _load_story_file(path: Path):
            try:
                ver = load_ai_director_story(path.read_bytes())
                if not ver:
                    ui.notify("The selected file is not a valid saved AI Director story.", color="negative")
                    return
                load_dlg.close()
                ui.notify(f"Loaded story “{ver.get('title', 'Untitled')}” — ready for export.", color="positive")
                _repair_loaded_story_sources(ver)
                # Also repair times from sidecars (so loaded old stories get full verbatim spans too)
                try:
                    from minicat.core.video import repair_director_version_with_transcripts
                    from minicat.ui.app import get_state
                    st = get_state()
                    cat_root = getattr(st, "catalog_root", None) if st else None
                    vids = getattr(st, "videos", None) or []
                    if cat_root:
                        ver = repair_director_version_with_transcripts(ver, cat_root, vids)
                except Exception:
                    pass
                has_narr = bool(ver.get("narration_text")) or any(
                    item.get("type") == "narration" for item in (ver.get("narrative_elements") or [])
                )
                if has_narr:
                    # Directly render the AI Narration / Voiceover Script (as WAV bridges via its language + provider)
                    # and produce XML with them added. No extra choice dialog — "load to render narrations & XML" just works.
                    ui.timer(0.1, lambda v=ver: _perform_narration_vo_export_for_loaded_story(v), once=True)
                else:
                    try:
                        suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
                        target_dir = create_export_subfolder(suggestion)
                        out = multi_xmeml_exporter.export_ai_director_multi_xmeml(ver, output_dir=target_dir)
                        if out:
                            ui.notify(f"XML + AI DIRECTOR — MULTI-CLIP SCRIPT.txt exported → {out.name} (in {target_dir.name})", color="positive", duration=6)
                        else:
                            ui.notify("Export returned no file", color="warning")
                    except Exception as ex:
                        ui.notify(f"XML export failed: {ex}", color="negative")
            except Exception as ex:
                ui.notify(f"Could not load story: {ex}", color="negative")
                print(f"[Load AI Story] {ex}")

        if story_files:
            ui.label("Recent saved stories (from your export dir / ai_director_stories):").classes("text-sm font-medium mt-1 mb-1")
            with ui.column().classes("w-full max-h-[220px] overflow-auto border border-zinc-800 rounded p-1 mb-3 gap-y-0.5"):
                for p in story_files:
                    try:
                        raw = p.read_text(encoding="utf-8", errors="ignore")
                        payload = json.loads(raw)
                        ver = payload.get("version") or payload
                        title = (ver.get("title") or p.stem)[:60]
                        n = len(ver.get("selected_segments") or ver.get("narrative_elements") or [])
                        saved_at = payload.get("saved_at", "")
                        if saved_at:
                            try:
                                dt = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
                                when = dt.strftime("%Y-%m-%d %H:%M")
                            except Exception:
                                when = saved_at[:16]
                        else:
                            when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                        label = f"{title}  •  {n} clips  •  {when}"
                    except Exception:
                        label = p.name

                    with ui.row().classes("w-full items-center justify-between hover:bg-zinc-800 rounded cursor-pointer px-2 py-1 text-sm").on("click", lambda pp=p: _load_story_file(pp)):
                        ui.label(label).classes("truncate flex-1")
                        ui.button(icon="play_arrow", on_click=lambda pp=p: _load_story_file(pp)).props("flat dense size=xs round").tooltip("Load this story")
            with ui.row().classes("w-full justify-between items-center mb-2"):
                ui.button("Open stories folder", icon="folder", on_click=lambda: _reveal_stories_folder(stories_dir)).props("outline size=sm").classes("text-xs")
                ui.label(f"{len(story_files)} recent").classes("text-xs text-grey-5")
        else:
            ui.label("No saved stories yet in the standard location.").classes("text-xs text-grey-5 mb-2")
            ui.button("Open stories folder", icon="folder", on_click=lambda: _reveal_stories_folder(stories_dir)).props("outline size=sm").classes("mb-3")

        ui.label("Or load a .json from anywhere:").classes("text-xs text-grey-5 mt-1 mb-1")

        def handle_story_upload(e):
            try:
                ver = load_ai_director_story(e.content.read())
                if not ver:
                    ui.notify("The selected file is not a valid saved AI Director story.", color="negative")
                    return
                load_dlg.close()
                ui.notify(f"Loaded story “{ver.get('title', 'Untitled')}” — ready for export.", color="positive")
                _repair_loaded_story_sources(ver)
                # Also repair times from sidecars (so loaded old stories get full verbatim spans too)
                try:
                    from minicat.core.video import repair_director_version_with_transcripts
                    from minicat.ui.app import get_state
                    st = get_state()
                    cat_root = getattr(st, "catalog_root", None) if st else None
                    vids = getattr(st, "videos", None) or []
                    if cat_root:
                        ver = repair_director_version_with_transcripts(ver, cat_root, vids)
                except Exception:
                    pass
                has_narr = bool(ver.get("narration_text")) or any(
                    item.get("type") == "narration" for item in (ver.get("narrative_elements") or [])
                )
                if has_narr:
                    # Directly render the AI Narration / Voiceover Script (as WAV bridges via its language + provider)
                    # and produce XML with them added. No extra choice dialog — "load to render narrations & XML" just works.
                    ui.timer(0.1, lambda v=ver: _perform_narration_vo_export_for_loaded_story(v), once=True)
                else:
                    try:
                        suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
                        target_dir = create_export_subfolder(suggestion)
                        out = multi_xmeml_exporter.export_ai_director_multi_xmeml(ver, output_dir=target_dir)
                        if out:
                            ui.notify(f"XML + AI DIRECTOR — MULTI-CLIP SCRIPT.txt exported → {out.name} (in {target_dir.name})", color="positive", duration=6)
                        else:
                            ui.notify("Export returned no file", color="warning")
                    except Exception as ex:
                        ui.notify(f"XML export failed: {ex}", color="negative")
            except Exception as ex:
                ui.notify(f"Could not load story: {ex}", color="negative")
                print(f"[Load AI Story] {ex}")

        ui.upload(
            label="Choose saved .json story file",
            on_upload=handle_story_upload,
            auto_upload=True,
            max_files=1,
        ).props('accept=".json"').classes("w-full mb-2")

        with ui.row().classes("w-full justify-end"):
            ui.button("Cancel", on_click=load_dlg.close).props("flat")

    load_dlg.open()


# --- Hoisted export dialog so it can be used from load path too ---
def _show_ai_director_xml_export_dialog(ver: dict):
    """Small dialog to choose voiceover options when exporting XML (hoisted for reuse from saved stories)."""
    with ui.dialog() as dlg, ui.card().classes("w-[420px]"):
        ui.label("Export AI Director XML").classes("text-h6 mb-2")
        ui.label("Voiceover narration (if enabled) will be included as a separate audio track in the XML with correct timeline position.").classes("text-xs text-grey-5 mb-3")

        # Voiceover options
        generate_vo = ui.checkbox(
            "Generate voiceover narration audio",
            value=True
        ).classes("mb-2")

        # === Robust language selector (defensive against stale pycache / bad narration_language values) ===
        try:
            from minicat.core.settings import SUPPORTED_LANGUAGES as _SETTINGS_LANGS
        except Exception:
            _SETTINGS_LANGS = None
        _supported = _SETTINGS_LANGS or [
            ("en", "English"),
            ("fi", "Finnish"),
            ("de", "German"),
            ("sv", "Swedish"),
            ("fr", "French"),
            ("es", "Spanish"),
        ]
        lang_options = {code: f"{name} ({code})" for code, name in _supported}
        valid_codes = set(lang_options.keys())

        # Prefer settings > version data > safe fallback
        from minicat.core.settings import get_tts_default_language, get_tts_voice, clean_tts_voice, clean_tts_language
        raw_default = clean_tts_language(ver.get("narration_language") or get_tts_default_language() or "en") or "en"
        if raw_default in valid_codes:
            default_lang = raw_default
        elif "en" in valid_codes:
            default_lang = "en"
        elif lang_options:
            default_lang = next(iter(lang_options.keys()))
        else:
            default_lang = "en"

        # Final safety net
        codes = list(lang_options.keys())
        if default_lang not in codes:
            default_lang = "en" if "en" in codes else (codes[0] if codes else "en")

        # Use two-step select creation (value=None first, then assign + update)
        # to avoid NiceGUI "Invalid value: fi" etc. at construction time.
        lang_select = ui.select(
            options=lang_options,
            value=None,
            label="Voiceover Language"
        ).props("dense").classes("w-full mb-4")
        if default_lang in codes:
            lang_select.value = default_lang
        else:
            lang_select.value = codes[0] if codes else "en"
        lang_select.update()

        # Voice selector - shown for both local (Piper) and Google providers.
        tts_provider = get_tts_provider()
        supports_custom_voice = tts_provider in ("local", "google")

        def update_voice_options():
            if not voice_select or not supports_custom_voice:
                return
            lang = clean_tts_language((lang_select.value if lang_select else None) or default_lang) or default_lang
            if tts_provider == "local":
                voices = get_piper_voices_for_language(lang) or []
            else:
                voices = get_google_voices_for_language(lang) or []
            if voices:
                voice_select.options = voices
                cur_val = clean_tts_voice(voice_select.value)
                vnames = [v[0] for v in voices]
                if not cur_val or cur_val not in vnames:
                    voice_select.value = voices[0][0]
                voice_select.update()
            else:
                voice_select.options = [("default", "default voice for language")]
                voice_select.value = "default"
                voice_select.update()

        lang_select.on_value_change(lambda e: update_voice_options())

        if tts_provider == "local":
            voice_list = get_piper_voices_for_language(default_lang) or []
            voice_label = "Local Voice (Piper, offline)"
        else:
            voice_list = get_google_voices_for_language(default_lang) or []
            voice_label = "Google Voice (WaveNet recommended for quality – 4M free tier/month)"

        default_voice = clean_tts_voice(get_tts_voice())
        preferred_voice = None
        voice_names = [v[0] for v in voice_list] if voice_list else []
        if default_voice and default_voice in voice_names:
            preferred_voice = default_voice
        elif voice_list:
            preferred_voice = voice_list[0][0]

        # Two-step for voice too
        voice_select = ui.select(
            options=voice_list if voice_list else [("default", "default voice for language")],
            value=None,
            label=voice_label,
        ).props("dense").classes("w-full mb-3")

        if preferred_voice is not None:
            vnames = [v[0] for v in (voice_select.options or voice_list or [])]
            if preferred_voice in vnames:
                voice_select.value = preferred_voice
            elif voice_list:
                voice_select.value = voice_list[0][0]
        elif voice_list:
            voice_select.value = voice_list[0][0]
        voice_select.update()

        if not supports_custom_voice:
            voice_select.disable()

        try:
            update_voice_options()
        except Exception:
            pass

        # Disable language/voice selects when checkbox is off
        def toggle_lang(e):
            lang_select.enabled = e.value
            voice_select.enabled = e.value and supports_custom_voice
        generate_vo.on_value_change(toggle_lang)
        lang_select.enabled = generate_vo.value
        voice_select.enabled = generate_vo.value and supports_custom_voice

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dlg.close).props("flat")

            async def do_export():
                dlg.close()
                do_generate_vo = bool(generate_vo.value)
                selected_lang = lang_select.value if do_generate_vo else "en"
                selected_lang = clean_tts_language(selected_lang) or "en"
                selected_voice = None
                if voice_select and do_generate_vo and supports_custom_voice:
                    selected_voice = clean_tts_voice(voice_select.value)

                if not do_generate_vo:
                    # Fast path (no audio generation, just XML or titles-only via other buttons)
                    # Compute subfolder so even titles-only or no-VO lands grouped
                    suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
                    target_dir = create_export_subfolder(suggestion)
                    def _run_vo_export():
                        return narrative_vo_exporter.export_narrative_vo_xmeml(
                            ver,
                            generate_voiceover=False,
                            voiceover_language=selected_lang,
                            voiceover_voice=selected_voice,
                            output_dir=target_dir,
                        )
                    try:
                        out_path = await asyncio.to_thread(_run_vo_export)
                        if out_path:
                            ui.notify(f"XML + AI DIRECTOR — MULTI-CLIP SCRIPT.txt exported → {out_path.name} (in {target_dir.name})", color="positive", duration=6)
                        else:
                            ui.notify("XML export returned no file (see console)", color="warning")
                    except Exception as ex:
                        ui.notify(f"XML export failed: {ex}", color="negative")
                        print(f"[XML Export] {ex}")
                        import traceback
                        traceback.print_exc()
                    return

                # === VO generation path with live progress bar ===
                # We generate bridges one-by-one (awaitable) so we can show per-bridge status.
                # Once all audio files + their technical details (dur, sr, ch) are known, we
                # build the timeline XML (with picture clips shifted for correct narration gaps).
                with ui.dialog() as prog_dlg, ui.card().classes("w-[520px]"):
                    ui.label("Generating Narration Voiceovers").classes("text-h6 mb-1")
                    ui.label("The system will generate WAV (Piper) or MP3 (Google) for each narration bridge, probe exact duration + audio specs (sr/ch), then emit a timeline XML with VOs at their correct interleaved positions.").classes("text-xs text-grey-5 mb-2")
                    pbar = ui.linear_progress(value=0.0, show_value=True).props("size=lg")
                    status_label = ui.label("Preparing TTS...").classes("text-sm mt-1")
                    detail_label = ui.label("").classes("text-xs text-grey-6 wrap")
                    btn_row = ui.row().classes("justify-end mt-2")
                    with btn_row:
                        ui.button("Cancel", on_click=prog_dlg.close).props("flat size=sm")

                prog_dlg.open()

                vo_files: list[dict] = []
                try:
                    narrative_sequence = get_narrative_sequence(ver) or []
                    narration_bridges = [item for item in narrative_sequence if item.get("type") == "narration"]
                    n = len([b for b in narration_bridges if (b.get("text") or "").strip()])
                    # Always create a fresh subfolder inside the default export directory (e.g. ~/CAT+TAG/Exports) for this export
                    suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
                    export_dir = create_export_subfolder(suggestion)
                    name = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:30] }"
                    lang_to_use = selected_lang

                    if n > 0:
                        # Ensure provider package (once)
                        try:
                            provider = get_tts_provider()
                        except Exception:
                            provider = "local"
                        if provider == "local":
                            ensure_piper_package()
                        else:
                            ensure_google_tts_package()

                        # Piper now outputs WAV (stereo, 44100Hz); Google keeps MP3
                        # All VO audio files (bridges + Narration.wav) use the same format as the exported Narrations:
                        # 44100Hz stereo 16-bit PCM WAV
                        vo_ext = "wav"

                        bridge_num = 0
                        for item in narration_bridges:
                            bridge_text = (item.get("text") or "").strip()
                            if not bridge_text:
                                continue
                            bridge_num += 1
                            status_label.text = f"Generating bridge {bridge_num} of {n}..."
                            pbar.value = max(0.05, (bridge_num - 0.6) / max(1, n))
                            short = bridge_text if len(bridge_text) <= 90 else bridge_text[:87] + "..."
                            detail_label.text = short

                            vo_name = f"Narration_Bridge{bridge_num:02d}.{vo_ext}"
                            vo_p = export_dir / vo_name  # export_dir is already the new per-export subfolder

                            try:
                                await asyncio.to_thread(
                                    generate_narration_audio_sync,
                                    text=bridge_text,
                                    language=lang_to_use,
                                    output_path=vo_p,
                                    voice=selected_voice,
                                )
                                # Probe duration (for timeline placement) + technical audio details (for XML <media>)
                                try:
                                    _, vo_dur = get_media_start_offset_and_duration(vo_p)
                                    vo_duration = float(vo_dur) if vo_dur else 0.0
                                except Exception:
                                    vo_duration = 0.0
                                try:
                                    ainfo = get_audio_characteristics(vo_p)
                                except Exception:
                                    ainfo = {"channels": 1, "sample_rate": 44100}

                                vo_files.append({
                                    "path": vo_p,
                                    "text": bridge_text,
                                    "index": bridge_num,
                                    "duration": vo_duration,
                                    "sample_rate": ainfo.get("sample_rate", 44100),
                                    "channels": ainfo.get("channels", 1),
                                })
                                print(f"[Narrative VO Exporter] Bridge {bridge_num}/{n} ready: {vo_duration:.2f}s sr={ainfo.get('sample_rate')} ch={ainfo.get('channels')}")
                            except Exception as vo_ex:
                                print(f"[Narrative VO Exporter] Bridge {bridge_num} generation failed: {vo_ex}")
                                # continue without this bridge; XML will still be valid

                            pbar.value = bridge_num / max(1, n)

                    # Additionally export the full AI Narration / Voiceover Script as "Narration.wav"
                    # (using the combined script text) so the requested filename is always produced
                    # for the narration script.
                    full_script = (ver.get("narration_text") or "").strip()
                    if not full_script and narration_bridges:
                        full_script = "\n\n".join(b.get("text", "").strip() for b in narration_bridges if b.get("text"))
                    if full_script:
                        nar_p = export_dir / "Narration.wav"  # lands inside the per-export subfolder
                        try:
                            await asyncio.to_thread(
                                generate_narration_audio_sync,
                                text=full_script,
                                language=lang_to_use,
                                output_path=nar_p,
                                voice=selected_voice,
                            )
                            print(f"[Narrative VO Exporter] Exported full narration script as Narration.wav")
                            # Also add to vo_files so Narration.wav gets imported/added to the XML (extra VO clip)
                            try:
                                _, nar_dur = get_media_start_offset_and_duration(nar_p)
                                nar_duration = float(nar_dur) if nar_dur else 0.0
                                ainfo = get_audio_characteristics(nar_p)
                                vo_files.append({
                                    "path": nar_p,
                                    "text": full_script,
                                    "index": 99,
                                    "duration": nar_duration,
                                    "sample_rate": ainfo.get("sample_rate", 44100),
                                    "channels": ainfo.get("channels", 1),
                                })
                            except Exception as ex:
                                print(f"[Narrative VO Exporter] Could not add Narration.wav to pregen for XML: {ex}")
                        except Exception as ex:
                            print(f"[Narrative VO Exporter] Failed to export Narration.wav: {ex}")
                    else:
                        status_label.text = "No narration bridges – building XML..."

                    # All bridges done (or none). Now build XML using the pre-generated files
                    # (so exporter knows real durs + audio specs, and we get correct placement + gaps).
                    status_label.text = "Building XMEML with voiceovers at correct positions..."
                    pbar.value = 0.97
                    await asyncio.sleep(0.03)

                    def _run_with_pregen():
                        return narrative_vo_exporter.export_narrative_vo_xmeml(
                            ver,
                            generate_voiceover=True,
                            voiceover_language=lang_to_use,
                            voiceover_voice=selected_voice,
                            pregenerated_vo_files=vo_files,
                            output_dir=export_dir,
                        )

                    out_path = await asyncio.to_thread(_run_with_pregen)
                    prog_dlg.close()

                    if out_path:
                        msg = f"XML + voiceovers + AI DIRECTOR — MULTI-CLIP SCRIPT.txt exported → {out_path.name} (folder: {export_dir.name})"
                        ui.notify(msg, color="positive", duration=8)
                    else:
                        ui.notify("Voiceover XML export returned no file (see console)", color="warning")
                except Exception as ex:
                    try:
                        prog_dlg.close()
                    except Exception:
                        pass
                    ui.notify(f"XML + Voiceover export failed: {ex}", color="negative")
                    print(f"[XML + Voiceover Export] {ex}")
                    import traceback
                    traceback.print_exc()

            ui.button("Export XML", icon="description", color="primary", on_click=do_export)

    dlg.open()


def export_ai_director_multi_clip_script(ver: dict, target_dir: Path | None = None) -> Path | None:
    """Export a human-readable script that clearly attributes every line to its original clip.

    The ver is re-repaired from per-source trans .txt sidecars immediately before
    formatting so that any long combined "Text" beats use the full authoritative spans
    (exactly as for AI Journalist rich scripts). This is the final safety net even for
    loaded stories or direct calls.
    """
    try:
        from minicat.core.video import repair_director_version_with_transcripts
        from minicat.ui.app import get_state
        st = get_state()
        cat_root = getattr(st, "catalog_root", None) if st else None
        vids = getattr(st, "videos", None) or []
        if cat_root:
            ver = repair_director_version_with_transcripts(ver, cat_root, vids)
    except Exception:
        pass

    # Determine the scripted/transcripted language for this Director version so that
    # the *entire* output TXT (all headers, labels, notes, "Text:", "Why chosen:" etc.)
    # is in the same language as the content. Prefer explicit on ver; fall back to en.
    script_lang = "en"
    for k in ("script_language", "narration_language", "material_language", "content_language"):
        val = ver.get(k)
        if val:
            script_lang = val
            break
    labels = get_script_labels(script_lang)

    try:
        from datetime import datetime
        if target_dir is not None:
            export_dir = Path(target_dir)
            export_dir.mkdir(parents=True, exist_ok=True)
            out_name = "AI DIRECTOR — MULTI-CLIP SCRIPT.txt"
            out_path = export_dir / out_name
        else:
            export_dir = get_default_export_directory()
            base = "MultiClip"
            ver_id = ver.get('version_id', 'X')
            title_slug = (ver.get('title') or 'Story').replace(' ', '_')[:30]
            out_name = f"{base}_{ver_id}_{title_slug}.txt"
            out_path = export_dir / out_name

        lines = []
        lines.append("=" * 72)
        lines.append(labels["ai_director_multi"])
        lines.append("=" * 72)
        lines.append(f"{labels['exported']} {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append(f"{labels['version']}  {ver.get('version_id', '?')} — {ver.get('title', 'Untitled')}")
        dur_str = format_duration_timecode(ver.get('total_duration', 0), 25)
        lines.append(f"{labels['duration']} {dur_str}")
        lines.append("")

        summary = ver.get('narrative_summary', '').strip()
        if summary:
            lines.append(labels["editorial_summary"])
            lines.append("-" * 72)
            lines.append(summary)
            lines.append("")

        # Build the narrative sequence (clips + optional interleaved narration bridges).
        # get_narrative_sequence returns only clip items (no bridges) when narration
        # was not enabled / stripped.
        try:
            narrative_sequence = get_narrative_sequence(ver) or []
        except Exception:
            narrative_sequence = []
            for seg in ver.get("selected_segments", []):
                narrative_sequence.append({
                    "type": "clip",
                    "source_label": seg.get("source_label"),
                    "source_in": seg.get("source_in") or seg.get("start"),
                    "source_out": seg.get("source_out") or seg.get("end"),
                    "text": seg.get("text"),
                    "reason": seg.get("reason"),
                    "source_path": seg.get("source_path"),
                    "source_filename": seg.get("source_filename"),
                })
            # Legacy fallback for combined narration_text when sequence helper unavailable
            narration_text = (ver.get("narration_text") or "").strip()
            if narration_text:
                narrative_sequence.append({"type": "narration", "text": narration_text})

        has_bridges = any(item.get("type") == "narration" for item in narrative_sequence)

        if has_bridges:
            lines.append(labels["narrative_script"])
            lines.append("-" * 72)
            lines.append("")
            lines.append(labels["narration_explain_line1"])
            lines.append(labels["narration_explain_line2"])
            lines.append(labels["narration_explain_line3"])
            lines.append(labels["narration_explain_line4"])
            lines.append("")
        else:
            lines.append(labels["selected_content"])
            lines.append("-" * 72)
            lines.append("")

        lines.append(labels["note_xml_line1"])
        lines.append(labels["note_xml_line2"])
        lines.append(labels["note_xml_line3"])
        lines.append(labels["note_xml_line4"])
        lines.append("")

        def fmt(t):
            try:
                t = float(t or 0)
            except Exception:
                t = 0.0
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s2 = t % 60
            return f"{h:02d}:{m:02d}:{s2:05.2f}" if h > 0 else f"{m:02d}:{s2:05.2f}"

        clip_num = 0
        bridge_note_shown = False
        for item in narrative_sequence:
            if item.get("type") == "clip":
                clip_num += 1
                s = item.get("source_in") or item.get("start", 0)
                e = item.get("source_out") or item.get("end", 0)
                src_label = item.get("source_label") or "Unknown"
                src_fname = item.get("source_filename")
                if src_fname:
                    src = f"{src_label} - {src_fname}"
                else:
                    src = src_label
                dur = max(0.0, (e - s))
                lines.append(f"\n{clip_num}. [{fmt(s)} → {fmt(e)}] ({dur:.2f}s)  — from {src}")
                lines.append(f"   {labels['text_label_dir']}   {item.get('text', '')}")
                reason = (item.get('reason') or '').strip()
                if reason:
                    lines.append(f"   {labels['why_chosen']} {reason}")
                else:
                    lines.append(f"   {labels['why_chosen']} {labels['why_chosen_default']}")
            elif item.get("type") == "narration":
                # Only present when narration was enabled for this version
                bridge_text = (item.get("text") or "").strip()
                if bridge_text:
                    lines.append("")
                    lines.append(labels["narration_bridge"])
                    for bt_line in bridge_text.splitlines():
                        lines.append(f"      {bt_line}")
                    if not bridge_note_shown:
                        lines.append(labels["bridge_note"])
                        bridge_note_shown = True
                    lines.append("")

        content = "\n".join(lines)
        out_path.write_text(content, encoding="utf-8")
        if target_dir is None:
            ui.notify(f"Script exported to {out_path}", color="positive", duration=6)
        return out_path
    except Exception as ex:
        if target_dir is None:
            ui.notify(f"TXT export failed: {ex}", color="negative")
        print(f"[AI Director] Multi-clip script TXT export failed: {ex}")
        return None


def _perform_narration_vo_export_for_loaded_story(ver: dict):
    """Sync entry point (must be called from a UI context such as a timer callback or event handler).

    Creates the progress dialog + labels in the current NiceGUI slot/context,
    then schedules the async work (generation + XML build) in a task.
    This avoids creating UI elements from a bare asyncio task (which has no slot stack).

    Used by load story paths so loading a saved AIStory_*.json with narration_text
    automatically renders the AI Narration / Voiceover Script (as WAV bridges)
    using the story's language + provider and includes them in the XML.
    """
    # Local imports for TTS helpers (keep top-level clean)
    from minicat.core.settings import (
        get_tts_default_language,
        get_tts_voice,
        clean_tts_voice,
        clean_tts_language,
    )
    from minicat.ai.voiceover import (
        generate_narration_audio_sync,
        ensure_piper_package,
        get_voice_for_language,
        get_piper_voices_for_language,
    )

    lang_to_use = clean_tts_language(
        ver.get("narration_language") or get_tts_default_language() or "en"
    ) or "en"

    # Force "local" (Piper) for loaded stories' AI Narration / Voiceover Script rendering.
    # This guarantees WAV (stereo 44100Hz) output using Piper for the script (e.g. 'fi'),
    # offline, no Google creds needed. Matches user's explicit requirement:
    # "AI Narration / Voiceover Script (fi) - use piper tts" and "get these out as audio (wav)".
    # The global provider setting still controls the choice dialog for live Director exports.
    # We also import get_tts_provider but override here.
    provider = "local"
    selected_voice = None
    try:
        vlist = get_piper_voices_for_language(lang_to_use) or []
        if vlist:
            saved = clean_tts_voice(get_tts_voice())
            if saved and any(saved == v[0] for v in vlist):
                selected_voice = saved
            else:
                selected_voice = vlist[0][0]
    except Exception:
        selected_voice = None

    # Create progress UI *synchronously* here (we are in a timer/event callback that has a slot)
    with ui.dialog() as prog_dlg, ui.card().classes("w-[520px]"):
        ui.label("Generating Narration Voiceovers").classes("text-h6 mb-1")
        ui.label(
            f"Rendering the AI Narration / Voiceover Script for this story as audio ({lang_to_use}, forced local/Piper for WAV). "
            "Bridges will be WAV and added to the XML at their correct positions."
        ).classes("text-xs text-grey-5 mb-2")
        pbar = ui.linear_progress(value=0.0, show_value=True).props("size=lg")
        status_label = ui.label("Preparing TTS for narration script...").classes("text-sm mt-1")
        detail_label = ui.label("").classes("text-xs text-grey-6 wrap")
        with ui.row().classes("justify-end mt-2"):
            ui.button("Cancel", on_click=prog_dlg.close).props("flat size=sm")

    prog_dlg.open()

    # Safe progress holder (plain dataclass — worker mutates ONLY this; poll timer does all UI).
    # This is the key fix for "RuntimeError: slot stack is empty" when loading saved stories
    # that trigger VO export (including cases that previously printed the [Sanitize] log).
    progress: NarrationVOProgress = NarrationVOProgress(
        status="Preparing TTS for narration script...",
        value=0.0,
        detail="",
    )

    # Polling timer MUST be created in this safe UI context (timer callback has slot).
    # It performs every .text / value / close / notify / ui.update. Worker never touches UI.
    poll_timer_ref = {"timer": None}  # box so nested funcs can stop it

    def _poll_vo_progress():
        tmr = poll_timer_ref.get("timer")
        try:
            if status_label is not None:
                if progress.status:
                    status_label.text = progress.status
                try:
                    ui.update(status_label)
                except Exception:
                    pass
            if detail_label is not None:
                if progress.detail is not None:
                    detail_label.text = progress.detail
                try:
                    ui.update(detail_label)
                except Exception:
                    pass
            if pbar is not None:
                try:
                    pbar.value = max(0.0, min(1.0, float(progress.value or 0.0)))
                    ui.update(pbar)
                except Exception:
                    pass

            if progress.is_complete:
                # Stop polling and close from safe context
                try:
                    if tmr:
                        tmr.active = False
                except Exception:
                    pass
                try:
                    prog_dlg.close()
                except Exception:
                    pass

                if progress.error:
                    ui.notify(f"Failed to render narration VO for loaded story: {progress.error}", color="negative")
                    print(f"[Loaded Story VO] {progress.error}")
                elif progress.result_path:
                    ui.notify(progress.status or "Narration VO + XML export complete.", color="positive", duration=8)
                else:
                    ui.notify(progress.status or "Narration VO export finished (no file)", color="warning")
        except Exception as poll_err:
            print(f"[Loaded Story VO] poll error (non-fatal): {poll_err}")

    poll_timer = ui.timer(0.18, _poll_vo_progress)
    poll_timer_ref["timer"] = poll_timer

    # Background worker: ONLY mutates the progress holder. No ui.*, no dlg, no labels.
    # All awaits (TTS + XML export) are safe here.
    async def _worker(p: NarrationVOProgress):
        vo_files: list[dict] = []
        export_dir = None
        try:
            narrative_sequence = get_narrative_sequence(ver) or []
            narration_bridges = [item for item in narrative_sequence if item.get("type") == "narration"]
            n = len([b for b in narration_bridges if (b.get("text") or "").strip()])

            suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
            export_dir = create_export_subfolder(suggestion)
            # name var was unused in original; keep for parity if needed later
            _ = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:30] }"

            p.status = "Preparing TTS for narration script..."
            p.value = 0.01
            p.detail = ""

            if n > 0:
                # Force local/Piper + WAV stereo 44.1k (all audio files now same format as the exported Narration.wav)
                ensure_piper_package()
                vo_ext = "wav"

                bridge_num = 0
                for item in narration_bridges:
                    bridge_text = (item.get("text") or "").strip()
                    if not bridge_text:
                        continue
                    bridge_num += 1
                    p.status = f"Generating bridge {bridge_num} of {n} for narration script..."
                    p.detail = bridge_text if len(bridge_text) <= 90 else bridge_text[:87] + "..."
                    p.value = max(0.05, (bridge_num - 0.6) / max(1, n))

                    vo_name = f"Narration_Bridge{bridge_num:02d}.{vo_ext}"
                    vo_p = export_dir / vo_name  # inside the new per-export subfolder

                    try:
                        await asyncio.to_thread(
                            generate_narration_audio_sync,
                            text=bridge_text,
                            language=lang_to_use,
                            output_path=vo_p,
                            voice=selected_voice,
                        )
                        try:
                            _, vo_dur = get_media_start_offset_and_duration(vo_p)
                            vo_duration = float(vo_dur) if vo_dur else 0.0
                        except Exception:
                            vo_duration = 0.0
                        try:
                            ainfo = get_audio_characteristics(vo_p)
                        except Exception:
                            ainfo = {"channels": 2, "sample_rate": 44100}

                        vo_files.append({
                            "path": vo_p,
                            "text": bridge_text,
                            "index": bridge_num,
                            "duration": vo_duration,
                            "sample_rate": ainfo.get("sample_rate", 44100),
                            "channels": ainfo.get("channels", 1),
                        })
                        print(f"[Loaded Story VO] Bridge {bridge_num}/{n} ready from narration script")
                    except Exception as vo_ex:
                        print(f"[Loaded Story VO] Bridge {bridge_num} generation failed: {vo_ex}")
                        # continue; XML will still be produced (without this bridge's audio)

                    p.value = bridge_num / max(1, n)

                # Additionally export the full AI Narration / Voiceover Script as "Narration.wav"
                # as explicitly requested. This is the voiced version of the entire script text
                # (joined from narration_text or the bridges), using Piper WAV. The per-bridge
                # files are still generated for correct timeline placement of individual parts.
                full_script = (ver.get("narration_text") or "").strip()
                if not full_script and narration_bridges:
                    full_script = "\n\n".join(b.get("text", "").strip() for b in narration_bridges if b.get("text"))
                if full_script:
                    nar_p = export_dir / "Narration.wav"  # inside the new per-export subfolder
                    try:
                        await asyncio.to_thread(
                            generate_narration_audio_sync,
                            text=full_script,
                            language=lang_to_use,
                            output_path=nar_p,
                            voice=selected_voice,
                        )
                        print(f"[Loaded Story VO] Exported full narration script as Narration.wav")
                        # Also add to vo_files so it gets imported/added to the XML (as extra VO clip at end of track)
                        try:
                            _, nar_dur = get_media_start_offset_and_duration(nar_p)
                            nar_duration = float(nar_dur) if nar_dur else 0.0
                            ainfo = get_audio_characteristics(nar_p)
                            vo_files.append({
                                "path": nar_p,
                                "text": full_script,
                                "index": 99,
                                "duration": nar_duration,
                                "sample_rate": ainfo.get("sample_rate", 44100),
                                "channels": ainfo.get("channels", 1),
                            })
                        except Exception as ex:
                            print(f"[Loaded Story VO] Could not add Narration.wav to pregen for XML: {ex}")
                    except Exception as ex:
                        print(f"[Loaded Story VO] Failed to export Narration.wav for script: {ex}")
            if n == 0:
                p.status = "No narration bridges in the loaded story – building XML..."
                p.detail = ""

            # Build the XML using the (possibly partial) pre-generated VO files for the script
            p.status = "Building XMEML with voiceovers from the narration script at correct positions..."
            p.value = 0.97
            await asyncio.sleep(0.03)

            def _run_with_pregen():
                return narrative_vo_exporter.export_narrative_vo_xmeml(
                    ver,
                    generate_voiceover=True,
                    voiceover_language=lang_to_use,
                    voiceover_voice=selected_voice,
                    pregenerated_vo_files=vo_files,
                    output_dir=export_dir,
                )

            out_path = await asyncio.to_thread(_run_with_pregen)

            if out_path:
                p.result_path = str(out_path)
                p.status = f"XML + voiceovers + AI DIRECTOR — MULTI-CLIP SCRIPT.txt exported → {out_path.name} (folder: {export_dir.name})"
            else:
                p.status = "Narration VO export returned no file (see console)"
            p.value = 1.0
            p.is_complete = True

        except Exception as ex:
            p.error = str(ex)
            p.is_complete = True
            print(f"[Loaded Story VO] {ex}")
            import traceback
            traceback.print_exc()

    asyncio.create_task(_worker(progress))


def _show_rich_client_dialog(state: AppState, client_id: int | None = None):
    """Full rich editor for a Client (contact info, notes, projects overview, placeholders for attachments/calendar)."""
    if client_id:
        client = db.get_client(state.catalog_root, client_id)
    else:
        client = Client(name="")

    if not client:
        ui.notify("Client not found", color="negative")
        return

    with ui.dialog() as dialog, ui.card().classes("w-[620px]"):
        title = f"Client: {client.name}" if client.id else "New Client"
        ui.label(title).classes("text-h5 mb-2")

        with ui.column().classes("gap-2"):
            name = ui.input("Client Name *", value=client.name).props("autofocus")
            contact = ui.input("Contact Person", value=client.contact_person or "")
            email = ui.input("Email", value=client.email or "")
            phone = ui.input("Phone", value=client.phone or "")
            address = ui.textarea("Address", value=client.address or "").props("rows=2")
            notes = ui.textarea("Notes", value=client.notes or "").props("rows=3")

            # Color picker (simple text for now)
            color = ui.input("Color (hex, optional)", value=client.color or "")

        ui.separator().classes("my-3")

        # Projects belonging to this client
        ui.label("Projects").classes("text-base font-semibold mb-2")
        projects_under_client = db.get_projects_for_client(state.catalog_root, client.id) if client.id else []

        if projects_under_client:
            for p in projects_under_client:
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.label(p).classes("text-sm")
                    ui.button(icon="info", on_click=lambda pp=p: (_show_rich_project_dialog(state, pp), dialog.close())).props("size=sm flat dense round")
        else:
            ui.label("No projects assigned yet.").classes("text-xs text-grey-5 italic")

        ui.separator().classes("my-3")

        # Placeholders for future rich features
        with ui.row().classes("gap-4"):
            with ui.column().classes("flex-1"):
                ui.label("Attachments").classes("text-sm font-semibold")
                ui.label("(Coming soon: attach contracts, briefs, invoices...)").classes("text-xs text-grey-5 italic")

            with ui.column().classes("flex-1"):
                ui.label("Calendar / Timeline").classes("text-sm font-semibold")
                ui.label("(Coming soon: project schedule view for this client)").classes("text-xs text-grey-5 italic")

        def save_client():
            new_client = Client(
                id=client.id,
                name=name.value.strip(),
                contact_person=contact.value.strip() or None,
                email=email.value.strip() or None,
                phone=phone.value.strip() or None,
                address=address.value.strip() or None,
                notes=notes.value.strip() or None,
                color=color.value.strip() or None,
            )
            if not new_client.name:
                ui.notify("Client name is required", color="warning")
                return

            saved = db.create_or_update_client(state.catalog_root, new_client)
            dialog.close()
            refresh_all_ui(state)
            ui.notify(f"Client '{saved.name}' saved", color="positive")

        with ui.row().classes("justify-between gap-2 mt-4 w-full"):
            ui.button("Delete", on_click=lambda: (ui_dialogs.delete_client_dialog(state, client), dialog.close()), color="negative").props("flat")
            with ui.row().classes("gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save Client", on_click=save_client, color="primary")

    dialog.open()


def _play_video(path: str, prefer_proxy: bool = True):
    """Open the video file.
    On macOS: Tries Quick Look first (qlmanage -p), falls back to default app if it fails.
    Prefers proxy when available.
    """
    import platform
    import subprocess
    try:
        orig = Path(path).expanduser().resolve()
        to_play = orig

        current_state = get_state()
        if prefer_proxy and current_state and current_state.selected:
            v = current_state.selected
            if v.project:
                proxy_dir = current_state.catalog_root / "proxies" / v.project
                proxy_path = proxy_dir / (orig.stem + "_proxy.mov")
                if proxy_path.exists():
                    to_play = proxy_path
                    print(f"[Playback] Using proxy: {to_play}")

        if not to_play.exists():
            ui.notify(f"File not found: {to_play}", color="negative")
            return

        if platform.system() == "Darwin":
            prefer_quicklook = get_preference("use_quicklook", True)

            if prefer_quicklook:
                # Try macOS Quick Look first
                used_quicklook = False
                try:
                    result = subprocess.run(
                        ["qlmanage", "-p", str(to_play)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False
                    )
                    if result.returncode == 0:
                        used_quicklook = True
                except Exception as ex:
                    print(f"[Playback] qlmanage error: {ex}")

                if not used_quicklook:
                    subprocess.run(["open", str(to_play)])
            else:
                # User prefers default player
                subprocess.run(["open", str(to_play)])
        elif platform.system() == "Windows":
            os.startfile(str(to_play))
        else:
            subprocess.run(["xdg-open", str(to_play)])
    except Exception as e:
        ui.notify(f"Could not play video: {e}", color="negative")
        print(f"[Playback] Error: {e}")


def _create_media_toolbar(state: AppState) -> None:
    """Shared toolbar for both Grid and List views."""
    # Show active project (and other filters) prominently on top when filtering
    _render_active_filters(state)

    with ui.row().classes("items-center justify-between mb-3"):
        sel_count = len(state.selected_ids)
        label_text = f"{len(state.videos)} clips"
        if sel_count > 0:
            label_text += f"  •  {sel_count} selected"
        ui.label(label_text).classes("text-caption")

        with ui.row().classes("gap-2 items-center"):
            # Redesigned sort control - simple and reliable
            sort_choices = [
                ("Shoot Date (newest first)", "shoot_date", True),
                ("Shoot Date (oldest first)", "shoot_date", False),
                ("Filename (A–Z)", "filename", False),
                ("Filename (Z–A)", "filename", True),
                ("Duration (longest first)", "duration", True),
                ("Import Date (newest first)", "import_date", True),
            ]

            # Build label -> (field, desc) map for clean lookup
            sort_map = {label: (field, desc) for label, field, desc in sort_choices}
            current_sort_label = next(
                (label for label, field, desc in sort_choices if field == state.sort_field and desc == state.sort_desc),
                sort_choices[0][0]
            )

            sort_select = ui.select(
                options=[label for label, _, _ in sort_choices],
                value=current_sort_label,
                label="Sort"
            ).props("dense").classes("w-52")

            def on_sort_change(e):
                # NiceGUI passes ValueChangeEventArguments, not the raw value
                new_label = e.value
                if new_label in sort_map:
                    field, desc = sort_map[new_label]
                    state.set_sort(field, desc)

            sort_select.on_value_change(on_sort_change)

            # Selection actions
            ui.button("Select All Visible", icon="select_all", on_click=lambda: _select_all_visible(state)).props("size=sm outline")

            if sel_count > 0:
                ui.button("Unselect All", icon="clear", on_click=state.clear_selection).props("size=sm outline")

            # Column customization for list view (reorder + hide columns)
            if state.view_mode == "list":
                ui.button("Columns", icon="view_column", on_click=lambda: _show_list_column_customizer(state)).props("size=sm outline")


def render_media_card(v: Video, state: AppState):
    """High-density, 16:9 cinematic media card."""
    is_sel = state.is_selected(v)
    card_classes = "cursor-pointer hover:shadow-lg transition-all relative rounded-lg border border-zinc-800 bg-zinc-900/40"
    if is_sel:
        card_classes += " ring-2 ring-blue-500 bg-blue-900/10"

    with ui.card().classes(card_classes + " overflow-hidden").on(
        "click", lambda vv=v: state.set_single_selection(vv)
    ):
        # === MULTI-SELECT CHECKBOX (top-left) ===
        # Allows proper multi-selection without forcing single-select on every click
        is_sel = state.is_selected(v)
        with ui.element("div").classes("absolute top-1 left-1 z-30 bg-black/60 rounded-sm p-0.5"):
            cb = ui.checkbox(value=is_sel).props("dense color=primary")
            cb.on("click.stop.prevent", lambda vv=v: state.toggle_select(vv))
        # === THUMBNAIL + OVERLAYS (restored nice visual style) ===
        is_audio = False
        try:
            from minicat.cli.main import _is_audio_file
            is_audio = _is_audio_file(Path(v.path))
        except Exception:
            is_audio = v.filename.lower().endswith((".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aif"))

        with ui.element("div").classes("relative w-full aspect-video bg-black overflow-hidden"):
            # The actual thumbnail or placeholder
            thumb = str(v.thumbnail_path) if v.thumbnail_path and Path(v.thumbnail_path).exists() else ""
            if thumb:
                ui.image(thumb).classes("w-full h-full object-cover")
            elif is_audio:
                with ui.element("div").classes("w-full h-full bg-[#1a2332] flex items-center justify-center"):
                    ui.icon("graphic_eq", size="2.8em").classes("text-blue-400")
            else:
                with ui.element("div").classes("w-full h-full bg-zinc-800 flex items-center justify-center"):
                    ui.icon("movie", size="2.8em").classes("text-zinc-500")

            # Duration overlay - bottom left
            dur_str = format_duration_mmss(v.duration)
            with ui.element("div").classes(
                "absolute bottom-1 left-1 z-10 px-1.5 py-px bg-black/90 text-white text-xs font-mono rounded-sm leading-none flex items-center"
            ):
                ui.label(dur_str)

            # Resolution / type overlay - bottom right
            res_str = "AUDIO" if is_audio else get_resolution_label(v.width, v.height)
            with ui.element("div").classes(
                "absolute bottom-1 right-1 z-10 px-1.5 py-px bg-black/90 text-white text-xs font-medium rounded-sm leading-none flex items-center"
            ):
                ui.label(res_str)

            # Optional subtle play hint overlay (right side)
            with ui.element("div").classes(
                "absolute bottom-5 right-1 z-10 px-1 py-0.5 bg-black/70 text-white text-xs rounded-sm leading-none flex items-center cursor-pointer hover:bg-black/90 transition-colors"
            ).on("click.stop", lambda vv=v: _play_current_from_card(vv)):
                ui.icon("play_arrow", size="0.95rem").classes("text-white")

            # Timecode overlay - top center (new requested position)
            if getattr(v, "tc_start", None) or getattr(v, "tc_end", None):
                tc_display = f"{v.tc_start or '—'} → {v.tc_end or '—'}"
                with ui.element("div").classes(
                    "absolute top-1 left-1/2 -translate-x-1/2 z-20 px-1.5 py-px bg-black/90 text-amber-400 text-[10px] font-mono rounded-sm leading-none flex items-center whitespace-nowrap"
                ):
                    ui.label(tc_display).classes("whitespace-nowrap")

        # === UNDER THE IMAGE: Name + Action buttons + Metadata ===
        with ui.column().classes("w-full q-pa-sm gap-y-0"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(v.filename).classes("text-sm font-semibold text-zinc-100 truncate flex-1")
                with ui.row().classes("gap-0.5"):
                    # AI Tag Suggestion
                    has_ai_tags = bool(getattr(v, "tags", None))
                    ai_color = "text-blue-400" if has_ai_tags else "text-zinc-500"
                    has_storyboard = bool(getattr(v, "storyboard_path", None))
                    has_transcript = bool(getattr(v, "transcription_segments", None))

                    is_audio_only = False
                    try:
                        from minicat.cli.main import _is_audio_file
                        is_audio_only = _is_audio_file(Path(v.path))
                    except Exception:
                        is_audio_only = v.filename.lower().endswith((".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aif"))

                    if has_ai_tags:
                        ai_tip = "AI tags suggested (click to ask again)"
                    elif has_storyboard:
                        ai_tip = "Ask AI for tags (storyboard)"
                    elif has_transcript:
                        if is_audio_only:
                            ai_tip = "Ask AI for tags (based on audio transcription)"
                        else:
                            ai_tip = "Ask AI for tags (transcript)"
                    else:
                        if is_audio_only:
                            ai_tip = "Transcribe first to enable AI tagging"
                        else:
                            ai_tip = "Ask AI for tags (needs storyboard or transcription)"

                    ui.button(
                        icon="auto_awesome",
                        on_click=lambda vv=v: _launch_ai_tag_suggestions(vv)
                    ).props("flat dense size=xs").classes(f"{ai_color} hover:text-primary").tooltip(ai_tip)

                    # Transcribe
                    has_trans = bool(getattr(v, "transcription_segments", None))
                    trans_color = "text-green-500" if has_trans else "text-zinc-500"
                    trans_tip = "Transcription ready (click to re-transcribe)" if has_trans else "Transcribe audio with AI"

                    ui.button(
                        icon="mic",
                        on_click=lambda vv=v: _launch_transcription(vv)
                    ).props("flat dense size=xs").classes(f"{trans_color} hover:text-primary").tooltip(trans_tip)

                    # Storyboard
                    has_sb = bool(getattr(v, "storyboard_path", None))
                    sb_color = "text-blue-400" if has_sb else "text-zinc-500"
                    sb_tip = "Storyboard ready (click to view)" if has_sb else "Generate / view storyboard"
                    ui.button(
                        icon="grid_view",
                        on_click=lambda vv=v: _show_storyboard_dialog(vv)
                    ).props("flat dense size=xs").classes(f"{sb_color} hover:text-primary").tooltip(sb_tip)

                    # AI Journalist Cut
                    ui.button(
                        icon="content_cut",
                        on_click=lambda vv=v: ui_dialogs.show_ai_journalist_cut_dialog(vv)
                    ).props("flat dense size=xs").classes("text-zinc-500 hover:text-primary")

            # Tag row - compact and removable
            tags = getattr(v, "tags", []) or []
            if tags:
                with ui.row().classes("w-full gap-1 flex-wrap q-my-xs"):
                    for tag in tags:
                        with ui.badge(color="blue-8").classes("cursor-pointer text-[10px]"):
                            ui.label(tag)
                            ui.icon("close", size="xs").classes("q-ml-xs").on("click", lambda t=tag: _remove_tag_from_video(v, t))

            # Secondary Metadata Row (compact)
            with ui.row().classes("w-full items-center gap-x-1.5 text-[10px] text-zinc-500"):
                ui.label(v.camera or "N/A").classes("uppercase tracking-wider")
                ui.label("•")
                ui.label(str(v.shoot_date) or "No Date")


def create_media_grid() -> None:
    """The central workspace - grid of video cards."""
    state = get_state()
    if state is None:
        return

    # Lazy import so we don't pull CLI deps at module load for every render
    try:
        from minicat.cli.main import _is_audio_file
    except Exception:
        def _is_audio_file(p: Path) -> bool:  # type: ignore
            return p.suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".aiff", ".aif"}

    # While an import (including AI tagging) is in progress, don't re-render the media grid.
    # This avoids trying to load storyboard/thumbnail images for videos whose files are still being written.
    if getattr(state, '_importing', False):
        with ui.element("div").classes("q-pa-md w-full text-center text-grey-6"):
            ui.label("Import in progress — media library will update when the import dialog is closed.").classes("text-sm")
        return

    with ui.element("div").classes("q-pa-md w-full"):
        _create_media_toolbar(state)

        # Always ensure the list is sorted right before rendering (makes sort extremely reliable)
        state.apply_sort()

        # Warn if we hit the safety limit (user's processed clips with thumbs/transcriptions may be "missing" from view)
        if len(getattr(state, '_raw_videos', [])) >= 1999:
            ui.label("⚠️ Showing limited results (max ~2000 clips). Use search/filters to bring your transcribed or previewed clips into view. Clear filters to see newest first.").classes("text-xs text-orange-400 mb-2")

        if not state.videos:
            ui.label("No clips match the current filters. Try clearing filters or importing footage.").classes("text-grey-6 mt-8")
            return

        # High-density cinematic media grid
        with ui.grid(columns=3).classes("gap-5 w-full"):
            for v in state.videos:
                render_media_card(v, state)

def create_media_list() -> None:
    """List view of clips with columns including camera sidecar XML technical metadata."""
    state = get_state()
    if state is None:
        return

    # While an import (including AI tagging) is in progress, don't re-render the media list.
    if getattr(state, '_importing', False):
        with ui.element("div").classes("q-pa-md w-full text-center text-grey-6"):
            ui.label("Import in progress — media library will update when the import dialog is closed.").classes("text-sm")
        return

    with ui.element("div").classes("q-pa-md w-full"):
        _create_media_toolbar(state)

        # Always ensure the list is sorted right before rendering (makes sort extremely reliable)
        state.apply_sort()

        # Warn if we hit the safety limit (user's processed clips with thumbs/transcriptions may be "missing" from view)
        if len(getattr(state, '_raw_videos', [])) >= 1999:
            ui.label("⚠️ Showing limited results (max ~2000 clips). Use search/filters to bring your transcribed or previewed clips into view. Clear filters to see newest first.").classes("text-xs text-orange-400 mb-2")

        if not state.videos:
            ui.label("No clips match the current filters. Try clearing filters or importing footage.").classes("text-grey-6 mt-8")
            return

        # Build clean table data (avoid storing complex objects to prevent serialization issues)
        rows = []
        for v in state.videos:
            date_str = str(v.shoot_date) if v.shoot_date else ""
            length_str = format_duration_timecode(v.duration, v.fps)
            # Only timecode style (00:00:00:00) in list view — no seconds

            tags_str = ", ".join(v.tags) if v.tags else ""

            is_sel = state.is_selected(v)
            rows.append({
                "id": v.id or 0,
                "selected": "☑" if is_sel else "☐",
                "filename": v.filename,
                "date": date_str or "—",
                "length": length_str,
                "codec": (v.codec or "").upper() or "—",
                "resolution": f"{v.width}×{v.height}" if v.width and v.height else "—",
                "camera": v.camera or "—",
                "operator": v.operator or "—",
                "lens": v.lens or "—",
                "location": v.location or "—",
                "tags": tags_str or "—",
                # New camera XML technical metadata columns
                "iso": str(v.iso) if v.iso else "—",
                "aperture": f"f/{v.f_number}" if v.f_number else "—",
                "shutter": v.shutter_speed or "—",
                "focal": f"{int(v.focal_length)}mm" if v.focal_length else "—",
                "wb": v.white_balance or "—",
                "gamma": v.gamma or "—",
                "color_primaries": v.color_primaries or "—",
                "coding_equations": v.coding_equations or "—",
                "tc_start": v.tc_start or "—",
                "tc_end": v.tc_end or "—",
            })

        # Build columns respecting user's custom order + visibility (from column customizer)
        base_columns = {c["name"]: c for c in _get_all_list_columns()}
        all_names = [c["name"] for c in _get_all_list_columns()]

        if not state.list_column_order:
            # First-time defaults
            default_order = DEFAULT_VISIBLE_LIST_COLUMNS.copy()
            for name in all_names:
                if name not in default_order:
                    default_order.append(name)
            state.list_column_order = default_order
            state.hidden_list_columns = set(all_names) - set(DEFAULT_VISIBLE_LIST_COLUMNS)

        desired_order = state.list_column_order or all_names

        columns = []
        for name in desired_order:
            if name in state.hidden_list_columns:
                continue
            if name in base_columns:
                columns.append(base_columns[name])

        # Simple, reliable table.
        # Per-row click-to-toggle is not supported in this NiceGUI version.
        # Selection is handled via the toolbar buttons above (Select All Visible + Unselect All).
        # The checkbox column below shows current selection state and updates on toolbar actions.
        ui.table(
            columns=columns,
            rows=rows,
            row_key="id",
            pagination=50,   # prevent huge tables from causing issues
        ).classes("w-full list-view-table")


def _render_welcome_screen() -> None:
    """Clean first-launch welcome screen.
    Dialogs are pre-created at render time so their context is stable.
    """
    with ui.column().classes("w-full h-screen items-center justify-center gap-8 bg-[#0f0f11]"):
        with ui.row().classes("items-center"):
            ui.label("Welcome to").classes("text-4xl font-bold")
            ui.html(
                '<span class="text-4xl font-bold text-white">CAT</span>'
                '<span class="text-4xl font-bold text-primary">+</span>'
                '<span class="text-4xl font-bold text-white">TAG</span>'
            )
        ui.label("Your personal video catalog — 100% local and private").classes("text-lg text-grey-5")

        with ui.card().classes("w-96 p-6"):
            ui.label("Where do you want to store your catalog?").classes("text-lg mb-4")

            # === Pre-create stable fallback dialogs (this fixes the slot error) ===
            with ui.dialog() as create_dialog, ui.card().classes("w-96"):
                folder_create = ui.input("New catalog folder path", value=str(Path.home() / "CAT+TAG"))
                def do_create():
                    p = Path(folder_create.value).expanduser()
                    p.mkdir(parents=True, exist_ok=True)
                    create_dialog.close()
                    _switch_to_catalog(p)
                ui.button("Create / Use Folder", on_click=do_create, color="primary").classes("w-full mt-2")

            with ui.dialog() as open_dialog, ui.card().classes("w-96"):
                folder_open = ui.input("Existing catalog folder path")
                def do_open():
                    p = Path(folder_open.value).expanduser()
                    if (p / "catalog.db").exists():
                        open_dialog.close()
                        _switch_to_catalog(p)
                    else:
                        ui.notify("No valid CAT+TAG catalog found in that folder", color="negative")
                ui.button("Open Catalog", on_click=do_open, color="primary").classes("w-full mt-2")

            # === Buttons ===
            def choose_and_create():
                # Prefer native macOS dialog
                try:
                    import webview
                    if webview.windows:
                        result = webview.windows[0].create_file_dialog(
                            webview.FileDialog.FOLDER,
                            directory=str(Path.home()),
                            allow_multiple=False,
                        )
                        if result:
                            chosen = Path(result[0])
                            chosen.mkdir(parents=True, exist_ok=True)
                            _switch_to_catalog(chosen)
                            return
                except Exception:
                    pass
                # Fallback to pre-created dialog (safe context)
                create_dialog.open()

            def choose_existing():
                try:
                    import webview
                    if webview.windows:
                        result = webview.windows[0].create_file_dialog(
                            webview.FileDialog.FOLDER,
                            directory=str(Path.home()),
                            allow_multiple=False,
                        )
                        if result:
                            chosen = Path(result[0])
                            if (chosen / "catalog.db").exists():
                                _switch_to_catalog(chosen)
                            else:
                                ui.notify("No valid CAT+TAG catalog in that folder", color="warning")
                            return
                except Exception:
                    pass
                open_dialog.open()

            with ui.column().classes("w-full gap-3"):
                ui.button("Create New Catalog Folder...", icon="create_new_folder", on_click=choose_and_create, color="primary").classes("w-full py-3 text-lg")
                ui.button("Open Existing Catalog...", icon="folder_open", on_click=choose_existing).classes("w-full py-3 text-lg")

        ui.label("Your footage never leaves this Mac").classes("text-xs text-grey-6 mt-4")


def _switch_to_catalog(catalog_path: Path) -> None:
    """User chose a catalog folder from the welcome screen.
    We initialize it, save the choice, and do a clean reload.
    On reload the page will detect the saved catalog and render the full app.
    """
    try:
        # Make sure the catalog exists on disk right now
        config.resolve_catalog(catalog_path)
        settings.set_last_catalog(catalog_path)
    except Exception as e:
        ui.notify(f"Failed to prepare catalog: {e}", color="negative")
        return

    ui.notify("Loading catalog...", color="positive")

    # Clean full reload — the next render will see the saved catalog
    ui.navigate.reload()


def trigger_import() -> None:
    """Import button handler.
    Very defensive version to guarantee the dialog opens and we see any errors.
    """
    try:
        state = get_state()

        if state is None:
            ui.notify("No catalog loaded", color="warning", duration=3)
            return

        # Get existing projects - prefer the rich projects table so that
        # projects created in the Project Details UI (even with 0 clips) are visible.
        try:
            all_rich_projects = db.get_all_projects(state.catalog_root)
            existing_projects = [p.name for p in all_rich_projects]
        except Exception:
            # Fallback to projects that have videos
            try:
                existing_projects = db.get_distinct_values(state.catalog_root, "project") or []
            except Exception:
                existing_projects = []

        # Create dialog as early as possible
        dialog = ui.dialog()
        with dialog, ui.card().classes("w-[620px]"):
            ui.label("Import Footage").classes("text-h5 mb-4")

            # Simple version for maximum stability
            ui.label("1. Client & Project").classes("font-bold mb-1")

            # Client selection (with support for no client)
            all_clients_for_import = db.get_clients(state.catalog_root)
            client_options = {None: "(No client)"}
            for c in all_clients_for_import:
                client_options[c.id] = c.name

            selected_client_id = ui.select(
                options=client_options,
                label="Client",
                value=None
            ).classes("w-full mb-2")

            def create_client_from_import():
                def do_create():
                    name = new_client_name.value.strip()
                    if not name:
                        ui.notify("Client name is required", color="warning")
                        return
                    new_c = Client(name=name)
                    saved = db.create_or_update_client(state.catalog_root, new_c)
                    ui.notify(f"Client '{saved.name}' created", color="positive")

                    # Live update the client select
                    client_options[saved.id] = saved.name
                    try:
                        selected_client_id.options = client_options
                        selected_client_id.value = saved.id
                        selected_client_id.update()
                    except Exception as ex:
                        print(f"[Import] Failed to live-update client select: {ex}")
                        try:
                            dialog.close()
                            ui.timer(0.05, lambda: trigger_import(), once=True)
                        except Exception:
                            pass

                    client_dialog.close()

                with ui.dialog() as client_dialog, ui.card().classes("w-[420px] q-pa-md"):
                    ui.label("New client").classes("text-h6 mb-4")
                    new_client_name = ui.input("Name *").props("autofocus dense").classes("w-full")

                    with ui.row().classes("justify-end gap-2 mt-6 w-full"):
                        ui.button("Cancel", on_click=client_dialog.close).props("flat")
                        ui.button("Create client", on_click=do_create, color="primary")
                client_dialog.open()

            ui.button("Create New Client", icon="add", on_click=create_client_from_import).props("size=sm flat dense").classes("mb-2")

            # Project mode - restored to original radio style the user had before
            project_mode = ui.radio(["New Project", "Existing Project"], value="New Project").props("inline")

            project_name = ui.input("Project Name").classes("w-full mb-2")

            project_select = None
            if existing_projects:
                project_select = ui.select(
                    options=existing_projects,
                    label="Select Existing Project",
                    value=existing_projects[0] if existing_projects else None,
                ).classes("w-full mb-2")
                project_select.visible = False

                def on_mode_change(e):
                    is_new = e.value == "New Project"
                    project_name.visible = is_new
                    if project_select:
                        project_select.visible = not is_new
                    # Ensure NiceGUI actually updates the visibility
                    try:
                        project_name.update()
                        if project_select:
                            project_select.update()
                    except Exception:
                        pass

                project_mode.on_value_change(on_mode_change)

            # Source folder
            ui.label("2. Source Folder").classes("font-bold mb-1 mt-2")
            folder_label = ui.label("No folder selected").classes("text-grey-6 mb-2")

            def pick_folder():
                try:
                    import webview
                    win = webview.active_window() or (webview.windows[0] if webview.windows else None)
                    if win:
                        res = win.create_file_dialog(webview.FileDialog.FOLDER, allow_multiple=False)
                        if res:
                            folder_label.text = res[0] if isinstance(res, (list, tuple)) else str(res)
                            folder_label.classes(remove="text-grey-6", add="text-positive")
                            return
                    # Fallback to manual input
                    ui.notify("Native folder picker not available — enter path manually below", color="warning")
                except Exception as ex:
                    ui.notify(f"Picker error: {ex} — enter path manually", color="warning")

            ui.button("Choose Source Folder...", on_click=pick_folder, color="primary").classes("mb-2")

            # Manual path fallback (useful when native picker fails or isn't available)
            manual_folder = ui.input("Or type/paste folder path here", placeholder="/path/to/footage/folder").classes("w-full")
            def use_manual():
                if manual_folder.value.strip():
                    folder_label.text = manual_folder.value.strip()
                    folder_label.classes(remove="text-grey-6", add="text-positive")
            ui.button("Use typed path", on_click=use_manual).props("flat size=sm").classes("mb-4")

            # Timecode
            burn_timecode = ui.checkbox("Burn timecode at bottom", value=False)
            timecode_start = ui.input("Starting timecode", value="00:00:00:00").classes("w-full")
            burn_timecode.visible = False
            timecode_start.visible = False

            def toggle_tc(e):
                timecode_start.visible = e.value
                try:
                    timecode_start.update()
                except Exception:
                    pass
            burn_timecode.on_value_change(toggle_tc)

            # AI Auto-tagging option
            ai_auto_tag = ui.checkbox(
                "Use AI to automatically suggest tags for imported clips",
                value=False
            ).classes("mt-4")

            # Transcription option (default OFF as requested)
            auto_transcribe = ui.checkbox(
                "Transcribe imported clips with AI",
                value=False
            ).classes("mt-1")

            async def start_import():
                try:
                    if project_mode.value == "New Project":
                        proj = project_name.value.strip()
                        if not proj:
                            ui.notify("Enter project name", color="negative")
                            return

                        # Prevent duplicate project names (the main cause of the UNIQUE constraint error)
                        if db.get_project(state.catalog_root, proj):
                            ui.notify(f"A project named '{proj}' already exists. Please select it from the Existing Projects list or choose a different name.", color="negative")
                            return

                        # Enforce client for new projects
                        selected_cid = selected_client_id.value
                        if not selected_cid:
                            ui.notify("Please select a Client for the new project", color="negative")
                            return

                        # Create project + assign client
                        new_proj = Project(name=proj)
                        db.create_or_update_project(state.catalog_root, new_proj)
                        db.set_project_clients(state.catalog_root, proj, [selected_cid])

                    else:
                        # Existing Project
                        proj = project_select.value if project_select else None
                        if not proj:
                            ui.notify("Select a project", color="negative")
                            return

                        # If a client was selected, add it to the existing project (additive)
                        selected_cid = selected_client_id.value
                        if selected_cid:
                            try:
                                current = [c.id for c in db.get_clients_for_project(state.catalog_root, proj)]
                                if selected_cid not in current:
                                    current.append(selected_cid)
                                    db.set_project_clients(state.catalog_root, proj, current)
                            except Exception as ex:
                                print(f"[Import] Failed to add client to existing project: {ex}")

                    folder = folder_label.text.strip()
                    if not folder or folder == "No folder selected":
                        ui.notify("Choose a source folder", color="negative")
                        return

                    dialog.close()

                    # Create progress dialog
                    progress_dialog = ui.dialog()
                    with progress_dialog, ui.card().classes("w-[560px]"):
                        import_title = ui.label("Importing Media + Metadata").classes("text-h5 mb-2")

                        with ui.column().classes("w-full gap-1"):
                            # Percent label on top of the bar (integer, no decimal)
                            percent_label = ui.label("0%").classes("text-sm tabular-nums self-end mb-1")

                            # Progress bar (no built-in value to avoid 0.222222222 float display)
                            progress_bar = ui.linear_progress(value=0, show_value=False).classes("w-full")

                            status_label = ui.label("Preparing import...").classes("text-sm mt-1")
                            file_label = ui.label("").classes("text-xs text-grey-6")

                        summary_label = ui.label("").classes("text-sm mt-4")

                        close_btn = ui.button("Close", on_click=progress_dialog.close).props("flat").classes("mt-4 w-full")
                        close_btn.visible = False

                        cancel_btn = ui.button("Cancel Import", on_click=lambda: _request_cancel(), color="negative").props("flat").classes("mt-2 w-full")

                        def _request_cancel():
                            state._import_cancel_requested = True
                            if status_label:
                                status_label.text = "Cancel requested — finishing current file/operation..."
                            ui.update(status_label)
                            try:
                                cancel_btn.disable()
                            except Exception:
                                pass

                        def _cleanup_after_import():
                            # Re-enable normal rendering of the media library and do a final refresh
                            if hasattr(state, '_importing'):
                                state._importing = False
                            if hasattr(state, '_import_cancel_requested'):
                                state._import_cancel_requested = False
                            try:
                                state.reload()
                                refresh_all_ui(state)
                            except Exception:
                                pass

                        progress_dialog.on('hide', _cleanup_after_import)

                    progress_dialog.open()
                    await asyncio.sleep(0.08)  # let the dialog paint before heavy work begins

                    # Freeze the main media grid/list during the entire import + AI tagging process.
                    # This prevents the grid from trying to render the new videos (and their images)
                    # while previews are still being written and tagging is running, which was causing 404s.
                    state._importing = True
                    state._import_cancel_requested = False

                    await do_wizard_import(
                        state=state,
                        folder_path=folder,
                        project_name=proj,
                        generate_proxies=False,
                        burn_timecode=burn_timecode.value,
                        timecode_start=timecode_start.value,
                        ai_auto_tag=ai_auto_tag.value,
                        auto_transcribe=auto_transcribe.value,
                        progress_bar=progress_bar,
                        status_label=status_label,
                        file_label=file_label,
                        percent_label=percent_label,
                        summary_label=summary_label,
                        close_btn=close_btn,
                        import_title=import_title,
                        cancel_btn=cancel_btn,
                    )

                except Exception as ex:
                    ui.notify(f"Start import error: {ex}", color="negative")
                    print(f"[Wizard] start_import error: {ex}")

            with ui.row().classes("justify-end w-full mt-6"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Start Import", on_click=start_import, color="primary")

        dialog.open()
        pass  # removed noisy debug print

    except Exception as ex:
        ui.notify(f"Could not open Import Wizard: {ex}", color="negative", duration=8)
        print(f"[Import] top-level error: {ex}")  # keep for serious errors only
        import traceback
        traceback.print_exc()


async def do_wizard_import(
    state: AppState,
    folder_path: str,
    project_name: str,
    generate_proxies: bool,
    burn_timecode: bool = False,
    timecode_start: str = "00:00:00:00",
    ai_auto_tag: bool = False,
    auto_transcribe: bool = False,
    # Progress UI elements (optional)
    progress_bar=None,
    status_label=None,
    file_label=None,
    percent_label=None,
    summary_label=None,
    close_btn=None,
    import_title=None,   # so we can update the dialog title when AI phase starts
    cancel_btn=None,
):
    """Main import logic with optional proxy generation and live progress UI."""
    try:
        from minicat.cli.main import _is_audio_file, _is_supported_file, _is_video_file
        from minicat.core.video import find_ffmpeg, get_auto_import_tags

        # Friendly pre-flight check for ffmpeg (the #1 support issue for new users)
        try:
            find_ffmpeg()
        except RuntimeError:
            show_ffmpeg_required_dialog()
            if status_label:
                status_label.text = "ffmpeg is required for import"
            if summary_label:
                summary_label.text = "Please install ffmpeg, then try again."
            ui.update(status_label, summary_label)
            if close_btn:
                close_btn.enable()
            return

        # Give immediate feedback while we may be scanning a large tree
        if status_label:
            status_label.text = "Scanning source folder for media files (video + audio)..."
        if file_label:
            file_label.text = folder_path
        if percent_label:
            percent_label.text = "..."
        ui.update(*[w for w in (status_label, file_label, percent_label, cancel_btn) if w])
        await asyncio.sleep(0.03)

        p = Path(folder_path).expanduser().resolve()
        if not p.exists():
            ui.notify("Folder no longer exists", color="negative")
            return

        all_files = [f for f in p.rglob("*") if f.is_file()]
        supported_files = [f for f in all_files if _is_supported_file(f)]

        if not supported_files:
            ui.notify("No supported media files found (video or audio)", color="warning")
            return

        total = len(supported_files)

        added = 0
        skipped = 0
        proxy_errors = 0
        errors = []
        newly_added_ids: list[int] = []  # collect IDs; fetch fresh Video objs (with id+storyboard) after previews

        proxy_base = state.catalog_root / "proxies" / project_name
        proxy_base.mkdir(parents=True, exist_ok=True)

        # === IMMEDIATE INITIAL PROGRESS (so the bar is visible right away) ===
        if progress_bar:
            progress_bar.value = 0.0
        if status_label:
            status_label.text = f"Found {total} media files (video + audio) — starting import..."
        if file_label:
            file_label.text = ""
        if percent_label:
            percent_label.text = "0%"
        ui.update(*[w for w in (progress_bar, status_label, file_label, percent_label, cancel_btn) if w])
        await asyncio.sleep(0.06)

        for index, f in enumerate(supported_files):
            current = index + 1
            percent = current / total
            pct_text = f"{int(percent * 100)}%"

            # Update UI *before* starting heavy work on this file (bar moves early)
            if progress_bar:
                progress_bar.value = percent
            if status_label:
                status_label.text = f"Processing {current} / {total}"
            if file_label:
                file_label.text = f.name
            if percent_label:
                percent_label.text = pct_text
            ui.update(*[w for w in (progress_bar, status_label, file_label, percent_label, cancel_btn) if w])
            await asyncio.sleep(0.025)   # give the client time to render the update

            # Check for user cancel request (from the Cancel button in progress dialog)
            if getattr(state, '_import_cancel_requested', False):
                if status_label:
                    status_label.text = "Import canceled by user."
                ui.update(status_label)
                break

            if db.get_video_by_path(state.catalog_root, f):
                skipped += 1
                # still advance the visual progress for skipped items
                continue

            try:
                # --- Metadata (ffprobe + sidecar XML + optional ExifTool) ---
                if status_label:
                    status_label.text = f"Processing {current} / {total} — Extracting metadata (XML + camera info)"
                ui.update(status_label)
                await asyncio.sleep(0.015)

                meta = await asyncio.to_thread(video.extract_metadata, f)
                fps = meta.get("fps")
                tc_start = meta.get("tc_start")

                if tc_start:
                    if fps and float(fps) > 0:
                        try:
                            fps_clean = float(fps)
                            fps_str = f"{fps_clean:g}"
                        except Exception:
                            fps_str = str(fps)
                        fps_part = f" ({fps_str} fps)"
                    else:
                        fps_part = ""
                    if status_label:
                        status_label.text = f"Processing {current} / {total} — Real embedded TC: {tc_start}{fps_part}"
                    ui.update(status_label)
                    await asyncio.sleep(0.005)
                else:
                    if fps and float(fps) > 0:
                        if status_label:
                            status_label.text = f"Processing {current} / {total} — Framerate confirmed at import: {fps} fps (immutable)"
                        ui.update(status_label)
                        await asyncio.sleep(0.01)
                    else:
                        if status_label:
                            status_label.text = f"Processing {current} / {total} — WARNING: could not confirm framerate"
                        ui.update(status_label)

                # Enrich with camera sidecar XML / XMP / RMD + ExifTool fallback (import is the right time to pay this cost)
                xml_meta = await asyncio.to_thread(
                    video.extract_camera_xml_metadata, f, enrich_with_exiftool=True
                )
                if xml_meta:
                    if xml_meta.get("camera"):
                        meta["camera"] = xml_meta["camera"]
                    if xml_meta.get("shoot_date"):
                        meta["creation_date"] = xml_meta["shoot_date"]
                    # Codec from sidecar is often more accurate/descriptive for Sony cameras
                    if xml_meta.get("codec"):
                        meta["codec"] = xml_meta["codec"]

                v = Video(
                    path=str(f),
                    filename=f.name,
                    size=meta.get("size"),
                    duration=meta.get("duration"),
                    width=meta.get("width"),
                    height=meta.get("height"),
                    fps=fps,
                    codec=meta.get("codec"),
                    bit_rate=meta.get("bit_rate"),
                    audio_channels=meta.get("audio_channels"),
                    project=project_name,
                    camera=meta.get("camera"),
                    operator=None,
                    lens=xml_meta.get("lens") if xml_meta else None,
                    shoot_date=meta.get("creation_date") if isinstance(meta.get("creation_date"), date) else None,
                    camera_xml_path=xml_meta.get("source_xml") if xml_meta else None,
                    iso=xml_meta.get("iso") if xml_meta else None,
                    f_number=xml_meta.get("f_number") if xml_meta else None,
                    shutter_speed=xml_meta.get("shutter_speed") if xml_meta else None,
                    focal_length=xml_meta.get("focal_length") if xml_meta else None,
                    white_balance=xml_meta.get("white_balance") if xml_meta else None,
                    gamma=xml_meta.get("gamma") if xml_meta else None,
                    color_primaries=xml_meta.get("color_primaries") if xml_meta else None,
                    coding_equations=xml_meta.get("coding_equations") if xml_meta else None,
                    tc_start=meta.get("tc_start"),
                    tc_end=meta.get("tc_end"),
                )
                v.fingerprint = await asyncio.to_thread(video.fast_fingerprint, f, duration=v.duration)
                video_id = db.add_video(state.catalog_root, v)
                added += 1
                newly_added_ids.append(video_id)

                is_audio = _is_audio_file(f)

                # Automatic resolution / type tags on import (user request)
                try:
                    auto_tags = get_auto_import_tags(meta, is_audio=is_audio)
                    if auto_tags:
                        db.set_video_tags(state.catalog_root, video_id, auto_tags)
                except Exception as tag_err:
                    print(f"[Import] Auto tag failed for {f.name}: {tag_err}")

                # --- Previews (the heaviest step — thumbnail + storyboard via ffmpeg)
                #     Skipped for audio files; they use a generic waveform icon instead.
                if not is_audio:
                    if status_label:
                        status_label.text = f"Processing {current} / {total} — Generating previews"
                    ui.update(status_label)
                    await asyncio.sleep(0.015)

                    try:
                        thumb_path, board_path = await asyncio.to_thread(
                            video.generate_previews, f, state.catalog_root, video_id
                        )
                        db.update_video_fields(
                            state.catalog_root,
                            video_id,
                            thumbnail_path=str(thumb_path),
                            storyboard_path=str(board_path),
                        )
                    except Exception as prev_err:
                        print(f"[Previews] Failed for {f.name}: {prev_err}")
                else:
                    # Audio: no video thumbnails/storyboards. Grid will render waveform icon.
                    if status_label:
                        status_label.text = f"Processing {current} / {total} — Audio imported (waveform icon)"
                    ui.update(status_label)

                # --- Proxy (only if enabled; currently disabled in wizard) ---
                if generate_proxies:
                    if status_label:
                        status_label.text = f"Processing {current} / {total} — Creating proxy"
                    ui.update(status_label)
                    await asyncio.sleep(0.015)

                    try:
                        # Note: when re-enabling, pick the correct extension per preset
                        proxy_name = f.stem + "_proxy.mp4"
                        proxy_out = proxy_base / proxy_name
                        await asyncio.to_thread(
                            video.create_proxy,
                            source_path=f,
                            output_path=proxy_out,
                            preset="H.264 \"Performance\" Proxy (720p)",
                            burn_text=True,
                            text="CAT+TAG-Proxy",
                            burn_timecode=burn_timecode,
                            timecode_start=timecode_start,
                        )
                        print(f"[Proxy] Created: {proxy_out}")
                    except Exception as proxy_err:
                        proxy_errors += 1
                        print(f"[Proxy] Failed for {f.name}: {proxy_err}")

            except Exception as err:
                errors.append(f"{f.name}: {err}")

            # End-of-file progress nudge (in case earlier updates were missed)
            if progress_bar:
                progress_bar.value = percent
            if status_label:
                status_label.text = f"Processed {current} / {total}"
            if percent_label:
                percent_label.text = pct_text
            ui.update(*[w for w in (progress_bar, status_label, file_label, percent_label, cancel_btn) if w])
            await asyncio.sleep(0.02)

        # Final UI update
        if progress_bar:
            progress_bar.value = 1.0
        if status_label:
            status_label.text = "Import finished"
        if file_label:
            file_label.text = ""
        if percent_label:
            percent_label.text = "100%"
        ui.update(*[w for w in (progress_bar, status_label, file_label, percent_label, cancel_btn) if w])

        # Clean up transient progress widgets so the dialog ends in a nice "summary only" state
        for w in (progress_bar, percent_label, status_label, file_label, cancel_btn):
            if w:
                w.visible = False
        ui.update(*[w for w in (progress_bar, percent_label, status_label, file_label, cancel_btn) if w])

        # Always give complete accounting so user knows exactly what happened to every file
        added + skipped + len(errors)
        if summary_label:
            msg = f"Import finished: {total} files found\n"
            msg += f"{added} newly imported • {skipped} already in catalog • {len(errors)} failed"
            if proxy_errors:
                msg += f" • {proxy_errors} proxy errors"
            if errors:
                msg += "\n\nFirst failures:\n" + "\n".join(errors[:5])
                if len(errors) > 5:
                    msg += f"\n... + {len(errors)-5} more (see console for full list)"
            summary_label.text = msg
            # Make multi-line summary readable
            summary_label.classes(add="whitespace-pre-line")
            ui.update(summary_label)

        # Only show Close button here if we are NOT doing AI tagging.
        # When AI auto-tagging is active, the tagging task itself will show the button
        # only after it has completely finished.
        # When AI auto-tagging was requested for the imported files, we deliberately
        # keep the Close button hidden here. The tagging task will reveal it only
        # after it has completely finished.
        if close_btn and not (ai_auto_tag and newly_added_ids):
            close_btn.visible = True
            ui.update(close_btn)
            if cancel_btn:
                cancel_btn.visible = False
                ui.update(cancel_btn)

        # Check cancel before starting AI phases
        if getattr(state, '_import_cancel_requested', False):
            if status_label:
                status_label.text = "Import canceled by user (AI phases skipped)."
            if close_btn:
                close_btn.visible = True
                ui.update(close_btn)
            if cancel_btn:
                cancel_btn.visible = False
            # skip AI and transcription
            return

        # === AI Auto-tagging after import (if requested) ===
        # We re-fetch fresh Video objects here so they have .id and .storyboard_path populated
        # (previews run inside the loop before we reach this point).
        pass  # removed verbose import AI debug print

        if ai_auto_tag and newly_added_ids:
            api_key = get_gemini_api_key()
            if api_key:
                try:
                    fresh_videos = db.get_videos_by_ids(state.catalog_root, newly_added_ids)
                    ready = [vv for vv in fresh_videos if vv.storyboard_path]
                    if ready:
                        # Create progress tracker and keep the Close button hidden
                        # until BOTH file import + AI tagging are 100% complete.
                        tagging_progress = AITaggingProgress(total=len(ready))

                        if close_btn:
                            close_btn.visible = False
                            ui.update(close_btn)

                        pass  # removed noisy launch print
                        # Launch the background tagging worker (it must never touch NiceGUI UI directly)
                        asyncio.create_task(
                            _auto_ai_tag_clips(
                                state,
                                ready,
                                api_key,
                                progress=tagging_progress,
                            )
                        )

                        # Make the AI tagging phase clearly visible by updating the dialog title
                        if import_title:
                            import_title.text = "Importing Footage + AI Tagging"
                            ui.update(import_title)

                        # Initial status in the dialog
                        if status_label:
                            status_label.text = f"AI Tagging: 0/{len(ready)} clips..."
                            ui.update(status_label)

                        if summary_label:
                            current = summary_label.text or ""
                            summary_label.text = current + f"\n\n[AI Phase] Auto-tagging {len(ready)} new clips with Gemini..."
                            ui.update(summary_label)

                        ui.notify("AI auto-tagging started (dialog stays open until tagging finishes)", color="info", duration=5)

                        # === Safe polling timer (created in the main NiceGUI context) ===
                        # This timer runs in the import dialog's context and is allowed to create/update UI.
                        def _poll_ai_tagging_progress():
                            if getattr(state, "_import_cancel_requested", False):
                                if status_label:
                                    status_label.text = "Import canceled during AI tagging."
                                try:
                                    poll_timer.active = False
                                except Exception:
                                    pass
                                return
                            if not tagging_progress:
                                return

                            # Live update while tagging is running
                            if status_label and not tagging_progress.is_complete:
                                status_label.text = (
                                    f"AI Tagging: {tagging_progress.completed}/{tagging_progress.total} clips • "
                                    f"{tagging_progress.total_tags_added} tags written so far..."
                                )
                                ui.update(status_label)

                            # When the background task signals completion, do the final UI work and allow closing
                            if tagging_progress.is_complete:
                                try:
                                    # Final status
                                    final_msg = (
                                        f"AI tagging complete: {tagging_progress.completed}/{tagging_progress.total} "
                                        f"videos tagged ({tagging_progress.total_tags_added} tags total written)"
                                    )
                                    if status_label:
                                        status_label.text = final_msg
                                        ui.update(status_label)

                                    if summary_label:
                                        current = summary_label.text or ""
                                        summary_label.text = current + "\n\n" + final_msg
                                        summary_label.classes(add="whitespace-pre-line")
                                        ui.update(summary_label)

                                    # Finally allow the user to close the import window
                                    if close_btn:
                                        close_btn.visible = True
                                        ui.update(close_btn)
                                    if cancel_btn:
                                        cancel_btn.visible = False
                                        ui.update(cancel_btn)

                                    # One last global refresh
                                    state._importing = False
                                    state.reload()
                                    refresh_all_ui(state)

                                    # Toast
                                    if tagging_progress.completed > 0:
                                        ui.notify(
                                            f"AI tagging finished — {tagging_progress.completed} clips tagged "
                                            f"({tagging_progress.total_tags_added} tags total)",
                                            color="positive", duration=8
                                        )
                                    else:
                                        ui.notify("AI auto-tagging finished (no new tags added).", color="warning", duration=6)

                                except Exception as poll_final_err:
                                    print(f"[Import] AI poll error: {poll_final_err}")  # keep for errors
                                    if close_btn:
                                        close_btn.visible = True
                                        ui.update(close_btn)
                                    if cancel_btn:
                                        cancel_btn.visible = False
                                        ui.update(cancel_btn)

                                # Stop polling
                                try:
                                    poll_timer.active = False
                                except Exception:
                                    pass

                        poll_timer = ui.timer(0.25, _poll_ai_tagging_progress)  # created in main context → safe
                    else:
                        skip_msg = "(AI auto-tagging skipped — storyboards not ready)"
                        if summary_label:
                            current = summary_label.text or ""
                            summary_label.text = current + "\n\n" + skip_msg
                            ui.update(summary_label)
                        if close_btn:
                            close_btn.visible = True
                            ui.update(close_btn)
                        if cancel_btn:
                            cancel_btn.visible = False
                            ui.update(cancel_btn)
                except Exception as fetch_err:
                    print(f"[AI Auto-Tag] Could not prepare videos for background tagging: {fetch_err}")
                    if close_btn:
                        close_btn.visible = True
                        ui.update(close_btn)
                    if cancel_btn:
                        cancel_btn.visible = False
                        ui.update(cancel_btn)

            else:
                skip_msg = "AI auto-tagging skipped — no Gemini API key configured in Settings"
                if status_label:
                    status_label.text = skip_msg
                    ui.update(status_label)
                if summary_label:
                    current = summary_label.text or ""
                    summary_label.text = current + "\n\n(Note: " + skip_msg + ")"
                    ui.update(summary_label)
                ui.notify("AI auto-tagging skipped: set your Gemini key in Settings first", color="warning", duration=6)
                if close_btn:
                    close_btn.visible = True
                    ui.update(close_btn)
                if cancel_btn:
                    cancel_btn.visible = False
                    ui.update(cancel_btn)

        # Make absolutely sure the final summary + status (including any AI note) is pushed to the UI
        if summary_label:
            ui.update(summary_label)
        if status_label:
            ui.update(status_label)

        # === AI Transcription after import (if requested) ===
        if auto_transcribe and newly_added_ids:
            try:
                fresh_videos = db.get_videos_by_ids(state.catalog_root, newly_added_ids)
                queued_count = 0
                for v in fresh_videos:
                    if v.id:
                        # Queue transcription (no auto-translate by default in wizard)
                        queue_for_transcription(v, do_translate=False)
                        queued_count += 1

                if queued_count > 0:
                    if status_label:
                        status_label.text = f"Queued AI transcription for {queued_count} new clips..."
                        ui.update(status_label)
                    if summary_label:
                        current = summary_label.text or ""
                        summary_label.text = current + f"\n\n[Transcription] Queued {queued_count} clips for AI transcription (check inspector for progress)."
                        summary_label.classes(add="whitespace-pre-line")
                        ui.update(summary_label)
                    ui.notify(f"AI transcription queued for {queued_count} imported clips", color="info", duration=5)
            except Exception as trans_ex:
                print(f"[Import] Transcription queuing failed: {trans_ex}")

        # Give the client a moment to render the final summary + AI status before the function ends
        await asyncio.sleep(0.08)

        # Log full details to console for debugging
        if errors:
            # Failed files list (removed verbose print for performance; errors are notified individually)
            for e in errors:
                print("  -", e)

    except Exception as e:
        ui.notify(f"Import failed: {e}", color="negative")
        print(f"[Import] top level error: {e}")
        import traceback
        traceback.print_exc()
        if summary_label:
            summary_label.text = f"Import failed hard: {e}\n\nCheck console for traceback."
            summary_label.classes(add="whitespace-pre-line")
        if percent_label:
            percent_label.text = "ERR"
        if close_btn:
            close_btn.visible = True
        if cancel_btn:
            cancel_btn.visible = False
            ui.update(cancel_btn)

        # Make sure we don't leave the main grid frozen on error
        if hasattr(state, '_importing'):
            state._importing = False


# ---------------------------------------------------------------------------
# Layer 2: Background Workers - Delegated to core/workers.py
# ---------------------------------------------------------------------------

# Re-export for backward compatibility during transition
TranscriptionJob = task_workers.TranscriptionJob
AITaggingProgress = task_workers.AITaggingProgress
NarrationVOProgress = task_workers.NarrationVOProgress
TRANSCRIPTION_JOBS = task_workers.TRANSCRIPTION_JOBS

# Thin adapters so existing calls in app.py continue to work
def queue_for_transcription(clip: Video, do_translate: bool = False):
    """Compatibility wrapper around the new task_workers module."""
    success = task_workers.queue_transcription(
        clip_id=clip.id,
        filename=clip.filename,
        do_translate=do_translate,
    )
    if success:
        kind = "transcription + translation" if do_translate else "transcription"
        ui.notify(f"Queued for {kind}. Check bottom bar for progress.", color="positive", duration=4)
    else:
        ui.notify(f"{clip.filename} is already in the queue", color="info")


def _launch_transcription(clip: Video, do_translate: bool = False) -> None:
    queue_for_transcription(clip, do_translate=do_translate)


# Register the footer status updater with the new worker system
def _register_transcription_status_updater(updater: Any) -> None:
    """Called from the footer to let the worker system drive UI updates."""
    task_workers.register_status_updater(updater)


# Note: The old heavy _transcription_worker, _transcribe_video, etc. have been
# moved to minicat/core/workers.py. The new implementation is cleaner and
# pushes UI concerns out of the worker layer.


def _launch_ai_tag_suggestions(video: Video) -> None:
    """Safe launcher for AI tag suggestions.

    Prefers storyboard (vision) when available.
    Falls back to transcription (text) for audio files or clips without storyboards.
    """
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
            await _ask_ai_suggestions_work(video, api_key)
        finally:
            try:
                loading.close()
            except Exception:
                pass

    asyncio.create_task(run())


async def _ask_ai_suggestions_work(video: Video, api_key: str) -> None:
    """Pure work function for AI tag suggestions (no UI element creation)."""
    has_storyboard = bool(getattr(video, "storyboard_path", None))
    has_transcript = bool(getattr(video, "transcription_segments", None))

    try:
        if has_storyboard:
            # Preferred path for video: visual analysis of storyboard
            suggestions = await asyncio.to_thread(
                suggest_tags_from_storyboard,
                video.storyboard_path,
                api_key,
                min_tags=3,
                max_tags=8,
                model_name=get_gemini_model(),
            )
        elif has_transcript:
            # Fallback for audio files (and video without storyboard)
            suggestions = await asyncio.to_thread(
                suggest_tags_from_transcript,
                video.transcription_segments,
                api_key,
                min_tags=3,
                max_tags=8,
                model_name=get_gemini_model(),
            )
        else:
            suggestions = []
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg and "no longer available" in error_msg.lower():
            error_msg = "Selected Gemini model is outdated. Please choose a current model in Settings."
        ui.notify(f"AI failed: {error_msg}", color="negative", multi_line=True)
        return

    if not suggestions:
        ui.notify("No suggestions from AI", color="warning")
        return

    # Reuse the rich multi-select review dialog (safe to call from here)
    _show_rich_ai_tag_review_dialog(video, suggestions)


# ---------------------------------------------------------------------------
# AI Auto-tagging for Import Wizard (background worker)
# ---------------------------------------------------------------------------

async def _auto_ai_tag_clips(state, videos: list, api_key: str, progress: "AITaggingProgress"):
    """Background worker for auto-tagging newly imported clips during the Import wizard.
    Must NEVER create or directly touch NiceGUI UI elements (use the progress object instead).
    """
    try:
        for video in videos:
            try:
                suggestions: list[str] = []

                if getattr(video, "storyboard_path", None):
                    suggestions = await asyncio.to_thread(
                        suggest_tags_from_storyboard,
                        video.storyboard_path,
                        api_key,
                        min_tags=3,
                        max_tags=8,
                        model_name=get_gemini_model(),
                    )
                elif getattr(video, "transcription_segments", None):
                    suggestions = await asyncio.to_thread(
                        suggest_tags_from_transcript,
                        video.transcription_segments,
                        api_key,
                        min_tags=3,
                        max_tags=8,
                        model_name=get_gemini_model(),
                    )

                if suggestions:
                    current_tags = getattr(video, "tags", None) or []
                    # Deduplicate while preserving order + normalize
                    new_tags = [t.lower().strip() for t in suggestions if t and t.strip()]
                    combined = list(dict.fromkeys(current_tags + new_tags))
                    if combined != current_tags:
                        db.set_video_tags(state.catalog_root, video.id, combined)
                        newly_added = len(combined) - len(current_tags)
                        progress.total_tags_added += max(0, newly_added)

                progress.completed += 1

            except Exception as per_clip_err:
                print(f"[AI Auto-Tag] Failed on {getattr(video, 'filename', 'unknown')}: {per_clip_err}")
                progress.errors += 1
                progress.completed += 1

    finally:
        progress.is_complete = True


def _show_rich_ai_tag_review_dialog(video: Video, suggestions: list[str]):
    """Shared rich review dialog used by Grid cards, storyboard viewer, etc."""
    with ui.dialog() as sug_dialog, ui.card().classes("w-[520px]"):
        ui.label("AI Suggested Tags").classes("text-h6 mb-2")
        ui.label("Review, edit, and select the tags you want to add").classes("text-xs text-grey-6 mb-3")

        tag_items = [{"text": tag, "selected": True} for tag in suggestions]

        def get_selected_count():
            return sum(1 for item in tag_items if item.get("selected") and item.get("text"))

        items_container = ui.column().classes("w-full gap-2 mb-2")

        def refresh_items():
            items_container.clear()
            with items_container:
                for idx, item in enumerate(tag_items):
                    text = item.get("text") or ""
                    with ui.row().classes("items-center gap-2 w-full py-0.5"):
                        cb = ui.checkbox(
                            text or "(empty tag)",
                            value=bool(item.get("selected"))
                        ).props("dense")

                        def make_cb_handler(i=idx):
                            def handler(e):
                                tag_items[i]["selected"] = bool(getattr(e, "value", False))
                                update_add_button()
                            return handler
                        cb.on_value_change(make_cb_handler())

                        def edit_item(i=idx):
                            async def do_edit():
                                try:
                                    new_val = await ui.input_dialog("Edit tag text", value=tag_items[i].get("text", ""))
                                    if new_val is not None:
                                        cleaned = (new_val or "").strip().lower()
                                        if cleaned:
                                            tag_items[i]["text"] = cleaned
                                            refresh_items()
                                            update_add_button()
                                except Exception as ex:
                                    ui.notify(f"Could not edit: {ex}", color="negative")
                            asyncio.create_task(do_edit())

                        ui.button(icon="edit", on_click=edit_item).props("flat dense size=sm color=grey-7")

                        def delete_item(i=idx):
                            if 0 <= i < len(tag_items):
                                del tag_items[i]
                                refresh_items()
                                update_add_button()
                        ui.button(icon="delete", on_click=delete_item).props("flat dense size=sm color=grey-7")

                with ui.row().classes("items-center gap-2 w-full mt-2 pt-2 border-t border-grey-8"):
                    custom_input = ui.input(placeholder="Add your own tag...").props("dense").classes("flex-1")
                    def add_custom():
                        val = (custom_input.value or "").strip().lower()
                        if val:
                            tag_items.append({"text": val, "selected": True})
                            custom_input.value = ""
                            refresh_items()
                            update_add_button()
                    ui.button("Add", on_click=add_custom, color="primary").props("dense size=sm")

        refresh_items()

        button_row = ui.row().classes("justify-end gap-2 mt-2 w-full")

        def update_add_button():
            button_row.clear()
            with button_row:
                ui.button("Cancel", on_click=sug_dialog.close).props("flat")
                count = get_selected_count()
                add_btn = ui.button(f"Add Selected ({count})", on_click=apply_tags, color="primary")
                if count == 0:
                    add_btn.props("disable")

        def apply_tags():
            if not video.id:
                sug_dialog.close()
                return
            selected = [item["text"] for item in tag_items if item.get("selected") and item.get("text")]
            if not selected:
                sug_dialog.close()
                return
            current_state = get_state()
            if current_state:
                current = set(getattr(video, 'tags', None) or [])
                new_tags = current | set(selected)
                db.set_video_tags(current_state.catalog_root, video.id, list(new_tags))

                fresh = db.get_video_by_path(current_state.catalog_root, video.path)
                if fresh:
                    video.tags = fresh.tags
                    current_state.selected = fresh
                    current_state.selected_ids = {fresh.id} if fresh.id else set()

                current_state.reload()
                refresh_all_ui(current_state)

            sug_dialog.close()
            ui.notify(f"Added {len(selected)} tag(s)", color="positive")

        def select_all():
            for item in tag_items:
                item["selected"] = True
            refresh_items()
            update_add_button()

        def deselect_all():
            for item in tag_items:
                item["selected"] = False
            refresh_items()
            update_add_button()

        update_add_button()

        with ui.row().classes("justify-between w-full mt-3"):
            ui.button("Select All", on_click=select_all, color="primary").props("outline size=sm")
            ui.button("Deselect All", on_click=deselect_all).props("flat size=sm")

    sug_dialog.open()


def _remove_tag_from_video(video: Video, tag: str) -> None:
    """Remove a single tag from a video and refresh the UI."""
    state = get_state()
    if not state or not video.id:
        return

    current_tags = list(getattr(video, 'tags', []) or [])
    if tag not in current_tags:
        return

    new_tags = [t for t in current_tags if t != tag]
    db.set_video_tags(state.catalog_root, video.id, new_tags)

    # Refresh the video object in state
    fresh = db.get_video_by_path(state.catalog_root, video.path)
    if fresh:
        video.tags = fresh.tags
        if state.selected and state.selected.id == video.id:
            state.selected = fresh
            state.selected_ids = {fresh.id} if fresh.id else set()

    state.reload()
    refresh_all_ui(state)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def setup_ui(catalog_root: Path | str | None = None) -> None:
    """
    Main entry point.
    The decision of "welcome vs full app" is made fresh on every page render
    (including after ui.navigate.reload() from the welcome screen).
    This makes the transition after choosing a catalog reliable.
    """

    # Serve custom assets (fonts, icons, etc.)
    app.add_static_files('/assets', str(Path(__file__).parent.parent.parent / 'assets'))

    # Apply design system inspired by the Gemini reference (deep dark theme, refined typography, subtle accents)
    # Must use shared=True because we are using @ui.page
    ui.add_head_html('''
    <style>
        /* ========================================
           PT Root UI Font Family
           Place the woff2 files in assets/fonts/
           See assets/fonts/README.md for instructions
        ======================================== */
        @font-face {
            font-family: 'PT Root UI';
            src: url('/assets/fonts/pt-root-ui_regular.woff2') format('woff2');
            font-weight: 400;
            font-style: normal;
            font-display: swap;
        }
        @font-face {
            font-family: 'PT Root UI';
            src: url('/assets/fonts/pt-root-ui_medium.woff2') format('woff2');
            font-weight: 500;
            font-style: normal;
            font-display: swap;
        }
        @font-face {
            font-family: 'PT Root UI';
            src: url('/assets/fonts/pt-root-ui_bold.woff2') format('woff2');
            font-weight: 700;
            font-style: normal;
            font-display: swap;
        }

        :root {
            /* Core colors matching modern Gemini-inspired pro media tools */
            --q-dark: #0a0a0c;
            --q-dark-page: #0a0a0c;
            --q-dark-surface: #141416;
            --q-dark-elevated: #1c1c20;

            /* Text */
            --q-dark-text: #f1f1f3;
            --q-dark-text-secondary: #a1a1aa;
            --q-dark-text-muted: #71717a;

            /* Accent (refined indigo/violet for a cinematic feel) */
            --q-primary: #6366f1;
            --q-primary-dark: #4f46e5;

            /* Borders & surfaces */
            --q-border: #27272a;
            --q-hover: #1f1f23;

            /* Typography */
            --q-font-family: 'PT Root UI', system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }

        body {
            font-family: var(--q-font-family);
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            font-feature-settings: "kern" 1, "tnum" 1, "cv05" 1; /* Better UI numerals + kerning */
        }

        /* Apply PT Root UI to text content, but NEVER to icons */
        body,
        .q-btn,
        .q-input,
        .q-select,
        .q-chip,
        .q-table,
        .q-card,
        .q-drawer,
        .q-dialog,
        .q-item,
        .q-toolbar,
        label,
        button:not(.q-icon) {
            font-family: var(--q-font-family) !important;
        }

        /* Protect all icon elements so they keep using the icon font (Material Symbols etc.) */
        .q-icon,
        .q-icon *,
        .material-icons,
        .material-symbols-outlined,
        .material-symbols-rounded,
        .material-symbols-sharp {
            font-family: 'Material Symbols Outlined', 'Material Icons', sans-serif !important;
        }

        /* Refined card styling for media grid */
        .q-card {
            background-color: var(--q-dark-surface) !important;
            border: 1px solid var(--q-border);
            transition: all 0.15s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .q-card:hover {
            border-color: #3f3f46;
            box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1);
            transform: translateY(-1px);
        }

        /* Left drawer / sidebar polish */
        .q-drawer {
            background-color: var(--q-dark-surface) !important;
            border-right: 1px solid var(--q-border) !important;
        }

        /* Inspector / right drawer */
        .q-drawer--right {
            background-color: var(--q-dark-elevated) !important;
            border-left: 1px solid var(--q-border) !important;
        }

        /* Better typography scale */
        .text-h5 { font-weight: 600; letter-spacing: -0.025em; }
        .text-h6 { font-weight: 600; letter-spacing: -0.02em; }

        /* Chip and button refinements */
        .q-chip {
            font-weight: 500;
        }

        /* Table (list view) polish */
        .q-table {
            background-color: var(--q-dark-surface) !important;
        }

        .q-table th {
            color: var(--q-dark-text-secondary) !important;
            font-weight: 600;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        /* List view: make the selection indicator (first column) 100% bigger */
        .list-view-table td:first-child {
            font-size: 1.8em !important;
            line-height: 1 !important;
            padding-top: 2px !important;
            padding-bottom: 2px !important;
        }

        /* Active filter chips */
        .q-chip--outline {
            border-color: var(--q-border);
        }

        /* Ultra-compact inspector expansion headers for high-density video editor aesthetic.
           Target min-h-[32px] + q-py-none equivalent. Side padding for px-2/px-3 data rows.
           Titles: text-xs, font-semibold, tracking-wider (capitalized in content).
           Uses custom .inspector-expansion class for high specificity.
        */
        .inspector-expansion .q-expansion-item__header,
        .q-expansion-item.inspector-expansion .q-expansion-item__header {
            padding: 0 6px !important;
            min-height: 32px !important;
            height: auto !important;
            line-height: 1 !important;
        }
        .inspector-expansion .q-expansion-item__header .q-item,
        .inspector-expansion .q-expansion-item__header .q-focusable,
        .inspector-expansion .q-expansion-item__header .q-item__section {
            padding: 0 4px !important;
            min-height: 30px !important;
            height: auto !important;
        }
        /* Icon + chevron sizing for 32px header */
        .inspector-expansion .q-expansion-item__header .q-icon {
            font-size: 14px !important;
            line-height: 1 !important;
            height: 16px !important;
            width: 16px !important;
            margin: 0 4px 0 0 !important;
            vertical-align: middle !important;
        }
        .inspector-expansion .q-expansion-item__header .q-item__label {
            font-size: 0.75rem !important;   /* text-xs */
            font-weight: 600 !important;     /* font-semibold */
            letter-spacing: 0.05em !important; /* tracking-wider */
            line-height: 1 !important;
            padding: 0 !important;
            margin: 0 !important;
            vertical-align: middle !important;
        }
        /* Expansion body: p-0 m-0 with side padding for data rows (px-2 equiv) */
        .inspector-expansion .q-expansion-item__content {
            padding: 0 8px 4px !important;
            margin: 0 !important;
        }
        /* Kill default vertical spacing on inputs and rows inside */
        .inspector-expansion .q-expansion-item__content .q-input,
        .inspector-expansion .q-expansion-item__content .q-row,
        .inspector-expansion .q-expansion-item__content .q-col {
            margin-top: 0 !important;
            margin-bottom: 0 !important;
            padding-top: 0 !important;
            padding-bottom: 0 !important;
        }
        /* Input labels */
        .inspector-expansion .q-expansion-item__content .q-field__label {
            margin-bottom: 0 !important;
            line-height: 1 !important;
            padding-bottom: 0 !important;
            font-size: 0.75rem !important;
        }
        /* Dense fields minimal height */
        .inspector-expansion .q-expansion-item__content .q-field--dense {
            min-height: 22px !important;
            padding: 0 !important;
        }
        /* Reduce internal control padding */
        .inspector-expansion .q-expansion-item__content .q-field__control {
            padding-top: 1px !important;
            padding-bottom: 1px !important;
            min-height: 20px !important;
        }
        /* Tight vertical rhythm, no negative overkill that can collapse layouts */
        .inspector-expansion .q-expansion-item__content > *,
        .inspector-expansion .q-expansion-item__content .q-field,
        .inspector-expansion .q-expansion-item__content .q-input {
            margin-top: 0 !important;
            margin-bottom: 1px !important;
        }
    </style>
    ''', shared=True)

    @ui.page("/")
    def main_page():
        global CONTENT, STATE, APP_MODE

        ui.dark_mode(True)

        # Global keyboard support (e.g. "?" to open Help)
        def _handle_global_keys(e):
            if e.key == "?":
                _open_help_dialog()

        ui.keyboard(on_key=_handle_global_keys)

        try:
            # Fresh detection on every render/reload
            # Uses get_effective_catalog() which defaults to ~/CAT+TAG and migrates stale legacy paths.
            effective = catalog_root
            if effective is None:
                eff = settings.get_effective_catalog()
                effective = str(eff)

            if effective:
                root = config.resolve_catalog(effective)
                STATE = AppState(root)
                STATE.reload()
                APP_MODE = "app"

                # Automatic cleanup of orphaned files: audio, transcripts, subtitles, and preview boards/thumbs (runs in background)
                try:
                    from minicat.core.video import cleanup_orphaned_catalog_files
                    import asyncio as _asyncio
                    clip_ids = {v.id for v in STATE.videos if v.id}
                    _asyncio.create_task(
                        _asyncio.to_thread(cleanup_orphaned_catalog_files, root, clip_ids)
                    )
                except Exception as _cleanup_ex:
                    print(f"[Audio Cache] Background cleanup failed to start: {_cleanup_ex}")

                # Also eagerly purge any legacy .wav proxies for current clips (fast; the upgrade from WAV to processed .m4a proxy).
                # This ensures that right after opening a catalog you won't see old .wav files in the audio/ folder.
                try:
                    from minicat.core.video import purge_legacy_wav_caches
                    purged = purge_legacy_wav_caches(root, clip_ids)
                    if purged:
                        print(f"[Audio Cache] Eager legacy WAV purge on load removed {purged} file(s)")
                except Exception as _legacy_ex:
                    print(f"[Audio Cache] Eager legacy WAV purge on load failed: {_legacy_ex}")

                # If launched with a specific AIStory_*.json (e.g. `minicat open /path/to/AIStory_....json`),
                # auto-load it and go straight to the narration render + XML export dialog.
                initial_story = os.environ.pop("CAT_TAG_INITIAL_STORY", None)
                if initial_story:
                    def _trigger_direct_story_load():
                        try:
                            load_ai_director_story_and_show_export(initial_story)
                        except Exception as _story_ex:
                            ui.notify(f"Auto-load story failed: {_story_ex}", color="negative")
                            print(_story_ex)
                    # Give the main layout (header, drawers, content) time to render
                    ui.timer(1.8, _trigger_direct_story_load, once=True)
            else:
                STATE = None
                APP_MODE = "welcome"

            if APP_MODE == "app" and STATE is not None:
                # Top-level layout elements - must be direct children of the page
                create_header()
                create_left_drawer()
                create_right_drawer()

                CONTENT = ui.element("div").classes("w-full q-pa-none")
                with CONTENT:
                    main_content()

                with ui.footer().classes("bg-grey-10 text-grey-5 q-pa-xs text-caption"):
                    with ui.row().classes("items-center justify-between w-full px-4"):
                        ui.label(f"Catalog: {STATE.catalog_root}")
                        ui.label(f"📦 {len(STATE.videos)} clips visible")

                        # Live-updating transcription queue status (refreshes every 0.5s)
                        transcription_status_label = ui.label("").classes("text-orange-400 font-medium")

                        def _update_transcription_status():
                            all_jobs = list(TRANSCRIPTION_JOBS)
                            running_jobs = [j for j in all_jobs if j.status == "running"]
                            queued_jobs = [j for j in all_jobs if j.status == "queued"]
                            done_jobs = [j for j in all_jobs if j.status == "done"]
                            error_jobs = [j for j in all_jobs if j.status == "error"]

                            if running_jobs:
                                job = running_jobs[0]
                                total = len(queued_jobs) + len(running_jobs) + len(done_jobs) + len(error_jobs)
                                current = len(done_jobs) + 1
                                text = f"🎙️ {job.message} ({current}/{total}) — {job.filename}"
                            elif queued_jobs:
                                text = f"🎙️ Transcription queue: {len(queued_jobs)} waiting"
                            elif error_jobs:
                                job = error_jobs[0]
                                text = f"❌ Transcription failed: {job.message} — {job.filename}"
                            else:
                                text = ""

                            transcription_status_label.set_text(text)
                            try:
                                ui.update(transcription_status_label)
                            except Exception:
                                pass

                        # Register with the new decoupled worker system (Layer 2)
                        _register_transcription_status_updater(_update_transcription_status)

                        ui.timer(0.5, _update_transcription_status, once=False)
            else:
                CONTENT = ui.element("div").classes("w-full h-full")
                with CONTENT:
                    main_content()

        except Exception as e:
            # Prevent 500 - show error and fallback to welcome
            import traceback
            traceback.print_exc()
            ui.notify(f"Error loading UI: {e}", color="negative", duration=10)
            print(f"[Startup] Critical error in main_page: {e}")

            # Reset globals to a clean state (the top-level global declaration already covers these names)
            STATE = None
            APP_MODE = "welcome"
            CONTENT = ui.element("div").classes("w-full h-full")

            with CONTENT:
                ui.label("There was a problem loading the app.").classes("text-red text-xl")
                ui.label(f"Error: {e}").classes("text-sm text-red-500 mt-2")
                ui.label("Please choose your catalog folder again.").classes("text-grey mt-4")

                # Re-use the welcome catalog chooser logic
                def choose_catalog_again():
                    try:
                        import webview
                        win = webview.active_window() or (webview.windows[0] if webview.windows else None)
                        if win:
                            res = win.create_file_dialog(webview.FileDialog.FOLDER, allow_multiple=False)
                            if res:
                                chosen = Path(res[0])
                                chosen.mkdir(parents=True, exist_ok=True)
                                config.resolve_catalog(chosen)
                                settings.set_last_catalog(chosen)
                                ui.navigate.reload()
                        else:
                            # Fallback text input if no webview
                            with ui.dialog() as d, ui.card():
                                path_input = ui.input("Catalog folder path", value=str(Path.home() / "CAT+TAG"))
                                def do_choose():
                                    p = Path(path_input.value).expanduser()
                                    p.mkdir(parents=True, exist_ok=True)
                                    config.resolve_catalog(p)
                                    settings.set_last_catalog(p)
                                    d.close()
                                    ui.navigate.reload()
                                ui.button("Use this folder", on_click=do_choose, color="primary")
                            d.open()
                    except Exception as ex2:
                        ui.notify(f"Failed to choose folder: {ex2}", color="negative")

                ui.button("Choose Catalog Folder Again", on_click=choose_catalog_again, color="primary").classes("mt-4")


def run_web(catalog_root: Path | str, **kwargs) -> None:
    """Run CAT+TAG as a normal web app (opens in browser by default)."""
    setup_ui(catalog_root)

    run_kwargs = dict(
        title="CAT+TAG",
        dark=True,
        reload=False,
        show=True,
        port=0,
        favicon="🎥",
    )
    run_kwargs.update(kwargs)

    ui.run(**run_kwargs)


def create_app(catalog_root: Path | str) -> None:
    """
    Backward-compatible entry point.
    Runs the web version (for the old `minicat open` behavior).
    """
    run_web(catalog_root)


if __name__ == "__main__":
    # For direct testing: python -m minicat.ui.app /path/to/catalog
    import sys
    if len(sys.argv) > 1:
        create_app(sys.argv[1])
    else:
        print("Usage: python -m minicat.ui.app /path/to/catalog")
