import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { WorkbenchErrorBoundary } from './WorkbenchErrorBoundary'

;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function ExplodingWorkbench(): never {
  throw new Error('private-render-detail')
}

describe('WorkbenchErrorBoundary', () => {
  it('stops at a safe, focused recovery screen without exposing the exception', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)

    await act(async () => root.render(
      <WorkbenchErrorBoundary>
        <ExplodingWorkbench />
      </WorkbenchErrorBoundary>,
    ))

    expect(document.body.textContent).toContain('The workbench could not render')
    expect(document.body.textContent).toContain('Agent state, files, Git, and pending tool decisions were not changed')
    expect(document.body.textContent).not.toContain('private-render-detail')
    expect(document.activeElement?.textContent).toBe('Reload workbench')

    await act(async () => root.unmount())
    container.remove()
    consoleError.mockRestore()
  })
})
