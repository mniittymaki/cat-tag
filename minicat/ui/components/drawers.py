from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Optional

from nicegui import ui

from minicat.core.models import Client
from minicat.core import db
from minicat.ui.components import dialogs as ui_dialogs


@ui.refreshable
def rich_tags_section(
    state: Any,
    on_refresh: Optional[Callable[[], None]] = None,
) -> None:
    if state is None:
        return

    try:
        most_used = db.get_most_used_tags(state.catalog_root, limit=18)
    except Exception:
        most_used = []

    # Compact grid container for tags
    with ui.element("div").classes("flex flex-wrap gap-1 q-px-0"):
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
                "rounded px-1.5 py-0.5 text-xs cursor-pointer transition-colors border select-none "
                "bg-primary text-white border-primary" if is_active else
                "rounded px-1.5 py-0.5 text-xs cursor-pointer bg-zinc-800 text-zinc-300 border-zinc-700 hover:bg-zinc-700"
            )

            badge = ui.badge(f"{tag} ({count})").classes(badge_classes)
            badge.on("click", make_handler(tag))


@ui.refreshable
def left_drawer_content(
    state: Any,
    refresh_callbacks: Optional[dict[str, Callable[[], None]]] = None,
) -> None:
    if state is None:
        ui.label("No catalog loaded").classes("text-zinc-500 italic text-xs q-pa-0")
        return

    rc = refresh_callbacks or {}

    def get_refresh(name: str) -> Callable[[], None]:
        return rc.get(name) or (lambda: None)

    # Force strict uniform vertical rhythm with a tight layout gap
    with ui.column().classes("w-full p-0 m-0 gap-0 select-none"):

        # === BROWSE ===
        with ui.expansion("BROWSE", icon="home", value=True).classes("w-full text-xs font-semibold text-zinc-400 tracking-wider q-py-none min-h-[36px]").props('header-class="text-zinc-400" dense disable-toggle'):
            def show_all_media():
                state.clear_filters()
                get_refresh("rich_tags")()

            with ui.row().classes("w-full items-center justify-between hover:bg-zinc-800 rounded q-px-xs py-0.5 cursor-pointer text-sm").on("click", show_all_media):
                with ui.row().classes("items-center gap-x-1 no-wrap"):
                    ui.icon("home", size="xs").classes("text-zinc-400")
                    ui.label("All Media").classes("text-zinc-200")
                ui.badge(str(len(state.videos)), color="zinc-700").classes("text-xs text-zinc-400 q-px-1")

            def show_recently_added():
                state.clear_filters()
                state.set_sort("import_date", True)
                get_refresh("rich_tags")()

            with ui.row().classes("w-full items-center justify-between hover:bg-zinc-800 rounded q-px-xs py-0.5 cursor-pointer text-sm").on("click", show_recently_added):
                with ui.row().classes("items-center gap-x-1 no-wrap"):
                    ui.icon("schedule", size="xs").classes("text-zinc-400")
                    ui.label("Recently Added").classes("text-zinc-200")

        ui.separator().classes("bg-zinc-800/60 my-0")

        # === CLIENTS ===
        with ui.expansion("CLIENTS", icon="business", value=True).classes("w-full text-xs font-semibold text-zinc-400 tracking-wider q-py-none min-h-[36px]").props('header-class="text-zinc-400" dense disable-toggle'):

            def create_new_client():
                try:
                    ui_dialogs.show_rich_client_dialog(state, client_id=None)
                except Exception:
                    ui.notify("Client dialog not available yet", color="warning")

            # Clean, flat, dense button under the header
            ui.button("NEW CLIENT", icon="add", on_click=create_new_client).props("size=sm flat dense color=primary").classes("q-px-xs text-xs font-medium self-start")

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
                client_row_classes = "group w-full items-center justify-between hover:bg-zinc-800 rounded q-px-xs py-0.5 cursor-pointer text-sm q-pl-sm no-wrap"
                if is_active_client:
                    client_row_classes += " bg-blue-900/25"

                with ui.row().classes(client_row_classes):
                    # Main content wrapper for label + edit icon (for easy access)
                    with ui.row().classes("w-full items-center justify-between group"):
                        with ui.row().classes("items-center flex-1 gap-x-1 no-wrap").on(
                            "click", lambda cn=client_name: (state.toggle_client(cn), ui.update())
                        ):
                            ui.icon("business", size="xs").classes("text-zinc-400 shrink-0")
                            label_classes = "text-sm text-zinc-200 truncate"
                            if is_active_client:
                                label_classes += " text-blue-300"
                            ui.label(client_display_name).classes(label_classes)
                            ui.badge(str(total_for_client), color="zinc-700").classes("text-xs text-zinc-400 q-px-1 shrink-0")

                        # Subtle direct Edit icon + three-dot menu
                        with ui.row().classes("items-center opacity-0 group-hover:opacity-100 transition-opacity shrink-0 no-wrap gap-1"):
                            client_obj = next((c for c in db.get_clients(state.catalog_root) if c.name == client_name), None)
                            if client_obj:
                                ui.button(
                                    icon="edit",
                                    on_click=lambda cid=client_obj.id: ui_dialogs.show_rich_client_dialog(state, cid)
                                ).props("flat dense round size=xs").classes("text-zinc-500 hover:text-zinc-300")
                                with ui.button(icon="more_vert").props("size=xs flat dense round text-zinc-600"):
                                    with ui.menu():
                                        ui.item("Edit Client", on_click=lambda cid=client_obj.id: ui_dialogs.show_rich_client_dialog(state, cid)).props("clickable")
                                        ui.item("Delete Client", on_click=lambda c=client_obj: ui_dialogs.delete_client_dialog(state, c)).props("clickable")

                # Projects - deeper indent + Title Case + locked row configuration
                for proj_name, proj_count in sorted(projects_dict.items()):
                    project_display_name = proj_name.title() if proj_name else "(Unnamed Project)"
                    with ui.row().classes("group w-full items-center justify-between hover:bg-zinc-800 rounded q-px-xs py-0.5 cursor-pointer text-sm q-pl-md no-wrap"):
                        # Main content wrapper for label + edit icon
                        with ui.row().classes("w-full items-center justify-between group"):
                            with ui.row().classes("items-center flex-1 gap-x-1 no-wrap").on(
                                "click", lambda p=proj_name: (state.toggle_project(p), ui.update())
                            ):
                                ui.icon("folder_open", size="xs").classes("text-zinc-500 shrink-0")
                                ui.label(project_display_name).classes("text-sm text-zinc-300 truncate")
                                ui.badge(str(proj_count), color="zinc-700").classes("text-xs text-zinc-400 q-px-1 shrink-0")

                            # Subtle direct Edit icon + three-dot menu
                            with ui.row().classes("items-center opacity-0 group-hover:opacity-100 transition-opacity shrink-0 no-wrap gap-1"):
                                ui.button(
                                    icon="edit",
                                    on_click=lambda p=proj_name: ui_dialogs.show_rich_project_dialog(state, p)
                                ).props("flat dense round size=xs").classes("text-zinc-500 hover:text-zinc-300")
                                with ui.button(icon="more_vert").props("size=xs flat dense round text-zinc-600"):
                                    with ui.menu():
                                        ui.item("Edit Project", on_click=lambda p=proj_name: ui_dialogs.show_rich_project_dialog(state, p)).props("clickable")
                                        ui.item("Delete Project", on_click=lambda p=proj_name: ui_dialogs.delete_project_dialog(state, p)).props("clickable")

        ui.separator().classes("bg-zinc-800/60 my-0")

        # === LOCATIONS (collapsible) ===
        with ui.expansion("LOCATIONS", icon="place", value=False).classes("w-full text-xs font-semibold text-zinc-400 tracking-wider q-py-none min-h-[36px]").props('header-class="text-zinc-400" dense'):
            for loc in state.all_locations[:12]:
                count = len([v for v in state.videos if v.location == loc])
                with ui.row().classes("w-full items-center justify-between hover:bg-zinc-800 rounded q-px-xs py-0.5 cursor-pointer text-sm no-wrap").on(
                    "click", lambda location=loc: (state.toggle_location(location), ui.update())
                ):
                    with ui.row().classes("items-center gap-x-1 no-wrap flex-1"):
                        ui.icon("place", size="xs").classes("text-zinc-400 shrink-0")
                        ui.label(loc).classes("text-zinc-300 truncate")
                    ui.badge(str(count), color="zinc-700").classes("text-xs text-zinc-400 q-px-1 shrink-0")

        # === CAMERAS (collapsible) ===
        with ui.expansion("CAMERAS", icon="videocam", value=False).classes("w-full text-xs font-semibold text-zinc-400 tracking-wider q-py-none min-h-[36px]").props('header-class="text-zinc-400" dense'):
            for cam in state.all_cameras[:15]:
                count = len([v for v in state.videos if v.camera == cam])
                with ui.row().classes("w-full items-center justify-between hover:bg-zinc-800 rounded q-px-xs py-0.5 cursor-pointer text-sm no-wrap").on(
                    "click", lambda c=cam: (state.toggle_camera(c), ui.update())
                ):
                    with ui.row().classes("items-center gap-x-1 no-wrap flex-1"):
                        ui.icon("videocam", size="xs").classes("text-zinc-400 shrink-0")
                        ui.label(cam).classes("text-zinc-300 truncate")
                    ui.badge(str(count), color="zinc-700").classes("text-xs text-zinc-400 q-px-1 shrink-0")

        # === RICH TAGS (collapsible) ===
        with ui.expansion("RICH TAGS", icon="tag", value=False).classes("w-full text-xs font-semibold text-zinc-400 tracking-wider q-py-none min-h-[36px]").props('header-class="text-zinc-400" dense'):
            rich_tags_section(state, on_refresh=get_refresh("rich_tags"))


def create_left_drawer(main_layout_state: Any) -> ui.left_drawer:
    left_drawer = ui.left_drawer(value=True, fixed=True, elevated=False).props('width=300 behavior=desktop bordered')
    left_drawer.classes('bg-[#111112] text-zinc-300 border-r border-zinc-800/40 q-pa-none overflow-hidden')
    
    with left_drawer:
        # Use q-pa-none here to stop the drawer from pushing content inward
        with ui.scroll_area().classes('w-full h-full q-pa-none'):
            # Only ONE call here
            left_drawer_content(main_layout_state)
    return left_drawer