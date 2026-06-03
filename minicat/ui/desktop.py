"""
Desktop / Native Window launcher for CAT+TAG using pywebview.

This allows running CAT+TAG as a real visual desktop application
(no visible terminal, no browser tab).
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Callable
from contextlib import closing
from threading import Thread

import webview
from nicegui import ui


def _find_free_port() -> int:
    """Find an available port on localhost."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def launch_desktop(
    catalog_root: str | None = None,
    *,
    title: str = "CAT+TAG",
    width: int = 1920,
    height: int = 1080,
    on_window_loaded: Callable[[], None] | None = None,
    initial_story_path: str | None = None,
) -> None:
    """
    Launch CAT+TAG inside a native desktop window using pywebview.

    Default window size is 1920x1080 (Full HD).
    This is currently the best way to get a true "visual app" experience.

    initial_story_path: if provided (a .json AIStory file), the app will auto-load it
    after startup and open the dialog to render narrations (TTS) + export XML.
    """
    from minicat.core import settings
    from minicat.ui.app import setup_ui

    if initial_story_path:
        os.environ["CAT_TAG_INITIAL_STORY"] = str(initial_story_path)

    # Smart catalog selection
    # Uses ~/CAT+TAG by default (or migrates legacy bad paths). Honors explicit last_catalog.
    effective_catalog = catalog_root
    if effective_catalog is None:
        eff = settings.get_effective_catalog()
        effective_catalog = str(eff)

    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"

    def start_nicegui_server():
        """Run the NiceGUI web server in a background thread."""
        import os
        if os.environ.get("CAT_TAG_USE_NEW_WORKSPACE") == "1":
            # Previous experimental workspace (v1 attempt)
            from minicat.ui.workspace import setup_workspace_ui
            setup_workspace_ui(effective_catalog)
        else:
            setup_ui(effective_catalog)

        ui.run(
            host="127.0.0.1",
            port=port,
            reload=False,
            show=False,
            dark=True,
            title=title,
        )

    # Start NiceGUI server in background
    server_thread = Thread(
        target=start_nicegui_server,
        daemon=True,
        name="CAT+TAG-NiceGUI-Server"
    )
    server_thread.start()

    # Wait until the local server is ready to accept connections
    for _ in range(60):  # up to 6 seconds
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"CAT+TAG server failed to start on port {port}")

    # Give NiceGUI enough time to render the welcome screen and attach all click handlers
    time.sleep(1.2)

    # Create the actual native desktop window
    window_title = title if os.environ.get("CAT_TAG_USE_NEW_WORKSPACE") != "1" else "CAT+TAG — Clean Workspace (v1)"
    window = webview.create_window(
        title=window_title,
        url=url,
        width=width,
        height=height,
        min_size=(1100, 720),
        resizable=True,
        text_select=True,
        background_color="#121212",
    )

    if on_window_loaded:
        window.events.loaded += on_window_loaded

    # This blocks until the user closes the window
    webview.start(debug=False)
