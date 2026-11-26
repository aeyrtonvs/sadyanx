"""Push notifications: macOS banner always; iPhone through ntfy only if you set a topic."""

from __future__ import annotations

import subprocess
import urllib.request

from . import net


def _osascript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_script(title: str, message: str, subtitle: str = "", sound: str = "") -> str:
    script = f"display notification {_osascript_quote(message[:240])} with title {_osascript_quote(title)}"
    if subtitle:
        script += f" subtitle {_osascript_quote(subtitle[:160])}"
    if sound:
        script += f" sound name {_osascript_quote(sound)}"
    return script


def send(title: str, message: str, cfg_notify: dict, log=None, subtitle: str = "") -> None:
    """`subtitle` is the persona's line (see persona.py): an extra, never the facts."""
    if cfg_notify.get("macos", True):
        script = macos_script(title, message, subtitle, cfg_notify.get("sound", "Glass"))
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            if log:
                log(f"macOS notification failed: {exc}")
    topic = (cfg_notify.get("ntfy_topic") or "").strip()
    if topic:
        server = (cfg_notify.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
        body = "\n".join(x for x in (title, subtitle, message) if x).encode()
        req = urllib.request.Request(f"{server}/{topic}", data=body, method="POST")
        try:
            net.urlopen(req, timeout=10).read()
        except Exception as exc:  # a failed push must never fail a task
            if log:
                log(f"ntfy notification failed: {exc}")
