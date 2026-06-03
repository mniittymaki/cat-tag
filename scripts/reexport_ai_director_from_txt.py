#!/usr/bin/env python3
"""
Helper to re-export a clean AI Director multi XMEML from an exported "MultiClip_*.txt" script
+ one of the app's original AI_Multi_*.xml files (used only to recover the exact C-label → source file
mapping that the Director version used for that generation).

This is extremely useful for incremental testing (add 1 camera at a time) and for repairing cases
where the model produced bad source_in/out numbers for some cameras.

Usage examples:
  python scripts/reexport_ai_director_from_txt.py \
      --txt "/path/to/MultiClip_A_....txt" \
      --mapping-xml "/path/to/AI_Multi_A_....xml" \
      --output "/path/to/AI_Multi_A_3cameras_REEXPORTED.xml"

  # With transcription repair (finds the *real* time of each script text in the actual media):
  python scripts/reexport_ai_director_from_txt.py \
      --txt ... --mapping-xml ... --repair-times --language fi

Requirements for --repair-times:
  - ffmpeg in PATH (to extract audio)
  - GEMINI_API_KEY available (via .env or stored preference)
  - The source files must be accessible at the paths recorded in the mapping XML.

The script will:
  1. Parse the TXT (correctly handling MM:SS.ss vs HH:MM:SS.ss printed format).
  2. Extract C-label → absolute file path from the mapping XML.
  3. (Optional) For each used camera, re-transcribe its source and match the exact script texts
     to recover the true media times, overriding any bad model-provided times.
  4. Feed the (possibly repaired) segments to the current fixed _build_mixed_sources_xmeml.
  5. Write the resulting XML (you still get the nice debug prints showing which fids used
     DIRECT vs PACK and why).

This lets you see exactly when/why problems appear as you add cameras.
"""

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict
from pathlib import Path

# Make sure we import from the local source tree
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from minicat.ai.multi_xmeml_exporter import (  # noqa: E402
    _build_mixed_sources_xmeml,
    prepare_director_sources,
)
from minicat.ai.transcriber import transcribe_audio_with_timestamps  # noqa: E402
from minicat.core.settings import get_gemini_api_key  # noqa: E402


def parse_time_to_seconds(tstr: str) -> float:
    """Parse a time string exactly as the app's TXT exporter prints it.

    The exporter's fmt() does:
        if h > 0:  "HH:MM:SS.ss"
        else:      "MM:SS.ss"
    So we use the number of ':' characters to decide.
    """
    tstr = tstr.strip()
    if "→" in tstr:
        tstr = tstr.split("→")[0].strip()
    num_colons = tstr.count(":")
    parts = re.split(r"[:.]", tstr)
    vals = [float(p) for p in parts if p.replace(".", "").isdigit()]
    if not vals:
        return 0.0
    if num_colons >= 2:
        h = vals[0] if len(vals) > 0 else 0
        m = vals[1] if len(vals) > 1 else 0
        s = vals[2] if len(vals) > 2 else 0
    else:
        h = 0
        m = vals[0] if len(vals) > 0 else 0
        s = vals[1] if len(vals) > 1 else 0
    return h * 3600 + m * 60 + s


def parse_txt_clips(txt_path: Path):
    """Return list of clip dicts in the order they appear in the narrative."""
    content = txt_path.read_text(encoding="utf-8", errors="replace")
    clips = []
    header_re = re.compile(
        r"^\s*(\d+)\.\s+\[([^\]]+)\]\s+\(([0-9.]+)s\)\s*—\s*from\s+(C\d+)\s*$",
        re.MULTILINE,
    )
    headers = list(header_re.finditer(content))
    for i, m in enumerate(headers):
        _ = int(m.group(1))
        time_str = m.group(2)
        dur_s = float(m.group(3))
        cl = m.group(4)
        start_pos = m.end()
        end_pos = headers[i + 1].start() if (i + 1) < len(headers) else len(content)
        block = content[start_pos:end_pos]
        text_m = re.search(r"Text:\s*(.+?)(?:\n\s*Why chosen:|$)", block, re.DOTALL)
        txt = (text_m.group(1).strip() if text_m else "").replace("\n", " ").strip()
        reason_m = re.search(r"Why chosen:\s*(.+?)(?:\n\s*🎙️|$)", block, re.DOTALL)
        reason = (
            reason_m.group(1).strip()
            if reason_m
            else "Selected for its contribution to the overall narrative arc across sources."
        )
        reason = reason.replace("\n", " ").strip()
        raw_in = parse_time_to_seconds(time_str)
        raw_out = raw_in + max(0.1, dur_s)
        clips.append(
            {
                "source_label": cl,
                "source_in": round(raw_in, 2),
                "source_out": round(raw_out, 2),
                "text": txt,
                "reason": reason,
                "dur": dur_s,
            }
        )
    return clips


