export interface LiveFileNode {
  name: string
  path: string
  kind: 'directory' | 'file'
  children: LiveFileNode[]
}

export interface LiveSession {
  session_id: string
  title: string
  updated_at: string
  round_count: number
  preview: string
  has_plan: boolean
  failed_check: boolean
  has_compaction: boolean
}

export interface LiveGitFile {
  path: string
  status: string
  kind: 'modified' | 'added' | 'deleted' | 'renamed' | 'untracked' | 'conflict'
}

export interface WorkbenchSnapshotV1 {
  schema_version: 1
  source: 'live'
  generated_at: string
  workspace: { name: string; identity: string }
  provider: {
    provider: string
    display_name: string
    model: string
    wire_protocol: string
    thinking_enabled: boolean
  }
  run: { status: 'not_connected'; detail: string }
  git: { available: boolean; branch: string | null; change_count: number; files: LiveGitFile[]; truncated: boolean }
  sessions: { available: boolean; items: LiveSession[]; invalid_count: number; total_count: number }
  files: { root: string; items: LiveFileNode[]; truncated: boolean }
  task: {
    source: 'saved_session' | 'unavailable'
    session_id: string | null
    steps: Array<{ title: string; status: 'pending' | 'in_progress' | 'completed' }>
  }
  context: {
    source: 'server_reported' | 'unavailable'
    input_tokens: number | null
    output_tokens: number | null
    total_tokens: number | null
    limit_tokens: number | null
  }
  review: {
    state: 'empty' | 'passed' | 'failed' | 'stale' | 'unavailable'
    approval_available: false
    cost_available: false
  }
  security: {
    mode: 'read_only'
    binding: 'loopback'
    bootstrap_token_required: true
    write_routes: 0
    agent_connected: false
  }
}

export type LiveConnectionState = 'fixture' | 'connecting' | 'live' | 'offline'

const getBootstrapSecret = () => {
  const hash = new URLSearchParams(window.location.hash.slice(1))
  const secret = hash.get('bootstrap')
  if (secret) window.history.replaceState({}, '', `${window.location.pathname}${window.location.search}`)
  return secret
}

let liveSnapshotRequest: Promise<WorkbenchSnapshotV1 | null> | null = null

export const fetchLiveSnapshot = (): Promise<WorkbenchSnapshotV1 | null> => {
  if (new URLSearchParams(window.location.search).has('scene')) return Promise.resolve(null)
  if (liveSnapshotRequest) return liveSnapshotRequest
  liveSnapshotRequest = (async () => {
    const bootstrap = getBootstrapSecret()
    if (bootstrap) {
      const exchange = await fetch('/api/v1/bootstrap', {
        method: 'POST',
        headers: { 'X-Neil-Bootstrap': bootstrap },
        credentials: 'include',
      })
      if (!exchange.ok) throw new Error('Local bootstrap failed')
    }
    const response = await fetch('/api/v1/snapshot', {
      credentials: 'include',
      cache: 'no-store',
    })
    if (response.status === 401 && !bootstrap) return null
    if (!response.ok) throw new Error('Local snapshot unavailable')
    const payload: unknown = await response.json()
    if (!isSnapshot(payload)) throw new Error('Local snapshot contract mismatch')
    return payload
  })()
  return liveSnapshotRequest
}

export const resetLiveSnapshotRequestForTests = () => {
  liveSnapshotRequest = null
}

const isSnapshot = (value: unknown): value is WorkbenchSnapshotV1 => {
  if (!value || typeof value !== 'object') return false
  const record = value as Record<string, unknown>
  return record.schema_version === 1 && record.source === 'live' && typeof record.generated_at === 'string'
}
