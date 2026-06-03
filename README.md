# CAT+TAG

**Simple. Effective. Yours.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)]()

A fast, private, local catalog for your video footage — now with powerful **AI tools** built in.

CAT+TAG gives you a clean native desktop experience to browse, search, and organize raw video clips across many projects. It includes thumbnails, storyboards, rich metadata, **and** AI-powered audio transcription, translation, and professional subtitle burning. Everything stays on your machine.

Built for video professionals, documentarians, and anyone who needs to actually find and repurpose material later.

Copyright © 2026 Mikko Niittymäki

## Quick Start

### Easiest on macOS

```bash
brew install ffmpeg uv
git clone https://github.com/mniittymaki/cat-tag.git
cd cat-tag
uv sync
uv run minicat
```

On first launch CAT+TAG automatically uses `~/CAT+TAG` as the catalog folder (created and initialized for you). Use the folder icon in the top bar ("Open Catalog") to switch to a different catalog location at any time. The choice is remembered.

### Other platforms

Install `ffmpeg` and `uv`, then:

```bash
uv sync
uv run minicat
```

**First-time workflow:**
1. Click the **Import** button (↓ icon in the top bar)
2. Choose a folder with your footage
3. The app reads rich camera metadata from sidecar XML files (especially Sony) automatically
4. (Optional) Enable AI auto-tagging during import
5. Later, use **"Transcribe Audio with AI"** in the inspector → translate → burn professional subtitles or export .srt files

## Key Features

- **Native desktop app** — clean macOS-style window with sidebar, media grid/list, and rich inspector
- **AI Transcription** — Extract audio and transcribe with accurate timestamps using Gemini
- **AI Translation** — Translate transcripts into other languages while keeping perfect sync
- **Burn & Export Subtitles** — Burn professional EBU-styled subtitles into video files + export clean .srt files
- **AI Tag Suggestions** — Generate smart tags from video storyboards
- **Professional proxies** — ProRes 422 Proxy or H.264 with burned-in timecode + CAT+TAG watermark
- **Excellent Sony XML support** — Automatically reads model, lens, ISO, aperture, shutter, white balance, gamma, color primaries, etc.
- **Rich Projects + Clients** — Organize footage with rich project and client metadata (many-to-many)
- **Strong technical metadata** — Fully visible and editable
- **Visual browsing** — Thumbnails + multi-frame storyboards
- **Powerful search & filters** — Full-text search + Projects, Clients, Cameras, Locations, Tags. Left sidebar is now high-density professional video-editor style (compact headers, minimal padding, maximized text width for fast navigation).
- **In-app Import Wizard** — With progress, proxies, and optional AI auto-tagging
- **Grid + List views**
- **Export tools** — Premiere Pro XMEML v4 (with correct audio tracks), FCP7 XML, and self-contained media packages
- **AI Director exports** — Always create a fresh dated subfolder in your default export library containing the XML + the full rich "AI DIRECTOR — MULTI-CLIP SCRIPT.txt" (attributed clips + narration bridges). Voiceover WAVs are included when narration is present.
- **Narration controls (AI Journalist + AI Director)** — When using a narration style, directly set min/max total narration duration (seconds) and min/max number of narration bridges in the generation dialog for precise control over the AI-written script.
- **Language-matched exports** — Rich script TXT files (SELECTED SEGMENTS + full transcript) are produced in the same language as the transcripted/scripted content (e.g. complete Finnish labels/structure when scripting from a Finnish transcript).
- **Dedicated Text to Speech settings** — Separate Settings tab for Piper (local/offline) vs Google Cloud TTS, default language/voice, testing, model preparation, and credentials.
- **100% local & private** — Nothing ever leaves your machine

## Requirements

- macOS, Linux, or Windows
- Python 3.11+
- **ffmpeg** (required for thumbnails, storyboards, proxies, audio extraction, and metadata)
- **Gemini API key** (free tier works) — required for AI features (tag suggestions, transcription, and translation)

The app gives clear, friendly errors if ffmpeg or an API key is missing.

## AI Features

CAT+TAG includes powerful AI tools powered by Google Gemini:

- **Audio Transcription** — Transcribe the spoken audio of any clip with accurate timecodes
- **Multi-language Translation** — Translate transcripts while preserving perfect sync
- **Burn Subtitles** — Hardcode professional EBU-styled subtitles directly into video files
- **.srt Export** — Export clean, timed subtitle files for any language
- **Smart Tag Suggestions** — Generate relevant tags by analyzing video storyboards
- **AI Journalist Cuts** — Automatically generate multiple professional short narrative versions from long interviews/transcripts. When narration is enabled you can budget total narration length (min/max seconds) and number of bridges (min/max). Export as clean Premiere Pro XMEML v4 sequences (correct dual-mono or stereo audio tracks that relink properly) or as self-contained rendered MP4/WAV clips. "Export File" always includes the rich script TXT. Exported scripts match the language of your chosen transcript.
- **AI Director (multi-clip)** — Intercut 2+ clips into narrative versions. In the generate dialog you can control narration duration budget (min/max seconds across bridges) and exact min/max bridge count. Exports always include the rich attributed script TXT (now language-matched). Dedicated **Text to Speech** tab in Settings for all voiceover (Piper/Google) configuration.

All AI processing happens with your own API key — your footage and transcripts never leave your machine except for the API calls you explicitly make.

### AI Journalist Cuts & Premiere Export

One of the most powerful workflows in CAT+TAG:

1. Transcribe a long interview or documentary clip.
2. Open **AI Journalist Cut** from the inspector.
3. Ask the AI to create 1–5 professional short versions (different tones/purposes supported).
4. For each version you can:
   - Export a **Premiere-ready XMEML v4** sequence with correct audio track linking (two Mono tracks for typical 2-channel sources)
   - Export a self-contained rendered **MP4 or WAV** ("Export as New Clip")

