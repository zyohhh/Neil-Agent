import { spawn } from 'node:child_process'
import { chromium } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

const host = 'http://127.0.0.1:4174'
const viteCli = new URL('../node_modules/vite/bin/vite.js', import.meta.url)
const server = spawn(
  process.execPath,
  [viteCli.pathname.slice(process.platform === 'win32' ? 1 : 0), 'preview', '--host', '127.0.0.1', '--port', '4174', '--strictPort'],
  { stdio: 'ignore', windowsHide: true },
)

const waitForServer = async () => {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      if ((await fetch(host)).ok) return
    } catch {
      // Preview is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error('Timed out waiting for the fixture preview.')
}

const assert = (condition, message) => {
  if (!condition) throw new Error(message)
}

const assertNoSeriousA11yViolations = (result, viewportName) => {
  const violations = result.violations.filter((item) => ['critical', 'serious'].includes(item.impact ?? ''))
  if (violations.length > 0) {
    const details = violations.map((item) => `${item.id}: ${item.help} (${item.nodes.map((node) => node.target.join(' ')).join(', ')})`).join('; ')
    throw new Error(`${viewportName} has critical or serious accessibility violations: ${details}`)
  }
}

const browser = await chromium.launch()
try {
  await waitForServer()
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
  let externalRequests = 0
  context.on('request', (request) => {
    const url = new URL(request.url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) externalRequests += 1
  })
  const page = await context.newPage()
  const checkedViewports = [
    { width: 1440, height: 900 },
    { width: 1280, height: 800 },
    { width: 768, height: 1024 },
    { width: 390, height: 844 },
    { width: 320, height: 720 },
  ]

  for (const viewport of checkedViewports) {
    await page.setViewportSize(viewport)
    await page.goto(`${host}/?scene=running`, { waitUntil: 'networkidle' })
    assert(await page.getByText('P0 fixture preview').isVisible(), `Fixture banner is not visible at ${viewport.width}px.`)
    assert(await page.getByRole('heading', { name: 'Workspace' }).isVisible(), `Workspace is not visible at ${viewport.width}px.`)
    assert((await page.getByText('Approve & Apply').count()) === 0, 'Aggregate approval wording must not appear.')
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)
    assert(!overflow, `Page has horizontal overflow at ${viewport.width}px.`)
  }

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`${host}/?scene=running`, { waitUntil: 'networkidle' })
  assert(await page.getByRole('heading', { name: 'Review' }).isVisible(), 'Desktop review is not visible.')
  assert(await page.getByText('Unavailable').isVisible(), 'Cost unavailable state is missing.')
  const desktopA11y = await new AxeBuilder({ page }).analyze()
  assertNoSeriousA11yViolations(desktopA11y, 'Desktop')

  const initialLayout = await page.locator('.app-shell').evaluate((element) => ({
    row: getComputedStyle(element).gridTemplateRows,
    variable: getComputedStyle(element).getPropertyValue('--output-height'),
  }))
  await page.getByRole('button', { name: 'Expand output' }).click()
  await page.waitForTimeout(50)
  const expandedLayout = await page.locator('.app-shell').evaluate((element) => ({
    row: getComputedStyle(element).gridTemplateRows,
    variable: getComputedStyle(element).getPropertyValue('--output-height'),
  }))
  assert(
    expandedLayout.variable.trim() === '304px' && expandedLayout.row !== initialLayout.row,
    `Expanding Output did not resize the parent layout row (${JSON.stringify({ initialLayout, expandedLayout })}).`,
  )
  await page.getByRole('button', { name: 'Output fixture', exact: true }).click()
  await page.waitForTimeout(50)
  const collapsedLayout = await page.locator('.app-shell').evaluate((element) => ({
    row: getComputedStyle(element).gridTemplateRows,
    variable: getComputedStyle(element).getPropertyValue('--output-height'),
  }))
  assert(
    collapsedLayout.variable.trim() === '52px' && collapsedLayout.row !== expandedLayout.row,
    `Collapsing Output did not release layout space (${JSON.stringify({ expandedLayout, collapsedLayout })}).`,
  )

  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`${host}/?scene=approval`, { waitUntil: 'networkidle' })
  assert(await page.getByLabel('Preview state').isVisible(), 'Mobile fixture scene control is missing.')
  await page.getByRole('button', { name: 'Open review' }).click()
  assert(await page.getByRole('button', { name: 'Close review' }).isVisible(), 'Mobile review drawer did not open.')
  assert(await page.getByRole('button', { name: 'Close review' }).evaluate((element) => element === document.activeElement), 'Review drawer did not receive focus.')
  assert(await page.locator('.review-panel').getAttribute('aria-modal') === 'true', 'Review drawer is not exposed as modal.')
  await page.keyboard.press('Shift+Tab')
  assert(await page.locator('.review-panel').evaluate((drawer) => drawer.contains(document.activeElement)), 'Focus escaped the open review drawer.')
  await page.getByRole('button', { name: 'Approve fixture' }).click()
  assert(await page.getByText('Fixture approved').isVisible(), 'Fixture approval did not update locally.')
  assert(await page.getByText('No real side effect').isVisible(), 'Fixture approval boundary is not visible.')
  await page.getByRole('button', { name: 'Close review' }).click()
  await page.waitForTimeout(50)
  const focusReturned = await page.getByRole('button', { name: 'Open review' }).evaluate((element) => element === document.activeElement)
  assert(focusReturned, 'Review trigger did not regain focus.')

  await page.getByRole('button', { name: 'Open navigation' }).click()
  assert(await page.getByRole('button', { name: 'Close navigation' }).evaluate((element) => element === document.activeElement), 'Navigation drawer did not receive focus.')
  await page.keyboard.press('Escape')
  await page.waitForTimeout(50)
  assert(await page.getByRole('button', { name: 'Open navigation' }).evaluate((element) => element === document.activeElement), 'Navigation trigger did not regain focus.')

  const mobileA11y = await new AxeBuilder({ page }).analyze()
  assertNoSeriousA11yViolations(mobileA11y, 'Mobile')
  assert(externalRequests === 0, `Fixture preview made ${externalRequests} unexpected external request(s).`)

  console.log(`P0 E2E: ${checkedViewports.length} viewports, drawer focus, local approval, output sizing, and axe checks passed`)
  await context.close()
} finally {
  await browser.close()
  server.kill('SIGTERM')
}
