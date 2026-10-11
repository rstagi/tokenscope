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
    expect(await ui.find({ type: 'Text', text: /^ {2}1m0[56]s$/ })).toBeDefined()
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
    const groups = ['investigate', 'change', 'verify', 'plan', 'research', 'ship', 'delegate', 'wait', 'other', 'change', 'verify', 'investigate', 'change']
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
})
