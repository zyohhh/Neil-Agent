import { createRoot } from 'react-dom/client'
import { act } from 'react'
import App from './App'
import { reduceWorkbenchEvent, resetLiveSnapshotRequestForTests, WorkbenchRealtimeClient, type WorkbenchSnapshotV1 } from './protocol'

;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const snapshot = {
  schema_version: 1,
  source: 'live',
  generated_at: '2026-08-13T08:00:00Z',
  workspace: { name: 'Neil-Agent-Live', identity: '0123456789abcdef' },
  provider: { provider: 'deepseek', display_name: 'DeepSeek', model: 'deepseek-live', available_models: ['deepseek-live', 'deepseek-fast'], wire_protocol: 'anthropic-messages', thinking_enabled: false },
  run: { status: 'idle', run_id: null, objective: null, started_at: null, finished_at: null, error_type: null },
  revision: 0,
  last_sequence: 0,
  capabilities: { can_start_turn: true, can_cancel_turn: false, can_request_control: true, can_approve_tool: false, can_show_diff: true, can_estimate_cost: false, can_create_session: true, can_select_session: true, can_switch_model: true, tool_permission_mode: 'approval_gated', has_pty: false },
  timeline: [],
  output: [],
  approval: null,
  git: { available: true, branch: 'feature/web-workbench', revision: '0123456789abcdef', change_count: 1, files: [{ path: 'src/live.py', previous_path: null, status: 'M', kind: 'modified', additions: 2, deletions: 1, diff_available: true, diff_reason: 'available' }], truncated: false },
  sessions: { available: true, items: [], invalid_count: 0, total_count: 0 },
  active_session: { session_id: '20260813T080000000000Z-deadbeef', title: '新会话', round_count: 0, persistence_status: 'unsaved', runtime_provider: 'deepseek', runtime_model: 'deepseek-live' },
  files: { root: '', items: [{ name: 'src', path: 'src', kind: 'directory', children: [] }], truncated: false, revision: 'fedcba9876543210', unchanged: false },
  task: { source: 'unavailable', session_id: null, steps: [] },
  context: {
    source: 'local_estimate',
    tomography_schema_version: 2,
    budget_chars: 120000,
    limit_tokens: 200000,
    estimated_chars: 4200,
    estimated_tokens: 1260,
    stored_rounds: 0,
    selected_rounds: 0,
    omitted_rounds: 0,
    stored_history_chars: 0,
    omitted_history_chars: 0,
    checkpoint_state: 'none',
    layers: [
      { kind: 'system', chars: 800, estimated_tokens: 240, item_count: 1 },
      { kind: 'tool_schemas', chars: 2800, estimated_tokens: 840, item_count: 12 },
      { kind: 'project_instructions', chars: 0, estimated_tokens: 0, item_count: 0 },
      { kind: 'selected_history', chars: 0, estimated_tokens: 0, item_count: 0 },
      { kind: 'current_chain', chars: 0, estimated_tokens: 0, item_count: 0 },
    ],
    pressure: {
      level: 'safe',
      limiting_dimension: 'characters',
      character_basis_points: 350,
      token_basis_points: 630,
      character_headroom: 115800,
      token_headroom: 198740,
    },
    largest_tool_footprint: null,
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
  },
  review: { state: 'stale', quality_check: null, quality_checks: [], approval_available: false, cost_available: false, cost: { source: 'unavailable', estimated_usd: null, rate_table_version: null, rate_effective_date: null, model: null, reason: 'no_rate_table' } },
  security: { mode: 'approval_gated', binding: 'loopback', bootstrap_token_required: true, write_routes: 0, agent_connected: true, sandbox_backend: 'disabled', audit_enabled: true, shield_schema_version: 2, application_status: 'enforced', os_sandbox_status: 'disabled', audit_status: 'recording', tool_count: 12, direct_tool_count: 6, approval_tool_count: 6 },
} satisfies WorkbenchSnapshotV1

