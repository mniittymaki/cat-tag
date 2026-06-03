#!/usr/bin/env python3
"""
Quick test harness for the new AI Director (multi-clip) module.

Usage:
    uv run python scripts/test_ai_director.py

What it does:
- Builds a realistic multi-source combined labeled transcript.
- Prints exactly what the Director AI would receive as context.
- Exercises build_combined_labeled_transcript().
- Optionally makes a real (cheap) call to Gemini if you have an API key
  and pass --live (uses very small limits + 1 version).

This does NOT touch journalist_cutter.py at all.
"""

import sys
from pathlib import Path

# Make sure we can import from the project
sys.path.insert(0, str(Path(__file__).parent.parent))

from minicat.ai.director import (
    build_combined_labeled_transcript,
    generate_director_cuts,
    get_narrative_sequence,
)
from minicat.ai.narrative_vo_exporter import export_narrative_vo_xmeml
from minicat.ai.voiceover import get_google_tts_status


def make_fake_multi_source_data():
    """Create richer fake multi-source interview material for realistic testing."""
    c1_segments = [
        {
            "source_in": 8.2,
            "source_out": 14.7,
            "text": "I started working here in 2017. It was already under pressure, but people still believed in it.",
        },
        {
            "source_in": 18.9,
            "source_out": 26.4,
            "text": "The first round of cuts came in 2020. We lost three nurses on my ward in one month.",
        },
        {
            "source_in": 34.1,
            "source_out": 42.8,
            "text": "I remember one patient asking me why the night shift was always so short-staffed. I didn't know what to say.",
        },
        {
            "source_in": 51.6,
            "source_out": 59.3,
            "text": "Some of the younger staff left for better pay elsewhere. I understood why, but it hurt the team.",
        },
        {
            "source_in": 71.0,
            "source_out": 79.5,
            "text": "The last straw for me was when they closed the step-down unit. That was the safety valve.",
        },
        {
            "source_in": 94.2,
            "source_out": 101.8,
            "text": "I still come in early most days. I don't think the patients should suffer because of decisions made in an office.",
        },
    ]

    c2_segments = [
        {
            "source_in": 4.5,
            "source_out": 12.1,
            "text": "We had to make difficult decisions. The budget envelope was fixed by the ministry.",
        },
        {
            "source_in": 19.7,
            "source_out": 27.3,
            "text": "We did model the impact. We knew there would be pressure on the floor.",
        },
        {
            "source_in": 33.8,
            "source_out": 41.2,
            "text": "The priority was protecting acute care beds. That was the political reality.",
        },
        {
            "source_in": 48.9,
            "source_out": 57.6,
            "text": "I accept that front-line staff felt the consequences more than we anticipated.",
        },
        {
            "source_in": 65.4,
            "source_out": 72.0,
            "text": "There were proposals for targeted retention payments, but they didn't get approved.",
        },
        {
            "source_in": 81.3,
            "source_out": 88.7,
            "text": "In hindsight, the speed of the reductions was probably too aggressive.",
        },
    ]

    sources = [
        {
            "label": "C1",
            "filename": "ward_nurse_senior_2024-03-12.mp4",
            "duration": 112.0,
            "segments": c1_segments,
        },
        {
            "label": "C2",
            "filename": "health_official_interview.mp4",
            "duration": 95.0,
            "segments": c2_segments,
        },
    ]
    return sources


