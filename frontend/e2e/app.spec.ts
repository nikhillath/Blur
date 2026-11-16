import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Browser, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'
import path from 'node:path'

const FIX = (n: string) => path.resolve(import.meta.dirname, '../../tests/fixtures', n)
const PW = 'e2e-password-123'
let counter = 0
const uniq = () => `u${Date.now()}${counter++}@example.com`

async function register(page: Page, name = 'Test User') {
  const email = uniq()
  await page.goto('/register')
  await page.getByLabel('Name').fill(name)
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password').fill(PW)
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
  return email
}
async function login(page: Page, email: string) {
  await page.goto('/login')
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password').fill(PW)
  await page.getByRole('button', { name: 'Log in' }).click()
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
}
async function upload(page: Page, file: string) {
  await page.goto('/upload')
  await page.setInputFiles('input[type=file]', FIX(file))
  await expect(page).toHaveURL(/\/documents\/[0-9a-f-]{36}$/)
  // the first scan after a server start also loads the OCR and language models, so allow for that
  await expect(page.getByText('COMPLETED').first()).toBeVisible({ timeout: 120_000 })
  return page.url()
}
async function newUser(browser: Browser) {
  const ctx = await browser.newContext()
  const page = await ctx.newPage()
  return { ctx, page, email: await register(page) }
}
async function bulkApply(page: Page, min = '0') {
  await page.getByLabel('Minimum confidence').fill(min)
  await page.getByRole('button', { name: 'Apply' }).click()
}
async function selectText(page: Page, needle: string) {
  await page.evaluate((n) => {
    const doc = document.querySelector('[data-testid=doc]')!
    const walker = document.createTreeWalker(doc, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const i = node.textContent!.indexOf(n)
      if (i >= 0) {
        const r = document.createRange()
        r.setStart(node, i); r.setEnd(node, i + n.length)
        const s = getSelection()!
        s.removeAllRanges(); s.addRange(r)
        doc.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
        return
      }
    }
    throw new Error(`text ${n} not found`)
  }, needle)
}

test('full workflow: register, upload, scan, review, redact, download, log out and back in', async ({ page }) => {
  const email = await register(page, 'Flow User')
  await expect(page.getByText('Documents').first()).toBeVisible()

  await upload(page, 'sample.txt')
  await expect(page.getByText(/rules-only|hybrid/).first()).toBeVisible()
  await page.getByRole('link', { name: 'Review' }).click()
  await expect(page.locator('mark').first()).toBeVisible()
  const secretsInText = await page.getByTestId('doc').textContent()
  expect(secretsInText).toContain('riya.sharma@example.com')            // document text is shown to its owner...
  const list = await page.locator('.list').textContent()
  expect(list).not.toContain('riya.sharma@example.com')                 // ...but the entity list only shows masked values
  expect(list).toContain('model suggestion')

  // keep the email, redact everything else that is pending, then add a missed span by selecting text
  await page.locator('.ent', { hasText: 'r***@example.com' }).getByRole('button', { name: 'Keep' }).click()
  await expect(page.locator('.ent', { hasText: 'r***@example.com' }).getByText('human decision')).toBeVisible()
  await bulkApply(page)
  await expect(page.getByText('model suggestion')).toHaveCount(0)
  await selectText(page, 'fictional')
  await page.getByRole('button', { name: 'Add and redact' }).click()
  await expect(page.locator('mark', { hasText: 'fictional' })).toBeVisible()

  page.once('dialog', (d) => { expect(d.message()).toContain('item(s) will be redacted'); void d.accept() })
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    (async () => {
      await page.getByRole('button', { name: /^Redact \d+ item/ }).click()
      await expect(page.getByText('PASSED').first()).toBeVisible()
      await page.getByRole('button', { name: 'Download redacted file' }).click()
    })(),
  ])
  expect(download.suggestedFilename()).toBe('redacted_sample.txt')
  const out = readFileSync((await download.path())!, 'utf8')
  expect(out).toContain('riya.sharma@example.com')                       // KEEP respected
  for (const gone of ['98765 43210', 'ABCDE1234F', '4521 7834 9123', 'Riya Sharma', 'fictional']) expect(out).not.toContain(gone)
  expect(out).toContain('[REDACTED]')
  expect(out).toContain('CUSTOMER INFORMATION RECORD')

  await page.getByRole('link', { name: '← Document' }).click()
  await expect(page.getByRole('row', { name: /redactions/ })).toContainText('PASSED')

  await page.goto('/dashboard')
  await expect(page.getByText('Redacted files').locator('..')).toContainText('1')
  await expect(page.getByRole('cell', { name: 'redaction created' })).toBeVisible()

  await page.getByRole('button', { name: 'Log out' }).click()
  await expect(page).toHaveURL('/')
  await page.goto('/documents')
  await expect(page).toHaveURL(/\/login$/)                               // signed out: guarded
  await login(page, email)
  await page.goto('/documents')
  await expect(page.getByRole('link', { name: 'sample.txt' })).toBeVisible() // data persisted
})

