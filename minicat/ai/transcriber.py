"""Audio transcription using Gemini (with timestamps)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from minicat.core.settings import DEFAULT_GEMINI_MODEL, GEMINI_MODELS


def transcribe_audio_with_timestamps(
    audio_path: str | Path,
    api_key: str,
    *,
    model_name: str = DEFAULT_GEMINI_MODEL,
    language: str | None = None,
    fps: float | None = None,
    total_duration: float | None = None,
) -> list[dict[str, Any]]:
    """
    Transcribe an audio file using Gemini.

    IMPORTANT: Callers MUST confirm the source video framerate (using
    minicat.core.video.confirm_video_framerate) and pass fps= before calling this.
    The fps is used to quantize the (AI-provided) segment times to the video's
    exact frame grid after transcription, and for timecode formatting in output.
    The model itself is asked only for best-effort accurate seconds since the
    start of the provided audio (no frame-alignment pressure in the prompt).

    Pass total_duration (the exact length of the source video in seconds) so
    Gemini knows the full expected length and does not stop early on silence.

    Returns:
        {
            "segments": [ {"start": 12.34, "end": 15.67, "text": "..."}, ... ],
            "language": "en" | None
        }

    Philosophy (updated):
    - Primary goal: produce **natural spoken segments** (complete sentences / utterances)
      instead of aggressively chopped subtitle blocks.
    - Strict subtitle line-length rules are de-emphasized during raw transcription.
    - High-quality subtitle formatting is still available as a separate post-processing
      step when burning or exporting subtitles.
    """
    if model_name not in GEMINI_MODELS:
        print(f"[AI] Warning: Model '{model_name}' not in supported list. Falling back.")
        model_name = DEFAULT_GEMINI_MODEL

    try:
        from google import genai
    except ImportError as e:
        raise RuntimeError(
            "Transcription requires 'google-genai'. Run: uv pip install google-genai"
        ) from e

    if not api_key or not api_key.strip():
        raise ValueError("Gemini API key is required for transcription.")

    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    client = genai.Client(api_key=api_key.strip())

    # Read audio as bytes
    audio_bytes = audio_path.read_bytes()
    suffix = audio_path.suffix.lower()
    if suffix == ".wav":
        mime_type = "audio/wav"
    elif suffix in (".m4a", ".aac", ".mp4"):
        # .m4a is the production transcription proxy (AAC). "audio/mp4" is the most reliable
        # mime for Gemini when feeding AAC-in-m4a-container files.
        mime_type = "audio/mp4"
    elif suffix in (".mp3", ".mpeg", ".mpg"):
        mime_type = "audio/mpeg"
    else:
        mime_type = "audio/wav"  # safe fallback

    # Use the proper google-genai Part object for binary/audio data
    from google.genai import types

    audio_part = types.Part.from_bytes(
        data=audio_bytes,
        mime_type=mime_type,
    )

    # Build a language-aware, neutral prompt.
    if language:
        target_lang = language.lower().strip()
        lang_instruction = f"Transcribe the audio in {target_lang}."
    else:
        target_lang = None
        lang_instruction = "First, accurately detect the spoken language in the audio."

    # ENHANCED: Enforce strict granularity limits so the AI Director can easily cut and trim material.
    if target_lang == "fi":
        style_instruction = """For Finnish, create granular, bite-sized segments to enable precise multi-clip editing. Each segment should ideally be 2 to 5 seconds long (maximum 6 seconds). Split long sentences or multi-clause utterances actively at natural break points like commas, breathing pauses, or conjunctions (e.g., ja, mutta, tai, niin, sit). Never let a single segment span too many continuous words."""
    else:
        style_instruction = """Create granular, bite-sized segments to enable precise multi-clip editing. Each segment should ideally be 2 to 5 seconds long (maximum 6 seconds). Split long continuous speech or compound sentences actively at natural pause points, commas, or conjunctions (e.g., and, but, or). Never let a single segment span too many continuous words."""

    guidelines = [
        "TRANSCRIBE VERBATIM - ABSOLUTE RULE (this takes precedence over EVERYTHING else, including all granularity, style, length, or formatting instructions): You must output the EXACT words the speaker uttered, in the exact order spoken, with no changes whatsoever. Do not filter, censor, substitute, rephrase, correct grammar, improve flow, turn fragments into sentences, or 'clean up' anything. If the speaker said 'läskiaktivisti', 'perkele', or any self-identified / dialect / non-standard / sensitive term, output it literally exactly as heard. The text must be a direct verbatim record of the spoken words - character for character where possible - not an edited or normalized version. Your only job is to transcribe; you are not allowed to act as an editor, writer, or language corrector.",
        "Prioritize granular, punchy segments over long compound sentences.",
        "Split sentences at commas, conjunctions, or breathing pauses so that no segment spans more than 6 seconds of speech.",
        'Return a valid JSON **object** with exactly this structure:\n  {\n    "language": "en",     // ISO 639-1 code of the spoken language you used/detected\n    "segments": [\n      {"start": "00:01:23.450", "end": "00:01:25.180", "text": "Bite-sized segment or sub-clause"},\n      ...\n    ]\n  }',
        '"language" must be the actual spoken language (lowercase ISO code).',
        "Timestamps MUST be strictly accurate and realistic:\n  * Every segment MUST have start < end.\n  * Segments MUST be in strictly increasing chronological order (each start >= previous segment end).\n  * All timestamps must be between 0 and the actual length of the provided audio.\n  * Be conservative: do not start a segment before the first audible word or breath.\n  * Aim for sub-second accuracy; errors larger than ~0.8 s on clear speech are unacceptable.",
    ]
    if total_duration and total_duration > 0:
        guidelines.append(
            f"The total duration of this audio clip is exactly {total_duration:.1f} seconds. "
            "Transcribe the COMPLETE audio from start to finish up to that exact duration. "
            "If there are genuine long silences, use larger gaps between segments rather than omitting content or bunching timestamps."
        )
    guidelines.append(
        "Text must be EXACT verbatim spoken words with zero editing or normalization (see the TRANSCRIBE VERBATIM rule at the top of the guidelines)."
    )
    guidelines.append("Do not add explanations or markdown — return only the JSON object.")

    guidelines_text = "\n- ".join(guidelines)
    prompt = f"""
You are an expert transcriber specializing in video-editing ready workflows.

Task:
{lang_instruction}

Transcribe the audio into concise, granular segments. 
CRITICAL GRANULARITY RULE (SUBORDINATE TO VERBATIM RULE): This transcription is consumed by an automated narrative editor (AI Director) that requires precise, short segments to clip, rearrange, and intercut. Therefore, segments must be short. Aim for an ideal duration of 2 to 5 seconds per segment (maximum 6 seconds or roughly 10-14 words). Split by breaking at natural boundaries ONLY - you may insert a segment break, but you MUST NEVER change, reword, summarize, or 'improve' any of the spoken words themselves to make the split nicer. The words inside every segment must be exactly what was said.

