"""Video metadata extraction + preview generation.

Heavy lifting done by the excellent ffmpeg (already present on this machine).
Thumbnail and storyboard generation use ffmpeg to extract frames + Pillow for
clean, low-resolution compositing.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from minicat.core.models import Video

try:
    from dateutil import parser as date_parser
except ImportError:
    date_parser = None  # type: ignore


import xxhash
from PIL import Image

from minicat.core.config import (
    get_audio_dir,
    get_previews_dir,
    get_subtitles_dir,
    get_transcriptions_dir,
)
from minicat.core.env import get_ffmpeg_install_hint


def _run_ffmpeg_with_progress(
    cmd: list[str],
    total_duration: float,
    progress_callback: Callable[[float, float], None] | None = None,
    *,
    timeout: int = 3600,
) -> None:
    """
    Run an ffmpeg command and report progress via callback.
    callback(percent: 0-1, eta_seconds: float)
    """
    if not progress_callback:
        # Fallback to normal run
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        return

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    start_time = time.time()
    last_percent = 0.0

    try:
        for line in process.stdout or []:
            line = line.strip()
            if line.startswith("out_time=") or line.startswith("out_time_ms="):
                try:
                    if "out_time_ms=" in line:
                        ms = float(line.split("=")[1])
                        current = ms / 1000000.0
                    else:
                        # format HH:MM:SS.mmm
                        t = line.split("=")[1]
                        h, m, s = t.split(":")
                        current = int(h) * 3600 + int(m) * 60 + float(s)

                    if total_duration > 0:
                        percent = min(current / total_duration, 0.999)
                        elapsed = time.time() - start_time
                        if percent > 0.01:
                            eta = (elapsed / percent) * (1 - percent)
                        else:
                            eta = 0
                        if percent - last_percent > 0.005:  # update every 0.5%
                            progress_callback(percent, max(eta, 0))
                            last_percent = percent
                except Exception:
                    pass
            elif line.startswith("progress=end"):
                progress_callback(1.0, 0)
                break

        process.wait(timeout=timeout)
        if process.returncode != 0:
            raise RuntimeError(f"ffmpeg failed with code {process.returncode}")
    finally:
        try:
            if process.poll() is None:
                process.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe discovery (macOS-friendly)
# ---------------------------------------------------------------------------

FFMPEG_CANDIDATES = [
    "ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/bin/ffmpeg",
]


def find_ffmpeg() -> Path:
    """Return the first working ffmpeg binary or raise a helpful error."""
    for candidate in FFMPEG_CANDIDATES:
        p = shutil.which(candidate) or candidate
        if Path(p).exists():
            # Quick sanity check
            try:
                subprocess.run(
                    [p, "-version"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                return Path(p)
            except Exception:
                continue
    raise RuntimeError(get_ffmpeg_install_hint())


def find_ffprobe() -> Path:
    """ffprobe lives next to ffmpeg in virtually all installations."""
    ffmpeg = find_ffmpeg()
    probe = ffmpeg.parent / "ffprobe"
    if probe.exists():
        return probe
    # Fallback to PATH
    p = shutil.which("ffprobe")
    if p:
        return Path(p)
    raise RuntimeError("ffprobe not found next to ffmpeg")


def get_ffmpeg_version() -> str:
    """Return a short version string for the detected ffmpeg (best effort)."""
    try:
        ffmpeg = find_ffmpeg()
        result = subprocess.run(
            [str(ffmpeg), "-version"], capture_output=True, text=True, timeout=5
        )
        first_line = result.stdout.splitlines()[0] if result.stdout else ""
        return first_line.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


def _parse_ffprobe_output(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn ffprobe -show_format -show_streams JSON into something useful."""
    info: dict[str, Any] = {}

    fmt = raw.get("format", {})
    info["duration"] = float(fmt.get("duration", 0)) if fmt.get("duration") else None
    info["size"] = int(fmt.get("size", 0)) if fmt.get("size") else None
    info["bit_rate"] = int(fmt.get("bit_rate", 0)) if fmt.get("bit_rate") else None

    # Creation time (often the best "shoot date" we can get automatically)
    tags = fmt.get("tags", {}) or {}
    for key in ("creation_time", "date", "com.apple.quicktime.creationdate"):
        if key in tags:
            creation_raw = tags[key]
            info["creation_time"] = creation_raw
            # Try to parse a usable date for shoot_date suggestion
            try:
                if date_parser:
                    dt = date_parser.parse(creation_raw)
                    info["creation_date"] = dt.date()
            except Exception:
                pass
            break

    # Camera / make / model (very common in professional footage)
    for key in ("com.apple.quicktime.make", "com.apple.quicktime.model", "make", "model"):
        if key in tags:
            info.setdefault("camera_raw", []).append(tags[key])
    if "camera_raw" in info:
        info["camera"] = " ".join(info["camera_raw"])

    # Video stream details (first video stream)
    for stream in raw.get("streams", []):
        if stream.get("codec_type") == "video":
            info["width"] = stream.get("width")
            info["height"] = stream.get("height")
            info["codec"] = stream.get("codec_name")
            info["sample_aspect_ratio"] = stream.get("sample_aspect_ratio") or "1:1"
            # FPS (can be fractional)
            if "r_frame_rate" in stream:
                try:
                    num, den = map(int, stream["r_frame_rate"].split("/"))
                    info["fps"] = num / den if den else None
                except Exception:
                    info["fps"] = None
            break

    # Audio stream details
    for stream in raw.get("streams", []):
        if stream.get("codec_type") == "audio":
            info["audio_channels"] = stream.get("channels")
            break

    return info


