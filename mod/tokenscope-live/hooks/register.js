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

// 4s, 3m12s, 1h05m
export function fmtDur(s) {
  const v = Math.max(0, Math.floor(Number(s) || 0))
  const h = Math.floor(v / 3600)
  const m = Math.floor((v % 3600) / 60)
  if (h > 0) return h + 'h' + String(m).padStart(2, '0') + 'm'
  if (m > 0) return m + 'm' + String(v % 60).padStart(2, '0') + 's'
  return v + 's'
}

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
  }
}

// payload.recap (sent only while the session waits for you / is idle) -> { costliest, slowest, hints }, or null
function normalizeRecap(rc) {
  if (!rc || typeof rc !== 'object') return null
  const intent = (x) => (x && typeof x === 'object' ? {
    label: String(x.label || 'intent'), cat: groupOf(x.cat, x.cat), cw: Number(x.cw) || 0,
    usd: typeof x.usd === 'number' ? x.usd : null, pct: typeof x.pct === 'number' ? x.pct : null,
    s: typeof x.s === 'number' ? x.s : null,
  } : null)
  const hints = (Array.isArray(rc.hints) ? rc.hints : []).filter((h) => h && typeof h === 'object' && h.text)
    .slice(0, MAX_HINTS).map((h) => ({ text: String(h.text), evidence: h.evidence ? String(h.evidence) : '' }))
  return { costliest: intent(rc.costliest), slowest: intent(rc.slowest), hints }
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
    test: t ? { ok: !!t.ok, passed: typeof t.passed === 'number' ? t.passed : null, failed: typeof t.failed === 'number' ? t.failed : null } : null,
  }
}

// --- drawing helpers -------------------------------------------------------

