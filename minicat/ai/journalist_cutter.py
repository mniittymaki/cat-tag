"""
AI-powered interview cutting using Gemini.

The module turns a time-coded transcript into one or more professional
"journalist cuts" — short, coherent narrative versions of an interview.

**Transcript-only mode**: Gemini receives only the accurate, time-coded transcript.
No audio or video is ever sent. All timing and verbatim text come strictly from
the transcript segments. This is the required behavior for journalistic safety.

Special "rewrite" / "Verbatim Scriptwriter" mode acts as a narrative architect
that builds completely new story structures using only exact verbatim quotes
from the source (no invented words, full journalistic safety, same XML export contract).

The legacy `generate_journalist_cuts_from_audio()` (pure audio, no transcript)
remains available for experiments but is not used by the main UI flow.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from minicat.core.settings import DEFAULT_GEMINI_MODEL, GEMINI_MODELS

# =============================================================================
# TONE & PURPOSE GUIDANCE (centralized, used by prompt builder and docs)
# =============================================================================

TONE_INSTRUCTIONS: dict[str, str] = {
    "newsroom": """
You are a senior news editor at a respected public broadcaster (think Yle, BBC, or NPR).
Your cuts must be:
- Factually tight and journalistically responsible
- Clear narrative structure (usually Hook → Context → Key Revelation/Quote → Resolution/Implication)
- Prioritize clarity and public interest over drama
- Avoid sensationalism
""",
    "flexible": """
You are a creative documentary or long-form editor (think This American Life, The New York Times audio, or high-end YouTube documentaries).
Your cuts can be:
- More emotional, atmospheric, and human-centered
- Allow surprising or non-linear juxtapositions if they serve the story
- Embrace interesting turns of phrase, pauses, and personality
- Still coherent, but more artistic than traditional news
""",
    "documentary": """
You are a thoughtful documentary editor working on character-driven or observational films.
Your cuts should feel:
- Story-driven and atmospheric
- Allow space for emotion, silence, and subtext
- Build a deeper human or thematic portrait rather than just delivering information
- Use juxtaposition and rhythm to create meaning beyond the literal words
- Prioritize authenticity and emotional truth over tight pacing
""",
    "corporate": """
You are a professional corporate communications editor.
Your cuts must be:
- Polished, credible, and brand-appropriate
- Clear, structured, and professional in tone
- Focused on clarity, trust, and key business or organizational messages
- Avoid overly casual language, humor, or dramatic flair
- Suitable for internal videos, investor updates, or B2B communication
""",
    "commercial": """
You are a commercial / advertising editor working on marketing content.
Your cuts should be:
- Persuasive and benefit-driven
- Energetic, confident, and modern in tone
- Highlight value propositions, emotional hooks, and strong calls to action
- Use a marketing-friendly voice that feels promotional but not pushy
- Optimized for engagement and brand impact
""",
    "rewrite": """
