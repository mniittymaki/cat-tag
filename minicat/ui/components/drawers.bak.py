from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Optional

from nicegui import ui

from minicat.core.models import Client
from minicat.core import db
from minicat.ui.components import dialogs as ui_dialogs


def rich_tags_section(
    state: Any,
    on_refresh: Optional[Callable[[], None]] = None,
) -> None:
    if state is None:
        return

    # Muted, high-density professional header
    ui.label("RICH TAGS").classes("text-xs font-bold text-zinc-500 tracking-wider q-px-sm q-pb-xs")

    try:
        most_used = db.get_most_used_tags(state.catalog_root, limit=18)
    except Exception:
        most_used = []

    # Compact grid container for tags
    with ui.element("div").classes("flex flex-wrap gap-1 q-px-sm"):
        active_tags = state.filters.tags or []

        def make_handler(tag_name: str):
            def handler():
                state.toggle_tag(tag_name)
                if on_refresh:
                    on_refresh()
                else:
                    try:
                        from minicat.ui.app import main_content
                        main_content.refresh()
                    except Exception:
                        pass
            return handler

        for tag, count in most_used:
            is_active = tag in active_tags

            # Highly compact, dense badge layout (replaces heavy ui.chip)
            badge_classes = (
                "q-py-xs q-px-sm text-xs rounded-full cursor-pointer transition-colors border select-none "
                "bg-primary text-white border-primary" if is_active else
                "bg-zinc-800 text-zinc-300 border-zinc-700 hover:bg-zinc-700"
            )

            badge = ui.badge(f"{tag} ({count})").classes(badge_classes)
            badge.on("click", make_handler(tag))


