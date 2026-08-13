import { useEffect, useId, useRef, useState } from 'react'
import './App.css'

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

type RunState = 'loading' | 'idle' | 'running' | 'approval' | 'completed' | 'failed' | 'cancelled' | 'stale' | 'applied' | 'offline' | 'partial-error' | 'stress'
type ReviewState = 'empty' | 'checking' | 'approval' | 'passed' | 'failed' | 'stale' | 'applied'
type StepState = 'pending' | 'running' | 'waiting' | 'succeeded' | 'failed' | 'skipped' | 'cancelled'

interface FileNode {
  id: string
  name: string
  kind: 'folder' | 'file'
  language?: 'ts' | 'tsx' | 'md'
  children?: FileNode[]
}

interface TimelineStep {
  id: string
  kind: 'search' | 'read' | 'plan' | 'edit' | 'test' | 'summary'
  title: string
  subtitle?: string
  time: string
  datetime: string
  status: StepState
  body?: 'code' | 'plan' | 'test' | 'summary'
}

interface Scenario {
  id: RunState
  label: string
  runLabel: string
  runTone: 'live' | 'warning' | 'success' | 'danger' | 'muted'
  reviewState: ReviewState
  reviewLabel: string
  reviewDetail: string
  objective: string
  outputLines: string[]
  source: 'fixture'
}

const files: FileNode[] = [
  {
    id: 'src',
    name: 'src/',
    kind: 'folder',
    children: [
      { id: 'agent', name: 'agent.py', kind: 'file' },
      {
        id: 'tools',
        name: 'tools/',
        kind: 'folder',
        children: [
          { id: 'registry', name: 'registry.py', kind: 'file' },
          { id: 'filesystem', name: 'filesystem.py', kind: 'file' },
        ],
      },
      { id: 'events', name: 'events.py', kind: 'file' },
      { id: 'projections', name: 'projections.py', kind: 'file' },
    ],
  },
  {
    id: 'web',
    name: 'web/',
    kind: 'folder',
    children: [
      { id: 'app', name: 'App.tsx', kind: 'file', language: 'tsx' },
      { id: 'styles', name: 'App.css', kind: 'file' },
    ],
  },
  {
    id: 'docs',
    name: 'docs/',
    kind: 'folder',
    children: [
      {
        id: 'web-doc',
        name: 'web-workbench-development.md',
        kind: 'file',
        language: 'md',
      },
    ],
  },
]

const sessions = [
  { id: 'workbench', title: 'Build web workbench', time: '10:24 AM', active: true },
  { id: 'provider', title: 'Complete provider runtime', time: 'Yesterday' },
  { id: 'security', title: 'Review sandbox boundary', time: 'Aug 9' },
  { id: 'context', title: 'Visualize context budget', time: 'Aug 7' },
]

const baseSteps: TimelineStep[] = [
  {
    id: 'search',
    kind: 'search',
    title: 'Search',
    subtitle: 'Inspecting the current UI and runtime projection boundaries',
    time: '10:24 AM',
    datetime: '2026-08-13T10:24:00+08:00',
    status: 'succeeded',
  },
  {
    id: 'read',
    kind: 'read',
    title: 'Read file',
    subtitle: 'docs/web-workbench-development.md  •  853 lines',
    time: '10:24 AM',
    datetime: '2026-08-13T10:24:30+08:00',
    status: 'succeeded',
    body: 'code',
  },
  {
    id: 'plan',
    kind: 'plan',
    title: 'Plan',
    time: '10:25 AM',
    datetime: '2026-08-13T10:25:00+08:00',
    status: 'succeeded',
    body: 'plan',
  },
  {
    id: 'edit',
    kind: 'edit',
    title: 'Edit file',
    subtitle: 'web/src/App.tsx   +428   −0',
    time: '10:26 AM',
    datetime: '2026-08-13T10:26:00+08:00',
    status: 'succeeded',
  },
  {
    id: 'test',
    kind: 'test',
    title: 'Test',
    subtitle: 'npm run test',
    time: '10:28 AM',
    datetime: '2026-08-13T10:28:00+08:00',
    status: 'succeeded',
    body: 'test',
  },
  {
    id: 'summary',
    kind: 'summary',
    title: 'Summary',
    time: '10:28 AM',
    datetime: '2026-08-13T10:28:30+08:00',
    status: 'running',
    body: 'summary',
  },
]

