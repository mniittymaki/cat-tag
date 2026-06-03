# Sample Camera Metadata Sidecar XMLs

This folder contains realistic examples of camera-generated sidecar metadata files for testing CAT+TAG's multi-brand support.

## Purpose
These files let you test:
- `find_sidecar_xml()` detection
- `extract_camera_xml_metadata()` brand-specific + generic parsing
- ExifTool fallback behavior

## Files Included

| File                    | Brand / Format          | What to test                              |
|-------------------------|-------------------------|-------------------------------------------|
| `sony_fx3_nonrealtimemeta.xml` | Sony NonRealTimeMeta   | Existing Sony path + gamma/color science |
| `red_komodo_sample.rmd`        | RED .RMD               | RED-specific ISO / ColorSpace / Gamma    |
| `arri_alexa_mini_lf.xml`       | Arri XML / XMP         | `com.arri.camera.*` namespace fields     |
| `canon_c300_xf.xml`            | Canon XF               | Canon-style exposure + gamma tags        |
| `dji_mavic3_xmp.xml`           | DJI drone-dji XMP      | XMP namespace + altitude/gimbal data     |

## How to Use for Testing

1. Place any of these files next to a real video file (or a dummy `.mp4` with matching stem).
2. Run import or trigger "Rebuild Previews + Metadata" on a clip.
3. Check the inspector → TECHNICAL INFO and "Camera Metadata (from sidecar XML)" section.

## Generating Your Own Real Samples

- **Sony**: Record with FX3/FX6/VENICE → look for `CLIP/xxxxM01.XML`
- **RED**: Open any .R3D in REDCINE-X PRO → it creates/updates the matching `.RMD`
- **Arri**: Use ARRI Meta Extract (or current ART tool) on Alexa footage
- **Canon**: Record on C300/C400 → XF Utility or card will have matching `.XML`

## Notes
- These samples are sanitized / reconstructed from real-world files and documentation.
- They are intentionally small and safe for testing.
- The parser is deliberately forgiving — it does not require perfect schema compliance.

Last updated: 2026
