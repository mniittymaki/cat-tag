"""
Voiceover / Narration generation for AI Director.

Two providers supported:

- "local" (default): Piper TTS — fully offline, high-quality neural voices,
  no account / internet (after first model download), no gcloud hassle.
  Excellent native support for Finnish (Asmo high-quality + official Harri) + other languages.

- "google": Google Cloud TTS (WaveNet/Standard) — best quality, uses the
  generous 4M char/month free tier, requires one-time gcloud auth via Settings.

The local provider is recommended for most users. Switch to Google in
Settings → AI only if you want the absolute highest quality cloud voices.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from minicat.core.settings import (
    get_tts_provider,
    clean_tts_voice,
    clean_tts_language,
    get_tts_voice,
    get_gcp_credentials_path,
    SUPPORTED_LANGUAGES,
)
from minicat.core.video import find_ffmpeg

try:
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
except ImportError:
    google = None  # type: ignore
    DefaultCredentialsError = Exception  # type: ignore
    GoogleAuthError = Exception  # type: ignore

# Language code -> Recommended high-quality Edge TTS voice
# These are chosen for naturalness (Neural voices).
LANGUAGE_TO_VOICE: dict[str, str] = {
    "en": "en-US-AriaNeural",       # English (US) - very natural
    "en-us": "en-US-AriaNeural",
    "english": "en-US-AriaNeural",

    "fr": "fr-FR-DeniseNeural",
    "fr-fr": "fr-FR-DeniseNeural",
    "french": "fr-FR-DeniseNeural",

    "es": "es-ES-ElviraNeural",
    "es-es": "es-ES-ElviraNeural",
    "spanish": "es-ES-ElviraNeural",

    "fi": "fi-FI-NooraNeural",
    "finnish": "fi-FI-NooraNeural",

    "de": "de-DE-KatjaNeural",
    "german": "de-DE-KatjaNeural",

    "sv": "sv-SE-SofieNeural",
    "swedish": "sv-SE-SofieNeural",
}

DEFAULT_LANGUAGE = "en"
DEFAULT_VOICE = "en-US-AriaNeural"


def get_language_display_name(lang_code: str) -> str:
    """Return human-readable name for a language code."""
    code = clean_tts_language(lang_code) or (lang_code or "")
    for c, name in SUPPORTED_LANGUAGES:
        if c == code:
            return name
    return (code or lang_code or "").upper()


def get_tts_provider_display_name() -> str:
    """Human readable name of the currently selected TTS provider."""
    provider = get_tts_provider()
    if provider == "local":
        return "Local (Piper TTS - offline)"
    return "Google Cloud TTS"


def get_google_voices_for_language(lang_code: str) -> list[tuple[str, str]]:
    """
    Return a richer list of Google Cloud TTS voices for a language.
    Both Standard and WaveNet voices qualify for the generous free tier
    (up to 4 million characters per month).
    We prioritize WaveNet mainly for higher naturalness/quality.
    """
    lang = clean_tts_language(lang_code) or "en"
    base = GOOGLE_LANGUAGE_TO_VOICE.get(lang, DEFAULT_GOOGLE_VOICE)

    # Use short human-friendly labels for the dropdown (value stays the full id for TTS calls).
    # This keeps the voice pickers clean and short in the UI (TTS tab + cut dialogs).
    def _short_google_label(vid: str, is_recommended: bool = False) -> str:
        # e.g. "fi-FI-Wavenet-A" -> "Wavenet A", "en-US-Standard-C" -> "Standard C"
        parts = vid.split("-")
        kind = parts[-1] if parts else vid
        if "Wavenet" in vid:
            base = f"Wavenet {kind[-1] if kind else ''}".strip()
            return f"{base} (recommended)" if is_recommended else f"{base} (WaveNet)"
        else:
            base = f"Standard {kind[-1] if kind else ''}".strip()
            return f"{base} (recommended)" if is_recommended else f"{base} (Standard)"

    voices = [
        (base, _short_google_label(base, is_recommended=True)),
    ]

    # Expanded good WaveNet + Standard options per language
    alternatives = {
        "en": [
            "en-US-Wavenet-D", "en-US-Wavenet-F",
            "en-US-Standard-C", "en-US-Standard-D",
        ],
        "fi": [
            "fi-FI-Wavenet-A",
            "fi-FI-Standard-A",
        ],
        "fr": [
            "fr-FR-Wavenet-C", "fr-FR-Wavenet-D",
            "fr-FR-Standard-C",
        ],
        "de": [
            "de-DE-Wavenet-B", "de-DE-Wavenet-F",
            "de-DE-Standard-F",
        ],
        "es": [
            "es-ES-Wavenet-B", "es-ES-Wavenet-C",
            "es-ES-Standard-C",
        ],
        "sv": [
            "sv-SE-Wavenet-A",
            "sv-SE-Standard-A",
        ],
    }

    seen = {base}
    for alt in alternatives.get(lang, []):
        if alt not in seen:
            label = _short_google_label(alt)
            voices.append((alt, label))
            seen.add(alt)

    return voices


def ensure_google_tts_package() -> bool:
    """
    Public wrapper: Check if google-cloud-texttospeech is installed.
    If not, attempt to auto-install it using uv pip (preferred for this project) or pip.
    Returns True if the package is available (after possible auto-install).
    """
    return _ensure_google_tts_package()


def _ensure_google_tts_package() -> bool:
    """
    Internal implementation for ensure_google_tts_package.
    Only attempts auto-install once per process.
    """
    global _google_tts_install_tried

    try:
        import google.cloud.texttospeech  # noqa: F401
        return True
    except ImportError:
        pass

    if _google_tts_install_tried:
        return False

    _google_tts_install_tried = True
    print("[TTS] google-cloud-texttospeech not found. Attempting automatic installation...")

    import subprocess
    import sys
    import shutil

    def _try_install(cmd, label):
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                print(f"[TTS] {label} failed. Here is the actual output:")
                if result.stdout:
                    print("--- stdout ---")
                    print(result.stdout.strip()[-2500:])
                if result.stderr:
                    print("--- stderr ---")
                    print(result.stderr.strip()[-2500:])
            except Exception as cap_ex:
                print(f"[TTS] (could not capture detailed output: {cap_ex})")
            return False

    # Prefer uv binary
    uv_bin = shutil.which("uv")
    if uv_bin:
        if _try_install([uv_bin, "pip", "install", "google-cloud-texttospeech"], "uv pip install"):
            try:
                import google.cloud.texttospeech  # noqa: F401
                print("[TTS] Successfully auto-installed google-cloud-texttospeech.")
                return True
            except ImportError:
                pass

    # python -m uv
    if _try_install([sys.executable, "-m", "uv", "pip", "install", "google-cloud-texttospeech"], "python -m uv pip"):
        try:
            import google.cloud.texttospeech  # noqa: F401
            print("[TTS] Successfully auto-installed google-cloud-texttospeech.")
            return True
        except ImportError:
            pass

    # pip fallback
    if _try_install([sys.executable, "-m", "pip", "install", "google-cloud-texttospeech"], "pip install"):
        try:
            import google.cloud.texttospeech  # noqa: F401
            print("[TTS] Successfully auto-installed google-cloud-texttospeech.")
            return True
        except ImportError:
            pass

    print("[TTS] Automatic installation failed.")
    print("      Please run manually: uv pip install google-cloud-texttospeech")
    return False


def find_gcloud() -> Optional[str]:
    """Return absolute path to the gcloud binary if it can be located, else None.

    We search well-known locations because GUI apps (the .app bundle) often
    inherit a very limited PATH that does not include the user's shell PATH
    additions from Homebrew, the official installer, etc.
    """
    # 1. Let the normal environment find it (works if launched from terminal)
    p = shutil.which("gcloud")
    if p and os.path.exists(p):
        return os.path.abspath(p)

    home = os.path.expanduser("~")
    candidates = [
        "/opt/homebrew/bin/gcloud",
        "/usr/local/bin/gcloud",
        f"{home}/google-cloud-sdk/bin/gcloud",
        "/usr/local/google-cloud-sdk/bin/gcloud",
        "/usr/local/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin/gcloud",
        "/opt/google-cloud-sdk/bin/gcloud",
    ]
    for cand in candidates:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return os.path.abspath(cand)

    # 2. Ask brew for the location (very common on macOS)
    try:
        res = subprocess.run(
            ["brew", "--prefix", "google-cloud-sdk"],
            capture_output=True, text=True, timeout=3
        )
        if res.returncode == 0:
            prefix = res.stdout.strip()
            cand = os.path.join(prefix, "bin", "gcloud")
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return os.path.abspath(cand)
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["brew", "--prefix"], capture_output=True, text=True, timeout=3
        )
        if res.returncode == 0:
            prefix = res.stdout.strip()
            cand = os.path.join(prefix, "bin", "gcloud")
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return os.path.abspath(cand)
    except Exception:
        pass

    return None


def is_gcloud_available() -> bool:
    """Quick check if gcloud CLI can be found (used by Settings UI for easy auth)."""
    return find_gcloud() is not None


def run_gcloud_auth_application_default() -> bool:
    """Launch 'gcloud auth application-default login' (opens browser for login).

    Returns True if the process was launched.
    The user must complete the login in the browser.
    After login, either restart the app or click 'Refresh TTS Status'.
    """
    gcloud_bin = find_gcloud() or "gcloud"
    if gcloud_bin == "gcloud" and not is_gcloud_available():
        raise RuntimeError(
            "gcloud CLI not found. Please install Google Cloud SDK first "
            "(https://cloud.google.com/sdk/docs/install) and ensure 'gcloud' is in your PATH."
        )

    try:
        cmd = [gcloud_bin, "auth", "application-default", "login"]
        # Popen so it doesn't block the UI thread; browser will open for interactive login.
        # Using the full path (when we found one) works even if the GUI app has a restricted $PATH.
        subprocess.Popen(cmd)
        return True
    except Exception as e:
        raise RuntimeError(f"Failed to start gcloud authentication: {e}") from e


def get_voice_for_language(language: str | None) -> str:
    """Return a good Edge TTS voice for the given language code or name."""
    if not language:
        return DEFAULT_VOICE
    lang = clean_tts_language(language) or "en"
    return LANGUAGE_TO_VOICE.get(lang, DEFAULT_VOICE)


# ---------------------------------------------------------------------------
# Google Cloud TTS voice mapping
# Both WaveNet and Standard voices qualify for the good free tier:
# up to 4 million characters per month (much better than Neural2/Studio/Chirp).
# We default to WaveNet voices for better naturalness and quality.
# ---------------------------------------------------------------------------
GOOGLE_LANGUAGE_TO_VOICE: dict[str, str] = {
    # English - WaveNet voices are excellent and qualify for the 4M char free tier
    "en": "en-US-Wavenet-F",       # Warm, natural female (recommended)
    "en-us": "en-US-Wavenet-F",
    "english": "en-US-Wavenet-F",

    # Finnish - WaveNet is currently one of the best available
    "fi": "fi-FI-Wavenet-A",       # Good Finnish female voice
    "finnish": "fi-FI-Wavenet-A",

    # French
    "fr": "fr-FR-Wavenet-C",       # Natural female
    "french": "fr-FR-Wavenet-C",

    # German
    "de": "de-DE-Wavenet-F",       # Clear female
    "german": "de-DE-Wavenet-F",

    # Spanish (Spain)
    "es": "es-ES-Wavenet-C",       # Good female
    "spanish": "es-ES-Wavenet-C",

    # Swedish
    "sv": "sv-SE-Wavenet-A",
    "swedish": "sv-SE-Wavenet-A",
}

# Fallback if someone explicitly wants a Standard voice (also qualifies for 4M free tier)
GOOGLE_LANGUAGE_TO_STANDARD_VOICE: dict[str, str] = {
    "en": "en-US-Standard-C",
    "fi": "fi-FI-Standard-A",
    "fr": "fr-FR-Standard-C",
    "de": "de-DE-Standard-F",
    "es": "es-ES-Standard-C",
    "sv": "sv-SE-Standard-A",
}

DEFAULT_GOOGLE_VOICE = "en-US-Wavenet-F"  # WaveNet preferred for quality (both WaveNet & Standard get 4M free tier)

# Module-level flag so we only attempt auto-install of piper-tts once per process
# (prevents log spam when the UI re-renders status or the provider is local).
_piper_install_tried = False
_google_tts_install_tried = False


def reset_piper_install_flag() -> None:
    """Reset the one-time auto-install guard so ensure_piper_package will attempt install again.
    Useful from UI after user runs manual install, or to retry on different env.
    """
    global _piper_install_tried
    _piper_install_tried = False



# =============================================================================
# Local offline TTS via Piper (recommended default - zero cloud, zero auth)
# =============================================================================

# Good quality, reliable Piper voices for our supported languages.
# Models are ~ 30-80 MB each. Downloaded on first use for the language.
# Official voices use the rhasspy/piper-voices layout.
# Finnish has the official Harri (medium/low) + a high-quality community model (Asmo).

PIPER_VOICES: dict[str, list[dict]] = {
    "en": [{
        "id": "en_US-lessac-medium",
        "name": "Lessac (clear, natural US English)",
        "url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
    }],
    "fi": [
        {
            # High-quality community Finnish voice (2025, trained from scratch on synthetic data).
            # Generally considered the most natural Finnish Piper voice available.
            # License: CC-BY-NC-4.0 (non-commercial). Model ID follows standard Piper naming.
            "id": "fi_FI-asmo-medium",
            "name": "Asmo (Finnish, high quality)",
            "url": "https://huggingface.co/AsmoKoskinen/Piper_Finnish_Model/resolve/main/fi_FI-asmo-medium.onnx",
        },
        {
            "id": "fi_FI-harri-medium",
            "name": "Harri (Finnish, official)",
            "url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fi/fi_FI/harri/medium/fi_FI-harri-medium.onnx",
        },
        {
            "id": "fi_FI-harri-low",
            "name": "Harri (Finnish, low quality)",
            "url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fi/fi_FI/harri/low/fi_FI-harri-low.onnx",
        },
    ],
    "de": [{
        "id": "de_DE-thorsten-medium",
        "name": "Thorsten (German)",
        "url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx",
    }],
    "fr": [{
        "id": "fr_FR-siwis-medium",
        "name": "Siwis (French)",
        "url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fr/fr_FR/siwis/medium/fr_FR-siwis-medium.onnx",
    }],
    "es": [{
        "id": "es_ES-davefx-medium",
        "name": "Davefx (Spanish)",
        "url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/es/es_ES/davefx/medium/es_ES-davefx-medium.onnx",
    }],
    "sv": [{
        "id": "sv_SE-nst-medium",
        "name": "NST (Swedish)",
        "url": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/sv/sv_SE/nst/medium/sv_SE-nst-medium.onnx",
    }],
}


def _piper_cache_dir() -> Path:
    d = Path.home() / ".minicat" / "tts" / "piper"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download_piper_model(voice_info: dict) -> Path:
    """Download the .onnx + accompanying .json config for a Piper voice.
    Shows simple progress in the console. Returns path to the .onnx file.
    """
    cache = _piper_cache_dir()
    onnx_name = voice_info["id"] + ".onnx"
    onnx_path = cache / onnx_name
    json_path = cache / (onnx_name + ".json")

    if onnx_path.exists() and json_path.exists() and onnx_path.stat().st_size > 1000:
        return onnx_path

    print(f"[TTS] Downloading Piper offline voice: {voice_info.get('name', voice_info['id'])}")
    print(f"[TTS]   (first time only, ~30-80 MB, stored in {cache})")

    import urllib.request

    def _progress(block_num, block_size, total_size):
        if total_size > 0:
            pct = min(100.0, (block_num * block_size * 100.0) / total_size)
            # Only log at nice round milestones to avoid flooding the console
            if int(pct) in (0, 20, 40, 60, 80, 100) or (int(pct) % 25 == 0):
                print(f"  {pct:.0f}% downloaded...")

    for extra, target in [("", onnx_path), (".json", json_path)]:
        url = voice_info["url"] + extra
        tmp = target.with_suffix(target.suffix + ".download")
        try:
            urllib.request.urlretrieve(url, tmp, reporthook=_progress)
            tmp.replace(target)
            print(f"[TTS]   Saved {target.name}")
        except Exception as ex:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"Failed to download Piper model ({voice_info['id']}). "
                f"Check your internet connection or download manually from {url}"
            ) from ex

    return onnx_path


def ensure_piper_voice_model(language: str | None = None, voice_id: Optional[str] = None) -> Path:
    """Make sure a suitable Piper .onnx model exists locally for the language.
    Auto-downloads the recommended (first) voice for the language on first use.
    Supports multiple voices per language (e.g. Finnish now has Asmo high-quality + Harri medium/low).
    Returns the full path to the .onnx file.
    """
    voice_id = clean_tts_voice(voice_id)
    if voice_id:
        for lang_voices in PIPER_VOICES.values():
            for info in lang_voices:
                if info["id"] == voice_id:
                    return _download_piper_model(info)
        # Unknown id passed — try to treat the id itself as a stem in cache (advanced users)
        cache = _piper_cache_dir()
        candidate = cache / (voice_id if voice_id.endswith(".onnx") else voice_id + ".onnx")
        if candidate.exists():
            return candidate

    lang = clean_tts_language(language) or "en"
    lang_voices = PIPER_VOICES.get(lang, PIPER_VOICES["en"])
    info = lang_voices[0] if lang_voices else PIPER_VOICES["en"][0]
    return _download_piper_model(info)


def is_piper_available() -> bool:
    """Quick check whether the piper-tts package is importable."""
    try:
        from piper import PiperVoice  # noqa: F401
        return True
    except Exception:
        return False


def ensure_piper_package() -> bool:
    """Auto-install 'piper-tts' (uv binary preferred, then uv module, then pip) if missing.
    Returns True if the package is now available.
    Only attempts auto-install once per Python process to avoid repeated log spam
    when the Translation settings page re-renders status.
    """
    global _piper_install_tried

    if is_piper_available():
        return True

    if _piper_install_tried:
        return False

    _piper_install_tried = True
    print("[TTS] piper-tts not installed. Attempting automatic installation (local/offline TTS)...")

    import subprocess
    import sys
    import shutil

    def _try_install(cmd, label):
        """Run the install. On failure, re-execute once with output capture so the
        user sees the *real* pip/uv error instead of a generic exception.
        """
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Re-run to capture the actual error output for the user
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                print(f"[TTS] {label} failed. Here is the actual output:")
                if result.stdout:
                    print("--- stdout (last 3000 chars) ---")
                    print(result.stdout.strip()[-3000:])
                if result.stderr:
                    print("--- stderr (last 3000 chars) ---")
                    print(result.stderr.strip()[-3000:])
            except Exception as cap_err:
                print(f"[TTS] (could not re-capture detailed output: {cap_err})")
            return False

    # Prioritize installing directly into the *current running Python environment*.
    # This guarantees that `is_piper_available()` (the import check) will succeed
    # in *this process* without requiring an app restart. Critical for "seems offline" UX.
    if _try_install([sys.executable, "-m", "pip", "install", "piper-tts"], "python -m pip install"):
        if is_piper_available():
            print("[TTS] Successfully installed piper-tts for fully offline voiceovers.")
            return True

    # Then try uv (project-managed, recommended for consistency)
    uv_bin = shutil.which("uv")
    if uv_bin:
        if _try_install([uv_bin, "pip", "install", "piper-tts"], "uv pip install"):
            if is_piper_available():
                print("[TTS] Successfully installed piper-tts for fully offline voiceovers.")
                return True

    # Fallback: python -m uv (if uv was pip-installed into venv)
    if _try_install([sys.executable, "-m", "uv", "pip", "install", "piper-tts"], "python -m uv pip install"):
        if is_piper_available():
            print("[TTS] Successfully installed piper-tts for fully offline voiceovers.")
            return True

    print("[TTS] Automatic installation of piper-tts failed.")
    print("      Recommended (this project is managed with uv):")
    print("          uv pip install piper-tts")
    print("      Or for a clean install with the rest of the project:")
    print("          uv pip install '.[tts]'")
    print("      Then restart the app or toggle the TTS provider in Settings.")
    return False


def get_local_tts_status() -> dict:
    """Status for the local (Piper) provider, used by Settings and exporters."""
    provider = "local"
    if not ensure_piper_package():
        return {
            "ready": False,
            "provider": provider,
            "message": "Local offline TTS (Piper) selected but 'piper-tts' is not installed "
                       "(auto-install also failed). Run: uv pip install piper-tts",
            "fallback_used": False,
        }

    # Package is there. Models are downloaded lazily on first use per language.
    # We consider it "ready" as long as the package is present (models will be fetched when needed).
    return {
        "ready": True,
        "provider": provider,
        "message": "Local offline TTS ready (Piper neural voices, completely local after first download).",
        "fallback_used": False,
    }


def get_tts_status() -> dict:
    """Unified status for whatever provider is currently selected in Settings.
    Used by the UI and by exporters to give clear messages.
    """
    provider = get_tts_provider()
    if provider == "local":
        return get_local_tts_status()
    return get_google_tts_status()


def get_piper_voices_for_language(lang_code: str) -> list[tuple[str, str]]:
    """Return Piper voice options for the Settings voice selector (per language).
    Labels are short friendly names (the technical id is the value, not repeated in label).
    Some languages (Finnish) now expose multiple voices: Asmo (high quality community) + official Harri (medium + low).
    """
    lang = clean_tts_language(lang_code) or "en"
    if lang in PIPER_VOICES:
        return [(v["id"], v["name"]) for v in PIPER_VOICES[lang]]
    return [(v["id"], v["name"]) for v in PIPER_VOICES["en"]]


# =============================================================================
# Google TTS Readiness Detection (for better UX in narrative exporter + UI)
# =============================================================================

def is_google_tts_available() -> bool:
    """
    Returns True if the google-cloud-texttospeech package is installed
    AND Application Default Credentials are configured (either via gcloud
    login or via an explicit credentials JSON file chosen in Settings).
    """
    try:
        from google.cloud import texttospeech  # noqa: F401
    except ImportError:
        return False

    # If the user picked a credentials file in Settings, make sure the
    # environment variable is set for google-auth (works even without gcloud CLI).
    try:
        creds_path = get_gcp_credentials_path()
        if creds_path:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", creds_path)
    except Exception:
        pass

    # Check for usable credentials without making a real TTS call.
    try:
        import google.auth
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return credentials is not None
    except Exception:
        # Covers DefaultCredentialsError, GoogleAuthError, etc.
        return False


def get_google_tts_status() -> dict:
    """
    Returns a rich status object useful for UI and exporters.

    Keys:
      - ready: bool
      - provider: "google"
      - message: human readable explanation
      - fallback_used: False (no fallbacks in Google-only mode)
    """
    provider = get_tts_provider()

    if provider != "google":
        return {
            "ready": False,
            "provider": "google",
            "message": "Only Google TTS is supported.",
            "fallback_used": False,
        }

    # User wants Google - automatically install the package if missing
    if not ensure_google_tts_package():
        return {
            "ready": False,
            "provider": "google",
            "message": "Google Cloud TTS selected but 'google-cloud-texttospeech' is not installed "
                       "(auto-install also failed). Run: uv pip install google-cloud-texttospeech",
            "fallback_used": False,
        }

    # Make sure an explicit credentials file (if chosen in Settings) is active
    creds_path = None
    try:
        creds_path = get_gcp_credentials_path()
        if creds_path:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", creds_path)
    except Exception:
        pass

    if is_google_tts_available():
        if creds_path:
            msg = f"Google Cloud TTS ready (using credentials file: {Path(creds_path).name})."
        else:
            msg = "Google Cloud TTS ready (WaveNet/Standard voices, 4M char free tier)."
        return {
            "ready": True,
            "provider": "google",
            "message": msg,
            "fallback_used": False,
            "credentials_path": creds_path,
        }
    else:
        if creds_path:
            msg = (f"Google Cloud TTS selected but the credentials file "
                   f"{Path(creds_path).name} is invalid or not authorized for Text-to-Speech. "
                   "Check the file or re-select it in Settings.")
        else:
            msg = ("Google Cloud TTS selected but credentials are not configured. "
                   "Use the 'Log in with Google' button, or pick a service account JSON key file below.")
        return {
            "ready": False,
            "provider": "google",
            "message": msg,
            "fallback_used": False,
            "credentials_path": creds_path,
        }


def ensure_tts_ready_for_narration() -> tuple[str, bool]:
    """
    Used by narrative_vo_exporter and export dialogs.

    Returns (effective_provider, fell_back)
    fell_back is always False now (we no longer do silent fallbacks).
    """
    status = get_tts_status()
    provider = status.get("provider", get_tts_provider())
    if not status.get("ready"):
        return provider, False
    return provider, False


async def generate_narration_audio(
    text: str,
    *,
    language: str = DEFAULT_LANGUAGE,
    output_path: str | Path,
    voice: Optional[str] = None,
) -> Path:
    """
    Generate narration audio using the currently selected TTS provider
    ("local" = Piper offline, or "google" = Google Cloud TTS).

    Raises a clear RuntimeError with actionable instructions if the chosen
    provider is not ready (missing package, credentials, or download failure).
    No silent fallbacks.
    """
    provider = get_tts_provider()

    # The voice passed from UI selects can occasionally be a (id, label) tuple/list
    # because of how options are provided to nicegui ui.select. Clean it here for all providers.
    voice = clean_tts_voice(voice)
    # Language can similarly arrive as tuple from lang selects using (code, label) options.
    language = clean_tts_language(language) or DEFAULT_LANGUAGE

    if provider == "local":
        # Local Piper is synchronous under the hood; we still expose async API.
        status = get_local_tts_status()
        if not status["ready"]:
            raise RuntimeError(status["message"])

        # Piper generation is CPU-bound + possible first-time download.
        # Offload so we don't block the event loop.
        def _do_local():
            return _generate_with_piper(text, language, output_path, voice)

        import asyncio
        return await asyncio.to_thread(_do_local)

    elif provider == "google":
        status = get_google_tts_status()
        if not status["ready"]:
            raise RuntimeError(status["message"])

        try:
            return await _generate_with_google_tts(text, language, output_path, voice)
        except Exception as ex:
            raise RuntimeError(f"Google Cloud TTS failed: {ex}") from ex
    else:
        raise RuntimeError(f"Unknown TTS provider: {provider}. Choose 'local' or 'google' in Settings.")


def _generate_with_piper(
    text: str,
    language: str,
    output_path: str | Path,
    voice: Optional[str] = None,
) -> Path:
    """Fully offline Piper TTS generation.
    Downloads the model for the language on first use if needed.
    Output is always WAV, stereo, 44100 Hz (as requested).
    """
    try:
        from piper import PiperVoice
    except ImportError:
        raise RuntimeError(
            "Local offline TTS requires the 'piper-tts' package. "
            "Install with: uv pip install piper-tts"
        )

    # Pick voice: explicit (already cleaned by caller) > saved for local > recommended for language
    if voice:
        voice_id = voice
    else:
        saved = get_tts_voice()
        # get_tts_voice now cleans, but double-defend for legacy direct calls or old data.
        cleaned_saved = clean_tts_voice(saved)
        # If the saved voice looks like a Google Cloud name while using local, ignore it.
        if cleaned_saved and not cleaned_saved.startswith(("en_US", "fi_FI", "de_DE", "fr_FR", "es_ES", "sv_SE")):
            cleaned_saved = None
        voice_id = cleaned_saved or None

    model_path = ensure_piper_voice_model(language, voice_id)

    voice_obj = PiperVoice.load(str(model_path))

    out_path = Path(output_path).expanduser().resolve()
    # Force .wav for Piper (stereo 44.1kHz)
    if out_path.suffix.lower() != ".wav":
        out_path = out_path.with_suffix(".wav")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Synthesize to native rate/mono WAV first, then convert with ffmpeg
    # (Piper models are usually 22050 or 16000 Hz mono)
    with tempfile.TemporaryDirectory() as tmpd:
        tmpdir = Path(tmpd)
        native_wav = tmpdir / "piper_native.wav"

        # Write native WAV using wave (mono, model's rate, 16-bit)
        import wave
        with wave.open(str(native_wav), "wb") as wav_file:
            voice_obj.synthesize_wav(text, wav_file)

        # Convert to 44100 Hz stereo WAV using ffmpeg (duplicate channels for stereo) -- same as all other VO files and Narration.wav
        ffmpeg = find_ffmpeg()
        cmd = [
            str(ffmpeg), "-y",
            "-i", str(native_wav),
            "-ar", "44100",
            "-ac", "2",
            "-c:a", "pcm_s16le",
            str(out_path)
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode(errors="ignore")[:500]
            raise RuntimeError(f"ffmpeg failed to convert Piper output to 44100Hz stereo WAV: {err}") from e

    return out_path


async def _generate_with_edge_tts(
    text: str,
    language: str,
    output_path: str | Path,
    voice: Optional[str],
) -> Path:
    """Original edge-tts implementation (free, zero config)."""
    import edge_tts

    out_path = Path(output_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    chosen_voice = voice or get_voice_for_language(language)

    communicate = edge_tts.Communicate(text, chosen_voice)
    await communicate.save(str(out_path))

    return out_path


async def _generate_with_google_tts(
    text: str,
    language: str,
    output_path: str | Path,
    voice: Optional[str],
) -> Path:
    """
    Google Cloud Text-to-Speech implementation.
    Both WaveNet and Standard voices qualify for up to 4 million characters
    per month for free (much better than Neural2/Studio/Chirp voices).
    """
    try:
        from google.cloud import texttospeech
    except ImportError:
        raise RuntimeError(
            "Google Cloud TTS support requires 'google-cloud-texttospeech'. "
            "Install it with: uv pip install google-cloud-texttospeech"
        )

    out_path = Path(output_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Choose voice
    voice = clean_tts_voice(voice)
    if voice:
        voice_name = voice
        # Try to guess language code from voice name if possible
        lang_code = language or "en-US"
    else:
        lang = clean_tts_language(language) or "en"
        voice_name = GOOGLE_LANGUAGE_TO_VOICE.get(lang, DEFAULT_GOOGLE_VOICE)
        # Extract language code from voice name (e.g. "en-US-Neural2-F" → "en-US")
        lang_code = voice_name.split("-")[0] + "-" + voice_name.split("-")[1] if "-" in voice_name else "en-US"

    client = texttospeech.TextToSpeechClient()

    synthesis_input = texttospeech.SynthesisInput(text=text)

    voice_params = texttospeech.VoiceSelectionParams(
        language_code=lang_code,
        name=voice_name,
    )

    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=44100,
    )

    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice_params,
        audio_config=audio_config,
    )

    # Write as temp linear16 mono, then convert to 44100 stereo pcm WAV (exactly matching the Narration.wav format)
    # so ALL VO audio files (bridges and full Narration script) have the same format: 44100Hz stereo 16-bit PCM WAV
    # (no more "stereo mapped to 2 mono" difference)
    native_wav = out_path.with_suffix('.tmp.linear16.wav')
    native_wav.write_bytes(response.audio_content)

    final_out = out_path.with_suffix('.wav')
    ffmpeg = find_ffmpeg()
    cmd = [
        str(ffmpeg), "-y",
        "-i", str(native_wav),
        "-ar", "44100",
        "-ac", "2",
        "-c:a", "pcm_s16le",
        str(final_out)
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode(errors="ignore")[:500]
        if native_wav.exists():
            native_wav.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed to convert Google TTS output to 44100Hz stereo WAV: {err}") from e

    if native_wav.exists():
        native_wav.unlink(missing_ok=True)

    return final_out


def generate_narration_audio_sync(
    text: str,
    *,
    language: str = DEFAULT_LANGUAGE,
    output_path: str | Path,
    voice: Optional[str] = None,
) -> Path:
    """
    Synchronous wrapper around generate_narration_audio.
    Works for both "local" (Piper) and "google" providers.
    Safe to call from within a running asyncio event loop (NiceGUI etc.).
    """
    coro = generate_narration_audio(
        text=text,
        language=language,
        output_path=output_path,
        voice=voice,
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We are inside a running event loop (NiceGUI etc.).
        # Offload the async work to a fresh thread + new loop.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    else:
        return asyncio.run(coro)
