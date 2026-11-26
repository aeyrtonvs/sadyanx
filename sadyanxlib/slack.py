"""Replies on Slack to whoever asked for a task (D26).

A task created from a Slack message keeps where it came from (`slack_channel`, and
`slack_thread` when the reply belongs in a thread). The agent writes only the body of the
reply in its result; the harness wraps it in the configured format, shows it to you and sends
it only on your yes, in a short session pinned to that exact call. Nothing here talks to Slack.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# [slack] in sadyanx.toml: the format every reply follows, and the tool that sends it.
SLACK_DEFAULTS = {
    "greeting": "Hi!",          # the first line of every reply
    "sign_off": "Regards.",     # the last line
    "language": "English",      # the language the agent writes the body in
    "max_chars": 3500,          # longer bodies are refused, not cut
    # the claude.ai Slack connector's send tool, as Claude Code names it (a regex)
    "send_tool": r"mcp__claude_ai_Slack__slack_send_message",
}

CHANNEL = re.compile(r"^[CDG][A-Z0-9]{6,}$")
TS = re.compile(r"^\d{9,}\.\d{6}$")
PERMALINK_TS = re.compile(r"^p(\d{10,})(\d{6})$")


class OriginError(ValueError):
    pass


def parse_origin(value: str) -> tuple[str, str]:
    """(channel, thread_ts) from a message permalink or CHANNEL[/TS].

    Where the reply goes: in a direct message (channel D...) it is a plain message in the same
    conversation, unless the request was itself inside a thread; anywhere else it is a reply in
    the request's thread, so a channel does not get the answer at the top level.
    """
    value = (value or "").strip()
    if not value:
        raise OriginError("empty Slack origin")
    thread = ""
    if value.startswith(("http://", "https://")):
        url = urlparse(value)
        parts = [p for p in url.path.split("/") if p]
        if len(parts) < 3 or parts[0] != "archives":
            raise OriginError(f"not a Slack message link: {value}")
        channel, stamp = parts[1], parts[2]
        m = PERMALINK_TS.match(stamp)
        if not m:
            raise OriginError(f"no message timestamp in the link: {value}")
        ts = f"{m.group(1)}.{m.group(2)}"
        thread = (parse_qs(url.query).get("thread_ts") or [""])[0]
    else:
        channel, _, ts = value.replace(":", "/").partition("/")
    if not CHANNEL.match(channel):
        raise OriginError(f"not a Slack channel id: {channel}")
    for stamp in (ts, thread):
        if stamp and not TS.match(stamp):
            raise OriginError(f"not a Slack timestamp: {stamp}")
    if thread:
        return channel, thread
    if channel.startswith("D"):
        return channel, ""
    return channel, ts


def render(body: str, fmt: dict) -> str:
    """The reply as it is sent: greeting, the agent's body, sign-off. A greeting or sign-off the
    agent added anyway is dropped, so it is never doubled."""
    greeting, sign_off = fmt["greeting"].strip(), fmt["sign_off"].strip()
    lines = body.strip().splitlines()

    def same(a: str, b: str) -> bool:
        norm = lambda x: re.sub(r"[\W_]+", "", x).lower()  # noqa: E731
        return bool(norm(b)) and norm(a) == norm(b)

    while lines and (not lines[0].strip() or same(lines[0], greeting)):
        lines.pop(0)
    while lines and (not lines[-1].strip() or same(lines[-1], sign_off)):
        lines.pop()
    text = "\n".join(lines).strip()
    return "\n\n".join(part for part in (greeting, text, sign_off) if part)


def problem(body: str, fmt: dict) -> str | None:
    """Why a drafted body cannot be sent, or None."""
    text = (body or "").strip()
    if not text:
        return "the reply is empty"
    if len(text) > int(fmt["max_chars"]):
        return f"the reply has {len(text)} characters; the limit is {fmt['max_chars']}"
    return None


def send_args(channel: str, thread: str, text: str) -> dict:
    """The exact input of the send call, which the guard pins."""
    args = {"channel_id": channel, "message": text}
    if thread:
        args["thread_ts"] = thread
    return args


def where(channel: str, thread: str) -> str:
    kind = "direct message" if channel.startswith("D") else "channel"
    return f"{kind} {channel}" + (f", in the thread {thread}" if thread else "")