You are an experienced scriptwriter and narrative architect working on a journalistic documentary, news feature, or prestige non-fiction piece.
You must build a completely NEW story using ONLY the exact verbatim spoken words from the provided interview material.
Strict rules (non-negotiable for journalistic integrity):
- You may NEVER invent, paraphrase, or rewrite what the interviewee actually said. Every word in the final cut must be 100% their original spoken language.
- Your only creative tools are: which exact moments you select, and the order + juxtaposition in which you present them.
- You MUST treat the transcript as raw dramatic material, not as a conversation to lightly edit.
- Design a strong narrative architecture: clear dramatic arc, deliberate revelation order, emotional progression, thematic resonance, and effective use of juxtaposition.
- Break chronological order in a meaningful, structural way in every version. Small local reorders are not enough.
- Think like a screenwriter or documentary editor constructing a story from interview transcripts — the original sequence of the conversation is irrelevant.
- The output sequence must feel like a re-imagined short film or feature, not "the interview with some parts removed".
""",
    # === NEW 10 TONES (2026 expansion, copied from Director for consistency) ===
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

PURPOSE_GUIDANCE: dict[str, str] = {
    "News Package": "Create a balanced, self-contained news story with a clear beginning, middle, and end.",
    "Social Media Teaser": "Make it extremely punchy, curiosity-driven, and optimized for short attention spans (strong first 8 seconds).",
    "Best Soundbites / Quotes": "Focus on the most quotable, memorable, or shareable moments — even if they are not in perfect chronological order.",
    "Emotional / Human Story": "Prioritize human emotion, personal stakes, and moments that make the viewer feel something.",
    "In-depth Highlight": "Go deeper into the most important or surprising part of the conversation.",
    # === NEW 5 PURPOSES (2026 expansion) ===
    "Investigative Cold Open": "Construct a high-suspense, puzzle-like sequence that sequences hard-hitting facts and cliffhangers across clips to instantly hook the viewer into a mystery.",
    "Expert Manifesto / Manifesto Call": "Weave high-impact, mission-driven startup or activist claims together, culminating in a powerful, unified call to action using collective verbs.",
    "Character Retrospective": "Build a nostalgic legacy overview looking back on a timeline or career, prioritizing slower-paced, philosophical past-tense reflections.",
    "Social Jump-Cut Strip": "Optimized for TikTok/Shorts. Force an immediate ultra-aggressive structural edit that deletes every micro-pause and fits dynamic soundbites into 15-second thematic bursts.",
    "Three-Act Underdog Arc": "Strictly partition and re-order the cross-clip material into a chronological journey of Adversity (Act 1), The Epiphany (Act 2), and Triumph (Act 3).",
}


def _get_media_mime_type(path: Path) -> str:
    """Return a Gemini-compatible mime type for the given media file."""
    suffix = path.suffix.lower()
    if suffix in (".mp4", ".mov", ".m4v", ".mkv"):
        return "video/mp4"
    if suffix in (".mp3", ".mpeg"):
        return "audio/mpeg"
    if suffix in (".wav", ".wave"):
        return "audio/wav"
    if suffix in (".m4a", ".mp4"):
        # Production transcription proxy uses .m4a (AAC in MP4 container). "audio/mp4" is reliable for Gemini.
        return "audio/mp4"
    if suffix == ".aac":
        return "audio/aac"
    if suffix == ".ogg":
        return "audio/ogg"
    if suffix in (".webm",):
        return "video/webm"
    # Sensible default — Gemini is fairly tolerant
    return "video/mp4"


def generate_journalist_cuts(
    segments: list[dict[str, Any]],
    max_duration_seconds: float,
    *,
    min_duration_seconds: float = 0.0,
    purpose: str = "News Package",
    tone: str = "newsroom",  # newsroom, flexible, documentary, corporate, commercial
    num_versions: int = 2,
    clean_fillers: bool = False,  # When True, prefer and post-process for clean sentences without fillers
    generate_narration: bool = False,  # Back-compat: if True and narration_style is None, treat as "omniscient"
    narration_style: str
    | None = None,  # Optional: None | "omniscient" | "subjective" | "explainer" (enables style-aware voiceover narration script)
    material_language: str = "en",  # Language for any generated narration (must match source)
    model_name: str = DEFAULT_GEMINI_MODEL,
    api_key: str | None = None,
    # New controls for the (single) narration_text produced for Journalist cuts
    narration_min_seconds: float = 0.0,  # target total spoken seconds for the whole narration_text
    narration_max_seconds: float = 0.0,
    narration_min_bridges: int = 0,  # influences how many distinct "bridge sections" the narration script should have (0 = current sparing logic)
    narration_max_bridges: int = 0,
) -> list[dict[str, Any]]:
    """
    Ask Gemini to act as a professional journalist/editor and create
    one or more short, coherent "cuts" from a full interview transcript.

    AI Journalist is strictly **transcript-only**.

    Gemini receives only the accurate, time-coded transcript segments.
    No audio or video is ever sent to the model. All `source_in`/`source_out`
    timings and verbatim text are taken exclusively from the transcript
    (full journalistic safety + perfect XML/SRT/render sync).

    Parameters
    ----------
    segments
        List of timed segments (from transcription). Provides exact timings + text.
    max_duration_seconds
        Hard upper limit for each suggested cut.
    min_duration_seconds
        Optional lower limit for each version.
    purpose
        The journalistic goal (e.g. "News Package", "Social Media Teaser",
        "Best Soundbites / Quotes", "Emotional / Human Story", "In-depth Highlight").
    tone
        "newsroom"    → tight, factual, classic journalistic structure.
        "flexible"    → more creative, emotional, narrative storytelling.
        "documentary" → story-driven, atmospheric, character-focused.
        "corporate"   → professional, polished, business-appropriate.
        "commercial"  → persuasive, energetic, marketing-oriented.
        "rewrite"     → Verbatim Scriptwriter mode: AI acts as a narrative architect building a
                        completely new story using ONLY exact verbatim quotes from the transcript.
                        Strong re-sequencing and juxtaposition required. Journalistic safety: never
                        invent or paraphrase spoken words. Output remains compatible with XML export.
    clean_fillers
        When True, the AI is instructed to strongly prefer clean, complete sentences
        and a post-processing step removes common filler words ("um", "uh", "like", "you know",
        "tota", "niinku", etc.) from the final selected text. Great for polished deliverables.
    generate_narration
        Back-compat: if True and narration_style is None, acts as "omniscient".
    narration_style
        If set to "omniscient", "subjective", or "explainer" (matching AI Director), the AI produces
        a style-aware "narration_text" (voiceover script) on each version. This brings the full
        VoiceOver/Narration options from AI Director to single-clip AI Journalist cuts.
        The script is written in material_language and is for TTS as one "Narration.wav".

    narration_min_seconds / narration_max_seconds:
        Guide the AI on the desired total spoken length of the narration_text (in seconds at natural pace).
        narration_min_bridges / max_bridges can suggest how many distinct "bridge passages" to structure the script with.
    material_language
        The language code of the source material (e.g. "en", "fi"). Used to instruct the model
        to write any narration_text in the correct language, and passed through to the version
        for later TTS rendering.
    num_versions
        How many different cutting approaches to generate (recommended 1–3).
    model_name
        Gemini model to use.
    api_key
        Gemini API key. Falls back to settings if not provided.

    Returns
    -------
    list[dict]
        A list of version dicts. Timings and text always come from the provided
        transcript segments.
    """
    if not segments:
        raise ValueError("Cannot generate cuts from an empty transcript.")

    # Detect multi-source input (segments carry source_* keys from the multi-clip journalist dialog)
    is_multi_source = any(
        "source_label" in s or "source_filename" in s or "source_path" in s for s in segments
    )

    if model_name not in GEMINI_MODELS:
        print(f"[AI Journalist] Warning: Model '{model_name}' not in supported list. Falling back.")
        model_name = DEFAULT_GEMINI_MODEL

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            "AI Journalist Cutter requires 'google-genai'. Please run: uv pip install google-genai"
        ) from e

    # Resolve API key
    if not api_key:
        from minicat.core.settings import get_gemini_api_key

        api_key = get_gemini_api_key()

    if not api_key or not api_key.strip():
        raise ValueError("Gemini API key is required for AI cutting.")

    client = genai.Client(api_key=api_key.strip())

    # Normalize for back-compat with old callers using generate_narration=bool (like Director)
    if narration_style is None and generate_narration:
        narration_style = "omniscient"

    # Normalize segments to a clean list of dicts with float seconds
    normalized_segments = _normalize_segments(segments)

    # ------------------------------------------------------------------
    # Transcript-only mode (no audio/video is ever sent)
    # ------------------------------------------------------------------
    has_media = False  # Always False in current transcript-only journalist mode

    # ======================================================================
    # TWO-STAGE VERBATIM SCRIPTWRITER PIPELINE (for rewrite tone only)
    # This is the core of the "act as a scriptwriter, not a journalist editor".
    # Stage 1: Mine the best raw dramatic material (no ordering yet).
    # Stage 2: Build completely new story architecture from the mined moments only.
    # Both stages stay 100% verbatim. Output shape is unchanged.
    # ======================================================================
    if tone == "rewrite":
        print("[AI Journalist] Using two-stage Verbatim Scriptwriter pipeline...")

        versions = _generate_rewrite_versions_two_stage(
            client=client,
            model_name=model_name,
            normalized_segments=normalized_segments,
            max_duration_seconds=max_duration_seconds,
            min_duration_seconds=min_duration_seconds,
            purpose=purpose,
            num_versions=num_versions,
            clean_fillers=clean_fillers,
            generate_narration=bool(narration_style),
            narration_style=narration_style,
            material_language=material_language,
            narration_min_seconds=narration_min_seconds,  # noqa: F821
            narration_max_seconds=narration_max_seconds,  # noqa: F821
            narration_min_bridges=narration_min_bridges,  # noqa: F821
            narration_max_bridges=narration_max_bridges,  # noqa: F821
        )

        # Still run the existing reorder detection + retry as a final safety net
        versions = _enforce_rewrite_reordering_if_needed(
            client=client,
            model_name=model_name,
            versions=versions,
            normalized_segments=normalized_segments,
            max_duration_seconds=max_duration_seconds,
            num_versions=num_versions,
            transcript_text_for_retry=_segments_to_bag_of_moments(normalized_segments),
        )

    else:
        # Normal single-call path for all other tones (the main "Journalist mode")
        transcript_text = _segments_to_readable_transcript(normalized_segments)
        presentation_note = ""

        system_prompt = _build_journalist_system_prompt(
            max_duration_seconds=max_duration_seconds,
            purpose=purpose,
            tone=tone,
            num_versions=num_versions,
            clean_fillers=clean_fillers,
            min_duration_seconds=min_duration_seconds,
            has_audio=False,  # AI Journalist is transcript-only
            generate_narration=bool(narration_style),
            narration_style=narration_style,
            material_language=material_language,
            narration_min_seconds=narration_min_seconds,  # noqa: F821
            narration_max_seconds=narration_max_seconds,  # noqa: F821
            narration_min_bridges=narration_min_bridges,  # noqa: F821
            narration_max_bridges=narration_max_bridges,  # noqa: F821
        )

        multi_director_note = ""
        if is_multi_source:
            multi_director_note = """
MULTI-CLIP / MULTI-SOURCE MODE — MANDATORY WORKFLOW:

You are an experienced journalist and documentary director.

PHASE 1 — FULL ANALYSIS (do this thoroughly first, before any scripting):
- Read the complete labeled transcripts of every clip.
- Analyze the entire body of material as a whole:
  • Identify the most powerful, emotionally charged, surprising, revealing, or thematically significant moments across ALL clips.
  • Map connections, contrasts, contradictions, and juxtaposition opportunities between different speakers/clips.
  • Understand the overall narrative potential of the combined material.
- Do NOT propose any cuts or versions yet. This phase is pure analysis and synthesis.

PHASE 2 — SCRIPTING & DIRECTING (only after Phase 1):
- Now construct {num_versions} complete, self-contained narrative cuts.
- You are free (and expected) to intercut, reorder, and juxtapose moments from different original clips to serve the story.
- Every word must be 100% exact verbatim from the transcripts.
- The final versions must feel like finished, professionally directed short pieces — not simple montages or "best of" compilations.

DIVERSITY REQUIREMENT (MANDATORY):
You MUST use substantial spoken material from **multiple different clips** in every version.
- Using material from only 1 clip (or almost only 1 clip) is a failure.
- You should normally use material from at least 3 different clips (C1, C2, C3...) across the versions.
- The more the versions intercut between different clips, the better (as long as it serves a coherent story).

SOURCE TRACKING:
The source labels (C1, C2, ...) or filenames in the transcript tell you which original recording each moment comes from.
When you output selected_segments you MUST preserve the exact source_in / source_out numbers from the transcript of the correct source, and the system will carry the source metadata.

