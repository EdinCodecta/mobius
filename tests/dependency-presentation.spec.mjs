// Real production panels, mounted without a backend, login or live owner data.
import { test, expect } from '@playwright/test'
import { createServer } from '../frontend/node_modules/vite/dist/node/index.js'
import { fileURLToPath } from 'node:url'
import tailwindcss from '../frontend/node_modules/@tailwindcss/vite/dist/index.mjs'
import { mkdtemp, rm, mkdir, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
test.use({ storageState: { cookies: [], origins: [] } })
let server, base, cacheDir
test.beforeAll(async () => {
  const themeSource = await readFile(new URL('../backend/app/theme.py', import.meta.url), 'utf8')
  const theme = themeSource.split('DEFAULT_THEME = ')[1].split('\"\"\"')[1].replace(/^\\\n/, '')
  cacheDir = await mkdtemp(join(tmpdir(), 'dependency-panels-'))
  server = await createServer({ root: fileURLToPath(new URL('../frontend', import.meta.url)),
    configFile: false, cacheDir, plugins: [tailwindcss()], appType: 'custom', logLevel: 'error', esbuild: { jsx: 'automatic' },
    server: { host: '127.0.0.1', port: 0, hmr: false, ws: false } })
  server.middlewares.use(async (req, res, next) => {
    if (req.url !== '/') return next()
    res.setHeader('Content-Type', 'text/html')
    res.end(await server.transformIndexHtml(req.url, `<link rel="stylesheet" href="/vendor/fonts/mobius-fonts.css"><style>${theme}</style><meta name="viewport" content="width=device-width,initial-scale=1"><div id="root"></div><script type="module" src="/src/components/ChatView/__tests__/dependencyPresentationFixture.jsx"></script>`))
  })
  await server.listen()
  base = `http://127.0.0.1:${server.httpServer.address().port}`
})
test.afterAll(async () => { await server?.close(); if (cacheDir) await rm(cacheDir, { recursive: true, force: true }) })
async function open(page) {
  await page.goto(base)
  await expect(page.getByRole('heading', { name: 'Goal handoffs' })).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
}

test('card backlink disclosures are keyboard accessible and navigate without answering', async ({ page }) => {
  await open(page)
  const related = page.locator('.qcard__related-work > summary')
  await related.focus(); await page.keyboard.press('Enter')
  const many = page.locator('.chat__dependency-backlinks > summary')
  await many.focus(); await page.keyboard.press('Enter')
  const link = page.getByRole('link', { name: 'Finish the blocked update' })
  await expect(link).toBeVisible()
  await link.click()
  expect(await page.evaluate(() => window.navigations)).toEqual([base + '/shell/?chat=dependent'])
  await expect(page.locator('.qcard')).not.toHaveClass(/qcard--answered/)
})

test('exact decision link is distinct from source backlinks and preserves full condition', async ({ page }) => {
  await open(page)
  const details = page.getByRole('region', { name: 'Dependent Goal' })
  await expect(details).toContainText('after preserving local work and checking the host')
  await details.getByRole('link', { name: /^Open decision/ }).click()
  expect(await page.evaluate(() => window.navigations.at(-1))).toBe(base + '/shell/?chat=source&focus=question&question=choice')
  await page.evaluate(() => window.mountFixture({ held: true }))
  await expect(details).not.toContainText('Continues automatically')
  await expect(details).toContainText('Result kept until this Goal continues')
})

test('recovery action remains actionable and saved outcome expands as a quiet clock record', async ({ page }) => {
  await open(page)
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.getByRole('button', { name: 'Resume', exact: true }).click()
  expect(await page.evaluate(() => window.resumes)).toBe(1)
  const history = page.locator('.chat__wait-history')
  await history.locator('summary').click()
  await expect(history).toContainText('Answered without a source reply · Not now')
  await expect(history.getByRole('link', { name: /^Open decision/ })).toBeVisible()
})

for (const width of [390, 960]) test(`panels fill the message column without overflow at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1100 })
  await open(page)
  await page.locator('.qcard__related-work > summary').click()
  await page.locator('.chat__dependency-backlinks > summary').click()
  await page.locator('.chat__wait-history summary').click()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  const main = await page.locator('main').boundingBox()
  const card = await page.locator('.chat__wait-history').boundingBox()
  expect(Math.round(card.width)).toBe(Math.round(main.width - 32))
  if (process.env.MOBIUS_PRESENTATION_SCREENS) {
    await mkdir(process.env.MOBIUS_PRESENTATION_SCREENS, { recursive: true })
    await page.screenshot({ path: join(process.env.MOBIUS_PRESENTATION_SCREENS, `stage4-${width}.png`), fullPage: true })
  }
})
