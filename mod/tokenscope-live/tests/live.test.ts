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
  goal: { name: 'Fix login bug', source: 'keyword' },
  status: 'working',
  cw: 1_234_000,
  usd: 3.1,
  context_tokens: 84_000,
  current_intent: 'test',
  recent: [
    { intent: 'investigate', group: 'investigate', tools: ['Read', 'Grep'], err: false },
    { intent: 'change', group: 'change', tools: ['Edit'], err: false },
    { intent: 'verify', group: 'verify', tools: ['Bash pytest'], err: true },
  ],
  flags: [{ flag: 'reread_file', count: 1, detail: 'auth.py ×3' }],
  agents: [{ type: 'Explore', status: 'running', turns: 5 }],
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
  test('local fallback counts tools, turns and cost-weighted tokens', async ($: any, on: any) => {
    base(on)
    on('http.fetch', () => ({ deny: 'connection refused' }))
    on('tool.call', () => ({ result: 'ok' }))
    on('turn.step', async function* () {
      return {
        turnId: 't1', index: 0, answer: '', toolUses: [], stopReason: 'end_turn',
        usage: { model: 'm', input_tokens: 1000, output_tokens: 100, cache_read_input_tokens: 10000, cache_creation_input_tokens: 2000 },
      }
    })
    on('turn.complete', () => ({ text: '' }))

    await $.session.start(SESSION)
    await $.tool.call({ tool: 'Bash', tool_use_id: 'a', input: { command: 'ls' } })
    await $.tool.call({ tool: 'Read', tool_use_id: 'b', input: { file_path: '/x' } })
    await $.tool.call({ tool: 'Bash', tool_use_id: 'c', input: { command: 'pwd' } })
    for await (const _ of $.turn.step({ turnId: 't1', index: 0, model: 'm', messageCount: 1 })) {
      // drain
    }
    await $.turn.complete({ turnId: 't1', answer: '' })

    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    // CW = 1000 + 1.25*2000 + 0.1*10000 + 5*100 = 5000
    expect(await ui.find({ type: 'Text', text: /^CW 5k · ctx 13k · 1 turns · 3 calls$/ })).toBeDefined()
        expect(await ui.find({ type: 'Text', text: 'Bash ×2, Read ×1' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'recent: Bash, Read, Bash' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: 'start `tokenscope live` for intents and flags' })).toBeDefined()
  })

  test('pane renders the server summary', async ($: any, on: any) => {
    const clock = base(on, { TOKENSCOPE_LIVE_URL: 'http://127.0.0.1:9999' })
    const urls: string[] = []
    on('http.fetch', (_$: any, e: any) => {
      urls.push(e.url)
      return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(COMPACT) } }
    })

    await $.session.start(SESSION)
    await clock.advance(1000)
    const ui = await $.ui.mount({ plugin: 'tokenscope-live', surface: 'terminal', component: 'Pane', requestId: 'tokenscope-live', props: PANE_PROPS })
    expect(urls[0]).toBe('http://127.0.0.1:9999/api/session/sess-1/compact')
    expect(await ui.find({ type: 'Text', text: /^Fix login bug$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^CW 1\.2M · \$3\.10 · ctx 84k$/ })).toBeDefined()
    expect(await ui.findAll({ type: 'Text', text: /^█$/ })).toHaveLength(3)
    expect(await ui.find({ type: 'Text', text: /^ verify$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^verify  · Bash pytest ✗$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^⚠ re-read file · auth\.py ×3$/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /^↳ Explore running · 5 turns$/ })).toBeDefined()
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