CRITICAL FOR THE FINAL SCRIPT:
In your `narrative_summary` and especially in every segment's `reason` field, clearly state which clip the moment comes from using the labels (e.g. "Powerful emotional turning point from the main interviewee in C1", "Revealing contradiction from the expert in C2 that undercuts the claim in C1").
This attribution must be explicit so the final exported script makes it obvious exactly which original clip each piece of spoken content originates from.

OUTPUT FORMAT FOR MULTI-CLIP:
In addition to the standard fields, for every object in `selected_segments` please also return:
  "source": "C1"     // the exact label (C1, C2, ...) as shown in the input transcript for that moment

Focus exclusively on the spoken words, emotion, pacing, and delivery as described in the transcripts.
"""

        user_message = f"""
Here is the available interview material:

{transcript_text}

{presentation_note}

{multi_director_note}

Please analyze it as a professional journalist and return {num_versions} different cutting suggestions.
Each suggestion must have a total runtime between {min_duration_seconds} and {max_duration_seconds} seconds.
"""

        try:
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.7 if tone == "flexible" else 0.4,
            )

            contents = [system_prompt, user_message]

            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )

            raw_text = response.text.strip()

            # Clean possible markdown code fences
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.lower().startswith("json"):
                    raw_text = raw_text[4:].strip()

            data = json.loads(raw_text)

            # Basic validation + normalization
            try:
                versions = _validate_and_normalize_versions(data, num_versions)
            except ValueError as ve:
                if "AI failed to produce any valid cuts" in str(ve):
                    print(
                        "[AI Journalist] Falling back to a simple greedy cut because the model did not produce usable output."
                    )
                    versions = _create_simple_fallback_cut(
                        normalized_segments, max_duration_seconds, num_versions
                    )
                else:
                    raise

        except Exception as e:
            print(f"[AI Journalist] Gemini call failed: {e}")
            if "raw_text" in locals():
                print("[AI Journalist] Raw response was:")
                print(raw_text[:3000] if "raw_text" in locals() else "")
            raise RuntimeError(f"Failed to generate journalist cuts: {e}") from e

    # ------------------------------------------------------------------
    # FINAL SAFETY NET FOR REWRITE (works for both two-stage and any fallback)
    # ------------------------------------------------------------------
    if tone == "rewrite" and versions:
        chrono_versions = [
            v for v in versions if _segments_are_chronological(v.get("selected_segments", []))
        ]
        if chrono_versions:
            print(
                f"[AI Journalist] REWRITE WARNING: {len(chrono_versions)}/{len(versions)} version(s) still chronological after scriptwriter pipeline. Final aggressive retry..."
            )

            bag_text = _segments_to_bag_of_moments(normalized_segments)
            retry_system = (
                "You are a verbatim scriptwriter who has already failed once. "
                "The versions below are still basically in the original interview order. "
                "This is unacceptable. Rebuild a genuinely new story structure from the raw verbatim material only."
            )
            retry_user = f"""
Previous attempt failed to reorder. Here is all available verbatim material (random order):

{bag_text}

