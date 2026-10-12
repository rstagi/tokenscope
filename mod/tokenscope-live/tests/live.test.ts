import { describe, expect, mock, test } from 'claude-code/testing'

const PANE_PROPS = {
  title: 'tokenscope',
  isFocused: false,
  bodyColumns: 60,
  placement: 'dock',
  scroll: { offset: 0, bodyRows: 20 },
  view: {},
} as const

const SESSION = { cwd: '/tmp', surface: null, isInteractive: true }

const COMPACT = {
  // shape of GET /api/session/<id>/compact (docs/live-spec.md)
  goal: 'Fix: failing tests in invoice',
  status: 'working',
  cw: 182_000,
  usd: 0.41,
  context_tokens: 68_000,
  elapsed_s: 192,
  turns: 9,
  tool_calls: 14,
  errors: 1,
  current_intent: 'run_tests',
  asked: { goal: 'Fix: failing tests in invoice', source: 'keyword', prompt_n: 2, latest: 'also check tax rounding', latest_ts: 1 },
  now: { intent: 'run_tests', label: 'running tests', group: 'verify', tool: 'Bash run_tests.sh', target: 'run_tests.sh', since_s: 4, status: 'working' },
  tests: { passed: 12, failed: 0, ok: true, runs: 3, last_ts: 1, trend: [4, 2, 0] },
  files_edited: ['invoice.py', 'money.py', 'tax.py'],
  files_edited_n: 3,
  warnings: [],
  recent: [
    { intent: 'read_code', group: 'investigate', label: 'reading code', cw: 40_000, tools: ['Read', 'Read'], targets: ['money.py', 'tax.py'], tool_err: [false, false], err: false },
    { intent: 'read_code', group: 'investigate', label: 'reading code', cw: 80_000, tools: ['Read', 'Read', 'Grep', 'Grep'], targets: ['money.py', 'tax.py', 'round', 'Decimal'], tool_err: [false, false, false, false], err: false },
    { intent: 'search_code', group: 'investigate', label: 'searching code', cw: 8_000, tools: [], targets: [], tool_err: [], err: false },
    { intent: 'edit', group: 'change', label: 'editing files', cw: 18_000, tools: ['Edit'], targets: ['money.py'], tool_err: [false], err: false },
    { intent: 'run_tests', group: 'verify', label: 'running tests', cw: 15_000, tools: ['Bash run_tests.sh'], targets: ['run_tests.sh'], tool_err: [true], err: true },
  ],
  cw_series: [12_000, 9_000, 30_000, 18_000, 15_000, 15_000, 20_000, 8_000],
  agents: [],
}

const WARNING = { rule: 'stuck_error', text: 'same error ×3, no edits since', hint: 'try a different approach', count: 3, ts: 1 }

const OLD_SHAPE = {
  goal: { name: 'Fix login bug', source: 'keyword' },
  status: 'idle',
  cw: 1_234_000,
  cost_usd: 3.1,
  ctx_tokens: 160_000,
  turns: [{ intent: 'verify', tools: ['Bash pytest'], error: true }],
}

function base(on: any, env: Record<string, string> = {}) {
  mock.env(on, env)
  const clock = mock.clock(on)
  on('session.id', () => ({ value: 'sess-1' }))
  on('command.register', () => ({ value: undefined }))
  on('session.start', (_$: any, e: any) => e)
  return clock
}

