"""CAT+TAG command-line interface (Typer + Rich).

Real functionality is being wired in progressively. The add / search commands
already exercise the excellent DB + search engine (date / project / location / camera).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from minicat import __version__
from minicat.core import config, db, video
from minicat.core.models import SearchFilters, Video

app = typer.Typer(
    name="minicat",
    help="CAT+TAG — Catalog, tag, and visually browse your video footage locally.",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


@app.command()
def version() -> None:
    """Show CAT+TAG version."""
    console.print(f"[bold]CAT+TAG[/] [cyan]{__version__}[/]")


@app.command()
def create(
    catalog_path: Path = typer.Argument(
        ...,
        exists=False,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Path to new catalog folder (will be created).",
    ),
) -> None:
    """Create a new CAT+TAG catalog (initializes DB + preview folders)."""
    root = config.resolve_catalog(catalog_path)
    console.print(f"[green]✓[/] Catalog ready at [bold]{root}[/]")
    console.print("You can now add videos with structured labels:")
    console.print(
        '  [cyan]minicat add /path/to/clip.mov --project "Cessibon" '
        '--camera "Sony FX3" --location "Helsinki" --date 2025-03-12[/]'
    )


@app.command()
def open(  # noqa: A001
    catalog_or_story: Path | None = typer.Argument(
        None,
        exists=False,
        file_okay=True,
        dir_okay=True,
        help="Optional path to a catalog folder. Or pass a saved AIStory_*.json directly to open it for rendering narrations (TTS voiceovers) + XML export.",
    ),
    native: bool = typer.Option(
        True,
        "--native/--browser",
        "-n/-b",
        help="Launch as native desktop window (recommended) or in browser.",
    ),
    new_workspace: bool = typer.Option(
        False,
        "--new",
        "-N",
        help="Launch the experimental new clean three-column workspace (v1 attempt).",
    ),
) -> None:
    """
    Launch CAT+TAG as a visual application.

    If you don't pass a catalog path, the app uses `~/CAT+TAG` by default on first launch
    (the folder is created automatically). Use the top-bar folder icon to switch catalogs later.

    You can also pass a saved AI Director story JSON (e.g. the AIStory_....json you saved)
    directly: `minicat open /path/to/AIStory_....json`
    This will launch the app (using your last catalog or the wizard) and immediately
    let you render the Narrations (generate voiceover MP3s) and produce the XML.
    """
    if native:
        try:
            from minicat.ui.desktop import launch_desktop
        except ImportError as e:
            console.print("[red]pywebview is required for the desktop app.[/]")
            console.print("Run: [cyan]uv sync[/]")
            raise typer.Exit(1) from e

        if new_workspace:
            import os

            os.environ["CAT_TAG_USE_NEW_WORKSPACE"] = "1"
            console.print("[bold cyan]Launching experimental new clean workspace (v1)[/]")

        if catalog_or_story:
            p = Path(catalog_or_story)
            if p.is_file() and p.suffix.lower() == ".json":
                # Direct story load: user wants to render narrations & make XML from this saved AI project
                console.print(
                    f"[cyan]Launching CAT+TAG desktop app to load story for narration render + XML[/] → {p}"
                )
                import os

                os.environ["CAT_TAG_INITIAL_STORY"] = str(p)
                launch_desktop(
                    None, title="CAT+TAG"
                )  # will use last catalog (or wizard); story auto-loads after
            else:
                console.print(f"[cyan]Launching CAT+TAG desktop app[/] → {catalog_or_story}")
                launch_desktop(str(catalog_or_story), title="CAT+TAG")
        else:
            console.print(
                "[cyan]Launching CAT+TAG desktop app[/] (will show catalog wizard if needed)"
            )
            launch_desktop(None, title="CAT+TAG")  # triggers first-launch setup screen
    else:
        from minicat.ui.app import run_web

        run_web(str(catalog_or_story) if catalog_or_story else None)


@app.command()
def add(
    video_path: Path = typer.Argument(..., exists=True, dir_okay=False),
    catalog: Path = typer.Option(
        ...,
        "--catalog",
        "-c",
        exists=True,
        file_okay=False,
        help="Path to your CAT+TAG catalog folder",
    ),
    project: str | None = typer.Option(None, "--project", "-p"),
    location: str | None = typer.Option(None, "--location", "-l"),
    camera: str | None = typer.Option(None, "--camera", "-cam"),
    shoot_date_str: str | None = typer.Option(
        None, "--date", "-d", help="Shoot date in YYYY-MM-DD format"
    ),
    tags: list[str] = typer.Option([], "--tag", "-t", help="Can be repeated"),
) -> None:
    """Add a single video with full structured metadata (project, location, camera, date)."""
    root = config.resolve_catalog(catalog)
    meta = video.extract_metadata(video_path)
    fps = meta.get("fps")
    if fps and float(fps) > 0:
        console.print(
            f"[Import] Framerate confirmed at import: {fps} fps (probed live from {video_path.name}; will be immutable)"
        )
    else:
        console.print(
            f"[Import] WARNING: could not confirm framerate for {video_path.name} (will default later)"
        )

    tc_start = meta.get("tc_start")
    if tc_start:
        console.print(f"[Import] Real embedded start timecode: {tc_start}")

    shoot_date: date | None = None
    if shoot_date_str:
        try:
            shoot_date = date.fromisoformat(shoot_date_str)
        except ValueError:
            console.print(
                f"[red]Invalid date format for --date: {shoot_date_str}. Use YYYY-MM-DD.[/]"
            )
            raise typer.Exit(1)

    v = Video(
        path=str(video_path.resolve()),
        filename=video_path.name,
        size=meta.get("size"),
        duration=meta.get("duration"),
        width=meta.get("width"),
        height=meta.get("height"),
        fps=fps,
        codec=meta.get("codec"),
        bit_rate=meta.get("bit_rate"),
        audio_channels=meta.get("audio_channels"),
        project=project,
        location=location,
        camera=camera or meta.get("camera"),
        operator=None,
        lens=None,
        shoot_date=shoot_date,
        tc_start=meta.get("tc_start"),
        tc_end=meta.get("tc_end"),
    )

    # Fast fingerprint for duplicate detection
    v.fingerprint = video.fast_fingerprint(video_path, duration=v.duration)

    vid = db.add_video(root, v)

    # Automatic tags (resolution for video, "audio" for audio files)
    is_audio = _is_audio_file(video_path)
    auto_tags = video.get_auto_import_tags(meta, is_audio=is_audio)
    final_tags = list(
        dict.fromkeys((tags or []) + auto_tags)
    )  # user tags first, then auto, deduped
    if final_tags:
        db.set_video_tags(root, vid, final_tags)

    console.print(f"[green]✓[/] Added [bold]{video_path.name}[/] (id={vid})")
    if project or camera or location:
        console.print(
            f"   Labels: project={project}  camera={camera or meta.get('camera')}  location={location}"
        )
    console.print(
        f"   Duration: {v.duration:.1f}s  Resolution: {v.width}x{v.height}" if v.duration else ""
    )


# Broad list for professional and consumer footage (we rely on ffmpeg for actual decoding)
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".avi",
    ".webm",
    ".mts",
    ".m2ts",
    ".3gp",
    ".mxf",
    ".braw",
    ".r3d",
    ".dnxhd",
    ".dnxhr",
    ".exr",
    ".dpx",
    ".ari",
    ".vob",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2t",
    ".m2ts",
    ".m2v",
    ".flv",
    ".f4v",
    ".asf",
    ".wmv",
    ".ogv",
    ".ogg",
    ".qt",
}


def _is_video_file(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXTENSIONS


# Audio formats supported for direct import into the Library (use generic waveform icon)
AUDIO_EXTENSIONS = {
    ".wav",
    ".wave",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".oga",
    ".aiff",
    ".aif",
    ".aifc",
    ".wma",
    ".opus",
    ".amr",
    ".ac3",
    ".mid",
    ".midi",
}


def _is_audio_file(p: Path) -> bool:
    return p.suffix.lower() in AUDIO_EXTENSIONS


def _is_supported_file(p: Path) -> bool:
    return _is_video_file(p) or _is_audio_file(p)


@app.command()
def scan(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    catalog: Path = typer.Option(..., "--catalog", "-c", exists=True, file_okay=False),
    project: str | None = typer.Option(None, "--project", "-p"),
    camera: str | None = typer.Option(None, "--camera", "-cam"),
    location: str | None = typer.Option(None, "--location", "-l"),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", "-r"),
    limit: int | None = typer.Option(
        None, "--limit", help="Stop after N files (useful while testing)"
    ),
) -> None:
    """
    Recursively find video and audio files in a folder (your Premiere project folders,
    music libraries, external drives, etc.) and add them with the supplied default
    structured labels.

    Audio files are imported with a generic waveform icon as thumbnail.
    This is the primary way to bootstrap large catalogs quickly.
    """
    root = config.resolve_catalog(catalog)
    console.print(f"Scanning [bold]{folder}[/] (recursive={recursive})...")

    files: list[Path] = []
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    for p in iterator:
        if p.is_file() and _is_supported_file(p):
            files.append(p)

    if limit:
        files = files[:limit]

    console.print(f"Found {len(files)} media file(s) (video + audio)")

    added = skipped = errors = 0
    for f in files:
        if db.get_video_by_path(root, f):
            skipped += 1
            continue
        try:
            meta = video.extract_metadata(f)
            fps = meta.get("fps")
            if fps and float(fps) > 0:
                console.print(
                    f"[Import] Framerate confirmed at import: {fps} fps (probed live from {f.name}; will be immutable)"
                )
            else:
                console.print(f"[Import] WARNING: could not confirm framerate for {f.name}")

            # Enrich from camera sidecar XML if present (CLI import is explicit, so we take the ExifTool cost)
            xml_meta = video.extract_camera_xml_metadata(f, enrich_with_exiftool=True)
            if xml_meta:
                if xml_meta.get("camera") and not camera:
                    meta["camera"] = xml_meta["camera"]
                if xml_meta.get("lens"):
                    # set below
                    pass
                if xml_meta.get("codec"):
                    meta["codec"] = xml_meta["codec"]

            v = Video(
                path=str(f.resolve()),
                filename=f.name,
                size=meta.get("size"),
                duration=meta.get("duration"),
                width=meta.get("width"),
                height=meta.get("height"),
                fps=fps,
                codec=meta.get("codec"),
                bit_rate=meta.get("bit_rate"),
                audio_channels=meta.get("audio_channels"),
                project=project,
                location=location,
                camera=camera or meta.get("camera"),
                operator=None,
                lens=xml_meta.get("lens") if xml_meta else None,
                camera_xml_path=xml_meta.get("source_xml") if xml_meta else None,
                iso=xml_meta.get("iso") if xml_meta else None,
                f_number=xml_meta.get("f_number") if xml_meta else None,
                shutter_speed=xml_meta.get("shutter_speed") if xml_meta else None,
                focal_length=xml_meta.get("focal_length") if xml_meta else None,
                white_balance=xml_meta.get("white_balance") if xml_meta else None,
                tc_start=meta.get("tc_start"),
                tc_end=meta.get("tc_end"),
            )
            v.fingerprint = video.fast_fingerprint(f, duration=v.duration)
            video_id = db.add_video(root, v)
            added += 1

            # Automatic tags on import (resolution for video, "audio" for audio)
            try:
                is_audio = _is_audio_file(f)
                auto_tags = video.get_auto_import_tags(meta, is_audio=is_audio)
                if auto_tags:
                    db.set_video_tags(root, video_id, auto_tags)
            except Exception as tag_err:
                if added < 5:
                    console.print(f"[yellow]Warning[/] Auto-tagging failed for {f.name}: {tag_err}")

            if added % 20 == 0:
                console.print(f"  ... {added} media files added")
        except Exception as exc:
            errors += 1
            if errors < 5:
                console.print(f"[red]Error[/] {f.name}: {exc}")

    console.print(
        f"[green]✓[/] Scan finished — {added} added, {skipped} already present, {errors} errors."
    )


@app.command()
def search(
    text: str | None = typer.Argument(None, help="Free-text search (filename, notes, path)"),
    catalog: Path = typer.Option(..., "--catalog", "-c", exists=True),
    project: list[str] = typer.Option([], "--project", "-p"),
    location: list[str] = typer.Option([], "--location", "-l"),
    camera: list[str] = typer.Option([], "--camera", "-cam"),
    after_str: str | None = typer.Option(None, "--after", help="YYYY-MM-DD"),
    before_str: str | None = typer.Option(None, "--before", help="YYYY-MM-DD"),
    tag: list[str] = typer.Option([], "--tag", "-t"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Powerful search with excellent support for date, project, location, camera."""
    root = config.resolve_catalog(catalog)

    after: date | None = None
    before: date | None = None

    if after_str:
        try:
            after = date.fromisoformat(after_str)
        except ValueError:
            console.print(f"[red]Invalid date for --after: {after_str}. Use YYYY-MM-DD.[/]")
            raise typer.Exit(1)

    if before_str:
        try:
            before = date.fromisoformat(before_str)
        except ValueError:
            console.print(f"[red]Invalid date for --before: {before_str}. Use YYYY-MM-DD.[/]")
            raise typer.Exit(1)

    filters = SearchFilters(
        text=text,
        project=project or None,
        location=location or None,
        camera=camera or None,
        date_from=after,
        date_to=before,
        tags=tag or None,
    )
    results = db.search_videos(root, filters, limit=limit)

    if not results:
        console.print("[yellow]No matches.[/]")
        return

    table = Table(title=f"CAT+TAG Search — {len(results)} result(s)")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Date", style="green")
    table.add_column("Project")
    table.add_column("Camera")
    table.add_column("Location")
    table.add_column("Duration", justify="right")
    table.add_column("File")

    for v in results:
        table.add_row(
            str(v.id),
            str(v.shoot_date) if v.shoot_date else "",
            v.project or "",
            v.camera or "",
            v.location or "",
            f"{v.duration:.1f}s" if v.duration else "",
            v.filename,
        )
    console.print(table)


