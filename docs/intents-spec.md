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
3. a user prompt (incl. queued follow-up) or a slash-command skill the user typed (see 8) arrived before t;
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

Per step: CW, $, turns, duration, compact summary "✦ pr-review · Read ×6 · Grep ×2 · 1 thinking" (the step's
skills first, see 8), test outcome when it ran tests, files, `skills`. Per group: sums, `skills`. Per category: sums.

Wall time (`dur_s`, seconds): a step runs from its first call to its last event, i.e. the latest of its turns'
timestamps and their calls' tool results (`ts_end` of a segment turn). A step that only loads a skill ("Use skill
tdd": its Skill call returns at once) instead runs from the skill load to the end of the work the skill drove: the
following steps up to the next user prompt or the next step that loads a skill (`steps.skill_spans`). A group runs
from its first step's start to the latest end of its steps (a skill step's driven end included, so a group is never
shorter than a step in it). Live, the live step, the group holding it and a skill step driving it tick up to now
while the session works. An operation
runs from its tool_use to its tool_result; a running one (no result yet, session working) shows the time since it
started. The pane formats them compactly: 4s, 1m12s, 1h3m.

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
last 12 calls, `{"tool", "target", "cw" (the turn's CW split over its calls), "err" (null while running), "dur_s",
"test" ({"ok","passed","failed"}, test runs only), "skill" (Skill calls only)}`, tool / target redaction-aware as in
"now". Steps and groups carry `dur_s` and `skills`. The pane draws one row per call ("▶ Edit store.py · 4k · 2s",
"✓ unittest 22 passed · 3k · 4s", "✦ skill:pr-review · 2k · 3s") plus the in-process calls the transcript has not
caught up with (their elapsed time from the mod's own clock); step / group lines end with "· CW · $ · 1m12s". The
label is cut to fit, never the numbers. Short of rows: older expanded groups collapse, then the oldest intents lose their detail lines, then fold
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
pane draws it as a boxed section right under the header, above the intent list (which yields its rows first): a bright
full-width title bar `━━ DONE · recap · 120k · $0.40 ━━━`, the costliest / slowest lines with the share / time first,
then each hint as `➜ <action>` with its evidence dim on the line under it (and `✦ <skill> · stub: <path>` when the
hint names them), then a closing rule. It is removed entirely as soon as the agent works again (a new prompt, or a
call seen in-process). A hint also carries `action` (= `text`) and `kind` ("session", or "cross_session", section 9); the pane draws any kind (a
cross-session one shows `cross-session:` before its evidence) with optional `skill` and `stub_path`. `tools/eval_hints.py` replays real transcripts and reports per rule how often each fires at
idle points.

## 8. Skills

Two kinds of invocation (`tokenscope/skills.py`, `SkillScanner` over a thread's records in order):
- the Skill tool: a tool_use named `Skill` (input `skill`, else `name` / `command`). The skill's body then arrives as
  a meta user message "Base directory for this skill: ..." with `sourceToolUseID`; it is not counted again.
- a slash-command skill: a user message with `<command-name>/foo</command-name>` that is a skill, not a built-in
  (/model, /compact ...): it says `<skill-format>true</skill-format>`, or the next user message is a meta one starting
  with "Base directory for this skill:" and no `sourceToolUseID` (name = the command, else the directory's name).
A Skill call tags its turn's step; a slash-command skill tags the step of the next turn and, when typed (not a meta
message), is a step boundary like a prompt (rule 3) and counts as activity for the status. Steps and groups carry
`skills: [name]`; summaries lead with "✦ <name>"; the live compact payload has `skills: [{"name", "count"}]` (all
threads, most used first) and the offline output (`tools/intents_offline.py`) the same per session.
`tools/skills_stats.py [--root DIR]` counts invocations per skill (both kinds, main and subagent transcripts).

## 9. Cross-session suggestions

`tokenscope/xindex.py`; recap hints that know about your other sessions. Deterministic, no model calls.

Index. Every main transcript under the index root (`--index-root` on `tokenscope live` / the tools, else
`$TOKENSCOPE_INDEX_ROOT`, else ~/.claude/projects) is replayed through the live state, segmented (section 1) and
labelled without a model (rule / todo / subagent / declared labels), and cut into requests (a user prompt up to the
next one). Each request, and each group of >= 2 steps in it, becomes a record with a workflow signature: label
words (lowercase, light stemming; stopwords, the rule labels' generic words, identifiers, file names and numbers
dropped; a rule label contributes no words, its targets are compared as files / tools; an OVERHEAD label none),
the category sequence (repeats folded), the tool multiset (display names: Read, Edit, "Bash unittest", "Bash git
status"), file types, file basenames (only names that look like files: an extension, a dotfile or a known bare name
such as Makefile; directories, devices such as /dev/null and `*.output` task captures dropped), skills used,
CW, $, wall time and CW per category, its main-thread turns, its discovery (below: `disc`, per kind `disc_by`, the
files its reads found `disc_files`, relative to the project), plus its steps (category, label, files, commands, test
runner, CW). Records
are cached per transcript in `<cache dir>/xindex/<hash of root>.json` keyed by path + size + mtime of the transcript
(+ count and size of its subagent transcripts); a rebuild replays only new or changed transcripts (a few hundred
sessions load from the cache in well under a second), and label words are recomputed on load. A request's words
come mainly from the user prompt that started it (fenced code, `backticked` identifiers, paths, file names and
numbers stripped; stored as cleaned words, never shown) plus its steps' own labels; a skill-only step ("Use skill
tdd") gives no words, no category and never names the request.

Matching. The finished request (and its groups) against records of the same level from OTHER sessions (never the
current one): similarity = 0.4 × label-word Jaccard + 0.3 × category-sequence similarity (difflib ratio) + 0.3 ×
tool / file overlap (0.4 tool-multiset weighted Jaccard + 0.1 file-type Jaccard + 0.5 file overlap); when neither
side has label words the score is the other two parts renormalised. Two requests with prompt words on both sides
use the prompt words' Dice coefficient and 0.5 / 0.15 / 0.35 weights (the prompt says what the task is, the step
shape only how it was done: a /tdd run cycles test / edit where a plain run edits once), anchor Dice >= 0.5; each
prompt word weighs by its idf over the indexed requests with prompts (0 in >= 20% of them: boilerplate an app injects
into every prompt; 1 at <= 4%; log-linear between; all 1 below 30 such requests). File overlap is idf-weighted (a file most
requests touch, README.md, weighs little) and divided by the smaller set. A match needs score >= 0.55 and an anchor:
label-word Jaccard >= 0.34, or >= 2 shared files with overlap >= 0.5. A hint needs >= 3 prior matches in >= 2
distinct sessions, each >= 50k CW, and the current request / group >= 50k CW.

Saving (an estimate, labelled so): net per run = median(removable discovery) − median(context cost of the remedy)
over the matched prior runs. Never the whole run's cost, and never discovery the remedy cannot spare.
- Discovery of a run (indexed per request and group): the CW of its Glob / Grep / LS calls and Bash listings (ls,
  find, tree, fd, grep, rg ...), its ToolSearch calls, its exploration subagents (Explore type, or one whose calls
  only locate / read; their own CW plus the launching call's share) and its full reads (Read without offset / limit,
  `cat <file>`) of files the run does not edit (a file the agent edits must be read anyway; a re-read after the edit
  checks it). A call's CW is its turn's CW split evenly over the turn's calls (declare_intent not counted), as steps
  attribute it (section 1). Background task captures (`*.output`) are polling, not discovery. Each call is kept in
  order (`disc_items`: kind, CW, step, the read's file or the file names a search's output showed).
- Removable discovery of a run for a remedy (shown as "discovery/run"): only what the remedy's content would have
  short-circuited: full reads of the files it lists (a file map's files; a drafted skill's "## Files"; the files an
  existing skill's SKILL.md names) and the searches / listings that led to them (their output names one of them, or
  they come before the first read of one in the same step). Searches that never led to a listed file, ToolSearch and
  subagents stay out: a 1-file map of report.md spares that read, not the run's 40k of greps.
- Context cost of putting T tokens (characters / 4) in context for a run of n main-thread turns: T × (1.25 + 0.1 ×
  (n − 1)): one cache write, then a cache read on each later turn (the cost-weighted scale, tokenscope/pricing.py). A
  skill is paid on every turn after it loads (≈ the run's turns), not once.
- Gates: a token-saving hint needs net >= 20k CW and net >= 30% of the median removable discovery, and a substantive
  remedy: a file map (CLAUDE.md or in a skill) of >= 3 stably found files; a drafted skill with >= 2 shared steps and
  >= 3 files.

Remedies, at most one hint per recap (the candidate with the best passing net; ranked with the session hints by
`cw_at_stake` = net and always kept, replacing the weakest session hint when 3 are already there; a consistency hint
has `cw_at_stake` 0 and only takes a free place):
- `skill_filemap`: a skill this request used ran in >= half of the matching runs too: `➜ Add a file map to skill
  /<name> (<files>)`; cost = the map's tokens (the discovered files) for each run's turns; evidence "≈D
  discovery/run − ≈C file map in the skill → ≈N/run net · /<name> in K of M runs, S sessions · ≈X CW each · ...".
  Needs >= 3 stable discovered files.
- `existing_skill`: a SKILL.md (frontmatter name / description, plain or a YAML block scalar `>-` / `|`, + first
  lines; from ~/.claude/skills,
  <project>/.claude/skills and each `--skills-dir`) this session did not use shares >= 3 words with the workflow
  (label words, file stems, programs; shell utilities ignored), >= 30% of the smaller word set and a word of its name:
  `➜ Use skill /<name> for this`; cost = its body (the file after the frontmatter) for each run's turns; evidence
  `≈D discovery/run − ≈C skill body → ≈N/run net · done N× without it in K sessions · ≈X CW each · you have skill
  <name> ("<description>") that covers this (estimate)`.
- otherwise two remedies are weighed:
  - `claude_md`: `➜ Add a file map to CLAUDE.md`: the stable discovered files (found by >= half the runs, at most
    12; stable when >= half the runs found >= 3/4 of them) with a one-line role each; cost = its tokens in context on
    every turn of every session of the projects involved (indexed requests' turns × 0.1 + sessions × 1.25), divided
    by the number of matched runs. The snippet goes to `<cache dir>/claude-md-drafts/<slug>.md` (`stub_path`).
    Chosen when stable, passing and its net beats the skill's.
  - `repeat`: the skill below; cost = the drafted SKILL.md body for each run's turns; needs >= 2 steps the runs share
    and >= 3 files in its "## Files".
  Evidence `≈41k discovery/run − ≈9k skill body → ≈32k/run net · done 4× in 4 sessions · ...`.
- `consistency`: no remedy nets a saving (net <= 0) but >= 3 matched runs have the same >= 2 steps in order:
  `➜ Make "<name>" a skill for consistency`, evidence `same 4 steps in 4 runs; no token saving expected (≈D
  discovery/run − ≈C skill body) · ...`, with the SKILL.md draft. A net between 0 and the gates gives nothing.
- `repeat`: `➜ Make "<name>" a skill` (name = the task verb and first-clause words of the current prompt
  that most runs' prompts share, project-name words dropped, plus the tests / README / CHANGELOG most runs edited:
  "Add CLI subcommand with tests, README and CHANGELOG"; else shared label words, else "change <top edited files> and
  test"; the only prompt-derived text the pane shows), evidence `≈Y/run saving · done N× in K sessions · ≈X CW each · <steps> (estimate)`, the
  steps being the request's reads / edits on the file map and its test runs ("read cli.py → edit cli.py, README.md →
  run unittest"). A SKILL.md draft (frontmatter, steps, file map with counts, commands, one run's step labels) is
  written to `<cache dir>/skill-drafts/<slug>/SKILL.md` (slug: whole words up to 40 characters), never into a skills
  directory; its path is `stub_path`, shown as `~/...` under home, else `<cache>/skill-drafts/<slug>/SKILL.md` (never
  an absolute machine path). Its "## Steps" are the ordered steps the matched runs share (`common_steps`: the phase
  sequence of the run agreeing most with the others, each phase and target kept when >= half the runs have it):
  "1. read cli.py, models.py, store.py 2. edit cli.py + tests 3. update README.md, CHANGELOG.md 4. run unittest".
  Evidence always leads with the numbers; the pane wraps it onto up to 3 dim lines and cuts only the tail.
Hint fields: `rule`, `kind: "cross_session"`, `action` (= `text`), `evidence`, `est_saving_cw` (= `cw_at_stake`),
`estimate`, `skill` (existing skill or null), `stub_path` (or null), `matches` ([{sid prefix, level, label, cw,
score}]), `seq`, `discovery_cw`, `cost_cw`, `net_cw` (`est_saving_cw` = `cw_at_stake` = net; 0 for `consistency`). The pane
never cuts the draft's path at the right edge: on the skill's line when it fits whole, else on its own line with the
middle shortened (`~/.cache/tokenscope/…/<slug>/SKILL.md`, the file name and the slug kept), else wrapped at its
"/" onto dim lines. Live: the server builds the index in a background thread (refreshed every 10 min; `--no-xhints`
disables it); text fields go through the session redactor.

`tools/eval_xhints.py --root DIR [--skills-dir DIR ...] [--out FILE]`: chronological leave-future-out replay: for
each session (in start order) each finished request is an idle point evaluated against the sessions that started
earlier only. Prints firing rates per outcome (idle points, sessions) and the index build / rebuild timing; writes up
to --sample (20) random firings with their kind, median discovery, remedy cost, net, evidence and the matched prior
requests' step labels to --out; `--explain SID` logs every remedy weighed at that session's idle points.