The XMEML exporter is built to match native Premiere Pro export structure as closely as possible, including proper timecode, masterclip IDs, indexed links, and reliable audio channel handling.

**Save & re-open AI Director stories for later export** (also called "saved projects")

After generating versions in AI Journalist / AI Director:
- Click **"Save Story"** on a version card. This saves the full cut + any narration bridges (as JSON) e.g. `AIStory_A_Muodin_Inhimillinen_Hinta_ja_Muotoksen_Voima_20260602_192118.json` under your exports folder in `ai_director_stories/`.
- **To open a specific saved story JSON** (to render narrations/voiceovers + get the complete export package):
  - Command line (recommended, works from anywhere):
    ```bash
    uv run minicat open /path/to/AIStory_....json
    ```
    Launches the app and directly triggers the full export flow for that story (with progress).
  - Or in-app: **"Load Story"** button in the top bar → choose the .json (or pick from recent list).
- **What you always get in the export** (main "Export XML" button or loaded story):
  - A fresh dated subfolder inside your default export directory (e.g. `~/CAT+TAG/Exports/AI_Muodin_..._20260603_123456/`).
  - The multi-source XMEML XML.
  - The full rich **`AI DIRECTOR — MULTI-CLIP SCRIPT.txt`** (complete attributed narrative script with every clip + any AI narration bridges, source filenames, in/out times, reasons, etc.). When narration controls were used, the script respects the requested duration/bridge budgets. The TXT is localized to the language of the scripted content.
  - Voiceover audio files (`Narration.wav` + `Narration_BridgeNN.wav` — 44.1 kHz stereo WAV via local Piper, or MP3 via Google) **when the version contains a narration script**.
- Use the **"XML + Voiceover Audio"** button (or the choice dialog) for custom language/voice selection + live per-bridge progress.
- **Settings → Text to Speech** tab contains all voiceover configuration (Piper local recommended for offline use, Google Cloud option, default language/voice, Test button, "Prepare voices", Google login or credentials JSON file, etc.). TTS settings were moved here from the old Translation tab.

This workflow lets you re-render voiceovers and get the full package (XML + rich script TXT + audio) later without re-running the AI Director.

**Narration budgeting (new in AI Journalist & Director)**
When you select a narration/voiceover style in either tool, the generation dialog now exposes:
- Narration min/max seconds — total spoken duration budget for the AI-written narration script (or sum of all bridges in Director).
- Min/max bridges — exact range for the number of narration insertions/sections the AI should produce.
The AI is instructed to respect these budgets while still keeping bridges purposeful and sparse. The resulting rich export TXT reflects the chosen language and structure.

## Demos

See [assets/README.md](assets/README.md) for guidelines on recording demos.

### AI Transcription + Translation + Subtitle Burning

![Transcription Demo](assets/transcription-demo.gif)

- Transcribe any clip with accurate timecodes using Gemini
- Translate to multiple languages while keeping perfect sync
- Burn professional EBU-styled subtitles directly into the video
- Export clean `.srt` files for any language

*(Recording a good demo GIF for this section is high priority.)*

## Why CAT+TAG?

You already have excellent tools for *finished* projects (DaVinci Resolve, Premiere, Final Cut). CAT+TAG exists for the messy middle — the raw drives, the client folders, the personal projects, the "I know I have that shot somewhere..." moments.

It focuses on fast visual browsing, rich metadata, **and modern AI tools** (transcription, translation, AI journalist cuts, and clean Premiere XMEML export) — without the complexity or cost of a full MAM system. Everything stays local and private.

## Development

```bash
uv sync
uv run ruff check .
uv run minicat
```

Key modules:
- `minicat/ui/app.py` — Main NiceGUI interface
- `minicat/core/video.py` — ffmpeg/ffprobe helpers (proxies, storyboards, audio extraction, burning)
- `minicat/ai/` — Gemini integration (tag suggestions + transcription + translation + journalist cutter)
- `minicat/ai/xmeml_exporter.py` — Premiere Pro XMEML v4 exporter (correct audio tracks, timecode-aware)
- `minicat/core/db.py` — SQLite layer with FTS5

New AI features live primarily in `minicat/ai/` (transcriber, journalist_cutter, xmeml_exporter) and are wired into the inspector in `app.py`.

Demo assets (GIFs, screenshots) live in `assets/`. See `assets/README.md` for guidelines.

## Building a Standalone macOS App

```bash
uv run python scripts/build_macos_app.py
```

The script will ensure PyInstaller is installed in the project environment. The resulting `CAT+TAG.app` can be moved to `/Applications`.

**Note:** Even with a bundled app, users will still need `ffmpeg` installed on their system (via Homebrew on macOS).

## Roadmap / Coming Soon

### High Priority
- Dedicated **Transcription panel** in the inspector with clickable timestamps (seek in player)
- One-click "Export .srt + Burn video" flow with language selection
- More subtitle styling options when burning (font, position, colors, EBU presets)
- Proper dedicated storage for transcription data (instead of Notes field)

### Medium Priority
- Support for multiple audio tracks / language tracks per clip
- Better handling of very long videos during transcription (chunking)
- Windows & Linux packaging improvements + installers

### Nice to Have
- Better performance with very large libraries (thousands of clips)
- Batch transcription / translation of multiple clips
- Speaker identification in transcripts
- Integration with external players (e.g. open in VLC at specific timestamp)

Feedback and ideas are very welcome!

## License

Copyright © 2026 Mikko Niittymäki

This project is licensed under the MIT License — do whatever you want with it.

The full license text is available in the [LICENSE](LICENSE) file.

---

Made with love for messy video archives. May it help with yours too.
