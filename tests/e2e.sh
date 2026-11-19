#!/bin/bash
# End-to-end smoke test with a real (cheap) agent, in a throwaway root/state and a
# throwaway git repo whose "origin" is a local bare repo. Costs a few cents.
#
#   tests/e2e.sh            run both scenarios
#   KEEP=1 tests/e2e.sh     keep the temp dir for inspection
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
H="$HERE/bin/sadyanx"
TMP="$(mktemp -d)"
export SADYANX_ROOT="$TMP/root" SADYANX_STATE="$TMP/state"
[[ -n "${KEEP:-}" ]] || trap 'rm -rf "$TMP"' EXIT
mkdir -p "$SADYANX_ROOT/config"

# a repo with a local bare origin
git init -q --bare "$TMP/origin.git"
git init -q -b main "$TMP/repo"
git -C "$TMP/repo" remote add origin "$TMP/origin.git"
echo "# demo" > "$TMP/repo/README.md"
git -C "$TMP/repo" add -A
git -C "$TMP/repo" -c user.email=t@t -c user.name=t commit -qm init
git -C "$TMP/repo" push -q origin main

cat > "$SADYANX_ROOT/config/sadyanx.toml" <<EOF
[defaults]
mode = "smart"
budget_usd = 1.0
timeout_min = 10
code_review = "low"
code_review_mode = "cheap"
[notify]
macos = false
[guard]
extra_gated = ["^echo deploy-now"]
# every mode on Haiku so the test stays cheap; the mode names are still exercised
[modes.ultra]
model = "haiku"
effort = "low"
subagents = true
[modes.smart]
model = "haiku"
effort = "low"
[modes.balanced]
model = "haiku"
effort = "low"
[modes.cheap]
model = "haiku"
effort = "low"
EOF
cat > "$SADYANX_ROOT/config/repos.toml" <<EOF
[repos.demo]
path = "$TMP/repo"
kind = "personal"
base = "main"
gates = ["test \"\$(cat hello.txt)\" = hola"]
publish = "push"
EOF

fail() { echo "E2E FAIL: $*"; exit 1; }
status_of() { sed -n 's/^status: //p' "$SADYANX_ROOT"/tasks/"$1"-*.md; }

echo "== A: simple task -> gates -> push"
"$H" add --title "Create hello.txt" --repo demo --body-file - <<'EOF'
## Context
Harness smoke test.

## Acceptance criteria
- [ ] `hello.txt` exists at the root with exactly the text `hola` (a trailing newline does not matter).
- [ ] The change is committed with the message `add hello`.
EOF
"$H" run 1 --fg --mode fast || true
[[ "$(status_of 0001)" == "done" ]] || { cat "$SADYANX_ROOT/runs/0001/events.log"; fail "A did not end done"; }
git -C "$TMP/origin.git" rev-parse --verify -q "refs/heads/agent/0001-create-hello-txt" >/dev/null \
  || fail "A: the branch did not reach origin"
grep -q '"verdict": "allow"' "$SADYANX_ROOT/runs/0001/audit.jsonl" || fail "A: empty audit log"
# the one-off --mode applies to the run, not the task
grep -q '"mode": "balanced"' "$SADYANX_ROOT/runs/0001/usage.jsonl" || fail "A: usage not recorded in balanced mode"
grep -q '^mode: smart' "$SADYANX_ROOT"/tasks/0001-*.md || fail "A: --mode changed the task's own mode"
# a code agent starts with the trimmed toolset and no MCP
python3 - "$SADYANX_ROOT/runs/0001/stream.jsonl" <<'PY' || fail "A: toolset not trimmed"
import json, sys
init = next(json.loads(l) for l in open(sys.argv[1]) if '"subtype":"init"' in l.replace(" ", ""))
tools = init.get("tools", [])
assert not any(t.startswith("mcp__") for t in tools), tools
assert "Workflow" not in tools and "Agent" not in tools and "Bash" in tools, tools
print("toolset:", ",".join(sorted(tools)))
PY
# /code-review ran on the branch once the gates passed, and reported a findings list
grep -q '"phase": "code-review"' "$SADYANX_ROOT/runs/0001/usage.jsonl" || fail "A: the code review did not run"
ls "$SADYANX_ROOT"/runs/0001/code-review-*.json >/dev/null 2>&1 \
  || { grep "code review" "$SADYANX_ROOT/runs/0001/events.log"; fail "A: the code review reported no findings list"; }
grep "code review" "$SADYANX_ROOT/runs/0001/events.log"
echo "A OK"

echo "== B: gated action -> needs-approval -> approve -> done"
"$H" add --title "Deploy demo" --repo demo --body-file - <<'EOF'
## Context
The "deploy" of this test repo is running the command `echo deploy-now` as is.

## Acceptance criteria
- [ ] `hello.txt` exists with the text `hola`, committed.
- [ ] The command `echo deploy-now` was run and printed `deploy-now`.
EOF
"$H" run 2 --fg || true
[[ "$(status_of 0002)" == "needs-approval" ]] || { cat "$SADYANX_ROOT/runs/0002/events.log"; fail "B did not end in needs-approval"; }
"$H" list
"$H" approve 2 --fg || true
[[ "$(status_of 0002)" == "done" ]] || { cat "$SADYANX_ROOT/runs/0002/events.log"; fail "B did not end done after approval"; }
grep -q '"verdict": "allow-approved"' "$SADYANX_ROOT/runs/0002/audit.jsonl" || fail "B: the approved action did not go through the guard"
echo "B OK"

echo "== usage"
"$H" usage | tee "$TMP/usage.txt"
grep -q "agent calls" "$TMP/usage.txt" || fail "usage report empty"

echo "== report of B"
cat "$SADYANX_ROOT/runs/0002/report.md"
"$H" list
echo "E2E OK"