You MUST return {num_versions} versions in STRUCTURALLY NON-CHRONOLOGICAL order that tells a new story.
Return ONLY the JSON array in the exact same format.
"""

            try:
                retry_response = client.models.generate_content(
                    model=model_name,
                    contents=[retry_system, retry_user],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.95,
                    ),
                )
                retry_raw = retry_response.text.strip()
                if retry_raw.startswith("```"):
                    retry_raw = retry_raw.split("```")[1]
                    if retry_raw.lower().startswith("json"):
                        retry_raw = retry_raw[4:].strip()
                retry_data = json.loads(retry_raw)
                retry_versions = _validate_and_normalize_versions(retry_data, num_versions)

                if retry_versions:
                    improved = [
                        v
                        for v in retry_versions
                        if not _segments_are_chronological(v.get("selected_segments", []))
                    ]
                    if improved:
                        print(
                            f"[AI Journalist] FINAL REWRITE RETRY SUCCESS: {len(improved)} version(s) now reordered."
                        )
                        versions = retry_versions
            except Exception as retry_ex:
                print(f"[AI Journalist] Final rewrite retry failed (non-fatal): {retry_ex}")

    # Post-process for clean filler-free sentences if requested (applies to all tones)
    if clean_fillers:
        for version in versions:
            for seg in version.get("selected_segments", []):
                original_text = seg.get("text", "")
                cleaned = _remove_filler_words(original_text)
                if cleaned and cleaned != original_text:
                    seg["text"] = cleaned
                    seg["reason"] = (seg.get("reason", "") + " (fillers removed)").strip()

    # === Hard safety net: enforce max_duration_seconds ===
    # The model (especially in Verbatim Scriptwriter mode) often ignores the limit.
    # We allow a small tolerance (+10s) because a strong ending is usually more
    # valuable than cutting exactly at the requested number.
    TOLERANCE = 10.0
    for v in versions:
        segs = v.get("selected_segments", [])
        kept = []
        total = 0.0
        for seg in segs:
            s = seg.get("source_in") or seg.get("start", 0)
            e = seg.get("source_out") or seg.get("end", 0)
            try:
                dur = float(e) - float(s)
            except (TypeError, ValueError):
                continue
            if dur < 0.1:
                continue  # skip near-zero junk segments
            if total + dur > max_duration_seconds + TOLERANCE:
                break
            kept.append(seg)
            total += dur
        v["selected_segments"] = kept
        v["total_duration"] = round(total, 1)

        if total > max_duration_seconds:
            print(
                f"[AI Journalist] Note: Version {v.get('version_id')} ended up {total - max_duration_seconds:.1f}s over the requested limit (allowed tolerance: +{TOLERANCE}s)."
            )

        if min_duration_seconds > 0 and total < min_duration_seconds:
            print(
                f"[AI Journalist] Warning: Version {v.get('version_id')} is only {total:.1f}s — below the requested minimum of {min_duration_seconds}s."
            )

    # Attach diagnostic flag for UI (mainly useful for rewrite)
    for v in versions:
        segs = v.get("selected_segments", [])
        is_chrono = _segments_are_chronological(segs)
        v["_is_chronological"] = is_chrono
        if tone == "rewrite":
            v["_reorder_note"] = (
                "⚠️ Still mostly chronological (AI ignored reordering instructions)"
                if is_chrono
                else "✓ Non-linear / re-structured narrative"
            )
            v["_is_verbatim_scriptwriter"] = True
        else:
            v["_is_verbatim_scriptwriter"] = False

    # Attach language for narration TTS consistency (for both normal and rewrite paths)
    for v in versions:
        if narration_style or generate_narration or v.get("narration_text"):
            v["narration_language"] = material_language or v.get("narration_language", "en")
            if narration_style:
                v["narration_style"] = narration_style

    return versions


# ---------------------------------------------------------------------------
# EXPERIMENTAL: Audio-based AI Journalist Cutter
# ---------------------------------------------------------------------------


def generate_journalist_cuts_from_audio(
    audio_path: str | Path,
    max_duration_seconds: float,
    *,
    min_duration_seconds: float = 0.0,
    purpose: str = "News Package",
    tone: str = "rewrite",  # Recommended: "rewrite" / Verbatim Scriptwriter
    num_versions: int = 2,
    model_name: str = DEFAULT_GEMINI_MODEL,
    api_key: str | None = None,
    generate_narration: bool = False,
    narration_style: str | None = None,
) -> list[dict[str, Any]]:
    """
    EXPERIMENTAL PATH — Send the raw audio file to Gemini instead of (or in
    addition to) a text transcript.

    This allows the model to "listen" to tone, emotion, pacing, pauses,
    emphasis, laughter, etc., which is often lost in transcription.

    Currently this is a direct experiment. It returns the same structure as
    the text-based cutter so it can be dropped into the existing UI/export
    pipeline later if results are good.

    Note: For long interviews this can become expensive and may hit context
    limits. We may need to add chunking later.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if model_name not in GEMINI_MODELS:
        print(
            f"[AI Journalist Audio] Warning: Model '{model_name}' not in supported list. Falling back."
        )
        model_name = DEFAULT_GEMINI_MODEL

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise RuntimeError(
            "AI Journalist (audio) requires 'google-genai'. Please run: uv pip install google-genai"
        ) from e

    # Resolve API key
    if not api_key:
        from minicat.core.settings import get_gemini_api_key

        api_key = get_gemini_api_key()

    if not api_key or not api_key.strip():
        raise ValueError("Gemini API key is required for AI cutting.")

    client = genai.Client(api_key=api_key.strip())

    # Normalize for back-compat
    if narration_style is None and generate_narration:
        narration_style = "omniscient"

    # Read audio
    audio_bytes = audio_path.read_bytes()

    # Determine mime type (Gemini supports several)
    suffix = audio_path.suffix.lower()
    if suffix in (".mp3", ".mpeg"):
        mime_type = "audio/mpeg"
    elif suffix in (".wav", ".wave"):
        mime_type = "audio/wav"
    elif suffix in (".m4a", ".mp4"):
        # Production transcription proxy (.m4a). "audio/mp4" works reliably with Gemini for AAC-in-m4a.
        mime_type = "audio/mp4"
    elif suffix == ".aac":
        mime_type = "audio/aac"
    elif suffix == ".ogg":
        mime_type = "audio/ogg"
    else:
        mime_type = "audio/mpeg"  # reasonable default

    audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)

    # Strong prompt for audio-based cutting (especially good for Verbatim Scriptwriter)
    system_prompt = f"""
You are an experienced professional scriptwriter and narrative editor working on documentary, prestige non-fiction, or high-end interview content.

You have been given the RAW AUDIO of a full interview. Your job is to listen carefully (not just read a transcript) and create {num_versions} different, high-quality short narrative versions.

Key advantages you have because you can hear the audio:
- Detect emotional weight, pauses, hesitation, emphasis, laughter, tension, and vocal performance.
- Identify the most powerful, surprising, or human moments that don't always come across in text.
- Make more cinematic and emotionally intelligent cutting decisions.

Rules:
- You must return cuts using the actual spoken words from the audio (never invent dialogue).
- Each version must have a total runtime between {min_duration_seconds} and {max_duration_seconds} seconds.
- You are especially encouraged to create non-linear, re-structured stories (especially when tone="rewrite").
- Return the exact same JSON structure used by the text-based journalist cutter.

For each version return:
{{
  "version_id": "A",
  "title": "Short descriptive title",
  "total_duration": 178.4,
  "narrative_summary": "One or two sentences explaining the editorial thinking.",
  "selected_segments": [
    {{
      "source_in": 12.45,
      "source_out": 28.9,
      "text": "Exact words spoken in the audio",
      "reason": "Why this moment was chosen (consider tone, emotion, revelation, etc.)"
    }}
  ],
  "narration_text": "(optional, only if generate_narration or narration_style) The full voiceover narration script..."
}}

Return ONLY a JSON array containing {num_versions} version objects. Include "narration_text" if requested.
"""

    # Support narration for audio path too (for completeness with new options)
    if generate_narration or narration_style:
        style_note = f" using {narration_style} style" if narration_style else ""
        system_prompt += f"""
NARRATION / VOICEOVER SCRIPT (when requested{style_note}):
Also return "narration_text" : the full connecting voiceover script in the material's language. For TTS as single Narration.wav .
"""

    user_message = f"""
Here is the raw audio interview.

Please listen to it and create {num_versions} different cutting suggestions.
Each suggestion must have a total runtime between {min_duration_seconds} and {max_duration_seconds} seconds.

Purpose of this cut: {purpose}
Tone: {tone}

Focus on what actually sounds powerful, emotional, surprising, or structurally useful when you hear it — not just what reads well on paper.
"""

    try:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.85 if tone == "rewrite" else 0.7,
        )

        response = client.models.generate_content(
            model=model_name,
            contents=[system_prompt, user_message, audio_part],
            config=config,
        )

        raw_text = response.text.strip()

        # Clean markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.lower().startswith("json"):
                raw_text = raw_text[4:].strip()

        data = json.loads(raw_text)

        versions = _validate_and_normalize_versions(data, num_versions)

        # Apply the same duration safety net we use in the text path
        for v in versions:
            segs = v.get("selected_segments", [])
            kept = []
            total = 0.0
            for seg in segs:
                s = seg.get("source_in") or seg.get("start", 0)
                e = seg.get("source_out") or seg.get("end", 0)
                try:
                    dur = float(e) - float(s)
                except (TypeError, ValueError):
                    continue
                if dur < 0.1:
                    continue
                if total + dur > max_duration_seconds + 10:
                    break
                kept.append(seg)
                total += dur
            v["selected_segments"] = kept
            v["total_duration"] = round(total, 1)

        print(
            f"[AI Journalist Audio] Successfully generated {len(versions)} version(s) from raw audio."
        )

        # Attach lang/style for VO if requested
        for v in versions:
            if narration_style or generate_narration or v.get("narration_text"):
                v["narration_language"] = "en"  # audio path may not know lang; user can override
                if narration_style:
                    v["narration_style"] = narration_style

        return versions

    except Exception as e:
        print(f"[AI Journalist Audio] Gemini audio call failed: {e}")
        if "raw_text" in locals():
            print("[AI Journalist Audio] Raw response was:")
            print(raw_text[:3000] if "raw_text" in locals() else "")
        raise RuntimeError(f"Failed to generate journalist cuts from audio: {e}") from e


def _normalize_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert various timestamp formats into clean float seconds + text."""
    normalized = []
    for seg in segments:
        text = seg.get("text", "").strip()
        if not text:
            continue

        start = seg.get("start")
        end = seg.get("end")

        # Handle both "00:01:23.450" strings and float seconds
        try:
            if isinstance(start, str):
                start = _timestamp_to_seconds(start)
            if isinstance(end, str):
                end = _timestamp_to_seconds(end)
        except Exception:
            continue

        if start is None or end is None:
            continue

        normalized.append(
            {
                "start": float(start),
                "end": float(end),
                "text": text,
            }
        )

    return normalized


def _timestamp_to_seconds(ts: str) -> float:
    """Convert HH:MM:SS.mmm or MM:SS.mmm into seconds."""
    parts = ts.replace(",", ".").split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(parts[0])


def _segments_to_readable_transcript(segments: list[dict[str, Any]]) -> str:
    """Create a human-readable transcript with approximate timestamps (chronological)."""
    lines = []
    for seg in segments:
        start_str = _seconds_to_hms(seg["start"])
        end_str = _seconds_to_hms(seg["end"])
        lines.append(f"[{start_str} → {end_str}] {seg['text']}")
    return "\n".join(lines)


def _segments_to_bag_of_moments(segments: list[dict[str, Any]]) -> str:
    """
    Present segments as an unordered collection of available moments for rewrite mode.
    This removes the strong chronological bias that LLMs have when given a 'transcript'.
    Each moment gets a stable ID (M1, M2, ...) that the model can reference.
    """
    lines = []
    for i, seg in enumerate(segments, 1):
        start_str = _seconds_to_hms(seg["start"])
        end_str = _seconds_to_hms(seg["end"])
        dur = seg["end"] - seg["start"]
        lines.append(f"[M{i}] ({start_str}–{end_str}, {dur:.1f}s) {seg['text']}")
    return "\n".join(lines)


def _segments_are_chronological(segments: list[dict[str, Any]]) -> bool:
    """
    Return True if the source_in times are non-decreasing (i.e. still in original order).
    Used to detect when the rewrite tone failed to produce a re-structured narrative.
    """
    if not segments:
        return True
    prev = -1.0
    for seg in segments:
        t = seg.get("source_in") or seg.get("start")
        if t is None:
            continue
        try:
            t = float(t)
        except (TypeError, ValueError):
            continue
        if t < prev:
            return False
        prev = t
    return True


def _seconds_to_hms(seconds: float) -> str:
    """Convert seconds to HH:MM:SS format (no milliseconds for readability)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def _create_simple_fallback_cut(
    normalized_segments: list[dict[str, Any]],
    max_duration_seconds: float,
    num_versions: int,
) -> list[dict[str, Any]]:
    """
    Very simple fallback when Gemini completely fails to produce structured output.
    Creates one or more basic versions by taking segments from the start of the interview.
    """
    if not normalized_segments:
        return []

    versions = []
    for i in range(min(num_versions, 2)):  # at most 2 fallback versions
        selected = []
        total = 0.0
        for seg in normalized_segments:
            dur = seg["end"] - seg["start"]
            if total + dur > max_duration_seconds * 0.95:
                break
            selected.append(
                {
                    "source_in": round(seg["start"], 2),
                    "source_out": round(seg["end"], 2),
                    "text": seg["text"],
                    "reason": "Fallback: early segment from the interview",
                }
            )
            total += dur

        if not selected:
            continue

        version_id = chr(65 + i)  # A, B, ...
        versions.append(
            {
                "version_id": version_id,
                "title": f"Simple Early Cut {version_id}",
                "total_duration": round(total, 1),
                "narrative_summary": "Automatic fallback cut (Gemini did not return usable structured output).",
                "selected_segments": selected,
            }
        )

    return versions


