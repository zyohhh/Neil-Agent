import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'

const baseUrl = process.env.NEIL_WORKBENCH_PREVIEW_URL ?? 'http://127.0.0.1:4174'
const shouldStartServer = !process.env.NEIL_WORKBENCH_PREVIEW_URL
const outputDirectory = resolve('tests', 'visual-baselines')
const viewports = [
  { name: 'desktop-1440x900', width: 1440, height: 900 },
  { name: 'desktop-1280x800', width: 1280, height: 800 },
  { name: 'tablet-768x1024', width: 768, height: 1024 },
  { name: 'mobile-390x844', width: 390, height: 844 },
]

await mkdir(outputDirectory, { recursive: true })

const viteCli = new URL('../node_modules/vite/bin/vite.js', import.meta.url)
const server = shouldStartServer
  ? spawn(
    process.execPath,
    [viteCli.pathname.slice(process.platform === 'win32' ? 1 : 0), 'preview', '--host', '127.0.0.1', '--port', '4174', '--strictPort'],
    { stdio: 'ignore', windowsHide: true },
  )
  : undefined

for (let attempt = 0; shouldStartServer && attempt < 60; attempt += 1) {
  try {
    if ((await fetch(baseUrl)).ok) break
  } catch {
    // Preview is still starting.
  }
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 100))
}

const browser = await chromium.launch()
try {
  const page = await browser.newPage({ reducedMotion: 'reduce' })
  for (const viewport of viewports) {
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await page.goto(`${baseUrl}/?scene=running`, { waitUntil: 'networkidle' })
    await page.screenshot({
      path: resolve(outputDirectory, `${viewport.name}.png`),
      fullPage: false,
      animations: 'disabled',
    })
  }
} finally {
  await browser.close()
  server?.kill('SIGTERM')
}