@app.command()
def tags(
    catalog: Path = typer.Option(..., "--catalog", "-c", exists=True),
) -> None:
    """List all tags currently used in the catalog."""
    root = config.resolve_catalog(catalog)
    all_tags = db.get_all_tags(root)
    if not all_tags:
        console.print("[dim]No tags yet.[/]")
        return
    console.print("[bold]Tags in catalog:[/]")
    for t in all_tags:
        console.print(f"  • {t.name}")


# ---------------------------------------------------------------------------
# TEMPORARY EXPERIMENTAL COMMAND
# For testing the new audio-based AI Journalist cutter.
# This can be removed once testing is complete.
# ---------------------------------------------------------------------------
@app.command("test-ai-journalist-audio", hidden=True)
def test_ai_journalist_audio(
    audio_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to the audio file (wav, mp3, m4a, etc.)"
    ),
    max_duration: float = typer.Option(
        180.0, "--max-duration", "-d", help="Target maximum duration per version in seconds"
    ),
    min_duration: float = typer.Option(
        30.0, "--min-duration", help="Target minimum duration per version in seconds"
    ),
    purpose: str = typer.Option("News Package", "--purpose", "-p", help="Purpose of the cut"),
    tone: str = typer.Option(
        "rewrite", "--tone", "-t", help="Tone (rewrite recommended for testing)"
    ),
    versions: int = typer.Option(2, "--versions", "-n", help="Number of versions to generate"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Optional path to save full JSON results"
    ),
) -> None:
    """
    [EXPERIMENTAL] Test the new audio-based AI Journalist cutter.

    Sends the raw audio file to Gemini so the model can listen to tone,
    emotion, pacing and delivery instead of only using a transcript.

    This is a temporary testing command.
    """
    try:
        from minicat.ai.journalist_cutter import generate_journalist_cuts_from_audio
    except ImportError as e:
        console.print("[red]Failed to import the audio journalist cutter.[/]")
        console.print("Make sure google-genai is installed: [cyan]uv pip install google-genai[/]")
        raise typer.Exit(1) from e

    console.print("[bold cyan]Testing AI Journalist from AUDIO[/]")
    console.print(f"Audio: [bold]{audio_path}[/]")
    console.print(f"Max duration: {max_duration}s  |  Tone: {tone}  |  Versions: {versions}\n")

    try:
        results = generate_journalist_cuts_from_audio(
            audio_path=str(audio_path),
            max_duration_seconds=max_duration,
            min_duration_seconds=min_duration,
            purpose=purpose,
            tone=tone,
            num_versions=versions,
        )
    except Exception as ex:
        console.print(f"[bold red]Error generating cuts from audio:[/] {ex}")
        raise typer.Exit(1) from ex

    if not results:
        console.print("[yellow]No versions were generated.[/]")
        return

    for version in results:
        vid = version.get("version_id", "?")
        title = version.get("title", "Untitled")
        dur = version.get("total_duration", 0)
        summary = version.get("narrative_summary", "")

        console.print(f"[bold green]Version {vid} — {title}[/]")
        console.print(f"[dim]Duration: {dur:.1f}s[/]")
        if summary:
            console.print(f"[italic]{summary}[/]\n")

        segments = version.get("selected_segments", [])
        for i, seg in enumerate(segments, 1):
            sin = seg.get("source_in") or seg.get("start", 0)
            sout = seg.get("source_out") or seg.get("end", 0)
            txt = seg.get("text", "")[:120]
            reason = seg.get("reason", "")
            console.print(f"  {i}. [cyan]{sin:.1f}s → {sout:.1f}s[/]  {txt}")
            if reason:
                console.print(f"     [dim]→ {reason}[/]")
        console.print("")

    if output:
        import json

        output.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        console.print(f"[green]Full results saved to[/] {output}")

    console.print("[dim]Temporary test command. Remove when no longer needed.[/]")


