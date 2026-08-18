import type { LiveFileNode } from './protocol'

export type RunState =
  | 'loading'
  | 'idle'
  | 'running'
  | 'approval'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'stale'
  | 'applied'
  | 'offline'
  | 'partial-error'
  | 'stress'

export type ReviewState = 'empty' | 'checking' | 'approval' | 'passed' | 'failed' | 'stale' | 'applied'
export type StepState = 'pending' | 'running' | 'waiting' | 'succeeded' | 'failed' | 'skipped' | 'cancelled'

export interface FileNode {
  id: string
  name: string
  kind: 'folder' | 'file'
  language?: 'ts' | 'tsx' | 'md'
  children?: FileNode[]
}

export interface TimelineStep {
  id: string
  kind: 'search' | 'read' | 'plan' | 'edit' | 'test' | 'summary'
  title: string
  subtitle?: string
  time: string
  datetime: string
  status: StepState
  body?: 'code' | 'plan' | 'test' | 'summary'
}

export interface Scenario {
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

export const fixtureFiles: FileNode[] = [
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

export const fixtureSessions = [
  { id: 'workbench', title: 'Build web workbench', time: '10:24 AM', active: true },
  { id: 'provider', title: 'Complete provider runtime', time: 'Yesterday' },
  { id: 'security', title: 'Review sandbox boundary', time: 'Aug 9' },
  { id: 'context', title: 'Visualize context budget', time: 'Aug 7' },
]

export const toFileNodes = (nodes: LiveFileNode[]): FileNode[] => nodes.map((node) => ({
  id: node.path,
  name: `${node.name}${node.kind === 'directory' ? '/' : ''}`,
  kind: node.kind === 'directory' ? 'folder' : 'file',
  language: node.name.endsWith('.tsx') ? 'tsx' : node.name.endsWith('.md') ? 'md' : undefined,
  children: node.kind === 'directory' ? toFileNodes(node.children) : undefined,
}))

export const fixtureTimeline: TimelineStep[] = [
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

export const scenarios: Scenario[] = [
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

export const fixtureChangedFiles = [
  { id: 'app', name: 'App.tsx', added: 428, deleted: 0 },
  { id: 'css', name: 'App.css', added: 612, deleted: 0 },
  { id: 'fixture', name: 'workbench.ts', added: 184, deleted: 0 },
]
