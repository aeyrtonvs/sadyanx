# sadyanx — notes for working on this repo

- Everything in this repo is in English: code, comments, agent prompts, the /task command, CLI
  output, reports, config comments and docs.
- Python 3.11+ standard library only. Do not add dependencies.
- `sadyanxlib/guard.py` is the security piece: any change to its rules comes with cases in
  `tests/test_guard.py` (allowed, forbidden, gated and false positives). It must fail closed.
- Run `python3 -m unittest discover -s tests` before finishing; run `tests/e2e.sh` when you
  touch the runner, approvals or prompts (it uses real Haiku agents and costs cents).
- Run `python3 tests/sandbox_probe.py` when you touch `sadyanxlib/sandbox.py` or update
  Claude Code: it proves the sandbox still holds (one Haiku agent, about a cent).
- Design decisions live in `docs/DESIGN.md`; if you change one, update it there.
- The repo is public. `tasks/`, `runs/`, `config/sadyanx.toml` and `config/repos.toml` are local
  and ignored; never commit them, and keep employer names, tickets, hosts and account details
  out of everything tracked (use generic examples such as PROJ-1234 or work-api).