def make_fake_narrated_version():
    """Create a realistic version dict that includes narration bridges (for exporter #3 testing)."""
    segments = [
        {
            "source_label": "C1",
            "source_in": 12.3,
            "source_out": 28.7,
            "text": "I started working here in 2017. It was already under pressure, but people still believed in it.",
            "source_path": "/tmp/test_c1.mp4",
        },
        {
            "source_label": "C2",
            "source_in": 45.1,
            "source_out": 59.2,
            "text": "We had to make difficult decisions. The budget envelope was fixed by the ministry.",
            "source_path": "/tmp/test_c2.mp4",
        },
        {
            "source_label": "C1",
            "source_in": 71.0,
            "source_out": 79.5,
            "text": "The last straw for me was when they closed the step-down unit. That was the safety valve.",
            "source_path": "/tmp/test_c1.mp4",
        },
        {
            "source_label": "C3",
            "source_in": 92.4,
            "source_out": 108.1,
            "text": "I remember one patient asking me why the night shift was always so short-staffed.",
            "source_path": "/tmp/test_c3.mp4",
        },
    ]

    narrative_elements = [
        {
            "type": "clip",
            "source_label": "C1",
            "source_in": 12.3,
            "source_out": 28.7,
            "text": segments[0]["text"],
        },
        {
            "type": "narration",
            "text": "This moment captures the initial optimism before the cuts began to bite.",
        },
        {
            "type": "clip",
            "source_label": "C2",
            "source_in": 45.1,
            "source_out": 59.2,
            "text": segments[1]["text"],
        },
        {
            "type": "narration",
            "text": "The official rationale was presented as necessary and unavoidable.",
        },
        {
            "type": "clip",
            "source_label": "C1",
            "source_in": 71.0,
            "source_out": 79.5,
            "text": segments[2]["text"],
        },
        {
            "type": "narration",
            "text": "For frontline staff, the loss of the step-down unit removed the last buffer.",
        },
    ]

    version = {
        "version_id": "TEST-NARR-001",
        "title": "Test Narrated Multi Cut",
        "total_duration": 104.2,
        "narrative_summary": "A test version with interleaved narration bridges for exporter #3 validation.",
        "narrative_elements": narrative_elements,
        "selected_segments": segments,
        "narration_language": "en",
    }
    return version


def test_narrative_exporter():
    """Test exporter #3 (Multi + Narration / Voiceover)."""
    print("\n" + "=" * 70)
    print("EXPORTER #3 — NARRATIVE + VOICEOVER TEST")
    print("=" * 70)

    ver = make_fake_narrated_version()

    print("\n[1] Version has narration data:")
    narrative_seq = get_narrative_sequence(ver)
    narration_count = sum(1 for item in narrative_seq if item.get("type") == "narration")
    print(f"    Total narrative elements: {len(narrative_seq)}")
    print(f"    Narration bridges: {narration_count}")

    print("\n[2] Current TTS status:")
    tts_status = get_google_tts_status()
    print(f"    Provider: {tts_status['provider']}")
    print(f"    Status: {tts_status['message']}")

    # Test modes
    modes = [
        ("Text Titles only (no TTS)", dict(narration_as_titles=True, generate_voiceover=False)),
        (
            "Voiceover generation (uses current provider)",
            dict(generate_voiceover=True, narration_as_titles=False),
        ),
    ]

    for label, kwargs in modes:
        print(f"\n[3] Testing: {label}")
        try:
            result = export_narrative_vo_xmeml(ver, **kwargs)
            if result:
                print(f"    ✓ Exported to: {result}")
            else:
                print("    ✗ Exporter returned None")
        except Exception as e:
            print(f"    ✗ Error: {e}")
            import traceback

            traceback.print_exc()

    print("\n" + "=" * 70)
    print("Exporter #3 test complete.")
    print("You can pass --export-narrative to run only this part.")
    print("=" * 70)


