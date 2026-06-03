"""
CAT+TAG — Clean Workspace (First Version)

Three-column NLE-inspired layout as specified:

- Header: minimal, pro, with omnibar placeholder
- Left: Taxonomy / Navigation & Filters (collapsible smart collections + trees)
- Center: Media Grid (high-density cards, density controls, batch bar foundation)
- Right: Contextual Metadata Panel (vertical storyboard filmstrip on top + structured editor below)

Design goals for this clean v1:
- Breathing room + strong visual hierarchy (no 10px uppercase labels everywhere)
- Cinematic dark aesthetic, executed cleanly
- Persistent three columns (no god drawers where possible)
- Foundation for hover-scrub, omnibar, drag-drop ingest later

Run / test:
    from minicat.ui.workspace import setup_workspace_ui
    setup_workspace_ui("/path/to/catalog")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import app, ui

from minicat.core import config, db
from minicat.core.models import SearchFilters, Video
from minicat.core.settings import (
    get_last_catalog,
    set_last_catalog,
)
from minicat.ui.components import dialogs as ui_dialogs

# ---------------------------------------------------------------------------
# Theme tokens (cinematic dark, refined)
# ---------------------------------------------------------------------------

DARK_BG = "#0a0a0c"
DARK_SURFACE = "#141416"
DARK_ELEVATED = "#1c1c20"
DARK_BORDER = "#27272a"
TEXT_PRIMARY = "#f1f1f3"
TEXT_SECONDARY = "#a1a1aa"
TEXT_MUTED = "#71717a"
ACCENT = "#6366f1"  # refined indigo

# ---------------------------------------------------------------------------
# State for the new workspace (lightweight, clean)
# ---------------------------------------------------------------------------


class WorkspaceState:
    def __init__(self, catalog_root: Path):
        self.catalog_root = catalog_root
        self.videos: list[Video] = []
        self.selected: Video | None = None
        self.selected_ids: set[int] = set()
        self.filters: SearchFilters = SearchFilters()
        self.view_density: str = "medium"  # small | medium | large
        self.sort_mode: str = "shoot_date_desc"

    def reload(self) -> None:
        self.videos = db.search_videos(self.catalog_root, self.filters, limit=500)
        # simple default sort
        self.videos.sort(key=lambda v: v.shoot_date or v.import_date or "", reverse=True)

    def select(self, video: Video) -> None:
        self.selected = video
        self.selected_ids = {video.id} if video.id else set()
        render_workspace_content.refresh()

    def clear_selection(self) -> None:
        self.selected = None
        self.selected_ids.clear()
        render_workspace_content.refresh()


STATE: WorkspaceState | None = None
APP_MODE: str = "wizard"  # "wizard" | "workspace"


def _handle_keys(e: Any) -> None:
    """Global keyboard handler (Cmd/Ctrl+K for future omnibar)."""
    if e.key == "k" and (e.ctrl or e.meta):
        ui.notify(
            "⌘K Omnibar coming soon — structured search like cam:FX6 project:Foozu #Interview"
        )


# ---------------------------------------------------------------------------
# UI Components (clean, focused)
# ---------------------------------------------------------------------------


def create_header() -> None:
    """Clean pro header matching the spec."""
    with (
        ui.header(elevated=False)
        .classes("items-center q-pa-sm")
        .style(f"background: {DARK_BG}; border-bottom: 1px solid {DARK_BORDER}; height: 52px;")
    ):
        with ui.row().classes("items-center w-full px-2 gap-3"):
            # Left: Menu + brand
            with ui.row().classes("items-center gap-2"):
                ui.button(
                    icon="menu", on_click=lambda: ui.notify("Sidebar collapse coming soon")
                ).props("flat dense").classes("text-xl")
                ui.html(
                    f'<span class="text-xl font-semibold tracking-[-0.02em]" style="color: {TEXT_PRIMARY}">CAT</span>'
                    f'<span class="text-xl font-bold" style="color: {ACCENT}">+</span>'
                    f'<span class="text-xl font-semibold tracking-[-0.02em]" style="color: {TEXT_PRIMARY}">TAG</span>'
                )

            # Quick status pill
            ui.chip("Local", icon="lock", color="primary").props("outline size=sm dense").classes(
                "ml-1"
            )

            # Center: Omnibar (placeholder for v1 — real implementation next)
            with ui.row().classes("flex-1 justify-center"):
                with (
                    ui.input(placeholder="Search clips, tags, cameras...  (⌘K for advanced)")
                    .props("dense outlined clearable")
                    .classes("w-[480px] max-w-[55vw]") as search
                ):
                    search.style("background: #111; border-color: #333;")

            # Right actions
            with ui.row().classes("items-center gap-1"):
                ui.button(
                    "Quick Connect", icon="bolt", on_click=lambda: ui.notify("Coming in next pass")
                ).props("flat dense size=sm")
                ui.button(
                    icon="settings", on_click=lambda: ui.notify("Settings dialog in next iteration")
                ).props("flat dense")
                ui.button(
                    icon="help_outline", on_click=lambda: ui.notify("Help & shortcuts")
                ).props("flat dense")


def create_left_sidebar() -> None:
    """
    Left sidebar (persistent column, not a drawer).
    Matches the user's three-column NLE-inspired spec.
    """
    with (
        ui.column()
        .classes("w-[240px] h-full shrink-0 q-pa-sm overflow-auto")
        .style(f"background: {DARK_SURFACE}; border-right: 1px solid {DARK_BORDER};")
    ):
        with ui.column().classes("w-full gap-1 text-sm"):
            # Header
            ui.label("NAVIGATION & FILTERS").classes(
                "text-xs tracking-[1.5px] text-grey-6 px-1 mb-1"
            )

            # All Media
            with (
                ui.row()
                .classes("items-center px-2 py-1 rounded hover:bg-[#1f1f23] cursor-pointer")
                .on("click", lambda: _clear_filters())
            ):
                ui.icon("home", size="1rem").classes("text-grey-5 mr-2")
                ui.label("All Media").classes("flex-1")
                if STATE:
                    ui.label(str(len(STATE.videos))).classes("text-xs text-grey-6")

            ui.separator().classes("my-1 border-zinc-800")

            # Smart Collections section
            ui.label("SMART COLLECTIONS").classes(
                "text-xs tracking-[1.5px] text-grey-6 px-1 mt-1 mb-0.5"
            )

            for label, icon, count in [
                ("Recent Shoots (30d)", "schedule", 87),
                ("4K UHD", "4k", 312),
                ("1080p", "hd", 419),
                ("Interviews", "mic", 64),
            ]:
                with ui.row().classes(
                    "items-center px-2 py-0.5 rounded hover:bg-[#1f1f23] cursor-pointer text-sm"
                ):
                    ui.icon(icon, size="1rem").classes("text-grey-5 mr-2")
                    ui.label(label).classes("flex-1")
                    ui.label(str(count)).classes("text-xs text-grey-6")

            ui.separator().classes("my-2 border-zinc-800")

            # Projects (tree)
            ui.label("PROJECTS").classes("text-xs tracking-[1.5px] text-grey-6 px-1 mb-0.5")
            for proj in ["HaminaDoc", "Foozu Campaign", "Personal 2025", "Client: Yle News"]:
                with ui.row().classes(
                    "items-center pl-2 py-0.5 rounded hover:bg-[#1f1f23] cursor-pointer text-sm"
                ):
                    ui.icon("folder", size="1rem").classes("text-grey-5 mr-1.5")
                    ui.label(proj).classes("flex-1")

            ui.separator().classes("my-2 border-zinc-800")

            # Cameras
            ui.label("CAMERAS").classes("text-xs tracking-[1.5px] text-grey-6 px-1 mb-0.5")
            for cam in ["Sony FX6", "BMPCC 6K", "ARRI Alexa Mini LF", "Canon C300"]:
                with ui.row().classes(
                    "items-center px-2 py-0.5 rounded hover:bg-[#1f1f23] cursor-pointer text-sm"
                ):
                    ui.icon("videocam", size="1rem").classes("text-grey-5 mr-2")
                    ui.label(cam).classes("flex-1")

            ui.separator().classes("my-2 border-zinc-800")

            # Tags (compact)
            ui.label("TAGS").classes("text-xs tracking-[1.5px] text-grey-6 px-1 mb-0.5")
            with ui.row().classes("flex-wrap gap-1 px-1"):
                for tag, count in [
                    ("A-Roll", 142),
                    ("B-Roll", 89),
                    ("Interview", 67),
                    ("Drone", 41),
                    ("Exterior", 55),
                ]:
                    ui.chip(f"{tag} {count}").props("size=sm outline").classes(
                        "text-xs cursor-pointer"
                    )


def _clear_filters() -> None:
    if STATE:
        STATE.filters = {}
        STATE.reload()
        render_workspace_content.refresh()


def create_center_grid() -> None:
    """Center workspace: toolbar + media grid."""
    if STATE is None:
        ui.label("No catalog loaded").classes("text-grey-6 p-8")
        return

    with ui.column().classes("w-full h-full").style(f"background: {DARK_BG};"):
        # Toolbar
        with (
            ui.row()
            .classes("items-center justify-between px-4 py-2")
            .style(f"border-bottom: 1px solid {DARK_BORDER}; background: {DARK_SURFACE};")
        ):
            with ui.row().classes("items-center gap-2"):
                ui.button("Import", icon="arrow_downward", on_click=_trigger_import).props(
                    "flat dense size=sm"
                )
                ui.button(icon="grid_view", on_click=lambda: None).props("flat dense").tooltip(
                    "Grid view"
                )
                ui.button(icon="list", on_click=lambda: None).props("flat dense").tooltip(
                    "List view (soon)"
                )

            with ui.row().classes("items-center gap-3 text-sm"):
                ui.label("Density").classes("text-grey-6 text-xs")
                ui.slider(min=0, max=2, value=1, step=1).props("dense").classes(
                    "w-24"
                ).on_value_change(_change_density)
                ui.select(["Date ↓", "Name", "Duration"], value="Date ↓").props(
                    "outlined dense"
                ).classes("w-28")

            ui.label(f"{len(STATE.videos)} clips").classes("text-grey-6 text-xs")

        # The actual grid
        with ui.scroll_area().classes("w-full h-full q-pa-md"):
            if not STATE.videos:
                ui.label("No clips. Drop a folder to import.").classes(
                    "text-grey-6 mt-12 text-center"
                )
                return

            # Responsive grid — 3 to 6 columns depending on density
            cols = {"small": 6, "medium": 4, "large": 3}.get(STATE.view_density, 4)
            with ui.grid(columns=cols).classes("gap-4 w-full"):
                for v in STATE.videos[:60]:  # cap for first version performance
                    _render_media_card(v)


def _render_media_card(video: Video) -> None:
    """Clean media card for v1. Hover-scrub foundation will be added here."""
    is_selected = bool(
        STATE and video.id and video.id in (getattr(STATE, "selected_ids", set()) or set())
    )

    card_classes = "cursor-pointer overflow-hidden transition-all rounded-md border"
    if is_selected:
        card_classes += " ring-2 ring-primary border-primary/60"
    else:
        card_classes += " border-zinc-800 hover:border-zinc-700"

    with (
        ui.card()
        .classes(card_classes)
        .style(f"background: {DARK_SURFACE};")
        .on("click", lambda v=video: STATE.select(v) if STATE else None)
    ):
        # Thumbnail area (placeholder for real hover-scrub strip)
        with ui.element("div").classes("relative w-full aspect-video bg-black overflow-hidden"):
            if video.thumbnail_path and Path(video.thumbnail_path).exists():
                ui.image(str(video.thumbnail_path)).classes("w-full h-full object-cover")
            else:
                with (
                    ui.element("div")
                    .classes("w-full h-full flex items-center justify-center")
                    .style("background:#111")
                ):
                    ui.icon("movie", size="2.2rem").classes("text-grey-7")

            # Duration badge
            dur = (
                f"{int((video.duration or 0) // 60)}:{int((video.duration or 0) % 60):02d}"
                if video.duration
                else "—"
            )
            with ui.element("div").classes(
                "absolute bottom-1 left-1 px-1.5 py-px bg-black/80 text-[10px] font-mono rounded"
            ):
                ui.label(dur)

        # Footer info
        with ui.column().classes("px-2 py-1.5 gap-0.5"):
            ui.label(video.filename).classes("text-sm font-medium truncate")
            meta = " · ".join(
                filter(None, [video.camera, str(video.shoot_date) if video.shoot_date else None])
            )
            if meta:
                ui.label(meta).classes("text-xs text-grey-6 truncate")

            # Tag pills (first few)
            if video.tags:
                with ui.row().classes("gap-1 flex-wrap pt-0.5"):
                    for t in video.tags[:3]:
                        ui.chip(t).props("size=xs dense outline")


def _change_density(e: Any) -> None:
    if not STATE:
        return
    val = int(e.value)
    STATE.view_density = {0: "small", 1: "medium", 2: "large"}.get(val, "medium")
    render_workspace_content.refresh()


def create_right_metadata_panel() -> None:
    """
    Right panel exactly as specified:
    - Top: Vertical dynamic storyboard / filmstrip
    - Bottom: Structured metadata editor (Production + Technical + Tags)
    """
    if STATE is None or not STATE.selected:
        with (
            ui.column()
            .classes(
                "h-full items-center justify-center text-center px-4 py-2 flex flex-col gap-y-3"
            )
            .style(f"background: {DARK_ELEVATED}; border-left: 1px solid {DARK_BORDER};")
        ):
            ui.icon("info", size="2.8rem").classes("text-grey-7 mb-3")
            ui.label("Select a clip").classes("text-base text-grey-6")
            ui.label("Storyboard + metadata will appear here").classes("text-xs text-grey-7 mt-1")
        return

    v = STATE.selected

    with (
        ui.column()
        .classes("h-full w-full px-4 py-2 flex flex-col gap-y-3")
        .style(f"background: {DARK_ELEVATED}; border-left: 1px solid {DARK_BORDER};")
    ):
        # Top: Vertical Filmstrip / Storyboard area (the "Dynamic Storyboard")
        # Keep storyboard fully visible + prominent; do not shrink image sizing
        with (
            ui.column()
            .classes("w-full px-2 py-1")
            .style(f"border-bottom: 1px solid {DARK_BORDER};")
        ):
            ui.label("STORYBOARD").classes(
                "text-xs font-semibold tracking-wider text-zinc-500 mb-1 px-0.5"
            )
            # Vertical filmstrip — v1 uses the existing storyboard image tall + note
            # Future: generate 8-12 vertical frames and make them clickable (copy timestamp)
            if v.storyboard_path and Path(v.storyboard_path).exists():
                with ui.element("div").classes(
                    "w-full overflow-hidden rounded border border-zinc-800"
                ):
                    sb_img = (
                        ui.image(str(v.storyboard_path))
                        .classes("w-full object-cover cursor-pointer hover:opacity-90")
                        .style("max-height: 220px; image-rendering: crisp-edges;")
                    )
                    sb_img.on("click", lambda vv=v: ui_dialogs.show_storyboard_dialog(vv))
            else:
                with ui.element("div").classes(
                    "w-full h-[180px] bg-[#111] rounded flex flex-col items-center justify-center border border-zinc-800"
                ):
                    ui.icon("grid_view", size="2rem").classes("text-grey-7")
                    ui.label("No storyboard yet").classes("text-xs text-grey-6 mt-2")

            ui.label("Click image for full view").classes("text-[10px] text-grey-7 mt-1")

        # Bottom: Structured Metadata Editor
        with ui.scroll_area().classes("flex-1 px-2 py-1"):
            ui.label("METADATA").classes(
                "text-xs font-semibold tracking-wider text-zinc-500 mb-1 px-0.5"
            )

            # Production section
            ui.label("PRODUCTION").classes(
                "text-xs font-semibold tracking-wider text-zinc-500 mt-1 mb-0.5"
            )
            _inline_field("Project", v.project or "")
            _inline_field("Scene", "")  # placeholder for future model fields
            _inline_field("Take", "")
            _inline_field("Reel", "")

            # Technical
            ui.label("TECHNICAL").classes(
                "text-xs font-semibold tracking-wider text-zinc-500 mt-1 mb-0.5"
            )
            _inline_field("Camera", v.camera or "")
            _inline_field("Lens / Focal", v.lens or "")
            _inline_field("Resolution", f"{v.width}×{v.height}" if v.width and v.height else "—")
            _inline_field("Codec", (v.codec or "—").upper())
            _inline_field("FPS", f"{v.fps:.2f}" if v.fps else "—")

            # Tags
            ui.label("TAGS").classes(
                "text-xs font-semibold tracking-wider text-zinc-500 mt-1 mb-0.5"
            )
            current = ", ".join(v.tags) if v.tags else ""
            tag_input = (
                ui.input(value=current, placeholder="A-Roll, Interview, B-Roll")
                .props("dense outlined square")
                .classes("w-full text-xs q-my-none")
            )
            tag_input.on("blur", lambda: _save_tags_from_input(tag_input.value or ""))

            ui.button(
                "Rebuild Previews",
                icon="refresh",
                on_click=lambda: ui.notify("Will call existing rebuild logic"),
            ).props("flat dense size=sm").classes("mt-2 w-full")


def _inline_field(label: str, value: str) -> None:
    """Small clean inline label + value row."""
    with ui.row().classes("flex row no-wrap justify-between items-center q-py-xs py-0.5"):
        ui.label(label).classes("text-xs text-zinc-500 font-medium")
        ui.label(value or "—").classes("text-xs text-zinc-200 font-mono text-right truncate")


def _save_tags_from_input(value: str) -> None:
    if not STATE or not STATE.selected or not STATE.selected.id:
        return
    tags = [t.strip() for t in value.split(",") if t.strip()]
    db.set_video_tags(STATE.catalog_root, STATE.selected.id, tags)
    # refresh the selected video
    refreshed = db.get_video_by_path(STATE.catalog_root, STATE.selected.path)
    if refreshed:
        STATE.selected = refreshed
    render_workspace_content.refresh()


def _trigger_import() -> None:
    ui.notify("Import wizard will be wired in the next iteration", color="info")


# ---------------------------------------------------------------------------
# Visual First-Launch Wizard (Clean new design)
# ---------------------------------------------------------------------------


def _switch_to_catalog(path: Path | str) -> None:
    """Initialize the workspace with a catalog and switch out of wizard mode."""
    global STATE, APP_MODE

    # Use the official resolver — it creates the folder + runs init_catalog (DB schema, previews dirs, etc.)
    p = config.resolve_catalog(path)

    # Persist choice so next bare `uv run minicat` remembers it
    try:
        set_last_catalog(p)
    except Exception:
        pass

    STATE = WorkspaceState(p)
    STATE.reload()
    APP_MODE = "workspace"

    # Force a full page reload so the proper top-level layout
    # (header + main content + footer) gets created in setup_workspace_ui.
    # This is the safest way to switch from wizard → full workspace.
    ui.navigate.reload()


def render_welcome_wizard() -> None:
    """Beautiful, calm, visual wizard for new or existing catalog.
    Uses the brushed metal + logo background image.
    """
    with (
        ui.column()
        .classes("w-full h-screen items-center justify-center relative")
        .style(
            "background-image: url('/assets/cat-tag-wizard-bg.png');"
            "background-size: cover;"
            "background-position: center;"
            "background-repeat: no-repeat;"
            f"color: {TEXT_PRIMARY};"
        )
    ):
        # Dark overlay so the metal texture and logo remain visible but the UI is very readable
        with (
            ui.element("div")
            .classes("absolute inset-0")
            .style(
                "background: linear-gradient(rgba(10,10,12,0.68), rgba(10,10,12,0.76));"
                "pointer-events: none;"
            )
        ):
            pass

        # All wizard content sits on top of the background + overlay
        with ui.column().classes("relative z-10 items-center"):
            ui.keyboard(on_key=_handle_keys)

            # Branding
            with ui.column().classes("items-center mb-8"):
                with ui.row().classes("items-center gap-2"):
                    ui.html(
                        f'<span class="text-5xl font-semibold tracking-[-0.03em]" style="color: {TEXT_PRIMARY}">CAT</span>'
                        f'<span class="text-5xl font-bold" style="color: {ACCENT}">+</span>'
                        f'<span class="text-5xl font-semibold tracking-[-0.03em]" style="color: {TEXT_PRIMARY}">TAG</span>'
                    )
                ui.label("Your personal video catalog").classes("text-2xl text-grey-5 mt-1")
                ui.label("100% local. Nothing ever leaves your machine.").classes(
                    "text-sm text-grey-6 mt-1"
                )

            # Two clear options
            with ui.row().classes("gap-6 max-w-[820px] w-full px-6"):
                # === CREATE NEW ===
                with (
                    ui.card()
                    .classes("flex-1 p-6")
                    .style(f"background: {DARK_SURFACE}; border: 1px solid {DARK_BORDER};")
                ):
                    ui.icon("create_new_folder", size="2.8rem").classes("text-primary mb-3")
                    ui.label("Create New Catalog").classes("text-2xl font-semibold mb-1")
                    ui.label(
                        "Start a fresh library. CAT+TAG will store all metadata, thumbnails, and proxies inside one folder on your drive."
                    ).classes("text-sm text-grey-5 mb-5 leading-snug")

                    default_path = str(Path.home() / "VideoCatalogs" / "CAT+TAG")
                    new_path_input = (
                        ui.input("Catalog folder location", value=default_path)
                        .props("dense")
                        .classes("w-full mb-4")
                    )

                    def do_create_new():
                        target = Path(new_path_input.value).expanduser()
                        try:
                            _switch_to_catalog(target)
                        except Exception as e:
                            ui.notify(f"Could not create catalog: {e}", color="negative")

                    ui.button(
                        "Create Catalog & Continue",
                        icon="arrow_forward",
                        on_click=do_create_new,
                        color="primary",
                    ).props("size=lg").classes("w-full py-3 text-base")

                    ui.label("Recommended for most people").classes(
                        "text-[10px] text-grey-7 mt-3 text-center"
                    )

                # === OPEN EXISTING ===
                with (
                    ui.card()
                    .classes("flex-1 p-6")
                    .style(f"background: {DARK_SURFACE}; border: 1px solid {DARK_BORDER};")
                ):
                    ui.icon("folder_open", size="2.8rem").classes("text-grey-5 mb-3")
                    ui.label("Open Existing Catalog").classes("text-2xl font-semibold mb-1")
                    ui.label(
                        "Point to a folder that already contains a CAT+TAG catalog (has a catalog.db inside). Perfect when you already have one on an external drive."
                    ).classes("text-sm text-grey-5 mb-5 leading-snug")

                    def open_existing_native():
                        """Use native folder dialog when running in desktop (pywebview)."""
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
                                        ui.notify(
                                            "That folder does not contain a valid CAT+TAG catalog",
                                            color="warning",
                                        )
                                    return
                        except Exception:
                            pass
                        # Fallback: simple input dialog
                        with ui.dialog() as dlg, ui.card().classes("w-96"):
                            path_in = ui.input("Path to existing catalog folder")
                            ui.button(
                                "Open",
                                on_click=lambda: (
                                    _switch_to_catalog(path_in.value) if path_in.value else None,
                                    dlg.close(),
                                ),
                            ).props("color=primary").classes("w-full mt-2")
                        dlg.open()

                    ui.button(
                        "Choose Existing Folder...",
                        icon="drive_folder_upload",
                        on_click=open_existing_native,
                    ).props("size=lg outline").classes("w-full py-3 text-base mt-2")

                    ui.label("Use this when moving between machines or drives").classes(
                        "text-[10px] text-grey-7 mt-3 text-center"
                    )

            # Bottom reassurance
            with ui.row().classes("mt-10 items-center gap-2 text-xs text-grey-6"):
                ui.icon("lock", size="1rem")
                ui.label("Everything stays on your computer. No accounts. No cloud. No telemetry.")


# ---------------------------------------------------------------------------
# Main refreshable layout
# ---------------------------------------------------------------------------


def render_workspace_content() -> None:
    """
    The main three-column content area.
    This function can be safely refreshed because it only contains regular elements,
    not top-level layout elements like header/footer.
    """
    with (
        ui.row()
        .classes("w-full flex-1")
        .style(f"background: {DARK_BG}; color: {TEXT_PRIMARY}; overflow: hidden;")
    ):
        ui.keyboard(on_key=_handle_keys)

        # LEFT — Persistent sidebar (fixed width)
        create_left_sidebar()

        # CENTER — Main grid/workspace
        with ui.column().classes("flex-1 h-full overflow-hidden"):
            create_center_grid()

        # RIGHT — Contextual metadata panel (fixed width)
        with (
            ui.column()
            .classes("w-[340px] h-full shrink-0")
            .style(
                f"min-width: 300px; max-width: 420px; background: {DARK_ELEVATED}; border-left: 1px solid {DARK_BORDER};"
            )
        ):
            create_right_metadata_panel()


@ui.refreshable
def workspace_ui() -> None:
    """
    Entry point called from setup_workspace_ui.
    For the main workspace, the actual top-level layout (header, content, footer)
    is created in setup_workspace_ui() so that ui.header / ui.footer are direct
    children of the page.
    """
    if APP_MODE == "wizard" or STATE is None:
        render_welcome_wizard()
        return

    # When called as a refreshable, we only re-render the inner content area.
    # The header and footer stay in place (created once in setup_workspace_ui).
    render_workspace_content()


# ---------------------------------------------------------------------------
# Public entry point (clean, parallel to the old setup_ui)
# ---------------------------------------------------------------------------


def setup_workspace_ui(catalog_root: Path | str | None = None) -> None:
    """
    Launch the clean new workspace UI (three-column layout + visual wizard).
    """
    global STATE, APP_MODE

    # Serve assets (fonts, icons, etc.)
    try:
        app.add_static_files("/assets", str(Path(__file__).parent.parent.parent / "assets"))
    except Exception:
        pass

    # Apply clean cinematic theme
    ui.add_head_html("""
    <style>
        :root {
            --q-dark: #0a0a0c;
            --q-dark-page: #0a0a0c;
            --q-dark-surface: #141416;
            --q-primary: #6366f1;
        }
        body { font-feature-settings: "kern" 1, "tnum" 1; }
    </style>
    """)

    # Keyboard handler is now created inside the actual rendered content
    # (wizard or workspace) to avoid "Client has been deleted" errors during setup.

    # === Decide: show wizard or go straight to workspace ===
    effective = catalog_root

    if effective is None:
        # Try last used catalog from settings
        last = get_last_catalog()
        if last and (last / "catalog.db").exists():
            effective = last

    if effective:
        # User (or settings) gave us a valid starting point → skip wizard
        root = Path(effective).expanduser().resolve()
        STATE = WorkspaceState(root)
        STATE.reload()
        APP_MODE = "workspace"

        # === Create top-level layout elements DIRECTLY under the page ===
        create_header()

        # Main dynamic content (left sidebar + center grid + right panel).
        # render_workspace_content() already includes create_left_sidebar().
        render_workspace_content()

        # Footer at page root level
        with (
            ui.footer()
            .classes("q-pa-xs text-caption")
            .style(f"background: {DARK_BG}; border-top: 1px solid {DARK_BORDER}; height: 28px;")
        ):
            with ui.row().classes("items-center justify-between w-full px-4 text-xs text-grey-6"):
                ui.label("🖥️ Local Core Active")
                ui.label(f"📦 {len(STATE.videos)} clips indexed")
                ui.label("💾 100% local & private")

    else:
        # No catalog yet → show the beautiful visual wizard (no header/footer needed)
        APP_MODE = "wizard"
        STATE = None
        workspace_ui()  # wizard path is safe inside refreshable


# Convenience for quick manual testing
if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    setup_workspace_ui(path)
    ui.run(dark=True, title="CAT+TAG — Clean Workspace v1", reload=False)
