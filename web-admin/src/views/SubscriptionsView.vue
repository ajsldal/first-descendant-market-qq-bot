<template>
  <div>
    <div class="toolbar">
      <div class="left">
        <el-input v-model="filters.qq" placeholder="QQ" clearable style="width: 140px" />
        <el-input v-model="filters.query" placeholder="物品名 / 条件关键词" clearable style="width: 220px" />
        <el-select v-model="filters.enabled" style="width: 130px">
          <el-option label="全部状态" value="" />
          <el-option label="运行中" value="1" />
          <el-option label="已暂停" value="0" />
        </el-select>
        <el-button type="primary" @click="load">查询</el-button>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never">
      <el-table :data="rows" size="small" v-loading="loading">
        <el-table-column prop="id" label="#" width="60" />
        <el-table-column prop="qq" label="QQ" width="130" />
        <el-table-column label="商品" width="130">
          <template #default="{ row }">{{ itemName(row) }}</template>
        </el-table-column>
        <el-table-column label="筛选条件" min-width="240">
          <template #default="{ row }">
            <span class="muted">{{ specBrief(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="间隔" width="90">
          <template #default="{ row }">{{ formatInterval(row.interval_sec) }}</template>
        </el-table-column>
        <el-table-column label="下次执行" width="160">
          <template #default="{ row }">{{ fmtTime(row.next_run_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.enabled ? 'success' : 'info'">
              {{ row.enabled ? '运行中' : '已暂停' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Bark" width="150">
          <template #default="{ row }">
            <el-switch
              :model-value="!!row.bark_enabled"
              size="small"
              @change="(value: boolean) => toggleBark(row, value)"
            />
            <span class="bark-sound">{{ row.bark_sound || '继承' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="上次命中" width="90">
          <template #default="{ row }">{{ row.last_count ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="失败" width="70">
          <template #default="{ row }">{{ row.fail_count }}</template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="edit(row)">编辑</el-button>
            <el-button v-if="row.enabled" link type="warning" @click="pause(row)">暂停</el-button>
            <el-button v-else link type="success" @click="resume(row)">恢复</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="`编辑订阅 #${editing?.id ?? ''}`" width="480px">
      <el-form label-width="110px">
        <el-form-item label="间隔（分钟）">
          <el-input-number v-model="form.interval_min" :min="1" :max="1440" />
          <span class="hint">最小 1 分钟；建议只给开了手机推送的订阅设 1 分钟</span>
        </el-form-item>
        <el-form-item label="最多条数">
          <el-input-number v-model="form.max_push" :min="1" :max="50" />
        </el-form-item>
        <el-form-item label="仅推新">
          <el-switch v-model="form.only_new" />
        </el-form-item>
        <el-form-item label="首轮推送">
          <el-switch v-model="form.first_push" />
        </el-form-item>
        <el-form-item label="静默时段">
          <el-input v-model="form.quiet_hours" placeholder="如 23:00-08:00，留空关闭" />
        </el-form-item>
        <el-form-item label="手机推送">
          <el-switch v-model="form.bark_enabled" />
          <span class="hint">命中商品时推送到该用户的 Bark</span>
        </el-form-item>
        <el-form-item label="推送铃声">
          <el-input v-model="form.bark_sound" placeholder="留空继承用户默认，例如 alarm 或 minuet" />
        </el-form-item>
        <el-form-item label="模糊过滤（倒卖）">
          <el-input
            v-model="form.flip_overrides"
            type="textarea"
            :rows="2"
            placeholder="留空用默认：品质照抄、辅助忽略、能力取一条。示例：价格=100-8000 能力2=150-150"
          />
          <div class="hint">仅对开启手机推送的订阅生效；命中 1~3 条时对比模糊结果计算倒卖利润</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errorText } from '../api'
import { fmtTime } from '../utils'

const rows = ref<any[]>([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editing = ref<any>(null)
const filters = reactive({ qq: '', query: '', enabled: '' })
const form = reactive<any>({
  interval_min: 10,
  max_push: 20,
  only_new: false,
  first_push: true,
  quiet_hours: '',
  bark_enabled: false,
  bark_sound: '',
  flip_overrides: '',
})

function itemName(row: any): string {
  try {
    return JSON.parse(row.spec_json)?.item?.name || '-'
  } catch {
    return '-'
  }
}

function specBrief(row: any): string {
  try {
    const spec = JSON.parse(row.spec_json)
    const parts: string[] = []
    if (spec.price) parts.push(`价格 ${spec.price.min ?? ''}-${spec.price.max ?? ''}`)
    if (spec.quality) parts.push(`品质 ${spec.quality.min ?? ''}-${spec.quality.max ?? ''}`)
    if (spec.grade) parts.push(`品级 ${spec.grade}`)
    if (spec.sort && spec.sort !== 'latest') parts.push(`排序 ${spec.sort}`)
    return parts.length ? parts.join(' | ') : '无附加筛选'
  } catch {
    return '-'
  }
}

function formatInterval(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`
  return `${Math.round(seconds / 60)}m`
}

async function load() {
  loading.value = true
  try {
    const params: any = {
      qq: filters.qq || undefined,
      query: filters.query || undefined,
      enabled: filters.enabled === '' ? undefined : Number(filters.enabled),
      limit: 200,
    }
    const { data } = await api.get('/subscriptions', { params })
    rows.value = data.items
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    loading.value = false
  }
}

function edit(row: any) {
  editing.value = row
  form.interval_min = Math.max(5, Math.round(row.interval_sec / 60))
  form.max_push = row.max_push
  form.only_new = !!row.only_new
  form.first_push = !!row.first_push
  form.quiet_hours = row.quiet_hours || ''
  form.bark_enabled = !!row.bark_enabled
  form.bark_sound = row.bark_sound || ''
  form.flip_overrides = row.flip_overrides_text || ''
  dialogVisible.value = true
}

async function save() {
  if (!editing.value) return
  saving.value = true
  try {
    await api.patch(`/subscriptions/${editing.value.id}`, {
      interval_sec: form.interval_min * 60,
      max_push: form.max_push,
      only_new: form.only_new,
      first_push: form.first_push,
      quiet_hours: form.quiet_hours,
      bark_enabled: form.bark_enabled,
      bark_sound: form.bark_sound,
      flip_overrides: form.flip_overrides,
    })
    ElMessage.success('订阅已更新')
    dialogVisible.value = false
    await load()
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    saving.value = false
  }
}

async function toggleBark(row: any, enabled: boolean) {
  try {
    await api.patch(`/subscriptions/${row.id}`, { bark_enabled: enabled })
    row.bark_enabled = enabled ? 1 : 0
    ElMessage.success(enabled ? '已开启手机推送' : '已关闭手机推送')
  } catch (err) {
    ElMessage.error(errorText(err))
    await load()
  }
}

async function pause(row: any) {
  await api.post(`/subscriptions/${row.id}/pause`)
  ElMessage.success('已暂停')
  await load()
}

async function resume(row: any) {
  await api.post(`/subscriptions/${row.id}/resume`)
  ElMessage.success('已恢复')
  await load()
}

async function remove(row: any) {
  await ElMessageBox.confirm(`删除订阅 #${row.id}？此操作不可恢复。`, '确认删除', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消',
  })
  await api.delete(`/subscriptions/${row.id}`)
  ElMessage.success('已删除')
  await load()
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
.left {
  display: flex;
  gap: 8px;
}
.muted {
  color: #7a8597;
  font-size: 12px;
}
.hint {
  margin-left: 10px;
  color: #9aa4b5;
  font-size: 12px;
}
.bark-sound {
  margin-left: 8px;
  color: #7a8597;
  font-size: 12px;
}
</style>
