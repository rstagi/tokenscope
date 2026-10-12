# tokenscope live — design spec

Watch Claude Code sessions as they happen: goal -> intent -> tool/skill flow building up per session, plus a
multi-session board. Standard library only (Python >= 3.11), no network access except localhost. Runs on the
machine where Claude Code writes transcripts.

## Package layout (inside this repo)

    tokenscope/intents.py          tool_intent(record), turn_intent(records), record(name, input), GROUP, PRIORITY
    tokenscope/pricing.py          price_of(model), cost-weighted weights W_*
    tokenscope/redact.py           redaction helpers (final_pass etc.)
    tokenscope/live/__init__.py
    tokenscope/live/tail.py        incremental transcript tailer
    tokenscope/live/state.py       per-session live model + warning rules
    tokenscope/live/goalname.py    goal name from the first prompt (keyword heuristic; optional Haiku)
    tokenscope/live/hookevents.py  reads hook events written by the hook script (optional source)
    tokenscope/live/hook.py        the hook script: stdin JSON -> one line appended to the events file
    tokenscope/live/server.py      ThreadingHTTPServer: static pages, JSON API, Server-Sent Events
    tokenscope/live/static/board.html, session.html, live.css, live.js, demo.html (offline UI preview)
    tokenscope/live/statusline.py  Claude Code status line helper
    tokenscope/live/__main__.py    `python -m tokenscope.live`
    CLI: `tokenscope live [--root DIR] [--port 8765] [--host 127.0.0.1] [--no-redact] [--haiku] [--since-minutes 120]`
         (add a `live` subcommand to tokenscope/cli.py that calls tokenscope.live.server.main(args))

## Ingestion