**Cover the ENTIRE recording from the very first sound to the last. Do not skip the beginning, do not skip the middle, do not skip the end.**

TRANSCRIBE VERBATIM - THIS IS THE HIGHEST PRIORITY RULE AND OVERRIDES THE GRANULARITY RULE AND ALL OTHER INSTRUCTIONS:
You must output the EXACT sequence of words the speaker uttered. Do not filter, censor, substitute, rephrase, correct, normalize, or edit the language in any way. Preserve every repetition, false start, hesitation, dialect word, grammatical 'error', slang, or culturally specific term exactly as spoken (e.g. if the speaker said 'läskiaktivisti' you output 'läskiaktivisti' - never change it to something 'nicer' like 'pluskokoinen aktivisti'). The 'text' value for each segment must be a literal verbatim transcription of the utterance, not an edited or writerly version. You are a recording device, not a writer or editor.

{style_instruction}

Important guidelines:
- {guidelines_text}

FINAL REMINDER BEFORE YOU OUTPUT ANYTHING: The single most important rule is VERBATIM. If you ever feel the urge to "make the text nicer", "fix the grammar", or "choose better words", STOP. Output the ugly, repetitive, or non-standard words exactly as the person said them. That is what the downstream editing tools expect and what "verbatim" means here.
"""

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt, audio_part],
        )

        text = response.text.strip()

        # Clean up common markdown fences
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text else text
            if text.lower().startswith("json"):
                text = text[4:].strip()

        data = json.loads(text)

        # Support both new format (object with language + segments) and old array
        if isinstance(data, dict):
            detected_lang = data.get("language")
            raw_segments = data.get("segments", [])
        else:
            # Old format fallback
            detected_lang = None
            raw_segments = data if isinstance(data, list) else []

        # Basic validation + cleanup
        # Convert timestamps to float seconds
        cleaned = []
        for seg in raw_segments:
            if isinstance(seg, dict) and "start" in seg and "end" in seg and "text" in seg:
                cleaned.append(
                    {
                        "start": _parse_to_seconds(seg["start"]),
                        "end": _parse_to_seconds(seg["end"]),
                        "text": str(seg["text"]).strip(),
                    }
                )

        # Sanitize immediately: fix inversions, clamp, sort, dedup.
        # This protects against Gemini timestamp hallucinations (very common on longer Finnish interviews).
        cleaned = sanitize_transcription_segments(cleaned, max_duration=total_duration)

        # Quantize the AI's returned seconds to the source video's frame grid (our
        # post-processing step that "turns the timestamps from AI to TIMECODE").
        # This ensures .txt display and source .srt use exact legal frames for the
        # clip's fps, matching what the user sees scrubbing the media in Premiere.
        if fps and fps > 0:
            for seg in cleaned:
                for k in ("start", "end"):
                    if k in seg:
                        try:
                            val = float(seg[k])
                            seg[k] = round(val * fps) / fps
                        except Exception:
                            pass
            cleaned = sanitize_transcription_segments(cleaned, max_duration=total_duration)

        # --- Automatic recovery pass for the common "skipped beginning + bunched end" failure mode
        # seen on some longer Finnish interviews. If the first credible segment starts late or
        # we still see evidence of time collapse, we send the audio again with an explicit
        # "your previous timestamps were wrong, fix the beginning and spread the times" prompt.
        # If the recovery gives a meaningfully earlier start we adopt it (merged with repairs).
        # This costs one extra model call only on suspected bad outputs.
        # ------------------------------------------------------------------
        needs_recovery = False
        first_s = 0.0
        if cleaned:
            first_s = float(cleaned[0].get("start", 0) or 0)
            if first_s > 45:
                needs_recovery = True
            if len(cleaned) >= 5 and total_duration and total_duration > 100:
                tail = [float(s.get("start", 0) or 0) for s in cleaned[-5:]]
                if (max(tail) - min(tail)) < 3.0:
                    needs_recovery = True

        if needs_recovery:
            print(
                f"[Transcription] WARNING: First segment starts at {first_s:.1f}s (or end timestamps still collapsed) "
                f"on a {total_duration:.1f}s clip. Gemini likely missed the start or lost time sync. "
                "Running a targeted recovery pass..."
            )
            try:
                rec_prompt = prompt + (
                    "\n\nYOUR PREVIOUS OUTPUT HAD BAD TIMESTAMPS.\n"
                    f"The first segment you returned started at or after {first_s:.0f}s (or many segments shared nearly identical times at the very end). "
                    "That is wrong for this recording.\n"
                    "TASK: Re-analyze the audio from the beginning. Output the *full* accurate list of segments with timestamps that:\n"
                    "- Start with the actual first words spoken after time 0.000 (usually well before 60s).\n"
                    "- Advance realistically for every subsequent utterance (do not bunch later text at the final second).\n"
                    "- Respect the real pauses and speech rhythm in the file.\n"
                    "CRITICAL: All text must still obey the TRANSCRIBE VERBATIM rule from the main prompt 100% - output exact spoken words with no editing whatsoever.\n"
                    "Return the complete corrected JSON (language + all segments from start to end). No other text."
                )
                response2 = client.models.generate_content(
                    model=model_name,
                    contents=[rec_prompt, audio_part],
                )
                text2 = response2.text.strip()
                if text2.startswith("```"):
                    text2 = text2.split("```")[1] if "```" in text2 else text2
                    if text2.lower().startswith("json"):
                        text2 = text2[4:].strip()
                data2 = json.loads(text2)
                raw2 = (
                    data2.get("segments", [])
                    if isinstance(data2, dict)
                    else (data2 if isinstance(data2, list) else [])
                )
                rec = []
                for seg in raw2:
                    if isinstance(seg, dict) and "start" in seg and "end" in seg and "text" in seg:
                        rec.append(
                            {
                                "start": _parse_to_seconds(seg["start"]),
                                "end": _parse_to_seconds(seg["end"]),
                                "text": str(seg["text"]).strip(),
                            }
                        )
                rec = sanitize_transcription_segments(rec, max_duration=total_duration)
                if fps and fps > 0:
                    for seg in rec:
                        for k in ("start", "end"):
                            if k in seg:
                                try:
                                    val = float(seg[k])
                                    seg[k] = round(val * fps) / fps
                                except Exception:
                                    pass
                    rec = sanitize_transcription_segments(rec, max_duration=total_duration)

                # Prefer recovery if it actually starts substantially earlier OR if primary had
                # a collapsed/bunched tail and the recovery produced meaningfully better spread
                # on the ending timestamps (even if start time was already good). This catches
                # the common long-file "transcribed first 2/3 then dumped the rest at the final
                # second" case that the recovery prompt explicitly asks the model to avoid.
                rec_first = float(rec[0].get("start", 999999)) if rec else 999999
                improved_start = rec_first < first_s - 5
                improved_tail = False
                if (
                    rec
                    and len(rec) >= 5
                    and len(cleaned) >= 5
                    and total_duration
                    and total_duration > 100
                ):
                    p_tail = [float(s.get("start", 0) or 0) for s in cleaned[-5:]]
                    r_tail = [float(s.get("start", 0) or 0) for s in rec[-5:]]
                    p_span = max(p_tail) - min(p_tail) if p_tail else 0
                    r_span = max(r_tail) - min(r_tail) if r_tail else 0
                    if p_span < 3.0 and r_span > p_span + 1.5:
                        improved_tail = True
                if rec and (improved_start or improved_tail):
                    if improved_tail and not improved_start:
                        print(
                            "[Transcription] Recovery improved the collapsed end timestamps — adopting recovered segments."
                        )
                    else:
                        print(
                            "[Transcription] Recovery produced a clearly better (earlier) start — adopting recovered segments."
                        )
                    cleaned = rec
                else:
                    print(
                        "[Transcription] Recovery did not improve the start time over the primary (repaired) result; keeping primary."
                    )
            except Exception as rec_ex:
                print(
                    f"[Transcription] Recovery pass encountered an error (will use the primary + repaired segments): {rec_ex}"
                )

        # Return both the segments and the detected language
        return {"segments": cleaned, "language": detected_lang.lower() if detected_lang else None}

    except Exception as e:
        raise RuntimeError(f"Gemini transcription failed: {e}") from e


def translate_transcription_segments(
    segments: list[dict[str, Any]],
    target_language: str,
    api_key: str,
    *,
    model_name: str = DEFAULT_GEMINI_MODEL,
    max_duration: float | None = None,
) -> list[dict[str, Any]]:
    """
    Translate a list of timed transcription segments to another language
    while preserving the exact start/end timestamps.

    Returns a new list of segments with translated 'text'.
    """
    if model_name not in GEMINI_MODELS:
        model_name = DEFAULT_GEMINI_MODEL

    try:
        from google import genai
    except ImportError as e:
        raise RuntimeError("Translation requires 'google-genai'") from e

    if not segments:
        return []

    if not api_key or not api_key.strip():
        raise ValueError("Gemini API key is required.")

    client = genai.Client(api_key=api_key.strip())

    # Prepare compact input for the model
    input_json = json.dumps(
        [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segments],
        ensure_ascii=False,
    )

    prompt = f"""You are a professional subtitle translator.

