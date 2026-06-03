"""
Narrative + Voiceover XMEML Exporter for AI Director (with narrations/voiceovers).

This is exporter #3:

3. Multi - AI Director + narrations/voiceovers → narrative_vo_exporter.py (this file)

It is responsible for AI Director multi-source exports **when narration was generated**.

It adds the Director-specific features on top of multi-source XMEML:
- Narration bridges as visible on-screen <title> elements
- Generated voiceover bridges (WAV stereo 44100Hz for local Piper, MP3 for Google) on a dedicated audio track
- Full narrative_sequence support (interleaved clips + narration)
- Strong zero timecode policy enforcement

We have three distinct exporters:

1. Single                                   → xmeml_exporter.py
2. Multi - AI Director (no narration)       → multi_xmeml_exporter.py
3. Multi - AI Director + narrations/voiceovers → narrative_vo_exporter.py (this file)

This file (#3) is responsible for the most complex case: AI Director narrative versions that may include:
- Narration bridges rendered as on-screen titles
- Generated voiceover audio on a dedicated track
- Full multi-source handling with the Director's strict zero timecode policy
"""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from xml.dom import minidom
from typing import Any

# Re-export some helpers that the exporter needs
from minicat.ai.director import get_narrative_sequence
from minicat.ai.xmeml_exporter import (
    get_media_start_offset_and_duration,
    get_video_timebase,
    _seconds_to_frames,
)
from minicat.ai.voiceover import (
    generate_narration_audio_sync,
    get_tts_status,
    ensure_tts_ready_for_narration,
    get_tts_provider,
)

# Shared multi-source helpers (exporter #2 foundation)
from minicat.ai.multi_xmeml_exporter import (
    prepare_director_sources,
    _get_premiere_label_color,
    _build_mixed_sources_xmeml,
    get_audio_characteristics,
)

# ---------------------------------------------------------------------------
# Main public function (will eventually replace the one in app.py)
# ---------------------------------------------------------------------------

from pathlib import Path
from typing import Any

# This will eventually contain the full extracted and cleaned logic
# from the previous nested export_multi_xml in app.py.

# For now we keep a thin compatibility layer during migration.


