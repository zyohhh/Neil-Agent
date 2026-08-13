import { expect, test } from '@playwright/test'

test('renders the fixture workbench without page overflow', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/?scene=running')

  await expect(page.getByText('P0 fixture preview')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Workspace' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Review' })).toBeVisible()
  await expect(page.getByText('Unavailable')).toBeVisible()
  await expect(page.getByText('Approve & Apply')).toHaveCount(0)

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)
  expect(overflow).toBe(false)
})

test('opens mobile review drawer', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/?scene=approval')
  await page.getByRole('button', { name: 'Open review' }).click()
  await expect(page.getByRole('heading', { name: 'Review' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Close review' })).toBeFocused()
  await page.getByRole('button', { name: 'Close review' }).click()
  await expect(page.getByRole('button', { name: 'Open review' })).toBeFocused()
})