def _build_journalist_system_prompt(
    max_duration_seconds: float,
    purpose: str,
    tone: str,
    num_versions: int,
    clean_fillers: bool = False,
    min_duration_seconds: float = 0.0,
    *,
    has_audio: bool = False,
    generate_narration: bool = False,
    narration_style: str | None = None,
    material_language: str = "en",
    narration_min_seconds: float = 0.0,
    narration_max_seconds: float = 0.0,
    narration_min_bridges: int = 0,
    narration_max_bridges: int = 0,
) -> str:
    """
    Construct a very strong, opinionated system prompt.

    When has_audio=True, the prompt tells the model that it also received the
    raw media and should use listening (tone, emotion, delivery, pauses, etc.)
    to make better editorial choices, while still using the provided transcript
    for exact source_in/source_out and verbatim text.
    """
    tone_block = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["newsroom"])
    guidance = PURPOSE_GUIDANCE.get(
        purpose, "Create a coherent, editorially strong short version of the interview."
    )

    narrative_order_instruction = (
        "SCRIPTWRITER MODE (REWRITE TONE): You are building a new story architecture. "
        "The selected_segments you output MUST form a deliberately re-structured narrative that would feel substantially different from the original interview if watched in your chosen order. "
        "You are REQUIRED to break the original chronology in a structural, noticeable way. "
        "Use cinematic techniques available to you with verbatim material: non-linear revelation, thematic juxtaposition, starting in the middle or end, callbacks, contrast between moments, delayed context, etc. "
        "Returning segments in roughly the same order as they were spoken is a failure of this task."
        if tone == "rewrite"
        else "The selected segments should generally be played in the order they appear in the original interview "
        "to preserve natural speech flow and journalistic integrity. Only reorder if it significantly "
        "improves the story without confusing the viewer."
    )

    filler_instruction = ""
    if clean_fillers:
        filler_instruction = """
FILLER WORDS & CLEAN SPEECH (IMPORTANT):
- Prefer segments that form complete, natural sentences.
- Pure noise ("um", "uh", "öö", "aa") should be avoided when possible.
- Discourse markers such as "like", "you know", "I mean", "tota", "niinku" are acceptable **if they help the sentence flow or are part of natural speech**. 
  Do not reject a strong, meaningful segment just because it contains one or two such words.
- The post-processing step will intelligently remove only the fillers that are clearly unnecessary for the sentence to remain complete and natural.
- Goal: clean and professional language **without** damaging the authenticity or completeness of the spoken thought.
"""

    audio_listening_instruction = ""
    if has_audio:
        audio_listening_instruction = """
AUDIO + TRANSCRIPT (MULTIMODAL):
You have received BOTH the precise timed transcript AND the raw original audio (or video) file.
- Use the transcript for exact wording and the source_in / source_out timestamps (these must be taken directly from the transcript you were given — never guess or invent times).
- Use the audio to judge real emotional weight, vocal emphasis, pauses, hesitation, energy, tone shifts, laughter, tension, and delivery quality that the text alone cannot convey.
- Prioritize moments that *sound* powerful, surprising, moving, or structurally useful when heard, not just when read.
- Your "reason" fields should reflect what you heard (e.g. "quiet, emotional delivery that lands the key admission", "strong rising emphasis on the final phrase", "long thoughtful pause before the revelation").
"""

    narration_instruction = ""
    style = (
        (str(narration_style).strip().lower() if narration_style else None)
        if (generate_narration or narration_style)
        else None
    )
    if style or generate_narration:
        style_instruction = ""
        if style == "omniscient":
            style_instruction = """
NARRATION STYLE — OMNISCIENT (third-person journalistic):
- Write the narration script in an objective, authoritative, third-person voice (e.g. "The team later discovered...", "What emerged was...").
- The narrator acts as a trusted journalistic guide: providing context, bridging gaps, surfacing contradictions, without taking a personal side.
- Tone like high-quality newsroom or prestige documentary voice-over.
"""
        elif style == "subjective":
            style_instruction = """
NARRATION STYLE — SUBJECTIVE (first-person reflective / essay-film):
- Write in a first-person ("I/We") or deeply personal reflective voice, as if the filmmaker or participant is speaking directly.
- Add emotional shading, subtext, doubt, or personal connection. Feels like internal monologue or essay-film voice.
"""
        elif style == "explainer":
            style_instruction = """
NARRATION STYLE — EXPLAINER (high-energy short-form / social media hook):
- Write in a direct, energetic, snappy, YouTube/TikTok explainer or hype voice.
- Short punchy sentences, questions, clear setup→payoff. Optimized for immediate attention and engagement.
"""
        else:
            style_instruction = """
NARRATION STYLE — (default journalistic):
- Write an elegant, sparing, purposeful connecting narration script in the source language.
- Short bridges that add emotion, context, transition or thematic glue.
"""
        budget_extra = ""
        if (
            narration_min_seconds
            or narration_max_seconds
            or narration_min_bridges
            or narration_max_bridges
        ):
            budget_extra = "\nNARRATION LENGTH & STRUCTURE (user settings):\n"
            if narration_min_seconds or narration_max_seconds:
                mins = narration_min_seconds or 5
                maxs = narration_max_seconds or 90
                budget_extra += f"- Total spoken duration of the entire narration_text should be in the {mins:.0f}–{maxs:.0f} seconds range (plan text volume for ~2.5 words per second natural speech).\n"
            if narration_min_bridges or narration_max_bridges:
                minb = narration_min_bridges or 1
                maxb = narration_max_bridges or 5
                budget_extra += f"- The script should contain roughly {minb} to {maxb} distinct conceptual bridge sections/passages that connect the spoken beats.\n"

        narration_instruction = f"""
NARRATION / VOICEOVER SCRIPT (when narration requested via generate_narration or narration_style):
In addition to the selected spoken segments, also return a top-level "narration_text" string on the version.
This is the complete voiceover narration script for the cut — the connecting narration that ties the chosen verbatim spoken moments into one smooth, purposeful short story.
- Write in the exact source language of the material: {material_language}. Never English unless the clips are in English.
- {style_instruction}
- Make it elegant, sparing, and purposeful: short bridges (ideally 1 sentence, at most 2) placed conceptually between the spoken parts where they add emotion, context, transition, irony, thematic glue, or time/place signals.
- The narration_text is the full script that will later be rendered via TTS as a single "Narration.wav" for this cut (no per-bridge files for single-clip Journalist).
- Do not make the narration dominate — it exists to serve and enrich the real spoken interview material.
- In narrative_summary, you may briefly mention how the narration helps the story.
{budget_extra}
"""

    prompt = f"""
You are an experienced professional journalist and video editor working in a newsroom or documentary unit.

{tone_block}

TASK:
Create {num_versions} DIFFERENT, high-quality cutting suggestions for this interview.

CONSTRAINTS (very important):
- Total runtime of each version MUST be between {min_duration_seconds} and {max_duration_seconds} seconds (inclusive).
- You must select actual spoken segments from the provided material (do not invent text).
- Each selected segment should have a clear editorial reason.

{audio_listening_instruction}
{filler_instruction}

NARRATIVE ORDER:
- {narrative_order_instruction}
- Avoid chopping mid-sentence unless the break is clearly justified.

{narration_instruction}

PURPOSE OF THIS CUT:
{guidance}

For each version you propose, return a JSON object with this exact structure:

{{
  "version_id": "A",                    // A, B, C, ...
  "title": "Short, descriptive title for this cut (max 12 words)",
  "total_duration": 87.4,               // accurate sum of selected segments
  "narrative_summary": "One or two sentences explaining the editorial thinking behind this version.",
  "selected_segments": [
    {{
      "source_in": 12.45,               // float seconds from original transcript (source time)
      "source_out": 28.9,
      "text": "Exact text from the transcript",
      "reason": "Brief editorial justification (max 18 words)",
      "moment_id": "M3"                 // (optional but strongly recommended) the [Mxx] this is based on, for exact time attachment
    }}
  ],
  "narration_text": "(optional, only if generate_narration or narration_style) The full voiceover narration script in {material_language} that connects the spoken parts into a story. Style-aware if narration_style was provided."
}}

Return ONLY a JSON array containing {num_versions} version objects. Nothing else. No markdown, no explanations.

CRITICAL: You MUST use exactly the keys "source_in" and "source_out" (not "start"/"end") inside selected_segments, as shown in the example.

Example output format:
[
  {{ "version_id": "A", "title": "...", "selected_segments": [ {{"source_in": 12.3, "source_out": 45.6, "moment_id": "M3", ...}}, ... ] }},
  {{ "version_id": "B", "title": "...", ... }}
]
"""
    return prompt.strip()


