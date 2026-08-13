import { createRoot } from 'react-dom/client'
import { act } from 'react'
import App from './App'

describe('WebWorkbenchApp', () => {
  beforeEach(() => window.history.replaceState({}, '', '/?scene=running'))

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
})
