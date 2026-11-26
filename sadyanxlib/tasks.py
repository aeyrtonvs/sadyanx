"""The queue: one Markdown file per task in tasks/, with a scalar-only frontmatter.

The frontmatter is deliberately a flat `key: value` list (no nesting, no lists) so
it is trivial to edit by hand and to parse without a YAML dependency. Everything
structured (context, acceptance criteria, notes) lives in the Markdown body.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

STATUSES = (
    "draft",           # being written; `run` refuses it
    "ready",           # waiting to be run
    "running",
    "needs-approval",  # parked before an irreversible action
    "done",
    "failed",
    "blocked",         # the agent could not proceed without information from you
    "killed",
)

FIELD_ORDER = [
    "id", "title", "type", "repo", "source", "jira", "slack_channel", "slack_thread", "status", "phase",
    "model", "effort", "budget_usd", "timeout_min",
    "branch", "worktree", "session_id", "attempts", "rework", "cost_usd",
    "pr_url", "doc_url", "slack_reply", "report", "reason",
    "created", "updated",
]


def now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def slugify(text: str, limit: int = 40) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:limit].rstrip("-") or "task"


@dataclass
class Task:
    path: Path
    meta: dict[str, str] = field(default_factory=dict)
    body: str = ""

    # --- typed accessors -------------------------------------------------
    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def title(self) -> str:
        return self.meta.get("title", "")

    @property
    def status(self) -> str:
        return self.meta.get("status", "draft")

    @property
    def type(self) -> str:
        return self.meta.get("type", "code")

    def get(self, key: str, default: str = "") -> str:
        return self.meta.get(key, default) or default

    def num(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.meta.get(key) or default)
        except ValueError:
            return default

    def set(self, **values) -> None:
        for key, value in values.items():
            if value is None:
                value = ""
            if isinstance(value, float):
                value = f"{value:.4f}".rstrip("0").rstrip(".")
            self.meta[key] = str(value).replace("\n", " ").strip()

    # --- persistence -----------------------------------------------------
    def save(self) -> None:
        self.meta["updated"] = now()
        keys = [k for k in FIELD_ORDER if k in self.meta]
        keys += [k for k in self.meta if k not in FIELD_ORDER]
        lines = ["---"] + [f"{k}: {self.meta[k]}" for k in keys] + ["---", ""]
        text = "\n".join(lines) + self.body.lstrip("\n")
        if not text.endswith("\n"):
            text += "\n"
        tmp = self.path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)


def parse(path: Path) -> Task:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                key, sep, value = line.partition(":")
                if sep:
                    meta[key.strip()] = value.strip()
            body = text[end + 4:].lstrip("\n")
    if "id" not in meta:
        meta["id"] = path.stem.split("-", 1)[0]
    return Task(path=path, meta=meta, body=body)


def all_tasks() -> list[Task]:
    d = paths.tasks_dir()
    if not d.exists():
        return []
    return [parse(p) for p in sorted(d.glob("*.md"))]


def find(ref: str) -> Task:
    """Accepts 7, 0007, 0007-some-slug, a file path, or a Jira key."""
    ref = ref.strip()
    p = Path(ref)
    if p.suffix == ".md" and p.exists():
        return parse(p)
    tasks = all_tasks()
    if ref.isdigit():
        wanted = f"{int(ref):04d}"
        for t in tasks:
            if t.id == wanted:
                return t
    for t in tasks:
        if t.path.stem == ref or t.id == ref:
            return t
    matches = [t for t in tasks if t.get("jira").upper() == ref.upper()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(t.id for t in matches)
        raise LookupError(f"several tasks match {ref}: {ids}; use the id")
    raise LookupError(f"no task '{ref}' in {paths.tasks_dir()}")


def next_id() -> str:
    highest = 0
    for t in all_tasks():
        if t.id.isdigit():
            highest = max(highest, int(t.id))
    return f"{highest + 1:04d}"


def create(title: str, body: str, **meta) -> Task:
    paths.tasks_dir().mkdir(parents=True, exist_ok=True)
    task_id = next_id()
    path = paths.tasks_dir() / f"{task_id}-{slugify(title)}.md"
    task = Task(path=path, meta={"id": task_id, "title": title}, body=body)
    task.set(created=now(), attempts=0, cost_usd=0.0, **meta)
    task.save()
    return task
