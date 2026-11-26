"""runs/<id>/report.md: what the agent did, with evidence, for you to read afterwards."""

from __future__ import annotations

import json
from pathlib import Path

from . import approvals, gitops
from .tasks import Task, now


def _audit_counts(run_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    f = run_dir / "audit.jsonl"
    if not f.exists():
        return counts
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            verdict = json.loads(line).get("verdict", "?")
        except json.JSONDecodeError:
            continue
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def _denials(run_dir: Path, limit: int = 15) -> list[str]:
    f = run_dir / "audit.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("verdict") in ("deny", "deny-rejected"):
            out.append(f"- `{e.get('tool')}` ({e.get('category')}): {e.get('reason')}")
    return out[-limit:]


def write(task: Task, run_dir: Path, result: dict | None) -> Path:
    result = result or {}
    lines = [
        f"# {task.id} — {task.title}",
        "",
        f"- **Status:** {task.status}" + (f" — {task.get('reason')}" if task.get("reason") else ""),
        f"- **Type:** {task.type}" + (f" · repo `{task.get('repo')}`" if task.get("repo") else ""),
    ]
    if task.get("jira"):
        lines.append(f"- **Jira:** {task.get('jira')}")
    if task.get("branch"):
        lines.append(f"- **Branch:** `{task.get('branch')}`")
    if task.get("pr_url"):
        lines.append(f"- **PR:** {task.get('pr_url')}")
    if task.get("doc_url"):
        lines.append(f"- **Document:** {task.get('doc_url')}")
    lines += [
        f"- **Cost:** ${task.num('cost_usd'):.2f} of ${task.num('budget_usd'):.2f}"
        f" · agent attempts: {int(task.num('attempts'))}",
        f"- **Generated:** {now()}",
        "",
    ]
    if result.get("summary"):
        lines += ["## Summary", "", result["summary"].strip(), ""]

    criteria = result.get("criteria") or []
    if criteria:
        lines += ["## Acceptance criteria", ""]
        for c in criteria:
            mark = "✅" if c.get("met") else "❌"
            lines.append(f"- {mark} **{c.get('criterion', '').strip()}**")
            if c.get("evidence"):
                lines.append(f"  - Evidence: {c['evidence'].strip()}")
        lines.append("")

    gate_logs = sorted(run_dir.glob("gates-*.log"))
    if gate_logs:
        last = gate_logs[-1]
        verdicts = [line for line in last.read_text(encoding="utf-8").splitlines() if line.startswith(("$ ", "[exit"))]
        lines += [f"## Gates (last attempt: `{last.name}`)", "", "```", *verdicts, "```", ""]

    reviews = sorted((p for p in run_dir.glob("code-review-*.json") if p.stem.rsplit("-", 1)[-1].isdigit()),
                     key=lambda p: int(p.stem.rsplit("-", 1)[-1]))
    if reviews:
        try:
            last = json.loads(reviews[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            last = {}
        found = last.get("findings") or []
        title = f"## Code review (`/code-review {last.get('level', '?')}`, attempt {reviews[-1].stem.rsplit('-', 1)[-1]})"
        if found:
            if task.get("code_review_open"):
                intro = (f"{len(found)} finding(s), still open: the review came at the last attempt, with none "
                         "left to go through them (they are listed in the PR too):")
            elif last.get("answer"):
                intro = (f"{len(found)} finding(s). The agent went through them once (the review is not run "
                         "again; the external PR review is the next look):")
            else:
                intro = f"{len(found)} finding(s), sent to the agent:"
            lines += [title, "", intro, ""]
            for i, f in enumerate(found, 1):
                where = f"{f.get('file', '?')}" + (f":{f['line']}" if f.get("line") else "")
                lines.append(f"{i}. `{where}`: {str(f.get('summary') or f.get('short_summary') or '').strip()}")
            if last.get("answer"):
                lines += ["", "What the agent did:", "", *[f"> {line}" for line in last["answer"].splitlines()]]
        elif last.get("reported") is False:
            lines += [title, "", "The review finished without a structured findings list. Read its last message "
                                 "in case it describes a problem:", "",
                      *[f"> {line}" for line in (last.get("message") or "(empty)").splitlines()]]
        else:
            lines += [title, "", f"No findings ({len(reviews)} review(s) in this task)."]
        lines.append("")

    pr_review = run_dir / "pr-review.json"
    if pr_review.exists():
        try:
            pr = json.loads(pr_review.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pr = {}
        lines += ["## External PR review", ""]
        if not pr.get("found"):
            lines.append(f"No review comment on the PR within {pr.get('wait_min', '?')} min"
                         + (f" (last error: {pr['error']})" if pr.get("error") else "") + ".")
        else:
            link = f" — [the comment]({pr['comment_url']})" if pr.get("comment_url") else ""
            lines.append(f"Verdict: **{pr.get('verdict') or 'unknown'}**{link}. Full text in `pr-review.md`.")
            if not pr.get("has_findings"):
                lines.append("No findings to apply.")
            elif pr.get("outcome"):
                lines += ["", f"Outcome: {pr['outcome']}. What the agent applied and why it left the rest "
                              "is in the Summary."]
        lines.append("")

    ci_file = run_dir / "ci.json"
    if ci_file.exists():
        try:
            rec = json.loads(ci_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rec = {}
        lines += [f"## PR CI ({rec.get('provider', '?')})", ""]
        for run in rec.get("runs") or []:
            link = f" — [run]({run['url']})" if run.get("url") else ""
            failed = f"; failed: {', '.join(run['failed_steps'])}" if run.get("failed_steps") else ""
            note = f" ({run['note']})" if run.get("note") else ""
            lines.append(f"- `{str(run.get('commit', ''))[:8]}` {run.get('state')}: {run.get('outcome')}"
                         f"{failed}{note}{link}")
        plan = rec.get("plan")
        if plan:
            lines += ["", f"### Plan check: {'as expected' if plan.get('expected') and not plan.get('unexpected') else 'NOT as expected'}",
                      "", plan.get("summary", ""), ""]
            if plan.get("changes"):
                lines += ["| Resource | Action | Why |", "|---|---|---|"]
                lines += [f"| `{c.get('address', '')}` | {c.get('action', '')} | {str(c.get('why', '')).replace('|', '/')} |"
                          for c in plan["changes"]]
                lines.append("")
            for key, title in (("unexpected", "Unexpected"), ("drift", "Drift (not from this change)")):
                if plan.get(key):
                    lines += [f"{title}:", "", *[f"- {v}" for v in plan[key]], ""]
            if plan.get("unplanned"):
                lines += ["Not planned by this CI (check each in its own plan before it is applied):", "",
                          *[f"- `{u.get('environment', '?')}`: {u.get('check', '')}" for u in plan["unplanned"]], ""]
            if plan.get("logs"):
                lines.append(f"Plan logs: `{plan['logs']}`; the plan text in `plan.txt`.")
        lines.append("")

    worktree = task.get("worktree")
    if task.type == "code" and worktree and Path(worktree).exists():
        try:
            from . import config
            base = config.load().repo(task.get("repo")).base
            commits = gitops.commits_ahead(Path(worktree), base)
        except Exception:
            commits = []
        if commits:
            lines += ["## Commits", "", *[f"- {c}" for c in commits], ""]

    if task.get("slack_channel"):
        where = task.get("slack_channel") + (f", thread {task.get('slack_thread')}" if task.get("slack_thread") else "")
        lines += ["## Slack reply", "", f"To {where}: {task.get('slack_reply') or 'not drafted yet'}.", ""]
        reply_file = run_dir / "slack-reply.md"
        if reply_file.exists():
            lines += [*[f"> {line}" for line in reply_file.read_text(encoding="utf-8").strip().splitlines()], ""]

    items = approvals.load(run_dir)
    if items:
        lines += ["## Approvals", ""]
        lines += [f"- {i['status']}: {approvals.describe(i)}" for i in items]
        lines.append("")

    for key, title in (("links", "Links"), ("blockers", "Blockers"), ("learnings", "Learnings")):
        values = result.get(key) or []
        if values:
            lines += [f"## {title}", "", *[f"- {v}" for v in values], ""]

    counts = _audit_counts(run_dir)
    if counts:
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        lines += ["## Audit", "", f"Tool calls — {summary}. Details in `audit.jsonl`.", ""]
        denials = _denials(run_dir)
        if denials:
            lines += ["Guard denials:", "", *denials, ""]

    path = run_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
