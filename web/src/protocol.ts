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

export interface LiveActiveSession {
  session_id: string
  title: string
  round_count: number
  persistence_status: 'unsaved' | 'saved' | 'save_failed'
}

export interface LiveGitFile {
  path: string
  previous_path: string | null
  status: string
  kind: 'modified' | 'added' | 'deleted' | 'renamed' | 'untracked' | 'conflict'
  additions: number | null
  deletions: number | null
  diff_available: boolean
  diff_reason: 'available' | 'untracked' | 'binary' | 'conflict' | 'unavailable'
}

export interface LiveGitDiff {
  path: string
  previous_path: string | null
  revision: string
  available: boolean
  reason: 'available' | 'untracked' | 'binary' | 'conflict' | 'empty' | 'stale' | 'unavailable'
  content: string
  truncated: boolean
}

export interface LiveFileTree {
  root: string
  items: LiveFileNode[]
  truncated: boolean
  revision: string
  unchanged: boolean
}

export type LiveRunStatus = 'idle' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled'

export interface LiveRun {
  status: LiveRunStatus
  run_id: string | null
  objective: string | null
  started_at: string | null
  finished_at: string | null
  error_type: string | null
}

export interface LiveRuntimeStep {
  correlation_id: string
  stage: 'agent_turn' | 'model_request' | 'tool_call' | 'approval' | 'quality_check'
  title: string
  status: 'pending' | 'running' | 'waiting_for_approval' | 'succeeded' | 'failed' | 'skipped' | 'cancelled'
  timestamp: string
  metadata: Record<string, string | number | boolean>
}

export interface LiveOutputEntry {
  kind: 'status' | 'activity' | 'assistant' | 'warning' | 'error'
  text: string
  timestamp: string
}

export interface LiveApproval {
  request_id: string
  run_id: string
  tool_name: string
  preview: string
  created_at: string
  expires_at: string
  state: 'pending' | 'approved' | 'rejected' | 'expired' | 'stale'
  decision_detail: string | null
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
  run: LiveRun
  revision: number
  last_sequence: number
  capabilities: {
    can_start_turn: boolean
    can_cancel_turn: boolean
    can_request_control: boolean
    can_approve_tool: boolean
    can_show_diff: boolean
    can_estimate_cost: boolean
    can_create_session: boolean
    can_select_session: boolean
    tool_permission_mode: 'approval_gated'
    has_pty: false
  }
  timeline: LiveRuntimeStep[]
  output: LiveOutputEntry[]
  approval: LiveApproval | null
  git: { available: boolean; branch: string | null; revision: string | null; change_count: number; files: LiveGitFile[]; truncated: boolean }
  sessions: { available: boolean; items: LiveSession[]; invalid_count: number; total_count: number }
  active_session: LiveActiveSession | null
  files: LiveFileTree
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
    state: 'empty' | 'passed' | 'failed' | 'approval_required' | 'stale' | 'applied' | 'unavailable'
    approval_available: boolean
    quality_check: { check: string; status: 'passed' | 'failed' | 'not_run'; exit_code: number | null } | null
    quality_checks: Array<{ check: string; status: 'passed' | 'failed' | 'not_run'; exit_code: number | null }>
    cost_available: boolean
    cost: {
      source: 'versioned_rate_table' | 'unavailable'
      estimated_usd: string | null
      rate_table_version: string | null
      rate_effective_date: string | null
      model: string | null
      reason: 'no_rate_table' | 'no_saved_usage' | 'model_not_listed' | 'cache_rate_missing' | 'rate_not_effective' | 'estimated'
    }
  }
  security: {
    mode: 'approval_gated'
    binding: 'loopback'
    bootstrap_token_required: true
    write_routes: 0
    agent_connected: true
    sandbox_backend: 'disabled' | 'windows-sandbox'
    audit_enabled: boolean
    shield_schema_version: number
    application_status: 'enforced' | 'ready' | 'disabled' | 'incomplete' | 'unavailable'
    os_sandbox_status: 'enforced' | 'ready' | 'disabled' | 'incomplete' | 'unavailable'
    audit_status: 'recording' | 'busy' | 'disabled' | 'degraded' | 'unavailable'
    tool_count: number
    direct_tool_count: number
    approval_tool_count: number
  }
}

export type LiveConnectionState = 'fixture' | 'connecting' | 'live' | 'offline'

