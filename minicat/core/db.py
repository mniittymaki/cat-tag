"""SQLite database layer for CAT+TAG.

Uses the built-in sqlite3 module (with FTS5) for maximum simplicity and reliability.
WAL mode for decent concurrency. All paths are stored as absolute paths.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from minicat.core.models import Client, Project, SearchFilters, Tag, Video

SCHEMA_VERSION = 11

# Core schema (videos + tags + join + FTS5)
SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER,
    fingerprint TEXT,
    duration REAL,
    width INTEGER,
    height INTEGER,
    fps REAL,
    codec TEXT,
    bit_rate INTEGER,
    audio_channels INTEGER,
    shoot_date TEXT,           -- ISO date (YYYY-MM-DD) for easy filtering
    project TEXT,
    location TEXT,
    camera TEXT,
    operator TEXT,
    lens TEXT,
    notes TEXT,
    tag_names TEXT,            -- denormalized (space-separated lowercased) tags for FTS search + content= compatibility
    thumbnail_path TEXT,
    storyboard_path TEXT,
    camera_xml_path TEXT,
    iso INTEGER,
    f_number REAL,
    shutter_speed TEXT,
    focal_length REAL,
    white_balance TEXT,
    import_date TEXT,
    last_seen TEXT,
    missing INTEGER DEFAULT 0,
    transcription TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS video_tags (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, tag_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    start_date TEXT,
    end_date TEXT,
    client TEXT,
    director TEXT,
    producer TEXT,
    editor TEXT,
    camera_operators TEXT,      -- JSON array
    location TEXT,
    status TEXT DEFAULT 'Production',
    color TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Rich Clients (one client can have many projects)
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    contact_person TEXT,
    email TEXT,
    phone TEXT,
    address TEXT,
    notes TEXT,
    color TEXT,
    logo_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Many-to-many link between clients and projects (a project can belong to multiple clients)
CREATE TABLE IF NOT EXISTS client_projects (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    PRIMARY KEY (client_id, project)
);

-- FTS5 virtual table for fast full-text search across important text fields
CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
    path, filename, notes, tag_names,
    content='videos',
    content_rowid='id'
);

-- Triggers to keep FTS in sync (simple approach)
CREATE TRIGGER IF NOT EXISTS videos_fts_insert AFTER INSERT ON videos BEGIN
    INSERT INTO videos_fts (rowid, path, filename, notes, tag_names)
    VALUES (new.id, new.path, new.filename, new.notes, COALESCE(new.tag_names, ''));
END;

CREATE TRIGGER IF NOT EXISTS videos_fts_delete AFTER DELETE ON videos BEGIN
    INSERT INTO videos_fts (videos_fts, rowid, path, filename, notes, tag_names)
    VALUES ('delete', old.id, old.path, old.filename, old.notes, COALESCE(old.tag_names, ''));
END;

CREATE TRIGGER IF NOT EXISTS videos_fts_update AFTER UPDATE ON videos BEGIN
    INSERT INTO videos_fts (videos_fts, rowid, path, filename, notes, tag_names)
    VALUES ('delete', old.id, old.path, old.filename, old.notes, COALESCE(old.tag_names, ''));
    INSERT INTO videos_fts (rowid, path, filename, notes, tag_names)
    VALUES (new.id, new.path, new.filename, new.notes, COALESCE(new.tag_names, ''));
END;
"""


@contextmanager
def get_connection(catalog_root: Path) -> Iterator[sqlite3.Connection]:
    """Context manager that yields a connection to the catalog's database.
    Uses a long busy timeout + WAL to reduce 'database is locked' errors
    when multiple operations (import + AI tagging + manual edits) happen.
    """
    db_path = catalog_root / "catalog.db"
    conn = sqlite3.connect(
        str(db_path),
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        timeout=60.0,   # Increased from 30s
    )
    conn.row_factory = sqlite3.Row

    # Ensure busy timeout is set at connection level too (in addition to PRAGMA)
    try:
        conn.execute("PRAGMA busy_timeout = 60000;")  # 60 seconds
    except Exception:
        pass

    try:
        yield conn
    finally:
        conn.close()


