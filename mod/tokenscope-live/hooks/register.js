// tokenscope-live: a pane (and optional band) showing what this session is
// doing. With `tokenscope live` running it shows the server's intents, flags
// and subagents; without it, counters this module keeps from the events.

const PANE = 'tokenscope-live'
const DEFAULT_BASE = 'http://127.0.0.1:8765'
const MAX_ROWS = 8

// What the server last answered (null: local mode) and its text, to spot changes
let remote = null

// The server's /compact payload (see docs/live-spec.md) -> the shape the pane draws.
const FLAG_NAMES = {
  retry_loop: 'retry loop', same_intent_after_error: 'same intent after error', browser_loop: 'browser loop',
  reread_file: 're-read file', dead_tool: 'dead tool', context_large: 'large context', compaction: 'compacted',
}
function normalize(d) {
  const goal = d.goal && typeof d.goal === 'object' ? d.goal.name : d.goal
  const flags = (Array.isArray(d.flags) ? d.flags : []).map((f) => {
    if (typeof f === 'string') return f
    const name = FLAG_NAMES[f.flag] || String(f.flag || '').replace(/_/g, ' ')
    const extra = f.detail != null && f.detail !== '' ? ' · ' + f.detail : f.count > 1 ? ' ×' + f.count : ''
    return { text: name + extra }
  })
  return {
    goal,
    status: d.status,
    cw: d.cw,
    cost_usd: typeof d.usd === 'number' ? d.usd : d.cost_usd,
    ctx_tokens: d.context_tokens != null ? d.context_tokens : d.ctx_tokens,
    turns: (Array.isArray(d.recent) ? d.recent : Array.isArray(d.turns) ? d.turns : []).map((t) => ({
      intent: t.intent, group: t.group, tools: t.tools, error: t.err != null ? !!t.err : !!t.error,
    })),
    flags,
    subagents: Array.isArray(d.agents) ? d.agents : Array.isArray(d.subagents) ? d.subagents : [],
  }
}
let remoteText = ''
let timer = null
let inFlight = false

// Local fallback counters
const local = { tools: {}, recent: [], calls: 0, turns: 0, cw: 0, ctx: 0 }

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

function fit(text, width) {
  const s = String(text)
  if (width <= 0) return ''
  return s.length > width ? s.slice(0, Math.max(0, width - 1)) + '…' : s
}

function summaryLine() {
  if (remote) {
    const parts = ['CW ' + fmtTokens(remote.cw)]
    if (typeof remote.cost_usd === 'number') parts.push('$' + remote.cost_usd.toFixed(2))
    if (remote.ctx_tokens != null) parts.push('ctx ' + fmtTokens(remote.ctx_tokens))
    return parts.join(' · ')
  }
  return (
    'CW ' + fmtTokens(local.cw) + ' · ctx ' + fmtTokens(local.ctx) +
    ' · ' + local.turns + ' turns · ' + local.calls + ' calls'
  )
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
    $.ui.invalidate('ui.render')
  }
}

function drawRemote(Box, Text, width) {
  const r = remote
  const rows = []
  const status = r.status ? ' · ' + r.status : ''
  rows.push(
    Box({
      key: 'head',
      flexDirection: 'row',
      children: [
        Text({ bold: true, wrap: 'truncate-end', children: [fit(r.goal || 'session', width - status.length)] }),
        Text({ dimColor: true, children: [status] }),
      ],
    }),
  )
  rows.push(Text({ wrap: 'truncate-end', children: [fit(summaryLine(), width)] }))

  const turns = Array.isArray(r.turns) ? r.turns : []
  if (turns.length > 0) {
    const current = turns[turns.length - 1]
    const curGroup = groupOf(current.intent, current.group)
    const legend = ' ' + (current.intent || curGroup)
    const room = Math.max(1, width - legend.length)
    const blocks = turns.slice(-room).map((t, i) =>
      Text({ color: GROUP_COLORS[groupOf(t.intent, t.group)], children: ['█'] }),
    )
    rows.push(
      Box({
        key: 'strip',
        flexDirection: 'row',
        children: [
          ...blocks,
          Text({ color: GROUP_COLORS[curGroup], children: [fit(legend, width - blocks.length)] }),
        ],
      }),
    )
    turns.slice(-MAX_ROWS).reverse().forEach((t, i) => {
      const tools = Array.isArray(t.tools) ? t.tools.join(', ') : ''
      const mark = t.error ? ' ✗' : ''
      rows.push(
        Text({
          color: t.error ? 'red' : undefined,
          wrap: 'truncate-end',
          children: [fit((t.intent || groupOf(t.intent, t.group)) + '  · ' + tools + mark, width)],
        }),
      )
    })
  }

  const flags = Array.isArray(r.flags) ? r.flags : []
  flags.forEach((f, i) => {
    const text = typeof f === 'string' ? f : f && f.text ? f.text : ''
    if (text) rows.push(Text({ color: 'yellow', wrap: 'truncate-end', children: [fit('⚠ ' + text, width)] }))
  })

  const subs = Array.isArray(r.subagents) ? r.subagents : []
  subs.forEach((s, i) => {
    const line = '↳ ' + (s.name || s.type || 'agent') + ' ' + (s.status || '') + ' · ' + (s.turns || 0) + ' turns'
    rows.push(Text({ dimColor: true, wrap: 'truncate-end', children: [fit(line, width)] }))
  })
  return rows
}

function drawLocal(Box, Text, width) {
  const rows = []
  rows.push(
    Box({
      key: 'head',
      flexDirection: 'row',
      children: [
        Text({ bold: true, children: ['tokenscope'] }),
        Text({ dimColor: true, children: [fit(' · local mode', width - 10)] }),
      ],
    }),
  )
  rows.push(Text({ wrap: 'truncate-end', children: [fit(summaryLine(), width)] }))
  const top = Object.entries(local.tools)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([name, n]) => name + ' ×' + n)
    .join(', ')
  if (top) rows.push(Text({ wrap: 'truncate-end', children: [fit(top, width)] }))
  if (local.recent.length > 0) {
    rows.push(
      Text({ dimColor: true, wrap: 'truncate-end', children: [fit('recent: ' + local.recent.slice().reverse().join(', '), width)] }),
    )
  }
  rows.push(
    Text({ dimColor: true, wrap: 'truncate-end', children: [fit('start `tokenscope live` for intents and flags', width)] }),
  )
  return rows
}

export function register(onEvent) {
  onEvent('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'tokenscope',
      description: 'Toggle the tokenscope live pane',
      immediate: true,
    })
    if (timer) timer.cancel()
    timer = $.clock.every(1000, () => poll($))
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
    local.recent = [...local.recent, name].slice(-12)
    if (!remote) $.ui.invalidate('ui.render')
    return next(e)
  }).catch(async ($, e, next) => next(e)) // counting must never block a tool call

  onEvent('turn.step', async function* ($, e, next) {
    const result = yield* next(e)
    const usage = result && result.usage
    if (usage) {
      local.cw += costWeighted(usage)
      local.ctx =
        (usage.input_tokens || 0) +
        (usage.cache_read_input_tokens || 0) +
        (usage.cache_creation_input_tokens || 0)
      if (!remote) $.ui.invalidate('ui.render')
    }
    return result
  })

  onEvent('turn.complete', async ($, e, next) => {
    local.turns += 1
    if (!remote) $.ui.invalidate('ui.render')
    return next(e)
  })

  onEvent('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const width = Math.max(10, e.props.bodyColumns || 40)
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