# ---------------------------------------------------------------------------
# Filler word cleaning (for "clean sentences" mode)
# ---------------------------------------------------------------------------

FILLERS_EN = {
    "um",
    "uh",
    "er",
    "ah",
    "hmm",
    "mm",
    "like",
    "you know",
    "i mean",
    "sort of",
    "kinda",
    "kind of",
    "basically",
    "actually",
    "so",
    "well",
    "right",
    "okay",
    "you see",
    "i guess",
    "i suppose",
    "you know what i mean",
}

FILLERS_FI = {
    "öö",
    "aa",
    "äh",
    "hmm",
    "mm",
    "niinku",
    "tota",
    "siis",
    "no",
    "joo",
    "emmätiiä",
    "silleen",
    "niin",
    "kyl",
    "kyllä",
    "oikeestaan",
    "periaattees",
}


def _remove_filler_words(text: str, language_hint: str | None = None) -> str:
    """
    Remove filler words intelligently so that the resulting sentence remains
    complete, natural, and semantically intact.

    Core rule (per user request):
    - Never destroy or "miss" the sentence just because it contained fillers.
    - Remove pure noise (öö, um, etc.) and unnecessary discourse markers (niinku, tota, etc.)
      when they can be taken out without losing meaning or grammar.
    - If in doubt, prefer keeping a slightly less clean sentence over producing
      a broken or incomplete one.

    Example of desired behavior:
    "mulla on öö niinku kaksikymmentä kissaa" → "mulla on kaksikymmentä kissaa"
    """
    if not text or not text.strip():
        return text

    import re

    original = text.strip()
    lowered = " " + original.lower() + " "

    pure_noise = {"um", "uh", "er", "ah", "hmm", "mm", "öö", "aa", "äh", "emmätiiä"}

    # Discourse markers that are often safe to remove in spoken Finnish/English
    # when they don't carry essential meaning.
    discourse_markers = {
        "like",
        "you know",
        "i mean",
        "sort of",
        "kinda",
        "kind of",
        "basically",
        "actually",
        "so",
        "well",
        "right",
        "okay",
        "you see",
        "i guess",
        "i suppose",
        "niinku",
        "tota",
        "siis",
        "no",
        "joo",
        "silleen",
        "oikeestaan",
        "periaattees",
    }

    fillers_to_consider = pure_noise | discourse_markers
    if language_hint:
        lang = language_hint.lower()
        if lang.startswith("fi"):
            fillers_to_consider = fillers_to_consider | FILLERS_FI
        elif lang.startswith("en"):
            fillers_to_consider = fillers_to_consider | FILLERS_EN

    cleaned = lowered

    # === Pass 1: Remove pure noise (very safe) ===
    for filler in sorted(pure_noise, key=len, reverse=True):
        pattern = r"\b" + re.escape(filler) + r"\b"
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    # === Pass 2: Remove discourse markers more carefully ===
    # We use broader patterns but with a strong post-clean quality gate.
    for filler in sorted(discourse_markers, key=len, reverse=True):
        patterns = [
            r",\s*" + re.escape(filler) + r"\s*,",
            r",\s*" + re.escape(filler) + r"\s+",
            r"\s+" + re.escape(filler) + r"\s*,",
            r"\s+" + re.escape(filler) + r"\s+",
            r"^\s*" + re.escape(filler) + r"\s+",  # at the very start
            r"\s+" + re.escape(filler) + r"\s*$",  # at the very end
        ]
        for p in patterns:
            cleaned = re.sub(p, " ", cleaned, flags=re.IGNORECASE)

    # Final cleanup
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s*,\s*,", ",", cleaned)
    cleaned = re.sub(r"^\s*[,.\-–—]\s*", "", cleaned)
    cleaned = re.sub(r"\s*[,.\-–—]\s*$", "", cleaned)

    # === Quality / Meaning preservation checks ===
    original_words = [w for w in original.split() if w]
    cleaned_words = [w for w in cleaned.split() if w]

    if not cleaned_words:
        return original

    original_word_count = len(original_words)
    cleaned_word_count = len(cleaned_words)

    # Never accept if we removed more than 35% of the words (stricter than before)
    removal_ratio = 1 - (cleaned_word_count / max(original_word_count, 1))
    if removal_ratio > 0.35:
        return original

    # Reject if the result is too short to be a real sentence
    if cleaned_word_count <= 2 and original_word_count > 4:
        return original

    # Reject obviously broken results
    if cleaned.startswith(",") or cleaned.startswith("."):
        return original

    # New stronger check: preserve core meaning
    # Extract "content words" (longer words that are not common fillers)
    def _content_words(s):
        return [w for w in re.findall(r"\b\w{4,}\b", s.lower()) if w not in fillers_to_consider]

    original_content = set(_content_words(original))
    cleaned_content = set(_content_words(cleaned))

    if original_content:
        preserved_ratio = len(cleaned_content & original_content) / len(original_content)
        # If we lost more than 30% of the meaningful content words, reject
        if preserved_ratio < 0.70:
            return original

    # Restore capitalization
    if original and original[0].isupper() and cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]

    if not cleaned or len(cleaned) < 2:
        return original

    return cleaned.strip()


