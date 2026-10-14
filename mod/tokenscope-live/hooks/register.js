// tokenscope-live: a pane (and optional band) showing what the user asked and
// what the agent is doing right now. With `tokenscope live` running it shows the
// goal, the current intent, tests, edited files, cost and the flow of phases;
// without it, counters this module keeps from the events.
//
// Redraws never come straight from events: polls and local counters set
// `dirty`, and a 250 ms timer flushes it, so a burst of tool calls costs at
// most ~4 redraws a second.

const PANE = 'tokenscope-live'
const DEFAULT_BASE = 'http://127.0.0.1:8765'
const POLL_MS = 1000
const FLUSH_MS = 250
const MAX_PHASES = 30   // beyond the row budget, older phases are folded into "… N earlier phases" anyway
const EXPANDED_OLDER = 2   // older phases shown with their tools; the rest are one line each
const MAX_ROWS = 38 // the pane is ~40 rows tall
const MAX_TOOL_ROWS = 4
const MAX_TARGETS = 3
const SPARK_CHARS = 16
const CTX_LIMIT = 200_000
const LABEL_W = 7 // "ASKED  "
const MAX_OPS = 12   // operation rows under the live intent
const DETAIL_LABEL = 28   // a step label in an intent's one-line detail
const MAX_HINTS = 3   // recap hints shown

// What the server last answered (null: local mode) and its text, to spot changes
let remote = null
let serverUp = false   // the server answered (maybe 404: this session is not in a transcript yet)
let remoteText = ''
let inFlight = false
let dirty = false
let timers = []
// Warning rules already seen (null until the first payload, which only seeds it)
let seenRules = null
// Alert ids already seen (same: the first payload only seeds it, so opening the pane does not replay old alerts)
let seenAlerts = null

// The intent list's view state, kept across redraws: 'intents' or 'phases'; group.first -> expanded, as the
// person set it with /tokenscope expand|collapse or a press; and 'expanded' / 'collapsed' after "all"
let view = 'intents'
const groupOpen = new Map()
let allMode = null

// Local fallback counters
const local = { tools: {}, recent: [], calls: 0, turns: 0, cw: 0, ctx: 0, turnCw: 0, series: [], working: false, startMs: null, nowMs: 0, live: null }

const GROUP_COLORS = {
  investigate: 'blue',
  change: 'red',
  verify: 'yellow',
  ship: 'magenta',
  delegate: 'yellowBright',
  research: 'cyan',
  wait: 'gray',
  plan: 'magentaBright',
  overhead: 'gray',
  other: 'gray',
}

const GROUP_WORDS = [
  ['investigate', /investigat|explor|read|search|inspect|debug|understand/],
  ['change', /chang|edit|writ|implement|fix|refactor|code/],
  ['verify', /verif|test|check|lint|build|review/],
  ['ship', /ship|commit|push|release|deploy|pr\b/],
  ['delegate', /delegat|subagent|agent|task/],
  ['research', /research|web|fetch|docs/],
  ['wait', /wait|idle|sleep|poll/],
  ['plan', /plan|design|think/],
]

const STATUS_PILLS = {
  working: { text: '● working', color: 'green' },
  waiting: { text: '● waiting for you', color: 'yellow' },
  idle: { text: '● idle', dimColor: true },
}

export function groupOf(intent, group) {
  if (group && GROUP_COLORS[group]) return group
  const text = String(intent || '').toLowerCase()
  for (const [name, re] of GROUP_WORDS) if (re.test(text)) return name
  return 'other'
}

export function costWeighted(usage) {
  if (!usage) return 0
  return (
    (usage.input_tokens || 0) +
    1.25 * (usage.cache_creation_input_tokens || 0) +
    0.1 * (usage.cache_read_input_tokens || 0) +
    5 * (usage.output_tokens || 0)
  )
}

export function fmtTokens(n) {
  const v = Number(n) || 0
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M'
  if (v >= 1e3) return Math.round(v / 1e3) + 'k'
  return String(Math.round(v))
}

// 4s, 1m12s, 1h3m
export function fmtDur(s) {
  const v = Math.max(0, Math.floor(Number(s) || 0))
  const h = Math.floor(v / 3600)
  const m = Math.floor((v % 3600) / 60)
  if (h > 0) return h + 'h' + m + 'm'
  if (m > 0) return m + 'm' + (v % 60) + 's'
  return v + 's'
}

const numOrNull = (v) => (typeof v === 'number' && isFinite(v) ? v : null)
const skillList = (v) => (Array.isArray(v) ? v.filter((x) => x != null && x !== '').map(String) : [])

const intentLabel = (intent) => (intent ? String(intent).replace(/_/g, ' ') : '')

// The server's /compact payload (see docs/live-spec.md) -> the shape the pane draws.
// Every field is optional, and the older shape (goal object, cost_usd, turns, ...) still reads.
function normalize(d) {
  const goalName = d.goal && typeof d.goal === 'object' ? d.goal.name : d.goal
  const asked = d.asked && typeof d.asked === 'object' ? d.asked : {}
  const recent = Array.isArray(d.recent) ? d.recent : Array.isArray(d.turns) ? d.turns : []
  const turns = recent.map((t) => ({
    intent: t.intent,
    label: t.label || intentLabel(t.intent),
    group: groupOf(t.intent, t.group),
    cw: Number(t.cw) || 0,
    tools: Array.isArray(t.tools) ? t.tools.map(String) : [],
    targets: Array.isArray(t.targets) ? t.targets : [],
    toolErr: Array.isArray(t.tool_err) ? t.tool_err : [],
    error: t.err != null ? !!t.err : !!t.error,
    agents: Array.isArray(t.agents) ? t.agents.filter((a) => a && typeof a === 'object') : [],
  }))
  const series = Array.isArray(d.cw_series) ? d.cw_series.map((v) => Number(v) || 0) : turns.map((t) => t.cw)
  const files = Array.isArray(d.files_edited) ? d.files_edited.map(String) : []
  return {
    goal: asked.goal || goalName,
    goalSource: asked.source || (d.goal && typeof d.goal === 'object' ? d.goal.source : null),
    latest: (asked.prompt_n || 0) > 1 ? asked.latest : null,
    now: d.now && typeof d.now === 'object' ? d.now : null,
    currentIntent: d.current_intent,
    tests: d.tests && typeof d.tests === 'object' ? d.tests : null,
    files,
    filesN: typeof d.files_edited_n === 'number' ? d.files_edited_n : files.length,
    status: d.status,
    cw: d.cw,
    cost_usd: typeof d.usd === 'number' ? d.usd : d.cost_usd,
    ctx_tokens: d.context_tokens != null ? d.context_tokens : d.ctx_tokens,
    elapsed_s: d.elapsed_s,
    // main-thread calls only: the mod matches them against the main loop's own tool.call events
    toolCalls: typeof d.main_tool_calls === 'number' ? d.main_tool_calls : typeof d.tool_calls === 'number' ? d.tool_calls : null,
    turns,
    series,
    warnings: (Array.isArray(d.warnings) ? d.warnings : [])
      .filter((w) => w && typeof w === 'object')
      .map((w) => ({ rule: String(w.rule || w.text || ''), text: String(w.text || w.rule || ''), hint: w.hint ? String(w.hint) : '' })),
    subagents: Array.isArray(d.agents) ? d.agents : Array.isArray(d.subagents) ? d.subagents : [],
    intents: normalizeIntents(d.intents),
    recap: normalizeRecap(d.recap),
    alerts: normalizeAlerts(d.alerts),
    skills: (Array.isArray(d.skills) ? d.skills : []).filter((x) => x && typeof x === 'object' && x.name)
      .map((x) => ({ name: String(x.name), count: Number(x.count) || 1 })),
  }
}

// payload.alerts (tokenscope/alerts.py: [{id, kind, rule, severity, message, step, first, agent, active}]) -> the
// fields the banner draws; anything malformed is dropped
const ALERT_SEVERITIES = ['info', 'warning', 'critical']
function normalizeAlerts(v) {
  return (Array.isArray(v) ? v : []).filter((a) => a && typeof a === 'object' && a.message).map((a) => ({
    id: String(a.id || a.kind + ':' + a.rule + ':' + a.step),
    kind: String(a.kind || ''),
    rule: String(a.rule || ''),
    severity: ALERT_SEVERITIES.includes(a.severity) ? a.severity : 'warning',
    message: String(a.message),
    step: numOrNull(a.step),
    agent: a.agent ? String(a.agent) : null,
    active: a.active !== false,
  }))
}

