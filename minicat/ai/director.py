"""
AI Director for multi-source / multi-clip narrative construction.

This module is the dedicated counterpart to journalist_cutter.py.

While journalist_cutter.py is optimized for working with a *single* interview
(acting as a professional journalist/editor), director.py is built for the
case where the user has selected several clips and wants the AI to act as a
**journalist + director**:

- All chosen transcripts are combined into **one** clean, explicitly labeled
  document.
- Every timecoded segment clearly states which original clip it belongs to
  (C1, C2, ..., plus filename for human readability).
- The model receives strong direction to intercut, juxtapose, and build
  coherent short narrative versions using verbatim material from across
  the sources.
- Journalistic safety is absolute: only exact spoken words + accurate
  source_in / source_out from the original transcriptions are ever used.
- Output format is fully compatible with the existing XMEML / render /
  SRT export pipeline.

The "rewrite" (Verbatim Scriptwriter) tone is especially powerful here:
the AI is explicitly instructed to treat the combined material as raw
dramatic/documentary building blocks and construct genuinely new story
architectures through selection + ordering + cross-clip juxtaposition.

Audio (never video) can still be sent for listening, exactly as in the
single-clip journalist flow.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from minicat.core.settings import DEFAULT_GEMINI_MODEL, GEMINI_MODELS

import re

def detect_material_language(text: str, api_key: str | None = None) -> str:
    """
    Detect the primary language of the source material using Gemini.
    Returns a two-letter ISO code (en, fi, de, fr, es, sv, etc.).
    Defaults to 'en' if detection fails.
    """
    if not text or len(text.strip()) < 20:
        return "en"

    # Truncate for the detection call
    sample = text[:3000]

    prompt = f"""What is the primary language of the following transcript?
Reply with ONLY the two-letter ISO 639-1 language code (e.g. en, fi, de, fr, es, sv).
If the material is mixed, choose the clearly dominant language.

Transcript sample:
{sample}
"""

    try:
        from google import genai
        from minicat.core.settings import get_gemini_api_key

        key = api_key or get_gemini_api_key()
        if not key:
            return "en"

        client = genai.Client(api_key=key.strip())
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        raw = (response.text or "").strip().lower()

        # Extract a 2-letter code
        match = re.search(r'\b([a-z]{2})\b', raw)
        if match:
            code = match.group(1)
            # Validate common ones we support
            if code in ("en", "fi", "de", "fr", "es", "sv", "it", "nl"):
                return code
        return "en"
    except Exception:
        return "en"


def _extract_json(text: str) -> Any:
    """
    Robustly extract JSON (array or object) from Gemini output.
    Handles common cases: markdown fences, extra prose, ```json, truncated output,
    trailing commas, and attempts to salvage a valid prefix array when the model
    response was cut off mid-generation (common with long narrative_summary / many segments).
    """
    if not text:
        raise ValueError("Empty response from model")

    t = text.strip()

    # Remove common markdown wrappers (``` or ```json)
    if t.startswith("```"):
        parts = t.split("```", 2)
        t = parts[1] if len(parts) > 1 else t
        t = t.strip()
        if t.lower().startswith("json"):
            t = t[4:].strip()

    import re

    # 1. Direct parse on cleaned text (best case, especially with response_mime_type)
    try:
        return json.loads(t)
    except Exception:
        pass

    # 2. Fix common LLM JSON sins (trailing commas) and retry
    cleaned = re.sub(r',\s*([}\]])', r'\1', t)
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # 3. Extract the largest plausible JSON array by finding the outermost [ ... ]
    # Prefer the last occurrence of a full array in case of preamble text.
    array_candidates = list(re.finditer(r'\[[\s\S]*\]', t))
    for m in reversed(array_candidates):
        cand = m.group(0)
        # try to close it if truncated
        cand = cand.rstrip().rstrip(',').rstrip()
        if not cand.endswith(']'):
            cand += ']'
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            # try with trailing comma fix on this cand
            cand2 = re.sub(r',\s*([}\]])', r'\1', cand)
            try:
                parsed = json.loads(cand2)
                if isinstance(parsed, list):
                    return parsed
            except:
                pass

    # 4. Salvage strategy for truncated generations: start from the first [ and
    # progressively trim the end until we get a valid list (or a prefix of it).
    if '[' in t:
        start = t.find('[')
        partial = t[start:]
        # Try several trim lengths from the end
        for trim in range(0, min(len(partial), 3000), 30):
            for extra in (0, 10, 30, 80):
                try:
                    cand = partial[: max(10, len(partial) - trim - extra)].rstrip()
                    # attempt to terminate the array
                    if not cand.rstrip().endswith(']'):
                        # cut back to last complete object if possible
                        last_close = cand.rfind('}')
                        if last_close > 10:
                            cand = cand[:last_close+1] + ']'
                        else:
                            cand = cand.rstrip().rstrip(',') + ']'
                    parsed = json.loads(cand)
                    if isinstance(parsed, list) and len(parsed) > 0:
                        print(f"[AI Director] Salvaged truncated JSON array (trimmed ~{trim} chars)")
                        return parsed
                except Exception:
                    continue

    # 5. Last-ditch single object (rare for Director)
    obj_match = re.search(r'\{[\s\S]*\}', t)
    if obj_match:
        try:
            cand = obj_match.group(0)
            cand = re.sub(r',\s*([}\]])', r'\1', cand)
            return json.loads(cand)
        except Exception:
            pass

    # Give up — include head of the bad text for debugging
    raise ValueError(f"Could not parse JSON from model response. First 300 chars: {t[:300]}")


# =============================================================================
# TONE & PURPOSE GUIDANCE (Director-flavored versions)
# =============================================================================

