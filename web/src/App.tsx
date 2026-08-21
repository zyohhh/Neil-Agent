import { useEffect, useId, useRef, useState } from 'react'
import './App.css'
import {
  fetchLiveDiff,
  type LiveConnectionState,
  type LiveGitDiff,
  type LiveRuntimeStep,
  type WorkbenchSnapshotV1,
} from './protocol'
import {
  fixtureChangedFiles,
  fixtureFiles,
  fixtureSessions,
  fixtureTimeline,
  scenarios,
  toFileNodes,
  type FileNode,
  type Scenario,
  type TimelineStep,
} from './fixtures'
import { useWorkbenchConnection } from './useWorkbenchConnection'

type IconName =
  | 'spark'
  | 'chevron'
  | 'folder'
  | 'file'
  | 'code'
  | 'search'
  | 'branch'
  | 'pencil'
  | 'flask'
  | 'check'
  | 'plus'
  | 'settings'
  | 'panel'
  | 'x'
  | 'menu'

function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, React.ReactNode> = {
    spark: <><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/><circle cx="12" cy="12" r="3.5"/></>,
    chevron: <path d="m9 18 6-6-6-6" />,
    folder: <path d="M3 7.5V5a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" />,
    file: <><path d="M6 2h8l4 4v16H6z"/><path d="M14 2v5h5"/></>,
    code: <><path d="m8 9-3 3 3 3M16 9l3 3-3 3M14 5l-4 14"/></>,
    search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
    branch: <><circle cx="6" cy="5" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10M8 7c7 0 3 10 8 10M16 17h2"/></>,
    pencil: <><path d="m4 20 4.5-1 10-10a2.1 2.1 0 0 0-3-3l-10 10Z"/><path d="m14 7 3 3"/></>,
    flask: <><path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4A2 2 0 0 0 19 18l-5-9V3"/><path d="M8 15h8"/></>,
    check: <path d="m5 12 4 4L19 6" />,
    plus: <path d="M12 5v14M5 12h14" />,
    settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1V21h-4v-.09A1.7 1.7 0 0 0 8.6 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1-.4H3v-4h.09A1.7 1.7 0 0 0 4.6 8.6a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1V3h4v.09A1.7 1.7 0 0 0 15.4 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.2.37.53.72 1 .94.3.14.62.2 1 .2H21v4h-.09A1.7 1.7 0 0 0 19.4 15Z"/></>,
    panel: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 4v16M8 9h13"/></>,
    x: <path d="m6 6 12 12M18 6 6 18" />,
    menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  }

  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  )
}

function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <span className={`brand-mark brand-mark-${size}`} aria-hidden="true">
      <Icon name="spark" size={size} />
    </span>
  )
}

function FileTreeNode({
  node,
  activeId,
  onActiveChange,
  level = 0,
}: {
  node: FileNode
  activeId: string
  onActiveChange: (id: string) => void
  level?: number
}) {
  const [expanded, setExpanded] = useState(level === 0 || node.id === 'web')
  const hasChildren = Boolean(node.children?.length)

  if (node.kind === 'folder') {
    return (
      <li className="tree-group" role="none">
        <button
          type="button"
          className="tree-row"
          data-level={Math.min(level, 4)}
          role="treeitem"
          data-tree-id={node.id}
          aria-expanded={expanded}
          tabIndex={activeId === node.id ? 0 : -1}
          onFocus={() => onActiveChange(node.id)}
          onClick={() => {
            onActiveChange(node.id)
            setExpanded((value) => !value)
          }}
        >
          <span className={`tree-chevron ${expanded ? 'is-open' : ''}`}><Icon name="chevron" size={14} /></span>
          <Icon name="folder" size={18} />
          <span>{node.name}</span>
        </button>
        {expanded && hasChildren ? (
          <ul role="group">
            {node.children?.map((child) => (
              <FileTreeNode
                key={child.id}
                node={child}
                activeId={activeId}
                onActiveChange={onActiveChange}
                level={level + 1}
              />
            ))}
          </ul>
        ) : null}
      </li>
    )
  }

  return (
    <li role="none">
      <button
        type="button"
        className="tree-row tree-file"
        data-level={Math.min(level, 4)}
        role="treeitem"
        data-tree-id={node.id}
        tabIndex={activeId === node.id ? 0 : -1}
        onFocus={() => onActiveChange(node.id)}
        onClick={() => onActiveChange(node.id)}
        aria-label={`${node.name}, fixture file preview`}
      >
        <span className="tree-chevron tree-spacer" />
        <Icon name={node.language === 'tsx' ? 'code' : 'file'} size={17} />
        <span>{node.name}</span>
      </button>
    </li>
  )
}

