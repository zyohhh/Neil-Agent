import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { fileURLToPath } from 'node:url'

const host = 'http://127.0.0.1:4174'
const viteCli = fileURLToPath(new URL('../node_modules/vite/bin/vite.js', import.meta.url))
const playwrightCli = fileURLToPath(new URL('../node_modules/@playwright/test/cli.js', import.meta.url))
const forwardedArguments = process.argv.slice(2)
const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))

let portAlreadyInUse = false
try {
  await fetch(host, { signal: AbortSignal.timeout(500) })
  portAlreadyInUse = true
} catch {
  // No process owns the dedicated preview port.
}
if (portAlreadyInUse) throw new Error('Port 4174 is already in use; refusing to test an unknown server.')

const server = spawn(
  process.execPath,
  [viteCli, 'preview', '--host', '127.0.0.1', '--port', '4174', '--strictPort'],
  { stdio: 'ignore', windowsHide: true },
)
let serverError
server.once('error', (error) => { serverError = error })

const waitForServer = async () => {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (serverError) throw serverError
    if (server.exitCode !== null) throw new Error(`Vite preview exited with code ${server.exitCode}.`)
    try {
      if ((await fetch(host)).ok) return
    } catch {
      // The local preview is still starting.
    }
    await delay(100)
  }
  throw new Error('Timed out waiting for the local Vite preview.')
}

const stopServer = async () => {
  if (server.exitCode !== null) return
  const exited = once(server, 'exit')
  server.kill('SIGTERM')
  await Promise.race([exited, delay(3_000)])
  if (server.exitCode === null) {
    const killed = once(server, 'exit')
    server.kill('SIGKILL')
    await Promise.race([killed, delay(1_000)])
  }
}

let exitCode = 1
try {
  await waitForServer()
  const tests = spawn(
    process.execPath,
    [playwrightCli, 'test', ...forwardedArguments],
    { stdio: 'inherit', windowsHide: true },
  )
  const result = await Promise.race([
    once(tests, 'exit').then(([code]) => ({ code })),
    once(tests, 'error').then(([error]) => { throw error }),
  ])
  exitCode = typeof result.code === 'number' ? result.code : 1
} finally {
  await stopServer()
}

process.exitCode = exitCode
