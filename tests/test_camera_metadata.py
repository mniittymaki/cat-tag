"""
Unit tests for multi-brand camera metadata extraction.
Uses the sample files in assets/sample_camera_xmls/
"""

import tempfile
from pathlib import Path

import pytest

from minicat.core import video

SAMPLE_DIR = Path(__file__).parent.parent / "assets" / "sample_camera_xmls"


def _copy_sample_as_sidecar(sample_name: str, video_stem: str, tmpdir: Path) -> Path:
    """Helper: copy a sample sidecar next to a dummy video file with matching stem."""
    sample_path = SAMPLE_DIR / sample_name
    video_path = tmpdir / f"{video_stem}.mp4"
    video_path.write_bytes(b"dummy video data")

    # Determine extension from sample
    ext = sample_path.suffix
    sidecar_path = tmpdir / f"{video_stem}{ext}"
    sidecar_path.write_text(sample_path.read_text())

    return video_path


def test_find_camera_metadata_sidecar_finds_xml(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"data")
    (tmp_path / "clip.xml").write_text("<root/>")

    found = video.find_camera_metadata_sidecar(clip)
    assert found is not None
    assert found.suffix == ".xml"


def test_sony_nonrealtimemeta_parsing():
    if not (SAMPLE_DIR / "sony_fx3_nonrealtimemeta.xml").exists():
        pytest.skip("Sony sample missing")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        v = _copy_sample_as_sidecar("sony_fx3_nonrealtimemeta.xml", "sony_test", tmpdir)
        meta = video.extract_camera_xml_metadata(v)

        assert "gamma" in meta
        assert "s-log3" in meta["gamma"].lower() or "slog" in meta.get("gamma", "").lower()
        assert meta.get("source_xml", "").endswith(".xml")


def test_red_rmd_parsing():
    if not (SAMPLE_DIR / "red_komodo_sample.rmd").exists():
        pytest.skip("RED sample missing")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        v = _copy_sample_as_sidecar("red_komodo_sample.rmd", "red_test", tmpdir)
        meta = video.extract_camera_xml_metadata(v)

        assert meta.get("gamma") == "Log3G10"
        assert meta.get("iso") == 800


def test_arri_metadata_parsing():
    if not (SAMPLE_DIR / "arri_alexa_mini_lf.xml").exists():
        pytest.skip("Arri sample missing")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        v = _copy_sample_as_sidecar("arri_alexa_mini_lf.xml", "arri_test", tmpdir)
        meta = video.extract_camera_xml_metadata(v)

        # Arri parser should have pulled camera model or lens
        assert "ARRI" in str(meta.get("camera", "")) or "Signature Prime" in str(
            meta.get("lens", "")
        )


def test_canon_xf_parsing():
    if not (SAMPLE_DIR / "canon_c300_xf.xml").exists():
        pytest.skip("Canon sample missing")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        v = _copy_sample_as_sidecar("canon_c300_xf.xml", "canon_test", tmpdir)
        meta = video.extract_camera_xml_metadata(v)

        assert meta.get("iso") == 800 or "Canon Log" in str(meta.get("gamma", ""))


def test_dji_xmp_parsing():
    if not (SAMPLE_DIR / "dji_mavic3_xmp.xml").exists():
        pytest.skip("DJI sample missing")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        v = _copy_sample_as_sidecar("dji_mavic3_xmp.xml", "dji_test", tmpdir)
        meta = video.extract_camera_xml_metadata(v)

        # DJI XMP should at least parse without error and extract focal length or notes
        assert (
            meta.get("focal_length") == 24
            or "alt" in str(meta.get("notes", "")).lower()
            or len(meta) > 1
        )


def test_no_sidecar_returns_empty_or_exiftool_data():
    """When no sidecar exists we should get empty dict (or ExifTool data if installed)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        v = tmpdir / "lonely.mp4"
        v.write_bytes(b"data")

        meta = video.extract_camera_xml_metadata(v)
        # It's okay to get some data if ExifTool is present and the file has embedded metadata
        assert isinstance(meta, dict)


def test_exiftool_availability_check_does_not_crash():
    available = video.is_exiftool_available()
    assert isinstance(available, bool)