def export_narrative_vo_xmeml(
    ver: dict[str, Any],
    *,
    generate_voiceover: bool = True,
    voiceover_language: str = "en",
    narration_as_titles: bool = False,
    voiceover_voice: str | None = None,
    pregenerated_vo_files: list[dict] | None = None,
    output_dir: Path | None = None,
) -> Path | None:
    """
    Export an AI Director multi-source narrative version as XMEML.

    This is exporter #3:
    "Multi - AI Director + narrations/voiceovers"

    Owns:
    - Voiceover audio generation for narration bridges (all now 44100Hz stereo 16-bit PCM WAV to match Narration.wav format, regardless of provider) (when generate_voiceover=True)
      • Uses the provider chosen in Settings (Local Piper by default, or Google Cloud)
        is selected but not configured (missing package or no ADC credentials).
    - Narration-as-titles preparation (when narration_as_titles=True)
    - Full multi-source XMEML with optional extra voiceover audio track + titles track
    - Strict zero timecode policy on all picture sources
    - Optional pregenerated_vo_files (for UI progress/stepwise generation + real audio specs)
    - The rich "AI DIRECTOR — MULTI-CLIP SCRIPT.txt" (full attributed cut + narration) is also written into the export subfolder by the UI layer for voiceover exports.

    This is now the fully self-contained implementation for exporter #3.
    It composes on top of multi_xmeml_exporter for the picture base.
    """
    try:
        from pathlib import Path as _P

        # Best-effort sidecar repair for full verbatim spans in Director stories (loaded or passed in).
        try:
            from minicat.core.video import repair_director_version_with_transcripts
            from minicat.ui.app import get_state
            st = get_state()
            cat_root = getattr(st, "catalog_root", None) if st else None
            vids = getattr(st, "videos", None) or []
            if cat_root:
                ver = repair_director_version_with_transcripts(ver, cat_root, vids)
        except Exception:
            pass

        segs = ver.get("selected_segments", [])
        if not segs:
            return None

        source_paths: dict[str, str] = {}
        for seg in segs:
            p = seg.get("source_path")
            if p:
                source_paths[p] = seg.get("source_filename", _P(p).name)

        if not source_paths:
            return None

        if output_dir is not None:
            export_dir = Path(output_dir)
            export_dir.mkdir(parents=True, exist_ok=True)
        else:
            # Always create a new dated subfolder inside default library for this export's files
            from minicat.core.settings import create_export_subfolder
            suggestion = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:40] }"
            export_dir = create_export_subfolder(suggestion)

        raw_name = f"AI_Multi_{ver.get('version_id', 'X')}_{ (ver.get('title') or 'Cut')[:30] }"
        from minicat.core.settings import sanitize_for_filesystem
        name = sanitize_for_filesystem(raw_name, max_len=80)
        out_name = f"{name}.xml"
        out_path = export_dir / out_name

        # Always also export the rich AI Director multi-clip script TXT into the same subfolder.
        # This ensures the full attributed script is always alongside the XML (and voiceovers when present).
        try:
            from minicat.ui.app import export_ai_director_multi_clip_script
            export_ai_director_multi_clip_script(ver, target_dir=export_dir)
        except Exception:
            # UI layer may not be available in all contexts (tests, CLI); ignore
            pass

        # === Narration preparation (moved from the giant nested function) ===
        narrative_sequence = get_narrative_sequence(ver)
        narration_bridges = [item for item in narrative_sequence if item.get("type") == "narration"]

        if generate_voiceover and not narration_as_titles and narration_bridges:
            status = get_tts_status()
            print(f"[Narrative VO Exporter] TTS provider={status.get('provider')} status: {status.get('message')}")

        if pregenerated_vo_files is not None:
            vo_files = pregenerated_vo_files
        else:
            vo_files = _prepare_voiceover_bridges(
                ver=ver,
                name=name,
                export_dir=export_dir,
                generate_voiceover=generate_voiceover,
                narration_as_titles=narration_as_titles,
                voiceover_language=voiceover_language,
                voiceover_voice=voiceover_voice,
                narration_bridges=narration_bridges,
            )

        title_items = _prepare_narration_title_items(
            narration_as_titles=narration_as_titles,
            narrative_sequence=narrative_sequence,
        )

        source_list = list(source_paths.keys())

        if len(source_list) == 1:
            # Single-source + narration bridges: keep the high-quality single path
            # (voiceover audio files (WAV for Piper local, MP3 for Google) are already generated above; the XML itself stays
            # picture-only for now, matching previous behavior).
            cut_segments = []
            for seg in segs:
                cut_segments.append({
                    "source_in": seg.get("source_in") or seg.get("start"),
                    "source_out": seg.get("source_out") or seg.get("end"),
                    "text": seg.get("text", ""),
                    "reason": seg.get("reason", "")
                })

            detected_timebase = get_video_timebase(source_list[0])
            from minicat.ai.xmeml_exporter import generate_xmeml
            generate_xmeml(
                cut_segments=cut_segments,
                source_video_path=source_list[0],
                output_path=out_path,
                sequence_name=f"AI Multi Cut - {ver.get('title', ver.get('version_id'))}",
                narrative_summary=ver.get('narrative_summary'),
                timebase=detected_timebase,
            )

            if vo_files:
                print(f"[Narrative VO Exporter] Single-source: generated {len(vo_files)} voiceover file(s) (XML is picture-only)")
            return out_path

        else:
            # === MULTI-SOURCE + NARRATION/VOICEOVER (the real #3 case) ===
            ordered_sources, source_meta, tb = prepare_director_sources(ver, source_paths)

            pretty_xml = _build_narrative_vo_mixed_xmeml(
                ver=ver,
                ordered_sources=ordered_sources,
                source_meta=source_meta,
                source_paths=source_paths,
                timebase=tb,
                name=name,
                vo_files=vo_files,
                title_items=title_items,
            )

            if pretty_xml:
                out_path.write_text(pretty_xml, encoding="utf-8")

                if vo_files:
                    print(f"[Narrative VO Exporter] Exported multi-source with {len(vo_files)} voiceover bridge(s)")
                if title_items:
                    print(f"[Narrative VO Exporter] Exported with {len(title_items)} narration title(s)")

                return out_path

            # If we reach here the builder produced nothing usable — surface a clear failure
            raise RuntimeError("Narrative VO XMEML builder produced empty output for multi-source case")

    except Exception as ex:
        print(f"[Narrative VO Exporter #3] Preparation failed: {ex}")
        import traceback
        traceback.print_exc()
        return None


