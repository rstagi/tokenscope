// tokenscope-live: a pane (and optional band) showing what this session is
// doing. With `tokenscope live` running it shows the server's flow of phases,
// flags and subagents; without it, counters this module keeps from the events.
//
// Redraws never come straight from events: polls and local counters set
// `dirty`, and a 250 ms timer flushes it, so a burst of tool calls costs at
// most ~4 redraws a second.

const PANE = 'tokenscope-live'
const DEFAULT_BASE = 'http://127.0.0.1:8765'
const POLL_MS = 1000
const FLUSH_MS = 250
const MAX_PHASES = 8
const MAX_ROWS = 38 // the pane is ~40 rows tall
const MAX_TOOL_ROWS = 3
const RETRY_WINDOW = 3
const SPARK_CHARS = 30
const CTX_LIMIT = 200_000

// What the server last answered (null: local mode) and its text, to spot changes
let remote = null
let remoteText = ''
let inFlight = false
let dirty = false
let timers = []
// Flag keys already seen (null until the first payload, which only seeds it)
let seenFlags = null

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

const FLAG_NAMES = {
  retry_loop: 'retry loop', same_intent_after_error: 'same intent after error', browser_loop: 'browser loop',
  reread_file: 're-read file', dead_tool: 'dead tool', context_large: 'large context', compaction: 'compacted',
}

