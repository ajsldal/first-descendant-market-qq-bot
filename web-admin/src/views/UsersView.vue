<template>
  <div>
    <div class="toolbar">
      <div class="left">
        <el-input
          v-model="query"
          placeholder="按 QQ 搜索"
          clearable
          style="width: 220px"
          @keyup.enter="load"
        />
        <el-button type="primary" @click="load">搜索</el-button>
      </div>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never">
      <el-table :data="rows" size="small" v-loading="loading">
        <el-table-column prop="qq" label="QQ" width="130" />
        <el-table-column label="订阅" width="100">
          <template #default="{ row }">
            {{ row.sub_count }}（{{ row.active_sub_count }} 运行）
          </template>
        </el-table-column>
        <el-table-column label="登录态" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.token_status" size="small" :type="row.token_status === 'valid' ? 'success' : 'danger'">
              {{ row.token_status }}
            </el-tag>
            <span v-else class="muted">未登录</span>
          </template>
        </el-table-column>
        <el-table-column label="权限" min-width="220">
          <template #default="{ row }">
            <el-tag v-if="row.allow_subscribe" size="small" type="success">订阅</el-tag>
            <el-tag v-else size="small" type="info">禁订阅</el-tag>
            <el-tag v-if="row.allow_login" size="small" type="success">登录</el-tag>
            <el-tag v-else size="small" type="info">禁登录</el-tag>
            <el-tag v-if="row.allow_trade" size="small" type="success">交易</el-tag>
            <el-tag v-else size="small" type="info">禁交易</el-tag>
            <el-tag v-if="row.allow_price_query" size="small" type="success">查价</el-tag>
            <el-tag v-else size="small" type="info">禁查价</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="订阅配额" width="100">
          <template #default="{ row }">
            {{ row.max_subscriptions ?? '全局' }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.blocked" type="danger" size="small">已封禁</el-tag>
            <el-tag v-else type="success" size="small">正常</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="手机推送" width="110">
          <template #default="{ row }">
            <el-tag
              v-if="row.bark_configured"
              size="small"
              :type="row.bark_enabled ? 'success' : 'info'"
            >
              {{ row.bark_enabled ? '已开启' : '已关闭' }}
            </el-tag>
            <span v-else class="muted">未配置</span>
          </template>
        </el-table-column>
        <el-table-column prop="note" label="备注" min-width="120" />
        <el-table-column label="操作" width="300" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="edit(row)">权限</el-button>
            <el-button link type="success" @click="openBark(row)">手机推送</el-button>
            <el-button v-if="!row.blocked" link type="danger" @click="block(row)">封禁</el-button>
            <el-button v-else link type="warning" @click="unblock(row)">解封</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="`用户 ${editing?.qq ?? ''} 权限`" width="520px">
      <el-form label-width="120px">
        <el-form-item label="订阅配额">
          <el-input-number v-model="form.max_subscriptions" :min="1" :max="200" />
          <span class="hint">留空表示使用全局配额</span>
        </el-form-item>
        <el-form-item label="允许创建订阅">
          <el-switch v-model="form.allow_subscribe" />
        </el-form-item>
        <el-form-item label="允许登录">
          <el-switch v-model="form.allow_login" />
        </el-form-item>
        <el-form-item label="允许交易">
          <el-switch v-model="form.allow_trade" />
        </el-form-item>
        <el-form-item label="允许查价">
          <el-switch v-model="form.allow_price_query" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.note" type="textarea" :rows="2" maxlength="500" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="warning" @click="resetPermissions">恢复默认</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="barkVisible" :title="`用户 ${barkQq} 的手机推送（Bark）`" width="560px">
      <el-alert
        v-if="barkStatus.last_error"
        :title="barkStatus.last_error"
        type="error"
        :closable="false"
        class="mb"
      />
      <div class="bark-meta">
        当前：{{ barkStatus.configured ? `${barkStatus.key_tail}（${barkStatus.server}）` : '未配置' }}
        <span v-if="barkStatus.last_sent_at"> · 最近推送 {{ fmtTime(barkStatus.last_sent_at) }}</span>
      </div>
      <el-form label-width="130px">
        <el-form-item label="Bark 地址 / Key">
          <el-input
            v-model="barkForm.target"
            placeholder="https://api.day.app/你的Key，留空表示不改 Key"
          />
        </el-form-item>
        <el-form-item label="默认铃声">
          <el-input v-model="barkForm.sound" placeholder="例如 alarm 或 minuet，留空保持不变" />
          <div class="hint">直接填铃声名，例如 alarm / minuet，不要加后缀</div>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="barkForm.enabled" :disabled="!barkStatus.configured" />
          <span class="hint">关闭后该用户所有订阅都不会推送手机</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="barkVisible = false">取消</el-button>
        <el-button type="danger" plain :disabled="!barkStatus.configured" @click="clearBark">
          清空
        </el-button>
        <el-button :disabled="!barkStatus.configured" :loading="barkTesting" @click="testBark">
          发送测试
        </el-button>
        <el-button :disabled="!barkStatus.configured" :loading="barkFlooding" @click="testFlood">
          循环轰炸
        </el-button>
        <el-button type="primary" :loading="barkSaving" @click="saveBark">保存</el-button>
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
const query = ref('')
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editing = ref<any>(null)
const barkVisible = ref(false)
const barkQq = ref(0)
const barkSaving = ref(false)
const barkTesting = ref(false)
const barkFlooding = ref(false)
const barkStatus = ref<any>({
  configured: false,
  enabled: false,
  server: '',
  sound: '',
  key_tail: '',
})
const barkForm = reactive({ target: '', sound: '', enabled: false })

const form = reactive<any>({
  max_subscriptions: null,
  allow_subscribe: true,
  allow_login: true,
  allow_trade: true,
  allow_price_query: true,
  note: '',
})

async function load() {
  loading.value = true
  try {
    const { data } = await api.get('/users', { params: { query: query.value, limit: 200 } })
    rows.value = data.items
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    loading.value = false
  }
}

function edit(row: any) {
  editing.value = row
  form.max_subscriptions = row.max_subscriptions ?? null
  form.allow_subscribe = !!row.allow_subscribe
  form.allow_login = !!row.allow_login
  form.allow_trade = !!row.allow_trade
  form.allow_price_query = !!row.allow_price_query
  form.note = row.note || ''
  dialogVisible.value = true
}

async function save() {
  if (!editing.value) return
  saving.value = true
  try {
    await api.put(`/users/${editing.value.qq}/permissions`, {
      max_subscriptions: form.max_subscriptions || null,
      allow_subscribe: form.allow_subscribe,
      allow_login: form.allow_login,
      allow_trade: form.allow_trade,
      allow_price_query: form.allow_price_query,
      note: form.note,
    })
    ElMessage.success('已保存，最多 30 秒生效')
    dialogVisible.value = false
    await load()
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    saving.value = false
  }
}

async function resetPermissions() {
  if (!editing.value) return
  await api.post(`/users/${editing.value.qq}/permissions/reset`)
  ElMessage.success('已恢复全局默认')
  dialogVisible.value = false
  await load()
}

async function block(row: any) {
  const result = await ElMessageBox.prompt(`封禁 ${row.qq} 的原因`, '封禁用户', {
    inputPlaceholder: '例如：异常筛选条件',
    confirmButtonText: '封禁',
    cancelButtonText: '取消',
  }).catch(() => null)
  if (!result) return
  try {
    await api.post(`/users/${row.qq}/block`, { reason: result.value || '管理员封禁' })
    ElMessage.success('已封禁')
    await load()
  } catch (err) {
    ElMessage.error(errorText(err))
  }
}

async function unblock(row: any) {
  try {
    await api.delete(`/users/${row.qq}/block`)
    ElMessage.success('已解封')
    await load()
  } catch (err) {
    ElMessage.error(errorText(err))
  }
}

async function openBark(row: any) {
  barkQq.value = row.qq
  barkForm.target = ''
  barkForm.sound = ''
  try {
    const { data } = await api.get(`/users/${row.qq}/bark`)
    barkStatus.value = data
    barkForm.sound = data.sound || ''
    barkForm.enabled = !!data.enabled
  } catch (err) {
    ElMessage.error(errorText(err))
    return
  }
  barkVisible.value = true
}

async function saveBark() {
  barkSaving.value = true
  try {
    const payload: any = { sound: barkForm.sound.trim(), enabled: barkForm.enabled }
    if (barkForm.target.trim()) payload.target = barkForm.target.trim()
    const { data } = await api.put(`/users/${barkQq.value}/bark`, payload)
    barkStatus.value = data
    barkForm.target = ''
    ElMessage.success('手机推送配置已保存')
    await load()
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    barkSaving.value = false
  }
}

async function testBark() {
  barkTesting.value = true
  try {
    await api.post(`/users/${barkQq.value}/bark/test`, null, { params: { mode: 'normal' } })
    ElMessage.success('测试推送已发送')
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    barkingDone()
  }
}

function barkingDone() {
  barkTesting.value = false
  barkFlooding.value = false
}

async function testFlood() {
  barkFlooding.value = true
  try {
    await api.post(`/users/${barkQq.value}/bark/test`, null, { params: { mode: 'call' } })
    ElMessage.success('循环轰炸测试已发送：铃声会连续响约 30 秒')
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    barkingDone()
  }
}

async function clearBark() {
  await ElMessageBox.confirm('清空后该用户不会再收到手机推送。', '确认清空', {
    type: 'warning',
    confirmButtonText: '清空',
    cancelButtonText: '取消',
  })
  try {
    await api.delete(`/users/${barkQq.value}/bark`)
    barkStatus.value = {
      configured: false,
      enabled: false,
      server: '',
      sound: '',
      key_tail: '',
    }
    ElMessage.success('已清空')
    await load()
  } catch (err) {
    ElMessage.error(errorText(err))
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
.left {
  display: flex;
  gap: 10px;
}
.muted {
  color: #9aa4b5;
  font-size: 12px;
}
.hint {
  margin-left: 10px;
  color: #9aa4b5;
  font-size: 12px;
}
.bark-meta {
  color: #7a8597;
  font-size: 12px;
  margin-bottom: 12px;
}
.mb {
  margin-bottom: 12px;
}
</style>
