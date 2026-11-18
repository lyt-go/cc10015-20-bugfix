import { expect, test } from '@playwright/test'

const errors: string[] = []

test('同期群追踪：矩阵稳定性、排序分页、详情、补录回滚、重算', async ({ page, request }) => {
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400 && !r.url().includes('/supplement')) {
      errors.push(`${r.status()} ${r.url()}`)
    }
  })

  // 通过 API 新建独立群组，补录/重算只作用于它，测试不依赖后端既有状态
  const stamp = Date.now()
  const uniqueName = `E2E快照群-${stamp}`
  const createResp = await request.post('/api/cohort_track', {
    data: { 群组编号: `E2E-${stamp}`, 群组名称: uniqueName, 首次行为: '注册' },
  })
  expect(createResp.ok()).toBeTruthy()
  const created = await createResp.json()
  const targetId = Number(created.entry.id)
  const targetRow = `[data-testid="matrix-row-${targetId}"]`
  const denomSel = `${targetRow} [data-testid="denominator"]`

  await page.goto('/cohort_track')
  await expect(page.getByTestId('kpi-card').first()).toBeVisible()
  await expect(page.getByTestId('retention-matrix')).toBeVisible()

  // 1. 分页结构与总数（动态读取，不硬编码群组数量）
  const rows = page.getByTestId(/^matrix-row-/)
  await expect(rows).toHaveCount(2) // size=2
  const totalText = await page.locator('.page-foot span').first().innerText()
  const totalCohorts = Number(totalText.match(/共\s*(\d+)\s*个群组/)?.[1] ?? 0)
  expect(totalCohorts).toBeGreaterThanOrEqual(4)

  // 2. 翻到目标群组所在页，记录分母与整行数据，刷新后完全一致
  await gotoPageContaining(page, targetId)
  const targetDenom = await page.locator(denomSel).innerText()
  const ratesBefore = await page.locator(`${targetRow} td`).allInnerTexts()
  await page.getByTestId('refresh-matrix').click()
  await page.waitForTimeout(400)
  await gotoPageContaining(page, targetId)
  expect(await page.locator(denomSel).innerText()).toBe(targetDenom)
  expect(await page.locator(`${targetRow} td`).allInnerTexts()).toEqual(ratesBefore)

  // 3. 按新群组名称筛选后，分母与留存率不变
  await page.getByTestId('filter-群组名称').fill(uniqueName)
  await page.getByTestId('matrix-query').click()
  await expect(rows).toHaveCount(1)
  expect(await page.locator('[data-testid="denominator"]').innerText()).toBe(targetDenom)
  await page.getByTestId('matrix-reset').click()
  await expect(rows).toHaveCount(2)

  // 4. 排序：P2 升序/降序全量相反、元素集合相同（稳定 tie-breaker 保证全序）
  const p2Header = page.locator('[data-testid="retention-matrix"] thead th', { hasText: '第2周期' })
  await p2Header.click()
  await page.waitForTimeout(400)
  const orderAsc = await idsAcrossPages(page)
  await p2Header.click()
  await page.waitForTimeout(400)
  const orderDesc = await idsAcrossPages(page)
  expect(orderAsc).toEqual([...orderDesc].reverse())
  expect(new Set(orderAsc).size).toBe(totalCohorts)

  // 5. 翻页稳定：默认排序下所有页并集为全部群组，无重复遗漏
  await page.getByTestId('matrix-reset').click()
  await page.waitForTimeout(400)
  const allIds = await idsAcrossPages(page)
  expect(new Set(allIds).size).toBe(totalCohorts)
  expect(allIds).toContain(targetId)
  await gotoPageContaining(page, targetId)

  // 6. 详情抽屉：与工作台同数据，页面不重复渲染矩阵表
  await page.locator(`${targetRow} [data-testid="open-detail"]`).click()
  await expect(page.getByTestId('detail-drawer')).toBeVisible()
  await expect(page.getByTestId('retention-matrix')).toHaveCount(1)
  await expect(page.locator('.drawer .page-desc')).toContainText(`分母 ${targetDenom}`)
  await expect(page.getByTestId(/^rev-/).first()).toBeVisible()
  await page.locator('.drawer .btn.ghost').click()
  await expect(page.getByTestId('detail-drawer')).toHaveCount(0)

  // 7. 固定快照补录：分母不变，出现补录差异标注
  await page.locator(`${targetRow} [data-testid="open-supplement"]`).click()
  await expect(page.getByTestId('supplement-modal')).toBeVisible()
  await page.getByTestId('supplement-count').fill('8')
  await page.getByTestId('submit-supplement').click()
  await expect(page.getByTestId('supplement-modal')).toHaveCount(0)
  await page.waitForTimeout(400)
  expect(await page.locator(denomSel).innerText()).toBe(targetDenom)
  await expect(page.locator(targetRow)).toContainText('待并入 +8')

  // 8. 残留回滚：409 后矩阵不变，提示回滚信息
  await page.locator(`${targetRow} [data-testid="open-supplement"]`).click()
  await page.getByTestId('supplement-count').fill('3')
  await page.getByTestId('supplement-residue').check()
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/supplement')),
    page.getByTestId('submit-supplement').click(),
  ])
  expect(resp.status()).toBe(409)
  await page.waitForTimeout(400)
  expect(await page.locator(denomSel).innerText()).toBe(targetDenom)
  await expect(page.locator('.page-foot')).toContainText('回滚')

  // 9. 最新快照补录：吸收累计待并入（8 人）+ 本次 5 人，分母增加 13
  await page.locator(`${targetRow} [data-testid="open-supplement"]`).click()
  await page.getByTestId('supplement-count').fill('5')
  await page.selectOption('[data-testid="supplement-policy"]', 'latest_snapshot')
  await page.getByTestId('submit-supplement').click()
  await page.waitForTimeout(400)
  expect(Number(await page.locator(denomSel).innerText())).toBe(Number(targetDenom) + 13)

  // 10. 重算：通过 V020 校验，分母保持不变
  await page.locator(`${targetRow} [data-testid="recompute"]`).click()
  await page.waitForTimeout(400)
  expect(Number(await page.locator(denomSel).innerText())).toBe(Number(targetDenom) + 13)
  await expect(page.locator('.page-foot')).toContainText('V020')

  // 11. 运行分析（分析看板形态保留）
  await page.getByTestId('run-analysis').click()
  await expect(page.locator('.analysis-bar').first()).toBeVisible()
  await expect(page.locator('.data-table tbody tr').first()).toBeVisible()

  expect(errors).toEqual([])
})