# ---------------------------------------------------------------------------
# TEST COMMAND FOR EXPORTER #3 (Multi + Narration / Voiceover)
# Similar testing surface as the Gemini/AI Director flows.
# ---------------------------------------------------------------------------
@app.command("test-narrative-exporter", hidden=True)
def test_narrative_exporter_cli(
    with_voiceover: bool = typer.Option(
        True, "--with-voiceover/--no-voiceover", help="Generate actual voiceover MP3s"
    ),
    as_titles: bool = typer.Option(
        False,
        "--as-titles",
        help="Export narration bridges as visible text titles instead of audio",
    ),
    language: str = typer.Option("en", "--language", "-l", help="Voiceover language code"),
    output_dir: Path | None = typer.Option(
        None, "--output-dir", "-o", help="Where to write the XML (defaults to standard export dir)"
    ),
) -> None:
    """
    Test Exporter #3 (Multi-source AI Director with narration + optional voiceover).

    Creates fake multi-source data with narration bridges and exercises
    the full narrative_vo_exporter pipeline (XML + voiceover track or titles).
    """
    try:
        from minicat.ai.narrative_vo_exporter import export_narrative_vo_xmeml
        from minicat.ai.voiceover import get_tts_status
    except Exception as e:
        console.print(f"[red]Failed to import narrative exporter: {e}[/]")
        raise typer.Exit(1)

    console.print("[bold cyan]Testing Exporter #3 — Narrative + Voiceover[/]")
    console.print(
        f"Voiceover: {with_voiceover}  |  Titles mode: {as_titles}  |  Language: {language}"
    )

    status = get_tts_status()
    console.print(f"TTS status: {status['message']}")

    # Minimal fake version with narration
    fake_ver = {
        "version_id": "CLI-TEST-3",
        "title": "CLI Test Narrated Multi",
        "total_duration": 95.0,
        "narrative_summary": "Test export via CLI for exporter #3 validation.",
        "narrative_elements": [
            {
                "type": "clip",
                "source_label": "C1",
                "source_in": 10.0,
                "source_out": 25.0,
                "text": "We started with high hopes.",
            },
            {
                "type": "narration",
                "text": "This opening sets the optimistic tone before reality hit.",
            },
            {
                "type": "clip",
                "source_label": "C2",
                "source_in": 40.0,
                "source_out": 55.0,
                "text": "The budget constraints forced difficult choices.",
            },
            {"type": "narration", "text": "The official view was presented as unavoidable."},
        ],
        "selected_segments": [
            {
                "source_label": "C1",
                "source_in": 10.0,
                "source_out": 25.0,
                "text": "We started with high hopes.",
            },
            {
                "source_label": "C2",
                "source_in": 40.0,
                "source_out": 55.0,
                "text": "The budget constraints forced difficult choices.",
            },
        ],
        "narration_language": language,
    }

    try:
        result = export_narrative_vo_xmeml(
            fake_ver,
            generate_voiceover=with_voiceover and not as_titles,
            narration_as_titles=as_titles,
            voiceover_language=language,
        )
        if result:
            console.print(f"[green]✓ Exported successfully:[/] {result}")
        else:
            console.print("[yellow]Exporter returned None (check logs).[/]")
    except Exception as ex:
        console.print(f"[bold red]Export failed:[/] {ex}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# TEMPORARY COMPARISON COMMAND
# Runs both the classic transcript-based and the new audio-based
# AI Journalist cutters on the same file for direct comparison.
# ---------------------------------------------------------------------------
@app.command("compare-ai-journalist-audio", hidden=True)
def compare_ai_journalist_audio(
    audio_path: Path = typer.Argument(..., exists=True, dir_okay=False, help="Audio or video file"),
    max_duration: float = typer.Option(180.0, "--max-duration", "-d"),
    min_duration: float = typer.Option(30.0, "--min-duration"),
    purpose: str = typer.Option("News Package", "--purpose", "-p"),
    tone: str = typer.Option("rewrite", "--tone", "-t"),
    versions: int = typer.Option(2, "--versions", "-n"),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Save full comparison JSON here"
    ),
) -> None:
    """
    [EXPERIMENTAL] Compare transcript-based vs audio-based AI Journalist on the same file.

    This is a temporary testing tool to evaluate whether feeding raw audio
    to Gemini produces better narrative cuts than using the transcript.
    """
    try:
        from minicat.ai.journalist_cutter import (
            generate_journalist_cuts,
            generate_journalist_cuts_from_audio,
        )
        from minicat.ai.transcriber import transcribe_audio_with_timestamps
    except ImportError as e:
        console.print("[red]Missing dependencies for comparison.[/]")
        console.print("Make sure google-genai is installed.")
        raise typer.Exit(1) from e

    console.print("[bold magenta]Comparing AI Journalist methods[/]")
    console.print(f"File: [bold]{audio_path.name}[/]")
    console.print(
        f"Min: {min_duration}s | Max: {max_duration}s | Tone: {tone} | Versions: {versions}\n"
    )

    # Step 1: Transcribe (needed for the text-based path)
    console.print("[cyan]Step 1/3:[/] Transcribing audio for text-based cutter...")
    try:
        # ALWAYS confirm framerate of the associated video before transcription.
        # If a video file is provided or can be inferred next to the audio, probe it.
        fps = None
        from pathlib import Path

        from minicat.core.video import confirm_video_framerate, extract_metadata

        p = Path(audio_path)
        # Try sibling video with same stem
        for ext in (".MP4", ".mp4", ".MOV", ".mov", ".mxf"):
            candidate = p.with_suffix(ext)
            if candidate.exists():
                fps = confirm_video_framerate(candidate)
                console.print(f"  [fps] Confirmed {fps} fps from {candidate.name}")
                break
        if fps is None:
            # last resort: try to treat audio_path as video or use default via confirm (will warn)
            try:
                fps = confirm_video_framerate(audio_path)
            except Exception:
                fps = 25.0
        from minicat.core.settings import get_gemini_api_key

        api_key = get_gemini_api_key()
        if not api_key:
            console.print("[red]No Gemini API key configured. Cannot transcribe.[/]")
            raise typer.Exit(1)
        total_duration = None
        try:
            from minicat.core.video import extract_metadata

            if "candidate" in locals() and candidate and candidate.exists():
                total_duration = extract_metadata(candidate).get("duration")
            elif p.suffix.lower() in (".mp4", ".mov", ".mxf") and p.exists():
                total_duration = extract_metadata(audio_path).get("duration")
        except Exception:
            pass
        segments = transcribe_audio_with_timestamps(
            str(audio_path), api_key, fps=fps, total_duration=total_duration
        )
        console.print(f"  → Got {len(segments)} timed segments\n")
    except Exception as ex:
        console.print(f"[red]Transcription failed:[/] {ex}")
        raise typer.Exit(1) from ex

    # Step 2: Run text-based cutter
    console.print("[cyan]Step 2/3:[/] Running classic transcript-based AI Journalist...")
    try:
        text_versions = generate_journalist_cuts(
            segments,
            max_duration_seconds=max_duration,
            min_duration_seconds=min_duration,
            purpose=purpose,
            tone=tone,
            num_versions=versions,
            generate_narration=False,
            narration_style=None,
            narration_min_seconds=0,
            narration_max_seconds=0,
            narration_min_bridges=0,
            narration_max_bridges=0,
        )
        console.print(f"  → Generated {len(text_versions)} version(s)\n")
    except Exception as ex:
        console.print(f"[red]Text-based cutter failed:[/] {ex}")
        text_versions = []

    # Step 3: Run audio-based cutter
    console.print(
        "[cyan]Step 3/3:[/] Running new audio-based AI Journalist (listening to raw audio)..."
    )
    try:
        audio_versions = generate_journalist_cuts_from_audio(
            audio_path=str(audio_path),
            max_duration_seconds=max_duration,
            min_duration_seconds=min_duration,
            purpose=purpose,
            tone=tone,
            num_versions=versions,
            generate_narration=False,
            narration_style=None,
        )
        console.print(f"  → Generated {len(audio_versions)} version(s)\n")
    except Exception as ex:
        console.print(f"[red]Audio-based cutter failed:[/] {ex}")
        audio_versions = []

    # === Comparison output ===
    console.print("[bold]=== COMPARISON RESULTS ===[/]\n")

    def _print_versions(label: str, vers: list):
        console.print(f"[bold blue]{label}[/]")
        for v in vers:
            vid = v.get("version_id")
            title = v.get("title", "Untitled")
            dur = v.get("total_duration", 0)
            summary = v.get("narrative_summary", "")
            seg_count = len(v.get("selected_segments", []))
            console.print(f"  Version {vid}: [bold]{title}[/]  ({dur:.1f}s, {seg_count} segments)")
            if summary:
                console.print(f"    [dim]{summary[:160]}{'...' if len(summary) > 160 else ''}[/]")
        console.print("")

    _print_versions("TRANSCRIPT-BASED (classic)", text_versions)
    _print_versions("AUDIO-BASED (new experimental)", audio_versions)

    # Quick stats
    if text_versions and audio_versions:
        console.print("[bold]Quick observations:[/]")
        for i in range(min(len(text_versions), len(audio_versions))):
            t_dur = text_versions[i].get("total_duration", 0)
            a_dur = audio_versions[i].get("total_duration", 0)
            diff = a_dur - t_dur
            console.print(
                f"  Version {chr(65 + i)}: Transcript {t_dur:.1f}s vs Audio {a_dur:.1f}s  (diff: {diff:+.1f}s)"
            )

    if output:
        import json

        comparison = {
            "audio_file": str(audio_path),
            "max_duration": max_duration,
            "purpose": purpose,
            "tone": tone,
            "text_based": text_versions,
            "audio_based": audio_versions,
        }
        output.write_text(json.dumps(comparison, indent=2, ensure_ascii=False))
        console.print(f"\n[green]Full comparison saved to[/] {output}")

    console.print("\n[dim]Temporary comparison command. Remove when testing is complete.[/]")