const scenarios: Scenario[] = [
  {
    id: 'loading', label: 'Loading', runLabel: 'Preview connecting', runTone: 'muted', reviewState: 'empty', reviewLabel: 'Loading fixture', reviewDetail: 'Synthetic snapshot is preparing', objective: 'Load the deterministic workbench fixture.', outputLines: ['… loading fixture snapshot'], source: 'fixture',
  },
  {
    id: 'idle', label: 'Idle / empty', runLabel: 'Preview idle', runTone: 'muted', reviewState: 'empty', reviewLabel: 'No changes', reviewDetail: 'Fixture is ready for a new preview', objective: 'Explore the empty, idle workbench state.', outputLines: ['› fixture ready', 'No synthetic run is active.'], source: 'fixture',
  },
  {
    id: 'running',
    label: 'Running',
    runLabel: 'Preview running',
    runTone: 'live',
    reviewState: 'checking',
    reviewLabel: 'Checks in progress',
    reviewDetail: '1 of 2 checks complete',
    objective: 'Build a fixture-driven Web Workbench shell for Neil Agent.',
    outputLines: [
      '› npm run test',
      '✓ component state fixtures passed',
      '✓ keyboard navigation suite passed',
      '… visual baseline is rendering',
    ],
    source: 'fixture',
  },
  {
    id: 'approval',
    label: 'Approval',
    runLabel: 'Preview awaiting approval',
    runTone: 'warning',
    reviewState: 'approval',
    reviewLabel: 'Approval required',
    reviewDetail: 'One fixture tool is waiting',
    objective: 'Review the fixture file change before allowing one tool action.',
    outputLines: [
      '› fixture: write_file web/src/App.tsx',
      '✓ preview binding generated',
      '! waiting for a single-tool decision',
    ],
    source: 'fixture',
  },
  {
    id: 'completed',
    label: 'Completed',
    runLabel: 'Preview complete',
    runTone: 'success',
    reviewState: 'passed',
    reviewLabel: 'All checks passed',
    reviewDetail: 'Fixture run completed safely',
    objective: 'Review the completed P0 fixture and responsive workbench layout.',
    outputLines: [
      '› npm run build',
      '✓ 12 fixture tests passed',
      '✓ production bundle generated',
      '› ready for visual review',
    ],
    source: 'fixture',
  },
  {
    id: 'failed',
    label: 'Failed',
    runLabel: 'Preview failed',
    runTone: 'danger',
    reviewState: 'failed',
    reviewLabel: 'One check failed',
    reviewDetail: 'Responsive layout fixture needs attention',
    objective: 'Inspect the failed fixture without applying any real changes.',
    outputLines: [
      '› npm run test',
      '✓ component state fixtures passed',
      '× mobile drawer focus return failed',
      '› fixture process exited with code 1',
    ],
    source: 'fixture',
  },
  {
    id: 'cancelled', label: 'Cancelled', runLabel: 'Preview cancelled', runTone: 'warning', reviewState: 'stale', reviewLabel: 'Preview cancelled', reviewDetail: 'Synthetic remaining steps were skipped', objective: 'Inspect a cancelled fixture run.', outputLines: ['! fixture cancellation requested', '✓ no real operation was interrupted'], source: 'fixture',
  },
  {
    id: 'stale', label: 'Review stale', runLabel: 'Preview idle', runTone: 'warning', reviewState: 'stale', reviewLabel: 'Review is stale', reviewDetail: 'Fixture changed after its synthetic check', objective: 'Review stale fixture information without approval.', outputLines: ['! fixture revision changed', '› re-check required before a synthetic decision'], source: 'fixture',
  },
  {
    id: 'applied', label: 'Applied', runLabel: 'Preview complete', runTone: 'success', reviewState: 'applied', reviewLabel: 'One fixture tool applied', reviewDetail: 'Not staged or committed', objective: 'Inspect the local applied-state fixture.', outputLines: ['✓ one synthetic tool marked applied', '! no file, Git, or Agent side effect occurred'], source: 'fixture',
  },
  {
    id: 'offline', label: 'Offline / last known', runLabel: 'Preview offline · last known running', runTone: 'danger', reviewState: 'stale', reviewLabel: 'Last-known snapshot', reviewDetail: 'Connection fixture is offline', objective: 'Inspect stale last-known state while the fixture connection is offline.', outputLines: ['× fixture connection unavailable', '! displayed run state is last-known only'], source: 'fixture',
  },
  {
    id: 'partial-error', label: 'Partial error', runLabel: 'Preview degraded', runTone: 'danger', reviewState: 'failed', reviewLabel: 'Review fixture unavailable', reviewDetail: 'Other preview panels remain available', objective: 'Verify the workbench shell survives a partial fixture error.', outputLines: ['× synthetic review projection failed', '✓ workspace fixture remains readable'], source: 'fixture',
  },
  {
    id: 'stress', label: 'Stress / i18n', runLabel: '预览长文本压力场景', runTone: 'live', reviewState: 'checking', reviewLabel: '长文本检查中', reviewDetail: '验证 200% 缩放和超长内容', objective: '验证超长中文目标、超长模型名称、深层路径和代码行不会撑破工作台布局，并保持所有关键状态可读。', outputLines: ['› 验证超长路径 docs/研究项目/上下文断层图/非常长的工作台开发说明.md', '✓ 页面级水平溢出为 0', '… 100+ 条事件由有界 fixture 表示'], source: 'fixture',
  },
]

