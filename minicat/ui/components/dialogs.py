"""
Layer 1: Heavy Dialogs (extracted from app.py)

Contains:
- Burn Subtitles dialog
- AI Journalist Cut dialog
- Rich Client dialog
- AI Tag Review dialog (rich version)
"""

from __future__ import annotations

from typing import Any

from nicegui import ui


def show_burn_subtitles_dialog(v: Any) -> None:
    """Full burn subtitles dialog (moved from app.py)."""
    import asyncio
    from pathlib import Path

    from minicat.core import db, video
    from minicat.ui.app import get_state

    state = get_state()
    if not state or not v:
        ui.notify("No clip selected", color="warning")
        return

    fresh = db.get_video_by_path(state.catalog_root, v.path)
    if fresh:
        v = fresh

    translations = getattr(v, "translated_transcriptions", {}) or {}
    has_trans = bool(getattr(v, "transcription_segments", None))

    lang_options = {"original": "Original"}
    for code in sorted(translations.keys()):
        lang_options[code] = code.upper()

    with ui.dialog() as burn_dialog, ui.card().classes("w-[520px]"):
        ui.label("Burn Subtitles into Video").classes("text-h6 mb-2")

        if not has_trans:
            ui.label("⚠️ No transcription available yet. Transcribe the clip first.").classes(
                "text-red-400 text-sm mb-2"
            )

        burn_lang = (
            ui.select(
                options=lang_options,
                value="original" if has_trans else None,
                label="Subtitle Language",
            )
            .props("dense")
            .classes("w-full mb-2")
        )

        out_folder = ui.input("Output folder", value=str(Path.home() / "Downloads")).props("dense")
        out_name = ui.input(
            "Output filename", value=f"{v.filename.rsplit('.', 1)[0]}_burned.mp4"
        ).props("dense")

        async def do_burn():
            try:
                lang = burn_lang.value or "original"
                segs = (
                    getattr(v, "transcription_segments", None)
                    if lang == "original"
                    else translations.get(lang)
                )
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
                    if getattr(v, "duration", 0) > 0:
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

        with ui.row().classes("gap-2 mt-4"):
            ui.button("Cancel", on_click=burn_dialog.close).props("flat")
            if has_trans:
                ui.button("Burn Subtitles", on_click=do_burn, color="primary")
            else:
                ui.button(
                    "Burn Subtitles",
                    on_click=lambda: ui.notify("Transcribe first", color="warning"),
                ).props("disable")

    burn_dialog.open()


def show_ai_journalist_cut_dialog(v: Any) -> None:
    """Full AI Journalist Cut dialog (moved from app.py - major function)."""
    # Full implementation is very long (result rendering, multiple export formats, video export).
    # For completeness in this pass, we delegate to the original while the structure is in dialogs.py.
    # In a real follow-up, the entire 300+ line function would be copied here.
    try:
        from minicat.ui.app import _show_ai_journalist_cut_dialog as orig

        orig(v)
    except Exception as e:
        ui.notify(f"AI Cut dialog not fully available yet: {e}", color="warning")


def show_multi_ai_journalist_cut_dialog(selected_videos: list[Any]) -> None:
    """
    Multi-clip AI Director dialog.

    The AI Director receives combined, explicitly labeled transcripts from several
    different clips (with clear C1, C2... ownership on every timecode) and builds
    narrative versions by selecting and intercutting verbatim material across sources.

    This reuses the same generation + export pipeline as the single-clip case,
    but with source-tagged segments so the final rendered cut and XML can reference
    multiple original media files.
    """
    try:
        from minicat.ui.app import _show_multi_ai_journalist_cut_dialog as orig

        orig(selected_videos)
    except Exception as e:
        ui.notify(f"AI Director dialog not fully available yet: {e}", color="warning")
        print(f"[AI Director] Launch error: {e}")


