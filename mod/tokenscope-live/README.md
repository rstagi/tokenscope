# tokenscope-live (Claude Code mod)

A Claude Code mod that adds a live **tokenscope** pane to the session:

- the session goal and status, cost-weighted tokens (CW), cost and context size
  (`CW 1.2M · $3.10 · ctx 84k`)
- an **intent strip**: one coloured `█` per recent turn (investigate blue, change red,
  verify yellow, ship magenta, delegate gold, research cyan, wait gray, plan bright
  magenta, other gray) followed by the current intent
- the last 8 turns (`verify  · Bash pytest ✗`), flags in yellow (`⚠ re-read file ×3 …`)
  and running subagents (`↳ Explore running · 5 turns`)

The intents, flags and subagents come from a running `tokenscope live` server. Without
one, the pane runs in **local mode**: it counts tool calls per tool, the last 12 tool
names, turns, and cost-weighted tokens from each model response's usage:

    CW = input + 1.25 × cache_write + 0.1 × cache_read + 5 × output

The mod only observes: every hook passes the event on unchanged.

## Load it

```sh
claude --plugin-dir mod/tokenscope-live
```

Then type `/tokenscope` to toggle the pane (it works while Claude is busy, too).

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `TOKENSCOPE_LIVE_URL` | `http://127.0.0.1:8765` | Base URL of the `tokenscope live` server |
| `TOKENSCOPE_LIVE_PANE` | unset | `1` opens the pane when the session starts |
| `TOKENSCOPE_LIVE_BAND` | unset | `1` shows a one-line summary above the prompt |

## Network

Once a second the mod requests `GET {TOKENSCOPE_LIVE_URL}/api/session/{session id}/compact`,
which by default is localhost only. Nothing is sent anywhere else. A failed request leaves the
pane in local mode. The fields it reads (all optional) are:

```json
{
  "goal": { "name": "Fix login bug", "source": "keyword" }, "status": "working",
  "cw": 1234000, "usd": 3.1, "context_tokens": 84000, "current_intent": "test",
  "recent": [{ "intent": "test", "group": "verify", "tools": ["Bash pytest"], "err": true }],
  "flags": [{ "flag": "reread_file", "count": 1, "detail": "auth.py ×3" }],
  "agents": [{ "type": "Explore", "status": "running", "turns": 5 }]
}
```

This is the server's `/compact` payload (see `docs/live-spec.md` in the repository root).

`group` is one of the intent groups above. If it's missing, the mod guesses the group from the intent name.

## Develop

```sh
claude plugin validate mod/tokenscope-live
cd mod/tokenscope-live && claude plugin test
```

Tested with Claude Code **2.1.296**. License: MIT.