DIRECTOR_TONE_INSTRUCTIONS: dict[str, str] = {
    "newsroom": """
You are a senior news editor and director at a respected public broadcaster.
You are working with multiple interview clips that were recorded separately.
Your cuts must be:
- Factually tight and journalistically responsible
- Clear narrative structure that draws from the *best* material across all sources
- Prioritize clarity, public interest, and revelation over simple chronology
- Use cross-clip juxtaposition only when it genuinely strengthens the story
- Never invent words; every line must be verbatim from one of the provided clips
""",
    "flexible": """
You are a creative documentary director and editor (long-form audio/documentary style).
You have several different interview recordings as raw material.
Your cuts can be:
- More emotional, atmospheric, and human-centered
- Use surprising or meaningful juxtapositions between different speakers/clips
- Let interesting turns of phrase, pauses, and personality from different sources play against each other
- Still coherent, but more artistic than traditional news packages
- Only use exact verbatim spoken words from the source material
""",
    "documentary": """
You are a thoughtful documentary director working with multiple character-driven interviews.
You treat the combined transcripts as a body of raw dramatic material.
Your cuts should feel:
- Story-driven and atmospheric
- Allow space for emotion, silence, subtext, and deliberate contrast between sources
- Build a deeper human or thematic portrait by weaving moments from different clips
- Use juxtaposition and rhythm to create meaning that no single clip contains on its own
- Prioritize authenticity and emotional truth
""",
    "corporate": """
You are a professional corporate communications director.
You have multiple interview or testimony clips from different people or sessions.
Your cuts must be:
- Polished, credible, and brand-appropriate
- Clear, structured, and professional
- Draw the strongest, most on-message material from across the different sources
- Avoid overly casual language or dramatic flair unless it serves a clear corporate purpose
""",
    "commercial": """
You are a commercial / advertising director working with multiple interview or testimonial sources.
Your cuts should be:
- Persuasive and benefit-driven
- Energetic, confident, and modern
- Find the strongest hooks, proof points, and emotional beats across all the provided clips
- Use juxtaposition between speakers when it increases impact
- Optimized for engagement while remaining truthful to the spoken words
""",
    "rewrite": """
You are an experienced scriptwriter and narrative architect building a short non-fiction film or prestige journalistic piece from several separate interview recordings.

You have been given one combined, explicitly labeled transcript. Each segment is marked with its source (C1, C2, ... plus filename).

Your only creative tools are:
- Which exact verbatim moments you select
- The order and juxtaposition in which you present them (including deliberate intercutting between different original clips)

Strict rules (non-negotiable for journalistic integrity):
- You may NEVER invent, paraphrase, or rewrite what anyone actually said.
- Every single word in the final cut must be 100% original spoken language from one of the source clips.
- You MUST break the original per-clip chronology in a meaningful, structural way.
- Use cross-source juxtaposition as a primary storytelling device (contrast, contradiction, callback, thematic echo, emotional counterpoint, etc.).
- The result must feel like a deliberately constructed short film or feature, not "the best bits from several interviews".
- Think like a screenwriter or documentary director constructing a story from raw interview transcripts — the original sequence inside any one clip is irrelevant once you have decided which moments serve the new narrative.

You are building something new from the raw spoken material across all sources.
""",
    # === NEW 10 TONES (2026 expansion) ===
    "investigative_hook": """
You are a cinematic investigative director building high-tension narrative puzzles.
Your cuts must be:
- Tense, urgent, and highly revelatory — treat the material as pieces of an unfolding mystery.
- Prioritize sharp, standalone, high-impact claims early (strong "hooks" that demand answers).
- Cut immediately after open-ended or provocative statements to create deliberate cliffhangers between segments.
- Use cross-clip contrasts and contradictions to deepen the sense of revelation and withheld information.
- Never invent words; every line is 100% verbatim from the provided clips.
- The overall structure should feel like a thriller or exposé, not a conventional report.
""",
    "masterclass": """
You are an authoritative academic or expert lecturer / masterclass presenter.
Your cuts must be:
- Deliberate, articulate, and structurally rigorous — long, unbroken conceptual arguments are preferred over quick soundbites.
- Prioritize clear, complete reasoning chains and manifesto-like structural statements.
- Automatically filter out conversational stumbles, fillers, and hesitations to preserve intellectual clarity and authority.
- Favor longer, self-contained thought blocks that allow the speaker's full argument to land.
- Cross-clip material is used only to strengthen or extend the central thesis.
- The result should feel like a distilled, high-signal masterclass or keynote, not entertainment.
""",
    "confessional": """
You are an intimate, empathetic documentary director working in a vulnerable confessional mode.
Your cuts must be:
- Quiet, slow-paced, and deeply human — prioritize emotional authenticity over polish.
- Intentionally preserve stumbles, heavy sighs, self-corrections, false starts, and prolonged thinking pauses.
- Let silence and hesitation carry as much weight as the words themselves.
- Use cross-clip material sparingly and only for powerful emotional counterpoint or shared vulnerability.
- The goal is genuine connection and intimacy with the viewer, not narrative efficiency.
- Every word remains 100% verbatim; the editing serves the raw humanity of the performance.
""",
    "engagement_bomb": """
You are a hyper-aggressive short-form (TikTok / Reels / Shorts) director optimized for maximum retention.
Your cuts must be:
- Place the single most shocking, dynamic, or emotionally charged hook at exactly 00:00.
- Restrict every thought block / segment to a maximum of ~15 seconds.
- Execute ultra-tight jump-cuts that completely remove micro-silences, "ums", and dead air between words.
- Favor high-energy, quotable, surprising, or conflict-driven moments regardless of original chronology.
- Use rapid cross-clip intercutting for rhythmic impact and surprise.
- The result must feel like pure, addictive short-form video — fast, loud, and impossible to scroll past.
- Still 100% verbatim — no invented text, only ruthless selection and pacing.
""",
    "subversive": """
You are a witty, self-aware, ironic documentary director who loves to humanize authority.
Your cuts must be:
- Juxtapose formal, serious, or "on-brand" statements against unpolished, raw, or accidental moments (laughter, side-chatter, self-deprecation).
- Actively seek and isolate laughter, casual off-camera comments with crew, and moments of genuine human messiness.
- Use these contrasts to undercut pomposity or reveal the real person behind the performance.
- Cross-clip callbacks and ironic echoes are encouraged.
- The tone is playful but never cruel — the goal is warmth and insight through subversion.
- Maintain journalistic verbatim integrity while celebrating the unscripted.
""",
    "visual_poem": """
You are an avant-garde, sensory, poetic director who edits for musicality and atmosphere rather than literal information.
Your cuts must be:
- Treat vocal pitch, cadence, timbre, rhythm, and sensory language as primary material.
- Prioritize moments with strong poetic or metaphorical imagery, even if the "information" is secondary.
- Edit on the music of the voice — cuts that feel like musical phrases or visual rhythms.
- Allow space, repetition, and non-linear juxtaposition for emotional and atmospheric effect.
- The literal content is secondary to how the voice feels and sounds.
- Result should feel like a visual poem or tone poem made from spoken words — evocative and rhythmic.
""",
    "manifesto": """
You are an inspirational, commanding, mobilizing director building a call-to-arms piece.
Your cuts must be:
- Aggressively cluster collective, action-oriented language ("we must", "our goal", "let's build", "together we will").
- Build momentum through repetition and escalation across speakers.
- Culminate in an absolute, definitive, rising-intonation declarative finale.
- Favor high-stakes, visionary, or urgent statements that feel like a movement or mission launch.
- Cross-clip material is used to create a sense of unified, swelling chorus.
- The overall piece should feel like a powerful, rousing manifesto or launch video.
""",
    "underdog": """
You are a classic three-act narrative director telling an underdog / hero's journey story.
Your cuts MUST follow a strict dramatic structure:
- Act I (The Struggle): Friction, doubt, obstacles, low moments, "the problem is bigger than we thought".
- Act II (The Pivot / Spark / Realization): The turning point, insight, first sign of hope or new approach.
- Act III (The Future / Optimistic / Triumphant): Forward-looking statements, lessons, vision, or hard-won confidence.
- Re-order material across clips to serve this arc, even if it means breaking original chronology.
- Every version should feel like a complete emotional journey from adversity to (hard-won) possibility.
- Use source labels in reasons to show how different voices contribute to each act.
""",
    "analytical": """
You are an objective, structural, calculating analyst or investigative researcher.
Your cuts must be:
- Heavily prioritize clear cause-and-effect logic ("because of X we saw Y", "this led directly to...").
- Extract and foreground numerical data, project metrics, technical terminology, and measurable outcomes.
- Favor precise, matter-of-fact language over emotional or dramatic flourishes.
- Use cross-clip material to build logical chains and reveal underlying mechanisms or patterns.
- Ideal for mapping B-roll or data visualization opportunities.
- The result should feel like a clear, evidence-based structural breakdown or explainer.
""",
    "legacy": """
You are a warm, wistful, melancholic director looking back on a life, career, era, or body of work.
Your cuts must be:
- Prioritize past-tense memories, reflections, philosophical conclusions, and "looking back" statements.
- Favor slower cadence, wisdom, and emotional weight over forward momentum.
- Use cross-clip material for echoes, callbacks, and the sense of a life or project coming into focus.
- Preserve contemplative pauses and reflective tone.
- The overall piece should feel like a thoughtful, grounded legacy portrait or career retrospective.
""",
}


DIRECTOR_PURPOSE_GUIDANCE: dict[str, str] = {
    "News Package": "Create a balanced, self-contained news story with a clear beginning, middle, and end, drawing the strongest material from across all provided sources.",
    "Social Media Teaser": "Make it extremely punchy and curiosity-driven. Strong first 8 seconds. Use the most arresting moments regardless of which original clip they came from.",
    "Best Soundbites / Quotes": "Focus on the most quotable, memorable, or shareable lines across all clips — even if they require intercutting to feel powerful together.",
    "Emotional / Human Story": "Prioritize human emotion, personal stakes, and moments that make the viewer feel something. Use contrast between different speakers when it deepens the emotional impact.",
    "In-depth Highlight": "Go deeper into the most important or surprising territory by combining complementary or contrasting material from multiple sources.",
    # === NEW 5 PURPOSES (2026 expansion) ===
    "Investigative Cold Open": "Construct a high-suspense, puzzle-like sequence that sequences hard-hitting facts and cliffhangers across clips to instantly hook the viewer into a mystery.",
    "Expert Manifesto / Manifesto Call": "Weave high-impact, mission-driven startup or activist claims together, culminating in a powerful, unified call to action using collective verbs.",
    "Character Retrospective": "Build a nostalgic legacy overview looking back on a timeline or career, prioritizing slower-paced, philosophical past-tense reflections.",
    "Social Jump-Cut Strip": "Optimized for TikTok/Shorts. Force an immediate ultra-aggressive structural edit that deletes every micro-pause and fits dynamic soundbites into 15-second thematic bursts.",
    "Three-Act Underdog Arc": "Strictly partition and re-order the cross-clip material into a chronological journey of Adversity (Act 1), The Epiphany (Act 2), and Triumph (Act 3).",
}


# =============================================================================
# LOW-LEVEL HELPERS (duplicated from journalist_cutter to keep that module untouched)
# =============================================================================

def _get_media_mime_type(path: Path) -> str:
    """Return a Gemini-compatible mime type for the given media file."""
    suffix = path.suffix.lower()
    if suffix in (".mp4", ".mov", ".m4v", ".mkv"):
        return "video/mp4"
    if suffix in (".mp3", ".mpeg"):
        return "audio/mpeg"
    if suffix in (".wav", ".wave"):
        return "audio/wav"
    if suffix in (".m4a", ".aac"):
        return "audio/aac"
    if suffix == ".ogg":
        return "audio/ogg"
    if suffix in (".webm",):
        return "video/webm"
    return "video/mp4"