def init_catalog(catalog_root: Path) -> None:
    """Initialize (or upgrade) a catalog database at the given root."""
    catalog_root.mkdir(parents=True, exist_ok=True)
    (catalog_root / "previews" / "thumbs").mkdir(parents=True, exist_ok=True)
    (catalog_root / "previews" / "boards").mkdir(parents=True, exist_ok=True)
    (catalog_root / "audio").mkdir(parents=True, exist_ok=True)
    (catalog_root / "transcriptions").mkdir(parents=True, exist_ok=True)
    (catalog_root / "subtitles").mkdir(parents=True, exist_ok=True)

    with get_connection(catalog_root) as conn:
        conn.executescript(SCHEMA_SQL)

        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current_version = row[0] if row else 0

        if current_version < SCHEMA_VERSION:
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

        # Safety net: ensure all columns that have been added over time exist.
        # This protects old catalogs that may have incomplete history.
        for col, col_type in [
            ("bit_rate", "INTEGER"),
            ("audio_channels", "INTEGER"),
            ("operator", "TEXT"),
            ("lens", "TEXT"),
            ("camera_xml_path", "TEXT"),
            ("iso", "INTEGER"),
            ("f_number", "REAL"),
            ("shutter_speed", "TEXT"),
            ("focal_length", "REAL"),
            ("white_balance", "TEXT"),
            ("gamma", "TEXT"),
            ("color_primaries", "TEXT"),
            ("coding_equations", "TEXT"),
            ("transcription", "TEXT"),
            ("tag_names", "TEXT"),
            ("tc_start", "TEXT"),
            ("tc_end", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {col_type}")
            except Exception:
                pass

        # Safety net for clients table (added later: color + logo_path support)
        for col, col_type in [
            ("color", "TEXT"),
            ("logo_path", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE clients ADD COLUMN {col} {col_type}")
            except Exception:
                pass

        conn.commit()

    # FTS ensure must be *outside* the init connection to avoid "database is locked"
    # when ensure opens its own get_connection. This also guarantees the rebuild
    # that fixes "no such column: T.tag_names" runs on fresh conn.
    # Ensure FTS is consistent with videos.tag_names (fixes "no such column: T.tag_names" on stale FTS setups)
    # Rebuild is cheap and makes content= + tag search work even on old catalogs.
    ensure_fts_consistency(catalog_root)


def _row_to_video(row: sqlite3.Row, tag_names: list[str] | None = None) -> Video:
    data = dict(row)
    if data.get("shoot_date"):
        try:
            data["shoot_date"] = date.fromisoformat(data["shoot_date"])
        except ValueError:
            data["shoot_date"] = None
    for key in ("import_date", "last_seen"):
        if data.get(key):
            try:
                data[key] = datetime.fromisoformat(data[key])
            except ValueError:
                data[key] = None
    data["missing"] = bool(data.get("missing", 0))
    data["tags"] = tag_names or []

    # Parse transcription JSON if present (support both old and new formats)
    if data.get("transcription"):
        try:
            trans_data = json.loads(data["transcription"])
            if isinstance(trans_data, dict):
                original = trans_data.get("original")

                if isinstance(original, dict):
                    # New format: {"language": "en", "segments": [...] }
                    data["transcription_segments"] = original.get("segments")
                    if original.get("language"):
                        data["original_language"] = original["language"]
                else:
                    # Old format: original was directly the list of segments
                    data["transcription_segments"] = original

                data["translated_transcriptions"] = trans_data.get("translations", {})
        except Exception:
            pass

    return Video(**{k: v for k, v in data.items() if k in Video.model_fields})


def add_video(catalog_root: Path, video: Video) -> int:
    """Insert a new video record. Returns the new video id."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    with get_connection(catalog_root) as conn:
        cur = conn.execute(
            """
            INSERT INTO videos (
                path, filename, size, fingerprint, duration, width, height,
                fps, codec, bit_rate, audio_channels, shoot_date, project, location, camera, operator, lens, notes,
                tag_names,
                thumbnail_path, storyboard_path, camera_xml_path, iso, f_number, shutter_speed, focal_length, white_balance, gamma,
                color_primaries, coding_equations, tc_start, tc_end,
                import_date, last_seen, missing
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(video.path),
                video.filename,
                video.size,
                video.fingerprint,
                video.duration,
                video.width,
                video.height,
                video.fps,
                video.codec,
                video.bit_rate,
                video.audio_channels,
                video.shoot_date.isoformat() if video.shoot_date else None,
                video.project,
                video.location,
                video.camera,
                video.operator,
                video.lens,
                video.notes,
                '',  # tag_names (populated later via set_video_tags)
                str(video.thumbnail_path) if video.thumbnail_path else None,
                str(video.storyboard_path) if video.storyboard_path else None,
                str(video.camera_xml_path) if video.camera_xml_path else None,
                video.iso,
                video.f_number,
                video.shutter_speed,
                video.focal_length,
                video.white_balance,
                video.gamma,
                video.color_primaries,
                video.coding_equations,
                video.tc_start,
                video.tc_end,
                now,
                now,
                0,
            ),
        )
        video_id = cur.lastrowid
        conn.commit()
        return video_id


def get_video_by_path(catalog_root: Path, path: str | Path) -> Video | None:
    with get_connection(catalog_root) as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE path = ?", (str(path),)
        ).fetchone()
        if not row:
            return None
        tags = _get_tags_for_video(conn, row["id"])
        return _row_to_video(row, tags)


def get_videos_by_ids(catalog_root: Path, ids: list[int]) -> list[Video]:
    """Fetch full Video objects (with tags) for a list of IDs. Used for robust batch operations."""
    if not ids:
        return []
    with get_connection(catalog_root) as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM videos WHERE id IN ({placeholders}) ORDER BY id",
            ids
        ).fetchall()
        result = []
        for row in rows:
            tags = _get_tags_for_video(conn, row["id"])
            result.append(_row_to_video(row, tags))
        return result


def update_video_fields(
    catalog_root: Path,
    video_id: int,
    *,
    project: str | None = None,
    location: str | None = None,
    camera: str | None = None,
    operator: str | None = None,
    lens: str | None = None,
    shoot_date: date | None = None,
    notes: str | None = None,
    thumbnail_path: str | None = None,
    storyboard_path: str | None = None,
    codec: str | None = None,
    bit_rate: int | None = None,
    audio_channels: int | None = None,
    camera_xml_path: str | None = None,
    iso: int | None = None,
    f_number: float | None = None,
    shutter_speed: str | None = None,
    focal_length: float | None = None,
    white_balance: str | None = None,
    gamma: str | None = None,
    color_primaries: str | None = None,
    coding_equations: str | None = None,
    transcription: str | None = None,
    tc_start: str | None = None,
    tc_end: str | None = None,
    fps: float | None = None,
) -> None:
    """Update video fields (labeling + preview paths + technical metadata).

    fps: Only intended for one-time backfill on legacy clips that were imported
         before framerate was reliably stored. Once a non-zero fps is set on a
         video (confirmed at import time), it is treated as immutable and
         should not be overwritten.
    """
    sets: list[str] = []
    params: list[Any] = []
    if project is not None:
        sets.append("project = ?")
        params.append(project)
    if location is not None:
        sets.append("location = ?")
        params.append(location)
    if camera is not None:
        sets.append("camera = ?")
        params.append(camera)
    if operator is not None:
        sets.append("operator = ?")
        params.append(operator)
    if lens is not None:
        sets.append("lens = ?")
        params.append(lens)
    if shoot_date is not None:
        sets.append("shoot_date = ?")
        params.append(shoot_date.isoformat() if hasattr(shoot_date, 'isoformat') else shoot_date)
    if notes is not None:
        sets.append("notes = ?")
        params.append(notes)
    if thumbnail_path is not None:
        sets.append("thumbnail_path = ?")
        params.append(thumbnail_path)
    if storyboard_path is not None:
        sets.append("storyboard_path = ?")
        params.append(storyboard_path)
    if codec is not None:
        sets.append("codec = ?")
        params.append(codec)
    if bit_rate is not None:
        sets.append("bit_rate = ?")
        params.append(bit_rate)
    if audio_channels is not None:
        sets.append("audio_channels = ?")
        params.append(audio_channels)
    if camera_xml_path is not None:
        sets.append("camera_xml_path = ?")
        params.append(camera_xml_path)
    if iso is not None:
        sets.append("iso = ?")
        params.append(iso)
    if f_number is not None:
        sets.append("f_number = ?")
        params.append(f_number)
    if shutter_speed is not None:
        sets.append("shutter_speed = ?")
        params.append(shutter_speed)
    if focal_length is not None:
        sets.append("focal_length = ?")
        params.append(focal_length)
    if white_balance is not None:
        sets.append("white_balance = ?")
        params.append(white_balance)
    if gamma is not None:
        sets.append("gamma = ?")
        params.append(gamma)
    if color_primaries is not None:
        sets.append("color_primaries = ?")
        params.append(color_primaries)
    if coding_equations is not None:
        sets.append("coding_equations = ?")
        params.append(coding_equations)
    if transcription is not None:
        sets.append("transcription = ?")
        params.append(transcription)
    if tc_start is not None:
        sets.append("tc_start = ?")
        params.append(tc_start)
    if tc_end is not None:
        sets.append("tc_end = ?")
        params.append(tc_end)
    if fps is not None:
        # Respect "framerate confirmed at import time and never be changed".
        # Only allow setting fps if the clip currently has no positive fps (legacy backfill).
        try:
            with get_connection(catalog_root) as conn:
                current = conn.execute("SELECT fps FROM videos WHERE id = ?", (video_id,)).fetchone()
                current_fps = float(current[0]) if current and current[0] is not None else 0.0
            if current_fps > 0:
                # Allow correction if the currently stored value is unrealistic (bad import-time probe)
                # but the new one looks sane. This fixes cases like 150 fps on normal footage.
                new_fps = float(fps)
                if (current_fps > 120 or current_fps < 1) and (1 <= new_fps <= 120):
                    print(f"[DB] Correcting unrealistic stored fps={current_fps} to {new_fps} for video {video_id}")
                    sets.append("fps = ?")
                    params.append(new_fps)
                else:
                    print(f"[DB] Ignoring fps={fps} update for video {video_id}: framerate was already confirmed at import time and is immutable.")
            else:
                sets.append("fps = ?")
                params.append(float(fps))
        except Exception as guard_ex:
            # If guard fails, fall back to allowing the set (better than losing the value)
            print(f"[DB] fps guard check failed for {video_id}, allowing set: {guard_ex}")
            sets.append("fps = ?")
            params.append(float(fps))

    if not sets:
        return

    params.append(video_id)
    sql = f"UPDATE videos SET {', '.join(sets)} WHERE id = ?"
    with get_connection(catalog_root) as conn:
        conn.execute(sql, params)
        conn.commit()


def update_clips_by_project(catalog_root: Path, project_name: str, **kwargs) -> int:
    """Batch update all videos belonging to a project.
    Supports fields like shoot_date, location, operator, camera, etc.
    Returns the number of rows updated.
    """
    if not kwargs or not project_name:
        return 0

    sets: list[str] = []
    params: list[Any] = []

    for key, value in kwargs.items():
        if value is None or str(value).strip() == "":
            continue
        if key == "fps":
            # fps is confirmed at import and immutable; ignore in batch project updates
            # (individual corrections for bad probes are handled in update_video_fields with the guard above)
            continue

        db_field = key
        if key == "date":
            db_field = "shoot_date"

        sets.append(f"{db_field} = ?")

        if db_field == "shoot_date" and hasattr(value, "isoformat"):
            params.append(value.isoformat())
        else:
            params.append(value)

    if not sets:
        return 0

    params.append(project_name)
    sql = f"UPDATE videos SET {', '.join(sets)} WHERE project = ?"

    with get_connection(catalog_root) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def rename_project(catalog_root: Path, old_name: str, new_name: str) -> int:
    """Rename a project for all videos that use it.
    Returns the number of clips that were updated.
    """
    if not new_name or new_name == old_name:
        return 0
    with get_connection(catalog_root) as conn:
        cur = conn.execute(
            "UPDATE videos SET project = ? WHERE project = ?",
            (new_name, old_name)
        )
        conn.commit()
        return cur.rowcount


def delete_project(catalog_root: Path, name: str, also_delete_clips: bool = False) -> int:
    """Permanently delete a project from the database.
    - Always removes the project record from the 'projects' table.
    - Always removes its entries from client_projects.
    - If also_delete_clips is True: deletes the associated video clips (including their video_tags associations and FTS entries via triggers).
    - Otherwise: sets project = NULL on the associated videos (clips stay in catalog).
    Returns the number of affected clips (deleted or updated).

    ZERO GHOSTS: when also_delete_clips=True, the caller (UI dialog) + this function
    pre-call cleanup_all_generated_files_for_clip for every clip (files), then do
    explicit video_tags DELETE + videos DELETE + FTS safety purge (DB). No clip rows
    or generated artifacts (previews, proxies, trans, subs, audio) are left behind.
    """
    affected = 0
    clips_to_cleanup = []
    if also_delete_clips:
        # Collect clips that will be deleted so we can cleanup their artifacts (files + DB info)
        # even if this is called directly (not via UI dialog which pre-cleans).
        try:
            videos = search_videos(catalog_root, SearchFilters(), limit=100000)
            clips_to_cleanup = [(v.id, v.filename) for v in videos if getattr(v, "project", None) == name and v.id]
        except Exception:
            clips_to_cleanup = []

    if also_delete_clips and clips_to_cleanup:
        # Cleanup generated files for these clips (previews/boards, audio, transcripts, subtitles, proxies)
        try:
            from minicat.core.video import cleanup_all_generated_files_for_clip
            for cid, fname in clips_to_cleanup:
                try:
                    cleanup_all_generated_files_for_clip(cid, catalog_root, original_filename=fname)
                except Exception as cl_ex:
                    print(f"[Delete Project] Artifact cleanup failed for clip {cid}: {cl_ex}")
        except Exception as ex:
            print(f"[Delete Project] Pre-cleanup import failed: {ex}")

    # Heal FTS before any videos_fts touch (prevents T.tag_names errors on old/stale FTS defs).
    try:
        ensure_fts_consistency(catalog_root)
    except Exception:
        pass  # ensure already logs its own non-fatal warning

    with get_connection(catalog_root) as conn:
        # First remove client associations
        conn.execute("DELETE FROM client_projects WHERE project = ?", (name,))

        if also_delete_clips:
            # Clean tag associations for the clips being deleted (the per-clip delete_video
            # does this, but bulk project delete bypasses it).
            conn.execute("DELETE FROM video_tags WHERE video_id IN (SELECT id FROM videos WHERE project = ?)", (name,))
            cur = conn.execute("DELETE FROM videos WHERE project = ?", (name,))
            # Ensure FTS is fully clean for the deleted clips (in addition to the per-row triggers).
            # This removes any stale clip information from the search index.
            try:
                conn.execute("DELETE FROM videos_fts WHERE rowid NOT IN (SELECT id FROM videos)")
            except Exception as fts_ex:
                print(f"[DB] FTS stale purge note in delete_project (non-fatal): {fts_ex}")
            affected = cur.rowcount if cur else len(clips_to_cleanup)
        else:
            cur = conn.execute("UPDATE videos SET project = NULL WHERE project = ?", (name,))
            affected = cur.rowcount if cur else 0

        # Also delete the project record itself if it exists in the projects table
        conn.execute("DELETE FROM projects WHERE name = ?", (name,))

        conn.commit()
        return affected


# ---------------------------------------------------------------------------
# Rich Project Management (new in v4)
# ---------------------------------------------------------------------------

def _row_to_project(row: sqlite3.Row, clip_count: int = 0, total_duration: float = 0.0) -> Project:
    data = dict(row)

    # Parse JSON camera_operators
    ops = data.get("camera_operators")
    if ops:
        try:
            data["camera_operators"] = json.loads(ops)
        except Exception:
            data["camera_operators"] = [ops]
    else:
        data["camera_operators"] = []

    if data.get("start_date"):
        try:
            data["start_date"] = date.fromisoformat(data["start_date"])
        except ValueError:
            data["start_date"] = None
    if data.get("end_date"):
        try:
            data["end_date"] = date.fromisoformat(data["end_date"])
        except ValueError:
            data["end_date"] = None

    for key in ("created_at", "updated_at"):
        if data.get(key):
            try:
                data[key] = datetime.fromisoformat(data[key])
            except ValueError:
                data[key] = None

    data["clip_count"] = clip_count
    data["total_duration"] = total_duration

    # Legacy single client field is kept for now
    proj = Project(**{k: v for k, v in data.items() if k in Project.model_fields})
    return proj


def get_all_projects(catalog_root: Path) -> list[Project]:
    """Return all projects with computed stats from associated clips."""
    with get_connection(catalog_root) as conn:
        projects = []
        rows = conn.execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE").fetchall()
        for row in rows:
            stats = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(duration), 0) FROM videos WHERE project = ?",
                (row["name"],)
            ).fetchone()
            clip_count = stats[0] or 0
            total_duration = stats[1] or 0.0
            proj = _row_to_project(row, clip_count, total_duration)

            # Load associated clients via many-to-many
            client_rows = conn.execute(
                """
                SELECT c.name FROM clients c
                JOIN client_projects cp ON cp.client_id = c.id
                WHERE cp.project = ?
                ORDER BY c.name COLLATE NOCASE
                """,
                (proj.name,)
            ).fetchall()
            proj.clients = [r[0] for r in client_rows]

            projects.append(proj)
        return projects


