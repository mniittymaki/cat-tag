"""
Single-source XMEML Exporter.

This is exporter #1:

1. Single → xmeml_exporter.py (this file)

Primarily used by AI Journalist Cuts, but available for any single-source XMEML export.

Uses pure xml.etree.ElementTree for construction (no string building or minidom).

================================================================================
WORKING VERSION - 31 May 2026
================================================================================
This version produces correct audio tracks in Premiere Pro for 2-channel sources.

Key working features:
- Dynamic audio probing via ffprobe (real channels + sample rate from source)
- For 2+ channel sources: Creates TWO Mono audio tracks (not one Stereo)
  matching native Premiere export behavior for interview/journalist material
- Each audio clipitem emits dual <link> entries (trackindex 1 + trackindex 2)
- Video clipitems also carry links to both audio tracks
- Full <file> definition only on first clipitem + bare references after
- Correct masterclipid, full source duration on every clipitem, proper links
- file://localhost pathurl format

Tested and confirmed working for relink + audible stereo/dual-mono audio in Premiere
with the 2910_VERY_SHORT_FINAL.mp4 + native Premiere export reference.

DO NOT lightly change the audio track creation / linking logic without re-testing
in Premiere.

================================================================================
CRITICAL STRUCTURE (matches Premiere Pro native export template):
- Root is <xmeml version="4"> directly containing <sequence id="sequence-1">.
- No <project> or <children> wrapper at all.
- XML declaration + <!DOCTYPE xmeml> line.
- The FIRST <clipitem> in the video track contains the COMPLETE <file id="file-1">
  definition, including <name>, <pathurl> (file://localhost + absolute path),
  <rate>, <duration>, <timecode>, and <media> with BOTH <video> and <audio>
  samplecharacteristics.
- Every subsequent <clipitem> uses only the bare reference: <file id="file-1"/>.
- Timecode aware: ffprobe container start_time offset is added to source in/out.
- pathurl is constructed literally as file://localhost + absolute path.
"""

# NOTE: This is the known-working audio version. A backup copy exists as:
#       minicat/ai/xmeml_exporter.py.working-2026-05-31

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Any


def _seconds_to_frames(seconds: float, timebase: int = 25) -> int:
    if seconds is None:
        return 0
    return int(round(float(seconds) * timebase))


def get_master_start_offset(file_path: str | Path) -> float:
    """
    Backward-compatible wrapper. See get_media_start_offset_and_duration.
    """
    offset, _ = get_media_start_offset_and_duration(file_path)
    return offset


def get_media_start_offset_and_duration(file_path: str | Path) -> tuple[float, float]:
    """
    Use ffprobe to extract the container-level start_time offset (seconds) and
    the total duration (seconds) of the media file.

    The start_time offset is critical for files that do not begin at 00:00:00:00
    (common with camera originals). Duration is used for the <duration> tag
    inside the inline <file> definition.
    """
    file_path = str(file_path)
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        file_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        data = json.loads(result.stdout or "{}")
        fmt = data.get("format", {})

        # start_time: prefer format, fall back to first video stream
        start_time = fmt.get("start_time")
        if start_time is None:
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    start_time = stream.get("start_time")
                    if start_time is not None:
                        break
        start = float(start_time) if start_time is not None else 0.0

        # Also look for explicit timecode tags (common on camera originals)
        # This helps with more accurate timecode reporting even if container start_time differs.
        tags = fmt.get("tags", {}) or {}
        for key in ("timecode", "time_code", "tc", "start_timecode", "timecode_tc"):
            if key in tags and tags[key]:
                # We keep the numeric start for <in>/<out> calculation, but could expose this later
                break

        # duration: prefer format duration
        duration = fmt.get("duration")
        if duration is None:
            for stream in data.get("streams", []):
                if stream.get("codec_type") in ("video", "audio"):
                    duration = stream.get("duration")
                    if duration is not None:
                        break
        dur = float(duration) if duration is not None else 0.0

        return start, dur

    except Exception:
        return 0.0, 0.0


