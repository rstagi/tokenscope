# Specific intents: steps and intent groups (design, agreed)

Today an "intent" is a category (INVESTIGATE / CHANGE / VERIFY / PLAN / DELEGATE / ...) from tool-call rules
(`tokenscope/intents.py`) plus a target file. This adds real, specific intents on two levels:

    ▾ [CHANGE] Implement multi-warehouse stock            4 steps · 120k CW · $0.41
        [INVESTIGATE] Read store and model code           Read ×4 · 1 thinking · 18k · $0.06
        [CHANGE] Add per-warehouse quantities to Item     Edit ×3 · 22k · $0.07
        [VERIFY] Run store tests                          Bash unittest ✓ · 9k · $0.03
    ▸ [VERIFY] Run full suite and fix CLI failures         3 steps · 60k CW · $0.20

## 1. Steps (fine-grained, deterministic segmentation, locked labels)

Input: the main thread's turns in order (a turn = one assistant API message; see `LiveState` in
`tokenscope/live/state.py`: `s.main.turn_steps` has group / intent / tools / cw / ts per turn), plus user prompts
(incl. mid-turn follow-ups = `queued_command` attachments), TodoWrite calls, Agent/Task launches, declare_intent calls.

A new step starts at turn t when any of:
1. category change: t's group differs from the open step's group;
2. the previous turn contained a test run (`understand.test_runner`) -> the step closes after it;
3. a user prompt (incl. queued follow-up) arrived before t;
4. t launches a subagent (Agent/Task): it starts its own DELEGATE step (subagent cost is added to that step);
   the agents a Workflow call launches add their cost to the step of the turn that made the call (see 5);
5. a TodoWrite in the previous turn moved an item to in_progress or completed;
6. t contains a declare_intent call (see 3).
Segmentation must be incremental-stable: appending turns never moves an earlier boundary.

A step is open until the next step starts (offline: all steps are closed at the end; live: the last step stays
open while the session is working). An open step may show a provisional label. When a step closes its label is
computed once and LOCKED (stored by step index; never renamed afterwards, even if better information arrives).

Step label sources, in priority order (record `source` on each):
(a) `todo`: the agent's own todo list: the TodoWrite item that is in_progress when the step starts
    (use `activeForm` if present, else `content`), only when that item spans a single step (see below).
    A PLAN step exists only with evidence: a turn whose tools are
    only TodoWrite (creating a list of >= 2 items) or ExitPlanMode / plan-mode text; label e.g. "Plan 5 tasks".
    Never invent a PLAN step.
(b) `subagent` / `prompt`: a DELEGATE step is labelled from its subagent task description(s)
    ("Audit store.py edge cases" or "3 audits: store.py, pricing.py, cli.py"); user prompts are boundaries.
(c) `declared`: the latest declare_intent label active at the step start (opt-in, see 3), only when that label
    spans a single step.
(d) `haiku`: fallback; a cheap model labels each closed step from the agent's text/reasoning before it
    (short, masked), files (basenames) and commands (program + subcommand). Batched, cached (see 4).
(e) `rule`: provisional / no-model fallback: "<intent label> · <top targets>", e.g. "editing files · store.py, cli.py".
A step whose only tool is ToolSearch (loading deferred tool schemas, e.g. the declare_intent tool) is OVERHEAD and
labelled deterministically from the ToolSearch `query` (source `rule`, locked when closed, never sent to the model):
"select:mcp__srv__declare_intent" -> "Load declare_intent tool", several names -> "Load N deferred tools", a keyword
search -> "Load deferred tools".
ToolSearch calls always form their own OVERHEAD step, even when the same turn makes other calls: such a turn is
split, in call order, into runs of ToolSearch calls and runs of the other calls, each run a part treated as its own
turn (usually two: "load the tool" then the work that follows; segmentation rules apply per part, a part right
after a ToolSearch part always starts a new step, a prompt boundary belongs to the first part). The turn's CW and $
are split by the same even per-call rule as the live `ops` list (section 6): each part gets (its calls / the turn's
calls) of it, declare_intent calls not counted. Thinking and text go to the first part that is not ToolSearch, the
test outcome and subagent / workflow cost to the last one. Parts depend only on the turn's calls in order, so a call
arriving later only appends a part (incremental-stable); a step records which part of its first / last turn it
holds (`part_first` / `part_last`, None for a whole turn), and the live `ops` list shows only those calls.
Labels: <= 7 words, imperative or noun phrase, no guessed project names, the category tag is kept separately.
The rule label's targets are file basenames, else programs: a program is always a name (`sleep 100` -> "sleep",
`timeout 100 zsh -c ...` -> "zsh"), never a bare number, flag or shell syntax; wrapper options / durations are
skipped.

Todo / declared labels belong to the group, not repeated on every step. A span = consecutive steps with the same
in_progress todo item (or the same declared label), split at user prompts. A span of >= 2 steps becomes a group
carrying the todo / declared label (section 2) and its steps get their own specific labels from the next sources
(subagent, else `haiku`, else `rule`). A single-step span keeps the todo / declared label on that step (no group).
The last step of a live session may still be joined by the next step, so a todo / declared label is not locked on
it until its span is known (the next step started, or the session ended); other locking rules are unchanged.

## 2. Intent groups (wrap steps, never rewrite them)

Consecutive steps get a parent intent label once the purpose is clear:
- all steps that start while the same todo item is in_progress -> one group, label = that item, source `todo`
  (only for spans of >= 2 steps; a single step keeps the label itself, see 1);