// payload.recap (sent only while the session waits for you / is idle) -> { cw, usd, costliest, slowest, hints },
// or null. A hint is drawn whatever its kind ("session", or "cross_session" from your other sessions): its action,
// its evidence, and an optional skill name and stub path.
function normalizeRecap(rc) {
  if (!rc || typeof rc !== 'object') return null
  const intent = (x) => (x && typeof x === 'object' ? {
    label: String(x.label || 'intent'), cat: groupOf(x.cat, x.cat), cw: Number(x.cw) || 0,
    usd: typeof x.usd === 'number' ? x.usd : null, pct: typeof x.pct === 'number' ? x.pct : null,
    s: typeof x.s === 'number' ? x.s : null,
  } : null)
  const hints = (Array.isArray(rc.hints) ? rc.hints : []).filter((h) => h && typeof h === 'object' && (h.action || h.text))
    .slice(0, MAX_HINTS).map((h) => ({
      action: String(h.action || h.text),
      evidence: h.evidence ? String(h.evidence) : '',
      kind: h.kind ? String(h.kind) : 'session',
      skill: h.skill ? String(h.skill) : null,
      stubPath: h.stub_path ? String(h.stub_path) : null,
    }))
  return { cw: numOrNull(rc.cw), usd: numOrNull(rc.usd), costliest: intent(rc.costliest), slowest: intent(rc.slowest), hints }
}

// payload.intents (docs/intents-spec.md) -> { steps, groups, declared }, or null for an older server
function normalizeIntents(it) {
  if (!it || typeof it !== 'object' || !Array.isArray(it.steps)) return null
  const steps = it.steps.filter((x) => x && typeof x === 'object').map((x) => ({
    i: Number(x.i) || 0,
    cat: groupOf(x.cat, x.cat),
    label: String(x.label || intentLabel(x.cat) || 'step'),
    source: x.source ? String(x.source) : '',
    locked: !!x.locked,
    open: !!x.open,
    cw: Number(x.cw) || 0,
    usd: typeof x.usd === 'number' ? x.usd : null,
    summary: x.summary ? String(x.summary) : '',
    tested: x.tested === 'pass' || x.tested === 'fail' ? x.tested : null,
    group: typeof x.group === 'number' ? x.group : null,
    dur: numOrNull(x.dur_s),
    skills: skillList(x.skills),
    // the live step's calls (the server sends them for the open step, or the last one while working)
    ops: Array.isArray(x.ops) ? x.ops.filter((o) => o && typeof o === 'object' && !isDeclare(o.tool)).map(normalizeOp) : null,
  }))
  const groups = (Array.isArray(it.groups) ? it.groups : []).map((g, idx) => ({
    idx,
    first: Number(g && g.first) || 0,
    label: String((g && g.label) || 'intent'),
    source: g && g.source ? String(g.source) : '',
    cat: groupOf(g && g.cat, g && g.cat),
    cw: Number(g && g.cw) || 0,
    usd: g && typeof g.usd === 'number' ? g.usd : null,
    steps: Number(g && g.steps) || 0,
    dur: numOrNull(g && g.dur_s),
    skills: skillList(g && g.skills),
  }))
  // the number shown on a group line (▸2): its place in step order
  ;[...groups].sort((a, b) => a.first - b.first).forEach((g, n) => (g.n = n + 1))
  const declared = it.declared && typeof it.declared === 'object' ? Number(it.declared.calls) || 0 : 0
  return { steps, groups, declared }
}

// declare_intent (any server's) is the label source, not work: never an operation row
const isDeclare = (tool) => /declare_intent$/.test(String(tool || ''))

function normalizeOp(o) {
  const t = o.test && typeof o.test === 'object' ? o.test : null
  return {
    tool: String(o.tool || '?'),
    target: o.target != null && o.target !== '' ? String(o.target) : null,
    cw: Number(o.cw) || 0,
    err: o.err == null ? null : !!o.err,   // null: running (no result yet)
    dur: numOrNull(o.dur_s),
    skill: o.skill ? String(o.skill) : /^skill:./.test(String(o.tool || '')) ? String(o.tool).slice(6) : null,
    test: t ? { ok: !!t.ok, passed: typeof t.passed === 'number' ? t.passed : null, failed: typeof t.failed === 'number' ? t.failed : null } : null,
  }
}

// --- drawing helpers -------------------------------------------------------

function fit(text, width) {
  const s = String(text)
  if (width <= 0) return ''
  return s.length > width ? s.slice(0, Math.max(0, width - 1)) + '…' : s
}

// Word-wrap `text` into at most `maxLines` lines of `width` chars; only the tail of the last line is cut ("…"), so
// what leads (a hint's evidence starts with its numbers) is always shown whole. A word longer than a line is cut.
export function wrapText(text, width, maxLines) {
  const words = String(text).split(/\s+/).filter(Boolean)
  if (width <= 0 || maxLines <= 0 || !words.length) return []
  const lines = []
  let cur = ''
  let k = 0
  for (; k < words.length; k++) {
    const w = words[k]
    if (!cur) cur = w.length > width ? fit(w, width) : w
    else if (cur.length + 1 + w.length <= width) cur += ' ' + w
    else {
      lines.push(cur)
      if (lines.length === maxLines) break
      cur = w.length > width ? fit(w, width) : w
    }
  }
  if (lines.length < maxLines) return [...lines, cur]
  // out of lines with words left: the last line takes what fits and ends with "…"
  lines[maxLines - 1] = fit(lines[maxLines - 1] + ' ' + words.slice(k).join(' '), width)
  return lines
}

// A path in `width` chars, never cut at its end: whole when it fits, else its middle shortened, keeping the file and
// as much around it as fits ("~/.cache/tokenscope/…/<slug>/SKILL.md"); null when not even
// "<first>/…/<file>" ("<first>/…/<slug>/SKILL.md") fits (the caller wraps it).
export function fitPath(path, width) {
  const s = String(path)
  if (s.length <= width) return s
  const parts = s.split('/')
  const n = parts.length
  // the longest that fits (ties: more of the head); a generic file name ("SKILL.md") keeps its directory (the slug)
  let best = null
  for (let k = parts[n - 1] === 'SKILL.md' ? 2 : 1; k <= n - 2; k++) {
    for (let h = 1; h + k < n; h++) {
      const t = [...parts.slice(0, h), '…', ...parts.slice(n - k)].join('/')
      if (t.length <= width && (best == null || t.length > best.length)) best = t
    }
  }
  return best
}

// A path wrapped onto lines of `width` chars at its "/" (a later line starts with the "/"), a part longer than a
// line split: every character shown.
export function wrapPath(path, width) {
  if (width <= 0) return []
  const lines = []
  let cur = ''
  for (const part of String(path).split(/(?=\/)/)) {
    if (cur && cur.length + part.length > width) {
      lines.push(cur)
      cur = ''
    }
    cur += part
    while (cur.length > width) {
      lines.push(cur.slice(0, width))
      cur = cur.slice(width)
    }
  }
  if (cur) lines.push(cur)
  return lines
}

// Styled segments as Texts, cut to `width` across segments.
function texts(Text, segs, width) {
  const children = []
  let room = width
  for (const seg of segs) {
    if (room <= 0 || !seg || !seg.text) continue
    const { text, ...style } = seg
    const shown = fit(text, room)
    room -= shown.length
    children.push(Text({ ...style, wrap: 'truncate-end', children: [shown] }))
  }
  return children
}

// One row of styled segments, cut to `width` across segments.
function line(Box, Text, key, segs, width) {
  return Box({ key, flexDirection: 'row', children: texts(Text, segs, width) })
}

// A labelled row: "ASKED  <segs>"
function row(Box, Text, key, label, segs, width) {
  return line(Box, Text, key, [{ text: label.padEnd(LABEL_W), dimColor: true }, ...segs], width)
}

// Cumulative sums, resampled to at most `maxPoints` (the last point always kept).
export function cumulative(series, maxPoints) {
  const out = []
  let sum = 0
  for (const v of series) out.push((sum += Number(v) || 0))
  if (out.length <= maxPoints) return out
  const picked = []
  for (let i = 0; i < maxPoints; i++) picked.push(out[Math.round((i * (out.length - 1)) / (maxPoints - 1))])
  return picked
}

// Braille bars: two values per char, four dot rows, scaled to the max.
const LEFT_DOTS = [0x40, 0x04, 0x02, 0x01] // bottom -> top
const RIGHT_DOTS = [0x80, 0x20, 0x10, 0x08]
export function sparkline(values, maxChars) {
  if (!values.length || maxChars <= 0) return ''
  const pts = values.slice(-maxChars * 2)
  const max = Math.max(...pts)
  const level = (v) => (max <= 0 || v <= 0 ? 0 : Math.max(1, Math.round((v / max) * 4)))
  let s = ''
  for (let i = 0; i < pts.length; i += 2) {
    let bits = 0
    for (let r = 0; r < level(pts[i]); r++) bits |= LEFT_DOTS[r]
    if (i + 1 < pts.length) for (let r = 0; r < level(pts[i + 1]); r++) bits |= RIGHT_DOTS[r]
    s += String.fromCharCode(0x2800 + bits)
  }
  return s
}

// Consecutive turns with the same group, folded into phases. Each phase keeps its
// intents (to name it), runs of consecutive same-name tools and its subagents.
const TEST_TOOL = /^Bash .*test/i