describe('tokenscope-live', () => {
  test('local fallback draws header, metrics and sparkline from local per-turn CW', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ deny: 'connection refused' }))
    on('tool.call', () => ({ result: 'ok' }))
    on('turn.step', async function* (_$: any, e: any) {
      return {
        turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn',
        usage: { model: 'm', input_tokens: 1000, output_tokens: 100, cache_read_input_tokens: 10000, cache_creation_input_tokens: 2000 },
      }
    })
    on('turn.complete', () => ({ text: '' }))

    await $.session.start(SESSION)
    await $.tool.call({ tool: 'Bash', tool_use_id: 'a', input: { command: 'ls' } })
    await $.tool.call({ tool: 'Read', tool_use_id: 'b', input: { file_path: '/x' } })
    await $.tool.call({ tool: 'Bash', tool_use_id: 'c', input: { command: 'pwd' } })
    for (const id of ['t1', 't2']) {
      for await (const _ of $.turn.step({ turnId: id, index: 0, model: 'm', messageCount: 1 })) {
        // drain
      }
      await $.turn.complete({ turnId: id, answer: '' })
    }
    await clock.advance(65_000)

    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(await ui.find({ type: 'Text', text: /^tokenscope · local$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}● idle$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}1m[56]s$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^◼ idle$/ })).toBeDefined()
    // CW per turn = 1000 + 1.25*2000 + 0.1*10000 + 5*100 = 5000
    expect(await ui.find({ type: 'Text', text: /^10k CW$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^7%$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^2 turns · 3 calls$/ })).toBeDefined()
    // cumulative 5k, 10k: one braille char, left column 2 dots, right column 4
    expect(await ui.find({ type: 'Text', text: /^\u28fc$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Bash ×2 · Read ×1' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'recent: Bash, Read, Bash' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /tokenscope live/ })).toBeDefined()
  })

  test('pane draws ASKED, NOW, TESTS, FILES, COST and the flow tree', async ($: any, on: any) => {
    const clock = base(on, { TOKENSCOPE_LIVE_URL: 'http://127.0.0.1:9999' })
    const urls: string[] = []
    on('http.fetch', (_$: any, e: any) => {
      urls.push(e.url)
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(COMPACT) } }
    })
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns: 70 } })
    expect(urls[0]).toBe('http://127.0.0.1:9999/api/session/sess-1/compact')
    expect(await ui.find({ type: 'Text', text: /^tokenscope$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}● working$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}3m12s$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ASKED {2}$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Fix: failing tests in invoice$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /haiku/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /^↳ now: also check tax rounding$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^NOW {4}$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^▶ running tests$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · run_tests\.sh$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · 4s$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^TESTS {2}$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^✗4$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^✗2$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^✓ 12 passed$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^edited 3: invoice\.py, money\.py, tax\.py$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^COST {3}$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^[⠀-⣿]{4}$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^182k CW · \$0\.41$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^34%$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^── flow ──$/ })).toBeDefined()

    // flow: INVESTIGATE (3 turns, dominant "reading code") -> CHANGE -> VERIFY (live)
    expect(await ui.find({ type: 'Text', text: /^◆ Fix: failing tests in invoice$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^INVESTIGATE$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · reading code$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · 3 turns$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^│ ├ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ money\.py, tax\.py$/ })).toBeDefined()
    expect(await ui.findAll({ type: 'Text', text: /^ ×4$/ })).toHaveLength(1)
    expect(await ui.find({ type: 'Text', text: /^ round, Decimal$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · editing files$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^└ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · ▶ running tests$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}└ $/ })).toBeDefined()
    // the Bash target is already in its label; the error is a dim ✗1, nothing louder
    expect(await ui.find({ type: 'Text', text: /^Bash run_tests\.sh$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ ✗1$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /retry|↺/ })).toBeUndefined()
    // zero warnings: no ⚠ anywhere
    expect(await ui.find({ type: 'Text', text: /⚠/ })).toBeUndefined()
  })

  test('waiting and idle states; haiku goal; one prompt has no latest row; no tests, no files', async ($: any, on: any) => {
    const clock = base(on)
    let payload: any = { ...COMPACT, status: 'waiting', asked: { goal: 'Tidy the README', source: 'haiku', prompt_n: 1, latest: null }, tests: null, files_edited: [], files_edited_n: 0 }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns: 70 } })
    expect(await ui.find({ type: 'Text', text: /^ {2}● waiting for you$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^◼ done, waiting for you$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ \(haiku\)$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /↳ now/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /^TESTS/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /^FILES/ })).toBeUndefined()

    payload = { ...COMPACT, status: 'idle', files_edited: undefined, files_edited_n: 5 }
    await clock.advance(1000)
    await ui.unmount()
    ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(await ui.find({ type: 'Text', text: /^ {2}● idle$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^◼ idle$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^edited 5 files$/ })).toBeDefined()
  })

  test('a warning renders one line with its hint; a new rule toasts once', async ($: any, on: any) => {
    const clock = base(on)
    let payload: any = COMPACT
    const toasts: string[] = []
    let redraws = 0
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    on('ui.toast', (_$: any, e: any) => { toasts.push(e.text); return { value: undefined } })
    on('ui.invalidate', () => { redraws += 1; return { value: undefined } })
    await $.session.start(SESSION)
    await clock.advance(1000)
    const before = redraws
    await clock.advance(3000)
    expect(redraws).toBe(before) // unchanged payloads do not redraw

    payload = { ...COMPACT, warnings: [WARNING] }
    await clock.advance(1000)
    await clock.advance(1000)
    expect(toasts).toEqual(['⚠ same error ×3, no edits since'])
    expect(redraws).toBe(before + 1)
    payload = { ...COMPACT, warnings: [{ ...WARNING, count: 4 }] }
    await clock.advance(1000)
    expect(toasts).toHaveLength(1) // same rule: no second toast
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns: 70 } })
    expect(await ui.find({ type: 'Text', text: /^⚠ same error ×3, no edits since$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ — try a different approach$/ })).toBeDefined()
    expect(await ui.findAll({ type: 'Text', text: /⚠/ })).toHaveLength(1)
  })

  test('subagents hang under the phase that launched them', async ($: any, on: any) => {
    const clock = base(on)
    const payload = {
      goal: 'Fix invoice rounding', status: 'working',
      recent: [
        { group: 'investigate', label: 'reading code', tools: ['Read', 'Read', 'Grep'] },
        { group: 'delegate', label: 'delegating', tools: ['Agent(Explore)', 'Agent(general-purpose)'], agents: [
          { type: 'Explore', status: 'running', turns: 6, cw: 40_000 },
          { type: 'general-purpose', status: 'done', turns: 3, cw: 22_000 },
        ] },
        { group: 'verify', label: 'running tests', tools: ['Bash run_tests.sh'] },
      ],
      agents: [{ type: 'Explore', status: 'running', turns: 6 }, { type: 'general-purpose', status: 'done', turns: 3 }],
    }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns: 70 } })
    expect(await ui.find({ type: 'Text', text: /^Agent\(Explore\)$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^│ ├─↳ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Explore {8}$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · 6 turns$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^running$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^│ └─↳ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^general-purpose$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^done$/ })).toBeDefined()
    // all payload agents are in the tree: no separate rows at the bottom
    expect(await ui.find({ type: 'Text', text: /^↳ / })).toBeUndefined()
  })

  test('phases group by intent group and are named by their dominant intent (ties -> latest)', async ($: any, on: any) => {
    const clock = base(on)
    const payload = {
      goal: 'g', status: 'working',
      recent: [
        { group: 'change', label: 'editing files', tools: ['Edit'] },
        { group: 'change', label: 'writing files', tools: ['Write'] },
        { group: 'verify', label: 'linting', tools: ['Bash ruff'] },
      ],
    }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns: 70 } })
    expect(await ui.findAll({ type: 'Text', text: /^(CHANGE|VERIFY)$/ })).toHaveLength(2)
    expect(await ui.find({ type: 'Text', text: /^ · writing files$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · ▶ linting$/ })).toBeDefined()
  })

  test('older phases collapse to one line, then the oldest drop; the pane stays within 38 rows', async ($: any, on: any) => {
    const clock = base(on)
    const cycle = ['investigate', 'change', 'verify', 'plan', 'research', 'ship', 'delegate', 'wait', 'other']
    const groups = [...cycle, ...cycle, ...cycle, ...cycle, 'change', 'verify', 'investigate', 'change']   // more than fit
    const tools = ['Read', 'Grep', 'Glob', 'WebFetch', 'Bash some-long-command.sh', 'Edit']
    const payload = { goal: 'g', status: 'working', recent: groups.map(group => ({ group, label: group + 'ing', tools, targets: ['a', 'b', 'c', 'd', 'e', 'f'] })) }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    const boxes = await ui.findAll({ type: 'Box' })
    expect(boxes.length - 1).toBeLessThanOrEqual(38)
    expect(await ui.find({ type: 'Text', text: /^… \d+ earlier phases$/ })).toBeDefined()
    // collapsed: tool totals on the phase line
    expect(await ui.find({ type: 'Text', text: /^ · Read Grep Glob WebFetch/ })).toBeDefined()
    // the live phase is expanded: its newest 4 runs and a "… 2 earlier" row
    expect(await ui.find({ type: 'Text', text: /^ · ▶ changeing$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^… 2 earlier$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}└ $/ })).toBeDefined()
  })

  test('the old payload shape still draws', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(OLD_SHAPE) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(await ui.find({ type: 'Text', text: /^Fix login bug$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^◆ Fix login bug$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}● idle$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^1\.2M CW · \$3\.10$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^80%$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^VERIFY$/ })).toBeDefined()
  })

  test('NOW follows a tool call the transcript has not caught up with yet', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, tool_calls: 2 }) } }))
    on('tool.call', () => ({ result: 'ok' }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    await $.tool.call({ tool: 'Read', tool_use_id: 'a', input: { file_path: '/r/money.py' } })
    await $.tool.call({ tool: 'Edit', tool_use_id: 'b', input: { file_path: '/r/money.py' } })
    await clock.advance(1000)
    let ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(await ui.find({ type: 'Text', text: /^▶ running tests$/ })).toBeDefined()   // server already has both calls
    await $.tool.call({ tool: 'Write', tool_use_id: 'c', input: { file_path: '/r/textkit/stats.py' } })
    await clock.advance(1000)
    await ui.unmount()
    ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(await ui.find({ type: 'Text', text: /^▶ editing files$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /stats\.py/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Write/ })).toBeDefined()   // provisional tree line for the pending call
  })

  test('parallel subagents: named rows, a nested child, NOW counts them, their calls stay out of the main tree', async ($: any, on: any) => {
    const clock = base(on)
    const agents = [
      { id: 'a1', type: 'general-purpose', description: 'Audit store.py', status: 'done', turns: 5, children: [] },
      { id: 'a2', type: 'general-purpose', description: 'Audit cli.py', status: 'running', turns: 3,
        children: [{ id: 'c1', type: 'Explore', description: 'Map cli calls', status: 'running', turns: 2, parent: 'a2' }] },
    ]
    const payload = {
      ...COMPACT, main_tool_calls: 2, tests: null,
      recent: [{ intent: 'delegate', group: 'delegate', label: 'delegating to a subagent', cw: 9000,
                 tools: ['Agent(general-purpose)', 'Agent(general-purpose)'], targets: ['Audit store.py', 'Audit cli.py'],
                 tool_err: [false, false], err: false, agents }],
      agents: [...agents.map((a) => ({ ...a, children: undefined })), { id: 'c1', type: 'Explore', description: 'Map cli calls', status: 'running', turns: 2, parent: 'a2' }],
      now: { intent: 'delegate', label: 'delegating to a subagent', group: 'delegate', tool: 'Agent(general-purpose)', target: 'Audit cli.py', since_s: 9, status: 'working' },
    }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    on('tool.call', () => ({ result: 'ok' }))
    await $.session.start(SESSION)
    await $.tool.call({ tool: 'Agent', tool_use_id: 'm1', input: { subagent_type: 'general-purpose' } })
    await $.tool.call({ tool: 'Agent', tool_use_id: 'm2', input: { subagent_type: 'general-purpose' } })
    await $.tool.call({ tool: 'Read', tool_use_id: 's1', agentId: 'a2', input: { file_path: '/r/cli.py' } })
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns: 70 } })
    expect(await ui.find({ type: 'Text', text: /^▶ 2 subagents working$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /reading code cli\.py/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Audit store\.py/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Map cli calls/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /└─↳ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ \(Explore\)$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Read/ })).toBeUndefined()   // the subagent's Read is not a main tree line
    expect(await ui.find({ type: 'Text', text: /↳ Map cli calls/ })).toBeUndefined()   // not repeated at the bottom
  })

  test('a stale live call does not override the waiting state', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, status: 'waiting', main_tool_calls: 0 }) } }))
    on('tool.call', () => ({ result: 'ok' }))
    await $.session.start(SESSION)
    await $.tool.call({ tool: 'Bash', tool_use_id: 'b', input: { command: 'python3 -m unittest' } })
    await clock.advance(25_000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(await ui.find({ type: 'Text', text: /^◼ done, waiting for you$/ })).toBeDefined()
  })

  test('an edit-and-test loop reads as iterations; passed counts in TESTS; no ▶ once waiting', async ($: any, on: any) => {
    const clock = base(on)
    const turn = (tools: string[], targets: any[], errs: boolean[]) => ({ intent: 'edit', group: 'change', label: 'editing files', cw: 5000, tools, targets, tool_err: errs, err: errs.some(Boolean), agents: [] })
    const payload = {
      ...COMPACT, status: 'waiting',
      tests: { passed: 46, failed: 0, ok: true, runs: 3, last_ts: 1, trend: [0, 2, 0], passed_trend: [12, 18, 46] },
      recent: [
        turn(['Edit', 'Edit'], ['models.py', 'store.py'], [false, false]),
        turn(['Edit', 'Bash unittest'], ['store.py', 'unittest'], [false, false]),
        turn(['Write', 'Bash unittest'], ['test_history.py', 'unittest'], [false, true]),
        turn(['Edit', 'Bash unittest'], ['history.py', 'unittest'], [false, false]),
      ],
    }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns: 70 } })
    const heads = await ui.findAll({ type: 'Text', text: /^CHANGE$/ })
    expect(heads.length).toBe(3)                       // three iterations, each closed by a test run
    expect((await ui.findAll({ type: 'Text', text: /^✓$/ })).length).toBe(2)
    expect((await ui.findAll({ type: 'Text', text: /^✗$/ })).length).toBe(1)
    expect(await ui.find({ type: 'Text', text: /^✓12$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^✗2$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /▶ editing files/ })).toBeUndefined()   // waiting: nothing is live
  })

  test('only the live phase and the two before it are expanded', async ($: any, on: any) => {
    const clock = base(on)
    const groups = ['investigate', 'change', 'verify', 'investigate', 'change', 'verify']
    const recent = groups.map((g, i) => ({ intent: g, group: g, label: g, cw: 1000, tools: ['Read'], targets: ['f' + i + '.py'], tool_err: [false], err: false, agents: [] }))
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, recent }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns: 70, scroll: { offset: 0, bodyRows: 60 } } })
    expect((await ui.findAll({ type: 'Text', text: /^[│ ] [├└] $/ })).length).toBe(3)
  })

  test('test runs by subagents only are labelled as theirs', async ($: any, on: any) => {
    const clock = base(on)
    const payload = { ...COMPACT, tests: { passed: 4, failed: 7, ok: false, runs: 2, last_ts: 1, trend: [5, 7], scope: 'subagents' } }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(await ui.find({ type: 'Text', text: /^subagents: $/ })).toBeDefined()
  })

  test('a burst of tool calls is coalesced into few redraws', async ($: any, on: any) => {
    const clock = base(on)
    let redraws = 0
    on('http.fetch', () => ({ deny: 'down' }))
    on('tool.call', () => ({ result: 'ok' }))
    on('ui.invalidate', () => { redraws += 1; return { value: undefined } })
    await $.session.start(SESSION)
    await clock.advance(1000)
    const before = redraws
    for (let i = 0; i < 50; i++) await $.tool.call({ tool: 'Read', tool_use_id: 'r' + i, input: { file_path: '/x' } })
    expect(redraws).toBe(before)
    await clock.advance(250)
    expect(redraws).toBe(before + 1)
  })

  test('every row fits a narrow pane', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(COMPACT) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns: 24 } })
    const boxes = await ui.findAll({ type: 'Box' })
    for (const b of boxes.slice(1)) expect(String(b.text ?? '').length).toBeLessThanOrEqual(24)
    expect(await ui.find({ type: 'Text', text: /…$/ })).toBeDefined()
  })

  test('band shows a one-line summary when enabled', async ($: any, on: any) => {
    base(on, { TOKENSCOPE_LIVE_BAND: '1' })
    on('http.fetch', () => ({ deny: 'down' }))
    await $.session.start(SESSION)
    const band = await $.ui.mount({
      plugin: 'tokenscope-live', surface: 'terminal', component: 'AbovePrompt',
      props: { hasSurvey: false, isWorking: false, maxRows: 5, bodyColumns: 80, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    })
    expect(await band.find({ text: /tokenscope \(local\)/ })).toBeDefined()
  })

  test('band shows goal, current intent and cost; ⚠ only with warnings', async ($: any, on: any) => {
    const clock = base(on, { TOKENSCOPE_LIVE_BAND: '1' })
    let payload: any = COMPACT
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const props = { hasSurvey: false, isWorking: true, maxRows: 5, bodyColumns: 100, scroll: { offset: 0, bodyRows: 5 }, view: {} }
    let band = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'AbovePrompt', props })
    expect(await band.find({ text: '◆ Fix: failing tests in invoice · ▶ running tests · 182k CW · $0.41' })).toBeDefined()
    payload = { ...COMPACT, warnings: [WARNING] }
    await clock.advance(1000)
    band = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'AbovePrompt', props })
    expect(await band.find({ text: /\$0\.41 · ⚠$/ })).toBeDefined()
  })

  test('band is left to the engine when not enabled', async ($: any, on: any) => {
    base(on)
    on('http.fetch', () => ({ deny: 'down' }))
    on('ui.render', { component: 'AbovePrompt' }, (_$: any, e: any) => {
      const { Text } = _$.ui.resolve(e)
      return Text({ children: ['engine band'] })
    })
    await $.session.start(SESSION)
    const band = await $.ui.mount({
      plugin: 'tokenscope-live', surface: 'terminal', component: 'AbovePrompt',
      props: { hasSurvey: false, isWorking: false, maxRows: 5, bodyColumns: 80, scroll: { offset: 0, bodyRows: 5 }, view: {} },
    })
    expect(await band.find({ text: 'engine band' })).toBeDefined()
    expect(await band.find({ text: /tokenscope/ })).toBeUndefined()
  })

  // payload.intents (docs/intents-spec.md): two groups, then ungrouped steps, the last one open
  const step = (i: number, cat: string, label: string, extra: any = {}) => ({
    i, cat, label, source: 'rule', locked: true, open: false, cw: 10_000, usd: 0.03, turns: 1, summary: 'Read ×2', tested: null, group: null, ...extra,
  })
  const INTENTS = {
    steps: [
      step(0, 'investigate', 'Read store and model code', { group: 0, source: 'todo', summary: 'Read ×4 · 1 thinking' }),
      step(1, 'change', 'Add per-warehouse quantities', { group: 0, source: 'todo', summary: 'Edit ×3' }),
      step(2, 'verify', 'Run store tests', { summary: 'Bash unittest ✓', tested: 'pass', cw: 9_000 }),
      step(3, 'investigate', 'Inspect CLI entry point', { group: 1, source: 'declared' }),
      step(4, 'change', 'editing files · cli.py', { group: 1, source: 'rule', locked: false, open: true, summary: 'Edit ×2' }),
    ],
    groups: [
      { label: 'Implement multi-warehouse stock', source: 'todo', first: 0, last: 1, cat: 'change', cw: 120_000, usd: 0.41, steps: 2, summary: '' },
      { label: 'Wire warehouses into CLI', source: 'declared', first: 3, last: 4, cat: 'change', cw: 20_000, usd: 0.06, steps: 2, summary: '' },
    ],
    declared: { calls: 2, steps: 2 },
    labeller: { calls: 0 },
  }
  const mountPane = ($: any, bodyColumns = 90) => $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns } })

  test('intents: groups collapsed, the live group expanded, provisional label dimmed, declared footer', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, intents: INTENTS }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /^── intents ──$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^── flow ──$/ })).toBeUndefined()
    // group 1: collapsed, one line with its step count and costs; its steps hidden
    expect(await ui.find({ type: 'Text', text: /^▸1 $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Implement multi-warehouse stock$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · 2 steps · 120k · \$0\.41$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Read store and model code$/ })).toBeUndefined()
    // …and one dim detail line listing them
    const det = await ui.find({ type: 'Text', text: /^ {3}↳ Read store and model code · Add per-warehouse quantities$/ })
    expect(det).toBeDefined()
    expect(det.props.dimColor).toBe(true)
    // an ungrouped step at top level, its tools summary on its detail line
    expect(await ui.find({ type: 'Text', text: /^\[VERIFY\]$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Run store tests$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {3}↳ Bash unittest ✓$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · Bash unittest ✓$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /^ · 9k · \$0\.03$/ })).toBeDefined()
    // group 2 holds the open step: expanded, its steps under it, the open one ▶ with a dim provisional label…
    expect(await ui.find({ type: 'Text', text: /^▾2 $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}├ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Inspect CLI entry point$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}└ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^▶ $/ })).toBeDefined()
    const prov = await ui.find({ type: 'Text', text: /^ editing files · cli\.py…$/ })
    expect(prov).toBeDefined()
    expect(prov.props.dimColor).toBe(true)
    // ✎ for declared labels only, and the footer
    expect((await ui.findAll({ type: 'Text', text: /^ ✎$/ })).length).toBe(2)   // the declared group and step
    expect(await ui.find({ type: 'Text', text: /^✎ declared ×2$/ })).toBeDefined()
  })

  test('intents: a locked open label is not dimmed; no footer without declare_intent', async ($: any, on: any) => {
    const clock = base(on)
    const steps = INTENTS.steps.map((s) => (s.open ? { ...s, locked: true, label: 'Wire the CLI' } : s))
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, intents: { ...INTENTS, steps, declared: { calls: 0, steps: 0 } } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /^ Wire the CLI$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /…$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /declared ×/ })).toBeUndefined()
  })

  test('intents: older items fold into "… N earlier" within the row budget, never the live item', async ($: any, on: any) => {
    const clock = base(on)
    const cats = ['investigate', 'change', 'verify']
    const steps = Array.from({ length: 40 }, (_, i) => step(i, cats[i % 3], 'Step number ' + i, { open: i === 39, locked: i !== 39, group: i >= 36 ? 0 : null }))
    const groups = [{ label: 'Final group', source: 'haiku', first: 36, last: 39, cat: 'change', cw: 40_000, usd: 0.12, steps: 4 }]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, intents: { steps, groups, declared: { calls: 0 } } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await mountPane($)
    const boxes = await ui.findAll({ type: 'Box' })
    expect(boxes.length - 1).toBeLessThanOrEqual(38)
    const fold = await ui.find({ type: 'Text', text: /^… \d+ earlier$/ })
    expect(fold).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Step number 0$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /^ Step number 35$/ })).toBeDefined()   // newest ungrouped kept
    expect(await ui.find({ type: 'Text', text: /^▾1 $/ })).toBeDefined()              // the live group, expanded
    expect(await ui.find({ type: 'Text', text: /^ Step number 39…$/ })).toBeDefined()
    const n = Number(String(fold.text).match(/\d+/)![0])
    expect(await ui.find({ type: 'Text', text: new RegExp('^ Step number ' + (n - 1) + '$') })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: new RegExp('^ Step number ' + n + '$') })).toBeDefined()
  })

  test('intents: /tokenscope expand|collapse <n|all> and view phases|intents; state survives redraws', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, intents: INTENTS }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let res = await $.command.run({ command: 'tokenscope', args: 'expand 1' })
    expect(res.text).toBe('tokenscope: expanded group 1 · Implement multi-warehouse stock')
    res = await $.command.run({ command: 'tokenscope', args: 'collapse 2' })
    expect(res.text).toMatch(/collapsed group 2/)
    await clock.advance(3000)   // polls and redraws keep the person's choice
    let ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /^▾1 $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Read store and model code$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^▸2 $/ })).toBeDefined()   // collapsed although live
    expect(await ui.find({ type: 'Text', text: /cli\.py…/ })).toBeUndefined()
    await ui.unmount()

    expect((await $.command.run({ command: 'tokenscope', args: 'expand 7' })).text).toMatch(/no group 7/)
    await $.command.run({ command: 'tokenscope', args: 'collapse all' })
    ui = await mountPane($)
    expect((await ui.findAll({ type: 'Text', text: /^▸\d $/ })).length).toBe(2)
    await ui.unmount()
    await $.command.run({ command: 'tokenscope', args: 'expand all' })
    ui = await mountPane($)
    expect((await ui.findAll({ type: 'Text', text: /^▾\d $/ })).length).toBe(2)
    await ui.unmount()

    expect((await $.command.run({ command: 'tokenscope', args: 'view phases' })).text).toBe('tokenscope: phases view')
    ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /^── flow ──$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^INVESTIGATE$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /▸|▾/ })).toBeUndefined()
    await ui.unmount()
    await $.command.run({ command: 'tokenscope', args: 'view intents' })
    ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /^── intents ──$/ })).toBeDefined()
    expect((await $.command.run({ command: 'tokenscope', args: 'bogus' })).text).toMatch(/^usage:/)
  })

  test('intents: pressing a group line toggles it', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, intents: INTENTS }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await mountPane($)
    await ui.press({ key: 'group-0' })
    await ui.press({ key: 'group-3' })
    await ui.unmount()
    ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /^▾1 $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Read store and model code$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^▸2 $/ })).toBeDefined()
    await ui.press({ key: 'group-0' })
    await ui.unmount()
    ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /^▸1 $/ })).toBeDefined()
  })

  test('intent rows fit a narrow pane', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, intents: INTENTS }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await mountPane($, 30)
    const boxes = await ui.findAll({ type: 'Box' })
    for (const b of boxes.slice(1)) expect(String(b.text ?? '').length).toBeLessThanOrEqual(30)
    expect(await ui.find({ type: 'Text', text: /^▾2 $/ })).toBeDefined()
  })

  test('intents: a detail line folds equal labels ×N, marks tested steps, and overflows as +N', async ($: any, on: any) => {
    const clock = base(on)
    const lab = ['Read store.py', 'Edit models.py', 'Edit models.py', 'Run tests', 'Read cli.py', 'Edit cli.py', 'Edit docs/README.md']
    const steps = [
      ...lab.map((l, i) => step(i, 'change', l, { group: 0, tested: i === 3 ? 'pass' : i === 6 ? 'fail' : null })),
      step(7, 'investigate', 'Look around', { open: true, locked: false }),
    ]
    const groups = [{ label: 'Build it', source: 'haiku', first: 0, last: 6, cat: 'change', cw: 70_000, usd: 0.2, steps: 7 }]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, intents: { steps, groups, declared: { calls: 0 } } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await mountPane($, 200)
    expect(await ui.find({ type: 'Text', text: '   ↳ Read store.py · Edit models.py ×2 · Run tests ✓ · Read cli.py · Edit cli.py · Edit docs/README.md ✗' })).toBeDefined()
    await ui.unmount()
    ui = await mountPane($, 60)
    const det = await ui.find({ type: 'Text', text: /^ {3}↳ Read store\.py · Edit models\.py ×2 · Run tests ✓ \+3$/ })
    expect(det).toBeDefined()
    expect(String(det.text).length).toBeLessThanOrEqual(60)
  })

  // the live step's calls (payload ops) and one the transcript has not caught up with
  const OPS = [
    { tool: 'Read', target: 'store.py', cw: 2_000, err: false },
    { tool: 'Bash unittest', target: 'unittest', cw: 3_000, err: false, test: { ok: true, passed: 22, failed: 0 } },
    { tool: 'Bash unittest', target: 'unittest', cw: 3_000, err: true, test: { ok: false, passed: 21, failed: 1 } },
    { tool: 'Edit', target: 'store.py', cw: 4_000, err: null },
  ]

  test('intents: the live intent shows one row per operation, then pending in-process calls', async ($: any, on: any) => {
    const clock = base(on)
    const steps = [step(0, 'investigate', 'Read the code', { summary: 'Read ×2' }),
                   step(1, 'change', 'Fix the store', { open: true, locked: false, summary: 'Edit ×1', ops: OPS })]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, main_tool_calls: 6, intents: { steps, groups: [], declared: { calls: 0 } } }) } }))
    on('tool.call', () => ({ result: 'ok' }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: '✓ ' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Read store.py' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'unittest 22 passed' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'unittest 1 failed' })).toBeDefined()
    const running = await ui.find({ type: 'Text', text: 'Edit store.py' })
    expect(running.props.bold).toBe(true)
    expect((await ui.findAll({ type: 'Text', text: /^▶ $/ })).length).toBe(2)   // the open step's head and its running call
    expect((await ui.findAll({ type: 'Text', text: /^✗ $/ })).length).toBe(1)
    // the live step has no summary detail line; the older one does
    expect(await ui.find({ type: 'Text', text: /^ {3}↳ Edit ×1$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /^ {3}↳ Read ×2$/ })).toBeDefined()
    await ui.unmount()
    // calls seen in-process before the transcript has them: appended as running ops
    for (const id of ['a', 'b', 'c', 'd', 'e', 'f']) await $.tool.call({ tool: 'Read', tool_use_id: id, input: { file_path: '/r/x.py' } })
    await $.tool.call({ tool: 'Bash', tool_use_id: 'g', input: { command: 'git status' } })
    await clock.advance(1000)
    ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: 'git status' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Read x.py' })).toBeUndefined()   // the server already has those
  })

  test('intents: declare_intent calls are never operation rows, from the server or in-process', async ($: any, on: any) => {
    const clock = base(on)
    const ops = [{ tool: 'mcp:tokenscope-intents.declare_intent', target: null, cw: 6_000, err: false }, ...OPS.slice(0, 1)]
    const steps = [step(0, 'change', 'Fix the store', { open: true, locked: false, summary: 'Read ×1', ops })]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, main_tool_calls: 2, intents: { steps, groups: [], declared: { calls: 1 } } }) } }))
    on('tool.call', () => ({ result: 'ok' }))
    await $.session.start(SESSION)
    for (const id of ['a', 'b']) await $.tool.call({ tool: 'Read', tool_use_id: id, input: { file_path: '/r/x.py' } })
    await $.tool.call({ tool: 'mcp__tokenscope-intents__declare_intent', tool_use_id: 'c', input: { label: 'Next thing' } })
    await $.tool.call({ tool: 'Edit', tool_use_id: 'd', input: { file_path: '/r/y.py' } })
    await clock.advance(1000)
    const ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: 'Read store.py' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Edit y.py' })).toBeDefined()   // pending, after the declare call
    expect(await ui.find({ type: 'Text', text: /declare_intent/ })).toBeUndefined()
  })

  test('intents: when a new intent starts the previous live one collapses to its head and detail line', async ($: any, on: any) => {
    const clock = base(on)
    const before = [step(0, 'investigate', 'Read the code', { group: 0 }),
                    step(1, 'change', 'Fix the store', { group: 0, open: true, locked: false, ops: OPS })]
    const after = [step(0, 'investigate', 'Read the code', { group: 0 }), step(1, 'change', 'Fix the store', { group: 0 }),
                   step(2, 'verify', 'Run the suite', { open: true, locked: false, ops: [OPS[3]] })]
    const groups = [{ label: 'Fix stock bug', source: 'haiku', first: 0, last: 1, cat: 'change', cw: 20_000, usd: 0.06, steps: 2 }]
    let steps = before
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, intents: { steps, groups, declared: { calls: 0 } } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /^▾1 $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'unittest 22 passed' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {3}↳ / })).toBeUndefined()
    await ui.unmount()
    steps = after
    await clock.advance(1000)
    ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /^▸1 $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '   ↳ Read the code · Fix the store' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'unittest 22 passed' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: 'Edit store.py' })).toBeDefined()
  })

  test('intents: short of rows, the oldest detail lines go first, then whole intents; never the live one', async ($: any, on: any) => {
    const clock = base(on)
    let n = 14
    const steps = () => [...Array.from({ length: n }, (_, i) => step(i, 'investigate', 'Step number ' + i, { summary: 'Sum ' + i })),
                         step(n, 'change', 'Live one', { open: true, locked: false, ops: OPS })]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, intents: { steps: steps(), groups: [], declared: { calls: 0 } } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await mountPane($)
    expect((await ui.findAll({ type: 'Box' })).length - 1).toBeLessThanOrEqual(38)
    // every intent still shown; only the oldest lost their detail lines
    expect(await ui.find({ type: 'Text', text: /^… \d+ earlier$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /^ Step number 0$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: '   ↳ Sum 0' })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: '   ↳ Sum 13' })).toBeDefined()
    const kept = (await ui.findAll({ type: 'Text', text: /^ {3}↳ Sum \d+$/ })).map((t: any) => Number(String(t.text).match(/\d+/)![0]))
    expect(kept).toEqual(Array.from({ length: kept.length }, (_, i) => 14 - kept.length + i))   // the newest ones
    expect(await ui.find({ type: 'Text', text: 'Edit store.py' })).toBeDefined()
    await ui.unmount()
    // far more intents: the oldest fold into "… N earlier"; the live one and its operations stay
    n = 40
    await clock.advance(1000)
    ui = await mountPane($)
    expect((await ui.findAll({ type: 'Box' })).length - 1).toBeLessThanOrEqual(38)
    expect(await ui.find({ type: 'Text', text: /^… \d+ earlier$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Step number 0$/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /^ Step number 39$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Live one…$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Edit store.py' })).toBeDefined()
  })

  test('intents: a ToolSearch step is drawn as OVERHEAD', async ($: any, on: any) => {
    const clock = base(on)
    const steps = [step(0, 'overhead', 'Load declare_intent tool', { summary: 'ToolSearch' }), step(1, 'change', 'Edit it', { open: true })]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, intents: { steps, groups: [], declared: { calls: 0 } } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await mountPane($)
    const tag = await ui.find({ type: 'Text', text: '[OVERHEAD]' })
    expect(tag.props.color).toBe('gray')
    expect(await ui.find({ type: 'Text', text: ' Load declare_intent tool' })).toBeDefined()
  })

  // payload.recap: sent by the server only while the session waits for you / is idle
  const RECAP = {
    cw: 120_000, usd: 0.4, intents: 3,
    costliest: { label: 'Find the pricing code', cat: 'investigate', cw: 70_000, usd: 0.21, pct: 58 },
    slowest: { label: 'Run the full suite', cat: 'verify', s: 252 },
    hints: [
      { rule: 'reread', kind: 'session', action: 'Note what store.py holds in CLAUDE.md', text: 'Note what store.py holds in CLAUDE.md', evidence: 'store.py read in full 5× across 3 intents', cw_at_stake: 40_000, estimate: 'x' },
      { rule: 'polling', text: 'Wait in one call', evidence: '6 wake-ups in a row only waited', cw_at_stake: 30_000, estimate: 'x' },
    ],
    session: {},
  }
  const texts = async (ui: any) => (await ui.findAll({ type: 'Box' })).slice(1).map((b: any) => String(b.text ?? ''))

  test('recap: a bright boxed section right under the header while waiting; removed entirely once the agent works again', async ($: any, on: any) => {
    const clock = base(on)
    const steps = [step(0, 'investigate', 'Find the pricing code'), step(1, 'verify', 'Run the full suite')]
    let payload: any = { ...COMPACT, status: 'waiting', main_tool_calls: 0, recap: RECAP, intents: { steps, groups: [], declared: { calls: 0 } } }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await mountPane($, 80)
    let rows = await texts(ui)
    // the title bar spans the pane, bold and bright, as row 2 (right under the header, above ASKED and the list)
    const title = await ui.find({ type: 'Text', text: /^━━ DONE · recap · 120k · \$0\.40 ━+$/ })
    expect(title).toBeDefined()
    expect(String(title.text).length).toBe(80)
    expect(title.props.bold).toBe(true)
    expect(title.props.color).toBe('greenBright')
    expect(rows[1]).toMatch(/^━━ DONE · recap/)
    expect(rows.findIndex((t: string) => /^ASKED/.test(t))).toBeGreaterThan(rows.findIndex((t: string) => /^━+$/.test(t)))
    expect(rows.findIndex((t: string) => /^━+$/.test(t))).toBeLessThan(rows.findIndex((t: string) => t === '── intents ──'))
    // big top lines: the share / the time first
    expect(rows).toContain(' 58%  costliest [INVESTIGATE] Find the pricing code · 70k · $0.21')
    expect(rows).toContain(' 4m12s  slowest [VERIFY] Run the full suite')
    expect((await ui.find({ type: 'Text', text: '58%' })).props.bold).toBe(true)
    // hints: the action first (bold, bright), the evidence dim on the line under it; `text` alone still reads
    const action = await ui.find({ type: 'Text', text: 'Note what store.py holds in CLAUDE.md' })
    expect(action.props.bold).toBe(true)
    expect(action.props.color).toBe('yellowBright')
    const at = rows.indexOf(' ➜ Note what store.py holds in CLAUDE.md')
    expect(at).toBeGreaterThan(0)
    expect(rows[at + 1]).toBe('   store.py read in full 5× across 3 intents')
    expect((await ui.find({ type: 'Text', text: '   store.py read in full 5× across 3 intents' })).props.dimColor).toBe(true)
    expect(rows).toContain(' ➜ Wait in one call')
    expect(rows.filter((t: string) => /^━+$/.test(t)).length).toBe(1)   // the closing rule
    await ui.unmount()
    // the agent resumes after a new prompt: the server says working (and stops sending the recap); not a single
    // recap line is left, and the intent list goes on
    payload = { ...payload, status: 'working', recap: undefined, intents: { steps: [...steps, step(2, 'change', 'Fix rounding', { open: true })], groups: [], declared: { calls: 0 } } }
    await clock.advance(1000)
    ui = await mountPane($, 80)
    rows = await texts(ui)
    expect(rows.some((t: string) => /━|➜|costliest|slowest|recap/.test(t))).toBe(false)
    expect(rows[1]).toMatch(/^ASKED/)
    expect(await ui.find({ type: 'Text', text: ' Fix rounding' })).toBeDefined()
    await ui.unmount()
    // a stale recap with a working status is not drawn either
    payload = { ...payload, recap: RECAP }
    await clock.advance(1000)
    ui = await mountPane($, 80)
    expect(await ui.find({ type: 'Text', text: /DONE · recap/ })).toBeUndefined()
    await ui.unmount()
    // idle with no hints: costliest and slowest only
    payload = { ...payload, status: 'idle', recap: { ...RECAP, hints: [] } }
    await clock.advance(1000)
    ui = await mountPane($, 80)
    rows = await texts(ui)
    expect(rows[1]).toMatch(/^━━ DONE · recap/)
    expect(rows).toContain(' 4m12s  slowest [VERIFY] Run the full suite')
    expect(rows.some((t: string) => t.includes('➜'))).toBe(false)
  })

  test('recap: hints of any kind render action, evidence, skill and stub path', async ($: any, on: any) => {
    const clock = base(on)
    const hints = [{ rule: 'repeat', kind: 'cross-session', action: 'Make it a skill', evidence: 'done 4× in 3 sessions, ≈180k CW each', skill: 'release-notes', stub_path: '.claude/skills/release-notes/SKILL.md', cw_at_stake: 1 }]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, status: 'waiting', main_tool_calls: 0, recap: { ...RECAP, hints } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await mountPane($, 100)
    const rows = await texts(ui)
    const at = rows.indexOf(' ➜ Make it a skill')
    expect(at).toBeGreaterThan(0)
    expect(rows[at + 1]).toBe('   cross-session: done 4× in 3 sessions, ≈180k CW each')
    expect(rows[at + 2]).toBe('   ✦ release-notes · stub: .claude/skills/release-notes/SKILL.md')
    expect((await ui.find({ type: 'Text', text: '✦ release-notes' })).props.color).toBe('magentaBright')
  })

  test('recap: a cross_session hint from the server: action first, evidence dim after "cross-session:", stub path dim', async ($: any, on: any) => {
    const clock = base(on)
    const make = { rule: 'repeat', kind: 'cross_session', action: 'Make "change cli.py + README.md and test" a skill', text: 'x',
      evidence: '≈60k/run saving · done 4× in 4 sessions · ≈210k CW each · read cli.py → edit cli.py, README.md → run unittest (estimate)',
      est_saving_cw: 60_000, cw_at_stake: 60_000, estimate: 'estimate: median CW ...', skill: null,
      stub_path: '~/.cache/tokenscope/skill-drafts/change-cli-py/SKILL.md', matches: [{ sid: 'aaaaaaaa', cw: 200_000, score: 0.7 }] }
    const use = { ...make, rule: 'existing_skill', action: 'Use skill /release-notes for this', skill: 'release-notes', stub_path: null,
      evidence: '≈40k/run saving · done 3× without it in 2 sessions · ≈150k CW each · you have skill release-notes ("Write release notes") that covers this (estimate)' }
    let hints: any[] = [make, RECAP.hints[0]]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, status: 'waiting', main_tool_calls: 0, recap: { ...RECAP, hints } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await mountPane($, 200)
    let rows = await texts(ui)
    const at = rows.indexOf(' ➜ Make "change cli.py + README.md and test" a skill')
    expect(at).toBeGreaterThan(0)
    expect(rows[at + 1]).toBe('   cross-session: ≈60k/run saving · done 4× in 4 sessions · ≈210k CW each · read cli.py → edit cli.py, README.md → run unittest (estimate)')
    expect((await ui.find({ type: 'Text', text: rows[at + 1] })).props.dimColor).toBe(true)
    expect(rows[at + 2]).toBe('   stub: ~/.cache/tokenscope/skill-drafts/change-cli-py/SKILL.md')
    expect((await ui.find({ type: 'Text', text: 'stub: ~/.cache/tokenscope/skill-drafts/change-cli-py/SKILL.md' })).props.dimColor).toBe(true)
    expect(rows[at + 3]).toBe(' ➜ Note what store.py holds in CLAUDE.md')   // the session hint follows
    hints = [use]
    await clock.advance(3000)
    rows = await texts(ui)
    const u = rows.indexOf(' ➜ Use skill /release-notes for this')
    expect(u).toBeGreaterThan(0)
    expect(rows[u + 1]).toMatch(/^   cross-session: ≈40k\/run saving · done 3× without it in 2 sessions · ≈150k CW each · you have skill release-notes/)
    expect(rows[u + 2]).toBe('   ✦ release-notes')
  })

  test('recap: evidence wraps onto up to 3 dim lines, its leading numbers whole at 30, 50 and 80 columns', async ($: any, on: any) => {
    const clock = base(on)
    const cross = { rule: 'repeat', kind: 'cross_session', action: 'Make "Add CLI subcommand with tests" a skill', cw_at_stake: 34_000,
      evidence: '≈34k/run saving · done 4× in 4 sessions · ≈95k CW each · read cli.py, models.py → edit cli.py + tests → update README.md, CHANGELOG.md → run unittest (estimate)' }
    const sess = { rule: 'reread', kind: 'session', action: 'Note what store.py holds in CLAUDE.md', cw_at_stake: 20_000,
      evidence: '≈20k at stake · store.py read in full 5× across 3 intents, 4 of them unchanged since the read before' }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, status: 'waiting', main_tool_calls: 0, recap: { ...RECAP, hints: [cross, sess] } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    for (const width of [30, 50, 80]) {
      const ui = await mountPane($, width)
      const rows = await texts(ui)
      for (const t of rows) expect(t.length).toBeLessThanOrEqual(width)
      for (const [h, lead] of [[cross, /^   cross-session: ≈34k\/run|^   cross-session:$/], [sess, /^   ≈20k at stake/]] as const) {
        const at = rows.findIndex((t: string) => t.startsWith(' ➜ ') && h.action.startsWith(t.slice(3).replace(/…$/, '')))
        expect(at).toBeGreaterThan(0)
        const ev: string[] = []
        for (let k = at + 1; k < rows.length && rows[k].startsWith('   ') && !rows[k].startsWith('   stub') && !rows[k].startsWith(' ➜'); k++) ev.push(rows[k])
        expect(ev.length).toBeGreaterThan(1)        // wrapped, not cut at the pane width
        expect(ev.length).toBeLessThanOrEqual(3)
        expect(ev[0]).toMatch(lead)
        const joined = ev.map((t) => t.slice(3)).join(' ')
        // the numbers come through whole on every width; the words after them may lose their tail
        if (h === cross) {
          expect(joined).toContain('≈34k/run saving')
          expect(joined).toContain('done 4×')
        } else expect(joined).toContain('≈20k at stake')
        for (const t of ev) expect((await ui.find({ type: 'Text', text: t })).props.dimColor).toBe(true)
        if (width === 80 && h === cross) expect(ev).toEqual(['   cross-session: ≈34k/run saving · done 4× in 4 sessions · ≈95k CW each · read',
          '   cli.py, models.py → edit cli.py + tests → update README.md, CHANGELOG.md →',
          '   run unittest (estimate)'])
        if (width === 30) expect(ev[2]).toMatch(/…$/)   // out of lines: only the tail is cut
      }
      await ui.unmount()
    }
  })

  test('recap: the share leads so a narrow pane cuts the label, not the 38%; the cost stays too', async ($: any, on: any) => {
    const clock = base(on)
    const recap = { ...RECAP, costliest: { label: 'Add multi-warehouse stock and transfer between every warehouse', cat: 'change', cw: 63_000, usd: 0.22, pct: 38 } }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, status: 'waiting', main_tool_calls: 0, recap }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await mountPane($, 60)
    const rows = await texts(ui)
    const cost = rows.find((t: string) => t.includes('costliest'))!
    expect(cost).toMatch(/^ 38%  costliest \[CHANGE\] Add multi-warehouse.*… · 63k · \$0\.22$/)
    expect(cost.length).toBeLessThanOrEqual(60)
    for (const t of rows) expect(t.length).toBeLessThanOrEqual(60)
  })

  test('recap: costliest and slowest on one line when they are the same intent', async ($: any, on: any) => {
    const clock = base(on)
    const recap = { ...RECAP, hints: [], slowest: { label: 'Find the pricing code', cat: 'investigate', s: 72 } }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, status: 'waiting', main_tool_calls: 0, recap }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await mountPane($, 120)
    const rows = await texts(ui)
    expect(rows).toContain(' 58%  1m12s  costliest & slowest [INVESTIGATE] Find the pricing code · 70k · $0.21')
    expect(rows.some((t: string) => /^ \S+ +slowest/.test(t))).toBe(false)
  })

  test('recap: a call seen in-process (agent working again before the transcript says so) hides it', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, status: 'waiting', main_tool_calls: 0, recap: RECAP }) } }))
    on('tool.call', () => ({ result: 'ok' }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /DONE · recap/ })).toBeDefined()
    await ui.unmount()
    await $.tool.call({ tool: 'Read', tool_use_id: 'a', input: { file_path: '/r/x.py' } })
    await clock.advance(1000)
    ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /DONE · recap/ })).toBeUndefined()
    expect(await ui.find({ type: 'Text', text: /➜/ })).toBeUndefined()
  })

  test('recap: within the row budget, the intent list yields its oldest rows first; the recap stays whole', async ($: any, on: any) => {
    const clock = base(on)
    const steps = Array.from({ length: 40 }, (_, i) => step(i, 'investigate', 'Step number ' + i, { summary: 'Sum ' + i }))
    const hints = Array.from({ length: 5 }, (_, i) => ({ rule: 'r' + i, action: 'Hint ' + i, evidence: 'ev ' + i, cw_at_stake: 1 }))
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, status: 'waiting', main_tool_calls: 0, recap: { ...RECAP, hints }, intents: { steps, groups: [], declared: { calls: 0 } } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await mountPane($)
    const rows = await texts(ui)
    expect(rows.length).toBeLessThanOrEqual(38)
    expect(rows).toContain(' ➜ Hint 2')
    expect(rows).toContain('   ev 2')
    expect(rows).not.toContain(' ➜ Hint 3')   // at most 3 hints
    expect(rows.filter((t: string) => /^━+$/.test(t)).length).toBe(1)
    expect(await ui.find({ type: 'Text', text: /^… \d+ earlier$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Step number 39$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ Step number 0$/ })).toBeUndefined()
  })

  // wall time on every operation, step and group line; skills shown distinctly
  test('times: ops, steps and groups show wall time after tokens / $; a narrow pane cuts the label, never the numbers', async ($: any, on: any) => {
    const clock = base(on)
    const ops = [
      { tool: 'Bash unittest', target: 'unittest', cw: 3_000, err: false, dur_s: 4.2, test: { ok: true, passed: 22, failed: 0 } },
      { tool: 'Edit', target: 'store.py', cw: 4_000, err: null, dur_s: 75 },
    ]
    const steps = [
      step(0, 'change', 'Add X', { group: 0, dur_s: 3720 }),
      step(1, 'verify', 'Run store tests', { group: 0, summary: 'Bash unittest ✓', dur_s: 9 }),
      step(2, 'change', 'Fix the store with a very long label that will not fit a narrow pane', { open: true, locked: true, dur_s: 72, cw: 63_000, usd: 0.22, ops }),
    ]
    const groups = [{ label: 'Implement it', source: 'haiku', first: 0, last: 1, cat: 'change', cw: 20_000, usd: 0.06, steps: 2, dur_s: 3780 }]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, main_tool_calls: 2, intents: { steps, groups, declared: { calls: 0 } } }) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await mountPane($, 120)
    let rows = await texts(ui)
    expect(rows).toContain('▸1 [CHANGE] Implement it · 2 steps · 20k · $0.06 · 1h3m')
    expect(rows).toContain('▶ [CHANGE] Fix the store with a very long label that will not fit a narrow pane · 63k · $0.22 · 1m12s')
    expect(rows).toContain('   ✓ unittest 22 passed · 3k · 4s')
    expect(rows).toContain('   ▶ Edit store.py · 4k · 1m15s')
    await ui.unmount()
    ui = await mountPane($, 44)
    rows = await texts(ui)
    for (const t of rows) expect(t.length).toBeLessThanOrEqual(44)
    expect(rows.find((t: string) => t.startsWith('▸1'))).toMatch(/…? · 2 steps · 20k · \$0\.06 · 1h3m$/)
    expect(rows.find((t: string) => t.startsWith('▶ [CHANGE]'))).toMatch(/^▶ \[CHANGE\] Fix the.*… · 63k · \$0\.22 · 1m12s$/)
    await ui.unmount()
    ui = await mountPane($, 30)
    rows = await texts(ui)
    expect(rows).toContain('   ✓ unittest 22 pa… · 3k · 4s')
  })

  test('times: an in-process call the transcript has not caught up with shows its live elapsed time', async ($: any, on: any) => {
    const clock = base(on)
    const steps = [step(0, 'change', 'Fix the store', { open: true, locked: true, ops: [] })]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, main_tool_calls: 0, intents: { steps, groups: [], declared: { calls: 0 } } }) } }))
    on('tool.call', () => ({ result: 'ok' }))
    await $.session.start(SESSION)
    await $.tool.call({ tool: 'Bash', tool_use_id: 'a', input: { command: 'make build' } })
    await clock.advance(3000)
    const ui = await mountPane($, 80)
    expect(await texts(ui)).toContain('   ▶ make build · 3s')
  })

  test('skills: ✦ operations, step and group marks, the SKILLS row; an in-process Skill call', async ($: any, on: any) => {
    const clock = base(on)
    const ops = [{ tool: 'skill:pr-review', target: 'pr-review', cw: 2_000, err: false, dur_s: 3, skill: 'pr-review' },
                 { tool: 'Read', target: 'store.py', cw: 2_000, err: false, dur_s: 0.4 }]
    const steps = [
      step(0, 'investigate', 'Review the PR', { group: 0, skills: ['pr-review'], summary: '✦ pr-review · Read ×2' }),
      step(1, 'change', 'Apply the review', { group: 0 }),
      step(2, 'investigate', 'Check the diff', { open: true, locked: true, skills: ['pr-review'], summary: '✦ pr-review · Read ×1', ops }),
    ]
    const groups = [{ label: 'Review PR 218', source: 'haiku', first: 0, last: 1, cat: 'investigate', cw: 20_000, usd: 0.06, steps: 2, skills: ['pr-review', 'tdd'] }]
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify({ ...COMPACT, main_tool_calls: 2, skills: [{ name: 'pr-review', count: 2 }, { name: 'tdd', count: 1 }], intents: { steps, groups, declared: { calls: 0 } } }) } }))
    on('tool.call', () => ({ result: 'ok' }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    let ui = await mountPane($, 100)
    let rows = await texts(ui)
    expect(rows).toContain('SKILLS ✦ pr-review ×2 · tdd')
    expect(rows).toContain('▸1 [INVESTIGATE] Review PR 218 ✦ pr-review +1 · 2 steps · 20k · $0.06')
    expect(rows).toContain('▶ [INVESTIGATE] Check the diff ✦ pr-review · 10k · $0.03')
    expect(rows).toContain('   ✦ skill:pr-review · 2k · 3s')
    expect(rows).toContain('   ✓ Read store.py · 2k · 0s')
    const icon = await ui.find({ type: 'Text', text: '✦ ' })
    expect(icon.props.color).toBe('magentaBright')
    await ui.unmount()
    // a skill invoked in-process before the transcript has it
    await $.tool.call({ tool: 'Read', tool_use_id: 'a', input: { file_path: '/r/a.py' } })
    await $.tool.call({ tool: 'Read', tool_use_id: 'b', input: { file_path: '/r/b.py' } })
    await $.tool.call({ tool: 'Skill', tool_use_id: 'c', input: { skill: 'tdd' } })
    await clock.advance(2000)
    ui = await mountPane($, 100)
    rows = await texts(ui)
    expect(rows).toContain('   ✦ skill:tdd · 2s')
  })

  test('without an intents key the phase tree still draws', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(COMPACT) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await mountPane($)
    expect(await ui.find({ type: 'Text', text: /^── flow ──$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · ▶ running tests$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^── intents ──$/ })).toBeUndefined()
  })
})
