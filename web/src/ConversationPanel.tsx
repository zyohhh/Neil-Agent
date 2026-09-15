import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchLiveConversation, type ConversationPart } from './protocol'

export function ConversationPanel({ sessionId, revision }: { sessionId: string; revision: number }) {
  const [parts, setParts] = useState<ConversationPart[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [compacted, setCompacted] = useState(false)
  const request = useRef<AbortController | null>(null)
  const loadPage = useCallback(async (cursor: string | null) => {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    setError(false)
    try {
      const page = await fetchLiveConversation(sessionId, revision, cursor, controller.signal)
      if (controller.signal.aborted) return
      setParts(previous => cursor === null ? page.items : [...previous, ...page.items])
      setNextCursor(page.next_cursor)
      setCompacted(page.compacted)
    } catch {
      if (!controller.signal.aborted) setError(true)
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }, [sessionId, revision])
  useEffect(() => {
    void loadPage(null)
    return () => request.current?.abort()
  }, [loadPage])

  const messages: Array<{ index: number; role: 'user' | 'assistant'; text: string }> = []
  for (const part of parts) {
    const previous = messages.at(-1)
    if (previous?.index === part.message_index) previous.text += part.text
    else messages.push({ index: part.message_index, role: part.role, text: part.text })
  }
  return (
    <section className="conversation-panel" aria-label="会话历史">
      <h2>会话历史</h2>
      {compacted ? <p className="conversation-note">较早内容已压缩，以下是当前保存的问答。</p> : null}
      {messages.map(message => (
        <article className={`conversation-message conversation-${message.role}`} key={message.index}>
          <h3>{message.role === 'user' ? '你' : 'Neil Agent'}</h3>
          <div className="conversation-text">{message.text}</div>
        </article>
      ))}
      {loading ? <p className="conversation-note" role="status">正在加载历史…</p> : null}
      {error ? <p className="command-error" role="alert">会话历史暂时无法加载。</p> : null}
      {!loading && (error || nextCursor !== null) ? (
        <button type="button" className="text-button" onClick={() => void loadPage(nextCursor)}>{error ? '重试' : '继续加载历史'}</button>
      ) : null}
    </section>
  )
}
