import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { ConversationPanel } from './ConversationPanel'
import type { ConversationPage } from './protocol'

;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const page = (sessionId: string, revision: number): ConversationPage => ({
  session_id: sessionId, revision, items: [
    { message_index: 0, role: 'user', text: '以前的问题', offset: 0, is_last_part: true },
    { message_index: 1, role: 'assistant', text: '回答开头。\n\n', offset: 0, is_last_part: false },
  ], next_cursor: '1:7', total_messages: 2, compacted: false,
})
const response = (payload: ConversationPage) => new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } })

it('loads saved questions and answers and appends the rest of a long answer across pages', async () => {
  const first = page('selected', 1)
  const second: ConversationPage = { ...first, items: [
    { message_index: 1, role: 'assistant', text: '回答结尾。', offset: 7, is_last_part: true },
  ], next_cursor: null }
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response(first)).mockResolvedValueOnce(response(second))
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  try {
    await act(async () => root.render(<ConversationPanel sessionId="selected" revision={1} />))
    expect(Array.from(container.querySelectorAll('.conversation-text'), element => element.textContent)).toEqual(['以前的问题', '回答开头。\n\n'])
    await act(async () => container.querySelector<HTMLButtonElement>('button')!.click())
    expect(Array.from(container.querySelectorAll('.conversation-text'), element => element.textContent)).toEqual(['以前的问题', '回答开头。\n\n回答结尾。'])
    expect(fetchMock.mock.calls[1][0]).toContain('cursor=1%3A7')
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: 'include', cache: 'no-store' })
    expect(container.querySelector('button')).toBeNull()
  } finally {
    await act(async () => root.unmount())
    container.remove()
    fetchMock.mockRestore()
  }
})

it('aborts an old session request and never shows its body after switching sessions', async () => {
  let resolveOld!: (value: Response) => void
  const fetchMock = vi.spyOn(globalThis, 'fetch')
    .mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    .mockResolvedValueOnce(response({ ...page('new-session', 2), items: [{ message_index: 0, role: 'user', text: '新会话的问题', offset: 0, is_last_part: true }], next_cursor: null }))
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  try {
    await act(async () => root.render(<ConversationPanel key="old" sessionId="old-session" revision={1} />))
    const oldSignal = fetchMock.mock.calls[0][1]?.signal as AbortSignal
    await act(async () => root.render(<ConversationPanel key="new" sessionId="new-session" revision={2} />))
    expect(oldSignal.aborted).toBe(true)
    await act(async () => resolveOld(response(page('old-session', 1))))
    expect(container.textContent).toContain('新会话的问题')
    expect(container.textContent).not.toContain('以前的问题')
    expect(container.textContent).not.toContain('回答开头')
  } finally {
    await act(async () => root.unmount())
    container.remove()
    fetchMock.mockRestore()
  }
})

it('keeps already loaded history when the next page fails and retries that page', async () => {
  const first = page('selected', 1)
  const fetchMock = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(response(first))
    .mockResolvedValueOnce(new Response(null, { status: 409 }))
    .mockResolvedValueOnce(response({ ...first, items: [{ message_index: 1, role: 'assistant', text: '结尾', offset: 7, is_last_part: true }], next_cursor: null }))
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  try {
    await act(async () => root.render(<ConversationPanel sessionId="selected" revision={1} />))
    await act(async () => container.querySelector<HTMLButtonElement>('button')!.click())
    expect(container.textContent).toContain('回答开头。')
    expect(container.textContent).toContain('会话历史暂时无法加载。')
    await act(async () => container.querySelector<HTMLButtonElement>('button')!.click())
    expect(container.querySelectorAll('.conversation-text')[1].textContent).toBe('回答开头。\n\n结尾')
    expect(fetchMock.mock.calls[2][0]).toBe(fetchMock.mock.calls[1][0])
  } finally {
    await act(async () => root.unmount())
    container.remove()
    fetchMock.mockRestore()
  }
})