// A long edit-and-test loop is one CHANGE group turn after turn; a test run inside a CHANGE phase closes it, so
// the tree reads as iterations ("CHANGE · editing files · 3 turns · tests ✓") instead of one 40-turn phase.
export function phasesOf(turns) {
  const phases = []
  for (const t of turns) {
    let p = phases[phases.length - 1]
    if (!p || p.group !== t.group || p.closed) {
      phases.push((p = { group: t.group, turns: 0, cw: 0, errors: 0, intents: new Map(), runs: [], agents: [] }))
    }
    p.turns += 1
    p.cw += t.cw
    if (t.label) {
      const it = p.intents.get(t.label) || { n: 0, last: 0 }
      p.intents.set(t.label, { n: it.n + 1, last: p.turns })
    }
    p.agents.push(...t.agents)
    let toolErrs = 0
    t.tools.forEach((name, i) => {
      let run = p.runs[p.runs.length - 1]
      if (!run || run.name !== name) p.runs.push((run = { name, n: 0, err: 0, targets: [] }))
      run.n += 1
      const target = t.targets[i]
      if (target && !name.endsWith(' ' + target) && !run.targets.includes(String(target))) run.targets.push(String(target))
      if (t.toolErr[i]) (run.err += 1), (toolErrs += 1)
    })
    p.errors += toolErrs || (t.error ? 1 : 0)
    const ti = t.tools.map((n, i) => (TEST_TOOL.test(n) ? i : -1)).filter((i) => i >= 0).pop()
    if (ti != null) {
      p.tested = t.toolErr[ti] ? 'fail' : 'pass'
      if (p.group === 'change') p.closed = true
    }
  }
  for (const p of phases) {
    let best = null
    for (const [label, it] of p.intents) if (!best || it.n > best.n || (it.n === best.n && it.last > best.last)) best = { label, ...it }
    p.label = best ? best.label : ''
  }
  return phases
}

function header(Box, Text, width, label, status, elapsed) {
  const pill = STATUS_PILLS[status] || (status ? { text: '● ' + status, dimColor: true } : null)
  return line(Box, Text, 'head', [
    { text: label, bold: true },
    pill && { text: '  ' + pill.text, color: pill.color, dimColor: pill.dimColor, bold: !pill.dimColor },
    elapsed && { text: '  ' + elapsed, dimColor: true },
  ], width)
}

function gaugeSegs(ctx, cells) {
  const ratio = Math.max(0, Math.min(1, (Number(ctx) || 0) / CTX_LIMIT))
  const full = Math.round(ratio * cells)
  const color = ratio > 0.75 ? 'red' : ratio > 0.5 ? 'yellow' : 'green'
  return [
    { text: 'ctx ', dimColor: true },
    { text: '▕', dimColor: true },
    { text: '█'.repeat(full), color },
    { text: '░'.repeat(cells - full), dimColor: true },
    { text: '▏ ', dimColor: true },
    { text: Math.round(ratio * 100) + '%', color, bold: ratio > 0.75 },
  ]
}

const segsLen = (segs) => segs.reduce((n, seg) => n + (seg && seg.text ? seg.text.length : 0), 0)

// "⣀⣤⣶⣿ 182k CW · $0.41 · ctx ▕██░░░▏ 34%": the sparkline gets the room left over.
function costSegs(width, cw, usd, ctx, series) {
  const text = [fmtTokens(cw) + ' CW', typeof usd === 'number' && '$' + usd.toFixed(2)].filter(Boolean).join(' · ')
  const gauge = ctx != null ? [{ text: ' · ', dimColor: true }, ...gaugeSegs(ctx, width - LABEL_W >= 52 ? 10 : 5)] : []
  const segs = [{ text }, ...gauge]
  const values = cumulative(series, SPARK_CHARS * 2)
  const room = Math.min(SPARK_CHARS, width - LABEL_W - segsLen(segs) - 1)
  if (values.length > 1 && room >= 3) segs.unshift({ text: sparkline(values, room), color: 'cyan' }, { text: ' ' })
  return segs
}

// The transcript (and so the server) can trail the terminal by a few seconds: Claude Code writes a turn's
// tool_use lines in batches. The mod sees each tool call in-process the moment it starts, so when it has seen
// more calls than the server reports, NOW shows that live call instead.
const LIVE_LABELS = {
  Read: ['reading code', 'investigate'], Grep: ['searching the code', 'investigate'], Glob: ['searching the code', 'investigate'],
  LS: ['searching the code', 'investigate'], Edit: ['editing files', 'change'], MultiEdit: ['editing files', 'change'],
  Write: ['editing files', 'change'], NotebookEdit: ['editing files', 'change'], WebFetch: ['researching the web', 'research'],
  WebSearch: ['researching the web', 'research'], Agent: ['delegating to a subagent', 'delegate'],
  Task: ['delegating to a subagent', 'delegate'], Skill: ['using a skill', 'delegate'], TodoWrite: ['planning', 'plan'],
}

function liveTarget(ev) {
  const e = ev && ev.input && typeof ev.input === 'object' ? ev.input : ev
  const f = e && (e.file_path || e.notebook_path || e.path)
  if (typeof f === 'string' && f) return f.split('/').filter(Boolean).pop() || f
  if (e && typeof e.command === 'string') {
    const w = e.command.trim().split(/\s+/)
    const prog = (w[0] || '').split('/').pop()
    if (/^python[\d.]*$/.test(prog) && w[1] === '-m' && w[2]) return w[2]
    return ['git', 'npm', 'pnpm', 'yarn', 'cargo', 'go', 'make', 'uv', 'docker'].includes(prog) && w[1] ? prog + ' ' + w[1] : prog
  }
  if (e && typeof e.skill === 'string') return e.skill
  if (e && typeof e.subagent_type === 'string') return e.subagent_type
  if (e && typeof e.pattern === 'string') return e.pattern.slice(0, 24)
  return null
}

// Tool calls seen in-process that the server does not report yet, as provisional turns for the tree.
function pendingTurns(r) {
  if (r.toolCalls == null) return []
  return (local.liveCalls || []).filter((c) => c.calls > r.toolCalls && (r.status === 'working' || fresh(c))).map((c) => {
    const [label, group] = liveLabel(c)
    return { intent: null, label, group, cw: 0, tools: [c.tool === 'Bash' && c.target ? 'Bash ' + c.target : c.tool],
             targets: [c.tool === 'Bash' ? null : c.target], toolErr: [false], error: false, agents: [] }
  })
}

function liveLabel(l) {
  let [label, group] = LIVE_LABELS[l.tool] || (l.tool.startsWith('mcp__') ? ['calling ' + l.tool.split('__')[1], 'research'] : ['running a command', 'verify'])
  if (l.tool === 'Bash' && /test|pytest|unittest|jest|vitest/i.test(l.target || '')) label = 'running tests'
  if (l.tool === 'Bash' && /^git /.test(l.target || '')) [label, group] = ['checking git state', 'investigate']
  return [label, group]
}

const LIVE_FRESH_MS = 20_000

function fresh(x) {
  return x && x.atMs != null && local.nowMs - x.atMs < LIVE_FRESH_MS
}

function runningAgents(r) {
  return r.subagents.filter((a) => a && (a.status === 'running' || a.status === 'starting'))
}

function liveNow(r) {
  const l = local.live
  const ahead = l && r.toolCalls != null && l.calls > r.toolCalls
  // a call the server has not seen: trust it while it is fresh, or while the server still says working
  if (ahead && (r.status === 'working' || fresh(l))) {
    const [label, group] = liveLabel(l)
    return [
      { text: '▶ ' + label, color: GROUP_COLORS[group], bold: true },
      l.target && { text: ' · ' + l.target },
    ]
  }
  const running = runningAgents(r)
  if (running.length && r.status !== 'waiting' && r.status !== 'idle') {
    const sub = fresh(local.sub) ? local.sub : null
    const [label] = sub ? liveLabel(sub) : [null]
    return [
      { text: '▶ ' + running.length + (running.length === 1 ? ' subagent working' : ' subagents working'),
        color: GROUP_COLORS.delegate, bold: true },
      sub && { text: ' · ' + label + (sub.target ? ' ' + sub.target : ''), dimColor: true },
    ]
  }
  return null
}

function nowSegs(r) {
  const live = liveNow(r)
  if (live) return live
  if (r.status === 'waiting') return [{ text: '◼ done, waiting for you', color: 'yellow' }]
  if (r.status === 'idle') return [{ text: '◼ idle', dimColor: true }]
  const n = r.now
  const last = r.turns[r.turns.length - 1]
  const label = (n && n.label) || intentLabel(n ? n.intent : r.currentIntent) || (last && last.label) || 'working'
  const group = groupOf(n && n.intent, (n && n.group) || (last && last.group))
  const what = n ? n.target || n.tool : null
  return [
    { text: '▶ ' + label, color: GROUP_COLORS[group], bold: true },
    what && { text: ' · ' + what },
    n && n.since_s != null && { text: ' · ' + fmtDur(n.since_s), dimColor: true },
  ]
}