def _normalize_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize incoming segments to a clean list with float 'start'/'end' keys."""
    normalized = []
    for seg in segments or []:
        try:
            start = float(
                seg.get("source_in")
                or seg.get("start")
                or seg.get("in", 0)
            )
            end = float(
                seg.get("source_out")
                or seg.get("end")
                or seg.get("out", 0)
            )
            text = (seg.get("text") or "").strip()
            if end <= start or not text:
                continue

            item = {
                "start": round(start, 2),
                "end": round(end, 2),
                "text": text,
            }
            # Preserve any multi-source metadata
            for k, v in seg.items():
                if k.startswith("source_"):
                    item[k] = v
            normalized.append(item)
        except Exception:
            continue
    return normalized


def _build_labeled_transcript_block(
    label: str,
    filename: str,
    duration: float,
    segments: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """
    Build the labeled block for one source + the corresponding augmented segments.

    Produces a clean, Gemini-friendly markdown structure for multi-source
    Director input (inspired by recommended multi-transcript formatting).
    """
    lines: list[str] = []
    augmented: list[dict] = []

    # Nicer header inspired by good multi-transcript formatting practices
    header = f"\n### {label}: {filename} ({duration:.0f}s)"
    lines.append(header)

    for seg in segments:
        start = float(seg.get("source_in") or seg.get("start", 0))
        end = float(seg.get("source_out") or seg.get("end", 0))
        text = (seg.get("text") or "").strip()
        if not text or end <= start:
            continue

        # Human-readable time for the AI + precise seconds we keep internally
        start_hms = _seconds_to_hms(start)
        end_hms = _seconds_to_hms(end)

        # We show both for maximum clarity when feeding to Gemini
        tc = f"[{start_hms} → {end_hms}]  ({start:.1f}s–{end:.1f}s)"
        lines.append(f"{tc} {text}")

        augmented.append({
            "source_in": round(start, 2),
            "source_out": round(end, 2),
            "text": text,
            "source_label": label,
            "source_filename": filename,
            "source_path": seg.get("source_path"),
            "source_clip_index": seg.get("source_clip_index"),
        })

    return "\n".join(lines).strip(), augmented


def _seconds_to_hms(seconds: float) -> str:
    """Convert seconds to HH:MM:SS format (no milliseconds for readability)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def build_combined_labeled_transcript(
    sources: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """
    Canonical way to produce the single combined, explicitly labeled transcript
    that the Director AI will work from.

    The format uses clear markdown section headers per source
    (e.g. "### C1: filename.mp4 (187s)") followed by timecoded lines with
    both human-readable (HH:MM:SS) and precise seconds. This structure was
    chosen to be easily parseable by Gemini as multiple distinct transcripts
    while remaining highly machine-readable for our export pipeline.

    IMPORTANT: This function receives segments from the catalog database
    (Video.transcription_segments), **not** by parsing files from the
    <catalog>/transcriptions/ folder.

    The .txt files in that folder are output artifacts (plain text, often
    without precise timestamps) and are not used as input for the Director.
    Using the DB segments preserves the original high-precision float
    timestamps returned by the transcription model.

    Each source dict is expected to contain:
        label, filename, duration, segments (list of transcription segments)

    Returns:
        (pretty_labeled_transcript_string, flat_augmented_segments_list)
    """
    blocks: list[str] = []
    flat_segments: list[dict] = []

    for s in sources:
        label = s.get("label") or f"C{s.get('index', 0) + 1}"
        filename = s.get("filename") or "unknown"
        duration = float(s.get("duration") or 0)
        segs = s.get("segments") or []

        block, aug = _build_labeled_transcript_block(label, filename, duration, segs)
        if block:
            blocks.append(block)
            flat_segments.extend(aug)

    combined = "\n".join(blocks).strip()
    return combined, flat_segments


# =============================================================================
# VALIDATION (same contract as journalist_cutter for drop-in compatibility)
# =============================================================================

def _validate_and_normalize_versions(
    data: Any, expected_count: int
) -> list[dict[str, Any]]:
    """
    Validate and clean the JSON returned by Gemini.
    Preserves any source_* keys that were present on input segments.
    """
    if not isinstance(data, list):
        raise ValueError("Model did not return a list of versions.")

    versions: list[dict[str, Any]] = []

    for idx, v in enumerate(data):
        if not isinstance(v, dict):
            continue

        version_id = str(v.get("version_id") or chr(ord("A") + idx))
        title = str(v.get("title", f"Version {version_id}")).strip()[:80]
        narrative_summary = str(v.get("narrative_summary", "")).strip()

        # Support both old "selected_segments" and new "narrative_elements" formats
        narrative_elements = v.get("narrative_elements") or []
        raw_segments = v.get("selected_segments") or []

        clean_segments: list[dict] = []
        total = 0.0
        extracted_narrations = []

        # Prefer the new interleaved format when present
        if narrative_elements and isinstance(narrative_elements, list):
            # Thin first as safety net (in case stored data came from before the prompt fix)
            narrative_elements = _thin_narration_bridges(narrative_elements)
            for item in narrative_elements:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")

                if item_type == "clip":
                    try:
                        start = float(item.get("source_in") or item.get("start", 0))
                        end = float(item.get("source_out") or item.get("end", 0))
                        text = str(item.get("text", "")).strip()
                        if end <= start or not text:
                            continue

                        clean_seg = {
                            "source_in": round(start, 2),
                            "source_out": round(end, 2),
                            "text": text,
                            "reason": item.get("reason") or "Selected for its contribution to the cross-source narrative.",
                        }
                        for k, val in item.items():
                            if k.startswith("source_") and k not in clean_seg:
                                clean_seg[k] = val
                        if "source_label" in item:
                            clean_seg["source_label"] = item["source_label"]

                        clean_segments.append(clean_seg)
                        total += (end - start)
                    except Exception:
                        continue

                elif item_type == "narration":
                    narration_text_item = str(item.get("text", "")).strip()
                    if narration_text_item:
                        extracted_narrations.append(narration_text_item)

        else:
            # Fallback to old selected_segments format
            for seg in raw_segments if isinstance(raw_segments, list) else []:
                try:
                    start = float(seg.get("source_in") or seg.get("start", 0))
                    end = float(seg.get("source_out") or seg.get("end", 0))
                    text = str(seg.get("text", "")).strip()
                    reason = str(seg.get("reason", "")).strip()

                    if end <= start or not text:
                        continue

                    clean_seg = {
                        "source_in": round(start, 2),
                        "source_out": round(end, 2),
                        "text": text,
                        "reason": reason or "Selected for its contribution to the cross-source narrative.",
                    }

                    for k, val in seg.items():
                        if k.startswith("source_") and k not in clean_seg:
                            clean_seg[k] = val

                    if "source" in seg and "source_label" not in clean_seg:
                        clean_seg["source_label"] = str(seg["source"]).strip()

                    clean_segments.append(clean_seg)
                    total += (end - start)
                except Exception:
                    continue

        if not clean_segments:
            continue

        # Combine extracted narrations or fall back to old single narration_text
        if extracted_narrations:
            combined_narration = "\n\n".join(extracted_narrations)
        else:
            combined_narration = str(v.get("narration_text", "")).strip()

        version_dict = {
            "version_id": version_id,
            "title": title,
            "total_duration": round(total, 1),
            "narrative_summary": narrative_summary,
            "narration_text": combined_narration,
            "selected_segments": clean_segments,
        }

        # Store the rich interleaved structure if it was provided.
        # Apply thinning as a safety net against over-frequent bridges from the model.
        if narrative_elements:
            thinned_elements = _thin_narration_bridges(narrative_elements)
            version_dict["narrative_elements"] = thinned_elements
            # Also refresh the combined narration_text from the (now thinned) bridges for legacy consumers
            remaining_bridges = [it.get("text", "") for it in thinned_elements if it.get("type") == "narration"]
            if remaining_bridges:
                version_dict["narration_text"] = "\n\n".join(remaining_bridges)

        if combined_narration:
            # language will be attached by the caller if available
            pass

        versions.append(version_dict)

    if not versions:
        raise ValueError("No valid versions survived validation.")

    return versions[:expected_count]


def validate_and_normalize_versions(
    data: Any, expected_count: int
) -> list[dict[str, Any]]:
    """
    Public alias of the validator, for use by UI code (e.g. diversity retry logic).
    """
    return _validate_and_normalize_versions(data, expected_count)


def get_narrative_sequence(version: dict) -> list[dict]:
    """
    Normalizes a Director version into an ordered list of narrative elements.

    Supports both the new 'narrative_elements' format and the legacy
    'selected_segments' + optional 'narration_text' format.

    The AI Narration / Voiceover Script (narration_text) is treated exactly the same
    as explicit "narration" items (NARRATION BRIDGE (TTS)): it is turned into
    narration entry/entries in the sequence so it gets identical TTS audio (WAV)
    generation + placement + inclusion in the XML timeline.

    Returns a list like:
    [
        {"type": "clip", "source_label": "C1", "source_in": 12.3, "source_out": 28.7, "text": "...", "reason": "..."},
        {"type": "narration", "text": "Bridge text here..."},
        ...
    ]
    """
    elements = []
    has_narration_from_elements = False

    # Preferred new format (narrative_elements may contain clips + narration bridges).
    # We still respect a top-level AI Narration / Voiceover Script (narration_text)
    # as a fallback bridge if the elements didn't include explicit typed narration items.
    narrative_elements = version.get("narrative_elements")
    if narrative_elements and isinstance(narrative_elements, list):
        for item in narrative_elements:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "clip":
                elements.append({
                    "type": "clip",
                    "source_label": item.get("source_label"),
                    "source_in": item.get("source_in"),
                    "source_out": item.get("source_out"),
                    "text": item.get("text"),
                    "reason": item.get("reason"),
                    "source_path": item.get("source_path"),
                    "source_filename": item.get("source_filename"),
                })
            elif t == "narration":
                text = (item.get("text") or "").strip()
                if text:
                    elements.append({"type": "narration", "text": text})
                    has_narration_from_elements = True
        # NOTE: do not return here — we may still need to fall back to narration_text
        # so the AI Narration / Voiceover Script gets the exact same TTS/bridge treatment.
    else:
        # Legacy fallback for clips
        selected = version.get("selected_segments") or []
        for seg in selected:
            elements.append({
                "type": "clip",
                "source_label": seg.get("source_label"),
                "source_in": seg.get("source_in"),
                "source_out": seg.get("source_out"),
                "text": seg.get("text"),
                "reason": seg.get("reason"),
                "source_path": seg.get("source_path"),
                "source_filename": seg.get("source_filename"),
            })

    # If we have no explicit narration bridges from narrative_elements, treat the
    # AI Narration / Voiceover Script (narration_text) as a NARRATION BRIDGE (TTS)
    # exactly the same way: append it so it gets WAV audio + XML track.
    if not has_narration_from_elements:
        narration_text = (version.get("narration_text") or "").strip()
        if narration_text:
            # Treat as one bridge (at end for legacy compat; rich elements interleave properly).
            elements.append({"type": "narration", "text": narration_text})

    return elements


def _thin_narration_bridges(narrative_elements: list[dict], min_clips_between: int = 3) -> list[dict]:
    """
    Safety net: if the model still produces too many narration bridges despite
    updated prompts (too dense = interrupts spoken clips too much), thin them out.
    We keep the first bridge, then ensure at least `min_clips_between` clip items
    before the next bridge, preferring to keep bridges that come after stronger
    narrative moments. This is a post-hoc correction only.
    """
    if not narrative_elements or len(narrative_elements) < 2:
        return narrative_elements

    result = []
    clips_since_bridge = 0
    last_was_clip = False

    for item in narrative_elements:
        itype = item.get("type")
        if itype == "clip":
            result.append(item)
            clips_since_bridge += 1
            last_was_clip = True
        elif itype == "narration":
            text = (item.get("text") or "").strip()
            if not text:
                continue
            # Always keep the very first bridge if it appears early
            if not any(x.get("type") == "narration" for x in result):
                result.append({"type": "narration", "text": text})
                clips_since_bridge = 0
            elif clips_since_bridge >= min_clips_between:
                result.append({"type": "narration", "text": text})
                clips_since_bridge = 0
            # else: drop this excessive bridge
            last_was_clip = False
        else:
            result.append(item)

    # If we ended up with zero bridges but had some, keep the last one as fallback? No, respect the thinning.
    return result


# =============================================================================
# PROMPT CONSTRUCTION (Director-specific)
# =============================================================================

def _build_director_system_prompt(
    max_duration_seconds: float,
    purpose: str,
    tone: str,
    num_versions: int,
    clean_fillers: bool = False,
    min_duration_seconds: float = 0.0,
    *,
    has_audio: bool = False,
    source_count: int = 1,
    narration_style: str | None = None,
    material_language: str = "en",
    narration_min_seconds: float = 0.0,
    narration_max_seconds: float = 0.0,
    narration_min_bridges: int = 0,
    narration_max_bridges: int = 0,
) -> str:
    """
    Construct the system prompt for the multi-source Director role.

    narration_style (if provided and truthy) enables generation of narration/voiceover bridges.
    The specific string value controls the linguistic perspective and tone of the generated narration:
      - "omniscient": Objective, authoritative, third-person journalistic voice.
        Bridges time gaps, provides context, balances sources. Pairs with newsroom, analytical, investigative_hook.
      - "subjective": First-person ("I/We"), reflective, essay-film or diary-like voice.
        Adds emotional shading, subtext, personal connection. Pairs with documentary, flexible, legacy, visual_poem.
      - "explainer": Direct, high-energy, snappy social/short-form voice.
        Punchy, exclamation-heavy, hook-driven setups. Pairs with engagement_bomb, commercial.
    If narration_style is None or falsy, NO narration bridges are generated (pure clip-only output).
    """
    tone_block = DIRECTOR_TONE_INSTRUCTIONS.get(
        tone, DIRECTOR_TONE_INSTRUCTIONS["newsroom"]
    )
    guidance = DIRECTOR_PURPOSE_GUIDANCE.get(
        purpose, "Create a coherent, editorially strong short version by drawing from all available sources."
    )

    narrative_order_instruction = ""
    if tone == "rewrite":
        narrative_order_instruction = (
            "NARRATIVE ARCHITECTURE (REWRITE / SCRIPTWRITER MODE):\n"
            "You are building completely new story structures. You MUST use selection, order, and cross-source juxtaposition "
            "to create versions that feel substantially different from simply playing the clips in their original order. "
            "Break chronology within clips and across clips. Use contrast, contradiction, thematic echo, delayed revelation, "
            "and emotional counterpoint between different speakers as primary tools. "
            "Returning material in roughly the same per-clip sequence it was spoken is a failure of this task."
        )
    else:
        narrative_order_instruction = (
            "NARRATIVE ORDER:\n"
            "You may (and often should) reorder and intercut material from different clips to serve the story. "
            "Within any single speaker's material, preserve natural speech flow unless a different order significantly improves clarity or impact. "
            "The final versions should feel like finished, professionally directed short pieces — not simple 'best of' montages."
        )

    filler_instruction = ""
    if clean_fillers:
        filler_instruction = """
FILLER WORDS & CLEAN SPEECH:
- Prefer segments that form complete, natural sentences.
- Remove only obvious unnecessary fillers while preserving authenticity and meaning.
"""

    # Director mode is transcript-only. No audio listening instructions.
    audio_instruction = ""

    diversity_instruction = f"""
DIVERSITY & INTERCUTTING REQUIREMENT (MANDATORY — NON-NEGOTIABLE):
You are working with material from {source_count} different original clips.
- EVERY version MUST meaningfully intercut and use substantial spoken material from at least two (ideally 3+) different clips.
- Producing a version that is 80%+ from a single clip is a failure of this task.
- Treat this as a director's job: find contrasts, contradictions, emotional counterpoints, and thematic connections BETWEEN the different speakers/clips.
- In BOTH the `narrative_summary` AND every segment's `reason` field, explicitly reference which clip the moment comes from using the labels (C1, C2, etc.).
- For the `reason` field of each segment, write 1-2 short sentences explaining *why* the Director chose this exact moment — focusing on its dramatic, emotional, thematic or narrative value and cross-clip connections when relevant. Be specific and concise.
- The output must clearly demonstrate that you worked across the combined multi-source material.
- For longer total_durations, prefer a smaller number of longer, coherent verbatim blocks rather than dozens of tiny fragments (this keeps the JSON output manageable and parseable).
"""

    # Narration / Voiceover bridge logic (refactored from boolean to flexible style enum)
    narration_enabled = bool(narration_style and str(narration_style).strip())
    style = (str(narration_style).strip().lower() if narration_enabled else None)

    if narration_enabled:
        narration_field = '''"narrative_elements": [
      {"type": "clip", "source_label": "C2", "source_in": 12.45, "source_out": 28.9, "text": "Exact verbatim text"},
      {"type": "narration", "text": "Short connecting narration that makes the story flow and feel alive. Written in the tone of the PURPOSE and in the requested NARRATION STYLE."},
      {"type": "clip", "source_label": "C1", "source_in": 45.1, "source_out": 52.8, "text": "Exact verbatim text"}
    ]'''

        # Base critical instructions (sparing, purposeful, language-correct)
        base_narration_critical = '''- When narration_style is provided, you MUST return "narrative_elements" (interleaved clip + narration) instead of the old flat "selected_segments".
- "narrative_elements" MUST be an ordered array that alternates between "clip" (verbatim spoken) and "narration" bridges, but ONLY where a bridge adds real value.
- IMPORTANT — PURPOSEFUL, SPARING NARRATION (not frequent):
  * Insert narration bridges sparingly and selectively — typically after every 3–5 spoken clip segments, or only at natural transitions, emotional turning points, thematic contrasts, or places where context/time/place/emotion needs a gentle bridge. 
  * Do NOT insert a bridge after (almost) every clip or even every other clip. Overuse makes the spoken material feel interrupted and the bridges lose impact. Fewer, better bridges are strongly preferred.
  * Narration bridges must be short and focused: ideally 1 sentence, at most 2 sentences. Only in exceptional cases use 3 sentences if a slightly longer bridge is essential for clarity or emotional power. Keep them concise, elegant, and non-repetitive.
  * The goal is still to RICHEN the story when a bridge helps: add emotional shading, context, ironic contrast, thematic echoes, time/place signals, or connective tissue — but only when it meaningfully improves the flow.
- OPENING RULE: The very first element should almost always be a "clip". If you start with narration, keep the opening bridge very short (1 sentence max). Do NOT open with setup or exposition.
- Narration text must be in the same language as the source clips ("{material_language}").
- Do NOT dump one big narration block at the beginning or the end. Weave any bridges throughout the timeline only where they serve the story.
'''

        # New user-controlled constraints for number and total length of bridges
        if narration_min_bridges > 0 or narration_max_bridges > 0:
            min_b = narration_min_bridges or 1
            max_b = narration_max_bridges or 8
            base_narration_critical += f'''- NUMBER OF NARRATION BRIDGES (MANDATORY): You MUST produce between {min_b} and {max_b} discrete "narration" items in the narrative_elements for this version. Do not go below the min or above the max.
'''
        if narration_min_seconds > 0 or narration_max_seconds > 0:
            min_s = narration_min_seconds
            max_s = narration_max_seconds or (narration_min_seconds * 3 if narration_min_seconds else 120)
            base_narration_critical += f'''- TOTAL NARRATION DURATION BUDGET: The combined narration text you write (all bridges together) should be sized so that it would take approximately {min_s:.0f}–{max_s:.0f} seconds to speak at a natural, clear pace (~140-160 words per minute / ~2.5 words per second). Plan the amount of text accordingly. Do not make the narration dominate the spoken clips.
'''


        # Perspective / linguistic style instructions (the key addition for the new enum)
        style_instruction = ""
        if style == "omniscient":
            style_instruction = '''
NARRATION STYLE — OMNISCIENT (third-person journalistic):
- Write all narration bridges in an objective, authoritative, third-person voice (e.g. "The team later discovered...", "What the data showed was...").
- The narrator acts as a trusted journalistic guide: providing context, bridging time gaps between clips, surfacing contradictions or missing pieces, and balancing multiple sources without taking a personal side.
- Tone should feel like a high-quality newsroom voice-over or prestige documentary narrator.
- Avoid first-person ("I/We") unless it is literally a direct quote from a clip.
'''
        elif style == "subjective":
            style_instruction = '''
NARRATION STYLE — SUBJECTIVE (first-person reflective / essay-film):
- Write narration bridges in a first-person ("I/We") or deeply personal reflective voice, as if the director or a central participant is speaking directly to the viewer.
- Add emotional shading, subtext, doubt, wonder, or personal connection. The narration can feel like an internal monologue, diary entry, or essay-film voice.
- It is allowed (and often desirable) to comment on the feeling or meaning behind the clips rather than only the facts.
- This style pairs naturally with confessional, legacy, visual_poem, or intimate documentary material.
'''
        elif style == "explainer":
            style_instruction = '''
NARRATION STYLE — EXPLAINER (high-energy short-form / social media hook):
- Write narration in a direct, energetic, snappy, almost YouTube/TikTok explainer or hype voice.
- Use short, punchy sentences, exclamation, questions that hook the viewer, and clear "setup → payoff" structures.
- The voice should feel like the text-to-speech layer on top of fast-cut social video — confident, slightly salesy or highly engaging, optimized for immediate attention.
- Keep bridges extremely concise and forward-driving.
'''
        else:
            # Generic fallback if an unknown style string is passed — still enable narration but keep it purposeful
            style_instruction = '''
NARRATION STYLE — CUSTOM / UNSPECIFIED:
- Adapt the voice and perspective of the narration bridges to best serve the overall TONE and PURPOSE of the cut.
- Keep bridges short, elegant, and in the language of the source material.
'''

        narration_critical = base_narration_critical + style_instruction
    else:
        narration_field = ""
        narration_critical = ""

    prompt = f"""
You are an experienced journalist and documentary director working with multiple separate interview recordings.

{tone_block}

TASK:
Create {num_versions} DIFFERENT, high-quality narrative cuts from the combined material below.
Each cut must be a self-contained short story between {min_duration_seconds} and {max_duration_seconds} seconds.

CONSTRAINTS:
- Only use exact verbatim spoken words from the provided transcripts.
- Every selected segment must carry its correct source_in / source_out from its original clip.
- The combined transcript you are given explicitly labels every segment with its source (C1, C2, ...).
- Preserve those exact timestamps and source attribution in your output.
- The source material is primarily in language code "{material_language}". 
- CRITICAL LANGUAGE RULE: The source clips are in language "{material_language}". 
  → You MUST generate ALL narration text (every "narration" item) in "{material_language}".
  → Do NOT write narration in English unless the clips are in English.
  → This is non-negotiable.

{audio_instruction}
{filler_instruction}
{narrative_order_instruction}

{diversity_instruction}

PURPOSE OF THIS CUT:
{guidance}

OUTPUT FORMAT:
Return ONLY a JSON array of {num_versions} version objects.

When narration_style is **not** provided (or None), use this structure (pure verbatim clips only):
[
  {{
    "version_id": "A",
    "title": "Short descriptive title (max 12 words)",
    "total_duration": 87.4,
    "narrative_summary": "One or two sentences (max ~35 words total) explaining the editorial thinking and how the different sources were used. Be concise.",
    "selected_segments": [ ... verbatim clip segments with source_label ... ]
  }}
]

When narration_style **is** provided, you MUST use the richer interleaved structure with PURPOSEFUL, SPARING narration bridges (not frequent). The linguistic perspective of the narration must follow the requested NARRATION STYLE:
[
  {{
    "version_id": "A",
    "title": "Short descriptive title (max 12 words)",
    "total_duration": 87.4,
    "narrative_summary": "One or two sentences (max ~35 words total) explaining the editorial thinking and how the different sources were used. Be concise.",
    "narrative_elements": [
      {{"type": "clip", "source_label": "C2", "source_in": 12.45, "source_out": 28.9, "text": "Exact verbatim text"}},
      {{"type": "narration", "text": "A short bridge (1-2 sentences max) that adds emotion, context, or a powerful transition — written from the perspective required by the narration_style."}},
      {{"type": "clip", "source_label": "C1", "source_in": 45.1, "source_out": 52.8, "text": "Exact verbatim text"}},
      {{"type": "narration", "text": "Another short bridge (1-2 sentences). Use 3 sentences only if truly essential."}},
      {{"type": "clip", "source_label": "C3", "source_in": 67.0, "source_out": 71.2, "text": "Exact verbatim text"}}
    ]
  }}
]

CRITICAL (FORMAT — MUST FOLLOW EXACTLY):
- Use exactly the keys "source_in" and "source_out" (never "start"/"end").
- When using the simple format, every object in "selected_segments" MUST contain "source_label".
- In "narrative_summary", describe how you used material from multiple different clips. **Keep it to exactly 1-2 short sentences (max 35 words). Do not write long paragraphs or artistic prose.**
- For every "reason" field: **exactly one short sentence** (max 20 words). Be specific but extremely concise.
{narration_critical}
- If you fail to include source_label on segments, fail to intercut across clips, or produce overly frequent/long narration (or completely missing purposeful bridges) when narration_style was provided, the output is invalid.
- Return NOTHING except a valid JSON array. No markdown fences, no introductory text, no explanations. Keep all free-text fields (title, narrative_summary, reasons, narration text) short so the full output fits reliably.
"""
    return prompt.strip()


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def generate_director_cuts(
    segments: list[dict[str, Any]],
    max_duration_seconds: float,
    *,
    min_duration_seconds: float = 0.0,
    purpose: str = "News Package",
    tone: str = "newsroom",
    num_versions: int = 2,
    clean_fillers: bool = False,
    narration_style: str | None = None,   # Optional: "omniscient" | "subjective" | "explainer" (enables narration bridges with specific linguistic perspective)
    generate_narration: bool = False,     # Back-compat: if True and narration_style is None, treat as "omniscient"
    material_language: str | None = None,  # Preferred: language from clip metadata
    model_name: str = DEFAULT_GEMINI_MODEL,
    api_key: str | None = None,
    source_media_path: str | Path | None = None,
    # Director-specific: optionally pass a pre-built labeled transcript
    combined_transcript: str | None = None,
    source_count: int | None = None,
    # New controls for narration bridges (only used when narration_style is set)
    narration_min_seconds: float = 0.0,   # min total spoken seconds for all bridges combined (0 = no min)
    narration_max_seconds: float = 0.0,   # max total spoken seconds for all bridges combined (0 = no hard max, use "sparing")
    narration_min_bridges: int = 0,       # min number of discrete "narration" items (0 = use default sparing heuristic)
    narration_max_bridges: int = 0,       # max number of discrete "narration" items (0 = use default sparing heuristic)
) -> list[dict[str, Any]]:
    """
    Multi-source AI Director.

    Takes a flat list of segments that carry source_* metadata (source_label,
    source_filename, etc.) — exactly the shape produced by the multi-clip UI.

    If `combined_transcript` is provided, that exact labeled text is sent to the
    model. Otherwise the module builds a canonical labeled version from the
    segments that have source information.

    The Director is designed to work from the combined, explicitly labeled
    transcript(s) only. Audio listening is not used in Director mode.

    If `material_language` is provided (from clip metadata), it will be used
    for narration generation instead of auto-detection.

    narration_style:
        If a non-empty string is passed, narration/voiceover bridges are enabled.
        The value determines the required linguistic perspective of the generated
        narration text (see _build_director_system_prompt for details).
        Common values: "omniscient", "subjective", "explainer".
        If None or empty, pure clip-only versions are generated (no narration bridges).

    narration_min_seconds / narration_max_seconds:
        Desired total spoken duration (in seconds) of *all* narration bridges combined
        in a version. The AI will be instructed to produce an appropriate amount of
        narration text (at natural speaking rate) to fit in this range. 0 means no
        explicit budget (fall back to "purposeful and sparing").

    narration_min_bridges / narration_max_bridges:
        Exact min/max number of discrete narration bridge items the version should
        contain (in narrative_elements when using bridges). 0 means use the built-in
        "sparing, typically every 3-5 clips" heuristic.
    """
    if not segments:
        raise ValueError("Cannot generate director cuts from empty material.")

    if model_name not in GEMINI_MODELS:
        print(f"[AI Director] Warning: Model '{model_name}' not supported. Falling back.")
        model_name = DEFAULT_GEMINI_MODEL

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            "AI Director requires 'google-genai'. Run: uv pip install google-genai"
        ) from e

    if not api_key:
        from minicat.core.settings import get_gemini_api_key
        api_key = get_gemini_api_key()

    if not api_key or not api_key.strip():
        raise ValueError("Gemini API key is required for AI Director.")

    client = genai.Client(api_key=api_key.strip())

    normalized = _normalize_segments(segments)

    # Backward compatibility for old callers still passing generate_narration=bool
    if narration_style is None and generate_narration:
        narration_style = "omniscient"  # safe default that matches previous behavior (objective journalistic bridge)

    # Detect how many distinct sources we actually have (needed for prompts + rewrite path)
    detected_sources = {}
    for s in normalized:
        lbl = s.get("source_label") or s.get("source_filename")
        if lbl:
            detected_sources[lbl] = True
    effective_source_count = source_count or max(1, len(detected_sources))

    # Build or accept the labeled transcript the Director will actually read
    if combined_transcript and combined_transcript.strip():
        transcript_for_ai = combined_transcript.strip()
    else:
        # Best-effort reconstruction of labeled blocks from the flat segments
        # Group by source_label
        by_source: dict[str, list[dict]] = {}
        for s in normalized:
            lbl = s.get("source_label") or s.get("source_filename") or "Unknown"
            by_source.setdefault(lbl, []).append(s)

        blocks = []
        for lbl, segs in by_source.items():
            # crude filename from first segment if present
            fname = segs[0].get("source_filename", lbl) if segs else lbl
            dur = sum(se["end"] - se["start"] for se in segs)
            block = f"\n=== {lbl} : {fname} ({dur:.0f}s) ==="
            for seg in sorted(segs, key=lambda x: x["start"]):
                block += f"\n[{seg['start']:.1f}s → {seg['end']:.1f}s] {seg['text']}"
            blocks.append(block)
        transcript_for_ai = "\n".join(blocks).strip()

    # Use provided material language (from clip metadata) if available,
    # otherwise fall back to auto-detection.
    if not material_language:
        material_language = detect_material_language(transcript_for_ai, api_key)

    # === REWRITE (Verbatim Scriptwriter) two-stage path for multi-source Director ===
    if tone == "rewrite":
        print("[AI Director] Using two-stage Verbatim Director pipeline for rewrite tone...")
        return _director_two_stage_rewrite(
            client=client,
            model_name=model_name,
            normalized_segments=normalized,
            combined_transcript=transcript_for_ai,
            max_duration_seconds=max_duration_seconds,
            min_duration_seconds=min_duration_seconds,
            purpose=purpose,
            num_versions=num_versions,
            clean_fillers=clean_fillers,
            source_media_path=None,
            effective_source_count=effective_source_count,
            narration_style=narration_style,
            material_language=material_language or "en",
            narration_min_seconds=narration_min_seconds,
            narration_max_seconds=narration_max_seconds,
            narration_min_bridges=narration_min_bridges,
            narration_max_bridges=narration_max_bridges,
        )

    # Director mode is transcript-only (no audio is ever sent).
    media_part = None

    # Build the Director-specific system prompt (narration_style controls voiceover perspective)
    system_prompt = _build_director_system_prompt(
        max_duration_seconds=max_duration_seconds,
        purpose=purpose,
        tone=tone,
        num_versions=num_versions,
        clean_fillers=clean_fillers,
        min_duration_seconds=min_duration_seconds,
        has_audio=False,
        source_count=effective_source_count,
        narration_style=narration_style,
        material_language=material_language,
        narration_min_seconds=narration_min_seconds,
        narration_max_seconds=narration_max_seconds,
        narration_min_bridges=narration_min_bridges,
        narration_max_bridges=narration_max_bridges,
    )

    user_message = f"""
Here is the combined, explicitly labeled interview material from {effective_source_count} different original clips:

{transcript_for_ai}

You are acting as a director with access to all of this material simultaneously.
Analyze it as one body of work. Find the strongest narrative opportunities that only exist because you have multiple sources.
Return {num_versions} different cutting suggestions.
Each suggestion must have a total runtime between {min_duration_seconds} and {max_duration_seconds} seconds.

CRITICAL FOR RELIABLE OUTPUT: Keep "narrative_summary" to 1-2 sentences (max 35 words). Keep every "reason" to one short sentence. Be concise in all descriptive text so the complete JSON array is emitted without truncation.
"""

    try:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.75 if tone in ("flexible", "rewrite", "documentary") else 0.45,
        )

        contents = [system_prompt, user_message]
        # No audio is ever attached in Director mode.

        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config,
        )

        raw = (response.text or "").strip()
        data = _extract_json(raw)
        versions = _validate_and_normalize_versions(data, num_versions)

        # Attach the material language for narration voiceover consistency
        for v in versions:
            if v.get("narration_text") or v.get("narrative_elements"):
                v["narration_language"] = material_language or v.get("narration_language")

        return versions

    except Exception as e:
        print(f"[AI Director] Generation failed: {e}")
        print("[AI Director] Attempting lightweight JSON repair prompt...")

        # Lightweight repair attempt (cheap, low temp) — mirrors the one used in rewrite path
        try:
            repair_system = (
                "You are a precise JSON repair assistant. The previous response was almost correct "
                "but had formatting or truncation issues. Return ONLY a valid JSON array matching the "
                "requested Director output format exactly. Do not add any commentary, explanations, or markdown."
            )
            repair_user = f"""
The previous attempt produced this (possibly truncated or slightly malformed) output:

{raw[:6000] if 'raw' in locals() else (response.text or '')[:6000] if 'response' in locals() else 'No raw captured'}

Please output a clean, complete JSON array of exactly {num_versions} version objects in the exact format previously requested for the Director.
Use the moments from the original labeled transcript. Keep narrative_summary and reasons concise. Preserve all source_labels and verbatim text.
"""

            repair_resp = client.models.generate_content(
                model=model_name,
                contents=[repair_system, repair_user],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            repaired = _extract_json(repair_resp.text.strip())
            versions = _validate_and_normalize_versions(repaired, num_versions)
            print("[AI Director] JSON repair succeeded.")
            # re-attach language
            for v in versions:
                if v.get("narration_text") or v.get("narrative_elements"):
                    v["narration_language"] = material_language or v.get("narration_language")
            return versions
        except Exception as repair_err:
            print(f"[AI Director] JSON repair also failed: {repair_err}. Using simple fallback.")
            # Simple fallback: just take the first N segments across sources
            return _create_simple_director_fallback(
                normalized, max_duration_seconds, num_versions
            )


def _create_simple_director_fallback(
    normalized_segments: list[dict[str, Any]],
    max_duration_seconds: float,
    num_versions: int,
) -> list[dict[str, Any]]:
    """Very basic fallback that still tries to respect source diversity."""
    if not normalized_segments:
        return []

    # Sort by source then time, take chunks
    sorted_segs = sorted(
        normalized_segments,
        key=lambda s: (s.get("source_label", ""), s["start"]),
    )

    versions = []
    chunk_size = max(1, len(sorted_segs) // max(1, num_versions))

    for i in range(num_versions):
        start_idx = i * chunk_size
        chunk = sorted_segs[start_idx : start_idx + chunk_size]
        if not chunk:
            continue

        total = 0.0
        selected = []
        for seg in chunk:
            dur = seg["end"] - seg["start"]
            if total + dur > max_duration_seconds and selected:
                break
            item = {
                "source_in": seg["start"],
                "source_out": seg["end"],
                "text": seg["text"],
                "reason": f"Fallback selection from {seg.get('source_label', 'source')}",
            }
            # Preserve source metadata if present
            for k in ("source_label", "source_filename", "source_path"):
                if k in seg:
                    item[k] = seg[k]
            selected.append(item)
            total += dur

        if selected:
            versions.append({
                "version_id": chr(ord("A") + i),
                "title": f"Fallback Version {chr(ord('A') + i)}",
                "total_duration": round(total, 1),
                "narrative_summary": "Fallback selection (AI generation failed).",
                "selected_segments": selected,
            })

    return versions


# =============================================================================
# TWO-STAGE VERBATIM DIRECTOR PIPELINE (for tone == "rewrite")
# =============================================================================

def _director_two_stage_rewrite(
    *,
    client,
    model_name: str,
    normalized_segments: list[dict[str, Any]],
    combined_transcript: str,
    max_duration_seconds: float,
    min_duration_seconds: float,
    purpose: str,
    num_versions: int,
    clean_fillers: bool,
    source_media_path: str | Path | None,
    effective_source_count: int,
    narration_style: str | None = None,
    material_language: str = "en",
    narration_min_seconds: float = 0.0,
    narration_max_seconds: float = 0.0,
    narration_min_bridges: int = 0,
    narration_max_bridges: int = 0,
) -> list[dict[str, Any]]:
    """
    Two-stage pipeline for the Director in "rewrite" / Verbatim Scriptwriter mode.

    Stage 1: Mine the strongest raw dramatic moments across ALL sources
             (the model sees the fully labeled combined transcript).
    Stage 2: Given only those mined moments (still carrying source labels),
             act as narrative architect and build genuinely new story
             structures using cross-source juxtaposition as a primary tool.
    """
    from google.genai import types

    # Director mode is transcript-only. No audio is used in the two-stage rewrite path.
    stage1_media = None

    # Back-compat for old generate_narration callers
    if narration_style is None and generate_narration:
        narration_style = "omniscient"

    # --- Stage 1: Mine powerful verbatim moments across all sources ---
    print("[AI Director] Stage 1: Mining strongest moments across all clips...")

    stage1_system = f"""
You are a documentary scriptwriter and narrative architect preparing to build short non-fiction films from several separate interview recordings.

You have been given one combined, explicitly labeled transcript. Every segment is marked with its source (C1, C2, ...).

Your only job right now is to identify the best raw building blocks.

Select 15–25 of the most dramatically useful, emotionally charged, thematically rich, or narratively potent VERBATIM moments from the entire combined material.

For each moment you select, output:
- source_in / source_out (exact seconds from its original clip)
- text (exact spoken words)
- source_label (C1, C2, etc. — this is critical)
- dramatic_potential: one short sentence explaining the storytelling value, with explicit reference to other clips when relevant (e.g. "Powerful contradiction to the optimistic claim in C2", "Emotional counterpoint to C1's earlier statement").

Return ONLY a JSON array of objects. Do not build any story yet.
"""

    stage1_user = f"""
Here is the full combined, labeled transcript from {effective_source_count} different clips:

{combined_transcript}

Extract the strongest raw verbatim moments for building completely new narrative versions. Think across sources — the best building blocks often come from the tension or contrast between clips.
"""

    try:
        contents1 = [stage1_system, stage1_user]
        # No audio attached in Director mode.

        resp1 = client.models.generate_content(
            model=model_name,
            contents=contents1,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.65,
            ),
        )
        raw1 = resp1.text.strip()
        candidates = _extract_json(raw1)
    except Exception as e:
        print(f"[AI Director] Stage 1 mining failed: {e}. Falling back to single pass.")
        return _single_pass_director_fallback(
            client=client,
            model_name=model_name,
            combined_transcript=combined_transcript,
            normalized_segments=normalized_segments,
            max_duration_seconds=max_duration_seconds,
            min_duration_seconds=min_duration_seconds,
            purpose=purpose,
            num_versions=num_versions,
            clean_fillers=clean_fillers,
            source_media_path=None,
            effective_source_count=effective_source_count,
            narration_min_seconds=narration_min_seconds,
            narration_max_seconds=narration_max_seconds,
            narration_min_bridges=narration_min_bridges,
            narration_max_bridges=narration_max_bridges,
        )

    # Keep valid candidates and preserve source info
    valid_candidates: list[dict] = []
    for c in candidates if isinstance(candidates, list) else []:
        try:
            s = float(c.get("source_in") or c.get("start", 0))
            e = float(c.get("source_out") or c.get("end", 0))
            txt = (c.get("text") or "").strip()
            src = c.get("source_label") or c.get("source") or "Unknown"
            pot = (c.get("dramatic_potential") or "").strip()
            if e > s + 0.3 and txt:
                valid_candidates.append({
                    "source_in": round(s, 2),
                    "source_out": round(e, 2),
                    "text": txt,
                    "source_label": str(src).strip(),
                    "dramatic_potential": pot or "Strong moment",
                })
        except Exception:
            continue

    if len(valid_candidates) < 5:
        print("[AI Director] Stage 1 returned too few moments. Using fallback.")
        return _single_pass_director_fallback(
            client=client,
            model_name=model_name,
            combined_transcript=combined_transcript,
            normalized_segments=normalized_segments,
            max_duration_seconds=max_duration_seconds,
            min_duration_seconds=min_duration_seconds,
            purpose=purpose,
            num_versions=num_versions,
            clean_fillers=clean_fillers,
            source_media_path=None,
            effective_source_count=effective_source_count,
            narration_min_seconds=narration_min_seconds,
            narration_max_seconds=narration_max_seconds,
            narration_min_bridges=narration_min_bridges,
            narration_max_bridges=narration_max_bridges,
        )

    # Shuffle so Stage 2 is not biased by mining order
    random.shuffle(valid_candidates)

    # Present to Stage 2 with source labels front and center
    candidate_text = "\n".join(
        f"[{c['source_label']}] {c['source_in']}s–{c['source_out']}s: {c['text']}\n   Dramatic value: {c['dramatic_potential']}"
        for c in valid_candidates[:22]
    )

    # --- Stage 2: Build new story architectures as Director ---
    print(f"[AI Director] Stage 2: Architecting {num_versions} new stories from mined moments...")

    narration_enabled = bool(narration_style and str(narration_style).strip())
    style = (str(narration_style).strip().lower() if narration_enabled else None)

    if narration_enabled:
        # Purposeful, sparing narration mode for the rewrite / scriptwriter path (with style)
        narr_budget_lines = []
        if narration_min_bridges or narration_max_bridges:
            minb = narration_min_bridges or 1
            maxb = narration_max_bridges or 6
            narr_budget_lines.append(f"- NUMBER OF BRIDGES: Produce between {minb} and {maxb} discrete narration bridges for this version.")
        if narration_min_seconds or narration_max_seconds:
            mins = narration_min_seconds
            maxs = narration_max_seconds or (mins * 2.5 if mins else 90)
            narr_budget_lines.append(f"- TOTAL NARRATION LENGTH: The combined text of all bridges should speak in roughly {mins:.0f}–{maxs:.0f} seconds at natural pace (~2.5 w/s). Size your narration text accordingly.")
        budget_text = "\n".join(narr_budget_lines)
        if budget_text:
            budget_text = "\nNARRATION BUDGET (user specified):\n" + budget_text + "\n"

        stage2_system = f"""
You are a scriptwriter and director constructing short non-fiction narratives from raw verbatim interview material taken from multiple separate recordings.

You have been given a collection of the strongest moments, **each explicitly labeled with its source (C1, C2, ...)**.

Your ONLY job is to build {num_versions} completely different short narrative versions using ONLY these exact spoken words.

CRITICAL RULES (non-negotiable):
- Every word 100% verbatim from the provided moments. No inventions, no paraphrasing.
- You MUST create versions that actively intercut between different clips. A version that is mostly from one clip is a failure.
- When building the story, you MUST use the "narrative_elements" format (not the old flat selected_segments).
- "narrative_elements" is an ordered list that alternates between verbatim "clip" items and "narration" bridges ONLY where the bridge adds clear value.

PURPOSEFUL + SPARING NARRATION REQUIREMENTS (MANDATORY when narration is enabled):
- Insert narration bridges sparingly and selectively — typically every 3–5 spoken clips, or only at natural story transitions, emotional shifts, thematic contrasts, or moments that need context/time/place/emotional glue. 
- Do NOT insert a bridge after nearly every clip or every other clip. Too many bridges interrupt the spoken material and dilute their impact. Fewer, high-quality bridges are strongly preferred.
- Narration bridges must be short: ideally 1 sentence, at most 2 sentences. Only very rarely use 3 sentences if absolutely necessary for the story. Keep them concise, elegant, and punchy.
- Use the narration moments to add emotional shading, context, ironic contrast, thematic echoes, time/place signals, or connective tissue — but only when it meaningfully enriches.
- OPENING RULE: The story should almost always begin with a "clip" element. If you start with narration, keep the opening bridge very short (1 sentence). Avoid long setup or exposition.
- All narration must be written in the original language of the source material: {material_language}.
- In the narrative_summary, describe both the cross-source intercutting AND how the (sparing) narration bridges help weave a richer story.
{budget_text}
OUTPUT FORMAT (strict — use this exactly):
[
  {{
    "version_id": "A",
    "title": "Short descriptive title",
    "total_duration": 92.5,
    "narrative_summary": "How you built a new story using multiple sources + the role of the narration bridges. (1-2 sentences, max 35 words, concise).",
    "narrative_elements": [
      {{"type": "clip", "source_label": "C2", "source_in": 12.4, "source_out": 27.8, "text": "Exact verbatim...", "reason": "C2 — powerful opening image that establishes the central conflict."}},
      {{"type": "narration", "text": "A short bridge (1-2 sentences) that adds meaningful emotion, context, or transition."}},
      {{"type": "clip", "source_label": "C1", "source_in": 68.0, "source_out": 74.2, "text": "Exact verbatim...", "reason": "C1 — direct contradiction to C2 that reveals the human cost behind the official narrative."}},
      {{"type": "narration", "text": "Another short bridge (1-2 sentences max)."}}
    ]
  }}
]

Return ONLY the JSON array. Nothing else.
"""
        stage2_user = f"""
Here are the strongest raw verbatim moments available (with source labels and dramatic notes):

{candidate_text}

PURPOSE: {purpose}

Construct {num_versions} genuinely new, richly woven narrative versions.
Because narration_style is provided, you MUST use sparing, purposeful narration bridges (as described in the system instructions — only every 3-5 clips or at key moments, 1-2 sentences each). Begin with a clip in almost all cases.

Keep narrative_summary extremely short (1-2 sentences, <=35 words). All reasons: one short sentence. This prevents output truncation.
"""
    else:
        # Original powerful non-narration rewrite path — STRICTLY NO NARRATION
        stage2_system = f"""
You are a scriptwriter and director constructing short non-fiction narratives from raw verbatim interview material taken from multiple separate recordings.

You have been given a collection of the strongest moments, **each explicitly labeled with its source (C1, C2, ...)**.

Your ONLY job is to build {num_versions} completely different short narrative versions using ONLY these exact spoken words.

CRITICAL RULES (non-negotiable):
- Every word 100% verbatim. No inventions.
- You MUST create versions that actively intercut between different clips. A version that is mostly from one clip is a failure.
- **STRICTLY FORBIDDEN**: Do not create any narration text, do not use "narrative_elements", do not add any bridges or connecting text. The user explicitly did NOT request narration.
- Use ONLY the flat "selected_segments" format with verbatim spoken clips.
- In the `narrative_summary`, explicitly describe the cross-source storytelling. **Keep narrative_summary to 1-2 short sentences (max 35 words total).**
- For every selected segment, provide a short `reason` (**maximum 1 sentence**, max ~18 words) that clearly explains *why* the Director chose this exact moment. Focus on its dramatic, emotional, thematic or narrative value and any meaningful connection to other clips. Be specific and concise. Bad example: "C1 - important". Good example: "C1 — raw emotional turning point that directly undercuts the optimistic framing given earlier in C2."

OUTPUT REQUIREMENTS (very strict):
- Return ONLY a valid JSON array of exactly {num_versions} objects.
- Every segment MUST be in "selected_segments" (NOT narrative_elements).
- Every segment in selected_segments MUST include "source_label".
- No markdown, no extra text, no narration of any kind.

Do not return anything except the JSON array.
"""

        stage2_user = f"""
Here are the strongest raw verbatim moments available (with source labels and dramatic notes):

{candidate_text}

PURPOSE: {purpose}

Now construct {num_versions} genuinely new narrative versions from this material only.
**IMPORTANT: The user did NOT request narration. Return ONLY selected_segments with verbatim clips. Do not add any narration bridges or use narrative_elements.**

Keep narrative_summary to 1-2 sentences (max 35 words). Every reason must be one short sentence. Conciseness is required for complete JSON output.
"""

    try:
        resp2 = client.models.generate_content(
            model=model_name,
            contents=[stage2_system, stage2_user],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.82,
            ),
        )
        raw2 = resp2.text.strip()
        data = _extract_json(raw2)
        versions = _validate_and_normalize_versions(data, num_versions)

        # Attach language for voiceover consistency (rich narration path)
        for v in versions:
            if narration_enabled or v.get("narrative_elements") or v.get("narration_text"):
                v["narration_language"] = material_language or v.get("narration_language", "en")

        return versions

    except Exception as stage2_err:
        print(f"[AI Director] Stage 2 failed to produce valid JSON: {stage2_err}")
        print("[AI Director] Attempting lightweight JSON repair prompt...")

        # Lightweight repair attempt (very cheap)
        try:
            repair_system = (
                "You are a precise JSON repair assistant. The previous response was almost correct "
                "but had formatting issues. Return ONLY a valid JSON array matching the requested Director output format. "
                "Do not add commentary."
            )
            repair_user = f"""
The previous attempt produced this (possibly malformed) output:

{raw2[:4000] if 'raw2' in locals() else 'No raw output captured'}

Please output a clean JSON array of exactly {num_versions} version objects in the exact Director format previously requested.
Use only the moments that were provided earlier. Preserve source labels.
"""

            repair_resp = client.models.generate_content(
                model=model_name,
                contents=[repair_system, repair_user],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,   # low temp for repair
                ),
            )
            repaired = _extract_json(repair_resp.text.strip())
            versions = _validate_and_normalize_versions(repaired, num_versions)
            print("[AI Director] JSON repair succeeded.")
            return versions
        except Exception as repair_err:
            print(f"[AI Director] JSON repair also failed: {repair_err}. Using single-pass fallback.")
            return _single_pass_director_fallback(
                client=client,
                model_name=model_name,
                combined_transcript=combined_transcript,
                normalized_segments=normalized_segments,
                max_duration_seconds=max_duration_seconds,
                min_duration_seconds=min_duration_seconds,
                purpose=purpose,
                num_versions=num_versions,
                clean_fillers=clean_fillers,
                source_media_path=None,
                effective_source_count=effective_source_count,
                narration_min_seconds=narration_min_seconds,
                narration_max_seconds=narration_max_seconds,
                narration_min_bridges=narration_min_bridges,
                narration_max_bridges=narration_max_bridges,
            )


