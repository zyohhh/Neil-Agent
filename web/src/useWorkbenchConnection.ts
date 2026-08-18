import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchLiveFileTree,
  fetchLiveSnapshot,
  refreshLiveSnapshot,
  reduceWorkbenchEvent,
  WorkbenchRealtimeClient,
  type LiveConnectionState,
  type WorkbenchSnapshotV1,
} from './protocol'

export interface WorkbenchConnection {
  liveSnapshot: WorkbenchSnapshotV1 | null
  connectionState: LiveConnectionState
  hasControl: boolean
  commandError: string | null
  startTurn: (prompt: string) => void
  cancelTurn: () => void
  approveTool: (requestId: string) => void
  rejectTool: (requestId: string) => void
  refreshFiles: () => void
  refreshReview: () => void
}

export function useWorkbenchConnection(fixtureMode: boolean): WorkbenchConnection {
  const [liveSnapshot, setLiveSnapshot] = useState<WorkbenchSnapshotV1 | null>(null)
  const [connectionState, setConnectionState] = useState<LiveConnectionState>(fixtureMode ? 'fixture' : 'connecting')
  const [hasControl, setHasControl] = useState(false)
  const [commandError, setCommandError] = useState<string | null>(null)
  const realtimeClientRef = useRef<WorkbenchRealtimeClient | null>(null)

  useEffect(() => {
    let active = true
    fetchLiveSnapshot()
      .then((snapshot) => {
        if (!active) return
        if (!snapshot) {
          setConnectionState('fixture')
          return
        }

        setLiveSnapshot(snapshot)
        const client = new WorkbenchRealtimeClient({
          onConnection: setConnectionState,
          onSnapshot: setLiveSnapshot,
          onEvent: (event) => setLiveSnapshot((current) => (
            current ? reduceWorkbenchEvent(current, event) : current
          )),
          onControl: setHasControl,
          onCommandError: setCommandError,
        })
        realtimeClientRef.current = client
        client.start(snapshot)
      })
      .catch(() => {
        if (active) setConnectionState('offline')
      })

    return () => {
      active = false
      realtimeClientRef.current?.stop()
      realtimeClientRef.current = null
    }
  }, [])

  const startTurn = useCallback((prompt: string) => {
    realtimeClientRef.current?.startTurn(prompt)
  }, [])

  const cancelTurn = useCallback(() => {
    realtimeClientRef.current?.cancelTurn()
  }, [])

  const approveTool = useCallback((requestId: string) => {
    realtimeClientRef.current?.approveTool(requestId)
  }, [])

  const rejectTool = useCallback((requestId: string) => {
    realtimeClientRef.current?.rejectTool(requestId)
  }, [])

  const refreshFiles = useCallback(() => {
    if (!liveSnapshot) return
    void fetchLiveFileTree(liveSnapshot.files.revision)
      .then((tree) => {
        if (tree.unchanged) return
        setLiveSnapshot((current) => (current ? { ...current, files: tree } : current))
      })
      .catch(() => setCommandError('File tree refresh unavailable'))
  }, [liveSnapshot])

  const refreshReview = useCallback(() => {
    void refreshLiveSnapshot()
      .then((snapshot) => {
        if (snapshot) setLiveSnapshot(snapshot)
      })
      .catch(() => setCommandError('Review refresh unavailable'))
  }, [])

  return {
    liveSnapshot,
    connectionState,
    hasControl,
    commandError,
    startTurn,
    cancelTurn,
    approveTool,
    rejectTool,
    refreshFiles,
    refreshReview,
  }
}