export interface WorkbenchEventV1 {
  protocol_version: 1
  message_type: 'event'
  event_type: 'run_state' | 'assistant_text_delta' | 'activity' | 'runtime_step' | 'approval_requested' | 'approval_resolved' | 'control_changed' | 'session_changed' | 'snapshot_invalidated' | 'service_closing'
  sequence: number
  revision: number
  timestamp: string
  payload: Record<string, unknown>
}

type CommandName = 'acquire_control' | 'start_turn' | 'cancel_turn' | 'approve_tool' | 'reject_tool' | 'new_session' | 'select_session'

interface PendingCommand {
  command: CommandName
  payload: Record<string, unknown>
}

interface ConnectedMessage {
  protocol_version: 1
  message_type: 'connected'
  client_id: string
  sequence: number
  revision: number
  control: boolean
}

interface CommandResult {
  protocol_version: 1
  message_type: 'command_result'
  command_id: string
  status: 'accepted' | 'rejected'
  detail: string
  code: string | null
  run_id: string | null
  sequence: number
  revision: number
}

const getBootstrapSecret = () => {
  const hash = new URLSearchParams(window.location.hash.slice(1))
  const secret = hash.get('bootstrap')
  if (secret) window.history.replaceState({}, '', `${window.location.pathname}${window.location.search}`)
  return secret
}

const getCookie = (name: string) => document.cookie
  .split(';')
  .map((part) => part.trim())
  .find((part) => part.startsWith(`${name}=`))
  ?.slice(name.length + 1)

let liveSnapshotRequest: Promise<WorkbenchSnapshotV1 | null> | null = null

const requestSnapshot = async (exchangeBootstrap: boolean): Promise<WorkbenchSnapshotV1 | null> => {
  const bootstrap = exchangeBootstrap ? getBootstrapSecret() : null
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
}

export const fetchLiveSnapshot = (): Promise<WorkbenchSnapshotV1 | null> => {
  if (new URLSearchParams(window.location.search).has('scene')) return Promise.resolve(null)
  if (!liveSnapshotRequest) liveSnapshotRequest = requestSnapshot(true)
  return liveSnapshotRequest
}

export const refreshLiveSnapshot = () => requestSnapshot(false)

export const fetchLiveFileTree = async (revision: string): Promise<LiveFileTree> => {
  const response = await fetch(`/api/v1/files/tree?depth=2&revision=${encodeURIComponent(revision)}`, {
    credentials: 'include',
    cache: 'no-store',
  })
  if (!response.ok) throw new Error('File tree refresh unavailable')
  return response.json() as Promise<LiveFileTree>
}

export const fetchLiveDiff = async (path: string, revision: string): Promise<LiveGitDiff> => {
  const query = new URLSearchParams({ path, revision })
  const response = await fetch(`/api/v1/review/diff?${query}`, {
    credentials: 'include',
    cache: 'no-store',
  })
  if (!response.ok) throw new Error('Read-only diff unavailable')
  return response.json() as Promise<LiveGitDiff>
}

export const resetLiveSnapshotRequestForTests = () => {
  liveSnapshotRequest = null
}

const isSnapshot = (value: unknown): value is WorkbenchSnapshotV1 => {
  if (!value || typeof value !== 'object') return false
  const record = value as Record<string, unknown>
  const capabilities = record.capabilities as Record<string, unknown> | undefined
  const security = record.security as Record<string, unknown> | undefined
  return record.schema_version === 1
    && record.source === 'live'
    && typeof record.generated_at === 'string'
    && typeof record.revision === 'number'
    && typeof record.last_sequence === 'number'
    && typeof record.run === 'object'
    && typeof record.active_session === 'object'
    && typeof capabilities?.can_create_session === 'boolean'
    && typeof capabilities.can_select_session === 'boolean'
    && typeof security?.shield_schema_version === 'number'
    && typeof security.application_status === 'string'
}

interface RealtimeHandlers {
  onConnection: (state: LiveConnectionState) => void
  onSnapshot: (snapshot: WorkbenchSnapshotV1) => void
  onEvent: (event: WorkbenchEventV1) => void
  onControl: (hasControl: boolean) => void
  onCommandError: (message: string | null) => void
}

