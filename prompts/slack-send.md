Send one Slack message, exactly as given. Nothing else.

1. Find the Slack send tool with ToolSearch (`slack_send_message`).
2. Call it once with exactly these arguments, unchanged: not a character added, removed or
   reformatted, and no other argument:

```json
$args
```

Do not search, read, react, schedule, draft or call any other Slack tool. If the call is
refused or fails, do not retry it in another form. Return `sent` (whether the message was
posted), `link` (the message link the tool returned, or "") and a one-line `note`.