def has_opus_audio(path: str | Path) -> bool:
    """
    Returns True if the file contains at least one OPUS audio stream.
    Premiere Pro has well-known relinking issues with OPUS audio in XMEML sequences.
    """
    video_path = Path(path).expanduser().resolve()
    if not video_path.exists():
        return False

    try:
        ffprobe = find_ffprobe()
        cmd = [
            str(ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-select_streams",
            "a",
            str(video_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return False

        import json

        data = json.loads(result.stdout or "{}")
        for stream in data.get("streams", []):
            if stream.get("codec_name", "").lower() == "opus":
                return True
        return False
    except Exception:
        return False


def create_premiere_friendly_version(
    source_path: str | Path,
    output_path: str | Path | None = None,
    *,
    audio_codec: str = "aac",
    audio_bitrate: str = "320k",
) -> Path:
    """
    Create a copy of the video with audio re-encoded to a Premiere-friendly codec (AAC by default).
    Video stream is copied without re-encoding for speed and quality.

    This is useful when the original file uses OPUS audio, which Premiere often fails to
    properly link in XMEML sequences.

    Returns the path to the new file.
    """
    from minicat.core.video import find_ffmpeg

    src = Path(source_path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(src)

    if output_path is None:
        stem = src.stem
        suffix = src.suffix or ".mp4"
        output_path = src.parent / f"{stem}_for_Premiere{suffix}"

    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = find_ffmpeg()

    cmd = [
        str(ffmpeg),
        "-y",
        "-i",
        str(src),
        "-c:v",
        "copy",  # copy video without re-encoding
        "-c:a",
        audio_codec,
        "-b:a",
        audio_bitrate,
        "-movflags",
        "+faststart",
        str(out),
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf-8", errors="ignore")[:600] if e.stderr else str(e)
        raise RuntimeError(f"Failed to create Premiere-friendly version: {err}") from e

    if not out.exists() or out.stat().st_size < 100_000:
        raise RuntimeError(f"Transcoded file was not created properly: {out}")

    return out


def extract_metadata(path: str | Path) -> dict[str, Any]:
    """Return rich metadata for a video file using ffprobe."""
    video_path = Path(path).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    ffprobe = find_ffprobe()

    cmd = [
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_entries",
        "stream=codec_type,width,height,r_frame_rate,codec_name",
        str(video_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {video_path}: {result.stderr}")

    import json

    raw = json.loads(result.stdout or "{}")
    meta = _parse_ffprobe_output(raw)
    meta["path"] = str(video_path)
    meta["filename"] = video_path.name

    # Extract timecode start (and compute end) at import time
    try:
        tc_start, detected_fps = _get_start_timecode(video_path)
        if tc_start:
            meta["tc_start"] = tc_start
            # Compute TC End if we have duration
            duration = meta.get("duration")
            fps_for_tc = detected_fps or meta.get("fps") or 25.0
            if duration:
                meta["tc_end"] = _add_duration_to_timecode(tc_start, duration, fps_for_tc)
    except Exception:
        # Non-fatal — many files won't have embedded timecode
        pass

    return meta


def confirm_video_framerate(video_path: str | Path) -> float:
    """Probe the video file to obtain its framerate.

    Per user requirement:
    - Framerate MUST be confirmed (via live ffprobe in extract_metadata) at import time.
    - Once set on the Video record, it is NEVER changed (immutable).
    - Before starting transcription we still "confirm" by preferring the stored
      import-time value; only for legacy clips with missing fps do we probe and
      backfill exactly once.

    This function is the live-probe implementation used both at import and for
    legacy backfill / verification.

    Returns precise float fps or 25.0 default.
    """
    video_path = Path(video_path).expanduser().resolve()
    if not video_path.exists():
        print(f"[Import/Backfill] WARNING: video file not found: {video_path}")
        return 25.0

    try:
        meta = extract_metadata(str(video_path))
        fps = meta.get("fps")
        if fps and float(fps) > 0:
            fps = float(fps)
            if fps > 120 or fps < 1:
                print(
                    f"[Import/Backfill] WARNING: unrealistic fps {fps} probed from {video_path.name}, using 25.0 instead (file may have bad metadata)"
                )
                fps = 25.0
            else:
                print(f"[Import/Backfill] Live probe of {video_path.name} reports {fps} fps")
            return fps
    except Exception as ex:
        print(f"[Import/Backfill] extract_metadata fps probe failed for {video_path.name}: {ex}")

    # Fallback using the existing timebase prober (handles some NTSC rounding but we take as float)
    try:
        from minicat.ai.xmeml_exporter import get_video_timebase

        tb = get_video_timebase(video_path)
        if tb and tb > 0:
            # get_video_timebase may have rounded 23.976->24 etc.; for transcription we prefer
            # the raw rate, but this is better than nothing. Do a direct float probe if possible.
            if tb > 120 or tb < 1:
                print(
                    f"[Import/Backfill] WARNING: unrealistic timebase {tb} from fallback for {video_path.name}, using 25.0"
                )
                tb = 25
            else:
                print(f"[Import/Backfill] Fallback for {video_path.name}: {tb}")
            return float(tb)
    except Exception as ex:
        print(f"[Import/Backfill] timebase fallback failed for {video_path.name}: {ex}")

    print(f"[Import/Backfill] Using safe default 25.0 fps for {video_path.name}")
    return 25.0


# ---------------------------------------------------------------------------
# Camera sidecar XML support (Sony, Canon, BMD, etc.)
# Many professional cameras write rich metadata XML files next to the clips.
# ---------------------------------------------------------------------------


def find_camera_metadata_sidecar(video_path: Path) -> Path | None:
    """
    Find camera-generated metadata sidecars next to the video.
    Supports XML/XMP/RMD (Sony, RED, Arri, Canon, DJI) + Blackmagic JSON .sidecar files.
    """
    parent = video_path.parent
    stem = video_path.stem

    # Order matters: more specific camera formats first
    extensions = [
        ".xml",
        ".XML",
        ".xmp",
        ".XMP",
        ".rmd",
        ".RMD",  # RED
        ".sidecar",
        ".SIDE CAR",  # Blackmagic BRAW
        ".json",
        ".JSON",
        ".nksc",
        ".NKSC",  # Nikon sidecar (XMP-based, used by NX Studio)
    ]

    for ext in extensions:
        candidate = parent / f"{stem}{ext}"
        if candidate.exists():
            return candidate

    # Glob patterns (handles C0001M01.XML, clip_001_metadata.sidecar, etc.)
    glob_patterns = [
        f"{stem}*.xml",
        f"{stem}*.XML",
        f"{stem}*.xmp",
        f"{stem}*.XMP",
        f"{stem}*.rmd",
        f"{stem}*.RMD",
        f"{stem}*.sidecar",
        f"{stem}*.SIDE CAR",
        f"{stem}*.json",
        f"{stem}*.JSON",
        f"{stem}*.nksc",
        f"{stem}*.NKSC",
    ]
    for pattern in glob_patterns:
        matches = sorted(parent.glob(pattern))
        if matches:
            return matches[0]

    return None


def extract_metadata_with_exiftool(video_path: Path) -> dict[str, Any]:
    """
    Optional deep metadata extraction using ExifTool (if installed).
    This gives excellent coverage for Fuji (FilmMode), Nikon, Canon embedded,
    DJI, GoPro (via GPMF), Blackmagic, etc. even when no sidecar XML exists.
    """
    result: dict[str, Any] = {}
    try:
        # Try common exiftool locations
        exiftool_candidates = ["exiftool", "/usr/local/bin/exiftool", "/opt/homebrew/bin/exiftool"]
        exiftool = None
        for cand in exiftool_candidates:
            if shutil.which(cand) or Path(cand).exists():
                exiftool = cand
                break
        if not exiftool:
            return result

        cmd = [exiftool, "-ee", "-G", "-json", "-api", "largefilesupport=1", str(video_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if proc.returncode != 0 or not proc.stdout.strip():
            return result

        import json

        data = json.loads(proc.stdout)
        if not data:
            return result
        meta = data[0]

        # Map common ExifTool tags to our schema
        if meta.get("EXIF:Model") or meta.get("QuickTime:Model"):
            cam = meta.get("EXIF:Model") or meta.get("QuickTime:Model") or meta.get("File:Producer")
            if cam:
                result["camera"] = str(cam).strip()

        if meta.get("EXIF:LensModel") or meta.get("QuickTime:Lens"):
            result["lens"] = meta.get("EXIF:LensModel") or meta.get("QuickTime:Lens")

        def _ci(v):
            try:
                return int(float(str(v)))
            except Exception:
                return None

        if meta.get("EXIF:ISO") or meta.get("QuickTime:ISO"):
            result["iso"] = _ci(meta.get("EXIF:ISO") or meta.get("QuickTime:ISO"))

        if meta.get("EXIF:FNumber"):
            result["f_number"] = round(float(meta["EXIF:FNumber"]), 1)

        if meta.get("EXIF:ExposureTime"):
            result["shutter_speed"] = str(meta["EXIF:ExposureTime"])

        if meta.get("EXIF:FocalLength"):
            fl = meta["EXIF:FocalLength"]
            result["focal_length"] = round(float(str(fl).split()[0]))

        if meta.get("EXIF:WhiteBalance") or meta.get("QuickTime:WhiteBalance"):
            result["white_balance"] = str(
                meta.get("EXIF:WhiteBalance") or meta.get("QuickTime:WhiteBalance")
            )

        # Fuji Film Simulation (very valuable for Fuji shooters)
        fuji_film = (
            meta.get("MakerNotes:FilmMode")
            or meta.get("FujiFilm:FilmMode")
            or meta.get("FujiFilm:FilmSimulation")
        )
        if fuji_film:
            result["gamma"] = str(
                fuji_film
            )  # Users often want to see "Eterna", "Classic Negative", etc.
            if not result.get("camera"):
                result["camera"] = "Fujifilm"

        # GoPro / DJI / general
        if meta.get("GoPro:Model") or meta.get("DJI:Model"):
            result["camera"] = str(meta.get("GoPro:Model") or meta.get("DJI:Model"))

        # Blackmagic / RED embedded
        if meta.get("QuickTime:CompressorName"):
            result["codec"] = str(meta["QuickTime:CompressorName"])

    except Exception:
        # Silent fail — ExifTool is optional
        pass

    return result


def is_exiftool_available() -> bool:
    """Quick check if exiftool can be found (used by Settings UI)."""
    candidates = ["exiftool", "/usr/local/bin/exiftool", "/opt/homebrew/bin/exiftool"]
    for cand in candidates:
        if shutil.which(cand) or Path(cand).exists():
            return True
    return False


def extract_camera_xml_metadata(
    video_path: str | Path, *, enrich_with_exiftool: bool = False
) -> dict[str, Any]:
    """
    Best-effort parser for camera sidecar files (XML, XMP, RMD, JSON .sidecar).

    Supports:
      - Sony NonRealTimeMeta
      - RED .RMD
      - Arri (com.arri.camera.*)
      - Canon XF
      - DJI XMP
      - Blackmagic JSON .sidecar (BRAW workflows)
      - Generic XMP

    By default this ONLY parses small sidecar files next to the video.
    It does NOT open or read the actual video container (no ffprobe, no exiftool -ee).

    Set enrich_with_exiftool=True only for explicit "deep scan" / rebuild actions.
    The ExifTool pass can be slow on large 4K files because it extracts embedded
    metadata from the media container itself.
    """
    video_path = Path(video_path).expanduser().resolve()
    sidecar_path = find_camera_metadata_sidecar(video_path)
    if not sidecar_path:
        return {}

    result: dict[str, Any] = {"source_xml": str(sidecar_path)}

    def _safe_int(val) -> int | None:
        """Safe int conversion used by multiple sidecar parsers."""
        try:
            return int(float(str(val)))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Blackmagic JSON sidecar support (.sidecar / .json)
    # Very common with Pocket Cinema Camera 6K / URSA / Studio in BRAW workflows
    # ------------------------------------------------------------------
    if sidecar_path.suffix.lower() in (".sidecar", ".json"):
        try:
            import json

            data = json.loads(sidecar_path.read_text())
            # Blackmagic sidecars are usually a dict or list with one dict
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                meta = data.get("metadata", data)  # sometimes nested

                if meta.get("camera") or meta.get("cameraModel"):
                    result["camera"] = meta.get("camera") or meta.get("cameraModel")
                if meta.get("lens") or meta.get("lensModel"):
                    result["lens"] = meta.get("lens") or meta.get("lensModel")
                if meta.get("ISO") or meta.get("iso"):
                    result["iso"] = _safe_int(meta.get("ISO") or meta.get("iso"))
                if meta.get("whiteBalance") or meta.get("white_balance"):
                    result["white_balance"] = str(
                        meta.get("whiteBalance") or meta.get("white_balance")
                    )
                if meta.get("gamma") or meta.get("colorScience"):
                    result["gamma"] = str(meta.get("gamma") or meta.get("colorScience"))
                if meta.get("colorSpace"):
                    result["color_primaries"] = str(meta["colorSpace"])

                # Scene / take info is often useful
                if meta.get("scene") or meta.get("shot"):
                    result["notes"] = f"{meta.get('scene', '')} {meta.get('shot', '')}".strip()

            return result  # JSON sidecar handled — skip XML parsing
        except Exception as e:
            print(f"[Blackmagic Sidecar] Failed to parse {sidecar_path.name}: {e}")

    try:
        tree = ET.parse(sidecar_path)
        root = tree.getroot()

        camera_candidates: list[str] = []
        lens_candidates: list[str] = []
        codec_candidates: list[str] = []
        date_candidates: list[str] = []

        # Detect brand / format for targeted extraction
        root_tag = (root.tag or "").lower()
        root_ns = ""
        if "}" in root.tag:
            root_ns = root.tag.split("}")[0].strip("{").lower()

        brand = "generic"
        if "nonrealtimemeta" in root_tag or "professionaldisc" in root_ns:
            brand = "sony"
        elif "redmetadata" in root_tag or root_tag.endswith("rmd"):
            brand = "red"
        elif "arri" in root_ns or "arri" in root_tag:
            brand = "arri"
        elif "canon" in root_ns or "xf" in root_tag:
            brand = "canon"
        elif "drone-dji" in root_ns or "dji" in root_tag:
            brand = "dji"
        elif "xmpmeta" in root_tag or "adobe" in root_ns:
            brand = "xmp"

        # Sony-specific: extract starting LTC timecode and fps from LtcChangeTable
        # (very common on XAVC / ILCE / PXW etc. sidecars; Premiere reads this)
        if brand == "sony" or "nonrealtimemeta" in root_tag or "professionaldisc" in root_ns:
            try:
                for elem in root.iter():
                    tag = (elem.tag or "").lower().split("}")[-1]
                    if tag == "ltcchangetable":
                        tc_fps = elem.get("tcFps") or elem.get("tcfps")
                        if tc_fps:
                            try:
                                result["fps"] = float(tc_fps)
                            except Exception:
                                pass
                    if tag == "ltcchange":
                        if elem.get("frameCount") in ("0", "0.0"):
                            val = elem.get("value")
                            if val:
                                s = str(val).zfill(8)
                                # The value is stored as 8 digits in FFSSMMHH order (from observation on real files)
                                # Reorder pairs to standard HH:MM:SS:FF
                                tc = s[6:8] + ":" + s[4:6] + ":" + s[2:4] + ":" + s[0:2]
                                result["timecode"] = tc
                                break
            except Exception as ltc_ex:
                print(f"[Sony XML] LTC parse warning: {ltc_ex}")

        # Common technical field aliases (used by generic + brand parsers)
        technical_map = {
            "iso": ["iso", "isospeed", "gain", "isosensitivity", "exposureindex"],
            "f_number": ["fnumber", "f-number", "iris", "aperture", "fstop"],
            "shutter_speed": ["shutterspeed", "shutter", "exposuretime", "shutterspeedsec"],
            "focal_length": ["focallength", "focal"],
            "white_balance": ["whitebalance", "wb", "whitebal", "whitebalancepreset", "kelvin"],
            "gamma": ["gamma", "gammacurve", "gammatype", "gammamode", "capturegamma"],
        }

        def clean_float(val: str) -> float | None:
            try:
                return float(val)
            except Exception:
                return None

        def clean_int(val: str) -> int | None:
            try:
                return int(float(val))
            except Exception:
                return None

        def add_candidate(lst: list[str], value: str, min_len=3, max_len=140):
            v = value.strip()
            if min_len < len(v) < max_len:
                lst.append(v)

        # ------------------------------------------------------------------
        # Brand-specific fast paths (run first for best accuracy)
        # ------------------------------------------------------------------
        if brand == "red":
            # RED .RMD style
            for elem in root.iter():
                tag = (elem.tag or "").lower().split("}")[-1]
                attrs = {k.lower(): v.strip() for k, v in elem.attrib.items()}
                text = (elem.text or "").strip() or attrs.get("value", "")

                if tag in ("masteriso", "iso"):
                    val = clean_int(text or attrs.get("value", ""))
                    if val:
                        result["iso"] = val
                if tag in ("kelvin", "whitebalance"):
                    result["white_balance"] = text or attrs.get("value", "")
                if tag in ("colorspace", "gamma"):
                    result[tag] = text or attrs.get("value", "")
                if "lens" in tag:
                    add_candidate(lens_candidates, text or str(attrs))
                if "clipname" in tag or "clip" in tag:
                    add_candidate(codec_candidates, text)  # reuse for clip info

        elif brand == "arri":
            # Arri com.arri.camera.* namespace style
            for elem in root.iter():
                full_tag = (elem.tag or "").lower()
                tag = full_tag.split("}")[-1]
                attrs = {k.lower(): v.strip() for k, v in elem.attrib.items()}
                text = (elem.text or "").strip()

                val = text or attrs.get("value", "")
                if not val:
                    continue

                if "cameramodel" in tag or "camera" in tag:
                    add_candidate(camera_candidates, val)
                if "lens" in tag and "focal" in tag.lower():
                    add_candidate(lens_candidates, val)
                if "exposureindex" in tag or "asa" in tag:
                    val_i = clean_int(val)
                    if val_i:
                        result["iso"] = val_i
                if "whitebalance" in tag and "kelvin" in tag.lower():
                    result["white_balance"] = val
                if "shutterangle" in tag:
                    result["shutter_speed"] = val + "°"
                if "colorgamma" in tag or "gamma" in tag:
                    result["gamma"] = val
                if "color" in tag and "primar" in tag:
                    result["color_primaries"] = val

        elif brand == "canon":
            # Canon XF style
            for elem in root.iter():
                tag = (elem.tag or "").lower().split("}")[-1]
                text = (elem.text or "").strip()
                if not text:
                    continue
                if "model" in tag or "camera" in tag:
                    add_candidate(camera_candidates, text)
                if "lens" in tag:
                    add_candidate(lens_candidates, text)
                if tag in ("iso", "gain"):
                    val = clean_int(text)
                    if val:
                        result["iso"] = val
                if "shutter" in tag:
                    result["shutter_speed"] = text
                if "fnumber" in tag or "iris" in tag:
                    val = clean_float(text)
                    if val:
                        result["f_number"] = round(val, 1)
                if "gamma" in tag or "log" in text.lower():
                    result["gamma"] = text

        elif brand == "dji":
            # DJI drone-dji XMP namespace
            for elem in root.iter():
                attrs = {k.lower(): v.strip() for k, v in elem.attrib.items()}
                for k, v in attrs.items():
                    if "absolutealtitude" in k or "rel_alt" in k:
                        result["notes"] = result.get("notes", "") + f" Alt:{v}m "
                    if "gimbalpitch" in k:
                        result["notes"] = result.get("notes", "") + f" GimbalPitch:{v} "
                    if "flightyaw" in k:
                        result["notes"] = result.get("notes", "") + f" Yaw:{v} "
                    if "model" in k and ("drone" in k or "dji" in k or "make" in k):
                        add_candidate(camera_candidates, f"DJI {v}")

        # ------------------------------------------------------------------
        # Sony-specific handling (preserved and prioritized)
        # ------------------------------------------------------------------
        for elem in root.iter():
            tag = (elem.tag or "").lower().split("}")[-1]
            text = (elem.text or "").strip()
            attrs = {k.lower(): v.strip() for k, v in elem.attrib.items()}

            # Sony <Item name="..." value="..."/> pattern
            if "name" in attrs and "value" in attrs:
                item_name = attrs["name"].lower()
                item_value = attrs["value"]

                if "gamm" in item_name or "capturegamma" in item_name:
                    result["gamma"] = item_value
                if "colorprimar" in item_name or "colorspace" in item_name:
                    result["color_primaries"] = item_value
                if "codingequation" in item_name or "coding" in item_name:
                    result["coding_equations"] = item_value

                av = item_value
                if any(pt in item_name for pt in technical_map["iso"]):
                    val = clean_int(av)
                    if val:
                        result["iso"] = val
                if any(pt in item_name for pt in technical_map["f_number"]):
                    val = clean_float(av)
                    if val:
                        result["f_number"] = round(val, 1)
                if any(pt in item_name for pt in technical_map["shutter_speed"]):
                    result["shutter_speed"] = av
                if any(pt in item_name for pt in technical_map["focal_length"]):
                    val = clean_float(av)
                    if val:
                        result["focal_length"] = round(val)
                if any(pt in item_name for pt in technical_map["white_balance"]):
                    result["white_balance"] = av

            # Sony attribute handling (scoped)
            for an, av in attrs.items():
                if not av:
                    continue
                is_device_context = any(k in tag for k in ("device", "camera", "nonrealtimemeta"))
                if is_device_context and any(
                    k in an
                    for k in (
                        "modelname",
                        "model",
                        "devicename",
                        "manufacturer",
                        "make",
                        "cameramodel",
                    )
                ):
                    add_candidate(camera_candidates, av)

                is_lens_context = any(k in tag for k in ("lens", "lensmodel"))
                if is_lens_context and any(
                    k in an for k in ("modelname", "model", "lensmodelname", "lensmodel", "optic")
                ):
                    add_candidate(lens_candidates, av)

                if any(
                    k in an
                    for k in (
                        "codec",
                        "videocodec",
                        "format",
                        "recordingformat",
                        "codecname",
                        "formatname",
                    )
                ):
                    add_candidate(codec_candidates, av)

                # Technical fallbacks
                for field, aliases in technical_map.items():
                    if any(pt in an for pt in aliases):
                        if field == "iso":
                            val = clean_int(av)
                            if val:
                                result["iso"] = val
                        elif field == "f_number":
                            val = clean_float(av)
                            if val:
                                result["f_number"] = round(val, 1)
                        elif field == "shutter_speed":
                            result["shutter_speed"] = av
                        elif field == "focal_length":
                            val = clean_float(av)
                            if val:
                                result["focal_length"] = round(val)
                        elif field == "white_balance":
                            result["white_balance"] = av
                        elif field == "gamma":
                            result["gamma"] = av

            if not text:
                continue

            # Generic collection (works across brands)
            if any(
                k in tag
                for k in ("model", "device", "cameramodel", "manufacturer", "make", "modelname")
            ):
                add_candidate(camera_candidates, text)
            if any(k in tag for k in ("lens", "optic", "glass", "lensmodel", "lensmodelname")):
                add_candidate(lens_candidates, text)
            if any(
                k in tag for k in ("codec", "videocodec", "format", "recordingformat", "codecname")
            ):
                add_candidate(codec_candidates, text)
            if any(
                k in tag
                for k in (
                    "datetime",
                    "date",
                    "time",
                    "startdate",
                    "creation",
                    "recdate",
                    "creationdate",
                )
            ):
                date_candidates.append(text)

            for field, aliases in technical_map.items():
                if any(pt in tag for pt in aliases):
                    if field == "iso":
                        val = clean_int(text)
                        if val:
                            result["iso"] = val
                    elif field == "f_number":
                        val = clean_float(text)
                        if val:
                            result["f_number"] = round(val, 1)
                    elif field == "shutter_speed":
                        result["shutter_speed"] = text
                    elif field == "focal_length":
                        val = clean_float(text)
                        if val:
                            result["focal_length"] = round(val)
                    elif field == "white_balance":
                        result["white_balance"] = text
                    elif field == "gamma":
                        result["gamma"] = text

        # Final best-value selection
        if camera_candidates:
            cam = max(set(camera_candidates), key=len)
            if (
                any(x in cam.upper() for x in ("ILCE", "ILME", "PXW", "FDR", "BURANO"))
                and "SONY" not in cam.upper()
            ):
                cam = "Sony " + cam
            result["camera"] = cam

        if lens_candidates:
            result["lens"] = max(set(lens_candidates), key=len)
        if codec_candidates:
            result["codec"] = max(set(codec_candidates), key=len)

        for raw in date_candidates:
            try:
                if date_parser:
                    dt = date_parser.parse(raw)
                    result["shoot_date"] = dt.date()
                    break
            except Exception:
                continue

    except ET.ParseError as e:
        print(f"[XML] Parse error in {sidecar_path.name}: {e}")
    except Exception as e:
        print(f"[XML] Failed to process {sidecar_path.name}: {e}")

    # --- Optional ExifTool enrichment ---
    # This is DISABLED by default because it runs exiftool -ee on the *video file itself*,
    # which for large 4K/6K camera originals can take many seconds and heavy I/O.
    # We only do this for explicit user-initiated "Re-extract" or "Rebuild" actions.
    if enrich_with_exiftool:
        try:
            exif_data = extract_metadata_with_exiftool(video_path)
            for key in (
                "camera",
                "lens",
                "iso",
                "f_number",
                "shutter_speed",
                "focal_length",
                "white_balance",
                "gamma",
                "codec",
            ):
                if exif_data.get(key) and not result.get(key):
                    result[key] = exif_data[key]
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Fingerprinting (fast duplicate detection)
# ---------------------------------------------------------------------------


def fast_fingerprint(
    path: str | Path, sample_bytes: int = 4 * 1024 * 1024, duration: float | None = None
) -> str:
    """
    Very fast, effective fingerprint:
    file size + duration (from metadata) + xxh3 hash of first N bytes.
    Excellent at catching exact or near-identical camera originals.
    """
    p = Path(path).expanduser().resolve()
    size = p.stat().st_size

    hasher = xxhash.xxh3_64()
    with p.open("rb") as f:
        chunk = f.read(sample_bytes)
        hasher.update(chunk)

    if duration is not None:
        return f"{size}:{duration:.3f}:{hasher.hexdigest()}"
    return f"{size}:{hasher.hexdigest()}"


# ---------------------------------------------------------------------------
# Preview generation (the heart of "low resolution preview images")
# ---------------------------------------------------------------------------


def _ffmpeg_seek_time(duration: float | None) -> float:
    """Choose a good single-frame time for thumbnail (avoid first 1.5s of black)."""
    if duration and duration > 8:
        return max(1.8, duration * 0.12)  # ~12% in or at least 1.8s
    return 1.5


def generate_thumbnail(
    video_path: str | Path,
    catalog_root: Path,
    video_id: int,
    *,
    width: int = 480,
    quality: int = 82,
) -> Path:
    """
    Extract one well-chosen low-res frame and save it as JPEG inside the catalog.
    Returns the path to the saved thumbnail.
    """
    video_path = Path(video_path).expanduser().resolve()
    meta = extract_metadata(video_path)
    duration = meta.get("duration")

    seek = _ffmpeg_seek_time(duration)
    out_dir = get_previews_dir(catalog_root, "thumbs")
    out_path = out_dir / f"{video_id:06d}.jpg"

    ffmpeg = find_ffmpeg()

    # Use ffmpeg to extract a single high-quality frame, then let Pillow resize + compress
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    cmd = [
        str(ffmpeg),
        "-ss",
        str(seek),
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",  # high quality source frame
        "-y",
        str(tmp_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)

    # Resize + re-encode with Pillow for consistent small size
    with Image.open(tmp_path) as im:
        # Maintain aspect ratio
        im.thumbnail((width, width * 2), Image.Resampling.LANCZOS)
        im = im.convert("RGB")
        im.save(out_path, "JPEG", quality=quality, optimize=True)

    tmp_path.unlink(missing_ok=True)
    return out_path


def generate_storyboard(
    video_path: str | Path,
    catalog_root: Path,
    video_id: int,
    *,
    cols: int = 4,
    rows: int = 3,
    cell_width: int = 240,
    cell_height: int = 135,
    quality: int = 80,
) -> Path:
    """
    Create a consistent contact-sheet / storyboard grid (default 4x3 = 12 frames).

    IMPORTANT: Storyboards are now the SAME SIZE for every video in the catalog,
    regardless of duration or resolution. This gives uniform appearance in the
    inspector, dialogs, and AI features.

    Each cell is a fixed pixel size (default 240×135). Frames are resized to fit
    while preserving aspect ratio and centered with dark bars if needed. The
    final JPEG dimensions are therefore identical for any clip using the same
    cols/rows/cell size.
    """
    video_path = Path(video_path).expanduser().resolve()
    meta = extract_metadata(video_path)
    duration = meta.get("duration") or 60.0

    total_frames = cols * rows
    interval = duration / (total_frames + 1)

    ffmpeg = find_ffmpeg()
    out_dir = get_previews_dir(catalog_root, "boards")
    out_path = out_dir / f"{video_id:06d}.jpg"

    target_w = cell_width
    target_h = cell_height

    frames: list[Image.Image] = []

    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        for i in range(total_frames):
            t = interval * (i + 1)
            frame_path = tmpdir / f"f{i:02d}.jpg"
            cmd = [
                str(ffmpeg),
                "-ss",
                str(t),
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-y",
                str(frame_path),
            ]
            # Much longer timeout for big 4K/long files on external drives
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
            with Image.open(frame_path) as im:
                im = im.convert("RGB")
                img_w, img_h = im.size
                if img_w == 0 or img_h == 0:
                    # degenerate frame, create solid dark cell
                    cell = Image.new("RGB", (target_w, target_h), (30, 30, 34))
                    frames.append(cell)
                    continue

                # Fit while preserving aspect ratio
                scale = min(target_w / img_w, target_h / img_h)
                new_w = max(1, int(img_w * scale))
                new_h = max(1, int(img_h * scale))
                im_resized = im.resize((new_w, new_h), Image.Resampling.LANCZOS)

                # Create fixed-size cell with dark background (letter/pillarbox)
                cell = Image.new("RGB", (target_w, target_h), (30, 30, 34))
                x = (target_w - new_w) // 2
                y = (target_h - new_h) // 2
                cell.paste(im_resized, (x, y))
                frames.append(cell)

    # Composite into a grid — dimensions are now identical for every video
    # using the same cols/rows/cell_w/cell_h.
    cell_w = target_w
    cell_h = target_h
    margin = 6
    grid_w = cols * cell_w + (cols + 1) * margin
    grid_h = rows * cell_h + (rows + 1) * margin

    grid = Image.new("RGB", (grid_w, grid_h), color=(18, 18, 20))  # dark modern look

    for idx, frame in enumerate(frames):
        c = idx % cols
        r = idx // cols
        x = margin + c * (cell_w + margin)
        y = margin + r * (cell_h + margin)
        grid.paste(frame, (x, y))

    grid.save(out_path, "JPEG", quality=quality, optimize=True)
    return out_path


def generate_previews(
    video_path: str | Path,
    catalog_root: Path,
    video_id: int,
    *,
    thumb_width: int = 480,
    storyboard_cols: int = 4,
    storyboard_rows: int = 3,
    storyboard_cell_width: int = 240,
    storyboard_cell_height: int = 135,
) -> tuple[Path, Path]:
    """Convenience: generate both thumbnail and storyboard for a video.

    Storyboards are always the same pixel dimensions for a given
    cols/rows/cell size (see generate_storyboard for details).
    """
    thumb = generate_thumbnail(video_path, catalog_root, video_id, width=thumb_width)
    board = generate_storyboard(
        video_path,
        catalog_root,
        video_id,
        cols=storyboard_cols,
        rows=storyboard_rows,
        cell_width=storyboard_cell_width,
        cell_height=storyboard_cell_height,
    )
    return thumb, board


# ---------------------------------------------------------------------------
# Audio extraction for transcription
# ---------------------------------------------------------------------------

# Transcription proxy settings (the single processed audio file cached per clip).
# This is what Gemini (and AI Journalist "listening") actually receives.
# Simple clean format for best transcription results:
#   24 kHz mono AAC @ 64 kbps
#   + peak normalization to -3 dB (dynamic per clip)
# No noise reduction, no additional leveling.
TRANSCRIPTION_PROXY_SAMPLE_RATE = 24000
TRANSCRIPTION_PROXY_BITRATE = "64k"
TRANSCRIPTION_PROXY_FORMAT = "m4a"
TRANSCRIPTION_PROXY_NORM_DB = -3.0  # target peak (dBFS) for the final mono signal


def extract_audio_track(
    video_path: str | Path,
    output_path: str | Path,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    format: str = "wav",
    duration: float
    | None = None,  # optional: limit extraction to first N seconds (useful for very long files)
    bitrate: str | None = None,  # e.g. "128k" for MP3 or "64k" for AAC. Only for lossy formats.
    audio_filters: str
    | None = None,  # Full -af filter graph. When provided, enables the production transcription proxy chain.
) -> Path:
    """
    Extract (and optionally process) audio from a video file using ffmpeg.

    Two main modes:

    1. Simple extraction (default): raw PCM WAV or MP3 at requested rate/channels.
    2. Processed transcription proxy (recommended for AI): when audio_filters is supplied,
       the provided filter graph is applied (for the proxy this is mono downmix + dynamic
       peak volume normalization to -3 dB), then encode as efficient 24 kHz AAC @64 kbps.

    The transcription cache (ensure_cached_audio) computes a per-clip gain so the mono
    signal's peak reaches TRANSCRIPTION_PROXY_NORM_DB (default -3 dB), then uses:
      - 24 kHz sample rate
      - mono via "pan=mono|c0=0.5*c0+0.5*c1" (safe average downmix)
      - volume=...dB (dynamic gain to hit exactly -3 dB peak on the mono)
      - AAC @ 64 kbps in .m4a container (tiny files, clean for Gemini)

    Returns the path to the extracted/processed audio file.
    """
    ffmpeg = find_ffmpeg()
    video_path = Path(video_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(ffmpeg),
        "-ss",
        "0",
        "-i",
        str(video_path),
        "-vn",
    ]

    if audio_filters:
        cmd += ["-af", audio_filters]

    # Codec selection + sensible bitrate defaults for lossy formats
    fmt = (format or "wav").lower()
    if fmt in ("m4a", "aac", "mp4"):
        acodec = "aac"
        br = bitrate or TRANSCRIPTION_PROXY_BITRATE
        use_br = True
    elif fmt == "mp3":
        acodec = "libmp3lame"
        br = bitrate or "128k"
        use_br = True
    else:
        acodec = "pcm_s16le"
        br = None
        use_br = False

    cmd += ["-acodec", acodec]
    cmd += ["-ar", str(sample_rate)]

    if channels is not None:
        cmd += ["-ac", str(channels)]

    if use_br and br:
        cmd += ["-b:a", br]

    if duration:
        cmd += ["-t", str(duration)]

    cmd += ["-y", str(output_path)]

    # Very generous timeout for long 4K files.
    timeout = 900  # 15 minutes
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg audio extraction failed: {e.stderr.decode()[:500]}") from e

    return output_path


def _compute_mono_peak_norm_gain(
    video_path: str | Path, target_db: float = None, max_duration: float | None = None
) -> float:
    """Fast detection pass: compute the volume gain (in dB) needed after mono downmix
    so that the peak level of the mono signal reaches exactly target_db.

    If max_duration is given, only analyze the first N seconds (so the gain is
    computed for the audio that will actually be extracted).

    Used by the transcription proxy to replicate the "normalization to -3dB + mono"
    format that produced the best test results.
    """
    if target_db is None:
        target_db = TRANSCRIPTION_PROXY_NORM_DB
    ffmpeg = find_ffmpeg()
    vp = Path(video_path).expanduser().resolve()
    cmd = [
        str(ffmpeg),
        "-i",
        str(vp),
        "-vn",
    ]
    if max_duration:
        cmd += ["-t", str(max_duration)]
    cmd += [
        "-af",
        "pan=mono|c0=0.5*c0+0.5*c1,volumedetect",
        "-f",
        "null",
        "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        stderr = res.stderr or ""
        for line in stderr.splitlines():
            if "max_volume" in line:
                # e.g. "max_volume: -7.5 dB" or with [Parsed...] prefix
                after = line.split("max_volume", 1)[-1]
                val = after.replace(":", "").replace("dB", "").strip().split()[0]
                max_db = float(val)
                gain = target_db - max_db
                return round(gain, 2)
    except Exception as ex:
        print(f"[Audio] Peak detect for -3dB norm failed ({ex}); falling back to 0 dB gain")
    return 0.0


# ---------------------------------------------------------------------------
# Persistent transcription proxy audio (for Gemini transcription + AI Journalist listening)
# These live in <catalog>/audio/ as 0000XX.m4a so they are reused and never re-extracted.
# The proxy is now: 24 kHz mono AAC 64k + peak normalization to -3 dB (no other processing).
# ---------------------------------------------------------------------------


def get_cached_audio_path(clip_id: int, catalog_root: Path) -> Path:
    """
    Return the stable on-disk path for the single cached transcription proxy audio for a clip.

    Naming matches the preview convention (zero-padded 6 digits):
    - Stored as <catalog>/audio/000001.m4a (for clip ID 1)
    - 24 kHz mono AAC @64k with peak normalization to -3 dB:
        mono downmix (average) + dynamic volume to make mono peak = -3 dB
    - This single small file is used for:
        * Transcription (Gemini via the background worker)
        * AI Journalist listening (tone, emotion, delivery, pacing) in single- and multi-clip modes
    - Much smaller and higher-intelligibility than the old raw 16 kHz WAV caches.
    """
    audio_dir = get_audio_dir(catalog_root)
    return audio_dir / f"{clip_id:06d}.m4a"


def ensure_cached_audio(
    video_path: str | Path,
    clip_id: int,
    catalog_root: Path,
    max_duration: float | None = None,
) -> Path | None:
    """
    Return the single persistent *transcription proxy* audio for a clip.

    Format: 24 kHz mono AAC (64 kbps) with peak normalization to -3 dB:
        - pan mono downmix (0.5*c0 + 0.5*c1 average)
        - volume=...dB (dynamic per-clip gain computed so mono peak == -3 dB)
    Naming: zero-padded like previews → <catalog>/audio/000001.m4a

    Extracts (with full processing) only if the target .m4a does not already exist and is valid.
    This single lightweight, high-intelligibility file is reused for:
    - Transcription (background worker → Gemini)
    - AI Journalist (tone/emotion/delivery/pacing "listening") in both single-clip and multi-clip flows

    Old raw 16 kHz WAV caches are automatically removed on first access (one-time migration).
    After changing the proxy format, use "Rebuild Cached Audio" in the inspector to regenerate
    existing clips with the new -3 dB peak norm + mono only processing.
    """
    target = get_cached_audio_path(clip_id, catalog_root)
    if target.exists() and target.stat().st_size > 200:
        return target

    # One-time migration from previous WAV-based caches (pre processing upgrade).
    # We always re-extract from the original source with the new superior filter chain
    # instead of trying to "upgrade" a low-quality 16 kHz WAV.
    audio_dir = get_audio_dir(catalog_root)
    for legacy in (
        audio_dir / f"{clip_id:06d}.wav",
        audio_dir / f"{clip_id}.wav",  # old unpadded
    ):
        if legacy.exists() and legacy != target:
            try:
                legacy.unlink()
                print(
                    f"[Audio Cache] Removed legacy {legacy.name} (migrating clip {clip_id} to processed 24 kHz AAC proxy)"
                )
            except Exception:
                pass

    try:
        # Dynamic per-clip normalization to exactly -3 dB peak on the mono signal.
        # This matches the "only aac 64k + normalization to -3db + mono" format that tested best.
        gain = _compute_mono_peak_norm_gain(video_path, TRANSCRIPTION_PROXY_NORM_DB, max_duration)
        audio_filters = f"pan=mono|c0=0.5*c0+0.5*c1,volume={gain}dB"

        extract_audio_track(
            video_path,
            target,
            sample_rate=TRANSCRIPTION_PROXY_SAMPLE_RATE,
            channels=1,
            format=TRANSCRIPTION_PROXY_FORMAT,
            duration=max_duration,
            bitrate=TRANSCRIPTION_PROXY_BITRATE,
            audio_filters=audio_filters,
        )
        if target.exists() and target.stat().st_size > 200:
            print(
                f"[Audio Cache] Created persistent transcription proxy: {target.name} (24 kHz AAC mono + peak norm to {TRANSCRIPTION_PROXY_NORM_DB}dB)"
            )

            return target
    except Exception as ex:
        print(f"[Audio Cache] Failed to create transcription proxy for clip {clip_id}: {ex}")

    return None


def clear_cached_audio(clip_id: int, catalog_root: Path) -> int:
    """
    Delete the cached transcription proxy audio file(s) for a clip (current .m4a + any legacy .wav).
    Returns the number of files deleted (0, 1, or more if legacy variants existed).
    """
    deleted = 0
    audio_dir = get_audio_dir(catalog_root)

    # Current canonical
    p = get_cached_audio_path(clip_id, catalog_root)
    try:
        if p.exists():
            p.unlink()
            print(f"[Audio Cache] Cleared {p.name}")
            deleted += 1
    except Exception as ex:
        print(f"[Audio Cache] Failed to delete {p}: {ex}")

    # Also nuke any legacy WAVs for this clip (format migration safety)
    for legacy_name in (f"{clip_id:06d}.wav", f"{clip_id}.wav"):
        lp = audio_dir / legacy_name
        try:
            if lp.exists():
                lp.unlink()
                print(f"[Audio Cache] Cleared legacy WAV {lp.name}")
                deleted += 1
        except Exception as ex:
            print(f"[Audio Cache] Failed to delete legacy {lp}: {ex}")

    return deleted


def purge_legacy_wav_caches(catalog_root: Path, clip_ids: set[int] | None = None) -> int:
    """
    Remove obsolete .wav transcription caches from <catalog>/audio/ for the given (or all) clips.

    After the upgrade to the processed 24 kHz AAC proxy (.m4a), any old 16 kHz WAV files
    for live clips are just wasting space and confusing the folder listing. This purges them
    without touching modern .m4a proxies.

    If clip_ids is None, it will query the DB for current clips.
    Returns the number of .wav files removed.
    """
    audio_dir = get_audio_dir(catalog_root)
    if not audio_dir.exists():
        return 0

    if clip_ids is None:
        try:
            from minicat.core import db as _db
            from minicat.core.models import SearchFilters

            videos = _db.search_videos(catalog_root, SearchFilters(), limit=200000)
            clip_ids = {int(v.id) for v in videos if getattr(v, "id", None)}
        except Exception:
            clip_ids = set()

    deleted = 0
    for cid in list(clip_ids):
        for name in (f"{int(cid):06d}.wav", f"{int(cid)}.wav"):
            p = audio_dir / name
            if p.exists() and p.is_file():
                try:
                    p.unlink()
                    deleted += 1
                    print(f"[Audio Cache] Purged legacy WAV: {p.name} (clip {cid})")
                except Exception as ex:
                    print(f"[Audio Cache] Failed to purge legacy {p}: {ex}")

    if deleted:
        print(f"[Audio Cache] purge_legacy_wav_caches removed {deleted} file(s)")
    return deleted


def rebuild_cached_audio_for_clip(
    video_path: str | Path,
    clip_id: int,
    catalog_root: Path,
    progress_callback: Callable[[str], None] | None = None,
) -> bool:
    """
    Force re-extraction (with full production pre-processing) of the single
    transcription proxy audio for one clip.
    Returns True on success.
    """
    # Clear first
    clear_cached_audio(clip_id, catalog_root)

    if progress_callback:
        progress_callback(
            "Rebuilding transcription proxy audio (24 kHz AAC mono + -3dB peak norm)..."
        )

    # Rebuild full length (no duration limit on explicit rebuild)
    audio_path = ensure_cached_audio(video_path, clip_id, catalog_root, max_duration=None)
    return audio_path is not None


# ---------------------------------------------------------------------------
# Persistent transcription / SRT cache (similar to previews and audio)
# Stored in <catalog>/transcriptions/
# Naming: 000001.srt (original) or 000001_fi.srt (Finnish translation)
# ---------------------------------------------------------------------------


def get_transcription_txt_path(clip_id: int, catalog_root: Path, lang: str = "original") -> Path:
    """Return path for plain text transcript (.txt) in /transcriptions."""
    txt_dir = get_transcriptions_dir(catalog_root)
    if lang and str(lang).lower() not in ("", "original"):
        lang_code = str(lang).lower()
        return txt_dir / f"{clip_id:06d}_{lang_code}.txt"
    else:
        return txt_dir / f"{clip_id:06d}.txt"


def load_transcript_segments(
    catalog_root: Path | None, clip_id: int | None, lang: str = "fi"
) -> list[dict[str, Any]]:
    """Parse the authoritative sidecar transcript .txt (e.g. 000012_fi.txt) into a list of
    segments with exact source times from the written file.

    This is the source of truth for timecodes (including any post-processing quantization,
    spreads, or real-TC mapping done at transcription time). We prefer this over in-memory
    clip.transcription_segments or LLM-re-emitted numbers.

    Returns list of {"text": str, "source_in": float, "source_out": float, "prefix": str}
    in the order they appear in the transcript. Empty list if file missing or unparsable.
    """
    if not catalog_root or clip_id is None:
        return []
    try:
        txt_dir = get_transcriptions_dir(catalog_root)
        candidates = []
        if lang and str(lang).lower() not in ("", "original"):
            candidates.append(txt_dir / f"{int(clip_id):06d}_{str(lang).lower()}.txt")
        candidates.append(txt_dir / f"{int(clip_id):06d}.txt")
        candidates.append(txt_dir / f"{int(clip_id):06d}_fi.txt")
        candidates.append(txt_dir / f"{int(clip_id):06d}_original.txt")
        candidates.append(txt_dir / f"{int(clip_id):06d}_en.txt")

        p = None
        for cand in candidates:
            if cand.exists():
                p = cand
                break
        if p is None:
            return []

        segs: list[dict[str, Any]] = []
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"\[([^\]]+)\]\s*(.+)$", line)
                if not m:
                    continue
                prefix, text = m.groups()
                text = text.strip()
                # Extract the media seconds, e.g. from "(8.8s) → (10.5s)" or "(8.8s) → 10:03:30:14 (10.5s)"
                times = re.findall(r"\(([0-9.]+)s\)", prefix)
                if len(times) >= 2:
                    try:
                        sin = float(times[0])
                        sout = float(times[1])
                        if sout > sin + 0.01:
                            segs.append(
                                {
                                    "text": text,
                                    "source_in": sin,
                                    "source_out": sout,
                                    "prefix": f"[{prefix}]",
                                    "file": str(p),
                                }
                            )
                    except Exception:
                        continue
        return segs
    except Exception as e:
        print(f"[load_transcript_segments] failed for clip {clip_id}: {e}")
        return []


def _normalize_for_match(t: str) -> str:
    t = (t or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


def resolve_source_range_for_text(
    query_text: str, transcript_segs: list[dict[str, Any]], *, llm_hint_in: float | None = None
) -> tuple[float, float] | None:
    """Given a (possibly composed or lightly paraphrased) narrative text from AI Journalist
    SELECTED SEGMENTS, find the best matching span of real transcript lines and return
    their (source_in, source_out) union.

    The trans .txt sidecar is the source of truth. This function's only job is to map
    a (possibly glued/rewritten) "Text" back to the *full contiguous block* of verbatim
    lines it quotes, so that SELECTED lists, XML clipitems, SRTs and renders all use
    the exact real timecodes from the authoritative transcript.

    Returns (in_s, out_s) rounded or None if no usable match.
    """
    if not query_text or not transcript_segs:
        return None
    q = _normalize_for_match(query_text)
    if not q or len(q) < 2:
        return None
    q_words = set(re.findall(r"\w{2,}", q))
    sin = 0.0
    sout = 0.0

    # 1. Exact
    for s in transcript_segs:
        if _normalize_for_match(s.get("text", "")) == q:
            return round(s["source_in"], 2), round(s["source_out"], 2)

    # 2. Broad candidate collection (literal substring OR decent word overlap).
    # Used later for the contiguous-block safety net. This is what catches long
    # combined narrative beats even when the LLM's emitted "Text" is a prefix or
    # light paraphrase of several consecutive sidecar lines.
    matches: list[dict[str, Any]] = []
    for s in transcript_segs:
        nt = _normalize_for_match(s.get("text", ""))
        if not nt or len(nt) <= 8:
            continue
        s_words = set(re.findall(r"\w{2,}", nt))
        inter = len(q_words & s_words)
        if (nt in q or q in nt) or inter >= 2:
            matches.append(s)

    # 2b. Near-exact string similarity. Guarded: only return a single line's range
    # if the query is not substantially longer than the line (i.e. not a multi-line
    # glue/rewrite). Long queries must go through anchor + expansion + block logic.
    best_ratio = 0.0
    best_line = None
    for s in transcript_segs:
        nt = _normalize_for_match(s.get("text", ""))
        if len(nt) <= 12:
            continue
        ratio = difflib.SequenceMatcher(None, q, nt).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_line = s
    if best_line and best_ratio >= 0.82:
        blen = len(_normalize_for_match(best_line.get("text", "")))
        if len(q) <= blen * 1.7:  # allow light edits but not obvious glues of 2+ lines
            return round(best_line["source_in"], 2), round(best_line["source_out"], 2)
        # else fall through so combined quotes get full span

    # 2c. Anchor on the line that best matches the *opening* of the query.
    # Prefer exact startswith (most reliable for "this is where the quoted story starts").
    # If that misses (e.g. LLM capitalized or tiny splice diff), fall back to prefix difflib
    # but then *prefer the temporally leftmost* plausible starter so we don't anchor mid-paragraph.
    pre_line = None
    for s in transcript_segs:
        nt = _normalize_for_match(s.get("text", ""))
        if nt and q.startswith(nt) and len(nt) > 5:
            if not pre_line or s["source_in"] < pre_line["source_in"]:
                pre_line = s
    if not pre_line:
        q_prefix = q[:120]
        best_pre_ratio = 0.0
        pre_cands = []
        for s in transcript_segs:
            nt = _normalize_for_match(s.get("text", ""))
            if len(nt) <= 8:
                continue
            ratio = difflib.SequenceMatcher(None, q_prefix, nt).ratio()
            if ratio >= 0.55:
                pre_cands.append((ratio, s))
        if pre_cands:
            # leftmost among reasonable prefix matches
            pre_cands.sort(key=lambda x: (-x[0], x[1]["source_in"]))
            pre_line = pre_cands[0][1]
    if pre_line:
        sin = pre_line["source_in"]
        sout = pre_line["source_out"]
        p_idx = -1
        try:
            p_idx = transcript_segs.index(pre_line)
        except ValueError:
            p_idx = -1
        if p_idx >= 0:
            # Extend forward across continuation lines using either substring or word overlap.
            # Relaxed: a sidecar line whose *start* appears in the query, or that shares 3+
            # words, or whose own words are a subset of the query's distinctive set, extends.
            for ii in range(p_idx, min(len(transcript_segs), p_idx + 12)):
                s = transcript_segs[ii]
                nt = _normalize_for_match(s.get("text", ""))
                if not nt:
                    break
                nt_words = set(re.findall(r"\w{3,}", nt))
                q3 = set(re.findall(r"\w{3,}", q))
                inter3 = len(q3 & nt_words)
                # extend if the line text is largely contained (even partial), or strong overlap,
                # or the line is a direct continuation in time from current sout with content words in q
                if (
                    nt in q
                    or q in nt
                    or inter3 >= 3
                    or (s["source_in"] - sout < 1.0 and inter3 >= 2)
                ):
                    sout = max(sout, s["source_out"])
                else:
                    # stop only if we've already covered a reasonable block and this line
                    # has zero relevant overlap (prevents runaway on back-to-back unrelated speech)
                    if sout > sin + 4.0 and inter3 == 0:
                        break
        if sout > sin + 0.05:
            # will still go through the final flood-fill below
            pass

    # 3. Word overlap scoring + neighbor expansion from best
    if len(q_words) < 2:
        # still may have collected matches; fall to safety net
        pass
    else:
        scored: list[tuple[float, dict[str, Any]]] = []
        for s in transcript_segs:
            nt = _normalize_for_match(s.get("text", ""))
            if len(nt) <= 12:
                continue
            s_words = set(re.findall(r"\w{2,}", nt))
            inter = len(q_words & s_words)
            if inter >= 1:
                union = len(q_words | s_words) or 1
                score = inter / float(union)
                scored.append((score, s))

        # Opening boost so the start of the quoted thought wins
        opening_words = set(re.findall(r"\w{3,}", q[:60]))
        if len(opening_words) >= 3:
            for ii, (sc, s) in enumerate(scored):
                nt = _normalize_for_match(s.get("text", ""))
                inter = len(opening_words & set(re.findall(r"\w{3,}", nt)))
                if inter >= 3:
                    scored[ii] = (sc + 8.0, s)

        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            best = scored[0][1]
            sin = best["source_in"]
            sout = best["source_out"]

            try:
                idx = transcript_segs.index(best)
            except ValueError:
                idx = -1
            if idx >= 0:
                # left expand (chained while shares >=2)
                i = idx - 1
                while i >= 0:
                    prev = transcript_segs[i]
                    pw = set(re.findall(r"\w{2,}", _normalize_for_match(prev.get("text", ""))))
                    if len(q_words & pw) >= 2:
                        sin = min(sin, prev["source_in"])
                        i -= 1
                    else:
                        break
                # right expand
                i = idx + 1
                while i < len(transcript_segs):
                    nxt = transcript_segs[i]
                    nw = set(re.findall(r"\w{2,}", _normalize_for_match(nxt.get("text", ""))))
                    if len(q_words & nw) >= 2:
                        sout = max(sout, nxt["source_out"])
                        i += 1
                    else:
                        break

            # 2nd best near in time
            if len(scored) > 1:
                c2 = scored[1][1]
                if abs(c2.get("source_in", 0) - best.get("source_in", 0)) < 20.0:
                    c2_words = set(re.findall(r"\w{2,}", _normalize_for_match(c2.get("text", ""))))
                    if len(q_words & c2_words) >= 2:
                        sin = min(sin, c2["source_in"])
                        sout = max(sout, c2["source_out"])

            # Force the true opening line (by opening words) even if scoring missed it
            if len(opening_words) >= 3:
                for s in transcript_segs:
                    nt = _normalize_for_match(s.get("text", ""))
                    if len(nt) > 8:
                        inter = len(opening_words & set(re.findall(r"\w{3,}", nt)))
                        if inter >= 3:
                            sin = min(sin, s["source_in"])
                            sout = max(sout, s["source_out"])
                            break

    # --- Final safety net: flood-fill to the maximal contiguous spoken block ---
    # Collect every line that (a) shares >=2 words with the query, or (b) is temporally
    # inside or immediately adjacent (<5s gap) to the current candidate span.
    # Then take the min/max over the connected component(s) that overlap our candidate.
    # This is what makes combined "entire sentence / story" beats 100% reliable even
    # when the LLM's Text truncated the quote, scoring picked an internal line first,
    # or the pre_line forward-extend was conservative.
    cand_sin = 0.0
    cand_sout = 0.0
    # pick up whatever the earlier branches set (pre_line path or scored path)
    if "sin" in locals() and "sout" in locals():
        cand_sin = sin
        cand_sout = sout
    # start from whatever the paths above produced (or 0 if none)
    final_sin = cand_sin
    final_sout = cand_sout
    if not matches:
        # rebuild a candidate set from any line with decent overlap
        for s in transcript_segs:
            nt = _normalize_for_match(s.get("text", ""))
            if len(nt) <= 8:
                continue
            sw = set(re.findall(r"\w{2,}", nt))
            if len(q_words & sw) >= 2:
                matches.append(s)
    if matches:
        matches.sort(key=lambda x: x["source_in"])
        # group into contiguous blocks (spoken paragraphs). Use tight gap (2s) so that
        # interviewer prompts immediately before an answer do not get glued in just because
        # of <5s silence. Real combined quotes from the speaker are back-to-back <1s.
        blocks = []
        cur = [matches[0]]
        for m in matches[1:]:
            if m["source_in"] - cur[-1]["source_out"] < 2.0:
                cur.append(m)
            else:
                blocks.append(cur)
                cur = [m]
        blocks.append(cur)
        # Prefer the block that (1) contains a strong opening match for the query start,
        # or (2) overlaps our candidate from pre/scored, else largest.
        opening_words = set(re.findall(r"\w{3,}", q[:70]))
        best_block = None
        best_score = -1
        for b in blocks:
            bmin = min(x["source_in"] for x in b)
            bmax = max(x["source_out"] for x in b)
            score = 0
            # does it contain the actual start of the quote?
            for line in b:
                nt = _normalize_for_match(line.get("text", ""))
                if nt and (q.startswith(nt) or nt in q[:120]):
                    score += 100
                    break
            if opening_words:
                for line in b:
                    nt = _normalize_for_match(line.get("text", ""))
                    if len(set(re.findall(r"\w{3,}", nt)) & opening_words) >= 3:
                        score += 50
                        break
            # temporal overlap with what pre/scored already decided
            if final_sout > final_sin + 0.1:
                os = max(bmin, final_sin)
                oe = min(bmax, final_sout)
                if oe > os:
                    score += 10 + (oe - os)
            # size tiebreaker only if no opening signal
            if score == 0:
                score = 0.001 * (bmax - bmin)
            if score > best_score:
                best_score = score
                best_block = (bmin, bmax)
        if best_block:
            final_sin = min(final_sin or best_block[0], best_block[0])
            final_sout = max(final_sout or best_block[1], best_block[1])
        else:
            b0 = blocks[0]
            final_sin = min(final_sin or b0[0]["source_in"], b0[0]["source_in"])
            final_sout = max(final_sout or b0[-1]["source_out"], b0[-1]["source_out"])

    # final inclusive flood: only lines that have *some* word overlap with q and sit inside/bridge the window.
    # This prevents pulling unrelated adjacent speech (interviewer turns etc).
    if final_sout > final_sin + 0.1:
        q3 = set(re.findall(r"\w{3,}", q))
        for s in transcript_segs:
            si = s["source_in"]
            so = s["source_out"]
            sw3 = set(re.findall(r"\w{3,}", _normalize_for_match(s.get("text", ""))))
            has_overlap = len(q3 & sw3) >= 1
            inside_or_bridge = (si >= final_sin - 1.0 and so <= final_sout + 1.0) or (
                si < final_sout + 0.5 and so > final_sin - 0.5
            )
            if has_overlap and inside_or_bridge:
                final_sin = min(final_sin, si)
                final_sout = max(final_sout, so)

    if final_sout > final_sin + 0.05:
        return round(final_sin, 2), round(final_sout, 2)

    # last resort: if we have any matches at all, return the largest block's span
    if matches:
        matches.sort(key=lambda x: x["source_in"])
        # largest single contiguous (tight gap)
        blocks = []
        cur = [matches[0]]
        for m in matches[1:]:
            if m["source_in"] - cur[-1]["source_out"] < 2.0:
                cur.append(m)
            else:
                blocks.append(cur)
                cur = [m]
        blocks.append(cur)
        largest = max(blocks, key=lambda b: b[-1]["source_out"] - b[0]["source_in"])
        bmin = min(x["source_in"] for x in largest)
        bmax = max(x["source_out"] for x in largest)
        if bmax > bmin + 0.05:
            return round(bmin, 2), round(bmax, 2)

    return None


def repair_journalist_segments_with_transcript(
    selected_segments: list[dict[str, Any]],
    catalog_root: Path | None,
    clip_id: int | None,
    lang: str = "fi",
) -> list[dict[str, Any]]:
    """Return a shallow-copied list of selected_segments where source_in/source_out (and
    legacy start/end) have been replaced by the exact ranges from the trans .txt sidecar
    whenever a text match (exact/contains/fuzzy) succeeds.

    This is the enforcement point for "always use the timecodes we already have in the
    transscript, instead of in memory segments/db" for AI Journalist rewrite cuts.
    Call this at post-generate, on every render of the version list, and at every export
    (XML, TXT, SRT-driven audio/video renders) for maximum safety even on old/stored versions.

    If no .txt or no match for a seg, that seg keeps its incoming numbers (last resort).
    """
    if not selected_segments:
        return selected_segments
    trans = load_transcript_segments(catalog_root, clip_id, lang)
    if not trans:
        # No sidecar available; return copies unchanged
        return [dict(s) for s in selected_segments]

    repaired: list[dict[str, Any]] = []
    for seg in selected_segments:
        new_seg = dict(seg)  # copy top level
        q = (seg.get("text") or seg.get("reason") or "").strip()
        # Pass the (possibly hallucinated) LLM time as hint so that when there are multiple similar
        # phrases in the interview, we prefer the one the architect "meant".
        llm_hint = None
        try:
            llm_hint = float(seg.get("source_in") or seg.get("start") or 0)
            if llm_hint <= 0:
                llm_hint = None
        except Exception:
            llm_hint = None
        resolved = resolve_source_range_for_text(q, trans, llm_hint_in=llm_hint)
        if resolved:
            sin, sout = resolved
            new_seg["source_in"] = sin
            new_seg["source_out"] = sout
            # legacy aliases some code paths still read
            new_seg["start"] = sin
            new_seg["end"] = sout
            # helpful debug
            orig_in = seg.get("source_in") or seg.get("start", 0)
            orig_out = seg.get("source_out") or seg.get("end", 0)
            orig_dur = float(orig_out) - float(orig_in) if orig_out else 0
            new_dur = sout - sin
            grew = ""
            if new_dur > orig_dur + 0.1:
                grew = f" [GREW +{new_dur - orig_dur:.1f}s to cover full quote]"
            print(f"[repair_transcript] '{q[:40]}...' -> {sin}s–{sout}s (was {orig_in}) {grew}")
        repaired.append(new_seg)
    return repaired


def _match_source_video(seg: dict[str, Any], catalog_videos: list[Any]) -> Any | None:
    """Given a Director segment/item carrying source_filename / source_path / source_label,
    find the best matching Video object from the catalog list (to get its .id for sidecar lookup,
    and optionally .tc_start / .fps for real-TC formatting).
    """
    if not catalog_videos:
        return None
    fn = (
        seg.get("source_filename") or seg.get("source_path") or seg.get("source_label") or ""
    ).strip()
    if not fn:
        return None
    fn_lower = fn.lower()
    stem = Path(fn).stem.lower()
    for v in catalog_videos:
        try:
            vfn = getattr(v, "filename", None) or (
                Path(getattr(v, "path", "")).name if getattr(v, "path", None) else ""
            )
            if not vfn:
                continue
            vfn_l = str(vfn).lower()
            vstem = Path(vfn).stem.lower()
            vpath = str(getattr(v, "path", "") or "").lower()
            if (
                (fn_lower and fn_lower in vfn_l)
                or (stem and stem in vstem)
                or (fn_lower and fn_lower in vpath)
            ):
                return v
            # also exact name match
            if vfn_l == fn_lower or vstem == stem:
                return v
        except Exception:
            continue
    return None


def repair_director_version_with_transcripts(
    ver: dict[str, Any],
    catalog_root: Path | None,
    catalog_videos: list[Any] | None = None,
    default_lang: str = "fi",
) -> dict[str, Any]:
    """Return a shallow copy of a Director version dict whose clip items (in selected_segments
    and narrative_elements) have had their source_in/source_out re-resolved from the
    *authoritative per-source trans .txt sidecars* (using verbatim text match + the full
    contiguous block logic in resolve_source_range_for_text).

    This makes AI Director multi-clip exports obey the same rule as single-clip Journalist:
    "the perfect transcription (with real TC + verbatim text) is the source of truth" and
    "always use the timecodes we already have in the transscript, instead of in memory segments/db".

    Especially important for rewrite-tone stories where a narrative "Text" (or clip item text)
    glues multiple consecutive lines within one Cxx source — the LLM-provided window is often
    short/partial; this forces the full spoken paragraph from that source's sidecar.

    Call at post-generate (after _reattach), before every _render of the version list,
    and unconditionally right before writing XML or the rich MULTI-CLIP SCRIPT.txt (and
    before any cut rendering / SRT from the version).

    If no catalog or no sidecar for a source, that item's numbers are left as-is (last resort).
    """
    if not ver:
        return ver
    if not catalog_root:
        try:
            from minicat.ui.app import get_state

            st = get_state()
            catalog_root = getattr(st, "catalog_root", None) if st else None
        except Exception:
            pass
    if not catalog_root:
        return {k: v for k, v in ver.items()}  # nothing we can do

    # Build a working copy
    out = {k: v for k, v in ver.items()}
    if not catalog_videos:
        try:
            from minicat.ui.app import get_state

            st = get_state()
            catalog_videos = getattr(st, "videos", None) or []
        except Exception:
            catalog_videos = []

    def _repair_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not items:
            return items
        repaired_items: list[dict[str, Any]] = []
        for it in items:
            new_it = dict(it)
            if new_it.get("type") == "narration":
                repaired_items.append(new_it)
                continue
            # clip item (either bare selected_segments entry or {"type":"clip", ...})
            q = (new_it.get("text") or new_it.get("reason") or "").strip()
            if not q:
                repaired_items.append(new_it)
                continue

            v = _match_source_video(new_it, catalog_videos or [])
            clip_id = getattr(v, "id", None) if v else None
            if clip_id is None:
                # last resort: try to parse a numeric clip id if somehow present
                try:
                    clip_id = int(new_it.get("source_clip_index") or new_it.get("clip_id") or -1)
                    if clip_id < 0:
                        clip_id = None
                except Exception:
                    clip_id = None

            if not clip_id:
                repaired_items.append(new_it)
                continue

            # lang fallback: use video's known transcription lang if available (so we load the exact
            # sidecar that matches the segments that were fed to the Director), else the passed default.
            # This ensures resolve matches against the correct language version of the transcript.
            lang = new_it.get("lang") or new_it.get("language") or default_lang or "fi"
            if v:
                vid_lang = getattr(v, "_current_transcription_lang", None) or getattr(
                    v, "original_language", None
                )
                if vid_lang:
                    lang = vid_lang
            trans = load_transcript_segments(catalog_root, int(clip_id), str(lang))
            if not trans:
                repaired_items.append(new_it)
                continue

            llm_hint = None
            try:
                llm_hint = float(new_it.get("source_in") or new_it.get("start") or 0)
                if llm_hint <= 0:
                    llm_hint = None
            except Exception:
                llm_hint = None

            resolved = resolve_source_range_for_text(q, trans, llm_hint_in=llm_hint)
            if resolved:
                sin, sout = resolved
                new_it["source_in"] = sin
                new_it["source_out"] = sout
                new_it["start"] = sin
                new_it["end"] = sout
                orig_in = it.get("source_in") or it.get("start", 0)
                orig_out = it.get("source_out") or it.get("end", 0)
                orig_dur = float(orig_out) - float(orig_in) if orig_out else 0.0
                new_dur = sout - sin
                grew = ""
                if new_dur > orig_dur + 0.1:
                    grew = f" [GREW +{new_dur - orig_dur:.1f}s to cover full quote from sidecar]"
                print(
                    f"[repair_director_transcript] [{new_it.get('source_label', '?')}] '{q[:35]}...' -> {sin}s–{sout}s (was {orig_in}) {grew}"
                )
            repaired_items.append(new_it)
        return repaired_items

    # Repair legacy flat list
    if out.get("selected_segments"):
        out["selected_segments"] = _repair_list(out["selected_segments"])

    # Repair rich interleaved
    if out.get("narrative_elements"):
        elems = out["narrative_elements"]
        if isinstance(elems, list):
            out["narrative_elements"] = _repair_list(elems)

    return out


def get_subtitle_srt_path(clip_id: int, catalog_root: Path, lang: str = "original") -> Path:
    """Return path for timed subtitle (.srt) in /subtitles."""
    srt_dir = get_subtitles_dir(catalog_root)
    if lang and str(lang).lower() not in ("", "original"):
        lang_code = str(lang).lower()
        return srt_dir / f"{clip_id:06d}_{lang_code}.srt"
    else:
        return srt_dir / f"{clip_id:06d}.srt"


def get_available_subtitle_languages(clip_id: int, catalog_root: Path) -> list[tuple[str, str]]:
    """
    Returns list of (lang_code, display_name) for available .srt files for this clip.
    Example: [("original", "Original"), ("fi", "Finnish (fi)"), ("en", "English (en)")]
    """
    srt_dir = get_subtitles_dir(catalog_root)
    if not srt_dir.exists():
        return []

    results = []
    padded = f"{clip_id:06d}"

    for f in sorted(srt_dir.glob(f"{padded}*.srt")):
        name = f.stem  # e.g. "000042" or "000042_fi"
        if name == padded:
            results.append(("original", "Original"))
        elif name.startswith(padded + "_"):
            lang_code = name[len(padded) + 1 :]
            # Simple display name (can be improved later with a proper map)
            display = lang_code.upper()
            if lang_code.lower() in ("fi", "fin"):
                display = "Finnish (fi)"
            elif lang_code.lower() in ("en", "eng"):
                display = "English (en)"
            elif lang_code.lower() in ("sv", "swe"):
                display = "Swedish (sv)"
            elif lang_code.lower() in ("de", "deu"):
                display = "German (de)"
            results.append((lang_code, display))

    # Ensure "original" is first if present
    results.sort(key=lambda x: (0 if x[0] == "original" else 1, x[1]))
    return results


def _seconds_to_frames(seconds: float, fps: float) -> int:
    """Convert seconds to nearest frame count at the given fps."""
    if not fps or fps <= 0:
        return 0
    return int(round(float(seconds) * fps))


def _format_offset_timecode(seconds: float, fps: float = 25.0) -> str:
    """Format media-relative seconds as HH:MM:SS:FF (from head) for the given fps.
    This produces the offset that, when the clip's tc_start is 00:00:00:00, matches
    exactly what the user sees in Premiere's timecode for that position in the source file.
    """
    if not fps or fps <= 0:
        fps = 25.0
    frames = _seconds_to_frames(seconds, fps)
    tb = int(round(fps))
    h = frames // (tb * 3600)
    m = (frames // (tb * 60)) % 60
    s = (frames // tb) % 60
    f = frames % tb
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def segments_to_plain_text(
    segments: list[dict],
    include_timestamps: bool = True,
    *,
    fps: float | None = None,
    base_timecode: str | None = None,
) -> str:
    """Convert segments to a simple readable plain text transcript.
    Always sorts by time for sane output even if stored list had ordering issues.

    If fps is provided, the timestamp line includes the frame-accurate timecode
    (HH:MM:SS:FF) in addition to seconds.

    If base_timecode (the clip's real embedded start timecode, e.g. "11:31:22:17")
    is provided and non-00:00, the displayed [TC] will be the *real* timecode at that
    media position (base + media_offset), so it matches exactly what you see on the
    timeline in Premiere for this clip. The (seconds) values remain media-head relative
    (from file start) for reference to the actual spoken offsets.
    """
    # Defensive sanitize on input. This applies the full repair logic (debunch collapsed
    # end timestamps, prune junk, etc.) to *old* bad data that may already be stored in
    # the DB transcription JSON or on-disk .txt files. Makes "Export as TXT Script" and
    # inspector transcript views immediately better without requiring a re-transcribe.
    # Only applied to "raw" transcription segments (those using "start"/"end"); we skip
    # for AI Director narrative_elements that already use source_in/source_out + reason etc.
    try:
        segs = list(segments or [])
        looks_like_transcript = bool(segs and any("start" in s for s in segs[:3]))
        if looks_like_transcript:
            from minicat.ai.transcriber import sanitize_transcription_segments

            # Use the highest end time present as a proxy for "known duration" so end-cluster
            # repair can still trigger for old data that lacks an external duration.
            proxy_dur = None
            try:
                if segs:
                    proxy_dur = max(float(s.get("end") or s.get("source_out") or 0) for s in segs)
            except Exception:
                proxy_dur = None
            segments = sanitize_transcription_segments(segs, max_duration=proxy_dur)
    except Exception:
        pass

    # Defensive sort (sanitizer should have done this, but old data or direct calls may not)
    try:
        sorted_segs = sorted(
            segments or [], key=lambda s: float(s.get("source_in") or s.get("start") or 0)
        )
    except Exception:
        sorted_segs = segments or []

    lines = []
    for seg in sorted_segs:
        start = seg.get("source_in") or seg.get("start", 0)
        end = seg.get("source_out") or seg.get("end", 0)
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if include_timestamps:
            sec_part = f"{float(start):.1f}s → {float(end):.1f}s"
            if fps and fps > 0:
                if base_timecode and base_timecode not in (None, "00:00:00:00"):
                    tc_start = _add_duration_to_timecode(base_timecode, float(start), fps)
                    tc_end = _add_duration_to_timecode(base_timecode, float(end), fps)
                else:
                    tc_start = _format_offset_timecode(float(start), fps)
                    tc_end = _format_offset_timecode(float(end), fps)
                time_part = f"[{tc_start} ({float(start):.1f}s) → {tc_end} ({float(end):.1f}s)]"
            else:
                time_part = f"[{sec_part}]"
            lines.append(f"{time_part} {text}")
        else:
            lines.append(text)
    return "\n\n".join(lines)


def format_transcript_timecode(
    start: float, end: float, *, fps: float | None = None, base_timecode: str | None = None
) -> str:
    """Format seconds as the standard source transcript timecode display.

    Returns e.g. "[10:03:30:19 (10.7s) → 10:03:33:20 (13.8s)]"
    using the same logic as save_transcription_txt / segments_to_plain_text
    so that journalist scripts etc. can show the "correct" timecodes matching the
    transscript files (real clip TC when available + media seconds in parens).
    """
    if fps is None or fps <= 0:
        fps = 25.0
    start = float(start or 0)
    end = float(end or 0)
    if base_timecode and str(base_timecode).strip() not in ("", "00:00:00:00"):
        tc_start = _add_duration_to_timecode(str(base_timecode), start, fps)
        tc_end = _add_duration_to_timecode(str(base_timecode), end, fps)
    else:
        tc_start = _format_offset_timecode(start, fps)
        tc_end = _format_offset_timecode(end, fps)
    return f"[{tc_start} ({start:.1f}s) → {tc_end} ({end:.1f}s)]"


def save_transcription_txt(
    clip_id: int,
    catalog_root: Path,
    segments: list[dict],
    lang: str = "original",
    *,
    fps: float | None = None,
    base_timecode: str | None = None,
) -> Path | None:
    """Save plain text transcript (.txt) to /transcriptions.
    If fps is provided, the output lines will include frame-accurate timecodes
    (HH:MM:SS:FF) next to the seconds.

    If base_timecode (the clip's real embedded start timecode, e.g. "11:31:22:17")
    is provided, the [HH:MM:SS:FF] will be the *real* timecode at the spoken position
    (matching what Premiere shows on the clip's timeline).
    """
    # Robust resolution of fps + base_timecode (real embedded TC) so that even
    # direct calls without the params get correct "real timecode" display in the
    # generated .txt (matching what user sees in Premiere for the clip).
    if not fps or fps <= 0 or base_timecode is None:
        try:
            from minicat.core import db as _db

            vids = _db.get_videos_by_ids(catalog_root, [clip_id])
            if vids:
                v = vids[0]
                if not fps or fps <= 0:
                    from minicat.core.video import confirm_video_framerate

                    fps = confirm_video_framerate(v.path)
                if base_timecode is None:
                    base_timecode = getattr(v, "tc_start", None)
        except Exception:
            pass
    if not fps or fps <= 0:
        fps = 25.0

    try:
        content = segments_to_plain_text(segments, fps=fps, base_timecode=base_timecode)
        target = get_transcription_txt_path(clip_id, catalog_root, lang)
        target.parent.mkdir(parents=True, exist_ok=True)

        # Standard header explaining the provenance of the timestamps (per "transcribe old way,
        # then turn AI timestamps into TIMECODE" workflow).
        fps_str = f"{fps:.3f}".rstrip("0").rstrip(".")
        tc_note = ""
        if base_timecode and base_timecode not in (None, "00:00:00:00"):
            tc_note = f" | clip real start TC: {base_timecode}"
        header = (
            "# CAT+TAG source transcript\n"
            f"# Times are seconds from media head (00:00:00.000) as provided by the AI transcription (quantized to {fps_str} fps frame grid{tc_note}).\n"
            "# [HH:MM:SS:FF (seconds) → ...] is our post-processing that turns the AI seconds into\n"
            "# proper frame-accurate timecode for your video's fps (so it matches Premiere scrubbing).\n"
            "# When the clip has a non-zero embedded start timecode, the [TC] shows the real timecode\n"
            "# (media seconds in parentheses remain from file head for spoken offset reference).\n"
            "# A minimal local spread (or backward re-anchor for tail content) may have been applied\n"
            "# only to groups where the model assigned identical (or end-crammed) timestamps.\n"
            "# Large gaps mean the model did not return content/timing for that portion.\n\n"
        )
        full = header + content
        target.write_text(full, encoding="utf-8")
        print(f"[Transcriptions] Saved TXT: {target.name}")
        return target
    except Exception as ex:
        print(f"[Transcriptions] Failed to save TXT for clip {clip_id} (lang={lang}): {ex}")
        return None


def save_transcription_srt(
    clip_id: int,
    catalog_root: Path,
    segments: list[dict],
    lang: str = "original",
    fps: float | None = None,
    base_timecode: str | None = None,
) -> Path | None:
    """
    Save the given segments as an .srt file in the /subtitles folder.

    For source transcript SRTs we deliberately *preserve the original spoken
    times* from the transcription (so .srt times match the .txt and the
    timeline you see when scrubbing the clip in Premiere). We only apply
    text line-breaking (39 chars, max 2 lines) for readability.

    The heavier YLE timing rules (CPS, min/max dur, strict gaps that can
    shift start times for better reading) are still applied to AI-cut
    exports and when burning subtitles.

    fps: Optional video frame rate (e.g. 25.0). If given, the generated .srt
         timecodes will be quantized to exact frame boundaries so they align
         perfectly with the video's timeline/frames (fixes "wrong fps" perception
         when using the SRT with 25fps or 24fps video in Premiere, VLC, burning, etc.).
         A short comment header is prepended (safe for SRT parsers) noting the
         source fps and Premiere import steps (e.g. Interpret Footage > Assume 25).
    """
    from minicat.ai.transcriber import segments_to_srt

    # Robust fps resolution: ALWAYS confirm from the video file itself when possible.
    # The user requirement is to confirm framerate before transcription-related work.
    if not fps or fps <= 0:
        try:
            from minicat.core import db as _db

            vids = _db.get_videos_by_ids(catalog_root, [clip_id])
            if vids:
                video_path = vids[0].path
                from minicat.core.video import confirm_video_framerate

                fps = confirm_video_framerate(video_path)
        except Exception:
            pass
    if not fps or fps <= 0 or base_timecode is None:
        try:
            from minicat.core import db as _db

            vids = _db.get_videos_by_ids(catalog_root, [clip_id])
            if vids:
                v = vids[0]
                if not fps or fps <= 0:
                    from minicat.core.video import confirm_video_framerate

                    fps = confirm_video_framerate(v.path)
                if base_timecode is None:
                    base_timecode = getattr(v, "tc_start", None)
        except Exception:
            pass
    if not fps or fps <= 0:
        fps = 25.0

    try:
        # For *source transcript* SRTs (the per-clip reference files), preserve
        # the exact spoken times from the transcription so they match the .txt
        # and the timeline the user sees in Premiere. We only break text for
        # readability (39c/line, max 2 lines). No CPS/gap shifting that can
        # move a block's start away from when the words were spoken.
        #
        # Full YLE timing redistribution (that can adjust starts for reading
        # speed and strict gaps) is still used for AI cut exports and burned subs.
        from minicat.ai.transcriber import source_transcript_to_srt_segments

        processed_segments = source_transcript_to_srt_segments(segments, fps=fps)

        srt_content = segments_to_srt(
            processed_segments, strict_timing=True, fps=fps, base_timecode=base_timecode
        )

        target = get_subtitle_srt_path(clip_id, catalog_root, lang)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(srt_content, encoding="utf-8")
        print(f"[Subtitles] Saved SRT: {target.name}")

        # Also persist a high-quality EBU/YLE-styled .ass alongside.
        # ASS carries the same snapped timings + professional styling and is
        # excellent for burning or round-tripping via Subtitle Edit etc.
        # Premiere users having "30 fps" label issues with SRT can often get
        # better results by converting the .ass (or using Subtitle Edit on the pair).
        try:
            from minicat.ai.transcriber import segments_to_ass

            ass_content = segments_to_ass(
                segments,  # raw segments; function will format + snap internally
                title=f"CAT+TAG Transcript {clip_id} ({lang})",
                fps=fps or 25.0,
            )
            ass_target = target.with_suffix(".ass")
            ass_target.write_text(ass_content, encoding="utf-8")
            print(f"[Subtitles] Saved ASS companion: {ass_target.name}")
        except Exception as ass_ex:
            print(f"[Subtitles] (non-fatal) Could not also save .ass for {clip_id}: {ass_ex}")

        return target
    except Exception as ex:
        print(f"[Subtitles] Failed to save SRT for clip {clip_id} (lang={lang}): {ex}")
        return None


def cleanup_orphaned_catalog_files(
    catalog_root: Path, existing_clip_ids: set[int] | None = None
) -> int:
    """
    Automatic maintenance: remove orphaned files from the catalog that no longer have
    a corresponding clip.

    Currently cleans:
    - Transcription proxy audio (<catalog>/audio/ 00000x.m4a and legacy .wav)
    - Plain transcripts (<catalog>/transcriptions/ 00000x*.txt and legacy)
    - Subtitles (<catalog>/subtitles/ 00000x*.srt + *.ass companions)
    - Preview thumbnails and storyboards (in <catalog>/previews/thumbs and /boards, named 000042.jpg etc.)
    - Proxy files (<catalog>/proxies/**  <stem>_proxy.* or id-containing)
    - Stale FTS search entries and dangling video_tags (ensures *no* clip information remains in the .db after deletes)

    The DB safety purge (FTS + video_tags) ALWAYS runs.
    If existing_clip_ids is None, it will query the DB (slower but safe).
    Returns number of orphaned files deleted.
    """
    from minicat.core import db as _db

    deleted = 0

    if existing_clip_ids is None:
        try:
            # Get all current video ids (lightweight)
            videos = _db.search_videos(catalog_root, limit=100000)
            existing_clip_ids = {v.id for v in videos if v.id}
        except Exception:
            existing_clip_ids = set()
            videos = []
    else:
        videos = []

    # For proxy stem-based orphan detection (proxies are named after original media stem)
    active_stems: set[str] = set()
    try:
        if videos:
            active_stems = {
                Path(getattr(v, "filename", "") or "").stem
                for v in videos
                if getattr(v, "filename", None)
            }
        else:
            vids = _db.search_videos(catalog_root, limit=100000)
            active_stems = {
                Path(getattr(v, "filename", "") or "").stem
                for v in vids
                if getattr(v, "filename", None)
            }
    except Exception:
        active_stems = set()

    existing_clip_ids = existing_clip_ids or set()

    # Transcription proxy audio (do not early-return; other cleanups + DB safety must always run)
    audio_dir = get_audio_dir(catalog_root)
    audio_deleted = 0
    if audio_dir.exists():
        # Support current .m4a (processed 24 kHz AAC proxy) + legacy WAV patterns
        # (old _transcribe / _listen, unpadded "42.wav", padded "000042.wav")
        patterns = ["*_*.*", "*.wav", "*.m4a"]
        for pattern in patterns:
            for f in audio_dir.glob(pattern):
                if not f.is_file():
                    continue
                stem = f.stem
                cid = None

                # Current padded pattern ("000042.m4a" or "000042.wav") or legacy unpadded ("42.wav")
                if "_" not in stem and stem.isdigit():
                    try:
                        cid = int(stem)
                    except ValueError:
                        continue
                # Old patterns: "123_transcribe.wav" or "123_listen.mp3"
                elif "_" in stem:
                    try:
                        cid_str = stem.split("_", 1)[0]
                        cid = int(cid_str)
                    except (ValueError, IndexError):
                        continue

                if cid is not None:
                    if cid not in existing_clip_ids:
                        # Truly orphaned (clip was deleted from this catalog)
                        try:
                            f.unlink()
                            deleted += 1
                            audio_deleted += 1
                            print(f"[Audio Cache] Removed orphaned file: {f.name}")
                        except Exception:
                            pass
                    else:
                        # Live clip: remove any legacy .wav (pre-24 kHz AAC proxy upgrade).
                        # The canonical file is now always the processed .m4a.
                        # This prevents users from seeing old .wav files lingering in <catalog>/audio/
                        # after the transcription pre-processing upgrade. Next ensure/rebuild/transcribe
                        # for the clip will create the modern proxy.
                        if f.suffix.lower() == ".wav":
                            try:
                                f.unlink()
                                deleted += 1
                                audio_deleted += 1
                                print(
                                    f"[Audio Cache] Removed legacy WAV for live clip {cid}: {f.name}"
                                )
                            except Exception:
                                pass

    if audio_deleted:
        print(
            f"[Audio Cache] Cleanup complete: removed {audio_deleted} orphaned/legacy audio file(s)"
        )

    # Clean orphaned transcription .txt and .srt files
    try:
        txt_dir = get_transcriptions_dir(catalog_root)
        if txt_dir.exists():
            for f in list(txt_dir.glob("*.txt")) + list(txt_dir.glob("*.srt")):
                if not f.is_file():
                    continue
                stem = f.stem
                cid_str = stem.split("_", 1)[0]
                if cid_str.isdigit():
                    cid = int(cid_str)
                    if cid not in existing_clip_ids:
                        f.unlink()
                        deleted += 1
                        print(f"[Transcriptions] Removed orphaned file: {f.name}")
    except Exception as ex:
        print(f"[Transcriptions] Error during orphan cleanup: {ex}")

    try:
        srt_dir = get_subtitles_dir(catalog_root)
        if srt_dir.exists():
            for f in list(srt_dir.glob("*.srt")) + list(srt_dir.glob("*.ass")):
                if not f.is_file():
                    continue
                stem = f.stem
                cid_str = stem.split("_", 1)[0]
                if cid_str.isdigit():
                    cid = int(cid_str)
                    if cid not in existing_clip_ids:
                        f.unlink()
                        deleted += 1
                        print(f"[Subtitles] Removed orphaned subtitle file: {f.name}")
    except Exception as ex:
        print(f"[Subtitles] Error during orphan cleanup: {ex}")

    # Clean orphaned preview thumbnails and storyboards (in previews/thumbs and previews/boards)
    try:
        previews_root = catalog_root / "previews"
        if previews_root.exists():
            for f in previews_root.rglob("*"):
                if not f.is_file():
                    continue
                stem = f.stem
                # names are like 000042.jpg
                if stem.isdigit():
                    cid = int(stem)
                    if cid not in existing_clip_ids:
                        try:
                            f.unlink()
                            deleted += 1
                            try:
                                rel = f.relative_to(previews_root)
                            except Exception:
                                rel = f.name
                            print(f"[Previews] Removed orphaned preview: {rel}")
                        except Exception:
                            pass
    except Exception as ex:
        print(f"[Previews] Error during orphan cleanup: {ex}")

    # Clean orphaned proxy files (named <source_stem>_proxy.<ext> under proxies/ or subdirs;
    # also catches any that embed the padded clip id). Uses both id heuristic and source-stem match.
    try:
        proxies_root = catalog_root / "proxies"
        if proxies_root.exists():
            for f in proxies_root.rglob("*"):
                if not f.is_file():
                    continue
                fname = f.name
                if "proxy" not in fname.lower():
                    continue
                # Try to detect via embedded padded clip id (defensive for mixed naming)
                cid = None
                stem_part = Path(fname).stem
                if "_" not in stem_part and stem_part.isdigit():
                    try:
                        cid = int(stem_part)
                    except ValueError:
                        cid = None
                elif "_" in stem_part:
                    try:
                        cid_str = stem_part.split("_", 1)[0]
                        if cid_str.isdigit():
                            cid = int(cid_str)
                    except (ValueError, IndexError):
                        cid = None
                if cid is not None and cid in existing_clip_ids:
                    continue
                # Primary detection: derive base stem before _proxy etc and match against current catalog media stems
                base = fname
                low = fname.lower()
                for marker in ("_proxy", "-proxy", " proxy"):
                    if marker in low:
                        idx = low.find(marker)
                        base = fname[:idx]
                        break
                base_stem = Path(base).stem
                if base_stem and base_stem in active_stems:
                    continue
                # This is an orphan proxy
                try:
                    f.unlink()
                    deleted += 1
                    try:
                        rel = f.relative_to(proxies_root)
                    except Exception:
                        rel = f.name
                    print(f"[Proxies] Removed orphaned proxy: {rel}")
                except Exception:
                    pass
    except Exception as ex:
        print(f"[Proxies] Error during orphan cleanup: {ex}")

    if deleted > 0:
        print(f"[Catalog Cleanup] Total orphaned files removed this run: {deleted}")

    # Also purge any stale FTS entries and dangling video_tags for clips that no longer exist.
    # This ensures *no* clip information (rows, tags, search index) lingers in .db.
    # We force ensure_fts_consistency first to heal any stale FTS definition left from
    # old catalogs (the root cause of "no such column: T.tag_names" on videos_fts ops).
    try:
        from minicat.core.db import ensure_fts_consistency, get_connection

        ensure_fts_consistency(catalog_root)
        with get_connection(catalog_root) as conn:
            # FTS delete is best-effort (ensure above makes it safe in the common case);
            # the video_tags purge is non-negotiable for the zero-ghosts-in-db contract.
            try:
                conn.execute("DELETE FROM videos_fts WHERE rowid NOT IN (SELECT id FROM videos)")
            except Exception as fts_ex:
                # Do not emit the scary "[DB] Error cleaning..." for FTS; ensure already ran and healed for future ops.
                print(f"[DB] FTS stale-rowid purge note (non-fatal): {fts_ex}")
            conn.execute("DELETE FROM video_tags WHERE video_id NOT IN (SELECT id FROM videos)")
    except Exception as ex:
        msg = str(ex)
        if "tag_names" in msg.lower() or "no such column" in msg.lower():
            print(f"[DB] Stale FTS/video_tags purge note (ensure healing was attempted): {ex}")
        else:
            print(f"[DB] Error cleaning stale FTS / video_tags: {ex}")

    return deleted


def cleanup_all_generated_files_for_clip(
    clip_id: int, catalog_root: Path, *, original_filename: str | None = None
) -> int:
    """
    Thoroughly removes all generated artifacts for a clip when it is deleted from the library.

    ZERO GHOST FILES CONTRACT:
    When called (before or alongside DB removal), this + the delete_video/delete_project
    paths + orphan purge on startup guarantee:
    - No preview files (thumbs or boards) remain under previews/
    - No transcription proxy audio (.m4a) remains under audio/
    - No transcription .txt (or lang variants) remain under transcriptions/
    - No subtitle .srt/.ass remain under subtitles/
    - No proxy files remain under proxies/ (by id or source stem)
    - No clip row, no video_tags join rows, no videos_fts entries for the clip remain in catalog.db

    Deletes:
    - Cached transcription proxy audio (.m4a)
    - All preview thumbnails and storyboards (thumbs + boards)
    - All transcription .txt files (original + translations)
    - All subtitle .srt files (original + translations)
    - Proxy files (best-effort search by clip_id and by original filename stem)

    Does NOT delete the original media file.

    Parameters
    ----------
    original_filename : optional original media filename (without path).
                        Helps find proxies that are named after the source file.

    Returns the number of files successfully deleted.
    """
    deleted = 0
    padded_id = f"{clip_id:06d}"

    # 1. Cached audio
    try:
        deleted += clear_cached_audio(clip_id, catalog_root)
    except Exception as ex:
        print(f"[Cleanup] Audio cache deletion failed for {clip_id}: {ex}")

    # 2. Previews (thumbnails + storyboards) - aggressive recursive search in the previews/ parent
    # (get_previews_dir defaults to thumbs subdir only; we must search the parent to catch boards too)
    try:
        previews_root = catalog_root / "previews"
        if previews_root.exists():
            for f in previews_root.rglob(f"*{padded_id}*"):
                if f.is_file():
                    try:
                        f.unlink()
                        deleted += 1
                        try:
                            rel = f.relative_to(previews_root)
                        except Exception:
                            rel = f.name
                        print(f"[Cleanup] Removed preview: {rel}")
                    except Exception:
                        pass
    except Exception as ex:
        print(f"[Cleanup] Preview deletion failed for {clip_id}: {ex}")

    # 3. Transcriptions (.txt)
    try:
        txt_dir = get_transcriptions_dir(catalog_root)
        if txt_dir.exists():
            for f in txt_dir.glob(f"{clip_id:06d}*"):
                if f.is_file():
                    try:
                        f.unlink()
                        deleted += 1
                        print(f"[Cleanup] Removed transcription: {f.name}")
                    except Exception:
                        pass
    except Exception as ex:
        print(f"[Cleanup] Transcription deletion failed for {clip_id}: {ex}")

    # 4. Subtitles (.srt)
    try:
        srt_dir = get_subtitles_dir(catalog_root)
        if srt_dir.exists():
            for f in srt_dir.glob(f"{clip_id:06d}*"):
                if f.is_file():
                    try:
                        f.unlink()
                        deleted += 1
                        print(f"[Cleanup] Removed subtitle: {f.name}")
                    except Exception:
                        pass
    except Exception as ex:
        print(f"[Cleanup] Subtitle deletion failed for {clip_id}: {ex}")

    # 5. Proxies (aggressive search by clip id + original filename stem)
    try:
        proxies_root = catalog_root / "proxies"
        if proxies_root.exists():
            padded_id = f"{clip_id:06d}"

            # Search by clip_id anywhere in the proxies tree
            for f in proxies_root.rglob(f"*{padded_id}*"):
                if f.is_file():
                    try:
                        f.unlink()
                        deleted += 1
                        print(f"[Cleanup] Removed proxy (by id): {f.relative_to(proxies_root)}")
                    except Exception:
                        pass

            # Search by original media filename stem (most common proxy naming: stem + "_proxy.*")
            if original_filename:
                stem = Path(original_filename).stem
                for f in proxies_root.rglob(f"{stem}*proxy*"):
                    if f.is_file():
                        try:
                            f.unlink()
                            deleted += 1
                            print(
                                f"[Cleanup] Removed proxy (by name): {f.relative_to(proxies_root)}"
                            )
                        except Exception:
                            pass
    except Exception as ex:
        print(f"[Cleanup] Proxy cleanup failed for {clip_id}: {ex}")

    if deleted > 0:
        print(f"[Cleanup] Clip {clip_id} — removed {deleted} generated file(s)")

    return deleted


def burn_subtitles_to_video(
    video_path: str | Path,
    subtitle_path: str | Path,
    output_path: str | Path,
    *,
    use_ebu_style: bool = True,
    progress_callback=None,  # optional callable(percent: float)
) -> Path:
    """
    Burn subtitles into the video.
    - Pass a .ass/.ssa file directly (recommended, uses the 'ass' filter).
    - Or pass a .srt and set use_ebu_style=True to auto-convert to styled ASS.
    Returns the path to the burned video.
    """
    ffmpeg = find_ffmpeg()
    video_path = Path(video_path).expanduser().resolve()
    sub_path = Path(subtitle_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    final_sub_path = sub_path
    temp_ass = None

    try:
        if use_ebu_style and sub_path.suffix.lower() == ".srt":
            from minicat.ai.transcriber import segments_to_ass

            srt_content = sub_path.read_text(encoding="utf-8", errors="replace")
            segments = _parse_srt_to_segments(srt_content)
            # Quantize to the source video's fps so burned subs align to exact frames (fixes "wrong fps" in output)
            # Always confirm framerate from the actual video file.
            try:
                vid_fps = confirm_video_framerate(video_path)
            except Exception:
                vid_fps = 25.0
            ass_content = segments_to_ass(segments, title="CAT+TAG Subtitles", fps=vid_fps)

            temp_ass = sub_path.with_suffix(".temp_ebu.ass")
            temp_ass.write_text(ass_content, encoding="utf-8")
            final_sub_path = temp_ass

        sub_escaped = str(final_sub_path).replace("\\", "/").replace(":", "\\:")

        if final_sub_path.suffix.lower() in (".ass", ".ssa"):
            vf = f"ass='{sub_escaped}'"
        else:
            vf = f"subtitles='{sub_escaped}'"

        cmd = [
            str(ffmpeg),
            "-y",
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-c:a",
            "copy",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output_path),
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        duration = None
        for line in process.stdout:
            line = line.strip()
            if line.startswith("Duration=") and duration is None:
                # Not always present, we get it from ffprobe earlier usually
                pass
            if line.startswith("out_time_ms="):
                try:
                    ms = int(line.split("=")[1])
                    if duration and duration > 0:
                        percent = min(ms / (duration * 1_000_000), 0.99)
                        if progress_callback:
                            progress_callback(percent)
                except Exception:
                    pass

        process.wait()

        if process.returncode != 0:
            stderr = ""
            if process.stderr:
                try:
                    stderr = process.stderr.read() or ""
                except Exception:
                    pass
            # Also try to get any remaining stdout
            stdout_remain = ""
            if process.stdout:
                try:
                    stdout_remain = process.stdout.read() or ""
                except Exception:
                    pass
            raise RuntimeError(
                f"ffmpeg burn failed (code {process.returncode}):\n{stderr[:800] or stdout_remain[:800]}"
            )

    finally:
        if temp_ass and temp_ass.exists():
            try:
                temp_ass.unlink()
            except Exception:
                pass

    if progress_callback:
        progress_callback(1.0)

    return output_path


def _parse_srt_to_segments(srt_content: str) -> list[dict]:
    """Very lightweight SRT parser (only used internally for burning)."""
    import re

    segments = []
    blocks = re.split(r"\n\s*\n", srt_content.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) >= 3:
            time_line = lines[1]
            match = re.match(
                r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})", time_line
            )
            if match:
                text = " ".join(lines[2:])
                start = match.group(1).replace(",", ".")
                end = match.group(2).replace(",", ".")
                segments.append({"start": start, "end": end, "text": text})
    return segments


# ---------------------------------------------------------------------------
# Proxy generation (new feature)
# ---------------------------------------------------------------------------


def _get_start_timecode(video_path: Path) -> tuple[str | None, float | None]:
    """Extract the original starting timecode and frame rate from a video file.
    Tries multiple sources (format tags, stream tags, and sidecar XML) for robustness.
    """
    import json
    import subprocess

    timecode = None
    fps = None

    try:
        # Method 1: ffprobe format + stream tags (most common for camera originals)
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format_tags=timecode",
            "-show_entries",
            "stream_tags=timecode,time_code,r_frame_rate,avg_frame_rate",
            "-of",
            "json",
            str(video_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout or "{}")

        # Check format tags (common location)
        if not timecode and "format" in data:
            tags = data["format"].get("tags", {}) or {}
            for key in ("timecode", "time_code", "tc", "start_timecode"):
                if key in tags and tags[key]:
                    timecode = tags[key]
                    break

        # Check stream tags (very common on Sony, Canon, BMD, etc.)
        if not timecode and "streams" in data:
            for stream in data["streams"]:
                tags = stream.get("tags", {}) or {}
                for key in ("timecode", "time_code", "tc", "start_timecode", "timecode_string"):
                    if key in tags and tags[key]:
                        timecode = tags[key]
                        break
                if timecode:
                    break

        # Get best fps (prefer r_frame_rate, fall back to avg_frame_rate)
        if "streams" in data and data["streams"]:
            stream0 = data["streams"][0]
            tags = stream0.get("tags", {}) or {}
            r_frame_rate = (
                stream0.get("r_frame_rate")
                or tags.get("r_frame_rate")
                or stream0.get("avg_frame_rate")
                or tags.get("avg_frame_rate")
                or "25/1"
            )
            try:
                if "/" in str(r_frame_rate):
                    num, den = map(int, str(r_frame_rate).split("/"))
                    fps = num / den if den else 25.0
                else:
                    fps = float(r_frame_rate)
            except Exception:
                fps = 25.0

    except Exception:
        pass

    if not timecode:
        # Fallback: many pro camera files (Sony XAVC etc.) report the timecode in the
        # raw ffprobe text output as "TAG:timecode=..." even when the structured JSON
        # "tags" dict is empty or the -show_entries limits it. Premiere reads this.
        try:
            cmd2 = [
                "ffprobe",
                "-v",
                "quiet",
                "-show_format",
                "-show_streams",
                "-select_streams",
                "v:0",
                str(video_path),
            ]
            res = subprocess.run(cmd2, capture_output=True, text=True, timeout=15)
            for line in (res.stdout or "").splitlines():
                line = line.strip()
                if "timecode=" in line.lower():
                    tc = line.split("=", 1)[1].strip()
                    if tc and not timecode:
                        timecode = tc
                        break
        except Exception:
            pass

    # Method 2: Fall back to camera sidecar XML if available (often more reliable for Sony, RED, etc.)
    if not timecode:
        try:
            xml_meta = extract_camera_xml_metadata(video_path)
            if xml_meta.get("timecode"):
                timecode = xml_meta["timecode"]
            if not fps and xml_meta.get("fps"):
                fps = float(xml_meta["fps"])
        except Exception:
            pass

    if timecode:
        # Normalize common formats (e.g. 01:00:00:00 or 01;00;00;00)
        timecode = timecode.replace(";", ":").strip()
    else:
        # No embedded timecode found from camera — fall back to a clean synthetic
        # counter starting at 00:00:00:00 using the detected fps. This gives
        # proper frame-accurate timecode on the proxy (what most editors want).
        if fps and fps > 0:
            timecode = "00:00:00:00"

    return timecode, fps or 25.0


def _timecode_to_seconds(tc: str, fps: float = 25.0) -> float:
    """Convert HH:MM:SS:FF timecode string to total seconds."""
    if not tc:
        return 0.0
    tc = tc.replace(";", ":").strip()
    parts = tc.split(":")
    try:
        if len(parts) == 4:
            h, m, s, f = map(int, parts)
            return h * 3600 + m * 60 + s + (f / max(fps, 1))
        elif len(parts) == 3:
            h, m, s = map(int, parts)
            return h * 3600 + m * 60 + s
        elif len(parts) == 2:
            m, s = map(int, parts)
            return m * 60 + s
        else:
            return float(parts[0])
    except Exception:
        return 0.0


def _add_duration_to_timecode(start_tc: str, duration_seconds: float, fps: float = 25.0) -> str:
    """Add duration (in seconds) to a timecode and return new timecode string."""
    if not start_tc:
        return "00:00:00:00"
    start_sec = _timecode_to_seconds(start_tc, fps)
    end_sec = start_sec + duration_seconds

    # Local copy of timecode formatting to avoid UI import
    if end_sec is None or end_sec < 0:
        return "00:00:00:00"
    total = int(end_sec)
    hh = total // 3600
    mm = (total % 3600) // 60
    ss = total % 60
    if fps and fps > 0:
        ff = int(round((end_sec - total) * fps))
        ff = max(0, min(int(fps) - 1, ff))
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
    return f"{hh:02d}:{mm:02d}:{ss:02d}:00"


# ---------------------------------------------------------------------------
# New Proxy Profiles (2026) — exact recipes as specified
# ---------------------------------------------------------------------------

PROXY_PROFILE_DEFS: dict[str, dict] = {
    "Apple ProRes Proxy (Standard NLE Workflow)": {
        "height": 1080,
        "ext": ".mov",
        "vcodec": "prores_ks",
        "vcodec_args": ["-profile:v", "0", "-q:v", "20"],
        "acodec_args": ["-c:a", "pcm_s16le"],
    },
    "Avid DNxHR LB (Windows/Avid Workflow)": {
        "height": 1080,
        "ext": ".mov",
        "vcodec": "dnxhd",
        "vcodec_args": ["-profile:v", "dnxhr_lb", "-pixel_format", "yuv422p"],
        "acodec_args": ["-c:a", "pcm_s16le"],
    },
    'H.264 "Performance" Proxy (720p)': {
        "height": 720,
        "ext": ".mp4",
        "vcodec": "libx264",
        "vcodec_args": ["-preset", "superfast", "-crf", "28"],
        "acodec_args": ["-c:a", "aac", "-b:a", "128k"],
    },
    "HEVC/H.265 (Space-Saving Proxy)": {
        "height": 1080,
        "ext": ".mp4",
        "vcodec": "libx265",
        "vcodec_args": ["-preset", "ultrafast", "-crf", "30"],
        "acodec_args": ["-c:a", "aac", "-b:a", "128k"],
    },
    "MJPEG Draft (Low-CPU Legacy Proxy)": {
        "height": 540,
        "ext": ".mov",
        "vcodec": "mjpeg",
        "vcodec_args": ["-q:v", "15"],
        "acodec_args": ["-c:a", "pcm_s16le"],
    },
}


def create_proxy(
    source_path: str | Path,
    output_path: str | Path,
    *,
    preset: str = "Apple ProRes Proxy (Standard NLE Workflow)",
    # Branding / timecode (applied on top of the profile's codec settings)
    burn_text: bool = True,
    text: str = "CAT+TAG-Proxy",
    burn_timecode: bool = False,
    timecode_start: str = "00:00:00:00",
    timecode_fontsize: int = 24,
    # Subtle CAT+TAG logo watermark in bottom-left
    subtle_watermark: bool = True,
    watermark_text: str = "CAT+TAG",
    watermark_size: int = 18,
    watermark_opacity: float = 0.4,
    # Timecode options
    burn_original_timecode: bool = True,
    timecode_position: str = "bottom",  # "top" or "bottom"
    progress_callback: Callable[[float, float], None] | None = None,
) -> Path:
    """
    Create a proxy using one of the 5 official CAT+TAG profiles.
    Each profile has fixed resolution, codec, quality target, and container.
    Watermark + original timecode burn are applied on top (CAT+TAG identity).
    """
    src = Path(source_path).expanduser().resolve()
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = find_ffmpeg()

    # Resolve profile
    profile = PROXY_PROFILE_DEFS.get(preset)
    if not profile:
        # Fallback to the first official profile
        profile = PROXY_PROFILE_DEFS["Apple ProRes Proxy (Standard NLE Workflow)"]

    height = profile["height"]

    # Scale filter (always first)
    scale_filter = f"scale=-2:{height}:flags=lanczos"
    filters = [scale_filter]

    # === CAT+TAG subtle logo in bottom-left corner (verification watermark) ===
    if subtle_watermark:
        # CAT+TAG style: CAT in white, + in primary color (approximated), TAG in white
        # Using two drawtext calls for color effect
        filters.append(
            "drawtext=text='CAT':"
            "x=20:y=h-th-22:"
            f"fontsize={watermark_size}:fontcolor=white@{watermark_opacity}:"
            "box=1:boxcolor=black@0.3:boxborderw=2"
        )
        filters.append(
            "drawtext=text='+':"
            f"x=20+text_w*0.95:y=h-th-22:"
            f"fontsize={watermark_size}:fontcolor=#6366f1@{watermark_opacity}:"
            "box=0"
        )
        filters.append(
            "drawtext=text='TAG':"
            f"x=20+text_w*1.9:y=h-th-22:"
            f"fontsize={watermark_size}:fontcolor=white@{watermark_opacity}:"
            "box=1:boxcolor=black@0.3:boxborderw=2"
        )

    # === Timecode burn (original clip timecode preferred) ===
    # Using the recommended drawtext=timecode= syntax for reliable SMPTE timecode rendering.
    # Position can be "top" or "bottom" (default bottom for compatibility)
    timecode_y = "25" if timecode_position.lower() == "top" else "h-th-25"

    if burn_original_timecode:
        original_tc, fps = _get_start_timecode(src)

        if original_tc and fps:
            # Use the dedicated timecode parameter (more robust than pts:timecode expression)
            tc_escaped = original_tc.replace(":", "\\:")
            filters.append(
                f"drawtext=timecode='{tc_escaped}':"
                f"rate={fps:.2f}:"
                f"fontsize={timecode_fontsize}:"
                "fontcolor=white@0.95:"
                "box=1:boxcolor=black@0.6:boxborderw=5:"
                "x=(w-text_w)/2:"
                f"y={timecode_y}"
            )
        else:
            # Safe fallback when no real TC is available — use simple hms counter
            filters.append(
                "drawtext=text='%{pts\\\\:hms}':"
                f"x=(w-tw)/2:y={timecode_y}:"
                f"fontsize={timecode_fontsize}:fontcolor=white@0.95:"
                "box=1:boxcolor=black@0.5:boxborderw=3"
            )
    elif burn_timecode:
        filters.append(
            "drawtext=text='%{pts\\:hms}':"
            f"x=(w-tw)/2:y={timecode_y}:"
            f"fontsize={timecode_fontsize}:fontcolor=white@0.9:"
            "box=1:boxcolor=black@0.4:boxborderw=3"
        )

    # Optional main stamp (usually disabled when using subtle watermark)
    if burn_text and not subtle_watermark:
        filters.append(
            f"drawtext=text='{text}':"
            "x=w-tw-30:y=h-th-25:"
            "fontsize=22:fontcolor=white@0.75:"
            "box=1:boxcolor=black@0.35:boxborderw=4"
        )

    vf = ",".join(filters)

    # Build command with explicit stream mapping
    cmd = [str(ffmpeg), "-y", "-i", str(src), "-vf", vf]

    # Video codec from the selected profile
    vcodec = profile["vcodec"]
    cmd += ["-c:v", vcodec] + profile.get("vcodec_args", [])

    # Explicit maps
    cmd += ["-map", "0:v:0", "-map", "0:a?"]

    # Audio from the selected profile
    cmd += profile.get("acodec_args", ["-c:a", "aac", "-b:a", "128k"])

    cmd.append(str(out))

    if progress_callback:
        # Try to get duration for percentage
        try:
            meta = extract_metadata(src)
            total_dur = float(meta.get("duration") or 0)
        except Exception:
            total_dur = 0
        _run_ffmpeg_with_progress(cmd, total_dur, progress_callback)
    else:
        subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
    return out


# ---------------------------------------------------------------------------
# XML Timeline Export (FCP7 XML - works in Premiere Pro + DaVinci Resolve)
# ---------------------------------------------------------------------------


def export_fcp7_xml(
    videos: list[Video],
    output_path: Path,
    sequence_name: str = "CAT+TAG Sequence",
    fps: float = 24.0,
    start_timecode: str = "01:00:00:00",
) -> None:
    """
    Export selected clips as a Final Cut Pro 7 XML timeline.
    This format is reliably imported by Adobe Premiere Pro and DaVinci Resolve
    as a new sequence with all the clips in order.
    """
    import xml.etree.ElementTree as ET
    from xml.dom import minidom

    if not videos:
        raise ValueError("No clips to export")

    # Pick a reasonable timebase
    timebase = int(round(fps))
    ntsc = "FALSE"
    if abs(fps - 29.97) < 0.1:
        timebase = 30
        ntsc = "TRUE"
    elif abs(fps - 59.94) < 0.1:
        timebase = 60
        ntsc = "TRUE"

    def frames_to_timecode(frame_count: int, tb: int) -> str:
        h = frame_count // (tb * 3600)
        m = (frame_count // (tb * 60)) % 60
        s = (frame_count // tb) % 60
        f = frame_count % tb
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

    root = ET.Element("xmeml", version="5")
    project = ET.SubElement(root, "project")
    ET.SubElement(project, "name").text = sequence_name
    children = ET.SubElement(project, "children")

    sequence = ET.SubElement(children, "sequence", id="sequence-1")
    ET.SubElement(sequence, "name").text = sequence_name
    ET.SubElement(sequence, "duration").text = "0"  # will be updated

    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = str(timebase)
    ET.SubElement(rate, "ntsc").text = ntsc

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    track = ET.SubElement(video, "track")

    total_frames = 0
    file_id = 1
    clip_id = 1

    for v in videos:
        dur = v.duration or 10.0
        clip_frames = int(round(dur * timebase))

        clipitem = ET.SubElement(track, "clipitem", id=f"clipitem-{clip_id}")
        ET.SubElement(clipitem, "name").text = v.filename
        ET.SubElement(clipitem, "duration").text = str(clip_frames)

        rate2 = ET.SubElement(clipitem, "rate")
        ET.SubElement(rate2, "timebase").text = str(timebase)
        ET.SubElement(rate2, "ntsc").text = ntsc

        ET.SubElement(clipitem, "start").text = str(total_frames)
        ET.SubElement(clipitem, "end").text = str(total_frames + clip_frames)
        ET.SubElement(clipitem, "in").text = "0"
        ET.SubElement(clipitem, "out").text = str(clip_frames)

        # File reference
        file_elem = ET.SubElement(clipitem, "file", id=f"file-{file_id}")
        ET.SubElement(file_elem, "name").text = v.filename
        # Use file:// URL for best compatibility
        path_url = "file://" + str(Path(v.path).resolve()).replace("\\", "/")
        ET.SubElement(file_elem, "pathurl").text = path_url

        # Optional: reel helps some linkers
        reel = ET.SubElement(file_elem, "reel")
        ET.SubElement(reel, "name").text = Path(v.filename).stem[:32]

        # Timecode (approximate)
        tc = ET.SubElement(file_elem, "timecode")
        ET.SubElement(tc, "string").text = start_timecode
        rate3 = ET.SubElement(tc, "rate")
        ET.SubElement(rate3, "timebase").text = str(timebase)
        ET.SubElement(rate3, "ntsc").text = ntsc

        total_frames += clip_frames
        file_id += 1
        clip_id += 1

    # Update sequence duration
    sequence.find("duration").text = str(total_frames)

    # Pretty-print XML
    rough_string = ET.tostring(root, encoding="unicode")
    reparsed = minidom.parseString(rough_string)
    pretty_xml = reparsed.toprettyxml(indent="  ", encoding=None)

    # Remove extra blank lines that minidom adds
    lines = [line for line in pretty_xml.splitlines() if line.strip()]
    final_xml = "\n".join(lines)

    output_path.write_text(final_xml, encoding="utf-8")


def export_ai_journalist_cut_xml(
    video: Video,
    selected_segments: list[dict[str, Any]],
    output_path: Path,
    *,
    sequence_name: str | None = None,
    fps: float = 24.0,
    start_timecode: str | None = None,
) -> Path:
    """
    Export an AI-suggested journalist cut as a Premiere-compatible FCP7 XML.

    Each selected segment becomes a subclip in the sequence, preserving the
    original source timecode where possible.

    Parameters
    ----------
    video
        The original Video object (needs .path, .filename, and preferably duration).
    selected_segments
        List of dicts with keys: start, end, text, reason (in seconds).
    output_path
        Where to write the .xml file.
    sequence_name
        Optional name for the sequence in Premiere.
    fps
        Frame rate to use for the sequence.
    start_timecode
        Optional starting timecode (e.g. from camera). If None, tries to extract.
    """
    import xml.etree.ElementTree as ET
    from xml.dom import minidom

    if not selected_segments:
        raise ValueError("No segments to export")

    # Try to get original timecode if not provided
    if start_timecode is None:
        try:
            tc, _ = _get_start_timecode(Path(video.path))
            start_timecode = tc or "01:00:00:00"
        except Exception:
            start_timecode = "01:00:00:00"

    # Use the clip's actual fps if available
    effective_fps = getattr(video, "fps", None) or fps
    timebase = int(round(effective_fps))
    ntsc = "FALSE"
    if abs(effective_fps - 29.97) < 0.1:
        timebase = 30
        ntsc = "TRUE"
    elif abs(effective_fps - 59.94) < 0.1:
        timebase = 60
        ntsc = "TRUE"

    # Always confirm pixel aspect ratio from the actual clip (HD/4K → square/1.0, never assume D1/DV PAL 1.0940)
    par = "square"
    try:
        pcmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-select_streams",
            "v:0",
            str(getattr(video, "path", "")),
        ]
        pres = subprocess.run(pcmd, capture_output=True, text=True, timeout=10)
        pdata = json.loads(pres.stdout or "{}")
        for s in pdata.get("streams", []):
            if s.get("codec_type") == "video":
                rp = s.get("sample_aspect_ratio")
                if rp and rp not in ("1:1", "1/1", "1:1.0"):
                    par = rp
                break
    except Exception:
        pass

    def seconds_to_frames(sec: float, tb: int) -> int:
        return int(round(sec * tb))

    def frames_to_timecode(frame_count: int, tb: int) -> str:
        h = frame_count // (tb * 3600)
        m = (frame_count // (tb * 60)) % 60
        s = (frame_count // tb) % 60
        f = frame_count % tb
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"

    root = ET.Element("xmeml", version="5")
    project = ET.SubElement(root, "project")
    ET.SubElement(project, "name").text = sequence_name or f"AI Cut - {video.filename}"
    children = ET.SubElement(project, "children")

    # =====================================================
    # 1. Define the MASTER CLIP first (best practice for Premiere)
    # =====================================================
    master_clip = ET.SubElement(children, "clipitem", id="master-clip-1")
    ET.SubElement(master_clip, "name").text = video.filename
    ET.SubElement(master_clip, "duration").text = str(
        seconds_to_frames(getattr(video, "duration", 3600), timebase)
    )

    rate_master = ET.SubElement(master_clip, "rate")
    ET.SubElement(rate_master, "timebase").text = str(timebase)
    ET.SubElement(rate_master, "ntsc").text = ntsc

    # File reference
    file_master = ET.SubElement(master_clip, "file", id="master-file-1")
    ET.SubElement(file_master, "name").text = video.filename
    path_url = "file://" + str(Path(video.path).resolve()).replace("\\", "/")
    ET.SubElement(file_master, "pathurl").text = path_url

    # Reel
    reel = ET.SubElement(file_master, "reel")
    ET.SubElement(reel, "name").text = Path(video.filename).stem[:32]

    # Format / Sample characteristics on master file (critical for Premiere)
    fmt = ET.SubElement(file_master, "format")
    sample = ET.SubElement(fmt, "samplecharacteristics")
    rate_sample = ET.SubElement(sample, "rate")
    ET.SubElement(rate_sample, "timebase").text = str(timebase)
    ET.SubElement(rate_sample, "ntsc").text = ntsc

    width = getattr(video, "width", 1920) or 1920
    height = getattr(video, "height", 1080) or 1080
    ET.SubElement(sample, "width").text = str(width)
    ET.SubElement(sample, "height").text = str(height)
    ET.SubElement(sample, "anamorphic").text = "FALSE"
    ET.SubElement(sample, "pixelaspectratio").text = par
    ET.SubElement(sample, "fielddominance").text = "none"

    # Media definition with a sample clipitem (helps Premiere register the media)
    media_master = ET.SubElement(file_master, "media")
    video_media = ET.SubElement(media_master, "video")
    track_master = ET.SubElement(video_media, "track")
    sample_clip = ET.SubElement(track_master, "clipitem", id="master-sample")
    ET.SubElement(sample_clip, "name").text = video.filename
    ET.SubElement(sample_clip, "duration").text = str(
        seconds_to_frames(getattr(video, "duration", 3600), timebase)
    )
    rate_s = ET.SubElement(sample_clip, "rate")
    ET.SubElement(rate_s, "timebase").text = str(timebase)
    ET.SubElement(rate_s, "ntsc").text = ntsc

    # =====================================================
    # 2. Create the SEQUENCE
    # =====================================================
    sequence = ET.SubElement(children, "sequence", id="ai-cut-sequence")
    ET.SubElement(sequence, "name").text = sequence_name or f"AI Cut - {video.filename}"

    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = str(timebase)
    ET.SubElement(rate, "ntsc").text = ntsc

    # Timecode
    tc = ET.SubElement(sequence, "timecode")
    tc_rate = ET.SubElement(tc, "rate")
    ET.SubElement(tc_rate, "timebase").text = str(timebase)
    ET.SubElement(tc_rate, "ntsc").text = ntsc
    ET.SubElement(tc, "string").text = start_timecode
    ET.SubElement(tc, "frame").text = "0"
    ET.SubElement(tc, "displayformat").text = str(timebase)

    # Media + Format for sequence (very important)
    media = ET.SubElement(sequence, "media")
    video_track_elem = ET.SubElement(media, "video")

    format_elem = ET.SubElement(video_track_elem, "format")
    sample_seq = ET.SubElement(format_elem, "samplecharacteristics")
    rate_seq = ET.SubElement(sample_seq, "rate")
    ET.SubElement(rate_seq, "timebase").text = str(timebase)
    ET.SubElement(rate_seq, "ntsc").text = ntsc
    ET.SubElement(sample_seq, "width").text = str(width)
    ET.SubElement(sample_seq, "height").text = str(height)
    ET.SubElement(sample_seq, "anamorphic").text = "FALSE"
    ET.SubElement(sample_seq, "pixelaspectratio").text = par
    ET.SubElement(sample_seq, "fielddominance").text = "none"

    track = ET.SubElement(video_track_elem, "track")

    # =====================================================
    # 3. Add the actual cuts as clipitems (using subclip in/out)
    # =====================================================
    total_frames = 0
    clip_id = 1

    for i, seg in enumerate(selected_segments):
        start_sec = float(seg.get("source_in") or seg.get("start", 0))
        end_sec = float(seg.get("source_out") or seg.get("end", 0))
        duration_sec = end_sec - start_sec

        clip_frames = seconds_to_frames(duration_sec, timebase)
        start_in_timeline = total_frames
        end_in_timeline = total_frames + clip_frames

        in_point = seconds_to_frames(start_sec, timebase)
        out_point = seconds_to_frames(end_sec, timebase)

        clipitem = ET.SubElement(track, "clipitem", id=f"clipitem-{clip_id}")
        ET.SubElement(clipitem, "name").text = f"{video.filename} - AI Cut {i + 1}"
        ET.SubElement(clipitem, "duration").text = str(clip_frames)

        rate2 = ET.SubElement(clipitem, "rate")
        ET.SubElement(rate2, "timebase").text = str(timebase)
        ET.SubElement(rate2, "ntsc").text = ntsc

        ET.SubElement(clipitem, "start").text = str(start_in_timeline)
        ET.SubElement(clipitem, "end").text = str(end_in_timeline)
        ET.SubElement(clipitem, "in").text = str(in_point)
        ET.SubElement(clipitem, "out").text = str(out_point)

        # Reference the master file we defined above
        file_elem = ET.SubElement(clipitem, "file", id="master-file-1")
        ET.SubElement(file_elem, "name").text = video.filename
        ET.SubElement(file_elem, "pathurl").text = path_url

        # Add a marker with the AI reason
        reason = seg.get("reason", "")
        if reason:
            marker = ET.SubElement(clipitem, "marker")
            ET.SubElement(marker, "name").text = reason[:80]
            ET.SubElement(marker, "comment").text = f"AI Journalist reason: {reason}"

        total_frames = end_in_timeline
        clip_id += 1

    # Final sequence metadata
    ET.SubElement(sequence, "duration").text = str(total_frames)
    ET.SubElement(sequence, "label").text = "AI Journalist Cut"

    # Pretty print
    rough = ET.tostring(root, encoding="unicode")
    reparsed = minidom.parseString(rough)
    pretty = reparsed.toprettyxml(indent="  ", encoding=None)

    lines = [line for line in pretty.splitlines() if line.strip()]
    final = "\n".join(lines)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(final, encoding="utf-8")

    return output_path


# ---------------------------------------------------------------------------
# AI Journalist Cut — Rendered Video Export
# ---------------------------------------------------------------------------


def export_ai_journalist_cut_video(
    video: Video,
    selected_segments: list[dict[str, Any]],
    output_path: Path,
    *,
    title: str | None = None,
    preset: str = "medium",
    crf: int = 17,
    audio_bitrate: str = "320k",
) -> Path:
    """
    Render the AI-selected journalist cut segments into a single, playable MP4 file.

    Each segment is cut from the original source (with safe re-encoding for
    frame-accurate results) and then concatenated. The final file uses stream
    copy for maximum speed and quality on the concat step.

    This gives the user an actual video file they can watch, share, or import
    anywhere — not just an EDL/XML.

    Parameters
    ----------
    video
        The source Video object (must have .path).
    selected_segments
        List of dicts: {"start": float, "end": float, "text": str, "reason": str}
    output_path
        Destination .mp4 path (parent folder will be created).
    title
        Optional human title to embed in metadata / filename hint.
    preset / crf
        libx264 quality settings for the segment encoding pass (high quality defaults).
    """
    import subprocess
    import tempfile

    if not selected_segments:
        raise ValueError("No segments provided for video export")

    src = Path(video.path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source media not found: {src}")

    # Fail fast with a clear message if ffmpeg is missing
    try:
        ffmpeg = find_ffmpeg()
    except RuntimeError as e:
        raise RuntimeError(
            f"Cannot export video cut: {e}\n\n"
            "Please install ffmpeg (e.g. `brew install ffmpeg` on macOS) and restart the app."
        ) from e

    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = find_ffmpeg()

    # Preserve caller-provided order (critical for AI "rewrite / re-story" mode which intentionally
    # returns non-chronological selections for narrative impact). Only drop invalid segments.
    cleaned: list[dict] = []
    for seg in selected_segments:
        try:
            s = float(seg.get("source_in") or seg.get("start", 0))
            e = float(seg.get("source_out") or seg.get("end", 0))
            if e > s + 0.05:  # at least 50ms
                cleaned.append(
                    {
                        "start": s,
                        "end": e,
                        "text": seg.get("text", ""),
                        "reason": seg.get("reason", ""),
                    }
                )
        except Exception:
            continue

    if not cleaned:
        raise ValueError("All provided segments were invalid or too short")

    # Work in a temporary directory (auto-cleaned)
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        segment_files: list[Path] = []

        for idx, seg in enumerate(cleaned):
            seg_path = tmpdir / f"seg_{idx:03d}.mp4"
            cmd = [
                str(ffmpeg),
                "-y",
                "-ss",
                f"{seg['start']:.3f}",
                "-to",
                f"{seg['end']:.3f}",
                "-i",
                str(src),
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-crf",
                str(crf),
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                audio_bitrate,
                "-avoid_negative_ts",
                "make_zero",
                "-movflags",
                "+faststart",
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                str(seg_path),
            ]

            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            except subprocess.CalledProcessError as e:
                err = e.stderr.decode("utf-8", errors="ignore")[:600] if e.stderr else str(e)
                raise RuntimeError(
                    f"Failed to cut segment {idx + 1} ({seg['start']:.1f}s–{seg['end']:.1f}s): {err}"
                ) from e

            if seg_path.exists() and seg_path.stat().st_size > 1000:
                segment_files.append(seg_path)
            else:
                print(f"[AI Video Export] Warning: segment {idx} produced empty file, skipping")

        if not segment_files:
            raise RuntimeError("No valid segments could be extracted for the video cut")

        # Build concat list (concat demuxer format) - use absolute paths for robustness
        concat_list = tmpdir / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in segment_files), encoding="utf-8"
        )

        # Final concat pass (stream copy — very fast)
        concat_cmd = [
            str(ffmpeg),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out),
        ]

        try:
            subprocess.run(concat_cmd, check=True, capture_output=True, timeout=180)
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode("utf-8", errors="ignore")[:600] if e.stderr else str(e)
            raise RuntimeError(f"Final concat step failed: {err}") from e

    # Verify output
    if not out.exists() or out.stat().st_size < 10000:
        raise RuntimeError(f"Output video was not created or is too small: {out}")

    return out


# ---------------------------------------------------------------------------
# Automatic tags on import (resolution for video, "audio" for audio files)
# ---------------------------------------------------------------------------


def get_resolution_tag(width: int | None, height: int | None) -> str | None:
    """
    Return a concise tag based on vertical resolution.
    Used for automatic tagging during import.
    Uses the smaller dimension as "height" so vertical video and 720p are classified correctly.
    """
    if not width or not height:
        return None
    try:
        w, h = int(width), int(height)
        vert = min(w, h)  # correct vertical resolution regardless of orientation
    except (TypeError, ValueError):
        return None

    if vert >= 2160:
        return "4K UHD"
    elif vert >= 1080:
        return "HD"
    elif vert >= 720:
        return "720p"
    else:
        return "SD"


def get_auto_import_tags(
    meta: dict[str, Any],
    *,
    is_audio: bool = False,
) -> list[str]:
    """
    Compute automatic tags to attach on import.
    - Audio files → ["audio"]
    - Video files → resolution tag (4K UHD / HD / 720p / SD) if detectable
    """
    tags: list[str] = []

    if is_audio:
        tags.append("audio")
    else:
        res_tag = get_resolution_tag(meta.get("width"), meta.get("height"))
        if res_tag:
            tags.append(res_tag)

    return tags


# ---------------------------------------------------------------------------
# AI Journalist Cut — Audio Export (for pure audio sources)
# ---------------------------------------------------------------------------


def export_ai_journalist_cut_audio(
    video: Video,
    selected_segments: list[dict[str, Any]],
    output_path: Path,
    *,
    sample_rate: int = 48000,
    channels: int = 2,
) -> Path:
    """
    Export the selected AI Journalist segments from an audio source
    (or audio track) as a single WAV file.

    This is the audio equivalent of export_ai_journalist_cut_video.
    """
    import subprocess
    import tempfile

    if not selected_segments:
        raise ValueError("No segments provided for audio export")

    src = Path(video.path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source media not found: {src}")

    # Fail fast with a clear message if ffmpeg is missing
    try:
        ffmpeg = find_ffmpeg()
    except RuntimeError as e:
        raise RuntimeError(
            f"Cannot export audio cut: {e}\n\n"
            "Please install ffmpeg (e.g. `brew install ffmpeg` on macOS) and restart the app."
        ) from e

    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = find_ffmpeg()

    # Preserve caller-provided order (critical for AI "rewrite / re-story" mode which intentionally
    # returns non-chronological selections for narrative impact). Only drop invalid segments.
    cleaned: list[dict] = []
    for seg in selected_segments:
        try:
            s = float(seg.get("source_in") or seg.get("start", 0))
            e = float(seg.get("source_out") or seg.get("end", 0))
            if e > s + 0.05:
                cleaned.append({"start": s, "end": e})
        except Exception:
            continue

    if not cleaned:
        raise ValueError("All provided segments were invalid or too short")

    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        segment_files: list[Path] = []

        for idx, seg in enumerate(cleaned):
            seg_path = tmpdir / f"seg_{idx:03d}.wav"
            cmd = [
                str(ffmpeg),
                "-y",
                "-ss",
                f"{seg['start']:.3f}",
                "-to",
                f"{seg['end']:.3f}",
                "-i",
                str(src),
                "-vn",  # no video
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-avoid_negative_ts",
                "make_zero",
                str(seg_path),
            ]

            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=180)
            except subprocess.CalledProcessError as e:
                err = e.stderr.decode("utf-8", errors="ignore")[:500] if e.stderr else str(e)
                raise RuntimeError(f"Failed to cut audio segment {idx + 1}: {err}") from e

            if seg_path.exists() and seg_path.stat().st_size > 100:
                segment_files.append(seg_path)

        if not segment_files:
            raise RuntimeError("No valid audio segments could be extracted")

        # Concat via demuxer - use absolute paths for robustness
        concat_list = tmpdir / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in segment_files), encoding="utf-8"
        )

        concat_cmd = [
            str(ffmpeg),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            str(out),
        ]

        try:
            subprocess.run(concat_cmd, check=True, capture_output=True, timeout=120)
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode("utf-8", errors="ignore")[:500] if e.stderr else str(e)
            raise RuntimeError(f"Audio concat failed: {err}") from e

    if not out.exists() or out.stat().st_size < 1000:
        raise RuntimeError(f"Output audio file was not created properly: {out}")

    return out