def get_project(catalog_root: Path, name: str) -> Project | None:
    with get_connection(catalog_root) as conn:
        row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        if not row:
            return None
        stats = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration), 0) FROM videos WHERE project = ?",
            (name,)
        ).fetchone()
        proj = _row_to_project(row, stats[0] or 0, stats[1] or 0.0)

        # Load associated clients
        client_rows = conn.execute(
            """
            SELECT c.name FROM clients c
            JOIN client_projects cp ON cp.client_id = c.id
            WHERE cp.project = ?
            ORDER BY c.name COLLATE NOCASE
            """,
            (name,)
        ).fetchall()
        proj.clients = [r[0] for r in client_rows]
        return proj


def get_project_with_stats(catalog_root: Path, name: str) -> Project:
    """Always returns a Project object with correct clip_count and total_duration.
    If no rich project record exists yet, returns a minimal Project with just the stats.
    """
    proj = get_project(catalog_root, name)
    if proj:
        return proj

    # Legacy project (only exists on video records)
    with get_connection(catalog_root) as conn:
        stats = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration), 0) FROM videos WHERE project = ?",
            (name,)
        ).fetchone()
        clip_count = stats[0] or 0
        total_duration = stats[1] or 0.0

    proj = Project(
        name=name,
        clip_count=clip_count,
        total_duration=total_duration
    )

    # Load clients even for legacy projects
    with get_connection(catalog_root) as conn:
        client_rows = conn.execute(
            """
            SELECT c.name FROM clients c
            JOIN client_projects cp ON cp.client_id = c.id
            WHERE cp.project = ?
            """,
            (name,)
        ).fetchall()
        proj.clients = [r[0] for r in client_rows]
    return proj


