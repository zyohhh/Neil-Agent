import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

const checkedViewports = [
  { name: 'desktop-1440x900', width: 1440, height: 900 },
  { name: 'desktop-1280x800', width: 1280, height: 800 },
  { name: 'tablet-768x1024', width: 768, height: 1024 },
  { name: 'mobile-390x844', width: 390, height: 844 },
  { name: 'mobile-320x720', width: 320, height: 720 },
]

const expectNoSeriousA11yViolations = async (page: Page) => {
  const result = await new AxeBuilder({ page }).analyze()
  const violations = result.violations
    .filter((item) => item.impact === 'critical' || item.impact === 'serious')
    .map((item) => ({ id: item.id, targets: item.nodes.map((node) => node.target) }))
  expect(violations).toEqual([])
}

test('renders deterministic fixture states at every supported viewport', async ({ page }) => {
  const externalRequests: string[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) externalRequests.push(request.url())
  })

  for (const viewport of checkedViewports) {
    await page.setViewportSize(viewport)
    await page.goto('/?scene=running', { waitUntil: 'networkidle' })
    await expect(page.getByText('P0 fixture preview')).toBeVisible()
    await expect(page.getByLabel('Preview state')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()
    await expect(page.getByText('Approve & Apply')).toHaveCount(0)

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    )
    expect(overflow, `Page overflow at ${viewport.width}px`).toBe(false)
  }

  expect(externalRequests).toEqual([])
})

test('keeps desktop review, output sizing, and mode semantics accessible', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/?scene=running', { waitUntil: 'networkidle' })

  await expect(page.getByRole('heading', { name: 'Review' })).toBeVisible()
  await expect(page.getByText('Unavailable', { exact: true })).toBeVisible()
  await expect(page.getByRole('radiogroup', { name: 'Preview work mode' })).toBeVisible()
  await expect(page.getByRole('radio', { name: 'Build' })).toHaveAttribute('aria-disabled', 'true')

  const shell = page.locator('.app-shell')
  const initialRows = await shell.evaluate((element) => getComputedStyle(element).gridTemplateRows)
  await page.getByRole('button', { name: 'Expand output' }).click()
  await expect(shell).toHaveCSS('--output-height', '304px')
  const expandedRows = await shell.evaluate((element) => getComputedStyle(element).gridTemplateRows)
  expect(expandedRows).not.toBe(initialRows)

  await page.getByRole('button', { name: 'Output fixture', exact: true }).click()
  await expect(shell).toHaveCSS('--output-height', '52px')

  await page.goto('/?scene=idle', { waitUntil: 'networkidle' })
  const buildMode = page.getByRole('radio', { name: 'Build' })
  await buildMode.focus()
  await page.keyboard.press('ArrowLeft')
  await expect(page.getByRole('radio', { name: 'Focus' })).toHaveAttribute('aria-checked', 'true')

  await expectNoSeriousA11yViolations(page)
})

test('traps and restores focus for mobile review and navigation drawers', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/?scene=approval', { waitUntil: 'networkidle' })

  await page.getByRole('button', { name: 'Open review' }).click()
  const review = page.locator('.review-panel')
  await expect(review).toHaveAttribute('aria-modal', 'true')
  await expect(page.getByRole('button', { name: 'Close review' })).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  expect(await review.evaluate((drawer) => drawer.contains(document.activeElement))).toBe(true)

  await page.getByRole('button', { name: 'Approve fixture' }).click()
  await expect(page.getByText('Fixture approved')).toBeVisible()
  await expect(page.getByText('No real side effect')).toBeVisible()
  await page.getByRole('button', { name: 'Close review' }).click()
  await expect(page.getByRole('button', { name: 'Open review' })).toBeFocused()

  await page.getByRole('button', { name: 'Open navigation' }).click()
  await expect(page.getByRole('button', { name: 'Close navigation' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Open navigation' })).toBeFocused()

  await expectNoSeriousA11yViolations(page)
})

test('@visual matches the approved P6 fixture baselines', async ({ page }) => {
  for (const viewport of checkedViewports.slice(0, 4)) {
    await page.setViewportSize(viewport)
    await page.goto('/?scene=running', { waitUntil: 'networkidle' })
    await page.evaluate(() => document.fonts.ready)
    await expect(page).toHaveScreenshot(`${viewport.name}.png`, { fullPage: false })
  }
})