def main():
    print("=" * 70)
    print("AI DIRECTOR TEST HARNESS")
    print("=" * 70)

    sources = make_fake_multi_source_data()

    # 1. Test the canonical labeled transcript builder (the heart of the feature)
    print("\n[1] Building combined labeled transcript (what the Director actually sees)...\n")
    combined, flat_segments = build_combined_labeled_transcript(sources)

    print(combined)
    print("\n---")
    print(f"Total segments in flat list: {len(flat_segments)}")
    print(f"Source labels present: {sorted(set(s.get('source_label') for s in flat_segments))}")

    # 2. Show that source metadata survives
    print("\n[2] Sample augmented segment (source tracking intact):")
    if flat_segments:
        print(flat_segments[0])

    # 3. Dry-run prompt inspection (no API call)
    print("\n[3] Prompt construction check (no API key needed for this part)...")
    print("    The Director module is ready to receive the labeled transcript above.")
    print("    When you run with a real multi-clip selection in the app, this exact")
    print("    structured format (with ### C1: filename headers + readable timecodes)")
    print("    is what gets sent to Gemini for the AI Director.")

    # 4. Optional: real (but cheap) live test
    if "--live" in sys.argv or "--real" in sys.argv:
        print("\n[4] LIVE TEST requested — attempting real Gemini call...")

        # Allow user to pick tone/purpose/narration_style via CLI for new features
        tone = "rewrite"
        if "--newsroom" in sys.argv:
            tone = "newsroom"
        if "--documentary" in sys.argv:
            tone = "documentary"
        if "--investigative" in sys.argv:
            tone = "investigative_hook"
        if "--underdog" in sys.argv:
            tone = "underdog"
        if "--engagement" in sys.argv:
            tone = "engagement_bomb"

        purpose = "In-depth Highlight"
        if "--underdog-purpose" in sys.argv or "--three-act" in sys.argv:
            purpose = "Three-Act Underdog Arc"
        if "--investigative-purpose" in sys.argv:
            purpose = "Investigative Cold Open"
        if "--jump-cut" in sys.argv:
            purpose = "Social Jump-Cut Strip"

        # New narration_style support
        narration_style = None
        for arg in sys.argv:
            if arg.startswith("--narration-style="):
                val = arg.split("=", 1)[1].strip().lower()
                if val in ("omniscient", "subjective", "explainer"):
                    narration_style = val
                elif val in ("none", "false", ""):
                    narration_style = None
        if "--no-narration" in sys.argv:
            narration_style = None
        if "--omniscient" in sys.argv:
            narration_style = "omniscient"
        if "--subjective" in sys.argv:
            narration_style = "subjective"
        if "--explainer" in sys.argv:
            narration_style = "explainer"

        print(f"    Tone: {tone}")
        print(f"    Purpose: {purpose}")
        print(f"    narration_style: {narration_style}")
        print("    Using modest constraints (max 60s, 1-2 versions) to keep cost low.")

        try:
            versions = generate_director_cuts(
                segments=flat_segments,
                max_duration_seconds=60.0,
                min_duration_seconds=25.0,
                purpose=purpose,
                tone=tone,
                num_versions=2 if tone != "rewrite" else 1,
                clean_fillers=False,
                narration_style=narration_style,
                combined_transcript=combined,
                source_count=2,
            )
            print("\n✓ SUCCESS — Director returned versions:")
            for v in versions:
                print(f"\n  Version {v.get('version_id')}: {v.get('title')}")
                print(f"    Duration: {v.get('total_duration')}s")
                print(f"    Summary: {v.get('narrative_summary', '')}")
                print(f"    narration_language: {v.get('narration_language')}")
                print(f"    Has narrative_elements: {bool(v.get('narrative_elements'))}")
                segs = v.get("selected_segments", [])
                print(f"    Segments ({len(segs)}):")
                for s in segs[:4]:
                    src = s.get("source_label", "?")
                    print(
                        f"      [{src}] {s['source_in']:.1f}s–{s['source_out']:.1f}s: {s['text'][:70]}..."
                    )
                if len(segs) > 4:
                    print(f"      ... and {len(segs) - 4} more")
                if v.get("narrative_elements"):
                    bridges = [e for e in v["narrative_elements"] if e.get("type") == "narration"]
                    print(f"    Narration bridges in elements: {len(bridges)}")
        except Exception as e:
            print(f"\n✗ Live call failed: {e}")
            import traceback

            traceback.print_exc()
    else:
        print("\n[4] Skipping live Gemini call (pass --live to attempt a real call).")
        print("    New flags: --underdog --investigative --engagement --three-act --jump-cut")
        print("    --narration-style=omniscient|subjective|explainer  (or --omniscient etc.)")
        print("    --no-narration")

    # 5. Exporter #3 (Narrative + Voiceover) testing
    if "--export-narrative" in sys.argv or "--narrative" in sys.argv:
        test_narrative_exporter()
    else:
        print(
            "\n[5] Skipping Exporter #3 test (pass --export-narrative to test narrative_vo_exporter)."
        )
        print(
            "    This exercises voiceover generation (current TTS provider), titles-only mode, and full multi XMEML with narration bridges."
        )
        print("    Useful for validating exporter #3 before using it from the UI.")

    print("\n" + "=" * 70)
    print("Test complete.")
    print("=" * 70)


if __name__ == "__main__":
    main()
