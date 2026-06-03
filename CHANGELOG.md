# Changelog

All notable changes to CAT+TAG.

## [0.2.0] - 2026-06-03

### Added
- Narration budgeting controls (min/max seconds + min/max bridges) in both AI Journalist and AI Director generation dialogs. Prompt-injected and honored in all exports.
- Language-matched rich script TXT exports: full headers, labels, SELECTED SEGMENTS, FULL transcript, reasons etc. always emitted in the exact language of the source transcript.
- "Export File" (AI Journalist) and equivalent Director paths now always bundle the rich attributed Script/Story TXT even when no narration/VO is present.
- First official macOS app bundle (`CAT+TAG.app`) built via PyInstaller + release asset.

### Changed
- **Left sidebar**: High-density professional video-editor aesthetic refactor (layout/spacing/typography only per strict 4-rule spec: p-0/m-0/gap-0 container, q-py-none min-h-[36px] + text-xs font-semibold tracking-wider headers, py-0.5/q-py-xs children + subtle indent, compact rounded badges + tight counters).
- **Right inspector (single + multi-clip)**: Parallel density tightening (min-h-[32px] expansions, px-4 py-2 root scroll with flex-col gap-y-3, flex row wrap for cached-audio buttons, block labels, dense outlined square inputs).
- Inspector TC display now shows real transcript timecodes on the line with text: `10:03:20:18 - 10:03:21:00\nKäy.` (no more bracketed seconds).
- Proxy audio extraction finalized to 24 kHz mono AAC 64 kbps (pan=mono + -3 dB peak only).

### Fixed
- Transcript `.txt` sidecars are now **the absolute source of truth** for every timecode in scripts, exports, XMLs:
  - `resolve_source_range_for_text` hardened with exact, guarded difflib, prefix-anchor expand (>=2 sig words), final <2s-gap flood-fill + opening bias.
  - `repair_journalist_segments_with_transcript` + new `repair_director_version_with_transcripts` + per-source sidecar matching wired everywhere (Journalist + Director multi-clip).
- Multi-clip right inspector layout breakage (overlaps, collisions, nesting failures visible in screenshots): explicit flex stacks/gaps, proper margins, block labels, no functional elements touched.
- AI Director JSON parse failures on long Finnish outputs (salvage progressive trim + low-temp repair prompt + strict 1-2 sentence brevity guidance in system prompt).
- "Export File" now correctly emits Script/Story TXT.
- Crashes: UnboundLocal 'labels' in export_txt, Label(color=) TypeError in delete dialogs.
- Storyboard thumbnail click in inspector now opens dialog.
- CI: Node 20 deprecation, setup-uv @v8 not found, ruff not present (now --dev group + stable @v6/@v8.2.0 tags + FORCE_NODE24 + E501 ignore for AI strings).
- XML: live PAR from ffprobe SAR, fps from clip, repaired real TCs for multi-source too.
- Verbatim transcription guidelines now highest-precedence block at top of transcriber instructions.

### Infrastructure
- Version 0.2.0.
- Cleaned repo (backups/ dirs, .bak files, secrets removed from history).
- .github/workflows/ci.yml fully reliable.
- macOS app artifact attached to GitHub Release.

See the v0.2.0 release notes on GitHub for the full narrative of every user-driven fix and verification round ("Is it now fixed 100%?", "Do everything!").

## [0.1.0] - Initial internal series
- Core catalog, transcription, AI Journalist, AI Director, XMEML/FCPXML, etc. (pre-public).