function fit(text, width) {
  const s = String(text)
  if (width <= 0) return ''
  return s.length > width ? s.slice(0, Math.max(0, width - 1)) + '…' : s
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

function costText(cw, usd) {
  return ' · ' + fmtTokens(cw) + (usd != null ? ' · $' + usd.toFixed(2) : '')
}

// "[VERIFY] Run store tests · Bash unittest ✓ · 9k · $0.03"; the summary (when `withSummary`: a top-level step
// has it on its detail line instead) gets what room the rest leaves
function stepSegs(s, prefix, width, withSummary) {
  const provisional = s.open && !s.locked
  const head = [
    { text: prefix, dimColor: true },
    s.open && { text: '▶ ', color: GROUP_COLORS[s.cat], bold: true },
    { text: '[' + s.cat.toUpperCase() + ']', color: GROUP_COLORS[s.cat], bold: true },
    { text: ' ' + s.label + (provisional ? '…' : ''), dimColor: provisional, bold: s.open && !provisional },
    s.source === 'declared' && { text: ' ✎', dimColor: true },
  ]
  const cost = { text: costText(s.cw, s.usd), dimColor: true }
  const room = width - segsLen(head) - cost.text.length - 3
  const summary = withSummary && s.summary && room >= 4 && { text: ' · ' + fit(s.summary, room), dimColor: true }
  return [...head, summary, cost]
}

function groupSegs(g, expanded, live) {
  return [
    { text: (expanded ? '▾' : '▸') + g.n + ' ', bold: live, dimColor: !live },
    { text: '[' + g.cat.toUpperCase() + ']', color: GROUP_COLORS[g.cat], bold: true },
    { text: ' ' + g.label, bold: live },
    g.source === 'declared' && { text: ' ✎', dimColor: true },
    { text: ' · ' + g.steps + (g.steps === 1 ? ' step' : ' steps') + costText(g.cw, g.usd), dimColor: true },
  ]
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

// "▶ Edit store.py · 4k", "✓ unittest 22 passed · 3k", "✗ unittest 1 failed · 3k"
export function opText(o) {
  const tool = /^Bash ./.test(o.tool) ? o.tool.slice(5) : o.tool
  const target = o.target && o.target !== tool && !tool.endsWith(' ' + o.target) ? ' ' + o.target : ''
  const t = o.test
  const test = !t ? '' : t.failed > 0 ? ' ' + t.failed + ' failed' : t.passed != null ? ' ' + t.passed + ' passed' : ''
  return tool + target + test + (o.cw > 0 ? ' · ' + fmtTokens(o.cw) : '')
}

function opSegs(o, prefix) {
  const failed = o.err || (o.test && (!o.test.ok || o.test.failed > 0))
  const icon = o.err == null ? { text: '▶ ', color: 'green', bold: true } : failed ? { text: '✗ ', color: 'red' } : { text: '✓ ', color: 'green' }
  return [{ text: prefix, dimColor: true }, icon, { text: opText(o), dimColor: o.err != null, bold: o.err == null }]
}

// In-process calls the transcript has not caught up with, as running ops of the live step
function pendingOps(r) {
  if (r.toolCalls == null) return []
  return (local.liveCalls || []).filter((c) => c.calls > r.toolCalls && (r.status === 'working' || fresh(c)) && !isDeclare(c.tool))
    .map((c) => ({ tool: c.tool === 'Bash' && c.target ? 'Bash ' + c.target : c.tool, target: c.tool === 'Bash' ? null : c.target, cw: 0, err: null, test: null }))
}

function opRows(Box, Text, width, s, prefix, extra) {
  const ops = [...(s.ops || []), ...extra].slice(-MAX_OPS)
  return ops.map((o, j) => line(Box, Text, 'op-' + s.i + '-' + j, opSegs(o, prefix), width))
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
      children: texts(Text, groupSegs(g, expanded, live), width),
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

// "── recap ──": what the work since your last prompt cost and up to 3 hints, while the session waits for you;
// gone as soon as the agent works again (or a call the transcript has not caught up with is running)
function recapRows(Box, Text, width, r) {
  const rc = r.recap
  if (!rc || (r.status !== 'waiting' && r.status !== 'idle') || liveNow(r)) return []
  const rows = [line(Box, Text, 'recap', [{ text: '── recap ──', dimColor: true }], width)]
  // "costliest 38% · [CHANGE] <label> · 63k · $0.22": the share (and duration) lead, the label gets what room is left
  const intentLine = (key, name, lead, x, cost) => {
    const head = [{ text: name, dimColor: true }, ...lead.flatMap((t) => [{ text: t, bold: true }, { text: ' · ', dimColor: true }]),
      { text: '[' + x.cat.toUpperCase() + ']', color: GROUP_COLORS[x.cat], bold: true }]
    const tail = cost ? { text: costText(x.cw, x.usd), dimColor: true } : null
    const label = { text: ' ' + fit(x.label, width - segsLen(head) - (tail ? tail.text.length : 0) - 1) }
    return line(Box, Text, key, [...head, label, tail], width)
  }
  const c = rc.costliest
  const s = rc.slowest && rc.slowest.s != null ? rc.slowest : null
  const share = c && c.pct != null ? [c.pct + '%'] : []
  if (c && s && s.label === c.label && s.cat === c.cat) {
    rows.push(intentLine('recap-cost', 'costliest & slowest ', [...share, fmtDur(s.s)], c, true))
  } else {
    if (c) rows.push(intentLine('recap-cost', 'costliest ', share, c, true))
    if (s) rows.push(intentLine('recap-slow', 'slowest   ', [fmtDur(s.s)], s, false))
  }
  rc.hints.forEach((h, i) => rows.push(line(Box, Text, 'recap-hint-' + i, [
    { text: '💡 ' + h.text, color: 'cyan' },
    h.evidence && { text: ' · ' + h.evidence, dimColor: true },
  ], width)))
  return rows
}

function drawRemote(Box, Text, width, Button, $) {
  const pending = pendingTurns(remote)
  const r = pending.length ? { ...remote, turns: [...remote.turns, ...pending] } : remote
  const top = [header(Box, Text, width, 'tokenscope', r.status, r.elapsed_s != null ? fmtDur(r.elapsed_s) : '')]
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
  // the recap goes under the list, which yields its older rows first to make room
  const recap = recapRows(Box, Text, width, r)
  const budget = Math.max(3, MAX_ROWS - top.length - recap.length - bottom.length)
  const tree = intents ? intentList(Box, Text, Button, width, r.intents, budget, $, r) : flowTree(Box, Text, width, r, budget)
  return [...top, ...tree, ...recap, ...bottom].slice(0, MAX_ROWS)
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
  return parts.join(' · ') + (r.warnings.length ? ' · ⚠' : '')
}

// --- polling and redraws ---------------------------------------------------

function toastNewWarnings($, warnings) {
  const fresh = seenRules ? warnings.filter((w) => !seenRules.has(w.rule)) : []
  if (!seenRules) seenRules = new Set()
  for (const w of warnings) seenRules.add(w.rule)
  if (fresh.length) $.ui.toast(fresh.map((w) => '⚠ ' + w.text).join('  '))
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
    dirty = true
  }
  local.nowMs = await $.clock.now()   // also ages the in-process live calls (fresh())
  if (!remote) dirty = true   // local mode: the header's clock ticks once a second
}

const USAGE = 'usage: /tokenscope [expand <n|all> | collapse <n|all> | view intents|phases]'

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
      description: 'Toggle the tokenscope live pane; expand|collapse <n|all>, view intents|phases',
      argumentHint: '[expand <n|all> | collapse <n|all> | view intents|phases]',
      immediate: true,
    })
    timers.forEach((t) => t.cancel())
    view = 'intents'
    groupOpen.clear()
    allMode = null
    remote = null
    remoteText = ''
    seenRules = null
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
    const [verb, arg] = String(e.args || '').trim().toLowerCase().split(/\s+/)
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
