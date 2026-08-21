import { createRoot } from 'react-dom/client'
import { act } from 'react'
import App from './App'
import { reduceWorkbenchEvent, resetLiveSnapshotRequestForTests, type WorkbenchSnapshotV1 } from './protocol'

;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const snapshot = {
  schema_version: 1,
  source: 'live',
  generated_at: '2026-08-13T08:00:00Z',
  workspace: { name: 'Neil-Agent-Live', identity: '0123456789abcdef' },
  provider: { provider: 'deepseek', display_name: 'DeepSeek', model: 'deepseek-live', wire_protocol: 'anthropic-messages', thinking_enabled: false },
  run: { status: 'idle', run_id: null, objective: null, started_at: null, finished_at: null, error_type: null },
  revision: 0,
  last_sequence: 0,
  capabilities: { can_start_turn: true, can_cancel_turn: false, can_request_control: true, can_select_session: true, can_approve_tool: false, can_show_diff: true, can_estimate_cost: false, tool_permission_mode: 'approval_gated', has_pty: false },
  timeline: [],
  output: [],
  approval: null,
  git: { available: true, branch: 'feature/web-workbench', revision: '0123456789abcdef', change_count: 1, files: [{ path: 'src/live.py', previous_path: null, status: 'M', kind: 'modified', additions: 2, deletions: 1, diff_available: true, diff_reason: 'available' }], truncated: false },
  sessions: { available: true, items: [], invalid_count: 0, total_count: 0, active_session_id: null },
  files: { root: '', items: [{ name: 'src', path: 'src', kind: 'directory', children: [] }], truncated: false, revision: 'fedcba9876543210', unchanged: false },
  task: { source: 'unavailable', session_id: null, steps: [] },
  context: { source: 'unavailable', input_tokens: null, output_tokens: null, total_tokens: null, limit_tokens: 200000 },
  review: { state: 'stale', quality_check: null, quality_checks: [], approval_available: false, cost_available: false, cost: { source: 'unavailable', estimated_usd: null, rate_table_version: null, rate_effective_date: null, model: null, reason: 'no_rate_table' } },
  security: { mode: 'approval_gated', binding: 'loopback', bootstrap_token_required: true, write_routes: 0, agent_connected: true },
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
    expect(document.querySelector<HTMLSelectElement>('select')?.options).toHaveLength(12)
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

  it('exchanges a launch secret and prepares the P7 realtime workbench', async () => {
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
    expect(document.body.textContent).toContain('P7 offline · last known')
    expect(document.body.textContent).toContain('Neil-Agent-Live')
    expect(document.body.textContent).toContain('deepseek-live')
    expect(document.body.textContent).toContain('Run Agent')
    expect(window.location.hash).toBe('')

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

  it('reduces session_changed onto the active session list', () => {
    const reduced = reduceWorkbenchEvent(snapshot, {
      protocol_version: 1,
      message_type: 'event',
      event_type: 'session_changed',
      sequence: 1,
      revision: 1,
      timestamp: '2026-08-13T08:00:01Z',
      payload: {
        session: {
          session_id: '20260813T080000000000Z-abcd1234',
          title: 'Restored session',
          updated_at: '2026-08-13T08:00:00Z',
          round_count: 2,
          preview: 'Visible bounded preview',
          has_plan: true,
          failed_check: false,
          has_compaction: false,
        },
        task: {
          source: 'saved_session',
          session_id: '20260813T080000000000Z-abcd1234',
          steps: [{ title: 'Inspect metadata', status: 'completed' }],
        },
        context: {
          source: 'server_reported',
          input_tokens: 12,
          output_tokens: 4,
          total_tokens: 16,
          limit_tokens: 200000,
        },
      },
    })

    expect(reduced.sessions.active_session_id).toBe('20260813T080000000000Z-abcd1234')
    expect(reduced.sessions.items[0].title).toBe('Restored session')
    expect(reduced.task.steps[0]?.title).toBe('Inspect metadata')
    expect(reduced.context.total_tokens).toBe(16)
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
