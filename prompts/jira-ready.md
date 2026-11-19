Move the Jira issue $key to the status "$status". Nothing else.

1. Find the Atlassian MCP tools with ToolSearch (getAccessibleAtlassianResources,
   getTransitionsForJiraIssue, transitionJiraIssue).
2. Get the cloudId, then the issue's transitions.
3. If the issue is already in "$status", do nothing. Otherwise pick the transition whose target
   status (`to.name`) is "$status" (ignore case) and apply it with transitionJiraIssue.
4. If no transition leads there, do not move it.

Do not comment, edit fields, assign or touch any other issue. Return `moved` (whether you applied
a transition), `status` (the issue's status at the end) and a one-line `note`.