const STATUS_PILLS = {
  working: { text: '● working', color: 'green' },
  waiting: { text: '◐ waiting', color: 'yellow' },
  idle: { text: '○ idle', color: 'gray' },
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

export function fmtElapsed(s) {
  const v = Math.max(0, Math.floor(Number(s) || 0))
  const h = Math.floor(v / 3600)
  const m = Math.floor((v % 3600) / 60)
  const ss = String(v % 60).padStart(2, '0')
  return h > 0 ? h + ':' + String(m).padStart(2, '0') + ':' + ss : m + ':' + ss
}

function flagText(f) {
  if (typeof f === 'string') return f
  const name = FLAG_NAMES[f.flag] || String(f.flag || '').replace(/_/g, ' ')
  const extra = f.detail != null && f.detail !== '' ? ' · ' + f.detail : f.count > 1 ? ' ×' + f.count : ''
  return name + extra
}

// The server's /compact payload (see docs/live-spec.md) -> the shape the pane draws.
// Every field is optional, and the older shape (goal object, cost_usd, turns, ...) still reads.
function normalize(d) {
  const goal = d.goal && typeof d.goal === 'object' ? d.goal.name : d.goal
  const recent = Array.isArray(d.recent) ? d.recent : Array.isArray(d.turns) ? d.turns : []
  const turns = recent.map((t) => ({
    intent: t.intent,
    group: groupOf(t.intent, t.group),
    cw: Number(t.cw) || 0,
    tools: Array.isArray(t.tools) ? t.tools.map(String) : [],
    toolErr: Array.isArray(t.tool_err) ? t.tool_err : [],
    error: t.err != null ? !!t.err : !!t.error,
    agents: Array.isArray(t.agents) ? t.agents.filter((a) => a && typeof a === 'object') : [],
  }))
  const series = Array.isArray(d.cw_series) ? d.cw_series.map((v) => Number(v) || 0) : turns.map((t) => t.cw)
  return {
    goal,
    status: d.status,
    cw: d.cw,
    cost_usd: typeof d.usd === 'number' ? d.usd : d.cost_usd,
    ctx_tokens: d.context_tokens != null ? d.context_tokens : d.ctx_tokens,
    elapsed_s: d.elapsed_s,
    turnCount: typeof d.turns === 'number' ? d.turns : null,
    errors: typeof d.errors === 'number' ? d.errors : turns.filter((t) => t.error).length,
    turns,
    series,
    flags: (Array.isArray(d.flags) ? d.flags : []).map((f) => ({
      key: typeof f === 'string' ? f : String(f.flag || ''),
      text: flagText(f),
    })),
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

// Consecutive turns with the same group, folded into phases. A phase whose group
// already failed within the last RETRY_WINDOW phases is a retry (`retry` counts
// them per group), and a tool failing again after failing in an earlier phase is
// marked `again`.
export function phasesOf(turns) {
  const phases = []
  for (const t of turns) {
    let p = phases[phases.length - 1]
    if (!p || p.group !== t.group) {
      phases.push((p = { group: t.group, turns: 0, cw: 0, error: false, retry: 0, tools: new Map(), agents: [] }))
    }
    p.turns += 1
    p.cw += t.cw
    p.error = p.error || t.error
    p.agents.push(...t.agents)
    t.tools.forEach((name, i) => {
      const agg = p.tools.get(name) || { n: 0, err: 0, again: false }
      agg.n += 1
      if (t.toolErr[i]) agg.err += 1
      p.tools.set(name, agg)
    })
  }
  const retries = {}
  const failed = new Set()
  phases.forEach((p, i) => {
    const window = phases.slice(Math.max(0, i - RETRY_WINDOW), i)
    if (window.some((q) => q.group === p.group && q.error)) p.retry = retries[p.group] = (retries[p.group] || 0) + 1
    for (const [name, agg] of p.tools) if (agg.err > 0 && failed.has(name)) agg.again = true
    for (const [name, agg] of p.tools) if (agg.err > 0) failed.add(name)
  })
  return phases
}

function header(Box, Text, width, label, status, elapsed) {
  const pill = STATUS_PILLS[status] || (status ? { text: '○ ' + status, color: 'gray' } : null)
  const right = [pill && pill.text, elapsed].filter(Boolean).join('  ')
  const left = fit(label, Math.max(1, width - right.length - 2))
  const gap = ' '.repeat(Math.max(1, width - left.length - right.length))
  return line(Box, Text, 'head', [
    { text: left, dimColor: true },
    { text: gap },
    pill && { text: pill.text, color: pill.color, bold: true },
    elapsed && { text: (pill ? '  ' : '') + elapsed, dimColor: true },
  ], width)
}

function metrics(Box, Text, width, text, series) {
  const rows = []
  const values = cumulative(series, SPARK_CHARS * 2)
  const room = Math.min(SPARK_CHARS, width - text.length - 2)
  if (values.length > 1 && room >= 8) {
    rows.push(line(Box, Text, 'metrics', [{ text }, { text: '  ' }, { text: sparkline(values, room), color: 'cyan' }], width))
  } else {
    rows.push(line(Box, Text, 'metrics', [{ text }], width))
    if (values.length > 1) {
      rows.push(line(Box, Text, 'spark', [{ text: sparkline(values, Math.min(SPARK_CHARS, width)), color: 'cyan' }], width))
    }
  }
  return rows
}

function gauge(Box, Text, width, ctx) {
  if (ctx == null) return []
  const ratio = Math.max(0, Math.min(1, (Number(ctx) || 0) / CTX_LIMIT))
  const pct = Math.round(ratio * 100) + '%'
  const cells = Math.max(4, Math.min(24, width - 'ctx ▕▏ 100%'.length))
  const full = Math.round(ratio * cells)
  const color = ratio > 0.75 ? 'red' : ratio > 0.5 ? 'yellow' : 'green'
  return [line(Box, Text, 'gauge', [
    { text: 'ctx ', dimColor: true },
    { text: '▕', dimColor: true },
    { text: '█'.repeat(full), color },
    { text: '░'.repeat(cells - full), dimColor: true },
    { text: '▏ ', dimColor: true },
    { text: pct, color, bold: ratio > 0.75 },
  ], width)]
}

const AGENT_STATUS = {
  running: { text: '● running', color: 'green' },
  done: { text: '✓ done', dimColor: true },
  completed: { text: '✓ done', dimColor: true },
  error: { text: '✗ error', color: 'red' },
  failed: { text: '✗ error', color: 'red' },
  starting: { text: '○ starting', dimColor: true },
}

function chipSegs(name, agg) {
  return [
    { text: name + (agg.n > 1 ? ' ×' + agg.n : '') },
    agg.err > 0 && { text: ' ✗' + agg.err, color: 'red' },
    agg.again && { text: ' ↺', color: 'yellow', bold: true },
  ].filter(Boolean)
}

const segsLen = (segs) => segs.reduce((n, seg) => n + seg.text.length, 0)

// The rows under one phase: its tools (one line when they fit, else one per tool
// group), then the subagents it launched.
function phaseRows(Box, Text, width, p, id, last) {
  const rows = []
  const stem = last ? '   ' : '│  '
  const stats = ' · ' + p.turns + (p.turns === 1 ? ' turn' : ' turns') + (p.cw > 0 ? ' · ' + fmtTokens(p.cw) : '')
  rows.push(line(Box, Text, 'phase-' + id, [
    { text: last ? '└─ ' : '├─ ', dimColor: true },
    p.retry > 0 && { text: '↺ ', color: 'yellow', bold: true },
    { text: p.group.toUpperCase(), color: GROUP_COLORS[p.group], bold: true },
    { text: stats, bold: last, dimColor: !last },
    p.error && { text: ' ✗', color: 'red', bold: true },
    p.retry > 0 && { text: ' retry ' + p.retry, color: 'yellow' },
    last && { text: '  ◀ now', bold: true },
  ], width))
  const chips = [...p.tools].map(([name, agg]) => chipSegs(name, agg))
  const agents = p.agents
  if (chips.length > 0) {
    const glyph = (isLast) => (isLast && !agents.length ? '└ ' : '├ ')
    const one = chips.flatMap((c, i) => (i ? [{ ...c[0], text: ' · ' + c[0].text }, ...c.slice(1)] : c))
    if (chips.length === 1 || stem.length + 2 + segsLen(one) <= width) {
      rows.push(line(Box, Text, 'tools-' + id, [{ text: stem + glyph(true), dimColor: true }, ...one], width))
    } else {
      const shown = chips.length > MAX_TOOL_ROWS ? chips.slice(0, MAX_TOOL_ROWS) : chips
      const more = chips.length - shown.length
      shown.forEach((c, i) => {
        const isLast = i === shown.length - 1 && !more
        rows.push(line(Box, Text, 'tools-' + id + '-' + i, [{ text: stem + glyph(isLast), dimColor: true }, ...c], width))
      })
      if (more) rows.push(line(Box, Text, 'tools-' + id + '-more', [{ text: stem + glyph(true) + '+' + more + ' more', dimColor: true }], width))
    }
  }
  const pad = Math.min(16, Math.max(0, ...agents.map((a) => String(a.type || a.name || 'agent').length)))
  agents.forEach((a, i) => {
    const st = AGENT_STATUS[a.status] || { text: a.status ? '○ ' + a.status : '', dimColor: true }
    const stats = (a.turns != null ? ' · ' + a.turns + (a.turns === 1 ? ' turn' : ' turns') : '') + (a.cw ? ' · ' + fmtTokens(a.cw) : '')
    rows.push(line(Box, Text, 'agent-' + id + '-' + i, [
      { text: stem + (i === agents.length - 1 ? '└─↳ ' : '├─↳ '), dimColor: true },
      { text: String(a.type || a.name || 'agent').padEnd(pad) + ' ' },
      { text: st.text, color: st.color, dimColor: st.dimColor },
      { text: stats, dimColor: true },
    ], width))
  })
  return rows
}

// The goal as root, then as many of the newest phases as fit in `budget` rows.
function flowTree(Box, Text, width, r, budget) {
  const phases = phasesOf(r.turns)
  if (!phases.length) return []
  const root = line(Box, Text, 'root', [{ text: '◆ ' + (r.goal || 'session'), bold: true }], width)
  const blocks = []
  let used = 1
  for (let i = phases.length - 1; i >= 0 && blocks.length < MAX_PHASES; i--) {
    const rows = phaseRows(Box, Text, width, phases[i], i, i === phases.length - 1)
    const cutRow = i > 0 ? 1 : 0
    if (blocks.length && used + rows.length + cutRow > budget) break
    blocks.unshift(blocks.length ? rows : rows.slice(0, Math.max(1, budget - 1 - cutRow)))
    used += blocks[0].length
  }
  const cut = phases.length - blocks.length
  const out = [root]
  if (cut > 0) out.push(line(Box, Text, 'cut', [{ text: '┊ +' + cut + ' earlier phase' + (cut === 1 ? '' : 's'), dimColor: true }], width))
  for (const rows of blocks) out.push(...rows)
  return out
}

function badges(Box, Text, width, flags) {
  const rows = []
  let segs = []
  let used = 0
  const flush = () => {
    if (segs.length) rows.push(line(Box, Text, 'flags-' + rows.length, segs, width))
    segs = []
    used = 0
  }
  for (const f of flags) {
    const text = ' ⚠ ' + f.text + ' '
    if (used > 0 && used + 1 + text.length > width) flush()
    if (used > 0) segs.push({ text: ' ' }), (used += 1)
    segs.push({ text, color: 'yellow', inverse: true, bold: true })
    used += text.length
  }
  flush()
  return rows
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
  const top = [header(Box, Text, width, 'tokenscope', r.status, r.elapsed_s != null ? fmtElapsed(r.elapsed_s) : '')]
  const parts = ['CW ' + fmtTokens(r.cw)]
  if (typeof r.cost_usd === 'number') parts.push('$' + r.cost_usd.toFixed(2))
  if (r.ctx_tokens != null) parts.push('ctx ' + fmtTokens(r.ctx_tokens))
  if (r.turnCount != null) parts.push(r.turnCount + ' turns')
  if (r.errors > 0) parts.push(r.errors + ' ✗')
  top.push(...metrics(Box, Text, width, parts.join(' · '), r.series))
  top.push(...gauge(Box, Text, width, r.ctx_tokens))
  const bottom = badges(Box, Text, width, r.flags)
  unplacedAgents(r).forEach((s, i) => {
    const turns = ' · ' + (s.turns || 0) + ' turns'
    bottom.push(line(Box, Text, 'agent-' + i, [
      { text: '↳ ' + (s.name || s.type || 'agent') + ' ', dimColor: true },
      { text: s.status || '', color: s.status === 'running' ? 'green' : 'gray' },
      { text: turns, dimColor: true },
    ], width))
  })
  const tree = flowTree(Box, Text, width, r, Math.max(3, MAX_ROWS - top.length - bottom.length))
  return [...top, ...tree, ...bottom].slice(0, MAX_ROWS)
}

function drawLocal(Box, Text, width) {
  const elapsed = local.startMs != null ? fmtElapsed((local.nowMs - local.startMs) / 1000) : ''
  const rows = [header(Box, Text, width, 'tokenscope · local', local.working ? 'working' : 'idle', elapsed)]
  const text = 'CW ' + fmtTokens(local.cw) + ' · ctx ' + fmtTokens(local.ctx) + ' · ' + local.turns + ' turns · ' + local.calls + ' calls'
  rows.push(...metrics(Box, Text, width, text, local.series))
  if (local.ctx > 0) rows.push(...gauge(Box, Text, width, local.ctx))
  const top = Object.entries(local.tools)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([name, n]) => name + ' ×' + n)
    .join(' · ')
  if (top) rows.push(line(Box, Text, 'tools', [{ text: top }], width))
  if (local.recent.length > 0) {
    rows.push(line(Box, Text, 'recent', [{ text: 'recent: ' + local.recent.slice().reverse().join(', '), dimColor: true }], width))
  }
  rows.push(line(Box, Text, 'hint', [{ text: 'start `tokenscope live` for the flow tree and flags', dimColor: true }], width))
  return rows
}

function summaryLine() {
  if (remote) {
    const parts = ['CW ' + fmtTokens(remote.cw)]
    if (typeof remote.cost_usd === 'number') parts.push('$' + remote.cost_usd.toFixed(2))
    if (remote.ctx_tokens != null) parts.push('ctx ' + fmtTokens(remote.ctx_tokens))
    return parts.join(' · ')
  }
  return 'CW ' + fmtTokens(local.cw) + ' · ctx ' + fmtTokens(local.ctx) + ' · ' + local.turns + ' turns · ' + local.calls + ' calls'
}

// --- polling and redraws ---------------------------------------------------

function toastNewFlags($, flags) {
  const fresh = seenFlags ? flags.filter((f) => !seenFlags.has(f.key)) : []
  seenFlags = new Set(flags.map((f) => f.key))
  if (fresh.length) $.ui.toast(fresh.map((f) => '⚠ ' + f.text).join('  '))
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
    if (remote) toastNewFlags($, remote.flags)
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
    seenFlags = null
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
    const lead = remote ? (remote.goal || 'session') + ' · ' : 'tokenscope (local) · '
    return Text({ dimColor: true, wrap: 'truncate-end', children: [fit(lead + summaryLine(), width)] })
  })
}
