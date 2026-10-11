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
const MAX_PHASES = 10
const MAX_ROWS = 38 // the pane is ~40 rows tall
const MAX_TOOL_ROWS = 4
const MAX_TARGETS = 3
const SPARK_CHARS = 16
const CTX_LIMIT = 200_000
const LABEL_W = 7 // "ASKED  "

// What the server last answered (null: local mode) and its text, to spot changes
let remote = null
let remoteText = ''
let inFlight = false
let dirty = false
let timers = []
// Warning rules already seen (null until the first payload, which only seeds it)
let seenRules = null

// Local fallback counters
const local = { tools: {}, recent: [], calls: 0, turns: 0, cw: 0, ctx: 0, turnCw: 0, series: [], working: false, startMs: null, nowMs: 0 }

const GROUP_COLORS = {
  investigate: 'blue',
  change: 'red',
  verify: 'yellow',
  ship: 'magenta',
  delegate: 'yellowBright',
  research: 'cyan',
  wait: 'gray',
  plan: 'magentaBright',
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
    turns,
    series,
    warnings: (Array.isArray(d.warnings) ? d.warnings : [])
      .filter((w) => w && typeof w === 'object')
      .map((w) => ({ rule: String(w.rule || w.text || ''), text: String(w.text || w.rule || ''), hint: w.hint ? String(w.hint) : '' })),
    subagents: Array.isArray(d.agents) ? d.agents : Array.isArray(d.subagents) ? d.subagents : [],
  }
}

// --- drawing helpers -------------------------------------------------------

function fit(text, width) {
  const s = String(text)
  if (width <= 0) return ''
  return s.length > width ? s.slice(0, Math.max(0, width - 1)) + '…' : s
}

