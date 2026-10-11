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
  goal: 'Fix invoice rounding',
  status: 'working',
  cw: 412_000,
  usd: 1.23,
  context_tokens: 84_000,
  elapsed_s: 151,
  turns: 23,
  tool_calls: 31,
  errors: 4,
  current_intent: 'test',
  recent: [
    { intent: 'read_code', group: 'investigate', cw: 40_000, tools: ['Read', 'Grep'], tool_err: [false, false], err: false },
    { intent: 'read_code', group: 'investigate', cw: 80_000, tools: ['Read', 'Read'], tool_err: [false, false], err: false },
    { intent: 'edit', group: 'change', cw: 18_000, tools: ['Edit'], tool_err: [false], err: false },
    { intent: 'test', group: 'verify', cw: 15_000, tools: ['Bash run_tests.sh'], tool_err: [true], err: true },
    { intent: 'test', group: 'verify', cw: 15_000, tools: ['Bash run_tests.sh'], tool_err: [true], err: true },
    { intent: 'edit', group: 'change', cw: 0, tools: ['Edit'], tool_err: [false], err: false },
  ],
  cw_series: [12_000, 9_000, 30_000, 18_000, 15_000, 15_000, 20_000, 8_000],
  flags: [{ flag: 'reread_file', count: 2, detail: '.py ×3' }],
  agents: [{ type: 'Explore', status: 'running', turns: 5 }],
}

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
    expect(await ui.find({ type: 'Text', text: /^○ idle$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}1:0[56]$/ })).toBeDefined()
    // CW per turn = 1000 + 1.25*2000 + 0.1*10000 + 5*100 = 5000
    expect(await ui.find({ type: 'Text', text: /^CW 10k · ctx 13k · 2 turns · 3 calls$/ })).toBeDefined()
    // cumulative 5k, 10k: one braille char, left column 2 dots, right column 4
    expect(await ui.find({ type: 'Text', text: /^\u28fc$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'Bash ×2 · Read ×1' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'recent: Bash, Read, Bash' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /tokenscope live/ })).toBeDefined()
  })

  test('pane draws header, metrics, gauge, flow tree, flags and subagents', async ($: any, on: any) => {
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
    // the header no longer carries the goal; it is the flow tree's root, once
    expect(await ui.find({ type: 'Text', text: /^tokenscope$/ })).toBeDefined()
    expect(await ui.findAll({ type: 'Text', text: /Fix invoice rounding/ })).toHaveLength(1)
    expect(await ui.find({ type: 'Text', text: /^● working$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}2:31$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^CW 412k · \$1\.23 · ctx 84k · 23 turns · 4 ✗$/ })).toBeDefined()
    // 8 turns -> 4 braille chars of cumulative CW
    const spark = await ui.findAll({ type: 'Text', text: /^[\u2800-\u28ff]+$/ })
    expect(spark).toHaveLength(1)
    expect(await ui.find({ type: 'Text', text: /^[\u2800-\u28ff]{4}$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^42%$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^█{10}$/ })).toBeDefined()

    // flow tree: investigate(2) -> change -> verify(2) -> change (now)
    expect(await ui.find({ type: 'Text', text: /^◆ Fix invoice rounding$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /retry/ })).toBeUndefined()
    const labels = await ui.findAll({ type: 'Text', text: /^(INVESTIGATE|CHANGE|VERIFY)$/ })
    expect(labels).toHaveLength(4)
    expect(await ui.find({ type: 'Text', text: /^ · 2 turns · 120k$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Read ×3$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · Grep$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · 2 turns · 30k$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Bash run_tests\.sh ×2$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ ✗2$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^└─ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · 1 turn$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {2}◀ now$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /earlier phase/ })).toBeUndefined()

    expect(await ui.find({ type: 'Text', text: /^ ⚠ re-read file · \.py ×3 $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^↳ Explore $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^running$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · 5 turns$/ })).toBeDefined()
  })

  test('subagents hang under the phase that launched them; VERIFY ✗ → CHANGE → VERIFY is a retry', async ($: any, on: any) => {
    const clock = base(on)
    const payload = {
      goal: 'Fix invoice rounding', status: 'working',
      recent: [
        { group: 'investigate', cw: 9_000, tools: ['Read', 'Read', 'Grep'] },
        { group: 'delegate', cw: 11_000, tools: ['Agent', 'Agent'], agents: [
          { type: 'Explore', status: 'running', turns: 5, cw: 40_000 },
          { type: 'general-purpose', status: 'done', turns: 3, cw: 22_000 },
        ] },
        { group: 'verify', cw: 5_000, tools: ['Bash run_tests.sh'], tool_err: [true], err: true },
        { group: 'change', cw: 4_000, tools: ['Edit'] },
        { group: 'verify', cw: 5_000, tools: ['Bash run_tests.sh'], tool_err: [true], err: true },
        { group: 'change', cw: 4_000, tools: ['Edit'] },
        { group: 'verify', cw: 5_000, tools: ['Bash run_tests.sh'] },
      ],
      agents: [{ type: 'Explore', status: 'running', turns: 5 }, { type: 'general-purpose', status: 'done', turns: 3 }],
    }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: { ...PANE_PROPS, bodyColumns: 70 } })
    // DELEGATE: tools first (├ since agents follow), then one sub-branch per agent
    expect(await ui.find({ type: 'Text', text: /^│ {2}├ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Agent ×2$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^│ {2}├─↳ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^Explore {9}$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^● running$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ · 5 turns · 40k$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^│ {2}└─↳ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^general-purpose $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^✓ done$/ })).toBeDefined()
    // all payload agents are in the tree: no separate rows at the bottom
    expect(await ui.find({ type: 'Text', text: /^↳ / })).toBeUndefined()
    // second and third VERIFY are retries; the tool that failed again is marked
    expect(await ui.findAll({ type: 'Text', text: /^↺ $/ })).toHaveLength(2)
    expect(await ui.find({ type: 'Text', text: /^ retry 1$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ retry 2$/ })).toBeDefined()
    expect(await ui.findAll({ type: 'Text', text: /^ ↺$/ })).toHaveLength(1)
    // CHANGE after VERIFY ✗ is not itself a retry
    expect(await ui.findAll({ type: 'Text', text: /retry/ })).toHaveLength(2)
  })

  test('the pane stays within 38 rows; many tool groups go one per line', async ($: any, on: any) => {
    const clock = base(on)
    const groups = ['investigate', 'change', 'verify', 'plan', 'research', 'ship', 'delegate', 'change']
    const tools = ['Read', 'Grep', 'Glob', 'WebFetch', 'Bash some-long-command.sh', 'Edit']
    const agents = [{ type: 'Explore', status: 'running', turns: 1 }, { type: 'Plan', status: 'starting' }]
    const payload = { goal: 'g', recent: groups.map(group => ({ group, tools, agents, cw: 1000 })) }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    const boxes = await ui.findAll({ type: 'Box' })
    expect(boxes.length - 1).toBeLessThanOrEqual(38)
    expect(await ui.find({ type: 'Text', text: /^┊ \+\d+ earlier phases$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {3}├ \+3 more$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^ {3}└─↳ $/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^○ starting$/ })).toBeDefined()
  })

  test('older phases fold into a "+N earlier" row', async ($: any, on: any) => {
    const clock = base(on)
    const groups = ['investigate', 'change', 'verify', 'plan', 'research', 'ship', 'delegate', 'wait', 'other', 'change']
    const payload = { goal: 'g', recent: groups.map(group => ({ group, tools: ['Read'], cw: 1000 })) }
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(await ui.find({ type: 'Text', text: /^┊ \+2 earlier phases$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^INVESTIGATE$/ })).toBeUndefined()
    expect(await ui.findAll({ type: 'Text', text: /^(├─|└─) $/ })).toHaveLength(8)
  })

  test('the old payload shape still draws', async ($: any, on: any) => {
    const clock = base(on)
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(OLD_SHAPE) } }))
    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(await ui.find({ type: 'Text', text: /^◆ Fix login bug$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^○ idle$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^CW 1\.2M · \$3\.10 · ctx 160k · 1 ✗$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^80%$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^VERIFY$/ })).toBeDefined()
  })

  test('a new flag toasts once; unchanged payloads do not redraw', async ($: any, on: any) => {
    const clock = base(on)
    let payload: any = COMPACT
    const toasts: string[] = []
    let redraws = 0
    on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }))
    on('ui.toast', (_$: any, e: any) => { toasts.push(e.text); return { value: undefined } })
    on('ui.invalidate', (_$: any, e: any) => { redraws += 1; return { value: undefined } })
    await $.session.start(SESSION)
    await clock.advance(1000)
    expect(toasts).toHaveLength(0) // flags already there at the first payload only seed
    const before = redraws
    await clock.advance(3000)
    expect(redraws).toBe(before)

    payload = { ...COMPACT, flags: [...COMPACT.flags, { flag: 'dead_tool', count: 1, detail: 'Bash run_tests.sh ×3' }] }
    await clock.advance(1000)
    await clock.advance(1000)
    expect(toasts).toEqual(['⚠ dead tool · Bash run_tests.sh ×3'])
    expect(redraws).toBe(before + 1)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(await ui.find({ type: 'Text', text: /^ ⚠ dead tool · Bash run_tests\.sh ×3 $/ })).toBeDefined()
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
