# tokenscope-live (Claude Code mod)

A Claude Code mod that adds a live **tokenscope** pane to the session. It uses
only `Box`/`Text`, so it works in any terminal, and every row is cut to the pane width:

```
tokenscope                                             ● working  2:31
CW 412k · $1.23 · ctx 84k · 23 turns · 3 ✗  ⣀⣤⣶⣿
ctx ▕██████████░░░░░░░░░░░░░░▏ 42%
◆ Fix invoice rounding
├─ INVESTIGATE · 2 turns · 120k
│  └ Read ×3 · Grep
├─ DELEGATE · 1 turn · 11k
│  ├ Agent ×2
│  ├─↳ Explore         ● running · 5 turns · 40k
│  └─↳ general-purpose ✓ done · 3 turns · 22k
├─ CHANGE · 1 turn · 18k
│  └ Edit
├─ VERIFY · 1 turn · 15k ✗
│  └ Bash run_tests.sh ✗1
├─ CHANGE · 1 turn · 6k
│  └ Edit
└─ ↺ VERIFY · 1 turn · 15k ✗ retry 1  ◀ now
   └ Bash run_tests.sh ×3 ✗3 ↺
 ⚠ dead tool · Bash run_tests.sh ×3
```

- **header**: `tokenscope`, a status pill (`● working` green, `◐ waiting` yellow, `○ idle` gray)
  and the elapsed time
- **metrics**: cost-weighted tokens (CW), cost, context, turns and errors, then a cyan braille
  sparkline of cumulative CW per turn (two turns per character, up to 30 characters)
- **context gauge** against 200k tokens: yellow above 50%, red above 75%
- **flow tree**: the goal in bold as its root, then consecutive turns of the same intent group
  folded into phases, each with its turns and CW. Tools show as `Name ×n` (failed ones as a red
  `✗n`) on one `└` line when they fit, otherwise one `├`/`└` line per tool (up to 3, then
  `+N more`). The current phase is marked `◀ now`.
  Phase colours: investigate blue, change red, verify yellow, ship magenta, delegate bright
  yellow, research cyan, wait gray, plan bright magenta, other gray
- **subagents** launched by a phase show as `↳` sub-branches under its tools: green `● running`,
  dim `✓ done` and `○ starting`, red `✗ error`. Agents in `agents` that no turn claims are listed
  at the bottom (`↳ Explore running · 5 turns`)
- **retries**: a phase whose group failed in one of the 3 phases before it (`VERIFY ✗ → CHANGE →
  VERIFY`) is marked in yellow as `↺ VERIFY … retry N`, with N counting that group's retries so far.
  A tool that fails again after failing in an earlier phase gets a yellow `↺`
- **flag badges** (`⚠ dead tool · …`); a flag that appears after the first payload also raises
  a toast once
- the pane stays within 38 rows: the last 8 phases at most, fewer when they don't fit, with
  `┊ +N earlier phases` above them

The flow, flags and subagents come from a running `tokenscope live` server. Without
one, the pane runs in **local mode**: the same header, metrics, sparkline and gauge, drawn from
per-turn cost-weighted tokens of each model response's usage, plus tool-call counts per tool and
the last 12 tool names:

    CW = input + 1.25 × cache_write + 0.1 × cache_read + 5 × output

The mod only observes: every hook passes the event on unchanged.

### Redraws

The mod polls the server once a second but redraws only when the payload's JSON changed. Polls
and tool/turn events only mark the pane dirty; a 250 ms timer flushes that into one
`$.ui.invalidate`, so a burst of tool calls costs at most ~4 redraws a second.

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
  "goal": "Fix invoice rounding", "status": "working", "cw": 412000, "usd": 1.23,
  "context_tokens": 84000, "elapsed_s": 151, "turns": 23, "tool_calls": 31, "errors": 4,
  "current_intent": "test",
  "flags": [{ "flag": "dead_tool", "count": 1, "detail": "Bash run_tests.sh ×3" }],
  "recent": [{ "intent": "read_code", "group": "investigate", "cw": 12000,
               "tools": ["Read", "Grep"], "tool_err": [false, false], "err": false,
               "agents": [{ "type": "Explore", "status": "running", "turns": 5, "cw": 40000 }] }],
  "cw_series": [12000, 9000],
  "agents": [{ "type": "Explore", "status": "running", "turns": 5 }]
}
```

`recent` is oldest first (up to 60 turns) and `cw_series` holds one CW value per main turn
(without it, the sparkline uses `recent[].cw`). The older shape still reads: `goal` as
`{ "name" }`, `cost_usd`, `ctx_tokens`, `turns` as a list of `{ intent, tools, error }`, and
`subagents`.

This is the server's `/compact` payload (see `docs/live-spec.md` in the repository root).

`group` is one of the intent groups above. If it's missing, the mod guesses the group from the intent name.

## Develop

```sh
claude plugin validate mod/tokenscope-live
cd mod/tokenscope-live && claude plugin test
```

Tested with Claude Code **2.1.296**. License: MIT.
