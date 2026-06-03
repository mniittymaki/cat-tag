"""
Multi - AI Director (without narrations)

This is exporter #2:

2. Multi - AI Director (no narration) → multi_xmeml_exporter.py (this file)

This module is responsible for exporting AI Director multi-source cuts
**when no narration/voiceover was generated**.

It provides:
- Robust multi-source XMEML v5 export (multiple source files)
- Per-source native timebases + actual resolution (width/height) and audio channel count from each source file
- Strict "zero timecode" policy: every source file is forced to start at 00:00:00:00 (on its *definition*)
- Exploded mono audio tracks (2 tracks, picture audio on first, second empty for compatibility)
- Clean, reliably relinkable XML for Premiere using classic XMEML style:
  full rich <file> (name/pathurl/rate/duration/timecode+00:00:00:00/media specs) emitted only on FIRST use of each source;
  later clipitems from the same source use lightweight <file id="file-N"/> references.
  This structure was present in the last known "perfect sync" multi exports.
- clipitem <duration> set to full source duration for every use of a source
- max_out_f_per_fid sanitization + real-sec timeline accumulation for <start>/<end> + DIRECT raw* tb for in/out on good data (pack fallback only for clearly bad per-fid)

This module must **not** know anything about:
- Narration bridges
- Text titles
- Voiceover audio generation
- narrative_sequence with mixed clip + narration items

Those belong in narrative_vo_exporter.py (#3).

Picture audio is always included as two exploded mono tracks (standard for reliable
multi-source relinking in Premiere when no narration/voiceover is present).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import subprocess
import json
from xml.dom import minidom
import xml.etree.ElementTree as ET
from collections import defaultdict

# Note: Some helpers are temporarily imported from app.py or xmeml_exporter during migration.
# They will be moved here over time.

def _build_mixed_sources_xmeml(
    ver: dict[str, Any],
    ordered_sources: list[str],
    source_meta: dict[str, dict],
    source_paths: dict[str, str],
    timebase: int,
    name: str,
) -> str:
    """
    Build the complete XMEML v5 for a multi-source AI Director export (NO narration features).

    This is exporter #2's core. It must produce:
    - Per-source native timebases for all clipitems and file defs
    - Strict 00:00:00:00 timecode on every source file
    - Exploded 2-mono audio track layout (picture audio only)
    - Full rich clipitem structure with links + sourcetracks for Premiere reliability
    - Optional narrative_summary as <comment> (editorial, not spoken narration)

    NEVER emit voiceover tracks, title elements, or any narration bridge artifacts.
    Those are exclusively for narrative_vo_exporter.py (#3).
    """
    ntsc = "FALSE"
    segs = ver.get("selected_segments", [])

    root = ET.Element("xmeml", version="5")
    project = ET.SubElement(root, "project")
    ET.SubElement(project, "name").text = f"AI Multi - {ver.get('title', ver.get('version_id', 'Cut'))}"
    children = ET.SubElement(project, "children")
    sequence = ET.SubElement(children, "sequence", id="sequence-1")
    ET.SubElement(sequence, "name").text = f"AI Multi - {ver.get('title', ver.get('version_id', 'Cut'))}"

    summary = (ver.get("narrative_summary") or "").strip()
    if summary:
        ET.SubElement(sequence, "comment").text = summary[:500]

    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = str(timebase)
    ET.SubElement(rate, "ntsc").text = ntsc

    ET.SubElement(sequence, "duration").text = "0"  # patched later

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")

    first_src = ordered_sources[0] if ordered_sources else None
    first_meta = source_meta.get(first_src, {}) if first_src else {}
    seq_w = first_meta.get("width", 1920)
    seq_h = first_meta.get("height", 1080)

    fmt = ET.SubElement(video, "format")
    sc = ET.SubElement(fmt, "samplecharacteristics")
    ET.SubElement(sc, "width").text = str(seq_w)
    ET.SubElement(sc, "height").text = str(seq_h)
    rsc = ET.SubElement(sc, "rate")
    ET.SubElement(rsc, "timebase").text = str(timebase)
    ET.SubElement(rsc, "ntsc").text = ntsc
    # Explicitly declare square pixels (or actual PAR from source clips) for HD/4K.
    # This prevents Premiere from defaulting to D1/DV PAL 1.0940 etc.
    par_seq = first_meta.get("pixel_aspect_ratio", "1:1")
    if par_seq in ("1:1", "1/1", "1:1.0", "1.000"):
        par_seq = "square"
    ET.SubElement(sc, "anamorphic").text = "FALSE"
    ET.SubElement(sc, "pixelaspectratio").text = par_seq
    ET.SubElement(sc, "fielddominance").text = "none"

    vtrack = ET.SubElement(video, "track")

    # Audio: always exactly 2 mono tracks for picture (no voiceover track in #2)
    aseq = ET.SubElement(media, "audio")
    afmt = ET.SubElement(aseq, "format")
    asc = ET.SubElement(afmt, "samplecharacteristics")
    ET.SubElement(asc, "depth").text = "16"
    ET.SubElement(asc, "samplerate").text = "48000"

    num_audio_tracks = 2
    audio_tracks = []
    for i in range(num_audio_tracks):
        t = ET.SubElement(
            aseq, "track",
            premiereTrackType="Mono",
            TL_SQTrackAudioKeyframeStyle="0",
            TL_SQTrackShy="0",
            MZ_TrackTargeted="1",
            TL_SQTrackExpandedHeight="41",
            currentExplodedTrackIndex=str(i),
            totalExplodedTrackCount=str(num_audio_tracks)
        )
        audio_tracks.append(t)

    atrack = audio_tracks[0] if audio_tracks else None

    # Accumulate real timeline in *seconds* (independent of per-source fps).
    # Then convert positions to the sequence timebase frames.
    # This guarantees correct <start>/<end> even when sources have different frame rates.
    current_seq_sec = 0.0
    clip_idx = 1

    # Track what we've already logged for this export (avoid per-clip spam)
    debug_logged_fids = set()

    # Track which source fids have already had their full <file> definition emitted (so we emit
    # the rich block with 00:00:00:00 + media specs only once per source, and use lightweight
    # <file id="file-N"/> references for all later clipitems from the same source).
    # This restores the structure of the last known perfectly material-syncing multi exports.
    seen_fids = set()

    # Per-fid subs collected from selected_segments. The raw label times (source_in/out)
    # are the true media offsets within each source file (see exported TXT note and how
    # transcripts provide [HH:MM:SS] as "original media time within each source file").
    # We treat every camera identically for timecode math.
    subs_per_fid: dict[int, list] = defaultdict(list)
    for seg in segs:
        sp = seg.get("source_path")
        if not sp or sp not in source_meta:
            continue
        meta = source_meta[sp]
        fid = meta["file_id"]
        raw_in = float(seg.get("source_in") or seg.get("start", 0.0))
        raw_out = float(seg.get("source_out") or seg.get("end", 0.0))
        if raw_out <= raw_in + 0.05:
            continue
        subs_per_fid[fid].append((raw_in, raw_out, seg))

    # --- Per-fid timecode policy (as it was for the 11:10 working exports) ---
    # DIRECT raw label time for fids where labels are within the real probed dur.
    # PACK (remap to small valid offsets, preserving order and durs) only for fids with bad/high
    # label times that would produce in/out > file dur.
    # This guarantees valid XML for relinking even with occasional bad Director data, while
    # keeping exact numbers for good data.
    # The <file duration> is the max needed.
    use_pack_for_fid: dict[int, bool] = {}
    max_out_f_per_fid: dict[int, int] = {}
    assigned_media: dict[tuple, tuple[float, float]] = {}

    for fid, sublist in subs_per_fid.items():
        if not sublist:
            use_pack_for_fid[fid] = False
            continue
        sp0 = sublist[0][2].get("source_path")
        m0 = source_meta.get(sp0, {})
        probed_dur_f = int(m0.get("dur_f", 0))
        ftb = m0.get("src_timebase", timebase)

        max_direct_out_f = 0
        would_overflow = False
        for rin, rout, _s in sublist:
            of = _seconds_to_frames(rout, ftb)
            if of > max_direct_out_f:
                max_direct_out_f = of
            if of > probed_dur_f + 50:
                would_overflow = True

        use_pack = would_overflow
        use_pack_for_fid[fid] = use_pack
        max_out_f_per_fid[fid] = max(probed_dur_f, max_direct_out_f)

    # Build packed positions only for bad fids.
    for fid, sublist in subs_per_fid.items():
        if not use_pack_for_fid.get(fid, False):
            continue
        sublist.sort(key=lambda t: t[0])
        pos_s = 0.0
        first_sp = sublist[0][2].get("source_path") if sublist else None
        meta0 = source_meta.get(first_sp, {}) if first_sp else {}
        ftb = meta0.get("src_timebase", timebase)
        for raw_in, raw_out, seg in sublist:
            d_s = max(0.01, raw_out - raw_in)
            m_in = pos_s
            m_out = pos_s + d_s
            key = (fid, round(raw_in, 3), round(raw_out, 3))
            assigned_media[key] = (m_in, m_out)
            pos_s = m_out
        packed_f = _seconds_to_frames(pos_s, ftb)
        max_out_f_per_fid[fid] = packed_f

    # Final safety for declared dur.
    for fid in list(subs_per_fid.keys()):
        probed_f = 0
        ftb = timebase
        for s in segs:
            m = source_meta.get(s.get("source_path"), {})
            if m.get("file_id") == fid:
                probed_f = max(probed_f, int(m.get("dur_f", 0)))
                ftb = m.get("src_timebase", ftb)
                break
        cur = max_out_f_per_fid.get(fid, probed_f)
        max_out_f_per_fid[fid] = max(probed_f, cur)

    for seg in segs:
        sp = seg.get("source_path")
        if not sp or sp not in source_meta:
            continue
        meta = source_meta[sp]
        fid = meta["file_id"]

        raw_in = float(seg.get("source_in") or seg.get("start", 0.0))
        raw_out = float(seg.get("source_out") or seg.get("end", 0.0))
        if raw_out <= raw_in + 0.05:
            continue

        file_timebase = meta.get("src_timebase", timebase)
        key = (fid, round(raw_in, 3), round(raw_out, 3))
        if use_pack_for_fid.get(fid, False) and key in assigned_media:
            media_in_sec, media_out_sec = assigned_media[key]
        else:
            # DIRECT: raw label seconds from the version/script (exact match to [times] in the TXT script
            # for this beat, and correct content when the Director data was good).
            media_in_sec = max(0.0, raw_in)
            media_out_sec = max(media_in_sec + 0.01, raw_out)
        in_f = _seconds_to_frames(media_in_sec, file_timebase)
        out_f = _seconds_to_frames(media_out_sec, file_timebase)
        dur_f = out_f - in_f
        if dur_f <= 0:
            continue

        # Harden max_out.
        if fid not in max_out_f_per_fid or out_f > max_out_f_per_fid[fid]:
            max_out_f_per_fid[fid] = out_f

        # Debug.
        probed_dur_f = meta.get("dur_f", 0)
        if fid not in debug_logged_fids:
            if use_pack_for_fid.get(fid, False):
                print(f"[XMEML] Using PACK (bad high labels for this source, content remapped to valid) for {meta['fname']} (fid={fid}): raw_in={raw_in:.2f} -> in_f={in_f} (file dur {probed_dur_f})")
            else:
                print(f"[XMEML] Using DIRECT (exact label time from script/version * tb) for {meta['fname']} (fid={fid}): raw_in={raw_in:.2f} -> in_f={in_f} (file dur {probed_dur_f})")
            debug_logged_fids.add(fid)

        # Compute the full source file dur (probed or max needed) -- set clipitem<duration> to this
        file_dur = max(meta.get("dur_f", 0), max_out_f_per_fid.get(fid, meta.get("dur_f", 0)))

        # Use the actual pulled length for timeline placement (the d_s from raw or the packed d_s)
        clip_dur_sec = max(0.1, media_out_sec - media_in_sec)
        start_f = _seconds_to_frames(current_seq_sec, timebase)
        end_f = _seconds_to_frames(current_seq_sec + clip_dur_sec, timebase)

        src_label = seg.get("source_label") or f"C{ordered_sources.index(sp) + 1 if sp in ordered_sources else '?'}"
        seg_name = f"{src_label} - {meta['fname']}"
        reason = (seg.get("reason") or seg.get("text") or "").strip()

        # VIDEO clipitem (rich structure for reliable relinking)
        clipitem = ET.SubElement(vtrack, "clipitem", id=f"clipitem-{clip_idx}")
        ET.SubElement(clipitem, "masterclipid").text = f"masterclip-{fid}"
        ET.SubElement(clipitem, "name").text = seg_name
        ET.SubElement(clipitem, "enabled").text = "TRUE"
        ET.SubElement(clipitem, "duration").text = str(file_dur) if file_dur > 0 else str(dur_f)

        color_key = f"{meta.get('camera', '') or src_label}::{meta['fname']}"
        label_color = _get_premiere_label_color(color_key)
        ET.SubElement(clipitem, "label").text = label_color

        r = ET.SubElement(clipitem, "rate")
        ET.SubElement(r, "timebase").text = str(file_timebase)
        ET.SubElement(r, "ntsc").text = ntsc

        ET.SubElement(clipitem, "start").text = str(start_f)
        ET.SubElement(clipitem, "end").text = str(end_f)
        ET.SubElement(clipitem, "in").text = str(in_f)
        ET.SubElement(clipitem, "out").text = str(out_f)

        # Emit FULL rich <file> definition (with 00:00:00:00, media specs, max dur) ONLY on the FIRST
        # use of this fid in the timeline. Subsequent clipitems (even from same source, non-contiguous)
        # get a lightweight reference <file id="file-N"/>. This matches the structure of the last
        # known perfectly-syncing working multi XMLs (exported ~11:10) while still providing the
        # zero-TC policy and rich media info on the definition.
        if fid not in seen_fids:
            seen_fids.add(fid)
            f = ET.SubElement(clipitem, "file", id=f"file-{fid}")
            ET.SubElement(f, "name").text = meta["fname"]
            abs_path = str(Path(sp).resolve()).replace("\\", "/")
            ET.SubElement(f, "pathurl").text = f"file://localhost{abs_path}"

            fr = ET.SubElement(f, "rate")
            ET.SubElement(fr, "timebase").text = str(file_timebase)
            ET.SubElement(fr, "ntsc").text = ntsc
            file_dur = max(meta.get("dur_f", 0), max_out_f_per_fid.get(fid, meta.get("dur_f", 0)))
            if file_dur > 0:
                ET.SubElement(f, "duration").text = str(file_dur)

            # Zero timecode policy - strictly enforced (on the definition)
            tc = ET.SubElement(f, "timecode")
            tcr = ET.SubElement(tc, "rate")
            ET.SubElement(tcr, "timebase").text = str(file_timebase)
            ET.SubElement(tcr, "ntsc").text = ntsc
            ET.SubElement(tc, "string").text = "00:00:00:00"
            ET.SubElement(tc, "frame").text = "0"
            ET.SubElement(tc, "displayformat").text = "NDF"

            mf = ET.SubElement(f, "media")
            mv = ET.SubElement(mf, "video")
            scv = ET.SubElement(mv, "samplecharacteristics")
            rv = ET.SubElement(scv, "rate")
            ET.SubElement(rv, "timebase").text = str(file_timebase)
            ET.SubElement(rv, "ntsc").text = ntsc
            ET.SubElement(scv, "width").text = str(meta["width"])
            ET.SubElement(scv, "height").text = str(meta["height"])
            ET.SubElement(scv, "anamorphic").text = "FALSE"
            par = meta.get("pixel_aspect_ratio", "1:1")
            if par in ("1:1", "1/1", "1:1.0", "1.000"):
                par = "square"
            ET.SubElement(scv, "pixelaspectratio").text = par
            ET.SubElement(scv, "fielddominance").text = "none"

            ma = ET.SubElement(mf, "audio")
            sca = ET.SubElement(ma, "samplecharacteristics")
            ET.SubElement(sca, "depth").text = "16"
            ET.SubElement(sca, "samplerate").text = str(meta["audio_sr"])
            ET.SubElement(ma, "channelcount").text = str(meta["audio_ch"])
        else:
            # Reference only for subsequent uses of the same source file (classic XMEML, proven to
            # give perfect material sync with the Director script for multi-cam with many cuts).
            ET.SubElement(clipitem, "file", id=f"file-{fid}")

        if reason:
            ET.SubElement(clipitem, "comment").text = f"[{src_label}] {reason}"[:240]

        # Sourcetrack + links for video (helps Premiere with track association and linked selection)
        vst = ET.SubElement(clipitem, "sourcetrack")
        ET.SubElement(vst, "mediatype").text = "video"
        ET.SubElement(vst, "trackindex").text = "1"

        alink = ET.SubElement(clipitem, "link")
        ET.SubElement(alink, "linkclipref").text = f"clipitem-{clip_idx}-audio"
        ET.SubElement(alink, "mediatype").text = "audio"
        ET.SubElement(alink, "trackindex").text = "1"
        ET.SubElement(alink, "clipindex").text = str(clip_idx)

        if meta.get("audio_ch", 2) >= 2:
            alink2 = ET.SubElement(clipitem, "link")
            ET.SubElement(alink2, "linkclipref").text = f"clipitem-{clip_idx}-audio"
            ET.SubElement(alink2, "mediatype").text = "audio"
            ET.SubElement(alink2, "trackindex").text = "2"
            ET.SubElement(alink2, "clipindex").text = str(clip_idx)

        # --- AUDIO emission (matches the proven working multi no-narration structure) ---
        # All actual audio content goes into the first mono track (explodedTrackIndex="0").
        # The second mono track is created empty (as in the reference working XML).
        # Video clipitems carry dual links (track 1 + track 2) pointing to the audio on track 1.
        if audio_tracks:
            # Emit audio clipitem into the first exploded mono track
            aclip = ET.SubElement(audio_tracks[0], "clipitem", id=f"clipitem-{clip_idx}-audio")
            ET.SubElement(aclip, "masterclipid").text = f"masterclip-{fid}"
            ET.SubElement(aclip, "name").text = f"{seg_name} (Audio)"
            ET.SubElement(aclip, "enabled").text = "TRUE"
            ET.SubElement(aclip, "duration").text = str(file_dur) if file_dur > 0 else str(dur_f)

            ET.SubElement(aclip, "label").text = label_color

            ar = ET.SubElement(aclip, "rate")
            ET.SubElement(ar, "timebase").text = str(file_timebase)
            ET.SubElement(ar, "ntsc").text = ntsc

            ET.SubElement(aclip, "start").text = str(start_f)
            ET.SubElement(aclip, "end").text = str(end_f)
            ET.SubElement(aclip, "in").text = str(in_f)
            ET.SubElement(aclip, "out").text = str(out_f)

            st = ET.SubElement(aclip, "sourcetrack")
            ET.SubElement(st, "mediatype").text = "audio"
            ET.SubElement(st, "trackindex").text = "1"

            vlink = ET.SubElement(aclip, "link")
            ET.SubElement(vlink, "linkclipref").text = f"clipitem-{clip_idx}"
            ET.SubElement(vlink, "mediatype").text = "video"
            ET.SubElement(vlink, "trackindex").text = "1"
            ET.SubElement(vlink, "clipindex").text = str(clip_idx)

            # For audio clipitems: emit full rich file def only on first use of the fid (the video
            # pass for the same source use will have already marked it in seen_fids). Use reference
            # for repeats. Matches the working 11:10 structure.
            if fid not in seen_fids:
                seen_fids.add(fid)
                f = ET.SubElement(aclip, "file", id=f"file-{fid}")
                ET.SubElement(f, "name").text = meta["fname"]
                abs_path = str(Path(sp).resolve()).replace("\\", "/")
                ET.SubElement(f, "pathurl").text = f"file://localhost{abs_path}"

                fr = ET.SubElement(f, "rate")
                ET.SubElement(fr, "timebase").text = str(file_timebase)
                ET.SubElement(fr, "ntsc").text = ntsc
                file_dur = max(meta.get("dur_f", 0), max_out_f_per_fid.get(fid, meta.get("dur_f", 0)))
                if file_dur > 0:
                    ET.SubElement(f, "duration").text = str(file_dur)
                # One-time-per-source summary (the per-clip version was too noisy)
                # (per-source summary already emitted in the video clipitem pass for this fid)

                # Zero timecode policy
                tc = ET.SubElement(f, "timecode")
                tcr = ET.SubElement(tc, "rate")
                ET.SubElement(tcr, "timebase").text = str(file_timebase)
                ET.SubElement(tcr, "ntsc").text = ntsc
                ET.SubElement(tc, "string").text = "00:00:00:00"
                ET.SubElement(tc, "frame").text = "0"
                ET.SubElement(tc, "displayformat").text = "NDF"

                mf = ET.SubElement(f, "media")
                ma = ET.SubElement(mf, "audio")
                sca = ET.SubElement(ma, "samplecharacteristics")
                ET.SubElement(sca, "depth").text = "16"
                ET.SubElement(sca, "samplerate").text = str(meta["audio_sr"])
                ET.SubElement(ma, "channelcount").text = str(meta["audio_ch"])
            else:
                ET.SubElement(aclip, "file", id=f"file-{fid}")

        current_seq_sec += clip_dur_sec
        clip_idx += 1

    # Patch real duration (sum of the AI-selected cuts)
    sequence.find("duration").text = str(_seconds_to_frames(current_seq_sec, timebase))

    # --- Clear per-fid diagnostic summary (very useful when incrementally adding cameras) ---
    print("[XMEML] === Per-source timecode decision summary (for testing) ===")
    for fid in sorted(max_out_f_per_fid.keys()):
        meta_for_fid = None
        for sp, m in source_meta.items():
            if m.get("file_id") == fid:
                meta_for_fid = m
                break
        if not meta_for_fid:
            continue
        # Always DIRECT now (in/out numbers exactly match script/TXT label times * tb for every camera).
        print(f"  fid={fid} {meta_for_fid.get('fname', '?')}: DIRECT (exact label time from script/version * tb)")
        print(f"           declared file_dur={max_out_f_per_fid[fid]}  (real probed={meta_for_fid.get('dur_f', 0)})")
    print("[XMEML] ============================================================")

    print(f"[XMEML] Multi export done: {len(ordered_sources)} sources, {clip_idx-1} clips. Per-source: DIRECT (exact label times when sane) or PACK (bad high labels only for that source) + file ref style (full only first use) + 00:00:00:00. Matches the 11:10 working exports.")

    rough_string = ET.tostring(root, encoding="unicode")
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ")

# ---------------------------------------------------------------------------
# Helper functions (being extracted from the legacy code)
# ---------------------------------------------------------------------------

def get_audio_characteristics(source_path: str | Path) -> dict[str, Any]:
    """Probe audio channel count and sample rate for a source (or VO) file.
    Returns {"channels": int, "sample_rate": int}.
    """
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-select_streams", "a:0", str(source_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True)
        data = json.loads(result.stdout or "{}")
        streams = data.get("streams", [])
        if streams:
            ch = streams[0].get("channels")
            sr = streams[0].get("sample_rate")
            return {
                "channels": int(ch) if ch else 2,
                "sample_rate": int(sr) if sr else 48000,
            }
    except Exception:
        pass
    return {"channels": 2, "sample_rate": 48000}


def _seconds_to_frames(seconds: float, timebase: int) -> int:
    """Convert seconds to frames using the given timebase."""
    if seconds is None or seconds <= 0:
        return 0
    return int(round(seconds * timebase))


def prepare_director_sources(
    ver: dict[str, Any],
    source_paths: dict[str, str],
) -> tuple[list[str], dict[str, dict], int]:
    """
    Pre-probe all sources for an AI Director multi-source export.

    Returns:
        (ordered_sources, source_meta, timebase)

    timebase for the *sequence* is chosen as the most common native rate
    across sources (not the first clip in narrative order). This avoids
    inheriting bizarre rates (e.g. 150 fps) from whichever source happened
    to appear first in the AI Director's selected_segments.

    Per-source src_timebase values are still preserved for accurate clipitem
    timing when sources have genuinely different frame rates.

    This logic is being extracted so it can be shared between
    multi_xmeml_exporter and narrative_vo_exporter.
    """
    from minicat.ai.xmeml_exporter import get_video_timebase, get_media_start_offset_and_duration

    # Derive timebase: prefer the *most common* across all unique sources
    # (not blindly the first clip that appears in the AI story order).
    # This prevents weird sequence rates (e.g. 150) when the narrative happens
    # to start with an unusual high-speed or mis-probed source.
    ordered_sources: list[str] = []
    seen = set()
    for seg in ver.get("selected_segments", []):
        p = seg.get("source_path")
        if p and p not in seen:
            seen.add(p)
            ordered_sources.append(p)

    timebase = 25
    unique_tbs: set[int] = set()
    if ordered_sources:
        try:
            timebase_counts: dict[int, int] = {}
            for sp in ordered_sources:
                try:
                    tb = get_video_timebase(sp)
                    if 1 <= tb <= 120:  # basic sanity filter
                        timebase_counts[tb] = timebase_counts.get(tb, 0) + 1
                        unique_tbs.add(tb)
                except Exception:
                    pass
            if timebase_counts:
                # most common
                timebase = max(timebase_counts.items(), key=lambda kv: kv[1])[0]
            else:
                timebase = get_video_timebase(ordered_sources[0])
        except Exception:
            timebase = 25

    if timebase > 120 or timebase < 1:
        print(f"[Multi XMEML] Warning: insane sequence timebase {timebase} detected, defaulting to 25")
        timebase = 25

    if len(unique_tbs) > 1:
        print(f"[Multi XMEML] Note: mixed native frame rates detected {sorted(unique_tbs)}. Using most common {timebase} as sequence timeline rate.")

    # (Optional note about mixed rates is logged inside the per-source probe loop below when we see varying src_timebase values)

    source_meta: dict[str, dict] = {}
    fid = 1

    for sp in ordered_sources:
        try:
            _, dsec = get_media_start_offset_and_duration(sp)
        except Exception:
            dsec = 0.0

        ainfo = get_audio_characteristics(sp)
        fname = source_paths.get(sp, Path(sp).name)

        par = "square"
        # Probe resolution + timebase (more robust command)
        try:
            res_cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "v:0", sp
            ]
            res_result = subprocess.run(res_cmd, capture_output=True, text=True, timeout=10, check=True)
            res_data = json.loads(res_result.stdout or "{}")
            w = h = r = None
            for stream in res_data.get("streams", []):
                if stream.get("codec_type") == "video":
                    w = stream.get("width")
                    h = stream.get("height")
                    r = stream.get("r_frame_rate") or stream.get("avg_frame_rate")
                    par = stream.get("sample_aspect_ratio") or "1:1"
                    break
            width = int(w) if w else 1920
            height = int(h) if h else 1080

            src_timebase = timebase
            if r and "/" in str(r):
                try:
                    num, den = map(int, str(r).split("/"))
                    if den > 0:
                        fps = num / den
                        # Match the smart NTSC rounding used by get_video_timebase
                        if abs(fps - 23.976) < 0.05 or abs(fps - 23.98) < 0.05:
                            src_timebase = 24
                        elif abs(fps - 29.97) < 0.05:
                            src_timebase = 30
                        elif abs(fps - 59.94) < 0.05:
                            src_timebase = 60
                        else:
                            src_timebase = int(round(fps))
                except Exception:
                    pass
            # Final sanity for per-source timebase
            if src_timebase > 120 or src_timebase < 1:
                src_timebase = timebase if (1 <= timebase <= 120) else 25
        except Exception:
            width, height = 1920, 1080
            src_timebase = timebase

        dur_f = _seconds_to_frames(dsec, src_timebase) if dsec > 0 else 1000

        source_meta[sp] = {
            "dur_sec": dsec,
            "dur_f": dur_f,
            "audio_ch": ainfo.get("channels", 2),
            "audio_sr": ainfo.get("sample_rate", 48000),
            "fname": fname,
            "file_id": fid,
            "width": width,
            "height": height,
            "pixel_aspect_ratio": par,
            "src_timebase": src_timebase,
            "force_zero_timecode": True,
            "timecode_string": "00:00:00:00",
            "timecode_frame": 0,
        }
        fid += 1

    return ordered_sources, source_meta, timebase


def _get_premiere_label_color(source_key: str) -> str:
    """
    Assign a stable Premiere label color based on camera/clip identity.
    Uses a hash so the same camera (or same clip filename) gets the same color
    every time you export a Director cut.
    """
    if not source_key:
        return "0"

    key = source_key.strip().lower()
    hash_val = hash(key) % 10
    return str(hash_val + 1)   # Premiere labels 1-10


# ---------------------------------------------------------------------------
# Main exported function for #2 (Multi - AI Director without narrations)
# ---------------------------------------------------------------------------

def export_ai_director_multi_xmeml(
    ver: dict[str, Any],
    *,
    generate_voiceover: bool = False,
    narration_as_titles: bool = False,
    voiceover_language: str = "en",
    voiceover_voice: str | None = None,
    output_dir: Path | None = None,
) -> Path | None:
    """
    Export an AI Director multi-source version as XMEML **without** any narration features.

    This is the dedicated, clean implementation for exporter #2:
    "Multi - AI Director (no narration)"

    Any narration/voiceover related kwargs are ignored. This function must
    never emit voiceover tracks, <title> elements, or narration bridge data.
    """
    try:
        from pathlib import Path as _P

        from minicat.ai.xmeml_exporter import get_video_timebase

        # Best-effort: re-resolve clip ranges from per-source trans .txt sidecars so that
        # even direct/scripted calls or loaded stories get the full verbatim spans for
        # combined narrative beats (same "trans is truth" contract as Journalist).
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
        # This ensures "AI DIRECTOR — MULTI-CLIP SCRIPT.txt" is always produced for AI Multi XML exports.
        try:
            from minicat.ui.app import export_ai_director_multi_clip_script
            export_ai_director_multi_clip_script(ver, target_dir=export_dir)
        except Exception:
            # UI may not be importable in all contexts (e.g. tests, scripts); safe to ignore
            pass

        source_list = list(source_paths.keys())

        if len(source_list) == 1:
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
            return out_path

        else:
            # === MIXED SOURCES — AI DIRECTOR MULTI-CAMERA EXPORT (NO NARRATION) ===
            # Delegates to the clean dedicated helper in this module (exporter #2).
            print("[Multi XMEML #2] Probing sources for resolution and audio channels...")
            ordered_sources, source_meta, timebase = prepare_director_sources(ver, source_paths)

            # Debug: show what we actually detected from the files
            for sp, meta in source_meta.items():
                print(f"  - {meta['fname']}: {meta['width']}x{meta['height']}, {meta['audio_ch']}ch, tb={meta['src_timebase']}")

            pretty_xml = _build_mixed_sources_xmeml(
                ver=ver,
                ordered_sources=ordered_sources,
                source_meta=source_meta,
                source_paths=source_paths,
                timebase=timebase,
                name=name,
            )

            out_path.write_text(pretty_xml, encoding="utf-8")
            return out_path

    except Exception as ex:
        print(f"[Multi XMEML #2] Export failed: {ex}")
        import traceback
        traceback.print_exc()
        return None