def _single_pass_director_fallback(
    *,
    client,
    model_name: str,
    combined_transcript: str,
    normalized_segments: list[dict[str, Any]],
    max_duration_seconds: float,
    min_duration_seconds: float,
    purpose: str,
    num_versions: int,
    clean_fillers: bool,
    source_media_path: str | Path | None,
    effective_source_count: int,
    narration_min_seconds: float = 0.0,
    narration_max_seconds: float = 0.0,
    narration_min_bridges: int = 0,
    narration_max_bridges: int = 0,
) -> list[dict[str, Any]]:
    """Single-pass fallback for rewrite when the two-stage pipeline fails."""
    from google.genai import types

    # Transcript-only mode — no audio fallback.
    fb_media = None

    system = f"""
You are a director building new short non-fiction stories from verbatim material across {effective_source_count} different interview clips.

You MUST use only the exact spoken words provided.
You are allowed (and expected) to intercut between different sources to create stronger narrative versions.

Return {num_versions} versions in the standard Director JSON format.
Each version must use material from multiple different clips.
"""

    user = f"""
Combined labeled transcript:

{combined_transcript[:12000]}

PURPOSE: {purpose}
Build {num_versions} new narrative versions from this verbatim multi-source material only.
"""

    try:
        contents = [system, user]
        # No audio in Director mode.

        resp = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.8,
            ),
        )
        raw = resp.text.strip()
        data = _extract_json(raw)
        return _validate_and_normalize_versions(data, num_versions)
    except Exception:
        # Last-resort ultra-simple fallback
        return _create_simple_director_fallback(
            normalized_segments, max_duration_seconds, num_versions
        )
