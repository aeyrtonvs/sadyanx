Harness task $id (research): **$title**

- Working directory (drafts, notes, scripts): `$workdir`
- Jira ticket: $jira (its content is already in the task below)

## The task

$body

## Deliverable

1. Research with whatever sources you need (local repos read-only, documentation, the web).
   Cite your sources.
2. Write the final report as a Claude Docs document: first load the guide with
   `mcp__claude_ai_Claude_Docs__guide` (items `["topic.instructions"]`) and follow its
   instructions to create it. The document is private to your user.
3. Also save a Markdown copy at `$workdir/report.md`.
4. Put the document URL in `links` and prove each acceptance criterion in `criteria`.

Work autonomously until you are done, then return the structured result.
