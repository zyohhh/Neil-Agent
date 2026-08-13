import { createRoot } from 'react-dom/client'
import { act } from 'react'
import App from './App'
import { reduceWorkbenchEvent, resetLiveSnapshotRequestForTests, type WorkbenchSnapshotV1 } from './protocol'

const snapshot = {
  schema_version: 1,
  source: 'live',
  generated_at: '2026-08-13T08:00:00Z',
  workspace: { name: 'Neil-Agent-Live', identity: '0123456789abcdef' },
  provider: { provider: 'deepseek', display_name: 'DeepSeek', model: 'deepseek-live', wire_protocol: 'anthropic-messages', thinking_enabled: false },
  run: { status: 'idle', run_id: null, objective: null, started_at: null, finished_at: null, error_type: null },
  revision: 0,
  last_sequence: 0,
  capabilities: { can_start_turn: true, can_cancel_turn: false, can_request_control: true, can_approve_tool: false, tool_permission_mode: 'read_only', has_pty: false },
  timeline: [],
  output: [],
  git: { available: true, branch: 'feature/web-workbench', change_count: 1, files: [{ path: 'src/live.py', status: 'M', kind: 'modified' }], truncated: false },
  sessions: { available: true, items: [], invalid_count: 0, total_count: 0 },
  files: { root: '', items: [{ name: 'src', path: 'src', kind: 'directory', children: [] }], truncated: false },
  task: { source: 'unavailable', session_id: null, steps: [] },
  context: { source: 'unavailable', input_tokens: null, output_tokens: null, total_tokens: null, limit_tokens: 200000 },
  review: { state: 'stale', approval_available: false, cost_available: false },
  security: { mode: 'agent_read_only', binding: 'loopback', bootstrap_token_required: true, write_routes: 0, agent_connected: true },
} satisfies WorkbenchSnapshotV1

describe('WebWorkbenchApp', () => {
  beforeEach(() => {
    resetLiveSnapshotRequestForTests()
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

  it('exchanges a launch secret and prepares the P2 realtime workbench', async () => {
    window.history.replaceState({}, '', '/#bootstrap=one-time-secret')
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
    expect(document.body.textContent).toContain('P2 offline · last known')
    expect(document.body.textContent).toContain('Neil-Agent-Live')
    expect(document.body.textContent).toContain('deepseek-live')
    expect(document.body.textContent).toContain('Run read-only')
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
})