const changedFiles = [
  { id: 'app', name: 'App.tsx', added: 428, deleted: 0 },
  { id: 'css', name: 'App.css', added: 612, deleted: 0 },
  { id: 'fixture', name: 'workbench.ts', added: 184, deleted: 0 },
]

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
    <span className="brand-mark" style={{ width: size, height: size }} aria-hidden="true">
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
          style={{ '--tree-level': level } as React.CSSProperties}
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
        style={{ '--tree-level': level } as React.CSSProperties}
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
}: {
  open: boolean
  onClose: () => void
  interactionLocked: boolean
  drawerMode: boolean
}) {
  const [selectedSession, setSelectedSession] = useState('workbench')
  const [activeTreeItem, setActiveTreeItem] = useState('src')
  const closeButtonRef = useRef<HTMLButtonElement>(null)

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
        <p className="eyebrow" id="project-heading">Project</p>
        <ul className="file-tree" role="tree" aria-label="Fixture project files" onKeyDown={handleTreeKeyDown}>
          {files.map((file) => (
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
        <p className="eyebrow" id="sessions-heading">Sessions</p>
        <div className="session-list">
          {sessions.map((session) => (
            <button
              type="button"
              key={session.id}
              className={`session-item ${selectedSession === session.id ? 'is-active' : ''}`}
              onClick={() => {
                if (!interactionLocked) setSelectedSession(session.id)
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
        <span className="sr-only" id="session-lock-reason">Session switching is disabled while this fixture run is active.</span>
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
  const steps = baseSteps.map((step) => {
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

function ContextGauge() {
  return (
    <div className="context-gauge" role="progressbar" aria-label="Fixture context usage" aria-valuemin={0} aria-valuemax={200} aria-valuenow={142}>
      <svg viewBox="0 0 160 94" aria-hidden="true">
        <path className="gauge-track" d="M18 80a62 62 0 0 1 124 0" pathLength="100" />
        <path className="gauge-value" d="M18 80a62 62 0 0 1 124 0" pathLength="100" />
      </svg>
      <span><strong>142K</strong><small>/ 200K fixture</small></span>
    </div>
  )
}

function ReviewPanel({
  open,
  onClose,
  scenario,
  drawerMode,
}: {
  open: boolean
  onClose: () => void
  scenario: Scenario
  drawerMode: boolean
}) {
  const [decision, setDecision] = useState<'none' | 'approved' | 'rejected'>('none')
  const approvalAvailable = scenario.reviewState === 'approval' && decision === 'none'
  const closeButtonRef = useRef<HTMLButtonElement>(null)

  useEffect(() => setDecision('none'), [scenario.id])
  useEffect(() => {
    if (open && drawerMode) closeButtonRef.current?.focus()
  }, [drawerMode, open])

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

      <section className="review-section">
        <p className="eyebrow">Current step</p>
        <div className={`review-status status-${scenario.reviewState}`}>
          <span className="status-dot" />
          <span><strong>{scenario.reviewLabel}</strong><small>{scenario.reviewDetail}</small></span>
        </div>
      </section>

      <section className="review-section">
        <p className="eyebrow">Changed files <span className="fixture-tag">fixture</span></p>
        <div className="changed-files">
          {changedFiles.map((file) => (
            <div
              key={file.id}
              className="changed-file"
              aria-label={`${file.name}, added ${file.added} lines, deleted ${file.deleted} lines`}
            >
              <span className="file-bullet" />
              <Icon name="file" size={17} />
              <span className="changed-name">{file.name}</span>
              <span className="diff-added" aria-label={`added ${file.added} lines`}>+{file.added}</span>
              <span className="diff-deleted" aria-label={`deleted ${file.deleted} lines`}>−{file.deleted}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="review-section metrics-section">
        <div>
          <p className="eyebrow">Context</p>
          <ContextGauge />
        </div>
        <div className="cost-block">
          <p className="eyebrow">Cost</p>
          <strong>Unavailable</strong>
          <small>No rate table</small>
        </div>
      </section>

      <section className="approval-section">
        <p className="eyebrow">Single-tool approval</p>
        <div className={`approval-card ${approvalAvailable ? 'is-ready' : ''}`}>
          <span>
            <strong>{decision === 'approved' ? 'Fixture approved' : decision === 'rejected' ? 'Fixture rejected' : approvalAvailable ? 'Write App.tsx' : 'No pending tool'}</strong>
            <small>{approvalAvailable ? 'One synthetic action' : 'No real side effect'}</small>
          </span>
          <span className="shield-badge"><Icon name={decision === 'rejected' ? 'x' : 'check'} size={17} /></span>
        </div>
        <button
          type="button"
          className="approve-button"
          disabled={!approvalAvailable}
          onClick={() => setDecision('approved')}
        >
          Approve fixture
        </button>
        <button
          type="button"
          className="reject-button"
          disabled={!approvalAvailable}
          onClick={() => setDecision('rejected')}
        >
          Reject fixture
        </button>
      </section>
    </aside>
  )
}

function OutputPanel({
  scenario,
  collapsed,
  height,
  onCollapsedChange,
  onHeightChange,
}: {
  scenario: Scenario
  collapsed: boolean
  height: number
  onCollapsedChange: (collapsed: boolean) => void
  onHeightChange: (height: number) => void
}) {
  const [cleared, setCleared] = useState(false)
  const panelRef = useRef<HTMLElement>(null)

  useEffect(() => setCleared(false), [scenario.id])

  const updateHeight = (nextHeight: number) => onHeightChange(Math.max(128, Math.min(window.innerHeight * 0.45, nextHeight)))

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
      ref={panelRef}
    >
      {!collapsed ? (
        <div
          className="output-resizer"
          role="separator"
          aria-label="Resize fixture output"
          aria-orientation="horizontal"
          aria-valuemin={128}
          aria-valuemax={Math.round(window.innerHeight * 0.45)}
          aria-valuenow={Math.round(height)}
          tabIndex={0}
          onPointerDown={startResize}
          onKeyDown={(event) => {
            if (event.key === 'ArrowUp') updateHeight(height + 16)
            if (event.key === 'ArrowDown') updateHeight(height - 16)
            if (event.key === 'Home') updateHeight(128)
            if (event.key === 'End') updateHeight(window.innerHeight * 0.45)
          }}
        />
      ) : null}
      <div className="output-toolbar">
        <button type="button" className="output-title" onClick={() => onCollapsedChange(!collapsed)} aria-expanded={!collapsed}>
          <Icon name="panel" size={19} />
          <strong id="output-heading">Output</strong>
          <span className="fixture-tag">fixture</span>
          <span className={`tree-chevron ${collapsed ? '' : 'is-open'}`}><Icon name="chevron" size={14} /></span>
        </button>
        <div className="output-actions">
          <span>feature/web-workbench</span>
          <span className="status-dot" />
          <button type="button" className="icon-button" aria-label="Clear fixture output" onClick={() => setCleared(true)}><Icon name="x" size={16} /></button>
          <button type="button" className="icon-button" onClick={() => updateHeight(300)} aria-label="Expand output"><Icon name="plus" size={17} /></button>
        </div>
      </div>
      {!collapsed ? (
        <div className="output-stream" role="log" aria-label="Synthetic fixture output">
          {(cleared ? ['Fixture output cleared locally.'] : scenario.outputLines).map((line, index) => (
            <div key={`${scenario.id}-${index}`} className={line.startsWith('×') || line.startsWith('!') ? 'output-warning' : line.startsWith('✓') ? 'output-success' : ''}>{line}</div>
          ))}
          <div className="output-prompt">› <span className="cursor" /></div>
        </div>
      ) : null}
    </section>
  )
}

function Header({
  scenario,
  onScenarioChange,
  onOpenSidebar,
  onOpenReview,
  compact,
  sidebarTriggerRef,
  reviewTriggerRef,
}: {
  scenario: Scenario
  onScenarioChange: (scenario: Scenario) => void
  onOpenSidebar: () => void
  onOpenReview: () => void
  compact: boolean
  sidebarTriggerRef: React.RefObject<HTMLButtonElement | null>
  reviewTriggerRef: React.RefObject<HTMLButtonElement | null>
}) {
  const scenarioId = useId()
  const [mode, setMode] = useState<'focus' | 'build'>('build')
  const interactionLocked = scenario.id === 'running' || scenario.id === 'approval'
  const setModeFromKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (interactionLocked) return
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault()
      setMode((current) => current === 'focus' ? 'build' : 'focus')
    }
  }

  return (
    <header className="global-header">
      <div className="brand-lockup">
        <button ref={sidebarTriggerRef} type="button" className="mobile-nav-button icon-button" onClick={onOpenSidebar} aria-label="Open navigation">
          <Icon name="menu" />
        </button>
        <BrandMark size={30} />
        <span>Neil Agent</span>
      </div>

      <button type="button" className="workspace-selector" disabled aria-label="Fixture workspace selector is not connected in P0">
        <span>workspace</span><b>/</b><strong>Neil-Agent</strong><span className="selector-chevron"><Icon name="chevron" size={15} /></span>
      </button>

      <div className="mode-switcher" role="group" aria-label="Fixture work mode" onKeyDown={setModeFromKey}>
        <button type="button" className={mode === 'focus' ? 'is-active' : ''} onClick={() => setMode('focus')} aria-pressed={mode === 'focus'} disabled={interactionLocked} title={interactionLocked ? 'Mode switching is disabled while this fixture run is active.' : undefined}>Focus</button>
        <button type="button" className={mode === 'build' ? 'is-active' : ''} onClick={() => setMode('build')} aria-pressed={mode === 'build'} disabled={interactionLocked} title={interactionLocked ? 'Mode switching is disabled while this fixture run is active.' : undefined}>Build</button>
      </div>

      <div className="header-spacer" />

      <label className={`scenario-select ${compact ? 'is-compact' : ''}`} htmlFor={scenarioId}>
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

      <button type="button" className="model-selector" disabled={interactionLocked} title={interactionLocked ? 'Model switching is disabled while this fixture run is active.' : 'Fixture model selector'}>
        <span>OpenAI</span><strong>gpt-5</strong><Icon name="chevron" size={14} />
      </button>

      <div className={`run-status tone-${scenario.runTone}`}>
        <span className="status-dot" />
        <span>{scenario.runLabel}</span>
      </div>

      <button ref={reviewTriggerRef} type="button" className="review-mobile-button icon-button" onClick={onOpenReview} aria-label="Open review">
        <Icon name="check" />
      </button>

      <div className="avatar-button" aria-label="Local fixture profile">NA</div>
    </header>
  )
}

function WorkspacePanel({ scenario }: { scenario: Scenario }) {
  return (
    <main className="workspace-panel panel">
      <div className="panel-title workspace-title">
        <BrandMark size={23} />
        <div><h1>Workspace</h1><span className="fixture-tag">fixture preview</span></div>
      </div>
      <div className="objective-bar">
        <span className="objective-check"><Icon name="check" size={14} /></span>
        <span>{scenario.objective}</span>
      </div>
      <div className="timeline-scroll" tabIndex={0} aria-label="Scrollable fixture timeline">
        <Timeline scenario={scenario} />
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
  const [outputHeight, setOutputHeight] = useState(174)
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth)
  const sidebarTriggerRef = useRef<HTMLButtonElement | null>(null)
  const reviewTriggerRef = useRef<HTMLButtonElement | null>(null)
  const overlayOpen = sidebarOpen || reviewOpen
  const sidebarDrawerMode = viewportWidth <= 1380
  const reviewDrawerMode = viewportWidth < 1024
  const compact = reviewDrawerMode
  const interactionLocked = scenario.id === 'running' || scenario.id === 'approval'

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
      style={{
        '--output-height': `${outputCollapsed ? 52 : outputHeight}px`,
        '--header-height': viewportWidth <= 767 ? '56px' : '64px',
        '--banner-height': viewportWidth <= 767 ? '30px' : '28px',
      } as React.CSSProperties}
    >
      <a className="skip-link" href="#workspace-main">Skip to workspace</a>
      <div className="preview-banner" role="status">
        <strong>P0 fixture preview</strong>
        <span>Synthetic data only · no Agent, model, file, Git, or approval action is connected</span>
      </div>
      <Header
        scenario={scenario}
        onScenarioChange={changeScenario}
        onOpenSidebar={() => setSidebarOpen(true)}
        onOpenReview={() => setReviewOpen(true)}
        compact={compact}
        sidebarTriggerRef={sidebarTriggerRef}
        reviewTriggerRef={reviewTriggerRef}
      />
      <Sidebar
        open={sidebarOpen}
        onClose={closeSidebar}
        interactionLocked={interactionLocked}
        drawerMode={sidebarDrawerMode}
      />
      <div id="workspace-main" className="workspace-cell" tabIndex={-1}>
        <WorkspacePanel scenario={scenario} />
      </div>
      <ReviewPanel open={reviewOpen} onClose={closeReview} scenario={scenario} drawerMode={reviewDrawerMode} />
      <OutputPanel
        scenario={scenario}
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