export class WorkbenchRealtimeClient {
  private socket: WebSocket | null = null
  private reconnectTimer: number | null = null
  private reconnectDelay = 400
  private stopped = false
  private revision = 0
  private lastSequence = 0
  private clientId: string | null = null
  private commandCounter = 0
  private readonly handlers: RealtimeHandlers
  private readonly pendingCommands = new Map<string, PendingCommand>()
  private resyncing = false

  constructor(handlers: RealtimeHandlers) {
    this.handlers = handlers
  }

  start(snapshot: WorkbenchSnapshotV1) {
    this.revision = snapshot.revision
    this.lastSequence = snapshot.last_sequence
    void this.connect().catch(() => this.handleDisconnect())
  }

  stop() {
    this.stopped = true
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer)
    this.socket?.close(1000, 'Workbench view closed')
    this.socket = null
  }

  startTurn(prompt: string) {
    return this.send('start_turn', { prompt })
  }

  cancelTurn() {
    return this.send('cancel_turn', {})
  }

  approveTool(requestId: string) {
    return this.send('approve_tool', { request_id: requestId })
  }

  rejectTool(requestId: string) {
    return this.send('reject_tool', { request_id: requestId })
  }

  newSession() {
    return this.send('new_session', {})
  }

  selectSession(sessionId: string) {
    return this.send('select_session', { session_id: sessionId })
  }

  private async connect() {
    if (this.stopped) return
    this.handlers.onConnection('connecting')
    const csrfToken = getCookie('neil_workbench_csrf')
    if (!csrfToken) throw new Error('Realtime CSRF token unavailable')
    const ticketResponse = await fetch('/api/v1/ws-ticket', {
      method: 'POST',
      headers: { 'X-Neil-CSRF': csrfToken },
      credentials: 'include',
      cache: 'no-store',
    })
    if (!ticketResponse.ok) throw new Error('Realtime ticket unavailable')
    const ticketPayload = await ticketResponse.json() as { ticket: string }
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(`${scheme}//${window.location.host}/api/v1/events?ticket=${encodeURIComponent(ticketPayload.ticket)}&after=${this.lastSequence}`)
    this.socket = socket
    socket.onmessage = (message) => this.handleMessage(message)
    socket.onclose = () => this.handleDisconnect()
    socket.onerror = () => socket.close()
  }

  private handleMessage(message: MessageEvent<string>) {
    let payload: ConnectedMessage | CommandResult | WorkbenchEventV1
    try {
      payload = JSON.parse(message.data) as ConnectedMessage | CommandResult | WorkbenchEventV1
    } catch {
      this.invalidate()
      return
    }
    if (payload.message_type === 'connected') {
      this.clientId = payload.client_id
      this.revision = Math.max(this.revision, payload.revision)
      this.lastSequence = payload.sequence
      this.reconnectDelay = 400
      this.handlers.onConnection('live')
      this.handlers.onControl(payload.control)
      this.send('acquire_control', {})
      return
    }
    if (payload.message_type === 'command_result') {
      this.revision = payload.revision
      const pending = this.pendingCommands.get(payload.command_id)
      this.pendingCommands.delete(payload.command_id)
      if (payload.status === 'rejected' && payload.code === 'revision_conflict') {
        if (pending && this.socket?.readyState === WebSocket.OPEN) {
          this.send(pending.command, pending.payload)
        } else {
          this.handlers.onCommandError('State changed; refreshing the local snapshot')
          this.invalidate()
        }
      } else if (payload.status === 'rejected') this.handlers.onCommandError(payload.detail)
      else {
        this.handlers.onCommandError(null)
        if (payload.detail === 'Control acquired') this.handlers.onControl(true)
      }
      return
    }
    if (payload.message_type !== 'event') return
    if (payload.event_type === 'snapshot_invalidated' || payload.sequence !== this.lastSequence + 1) {
      this.invalidate()
      return
    }
    this.lastSequence = payload.sequence
    this.revision = payload.revision
    if (payload.event_type === 'control_changed') {
      this.handlers.onControl(payload.payload.holder === this.clientId)
    }
    this.handlers.onEvent(payload)
  }

  private send(command: CommandName, payload: Record<string, unknown>) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      this.handlers.onCommandError('Realtime connection is not ready')
      return false
    }
    this.commandCounter += 1
    const commandId = `web-${Date.now().toString(36)}-${this.commandCounter.toString(36)}`
    this.pendingCommands.set(commandId, { command, payload })
    this.socket.send(JSON.stringify({
      protocol_version: 1,
      message_type: 'command',
      command_id: commandId,
      expected_revision: this.revision,
      command,
      payload,
    }))
    return true
  }

  private invalidate() {
    if (this.resyncing || this.stopped) return
    this.resyncing = true
    this.socket?.close()
    void this.resync()
  }

  private async resync() {
    if (this.stopped) return
    this.handlers.onConnection('connecting')
    try {
      const snapshot = await refreshLiveSnapshot()
      if (!snapshot) throw new Error('Session expired')
      this.revision = snapshot.revision
      this.lastSequence = snapshot.last_sequence
      this.handlers.onSnapshot(snapshot)
      await this.connect()
    } catch {
      this.resyncing = false
      this.handleDisconnect()
      return
    }
    this.resyncing = false
  }

  private handleDisconnect() {
    if (this.stopped || this.resyncing || this.reconnectTimer !== null) return
    this.socket = null
    this.pendingCommands.clear()
    this.handlers.onConnection('offline')
    this.handlers.onControl(false)
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      void this.resync()
    }, this.reconnectDelay)
    this.reconnectDelay = Math.min(5_000, this.reconnectDelay * 2)
  }
}

