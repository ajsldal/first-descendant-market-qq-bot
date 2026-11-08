<template>
  <div>
    <div class="toolbar">
      <h3>审计日志</h3>
      <div class="left">
        <el-input v-model="action" placeholder="按动作筛选" clearable style="width: 220px" />
        <el-button type="primary" @click="load">查询</el-button>
        <el-button :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <el-table :data="rows" size="small" v-loading="loading">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column prop="username" label="管理员" width="120" />
        <el-table-column prop="action" label="动作" width="200" />
        <el-table-column prop="target_type" label="类型" width="110" />
        <el-table-column prop="target_id" label="目标" width="140" />
        <el-table-column label="修改前" min-width="180">
          <template #default="{ row }">
            <span class="muted">{{ shortJson(row.before) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="修改后" min-width="180">
          <template #default="{ row }">
            <span class="muted">{{ shortJson(row.after) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="IP" width="130">
          <template #default="{ row }">{{ row.ip || '-' }}</template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="详情" width="80">
          <template #default="{ row }">
            <el-button link type="primary" @click="showDetail(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="detailVisible" title="审计详情" width="720px">
      <pre class="detail">{{ detailText }}</pre>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorText } from '../api'
import { fmtTime, shortJson } from '../utils'

const rows = ref<any[]>([])
const loading = ref(false)
const action = ref('')
const detailVisible = ref(false)
const detailText = ref('')

async function load() {
  loading.value = true
  try {
    const { data } = await api.get('/audit', {
      params: { limit: 200, action: action.value || undefined },
    })
    rows.value = data.items
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    loading.value = false
  }
}

function showDetail(row: any) {
  detailText.value = JSON.stringify(
    {
      id: row.id,
      admin: row.username,
      action: row.action,
      target: `${row.target_type}#${row.target_id}`,
      ip: row.ip,
      time: fmtTime(row.created_at),
      before: row.before,
      after: row.after,
    },
    null,
    2,
  )
  detailVisible.value = true
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
.left {
  display: flex;
  gap: 8px;
}
.muted {
  color: #7a8597;
  font-size: 12px;
}
.detail {
  max-height: 60vh;
  overflow: auto;
  background: #f6f8fb;
  padding: 14px;
  border-radius: 8px;
  font-size: 12px;
}
</style>
