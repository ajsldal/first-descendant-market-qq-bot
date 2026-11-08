<template>
  <div>
    <div class="toolbar">
      <h3>系统设置</h3>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-card shadow="never" v-loading="loading">
      <el-form label-width="180px" style="max-width: 720px">
        <el-form-item label="全局订阅上限">
          <el-input-number v-model="form.max_subscriptions" :min="1" :max="200" />
          <span class="hint">用户没有单独配额时使用</span>
        </el-form-item>
        <el-form-item label="订阅分析开关">
          <el-switch v-model="form.analytics_enabled" />
          <span class="hint">关闭后不再写入消失历史与比对</span>
        </el-form-item>
        <el-form-item label="订阅分析白名单 QQ">
          <el-input
            v-model="whitelistText"
            type="textarea"
            :rows="3"
            placeholder="逗号或换行分隔，例如 123456789, 123456"
          />
          <div class="hint">只有这些 QQ 的订阅会进入分析表；留空即关闭记录。</div>
        </el-form-item>
        <el-form-item label="抓取审计保留天数">
          <el-input-number v-model="form.analytics_runs_retention_days" :min="1" :max="3650" />
          <span class="hint">只影响 crawl_runs，消失历史永久保留</span>
        </el-form-item>

        <el-divider content-position="left">登录代理</el-divider>
        <el-form-item label="启用登录代理">
          <el-switch v-model="form.login_proxy_enabled" />
          <el-input
            v-model="form.login_proxy_url"
            class="proxy-input"
            placeholder="http://127.0.0.1:7897"
          />
          <div class="hint">
            仅作用于 /登录、/登录 自动、/浏览器登录 的浏览器；Steam 登录在国内需要代理。关闭或留空则回退启动参数 --login-proxy。
          </div>
        </el-form-item>

        <el-divider content-position="left">好友申请</el-divider>
        <el-form-item label="自动同意好友">
          <el-switch v-model="form.friend_auto_accept_enabled" />
          <span class="hint">收到好友申请后自动通过；已封禁/忽略的 QQ 除外</span>
        </el-form-item>
        <el-form-item label="通过后发送帮助">
          <el-switch v-model="form.friend_welcome_enabled" />
          <span class="hint">新好友会收到欢迎语、命令文字与使用图；图片失败自动只发文字</span>
        </el-form-item>
        <el-form-item label="每分钟最多通过">
          <el-input-number
            v-model="form.friend_accept_max_per_min"
            :min="1"
            :max="30"
          />
          <span class="hint">防止批量加好友骚扰，默认 5 个/分钟</span>
        </el-form-item>

        <el-divider content-position="left">手机推送（Bark）</el-divider>
        <el-form-item label="启用手机推送">
          <el-switch v-model="form.bark_enabled" />
          <span class="hint">关闭后所有用户的订阅都不会推送手机；每个用户还要配置 Key 和订阅开关</span>
        </el-form-item>
        <el-form-item label="冷却时间（秒）">
          <el-input-number v-model="form.bark_cooldown_sec" :min="10" :max="3600" />
          <span class="hint">同一订阅两次手机推送的最小间隔，默认 120 秒</span>
        </el-form-item>
        <el-form-item label="通知级别">
          <el-select v-model="form.bark_level" style="width: 180px">
            <el-option label="active（亮屏）" value="active" />
            <el-option label="timeSensitive（专注也显示）" value="timeSensitive" />
            <el-option label="passive（不亮屏）" value="passive" />
            <el-option label="critical（静音也响）" value="critical" />
          </el-select>
        </el-form-item>
        <el-form-item label="通知分组">
          <el-input v-model="form.bark_group" placeholder="TFD市场" style="max-width: 220px" />
        </el-form-item>
        <el-form-item label="循环响铃">
          <el-switch v-model="form.bark_call" />
          <span class="hint">开启后铃声循环 30 秒；critical 需在 Bark App 授权「重要警告」</span>
        </el-form-item>
        <el-form-item label="每用户每分钟上限">
          <el-input-number v-model="form.bark_max_per_min" :min="1" :max="30" />
          <span class="hint">防止瞬时大量外呼</span>
        </el-form-item>

        <el-divider content-position="left">倒卖利润（仅手机订阅）</el-divider>
        <el-form-item label="启用利润计算">
          <el-switch v-model="form.flip_enabled" />
          <span class="hint">命中 1~3 条时用模糊过滤算最高倒卖利润并私聊用户；命中 ≥4 条跳过</span>
        </el-form-item>
        <el-form-item label="手续费">
          <el-input-number v-model="form.flip_fee" :min="0" :max="100000" :step="100" />
          <span class="hint">每次倒卖固定成本，默认 500</span>
        </el-form-item>
        <el-form-item label="税率（%）">
          <el-input-number v-model="form.flip_tax_percent" :min="0" :max="50" :step="1" />
          <span class="hint">按卖出价的百分比收取，默认 10%</span>
        </el-form-item>
        <el-form-item label="消息冷却（秒）">
          <el-input-number v-model="form.flip_cooldown_sec" :min="10" :max="3600" />
          <span class="hint">同一订阅两次利润消息的最小间隔，默认 600</span>
        </el-form-item>

        <el-divider content-position="left">成交统计（超越武器）</el-divider>
        <el-form-item label="启用成交统计抓取">
          <el-switch v-model="form.trade_stats_enabled" />
          <span class="hint">后台每轮抓取 12 件超越武器近 2 天的按小时成交数据</span>
        </el-form-item>
        <el-form-item label="抓取间隔（小时）">
          <el-input-number v-model="form.trade_stats_interval_hours" :min="1" :max="168" />
          <span class="hint">默认 12 小时；只追加新出现的时间点，历史数据永久保留</span>
        </el-form-item>

        <el-divider content-position="left">Steam 自动登录</el-divider>
        <el-form-item label="启用自动登录">
          <el-switch v-model="form.steam_auto_login_enabled" />
          <span class="hint">关闭后 /登录 自动 会被拒绝</span>
        </el-form-item>
        <el-form-item label="登录白名单 QQ">
          <el-input
            v-model="loginWhitelistText"
            type="textarea"
            :rows="2"
            placeholder="逗号或换行分隔，例如 123456789"
          />
          <div class="hint">
            只有这些 QQ 能使用 /登录、/登录 自动、/导入登录、/登录状态、/检查登录；/登出 不受限制。留空则所有人禁止登录。
          </div>
        </el-form-item>
        <el-form-item label="自动登录白名单 QQ">
          <el-input
            v-model="steamWhitelistText"
            type="textarea"
            :rows="2"
            placeholder="逗号或换行分隔，例如 123456789"
          />
          <div class="hint">只有这些 QQ 能触发自动登录；留空即禁止所有人。</div>
        </el-form-item>
        <el-form-item label="验证码等待上限（分钟）">
          <el-input-number v-model="guardMinutes" :min="10" :max="120" />
          <span class="hint">等待 Steam Guard/邮箱验证码的最长时间，期间浏览器保持打开</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存设置</el-button>
          <span class="hint">保存后 bot 最多 30 秒内生效，无需重启</span>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never" class="steam-card" v-loading="loading">
      <template #header>
        <div class="card-head">
          <span>Steam 凭据（DPAPI 加密）</span>
          <el-tag v-if="steamStatus.configured" :type="steamStatus.readable ? 'success' : 'danger'" size="small">
            {{ steamStatus.readable ? '已配置' : '已配置但无法解密' }}
          </el-tag>
          <el-tag v-else type="info" size="small">未配置</el-tag>
        </div>
      </template>
      <el-alert
        v-if="steamStatus.error"
        :title="steamStatus.error"
        type="error"
        :closable="false"
        class="mb"
      />
      <div class="steam-meta">
        最后更新：{{ steamStatus.updated_at ? fmtTime(steamStatus.updated_at) : '-' }}
      </div>
      <el-form label-width="180px" style="max-width: 720px">
        <el-form-item label="Steam 用户名">
          <el-input v-model="steamUsername" autocomplete="off" placeholder="仅用于自动登录" />
        </el-form-item>
        <el-form-item label="Steam 密码">
          <el-input
            v-model="steamPassword"
            type="password"
            show-password
            autocomplete="new-password"
            placeholder="保存后无法查看，只能覆盖"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="steamSaving" @click="saveSteam">
            保存凭据
          </el-button>
          <el-button type="danger" plain :disabled="!steamStatus.configured" @click="clearSteam">
            清空凭据
          </el-button>
          <span class="hint">只保存到本机 data/secrets，不进数据库、不进日志</span>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, errorText } from '../api'
