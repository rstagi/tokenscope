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
  ├ +2 other steps
  └ [CHANGE] Add per-warehouse quantities · Edit ×3 · 40k · $0.14
  [VERIFY] Run store tests · Bash unittest ✓ · 9k · $0.03
▸2 [CHANGE] Wire warehouses into CLI ✎ · 2 steps · 20k · $0.06
  ├ +1 other step
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
- **intents** (when the server sends `intents`, see `docs/intents-spec.md`): the session as
  intents, newest at the bottom, never loose steps (README "Intents in the flows"). An intent
  is its head line `▸N [CAT] <label> · N steps · CW · $`, then a dim `├ +N other steps` and its
  latest step (`└ [CAT] <label> · <summary> · CW · $`, the summary cut to fit); the live step
  has its calls under it, one row each. Steps outside any group whose label is not settled yet
  (the open step, or steps the model has not labelled yet) are under a dim `pending intent…`
  head; a settled step outside every group is its own one-step intent, one line
  `[CAT] <label> · <summary> · CW · $` with its tools summary on a dim `↳` line. The open step is
  marked `▶`; while its label is still provisional (not locked) it is dim with a trailing `…`.
  The tag takes the category colour (below); a dim `✎` marks a label from `declare_intent`,
  and `✎ declared ×N` closes the list when it was used. When rows run short, older expanded
  intents go back to head / `+N other steps` / latest step, then the oldest one-step intents
  lose their `↳` lines, then the oldest items fold into `… N earlier`; the live item never folds.
  Expand (every step of an intent under it, `├` / `└`) / collapse: `/tokenscope expand <n|all>`,
  `/tokenscope collapse <n|all>` (`n` is the number after `▸` / `▾`), or press an intent line
  (Enter on it once the pane holds the focus, or a click). The choice holds across redraws. `/tokenscope view phases` switches to the phase tree below, `view intents` back
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
- **times**: every operation, step and group line ends with its wall time after tokens / $
  (`✓ unittest 22 passed · 3k · 4s`, `[CHANGE] Add X · 63k · $0.22 · 1m12s`, `1h3m`); the live
  step, its group and a running call tick. A narrow pane cuts the label, never the numbers
- **skills** (magenta `✦`): Skill-tool calls are `✦ skill:pr-review · 2k · 3s` rows; a step or
  group a skill drove has ` ✦ pr-review` after its label; `SKILLS ✦ pr-review ×2 · tdd` lists
  the session's skills (Skill tool and slash-command skills)
- **recap** (only while the session waits for you or is idle, from the server's `recap`): a box
  right under the header, `━━ DONE · recap · 120k · $0.40 ━━━…` in bold bright green across the
  pane, then ` 58%  costliest [CAT] <label> · CW · $` and ` 4m12s  slowest [CAT] <label>` for the
  work since your last prompt, then up to 3 hints: `➜ <action>` bold, the evidence dim under it
  (numbers first: `≈34k/run saving · done 4× in 4 sessions · ≈95k CW each · …`, wrapped onto up to
  3 lines, only the tail cut), and `✦ <skill> · stub: <path>` when the hint names them (only when the server
  found evidence; often none), then a closing rule. The intent list gives up rows first to make
  room; the box is removed entirely as soon as the agent works again
- **alerts** (from the server's `alerts`: the session's alert history, see the repository README
  "Alerts") are a section right under the header, above the recap: `▾ ALERTS · 2 new · 3 total ━━━`
  in the worst new alert's colour (bright red critical, bright yellow warning; green
  `✓ all acknowledged` once none is new), then one row per alert, at most one per kind (cost,
  off-task, looping), new ones first (worst, then newest), acknowledged ones dim with `✓`:
  `#2 ▲ retry loop ×3  10:25 · step 7 · same failing command retried 3× …` (`▲▲` critical,
  `×3` when its kind fired 3 times, `+2` when its latest trigger had more reasons, the time of
  that trigger's step, the step or step range, `subagent step` for a subagent's), then the
  commands. A new trigger of a kind updates its row in place. `#n` is the alert's number in the
  session, stable as new ones arrive. An alert stays until it is acknowledged (it then stays
  dim) or dismissed (it leaves); it comes back as new only when its severity rises. Press a new
  alert's row to acknowledge it, or `/tokenscope ack <#|all>`; `/tokenscope dismiss <#|all>`
  removes it (both go to the server, so the web views agree). `/tokenscope alerts` prints the
  whole history (with every reason and the earlier triggers) as the command's output. Collapse
  the section to its head line (`▸ ALERTS · 2 new · cost · loop · 3 total`) by pressing it or
  with `/tokenscope alerts hide` (`show` brings it back); the choice is kept in the mod's store,
  across sessions. A new alert raises one toast, and so does one that escalated
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
`/tokenscope expand|collapse <n|all>` and `/tokenscope view intents|phases` change the intent list;
`/tokenscope alerts [show|hide]`, `/tokenscope ack <#|all>` and `/tokenscope dismiss <#|all>` the alerts.

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `TOKENSCOPE_LIVE_URL` | `http://127.0.0.1:8765` | Base URL of the `tokenscope live` server |
| `TOKENSCOPE_LIVE_PANE` | unset | `1` opens the pane when the session starts |
| `TOKENSCOPE_LIVE_BAND` | unset | `1` shows a one-line summary above the prompt: `◆ <goal> · ▶ <now> · <cw> CW · $x`, plus `⚠` only when there are warnings |

## Network

Once a second the mod requests `GET {TOKENSCOPE_LIVE_URL}/api/session/{session id}/compact`,
which by default is localhost only; acknowledging or dismissing an alert sends
`POST /api/alerts/ack|dismiss` (`{"ids": [...]}` or `{"all": true, "sessions": [<session id>]}`)
to the same server. Nothing is sent anywhere else. A failed request leaves the
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