function Sidebar({
  open,
  onClose,
  interactionLocked,
  drawerMode,
  liveSnapshot,
  hasControl,
  onRefreshFiles,
  onSelectSession,
  onNewSession,
}: {
  open: boolean
  onClose: () => void
  interactionLocked: boolean
  drawerMode: boolean
  liveSnapshot: WorkbenchSnapshotV1 | null
  hasControl: boolean
  onRefreshFiles: () => void
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
}) {
  const [selectedSession, setSelectedSession] = useState('workbench')
  const [activeTreeItem, setActiveTreeItem] = useState('src')
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const visibleFiles = liveSnapshot ? toFileNodes(liveSnapshot.files.items) : fixtureFiles
  const activeSessionId = liveSnapshot?.sessions.active_session_id ?? selectedSession
  const visibleSessions = liveSnapshot
    ? liveSnapshot.sessions.items.map((session) => ({
      id: session.session_id,
      title: session.title,
      time: new Date(session.updated_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
      active: session.session_id === activeSessionId,
    }))
    : fixtureSessions

  useEffect(() => {
    if (liveSnapshot?.sessions.active_session_id) setSelectedSession(liveSnapshot.sessions.active_session_id)
  }, [liveSnapshot])

  useEffect(() => {
    if (open && drawerMode) closeButtonRef.current?.focus()
  }, [drawerMode, open])

  const handleTreeKeyDown = (event: React.KeyboardEvent<HTMLUListElement>) => {
    const target = (event.target as HTMLElement).closest<HTMLButtonElement>('[role="treeitem"]')
    if (!target) return

    const visibleItems = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="treeitem"]'),
    ).filter((item) => item.getClientRects().length > 0)
    const currentIndex = visibleItems.indexOf(target)
    const focusItem = (item?: HTMLButtonElement) => {
      if (!item) return
      const id = item.dataset.treeId
      if (id) setActiveTreeItem(id)
      item.focus()
    }

    if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
      event.preventDefault()
      if (event.key === 'Home') focusItem(visibleItems[0])
      if (event.key === 'End') focusItem(visibleItems.at(-1))
      if (event.key === 'ArrowDown') focusItem(visibleItems[Math.min(currentIndex + 1, visibleItems.length - 1)])
      if (event.key === 'ArrowUp') focusItem(visibleItems[Math.max(currentIndex - 1, 0)])
    }

    if (event.key === 'ArrowRight') {
      const expanded = target.getAttribute('aria-expanded')
      if (expanded === 'false') {
        event.preventDefault()
        target.click()
      } else if (expanded === 'true') {
        event.preventDefault()
        focusItem(visibleItems[currentIndex + 1])
      }
    }

    if (event.key === 'ArrowLeft') {
      const expanded = target.getAttribute('aria-expanded')
      if (expanded === 'true') {
        event.preventDefault()
        target.click()
      } else {
        const parentItem = target
          .closest('ul[role="group"]')
          ?.closest('li.tree-group')
          ?.querySelector<HTMLButtonElement>(':scope > [role="treeitem"]')
        if (parentItem) {
          event.preventDefault()
          focusItem(parentItem)
        }
      }
    }
  }

  return (
    <aside
      className={`sidebar ${open ? 'is-open' : ''}`}
      aria-label="Project and sessions"
      aria-hidden={drawerMode && !open ? true : undefined}
      aria-modal={drawerMode && open ? true : undefined}
      inert={drawerMode && !open ? true : undefined}
      role={drawerMode && open ? 'dialog' : undefined}
      tabIndex={drawerMode ? -1 : 0}
    >
      <div className="mobile-panel-heading">
        <span>Navigation</span>
        <button ref={closeButtonRef} className="icon-button" type="button" onClick={onClose} aria-label="Close navigation">
          <Icon name="x" />
        </button>
      </div>
      <section className="sidebar-section project-section" aria-labelledby="project-heading">
        <div className="section-heading-row">
          <p className="eyebrow" id="project-heading">Project</p>
          {liveSnapshot ? <button type="button" className="text-button" onClick={onRefreshFiles}>Refresh</button> : null}
        </div>
        <ul className="file-tree" role="tree" aria-label="Fixture project files" onKeyDown={handleTreeKeyDown}>
          {visibleFiles.map((file) => (
            <FileTreeNode
              key={file.id}
              node={file}
              activeId={activeTreeItem}
              onActiveChange={setActiveTreeItem}
            />
          ))}
        </ul>
      </section>

      <section className="sidebar-section sessions-section" aria-labelledby="sessions-heading">
        <div className="section-heading-row">
          <p className="eyebrow" id="sessions-heading">Sessions</p>
          {liveSnapshot ? (
            <button
              type="button"
              className="text-button"
              onClick={onNewSession}
              disabled={interactionLocked || !hasControl || liveSnapshot.capabilities.can_select_session === false}
            >
              New
            </button>
          ) : null}
        </div>
        <div className="session-list">
          {visibleSessions.map((session) => (
            <button
              type="button"
              key={session.id}
              className={`session-item ${selectedSession === session.id ? 'is-active' : ''}`}
              onClick={() => {
                if (interactionLocked) return
                if (liveSnapshot) {
                  if (!hasControl || liveSnapshot.capabilities.can_select_session === false) return
                  onSelectSession(session.id)
                  return
                }
                setSelectedSession(session.id)
              }}
              aria-pressed={selectedSession === session.id}
              aria-disabled={interactionLocked && selectedSession !== session.id}
              aria-describedby={interactionLocked ? 'session-lock-reason' : undefined}
            >
              <span className="status-orbit"><Icon name={session.active ? 'spark' : 'plus'} size={15} /></span>
              <span className="session-title">{session.title}</span>
              <time>{session.time}</time>
            </button>
          ))}
        </div>
        <span className="sr-only" id="session-lock-reason">Session switching is disabled while a run or approval is active.</span>
      </section>

      <button type="button" className="settings-button" aria-label="Fixture settings are not implemented in P0" disabled>
        <Icon name="settings" size={22} />
      </button>
    </aside>
  )
}

function TimelineBody({ type }: { type?: TimelineStep['body'] }) {
  if (type === 'code') {
    return (
      <pre className="code-preview" aria-label="Fixture code preview"><code><span className="line-number">23</span> <span className="syntax-keyword">interface</span> WorkbenchSnapshot {'{'}{`\n`}<span className="line-number">24</span>   version: <span className="syntax-number">1</span>{`\n`}<span className="line-number">25</span>   source: <span className="syntax-string">'fixture'</span>{`\n`}<span className="line-number">26</span> {'}'}</code></pre>
    )
  }

  if (type === 'plan') {
    return (
      <ul className="plan-preview">
        <li>Define fixture protocol states</li>
        <li>Build the responsive workbench shell</li>
        <li>Add keyboard and drawer interactions</li>
        <li>Capture visual baselines</li>
      </ul>
    )
  }

  if (type === 'test') {
    return <p className="test-result"><strong>12 passed</strong> in 1.8s</p>
  }

  if (type === 'summary') {
    return <p className="summary-copy">The P0 workbench shell is rendering from local, synthetic fixtures. No Agent process, file operation, approval, model request, or cost calculation is active.</p>
  }

  return null
}