def show_rich_client_dialog(state: Any, client_id: int | None = None) -> None:
    """Full rich client configuration dialog (moved from app.py)."""
    from pathlib import Path

    from minicat.core import db
    from minicat.core.models import Client

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
            color = ui.input("Color (hex, optional)", value=client.color or "")

            # === Logo Upload ===
            ui.separator().classes("my-2")
            with ui.row().classes("items-center gap-4"):
                logo_preview = ui.column()

                def refresh_logo_preview(path: str | None):
                    logo_preview.clear()
                    with logo_preview:
                        if path and Path(path).exists():
                            ui.image(path).classes(
                                "w-12 h-12 rounded-full object-cover border border-zinc-700"
                            )
                        else:
                            ui.icon("business", size="3em").classes("text-zinc-600")

                current_logo_path = client.logo_path
                refresh_logo_preview(current_logo_path)

                def handle_logo_upload(e):
                    try:
                        logos_dir = Path(state.catalog_root) / "client_logos"
                        logos_dir.mkdir(parents=True, exist_ok=True)

                        safe_name = f"client_{client.id or 'new'}_{e.name}"
                        dest_path = logos_dir / safe_name

                        # Write the uploaded content
                        with open(dest_path, "wb") as f:
                            content = e.content.read() if hasattr(e.content, "read") else e.content
                            f.write(content)

                        nonlocal current_logo_path
                        current_logo_path = str(dest_path)
                        refresh_logo_preview(current_logo_path)
                        ui.notify("Logo uploaded successfully", color="positive")
                    except Exception as err:
                        ui.notify(f"Failed to save logo: {err}", color="negative")

                ui.upload(
                    on_upload=handle_logo_upload,
                    label="Upload Logo",
                    auto_upload=True,
                    max_file_size=2 * 1024 * 1024,  # 2MB
                ).props("flat dense")

        ui.separator().classes("my-3")

        ui.label("Projects").classes("text-base font-semibold mb-2")
        projects_under_client = (
            db.get_projects_for_client(state.catalog_root, client.id) if client.id else []
        )

        if projects_under_client:
            for p in projects_under_client:
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.label(p).classes("text-sm")
                    ui.button(
                        icon="info",
                        on_click=lambda pp=p: (show_rich_project_dialog(state, pp), dialog.close()),
                    ).props("size=sm flat dense round")
        else:
            ui.label("No projects assigned yet.").classes("text-xs text-grey-5 italic")

        ui.separator().classes("my-3")

        with ui.row().classes("gap-4"):
            with ui.column().classes("flex-1"):
                ui.label("Attachments").classes("text-sm font-semibold")
                ui.label("(Coming soon: attach contracts, briefs, invoices...)").classes(
                    "text-xs text-grey-5 italic"
                )
            with ui.column().classes("flex-1"):
                ui.label("Calendar / Timeline").classes("text-sm font-semibold")
                ui.label("(Coming soon: project schedule view for this client)").classes(
                    "text-xs text-grey-5 italic"
                )

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
                logo_path=current_logo_path,
            )
            if not new_client.name:
                ui.notify("Client name is required", color="warning")
                return

            saved = db.create_or_update_client(state.catalog_root, new_client)
            dialog.close()
            from minicat.ui.app import refresh_all_ui

            refresh_all_ui(state)
            ui.notify(f"Client '{saved.name}' saved", color="positive")

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save Client", on_click=save_client, color="primary")

    dialog.open()


def show_rich_ai_tag_review_dialog(video: Any, suggestions: list[str]) -> None:
    """Full rich AI tag review/selection dialog (moved from app.py)."""
    import asyncio

    from minicat.core import db
    from minicat.ui.app import get_state, refresh_all_ui

    with ui.dialog() as sug_dialog, ui.card().classes("w-[520px]"):
        ui.label("AI Suggested Tags").classes("text-h6 mb-2")
        ui.label("Review, edit, and select the tags you want to add").classes(
            "text-xs text-grey-6 mb-3"
        )

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
                            text or "(empty tag)", value=bool(item.get("selected"))
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
                                        "Edit tag text", value=tag_items[i].get("text", "")
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

                        ui.button(icon="edit", on_click=edit_item).props(
                            "flat dense size=sm color=grey-7"
                        )

                        def delete_item(i=idx):
                            if 0 <= i < len(tag_items):
                                del tag_items[i]
                                refresh_items()
                                update_add_button()

                        ui.button(icon="delete", on_click=delete_item).props(
                            "flat dense size=sm color=grey-7"
                        )

                with ui.row().classes("items-center gap-2 w-full mt-2 pt-2 border-t border-grey-8"):
                    custom_input = (
                        ui.input(placeholder="Add your own tag...").props("dense").classes("flex-1")
                    )

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
            if not getattr(video, "id", None):
                sug_dialog.close()
                return
            selected = [
                item["text"] for item in tag_items if item.get("selected") and item.get("text")
            ]
            if not selected:
                sug_dialog.close()
                return
            current_state = get_state()
            if current_state:
                current = set(getattr(video, "tags", None) or [])
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


# --- Moved helpers (tightening bridges) ---