// "✗4 → ✗2 → ✓ 12 passed"
function testSegs(t) {
  const all = Array.isArray(t.trend) ? t.trend : []
  const passed = Array.isArray(t.passed_trend) ? t.passed_trend : []
  const from = Math.max(0, all.length - 1 - 4)
  // only subagents ran tests so far: often their own new, deliberately red audit tests, so say whose they are
  const segs = t.scope === 'subagents' ? [{ text: 'subagents: ', dimColor: true }] : []
  for (let i = from; i < all.length - 1; i++) {
    const f = all[i]
    const ok = passed[i] != null ? { text: '✓' + passed[i], color: 'green' } : { text: '✓', color: 'green' }
    segs.push(f == null ? { text: '?', dimColor: true } : f > 0 ? { text: '✗' + f, color: 'red' } : ok)
    segs.push({ text: ' → ', dimColor: true })
  }
  if (t.ok && !(t.failed > 0)) segs.push({ text: '✓ ' + (t.passed != null ? t.passed + ' passed' : 'passed'), color: 'green', bold: true })
  else if (t.failed > 0) segs.push({ text: '✗ ' + t.failed + ' failed', color: 'red', bold: true }, t.passed != null && { text: ' · ' + t.passed + ' passed', dimColor: true })
  else segs.push({ text: '? ran ' + (t.runs || 1) + '×', dimColor: true })
  return segs
}

function filesText(r) {
  if (!r.filesN && !r.files.length) return null
  const n = Math.max(r.filesN, r.files.length)
  return r.files.length ? 'edited ' + n + ': ' + r.files.slice(-6).join(', ') : 'edited ' + n + (n === 1 ? ' file' : ' files')
}

const AGENT_STATUS = {
  running: { text: 'running', color: 'green' },
  done: { text: 'done', dimColor: true },
  completed: { text: 'done', dimColor: true },
  error: { text: 'error', color: 'red' },
  failed: { text: 'error', color: 'red' },
  starting: { text: 'starting', dimColor: true },
}

function runSegs(run) {
  const shown = run.targets.slice(0, MAX_TARGETS)
  const more = run.targets.length - shown.length
  return [
    { text: run.name },
    shown.length && { text: ' ' + shown.join(', ') + (more > 0 ? ' +' + more : '') },
    run.n > 1 && { text: ' ×' + run.n, dimColor: true },
    run.err > 0 && { text: ' ✗' + run.err, color: 'red', dimColor: true },
  ]
}

// "Read ×4 Grep ×2" for a collapsed phase
function toolTotals(p) {
  const totals = new Map()
  for (const run of p.runs) totals.set(run.name, (totals.get(run.name) || 0) + run.n)
  return [...totals].map(([name, n]) => name + (n > 1 ? ' ×' + n : '')).join(' ')
}

function phaseHead(p, live, last) {
  const turns = p.turns > 1 ? ' · ' + p.turns + ' turns' : ''
  return [
    { text: last ? '└ ' : '├ ', dimColor: true },
    { text: p.group.toUpperCase(), color: GROUP_COLORS[p.group], bold: true, dimColor: !live },
    p.label && { text: ' · ' + (live ? '▶ ' : '') + p.label, bold: live, dimColor: !live },
    { text: turns, dimColor: true },
    p.tested && p.group === 'change' && { text: ' · tests ', dimColor: true },
    p.tested && p.group === 'change' && (p.tested === 'pass' ? { text: '✓', color: 'green' } : { text: '✗', color: 'red' }),
  ]
}

function phaseCollapsed(Box, Text, width, p, id, last) {
  const tools = toolTotals(p)
  return [line(Box, Text, 'phase-' + id, [
    ...phaseHead(p, false, last),
    tools && { text: ' · ' + tools, dimColor: true },
    p.agents.length > 0 && { text: ' · ' + p.agents.length + (p.agents.length === 1 ? ' agent' : ' agents'), dimColor: true },
    p.errors > 0 && { text: ' ✗' + p.errors, color: 'red', dimColor: true },
  ], width)]
}

// "Audit store.py edge cases" (the task it was given) when known, else its type
function agentName(a) {
  return String(a.description || a.type || a.name || 'agent')
}

function agentSegs(a, pad) {
  const st = AGENT_STATUS[a.status] || { text: a.status || '', dimColor: true }
  const typed = a.description && a.type && a.type !== 'general-purpose'
  return [
    { text: agentName(a).padEnd(pad) },
    typed && { text: ' (' + a.type + ')', dimColor: true },
    a.turns != null && { text: ' · ' + a.turns + (a.turns === 1 ? ' turn' : ' turns'), dimColor: true },
    st.text && { text: ' · ', dimColor: true },
    st.text && { text: st.text, color: st.color, dimColor: st.dimColor },
  ]
}

// A phase with its tool runs (the newest MAX_TOOL_ROWS) and subagents under it.
function phaseExpanded(Box, Text, width, p, id, live, last) {
  const stem = last ? '  ' : '│ '
  const rows = [line(Box, Text, 'phase-' + id, phaseHead(p, live, last), width)]
  const runs = p.runs.slice(-MAX_TOOL_ROWS)
  const hidden = p.runs.length - runs.length
  const items = []
  if (hidden > 0) items.push({ key: 'more', segs: [{ text: '… ' + hidden + ' earlier', dimColor: true }] })
  runs.forEach((run, i) => items.push({ key: 'tool-' + i, segs: runSegs(run) }))
  const pad = Math.min(22, Math.max(0, ...p.agents.map((a) => agentName(a).length)))
  p.agents.forEach((a, i) => {
    items.push({ key: 'agent-' + i, agent: true, segs: agentSegs(a, pad) })
    const kids = Array.isArray(a.children) ? a.children : []
    kids.forEach((c, j) => items.push({ key: 'agent-' + i + '-' + j, child: true, lastChild: j === kids.length - 1, segs: agentSegs(c, 0) }))
  })
  items.forEach((it, i) => {
    const end = !items.slice(i + 1).some((x) => !x.child)   // no sibling below at this level
    let glyph
    if (it.child) glyph = (end ? '    ' : '│   ') + (it.lastChild ? '└─↳ ' : '├─↳ ')
    else glyph = it.agent ? (end ? '└─↳ ' : '├─↳ ') : end ? '└ ' : '├ '
    rows.push(line(Box, Text, 'phase-' + id + '-' + it.key, [{ text: stem + glyph, dimColor: true }, ...it.segs], width))
  })
  return rows
}

// The goal as root, then the newest phases: the live one always expanded, older
// ones collapsed (oldest first) and then dropped when they don't fit `budget`.
function flowTree(Box, Text, width, r, budget) {
  const phases = phasesOf(r.turns)
  if (!phases.length) return []
  const root = line(Box, Text, 'root', [{ text: '◆ ' + (r.goal || 'session'), bold: true }], width)
  const lastI = phases.length - 1
  let from = Math.max(0, phases.length - MAX_PHASES)
  const expanded = phases.map((p, i) => phaseExpanded(Box, Text, width, p, i, i === lastI && r.status !== 'waiting' && r.status !== 'idle', i === lastI))
  const collapsed = phases.map((p, i) => phaseCollapsed(Box, Text, width, p, i, i === lastI))
  const live = expanded[lastI].slice(0, Math.max(1, budget - 1))
  const pick = (fromI, foldBelow) => {
    const blocks = []
    for (let i = fromI; i < lastI; i++) blocks.push(i < foldBelow ? collapsed[i] : expanded[i])
    blocks.push(live)
    return blocks
  }
  const size = (blocks, fromI) => 1 + (fromI > 0 ? 1 : 0) + blocks.reduce((n, b) => n + b.length, 0)
  let fold = Math.max(from, lastI - EXPANDED_OLDER)
  let blocks = pick(from, fold)
  while (size(blocks, from) > budget && fold < lastI) blocks = pick(from, ++fold)
  while (size(blocks, from) > budget && from < lastI) blocks = pick(++from, lastI)
  const out = [root]
  if (from > 0) out.push(line(Box, Text, 'cut', [{ text: '… ' + from + ' earlier phase' + (from === 1 ? '' : 's'), dimColor: true }], width))
  for (const b of blocks) out.push(...b)
  return out
}

// payload.agents not already drawn under the phase that launched them
function unplacedAgents(r) {
  const placed = {}
  for (const t of r.turns) for (const a of t.agents) {
    const k = a.id || a.type || a.name
    placed[k] = (placed[k] || 0) + 1
  }
  return r.subagents.filter((s) => {
    if (s.parent) return false   // drawn under the subagent that launched it
    const k = s.id || s.type || s.name
    if (!placed[k]) return true
    placed[k] -= 1
    return false
  })
}