function Timeline({ scenario }: { scenario: Scenario }) {
  const [expandedSteps, setExpandedSteps] = useState<string[]>(['read', 'plan', 'test', 'summary'])
  const steps = fixtureTimeline.map((step) => {
    if (scenario.id === 'loading') return { ...step, status: 'pending' as const }
    if (scenario.id === 'idle') return { ...step, status: 'skipped' as const }
    if (scenario.id === 'cancelled') {
      if (step.id === 'summary') return { ...step, title: 'Cancelled', status: 'cancelled' as const }
      if (step.id === 'test') return { ...step, status: 'skipped' as const }
    }
    if (step.id !== 'summary') return step
    if (scenario.id === 'completed') return { ...step, status: 'succeeded' as const }
    if (scenario.id === 'approval') return { ...step, title: 'Approval', status: 'waiting' as const }
    if (scenario.id === 'failed') return { ...step, title: 'Summary', status: 'failed' as const }
    if (scenario.id === 'offline') return { ...step, title: 'Last-known running', status: 'running' as const }
    return step
  })

  const iconForStep: Record<TimelineStep['kind'], IconName> = {
    search: 'search',
    read: 'file',
    plan: 'branch',
    edit: 'pencil',
    test: 'flask',
    summary: 'spark',
  }

  const toggleStep = (id: string) => {
    setExpandedSteps((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])
  }

  return (
    <ol className="timeline" aria-label="Fixture Agent timeline">
      {steps.map((step, index) => {
        const expanded = expandedSteps.includes(step.id)
        return (
          <li className={`timeline-step step-${step.status}`} key={step.id}>
            <span className="timeline-node"><Icon name={iconForStep[step.kind]} size={20} /></span>
            <div className="timeline-content">
              <button
                type="button"
                className="step-heading"
                aria-expanded={expanded}
                onClick={() => toggleStep(step.id)}
              >
                <span>
                  <strong>{step.title}</strong>
                  {step.subtitle ? <small>{step.subtitle}</small> : null}
                </span>
                <span className="step-meta">
                  <time dateTime={step.datetime}>{step.time}</time>
                  <span className="step-check" aria-label={step.status}><Icon name={step.status === 'failed' ? 'x' : 'check'} size={15} /></span>
                </span>
              </button>
              {expanded ? <TimelineBody type={step.body} /> : null}
            </div>
            {index < steps.length - 1 ? <span className="timeline-rail" /> : null}
          </li>
        )
      })}
    </ol>
  )
}

const liveStepIcon: Record<LiveRuntimeStep['stage'], IconName> = {
  agent_turn: 'spark',
  model_request: 'search',
  tool_call: 'panel',
  approval: 'check',
  quality_check: 'flask',
}