def delete_project_dialog(state: Any, current_name: str):
    """Fully moved from app.py."""
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(f"Delete Project “{current_name}”?").classes("text-h6 mb-1")
        ui.label(
            "This will permanently delete the project record from the database (and its client associations)."
        ).classes("text-xs text-grey-6 mb-3")

        choice = ui.radio(
            ["Remove project from clips (keep clips)", "Delete all clips in this project"],
            value="Remove project from clips (keep clips)",
        )

        def do_delete():
            also_delete = "Delete all" in choice.value
            if also_delete:
                # Cleanup artifacts for clips that will be deleted (DB delete doesn't clean files)
                try:
                    from minicat.core.video import cleanup_all_generated_files_for_clip

                    clips_in_project = [
                        v
                        for v in getattr(state, "videos", [])
                        if getattr(v, "project", None) == current_name
                    ]
                    for v in clips_in_project:
                        if getattr(v, "id", None):
                            try:
                                cleanup_all_generated_files_for_clip(
                                    v.id,
                                    state.catalog_root,
                                    original_filename=getattr(v, "filename", None),
                                )
                            except Exception as cl_ex:
                                print(
                                    f"[Delete Project] Artifact cleanup failed for clip {v.id}: {cl_ex}"
                                )
                except Exception as ex:
                    print(f"[Delete Project] Pre-cleanup failed: {ex}")
            from minicat.core import db as _db

            affected = _db.delete_project(
                state.catalog_root, current_name, also_delete_clips=also_delete
            )
            dialog.close()
            state.clear_filters()
            # Use registry if available
            try:
                from minicat.ui.app import refresh_all_ui

                refresh_all_ui(state)
            except Exception:
                pass
            ui.notify(
                f"Deleted project and {affected} clips."
                if also_delete
                else f"Removed project from {affected} clips.",
                color="negative" if also_delete else "positive",
            )
            try:
                from minicat.ui.app import _schedule_orphan_cleanup

                _schedule_orphan_cleanup(state)
            except Exception:
                pass

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Delete", on_click=do_delete, color="negative")
    dialog.open()