# ---------------------------------------------------------------------------
# Narration / Voiceover preparation helpers (extracted from the legacy)
# These are pure with respect to the AI Director version dict and are safe
# to call from either the exporter or the UI dialog.
# ---------------------------------------------------------------------------

def _prepare_voiceover_bridges(
    *,
    ver: dict[str, Any],
    name: str,
    export_dir: Path,
    generate_voiceover: bool,
    narration_as_titles: bool,
    voiceover_language: str,
    voiceover_voice: str | None,
    narration_bridges: list[dict],
) -> list[dict]:
    """
    Generate voiceover bridge files for the narration bridges (if requested).
    For local Piper: WAV (stereo 44100Hz) as requested.
    For Google: MP3.

    Audio files are named Narration_BridgeNN.wav (and Narration.wav for the full script)
    so they are clean and grouped when placed inside a per-export subfolder.

    Returns a list of {"path": Path, "text": str, "index": int, ...} for each generated bridge.
    Skips generation entirely when narration_as_titles=True (text titles only).
    """
    vo_files: list[dict] = []

    if not (generate_voiceover and not narration_as_titles and narration_bridges):
        return vo_files

    # Ensure the package for the active provider is present (auto-installs if needed)
    try:
        provider = get_tts_provider()
    except Exception:
        provider = "local"
    if provider == "local":
        from minicat.ai.voiceover import ensure_piper_package
        if not ensure_piper_package():
            print("[Narrative VO Exporter] Local (Piper) package not available (auto-install failed). "
                  "Voiceover generation will fail with a clear error.")
    else:
        from minicat.ai.voiceover import ensure_google_tts_package
        if not ensure_google_tts_package():
            print("[Narrative VO Exporter] Google TTS package not available (auto-install attempt failed). "
                  "Voiceover generation will fail with a clear error.")

    lang_to_use = voiceover_language or ver.get("narration_language") or "en"

    # All VO audio files (including Narration.wav and bridges) use identical format:
    # 44100Hz stereo 16-bit PCM WAV (to match exactly what was exported for the full Narration script)
    vo_ext = "wav"

    for i, bridge in enumerate(narration_bridges, 1):
        try:
            bridge_text = bridge.get("text", "").strip()
            if not bridge_text:
                continue

            vo_name = f"Narration_Bridge{i:02d}.{vo_ext}"
            vo_p = export_dir / vo_name

            # This will raise a clear error if the active TTS provider (local Piper or Google) is not ready.
            # Auto-install of the required package happens inside the provider status / generate functions.
            generate_narration_audio_sync(
                text=bridge_text,
                language=lang_to_use,
                output_path=vo_p,
                voice=voiceover_voice,
            )

            # Probe actual duration of the generated audio for accurate timeline placement
            try:
                _, vo_dur = get_media_start_offset_and_duration(vo_p)
                vo_duration = float(vo_dur) if vo_dur else 0.0
            except Exception:
                vo_duration = 0.0

            # Probe real technical audio details (sr, ch) so the XMEML <file> for this bridge
            # declares correct media characteristics.
            try:
                ainfo = get_audio_characteristics(vo_p)
            except Exception:
                ainfo = {"channels": 1, "sample_rate": 44100}

            vo_files.append({
                "path": vo_p,
                "text": bridge_text,
                "index": i,
                "duration": vo_duration,   # real duration from file
                "sample_rate": ainfo.get("sample_rate", 44100),
                "channels": ainfo.get("channels", 1),
            })
        except Exception as vo_ex:
            print(f"[Narrative VO Exporter] Bridge {i} generation failed: {vo_ex}")
            print("  → This bridge will be missing from the voiceover track (text titles may still be available).")

    if vo_files:
        print(f"[Narrative VO Exporter] Generated {len(vo_files)} voiceover bridge file(s)")

    return vo_files


