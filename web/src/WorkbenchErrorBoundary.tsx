import { Component, type ReactNode } from 'react'
import './WorkbenchErrorBoundary.css'

interface WorkbenchErrorBoundaryProps {
  children: ReactNode
}

interface WorkbenchErrorBoundaryState {
  failed: boolean
}

export class WorkbenchErrorBoundary extends Component<
  WorkbenchErrorBoundaryProps,
  WorkbenchErrorBoundaryState
> {
  state: WorkbenchErrorBoundaryState = { failed: false }

  static getDerivedStateFromError(): WorkbenchErrorBoundaryState {
    return { failed: true }
  }

  componentDidCatch(): void {
    console.error('Neil Agent Web Workbench stopped rendering safely.')
  }

  render() {
    if (!this.state.failed) return this.props.children

    return (
      <main className="workbench-fatal" role="alert" aria-labelledby="workbench-fatal-heading">
        <section className="workbench-fatal-card">
          <span className="workbench-fatal-mark" aria-hidden="true">✦</span>
          <p className="workbench-fatal-eyebrow">Safe recovery</p>
          <h1 id="workbench-fatal-heading">The workbench could not render</h1>
          <p>
            The interface stopped before continuing. Agent state, files, Git, and pending tool decisions
            were not changed by this recovery screen.
          </p>
          <button type="button" autoFocus onClick={() => window.location.reload()}>
            Reload workbench
          </button>
          <small>Diagnostic details are intentionally not shown in the interface.</small>
        </section>
      </main>
    )
  }
}