Translate the following timed transcript segments into {target_language}.

Rules:
- Keep the exact same "start" and "end" timestamps.
- Translate the "text" field naturally and accurately.
- Return ONLY a valid JSON array with the same structure.
- Do not add explanations or extra text.

Input:
{input_json}
"""

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=[prompt],
        )

        text = response.text.strip()

        # Clean markdown code blocks if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()

        translated = json.loads(text)

        # Validate structure
        # Convert to float seconds on the way out (consistent with transcription).
        cleaned = []
        for item in translated:
            if isinstance(item, dict) and "start" in item and "end" in item and "text" in item:
                cleaned.append(
                    {
                        "start": _parse_to_seconds(item["start"]),
                        "end": _parse_to_seconds(item["end"]),
                        "text": str(item["text"]).strip(),
                    }
                )

        # Always sanitize translated output too (catches any bad times the translator
        # might have echoed, and applies the trailing-junk pruning using the known max).
        cleaned = sanitize_transcription_segments(cleaned, max_duration=max_duration)
        return cleaned

    except Exception as e:
        raise RuntimeError(f"Gemini translation failed: {e}") from e


# =============================================================================
# Finnish Broadcast Subtitle Formatting
#
# This module contains two layers:
# 1. The main transcription prompt (in transcribe_audio_with_timestamps) now asks
#    Gemini to directly produce well-segmented, subtitle-ready blocks following
#    Finnish TV rules (39 chars/line, max 2 lines, good CPS, natural breaks).
#
# 2. The functions below (`format_for_finnish_broadcast` and helpers) act as a
#    robust safety net / post-processor. They are especially important after
#    translation, and as a fallback when the model doesn't perfectly follow the
#    strict rules.
# =============================================================================


def _break_text_into_lines(text: str, max_chars: int = 39) -> list[str]:
    """
    Break text into lines of max 39 characters.
    Never truncates — returns as many lines as needed.
    Tries to break at natural word boundaries.
    """
    text = text.strip()
    if not text:
        return []

    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = (current + " " + word).strip() if current else word
        if len(test) <= max_chars:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
            # Handle extremely long words (rare)
            while len(current) > max_chars:
                lines.append(current[:max_chars])
                current = current[max_chars:]

    if current:
        lines.append(current)

    return lines


def _group_lines_into_blocks(lines: list[str], max_lines: int = 2) -> list[str]:
    """
    Group lines into subtitle blocks of at most `max_lines` lines each.
    Each block is returned as a string with \\N for line breaks.
    """
    blocks = []
    i = 0
    while i < len(lines):
        block_lines = lines[i : i + max_lines]
        if len(block_lines) == 1:
            blocks.append(block_lines[0])
        else:
            blocks.append("\\N".join(block_lines))
        i += max_lines
    return blocks


def _ensure_max_two_lines(block: str) -> list[str]:
    """
    Safety helper: if a block string contains more than one \\N (i.e. 3+ lines),
    split it into multiple valid 2-line (or 1-line) blocks.
    """
    if "\\N" not in block:
        return [block]

    parts = block.split("\\N")
    result = []
    i = 0
    while i < len(parts):
        if i + 1 < len(parts):
            result.append(parts[i] + "\\N" + parts[i + 1])
            i += 2
        else:
            result.append(parts[i])
            i += 1
    return result


def _prefer_single_line_for_short_content(blocks: list[str]) -> list[str]:
    """
    For blocks with very few words, prefer presenting them as single-line subtitles
    rather than forcing a 2-line block. This improves readability for short phrases.
    """
    result = []
    for block in blocks:
        if "\\N" not in block:
            result.append(block)
            continue

        # Count words in the block (rough estimate)
        word_count = len(block.replace("\\N", " ").split())
        if word_count <= 6:  # low word count → split into two 1-line subtitles
            parts = block.split("\\N")
            result.extend(parts)
        else:
            result.append(block)
    return result


def _sanitize_subtitle_text(text: str) -> str:
    """
    Final hard guarantee: a subtitle text block must never contain more than one \\N
    (i.e. max 2 lines). If it does, keep only the first two lines.
    """
    if not text:
        return ""
    # Normalize any mix of \n and \\N to a single \\N representation first
    text = text.replace("\n", "\\N").replace("\\n", "\\N")
    parts = [p.strip() for p in text.split("\\N") if p.strip()]
    if len(parts) <= 2:
        return "\\N".join(parts)
    else:
        return "\\N".join(parts[:2])


def _parse_time_to_seconds(t: str | float | int | None) -> float:
    """Parse 'HH:MM:SS.mmm' / 'HH:MM:SS,mmm' or a number (already seconds) to float seconds."""
    if t is None:
        return 0.0
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        t = t.strip().replace(",", ".")
        try:
            parts = t.split(":")
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + float(s)
            elif len(parts) == 2:
                m, s = parts
                return int(m) * 60 + float(s)
            else:
                return float(parts[0])
        except Exception:
            return 0.0
    return 0.0


def _parse_to_seconds(ts: str | float | int | None) -> float:
    """Normalize any timestamp representation to float seconds.

    Accepts:
    - float/int (already seconds) → returned as-is
    - "HH:MM:SS.mmm" or "MM:SS.mmm" or "SS.mmm" strings
    """
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        t = ts.strip().replace(",", ".")
        parts = t.split(":")
        try:
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + float(s)
            elif len(parts) == 2:
                m, s = parts
                return int(m) * 60 + float(s)
            else:
                return float(parts[0])
        except Exception:
            return 0.0
    return 0.0


def _split_long_segment_by_words(
    seg: dict[str, Any], max_words: int = 12, max_duration: float = 5.5
) -> list[dict[str, Any]]:
    """
    Programmatic fallback safeguard: Splits an excessively long transcription segment
    into granular pieces using linear interpolation based on character length.
    Ensures the AI Director never receives massive uncut blocks.
    """
    text = seg.get("text", "").strip()
    try:
        start = float(seg["start"])
        end = float(seg["end"])
    except (KeyError, ValueError, TypeError):
        return [seg]

    duration = end - start
    words = text.split()

    if len(words) <= max_words and duration <= max_duration:
        return [seg]

    # Decide number of pieces: respect both word limit and (more importantly for editing)
    # the max time per segment. Time-based splitting ensures slow speech with few words
    # still gets broken into bite-sized pieces for the AI Director.
    word_pieces = max(1, (len(words) + max_words - 1) // max_words)
    time_pieces = max(
        1, int(duration / max_duration) + (1 if (duration % max_duration) > 0.1 else 0)
    )
    num_pieces = max(word_pieces, time_pieces)

    if num_pieces <= 1:
        return [seg]

    # Distribute words as evenly as possible across the required number of pieces
    chunks = []
    if num_pieces > 0 and words:
        chunk_size = max(1, (len(words) + num_pieces - 1) // num_pieces)
        for i in range(0, len(words), chunk_size):
            ch = words[i : i + chunk_size]
            if ch:
                chunks.append(" ".join(ch))

    if not chunks:
        return [seg]

    # Linearly interpolate timestamps based on character length weighting
    total_chars = sum(len(c) for c in chunks)
    if total_chars <= 0:
        return [seg]

    sub_segments = []
    current_start = start

    for chunk in chunks:
        chunk_len = len(chunk)
        chunk_dur = (chunk_len / total_chars) * duration
        chunk_end = current_start + chunk_dur

        sub_segments.append(
            {"start": round(current_start, 3), "end": round(min(chunk_end, end), 3), "text": chunk}
        )
        current_start = chunk_end

    # Hard lock the final sub-segment back to the original boundaries
    if sub_segments:
        sub_segments[-1]["end"] = round(end, 3)

    return sub_segments


def sanitize_transcription_segments(
    segments: list[dict[str, Any]],
    *,
    max_duration: float | None = None,
) -> list[dict[str, Any]]:
    """
    Robust post-processing for segments returned by the transcriber (or after translation).

    - Swaps inverted start/end (common Gemini timestamp hallucination).
    - Drops segments with no text or near-zero duration.
    - Clamps times to [0, max_duration] if provided (prevents 1000s+ on a long file). Pass the video's total duration so Gemini (via the caller) and sanitize know the full length.
    - Sorts by start time (Gemini sometimes returns non-monotonic).
    - Basic dedup for identical consecutive text at same time.
    - Prunes short "Joo."-style filler tokens the model sometimes appends at the literal end to satisfy a total_duration instruction.
    - **Minimal local spread for bunched AI timestamps**: when the model gives several consecutive utterances the *exact same* start time, we spread *only that local group* forward from the time the AI assigned (using text length for duration). For the special case of a large group crammed at the very final second of a long clip we instead lay it out *backward* from the true end (so the generated per-clip .txt and source .srt show advancing timestamps for the ending dialogue instead of 100+ lines sharing one final TC). We never move content across big gaps or re-time early/middle speech. This is the "turn the timestamps from AI to TIMECODE" post-processing step. The model is no longer pressured in the prompt to produce frame-aligned numbers itself.

    This ensures downstream SRT/TXT/AI Journalist always get sane, usable data
    even when the raw model output has bad timestamps.
    """
    if not segments:
        return []

    cleaned = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue

        s = _parse_to_seconds(seg.get("start"))
        e = _parse_to_seconds(seg.get("end"))

        if s > e:
            s, e = e, s  # fix inversion

        if e - s < 0.05:
            e = s + 1.0  # give it a minimal sensible duration instead of dropping the text

        if max_duration is not None and max_duration > 0:
            s = max(0.0, min(s, max_duration))
            e = max(s + 0.1, min(e, max_duration))

        cleaned.append({"start": round(s, 3), "end": round(e, 3), "text": text})

    # Sort by start (important for SRT and display)
    cleaned.sort(key=lambda x: x["start"])

    # Light dedup: drop exact duplicate text if it starts at almost same time as previous
    deduped = []
    for item in cleaned:
        if deduped:
            prev = deduped[-1]
            if (
                abs(item["start"] - prev["start"]) < 0.5
                and item["text"].lower() == prev["text"].lower()
            ):
                # extend previous if this one is longer
                if item["end"] > prev["end"]:
                    prev["end"] = item["end"]
                continue
        deduped.append(item)

    # ------------------------------------------------------------------
    # Prune "prompt obedience" trailing junk.
    # Gemini sometimes transcribes the first few minutes of a long interview,
    # then to "honor" a total_duration instruction it appends a single short
    # filler word ("Joo.", "Yeah.", "Mm.") anchored exactly at the final second.
    # This creates a 15-20 minute "hole" followed by a 0.1-1s garbage segment.
    # We detect and drop such degenerate trailing fillers after a large gap.
    # ------------------------------------------------------------------
    if deduped and len(deduped) >= 2:
        # Find the last segment that looks like real spoken content
        last_real_idx = len(deduped) - 1
        for i in range(len(deduped) - 1, -1, -1):
            txt = (deduped[i].get("text") or "").strip()
            dur = float(deduped[i].get("end", 0)) - float(deduped[i].get("start", 0))
            # "real" = decent length text or decent duration
            if len(txt) >= 12 or dur >= 1.2:
                last_real_idx = i
                break

        if last_real_idx < len(deduped) - 1:
            # There are one or more segments after the last "real" one
            gap = float(deduped[last_real_idx + 1]["start"]) - float(deduped[last_real_idx]["end"])
            trailing = deduped[last_real_idx + 1 :]

            # Common single-word acknowledgments / fillers in EN/FI that the model loves to
            # place at the forced end timestamp when it has given up on the middle.
            short_fillers = {
                "joo",
                "joo.",
                "kyllä",
                "kyllä.",
                "mm",
                "mmm",
                "hmm",
                "em",
                "niin",
                "aivan",
                "totta",
                "no",
                "no.",
                "ok",
                "okay",
                "yeah",
                "yea",
                "yep",
                "right",
                "uh",
                "um",
                "mhm",
                "aha",
                "juu",
                "kjoo",
            }

            looks_like_padding = True
            for t in trailing:
                ttxt = (t.get("text") or "").strip().lower().rstrip(".,!? ")
                tdur = float(t.get("end", 0)) - float(t.get("start", 0))
                if not (ttxt in short_fillers or len(ttxt) <= 8 or tdur < 1.5):
                    looks_like_padding = False
                    break

            # Large gap + trailing looks like padding → drop the junk
            if gap > 45.0 and looks_like_padding:
                dropped = len(trailing)
                deduped = deduped[: last_real_idx + 1]
                print(
                    f"[Sanitize] Pruned {dropped} degenerate trailing filler segment(s) "
                    f"(gap {gap:.1f}s before them on a long clip). This usually means "
                    "Gemini stopped transcribing mid-recording and tried to satisfy "
                    "the 'use the full duration' instruction with a token at the end."
                )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Minimal local repair for bunched/identical timestamps from the AI.
    # When the model assigns the exact same start time to several consecutive distinct
    # utterances (common when it loses fine time tracking on long audio), we locally
    # spread *only that group* forward from the time the AI itself assigned, using
    # text length for plausible per-utterance duration.
    #
    # Special case: if the bunched group is at/near the very end of the clip (the
    # classic "model transcribed beginning then dumped the rest at the final second
    # to obey total_duration"), we lay the group out *backward* from the true clip
    # end instead. This is the only re-anchoring we do, and only for the tail group.
    # It keeps the source .txt / source .srt from having 100+ lines all sharing one
    # timestamp at the end (which looks weird and produces broken flashing subs).
    # We still never move content across big gaps or invent times for early/middle content.
    #
    # This follows the "transcribe the old way (model gives its best seconds), then
    # our code turns the AI timestamps into proper TIMECODE" approach + pragmatic
    # repair so the persisted per-clip transcripts remain usable on long files.
    # ------------------------------------------------------------------
    if deduped and len(deduped) >= 2:
        new_list = []
        i = 0
        while i < len(deduped):
            j = i + 1
            base_start = float(deduped[i]["start"])
            # Collect run with essentially identical start time (the AI collapsed them)
            while j < len(deduped) and abs(float(deduped[j]["start"]) - base_start) < 0.01:
                j += 1
            group = deduped[i:j]
            if len(group) >= 2:
                # Decide whether this is a normal middle bunch (forward from AI time)
                # or a crammed tail that needs backward layout from clip end.
                is_crammed_tail = bool(
                    max_duration and max_duration > 30 and base_start >= (max_duration - 5.0)
                )
                rate = 14.0
                natural = []
                cur = base_start
                for c in group:
                    ch = max(1, len(c.get("text", "")))
                    dur = max(0.5, ch / rate)
                    natural.append((cur, cur + dur, c["text"]))
                    cur += dur + 0.08
                last_natural_end = natural[-1][1] if natural else base_start

                if is_crammed_tail:
                    # Backward layout so the *last* utterance ends near max_duration and
                    # previous ones in the tail fill realistic time before it (text-len proxy).
                    # This fixes the "weird" all-identical final timestamps in 00000x_fi.txt
                    # and the corresponding source .srt without touching earlier segments.
                    cur_t = min(max_duration, float(group[-1].get("end") or max_duration))
                    assigned = []
                    for nat_start, nat_end, txt in reversed(natural):
                        dur = nat_end - nat_start
                        e = cur_t
                        s = cur_t - dur
                        if max_duration:
                            e = min(e, max_duration)
                            s = max(0.0, min(s, e - 0.1))
                        assigned.append((round(s, 3), round(e, 3), txt))
                        cur_t = s - 0.06
                    assigned.reverse()
                    for s, e, txt in assigned:
                        new_list.append({"start": s, "end": e, "text": txt})
                    print(
                        f"[Sanitize] Re-anchored {len(group)} tail segments *backward* from clip end "
                        f"(AI had crammed them at {base_start:.1f}s; now .txt/.srt show advancing TC)."
                    )
                else:
                    # Original forward local spread for non-tail bunches.
                    if (
                        max_duration
                        and last_natural_end > max_duration
                        and last_natural_end > base_start
                    ):
                        scale = (max_duration - base_start) / (last_natural_end - base_start)
                        scale = max(0.1, min(1.0, scale))
                    else:
                        scale = 1.0
                    cur_t = base_start
                    for nat_start, nat_end, txt in natural:
                        dur = (nat_end - nat_start) * scale
                        e = (
                            min(max_duration or (cur_t + dur + 1), cur_t + dur)
                            if max_duration
                            else (cur_t + dur)
                        )
                        new_list.append({"start": round(cur_t, 3), "end": round(e, 3), "text": txt})
                        cur_t = e + 0.05
                    print(
                        f"[Sanitize] Locally spread {len(group)} segments that AI gave identical "
                        f"start {base_start:.3f}s (AI time used as anchor; turned into distinct TIMECODE)."
                    )
            else:
                new_list.append(deduped[i])
            i = j
        deduped = new_list

    # Late-start warning (helps surface the "time code jumps right in beginning" symptom immediately)
    if deduped and max_duration and max_duration > 60:
        first_start = float(deduped[0].get("start", 0) or 0)
        if first_start > 45:
            print(
                f"[Sanitize] WARNING: Transcription starts late (first segment at {first_start:.1f}s on a {max_duration:.1f}s clip). "
                "Gemini likely skipped the beginning of the recording. Re-transcribe or check the source audio for early speech. "
                "The .txt/.srt will only show content from the first captured utterance onward."
            )

    # ENHANCED SAFENET: Guard against escaped long blocks by forcing a programmatic split pass
    final_granular_list = []
    for seg in deduped:
        final_granular_list.extend(
            _split_long_segment_by_words(seg, max_words=12, max_duration=5.5)
        )

    return final_granular_list


def _seconds_to_ass_time(seconds: float) -> str:
    """Convert seconds to ASS time format H:MM:SS.CC (centiseconds with dot)."""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds % 1.0) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def format_for_finnish_broadcast(
    segments: list[dict[str, Any]],
    *,
    max_chars_per_line: int = 39,
    max_cps: float = 16.0,
    min_duration: float = 1.0,
    max_duration: float = 6.5,
    gap_between_subtitles: float = 0.12,
    fps: float | None = None,
) -> list[dict[str, Any]]:
    """
    Post-processes timed segments into properly segmented subtitles that follow
    Yle (Finnish Broadcasting Company) and EBU subtitle guidelines for Finnish content.

    Rules applied:
    - Max 39 characters per line
    - Max 2 lines per subtitle block
    - ~15-17 characters per second reading speed
    - Minimum 1.0 second on screen
    - Maximum ~6.5 seconds per subtitle
    - Natural semantic breaks
    - **Strict non-overlap guarantee**: each subtitle always fully ends before the next one starts
      (we enforce at least 120 ms separation between consecutive blocks, even under tight timing).

    fps: If provided (e.g. 25.0), all computed block start/end times are quantized
         to exact frame boundaries *before* formatting to SRT/ASS strings. This
         makes the .srt timings land on exact frames for the source video's fps
         (prevents "wrong fps" artifacts and mid-frame subtitles).

    This function acts as a safety net and is especially useful after translation.
    The primary transcription prompt also asks Gemini to respect these same Yle/EBU rules upfront.
    """
    if not segments:
        return []

    result = []

    for seg in segments:
        original_text = seg.get("text", "").strip()
        if not original_text:
            continue

        # Normalize: replace any existing line breaks (\N or actual newlines) with spaces.
        # This is critical when re-formatting already-formatted segments (e.g. after translation)
        # so we don't accidentally accumulate multiple \N and create 3+ line blocks.
        original_text = original_text.replace("\\N", " ").replace("\n", " ").strip()

        start_sec = _parse_time_to_seconds(seg.get("start", "00:00:00.000"))
        end_sec = _parse_time_to_seconds(seg.get("end", "00:00:00.000"))
        original_duration = max(0.1, end_sec - start_sec)

        # 1. Break into individual lines (max 39 chars)
        lines = _break_text_into_lines(original_text, max_chars_per_line)

        # 2. Group into blocks of max 2 lines
        blocks = _group_lines_into_blocks(lines, max_lines=2)

        # Hard safety net: ensure no block ever contains more than one \N (i.e. max 2 lines).
        # This protects against any upstream text that somehow already contained line breaks.
        blocks = [b for block in blocks for b in _ensure_max_two_lines(block)]

        # Prefer 1-line blocks for short/low word count content (better readability)
        blocks = _prefer_single_line_for_short_content(blocks)

        if not blocks:
            continue

        # 3. Calculate ideal duration for each block (respecting reading speed + min/max rules)
        block_durations = []
        for block in blocks:
            char_count = len(block.replace("\\N", " "))
            dur = char_count / max_cps
            dur = max(min_duration, min(dur, max_duration))
            block_durations.append(dur)

        n = len(blocks)

        # 4. Time distribution with a *hard guarantee* of no overlap.
        #
        # Requirement: Every subtitle must fully end BEFORE the next one starts.
        # We enforce a minimum positive separation (hard_min_gap) between consecutive blocks.
        # When there is enough time we use the preferred gap. When space is tight we
        # compress durations but *never* reduce the gap below the hard minimum.

        max_overrun = 1.8  # Allow the last subtitle a bit of extra linger time
        preferred_gap = gap_between_subtitles
        hard_min_gap = 0.12  # <<< Hard guarantee: at least 120ms separation between blocks
        # (increased from 0.10 to eliminate tiny visual overlaps when burned)

        # Reserve the minimum required separation time
        min_gaps_time = (n - 1) * hard_min_gap

        # Maximum total timeline we are willing to use for this group of subtitles
        max_total_time = original_duration + max_overrun

        total_ideal_block = sum(block_durations)

        if total_ideal_block + min_gaps_time <= max_total_time:
            # We have room — use ideal (or near-ideal) durations + the preferred gap
            effective_gap = preferred_gap
            final_durations = block_durations[:]
        else:
            # Not enough room even with minimum gaps.
            # Scale durations down so the blocks + hard_min_gaps fit.
            available_for_blocks = max_total_time - min_gaps_time
            scale = max(available_for_blocks, n * min_duration) / total_ideal_block
            final_durations = [
                max(min_duration, min(d * scale, max_duration)) for d in block_durations
            ]
            effective_gap = hard_min_gap  # never go below the hard separation

        # Lay out the blocks with the chosen (guaranteed positive) gap.
        # This ensures: block[i].end + effective_gap <= block[i+1].start
        current_time = start_sec
        for block, dur in zip(blocks, final_durations):
            block_start = current_time
            block_end = current_time + dur

            if fps and fps > 0:
                block_start = round(block_start * fps) / fps
                block_end = round(block_end * fps) / fps

            result.append(
                {
                    "start": _seconds_to_srt_time(block_start),
                    "end": _seconds_to_srt_time(block_end),
                    "text": _sanitize_subtitle_text(block),
                }
            )

            current_time = block_end + effective_gap

        # === Final enforcement pass ===
        # Walk through the result and force the hard minimum gap.
        # This catches any floating-point or rounding issues that could cause tiny overlaps
        # when the subtitles are rendered or burned.
        for i in range(1, len(result)):
            prev_end = _parse_time_to_seconds(result[i - 1]["end"])
            curr_start = _parse_time_to_seconds(result[i]["start"])
            if curr_start < prev_end + hard_min_gap:
                shift = (prev_end + hard_min_gap) - curr_start
                new_start = curr_start + shift
                new_end = _parse_time_to_seconds(result[i]["end"]) + shift
                if fps and fps > 0:
                    new_start = round(new_start * fps) / fps
                    new_end = round(new_end * fps) / fps
                result[i]["start"] = _seconds_to_srt_time(new_start)
                result[i]["end"] = _seconds_to_srt_time(new_end)

        # Ultimate safety: enforce max 2 lines using the sanitizer
        for item in result:
            item["text"] = _sanitize_subtitle_text(item["text"])

    return result


def source_transcript_to_srt_segments(
    segments: list[dict[str, Any]],
    *,
    fps: float | None = None,
) -> list[dict[str, Any]]:
    """Prepare raw source transcription segments for SRT, preserving spoken timing.

    - Keeps the original start/end times from the transcription (so the .srt
      times match the .txt and what the user sees scrubbing the video in Premiere).
    - Only does text line-breaking (39 chars/line, max 2 lines per display block)
      for readability. No CPS-based duration changes or global gap shifting that
      can move a subtitle's start away from when the words were actually spoken.
    - If one spoken segment's text needs several display blocks (long utterance),
      the original time window is lightly subdivided with small internal gaps.
    - Quantizes to fps frame boundaries if provided (same as other paths).

    This is used specifically for the persistent per-clip source transcript SRTs
    (00000x_fi.srt). The full `format_for_finnish_broadcast` (with timing
    redistribution) is still used for AI cut exports and burned subtitles.
    """
    if not segments:
        return []

    result: list[dict[str, Any]] = []
    hard_min_gap = 0.08

    for seg in segments:
        start_sec = _parse_time_to_seconds(seg.get("start"))
        end_sec = _parse_time_to_seconds(seg.get("end"))
        original_text = str(seg.get("text", "")).strip()
        if not original_text:
            continue

        original_text = original_text.replace("\\N", " ").replace("\n", " ").strip()

        lines = _break_text_into_lines(original_text, max_chars=39)
        blocks = _group_lines_into_blocks(lines, max_lines=2)
        blocks = [b for block in blocks for b in _ensure_max_two_lines(block)]
        blocks = _prefer_single_line_for_short_content(blocks)

        if not blocks:
            blocks = [original_text]

        n = len(blocks)
        if n == 1:
            bs = start_sec
            be = end_sec
            if fps and fps > 0:
                bs = round(bs * fps) / fps
                be = round(be * fps) / fps
            result.append(
                {
                    "start": bs,
                    "end": be,
                    "text": _sanitize_subtitle_text(blocks[0]),
                }
            )
        else:
            # Lightly subdivide the spoken window for this utterance
            min_gaps = (n - 1) * hard_min_gap
            avail = max(0.5, (end_sec - start_sec) - min_gaps)
            dur = avail / n
            cur = start_sec
            for b in blocks:
                bs = cur
                be = cur + dur
                if fps and fps > 0:
                    bs = round(bs * fps) / fps
                    be = round(be * fps) / fps
                result.append(
                    {
                        "start": bs,
                        "end": be,
                        "text": _sanitize_subtitle_text(b),
                    }
                )
                cur = be + hard_min_gap

    return result


def segments_to_srt(
    segments: list[dict[str, Any]],
    *,
    strict_timing: bool = False,
    fps: float | None = None,
    base_timecode: str | None = None,
) -> str:
    """
    Convert timed segments to standard .srt.

    strict_timing=True: Use the exact start/end times provided (no broadcast
        reformatting, no duration adjustment, no splitting). Ideal for AI
        Journalist cuts where each segment represents a deliberate clip choice
        and the SRT must match the exported sequence / rendered file exactly.
    strict_timing=False (default): Apply full Finnish broadcast formatting rules
        (recommended for full interviews and normal transcription).

    fps: If provided (e.g. 25.0 or 24.0), quantize all start/end times to exact
        frame boundaries before writing the timecodes. This ensures the .srt
        timings land on exact frames in the video's timeline (prevents "wrong fps"
        drift or mid-frame starts when used with Premiere, burning, etc.).
        The underlying transcription/cut data remains in seconds.
    """
    if strict_timing:
        formatted = segments  # trust the caller-provided times exactly
    else:
        formatted = format_for_finnish_broadcast(segments, fps=fps)

    lines = []
    # Helpful header for users (especially Premiere) when the file is opened/imported.
    # SRT parsers skip leading non-block lines; the first "1" starts the real content.
    if fps and fps > 0:
        fps_str = f"{fps:.3f}".rstrip("0").rstrip(".")
        lines.append(
            f"# CAT+TAG subtitles - generated for {fps_str} fps source media (r_frame_rate from original file)"
        )
        lines.append(
            "# Event times are real seconds from media head (00:00:00.000), quantized to exact video frame boundaries."
        )
        if base_timecode and base_timecode not in (None, "00:00:00:00"):
            lines.append(
                f"# Clip embedded start timecode: {base_timecode} (SRT timings above are media-head relative for correct import/alignment when attached to this clip)."
            )
        lines.append(
            "# In Premiere Pro the asset often shows a default '30 fps' label (cosmetic). Right-click the .srt in Project panel > Modify > Interpret Footage > 'Assume this frame rate' and enter the source fps (e.g. 25.000) to match your 25 fps media."
        )
        lines.append("")

    for i, seg in enumerate(formatted, 1):
        start_val = seg.get("start")
        end_val = seg.get("end")

        if isinstance(start_val, (int, float)):
            start_sec = float(start_val)
            if fps and fps > 0:
                start_sec = round(start_sec * fps) / fps
            start = _seconds_to_srt_time(start_sec)
        else:
            if fps and fps > 0:
                start_sec = _parse_time_to_seconds(start_val)
                start_sec = round(start_sec * fps) / fps
                start = _seconds_to_srt_time(start_sec)
            else:
                start = str(start_val).replace(".", ",")

        if isinstance(end_val, (int, float)):
            end_sec = float(end_val)
            if fps and fps > 0:
                end_sec = round(end_sec * fps) / fps
            end = _seconds_to_srt_time(end_sec)
        else:
            if fps and fps > 0:
                end_sec = _parse_time_to_seconds(end_val)
                end_sec = round(end_sec * fps) / fps
                end = _seconds_to_srt_time(end_sec)
            else:
                end = str(end_val).replace(".", ",")

        clean_text = _sanitize_subtitle_text(seg.get("text", ""))
        text = clean_text.replace("\\N", "\n")

        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")

    return "\n".join(lines)


def _seconds_to_srt_time(seconds: float) -> str:
    """Convert float seconds to SRT time format HH:MM:SS,mmm."""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1.0) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ai_journalist_cut_to_srt_segments(
    selected_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Convert AI Journalist selected_segments (in the AI-chosen narrative order)
    into subtitle timing for the *new cut's timeline*, starting at 00:00.

    This is essential for reordered cuts (especially Verbatim Scriptwriter mode)
    and even for simple trims: the resulting .srt must match the exported XML
    sequence or rendered clip, not the original source media timestamps.

    Returns a list ready to be passed to segments_to_srt().

    For Verbatim Scriptwriter (rewrite) versions, pass strict_timing=True
    (this is done automatically in the UI export paths) so the SRT is a
    literal 1:1 representation of the AI-chosen segments and order.
    """
    timeline_pos = 0.0
    result: list[dict[str, Any]] = []

    for seg in selected_segments:
        src_start = seg.get("source_in") or seg.get("start")
        src_end = seg.get("source_out") or seg.get("end")
        if src_start is None or src_end is None:
            continue
        try:
            s = float(src_start)
            e = float(src_end)
        except (TypeError, ValueError):
            continue

        dur = max(0.01, e - s)
        text = (seg.get("text") or seg.get("reason") or "").strip()
        result.append({"start": timeline_pos, "end": timeline_pos + dur, "text": text})
        timeline_pos += dur

    return result


