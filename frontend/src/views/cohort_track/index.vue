<template>
  <section class="page" data-module="cohort_track">
    <header class="page-head">
      <div>
        <h2>同期群追踪</h2>
        <p class="page-desc">群组快照与留存周期共用同一事件边界，列表筛选、排序、翻页与详情页始终基于同一份留存矩阵。</p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-matrix" type="button" @click="reloadMatrix(1)">刷新矩阵</button>
        <button class="btn" data-testid="export-matrix" type="button" @click="exportMatrix">导出矩阵</button>
      </div>
    </header>

    <!-- KPI：取自已确认快照，刷新不变 -->
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>

    <!-- 运行分析（数据取自已确认快照，不再随机） -->
    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <input v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="analysisResults.length" style="margin-bottom:16px">
      <thead><tr><th v-for="c in analysisColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in analysisResults" :key="row.id"><td v-for="c in analysisColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>

    <!-- 留存矩阵筛选：周期、群组名称、状态 -->
    <form class="filter-bar" @submit.prevent="reloadMatrix(1)">
      <label class="filter-item"><span>群组名称</span>
        <input v-model="filters['群组名称']" data-testid="filter-群组名称" placeholder="按群组名称检索" />
      </label>
      <label class="filter-item"><span>群组状态</span>
        <select v-model="filters.status" data-testid="filter-status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <label class="filter-item"><span>留存周期</span>
        <select v-model="selectedPeriods" data-testid="filter-periods" multiple size="3" style="min-width:120px">
          <option v-for="p in allPeriods" :key="p" :value="p">{{ periodLabel(p) }}</option>
        </select>
      </label>
      <button class="btn" data-testid="matrix-query" type="submit">查询</button>
      <button class="btn ghost" data-testid="matrix-reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <!-- 留存矩阵数据表：页面内唯一一份，详情抽屉复用同一接口数据 -->
    <table class="data-table" data-testid="retention-matrix">
      <thead>
        <tr>
          <th @click="toggleSort('cohort_id')" class="sortable">群组编号 {{ sortMark('cohort_id') }}</th>
          <th @click="toggleSort('群组名称')" class="sortable">群组名称 {{ sortMark('群组名称') }}</th>
          <th @click="toggleSort('denominator')" class="sortable">分母 {{ sortMark('denominator') }}</th>
          <th>事件边界</th>
          <th>快照版本</th>
          <th
            v-for="p in matrixPayload.periods"
            :key="p"
            class="sortable"
            @click="toggleSort(p)"
          >{{ periodLabel(p) }} {{ sortMark(p) }}</th>
          <th>补录差异</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in matrixRows" :key="String(row.cohort_id)" :data-testid="`matrix-row-${row.cohort_id}`">
          <td>{{ row['群组编号'] }}</td>
          <td>{{ row['群组名称'] }}</td>
          <td data-testid="denominator">{{ row.denominator }}</td>
          <td>{{ row.event_boundary }}</td>
          <td>v{{ row.snapshot_revision }}</td>
          <td v-for="cell in row.cells" :key="cell.period">
            {{ (cell.rate * 100).toFixed(2) }}%
            <span style="color:var(--muted);font-size:11px">({{ cell.retained }}/{{ cell.denominator }})</span>
          </td>
          <td>
            <span v-if="row.supplement_diff" style="color:#b54708;font-size:12px">
              待并入 +{{ row.supplement_diff.added_members }}（新分母 {{ row.supplement_diff.latest_denominator }}）
            </span>
            <span v-else style="color:var(--muted)">—</span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" data-testid="open-detail" @click="openDetail(row.cohort_id)">详情</button>
            <button class="link" type="button" data-testid="open-supplement" @click="openSupplement(row.cohort_id)">补录</button>
            <button class="link" type="button" data-testid="recompute" @click="recompute(row.cohort_id)">重算</button>
          </td>
        </tr>
        <tr v-if="!matrixRows.length">
          <td :colspan="matrixPayload.periods.length + 7" class="empty-state">暂无留存矩阵数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ matrixPayload.total }} 个群组 · 第 {{ matrixPayload.page }} / {{ totalPages }} 页</span>
      <span class="pager">
        <button class="btn" type="button" data-testid="prev-page" :disabled="matrixPayload.page <= 1" @click="reloadMatrix(matrixPayload.page - 1)">上一页</button>
        <button class="btn" type="button" data-testid="next-page" :disabled="matrixPayload.page >= totalPages" @click="reloadMatrix(matrixPayload.page + 1)">下一页</button>
      </span>
      <span v-if="message" :class="messageError ? 'error-text' : ''">{{ message }}</span>
    </footer>

    <!-- 详情抽屉：与工作台同一接口快照，只展示成员事件/修订历史，不重复渲染矩阵表 -->
    <div v-if="detail" class="drawer-backdrop" data-testid="detail-drawer" @click.self="detail = null">
      <aside class="drawer">
        <div class="drawer-head">
          <h3>群组详情 · {{ detail['群组编号'] }}</h3>
          <button class="btn ghost" type="button" @click="detail = null">关闭</button>
        </div>
        <p class="page-desc">
          快照版本 v{{ detail.snapshot_revision }} ｜ 事件边界 {{ detail.event_boundary }} ｜
          指标键 {{ detail.metric_key }} ｜ 分母 {{ detail.denominator }}
        </p>
        <h4>成员事件（同一事件边界，前 10 条）</h4>
        <table class="data-table">
          <thead><tr><th>成员</th><th>活跃周期</th><th>事件边界</th></tr></thead>
          <tbody>
            <tr v-for="m in detail.member_events.slice(0, 10)" :key="m.user_id">
              <td>{{ m.user_id }}</td>
              <td>{{ m.active_periods.map(periodLabel).join('、') }}</td>
              <td>{{ m.event_boundary }}</td>
            </tr>
          </tbody>
        </table>
        <h4>修订历史（独立版本，补录不覆盖筛选结果）</h4>
        <table class="data-table">
          <thead><tr><th>版本</th><th>状态</th><th>来源</th><th>分母</th><th>补录</th><th>备注</th></tr></thead>
          <tbody>
            <tr v-for="rev in detail.revisions" :key="rev.revision" :data-testid="`rev-${rev.revision}`">
              <td>v{{ rev.revision }}</td>
              <td>{{ statusText(rev.status) }}</td>
              <td>{{ rev.source }}</td>
              <td>{{ rev.matrix.denominator }}</td>
              <td>{{ rev.supplement_count }}</td>
              <td>{{ rev.remark || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </aside>
    </div>

    <!-- 补录弹窗：固定/最新两种版本冲突策略 + 残留回滚演示 -->
    <div v-if="supplementTarget" class="modal-backdrop" data-testid="supplement-modal">
      <form class="modal" @submit.prevent="submitSupplement">
        <h3>成员补录 · 群组 #{{ supplementTarget }}</h3>
        <label class="create-field"><span>补录人数</span>
          <input v-model.number="supplementForm.count" data-testid="supplement-count" type="number" min="1" required />
        </label>
        <label class="create-field"><span>版本冲突策略</span>
          <select v-model="supplementForm.policy" data-testid="supplement-policy">
            <option value="fixed_snapshot">固定筛选前快照（保留分母，标注补录差异）</option>
            <option value="latest_snapshot">采用补录后最新快照（新分母并重算周期）</option>
          </select>
        </label>
        <label class="create-field"><span>备注</span>
          <input v-model="supplementForm.remark" data-testid="supplement-remark" />
        </label>
        <label class="create-field"><span>
          <input v-model="supplementForm.residue" type="checkbox" data-testid="supplement-residue" />
          模拟未清残留（V020 触发事务回滚）
        </span></label>
        <div class="page-actions">
          <button class="btn" type="button" @click="supplementTarget = null">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-supplement">确认补录</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'

const ENDPOINT = '/api/cohort_track'
const allPeriods = ['P0', 'P1', 'P2', 'P3', 'P4', 'P5']
const statuses = ['草稿', '待校验', '已生效', '已停用']
const statusTextMap: Record<string, string> = {
  confirmed: '已确认',
  pending: '重算中',
  rolled_back: '已回滚',
  standby: '待并入',
  superseded: '已吸收',
}

type Cell = { period: string; label: string; denominator: number; retained: number; rate: number }
type MatrixRow = {
  cohort_id: number
  denominator: number
  event_boundary: number
  snapshot_revision: number
  metric_key: string
  cells: Cell[]
  supplement_diff: { added_members: number; latest_denominator: number } | null
  [key: string]: unknown
}

const summaryMetrics = ref<{ label: string; value: number; trend: number }[]>([])
const params = ref<Record<string, string>>({})
const paramFields = ['首次行为', '时间基准', '追踪周期', '留存窗口']
const analysisColumns = ['id', '分组', '指标值', '对比基准', '差异']
const analysisResults = ref<Record<string, number | string>[]>([])
const chartData = ref<{ label: string; value: number }[]>([])

const filters = ref<Record<string, string>>({ '群组名称': '', status: '' })
const selectedPeriods = ref<string[]>([])
const sortBy = ref('cohort_id')
const sortOrder = ref<'asc' | 'desc'>('asc')
const page = ref(1)
const pageSize = 2
const matrixRows = ref<MatrixRow[]>([])
const matrixPayload = ref<{ total: number; page: number; size: number; periods: string[] }>({
  total: 0, page: 1, size: pageSize, periods: [...allPeriods],
})
const detail = ref<Record<string, any> | null>(null)
const supplementTarget = ref<number | null>(null)
const supplementForm = ref({ count: 10, policy: 'fixed_snapshot', remark: '', residue: false })
const message = ref('')
const messageError = ref(false)

const totalPages = computed(() => Math.max(1, Math.ceil(matrixPayload.value.total / matrixPayload.value.size)))

function periodLabel(p: string) {
  const idx = allPeriods.indexOf(p)
  return idx >= 0 ? `第${idx}周期` : p
}
function barHeight(v: number) {
  const max = Math.max(...chartData.value.map((d) => d.value), 1)
  return Math.max(10, (v / max) * 140)
}
function statusText(s: string) {
  return statusTextMap[s] ?? s
}
function sortMark(column: string) {
  if (sortBy.value !== column) return ''
  return sortOrder.value === 'asc' ? '▲' : '▼'
}
function toggleSort(column: string) {
  if (sortBy.value === column) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortBy.value = column
    sortOrder.value = 'asc'
  }
  void reloadMatrix(1)
}
function setMessage(text: string, isError = false) {
  message.value = text
  messageError.value = isError
}

async function fetchSummary() {
  try {
    const res = await request(`${ENDPOINT}/summary`)
    summaryMetrics.value = (await res.json()).metrics || []
  } catch {
    summaryMetrics.value = []
  }
}

async function runAnalysis() {
  const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) })
  const data = await res.json()
  analysisResults.value = data.items || []
  chartData.value = (data.items || []).map((r: Record<string, number | string>) => ({
    label: String(r.分组),
    value: Number(r.指标值) || 0,
  })).slice(0, 6)
}