export const reduceWorkbenchEvent = (
  snapshot: WorkbenchSnapshotV1,
  event: WorkbenchEventV1,
): WorkbenchSnapshotV1 => {
  const base = { ...snapshot, revision: event.revision, last_sequence: event.sequence }
  if (event.event_type === 'run_state') {
    const run = event.payload as unknown as LiveRun
    const active = run.status === 'running' || run.status === 'cancelling'
    const persistenceBlocked = snapshot.active_session?.persistence_status === 'save_failed'
    return {
      ...base,
      run,
      capabilities: {
        ...snapshot.capabilities,
        can_start_turn: !active && !persistenceBlocked,
        can_cancel_turn: active,
        can_create_session: !active,
        can_select_session: !active,
      },
    }
  }
  if (event.event_type === 'session_changed') {
    const payload = event.payload as {
      active_session: WorkbenchSnapshotV1['active_session']
      sessions: WorkbenchSnapshotV1['sessions']
      task: WorkbenchSnapshotV1['task']
      context: WorkbenchSnapshotV1['context']
      review: WorkbenchSnapshotV1['review']
      capabilities: WorkbenchSnapshotV1['capabilities']
      reset_runtime: boolean
    }
    return {
      ...base,
      active_session: payload.active_session,
      sessions: payload.sessions,
      task: payload.task,
      context: payload.context,
      review: payload.review,
      capabilities: payload.capabilities,
      ...(payload.reset_runtime ? {
        run: { status: 'idle', run_id: null, objective: null, started_at: null, finished_at: null, error_type: null } as LiveRun,
        timeline: [],
        output: [],
        approval: null,
      } : {}),
    }
  }
  if (event.event_type === 'runtime_step') {
    const step = event.payload.step as LiveRuntimeStep
    const timeline = snapshot.timeline.filter((item) => item.correlation_id !== step.correlation_id)
    return { ...base, timeline: [...timeline, step].slice(-200) }
  }
  if (event.event_type === 'approval_requested' || event.event_type === 'approval_resolved') {
    const approval = event.payload.approval as LiveApproval
    const pending = approval.state === 'pending'
    return {
      ...base,
      approval,
      review: {
        ...snapshot.review,
        approval_available: pending,
        state: pending
          ? 'approval_required'
          : approval.state === 'approved'
            ? 'applied'
            : approval.state === 'expired' || approval.state === 'stale'
              ? 'stale'
              : approval.state === 'rejected'
                ? snapshot.git.change_count > 0 ? 'stale' : 'empty'
                : snapshot.review.state,
      },
      capabilities: { ...snapshot.capabilities, can_approve_tool: pending },
    }
  }
  if (event.event_type === 'assistant_text_delta' || event.event_type === 'activity') {
    const text = event.event_type === 'assistant_text_delta'
      ? String(event.payload.text ?? '')
      : String(event.payload.message ?? '')
    if (!text) return base
    return {
      ...base,
      output: [...snapshot.output, {
        kind: event.event_type === 'assistant_text_delta' ? 'assistant' : 'activity',
        text,
        timestamp: event.timestamp,
      }].slice(-200) as LiveOutputEntry[],
    }
  }
  return base
}