// --- intent list (payload.intents) -----------------------------------------

// A group is expanded when it holds the open step, unless the person said otherwise (per group, or "all").
function groupExpanded(g, live) {
  if (groupOpen.has(g.first)) return groupOpen.get(g.first)
  if (allMode) return allMode === 'expanded'
  return live
}

const groupIsLive = (it, g) => it.steps.some((s) => s.open && s.group === g.idx)

// " · 63k · $0.22 · 1m12s": tokens, $ and (when known) wall time
function costText(cw, usd, dur) {
  return ' · ' + fmtTokens(cw) + (usd != null ? ' · $' + usd.toFixed(2) : '') + (dur != null ? ' · ' + fmtDur(dur) : '')
}

const SKILL_COLOR = 'magentaBright'

// " ✦ pr-review" (" ✦ pr-review +1"): the skills that drove a step or group
function skillMark(skills) {
  if (!skills || !skills.length) return null
  return { text: ' ✦ ' + skills[0] + (skills.length > 1 ? ' +' + (skills.length - 1) : ''), color: SKILL_COLOR }
}

// The label segment gets what room the fixed parts leave, so a narrow pane cuts the label, never the numbers
// (the `marks` may give way too). `label` is { text, ...style } without the leading space.
function fitLabel(fixed, label, marks, tail, width) {
  const fixedLen = segsLen(fixed) + segsLen(tail)
  let room = width - fixedLen - segsLen(marks)
  if (room < 6) (marks = []), (room = width - fixedLen)
  return [...fixed, { ...label, text: ' ' + fit(label.text, Math.max(1, room - 1)) }, ...marks, ...tail]
}

// "[VERIFY] Run store tests · Bash unittest ✓ · 9k · $0.03 · 12s"; the summary (when `withSummary`: a top-level
// step has it on its detail line instead) gets what room the rest leaves
function stepSegs(s, prefix, width, withSummary) {
  const provisional = s.open && !s.locked
  const fixed = [
    { text: prefix, dimColor: true },
    s.open && { text: '▶ ', color: GROUP_COLORS[s.cat], bold: true },
    { text: '[' + s.cat.toUpperCase() + ']', color: GROUP_COLORS[s.cat], bold: true },
  ]
  const marks = [s.source === 'declared' && { text: ' ✎', dimColor: true }, skillMark(s.skills)].filter(Boolean)
  const cost = { text: costText(s.cw, s.usd, s.dur), dimColor: true }
  const label = { text: s.label + (provisional ? '…' : ''), dimColor: provisional, bold: s.open && !provisional }
  // the head already names the skills: the summary does not repeat them
  const sum = (s.skills.length ? s.summary.split(' · ').filter((p) => !p.startsWith('✦')).join(' · ') : s.summary)
  const room = width - segsLen(fixed) - segsLen(marks) - (label.text.length + 1) - cost.text.length - 3
  const summary = withSummary && sum && room >= 4 && { text: ' · ' + fit(sum, room), dimColor: true }
  return fitLabel(fixed, label, marks, [summary, cost].filter(Boolean), width)
}

function groupSegs(g, expanded, live, width) {
  const fixed = [
    { text: (expanded ? '▾' : '▸') + g.n + ' ', bold: live, dimColor: !live },
    { text: '[' + g.cat.toUpperCase() + ']', color: GROUP_COLORS[g.cat], bold: true },
  ]
  const marks = [g.source === 'declared' && { text: ' ✎', dimColor: true }, skillMark(g.skills)].filter(Boolean)
  const tail = [{ text: ' · ' + g.steps + (g.steps === 1 ? ' step' : ' steps') + costText(g.cw, g.usd, g.dur), dimColor: true }]
  return fitLabel(fixed, { text: g.label, bold: live }, marks, tail, width)
}

// The step that is happening now: the open one, or the one the server sent calls for (a test run closes a step
// before the next turn opens one)
const isLive = (s) => s.open || s.ops != null

// "Read store.py": a step label cut to fit an intent's detail line
function shortLabel(label) {
  return fit(String(label).replace(/…$/, ''), DETAIL_LABEL)
}

// "↳ Read store.py · Edit models.py ×2 · Run tests ✓": equal neighbours fold into ×N, what does not fit is "+N"
export function stepsDetail(steps, width) {
  const parts = []
  for (const s of steps) {
    const mark = s.tested === 'pass' ? ' ✓' : s.tested === 'fail' ? ' ✗' : ''
    const label = shortLabel(s.label)
    const last = parts[parts.length - 1]
    if (last && last.label === label && last.mark === mark) last.n += 1
    else parts.push({ label, mark, n: 1 })
  }
  const text = (p) => p.label + (p.n > 1 ? ' ×' + p.n : '') + p.mark
  let out = ''
  for (let k = 0; k < parts.length; k++) {
    const next = (out ? out + ' · ' : '') + text(parts[k])
    const rest = parts.length - k - 1
    // keep room for " +N" while parts remain
    if (next.length + (rest > 0 ? (' +' + rest).length : 0) > width) {
      const more = parts.length - k
      return out ? out + ' +' + more : fit(text(parts[k]), width - (' +' + (more - 1)).length) + (more > 1 ? ' +' + (more - 1) : '')
    }
    out = next
  }
  return out
}

const DETAIL_PREFIX = '   ↳ '

function detailRow(Box, Text, key, text, width) {
  return line(Box, Text, key, [{ text: DETAIL_PREFIX + text, dimColor: true }], width)
}

// An operation row's label ("unittest 22 passed", "skill:pr-review") and numbers (" · 3k · 4s")
function opParts(o) {
  const tool = /^Bash ./.test(o.tool) ? o.tool.slice(5) : o.tool
  const t0 = o.target
  const target = t0 && t0 !== tool && !tool.endsWith(' ' + t0) && !tool.endsWith(':' + t0) ? ' ' + t0 : ''
  const t = o.test
  const test = !t ? '' : t.failed > 0 ? ' ' + t.failed + ' failed' : t.passed != null ? ' ' + t.passed + ' passed' : ''
  const nums = [o.cw > 0 && fmtTokens(o.cw), o.dur != null && fmtDur(o.dur)].filter(Boolean)
  return { label: tool + target + test, nums: nums.length ? ' · ' + nums.join(' · ') : '' }
}

// "Edit store.py · 4k · 2s", "unittest 22 passed · 3k · 4s", "skill:pr-review · 2k · 3s"
export function opText(o) {
  const p = opParts(o)
  return p.label + p.nums
}

// "▶ Edit store.py · 4k · 2s" (running), "✓ …", "✗ …"; a skill is "✦ skill:pr-review · 2k · 3s". The label is cut
// to fit, never the numbers.
function opSegs(o, prefix, width) {
  const failed = o.err || (o.test && (!o.test.ok || o.test.failed > 0))
  const icon = failed ? { text: '✗ ', color: 'red' }
    : o.skill ? { text: '✦ ', color: SKILL_COLOR, bold: o.err == null }
      : o.err == null ? { text: '▶ ', color: 'green', bold: true } : { text: '✓ ', color: 'green' }
  const p = opParts(o)
  const style = { dimColor: o.err != null, bold: o.err == null }
  const room = width - prefix.length - icon.text.length - p.nums.length
  if (room < 4) return [{ text: prefix, dimColor: true }, icon, { text: p.label + p.nums, ...style }]
  return [{ text: prefix, dimColor: true }, icon, { text: fit(p.label, room), ...style }, p.nums && { text: p.nums, dimColor: true }]
}

// In-process calls the transcript has not caught up with, as running ops of the live step
function pendingOps(r) {
  if (r.toolCalls == null) return []
  return (local.liveCalls || []).filter((c) => c.calls > r.toolCalls && (r.status === 'working' || fresh(c)) && !isDeclare(c.tool))
    .map((c) => ({
      tool: c.tool === 'Bash' && c.target ? 'Bash ' + c.target : c.tool === 'Skill' ? 'skill:' + (c.target || '?') : c.tool,
      target: c.tool === 'Bash' || c.tool === 'Skill' ? null : c.target, cw: 0, err: null, test: null,
      skill: c.tool === 'Skill' ? c.target || '?' : null,
      dur: c.atMs != null ? Math.max(0, (local.nowMs - c.atMs) / 1000) : null,
    }))
}

function opRows(Box, Text, width, s, prefix, extra) {
  const ops = [...(s.ops || []), ...extra].slice(-MAX_OPS)
  return ops.map((o, j) => line(Box, Text, 'op-' + s.i + '-' + j, opSegs(o, prefix, width), width))
}