test('session survives a page reload (refresh cookie) and ends on logout', async ({ page }) => {
  await register(page)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
  await page.getByRole('button', { name: 'Log out' }).click()
  await page.goto('/dashboard')
  await expect(page).toHaveURL(/\/login$/)
})

test('PDF: redact in the browser and download a verified file', async ({ page }) => {
  await register(page)
  await upload(page, 'sample.pdf')
  await page.getByRole('link', { name: 'Review' }).click()
  await expect(page.locator('mark').first()).toBeVisible()
  await bulkApply(page)
  page.once('dialog', (d) => void d.accept())
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    (async () => {
      await page.getByRole('button', { name: /^Redact \d+ item/ }).click()
      await expect(page.getByText('PASSED').first()).toBeVisible()
      await page.getByRole('button', { name: 'Download redacted file' }).click()
    })(),
  ])
  expect(download.suggestedFilename()).toBe('redacted_sample.pdf')
  const bytes = readFileSync((await download.path())!)
  expect(bytes.subarray(0, 5).toString()).toBe('%PDF-')
  expect(bytes.length).toBeGreaterThan(500)
})

test('declining the confirmation sends nothing', async ({ page }) => {
  await register(page)
  await upload(page, 'sample.txt')
  await page.getByRole('link', { name: 'Review' }).click()
  await bulkApply(page)
  let created = false
  page.on('request', (r) => { if (r.url().endsWith('/redact')) created = true })
  page.once('dialog', (d) => void d.dismiss())
  await page.getByRole('button', { name: /^Redact \d+ item/ }).click()
  await page.waitForTimeout(800)
  expect(created).toBe(false)
})

test('another user cannot open or see someone else\'s document, and cannot reach admin', async ({ page, browser }) => {
  await register(page)
  const docUrl = await upload(page, 'sample.txt')
  const other = await newUser(browser)
  await other.page.goto(new URL(docUrl).pathname)
  await expect(other.page.getByRole('alert')).toContainText(/not found/i)
  await other.page.goto('/documents')
  await expect(other.page.getByText('No documents found.')).toBeVisible()
  await other.page.goto('/admin')
  await expect(other.page).toHaveURL(/\/dashboard$/)
  await expect(other.page.getByRole('link', { name: 'Admin' })).toHaveCount(0)
  await other.ctx.close()
})

test('assigned reviewer can review but cannot open the document page', async ({ page, browser }) => {
  await register(page)
  const docUrl = await upload(page, 'sample.txt')
  await page.getByLabel('Reviewer email').fill('reviewer-e2e@example.com')
  await page.getByRole('button', { name: 'Add reviewer' }).click()
  await expect(page.getByText('E2E Reviewer')).toBeVisible()

  const ctx = await browser.newContext()
  const r = await ctx.newPage()
  await login(r, 'reviewer-e2e@example.com')
  await expect(r.getByText('Assigned to you for review')).toBeVisible()
  await r.getByRole('link', { name: 'Review' }).click()
  await expect(r.locator('mark').first()).toBeVisible()
  await r.locator('.ent').first().getByRole('button', { name: 'Keep' }).click()
  await expect(r.locator('.ent').first().getByText('human decision')).toBeVisible()
  await r.goto(new URL(docUrl).pathname)
  await expect(r.getByRole('alert')).toContainText(/not found/i)
  await ctx.close()

  await page.reload()                                                   // the owner sees the reviewer's decision
  await page.getByRole('link', { name: 'Review' }).click()
  await expect(page.getByText('human decision').first()).toBeVisible()
})

