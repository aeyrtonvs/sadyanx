"""Push notifications: macOS banner always; iPhone through ntfy only if you set a topic."""

from __future__ import annotations

import subprocess
import urllib.request

from . import net


def _osascript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def send(title: str, message: str, cfg_notify: dict, log=None) -> None:
    if cfg_notify.get("macos", True):
        script = f"display notification {_osascript_quote(message[:240])} with title {_osascript_quote(title)}"
        sound = cfg_notify.get("sound", "Glass")
        if sound:
            script += f" sound name {_osascript_quote(sound)}"
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            if log:
                log(f"macOS notification failed: {exc}")
    topic = (cfg_notify.get("ntfy_topic") or "").strip()
    if topic:
        server = (cfg_notify.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
        body = f"{title}\n{message}".encode()
        req = urllib.request.Request(f"{server}/{topic}", data=body, method="POST")
        try:
            net.urlopen(req, timeout=10).read()
        except Exception as exc:  # a failed push must never fail a task
            if log:
                log(f"ntfy notification failed: {exc}")