// Groups (collapsed to one line unless expanded) and ungrouped steps, oldest first, as blocks of rows. A block
// that is not live has a dim detail line under its head (dropped first when rows are short); the live block shows
// its step's calls instead, one row each.
function intentBlocks(Box, Text, Button, width, it, $, r) {
  const byIdx = new Map(it.groups.map((g) => [g.idx, g]))
  const done = new Set()
  const blocks = []
  const liveStep = [...it.steps].reverse().find(isLive)
  const extra = liveStep ? pendingOps(r) : []
  for (const s of it.steps) {
    const g = s.group != null ? byIdx.get(s.group) : null
    if (!g) {
      const live = s === liveStep
      const head = line(Box, Text, 'step-' + s.i, stepSegs(s, live ? '' : '  ', width, false), width)
      const ops = live ? opRows(Box, Text, width, s, '   ', extra) : []
      const detail = !ops.length && s.summary ? detailRow(Box, Text, 'detail-' + s.i, s.summary, width) : null
      blocks.push({ live, head, detail, rows: [head, ...ops] })
      continue
    }
    if (done.has(g.idx)) continue
    done.add(g.idx)
    const kids = it.steps.filter((x) => x.group === g.idx)
    const live = kids.includes(liveStep) || groupIsLive(it, g)
    const expanded = groupExpanded(g, live)
    const head = Box({ key: 'group-' + g.first, flexDirection: 'row', children: [Button({
      key: 'group-' + g.first, plain: true, onPress: () => toggleGroup($, g.first),
      children: texts(Text, groupSegs(g, expanded, live, width), width),
    })] })
    const rows = []
    if (expanded) {
      kids.forEach((x, j) => {
        const end = j === kids.length - 1
        rows.push(line(Box, Text, 'step-' + x.i, stepSegs(x, end ? '  └ ' : '  ├ ', width, true), width))
        if (x === liveStep) rows.push(...opRows(Box, Text, width, x, end ? '      ' : '  │   ', extra))
      })
    }
    const summary = detailRow(Box, Text, 'detail-g' + g.first, stepsDetail(kids, Math.max(1, width - DETAIL_PREFIX.length)), width)
    blocks.push({ live, group: true, head, summary, detail: expanded ? null : summary, rows: [head, ...rows] })
  }
  return blocks
}

// Newest at the bottom, each block its head, rows and detail line. Over `budget`: older expanded groups collapse
// first, then the oldest blocks lose their detail lines, then fold into "… N earlier"; the live block always stays
// (cut to its newest rows if it alone is too tall).
function intentList(Box, Text, Button, width, it, budget, $, r) {
  const blocks = intentBlocks(Box, Text, Button, width, it, $, r)
  if (!blocks.length) return []
  let from = 0
  const rowsOf = (b) => (b.detail ? [b.rows[0], b.detail, ...b.rows.slice(1)] : b.rows)
  const size = () => (from > 0 ? 1 : 0) + blocks.slice(from).reduce((n, b) => n + rowsOf(b).length, 0)
  for (let i = 0; i < blocks.length && size() > budget; i++) {
    const b = blocks[i]
    if (b.group && !b.live && b.rows.length > 1) (b.rows = [b.head]), (b.detail = b.summary)
  }
  for (let i = 0; i < blocks.length && size() > budget; i++) if (!blocks[i].live) blocks[i].detail = null
  while (size() > budget && from < blocks.length - 1 && !blocks[from].live) from++
  const live = blocks.find((b) => b.live)
  if (live && size() > budget && live.rows.length > 2) {
    const keep = Math.max(1, live.rows.length - (size() - budget) - 1)
    const cut = live.rows.length - 1 - keep
    live.rows = [live.rows[0], line(Box, Text, 'cut-live', [{ text: '  … ' + cut + ' earlier', dimColor: true }], width), ...live.rows.slice(-keep)]
  }
  const out = []
  if (from > 0) out.push(line(Box, Text, 'cut', [{ text: '… ' + from + ' earlier', dimColor: true }], width))
  for (const b of blocks.slice(from)) out.push(...rowsOf(b))
  return out
}

function toggleGroup($, first) {
  const it = remote && remote.intents
  const g = it && it.groups.find((x) => x.first === first)
  if (!g) return
  groupOpen.set(first, !groupExpanded(g, groupIsLive(it, g)))
  void $.ui.invalidate('ui.render')
}

const intentView = (r) => view === 'intents' && r.intents && r.intents.steps.length > 0

// The idle recap, a box right under the header while the session waits for you: a bright title bar
// "━━ DONE · recap · 120k · $0.40 ━━━…" across the pane, the costliest and slowest intents (share / time first),
// then up to 3 hints, the action first ("➜ Make it a skill") and the evidence dim under it (wrapped onto up to
// EVIDENCE_LINES lines, the tail cut, never its leading numbers), then a closing rule.
// Gone entirely as soon as the agent works again (or a call the transcript has not caught up with is running).
const RECAP_COLOR = 'greenBright'
const HINT_COLOR = 'yellowBright'
const EVIDENCE_LINES = 3

function recapRows(Box, Text, width, r) {
  const rc = r.recap
  if (!rc || (r.status !== 'waiting' && r.status !== 'idle') || liveNow(r)) return []
  const rule = (key, title) => {
    const head = title ? '━━ ' + title + ' ' : ''
    return line(Box, Text, key, [{ text: head + '━'.repeat(Math.max(0, width - head.length)), color: RECAP_COLOR, bold: true }], width)
  }
  const total = rc.cw != null ? ' · ' + fmtTokens(rc.cw) + (rc.usd != null ? ' · $' + rc.usd.toFixed(2) : '') : ''
  const rows = [rule('recap', 'DONE · recap' + total)]
  // "58%  costliest  [CHANGE] <label> · 63k · $0.22": the share (and time) lead, the label gets what room is left
  const intentLine = (key, lead, name, x, cost) => {
    const fixed = [{ text: ' ' }, ...lead.flatMap((t) => [{ text: t, bold: true, color: RECAP_COLOR }, { text: '  ' }]),
      { text: name, dimColor: true }, { text: '[' + x.cat.toUpperCase() + ']', color: GROUP_COLORS[x.cat], bold: true }]
    const tail = cost ? [{ text: costText(x.cw, x.usd), dimColor: true }] : []
    return line(Box, Text, key, fitLabel(fixed, { text: x.label, bold: true }, [], tail, width), width)
  }
  const c = rc.costliest
  const sl = rc.slowest && rc.slowest.s != null ? rc.slowest : null
  const share = c && c.pct != null ? [c.pct + '%'] : []
  if (c && sl && sl.label === c.label && sl.cat === c.cat) {
    rows.push(intentLine('recap-cost', [...share, fmtDur(sl.s)], 'costliest & slowest ', c, true))
  } else {
    if (c) rows.push(intentLine('recap-cost', share, 'costliest ', c, true))
    if (sl) rows.push(intentLine('recap-slow', [fmtDur(sl.s)], 'slowest ', sl, false))
  }
  rc.hints.forEach((h, i) => {
    rows.push(line(Box, Text, 'recap-hint-' + i, [{ text: ' ➜ ', color: HINT_COLOR, bold: true }, { text: h.action, color: HINT_COLOR, bold: true }], width))
    // a cross-session hint (kind "cross_session": evidence from your other sessions) says so before its evidence
    const kind = h.kind && h.kind !== 'session' ? h.kind.replace(/_/g, '-') + ': ' : ''
    wrapText(kind + h.evidence, width - 3, EVIDENCE_LINES).forEach((t, k) =>
      rows.push(line(Box, Text, 'recap-ev-' + i + '-' + k, [{ text: '   ' + t, dimColor: true }], width)))
    // the skill, then the draft's path: never cut at the right edge (its file name must show): on the skill's line
    // when it fits whole, else on its own line with the middle of the path shortened, else wrapped (dim)
    const skill = h.skill ? '✦ ' + h.skill : ''
    const stub = h.stubPath ? 'stub: ' + h.stubPath : ''
    if (skill && (!stub || 3 + skill.length + 3 + stub.length <= width)) {
      rows.push(line(Box, Text, 'recap-skill-' + i, [{ text: '   ' }, { text: skill, color: SKILL_COLOR },
        stub && { text: ' · ' + stub, dimColor: true }], width))
    } else if (skill) {
      rows.push(line(Box, Text, 'recap-skill-' + i, [{ text: '   ' }, { text: skill, color: SKILL_COLOR }], width))
    }
    if (stub && !(skill && 3 + skill.length + 3 + stub.length <= width)) {
      const short = fitPath(h.stubPath, width - 9)
      if (short != null) {
        rows.push(line(Box, Text, 'recap-stub-' + i, [{ text: '   ' }, { text: 'stub: ' + short, dimColor: true }], width))
      } else {
        rows.push(line(Box, Text, 'recap-stub-' + i, [{ text: '   ' }, { text: 'stub:', dimColor: true }], width))
        wrapPath(h.stubPath, width - 5).forEach((t, k) =>
          rows.push(line(Box, Text, 'recap-stub-' + i + '-' + k, [{ text: '     ' }, { text: t, dimColor: true }], width)))
      }
    }
  })
  rows.push(rule('recap-end', ''))
  return rows
}