function matrixQuery(targetPage: number) {
  const query = new URLSearchParams()
  if (filters.value['群组名称']) query.set('群组名称', filters.value['群组名称'])
  if (filters.value.status) query.set('status', filters.value.status)
  if (selectedPeriods.value.length) query.set('periods', selectedPeriods.value.join(','))
  query.set('sort_by', sortBy.value)
  query.set('sort_order', sortOrder.value)
  query.set('page', String(targetPage))
  query.set('size', String(pageSize))
  return query.toString()
}

async function reloadMatrix(targetPage?: number, keepMessage = false) {
  if (targetPage && targetPage >= 1) page.value = targetPage
  if (!keepMessage) setMessage('')
  try {
    const res = await request(`${ENDPOINT}/matrix?${matrixQuery(page.value)}`)
    if (!res.ok) throw new Error('留存矩阵读取失败')
    const data = await res.json()
    matrixRows.value = data.items ?? []
    matrixPayload.value = {
      total: data.total ?? 0,
      page: data.page ?? 1,
      size: data.size ?? pageSize,
      periods: data.periods ?? [...allPeriods],
    }
    page.value = matrixPayload.value.page
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '留存矩阵读取失败', true)
  }
}

function resetFilters() {
  filters.value = { '群组名称': '', status: '' }
  selectedPeriods.value = []
  sortBy.value = 'cohort_id'
  sortOrder.value = 'asc'
  void reloadMatrix(1)
}