def create_or_update_project(catalog_root: Path, project: Project) -> Project:
    """Create or update a rich project record. Returns the saved project."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    project.updated_at = datetime.utcnow()

    if project.created_at is None:
        project.created_at = project.updated_at

    ops_json = json.dumps(project.camera_operators or [])

    with get_connection(catalog_root) as conn:
        if project.id:
            conn.execute(
                """
                UPDATE projects SET
                    name = ?, start_date = ?, end_date = ?, client = ?, director = ?,
                    producer = ?, editor = ?, camera_operators = ?, location = ?,
                    status = ?, color = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    project.name,
                    project.start_date.isoformat() if project.start_date else None,
                    project.end_date.isoformat() if project.end_date else None,
                    project.client, project.director, project.producer, project.editor,
                    ops_json, project.location, project.status, project.color, project.notes,
                    now, project.id
                )
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO projects (
                    name, start_date, end_date, client, director, producer, editor,
                    camera_operators, location, status, color, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project.name,
                    project.start_date.isoformat() if project.start_date else None,
                    project.end_date.isoformat() if project.end_date else None,
                    project.client, project.director, project.producer, project.editor,
                    ops_json, project.location, project.status, project.color, project.notes,
                    now, now
                )
            )
            project.id = cur.lastrowid

        conn.commit()
        return get_project(catalog_root, project.name) or project


