# tokenscope-live (Claude Code mod)

A Claude Code mod that adds a live **tokenscope** pane to the session: what you asked, and
what the agent is doing right now. It uses only `Box`/`Text`, so it works in any terminal, and
every row is cut to the pane width with `…` (never wrapped):

```
tokenscope  ● working  3m12s
ASKED  Fix: failing tests in invoice
       ↳ now: also check tax rounding
NOW    ▶ running tests · run_tests.sh · 4s
TESTS  ✗4 → ✗2 → ✓ 12 passed
FILES  edited 3: invoice.py, money.py, tax.py
COST   ⣀⣤⣶⣿ 182k CW · $0.41 · ctx ▕███░░░░░░░▏ 34%
── intents ──
▸1 [CHANGE] Implement multi-warehouse stock · 3 steps · 120k · $0.41
  [VERIFY] Run store tests · Bash unittest ✓ · 9k · $0.03
▾2 [CHANGE] Wire warehouses into CLI ✎ · 2 steps · 20k · $0.06
  ├ [INVESTIGATE] Inspect CLI entry point ✎ · Read ×2 · 10k · $0.03
  └ ▶ [CHANGE] editing files · cli.py… · Edit ×2 · 10k · $0.03
⚠ same error ×3, no edits since — try a different approach
✎ declared ×2
```

- **header**: `tokenscope`, a status pill (`● working` green, `● waiting for you` yellow,
  `● idle` dim) and the elapsed time
- **ASKED**: the goal from your prompt in bold (a dim `(haiku)` when a model named it); with
  more than one prompt, `↳ now:` shows the latest
- **NOW**: the current intent in plain words, its target (or tool) and how long this phase has
  run; `◼ done, waiting for you` when Claude is waiting, `◼ idle` when idle
- **TESTS**: failures per test run (`✗4 → ✗2`) then the last result; omitted before any test run
- **FILES**: the files edited so far (or `edited N files` when the server redacts names)
- **COST**: a cyan braille sparkline of cumulative CW per turn, CW, cost, and a context gauge
  against 200k tokens (yellow above 50%, red above 75%)
- **intents** (when the server sends `intents`, see `docs/intents-spec.md`): steps and intent
  groups, newest at the bottom. A group is one line, `▸N [CAT] <label> · N steps · CW · $`;
  the group holding the open step is expanded (`▾N`) with its steps under it (`├` / `└`).
  Steps outside a group are one line each: `[CAT] <label> · <summary> · CW · $`, the summary
  cut to fit. The open step is marked `▶`; while its label is still provisional (not locked)
  it is dim with a trailing `…`. The tag takes the category colour (below); a dim `✎` marks a
  label from `declare_intent`, and `✎ declared ×N` closes the list when it was used. When
  rows run short, older expanded groups collapse, then the oldest items fold into
  `… N earlier`; the live item never folds.
  Expand / collapse: `/tokenscope expand <n|all>`, `/tokenscope collapse <n|all>` (`n` is the
  number after `▸` / `▾`), or press a group line (Enter on it once the pane holds the focus,
  or a click). The choice holds across redraws, and a group you collapsed stays collapsed
  while live. `/tokenscope view phases` switches to the phase tree below, `view intents` back
- **flow** (an older server without `intents`, or `view phases`): the goal as root, then phases: runs of consecutive turns with the same intent
  group, named `GROUP · <dominant intent>` (most turns, ties to the latest). Under a phase,
  consecutive calls of one tool fold into one line with up to 3 targets (`+N` more) and `×N`;
  failed calls show as a dim red `✗N`. The last phase is the live one (`▶`, brighter, always
  expanded). When rows run short, older phases collapse to one line
  (`├ INVESTIGATE · reading code · 3 turns · Read ×4 Grep ×2`), then the oldest drop behind
  `… N earlier phases`. The pane stays within 38 rows.
  Phase colours: investigate blue, change red, verify yellow, ship magenta, delegate bright
  yellow, research cyan, wait gray, plan bright magenta, other gray