async function openDetail(cohortId: number) {
  const periods = selectedPeriods.value.length ? selectedPeriods.value.join(',') : ''
  const suffix = periods ? `?periods=${periods}` : ''
  const res = await request(`${ENDPOINT}/${cohortId}/matrix${suffix}`)
  detail.value = await res.json()
}

function openSupplement(cohortId: number) {
  supplementTarget.value = cohortId
  supplementForm.value = { count: 10, policy: 'fixed_snapshot', remark: '', residue: false }
}

async function submitSupplement() {
  if (!supplementTarget.value) return
  try {
    const res = await request(`${ENDPOINT}/${supplementTarget.value}/supplement`, {
      method: 'POST',
      body: JSON.stringify({
        count: supplementForm.value.count,
        policy: supplementForm.value.policy,
        remark: supplementForm.value.remark,
        residue: supplementForm.value.residue,
      }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      // 409 事务回滚：保留后端 detail 文案，矩阵不变
      setMessage(data.detail || '补录失败，事务已回滚', true)
    } else {
      setMessage(`补录已生成独立修订版本（${supplementForm.value.policy === 'fixed_snapshot' ? '固定快照，分母不变' : '最新快照已生效'}）`)
    }
    supplementTarget.value = null
    await reloadMatrix(undefined, true)
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '补录请求失败', true)
    supplementTarget.value = null
  }
}

async function recompute(cohortId: number) {
  try {
    const res = await request(`${ENDPOINT}/${cohortId}/recompute`, { method: 'POST' })
    const data = await res.json().catch(() => ({}))
    setMessage(res.ok ? data.message || '重算完成' : `重算失败，已沿用当前完整矩阵：${data.detail || ''}`, !res.ok)
    await reloadMatrix(undefined, true)
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '重算失败', true)
  }
}

async function exportMatrix() {
  const res = await request(`${ENDPOINT}/matrix/export?${matrixQuery(1)}`)
  if (!res.ok) {
    setMessage('矩阵导出失败', true)
    return
  }
  const url = URL.createObjectURL(await res.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = 'cohort_track_matrix.csv'
  link.click()
  URL.revokeObjectURL(url)
}

onMounted(() => {
  void fetchSummary()
  void reloadMatrix(1)
})
</script>

<style scoped>
.sortable { cursor: pointer; user-select: none; white-space: nowrap; }
.drawer-backdrop { position: fixed; inset: 0; background: rgb(15 23 42 / 35%); z-index: 30; display: flex; justify-content: flex-end; }
.drawer { width: min(720px, 92vw); height: 100%; background: #fff; padding: 20px; overflow: auto; box-shadow: -12px 0 40px rgb(15 23 42 / 20%); }
.drawer-head { display: flex; justify-content: space-between; align-items: center; }
.drawer h4 { margin: 16px 0 8px; }
.pager { display: flex; gap: 6px; }
.pager .btn:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