// The alert banner, right under the header (above the recap): a bar "▲ ALERTS · 2 active" in the worst severity's
// colour, then one line per active alert, worst first ("▲ cost jump  step 9 · step cost $1.80, 56.1× the session
// median $0.03"), at most MAX_ALERTS of them and "+N more"; nothing when no alert is active.
const MAX_ALERTS = 3
const ALERT_COLORS = { critical: 'redBright', warning: 'yellowBright', info: 'cyan' }
const ALERT_NAMES = {
  session_usd: 'session $', intent_usd: 'intent $', jump: 'cost jump', unrelated: 'off-task',
  repeat_read: 'read loop', repeat_search: 'search loop', repeat_command: 'command loop', repeat_call: 'call loop',
  retry: 'retry loop', edit_revert: 'edit revert',
}
export const alertName = (a) => ALERT_NAMES[a.rule] || (a.rule || a.kind || 'alert').replace(/_/g, ' ')

export function activeAlerts(alerts) {
  const rank = (a) => ALERT_SEVERITIES.indexOf(a.severity)
  return alerts.filter((a) => a.active).slice().sort((a, b) => rank(b) - rank(a))
}

function alertRows(Box, Text, width, r) {
  const act = activeAlerts(r.alerts)
  if (!act.length) return []
  const color = ALERT_COLORS[act[0].severity]
  const head = '▲ ALERTS · ' + act.length + ' active' + (r.alerts.length > act.length ? ' · ' + r.alerts.length + ' total' : '') + ' '
  const rows = [line(Box, Text, 'alerts', [{ text: head + '━'.repeat(Math.max(0, width - head.length)), color, bold: true }], width)]
  act.slice(0, MAX_ALERTS).forEach((a, i) => {
    const where = a.step != null ? (a.agent ? 'subagent step ' : 'step ') + a.step + ' · ' : ''
    rows.push(line(Box, Text, 'alert-' + i, [
      { text: ' ▲ ', color: ALERT_COLORS[a.severity], bold: true },
      { text: alertName(a), color: ALERT_COLORS[a.severity], bold: true },
      { text: '  ' + where, dimColor: true },
      { text: a.message },
    ], width))
  })
  if (act.length > MAX_ALERTS) rows.push(line(Box, Text, 'alert-more', [{ text: '   +' + (act.length - MAX_ALERTS) + ' more', dimColor: true }], width))
  return rows
}

// "SKILLS ✦ pr-review ×2 · tdd": skills used in the session (Skill tool and slash-command skills)
function skillsSegs(skills) {
  return [{ text: '✦ ', color: SKILL_COLOR }, { text: skills.map((x) => x.name + (x.count > 1 ? ' ×' + x.count : '')).join(' · ') }]
}

function drawRemote(Box, Text, width, Button, $) {
  const pending = pendingTurns(remote)
  const r = pending.length ? { ...remote, turns: [...remote.turns, ...pending] } : remote
  const top = [header(Box, Text, width, 'tokenscope', r.status, r.elapsed_s != null ? fmtDur(r.elapsed_s) : '')]
  // alerts, then the recap, right under the header, where they are seen without scrolling; the intent list yields
  // rows first
  top.push(...alertRows(Box, Text, width, r))
  const recap = recapRows(Box, Text, width, r)
  top.push(...recap)
  top.push(row(Box, Text, 'asked', 'ASKED', [
    { text: r.goal || '(no prompt yet)', bold: !!r.goal, dimColor: !r.goal },
    r.goalSource === 'haiku' && { text: ' (haiku)', dimColor: true },
  ], width))
  if (r.latest) top.push(row(Box, Text, 'latest', '', [{ text: '↳ now: ' + r.latest }], width))
  top.push(row(Box, Text, 'now', 'NOW', nowSegs(r), width))
  if (r.tests) top.push(row(Box, Text, 'tests', 'TESTS', testSegs(r.tests), width))
  const files = filesText(r)
  if (files) top.push(row(Box, Text, 'files', 'FILES', [{ text: files }], width))
  top.push(row(Box, Text, 'cost', 'COST', costSegs(width, r.cw, r.cost_usd, r.ctx_tokens, r.series), width))
  if (r.skills.length) top.push(row(Box, Text, 'skills', 'SKILLS', skillsSegs(r.skills), width))
  const intents = intentView(r)
  if (intents) top.push(line(Box, Text, 'flow', [{ text: '── intents ──', dimColor: true }], width))
  else if (r.turns.length) top.push(line(Box, Text, 'flow', [{ text: '── flow ──', dimColor: true }], width))

  const bottom = (intents ? runningAgents(r) : unplacedAgents(r)).map((s, i) => line(Box, Text, 'agent-' + i, [
    { text: '↳ ' + agentName(s), dimColor: true },
    { text: ' · ' + (s.turns || 0) + ' turns · ', dimColor: true },
    { text: s.status || '', color: s.status === 'running' ? 'green' : undefined, dimColor: s.status !== 'running' },
  ], width))
  r.warnings.forEach((w, i) => bottom.push(line(Box, Text, 'warn-' + i, [
    { text: '⚠ ' + w.text, color: 'yellow', bold: true },
    w.hint && { text: ' — ' + w.hint, color: 'yellow' },
  ], width)))
  if (intents && r.intents.declared > 0) {
    bottom.push(line(Box, Text, 'declared', [{ text: '✎ declared ×' + r.intents.declared, dimColor: true }], width))
  }
  const budget = Math.max(3, MAX_ROWS - top.length - bottom.length)
  const tree = intents ? intentList(Box, Text, Button, width, r.intents, budget, $, r) : flowTree(Box, Text, width, r, budget)
  return [...top, ...tree, ...bottom].slice(0, MAX_ROWS)
}

function drawLocal(Box, Text, width) {
  const elapsed = local.startMs != null ? fmtDur((local.nowMs - local.startMs) / 1000) : ''
  const rows = [header(Box, Text, width, 'tokenscope · local', local.working ? 'working' : 'idle', elapsed)]
  const last = local.recent[local.recent.length - 1]
  rows.push(row(Box, Text, 'now', 'NOW', local.working ? [{ text: '▶ ' + (last || 'thinking'), bold: true }] : [{ text: '◼ idle', dimColor: true }], width))
  rows.push(row(Box, Text, 'cost', 'COST', costSegs(width, local.cw, null, local.ctx > 0 ? local.ctx : null, local.series), width))
  const top = Object.entries(local.tools)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([name, n]) => name + ' ×' + n)
    .join(' · ')
  if (top) rows.push(row(Box, Text, 'tools', 'TOOLS', [{ text: top }], width))
  rows.push(row(Box, Text, 'turns', '', [{ text: local.turns + ' turns · ' + local.calls + ' calls', dimColor: true }], width))
  if (local.recent.length > 0) {
    rows.push(line(Box, Text, 'recent', [{ text: 'recent: ' + local.recent.slice().reverse().join(', '), dimColor: true }], width))
  }
  const hint = serverUp ? 'connecting to tokenscope live… (waiting for the transcript)' : 'start `tokenscope live` for the goal and the flow'
  rows.push(line(Box, Text, 'hint', [{ text: hint, dimColor: true }], width))
  return rows
}

function bandText() {
  if (!remote) return 'tokenscope (local) · ' + fmtTokens(local.cw) + ' CW · ' + local.turns + ' turns · ' + local.calls + ' calls'
  const r = remote
  const now = r.status === 'waiting' ? '◼ waiting for you' : nowSegs(r)[0].text
  const parts = ['◆ ' + (r.goal || 'session'), now, fmtTokens(r.cw) + ' CW']
  if (typeof r.cost_usd === 'number') parts.push('$' + r.cost_usd.toFixed(2))
  const act = activeAlerts(r.alerts)
  if (act.length) parts.push('▲ ' + alertName(act[0]) + (act.length > 1 ? ' +' + (act.length - 1) : ''))
  return parts.join(' · ') + (r.warnings.length ? ' · ⚠' : '')
}

// --- polling and redraws ---------------------------------------------------

function toastNewWarnings($, warnings) {
  const fresh = seenRules ? warnings.filter((w) => !seenRules.has(w.rule)) : []
  if (!seenRules) seenRules = new Set()
  for (const w of warnings) seenRules.add(w.rule)
  if (fresh.length) $.ui.toast(fresh.map((w) => '⚠ ' + w.text).join('  '))
}

function toastNewAlerts($, alerts) {
  const fresh = seenAlerts ? activeAlerts(alerts).filter((a) => !seenAlerts.has(a.id)) : []
  if (!seenAlerts) seenAlerts = new Set()
  for (const a of alerts) seenAlerts.add(a.id)
  if (fresh.length) $.ui.toast(fresh.slice(0, 2).map((a) => '▲ ' + alertName(a) + ': ' + a.message).join('  '))
}