def ai_journalist_cut_to_yle_srt_segments(
    selected_segments: list[dict[str, Any]],
    *,
    max_chars_per_line: int = 39,
    max_lines_per_block: int = 2,
    max_cps: float = 16.0,
    min_duration: float = 1.0,
    max_duration: float = 6.5,
    gap_between_subtitles: float = 0.08,
) -> list[dict[str, Any]]:
    """
    Convert AI Journalist selected segments into SRT-ready segments with
    proper YLE-style text formatting, while strictly respecting the
    cumulative timeline of the AI cut.

    This is the recommended path for AI Journalist (especially Verbatim
    Scriptwriter) cuts:
    - Overall timing exactly follows the AI's chosen order and clip durations.
    - Text inside each chosen verbatim segment is broken according to YLE rules
      (39 chars/line, max 2 lines, reasonable reading speed).
    - No subtitles leak outside their originating AI-selected clip.

    Returns a list suitable for `segments_to_srt(..., strict_timing=True)`.
    """
    timeline_pos = 0.0
    result: list[dict[str, Any]] = []

    hard_min_gap = 0.08  # small but safe gap inside each AI segment

    for seg in selected_segments:
        src_start = seg.get("source_in") or seg.get("start")
        src_end = seg.get("source_out") or seg.get("end")
        if src_start is None or src_end is None:
            continue
        try:
            s = float(src_start)
            e = float(src_end)
        except (TypeError, ValueError):
            continue

        dur = max(0.1, e - s)
        raw_text = (seg.get("text") or seg.get("reason") or "").strip()
        if not raw_text:
            timeline_pos += dur
            continue

        # 1. Break text according to YLE rules
        lines = _break_text_into_lines(raw_text, max_chars_per_line)
        blocks = _group_lines_into_blocks(lines, max_lines_per_block)

        if not blocks:
            timeline_pos += dur
            continue

        n = len(blocks)

        # 2. Calculate ideal durations for these blocks inside this segment's window
        block_durations = []
        for block in blocks:
            char_count = len(block.replace("\\N", " "))
            d = char_count / max_cps
            d = max(min_duration, min(d, max_duration))
            block_durations.append(d)

        total_ideal = sum(block_durations)
        min_gaps = max(0, n - 1) * hard_min_gap
        available = dur - min_gaps

        if total_ideal + min_gaps > dur:
            # Tight fit — scale down durations (but respect minimums)
            scale = max(0.1, available) / max(total_ideal, 0.1)
            final_durs = [max(min_duration, d * scale) for d in block_durations]
        else:
            final_durs = block_durations[:]

        # 3. Lay out the blocks inside this segment's window
        current = timeline_pos
        for block, d in zip(blocks, final_durs):
            block_start = current
            block_end = current + d

            result.append({"start": block_start, "end": block_end, "text": block})
            current = block_end + hard_min_gap

        timeline_pos += dur

    return result