1. Transcript tailer (always on). Root default `~/.claude/projects` (or $TOKENSCOPE_ROOT / --root). Every 0.5 s:
   - glob `*/*.jsonl` (main sessions) and `*/<session-id>/subagents/**/agent-*.jsonl` (subagents; the sibling
     `agent-<id>.meta.json` has `toolUseId`, `agentType`, `description` linking it to the parent's Agent/Task call).
   - Only files modified within `--since-minutes` (default 120) are tracked at startup; afterwards any file that
     changes is tracked.
   - Per file keep byte offset; read appended bytes; process complete lines only (keep a partial trailing line in a
     buffer). Handle truncation/rotation (size < offset -> restart from 0, reset that session).
   - Malformed JSON lines are skipped and counted.
2. Hook events (optional, more precise status). `tokenscope live hooks` prints a settings.json snippet that runs
   `python3 -m tokenscope.live.hook` (async, never blocks, always exit 0, < 50 ms) for UserPromptSubmit,
   PreToolUse, PostToolUse, PostToolUseFailure, Notification, SubagentStart, SubagentStop, Stop, SessionStart,
   SessionEnd. The hook appends `{"ts", "event": hook_event_name, "session_id", "tool_name"?, "notification_type"?,
   "message"? (Notification only, truncated 200 chars)}` to `$TOKENSCOPE_LIVE_EVENTS` (default
   `~/.cache/tokenscope-live/events.jsonl`). Never write tool inputs/outputs or prompts there. The server tails this
   file too and uses it for status (see below). Everything must work without hooks.

## Transcript records (Claude Code JSONL)

Each line is an event dict with `type` in user / assistant / system / attachment / summary / ...; `timestamp`
(ISO, Z), `sessionId`, `cwd`, `isSidechain`, `uuid`, `message`.
- assistant: `message.model`, `message.usage` {input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
  output_tokens}, `message.id` (several lines can share one message.id — one per content block; count usage ONCE
  per message.id, using the last line seen for that id), `message.content` blocks: `thinking` ({thinking: text}
  — text may be empty/redacted; use its length or the `signature` presence), `text`, `tool_use` {id, name, input}.
  `message.stop_reason` end_turn / tool_use.
- user: `message.content` is a string (a prompt) or blocks. Blocks of type `tool_result` {tool_use_id, is_error,
  content}. A real human prompt = string content or text blocks, not tool_result, not isMeta, not starting with
  `<command-` / `<local-command` / `<system-reminder>` / `Caveat:`. Reuse `tokenscope.parse.real_prompt_text(ev)`
  if it fits (check its signature) — it already implements these rules.
- `isCompactSummary: true` or system subtype containing `compact` -> a compaction marker.
- `toolUseResult` on the user line carries structured results (ignore content; never store full results — keep
  only length).

## Live session model (state.py)

Session: id, project (cwd basename; redacted -> `project-A`, `project-B` … stable per run), file path, started,
last_event_ts, model, goal {name, source: "keyword"|"haiku", prompt_chars}, prompts (list of {ts, chars, name, source, text?}),
steps (ordered), agents (subagent branches: {id, type, description, tool_use_id, steps, cw, usd, status}),
totals {cw, usd, input, cache_write, cache_read, output, turns, tool_calls, errors}, context_tokens (last main
request: input + cache_write + cache_read), status, flags.

Steps (append-only list per session or per agent branch), each with monotonically increasing `seq`:
- `{"k":"prompt","ts","chars","text"(omitted when redacted),"n"}`
- `{"k":"reason","ts","kind":"thinking"|"text","chars"}` (text kept only unredacted, first 160 chars)
- `{"k":"turn","ts","intent","group","tools":[{"name","label","target","id","err":bool|null,"dur_ms"?}],"cw","usd"}`
  one per assistant message that has tool_use blocks; intent = intents.turn_intent([...records]) (records via
  intents.record(name, input)); group = intents.GROUP[intent]. `label` = short tool label (Bash -> "Bash git",
  mcp__srv__op -> "mcp:srv.op", Skill -> "skill:<name>", Agent -> "Agent(<type>)"). `target` = file basename,
  bash program, url host, pattern (redacted: extension only / program only / "…").
  Tool results arriving later set `err` on the matching tool (by tool_use id) and may mark the step.
- `{"k":"fanout","ts","tool_use_id","agent_type","description"(redacted: omitted),"agent_id"}` emitted for an
  Agent/Task tool_use; the subagent's own steps live in `agents[agent_id].steps` once its transcript appears.
- `{"k":"compact","ts"}`
- `{"k":"flag","ts","flag","rule","detail","text","hint"}` when a warning first fires (also kept in
  session.warnings; `flag` = `rule`, `detail` = `text`, for compatibility).
CW per assistant message = input + 1.25*cache_write + 0.1*cache_read + 5*output (use pricing.W_*). $ = API
list-price equivalent from pricing.price_of(model) (None -> 0 and count as unpriced). Subagent usage counts into
the session totals and the agent's own totals.

Warnings (live; rare by design: a normal sequential bug-fix session -- test fails, edit, test fails differently,
edit, ..., green -- produces none). Payload shape: `warnings: [{"rule","text","hint","count","ts"}]`; the old
`flags: [{"flag","count","detail"}]` key is kept as an alias. `count` = number of episodes (firings) in the session.
- `stuck_error`: the same normalised error repeats on >= 3 failed tool results with no file edit in between
  (Edit/Write/MultiEdit/NotebookEdit, or any tool whose intent is `edit`), per thread (main and each subagent).
  Only the first + last 400 chars of a result are kept; normalising strips ANSI, lowercases, replaces timestamps,
  paths, hex ids, durations, line:col and numbers with placeholders; the signature is the last 1-2 error-ish lines
  (error|exception|failed|denied|not found|traceback|...), else the last line. Text "same error ×N, no edits
  since"; hint "try a different approach (read the error, change the code or the command)".
- `reread_loop`: the same file read in full (Read without offset/limit, or `cat <file>`) >= 4 times in one thread
  with no edit to that file and no compaction in between. Text "re-read <basename> ×N" (redacted: extension);
  hint "it is already in context; reference it or read only the lines you need".
- `browser_loop`: >= 20 consecutive browse_ui turns. Hint "script the flow or check the selector/page state".
- `context_near_full`: context_tokens >= 85% of the window (1_000_000 when the model id contains "[1m]" or the
  session ever exceeded 200k, else 200_000). Text "context 91% full"; hint "/compact or start a fresh session
  with a summary". Live: it disappears again when the context drops (e.g. after a compaction).
A compaction is a timeline / tree event, not a warning. `tools/eval_warnings.py --root DIR [--limit N] [--show]`
replays a transcripts tree offline through the same code and prints how often each rule fires.
Keep each rule a small pure function with unit tests.

Status (per session): `working` if the last event is a user prompt / tool_result / assistant tool_use, or a hook
says PreToolUse/UserPromptSubmit after the last Stop, and the last event is < 90 s old; `waiting` (for the user)
if a Notification hook (permission prompt / idle prompt) is newer than the last tool/prompt event, or the last
assistant message has stop_reason end_turn and the session is < 30 min old; `idle` otherwise (> 30 min since
last activity, or SessionEnd). Document the heuristic in the README; hooks make it exact.

Goal naming (goalname.py): from the first real prompt (every later prompt gets a keyword name too). The
imperative clause is used: the first sentence starting with a verb (after filler), else the first non-context
sentence with a verb (context sentences like "The test suite is red." / "You are ..." are skipped). Keyword
heuristic: lowercase, drop stopwords and filler
("please", "can you", "i want", "let's" …), detect a verb class (fix/debug -> "Fix", add/build/implement/create ->
"Build", refactor/clean -> "Refactor", test -> "Test", review/check/audit -> "Review", explain/what/how/why ->
"Explain", write/draft -> "Write", deploy/release -> "Ship", research/find/compare -> "Research", else "Work on"),
then up to 4 salient keywords (nouns-ish: not stopwords, length >= 3, prefer words appearing in code-like forms:
file names, CamelCase, snake_case). Result like "Fix: checkout total tax" (<= 48 chars). In redact mode mask
paths, emails, URLs, long numbers, hex ids and capitalised proper-noun-looking words before naming (reuse
tokenscope.redact helpers if suitable). `--haiku` (off by default): after the heuristic, call
`claude -p --model haiku --tools "" --output-format json` with ONLY the masked first 240 chars, asking for a 3-7
word goal name; cached per session in memory (later prompts: per (session, prompt n), keyword name until it
arrives); 30 s timeout; never blocks the tailer (background thread);
failures keep the heuristic name.

## Server (server.py)

ThreadingHTTPServer bound to 127.0.0.1 by default. Background thread runs the tailer loop. Endpoints:
- `GET /` -> static/board.html; `GET /s/<session_id>` -> static/session.html; `GET /static/<file>`.
- `GET /api/board` -> {"now", "redacted": bool, "sessions":[{"id","short","project","goal","status",
  "current_intent","current_group","last_step":{"label","ts","err"},"started","last_ts","elapsed_s","cw","usd",
  "turns","tool_calls","errors","agents_running","agents_total","context_tokens","flags":[{"flag","count"}],"warnings":[...],
  "intent_mix":[[group,cw],...],"intent":{"label","cat","open"} or null (the last intent step's group, else the
  step, labelled as in the pane),"skills":[names]}]} sorted by last_ts desc; sessions idle for more than 6 h dropped.
- `GET /api/session/<id>` -> full session model (as above, steps of main + agents), plus "seq" (max).
- `GET /api/session/<id>?since=<seq>` -> only steps with seq > since (+ totals, status, flags, agents summary).
- `GET /api/session/<id>/flow` -> the session canvas payload (static/session.html, built from web/canvas): the
  compact payload with `?ops=all`, where every intent step also carries all its calls and `ts0` / `ts1`, plus
  "header" (as in the full model: totals, flags, model, started, ...), "now_ts", and "flow": {"agents": [{"id","type",
  "description","status","turns","cw","usd","parent","first_ts","last_ts","launch_ts","launch":{"agent" (launching
  subagent id, null = main thread),"step" (that thread's intent step index)},"intents" (the subagent's own steps /
  groups: signal or rule labels, never a model call)}]} (a launched subagent without a transcript yet:
  id "pending:...", status "starting", intents null). `?replay=1[&restart=1][&duration=<s>]`: the same payload from a
  replay of the session's transcripts (main + subagents) fed in time order into a fresh state on a compressed
  timeline (about `duration` s, default 90, at most 3 s between two records; the state's clock is the transcript time
  at the replay position, so statuses, the live step and the idle recap behave as they did live) plus "replay":
  {"fed","records","done","elapsed_s","duration_s","speed"}; one replay per session, advancing with wall time.
- `GET /api/session/<id>/compact` -> small payload for the Claude Code mod: {"goal","status","cw","usd","elapsed_s","turns","tool_calls","errors","cw_series" (CW per main turn, last 120),
  "context_tokens","current_intent","warnings":[{"rule","text","hint","count","ts"}],"flags":[alias],
  "asked":{"goal","source","prompt_n","latest" (keyword/Haiku name of the latest prompt when prompt_n > 1),"latest_ts"},
  "now":{"intent","label" (human words, intents.LABELS),"group","tool","target","since_s" (current phase = trailing
  run of same-group main turns),"status"} or null,
  "tests": null or {"passed","failed","ok","runs","last_ts","trend":[failed of the last 8 runs]} (test-runner results
  parsed with understand.parse_test_output; an unparsable failed run -> passed/failed null, ok false),
  "files_edited":[basenames] (unredacted only), "files_edited_n",
  "recent":[last 60 main turn steps as {"intent","group","label","cw","tools":["label",...],"targets":[...],
  "tool_err","agents","err"}], "agents":[{"type","status","turns"}], "skills":[{"name","count"}] (Skill tool + slash-command skills, see
  docs/intents-spec.md section 8), "intents" (steps / groups with `dur_s` and `skills`, see docs/intents-spec.md),
  "recap" (only while waiting / idle)}. `?ops=all`: every intent step carries its `ops` (calls), not only the live
  one (the session page shows them when an older step is expanded).
- `GET /events` -> Server-Sent Events: `event: board` every 1 s when anything changed (same JSON as /api/board),
  and `event: step` {"session","step"} for every new step (so a live view can append without polling).
  Heartbeat comment every 15 s.
- All JSON responses: Cache-Control no-store. No CORS (same origin); the mod fetches from Claude Code process so
  CORS is irrelevant.
Redaction is ON by default (`--no-redact` turns it off): applied when building the model (prompts not stored,
project names masked, targets reduced, goal names from masked text). Print the URL at start.

## Non-goals / rules
- Standard library only. No writes outside $TOKENSCOPE_LIVE_EVENTS and its dir.
- Never read or keep tool result bodies beyond their length; never log prompt text when redacted.
- Code must be clean for open source: no personal names, local paths or secrets.
