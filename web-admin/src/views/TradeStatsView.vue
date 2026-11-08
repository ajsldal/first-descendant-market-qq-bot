<template>
  <div>
    <div class="toolbar">
      <div class="left">
        <h3>成交统计</h3>
        <span class="hint">
          {{
            status.enabled
              ? `自动抓取：每 ${status.interval_hours} 小时`
              : '自动抓取已关闭'
          }}
          <template v-if="status.last_run?.finished_at">
            · 上次 {{ fmtTime(status.last_run.finished_at) }}
          </template>
        </span>
      </div>
      <div class="right">
        <el-button :loading="loading" @click="reload">刷新</el-button>
        <el-button type="primary" :loading="refreshing" @click="refreshAll">立即抓取</el-button>
      </div>
    </div>

    <el-row :gutter="16">
      <el-col :span="6">
        <el-card shadow="never" class="list-card" v-loading="loading">
          <template #header>
            <div class="list-head">
              <span>超越武器</span>
              <el-tag size="small" type="info">{{ filtered.length }} / {{ items.length }}</el-tag>
            </div>
          </template>
          <el-input
            v-model="keyword"
            class="search"
            size="small"
            placeholder="搜索名称或 ID"
            clearable
          />
          <el-scrollbar class="list-scroll">
            <div
              v-for="item in filtered"
              :key="item.item_id"
              class="item"
              :class="{ active: item.item_id === current }"
              @click="select(item.item_id)"
            >
              <div class="item-top">
                <span class="name">{{ item.item_name || item.item_id }}</span>
                <el-tag v-if="item.last_error" size="small" type="danger">失败</el-tag>
                <el-tag v-else size="small" type="success">{{ item.point_count }} 点</el-tag>
              </div>
              <div class="item-meta">
                <span>24h {{ item.trades_24h || 0 }} 笔</span>
                <span v-if="item.avg_price_24h">均价 {{ fmtPrice(item.avg_price_24h) }}</span>
              </div>
              <div class="item-id">ID {{ item.item_id }}</div>
            </div>
            <el-empty
              v-if="!filtered.length"
              :description="items.length ? '没有匹配的武器' : '还没有统计数据'"
              :image-size="72"
            />
          </el-scrollbar>
        </el-card>
      </el-col>

      <el-col :span="18">
        <el-card shadow="never" v-loading="detailLoading">
          <template #header>
            <div class="chart-head">
              <div class="chart-title">
                <strong>{{ detail?.item_name || '请选择超越武器' }}</strong>
                <span v-if="detail" class="muted">ID {{ detail.item_id }}</span>
                <span v-if="detail?.points?.length" class="muted">
                  {{ detail.first_date }} ~ {{ detail.last_date }}
                </span>
              </div>
              <div class="chart-right">
                <span v-if="detail" class="muted">
                  {{ detail.last_error ? '最近抓取失败' : '数据更新' }}
                  {{ fmtTime(detail.last_crawled_at) }}
                </span>
                <el-tag size="small" type="info">近 2 天 · 按小时</el-tag>
              </div>
            </div>
          </template>

          <template v-if="detail?.points?.length">
            <TradeStatsChart :points="detail.points" height="440px" />
            <div class="summary">
              <div class="stat">
                <label>成交笔数</label>
                <span>{{ totalTrades }}</span>
              </div>
              <div class="stat">
                <label>加权均价</label>
                <span>{{ weightedPrice }}</span>
              </div>
              <div class="stat">
                <label>价格区间</label>
                <span>{{ priceRange }}</span>
              </div>
              <div class="stat">
                <label>峰值小时</label>
                <span>{{ peakText }}</span>
              </div>
            </div>
          </template>
          <el-empty
            v-else
            description="该武器暂无成交记录，可点击「立即抓取」或等待定时任务"
          />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorText } from '../api'
import { fmtTime } from '../utils'
import TradeStatsChart from '../components/TradeStatsChart.vue'

interface StatPoint {
  date: string
  average_price: number
  trade_count: number
}

interface StatsItem {
  item_id: string
  item_name: string
  point_count: number
  trades_24h: number | null
  avg_price_24h: number | null
  last_bucket_at: number | null
  last_crawled_at: number | null
  last_error: string
}

interface StatsDetail extends StatsItem {
  points: StatPoint[]
  first_date: string
  last_date: string
}