- all steps under the same declare_intent label -> one group, source `declared` (same >= 2 steps rule);
- otherwise the model proposes groups over closed, ungrouped consecutive steps (>= 2 steps each), source `haiku`;
- groups never cross a user-prompt boundary; a group only wraps whole steps; its label is locked when created;
  a todo/declared group may keep growing while its item is active.
Ungrouped steps stay top-level. Group tag = the category with most CW among its steps; a declared group with a
`kind` uses it instead (investigate / plan / change / verify -> that category; other -> the CW-dominant one).

## 3. declare_intent (opt-in experiment; live pane, new sessions only)

`tokenscope/live/intent_mcp.py`: a tiny stdio MCP server (JSON-RPC 2.0: initialize, notifications/initialized,
ping, tools/list, tools/call) exposing `declare_intent(label: string, kind?: "investigate"|"plan"|"change"|
"verify"|"other")`. It only records the call (JSONL at $TOKENSCOPE_INTENT_LOG, default
~/.cache/tokenscope/declared.jsonl) and answers "noted". The tool_use is in the transcript as
`mcp__<server>__declare_intent`; the live state reads it from there. `tokenscope live intents-install` prints the
`claude mcp add` command and a CLAUDE.md snippet ("Before starting each new sub-task, call declare_intent with a
short label") and writes nothing unless asked. Stats per session: calls, steps covered by a declared label.

## 4. Model labelling (fallback) budget

One `claude -p --model haiku` call per batch of <= 12 closed unlabelled steps (+ group proposal in the same call),
run in the neutral temp dir used by goal naming (`goalname._neutral_dir`), told not to guess project names, input
masked with `goalname.mask`. JSON answer. Cache by a hash of the batch input (~/.cache/tokenscope/step-labels.json).
Live: only with `--haiku`; trigger when >= 3 closed unlabelled steps or the session is waiting. Count calls and
tokens / cost from the CLI's JSON output.

## 5. Costs and summaries

Per step: CW, $, turns, duration, compact summary "Read ×6 · Grep ×2 · Skill pr-review · 1 thinking",
test outcome when it ran tests, files. Per group: sums. Per category: sums.

A step's CW and $ include the subagents its turns launched (Agent/Task, matched by the agent's meta `toolUseId`)
and the agents of the workflows its turns launched. Workflow agents live under
`<session>/subagents/workflows/wf_<id>/agent-*.jsonl`; run `wf_<id>` is matched to the main-thread Workflow call
whose tool result reported `runId: wf_<id>`; when there is no such result (not written yet, older transcripts) the
agent goes to the latest main-thread Workflow call at or before the agent's first record; agents of a workflow with
no earlier Workflow call stay unattributed. Such a step shows the call with its agent count, always among the
summary's tools: "Workflow ×1 (12 agents)". Main-thread turns without tool calls (a final text answer) belong to
no step, so the step total is a little below the session CW.

## 6. Live pane

Every intent line (a collapsed group or a top-level step) has one dim detail line under it: a group's step labels
("↳ Read store.py · Edit models.py ×2 · Run tests ✓", equal neighbours folded, cut with "+N"), a step's tools
summary. The live step (the open one, or the last one while the session works) carries `ops` in the payload: its
last 12 calls, `{"tool", "target", "cw" (the turn's CW split over its calls), "err" (null while running), "test"
({"ok","passed","failed"}, test runs only)}`, tool / target redaction-aware as in "now". The pane draws one row per
call ("▶ Edit store.py · 4k", "✓ unittest 22 passed · 3k") plus the in-process calls the transcript has not caught
up with. Short of rows: older expanded groups collapse, then the oldest intents lose their detail lines, then fold
into "… N earlier"; the live intent always stays.

## 7. Idle recap

While the session waits for the user (status waiting / idle) the compact payload carries `recap`
(`tokenscope/live/recap.py`, pure functions over the steps, groups and per-turn facts): for the work since the last
user prompt, the costliest intent (a group, or a step outside any group: label, category, CW, $, % of the request's
CW) and the slowest one (first to last timestamp of its steps), plus up to 3 hints ranked by the CW they put at
stake; `session` has the costliest / slowest over the whole session. A hint is `{rule, text, evidence, cw_at_stake,
estimate}`: `evidence` names concrete numbers / files from this session, `estimate` says how `cw_at_stake` was
estimated. Rules fire only past conservative thresholds (module constants) and return nothing otherwise:
reread (a file read in full >= 4×, >= 3× unchanged, across >= 3 intents), search_heavy (one intent >= 40% and
>= 100k CW of the request, >= 60% of >= 10 calls searches / re-reads), test_repeat (>= 4 failing runs in a row with
the same failure), toolsearch (>= 3 ToolSearch calls, >= 30k CW and >= 15% of the request), context (the request
took the context past 85% of the window, near autocompaction; never in headless / SDK sessions; estimate: cache
reads of the extra context over the session's median turns per request), shared_reads (>= 2 files, not images /
PDFs / binaries, each read in full by >= 3 and >= 20% of the request's subagents, duplicate reads >= 100k CW, sized by the read results themselves: chars/4 x (1.25 cache write + 0.1 per later turn of that subagent)), polling (>= 5 wake-ups in a row that only waited), outlier (an intent >= 8× the session's median intent
and >= 200k CW, >= 70% of its calls reads / searches). File names and labels go through the session redactor. The
pane draws a `── recap ──` section under the intent list (which yields its older rows first) and drops it as soon as
the agent works again. `tools/eval_hints.py` replays real transcripts and reports per rule how often each fires at
idle points.