// One row of styled segments, cut to `width` across segments.
function line(Box, Text, key, segs, width) {
  const children = []
  let room = width
  for (const seg of segs) {
    if (room <= 0 || !seg || !seg.text) continue
    const { text, ...style } = seg
    const shown = fit(text, room)
    room -= shown.length
    children.push(Text({ ...style, wrap: 'truncate-end', children: [shown] }))
  }
  return Box({ key, flexDirection: 'row', children })
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
export function phasesOf(turns) {
  const phases = []
  for (const t of turns) {
    let p = phases[phases.length - 1]
    if (!p || p.group !== t.group) {
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

function nowSegs(r) {
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
  const trend = Array.isArray(t.trend) ? t.trend.slice(0, -1).slice(-4) : []
  const segs = []
  for (const f of trend) {
    segs.push(f == null ? { text: '?', dimColor: true } : f > 0 ? { text: '✗' + f, color: 'red' } : { text: '✓', color: 'green' })
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

// A phase with its tool runs (the newest MAX_TOOL_ROWS) and subagents under it.
function phaseExpanded(Box, Text, width, p, id, live, last) {
  const stem = last ? '  ' : '│ '
  const rows = [line(Box, Text, 'phase-' + id, phaseHead(p, live, last), width)]
  const runs = p.runs.slice(-MAX_TOOL_ROWS)
  const hidden = p.runs.length - runs.length
  const items = []
  if (hidden > 0) items.push({ key: 'more', segs: [{ text: '… ' + hidden + ' earlier', dimColor: true }] })
  runs.forEach((run, i) => items.push({ key: 'tool-' + i, segs: runSegs(run) }))
  const pad = Math.min(16, Math.max(0, ...p.agents.map((a) => String(a.type || a.name || 'agent').length)))
  p.agents.forEach((a, i) => {
    const st = AGENT_STATUS[a.status] || { text: a.status || '', dimColor: true }
    items.push({ key: 'agent-' + i, agent: true, segs: [
      { text: String(a.type || a.name || 'agent').padEnd(pad) },
      a.turns != null && { text: ' · ' + a.turns + (a.turns === 1 ? ' turn' : ' turns'), dimColor: true },
      st.text && { text: ' · ', dimColor: true },
      st.text && { text: st.text, color: st.color, dimColor: st.dimColor },
    ] })
  })
  items.forEach((it, i) => {
    const end = i === items.length - 1
    const glyph = it.agent ? (end ? '└─↳ ' : '├─↳ ') : end ? '└ ' : '├ '
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
  const expanded = phases.map((p, i) => phaseExpanded(Box, Text, width, p, i, i === lastI, i === lastI))
  const collapsed = phases.map((p, i) => phaseCollapsed(Box, Text, width, p, i, i === lastI))
  const live = expanded[lastI].slice(0, Math.max(1, budget - 1))
  const pick = (fromI, foldBelow) => {
    const blocks = []
    for (let i = fromI; i < lastI; i++) blocks.push(i < foldBelow ? collapsed[i] : expanded[i])
    blocks.push(live)
    return blocks
  }
  const size = (blocks, fromI) => 1 + (fromI > 0 ? 1 : 0) + blocks.reduce((n, b) => n + b.length, 0)
  let fold = from
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
    const k = s.id || s.type || s.name
    if (!placed[k]) return true
    placed[k] -= 1
    return false
  })
}

function drawRemote(Box, Text, width) {
  const r = remote
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
  if (r.turns.length) top.push(line(Box, Text, 'flow', [{ text: '── flow ──', dimColor: true }], width))

  const bottom = unplacedAgents(r).map((s, i) => line(Box, Text, 'agent-' + i, [
    { text: '↳ ' + (s.name || s.type || 'agent'), dimColor: true },
    { text: ' · ' + (s.turns || 0) + ' turns · ', dimColor: true },
    { text: s.status || '', color: s.status === 'running' ? 'green' : undefined, dimColor: s.status !== 'running' },
  ], width))
  r.warnings.forEach((w, i) => bottom.push(line(Box, Text, 'warn-' + i, [
    { text: '⚠ ' + w.text, color: 'yellow', bold: true },
    w.hint && { text: ' — ' + w.hint, color: 'yellow' },
  ], width)))
  const tree = flowTree(Box, Text, width, r, Math.max(3, MAX_ROWS - top.length - bottom.length))
  return [...top, ...tree, ...bottom].slice(0, MAX_ROWS)
}

function drawLocal(Box, Text, width) {
  const elapsed = local.startMs != null ? fmtDur((local.nowMs - local.startMs) / 1000) : ''
  const rows = [header(Box, Text, width, 'tokenscope · local', local.working ? 'working' : 'idle', elapsed)]
  const last = local.recent[local.recent.length - 1]
  rows.push(row(Box, Text, 'now', 'NOW', local.working && last ? [{ text: '▶ ' + last, bold: true }] : [{ text: '◼ idle', dimColor: true }], width))
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
  rows.push(line(Box, Text, 'hint', [{ text: 'start `tokenscope live` for the goal and the flow', dimColor: true }], width))
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
    if (res.ok) {
      text = res.text
      next = JSON.parse(text)
      if (!next || typeof next !== 'object') next = null
    }
  } catch {
    next = null
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
  if (!remote) {
    // local mode: the header's clock ticks once a second
    local.nowMs = await $.clock.now()
    dirty = true
  }
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
      description: 'Toggle the tokenscope live pane',
      immediate: true,
    })
    timers.forEach((t) => t.cancel())
    remote = null
    remoteText = ''
    seenRules = null
    local.startMs = local.nowMs = await $.clock.now()
    timers = [$.clock.every(POLL_MS, () => poll($)), $.clock.every(FLUSH_MS, () => flush($))]
    void poll($)
    if ((await $.env.get('TOKENSCOPE_LIVE_PANE')) === '1') {
      void $.ui.open({ id: PANE, title: 'tokenscope' })
    }
    return next(e)
  })

  onEvent('command.run', { command: 'tokenscope' }, async $ => {
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
    local.tools[name] = (local.tools[name] || 0) + 1
    local.calls += 1
    local.working = true
    local.recent = [...local.recent, name].slice(-12)
    if (!remote) dirty = true
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
    const { Box, Text } = $.ui.resolve(e)
    const width = Math.max(1, e.props.bodyColumns || 40)
    const rows = remote ? drawRemote(Box, Text, width) : drawLocal(Box, Text, width)
    return Box({ flexDirection: 'column', children: rows })
  })

  onEvent('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if ((await $.env.get('TOKENSCOPE_LIVE_BAND')) !== '1' || e.props.hasSurvey) return next(e)
    const { Text } = $.ui.resolve(e)
    const width = Math.max(10, e.props.bodyColumns || 40)
    return Text({ dimColor: true, wrap: 'truncate-end', children: [fit(bandText(), width)] })
  })
}