# ---------------------------------------------------------------------------
# Clients (rich entities, many-to-many with projects)
# ---------------------------------------------------------------------------

def _row_to_client(row: sqlite3.Row, project_count: int = 0) -> Client:
    data = dict(row)
    for key in ("created_at", "updated_at"):
        if data.get(key):
            try:
                data[key] = datetime.fromisoformat(data[key])
            except ValueError:
                data[key] = None
    data["project_count"] = project_count
    return Client(**{k: v for k, v in data.items() if k in Client.model_fields})


def get_clients(catalog_root: Path) -> list[Client]:
    with get_connection(catalog_root) as conn:
        rows = conn.execute(
            "SELECT * FROM clients ORDER BY name COLLATE NOCASE"
        ).fetchall()
        clients = []
        for row in rows:
            stats = conn.execute(
                "SELECT COUNT(*) FROM client_projects WHERE client_id = ?",
                (row["id"],)
            ).fetchone()
            clients.append(_row_to_client(row, stats[0] or 0))
        return clients


def get_client(catalog_root: Path, client_id: int) -> Client | None:
    with get_connection(catalog_root) as conn:
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if not row:
            return None
        stats = conn.execute(
            "SELECT COUNT(*) FROM client_projects WHERE client_id = ?",
            (client_id,)
        ).fetchone()
        return _row_to_client(row, stats[0] or 0)