def _validate_and_normalize_versions(data: Any, expected_count: int) -> list[dict[str, Any]]:
    """Validate Gemini output and normalize it to our expected shape."""
    if not isinstance(data, list):
        raise ValueError("AI did not return a list of versions.")

    versions = []
    for i, v in enumerate(data):
        if not isinstance(v, dict):
            continue

        version_id = v.get("version_id") or chr(65 + i)  # A, B, C...

        selected = v.get("selected_segments", [])
        if not isinstance(selected, list) or len(selected) == 0:
            continue

        clean_segments = []
        total = 0.0
        for seg in selected:
            try:
                # Support both the new recommended keys and legacy keys
                # (in case the model doesn't follow the prompt perfectly)
                start = float(seg.get("source_in") or seg.get("start", 0))
                end = float(seg.get("source_out") or seg.get("end", 0))
                text = str(seg.get("text", "")).strip()
                reason = str(seg.get("reason", "")).strip()

                if end <= start or not text:
                    continue

                # Convert to frame-accurate integers for the exporter (25 fps)
                start_frame = int(round(start * 25))
                end_frame = int(round(end * 25))

                clean_seg = {
                    "source_in": round(start, 2),
                    "source_out": round(end, 2),
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "text": text,
                    "reason": reason or "Selected by AI",
                }

                # Preserve any multi-source / cross-clip metadata the caller injected
                for k, val in seg.items():
                    if k.startswith("source_") and k not in clean_seg:
                        clean_seg[k] = val

                # If the AI itself returned a "source" field (we now instruct it to),
                # map it to source_label so the UI and exports can use it directly.
                if "source" in seg and "source_label" not in clean_seg:
                    clean_seg["source_label"] = str(seg["source"]).strip()

                # Preserve moment_id (from the [Mxx] the LLM chose) so the post-repair can attach
                # exact ground-truth timing from the original candidates list with zero guessing.
                if "moment_id" in seg and "moment_id" not in clean_seg:
                    clean_seg["moment_id"] = str(seg["moment_id"]).strip().upper()

                clean_segments.append(clean_seg)
                total += end - start
            except Exception:
                continue

        if not clean_segments:
            continue

        version_dict = {
            "version_id": version_id,
            "title": str(v.get("title", f"Version {version_id}")).strip(),
            "total_duration": round(total, 1),
            "narrative_summary": str(v.get("narrative_summary", "")).strip(),
            "selected_segments": clean_segments,
        }

        narration_text = str(v.get("narration_text", "")).strip()
        if narration_text:
            version_dict["narration_text"] = narration_text

        versions.append(version_dict)

    if not versions:
        # Helpful diagnostics for the common "Gemini returned something but nothing was valid" case
        print("[AI Journalist] Gemini returned data but no valid cuts survived validation.")
        print("[AI Journalist] Raw parsed data from model:")
        try:
            print(json.dumps(data, indent=2, ensure_ascii=False)[:3000])
        except Exception:
            print(data)

        raise ValueError(
            "AI failed to produce any valid cuts. "
            "Check the console log above — it contains the raw JSON Gemini returned. "
            "Common causes: max duration too short, or the model ignored the required key names."
        )

    return versions[:expected_count]


# ---------------------------------------------------------------------------
# Two-stage Verbatim Scriptwriter helpers (used only for tone == "rewrite")
# ---------------------------------------------------------------------------


def _generate_rewrite_versions_two_stage(
    *,
    client,
    model_name: str,
    normalized_segments: list[dict[str, Any]],
    max_duration_seconds: float,
    min_duration_seconds: float = 0.0,
    purpose: str,
    num_versions: int,
    clean_fillers: bool,
    generate_narration: bool = False,
    narration_style: str | None = None,
    material_language: str = "en",
) -> list[dict[str, Any]]:
    """
    Two-stage pipeline for Verbatim Scriptwriter mode (transcript-only).

    Stage 1: Mine the most dramatically useful verbatim moments (no ordering).
    Stage 2: Given only those moments, act as a scriptwriter and build new story
             architectures. Output must still be valid selected_segments so the
             existing XML exporter continues to work unchanged.

    No audio or video is sent to Gemini.
    """
    # Ensure google-genai types are available in this helper scope
    from google.genai import types

    # Transcript-only: no media is attached
    _stage1_media_part = None  # noqa: F841 (kept for future media support)

    # --- Stage 1: Mine powerful raw material (as scriptwriter, not editor) ---
    print("[AI Journalist] Stage 1: Mining powerful verbatim moments for story construction...")

    stage1_system = """
You are a scriptwriter preparing to build a short non-fiction film or journalistic feature from raw interview transcripts.

Your only job right now is to identify the best raw material.

Go through the entire transcript and select 12–20 of the most dramatically useful, emotionally charged, thematically rich, or narratively potent VERBATIM moments.

For each moment you select, output:
- source_in / source_out (exact seconds)
- text (exact spoken words, never edited)
- dramatic_potential: one short sentence explaining the *storytelling value* of this moment as building block for a new narrative (e.g. "Powerful emotional turning point", "Reveals hidden contradiction", "Perfect hook for the whole story", "Strong thematic callback candidate", "Creates great juxtaposition potential", "The key revelation the audience needs").

Return ONLY a JSON array of objects with those four keys. Do not order them or build any story yet.
"""

    stage1_user = f"""
Here is the full interview transcript with timestamps:

{_segments_to_readable_transcript(normalized_segments)}

Extract the strongest raw material for building a completely new short story. Focus on dramatic and thematic potential, not on chronological importance.
"""

    try:
        stage1_contents = [stage1_system, stage1_user]

        resp1 = client.models.generate_content(
            model=model_name,
            contents=stage1_contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.6,
            ),
        )
        raw1 = resp1.text.strip()
        if raw1.startswith("```"):
            raw1 = raw1.split("```")[1]
            if raw1.lower().startswith("json"):
                raw1 = raw1[4:].strip()
        candidates = json.loads(raw1)
    except Exception as e:
        print(
            f"[AI Journalist] Stage 1 mining failed ({e}). Falling back to normal single-pass rewrite."
        )
        # Fallback to old behavior (transcript-only)
        return _single_pass_rewrite_fallback(
            client=client,
            model_name=model_name,
            normalized_segments=normalized_segments,
            max_duration_seconds=max_duration_seconds,
            min_duration_seconds=min_duration_seconds,
            purpose=purpose,
            num_versions=num_versions,
            clean_fillers=clean_fillers,
            generate_narration=bool(narration_style),
            narration_style=narration_style,
            material_language=material_language,
            narration_min_seconds=narration_min_seconds,  # noqa: F821
            narration_max_seconds=narration_max_seconds,  # noqa: F821
            narration_min_bridges=narration_min_bridges,  # noqa: F821
            narration_max_bridges=narration_max_bridges,  # noqa: F821
        )

    # Keep only valid candidates with real timestamps
    valid_candidates = []
    for c in candidates if isinstance(candidates, list) else []:
        try:
            s = float(c.get("source_in") or c.get("start", 0))
            e = float(c.get("source_out") or c.get("end", 0))
            txt = (c.get("text") or "").strip()
            pot = (c.get("dramatic_potential") or "").strip()
            if e > s + 0.3 and txt:
                valid_candidates.append(
                    {
                        "source_in": round(s, 2),
                        "source_out": round(e, 2),
                        "text": txt,
                        "dramatic_potential": pot or "Strong moment",
                    }
                )
        except Exception:
            continue

    if len(valid_candidates) < 4:
        print("[AI Journalist] Stage 1 returned too few good moments. Using fallback.")
        return _single_pass_rewrite_fallback(
            client=client,
            model_name=model_name,
            normalized_segments=normalized_segments,
            max_duration_seconds=max_duration_seconds,
            min_duration_seconds=min_duration_seconds,
            purpose=purpose,
            num_versions=num_versions,
            clean_fillers=clean_fillers,
            generate_narration=bool(narration_style),
            narration_style=narration_style,
            material_language=material_language,
            narration_min_seconds=narration_min_seconds,  # noqa: F821
            narration_max_seconds=narration_max_seconds,  # noqa: F821
            narration_min_bridges=narration_min_bridges,  # noqa: F821
            narration_max_bridges=narration_max_bridges,  # noqa: F821
            # (source_media_path removed - journalist is transcript-only)
        )

    # Shuffle for Stage 2 so the architect is not biased by mining order
    random.shuffle(valid_candidates)
    candidate_text = "\n".join(
        f"[M{i + 1}] ({c['source_in']}s–{c['source_out']}s) {c['text']}\n   → Potential: {c['dramatic_potential']}"
        for i, c in enumerate(valid_candidates[:18])  # cap context
    )

    # --- Stage 2: Build new story architecture as scriptwriter ---
    print(
        f"[AI Journalist] Stage 2: Building new story architecture from {len(valid_candidates)} mined moments..."
    )

    stage2_system = f"""
You are a scriptwriter and narrative architect.

You have been given a collection of the strongest verbatim moments from an interview (with their storytelling potential noted).

Your job is to construct {num_versions} completely different short narrative versions using ONLY these exact spoken words.

Rules (journalistic safety):
- You may only use the verbatim text exactly as provided. Never invent, paraphrase, or add new spoken words.
- You are building a new story, not editing the original conversation.
- Use selection + order + juxtaposition as your only storytelling tools.

For each version you create:
- Aim for a total duration close to {max_duration_seconds} seconds. A small overrun of up to ~10 seconds is acceptable if it lets you keep a strong ending or key moment.
- Arrange them in a new, non-chronological order that serves a clear dramatic arc.
- Write a strong narrative_summary that explains the new story you are telling with this specific selection and order.
- For each selected segment, write a concise "reason" explaining its role in your new narrative.
"""
    if generate_narration or narration_style:
        style_note = ""
        if narration_style:
            style_note = f" (using {narration_style} perspective)"
        stage2_system += f"""
Additionally, because narration/voiceover generation was requested{style_note}, for each version also produce a top-level
"narration_text": "The full voiceover narration script (connecting text) for this re-structured cut, written elegantly and sparingly in the original source language {material_language}. The narration bridges the selected verbatim spoken moments into one cohesive short story. Keep bridges short (1-2 sentences). This will be rendered via TTS as a single Narration.wav for the cut."
"""
    stage2_system += f"""
Output exactly the normal journalist cut JSON format (version_id, title, total_duration, narrative_summary, selected_segments with source_in/source_out/text/reason). If narration was requested, also include "narration_text" on each version object.

For each item in selected_segments, if it is based on one of the provided [Mxx] moments, also include "moment_id": "M5" (or whichever) so the system can attach the exact original timing without any guessing.

Return ONLY the JSON array of {num_versions} versions.
"""

    stage2_user = f"""
Here are the best raw verbatim moments available (presented in random order, with notes on their storytelling value):

{candidate_text}

PURPOSE OF THIS CUT: {purpose}

Now build {num_versions} genuinely new story versions from this material only.
"""

    try:
        resp2 = client.models.generate_content(
            model=model_name,
            contents=[stage2_system, stage2_user],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.85,
            ),
        )
        raw2 = resp2.text.strip()
        if raw2.startswith("```"):
            raw2 = raw2.split("```")[1]
            if raw2.lower().startswith("json"):
                raw2 = raw2[4:].strip()
        data = json.loads(raw2)
        versions = _validate_and_normalize_versions(data, num_versions)

        # === Robust post-repair for two-stage rewrite ===
        # The LLM in Stage 2 is asked to build selected_segments using the source_in/out it was shown
        # next to each [Mxx] moment, but it frequently hallucinates, rounds wrongly, or invents tiny
        # windows (especially at temperature 0.85).
        # 1. If it included "moment_id": "M3", use that to attach the exact candidate timing 100% reliably.
        # 2. Otherwise fall back to text match on the emitted "text".
        # This guarantees the exported AI_Cut_*.txt SELECTED and the XML clipitem <in>/<out> use the
        # exact material the architect chose from the mined moments (matching the transcription .txt).
        if valid_candidates:
            cand_by_norm = {}
            cand_by_moment = {}
            for idx, c in enumerate(valid_candidates):
                norm = (c.get("text") or "").strip().lower()[:120]
                if norm:
                    cand_by_norm.setdefault(norm, c)
                # Map "M1", "M2", ... (1-based as shown to LLM)
                cand_by_moment[f"M{idx + 1}"] = c
                cand_by_moment[f"m{idx + 1}"] = c

            for ver in versions:
                for seg in ver.get("selected_segments", []):
                    attached = False
                    # Preferred: direct moment_id from LLM (no hallucination possible)
                    mid = (seg.get("moment_id") or seg.get("momentId") or "").strip().upper()
                    if mid and mid in cand_by_moment:
                        c = cand_by_moment[mid]
                        try:
                            if "source_in" in c and "source_out" in c:
                                real_in = float(c["source_in"])
                                real_out = float(c["source_out"])
                                if real_out > real_in + 0.05:
                                    seg["source_in"] = round(real_in, 2)
                                    seg["source_out"] = round(real_out, 2)
                                    attached = True
                        except Exception:
                            pass
                    if not attached:
                        # Fallback: text match (for older outputs or when moment_id not emitted)
                        norm = (seg.get("text") or "").strip().lower()[:120]
                        if norm in cand_by_norm:
                            c = cand_by_norm[norm]
                            try:
                                if "source_in" in c and "source_out" in c:
                                    real_in = float(c["source_in"])
                                    real_out = float(c["source_out"])
                                    if real_out > real_in + 0.05:
                                        seg["source_in"] = round(real_in, 2)
                                        seg["source_out"] = round(real_out, 2)
                            except Exception:
                                pass

        return versions
    except Exception as e:
        print(f"[AI Journalist] Stage 2 story architecture failed ({e}). Falling back.")
        return _single_pass_rewrite_fallback(
            client=client,
            model_name=model_name,
            normalized_segments=normalized_segments,
            max_duration_seconds=max_duration_seconds,
            min_duration_seconds=min_duration_seconds,
            purpose=purpose,
            num_versions=num_versions,
            clean_fillers=clean_fillers,
            generate_narration=bool(narration_style),
            narration_style=narration_style,
            material_language=material_language,
            narration_min_seconds=narration_min_seconds,  # noqa: F821
            narration_max_seconds=narration_max_seconds,  # noqa: F821
            narration_min_bridges=narration_min_bridges,  # noqa: F821
            narration_max_bridges=narration_max_bridges,  # noqa: F821
            # (source_media_path removed - journalist is transcript-only)
        )


