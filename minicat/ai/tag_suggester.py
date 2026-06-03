"""AI-powered tag suggestions using Gemini.

Supports two modes:
- Vision: from storyboard image (suggest_tags_from_storyboard)
- Text: from audio/video transcription (suggest_tags_from_transcript)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from minicat.core.settings import DEFAULT_GEMINI_MODEL, GEMINI_MODELS


def suggest_tags_from_storyboard(
    storyboard_path: str | Path,
    api_key: str,
    *,
    max_tags: int = 8,
    min_tags: int = 3,
    model_name: str = DEFAULT_GEMINI_MODEL,
) -> list[str]:
    """
    Analyze a storyboard image with Gemini and return suggested tags.

    Returns between min_tags and max_tags concise, useful tags.
    If an invalid or deprecated model is passed, it automatically falls back.
    """
    # Safety net: validate model name
    if model_name not in GEMINI_MODELS:
        print(f"[AI] Warning: Model '{model_name}' is not in the supported list. "
              f"Falling back to {DEFAULT_GEMINI_MODEL}.")
        model_name = DEFAULT_GEMINI_MODEL

    try:
        from google import genai
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "AI features require 'google-genai' and 'pillow'. "
            "Please run: uv pip install google-genai pillow"
        ) from e

    if not api_key or not api_key.strip():
        raise ValueError("Gemini API key is required for AI tag suggestions.")

    storyboard_path = Path(storyboard_path)
    if not storyboard_path.exists():
        raise FileNotFoundError(f"Storyboard not found: {storyboard_path}")

    client = genai.Client(api_key=api_key.strip())

    # Load the storyboard image
    image = Image.open(storyboard_path)

    prompt = f"""
You are an expert video cataloger and archivist.

Analyze this storyboard (a grid of representative frames from a video clip).

Suggest between {min_tags} and {max_tags} concise, useful tags that would help someone find and organize this footage later.

Guidelines:
- Focus on visual content, actions, environment, mood, style, and objects.
- Use natural, searchable language (not too generic like "video" or "footage").
- Prefer specific over vague when possible.
- Output ONLY a JSON array of strings, nothing else. Example: ["interview", "outdoor", "golden hour", "drone shot"]

Return exactly a JSON array.
"""

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt, image],
        )

        # Clean up the response
        text = response.text.strip()

        # Try to extract JSON array
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text

        # Parse the JSON
        tags = json.loads(text)

        if not isinstance(tags, list):
            return []

        # Clean and filter tags
        cleaned: list[str] = []
        for tag in tags:
            if isinstance(tag, str):
                t = tag.strip().lower()
                if t and len(t) > 1 and t not in cleaned:
                    cleaned.append(t)

        # Enforce min/max
        if len(cleaned) > max_tags:
            cleaned = cleaned[:max_tags]
        if len(cleaned) < min_tags and len(cleaned) > 0:
            pass

        return cleaned

    except Exception as e:
        error_str = str(e)
        if "401" in error_str or "invalid authentication" in error_str.lower() or "ACCESS_TOKEN_TYPE_UNSUPPORTED" in error_str:
            raise RuntimeError(
                "Invalid Gemini API key.\n\n"
                "Please make sure you created the key at:\n"
                "https://aistudio.google.com/app/apikey\n\n"
                "Keys created in Google Cloud Console usually do not work with this library."
            ) from e

        raise RuntimeError(f"Failed to get tag suggestions from Gemini: {e}") from e


def suggest_tags_from_transcript(
    segments: list[dict[str, Any]] | str,
    api_key: str,
    *,
    max_tags: int = 8,
    min_tags: int = 3,
    model_name: str = DEFAULT_GEMINI_MODEL,
) -> list[str]:
    """
    Analyze a transcription (from audio or video) with Gemini text model
    and return suggested tags.

    Especially useful for pure audio files that have no visual storyboard.

    Accepts either:
    - A list of segment dicts (each with a "text" key)
    - Or a single string containing the full transcript text
    """
    # Safety net: validate model name
    if model_name not in GEMINI_MODELS:
        print(f"[AI] Warning: Model '{model_name}' is not in the supported list. "
              f"Falling back to {DEFAULT_GEMINI_MODEL}.")
        model_name = DEFAULT_GEMINI_MODEL

    try:
        from google import genai
    except ImportError as e:
        raise RuntimeError(
            "AI features require 'google-genai'. "
            "Please run: uv pip install google-genai"
        ) from e

    if not api_key or not api_key.strip():
        raise ValueError("Gemini API key is required for AI tag suggestions.")

    # Build plain transcript text
    if isinstance(segments, str):
        transcript_text = segments.strip()
    else:
        parts: list[str] = []
        for seg in (segments or []):
            t = ""
            if isinstance(seg, dict):
                t = seg.get("text") or seg.get("content") or ""
            elif isinstance(seg, str):
                t = seg
            if t:
                parts.append(str(t).strip())
        transcript_text = " ".join(parts)

    if not transcript_text or len(transcript_text) < 10:
        raise ValueError("Transcript is empty or too short for meaningful tag suggestions.")

    # Truncate very long transcripts (Gemini has context limits; we only need the essence)
    if len(transcript_text) > 12000:
        transcript_text = transcript_text[:12000] + " ... [truncated]"

    client = genai.Client(api_key=api_key.strip())

    prompt = f"""
You are an expert media archivist and librarian specializing in audio and video collections.

Here is the full transcript of a clip (it may be an interview, podcast, speech, field recording, music with lyrics, etc.):

--- TRANSCRIPT START ---
{transcript_text}
--- TRANSCRIPT END ---

Your task: Suggest between {min_tags} and {max_tags} concise, useful, searchable tags that would help someone find and organize this recording later in a professional catalog.

Guidelines:
- Focus on spoken topics, people, places, organizations, themes, emotions, events, technical content, and style/tone.
- For interviews/podcasts: include speaker names or roles if clear, main subjects discussed.
- For music or performance: genre, mood, instrumentation, language, era if detectable.
- Use natural, specific language (avoid ultra-generic words like "audio", "speech", "talk", "recording").
- Prefer proper nouns and distinctive phrases when they appear.
- Output ONLY a JSON array of strings, nothing else.
  Example: ["climate policy", "marine biology", "helsinki", "interview", "sustainability", "arctic"]

Return exactly a JSON array.
"""

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt],   # pure text, no image
        )

        text = response.text.strip()

        # Try to extract JSON array (handle markdown code fences)
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
            text = text.replace("```json", "").replace("```", "").strip()

        tags = json.loads(text)

        if not isinstance(tags, list):
            return []

        # Clean and filter
        cleaned: list[str] = []
        for tag in tags:
            if isinstance(tag, str):
                t = tag.strip().lower()
                if t and len(t) > 1 and t not in cleaned:
                    cleaned.append(t)

        if len(cleaned) > max_tags:
            cleaned = cleaned[:max_tags]

        return cleaned

    except Exception as e:
        error_str = str(e)
        if "401" in error_str or "invalid authentication" in error_str.lower() or "ACCESS_TOKEN_TYPE_UNSUPPORTED" in error_str:
            raise RuntimeError(
                "Invalid Gemini API key.\n\n"
                "Please make sure you created the key at:\n"
                "https://aistudio.google.com/app/apikey\n\n"
                "Keys created in Google Cloud Console usually do not work with this library."
            ) from e

        raise RuntimeError(f"Failed to get tag suggestions from transcript: {e}") from e
