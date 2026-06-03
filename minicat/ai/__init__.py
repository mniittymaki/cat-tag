"""
AI modules for CAT+TAG.

Currently includes:
- Transcription + translation (with Finnish broadcast formatting)
- Tag suggestions from storyboards (video) or transcripts (audio + video)
- AI-powered journalist cutting / interview editing
"""

from minicat.core.settings import set_gcp_credentials_path  # alternative Google auth path (no CLI)

from .director import build_combined_labeled_transcript, generate_director_cuts
from .fcpxml_exporter import create_fcpxml, create_sequence
from .journalist_cutter import generate_journalist_cuts
from .tag_suggester import suggest_tags_from_storyboard, suggest_tags_from_transcript
from .transcriber import (
    parse_transcription_txt_to_segments,
    transcribe_audio_with_timestamps,
    translate_transcription_segments,
)
from .voiceover import (
    ensure_google_tts_package,
    ensure_piper_package,
    ensure_piper_voice_model,
    ensure_tts_ready_for_narration,
    find_gcloud,
    generate_narration_audio,
    generate_narration_audio_sync,
    get_gcp_credentials_path,
    get_google_tts_status,
    get_local_tts_status,
    get_piper_voices_for_language,
    get_tts_provider,
    get_tts_provider_display_name,
    get_tts_status,
    is_gcloud_available,
    is_google_tts_available,
    is_piper_available,
    reset_piper_install_flag,
    run_gcloud_auth_application_default,
)
from .xmeml_exporter import create_xmeml, generate_xmeml

__all__ = [
    "generate_journalist_cuts",
    "generate_director_cuts",
    "build_combined_labeled_transcript",
    "suggest_tags_from_storyboard",
    "suggest_tags_from_transcript",
    "transcribe_audio_with_timestamps",
    "translate_transcription_segments",
    "parse_transcription_txt_to_segments",
    "create_sequence",
    "create_fcpxml",
    "generate_xmeml",
    "create_xmeml",
    "generate_narration_audio",
    "generate_narration_audio_sync",
    "get_tts_provider",
    "get_tts_provider_display_name",
    "get_tts_status",
    "get_google_tts_status",
    "get_local_tts_status",
    "ensure_tts_ready_for_narration",
    "is_google_tts_available",
    "ensure_google_tts_package",
    "is_gcloud_available",
    "find_gcloud",
    "run_gcloud_auth_application_default",
    "get_gcp_credentials_path",
    "set_gcp_credentials_path",
    "is_piper_available",
    "ensure_piper_package",
    "reset_piper_install_flag",
    "get_piper_voices_for_language",
    "ensure_piper_voice_model",
]