def _single_pass_rewrite_fallback(
    *,
    client,
    model_name: str,
    normalized_segments: list[dict[str, Any]],
    max_duration_seconds: float,
    min_duration_seconds: float = 0.0,
    purpose: str,
    num_versions: int,
    clean_fillers: bool,
    generate_narration: bool = False,
    narration_style: str | None = None,
    material_language: str = "en",
    narration_min_seconds: float = 0.0,
    narration_max_seconds: float = 0.0,
    narration_min_bridges: int = 0,
    narration_max_bridges: int = 0,
) -> list[dict[str, Any]]:
    """Fallback single-pass rewrite when the two-stage pipeline fails (transcript-only)."""
    # Ensure google-genai types are available in this helper scope
    from google.genai import types

    shuffled = list(normalized_segments)
    random.shuffle(shuffled)
    transcript_text = _segments_to_bag_of_moments(shuffled)

    system = _build_journalist_system_prompt(
        max_duration_seconds=max_duration_seconds,
        purpose=purpose,
        tone="rewrite",
        num_versions=num_versions,
        clean_fillers=clean_fillers,
        min_duration_seconds=min_duration_seconds,
        has_audio=False,
        generate_narration=bool(narration_style),
        narration_style=narration_style,
        material_language=material_language,
        narration_min_seconds=narration_min_seconds,
        narration_max_seconds=narration_max_seconds,
        narration_min_bridges=narration_min_bridges,
        narration_max_bridges=narration_max_bridges,
    )
    user = f"""
Here is the available interview material (random order):

{transcript_text}

Build {num_versions} new story versions from this verbatim material only.
"""

    try:
        fb_contents = [system, user]

        resp = client.models.generate_content(
            model=model_name,
            contents=fb_contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.9,
            ),
        )
        raw = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        data = json.loads(raw)
        return _validate_and_normalize_versions(data, num_versions)
    except Exception:
        return _create_simple_fallback_cut(normalized_segments, max_duration_seconds, num_versions)


def _enforce_rewrite_reordering_if_needed(
    *,
    client,
    model_name: str,
    versions: list[dict[str, Any]],
    normalized_segments: list[dict[str, Any]],
    max_duration_seconds: float,
    num_versions: int,
    transcript_text_for_retry: str,
) -> list[dict[str, Any]]:
    """Legacy safety net kept for compatibility."""
    # This function is called after the two-stage, so we keep it lightweight.
    # The main enforcement now happens inside _generate_rewrite_versions_two_stage + final retry.
    return versions