async function gotoFirstPage(page) {
  let guard = 0
  while (guard++ < 20 && !(await page.getByTestId('prev-page').isDisabled())) {
    await page.getByTestId('prev-page').click()
    await page.waitForTimeout(300)
  }
}

async function gotoPageContaining(page, cohortId) {
  const selector = `[data-testid="matrix-row-${cohortId}"]`
  await gotoFirstPage(page)
  let guard = 0
  while (guard++ < 20) {
    if (await page.locator(selector).count()) return
    if (await page.getByTestId('next-page').isDisabled()) break
    await page.getByTestId('next-page').click()
    await page.waitForTimeout(300)
  }
  throw new Error(`cohort ${cohortId} row not found in matrix pages`)
}

async function idsInOrder(page): Promise<number[]> {
  const testids = await page.getByTestId(/^matrix-row-/).evaluateAll(
    (els) => els.map((el) => el.getAttribute('data-testid')),
  )
  return testids.map((t) => Number(String(t).replace('matrix-row-', '')))
}

async function idsAcrossPages(page): Promise<number[]> {
  await gotoFirstPage(page)
  const collected: number[] = []
  let guard = 0
  while (guard++ < 20) {
    collected.push(...await idsInOrder(page))
    if (await page.getByTestId('next-page').isDisabled()) break
    await page.getByTestId('next-page').click()
    await page.waitForTimeout(300)
  }
  return collected
}