import { fmtTime } from '../utils'

const loading = ref(false)
const saving = ref(false)
const steamSaving = ref(false)
const whitelistText = ref('')
const loginWhitelistText = ref('')
const steamWhitelistText = ref('')
const steamUsername = ref('')
const steamPassword = ref('')
const steamStatus = ref<any>({ configured: false, readable: false })
const form = reactive({
  max_subscriptions: 5,
  analytics_enabled: true,
  analytics_runs_retention_days: 30,
  steam_auto_login_enabled: true,
  login_proxy_enabled: true,
  login_proxy_url: '',
  bark_enabled: true,
  bark_cooldown_sec: 120,
  bark_level: 'timeSensitive',
  bark_group: 'TFD市场',
  bark_call: false,
  bark_max_per_min: 3,
  flip_enabled: true,
  flip_fee: 500,
  flip_tax_percent: 10,
  flip_cooldown_sec: 600,
  trade_stats_enabled: true,
  trade_stats_interval_hours: 12,
  friend_auto_accept_enabled: true,
  friend_welcome_enabled: true,
  friend_accept_max_per_min: 5,
})
const guardMinutes = ref(30)

async function load() {
  loading.value = true
  try {
    const [settingsRes, steamRes] = await Promise.all([
      api.get('/settings'),
      api.get('/steam/status'),
    ])
    const settings = settingsRes.data.settings || {}
    form.max_subscriptions = settings.max_subscriptions ?? 5
    form.analytics_enabled = !!settings.analytics_enabled
    form.analytics_runs_retention_days = settings.analytics_runs_retention_days ?? 30
    form.steam_auto_login_enabled = !!settings.steam_auto_login_enabled
    form.login_proxy_enabled = settings.login_proxy_enabled !== false
    form.login_proxy_url = settings.login_proxy_url || ''
    form.bark_enabled = settings.bark_enabled !== false
    form.bark_cooldown_sec = settings.bark_cooldown_sec ?? 120
    form.bark_level = settings.bark_level || 'timeSensitive'
    form.bark_group = settings.bark_group || 'TFD市场'
    form.bark_call = !!settings.bark_call
    form.bark_max_per_min = settings.bark_max_per_min ?? 3
    form.flip_enabled = settings.flip_enabled !== false
    form.flip_fee = settings.flip_fee ?? 500
    form.flip_tax_percent = Math.round((settings.flip_tax_rate ?? 0.1) * 100)
    form.flip_cooldown_sec = settings.flip_cooldown_sec ?? 600
    form.trade_stats_enabled = settings.trade_stats_enabled !== false
    form.trade_stats_interval_hours = settings.trade_stats_interval_hours ?? 12
    form.friend_auto_accept_enabled = settings.friend_auto_accept_enabled !== false
    form.friend_welcome_enabled = settings.friend_welcome_enabled !== false
    form.friend_accept_max_per_min = settings.friend_accept_max_per_min ?? 5
    whitelistText.value = (settings.analytics_whitelist_qq || []).join(', ')
    steamWhitelistText.value = (settings.steam_auto_allowed_qq || []).join(', ')
    loginWhitelistText.value = (settings.login_allowed_qq || []).join(', ')
    guardMinutes.value = Math.max(10, Math.round((settings.steam_auto_guard_timeout || 1800) / 60))
    steamStatus.value = steamRes.data || {}
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    loading.value = false
  }
}