function LiveTimeline({ steps }: { steps: LiveRuntimeStep[] }) {
  if (steps.length === 0) {
    return <div className="live-empty-state"><strong>No active timeline</strong><span>Start an Agent task to stream runtime events here.</span></div>
  }
  return (
    <ol className="timeline" aria-label="Live Agent timeline">
      {steps.map((step, index) => (
        <li className={`timeline-step step-${step.status === 'waiting_for_approval' ? 'waiting' : step.status}`} key={step.correlation_id}>
          <span className="timeline-node"><Icon name={liveStepIcon[step.stage]} size={20} /></span>
          <div className="timeline-content">
            <div className="step-heading live-step-heading">
              <span><strong>{step.title}</strong><small>{step.stage.replaceAll('_', ' ')}</small></span>
              <span className="step-meta">
                <time dateTime={step.timestamp}>{new Date(step.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>
                <span className="step-check" aria-label={step.status}><Icon name={step.status === 'failed' ? 'x' : 'check'} size={15} /></span>
              </span>
            </div>
          </div>
          {index < steps.length - 1 ? <span className="timeline-rail" /> : null}
        </li>
      ))}
    </ol>
  )
}

function ContextGauge({ liveSnapshot }: { liveSnapshot: WorkbenchSnapshotV1 | null }) {
  const totalTokens = liveSnapshot ? (liveSnapshot.context.total_tokens ?? 0) : 142_000
  const limitTokens = liveSnapshot ? liveSnapshot.context.limit_tokens : 200_000
  const hasLiveUsage = Boolean(liveSnapshot && liveSnapshot.context.source === 'server_reported' && liveSnapshot.context.total_tokens !== null)
  const progressMax = limitTokens ?? Math.max(totalTokens, 1)
  const progressPercent = hasLiveUsage || !liveSnapshot ? Math.min((totalTokens / progressMax) * 100, 100) : 0
  return (
    <div className="context-gauge" role="progressbar" aria-label={hasLiveUsage ? 'Last server-reported token usage' : liveSnapshot ? 'Context usage unavailable' : 'Fixture context usage'} aria-valuemin={0} aria-valuemax={progressMax} aria-valuenow={hasLiveUsage || !liveSnapshot ? totalTokens : undefined}>
      <svg viewBox="0 0 160 94" aria-hidden="true">
        <path className="gauge-track" d="M18 80a62 62 0 0 1 124 0" pathLength="100" />
        <path className="gauge-value" d="M18 80a62 62 0 0 1 124 0" pathLength="100" strokeDasharray={`${progressPercent} 100`} />
      </svg>
      <span>
        <strong>{hasLiveUsage ? totalTokens.toLocaleString() : liveSnapshot ? 'Unavailable' : '142K'}</strong>
        <small>{hasLiveUsage ? (limitTokens ? `/ ${limitTokens.toLocaleString()}` : 'last saved run') : liveSnapshot ? 'No saved usage' : '/ 200K fixture'}</small>
      </span>
    </div>
  )
}

function ReviewPanel({
  open,
  onClose,
  scenario,
  drawerMode,
  liveSnapshot,
  hasControl,
  onApproveTool,
  onRejectTool,
  onRefreshReview,
}: {
  open: boolean
  onClose: () => void
  scenario: Scenario
  drawerMode: boolean
  liveSnapshot: WorkbenchSnapshotV1 | null
  hasControl: boolean
  onApproveTool: (requestId: string) => void
  onRejectTool: (requestId: string) => void
  onRefreshReview: () => void
}) {
  const [decision, setDecision] = useState<'none' | 'approved' | 'rejected'>('none')
  const [fixtureReviewRecovered, setFixtureReviewRecovered] = useState(false)
  const [selectedDiff, setSelectedDiff] = useState<LiveGitDiff | null>(null)
  const [diffState, setDiffState] = useState<'idle' | 'loading' | 'error'>('idle')
  const fixtureReviewUnavailable = !liveSnapshot
    && scenario.id === 'partial-error'
    && !fixtureReviewRecovered
  const liveApproval = liveSnapshot?.approval ?? null
  const approvalAvailable = liveSnapshot
    ? Boolean(liveApproval?.state === 'pending' && liveSnapshot.capabilities.can_approve_tool && hasControl)
    : scenario.reviewState === 'approval' && decision === 'none'
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const visibleChangedFiles = liveSnapshot
    ? liveSnapshot.git.files.map((file) => ({
      id: `${file.status}:${file.path}`,
      name: file.path,
      added: file.additions,
      deleted: file.deletions,
      status: file.status,
    }))
    : fixtureChangedFiles.map((file) => ({ ...file, status: null }))
  const reviewLabel = liveSnapshot
    ? ({
      empty: 'Working tree clean',
      passed: 'Latest saved check passed',
      failed: 'Latest saved check failed',
      approval_required: 'One tool needs approval',
      stale: 'Changes need a new check',
      applied: 'One approved tool executed',
      unavailable: 'Review unavailable',
    } as const)[liveSnapshot.review.state]
    : fixtureReviewRecovered && scenario.id === 'partial-error'
      ? 'Review fixture restored'
      : scenario.reviewLabel
  const reviewDetail = liveSnapshot
    ? `${liveSnapshot.git.change_count} read-only Git change${liveSnapshot.git.change_count === 1 ? '' : 's'}`
    : fixtureReviewRecovered && scenario.id === 'partial-error'
      ? 'Local retry restored synthetic panel data'
      : scenario.reviewDetail
  const reviewState = liveSnapshot
    ? liveSnapshot.review.state === 'unavailable' ? 'failed' : liveSnapshot.review.state
    : fixtureReviewRecovered && scenario.id === 'partial-error'
      ? 'stale'
      : scenario.reviewState

  useEffect(() => {
    setDecision('none')
    setFixtureReviewRecovered(false)
  }, [scenario.id])
  useEffect(() => {
    setSelectedDiff(null)
    setDiffState('idle')
  }, [liveSnapshot?.git.revision])
  useEffect(() => {
    if (open && drawerMode) closeButtonRef.current?.focus()
  }, [drawerMode, open])

  if (fixtureReviewUnavailable) {
    return (
      <aside
        className={`review-panel panel ${open ? 'is-open' : ''}`}
        aria-labelledby="review-heading"
        aria-hidden={drawerMode && !open ? true : undefined}
        aria-modal={drawerMode && open ? true : undefined}
        inert={drawerMode && !open ? true : undefined}
        role={drawerMode && open ? 'dialog' : undefined}
        tabIndex={drawerMode ? -1 : 0}
      >
        <div className="panel-title-row">
          <div className="panel-title"><BrandMark size={23} /><h2 id="review-heading">Review</h2></div>
          <button ref={closeButtonRef} className="icon-button mobile-close" type="button" onClick={onClose} aria-label="Close review">
            <Icon name="x" />
          </button>
        </div>
        <section className="review-recovery" role="alert" tabIndex={0} aria-labelledby="review-recovery-heading">
          <span className="recovery-icon" aria-hidden="true"><Icon name="x" size={22} /></span>
          <p className="eyebrow">Panel unavailable</p>
          <h3 id="review-recovery-heading">Review fixture could not render</h3>
          <p>The workspace and Output remain available. No Agent, file, Git, or tool action was attempted.</p>
          <button type="button" className="recovery-button" onClick={() => setFixtureReviewRecovered(true)}>
            Retry fixture panel
          </button>
          <small>Local preview recovery only · no network request</small>
        </section>
      </aside>
    )
  }

  return (
    <aside
      className={`review-panel panel ${open ? 'is-open' : ''}`}
      aria-labelledby="review-heading"
      aria-hidden={drawerMode && !open ? true : undefined}
      aria-modal={drawerMode && open ? true : undefined}
      inert={drawerMode && !open ? true : undefined}
      role={drawerMode && open ? 'dialog' : undefined}
      tabIndex={drawerMode ? -1 : 0}
    >
      <div className="panel-title-row">
        <div className="panel-title"><BrandMark size={23} /><h2 id="review-heading">Review</h2></div>
        <div className="review-heading-actions">
          {liveSnapshot ? <button type="button" className="text-button" onClick={onRefreshReview}>Refresh</button> : null}
          <button ref={closeButtonRef} className="icon-button mobile-close" type="button" onClick={onClose} aria-label="Close review">
            <Icon name="x" />
          </button>
        </div>
      </div>

      <section className="review-section">
        <p className="eyebrow">Current step</p>
        <div className={`review-status status-${reviewState}`}>
          <span className="status-dot" />
          <span><strong>{reviewLabel}</strong><small>{reviewDetail}</small></span>
        </div>
      </section>

      <section className="review-section">
        <p className="eyebrow">Changed files <span className="fixture-tag">{liveSnapshot ? 'read-only Git' : 'fixture'}</span></p>
        <div className="changed-files">
          {visibleChangedFiles.map((file) => (
            <button
              type="button"
              key={file.id}
              className={`changed-file ${selectedDiff?.path === file.name ? 'is-selected' : ''}`}
              disabled={!liveSnapshot || !liveSnapshot.git.revision || !liveSnapshot.capabilities.can_show_diff}
              onClick={() => {
                if (!liveSnapshot?.git.revision) return
                setDiffState('loading')
                void fetchLiveDiff(file.name, liveSnapshot.git.revision)
                  .then((diff) => {
                    setSelectedDiff(diff)
                    setDiffState('idle')
                  })
                  .catch(() => setDiffState('error'))
              }}
              aria-label={file.added === null ? `${file.name}, Git status ${file.status}` : `${file.name}, added ${file.added} lines, deleted ${file.deleted} lines`}
            >
              <span className="file-bullet" />
              <Icon name="file" size={17} />
              <span className="changed-name">{file.name}</span>
              {file.added === null ? <span className="git-status-code">{file.status}</span> : (
                <>
                  <span className="diff-added" aria-label={`added ${file.added} lines`}>+{file.added}</span>
                  <span className="diff-deleted" aria-label={`deleted ${file.deleted} lines`}>−{file.deleted}</span>
                </>
              )}
            </button>
          ))}
        </div>
        {liveSnapshot ? (
          <div className="diff-viewer" aria-live="polite">
            {diffState === 'loading' ? <p>Loading bounded diff…</p> : null}
            {diffState === 'error' ? <p>Read-only diff could not be loaded.</p> : null}
            {selectedDiff?.available ? (
              <>
                <div className="diff-heading"><strong>{selectedDiff.path}</strong><span>{selectedDiff.truncated ? 'Truncated at 40K' : 'Complete bounded diff'}</span></div>
                <pre tabIndex={0}><code>{selectedDiff.content}</code></pre>
              </>
            ) : selectedDiff ? <p>No text diff: {selectedDiff.reason.replaceAll('_', ' ')}.</p> : null}
          </div>
        ) : null}
      </section>

      {liveSnapshot && liveSnapshot.review.quality_checks.length > 0 ? (
        <section className="review-section quality-history">
          <p className="eyebrow">Quality history <span className="fixture-tag">bounded</span></p>
          <ol>
            {liveSnapshot.review.quality_checks.map((check, index) => (
              <li key={`${check.check}:${index}`}><span>{check.check}</span><strong>{check.status}</strong></li>
            ))}
          </ol>
        </section>
      ) : null}

      <section className="review-section metrics-section">
        <div>
          <p className="eyebrow">Context</p>
          <ContextGauge liveSnapshot={liveSnapshot} />
        </div>
        <div className="cost-block">
          <p className="eyebrow">Cost</p>
          <strong>{liveSnapshot?.review.cost_available ? `$${liveSnapshot.review.cost.estimated_usd}` : 'Unavailable'}</strong>
          <small>{liveSnapshot?.review.cost_available ? `Estimate · ${liveSnapshot.review.cost.rate_table_version}` : liveSnapshot ? liveSnapshot.review.cost.reason.replaceAll('_', ' ') : 'No rate table'}</small>
        </div>
      </section>

      <section className="approval-section">
        <p className="eyebrow">Single-tool approval</p>
        <div className={`approval-card ${approvalAvailable ? 'is-ready' : ''}`}>
          <span>
            <strong>{liveApproval ? `${liveApproval.tool_name} · ${liveApproval.state}` : decision === 'approved' ? 'Fixture approved' : decision === 'rejected' ? 'Fixture rejected' : approvalAvailable ? 'Write App.tsx' : 'No pending tool'}</strong>
            <small>{liveApproval?.decision_detail ?? (liveApproval ? 'This decision applies to exactly one tool preview' : approvalAvailable ? 'One synthetic action' : 'No real side effect')}</small>
          </span>
          <span className="shield-badge"><Icon name={decision === 'rejected' ? 'x' : 'check'} size={17} /></span>
        </div>
        {liveApproval ? (
          <pre className="approval-preview" tabIndex={0} aria-label={`Preview for ${liveApproval.tool_name}`}><code>{liveApproval.preview}</code></pre>
        ) : null}
        <button
          type="button"
          className="approve-button"
          disabled={!approvalAvailable}
          onClick={() => {
            if (liveApproval) onApproveTool(liveApproval.request_id)
            else setDecision('approved')
          }}
        >
          {liveSnapshot ? 'Approve one tool' : 'Approve fixture'}
        </button>
        <button
          type="button"
          className="reject-button"
          disabled={!approvalAvailable}
          onClick={() => {
            if (liveApproval) onRejectTool(liveApproval.request_id)
            else setDecision('rejected')
          }}
        >
          {liveSnapshot ? 'Reject one tool' : 'Reject fixture'}
        </button>
      </section>
    </aside>
  )
}

function OutputPanel({
  scenario,
  liveSnapshot,
  collapsed,
  height,
  onCollapsedChange,
  onHeightChange,
}: {
  scenario: Scenario
  liveSnapshot: WorkbenchSnapshotV1 | null
  collapsed: boolean
  height: number
  onCollapsedChange: (collapsed: boolean) => void
  onHeightChange: (height: number) => void
}) {
  const [cleared, setCleared] = useState(false)
  const outputSource = liveSnapshot ? 'live Agent' : 'fixture'
  const visibleOutputLines = liveSnapshot
    ? liveSnapshot.output.map((entry) => entry.text)
    : scenario.outputLines

  useEffect(() => setCleared(false), [scenario.id])

  const outputMaximum = Math.max(128, Math.min(720, Math.floor((window.innerHeight * 0.45) / 16) * 16))
  const updateHeight = (nextHeight: number) => {
    const snapped = Math.round(nextHeight / 16) * 16
    onHeightChange(Math.max(128, Math.min(outputMaximum, snapped)))
  }

  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId)
    const initialY = event.clientY
    const initialHeight = height
    const onMove = (moveEvent: PointerEvent) => updateHeight(initialHeight + initialY - moveEvent.clientY)
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  return (
    <section
      className={`output-panel panel ${collapsed ? 'is-collapsed' : ''}`}
      aria-labelledby="output-heading"
    >
      {!collapsed ? (
        <div
          className="output-resizer"
          role="separator"
          aria-label={`Resize ${outputSource} output`}
          aria-orientation="horizontal"
          aria-valuemin={128}
          aria-valuemax={outputMaximum}
          aria-valuenow={Math.round(height)}
          tabIndex={0}
          onPointerDown={startResize}
          onKeyDown={(event) => {
            if (event.key === 'ArrowUp') updateHeight(height + 16)
            if (event.key === 'ArrowDown') updateHeight(height - 16)
            if (event.key === 'Home') updateHeight(128)
            if (event.key === 'End') updateHeight(outputMaximum)
          }}
        />
      ) : null}
      <div className="output-toolbar">
        <button type="button" className="output-title" onClick={() => onCollapsedChange(!collapsed)} aria-expanded={!collapsed}>
          <Icon name="panel" size={19} />
          <strong id="output-heading">Output</strong>
          <span className="fixture-tag">{liveSnapshot ? 'live stream' : 'fixture'}</span>
          <span className={`tree-chevron ${collapsed ? '' : 'is-open'}`}><Icon name="chevron" size={14} /></span>
        </button>
        <div className="output-actions">
          <span>{liveSnapshot?.git.branch ?? 'feature/web-workbench'}</span>
          <span className="status-dot" />
          <button type="button" className="icon-button" aria-label={`Clear displayed ${outputSource} output locally`} onClick={() => setCleared(true)}><Icon name="x" size={16} /></button>
          <button type="button" className="icon-button" onClick={() => updateHeight(304)} aria-label="Expand output"><Icon name="plus" size={17} /></button>
        </div>
      </div>
      {!collapsed ? (
        <div className="output-stream" role="log" aria-label={liveSnapshot ? 'Live Agent output' : 'Synthetic fixture output'}>
          {(cleared ? [`${liveSnapshot ? 'Live' : 'Fixture'} output cleared locally.`] : visibleOutputLines).map((line, index) => (
            <div key={`${scenario.id}-${index}`} className={line.startsWith('×') || line.startsWith('!') ? 'output-warning' : line.startsWith('✓') ? 'output-success' : ''}>{line}</div>
          ))}
        </div>
      ) : null}
    </section>
  )
}

function Header({
  onOpenSidebar,
  onOpenReview,
  sidebarTriggerRef,
  reviewTriggerRef,
  liveSnapshot,
  connectionState,
  interactionLocked,
  fixtureStatusLabel,
  fixtureTone,
}: {
  onOpenSidebar: () => void
  onOpenReview: () => void
  sidebarTriggerRef: React.RefObject<HTMLButtonElement | null>
  reviewTriggerRef: React.RefObject<HTMLButtonElement | null>
  liveSnapshot: WorkbenchSnapshotV1 | null
  connectionState: LiveConnectionState
  interactionLocked: boolean
  fixtureStatusLabel: string
  fixtureTone: Scenario['runTone']
}) {
  const [mode, setMode] = useState<'focus' | 'build'>('build')
  const focusModeRef = useRef<HTMLButtonElement>(null)
  const buildModeRef = useRef<HTMLButtonElement>(null)
  const selectMode = (nextMode: 'focus' | 'build', moveFocus = false) => {
    if (interactionLocked) return
    setMode(nextMode)
    if (moveFocus) {
      window.setTimeout(() => (nextMode === 'focus' ? focusModeRef : buildModeRef).current?.focus(), 0)
    }
  }
  const setModeFromKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (interactionLocked) return
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault()
      selectMode(mode === 'focus' ? 'build' : 'focus', true)
    }
  }

  return (
    <header className="global-header">
      <div className="header-left">
        <div className="brand-lockup">
          <button ref={sidebarTriggerRef} type="button" className="mobile-nav-button icon-button" onClick={onOpenSidebar} aria-label="Open navigation">
            <Icon name="menu" />
          </button>
          <BrandMark size={30} />
          <span>Neil Agent</span>
        </div>

        <button type="button" className="workspace-selector" disabled aria-label="Workspace selector is fixed for this local service">
          <span>workspace</span><b>/</b><strong>{liveSnapshot?.workspace.name ?? 'Neil-Agent'}</strong><span className="selector-chevron"><Icon name="chevron" size={15} /></span>
        </button>
      </div>

      <div
        className="mode-switcher"
        role="radiogroup"
        aria-label="Preview work mode"
        aria-describedby={interactionLocked ? 'mode-lock-reason' : 'mode-preview-reason'}
        onKeyDown={setModeFromKey}
      >
        <button ref={focusModeRef} type="button" role="radio" className={mode === 'focus' ? 'is-active' : ''} onClick={() => selectMode('focus')} aria-checked={mode === 'focus'} aria-disabled={interactionLocked} tabIndex={mode === 'focus' ? 0 : -1}>Focus</button>
        <button ref={buildModeRef} type="button" role="radio" className={mode === 'build' ? 'is-active' : ''} onClick={() => selectMode('build')} aria-checked={mode === 'build'} aria-disabled={interactionLocked} tabIndex={mode === 'build' ? 0 : -1}>Build</button>
      </div>
      <span className="sr-only" id="mode-lock-reason">Mode switching is unavailable while a run or approval is active.</span>
      <span className="sr-only" id="mode-preview-reason">Mode is a local interface preview and does not change tool permissions.</span>

      <div className="header-right">
        <button type="button" className="model-selector" disabled aria-describedby="model-lock-reason">
          <span>{liveSnapshot?.provider.display_name ?? 'OpenAI'}</span><strong>{liveSnapshot?.provider.model ?? 'gpt-5'}</strong><Icon name="chevron" size={14} />
        </button>
        <span className="sr-only" id="model-lock-reason">Model selection is fixed when the local Web Workbench starts.</span>

        <div className={`run-status tone-${connectionState === 'live' ? 'success' : connectionState === 'offline' ? 'danger' : fixtureTone}`} role="status" aria-live="polite">
          <span className="status-dot" />
          <span>{connectionState === 'live' ? `Live · ${liveSnapshot?.run.status ?? 'idle'}` : connectionState === 'offline' ? 'Offline · last known' : connectionState === 'connecting' ? 'Reconnecting locally' : `Fixture · ${fixtureStatusLabel}`}</span>
        </div>

        <button ref={reviewTriggerRef} type="button" className="review-mobile-button icon-button" onClick={onOpenReview} aria-label="Open review">
          <Icon name="check" />
        </button>

        <div className="avatar-button" role="img" aria-label="Local Neil Agent profile">NA</div>
      </div>
    </header>
  )
}