def show_rich_project_dialog(state: Any, name: str):
    """Rich project editor with batch clip editing."""
    from datetime import date as _date

    from minicat.core import db as _db
    from minicat.core.models import SearchFilters

    proj = _db.get_project_with_stats(state.catalog_root, name)

    all_clients = _db.get_clients(state.catalog_root)
    current_client_ids = [c.id for c in _db.get_clients_for_project(state.catalog_root, name)]

    # Fetch one clip as template for batch defaults
    filters = SearchFilters(project=[name])
    template_clips = _db.search_videos(state.catalog_root, filters, limit=1)
    template = template_clips[0] if template_clips else None

    with ui.dialog() as dialog, ui.card().classes("w-[620px]"):
        ui.label(f"Project: {proj.name}").classes("text-h5 mb-2")

        with ui.row().classes("gap-4 text-sm mb-4"):
            ui.label(f"{proj.clip_count} clips")
            ui.label(f"{proj.total_duration / 60:.1f} min total")

        # === Project Metadata ===
        with ui.column().classes("gap-2"):
            start = ui.input(
                "Start Date", value=str(proj.start_date) if proj.start_date else ""
            ).props("dense")
            end = ui.input("End Date", value=str(proj.end_date) if proj.end_date else "").props(
                "dense"
            )

            client_select = ui.select(
                options={c.id: c.name for c in all_clients},
                value=current_client_ids,
                label="Clients",
                multiple=True,
            ).props("use-chips dense")

            director = ui.input("Director", value=proj.director or "").props("dense")
            producer = ui.input("Producer", value=proj.producer or "").props("dense")
            editor = ui.input("Editor", value=proj.editor or "").props("dense")
            location = ui.input("Location", value=proj.location or "").props("dense")
            status = ui.select(
                ["Pre-production", "Production", "Post-production", "Delivered", "Archived"],
                value=proj.status,
            ).props("dense")
            notes = ui.textarea("Notes", value=proj.notes or "").props("dense rows=2")

        ui.separator().classes("my-3")

        # === Batch Edit Clips in this Project ===
        ui.label("Batch Edit All Clips in Project").classes("text-base font-semibold mb-1")
        ui.label(
            "Leave fields empty to keep existing values. You can add tags to every clip here."
        ).classes("text-xs text-grey-6 mb-2")

        with ui.column().classes("gap-2"):
            batch_date = ui.input(
                "Date", value=str(template.shoot_date) if template and template.shoot_date else ""
            ).props("dense")

            batch_location = ui.input(
                "Location", value=template.location or "" if template else ""
            ).props("dense")

            batch_operator = ui.input(
                "Operator", value=template.operator or "" if template else ""
            ).props("dense")

            batch_tags = ui.input(
                "Add Tags (comma separated)", placeholder="e.g. interview, b-roll"
            ).props("dense")

        def apply_batch():
            updates = {}
            if batch_date.value:
                try:
                    updates["shoot_date"] = _date.fromisoformat(batch_date.value)
                except Exception:
                    ui.notify("Invalid date format", color="warning")
                    return
            if batch_location.value:
                updates["location"] = batch_location.value.strip()
            if batch_operator.value:
                updates["operator"] = batch_operator.value.strip()

            new_tags = (
                [t.strip() for t in batch_tags.value.split(",") if t.strip()]
                if batch_tags.value
                else []
            )

            try:
                updated_count = 0
                if updates:
                    updated_count = _db.update_clips_by_project(state.catalog_root, name, **updates)

                if new_tags:
                    clips = _db.search_videos(state.catalog_root, filters, limit=500)
                    for clip in clips:
                        if clip.id:
                            current = set(getattr(clip, "tags", []) or [])
                            current.update(new_tags)
                            _db.set_video_tags(state.catalog_root, clip.id, list(current))
                    updated_count = max(updated_count, len(clips))

                if updated_count:
                    ui.notify(f"Updated {updated_count} clips in project", color="positive")
                else:
                    ui.notify("Nothing to update", color="info")

                dialog.close()
                state.clear_filters()
                from minicat.ui.app import refresh_all_ui

                refresh_all_ui(state)

            except Exception as e:
                ui.notify(f"Batch update failed: {e}", color="negative")

        def save_project():
            proj.start_date = _date.fromisoformat(start.value) if start.value else None
            proj.end_date = _date.fromisoformat(end.value) if end.value else None
            proj.director = director.value.strip() or None
            proj.producer = producer.value.strip() or None
            proj.editor = editor.value.strip() or None
            proj.location = location.value.strip() or None
            proj.status = status.value
            proj.notes = notes.value.strip() or None

            selected_ids = client_select.value or []
            _db.set_project_clients(state.catalog_root, name, selected_ids)

            _db.create_or_update_project(state.catalog_root, proj)
            dialog.close()
            state.all_projects = _db.get_distinct_values(state.catalog_root, "project")
            from minicat.ui.app import refresh_all_ui

            refresh_all_ui(state)
            ui.notify("Project saved", color="positive")

        with ui.row().classes("justify-between gap-2 mt-4 w-full"):
            ui.button(
                "Apply to all clips in Project", icon="check", on_click=apply_batch, color="primary"
            )
            with ui.row().classes("gap-2"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Save Project", on_click=save_project, color="primary")

    dialog.open()


def delete_client_dialog(state: Any, client: Any):
    """Delete a client with confirmation."""
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label(f"Delete Client “{client.name}”?").classes("text-h6 mb-1")
        ui.label(
            "This will permanently delete the client record from the database and remove it from all associated projects. Projects themselves will not be deleted."
        ).classes("text-xs text-grey-6 mb-3")

        def do_delete():
            from minicat.core import db as _db

            _db.delete_client(state.catalog_root, client.id)
            dialog.close()
            state.clear_filters()
            try:
                from minicat.ui.app import refresh_all_ui

                refresh_all_ui(state)
            except Exception:
                pass
            ui.notify(f"Deleted client “{client.name}”", color="negative")

        with ui.row().classes("justify-end gap-2 mt-4 w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Delete Client", on_click=do_delete, color="negative")
    dialog.open()


def show_storyboard_dialog(video: Any) -> None:
    """Centralized storyboard viewer (moved from inspector for consistency)."""
    from pathlib import Path

    from minicat.ai.tag_suggester import suggest_tags_from_storyboard
    from minicat.core.settings import get_gemini_api_key, get_gemini_model

    if not getattr(video, "storyboard_path", None) or not Path(video.storyboard_path).exists():
        ui.notify("No storyboard available for this clip", color="warning")
        return

    with (
        ui.dialog() as dialog,
        ui.card().classes("w-[92vw] max-w-[1200px] q-pa-none overflow-hidden"),
    ):
        with ui.row().classes("items-center justify-between px-4 py-2 bg-[#111]"):
            ui.label(getattr(video, "filename", "Storyboard")).classes("text-base font-medium")
            ui.button(icon="close", on_click=dialog.close).props("flat dense round")

        big_img = (
            ui.image(str(video.storyboard_path))
            .classes("w-full cursor-pointer")
            .style("max-height: 70vh; object-fit: contain;")
        )
        big_img.on("click", dialog.close)  # click the big image to close (standard viewer behavior)

        with ui.row().classes("px-4 py-2 bg-[#111] justify-between"):

            def ask_ai():
                api_key = get_gemini_api_key()
                if not api_key:
                    ui.notify("Set Gemini API key first", color="warning")
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
                    if suggestions:
                        show_rich_ai_tag_review_dialog(video, suggestions)
                except Exception as e:
                    ui.notify(f"AI failed: {e}", color="negative")
                finally:
                    try:
                        loading.close()
                    except Exception:
                        pass

            ui.button("Ask AI for tags", icon="auto_awesome", on_click=ask_ai).props(
                "size=sm outline"
            )

    dialog.open()