describe('WebWorkbenchApp', () => {
  beforeEach(() => {
    resetLiveSnapshotRequestForTests()
    document.cookie = 'neil_workbench_csrf=; Max-Age=0; Path=/'
    window.history.replaceState({}, '', '/?scene=running')
  })

  it('renders a clearly marked synthetic P0 workbench', async () => {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)

    await act(async () => root.render(<App />))

    expect(document.body.textContent).toContain('P0 fixture preview')
    expect(document.body.textContent).toContain('Synthetic data only')
    expect(document.body.textContent).toContain('Workspace')
    expect(document.body.textContent).toContain('Review')
    expect(document.body.textContent).toContain('Output')
    expect(document.body.textContent).toContain('Unavailable')
    expect(document.body.textContent).not.toContain('Approve & Apply')
    expect(document.querySelector<HTMLSelectElement>('.scenario-select select')?.options).toHaveLength(12)
    expect(document.querySelectorAll('[role="treeitem"][tabindex="0"]')).toHaveLength(1)

    await act(async () => root.unmount())
    container.remove()
  })

  it('restores a deterministic scene from the URL', async () => {
    window.history.replaceState({}, '', '/?scene=approval')
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)

    await act(async () => root.render(<App />))

    expect(document.body.textContent).toContain('Approval required')
    expect(document.body.textContent).toContain('Approve fixture')

    await act(async () => root.unmount())
    container.remove()
  })

  it('recovers a failed fixture panel locally without a network or tool action', async () => {
    window.history.replaceState({}, '', '/?scene=partial-error')
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)

    await act(async () => root.render(<App />))

    expect(document.body.textContent).toContain('Review fixture could not render')
    expect(document.body.textContent).toContain('No Agent, file, Git, or tool action was attempted')
    const retryButton = Array.from(document.querySelectorAll('button'))
      .find((button) => button.textContent?.includes('Retry fixture panel'))
    expect(retryButton).toBeDefined()

    await act(async () => retryButton?.click())

    expect(document.body.textContent).toContain('Review fixture restored')
    expect(document.body.textContent).toContain('Local retry restored synthetic panel data')
    expect(fetchMock).not.toHaveBeenCalled()

    await act(async () => root.unmount())
    container.remove()
    fetchMock.mockRestore()
  })

  it('exchanges a launch secret and prepares the P8 realtime workbench', async () => {
    window.history.replaceState({}, '', '/#bootstrap=one-time-secret')
    document.cookie = 'neil_workbench_csrf=test-csrf-token; Path=/'
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(snapshot), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ticket: 'ticket' }), { status: 500 }))
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)

    await act(async () => root.render(<App />))
    await act(async () => Promise.resolve())

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({
      method: 'POST',
      headers: { 'X-Neil-CSRF': 'test-csrf-token' },
    })
    expect(document.body.textContent).toContain('P8 offline · last known')
    expect(document.body.textContent).toContain('Neil-Agent-Live')
    expect(document.body.textContent).toContain('deepseek-live')
    expect(document.body.textContent).toContain('Run Agent')
    expect(window.location.hash).toBe('')

    await act(async () => root.unmount())
    container.remove()
    fetchMock.mockRestore()
  })

  it('shows a persisted session save failure as an explicit recovery state', async () => {
    window.history.replaceState({}, '', '/#bootstrap=one-time-secret')
    document.cookie = 'neil_workbench_csrf=test-csrf-token; Path=/'
    const active = { ...snapshot.active_session!, title: 'Resume work', persistence_status: 'save_failed' as const }
    const failedSnapshot = {
      ...snapshot,
      active_session: active,
      capabilities: { ...snapshot.capabilities, can_start_turn: false },
      sessions: {
        available: true,
        invalid_count: 0,
        total_count: 1,
        items: [{
          session_id: active.session_id,
          title: active.title,
          updated_at: '2026-08-13T08:00:00Z',
          round_count: 1,
          preview: 'Visible preview',
          has_plan: false,
          failed_check: false,
          has_compaction: false,
          runtime_provider: 'deepseek',
          runtime_model: 'deepseek-live',
        }],
      },
    } satisfies WorkbenchSnapshotV1
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(failedSnapshot), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ticket: 'ticket' }), { status: 500 }))
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)

    await act(async () => root.render(<App />))
    await act(async () => Promise.resolve())

    expect(document.querySelector('.session-persistence.is-save_failed')?.textContent).toBe('save failed')
    expect(document.body.textContent).toContain('Create or select a session before continuing')

    await act(async () => root.unmount())
    container.remove()
    fetchMock.mockRestore()
  })

  it('reduces ordered P2 events without losing snapshot metadata', () => {
    const reduced = reduceWorkbenchEvent(snapshot, {
      protocol_version: 1,
      message_type: 'event',
      event_type: 'assistant_text_delta',
      sequence: 1,
      revision: 1,
      timestamp: '2026-08-13T08:00:01Z',
      payload: { text: 'Hello from Agent' },
    })

    expect(reduced.output[0].text).toBe('Hello from Agent')
    expect(reduced.workspace.name).toBe('Neil-Agent-Live')
    expect(reduced.last_sequence).toBe(1)
  })

  it('reduces one live approval request into an actionable review state', () => {
    const reduced = reduceWorkbenchEvent(snapshot, {
      protocol_version: 1,
      message_type: 'event',
      event_type: 'approval_requested',
      sequence: 1,
      revision: 1,
      timestamp: '2026-08-13T08:00:01Z',
      payload: {
        approval: {
          request_id: `approval-${'a'.repeat(32)}`,
          run_id: `run-${'b'.repeat(32)}`,
          tool_name: 'write_file',
          preview: 'Write one file',
          created_at: '2026-08-13T08:00:01Z',
          expires_at: '2026-08-13T08:05:01Z',
          state: 'pending',
          decision_detail: null,
        },
      },
    })

    expect(reduced.review.state).toBe('approval_required')
    expect(reduced.approval?.tool_name).toBe('write_file')
    expect(reduced.capabilities.can_approve_tool).toBe(true)
  })

  it('reduces an atomic runtime model change projection', () => {
    const reduced = reduceWorkbenchEvent(snapshot, {
      protocol_version: 1,
      message_type: 'event',
      event_type: 'model_changed',
      sequence: 1,
      revision: 1,
      timestamp: '2026-08-13T08:00:01Z',
      payload: {
        provider: { ...snapshot.provider, model: 'deepseek-fast', available_models: ['deepseek-fast', 'deepseek-live'] },
        active_session: { ...snapshot.active_session!, runtime_model: 'deepseek-fast' },
        context: snapshot.context,
        review: snapshot.review,
        capabilities: snapshot.capabilities,
      },
    })

    expect(reduced.provider.model).toBe('deepseek-fast')
    expect(reduced.active_session?.runtime_model).toBe('deepseek-fast')
    expect(reduced.provider.available_models).toEqual(['deepseek-fast', 'deepseek-live'])
  })

  it('sends an allowlisted runtime model command through the realtime protocol', async () => {
    document.cookie = 'neil_workbench_csrf=test-csrf-token; Path=/'
    const sockets: FakeWebSocket[] = []
    class FakeWebSocket {
      static readonly OPEN = 1
      readonly readyState = FakeWebSocket.OPEN
      readonly sent: string[] = []
      readonly url: string
      onmessage: ((event: MessageEvent<string>) => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null

      constructor(url: string) {
        this.url = url
        sockets.push(this)
      }

      send(value: string) {
        this.sent.push(value)
      }

      close() {}
    }
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ticket: 'one-time-ticket' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const client = new WorkbenchRealtimeClient({
      onConnection: vi.fn(),
      onSnapshot: vi.fn(),
      onEvent: vi.fn(),
      onControl: vi.fn(),
      onCommandError: vi.fn(),
    })

    client.start(snapshot)
    await vi.waitFor(() => expect(sockets).toHaveLength(1))
    sockets[0].onmessage?.(new MessageEvent('message', {
      data: JSON.stringify({
        protocol_version: 1,
        message_type: 'connected',
        client_id: 'local-client',
        sequence: 0,
        revision: 0,
        control: false,
      }),
    }))
    client.switchModel('deepseek-fast')

    const command = JSON.parse(sockets[0].sent.at(-1) ?? '{}') as Record<string, unknown>
    expect(command.command).toBe('switch_model')
    expect(command.payload).toEqual({ model: 'deepseek-fast' })
    expect(command.expected_revision).toBe(0)

    client.stop()
    fetchMock.mockRestore()
    vi.unstubAllGlobals()
  })

  it('reduces a session change without exposing conversation bodies', () => {
    const activeSession = { session_id: '20260813T081500000000Z-cafebabe', title: 'Resume work', round_count: 2, persistence_status: 'saved' as const, runtime_provider: 'deepseek', runtime_model: 'deepseek-live' }
    const previous = {
      ...snapshot,
      run: { ...snapshot.run, status: 'running' as const, run_id: `run-${'a'.repeat(32)}` },
      output: [{ kind: 'assistant' as const, text: 'old transient output', timestamp: '2026-08-13T08:14:00Z' }],
    }
    const reduced = reduceWorkbenchEvent(previous, {
      protocol_version: 1,
      message_type: 'event',
      event_type: 'session_changed',
      sequence: 1,
      revision: 1,
      timestamp: '2026-08-13T08:15:00Z',
      payload: {
        active_session: activeSession,
        sessions: snapshot.sessions,
        task: snapshot.task,
        context: snapshot.context,
        review: snapshot.review,
        capabilities: snapshot.capabilities,
        reset_runtime: true,
      },
    })

    expect(reduced.active_session).toEqual(activeSession)
    expect(reduced.run.status).toBe('idle')
    expect(reduced.output).toEqual([])
    expect(JSON.stringify(reduced)).not.toContain('old transient output')
  })

  it('loads one bounded live Git diff from a selected changed file', async () => {
    window.history.replaceState({}, '', '/#bootstrap=one-time-secret')
    document.cookie = 'neil_workbench_csrf=test-csrf-token; Path=/'
    const diff = {
      path: 'src/live.py',
      previous_path: null,
      revision: '0123456789abcdef',
      available: true,
      reason: 'available',
      content: '@@ -1 +1,2 @@\n-old\n+new',
      truncated: false,
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(snapshot), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ticket: 'ticket' }), { status: 500 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(diff), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)

    await act(async () => root.render(<App />))
    await act(async () => Promise.resolve())
    const changedFile = Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('src/live.py'))
    expect(changedFile).toBeDefined()
    await act(async () => changedFile?.click())
    await act(async () => Promise.resolve())

    expect(document.body.textContent).toContain('Complete bounded diff')
    expect(document.body.textContent).toContain('@@ -1 +1,2 @@')
    expect(fetchMock.mock.calls.at(-1)?.[0]).toContain('/api/v1/review/diff?')

    await act(async () => root.unmount())
    container.remove()
    fetchMock.mockRestore()
  })
})