function PreviewBanner({
  scenario,
  onScenarioChange,
  connectionState,
}: {
  scenario: Scenario
  onScenarioChange: (scenario: Scenario) => void
  connectionState: LiveConnectionState
}) {
  const scenarioId = useId()
  const label = connectionState === 'live'
    ? 'P7 live Agent'
    : connectionState === 'connecting'
      ? 'P7 reconnecting'
      : connectionState === 'offline'
        ? 'P7 offline · last known'
        : 'P0 fixture preview'
  const detail = connectionState === 'live'
    ? 'Local realtime execution · bounded Git review · preview-gated tools · no aggregate approval or PTY'
    : connectionState === 'fixture'
      ? 'Synthetic data only · no Agent, model, file, Git, or approval action is connected'
      : 'Last-known state is preserved while the local event stream reconnects'

  return (
    <div className="preview-banner">
      <div className="preview-message" role="status">
        <strong>{label}</strong>
        <span>{detail}</span>
      </div>
      {connectionState === 'fixture' ? (
        <label className="scenario-select" htmlFor={scenarioId}>
          <span>Preview state</span>
          <select
            id={scenarioId}
            aria-label="Preview state"
            value={scenario.id}
            onChange={(event) => onScenarioChange(scenarios.find((item) => item.id === event.target.value) ?? scenarios[0])}
          >
            {scenarios.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
      ) : null}
    </div>
  )
}

function WorkspacePanel({
  scenario,
  liveSnapshot,
  hasControl,
  commandError,
  onStartTurn,
  onCancelTurn,
}: {
  scenario: Scenario
  liveSnapshot: WorkbenchSnapshotV1 | null
  hasControl: boolean
  commandError: string | null
  onStartTurn: (prompt: string) => void
  onCancelTurn: () => void
}) {
  const [prompt, setPrompt] = useState('')
  const active = liveSnapshot?.run.status === 'running' || liveSnapshot?.run.status === 'cancelling'
  return (
    <main className="workspace-panel panel">
      <div className="panel-title workspace-title">
        <BrandMark size={23} />
        <div><h1>Workspace</h1><span className="fixture-tag">{liveSnapshot ? 'live snapshot' : 'fixture preview'}</span></div>
      </div>
      {liveSnapshot ? (
        <form
          className="live-task-form"
          onSubmit={(event) => {
            event.preventDefault()
            const value = prompt.trim()
            if (!value) return
            onStartTurn(value)
            setPrompt('')
          }}
        >
          <label htmlFor="live-task-prompt">Agent task</label>
          <div className="live-task-row">
            <textarea
              id="live-task-prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="Ask Neil Agent to inspect or explain this workspace…"
              maxLength={8000}
              rows={2}
              disabled={active}
            />
            {active ? (
              <button type="button" className="cancel-run-button" onClick={onCancelTurn} disabled={!hasControl || liveSnapshot.run.status === 'cancelling'}>
                {liveSnapshot.run.status === 'cancelling' ? 'Cancelling…' : 'Cancel'}
              </button>
            ) : (
              <button type="submit" className="start-run-button" disabled={!hasControl || !prompt.trim()}>Run Agent</button>
            )}
          </div>
          <div className="live-task-meta">
            <span>{hasControl ? 'This tab has control' : 'Observing · another tab may have control'}</span>
            <span>Mutations require one-tool preview approval</span>
          </div>
          {commandError ? <p className="command-error" role="alert">{commandError}</p> : null}
        </form>
      ) : (
        <div className="objective-bar">
          <span className="objective-check"><Icon name="check" size={14} /></span>
          <span>{scenario.objective}</span>
        </div>
      )}
      {liveSnapshot?.run.objective ? <div className="objective-bar"><span className="objective-check"><Icon name="check" size={14} /></span><span>{liveSnapshot.run.objective}</span></div> : null}
      <div className="timeline-scroll" tabIndex={0} aria-label={liveSnapshot ? 'Scrollable live timeline' : 'Scrollable fixture timeline'}>
        {liveSnapshot ? <LiveTimeline steps={liveSnapshot.timeline} /> : <Timeline scenario={scenario} />}
      </div>
    </main>
  )
}

function App() {
  const scenarioFromLocation = new URLSearchParams(window.location.search).get('scene')
  const [scenario, setScenario] = useState(
    () => scenarios.find((item) => item.id === scenarioFromLocation) ?? scenarios[2],
  )
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [reviewOpen, setReviewOpen] = useState(false)
  const [outputCollapsed, setOutputCollapsed] = useState(false)
  const [outputHeight, setOutputHeight] = useState(176)
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth)
  const {
    liveSnapshot,
    connectionState,
    hasControl,
    commandError,
    startTurn,
    cancelTurn,
    approveTool,
    rejectTool,
    selectSession,
    newSession,
    refreshFiles,
    refreshReview,
  } = useWorkbenchConnection(Boolean(scenarioFromLocation))
  const sidebarTriggerRef = useRef<HTMLButtonElement | null>(null)
  const reviewTriggerRef = useRef<HTMLButtonElement | null>(null)
  const overlayOpen = sidebarOpen || reviewOpen
  const sidebarDrawerMode = viewportWidth <= 1380
  const reviewDrawerMode = viewportWidth < 1024
  const interactionLocked = liveSnapshot
    ? liveSnapshot.run.status === 'running'
      || liveSnapshot.run.status === 'cancelling'
      || liveSnapshot.approval?.state === 'pending'
    : scenario.id === 'running' || scenario.id === 'approval'

  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const closeSidebar = () => {
    setSidebarOpen(false)
    window.setTimeout(() => sidebarTriggerRef.current?.focus(), 0)
  }

  const closeReview = () => {
    setReviewOpen(false)
    window.setTimeout(() => reviewTriggerRef.current?.focus(), 0)
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Tab' && overlayOpen) {
        const drawer = document.querySelector<HTMLElement>(sidebarOpen ? '.sidebar.is-open' : '.review-panel.is-open')
        const focusable = drawer
          ? Array.from(drawer.querySelectorAll<HTMLElement>('button:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'))
            .filter((element) => !element.hasAttribute('aria-hidden'))
          : []
        const first = focusable[0]
        const last = focusable.at(-1)
        if (first && last) {
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault()
            last.focus()
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault()
            first.focus()
          }
        }
      }
      if (event.key === 'Escape') {
        if (sidebarOpen) closeSidebar()
        if (reviewOpen) closeReview()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [overlayOpen, reviewOpen, sidebarOpen])

  const changeScenario = (nextScenario: Scenario) => {
    setScenario(nextScenario)
    const url = new URL(window.location.href)
    url.searchParams.set('scene', nextScenario.id)
    window.history.replaceState({}, '', url)
  }

  return (
    <div
      className="app-shell"
      data-output-height={outputCollapsed ? 52 : outputHeight}
    >
      <a className="skip-link" href="#workspace-main">Skip to workspace</a>
      <PreviewBanner scenario={scenario} onScenarioChange={changeScenario} connectionState={connectionState} />
      <Header
        onOpenSidebar={() => setSidebarOpen(true)}
        onOpenReview={() => setReviewOpen(true)}
        sidebarTriggerRef={sidebarTriggerRef}
        reviewTriggerRef={reviewTriggerRef}
        liveSnapshot={liveSnapshot}
        connectionState={connectionState}
        interactionLocked={interactionLocked}
        fixtureStatusLabel={scenario.label}
        fixtureTone={scenario.runTone}
      />
      <Sidebar
        open={sidebarOpen}
        onClose={closeSidebar}
        interactionLocked={interactionLocked}
        drawerMode={sidebarDrawerMode}
        liveSnapshot={liveSnapshot}
        hasControl={hasControl}
        onRefreshFiles={refreshFiles}
        onSelectSession={selectSession}
        onNewSession={newSession}
      />
      <div id="workspace-main" className="workspace-cell" tabIndex={-1}>
        <WorkspacePanel
          scenario={scenario}
          liveSnapshot={liveSnapshot}
          hasControl={hasControl}
          commandError={commandError}
          onStartTurn={startTurn}
          onCancelTurn={cancelTurn}
        />
      </div>
      <ReviewPanel
        open={reviewOpen}
        onClose={closeReview}
        scenario={scenario}
        drawerMode={reviewDrawerMode}
        liveSnapshot={liveSnapshot}
        hasControl={hasControl}
        onApproveTool={approveTool}
        onRejectTool={rejectTool}
        onRefreshReview={refreshReview}
      />
      <OutputPanel
        scenario={scenario}
        liveSnapshot={liveSnapshot}
        collapsed={outputCollapsed}
        height={outputHeight}
        onCollapsedChange={setOutputCollapsed}
        onHeightChange={setOutputHeight}
      />
      {overlayOpen ? <button type="button" className="drawer-backdrop" onClick={() => { if (sidebarOpen) closeSidebar(); if (reviewOpen) closeReview() }} aria-label="Close open panel" /> : null}
    </div>
  )
}

export default App
