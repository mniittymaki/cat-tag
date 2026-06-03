#!/usr/bin/env python3
"""
Build a standalone macOS .app bundle for CAT+TAG.

This produces a native-looking macOS application you can drag to /Applications.

Recommended usage (ensures correct virtualenv + deps):
    uv run python scripts/build_macos_app.py

Alternative (if you have activated the .venv):
    uv pip install pyinstaller
    python scripts/build_macos_app.py
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
ENTRY = PROJECT_ROOT / "minicat" / "ui" / "desktop.py"

APP_NAME = "CAT+TAG"


def main():
    print("Building CAT+TAG macOS app bundle...")
    print("This may take a minute or two.\n")

    # Robustness: ensure PyInstaller is available. Prefer uv environment.
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller not found. Installing via 'uv pip install pyinstaller' into the project environment..."
        )
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "pyinstaller"], cwd=PROJECT_ROOT
            )
        except Exception:
            print("Direct pip install failed. Trying 'uv pip install pyinstaller'...")
            subprocess.check_call(["uv", "pip", "install", "pyinstaller"], cwd=PROJECT_ROOT)

    # Common hidden imports needed for NiceGUI + pywebview + our stack
    hidden_imports = [
        "nicegui",
        "nicegui.ui",
        "nicegui.elements",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.websockets",
        "uvicorn.lifespan",
        "starlette",
        "starlette.applications",
        "starlette.routing",
        "starlette.responses",
        "starlette.staticfiles",
        "webview",
        "webview.platforms.cocoa",
        "engineio.async_drivers",
        "engineio.async_drivers.threading",
        "socketio.async_drivers",
    ]

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name",
        APP_NAME,
        "--windowed",  # no console window on macOS
        "--onedir",
        "--clean",
        "--noconfirm",
        "--strip",  # smaller binary
        "--noupx",  # avoid upx issues on arm64
    ]

    # Icon handling: prefer .icns, auto-generate from PNG if missing (macOS only)
    icon_path = PROJECT_ROOT / "assets" / "cat-tag.icns"
    png_path = PROJECT_ROOT / "assets" / "cat-tag.png"

    if not icon_path.exists() and png_path.exists():
        print("  Generating .icns from assets/cat-tag.png ...")
        try:
            _generate_icns_from_png(png_path, icon_path)
        except Exception as e:
            print(f"  Warning: Could not generate .icns ({e})")

    if icon_path.exists():
        cmd += ["--icon", str(icon_path)]
    else:
        print("  (No icon found — building without custom icon)")

    # Hidden imports
    for imp in hidden_imports:
        cmd += ["--hidden-import", imp]

    # Collect the whole minicat package + nicegui static assets (critical for frozen NiceGUI apps)
    cmd += ["--collect-all", "minicat"]
    cmd += ["--collect-all", "nicegui"]
    cmd += ["--collect-data", "nicegui"]
    cmd += ["--collect-data", "starlette"]
    cmd += ["--collect-data", "aiofiles"]

    # The actual entry script
    cmd += [str(ENTRY)]

    # Preserve the committed portable CAT+TAG.spec.
    # PyInstaller's -m run will overwrite it in cwd with a generated version containing absolute paths.
    spec_path = PROJECT_ROOT / "CAT+TAG.spec"
    orig_spec = spec_path.read_bytes() if spec_path.exists() else None

    print("Running PyInstaller...\n")
    print("Command:", " ".join(cmd))
    print()

    subprocess.check_call(cmd, cwd=PROJECT_ROOT)

    # Restore the clean committed .spec so the working tree stays clean and the repo version remains portable.
    if orig_spec is not None:
        spec_path.write_bytes(orig_spec)
        print("  (Restored the portable CAT+TAG.spec from repo)")

    app_path = PROJECT_ROOT / "dist" / f"{APP_NAME}.app"
    print(f"\n✅ Built: {app_path}")
    print()
    print("Important notes for distribution:")
    print("  • Drag CAT+TAG.app to /Applications (or your Applications folder)")
    print("  • Users will still need ffmpeg installed (brew install ffmpeg)")
    print("  • On first run, macOS may show a Gatekeeper warning if unsigned")
    print("  • For easy sharing with others, you will need to sign + notarize the app")
    print()
    print("To test the built app:")
    print(f"  open {app_path}")
    print()
    print("Next time, run with: uv run python scripts/build_macos_app.py  (recommended)")


def _generate_icns_from_png(png_path: Path, icns_path: Path) -> None:
    """Generate a .icns file from a PNG using macOS tools (requires Pillow + iconutil)."""
    import tempfile

    from PIL import Image

    iconset = Path(tempfile.mkdtemp()) / "cat-tag.iconset"
    iconset.mkdir(parents=True)

    sizes = [16, 32, 128, 256, 512, 1024]
    for s in sizes:
        img = Image.open(png_path).resize((s, s), Image.Resampling.LANCZOS)
        img.save(iconset / f"icon_{s}x{s}.png")
        img2 = Image.open(png_path).resize((s * 2, s * 2), Image.Resampling.LANCZOS)
        img2.save(iconset / f"icon_{s}x{s}@2x.png")

    subprocess.check_call(["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)])
    # Cleanup
    import shutil

    shutil.rmtree(iconset.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