def create_or_update_client(catalog_root: Path, client: Client) -> Client:
    """Create or update a rich client record."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    client.updated_at = datetime.utcnow()

    if client.created_at is None:
        client.created_at = client.updated_at

    with get_connection(catalog_root) as conn:
        if client.id:
            conn.execute(
                """
                UPDATE clients SET
                    name = ?, contact_person = ?, email = ?, phone = ?,
                    address = ?, notes = ?, color = ?, logo_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    client.name, client.contact_person, client.email, client.phone,
                    client.address, client.notes, client.color, client.logo_path, now, client.id
                )
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO clients (
                    name, contact_person, email, phone, address, notes, color, logo_path,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client.name, client.contact_person, client.email, client.phone,
                    client.address, client.notes, client.color, client.logo_path, now, now
                )
            )
            client.id = cur.lastrowid

        conn.commit()
        return get_client(catalog_root, client.id) or client


def get_clients_for_project(catalog_root: Path, project: str) -> list[Client]:
    """Return all rich Client objects that this project belongs to."""
    with get_connection(catalog_root) as conn:
        rows = conn.execute(
            """
            SELECT c.* FROM clients c
            JOIN client_projects cp ON cp.client_id = c.id
            WHERE cp.project = ?
            ORDER BY c.name COLLATE NOCASE
            """,
            (project,)
        ).fetchall()
        return [_row_to_client(row) for row in rows]


def set_project_clients(catalog_root: Path, project: str, client_ids: list[int]) -> None:
    """Replace all client associations for a project with the given list of client IDs."""
    with get_connection(catalog_root) as conn:
        conn.execute("DELETE FROM client_projects WHERE project = ?", (project,))
        for cid in client_ids:
            conn.execute(
                "INSERT OR IGNORE INTO client_projects (client_id, project) VALUES (?, ?)",
                (cid, project)
            )
        conn.commit()


def get_projects_for_client(catalog_root: Path, client_id: int) -> list[str]:
    """Return project names belonging to this client."""
    with get_connection(catalog_root) as conn:
        rows = conn.execute(
            "SELECT project FROM client_projects WHERE client_id = ?",
            (client_id,)
        ).fetchall()
        return [r[0] for r in rows]


def delete_client(catalog_root: Path, client_id: int) -> None:
    """Delete a client and remove all its project associations.
    Projects themselves are not deleted.
    Also cleans up any legacy 'client' TEXT references in the projects table.
    """
    with get_connection(catalog_root) as conn:
        # Get the client name for legacy cleanup (before deleting the row)
        row = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
        client_name = row[0] if row else None

        # Remove all associations for this client
        conn.execute("DELETE FROM client_projects WHERE client_id = ?", (client_id,))

        # Delete the client record
        conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))

        # Clean up legacy client field in projects table (if any projects still reference it by the old name)
        if client_name:
            conn.execute("UPDATE projects SET client = NULL WHERE client = ?", (client_name,))

        conn.commit()


def mark_missing(catalog_root: Path, video_id: int, missing: bool = True) -> None:
    with get_connection(catalog_root) as conn:
        conn.execute(
            "UPDATE videos SET missing = ?, last_seen = ? WHERE id = ?",
            (1 if missing else 0, datetime.utcnow().isoformat(timespec="seconds"), video_id),
        )
        conn.commit()