def left_drawer_content(
    state: Any,
    refresh_callbacks: Optional[dict[str, Callable[[], None]]] = None,
) -> None:
    if state is None:
        ui.label("No catalog loaded").classes("text-zinc-500 italic text-xs q-pa-sm")
        return

    rc = refresh_callbacks or {}

    def get_refresh(name: str) -> Callable[[], None]:
        return rc.get(name) or (lambda: None)

    # Force strict uniform vertical rhythm with a tight layout gap
    with ui.column().classes("w-full gap-y-3 q-pa-xs select-none"):

        # === BROWSE & FILTER ===
        with ui.column().classes("w-full gap-y-0.5"):
            ui.label("BROWSE & FILTER").classes("text-xs font-bold text-zinc-500 tracking-wider q-px-sm q-pb-xs")

            def show_all_media():
                state.clear_filters()
                get_refresh("rich_tags")()

            with ui.row().classes("w-full items-center justify-between hover:bg-zinc-800 rounded q-px-sm q-py-xs cursor-pointer text-sm").on("click", show_all_media):
                with ui.row().classes("items-center gap-x-2 no-wrap"):
                    ui.icon("home", size="xs").classes("text-zinc-400")
                    ui.label("All Media").classes("text-zinc-200")
                ui.badge(str(len(state.videos)), color="zinc-700").classes("text-xs text-zinc-400 q-px-xs")

            def show_recently_added():
                state.clear_filters()
                state.set_sort("import_date", True)
                get_refresh("rich_tags")()

            with ui.row().classes("w-full items-center justify-between hover:bg-zinc-800 rounded q-px-sm q-py-xs cursor-pointer text-sm").on("click", show_recently_added):
                with ui.row().classes("items-center gap-x-2 no-wrap"):
                    ui.icon("schedule", size="xs").classes("text-zinc-400")
                    ui.label("Recently Added").classes("text-zinc-200")

        ui.separator().classes("bg-zinc-800/60 my-0")

        # === CLIENTS ===
        with ui.column().classes("w-full gap-y-0.5"):
            ui.label("CLIENTS").classes("text-xs font-bold text-zinc-500 tracking-wider q-px-sm q-pb-xs")

            def create_new_client():
                try:
                    ui_dialogs.show_rich_client_dialog(state, client_id=None)
                except Exception:
                    ui.notify("Client dialog not available yet", color="warning")

            # Clean, flat, dense button under the header
            ui.button("NEW CLIENT", icon="add", on_click=create_new_client).props("size=sm flat dense color=primary").classes("q-px-sm text-xs font-medium self-start")

            client_structure = defaultdict(lambda: defaultdict(int))

            for client_name in state.all_clients:
                if client_name not in client_structure:
                    client_structure[client_name]

            for v in state.videos:
                p = v.project or "(none)"
                proj_clients = db.get_clients_for_project(state.catalog_root, p) if p != "(none)" else []
                if not proj_clients:
                    proj_clients = [Client(name="(No Client)")]

                for cl in proj_clients:
                    client_structure[cl.name][p] += 1

            if "(No Client)" in client_structure:
                del client_structure["(No Client)"]

            for client_name in list(state.all_clients):
                client_obj = next((c for c in db.get_clients(state.catalog_root) if c.name == client_name), None)
                if not client_obj:
                    continue
                projects_for_client = db.get_projects_for_client(state.catalog_root, client_obj.id)
                for proj_name in projects_for_client:
                    if proj_name not in client_structure[client_name]:
                        client_structure[client_name][proj_name] = 0

            for client_name in sorted(client_structure.keys()):
                projects_dict = client_structure[client_name]
                total_for_client = sum(projects_dict.values())

                is_active_client = bool(state.filters.client and client_name in state.filters.client)

                # Client row - slight indent + Title Case + bright text + Strict row scaling
                client_display_name = client_name.title() if client_name else "(Unnamed Client)"
                client_row_classes = "group w-full items-center justify-between hover:bg-zinc-800 rounded q-pr-sm q-py-xs cursor-pointer text-sm q-pl-md no-wrap"
                if is_active_client:
                    client_row_classes += " bg-blue-900/25"

                with ui.row().classes(client_row_classes):
                    with ui.row().classes("items-center flex-1 gap-x-2 no-wrap").on(
                        "click", lambda cn=client_name: (state.toggle_client(cn), ui.update())
                    ):
                        ui.icon("business", size="xs").classes("text-zinc-400 shrink-0")
                        label_classes = "text-sm text-zinc-200 truncate"
                        if is_active_client:
                            label_classes += " text-blue-300"
                        ui.label(client_display_name).classes(label_classes)
                        ui.badge(str(total_for_client), color="zinc-700").classes("text-xs text-zinc-400 q-px-xs shrink-0")

                    # Action buttons - right aligned, hover reveal, locked inline
                    with ui.row().classes("items-center gap-x-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 no-wrap"):
                        client_obj = next((c for c in db.get_clients(state.catalog_root) if c.name == client_name), None)
                        if client_obj:
                            ui.button(icon="info", on_click=lambda cid=client_obj.id: ui_dialogs.show_rich_client_dialog(state, cid)).props("size=xs flat dense round text-zinc-600")

                        with ui.button(icon="more_vert").props("size=xs flat dense round text-zinc-600"):
                            with ui.menu():
                                if client_obj:
                                    ui.item("Client Details...", on_click=lambda cid=client_obj.id: ui_dialogs.show_rich_client_dialog(state, cid)).props("clickable")

                # Projects - deeper indent + Title Case + locked row configuration
                for proj_name, proj_count in sorted(projects_dict.items()):
                    project_display_name = proj_name.title() if proj_name else "(Unnamed Project)"
                    with ui.row().classes("items-center pl-6 hover:bg-zinc-800 rounded q-px-sm q-py-xs cursor-pointer text-sm q-pl-xl no-wrap"):
                        with ui.row().classes("items-center flex-1 gap-x-2 no-wrap").on(
                            "click", lambda p=proj_name: (state.toggle_project(p), ui.update())
                        ):
                            ui.label(project_display_name).classes("text-sm text-zinc-300 truncate")
                            ui.badge(str(proj_count), color="zinc-700").classes("text-xs text-zinc-400 q-px-xs shrink-0")

                        with ui.row().classes("items-center gap-x-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 no-wrap"):
                            ui.button(icon="info", on_click=lambda p=proj_name: state.select_project(p)).props("size=xs flat dense round text-zinc-600")

                            def make_delete(p=proj_name):
                                return lambda: ui_dialogs.delete_project_dialog(state, p)

                            ui.button(icon="delete", on_click=make_delete()).props("size=xs flat dense round text-zinc-600")

        ui.separator().classes("bg-zinc-800/60 my-0")

        # === LOCATIONS ===
        with ui.column().classes("w-full gap-y-0.5"):
            ui.label("LOCATIONS").classes("text-xs font-bold text-zinc-500 tracking-wider q-px-sm q-pb-xs")

            for loc in state.all_locations[:12]:
                count = len([v for v in state.videos if v.location == loc])
                with ui.row().classes("w-full items-center hover:bg-zinc-800 rounded q-px-sm q-py-xs cursor-pointer text-sm").on(
                    "click", lambda location=loc: (state.toggle_location(location), ui.update())
                ):
                    ui.icon("place", size="xs").classes("text-zinc-400 mr-1")
                    ui.label(loc).classes("flex-1 text-zinc-300")
                    ui.badge(str(count), color="zinc-700").classes("text-xs text-zinc-400 q-px-xs")

        # === CAMERAS ===
        with ui.column().classes("w-full gap-y-0.5"):
            ui.label("CAMERAS").classes("text-xs font-bold text-zinc-500 tracking-wider q-px-sm q-pb-xs")

            for cam in state.all_cameras[:15]:
                count = len([v for v in state.videos if v.camera == cam])
                with ui.row().classes("w-full items-center hover:bg-zinc-800 rounded q-px-sm q-py-xs cursor-pointer text-sm").on(
                    "click", lambda c=cam: (state.toggle_camera(c), ui.update())
                ):
                    ui.icon("videocam", size="xs").classes("text-zinc-400 mr-1")
                    ui.label(cam).classes("flex-1 text-zinc-300")
                    ui.badge(str(count), color="zinc-700").classes("text-xs text-zinc-400 q-px-xs")

        ui.element("div").classes("h-0")
        rich_tags_section(state, on_refresh=get_refresh("rich_tags"))


def create_left_drawer(main_layout_state: Any) -> ui.left_drawer:
    """
    Creates the main navigation and filtering sidebar for MINI CAT&TAG.
    Optimized for a tight, professional NLE-inspired visual footprint.
    """
    # 1. Shrink width to a dense 240px (standard for NLE bins/sidebars)
    # 2. Force desktop behavior so it doesn't try to overlay or hide automatically
    left_drawer = ui.left_drawer(
        value=True,
        fixed=True,
        elevated=False
    ).props('width=240 behavior=desktop')

    # Apply premium deep matte background and a thin, muted border line
    left_drawer.classes('bg-[#111112] text-zinc-300 border-r border-zinc-800/40 q-pa-none overflow-hidden')

    with left_drawer:
        # Wrap everything in a tight native Quasar scroll area
        # so dynamic client/project lists scale seamlessly without breaking layout constraints
        with ui.scroll_area().classes('w-full h-full q-pa-sm'):
            left_drawer_content(main_layout_state)

    return left_drawer