- **subagents** launched by a phase are sub-branches under it (`├─↳ Explore · 6 turns · done`).
  Agents in `agents` that no turn claims are listed at the bottom
- **recap** (only while the session waits for you or is idle, from the server's `recap`):
  `── recap ──`, then `costliest [CAT] <label> · CW · $ · N%` and `slowest [CAT] <label> · 4m12s`
  for the work since your last prompt, and up to 3 cyan hints `💡 <what to do> · <evidence>`
  (only when the server found evidence; often none). It sits under the intent list, which
  folds its older rows first to make room, and disappears as soon as the agent works again
- **warnings** are rare and come only from the server's `warnings`: one yellow line each,
  `⚠ <text> — <hint>`. A warning rule that appears after the first payload also raises one
  toast. A session without warnings shows no `⚠` anywhere

Everything but the header, NOW and COST comes from a running `tokenscope live` server. Without
one, the pane runs in **local mode**: the header, the last tool as NOW, and COST (sparkline and gauge) drawn from
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
`/tokenscope expand|collapse <n|all>` and `/tokenscope view intents|phases` change the intent list.

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `TOKENSCOPE_LIVE_URL` | `http://127.0.0.1:8765` | Base URL of the `tokenscope live` server |
| `TOKENSCOPE_LIVE_PANE` | unset | `1` opens the pane when the session starts |
| `TOKENSCOPE_LIVE_BAND` | unset | `1` shows a one-line summary above the prompt: `◆ <goal> · ▶ <now> · <cw> CW · $x`, plus `⚠` only when there are warnings |

## Network

Once a second the mod requests `GET {TOKENSCOPE_LIVE_URL}/api/session/{session id}/compact`,
which by default is localhost only. Nothing is sent anywhere else. A failed request leaves the
pane in local mode. The fields it reads (all optional) are:

```json
{
  "goal": "Fix: failing tests in invoice", "status": "working", "cw": 182000, "usd": 0.41,
  "context_tokens": 68000, "elapsed_s": 192, "turns": 9, "tool_calls": 14, "errors": 1,
  "asked": { "goal": "Fix: failing tests in invoice", "source": "keyword", "prompt_n": 2,
             "latest": "also check tax rounding" },
  "now": { "intent": "run_tests", "label": "running tests", "group": "verify",
           "tool": "Bash run_tests.sh", "target": "run_tests.sh", "since_s": 4 },
  "tests": { "passed": 12, "failed": 0, "ok": true, "runs": 3, "trend": [4, 2, 0] },
  "files_edited": ["invoice.py", "money.py", "tax.py"], "files_edited_n": 3,
  "warnings": [{ "rule": "stuck_error", "text": "same error ×3, no edits since",
                 "hint": "try a different approach", "count": 3, "ts": 1760000000 }],
  "recent": [{ "intent": "read_code", "group": "investigate", "label": "reading code", "cw": 12000,
               "tools": ["Read", "Grep"], "targets": ["money.py", "round"],
               "tool_err": [false, false], "err": false,
               "agents": [{ "type": "Explore", "status": "running", "turns": 5, "cw": 40000 }] }],
  "cw_series": [12000, 9000],
  "agents": [{ "type": "Explore", "status": "running", "turns": 5 }]
}
```

`recent` is oldest first (up to 60 turns) and `cw_series` holds one CW value per main turn
(without it, the sparkline uses `recent[].cw`). `tests` is null before the first test run and
`tests.trend` holds the failures of the last runs (null when unknown). With redaction on, the
server sends only `files_edited_n`. The old `flags` list is ignored. The older shape still reads:
`goal` as `{ "name", "source" }`, `cost_usd`, `ctx_tokens`, `turns` as a list of
`{ intent, tools, error }`, and `subagents`.

This is the server's `/compact` payload (see `docs/live-spec.md` in the repository root).

`group` is one of the intent groups above. If it's missing, the mod guesses the group from the intent name.

## Develop

```sh
claude plugin validate mod/tokenscope-live
cd mod/tokenscope-live && claude plugin test
```

Tested with Claude Code **2.1.296**. License: MIT.