@app.command(
    "txt-to-srt",
    help="Convert a per-clip transcription .txt (00000X_fi.txt or MN-..._fi.txt) to a source-style .srt (media-head timings, with real TC note in header). Useful for round-tripping after editing the human .txt.",
)
def txt_to_srt(
    txt: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="The _fi.txt (or _en.txt etc) transcription file"
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Where to write the .srt (default: same dir, same stem .srt)"
    ),
    fps: float = typer.Option(
        25.0, "--fps", help="Source fps for quantization (usually from the clip)"
    ),
    base_timecode: str | None = typer.Option(
        None,
        "--base-tc",
        help="Real embedded start timecode of the clip, e.g. 11:43:09:01 (for the header comment)",
    ),
) -> None:
    """Turn an edited or external per-clip transcript TXT into a usable source .srt."""
    try:
        from minicat.ai.transcriber import (
            parse_transcription_txt_to_segments,
            segments_to_srt,
            source_transcript_to_srt_segments,
        )
    except Exception as e:
        console.print(f"[red]Could not import transcription helpers: {e}[/]")
        raise typer.Exit(1)

    segs = parse_transcription_txt_to_segments(txt)
    if not segs:
        console.print("[yellow]No segments parsed from the TXT.[/]")
        raise typer.Exit(1)

    processed = source_transcript_to_srt_segments(segs, fps=fps)
    srt_text = segments_to_srt(
        processed,
        strict_timing=True,
        fps=fps,
        base_timecode=base_timecode,
    )

    if output is None:
        output = txt.with_suffix(".srt")
    output.write_text(srt_text, encoding="utf-8")
    console.print(f"[green]Wrote {len(segs)} utterances as source .srt[/] → {output}")
    console.print(
        "[dim]Header includes real TC note if --base-tc was given. Timings are media-head relative for sync.[/]"
    )


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        # Bare invocation → launch the regular (current stable) desktop app
        try:
            from minicat.ui.desktop import launch_desktop

            console.print("[cyan]Starting CAT+TAG desktop app...[/]")
            launch_desktop(None, title="CAT+TAG")
        except Exception as exc:
            import traceback

            console.print("[bold red]Failed to launch desktop app[/]")
            console.print(f"[red]Error:[/] {exc}")
            traceback.print_exc()
            console.print("\n[bold]CAT+TAG[/] — personal video catalog")
            console.print(f"Version [cyan]{__version__}[/]\n")
            console.print("Launch the visual app with: [cyan]minicat open[/]")
            console.print("Or see all commands: [cyan]minicat --help[/]")
            raise typer.Exit(1) from exc