function parseIds(text: string): number[] {
  const ids = new Set<number>()
  for (const part of text.split(/[,，\s]+/)) {
    const value = part.trim()
    if (value && /^\d+$/.test(value)) ids.add(Number(value))
  }
  return [...ids].sort((a, b) => a - b)
}

const parseWhitelist = computed(() => parseIds(whitelistText.value))

async function save() {
  saving.value = true
  try {
    const { data } = await api.put('/settings', {
      max_subscriptions: form.max_subscriptions,
      analytics_enabled: form.analytics_enabled,
      analytics_runs_retention_days: form.analytics_runs_retention_days,
      analytics_whitelist_qq: parseWhitelist.value,
      steam_auto_login_enabled: form.steam_auto_login_enabled,
      steam_auto_allowed_qq: parseIds(steamWhitelistText.value),
      login_allowed_qq: parseIds(loginWhitelistText.value),
      steam_auto_guard_timeout: guardMinutes.value * 60,
      login_proxy_enabled: form.login_proxy_enabled,
      login_proxy_url: form.login_proxy_url.trim(),
      bark_enabled: form.bark_enabled,
      bark_cooldown_sec: form.bark_cooldown_sec,
      bark_level: form.bark_level,
      bark_group: form.bark_group,
      bark_call: form.bark_call,
      bark_max_per_min: form.bark_max_per_min,
      flip_enabled: form.flip_enabled,
      flip_fee: form.flip_fee,
      flip_tax_rate: form.flip_tax_percent / 100,
      flip_cooldown_sec: form.flip_cooldown_sec,
      trade_stats_enabled: form.trade_stats_enabled,
      trade_stats_interval_hours: form.trade_stats_interval_hours,
      friend_auto_accept_enabled: form.friend_auto_accept_enabled,
      friend_welcome_enabled: form.friend_welcome_enabled,
      friend_accept_max_per_min: form.friend_accept_max_per_min,
    })
    const settings = data.settings || {}
    whitelistText.value = (settings.analytics_whitelist_qq || []).join(', ')
    loginWhitelistText.value = (settings.login_allowed_qq || []).join(', ')
    steamWhitelistText.value = (settings.steam_auto_allowed_qq || []).join(', ')
    ElMessage.success('设置已保存')
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    saving.value = false
  }
}