async function poll($) {
  if (inFlight) return
  inFlight = true
  let next = null
  let text = ''
  try {
    const base = (await $.env.get('TOKENSCOPE_LIVE_URL')) || DEFAULT_BASE
    const sessionId = await $.session.id()
    const res = await $.http.fetch(
      base.replace(/\/+$/, '') + '/api/session/' + encodeURIComponent(sessionId) + '/compact',
    )
    serverUp = true
    if (res.ok) {
      text = res.text
      next = JSON.parse(text)
      if (!next || typeof next !== 'object') next = null
    }
  } catch {
    next = null
    serverUp = false
  } finally {
    inFlight = false
  }
  if (!next) text = ''
  if (text !== remoteText) {
    remote = next ? normalize(next) : null
    remoteText = text
    if (remote) toastNewWarnings($, remote.warnings)
    if (remote) toastNewAlerts($, remote.alerts)
    dirty = true
  }
  local.nowMs = await $.clock.now()   // also ages the in-process live calls (fresh())
  if (!remote) dirty = true   // local mode: the header's clock ticks once a second
}

const USAGE = 'usage: /tokenscope [expand <n|all> | collapse <n|all> | view intents|phases | top [5h|24h|7d|30d] [session|goal|turn|intent]]'
const TOP_BY = { session: 'sessions', goal: 'goals', turn: 'turns', intent: 'intents' }

// `/tokenscope top 5h [session|goal|turn|intent]`: the window's costliest sessions and intents (or, grouped by
// session / goal / turn, just those, each with its session's goal) from the server's GET /api/top, as the command's
// output (not the pane)
export function topText(d) {
  if (!d || d.error) return 'tokenscope: ' + ((d && d.error) || 'no top spenders')
  if (d.pending) return 'tokenscope: indexing transcripts, try again in a few seconds'
  const t = d.total || {}
  const by = TOP_BY[d.by] ? d.by : 'intent'
  const usd = (v) => '$' + (Number(v) || 0).toFixed(2)
  const sub = (x) => ' « ' + (x.session_label || '-') + (x.sessions > 1 ? ' +' + (x.sessions - 1) : '')
  const row = (x, cat, of) => '  ' + usd(x.usd) + ' ' + (Number(x.share) || 0).toFixed(1) + '% · ' + fmtTokens(x.cw) + ' CW · ' +
    fmtDur(x.wall_s) + ' · ' + x.short + ' ' + (cat ? '[' + String(x.cat || 'other').toUpperCase() + '] ' : '') + (x.label || '') +
    (of ? sub(x) : '')
  const lines = ['tokenscope top ' + d.window + (by === 'intent' ? '' : ' by ' + by) + ': ' + usd(t.usd) + ' · ' +
    fmtTokens(t.cw) + ' CW · ' + (t.sessions || 0) + ' sessions']
  if (by === 'intent') {
    lines.push('sessions', ...(d.sessions || []).map((x) => row(x, false)))
    lines.push('intents', ...(d.intents || []).map((x) => row(x, true)))
  } else {
    const list = d[TOP_BY[by]] || []
    lines.push(TOP_BY[by], ...(list.length ? list.map((x) => row(x, false, by !== 'session')) : ['  (nothing in this window)']))
  }
  return lines.join('\n')
}

// `top` arguments in any order: a grouping (session|goal|turn|intent, plural accepted) and a window (default 5h)
function topArgs(args) {
  let window = '5h', by = null
  for (const a of args.filter(Boolean)) {
    const b = a.replace(/s$/, '')
    if (TOP_BY[a] || TOP_BY[b]) by = TOP_BY[a] ? a : b
    else window = a
  }
  return { window, by }
}

async function runTop($, args) {
  const base = (await $.env.get('TOKENSCOPE_LIVE_URL')) || DEFAULT_BASE
  const { window, by } = topArgs(args)
  try {
    const res = await $.http.fetch(base.replace(/\/+$/, '') + '/api/top?limit=5&window=' + encodeURIComponent(window) +
      (by ? '&by=' + by : ''))
    return topText(JSON.parse(res.text))
  } catch {
    return 'tokenscope: cannot reach the tokenscope server (tokenscope live)'
  }
}

// `/tokenscope expand 2`, `collapse all`, `view phases`: the answer is the command's output line
function runView(verb, arg) {
  if (verb === 'view') {
    if (arg !== 'intents' && arg !== 'phases') return USAGE
    view = arg
    return 'tokenscope: ' + arg + ' view'
  }
  if (verb !== 'expand' && verb !== 'collapse') return USAGE
  const open = verb === 'expand'
  if (arg === 'all') {
    groupOpen.clear()
    allMode = open ? 'expanded' : 'collapsed'
    return 'tokenscope: ' + (open ? 'expanded' : 'collapsed') + ' all groups'
  }
  const n = Number(arg)
  const g = remote && remote.intents && remote.intents.groups.find((x) => x.n === n)
  if (!g) return 'tokenscope: no group ' + (arg || '') + ' (the number after ▸ / ▾)'
  groupOpen.set(g.first, open)
  return 'tokenscope: ' + (open ? 'expanded' : 'collapsed') + ' group ' + n + ' · ' + g.label
}

function flush($) {
  if (!dirty) return
  dirty = false
  $.ui.invalidate('ui.render')
}

export function register(onEvent) {
  onEvent('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'tokenscope',
      description: 'Toggle the tokenscope live pane; expand|collapse <n|all>, view intents|phases, top <window> [by]',
      argumentHint: '[expand <n|all> | collapse <n|all> | view intents|phases | top 5h|24h|7d|30d [session|goal|turn|intent]]',
      immediate: true,
    })
    timers.forEach((t) => t.cancel())
    view = 'intents'
    groupOpen.clear()
    allMode = null
    remote = null
    remoteText = ''
    seenRules = null
    seenAlerts = null
    local.calls = 0
    local.live = null
    local.sub = null
    local.liveCalls = []   // tool calls are matched against this session's server-side count
    local.startMs = local.nowMs = await $.clock.now()
    timers = [$.clock.every(POLL_MS, () => poll($)), $.clock.every(FLUSH_MS, () => flush($))]
    void poll($)
    if ((await $.env.get('TOKENSCOPE_LIVE_PANE')) === '1') {
      void $.ui.open({ id: PANE, title: 'tokenscope' })
    }
    return next(e)
  })

  onEvent('command.run', { command: 'tokenscope' }, async ($, e) => {
    const [verb, arg, ...more] = String(e.args || '').trim().toLowerCase().split(/\s+/)
    if (verb === 'top') return { text: await runTop($, [arg, ...more]) }
    if (verb) {
      const text = runView(verb, arg)
      void $.ui.invalidate('ui.render')
      return { text }
    }
    const panes = await $.ui.panes()
    if (panes.some(p => p.id === PANE)) {
      await $.ui.close({ id: PANE })
    } else {
      await $.ui.open({ id: PANE, title: 'tokenscope' })
    }
    return {}
  })

  onEvent('tool.call', async ($, e, next) => {
    const name = String(e.tool)
    const atMs = await $.clock.now()
    local.tools[name] = (local.tools[name] || 0) + 1
    local.working = true
    local.recent = [...local.recent, name].slice(-12)
    if (e.agentId) {
      // a subagent's call: never a main-thread tree line, only "what the subagents are doing" in NOW
      local.sub = { tool: name, target: liveTarget(e), agentId: e.agentId, atMs }
    } else {
      local.calls += 1
      local.live = { tool: name, target: liveTarget(e), calls: local.calls, atMs }
      local.liveCalls = [...(local.liveCalls || []), local.live].slice(-12)
    }
    dirty = true
    return next(e)
  }).catch(async ($, e, next) => next(e)) // counting must never block a tool call

  onEvent('turn.step', async function* ($, e, next) {
    local.working = true
    const result = yield* next(e)
    const usage = result && result.usage
    if (usage) {
      const cw = costWeighted(usage)
      local.cw += cw
      local.turnCw += cw
      local.ctx =
        (usage.input_tokens || 0) +
        (usage.cache_read_input_tokens || 0) +
        (usage.cache_creation_input_tokens || 0)
      if (!remote) dirty = true
    }
    return result
  })

  onEvent('turn.complete', async ($, e, next) => {
    local.turns += 1
    local.series = [...local.series, local.turnCw].slice(-500)
    local.turnCw = 0
    local.working = false
    if (!remote) dirty = true
    return next(e)
  })

  onEvent('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const width = Math.max(1, e.props.bodyColumns || 40)
    const rows = remote ? drawRemote(Box, Text, width, Button, $) : drawLocal(Box, Text, width)
    return Box({ flexDirection: 'column', children: rows })
  })

  onEvent('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if ((await $.env.get('TOKENSCOPE_LIVE_BAND')) !== '1' || e.props.hasSurvey) return next(e)
    const { Text } = $.ui.resolve(e)
    const width = Math.max(10, e.props.bodyColumns || 40)
    return Text({ dimColor: true, wrap: 'truncate-end', children: [fit(bandText(), width)] })
  })
}