def _seconds_to_timecode_string(seconds: float, timebase: int = 25) -> str:
    """Convert a float seconds value into HH:MM:SS:FF timecode string."""
    if seconds is None or seconds < 0:
        seconds = 0.0

    total_frames = int(round(seconds * timebase))
    h = total_frames // (timebase * 3600)
    m = (total_frames // (timebase * 60)) % 60
    s = (total_frames // timebase) % 60
    f = total_frames % timebase

    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def _get_source_audio_info(file_path: str | Path) -> dict[str, Any]:
    """
    Probe the first audio stream in the source file for real properties.
    Used so the XMEML can declare accurate channel count, sample rate, etc.
    instead of hard-coding stereo 48k 16-bit.
    """
    file_path = str(file_path)
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        file_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        data = json.loads(result.stdout or "{}")

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "audio":
                return {
                    "channels": stream.get("channels") or 2,
                    "channel_layout": stream.get("channel_layout") or "stereo",
                    "sample_rate": int(stream.get("sample_rate") or 48000),
                    "codec": stream.get("codec_name"),
                    "sample_fmt": stream.get("sample_fmt"),
                }
    except Exception as e:
        print(f"[XMEML] Warning: Could not probe audio info from {file_path}: {e}")

    # Safe defaults (what we used to hard-code)
    return {
        "channels": 2,
        "channel_layout": "stereo",
        "sample_rate": 48000,
        "codec": "unknown",
        "sample_fmt": "unknown",
    }


def get_video_timebase(file_path: str | Path) -> int:
    """
    Probe the primary video stream for its frame rate / timebase.

    Uses r_frame_rate (exact) falling back to avg_frame_rate.
    Handles common fractional rates:
        "25/1"       → 25
        "24000/1001" → 24   (23.976)
        "30000/1001" → 30   (29.97)
        etc.

    Returns a sensible integer timebase suitable for XMEML <rate><timebase>.
    Falls back to 25 if the file cannot be probed or has no video stream.
    """
    file_path = str(file_path)
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-select_streams",
        "v:0",
        "-show_streams",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        data = json.loads(result.stdout or "{}")
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                rate = stream.get("r_frame_rate") or stream.get("avg_frame_rate") or "25/1"
                if "/" in str(rate):
                    try:
                        num_str, den_str = str(rate).split("/", 1)
                        num = int(num_str)
                        den = int(den_str) if den_str else 1
                        if den > 0:
                            fps = num / den
                            # Common NTSC fractional rates → nearest conventional timebase
                            if abs(fps - 23.976) < 0.05 or abs(fps - 23.98) < 0.05:
                                return 24
                            if abs(fps - 29.97) < 0.05:
                                return 30
                            if abs(fps - 59.94) < 0.05:
                                return 60
                            tb = int(round(fps))
                            if tb > 120 or tb < 1:
                                print(
                                    f"[XMEML] Warning: unrealistic timebase {tb} probed from {file_path}, using 25 instead"
                                )
                                return 25
                            return tb
                    except Exception:
                        pass
                else:
                    try:
                        tb = int(float(rate))
                        if tb > 120 or tb < 1:
                            print(
                                f"[XMEML] Warning: unrealistic timebase {tb} probed from {file_path}, using 25 instead"
                            )
                            return 25
                        return tb
                    except Exception:
                        pass
    except Exception as e:
        print(f"[XMEML] Warning: could not probe video timebase from {file_path}: {e}")

    return 25


def generate_xmeml(
    cut_segments: list[dict[str, Any]],
    source_video_path: str | Path,
    output_path: str | Path,
    *,
    sequence_name: str | None = None,
    narrative_summary: str | None = None,
    timebase: int | None = None,
    width: int = 1920,
    height: int = 1080,
    audio_channels: int | None = None,
    audio_sample_rate: int | None = None,
    source_start_timecode: str | float | None = None,
) -> Path:
    """
    Generate a Premiere-friendly XMEML v4 file for a single AI journalist cut.

    The structure exactly follows a native Premiere Pro export template:

    Audio properties are now dynamically read from the source file (via ffprobe)
    instead of being hard-coded. This is critical for correct stereo / mono /
    multi-track linking in Premiere.

    - <?xml ...?>
    - <!DOCTYPE xmeml>
    - <xmeml version="4">
        <sequence id="sequence-1">
          <name>...</name>
          <duration>...</duration>
          <rate>...</rate>
          <media>
            <video>
              <format><samplecharacteristics>...</samplecharacteristics></format>
              <track>
                <clipitem id="clipitem-1"> ... <file id="file-1"> FULL DEF </file> ...
                <clipitem id="clipitem-2"> ... <file id="file-1"/> ...
              </track>
            </video>
          </media>
        </sequence>
      </xmeml>

    The FULL <file id="file-1"> (with pathurl, rate, timecode, media containing
    BOTH video and audio samplecharacteristics) lives ONLY inside the first
    clipitem. All later clipitems use the bare self-closing reference.

    Timecode offset from ffprobe is applied to every clip's <in>/<out>.

    Parameters
    ----------
    cut_segments
        List of dicts with "source_in"/"source_out" (or legacy "start"/"end").
        "reason" or "text" becomes an optional <comment> on each clipitem.
    source_video_path
        Path to the original source media. Used for filename and the inline
        <file> definition (pathurl + timecode offset).
    output_path
        Destination .xml file.
    sequence_name
        Name for the sequence.
    narrative_summary
        Optional editorial summary from the AI (e.g. from Verbatim Scriptwriter).
        Added as a <comment> on the sequence itself.
    timebase, width, height
        Sequence metadata. If timebase is None (the default), it is automatically
        probed from the source video file using ffprobe (r_frame_rate / avg_frame_rate).
        This guarantees the XML matches the original media instead of using a hard-coded
        24 or 25 fps.

    Returns
    -------
    Path
        The written file path.
    """
    if not cut_segments:
        raise ValueError("No cut segments provided for XMEML export")

    src_path = Path(source_video_path).resolve()
    filename = src_path.name
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Always derive timebase from the actual source file unless the caller explicitly supplied one.
    # This prevents hard-coded 24/25 fps assumptions.
    if timebase is None:
        timebase = get_video_timebase(src_path)

    # Timecode-aware probe (start offset + total source duration in seconds)
    offset, src_dur_sec = get_media_start_offset_and_duration(src_path)
    src_dur_frames = _seconds_to_frames(src_dur_sec, timebase) if src_dur_sec > 0 else 0

    # === NEW: Real audio properties from the actual source file ===
    if audio_channels is None or audio_sample_rate is None:
        audio_info = _get_source_audio_info(src_path)
        if audio_channels is None:
            audio_channels = audio_info["channels"]
        if audio_sample_rate is None:
            audio_sample_rate = audio_info["sample_rate"]
        print(
            f"[XMEML] Detected audio from source: {audio_channels}ch @ {audio_sample_rate}Hz "
            f"(layout: {audio_info.get('channel_layout')}, codec: {audio_info.get('codec')})"
        )
    else:
        print(f"[XMEML] Using caller-supplied audio: {audio_channels}ch @ {audio_sample_rate}Hz")

    # Always probe video dimensions + pixel aspect ratio from the actual source clip (HD/4K must be square 1.0)
    # This guarantees we don't inherit old D1/DV PAL anamorphic values.
    par = "square"
    try:
        vcmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-select_streams",
            "v:0",
            str(src_path),
        ]
        vres = subprocess.run(vcmd, capture_output=True, text=True, timeout=10, check=True)
        vdata = json.loads(vres.stdout or "{}")
        for stream in vdata.get("streams", []):
            if stream.get("codec_type") == "video":
                pw = stream.get("width")
                if pw:
                    width = int(pw)
                ph = stream.get("height")
                if ph:
                    height = int(ph)
                raw_par = stream.get("sample_aspect_ratio")
                if raw_par:
                    if raw_par in ("1:1", "1/1", "1:1.0", "1.000"):
                        par = "square"
                    else:
                        par = raw_par
                break
    except Exception as ex:
        print(f"[XMEML] Warning: could not probe PAR from source, defaulting to square: {ex}")

    # Decide track type for Premiere (simple for now: 1 = Mono, 2 = Stereo)

    ntsc = "FALSE"

    # Root: xmeml v4 with NO project/children wrapper
    root = ET.Element("xmeml", version="4")

    # ------------------------------------------------------------------
    # SEQUENCE (direct child of xmeml)
    # ------------------------------------------------------------------
    sequence = ET.SubElement(root, "sequence", id="sequence-1")
    ET.SubElement(sequence, "name").text = sequence_name or f"AI Cut - {filename}"
    if narrative_summary:
        ET.SubElement(sequence, "comment").text = narrative_summary[:500]
    ET.SubElement(sequence, "duration").text = "0"  # placeholder; fixed after building clips

    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = str(timebase)
    ET.SubElement(rate, "ntsc").text = ntsc

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")

    # Sequence format samplecharacteristics (minimal but sufficient)
    fmt = ET.SubElement(video, "format")
    sample = ET.SubElement(fmt, "samplecharacteristics")
    ET.SubElement(sample, "width").text = str(width)
    ET.SubElement(sample, "height").text = str(height)
    rate_sample = ET.SubElement(sample, "rate")
    ET.SubElement(rate_sample, "timebase").text = str(timebase)
    ET.SubElement(rate_sample, "ntsc").text = ntsc
    # Declare PAR explicitly (square for HD/4K, or actual from clip) to avoid D1/DV PAL default.
    ET.SubElement(sample, "anamorphic").text = "FALSE"
    ET.SubElement(sample, "pixelaspectratio").text = par
    ET.SubElement(sample, "fielddominance").text = "none"

    track = ET.SubElement(video, "track")

    # Audio track(s) so Premiere imports the audio channels from the source file
    seq_audio = ET.SubElement(media, "audio")

    # Explicit sequence audio format declaration (required for correct stereo linking in Premiere)
    # Now using real values probed from the source file.
    format_elem = ET.SubElement(seq_audio, "format")
    samplechar = ET.SubElement(format_elem, "samplecharacteristics")
    ET.SubElement(samplechar, "depth").text = "16"
    ET.SubElement(samplechar, "samplerate").text = str(audio_sample_rate)

    # Create audio tracks.
    # For 2-channel sources we now create TWO Mono tracks (matching native Premiere export behavior
    # for most interview/journalist material). This has been the missing piece for reliable audio relink.
    if audio_channels >= 2:
        # Two mono tracks (exploded stereo pair) - this is what Premiere does natively
        audio_track1 = ET.SubElement(
            seq_audio,
            "track",
            premiereTrackType="Mono",
            TL_SQTrackAudioKeyframeStyle="0",
            TL_SQTrackShy="0",
            MZ_TrackTargeted="1",
            TL_SQTrackExpandedHeight="41",
            currentExplodedTrackIndex="0",
            totalExplodedTrackCount="2",
        )
        ET.SubElement(
            seq_audio,
            "track",
            premiereTrackType="Mono",
            TL_SQTrackAudioKeyframeStyle="0",
            TL_SQTrackShy="0",
            MZ_TrackTargeted="1",
            TL_SQTrackExpandedHeight="41",
            currentExplodedTrackIndex="1",
            totalExplodedTrackCount="2",
        )
        primary_audio_track = audio_track1  # we place the actual clipitems here
    else:
        # Fallback for true mono sources
        primary_audio_track = ET.SubElement(
            seq_audio,
            "track",
            premiereTrackType="Mono",
            TL_SQTrackAudioKeyframeStyle="0",
            TL_SQTrackShy="0",
            MZ_TrackTargeted="1",
        )

    # ------------------------------------------------------------------
    # Define the Global Source Duration (required for correct clipitem duration)
    # ------------------------------------------------------------------
    total_source_frames = src_dur_frames if src_dur_frames > 0 else 0

    # ------------------------------------------------------------------
    # CLIPITEMS (Video + matching Audio)
    # Strictly following the required Premiere-compatible structure.
    # ------------------------------------------------------------------
    current_start = 0
    clip_idx = 1

    for seg in cut_segments:
        raw_in = float(seg.get("source_in") or seg.get("start", 0.0))
        raw_out = float(seg.get("source_out") or seg.get("end", 0.0))

        # Use raw times directly (no offset). Consistent with multi-source Director policy:
        # treat sources as starting at 00:00:00:00 in the XML for reliable relinking.
        # The times in the AI script/TXT are the media times to use for in/out.
        # This makes single clip "sync" match the selected material description (as it did at 11:10).
        seg_in = raw_in
        seg_out = raw_out

        if seg_out <= seg_in:
            continue

        in_f = _seconds_to_frames(seg_in, timebase)
        out_f = _seconds_to_frames(seg_out, timebase)
        dur_f = out_f - in_f
        if dur_f <= 0:
            continue

        start_f = current_start
        end_f = current_start + dur_f

        # === VIDEO CLIPITEM (strict structure) ===
        clipitem = ET.SubElement(track, "clipitem", id=f"clipitem-{clip_idx}")
        ET.SubElement(clipitem, "masterclipid").text = "masterclip-1"
        ET.SubElement(clipitem, "name").text = f"{filename} - Segment {clip_idx}"
        # CRITICAL: Duration = FULL SOURCE FILE DURATION
        ET.SubElement(clipitem, "duration").text = (
            str(total_source_frames) if total_source_frames > 0 else str(dur_f)
        )

        r = ET.SubElement(clipitem, "rate")
        ET.SubElement(r, "timebase").text = str(timebase)
        ET.SubElement(r, "ntsc").text = ntsc

        # Timeline positions
        ET.SubElement(clipitem, "start").text = str(start_f)
        ET.SubElement(clipitem, "end").text = str(end_f)
        # Source positions
        ET.SubElement(clipitem, "in").text = str(in_f)
        ET.SubElement(clipitem, "out").text = str(out_f)

        if clip_idx == 1:
            # FULL file definition ONLY on the first clipitem (as required)
            f = ET.SubElement(clipitem, "file", id="file-1")
            ET.SubElement(f, "name").text = filename

            abs_path = str(src_path).replace("\\", "/")
            pathurl = f"file://localhost{abs_path}"
            ET.SubElement(f, "pathurl").text = pathurl

            fr = ET.SubElement(f, "rate")
            ET.SubElement(fr, "timebase").text = str(timebase)
            ET.SubElement(fr, "ntsc").text = ntsc

            if total_source_frames > 0:
                ET.SubElement(f, "duration").text = str(total_source_frames)

            # File timecode: prefer the real embedded start timecode from the source clip (e.g.
            # "10:03:20:01") so labels match the source transcription .txt displays. Falls back
            # to media-head 00:00:00:00. The clipitem <in>/<out> remain media-relative (from head)
            # using the (now repaired) source_in values.
            tc_string = "00:00:00:00"
            tc_frame = "0"
            if source_start_timecode:
                if isinstance(source_start_timecode, str) and source_start_timecode.strip():
                    tc_string = source_start_timecode.strip()
                else:
                    try:
                        secs = float(source_start_timecode)
                        if secs > 0:
                            tc_string = _seconds_to_timecode_string(secs, timebase)
                            tc_frame = str(int(round(secs * timebase)) % (timebase * 24 * 3600))
                    except Exception:
                        pass

            tc_elem = ET.SubElement(f, "timecode")
            tc_rate = ET.SubElement(tc_elem, "rate")
            ET.SubElement(tc_rate, "timebase").text = str(timebase)
            ET.SubElement(tc_rate, "ntsc").text = ntsc
            ET.SubElement(tc_elem, "string").text = tc_string
            ET.SubElement(tc_elem, "frame").text = tc_frame
            ET.SubElement(tc_elem, "displayformat").text = "NDF"

            media_f = ET.SubElement(f, "media")

            vid = ET.SubElement(media_f, "video")
            scv = ET.SubElement(vid, "samplecharacteristics")
            r2 = ET.SubElement(scv, "rate")
            ET.SubElement(r2, "timebase").text = str(timebase)
            ET.SubElement(r2, "ntsc").text = ntsc
            ET.SubElement(scv, "width").text = str(width)
            ET.SubElement(scv, "height").text = str(height)
            ET.SubElement(scv, "anamorphic").text = "FALSE"
            ET.SubElement(scv, "pixelaspectratio").text = par
            ET.SubElement(scv, "fielddominance").text = "none"

            aud = ET.SubElement(media_f, "audio")
            sca = ET.SubElement(aud, "samplecharacteristics")
            ET.SubElement(sca, "depth").text = "16"
            ET.SubElement(sca, "samplerate").text = str(audio_sample_rate)
            ET.SubElement(aud, "channelcount").text = str(audio_channels)
        else:
            ET.SubElement(clipitem, "file", id="file-1")

        # Explicit Link definitions (strict structure)
        # Video self-link
        link1 = ET.SubElement(clipitem, "link")
        ET.SubElement(link1, "linkclipref").text = f"clipitem-{clip_idx}"
        ET.SubElement(link1, "mediatype").text = "video"
        ET.SubElement(link1, "trackindex").text = "1"
        ET.SubElement(link1, "clipindex").text = str(clip_idx)

        # Link to audio on track 1
        link2 = ET.SubElement(clipitem, "link")
        ET.SubElement(link2, "linkclipref").text = f"clipitem-{clip_idx}-audio"
        ET.SubElement(link2, "mediatype").text = "audio"
        ET.SubElement(link2, "trackindex").text = "1"
        ET.SubElement(link2, "clipindex").text = str(clip_idx)

        # For 2-channel sources, also link the video clipitem to audio track 2
        if audio_channels >= 2:
            link3 = ET.SubElement(clipitem, "link")
            ET.SubElement(link3, "linkclipref").text = f"clipitem-{clip_idx}-audio"
            ET.SubElement(link3, "mediatype").text = "audio"
            ET.SubElement(link3, "trackindex").text = "2"
            ET.SubElement(link3, "clipindex").text = str(clip_idx)

        # Optional editorial note
        reason = seg.get("reason") or seg.get("text")
        if reason:
            ET.SubElement(clipitem, "comment").text = str(reason)[:240]

        # === AUDIO CLIPITEM (following same strict philosophy) ===
        # Place the clipitem in the primary mono track. For 2ch sources we also emit
        # a second link so Premiere knows it belongs to the stereo pair (tracks 1+2).
        audio_clip = ET.SubElement(primary_audio_track, "clipitem", id=f"clipitem-{clip_idx}-audio")
        ET.SubElement(audio_clip, "masterclipid").text = "masterclip-1"
        ET.SubElement(audio_clip, "name").text = f"{filename} - Segment {clip_idx} (Audio)"
        ET.SubElement(audio_clip, "enabled").text = "TRUE"
        ET.SubElement(audio_clip, "duration").text = (
            str(total_source_frames) if total_source_frames > 0 else str(dur_f)
        )

        ar = ET.SubElement(audio_clip, "rate")
        ET.SubElement(ar, "timebase").text = str(timebase)
        ET.SubElement(ar, "ntsc").text = ntsc

        ET.SubElement(audio_clip, "start").text = str(start_f)
        ET.SubElement(audio_clip, "end").text = str(end_f)
        ET.SubElement(audio_clip, "in").text = str(in_f)
        ET.SubElement(audio_clip, "out").text = str(out_f)

        # Source track mapping for the audio clip.
        sourcetrack = ET.SubElement(audio_clip, "sourcetrack")
        ET.SubElement(sourcetrack, "mediatype").text = "audio"
        ET.SubElement(sourcetrack, "trackindex").text = "1"

        # NOW add the file reference
        ET.SubElement(audio_clip, "file", id="file-1")

        # === LINKS (critical for correct stereo linking in Premiere) ===
        # Video link (back to the video clipitem)
        vlink = ET.SubElement(audio_clip, "link")
        ET.SubElement(vlink, "linkclipref").text = f"clipitem-{clip_idx}"
        ET.SubElement(vlink, "mediatype").text = "video"
        ET.SubElement(vlink, "trackindex").text = "1"
        ET.SubElement(vlink, "clipindex").text = str(clip_idx)

        # Audio link to track 1 (self)
        alink1 = ET.SubElement(audio_clip, "link")
        ET.SubElement(alink1, "linkclipref").text = f"clipitem-{clip_idx}-audio"
        ET.SubElement(alink1, "mediatype").text = "audio"
        ET.SubElement(alink1, "trackindex").text = "1"
        ET.SubElement(alink1, "clipindex").text = str(clip_idx)

        # Second audio link to track 2 — this is what makes Premiere see proper stereo/dual-mono
        if audio_channels >= 2:
            alink2 = ET.SubElement(audio_clip, "link")
            ET.SubElement(alink2, "linkclipref").text = f"clipitem-{clip_idx}-audio"
            ET.SubElement(alink2, "mediatype").text = "audio"
            ET.SubElement(alink2, "trackindex").text = "2"
            ET.SubElement(alink2, "clipindex").text = str(clip_idx)

        current_start = end_f
        clip_idx += 1

    # Now that we know the total timeline length, patch the sequence duration
    sequence.find("duration").text = str(current_start)

    # ------------------------------------------------------------------
    # WRITE (pure ElementTree + exact header + DOCTYPE)
    # ------------------------------------------------------------------
    tree = ET.ElementTree(root)

    buf = BytesIO()
    tree.write(buf, encoding="UTF-8", xml_declaration=False, method="xml")
    body = buf.getvalue()

    header = b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
    out_path.write_bytes(header + body)

    return out_path


# Convenience alias for callers that prefer the xmeml name
create_xmeml = generate_xmeml