def segments_to_ass(
    segments: list[dict[str, Any]], *, title: str = "Subtitles", fps: float | None = None
) -> str:
    """
    Convert timed segments into high-quality EBU-style .ass subtitles
    following Yle (Finnish Broadcasting Company) guidelines.

    Applies Yle/EBU-aligned rules:
    - Max 39 characters per line
    - Max 2 lines per block
    - 15-17 characters per second reading speed
    - Strong black outline (typical for Finnish TV readability)
    - Safe margins and professional broadcast appearance

    This is used when burning subtitles into proxies or video files.

    fps: If provided, quantize times to frame boundaries (same as segments_to_srt)
         so burned subs align exactly to the video's fps (e.g. 25).
    """
    formatted = format_for_finnish_broadcast(segments, fps=fps)

    # Finnish broadcast friendly ASS styling (strong readability)
    header = f"""[Script Info]
Title: {title}
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1920
PlayResY: 1080
"""
    if fps and fps > 0:
        fps_str = f"{fps:.3f}".rstrip("0").rstrip(".")
        header += f"; Source media fps: {fps_str} (r_frame_rate from original file)\n"
        header += "; Premiere note: SRT/ASS often shows '30 fps' label by default. Right-click asset > Modify > Interpret Footage > Assume this frame rate = 25.000 (or the source fps) to align with 25 fps media.\n"
    header += """
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,80,80,65,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    for seg in formatted:
        start_sec = _parse_time_to_seconds(seg.get("start", "0"))
        end_sec = _parse_time_to_seconds(seg.get("end", "0"))
        if fps and fps > 0:
            start_sec = round(start_sec * fps) / fps
            end_sec = round(end_sec * fps) / fps
        start = _seconds_to_ass_time(start_sec)
        end = _seconds_to_ass_time(end_sec)
        # Sanitize to guarantee max 2 lines
        clean_text = _sanitize_subtitle_text(seg["text"])
        text = clean_text.replace("{", "\\{").replace("}", "\\}")

        events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    return header + "\n".join(events)


def parse_transcription_txt_to_segments(txt_path: Path | str) -> list[dict[str, Any]]:
    """Parse a per-clip source transcript .txt (e.g. the 000005_fi.txt or MN-...._fi.txt)
    back into a list of timed segments.

    The TXT is the one written by save_transcription_txt / segments_to_plain_text.
    We extract the media seconds from the (X.Xs) parentheses (these are the spoken
    offsets from the start of the media file). This enables round-tripping:
    - User (or external tool) edits the human-readable .txt (fix OCR/text errors,
      tweak timings, etc.)
    - Then call this + source_transcript_to_srt_segments + segments_to_srt (or the
      save functions) to produce a fresh matching source .srt that will sync.

    The real timecode shown in the [HH:MM:SS:FF ...] is only for human reference
    (it comes from the clip's embedded tc_start + media offset); we use the (sec)
    values for the actual timing data because they are the authoritative spoken
    positions relative to the file head.

    Returns:
        list of {"start": float, "end": float, "text": str}  (media seconds)
        ready to be fed to source_transcript_to_srt_segments(...) or
        segments_to_srt(..., strict_timing=True).
    """
    txt_path = Path(txt_path)
    if not txt_path.exists():
        raise FileNotFoundError(f"Transcript TXT not found: {txt_path}")

    content = txt_path.read_text(encoding="utf-8", errors="replace")
    segments: list[dict[str, Any]] = []

    # Primary pattern produced by the exporter when fps is known:
    #   [11:43:09:24 (0.9s) → 11:43:10:20 (1.8s)] Nyt mennään.
    # We care about the media seconds in the ( ) and the text after the final ]
    primary = re.compile(
        r"\[[^\]]+?\s+\(([0-9.]+)s\)\s*→\s*[^\]]+?\s+\(([0-9.]+)s\)\]\s*(.*)$",
        re.MULTILINE,
    )

    # Fallback for TXT generated without fps (or manually created):
    #   [0.9s → 1.8s] Nyt mennään.
    fallback = re.compile(
        r"\[([0-9.]+)s\s*→\s*([0-9.]+)s\]\s*(.*)$",
        re.MULTILINE,
    )

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        m = primary.search(line)
        if m:
            start_s = float(m.group(1))
            end_s = float(m.group(2))
            text = m.group(3).strip()
            if text:
                segments.append({"start": start_s, "end": end_s, "text": text})
            continue

        m = fallback.search(line)
        if m:
            start_s = float(m.group(1))
            end_s = float(m.group(2))
            text = m.group(3).strip()
            if text:
                segments.append({"start": start_s, "end": end_s, "text": text})

    return segments
