<template>
  <div>
    <div class="toolbar">
      <h3>订阅分析</h3>
      <div class="left">
        <el-input v-model="itemId" placeholder="物品 ID" clearable style="width: 150px" />
        <el-input
          v-if="tab !== 'flips'"
          v-model="identityKey"
          placeholder="identity_key"
          clearable
          style="width: 220px"
        />
        <el-input
          v-if="tab === 'flips'"
          v-model="sellerFilter"
          placeholder="导狗昵称 / #编号"
          clearable
          style="width: 180px"
        />
        <el-checkbox v-if="tab === 'groups'" v-model="onlyMultiSeller">
          只看 ≥2 个卖家
        </el-checkbox>
        <el-button type="primary" @click="load">查询</el-button>
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <el-tabs v-model="tab" @tab-change="load">
        <el-tab-pane label="消失历史" name="disappeared">
          <el-table :data="rows" size="small" v-loading="loading">
            <el-table-column prop="item_name" label="商品" width="120" />
            <el-table-column prop="seller" label="卖家" min-width="170" />
            <el-table-column prop="price" label="价格" width="90" />
            <el-table-column prop="quality" label="品质" width="70" />
            <el-table-column label="身份" width="150">
              <template #default="{ row }">
                <span class="mono">{{ shortKey(row.identity_key) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="qq" label="QQ" width="110" />
            <el-table-column label="消失时间" width="160">
              <template #default="{ row }">{{ fmtTime(row.disappeared_at) }}</template>
            </el-table-column>
            <el-table-column label="明细" width="70">
              <template #default="{ row }">
                <el-button link type="primary" @click="showDisappeared(row)">查看</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="同一商品分组" name="groups">
          <el-table :data="rows" size="small" v-loading="loading">
            <el-table-column prop="item_name" label="商品" width="120" />
            <el-table-column label="identity_key" min-width="270">
              <template #default="{ row }">
                <span class="mono">{{ row.identity_key }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="listings" label="挂单数" width="90" />
            <el-table-column prop="appearances" label="出现" width="70" />
            <el-table-column prop="disappearances" label="消失" width="70" />
            <el-table-column label="最早出现" width="150">
              <template #default="{ row }">{{ fmtTime(row.first_seen_at) }}</template>
            </el-table-column>
            <el-table-column label="最近消失" width="150">
              <template #default="{ row }">{{ fmtTime(row.last_disappeared_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="190">
              <template #default="{ row }">
                <el-button link type="primary" @click="openItemDetail(row)">
                  查看商品数据
                </el-button>
                <el-button link type="primary" @click="openSellers(row)">查看卖家</el-button>
              </template>
            </el-table-column>
            <el-table-column prop="sellers" label="卖家数" width="80" />
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="导狗记录" name="flips">
          <el-table :data="rows" size="small" v-loading="loading">
            <el-table-column prop="item_name" label="商品" width="110" />
            <el-table-column prop="original_seller" label="原始卖家" min-width="150" />
            <el-table-column prop="prev_seller" label="上一手" min-width="150" />
            <el-table-column prop="reseller" label="导狗" min-width="150" />
            <el-table-column label="原卖家消失" width="150">
              <template #default="{ row }">{{ fmtTime(row.source_disappeared_at) }}</template>
            </el-table-column>
            <el-table-column label="导狗上架" width="150">
              <template #default="{ row }">
                {{ fmtTime(row.reseller_first_seen_at) }}
              </template>
            </el-table-column>
            <el-table-column label="间隔" width="90">
              <template #default="{ row }">{{ fmtGap(row.gap_sec) }}</template>
            </el-table-column>
            <el-table-column label="价格变化" width="150">
              <template #default="{ row }">
                <span>{{ fmtPrice(row.source_price) }} → {{ fmtPrice(row.reseller_price) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="差价" width="100">
              <template #default="{ row }">
                <span :class="deltaClass(row.source_price, row.reseller_price)">
                  {{ fmtDelta(row.source_price, row.reseller_price) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.reseller_disappeared_at" size="small" type="info">
                  已消失
                </el-tag>
                <el-tag v-else size="small" type="success">在售</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="明细" width="70">
              <template #default="{ row }">
                <el-button link type="primary" @click="showFlip(row)">查看</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 商品具体数据：方案 B 仪表卡 -->
    <el-dialog v-model="itemVisible" title="商品具体数据" width="980px">
      <div v-if="itemDetail" class="detail-card" v-loading="itemLoading">
        <div class="detail-head">
          <div class="detail-title">
            <h2>{{ itemDetail.item_name }}</h2>
            <span class="tag gold">品质 {{ itemDetail.quality ?? '-' }}</span>
            <span class="muted mono">ID {{ itemDetail.item_key }}</span>
          </div>
          <div class="detail-actions">
            <el-button size="small" @click="openSellers(itemDetail)">查看卖家</el-button>
            <el-button size="small" type="primary" @click="jumpHistory(itemDetail)">
              查消失历史
            </el-button>
          </div>
        </div>

        <div class="detail-split">
          <div class="detail-left">
            <div class="info-list">
              <div class="info">
                <span>商品身份</span>
                <span class="mono identity">{{ itemDetail.identity_key }}</span>
              </div>
              <div class="info"><span>卖家数</span><span>{{ itemDetail.sellers }} 人</span></div>
              <div class="info"><span>挂单数</span><span>{{ itemDetail.listings }} 次</span></div>
              <div class="info"><span>首次出现</span><span>{{ fmtTime(itemDetail.first_seen_at) }}</span></div>
              <div class="info"><span>最近消失</span><span>{{ fmtTime(itemDetail.last_disappeared_at) }}</span></div>
              <div class="info"><span>最近价格</span><span>{{ fmtPrice(recentPrice) }}</span></div>
            </div>
            <div class="chips">
              <span v-for="perk in itemDetail.subperks" :key="perk.name" class="chip">
                <b>{{ perk.name }}</b> · {{ perk.details.join(' / ') }}
              </span>
              <span v-if="!itemDetail.subperks.length" class="muted">无辅助词条</span>
            </div>
          </div>

          <div class="detail-right">
            <div class="ability-row">
              <div class="ability">
                <label>能力 1</label>
                <b>{{ itemDetail.abilities['0'] ?? '-' }}<em>点</em></b>
              </div>
              <div class="ability">
                <label>能力 2</label>
                <b>{{ itemDetail.abilities['1'] ?? '-' }}<em>点</em></b>
              </div>
              <div class="ability">
                <label>能力 3</label>
                <b>{{ itemDetail.abilities['2'] ?? '-' }}<em>点</em></b>
              </div>
            </div>

            <div class="spark">
              <div class="spark-head">
                <b>挂单价格走势</b>
                <span v-if="spark.count >= 2">
                  {{ fmtPrice(spark.first) }} → {{ fmtPrice(spark.last) }}
                  <span :class="spark.delta >= 0 ? 'delta-up' : 'delta-down'">
                    {{ spark.delta >= 0 ? '+' : '' }}{{ spark.delta.toFixed(1) }}%
                  </span>
                </span>
                <span v-else class="muted">数据不足</span>
              </div>
              <svg
                v-if="spark.count >= 2"
                width="100%"
                height="104"
                viewBox="0 0 520 104"
                preserveAspectRatio="none"
              >
                <defs>
                  <linearGradient id="sparkFade" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="#3b82f6" stop-opacity=".22" />
                    <stop offset="100%" stop-color="#3b82f6" stop-opacity="0" />
                  </linearGradient>
                </defs>
                <polyline
                  :points="spark.points"
                  fill="none"
                  stroke="#3b82f6"
                  stroke-width="2.4"
                  stroke-linejoin="round"
                />
                <polygon :points="spark.area" fill="url(#sparkFade)" />
                <circle
                  :cx="spark.endX"
                  :cy="spark.endY"
                  r="4"
                  fill="#fff"
                  stroke="#c0392b"
                  stroke-width="2"
                />
              </svg>
              <div class="muted spark-hint">按挂单价格绘制，横轴为挂单顺序</div>
            </div>
          </div>
        </div>

        <el-collapse class="raw">
          <el-collapse-item title="原始数据 JSON">
            <pre class="detail-pre">{{ pretty(itemDetail) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </div>
    </el-dialog>

    <!-- 卖家明细 -->
    <el-dialog v-model="sellerVisible" title="卖家明细" width="900px">
      <div class="dialog-head">
        <strong>{{ groupTitle }}</strong>
        <span class="muted mono">{{ groupKey }}</span>
      </div>
      <el-table :data="sellerRows" size="small">
        <el-table-column label="卖家" min-width="170">
          <template #default="{ row }">
            <span>{{ row.seller_name }}</span>
            <span class="muted"> #{{ row.seller_key }}</span>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.is_original" size="small" type="warning">原始卖家</el-tag>
            <el-tag v-else-if="row.flip_in_count" size="small" type="danger">导狗</el-tag>
            <el-tag v-else size="small" type="info">普通卖家</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="首次上架" width="150">
          <template #default="{ row }">{{ fmtTime(row.first_seen_at) }}</template>
        </el-table-column>
        <el-table-column label="最近上架" width="150">
          <template #default="{ row }">{{ fmtTime(row.last_seen_at) }}</template>
        </el-table-column>
        <el-table-column label="最近消失" width="150">
          <template #default="{ row }">{{ fmtTime(row.last_disappeared_at) }}</template>
        </el-table-column>
        <el-table-column prop="listing_count" label="挂单" width="70" />
        <el-table-column prop="flip_in_count" label="接手" width="70" />
        <el-table-column label="价格区间" width="140">
          <template #default="{ row }">
            <span>{{ fmtPrice(row.price_min) }} ~ {{ fmtPrice(row.price_max) }}</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="flip-title">该商品的导狗记录</div>
      <el-table :data="sellerFlips" size="small" empty-text="暂无已验证的倒卖记录">
        <el-table-column prop="original_seller" label="原始卖家" min-width="140" />
        <el-table-column prop="prev_seller" label="上一手" min-width="140" />
        <el-table-column prop="reseller" label="导狗" min-width="140" />
        <el-table-column label="原卖家消失" width="145">
          <template #default="{ row }">{{ fmtTime(row.source_disappeared_at) }}</template>
        </el-table-column>
        <el-table-column label="导狗上架" width="145">
          <template #default="{ row }">{{ fmtTime(row.reseller_first_seen_at) }}</template>
        </el-table-column>
        <el-table-column label="价格变化" width="140">
          <template #default="{ row }">
            {{ fmtPrice(row.source_price) }} → {{ fmtPrice(row.reseller_price) }}
          </template>
        </el-table-column>
        <el-table-column label="差价" width="100">
          <template #default="{ row }">
            <span :class="deltaClass(row.source_price, row.reseller_price)">
              {{ fmtDelta(row.source_price, row.reseller_price) }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 消失历史 / 导狗记录 明细 -->
    <el-dialog v-model="recordVisible" :title="recordTitle" width="760px">
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item
          v-for="entry in recordRows"
          :key="entry.label"
          :label="entry.label"
        >
          {{ entry.value }}
        </el-descriptions-item>
      </el-descriptions>

      <div v-if="recordAttrs" class="record-attrs">
        <div class="attr-line">
          <span class="muted">品质</span>
          <b>{{ recordAttrs.quality ?? '-' }}</b>
          <span class="muted">能力</span>
          <b>
            {{ recordAttrs.abilities['0'] ?? '-' }} /
            {{ recordAttrs.abilities['1'] ?? '-' }} /
            {{ recordAttrs.abilities['2'] ?? '-' }}
          </b>
        </div>
        <div class="chips">
          <span v-for="perk in recordAttrs.subperks" :key="perk.name" class="chip">
            <b>{{ perk.name }}</b> · {{ perk.details.join(' / ') }}
          </span>
        </div>
      </div>

      <div v-if="recordNote" class="record-note">{{ recordNote }}</div>

      <el-collapse class="raw">
        <el-collapse-item title="原始 JSON">
          <pre class="detail-pre">{{ recordRaw }}</pre>
        </el-collapse-item>
      </el-collapse>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorText } from '../api'
import { fmtTime } from '../utils'

const tab = ref('disappeared')
const rows = ref<any[]>([])
const loading = ref(false)
const itemId = ref('')
const identityKey = ref('')
const sellerFilter = ref('')
const onlyMultiSeller = ref(true)

const sellerVisible = ref(false)
const sellerRows = ref<any[]>([])
const sellerFlips = ref<any[]>([])
const groupTitle = ref('')
const groupKey = ref('')

const itemVisible = ref(false)
const itemLoading = ref(false)
const itemDetail = ref<any>(null)

const recordVisible = ref(false)
const recordTitle = ref('')
const recordRows = ref<{ label: string; value: string }[]>([])
const recordAttrs = ref<any>(null)
const recordNote = ref('')
const recordRaw = ref('')

function shortKey(value: string): string {
  if (!value) return '-'
  return value.length > 16 ? value.slice(0, 16) + '…' : value
}

function fmtPrice(value: any): string {
  if (value === null || value === undefined || value === '') return '-'
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function fmtDelta(source: any, target: any): string {
  if (source === null || source === undefined || target === null || target === undefined) {
    return '-'
  }
  const delta = Number(target) - Number(source)
  const sign = delta > 0 ? '+' : ''
  return `${sign}${delta.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
}

function deltaClass(source: any, target: any): string {
  if (source === null || source === undefined || target === null || target === undefined) {
    return 'muted'
  }
  if (Number(target) > Number(source)) return 'up'
  if (Number(target) < Number(source)) return 'down'
  return 'muted'
}

function fmtGap(seconds: any): string {
  const value = Number(seconds || 0)
  if (value <= 0) return '-'
  if (value < 3600) return `${Math.round(value / 60)} 分钟`
  if (value < 86400) return `${(value / 3600).toFixed(1)} 小时`
  return `${(value / 86400).toFixed(1)} 天`
}

function pretty(value: any): string {
  try {
    return typeof value === 'string'
      ? JSON.stringify(JSON.parse(value), null, 2)
      : JSON.stringify(value, null, 2)
  } catch {
    return String(value ?? '')
  }
}

function parseAttrs(text: any) {
  if (!text) return null
  try {
    const attr = typeof text === 'string' ? JSON.parse(text) : text
    if (!attr || typeof attr !== 'object') return null
    return {
      quality: attr.quality,
      abilities: attr.abilities || {},
      subperks: attr.subperks || [],
    }
  } catch {
    return null
  }
}

async function load() {
  loading.value = true
  try {
    const params: any = { limit: 200 }
    if (itemId.value) params.item_id = itemId.value
    let url = '/analytics/disappeared'
    if (tab.value === 'disappeared') {
      if (identityKey.value) params.identity_key = identityKey.value
    }
    if (tab.value === 'groups') {
      url = '/analytics/identity-groups'
      params.min_sellers = onlyMultiSeller.value ? 2 : 0
      if (identityKey.value) params.identity_key = identityKey.value
    }
    if (tab.value === 'flips') {
      url = '/analytics/flips'
      if (sellerFilter.value) params.seller = sellerFilter.value
    }
    const { data } = await api.get(url, { params })
    rows.value = data.items
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    loading.value = false
  }
}

async function loadItemDetail(item_id: string, key: string) {
  itemLoading.value = true
  try {
    const { data } = await api.get('/analytics/identity-detail', {
      params: { item_id, identity_key: key },
    })
    itemDetail.value = data.item
  } catch (err) {
    itemDetail.value = null
    ElMessage.error(errorText(err))
  } finally {
    itemLoading.value = false
  }
}

async function openItemDetail(row: any) {
  itemId.value = row.item_key || row.item_id || ''
  identityKey.value = row.identity_key
  itemDetail.value = null
  itemVisible.value = true
  await loadItemDetail(row.item_key || row.item_id, row.identity_key)
}

async function jumpHistory(row: any) {
  itemId.value = row.item_key || ''
  identityKey.value = row.identity_key
  itemVisible.value = false
  tab.value = 'disappeared'
  await load()
}

async function openSellers(row: any) {
  const item_id = row.item_key || row.item_id
  const key = row.identity_key
  groupTitle.value = row.item_name
  groupKey.value = key
  sellerVisible.value = true
  sellerRows.value = []
  sellerFlips.value = []
  try {
    const [sellersRes, flipsRes] = await Promise.all([
      api.get('/analytics/group-sellers', {
        params: { item_id, identity_key: key },
      }),
      api.get('/analytics/flips', {
        params: { item_id, identity_key: key, limit: 200 },
      }),
    ])
    sellerRows.value = sellersRes.data.items || []
    sellerFlips.value = flipsRes.data.items || []
  } catch (err) {
    ElMessage.error(errorText(err))
  }
}

function showDisappeared(row: any) {
  recordTitle.value = `消失明细 · ${row.item_name}`
  recordRows.value = [
    { label: '商品', value: row.item_name },
    { label: '卖家', value: row.seller || '-' },
    { label: '价格', value: fmtPrice(row.price) },
    { label: '品质', value: String(row.quality ?? '-') },
    { label: '身份', value: shortKey(row.identity_key) },
    { label: 'QQ', value: String(row.qq ?? '-') },
    { label: '首次出现', value: fmtTime(row.first_seen_at) },
    { label: '最近出现', value: fmtTime(row.last_seen_at) },
    { label: '消失时间', value: fmtTime(row.disappeared_at) },
  ]
  recordAttrs.value = parseAttrs(row.identity_json)
  recordNote.value = ''
  recordRaw.value = pretty(row)
  recordVisible.value = true
}

function showFlip(row: any) {
  recordTitle.value = `导狗记录 · ${row.item_name}`
  recordRows.value = [
    { label: '商品', value: row.item_name },
    { label: '原始卖家', value: row.original_seller || '-' },
    { label: '上一手', value: row.prev_seller || '-' },
    { label: '导狗', value: row.reseller || '-' },
    { label: '原卖家消失', value: fmtTime(row.source_disappeared_at) },
    { label: '导狗上架', value: fmtTime(row.reseller_first_seen_at) },
    { label: '间隔', value: fmtGap(row.gap_sec) },
    { label: '价格变化', value: `${fmtPrice(row.source_price)} → ${fmtPrice(row.reseller_price)}` },
    { label: '差价', value: fmtDelta(row.source_price, row.reseller_price) },
    { label: '当前状态', value: row.reseller_disappeared_at ? '已消失' : '在售' },
  ]
  recordAttrs.value = parseAttrs(row.identity_json)
  recordNote.value = `判定依据：原始卖家 ${row.original_seller} 有该商品的消失记录，导狗 ${row.reseller} 在 ${fmtTime(row.reseller_first_seen_at)} 上架，晚于最后一次消失 ${fmtTime(row.source_disappeared_at)}。`
  recordRaw.value = pretty(row)
  recordVisible.value = true
}

const recentPrice = computed(() => {
  const history = itemDetail.value?.history || []
  for (let i = history.length - 1; i >= 0; i -= 1) {
    const value = history[i]?.price_last
    if (value !== null && value !== undefined) return value
  }
  return null
})

const spark = computed(() => {
  const history = itemDetail.value?.history || []
  const prices = history
    .map((row: any) => Number(row.price_last))
    .filter((value: number) => Number.isFinite(value) && value > 0)
  const count = prices.length
  if (count < 2) {
    return { count, points: '', area: '', first: 0, last: 0, delta: 0, endX: 0, endY: 0 }
  }
  const low = Math.min(...prices)
  const high = Math.max(...prices)
  const span = high - low || 1
  const step = count > 1 ? 500 / (count - 1) : 0
  const points = prices.map((value: number, index: number) => {
    const x = 10 + index * step
    const y = high === low ? 50 : 14 + ((high - value) / span) * 72
    return [x, y]
  })
  const first = prices[0]
  const last = prices[count - 1]
  const delta = first ? ((last - first) / first) * 100 : 0
  const [endX, endY] = points[points.length - 1]
  return {
    count,
    points: points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' '),
    area: `${points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ')} 512,96 10,96`,
    first,
    last,
    delta,
    endX,
    endY,
  }
})

onMounted(load)
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.toolbar h3 {
  margin: 0;
  color: #1f2a3a;
}
.left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.muted {
  color: #9aa4b5;
  font-size: 12px;
}
.mono {
  font-family: Consolas, monospace;
  font-size: 11px;
  color: #5b6678;
}
.up {
  color: #c0392b;
  font-weight: 600;
}
.down {
  color: #1f8a4c;
  font-weight: 600;
}
.dialog-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 10px;
}
.flip-title {
  margin: 16px 0 8px;
  font-weight: 600;
  color: #1f2a3a;
}

/* 商品详情（方案 B） */
.detail-card {
  border-radius: 12px;
  padding: 4px 2px;
}
.detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.detail-title {
  display: flex;
  align-items: center;
  gap: 10px;
}
.detail-title h2 {
  margin: 0;
  font-size: 20px;
}
.tag {
  border-radius: 6px;
  font-size: 11px;
  padding: 3px 8px;
  background: #eef4ff;
  color: #2f6fe0;
  border: 1px solid #d8e6ff;
}
.tag.gold {
  background: #fff8e6;
  color: #a67c00;
  border-color: #f3e2b3;
}
.detail-actions {
  display: flex;
  gap: 8px;
}
.detail-split {
  display: grid;
  grid-template-columns: 360px 1fr;
  gap: 22px;
  margin-top: 16px;
}
.info-list {
  display: grid;
  gap: 8px;
}
.info {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid #f1f4f9;
  padding: 7px 0;
  font-size: 12px;
  color: #5b6678;
}
.info span:last-child {
  color: #1f2a3a;
  font-weight: 600;
}
.identity {
  font-size: 10px !important;
  word-break: break-all;
}
.ability-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}
.ability {
  border-radius: 12px;
  padding: 12px;
  background: #f6f9ff;
  border: 1px solid #e3ecfb;
}
.ability:nth-child(2) {
  background: #fffaf2;
  border-color: #f6e8cb;
}
.ability:nth-child(3) {
  background: #f4fbf7;
  border-color: #d8eee3;
}
.ability label {
  font-size: 11px;
  color: #7a8597;
}
.ability b {
  display: block;
  font-size: 26px;
  line-height: 1.3;
  color: #2f6fe0;
}
.ability:nth-child(2) b {
  color: #b07b12;
}
.ability:nth-child(3) b {
  color: #1f8a4c;
}
.ability em {
  font-style: normal;
  font-size: 11px;
  color: #9aa4b5;
  margin-left: 4px;
}
.spark {
  margin-top: 14px;
  border: 1px solid #eef2f8;
  border-radius: 12px;
  padding: 10px 12px 6px;
}
.spark-head {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  margin-bottom: 2px;
}
.spark-head b {
  color: #1f2a3a;
}
.delta-up {
  color: #c0392b;
  font-weight: 600;
}
.delta-down {
  color: #1f8a4c;
  font-weight: 600;
}
.spark-hint {
  font-size: 11px;
  padding-bottom: 4px;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}
.chip {
  border-radius: 20px;
  padding: 4px 10px;
  font-size: 11px;
  background: #f4f7fc;
  border: 1px solid #e3e9f2;
  color: #4b5a70;
}
.chip b {
  color: #1f2a3a;
  font-weight: 600;
}
.raw {
  margin-top: 14px;
}
.detail-pre {
  max-height: 40vh;
  overflow: auto;
  background: #f6f8fb;
  padding: 12px;
  border-radius: 8px;
  font-size: 12px;
}
.record-attrs {
  margin-top: 14px;
  padding: 10px 12px;
  background: #f8fafd;
  border-radius: 10px;
}
.attr-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #5b6678;
}
.attr-line b {
  color: #1f2a3a;
  margin-right: 10px;
}
.record-note {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  background: #fffaf2;
  border: 1px solid #f3e6c8;
  font-size: 12px;
  color: #8a6a12;
}
</style>