const loading = ref(false)
const detailLoading = ref(false)
const refreshing = ref(false)
const items = ref<StatsItem[]>([])
const detail = ref<StatsDetail | null>(null)
const current = ref('')
const keyword = ref('')
const status = ref<{ enabled: boolean; interval_hours: number; last_run: any }>({
  enabled: true,
  interval_hours: 12,
  last_run: null,
})

const filtered = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  if (!text) return items.value
  return items.value.filter(
    (item) =>
      item.item_name.toLowerCase().includes(text) ||
      item.item_id.toLowerCase().includes(text),
  )
})

function fmtPrice(value: number): string {
  return Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

const totalTrades = computed(() =>
  detail.value?.points.reduce((sum, point) => sum + point.trade_count, 0) || 0,
)

const weightedPrice = computed(() => {
  const points = detail.value?.points || []
  const count = points.reduce((sum, point) => sum + point.trade_count, 0)
  if (!count) return '-'
  const turnover = points.reduce(
    (sum, point) => sum + point.average_price * point.trade_count,
    0,
  )
  return fmtPrice(turnover / count)
})

const priceRange = computed(() => {
  const points = detail.value?.points || []
  if (!points.length) return '-'
  const prices = points.map((point) => point.average_price)
  const low = Math.min(...prices)
  const high = Math.max(...prices)
  return low === high ? fmtPrice(low) : `${fmtPrice(low)} ~ ${fmtPrice(high)}`
})

const peakText = computed(() => {
  const points = detail.value?.points || []
  if (!points.length) return '-'
  const peak = points.reduce((best, point) =>
    point.trade_count > best.trade_count ? point : best,
  )
  return `${peak.date} · ${peak.trade_count} 笔`
})

async function reload() {
  loading.value = true
  try {
    const [listRes, statusRes] = await Promise.all([
      api.get('/trade-stats/items'),
      api.get('/trade-stats/status'),
    ])
    items.value = listRes.data.items || []
    status.value = statusRes.data || status.value
    if (!current.value && items.value.length) {
      current.value = items.value[0].item_id
    }
    const exists = items.value.some((item) => item.item_id === current.value)
    if (!exists && items.value.length) {
      current.value = items.value[0].item_id
    }
    if (current.value) await loadDetail(current.value)
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    loading.value = false
  }
}

async function loadDetail(itemId: string) {
  detailLoading.value = true
  try {
    const { data } = await api.get(`/trade-stats/${itemId}`)
    detail.value = data.item
  } catch (err) {
    detail.value = null
    ElMessage.error(errorText(err))
  } finally {
    detailLoading.value = false
  }
}

function select(itemId: string) {
  if (itemId === current.value) return
  current.value = itemId
  loadDetail(itemId)
}

async function refreshAll() {
  refreshing.value = true
  try {
    const { data } = await api.post('/trade-stats/refresh', null, { timeout: 180000 })
    const summary = data.summary || {}
    ElMessage.success(
      `抓取完成：${summary.items ?? 0} 件，新增 ${summary.inserted ?? 0} 个时间点，失败 ${summary.failed ?? 0}`,
    )
    await reload()
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    refreshing.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.left {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.left h3 {
  margin: 0;
  color: #1f2a3a;
}
.hint {
  color: #9aa4b5;
  font-size: 12px;
}
.right {
  display: flex;
  gap: 8px;
}
.list-card {
  border-radius: 10px;
}
.list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.search {
  margin-bottom: 10px;
}
.list-scroll {
  height: calc(100vh - 268px);
}
.item {
  padding: 10px 12px;
  margin-bottom: 8px;
  border: 1px solid #e8ebf0;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s;
  background: #fbfcfe;
}
.item:hover {
  border-color: #c8d3e3;
  background: #f4f8ff;
}
.item.active {
  border-color: #3b82f6;
  background: #eff6ff;
}
.item-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.item-top .name {
  font-size: 13px;
  font-weight: 600;
  color: #1f2a3a;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.item-meta {
  display: flex;
  gap: 10px;
  margin-top: 4px;
  font-size: 12px;
  color: #5b6678;
}
.item-id {
  margin-top: 2px;
  font-size: 11px;
  color: #a5aebd;
}
.chart-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.chart-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}
.chart-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.muted {
  color: #8b93a3;
  font-size: 12px;
}
.summary {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-top: 8px;
  padding-top: 14px;
  border-top: 1px solid #eef1f5;
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.stat label {
  color: #8b93a3;
  font-size: 12px;
}
.stat span {
  color: #1f2a3a;
  font-size: 15px;
  font-weight: 600;
}
</style>