async function saveSteam() {
  if (!steamUsername.value.trim() || !steamPassword.value) {
    ElMessage.warning('请输入 Steam 用户名和密码')
    return
  }
  steamSaving.value = true
  try {
    const { data } = await api.put('/steam/credentials', {
      username: steamUsername.value.trim(),
      password: steamPassword.value,
    })
    steamStatus.value = data
    steamUsername.value = ''
    steamPassword.value = ''
    ElMessage.success('Steam 凭据已加密保存')
  } catch (err) {
    ElMessage.error(errorText(err))
  } finally {
    steamSaving.value = false
  }
}

async function clearSteam() {
  await ElMessageBox.confirm('清空后无法自动登录，需要重新录入。', '确认清空 Steam 凭据', {
    type: 'warning',
    confirmButtonText: '清空',
    cancelButtonText: '取消',
  })
  try {
    const { data } = await api.delete('/steam/credentials')
    steamStatus.value = data
    ElMessage.success('已清空 Steam 凭据')
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
.toolbar h3 {
  margin: 0;
  color: #1f2a3a;
}
.hint {
  margin-left: 10px;
  color: #9aa4b5;
  font-size: 12px;
}
.proxy-input {
  max-width: 300px;
  margin-left: 10px;
}
.steam-card {
  margin-top: 16px;
  border-radius: 10px;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.steam-meta {
  color: #7a8597;
  font-size: 12px;
  margin-bottom: 12px;
}
.mb {
  margin-bottom: 12px;
}
</style>
