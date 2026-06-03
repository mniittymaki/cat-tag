"""
XMEML (v5) exporter for AI Journalist Cuts.

Generates Final Cut Pro 7 / XMEML version 5 XML suitable for
Premiere Pro, DaVinci Resolve, and Final Cut Pro.

Key features for the AI cut use case:
- Single master <clipitem id="master-A"> containing the original <pathurl>
- Sequence clipitems reference it via <file id="file-master-A"/>
- Correct separation: <in>/<out> = source timecode within master,
  <start>/<end> = position on the output timeline
- Hardcoded 25 fps timebase for consistency with project settings.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from xml.dom import minidom


def seconds_to_frames(seconds: float, fps: float = 25.0) -> int:
    """
    Convert seconds to FCPXML frame count.

    Parameters
    ----------
    seconds : float
        Time in seconds (can be float).
    fps : float
        Frames per second (default 25 for PAL).

    Returns
    -------
    int
        Number of frames (rounded to nearest frame).
    """
    if seconds is None:
        return 0
    return int(round(float(seconds) * fps))


def frames_to_timecode(frames: int, fps: float = 25.0, start_timecode: str = "01:00:00:00") -> str:
    """
    Convert frame count to HH:MM:SS:FF timecode string.

    This is mostly for the <timecode> elements.
    """
    timebase = int(round(fps))
    ntsc = "FALSE"
    if abs(fps - 29.97) < 0.1:
        timebase = 30
        ntsc = "TRUE"
    elif abs(fps - 59.94) < 0.1:
        timebase = 60
        ntsc = "TRUE"

    # Parse start timecode if provided (simplified - assumes HH:MM:SS:FF)
    try:
        h, m, s, f = map(int, start_timecode.split(":"))
        start_frames = ((h * 3600 + m * 60 + s) * timebase) + f
    except Exception:
        start_frames = 0

    total_frames = start_frames + frames

    h = total_frames // (timebase * 3600)
    m = (total_frames // (timebase * 60)) % 60
    s = (total_frames // timebase) % 60
    f = total_frames % timebase

    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def create_sequence(
    cuts: list[dict[str, Any]],
    video_metadata: dict[str, Any],
    *,
    sequence_name: str | None = None,
    fps: float | None = None,
    start_timecode: str = "01:00:00:00",
    separate_sequences: bool = True,
) -> str:
    """
    Convert journalist cut suggestions into FCPXML string.

    Parameters
    ----------
    cuts
        Output from generate_journalist_cuts(). List of version dicts.
    video_metadata
        Dict containing at minimum:
            - "path": str
            - "filename": str
            - "duration": float (optional)
            - "fps": float (optional)
    separate_sequences
        If True (default): one sequence per version.
        If False: all segments from all versions go into a single combined sequence.

    Returns
    -------
    str
        Pretty-printed FCPXML.
    """
    if not cuts:
        raise ValueError("No cuts provided")

    effective_fps = fps or video_metadata.get("fps") or 25.0
    timebase = int(round(effective_fps))
    ntsc = "FALSE"
    if abs(effective_fps - 29.97) < 0.1:
        timebase = 30
        ntsc = "TRUE"
    elif abs(effective_fps - 59.94) < 0.1:
        timebase = 60
        ntsc = "TRUE"

    video_path = Path(video_metadata["path"]).resolve()
    video_filename = video_metadata.get("filename") or video_path.name
    video_duration = float(video_metadata.get("duration") or 3600.0)

    root = ET.Element("xmeml", version="5")
    project = ET.SubElement(root, "project")
    ET.SubElement(project, "name").text = sequence_name or f"Journalist Cuts - {video_filename}"
    children = ET.SubElement(project, "children")

    if separate_sequences:
        _build_per_version_sequences(
            children, cuts, video_path, video_filename, video_duration,
            sequence_name, effective_fps, timebase, ntsc, start_timecode
        )
    else:
        _build_single_combined_sequence(
            children, cuts, video_path, video_filename, video_duration,
            sequence_name, effective_fps, timebase, ntsc, start_timecode
        )

    rough = ET.tostring(root, encoding="unicode")
    reparsed = minidom.parseString(rough)
    return reparsed.toprettyxml(indent="    ")


def _build_per_version_sequences(children, cuts, video_path, video_filename,
                                  video_duration, sequence_name, effective_fps,
                                  timebase, ntsc, start_timecode):
    for version in cuts:
        version_id = version.get("version_id", "A")
        title = version.get("title", f"Cut {version_id}")
        seq_name = sequence_name or f"{title} - {video_filename}"
        selected_segments = version.get("selected_segments", [])
        if not selected_segments:
            continue

        def _seg_duration(s):
            out_t = s.get("source_out") or s.get("end", 0)
            in_t = s.get("source_in") or s.get("start", 0)
            return float(out_t) - float(in_t)

        total_frames = sum(
            seconds_to_frames(_seg_duration(s), effective_fps)
            for s in selected_segments
        )

        sequence = ET.SubElement(children, "sequence", id=f"seq-{version_id}")
        ET.SubElement(sequence, "name").text = seq_name
        ET.SubElement(sequence, "duration").text = str(total_frames)

        rate = ET.SubElement(sequence, "rate")
        ET.SubElement(rate, "timebase").text = str(timebase)
        ET.SubElement(rate, "ntsc").text = ntsc

        media = ET.SubElement(sequence, "media")
        video = ET.SubElement(media, "video")
        track = ET.SubElement(video, "track")

        master_id = f"file-master-{version_id}"
        _add_master_file_reference(children, master_id, video_filename, video_path,
                                   video_duration, effective_fps, timebase, ntsc, start_timecode)

        current_start = 0
        for i, seg in enumerate(selected_segments, 1):
            start_f = seconds_to_frames(float(seg.get("source_in") or seg.get("start", 0)), effective_fps)
            end_f = seconds_to_frames(float(seg.get("source_out") or seg.get("end", 0)), effective_fps)
            dur = end_f - start_f
            if dur <= 0:
                continue

            clip = ET.SubElement(track, "clipitem", id=f"clipitem-{version_id}-{i}")
            ET.SubElement(clip, "name").text = f"{title} - {i}"
            ET.SubElement(clip, "duration").text = str(dur)

            r = ET.SubElement(clip, "rate")
            ET.SubElement(r, "timebase").text = str(timebase)
            ET.SubElement(r, "ntsc").text = ntsc

            ET.SubElement(clip, "start").text = str(current_start)
            ET.SubElement(clip, "end").text = str(current_start + dur)
            ET.SubElement(clip, "in").text = str(start_f)
            ET.SubElement(clip, "out").text = str(end_f)

            ET.SubElement(clip, "file", id=master_id)

            if "reason" in seg:
                ET.SubElement(clip, "comment").text = seg["reason"][:200]

            current_start += dur

        sequence.find("duration").text = str(current_start)


def _build_single_combined_sequence(children, cuts, video_path, video_filename,
                                     video_duration, sequence_name, effective_fps,
                                     timebase, ntsc, start_timecode):
    all_segs = []
    for ver in cuts:
        for s in ver.get("selected_segments", []):
            s = dict(s)
            s["_title"] = ver.get("title", ver.get("version_id", "Cut"))
            all_segs.append(s)

    if not all_segs:
        return

    total_frames = sum(
        seconds_to_frames(float(s["end"]) - float(s["start"]), effective_fps)
        for s in all_segs
    )

    seq_name = sequence_name or f"Combined Journalist Cuts - {video_filename}"
    sequence = ET.SubElement(children, "sequence", id="seq-combined")
    ET.SubElement(sequence, "name").text = seq_name
    ET.SubElement(sequence, "duration").text = str(total_frames)

    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = str(timebase)
    ET.SubElement(rate, "ntsc").text = ntsc

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    track = ET.SubElement(video, "track")

    master_id = "file-master-combined"
    _add_master_file_reference(children, master_id, video_filename, video_path,
                               video_duration, effective_fps, timebase, ntsc, start_timecode)

    current_start = 0
    for i, seg in enumerate(all_segs, 1):
        start_f = seconds_to_frames(float(seg.get("source_in") or seg.get("start", 0)), effective_fps)
        end_f = seconds_to_frames(float(seg.get("source_out") or seg.get("end", 0)), effective_fps)
        dur = end_f - start_f
        if dur <= 0:
            continue

        clip = ET.SubElement(track, "clipitem", id=f"clipitem-combined-{i}")
        ET.SubElement(clip, "name").text = f"{seg['_title']} - {i}"
        ET.SubElement(clip, "duration").text = str(dur)

        r = ET.SubElement(clip, "rate")
        ET.SubElement(r, "timebase").text = str(timebase)
        ET.SubElement(r, "ntsc").text = ntsc

        ET.SubElement(clip, "start").text = str(current_start)
        ET.SubElement(clip, "end").text = str(current_start + dur)
        ET.SubElement(clip, "in").text = str(start_f)
        ET.SubElement(clip, "out").text = str(end_f)

        ET.SubElement(clip, "file", id=master_id)

        if "reason" in seg:
            ET.SubElement(clip, "comment").text = seg["reason"][:200]

        current_start += dur

    sequence.find("duration").text = str(current_start)


def _add_master_file_reference(children, master_id, filename, path, duration,
                               fps, timebase, ntsc, start_tc):
    master = ET.SubElement(children, "clipitem", id=master_id.replace("file-", ""))
    ET.SubElement(master, "name").text = filename
    ET.SubElement(master, "duration").text = str(seconds_to_frames(duration, fps))

    r = ET.SubElement(master, "rate")
    ET.SubElement(r, "timebase").text = str(timebase)
    ET.SubElement(r, "ntsc").text = ntsc

    f = ET.SubElement(master, "file", id=master_id)
    ET.SubElement(f, "name").text = filename
    ET.SubElement(f, "pathurl").text = "file://" + str(Path(path).resolve()).replace("\\", "/")

    reel = ET.SubElement(f, "reel")
    ET.SubElement(reel, "name").text = Path(filename).stem[:32]

    tc = ET.SubElement(f, "timecode")
    ET.SubElement(tc, "string").text = start_tc
    r2 = ET.SubElement(tc, "rate")
    ET.SubElement(r2, "timebase").text = str(timebase)
    ET.SubElement(r2, "ntsc").text = ntsc


def create_sequence_from_video(
    video: "Video",
    cuts: list[dict[str, Any]],
    **kwargs
) -> str:
    """Convenience wrapper that accepts a Video object directly."""
    metadata = {
        "path": getattr(video, "path", ""),
        "filename": getattr(video, "filename", "unknown.mov"),
        "duration": getattr(video, "duration", None),
        "fps": getattr(video, "fps", None),
    }
    return create_sequence(cuts, metadata, **kwargs)


# Convenience alias
create_fcpxml = create_sequence

# Re-export the strict Premiere-oriented XMEML generator from the dedicated module
# so that existing imports from minicat.ai.fcpxml_exporter continue to work.
from .xmeml_exporter import generate_xmeml, create_xmeml  # noqa: F401


# ---------------------------------------------------------------------------
# Legacy / general-purpose FCPXML (XMEML) builders for multi-version exports
# (kept for backward compatibility with the older combined/per-version flows)
# ---------------------------------------------------------------------------

# generate_xmeml and create_xmeml are re-exported from .xmeml_exporter above.
# The old implementation has been moved to minicat/ai/xmeml_exporter.py
# for stricter Premiere Pro compatibility.