def extract_c_to_path_from_xml(xml_path: Path):
    """Return OrderedDict mapping 'C1' → absolute filesystem path for the first clip using that label."""
    tree = ET.parse(xml_path)  # type: ignore
    root = tree.getroot()
    c_to_path: OrderedDict[str, str] = OrderedDict()
    for ci in root.findall(".//video/track/clipitem"):
        name = ci.findtext("name", "")
        m = re.search(r"^(C\d+) - (.+\.MP4)", name)
        if not m:
            continue
        cl = m.group(1)
        if cl in c_to_path:
            continue
        f = ci.find("file")
        if f is None:
            continue
        pathurl = f.findtext("pathurl", "")
        if pathurl.startswith("file://localhost"):
            p = pathurl[len("file://localhost") :]
            c_to_path[cl] = p
        elif pathurl.startswith("file:///"):
            p = pathurl[len("file://") :]
            c_to_path[cl] = p
        else:
            c_to_path[cl] = pathurl
    return c_to_path


def get_transcript_segments(video_path: Path, language: str | None = None):
    """Return list of {'start': float, 'end': float, 'text': str} for the video.

    Uses a simple /tmp cache keyed by the video stem.
    Requires ffmpeg and a valid Gemini key (via get_gemini_api_key).
    """
    cache = Path("/tmp") / f"reexport_transcript_{video_path.stem}.json"
    if cache.exists():
        try:
            with open(cache) as f:
                data = json.load(f)
                return data.get("segments", [])
        except Exception:
            pass

    print(f"[re-export] Extracting audio + transcribing {video_path.name} ...")
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        audio = tmp / "audio.wav"
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(audio),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"[re-export] ffmpeg failed for {video_path.name}: {res.stderr[:300]}")
            return None
        api_key = get_gemini_api_key()
        if not api_key:
            print(
                "[re-export] No Gemini API key found (check .env or stored prefs). Skipping repair."
            )
            return None
        try:
            # ALWAYS confirm framerate of the video file before calling the transcriber.
            from minicat.core.video import confirm_video_framerate, extract_metadata

            fps = confirm_video_framerate(video_path)
            total_duration = extract_metadata(video_path).get("duration")
            result = transcribe_audio_with_timestamps(
                audio, api_key, language=language, fps=fps, total_duration=total_duration
            )
            segs = result.get("segments", [])
            # Sanitize: drop inverted or insanely high timestamps (Gemini sometimes returns garbage like 1004s on short audio)
            sane = []
            for s in segs:
                st = float(s.get("start", 0))
                en = float(s.get("end", 0))
                if en > st + 0.05 and st < 9000:  # 2.5h sanity for any interview
                    sane.append({"start": st, "end": en, "text": s.get("text", "")})
            if sane:
                # shift to zero if the first is high (defensive)
                min_s = min(ss["start"] for ss in sane)
                if min_s > 0.5:
                    for ss in sane:
                        ss["start"] = max(0.0, ss["start"] - min_s)
                        ss["end"] = max(ss["start"] + 0.1, ss["end"] - min_s)
            segs = sane
            with open(cache, "w") as f:
                json.dump({"segments": segs}, f)
            print(f"[re-export] Transcription complete: {len(segs)} segments cached (sanitized).")
            return segs
        except Exception as e:
            print(f"[re-export] Transcription failed: {e}")
            return None


