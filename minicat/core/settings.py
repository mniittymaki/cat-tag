"""
Persistent settings for CAT+TAG (last catalog, preferences, etc.).

Stored in a simple JSON file under the user's config directory.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()  # Load .env file once when settings module is first imported
except ImportError:

    def load_dotenv():
        return None  # type: ignore

    # python-dotenv not installed — environment variables still work via os.getenv

APP_NAME = "minicat"

# Default catalog location per user request: always ~/CAT+TAG (username agnostic)
DEFAULT_CATALOG_DIRECTORY = str(Path.home() / "CAT+TAG")


def _get_config_dir() -> Path:
    """Return the user config directory for CAT+TAG."""
    if "APPDATA" in os.environ:  # Windows
        base = Path(os.environ["APPDATA"])
    elif "XDG_CONFIG_HOME" in os.environ:
        base = Path(os.environ["XDG_CONFIG_HOME"])
    else:
        # macOS and Linux default
        base = Path.home() / ".config"
    config_dir = base / APP_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


CONFIG_FILE = _get_config_dir() / "settings.json"


def load_settings() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def save_settings(data: dict[str, Any]) -> None:
    CONFIG_FILE.write_text(json.dumps(data, indent=2))


def get_last_catalog() -> Path | None:
    data = load_settings()
    path = data.get("last_catalog")
    if path:
        p = Path(path).expanduser()
        # Return the saved path even if the folder does not exist yet.
        # The launch / switch logic will mkdir + init_catalog as needed.
        # (Previously we required .exists() which blocked new default ~/CAT+TAG etc.)
        return p
    return None


def set_last_catalog(path: str | Path) -> None:
    data = load_settings()
    data["last_catalog"] = str(Path(path).expanduser().resolve())
    save_settings(data)


def get_default_catalog_directory() -> Path:
    """Return the default CAT+TAG catalog location: ~/CAT+TAG .

    Ensures the directory exists (but does not init the DB — call resolve_catalog for that).
    This is the new default used on first launch when no last_catalog has been chosen.
    """
    p = Path.home() / "CAT+TAG"
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def get_effective_catalog() -> Path:
    """Decide the catalog root for this launch.

    - If last_catalog saved and the folder exists on disk, use it.
    - If last_catalog saved but folder missing, and it looks like a legacy default
      (e.g. old ~/VideoCatalogs/...), migrate to the new ~/CAT+TAG default.
    - Otherwise honor a user-chosen (possibly not-yet-created) saved path.
    - If nothing saved, use and persist ~/CAT+TAG .
    Always returns a resolved Path; caller should still call resolve_catalog on it
    to ensure subdirs + DB schema are ready.
    """
    last = get_last_catalog()
    if last:
        if last.exists():
            return last
        # Folder does not exist: check for known legacy prefill paths that were never real.
        last_str = str(last)
        if (
            "VideoCatalogs" in last_str
            or str(Path.home() / "VideoCatalogs" / "CAT+TAG") in last_str
        ):
            default = get_default_catalog_directory()
            set_last_catalog(default)
            return default
        # Deliberate prior choice of a fresh path (e.g. user picked a new dir name via Open Catalog before quitting)
        return last
    # First run ever
    default = get_default_catalog_directory()
    set_last_catalog(default)
    return default


# ---------------------------------------------------------------------------
# User Preferences
# ---------------------------------------------------------------------------


def get_preference(key: str, default: Any = None) -> Any:
    """Get a user preference with a default fallback."""
    data = load_settings()
    prefs = data.get("preferences", {})
    return prefs.get(key, default)


def set_preference(key: str, value: Any) -> None:
    """Set a user preference and persist it."""
    data = load_settings()
    if "preferences" not in data:
        data["preferences"] = {}
    data["preferences"][key] = value
    save_settings(data)


# ---------------------------------------------------------------------------
# Inspector-specific UI settings
# ---------------------------------------------------------------------------


def get_inspector_width(default: int = 380) -> int:
    """Get persisted width for the resizable right inspector panel (px)."""
    width = get_preference("ui.inspector_width", default)
    try:
        w = int(width)
        return max(280, min(w, 520))
    except (TypeError, ValueError):
        return default


def set_inspector_width(width: int) -> None:
    """Persist the current width of the inspector panel."""
    w = max(280, min(int(width), 520))
    set_preference("ui.inspector_width", w)


# ---------------------------------------------------------------------------
# AI Features (Gemini / OpenAI)
# ---------------------------------------------------------------------------


def get_gemini_api_key() -> str | None:
    """
    Return the Gemini API key.

    Priority:
    1. Environment variable GEMINI_API_KEY (loaded from .env or system env)
    2. Stored preference (legacy, stored in JSON)

    This moves the raw key out of the application config for better security.
    """
    load_dotenv()  # Safe to call multiple times; loads .env if present

    env_key = os.getenv("GEMINI_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    # Fallback to stored key (for backward compatibility)
    return get_preference("ai.gemini_api_key")


def set_gemini_api_key(key: str | None) -> None:
    """
    Store the Gemini API key in preferences (legacy method).

    WARNING: Environment variable GEMINI_API_KEY (via .env or system)
    takes precedence. Storing the key here is less secure.
    Prefer using a .env file with GEMINI_API_KEY=your-key
    """
    set_preference("ai.gemini_api_key", key)


GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
]

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def get_gemini_model() -> str:
    """Return the user's preferred Gemini model.
    Falls back to the default if the saved model is no longer available.
    """
    saved = get_preference("ai.gemini_model")
    if saved in GEMINI_MODELS:
        return saved
    return DEFAULT_GEMINI_MODEL


def set_gemini_model(model: str) -> None:
    """Store the chosen Gemini model."""
    if model in GEMINI_MODELS:
        set_preference("ai.gemini_model", model)
    else:
        set_preference("ai.gemini_model", DEFAULT_GEMINI_MODEL)


# ---------------------------------------------------------------------------
# Text-to-Speech (Voiceover) Provider
# ---------------------------------------------------------------------------

TTS_PROVIDERS = ["local", "google"]
DEFAULT_TTS_PROVIDER = "local"


def get_tts_provider() -> str:
    """Return the chosen TTS provider for AI Director voiceovers.

    "local" = Piper TTS (fully offline, no cloud, recommended default)
    "google" = Google Cloud TTS (highest quality WaveNet/Standard voices, requires gcloud auth)
    """
    saved = get_preference("ai.tts_provider", DEFAULT_TTS_PROVIDER)
    if saved not in TTS_PROVIDERS:
        saved = DEFAULT_TTS_PROVIDER
    return saved


def set_tts_provider(provider: str) -> None:
    """Store the preferred TTS provider."""
    if provider in TTS_PROVIDERS:
        set_preference("ai.tts_provider", provider)
    else:
        set_preference("ai.tts_provider", DEFAULT_TTS_PROVIDER)


# ---------------------------------------------------------------------------
# TTS Voiceover Defaults (for AI Director narrative exporter #3)
# ---------------------------------------------------------------------------

DEFAULT_TTS_LANGUAGE = "en"
DEFAULT_TTS_GOOGLE_VOICE = "en-US-Wavenet-F"

# Canonical list of languages supported for AI Director voiceovers / narration.
# Keep this in sync with voiceover.py usage and the UI selects.
SUPPORTED_LANGUAGES = [
    ("en", "English"),
    ("fr", "French"),
    ("es", "Spanish"),
    ("fi", "Finnish"),
    ("de", "German"),
    ("sv", "Swedish"),
]


def get_tts_default_language() -> str:
    """Default language to use for voiceover narration (used by exporter #3).
    Always returns a currently supported code (repairs stale 'fi' etc. saved prefs).
    """
    saved = get_preference("ai.tts_default_language", DEFAULT_TTS_LANGUAGE)
    code = clean_tts_language(saved) or DEFAULT_TTS_LANGUAGE
    valid = {c for c, _ in SUPPORTED_LANGUAGES}
    if code in valid:
        return code
    # repair bad persisted value
    return DEFAULT_TTS_LANGUAGE


def set_tts_default_language(lang: Any) -> None:
    """Store the default voiceover language. Only accepts currently supported codes."""
    code = clean_tts_language(lang)
    if not code:
        return
    valid = {c for c, _ in SUPPORTED_LANGUAGES}
    if code in valid:
        set_preference("ai.tts_default_language", code)


def clean_tts_voice(v: Any) -> str | None:
    """Sanitize voice values that may have been saved as (id, label) tuples/lists
    from ui.select or as "id (label)" strings. Always return a plain voice id string.
    """
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        if len(v) > 0:
            v = v[0]
        else:
            v = None
    if isinstance(v, str):
        s = v.strip()
        # If it looks like "voice-id (some label...)", take only the id part.
        # This recovers from cases where a full label string was persisted.
        if " (" in s:
            s = s.split(" (", 1)[0].strip()
        return s or None
    # last resort
    try:
        return str(v).strip() or None
    except Exception:
        return None


def clean_tts_language(v: Any) -> str | None:
    """Sanitize language values that may have been surfaced as (code, label) tuples/lists
    from ui.select (when options=list of (val, label)) or as "en (English)" strings.
    Always returns a plain lowercase language code (e.g. "en"), or None.
    """
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        if len(v) > 0:
            v = v[0]
        else:
            v = None
    if isinstance(v, str):
        s = v.strip()
        # Recover if a full "code (Label)" or "code (en)" label string ended up as the value.
        if " (" in s:
            s = s.split(" (", 1)[0].strip()
        s = s.lower()
        return s or None
    # last resort
    try:
        s = str(v).strip().lower()
        return s or None
    except Exception:
        return None


def get_tts_google_default_voice() -> str:
    """Preferred Google Cloud TTS voice (WaveNet recommended)."""
    saved = get_preference("ai.tts_google_default_voice", DEFAULT_TTS_GOOGLE_VOICE)
    cleaned = clean_tts_voice(saved)
    if cleaned and saved != cleaned:
        # auto-repair the stored pref so bad tuple/list data doesn't stick around
        set_tts_google_default_voice(cleaned)
    return cleaned or DEFAULT_TTS_GOOGLE_VOICE


def set_tts_google_default_voice(voice: Any) -> None:
    """Store the preferred Google voice name."""
    cleaned = clean_tts_voice(voice)
    if cleaned:
        set_preference("ai.tts_google_default_voice", cleaned)


# General TTS voice preference (used for both local and google; interpretation depends on provider)
DEFAULT_TTS_VOICE = "en-US-Wavenet-F"  # sensible default, UI will override per provider


def get_tts_voice() -> str:
    """Return the saved TTS voice preference (works for both providers)."""
    # Prefer a general key if set, otherwise fall back to the google one for backwards compat
    saved = get_preference("ai.tts_voice", None)
    cleaned = clean_tts_voice(saved)
    if cleaned:
        if saved != cleaned:
            # auto-repair stored pref (was tuple/list from select) so it doesn't cause downstream errors
            set_tts_voice(cleaned)
        return cleaned
    return get_tts_google_default_voice()


def set_tts_voice(voice: Any) -> None:
    """Store the preferred voice name (for the currently selected provider)."""
    cleaned = clean_tts_voice(voice)
    if cleaned:
        set_preference("ai.tts_voice", cleaned)


# ---------------------------------------------------------------------------
# Optional explicit Google Cloud credentials (service account JSON key).
# This allows using Google Cloud TTS without installing the gcloud CLI at all.
# When set, we inject GOOGLE_APPLICATION_CREDENTIALS so google.auth picks it up.
# ---------------------------------------------------------------------------


def get_gcp_credentials_path() -> str | None:
    """Return a previously chosen Google Cloud service account key JSON path if it exists."""
    p = get_preference("ai.gcp_credentials_path", None)
    if p and isinstance(p, str) and os.path.isfile(p):
        return os.path.abspath(p)
    return None


def set_gcp_credentials_path(path: str | None) -> None:
    """Persist path to a Google Cloud credentials JSON (or clear it)."""
    if path:
        abspath = os.path.abspath(path)
        if os.path.isfile(abspath):
            set_preference("ai.gcp_credentials_path", abspath)
            # Make it active for this process immediately
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = abspath
        else:
            raise ValueError(f"Credentials file does not exist: {path}")
    else:
        # clear
        data = load_settings()
        if "preferences" in data and "ai.gcp_credentials_path" in data["preferences"]:
            del data["preferences"]["ai.gcp_credentials_path"]
            save_settings(data)
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)


# ---------------------------------------------------------------------------
# Proxy Profiles (new 2026 set - exact ffmpeg recipes)
# ---------------------------------------------------------------------------

DEFAULT_PROXY_PRESET = "Apple ProRes Proxy (Standard NLE Workflow)"

PROXY_PRESETS = [
    "Apple ProRes Proxy (Standard NLE Workflow)",
    "Avid DNxHR LB (Windows/Avid Workflow)",
    'H.264 "Performance" Proxy (720p)',
    "HEVC/H.265 (Space-Saving Proxy)",
    "MJPEG Draft (Low-CPU Legacy Proxy)",
]

# Legacy burn flags (still exposed in settings UI for now)
DEFAULT_PROXY_BURN_TIMECODE = True
DEFAULT_PROXY_BURN_SUBTITLES = False


def get_proxy_default_preset() -> str:
    return get_preference("proxy.default_preset", DEFAULT_PROXY_PRESET)


def set_proxy_default_preset(value: str) -> None:
    if value in PROXY_PRESETS:
        set_preference("proxy.default_preset", value)
    else:
        set_preference("proxy.default_preset", DEFAULT_PROXY_PRESET)


# Resolution is no longer a separate user choice — each preset has a fixed one.
# These two functions are kept only to avoid breaking old saved preferences / imports.
def get_proxy_default_resolution() -> str:
    return "720p"


def set_proxy_default_resolution(value: str) -> None:
    pass  # no-op under new preset system


def get_proxy_default_burn_timecode() -> bool:
    return bool(get_preference("proxy.default_burn_timecode", DEFAULT_PROXY_BURN_TIMECODE))


def set_proxy_default_burn_timecode(value: bool) -> None:
    set_preference("proxy.default_burn_timecode", bool(value))


def get_proxy_default_burn_subtitles() -> bool:
    return bool(get_preference("proxy.default_burn_subtitles", DEFAULT_PROXY_BURN_SUBTITLES))


def set_proxy_default_burn_subtitles(value: bool) -> None:
    set_preference("proxy.default_burn_subtitles", bool(value))


# ---------------------------------------------------------------------------
# Default Export Directory
# ---------------------------------------------------------------------------

# New default (per request): inside the catalog tree for convenience.
# All AI exports (Director, Journalist, subtitles, etc.) land in dated subfolders here.
DEFAULT_EXPORT_DIRECTORY = str(Path.home() / "CAT+TAG" / "Exports")


def get_default_export_directory() -> Path:
    """
    Return the user's preferred default export directory.
    New default (when not customized): ~/CAT+TAG/Exports

    - If the user has never set a custom export directory (or had an old default like ~/Downloads),
      we automatically migrate to and persist the new ~/CAT+TAG/Exports location.
    - Custom user choices are always honored.
    - The directory is always ensured to exist.
    """
    # Check what is actually stored (None means "never explicitly set by user code")
    stored = get_preference("export.default_directory", None)

    # Known previous defaults that should trigger auto-migration for users who never customized.
    old_defaults = {
        str(Path.home() / "Downloads"),
        str(Path.home() / "Downloads" / "CAT+TAG"),
        str(Path.home() / "CAT+TAG_Export"),
    }

    if stored is None:
        # First time / no explicit export dir preference -> adopt + persist the new default.
        p = Path(DEFAULT_EXPORT_DIRECTORY).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        set_preference("export.default_directory", str(p))
        return p

    try:
        p = Path(stored).expanduser()
        sp = str(p)

        if sp in old_defaults:
            # Auto-migrate users who had a previous default value.
            new_p = Path(DEFAULT_EXPORT_DIRECTORY).expanduser().resolve()
            new_p.mkdir(parents=True, exist_ok=True)
            set_preference("export.default_directory", str(new_p))
            return new_p

        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
        return p.resolve()
    except Exception:
        # Hard fallback to the (new) default
        fallback = Path(DEFAULT_EXPORT_DIRECTORY).expanduser().resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def set_default_export_directory(path: str | Path) -> None:
    """Persist the user's chosen default export directory."""
    try:
        p = Path(path).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        set_preference("export.default_directory", str(p))
    except Exception as e:
        print(f"[Settings] Failed to set default export directory: {e}")


# ---------------------------------------------------------------------------
# Per-export subfolder creation (always new folder inside default library)
# ---------------------------------------------------------------------------


def _sanitize_folder_name(name: str) -> str:
    """Make a filesystem-safe folder name prefix from a title or suggestion."""
    return sanitize_for_filesystem(name, max_len=60)


def sanitize_for_filesystem(name: str, max_len: int = 80) -> str:
    """Return a safe name usable for folders and file stems (no / : * ? " < > | etc)."""
    if not name:
        return "Export"
    s = re.sub(r'[\\/:*?"<>|]+', "_", name)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    s = s[:max_len]
    return s or "Export"


def create_export_subfolder(suggestion: str | None = None) -> Path:
    """
    ALWAYS create and return a fresh unique subfolder inside the user's
    default export directory (the "default library", e.g. ~/CAT+TAG/Exports).

    This is called on every export of AI Director stories / XML + VO so that
    the new files for one export (the .xml + Narration.wav + Narration_BridgeNN.wav)
    are grouped together in their own dated folder instead of dumped flat.

    The timestamp in the folder name guarantees a new folder for each export run.
    Suggestion (e.g. "AI_Multi_..._Title") is sanitized for the prefix.
    """
    base = get_default_export_directory()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if suggestion:
        safe = _sanitize_folder_name(suggestion)
        folder_name = f"{safe}_{ts}"
    else:
        folder_name = f"Export_{ts}"
    sub = base / folder_name
    sub.mkdir(parents=True, exist_ok=True)
    return sub
