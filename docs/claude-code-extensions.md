# Claude Code extension points used by tokenscope live

Checked against Claude Code **2.1.296** and its bundled docs (plugins/mods overview, reference, interface, events,
API, hooks, statusline). Anything not listed here was not verified.

| Surface | What it gives tokenscope | Limits that matter |
| :- | :- | :- |
| **Transcripts** (`~/.claude/projects/<project>/<session>.jsonl`, subagents in `<session>/subagents/agent-*.jsonl` + `.meta.json`) | Everything the live view shows: prompts, assistant messages with usage, thinking/text/tool_use blocks, tool results, compactions | Written as the session runs; several lines can share one `message.id` (usage counted once). Thinking text is often empty (signature only), so reasoning steps show presence/length, not content. Format is not a documented stable API. |
| **Settings hooks** (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Notification`, `SubagentStart`/`Stop`, `Stop`, `SessionStart`/`End`) | Exact status: permission/idle prompts, long-running tools, session end | Run a command per event (we use `async: true`, exit 0 always). Configured in settings.json or per session with `claude --settings FILE`. |
| **Status line** (`statusLine` command in settings) | One line under the prompt, refreshed after each turn, with a JSON payload on stdin (`session_id`, model, cost, `context_window`) | Text only, one script per user, not interactive. |
| **Mods** (plugins whose `hooks/hooks.json` lists ES `modules`; v2.1.287+ in the terminal) | In-process hooks on events (`tool.call`, `turn.step` with usage, `turn.complete`, `session.start`, subagent events, `ui.render` …); can draw a **Pane** beside the transcript or a band **AbovePrompt**, register `/commands` that run without a Claude turn, keep state, `$.http.fetch`, `$.fs`, `$.process`, `$.clock.every` | Drawing appears only in the terminal and the Desktop app's Code tab — not in `claude -p`, the Agent SDK, the VS Code chat panel or cloud sessions (hooks still run there). Terminal elements: `Box`, `Text`, `Button`, `Link`, `Code`, `Markdown`, `Input`, `Select`, `Raster`, `Image`; `Svg` is Desktop-only, so the terminal pane uses coloured text blocks. Redraws throttled (10/s; 30/s for the visible pane in the terminal). A hook gets 10 s of its own time per event; `$.http.fetch` 30 s and 4 MiB; `$.store` 4 MiB. Mods are not sandboxed and can rewrite prompts/tool calls/approve tools — `claude plugin validate` lists what a mod reads, writes and fetches before you install it. A mod cannot change the permission prompt. |

## What tokenscope uses

- **Always:** transcript tailing (works for every session on the machine, interactive or `claude -p`, with zero setup).
- **Optional hooks:** precise working / waiting / idle status. The hook writes event names, session id and tool
  name only.
- **Optional mod** (`mod/tokenscope-live`): `/tokenscope` pane inside the terminal. It reads the local server's
  `/api/session/<id>/compact` once a second via `$.http.fetch` (localhost only) and falls back to its own counters
  from `tool.call` / `turn.step` / `turn.complete`. It never changes events (always `next(e)`), never approves
  tools, never calls a model.
- **Optional status line:** `python3 -m tokenscope.live.statusline`.

## What we did not do

- A richer in-terminal flow diagram: possible with `Raster` (coloured cells) but not with SVG in the terminal; the
  pane shows the intent list instead, and the browser view shows the same intents as expandable cards.
- Session-to-session messaging, prompt rewriting, tool approval: the mod API allows these; tokenscope does not use
  them on purpose (read-only observer).