def _get_tags_for_video(conn: sqlite3.Connection, video_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.name FROM tags t
        JOIN video_tags vt ON vt.tag_id = t.id
        WHERE vt.video_id = ?
        ORDER BY t.name
        """,
        (video_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def get_all_tags(catalog_root: Path) -> list[Tag]:
    with get_connection(catalog_root) as conn:
        rows = conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
        return [Tag(id=r["id"], name=r["name"], created_at=r["created_at"]) for r in rows]


def add_tag(catalog_root: Path, name: str) -> int:
    """Create a tag if it doesn't exist. Returns tag id.
    Retries on 'database is locked' because multiple writers (import + user actions) are common.
    """
    now = datetime.utcnow().isoformat(timespec="seconds")
    normalized = name.strip()

    for attempt in range(8):
        try:
            with get_connection(catalog_root) as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)",
                    (normalized, now),
                )
                if cur.lastrowid:
                    tag_id = cur.lastrowid
                else:
                    row = conn.execute("SELECT id FROM tags WHERE name = ?", (normalized,)).fetchone()
                    tag_id = row["id"] if row else None
                conn.commit()
                if tag_id:
                    return tag_id
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                import time
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
        except Exception:
            raise

    # Final attempt without catching
    with get_connection(catalog_root) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)",
            (normalized, now),
        )
        tag_id = cur.lastrowid or conn.execute(
            "SELECT id FROM tags WHERE name = ?", (normalized,)
        ).fetchone()["id"]
        conn.commit()
        return tag_id


def set_video_tags(catalog_root: Path, video_id: int, tag_names: list[str]) -> None:
    """Replace all tags for a video with the given list.
    Tries hard to do everything in one connection to reduce 'database is locked' errors.
    """
    normalized_names = [n.strip().lower() for n in tag_names if n and n.strip()]

    now = datetime.utcnow().isoformat(timespec="seconds")

    with get_connection(catalog_root) as conn:
        conn.execute("DELETE FROM video_tags WHERE video_id = ?", (video_id,))
        conn.execute("UPDATE videos SET tag_names = '' WHERE id = ?", (video_id,))

        if not normalized_names:
            conn.commit()
            return

        # Create all missing tags in this same connection
        tag_ids = []
        for name in normalized_names:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)",
                (name, now)
            )
            if cur.lastrowid:
                tag_ids.append(cur.lastrowid)
            else:
                row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
                if row:
                    tag_ids.append(row["id"])

        for tag_id in tag_ids:
            conn.execute(
                "INSERT OR IGNORE INTO video_tags (video_id, tag_id) VALUES (?, ?)",
                (video_id, tag_id),
            )

        # Denormalize for FTS (content= table must have the column; space-separated for MATCH)
        tag_str = " ".join(normalized_names)
        conn.execute("UPDATE videos SET tag_names = ? WHERE id = ?", (tag_str, video_id))

        conn.commit()


def delete_video(catalog_root: Path, video_id: int) -> bool:
    """Permanently remove a video entry (and its tag links) from the catalog database.
    Returns True if the video was deleted.

    ZERO GHOSTS CONTRACT (files + DB):
    - UI callers ALWAYS invoke cleanup_all_generated_files_for_clip (covers previews/boards,
      audio/, transcriptions/, subtitles/, proxies/) BEFORE this.
    - This function ensures: explicit video_tags DELETE + videos row DELETE (the AFTER DELETE
      trigger on videos handles the FTS "delete" token for the external-content index).
    - Post-delete orphan cleanup (scheduled by UI + runs at startup) performs the NOT IN safety
      purges on videos_fts and video_tags so that no clip information lingers in .db.
    - Result: absolutely no generated files on disk and no clip information (row, tags, FTS)
      left in catalog.db for a removed clip. See cleanup_all_generated_files_for_clip.
    """
    # Ensure FTS is healthy (cheap probe unless a stale T.tag_names definition is detected).
    try:
        ensure_fts_consistency(catalog_root)
    except Exception:
        pass

    # Core removal: tags + the videos row itself.
    # - The AFTER DELETE trigger on videos (see SCHEMA_SQL) will emit the FTS "delete" token.
    # - UI delete paths schedule cleanup_orphaned_catalog_files which runs the NOT IN safety
    #   purges for both videos_fts and video_tags (defense-in-depth for the zero-ghosts DB contract).
    with get_connection(catalog_root) as conn:
        conn.execute("DELETE FROM video_tags WHERE video_id = ?", (video_id,))
        cur = conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        conn.commit()
        return cur.rowcount > 0


def search_videos(
    catalog_root: Path,
    filters: SearchFilters,
    limit: int = 2000,  # raised to keep processed clips (with thumbs/transcriptions) visible; UI also warns on hit
) -> list[Video]:
    """
    Excellent search supporting the key user requirements:
    - Full-text via FTS5 (filename, path, notes, future tag names)
    - Strong structured filters on date, project, location, camera
    - Tag intersection
    - Duration ranges
    """
    with get_connection(catalog_root) as conn:
        where_clauses: list[str] = []
        params: list[Any] = []

        if filters.project:
            ph = ",".join("?" * len(filters.project))
            where_clauses.append(f"project IN ({ph})")
            params.extend(filters.project)

        if filters.client:
            # Many-to-many via client_projects
            ph = ",".join("?" * len(filters.client))
            where_clauses.append(f"""
                project IN (
                    SELECT cp.project FROM client_projects cp
                    JOIN clients c ON c.id = cp.client_id
                    WHERE c.name IN ({ph})
                )
            """)
            params.extend(filters.client)

        if filters.location:
            ph = ",".join("?" * len(filters.location))
            where_clauses.append(f"location IN ({ph})")
            params.extend(filters.location)

        if filters.camera:
            ph = ",".join("?" * len(filters.camera))
            where_clauses.append(f"camera IN ({ph})")
            params.extend(filters.camera)

        if filters.date_from:
            where_clauses.append("shoot_date >= ?")
            params.append(filters.date_from.isoformat())
        if filters.date_to:
            where_clauses.append("shoot_date <= ?")
            params.append(filters.date_to.isoformat())

        if filters.min_duration is not None:
            where_clauses.append("duration >= ?")
            params.append(filters.min_duration)
        if filters.max_duration is not None:
            where_clauses.append("duration <= ?")
            params.append(filters.max_duration)

        # Tag filter: use a proper WHERE condition (no leading AND)
        if filters.tags:
            tag_subquery = """
                id IN (
                    SELECT vt.video_id FROM video_tags vt
                    JOIN tags t ON t.id = vt.tag_id
                    WHERE t.name IN ({})
                    GROUP BY vt.video_id
                    HAVING COUNT(DISTINCT t.name) = ?
                )
            """.format(",".join("?" * len(filters.tags)))
            where_clauses.append(tag_subquery)
            params.extend(filters.tags)
            params.append(len(filters.tags))

        base_sql = "SELECT * FROM videos"
        if where_clauses:
            base_sql += " WHERE " + " AND ".join(where_clauses)

        if filters.text and filters.text.strip():
            fts_sql = """
                SELECT v.* FROM videos v
                JOIN videos_fts f ON f.rowid = v.id
                WHERE videos_fts MATCH ?
            """
            fts_params = [filters.text.strip()]

            if where_clauses:
                structured = " AND ".join(where_clauses)
                sql = f"""
                    SELECT * FROM (
                        {fts_sql} AND v.id IN (SELECT id FROM videos WHERE {structured})
                    )
                    ORDER BY v.shoot_date DESC, v.import_date DESC
                    LIMIT ?
                """
                params = fts_params + params + [limit]
            else:
                sql = f"""
                    {fts_sql}
                    ORDER BY rank, v.shoot_date DESC
                    LIMIT ?
                """
                params = fts_params + params + [limit]
        else:
            sql = f"""
                {base_sql}
                ORDER BY shoot_date DESC, import_date DESC
                LIMIT ?
            """
            params = params + [limit]

        rows = conn.execute(sql, params).fetchall()

        results: list[Video] = []
        for row in rows:
            tags = _get_tags_for_video(conn, row["id"])
            results.append(_row_to_video(row, tags))
        return results


def get_distinct_values(catalog_root: Path, column: str) -> list[str]:
    """Return sorted distinct values for filter UIs (project / location / camera)."""
    allowed = {"project", "location", "camera"}
    if column not in allowed:
        raise ValueError(f"Invalid column: {column}")
    with get_connection(catalog_root) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM videos WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column}"
        ).fetchall()
        return [r[0] for r in rows]


def get_most_used_tags(catalog_root: Path, limit: int = 20) -> list[tuple[str, int]]:
    """Return (tag_name, usage_count) pairs for the most frequently used tags across the entire catalog."""
    with get_connection(catalog_root) as conn:
        rows = conn.execute(
            """
            SELECT t.name, COUNT(vt.video_id) AS cnt
            FROM tags t
            JOIN video_tags vt ON vt.tag_id = t.id
            GROUP BY t.id, t.name
            ORDER BY cnt DESC, t.name ASC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()
        return [(r["name"], int(r["cnt"])) for r in rows]


def ensure_fts_consistency(catalog_root: Path) -> None:
    """Make sure the videos.tag_names column exists and the videos_fts is consistent
    (prevents "no such column: T.tag_names" during FTS operations or orphan cleanup).
    Safe to call often; cheap. Probes first and only does an expensive rebuild if a
    stale FTS definition is detected (avoids unnecessary DROP on healthy catalogs).
    """
    try:
        with get_connection(catalog_root) as conn:
            # 1. Ensure the denormalized column exists (old catalogs).
            try:
                conn.execute("ALTER TABLE videos ADD COLUMN tag_names TEXT")
            except Exception:
                pass

            # 2. Probe whether the FTS virtual table is usable with the expected columns.
            # If a "no such column: T.tag_names" (or similar) is latent in the FTS definition,
            # the probe (or a later MATCH / rowid op) will surface it here and we heal once.
            need_rebuild = False
            try:
                conn.execute("SELECT rowid FROM videos_fts LIMIT 0").fetchone()
                conn.execute("SELECT tag_names FROM videos_fts LIMIT 0").fetchone()
            except Exception as ex:
                msg = str(ex).lower()
                if any(k in msg for k in ("tag_names", "no such column", "fts", "malformed")):
                    need_rebuild = True
                else:
                    print(f"[DB] ensure_fts_consistency probe warning (non-fatal): {ex}")
                    need_rebuild = True

            if not need_rebuild:
                # Keep denorm in sync for any clips that have junction tags but empty tag_names.
                # This is safe DML, no FTS virtual table risk.
                try:
                    conn.execute("""
                        UPDATE videos SET tag_names = COALESCE( (SELECT GROUP_CONCAT(t.name, ' ') FROM tags t
                            JOIN video_tags vt ON vt.tag_id = t.id
                            WHERE vt.video_id = videos.id ORDER BY t.name), '' )
                        WHERE tag_names IS NULL OR tag_names = '';
                    """)
                    conn.commit()
                except Exception:
                    pass
                return

            # 3. Stale/broken FTS detected -> full heal (DROP + recreate + repopulate).
            try:
                conn.executescript("""
                    DROP TABLE IF EXISTS videos_fts;
                    CREATE VIRTUAL TABLE videos_fts USING fts5(
                        path, filename, notes, tag_names,
                        content='videos',
                        content_rowid='id'
                    );
                    UPDATE videos SET tag_names = COALESCE( (SELECT GROUP_CONCAT(t.name, ' ') FROM tags t
                                      JOIN video_tags vt ON vt.tag_id = t.id
                                      WHERE vt.video_id = videos.id ORDER BY t.name), '' );
                    INSERT INTO videos_fts (rowid, path, filename, notes, tag_names)
                    SELECT v.id, v.path, v.filename, v.notes, COALESCE(v.tag_names, '')
                    FROM videos v;
                """)
                conn.commit()
                print("[DB] FTS consistency rebuilt (healed stale 'T.tag_names' definition)")
            except Exception as ex:
                print(f"[DB] ensure_fts_consistency rebuild failed (non-fatal): {ex}")
    except Exception as ex:
        print(f"[DB] ensure_fts_consistency warning: {ex}")