def find_best_segment_for_text(text: str, segments: list[dict]) -> dict | None:
    """Return the segment whose text best matches the chosen script text.

    Requires strong containment or high fuzzy overlap to avoid bad matches that
    produce insane timestamps or wrong content. Returns None if no good match.
    """
    if not segments or not text:
        return None
    t = text.lower().strip()
    if not t:
        return None
    best = None
    best_score = 0.0
    for seg in segments:
        st = seg.get("text", "").lower().strip()
        if not st:
            continue
        # Strong preference for actual containment (the transcript chunk contains the chosen words)
        if t in st or st in t:
            # If the script text is mostly present, trust it (even if chunk is a bit larger)
            if len(t) > 12 and (t in st or (len(st) > 0 and st in t)):
                return seg
            # still consider for scoring
        score = difflib.SequenceMatcher(None, t, st).ratio()
        if score > best_score:
            best_score = score
            best = seg
    # Only accept if very good overlap (prevents matching short common phrases to wrong places/times)
    if best and best_score >= 0.72:
        return best
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Re-export AI Director XMEML from TXT script + mapping XML."
    )
    parser.add_argument(
        "--txt", required=True, type=Path, help="Path to the MultiClip_*.txt script"
    )
    parser.add_argument(
        "--mapping-xml",
        required=True,
        type=Path,
        help="One of the app's original AI_Multi_*.xml (for C→file mapping)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .xml path (default: same dir as TXT, with _REEXPORTED suffix)",
    )
    parser.add_argument(
        "--repair-times",
        action="store_true",
        help="Re-transcribe each source and match script texts to recover real media times (requires Gemini key + ffmpeg)",
    )
    parser.add_argument(
        "--language",
        default="fi",
        help="Language hint for transcription (default: fi, since most of these projects are Finnish)",
    )
    args = parser.parse_args()

    txt_path: Path = args.txt
    mapping_xml_path: Path = args.mapping_xml

    if not txt_path.exists():
        print(f"ERROR: TXT not found: {txt_path}")
        sys.exit(1)
    if not mapping_xml_path.exists():
        print(f"ERROR: mapping XML not found: {mapping_xml_path}")
        sys.exit(1)

    print(f"=== Re-exporting from {txt_path.name} using mapping from {mapping_xml_path.name} ===")

    clips = parse_txt_clips(txt_path)
    print(f"Parsed {len(clips)} clips from TXT.")

    c_to_path = extract_c_to_path_from_xml(mapping_xml_path)
    print(f"Recovered {len(c_to_path)} C-label → file mappings from XML.")

    if not clips or not c_to_path:
        print("ERROR: Insufficient data.")
        sys.exit(1)

    # Group for optional repair
    clips_by_c: dict[str, list] = defaultdict(list)
    for c in clips:
        clips_by_c[c["source_label"]].append(c)

    if args.repair_times:
        print("\n--- Optional transcription repair enabled ---")
        for cl, items in list(clips_by_c.items()):
            if cl not in c_to_path:
                continue
            p = Path(c_to_path[cl])
            segs = get_transcript_segments(p, language=args.language)
            if not segs:
                continue
            repaired = 0
            for item in items:
                best = find_best_segment_for_text(item["text"], segs)
                if best:
                    real_start = round(best["start"], 2)
                    intended_dur = item.get("dur") or max(
                        0.5,
                        item.get("source_out", real_start + 1) - item.get("source_in", real_start),
                    )
                    item["source_in"] = real_start
                    item["source_out"] = round(real_start + max(0.5, intended_dur), 2)
                    repaired += 1
            print(
                f"  {cl}: repaired {repaired}/{len(items)} items with real transcript times (preserving intended beat durs)."
            )
        print("--- Repair pass complete ---\n")

    # Build the data the exporter expects
    source_paths: dict[str, str] = {}
    segs: list[dict] = []
    for c in clips:
        cl = c["source_label"]
        if cl not in c_to_path:
            print(f"WARNING: no mapping for {cl}, skipping clip")
            continue
        p = c_to_path[cl]
        fname = Path(p).name
        source_paths[p] = fname
        segs.append(
            {
                "source_label": cl,
                "source_in": c["source_in"],
                "source_out": c["source_out"],
                "text": c["text"],
                "reason": c["reason"],
                "source_path": p,
                "source_filename": fname,
            }
        )

    if not segs:
        print("ERROR: No usable segments after mapping.")
        sys.exit(1)

    ver = {
        "version_id": "REEXPORT",
        "title": txt_path.stem,
        "narrative_summary": "",
        "selected_segments": segs,
    }

    print("Probing sources (ffprobe)...")
    ordered, source_meta, tb = prepare_director_sources(ver, source_paths)
    print(f"  {len(ordered)} unique sources prepared.")

    if args.output:
        out_path = args.output
    else:
        out_path = txt_path.with_name(txt_path.stem + "_REEXPORTED.xml")

    print(f"Building XMEML → {out_path}")
    pretty_xml = _build_mixed_sources_xmeml(
        ver=ver,
        ordered_sources=ordered,
        source_meta=source_meta,
        source_paths=source_paths,
        timebase=tb,
        name=out_path.stem,
    )

    out_path.write_text(pretty_xml, encoding="utf-8")
    print(f"\nWrote {out_path}")
    print("Look at the [XMEML] lines above for the per-camera DIRECT vs PACK decisions.")
    print(
        "Compare the emitted in/out values against the [times] in the original TXT for each numbered item."
    )
    print(
        "In Premiere: import the XML, relink the sources, and check that each story beat plays the text"
    )
    print(
        "that the script lists for that position (and that it comes from the correct physical camera file)."
    )


if __name__ == "__main__":
    main()
