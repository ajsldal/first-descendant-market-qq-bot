<template>
  <div>
    <div class="toolbar">
      <h3>运行概览</h3>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-row :gutter="14" class="cards">
      <el-col v-for="card in cards" :key="card.label" :span="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-label">{{ card.label }}</div>
          <div class="stat-value">{{ card.value }}</div>
          <div class="stat-hint">{{ card.hint }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="14">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header>最近审计</template>
          <el-table :data="recentAudit" size="small">
            <el-table-column prop="action" label="动作" width="180" />
            <el-table-column prop="username" label="管理员" width="120" />
            <el-table-column prop="target_id" label="目标" />
            <el-table-column label="时间" width="180">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>数据库</template>
          <div class="db-row">
            <span>bot 数据库</span>
            <b>{{ fmtSize(data?.dbs?.bot?.size) }}</b>
          </div>
          <div class="db-row">
            <span>bot WAL</span>
            <b>{{ fmtSize(data?.dbs?.bot?.wal) }}</b>
          </div>
          <div class="db-row">
            <span>admin 数据库</span>
            <b>{{ fmtSize(data?.dbs?.admin?.size) }}</b>
          </div>
          <el-divider />
          <div class="db-row">
            <span>最近抓取</span>
            <b>{{ fmtTime(stats.last_crawl_at) }}</b>
          </div>
          <div class="db-row">
            <span>最近订阅执行</span>
            <b>{{ fmtTime(stats.last_run_at) }}</b>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorText } from '../api'
import { fmtSize, fmtTime } from '../utils'

const loading = ref(false)
const data = ref<any>({})
const stats = computed(() => data.value?.stats || {})
const recentAudit = computed(() => data.value?.recent_audit || [])

const cards = computed(() => [
  { label: '用户', value: stats.value.users ?? 0, hint: `封禁 ${stats.value.blocked_users ?? 0}` },
  { label: '订阅', value: stats.value.subscriptions ?? 0, hint: `运行中 ${stats.value.active_subscriptions ?? 0}` },
  { label: '消失记录', value: stats.value.disappeared ?? 0, hint: `导狗 ${stats.value.flips ?? 0}` },
  { label: '卖家', value: stats.value.sellers ?? 0, hint: '剔重后的卖家编号' },
])

async function load() {
  loading.value = true
  try {
    const response = await api.get('/dashboard')
    data.value = response.data
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    loading.value = false
  }
}

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
.cards {
  margin-bottom: 14px;
}
.stat-card {
  border-radius: 10px;
}
.stat-label {
  color: #7a8597;
  font-size: 13px;
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
  color: #1f2a3a;
  margin: 6px 0 4px;
}
.stat-hint {
  color: #9aa4b5;
  font-size: 12px;
}
.db-row {
  display: flex;
  justify-content: space-between;
  padding: 8px 0;
  color: #5b6678;
}
</style>