test('admin can manage users and read the audit log', async ({ page, browser }) => {
  const victim = await newUser(browser)
  await login(page, 'admin-e2e@example.com')
  await page.getByRole('link', { name: 'Admin' }).click()
  const row = page.getByRole('row', { name: new RegExp(victim.email) })
  await expect(row).toBeVisible()
  await row.getByLabel(`Role of ${victim.email}`).selectOption('REVIEWER')
  await page.reload()
  await expect(page.getByRole('row', { name: new RegExp(victim.email) }).getByRole('combobox')).toHaveValue('REVIEWER')
  await page.getByLabel('Filter audit by action').fill('admin_user_updated')
  await expect(page.getByRole('cell', { name: 'ADMIN_USER_UPDATED' }).first()).toBeVisible()
  await victim.ctx.close()
})

test('bad uploads and failed scans show clear messages and recover', async ({ page }) => {
  await register(page)
  await page.goto('/upload')
  await page.setInputFiles('input[type=file]', FIX('malware.exe'))
  await expect(page.getByRole('alert')).toContainText(/unsupported file type/i)
  await expect(page).toHaveURL(/\/upload$/)

  await page.setInputFiles('input[type=file]', FIX('corrupt.pdf'))
  await expect(page).toHaveURL(/\/documents\/[0-9a-f-]{36}$/)
  await expect(page.getByText('the PDF could not be read')).toBeVisible()
  await page.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByRole('row').filter({ hasText: 'FAILED' })).toHaveCount(2) // history kept
})

test('a document without PII shows an empty review state', async ({ page }) => {
  await register(page)
  await upload(page, 'no_pii.txt')
  await page.getByRole('link', { name: 'Review' }).click()
  await expect(page.getByText('No detections in this document.')).toBeVisible()
  await expect(page.getByRole('button', { name: /^Redact 0 item/ })).toBeDisabled()
})

test('accessibility: no serious or critical axe violations on the main pages', async ({ page }) => {
  const scan = async (label: string) => {
    const r = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()
    const bad = r.violations.filter((v) => ['serious', 'critical'].includes(v.impact ?? ''))
    expect(bad.map((v) => `${label}: ${v.id} (${v.nodes.length}) ${v.help} :: ${v.nodes.slice(0, 2).map((n) => n.html.slice(0, 90) + ' => ' + (n.any[0]?.message ?? '')).join(' | ')}`), label).toEqual([])
  }
  for (const p of ['/', '/login', '/register']) { await page.goto(p); await scan(p) }
  await register(page)
  const docUrl = await upload(page, 'sample.txt')
  await scan('document')
  for (const p of ['/dashboard', '/documents', '/upload']) { await page.goto(p); await expect(page.getByRole('heading').first()).toBeVisible(); await scan(p) }
  await page.goto(new URL(docUrl).pathname)
  await page.getByRole('link', { name: 'Review' }).click()
  await expect(page.locator('mark').first()).toBeVisible()
  await scan('review')
})

test('keyboard only: the login form can be completed and submitted', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('Email').focus()
  await page.keyboard.type('admin-e2e@example.com')
  await page.keyboard.press('Tab')
  await page.keyboard.type(PW)
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
})

test('mobile width: pages do not scroll horizontally', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 700 })
  await register(page)
  const docUrl = await upload(page, 'sample.txt')
  const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  for (const p of ['/dashboard', '/documents', '/upload', new URL(docUrl).pathname]) {
    await page.goto(p)
    await expect(page.getByRole('heading').first()).toBeVisible()
    expect(await overflow(), `horizontal overflow on ${p}`).toBeLessThanOrEqual(1)
  }
  await page.getByRole('link', { name: 'Review' }).click()
  await expect(page.locator('mark').first()).toBeVisible()
  expect(await overflow(), 'horizontal overflow on review').toBeLessThanOrEqual(1)
})