def _prepare_narration_title_items(
    *,
    narration_as_titles: bool,
    narrative_sequence: list[dict],
) -> list[dict]:
    """
    Build timed title items for on-screen narration bridges.

    Each item: {"start": seconds, "duration": seconds, "text": str}
    Uses the same ~3.2 words-per-second heuristic as the legacy implementation.
    """
    title_items: list[dict] = []

    if not (narration_as_titles and narrative_sequence):
        return title_items

    current_t = 0.0
    for item in narrative_sequence:
        if item.get("type") == "clip":
            dur = (item.get("source_out", 0) - item.get("source_in", 0)) or 0
            current_t += max(0.0, dur)
        elif item.get("type") == "narration":
            bridge_text = item.get("text", "").strip()
            if bridge_text:
                words = len(bridge_text.split())
                est_dur = max(2.0, words / 3.2)
                title_items.append({
                    "start": current_t,
                    "duration": est_dur,
                    "text": bridge_text,
                })
                current_t += est_dur

    return title_items


# ---------------------------------------------------------------------------
# Internal builders (to be filled in the next migration slices)
# ---------------------------------------------------------------------------

def _build_narrative_vo_mixed_xmeml(
    ver: dict[str, Any],
    ordered_sources: list[str],
    source_meta: dict[str, dict],
    source_paths: dict[str, str],
    timebase: int,
    name: str,
    *,
    vo_files: list[dict],
    title_items: list[dict],
) -> str:
    """
    Build the full enhanced multi-source XMEML for exporter #3 (narration + voiceover).

    Strategy (clean composition, zero duplication of picture logic):
    1. Get the proven rich picture-only XMEML from exporter #2's builder.
    2. Parse it.
    3. If vo_files: add a 3rd "Voiceover" mono audio track + the bridge clipitems
       (placed after the picture content on the timeline).
    4. If title_items: add an extra video <title> track with the narration text.
    5. Re-serialize with correct duration patch.

    This keeps the per-source timebase math, zero-TC policy, label colors,
    exploded mono tracks, links, etc. in a single place (#2) while #3 only
    owns the narration-specific layering.
    """
    import xml.etree.ElementTree as ET
    from xml.dom import minidom

    # 1. Base picture XML from the no-narration exporter (authoritative source)
    base_xml = _build_mixed_sources_xmeml(
        ver=ver,
        ordered_sources=ordered_sources,
        source_meta=source_meta,
        source_paths=source_paths,
        timebase=timebase,
        name=name,
    )

    if not base_xml or not base_xml.strip():
        # Fallback safety (should never happen in normal operation)
        return base_xml

    root = ET.fromstring(base_xml)

    # Find the key structural elements we need to extend
    sequence = root.find(".//sequence")
    if sequence is None:
        return base_xml

    media = sequence.find("media")
    if media is None:
        return base_xml

    video_elem = media.find("video")
    audio_elem = media.find("audio")

    ntsc = "FALSE"

    # Current picture duration (we will extend it if we add VO after the picture)
    try:
        pic_dur = int((sequence.find("duration").text or "0"))
    except Exception:
        pic_dur = 0

    current_f = pic_dur

    # === Narration-aware timeline injection ===
    # We compute the desired shifts and placements first (pure, safe).
    # Then mutate the tree + add VO/titles in a defensive try so that even if
    # the fancy interleaved VO injection has a bug with certain data, we still
    # return a valid (picture) XML and the generated narration audio files (WAV/MP3) are usable.
    narrative_sequence = get_narrative_sequence(ver) or []
    clip_shifts_sec: list[float] = []
    vo_placements: list[tuple[float, dict]] = []
    vo_durs = []
    for v in (vo_files or []):
        d = float(v.get("duration", 0) or 0)
        if d <= 0:
            w = len((v.get("text") or "").split())
            d = max(2.0, w / 3.2)
        vo_durs.append(d)
    title_durs = [float(t.get("duration", 0) or 3.0) for t in (title_items or [])] if title_items else []
    current_clip_base = 0.0
    preceding_nar = 0.0
    v_i = 0
    t_i = 0
    for itm in narrative_sequence:
        if itm.get("type") == "clip":
            cdur = max(0.0, (float(itm.get("source_out", 0)) - float(itm.get("source_in", 0))) or 0)
            clip_shifts_sec.append(preceding_nar)
            current_clip_base += cdur
        elif itm.get("type") == "narration":
            if vo_files and v_i < len(vo_durs):
                vd = vo_durs[v_i]
                nar_start = current_clip_base + preceding_nar
                if v_i < len(vo_files):
                    vo_placements.append((nar_start, vo_files[v_i]))
                preceding_nar += vd
                v_i += 1
            elif title_items and t_i < len(title_durs):
                td = title_durs[t_i]
                preceding_nar += td
                t_i += 1

    # Include any extra vo_files beyond the number of narration slots (e.g. the full "Narration.wav"
    # for the AI Narration / Voiceover Script) by appending them after the last placed narration.
    # This makes the Narration.wav "work with the XML" (it gets imported as an extra VO clipitem at the end).
    if vo_files and len(vo_placements) < len(vo_files):
        extra_start = 0.0
        if vo_placements:
            last = vo_placements[-1]
            extra_start = last[0] + last[1].get("duration", 10.0)
        for extra_i in range(len(vo_placements), len(vo_files)):
            vo_placements.append((extra_start, vo_files[extra_i]))
            extra_start += vo_files[extra_i].get("duration", 10.0)

    # Now do the tree mutations + additions defensively.
    try:
        # Apply the shifts to all picture clipitems (video track + picture audio track).
        # VO clipitems and <title> elements are emitted later with absolute program times.
        if clip_shifts_sec:
            if video_elem is not None:
                vtrk = video_elem.find("track")
                if vtrk is not None:
                    vclips = [c for c in vtrk.findall("clipitem")
                              if "-vo-" not in (c.get("id") or "") and "title-" not in (c.get("id") or "")]
                    for ii, vc in enumerate(vclips):
                        if ii >= len(clip_shifts_sec):
                            break
                        shf = _seconds_to_frames(clip_shifts_sec[ii], timebase)
                        for fld in ("start", "end"):
                            try:
                                node = vc.find(fld)
                                if node is not None and node.text is not None:
                                    old = int(node.text)
                                    node.text = str(old + shf)
                            except Exception:
                                pass
            if audio_elem is not None:
                atracks = audio_elem.findall("track")
                if atracks:
                    # Picture audio lives in first exploded mono track only
                    pic_atrack = atracks[0]
                    aclips = [c for c in pic_atrack.findall("clipitem") if "-vo-" not in (c.get("id") or "")]
                    for ii, ac in enumerate(aclips):
                        if ii >= len(clip_shifts_sec):
                            break
                        shf = _seconds_to_frames(clip_shifts_sec[ii], timebase)
                        for fld in ("start", "end"):
                            try:
                                node = ac.find(fld)
                                if node is not None and node.text is not None:
                                    old = int(node.text)
                                    node.text = str(old + shf)
                            except Exception:
                                pass

        # 2. Voiceover audio track with correct interleaved timing from narrative_sequence
        if vo_files and audio_elem is not None:
            # Create the 3rd track as Voiceover
            vo_track = ET.SubElement(
                audio_elem, "track",
                premiereTrackType="Voiceover",
                TL_SQTrackAudioKeyframeStyle="0",
                TL_SQTrackShy="0",
                MZ_TrackTargeted="1",
                TL_SQTrackExpandedHeight="41",
                currentExplodedTrackIndex="2",
                totalExplodedTrackCount="3"
            )

            vo_track_index = 2
            fid_counter = max((m.get("file_id", 1) for m in source_meta.values()), default=1) + 1

            # Use the accurate vo_placements computed earlier (with real probed durations + clip shifts accounted for).
            # Falls back to the legacy walk if for some reason vo_placements is empty.
            vo_timings = vo_placements if vo_placements else []
            if not vo_timings:
                current_t = 0.0
                vo_idx = 0
                for item in (get_narrative_sequence(ver) or []):
                    if item.get("type") == "clip":
                        dur = (item.get("source_out", 0) - item.get("source_in", 0)) or 0
                        current_t += max(0.0, dur)
                    elif item.get("type") == "narration" and vo_idx < len(vo_files):
                        vo_info = vo_files[vo_idx]
                        vo_timings.append((current_t, vo_info))
                        vo_dur = vo_info.get("duration", 0.0)
                        if vo_dur <= 0:
                            words = len(vo_info.get("text", "").split())
                            vo_dur = max(2.0, words / 3.2)
                        current_t += vo_dur
                        vo_idx += 1
                while vo_idx < len(vo_files):
                    vo_timings.append((current_t, vo_files[vo_idx]))
                    vo_dur = vo_files[vo_idx].get("duration", 10.0)
                    current_t += vo_dur
                    vo_idx += 1

            # VO track cursor (for extending the overall sequence duration after picture)
            current_vo_f = 0

            # Emit VO clipitems using the computed interleaved timings.
            for start_sec, vo_info in vo_timings:
                vo_fid = fid_counter
                fid_counter += 1

                # Defensive access to path/index (should always be present for pre-gen or internal prep)
                path_obj = vo_info.get("path")
                if path_obj is None:
                    path_obj = "bridge.wav" if get_tts_provider() == "local" else "bridge.mp3"
                safe_name = getattr(path_obj, "name", str(path_obj).split("/")[-1].split("\\")[-1])
                safe_path_str = str(path_obj)

                idx = vo_info.get("index", 0)
                try:
                    idx = int(idx)
                except Exception:
                    idx = 0

                vo_dur_sec = vo_info.get("duration", 0.0)
                if vo_dur_sec <= 0:
                    try:
                        _, vdur = get_media_start_offset_and_duration(path_obj if hasattr(path_obj, "exists") else safe_path_str)
                        vo_dur_sec = float(vdur) if vdur else 10.0
                    except Exception:
                        vo_dur_sec = 10.0

                vo_dur_f = _seconds_to_frames(vo_dur_sec, timebase)
                if not vo_dur_f:
                    vo_dur_f = _seconds_to_frames(10.0, timebase)
                start_f = _seconds_to_frames(start_sec, timebase)
                end_f = start_f + vo_dur_f

                # --- Single, clean VO clipitem (rich structure so the audio (WAV/MP3) relinks) ---
                vf = ET.SubElement(vo_track, "clipitem", id=f"clipitem-vo-{vo_fid}")
                ET.SubElement(vf, "masterclipid").text = f"masterclip-{vo_fid}"
                ET.SubElement(vf, "name").text = f"Bridge {idx:02d}"
                ET.SubElement(vf, "enabled").text = "TRUE"
                ET.SubElement(vf, "duration").text = str(vo_dur_f)

                vr = ET.SubElement(vf, "rate")
                ET.SubElement(vr, "timebase").text = str(timebase)
                ET.SubElement(vr, "ntsc").text = ntsc

                ET.SubElement(vf, "start").text = str(start_f)
                ET.SubElement(vf, "end").text = str(end_f)
                ET.SubElement(vf, "in").text = "0"
                ET.SubElement(vf, "out").text = str(vo_dur_f)

                # Full <file> for the generated bridge audio (zero TC, correct sr/ch for the VO)
                f_elem = ET.SubElement(vf, "file", id=f"file-{vo_fid}")
                ET.SubElement(f_elem, "name").text = safe_name
                pathurl = f"file://localhost{safe_path_str.replace(chr(92), '/')}"
                ET.SubElement(f_elem, "pathurl").text = pathurl

                fr = ET.SubElement(f_elem, "rate")
                ET.SubElement(fr, "timebase").text = str(timebase)
                ET.SubElement(fr, "ntsc").text = ntsc
                ET.SubElement(f_elem, "duration").text = str(vo_dur_f)

                vtc = ET.SubElement(f_elem, "timecode")
                vtcr = ET.SubElement(vtc, "rate")
                ET.SubElement(vtcr, "timebase").text = str(timebase)
                ET.SubElement(vtcr, "ntsc").text = ntsc
                ET.SubElement(vtc, "string").text = "00:00:00:00"
                ET.SubElement(vtc, "frame").text = "0"
                ET.SubElement(vtc, "displayformat").text = "NDF"

                vma = ET.SubElement(ET.SubElement(f_elem, "media"), "audio")
                vsca = ET.SubElement(vma, "samplecharacteristics")
                ET.SubElement(vsca, "depth").text = "16"
                sr = int(vo_info.get("sample_rate", 44100) or 44100)
                ch = int(vo_info.get("channels", 1) or 1)
                ET.SubElement(vsca, "samplerate").text = str(sr)
                ET.SubElement(vma, "channelcount").text = str(ch)

                vst_vo = ET.SubElement(vf, "sourcetrack")
                ET.SubElement(vst_vo, "mediatype").text = "audio"
                ET.SubElement(vst_vo, "trackindex").text = str(vo_track_index + 1)

                # Advance the VO cursor using the *actual* placed length for this bridge
                current_vo_f = end_f

            # Extend sequence duration to include the voiceovers (so the whole timeline is long enough in Premiere)
            new_dur = max(pic_dur, current_vo_f)
            if sequence.find("duration") is not None:
                sequence.find("duration").text = str(new_dur)

        # 3. Narration titles track (only when user chose "XML + Text Titles")
        if title_items and video_elem is not None:
            titles_track = ET.SubElement(video_elem, "track")
            title_idx = 1

            for t in title_items:
                start_f = _seconds_to_frames(t["start"], timebase)
                dur_f = _seconds_to_frames(t["duration"], timebase)
                end_f = start_f + dur_f

                title = ET.SubElement(titles_track, "title", id=f"title-{title_idx}")
                ET.SubElement(title, "name").text = f"Narration {title_idx:02d}"
                ET.SubElement(title, "enabled").text = "TRUE"
                ET.SubElement(title, "duration").text = str(dur_f)

                tr = ET.SubElement(title, "rate")
                ET.SubElement(tr, "timebase").text = str(timebase)
                ET.SubElement(tr, "ntsc").text = ntsc

                ET.SubElement(title, "start").text = str(start_f)
                ET.SubElement(title, "end").text = str(end_f)
                ET.SubElement(title, "in").text = "0"
                ET.SubElement(title, "out").text = str(dur_f)

                # User's requested visible text structure
                txt = ET.SubElement(title, "text")
                ts = ET.SubElement(txt, "text-style", ref="ts1")
                ts.text = t["text"]

                title_idx += 1

            # Titles extend the overall sequence duration
            max_title_end = max(
                (_seconds_to_frames(ti["start"] + ti["duration"], timebase) for ti in title_items),
                default=0
            )
            final_dur = max(int((sequence.find("duration").text or "0")), max_title_end)
            if sequence.find("duration") is not None:
                sequence.find("duration").text = str(final_dur)

        # Final duration reconciliation: after picture shifts + VO additions + title additions,
        # ensure <sequence duration> covers the true last frame on any track.
        try:
            max_end = 0
            if video_elem is not None:
                for tr in video_elem.findall("track"):
                    for c in list(tr.findall("clipitem")) + list(tr.findall("title")):
                        try:
                            e = int((c.find("end").text or "0") or 0)
                            if e > max_end:
                                max_end = e
                        except Exception:
                            pass
            if audio_elem is not None:
                for tr in audio_elem.findall("track"):
                    for c in tr.findall("clipitem"):
                        try:
                            e = int((c.find("end").text or "0") or 0)
                            if e > max_end:
                                max_end = e
                        except Exception:
                            pass
            if max_end > 0 and sequence.find("duration") is not None:
                sequence.find("duration").text = str(max_end)
        except Exception:
            pass
    except Exception as inj_ex:
        print(f"[Narrative VO Exporter] Narration/VO injection into XML failed (will export base picture XML + the generated voiceover audio files): {inj_ex}")
        import traceback
        traceback.print_exc()
        # Return the base (picture) XML so the export still produces a usable file.
        # The voiceover audio files (WAV for local Piper / MP3 for Google) were already written to the exports dir during generation.
        return base_xml

    # Re-pretty-print
    rough = ET.tostring(root, encoding="unicode")
    reparsed = minidom.parseString(rough)
    return reparsed.toprettyxml(indent="  ")


# ---------------------------------------------------------------------------
# MIGRATION STATUS (updated on "go!")
# ---------------------------------------------------------------------------
#
# Exporter #3 now owns:
# - Voiceover bridge audio generation (_prepare_voiceover_bridges)  # WAV stereo 44.1k for Piper, MP3 for Google
# - Narration-as-titles timing (_prepare_narration_title_items)
# - High-level single vs multi decision + file writing
# - Full enhanced multi-source build via composition:
#     _build_narrative_vo_mixed_xmeml re-uses _build_mixed_sources_xmeml (#2)
#     then injects the optional Voiceover audio track and/or <title> track.
#
# The giant nested _legacy_export_multi_xml in app.py is now only a
# fallback / transition shim. Direct calls from the dialog for the three
# enhanced export buttons already go through this module.
#
# Next: remove the remaining shims in app.py and delete the old prep
# + emission code from the legacy once the new path has proven stable.

