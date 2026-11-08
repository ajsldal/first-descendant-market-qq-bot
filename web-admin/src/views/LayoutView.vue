<template>
  <el-container class="layout">
    <el-aside width="220px" class="aside">
      <div class="logo">TFD 市场后台</div>
      <el-menu :default-active="route.path" router class="menu">
        <el-menu-item index="/dashboard">仪表盘</el-menu-item>
        <el-menu-item index="/users">用户与权限</el-menu-item>
        <el-menu-item index="/subscriptions">订阅管理</el-menu-item>
        <el-menu-item index="/settings">系统设置</el-menu-item>
        <el-menu-item index="/analytics">订阅分析</el-menu-item>
        <el-menu-item index="/trade-stats">成交统计</el-menu-item>
        <el-menu-item index="/audit">审计日志</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <div class="title">{{ title }}</div>
        <div class="right">
          <span class="user">
            {{ auth.admin?.username || '-' }}
            <el-tag size="small" type="info">{{ auth.admin?.role || '-' }}</el-tag>
          </span>
          <el-button link type="danger" @click="logout">退出登录</el-button>
        </div>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const titles: Record<string, string> = {
  '/dashboard': '仪表盘',
  '/users': '用户与权限',
  '/subscriptions': '订阅管理',
  '/settings': '系统设置',
  '/analytics': '订阅分析',
  '/trade-stats': '成交统计',
  '/audit': '审计日志',
}
const title = computed(() => titles[route.path] || '后台管理')

async function logout() {
  await auth.logout()
  router.push('/login')
}
</script>

<style scoped>
.layout {
  min-height: 100vh;
}
.aside {
  background: #11161f;
  color: #e6e9ef;
}
.logo {
  height: 56px;
  display: flex;
  align-items: center;
  padding: 0 18px;
  font-weight: 700;
  letter-spacing: 0.5px;
  border-bottom: 1px solid #232b38;
}
.menu {
  border-right: none;
  background: transparent;
  --el-menu-text-color: #aeb7c6;
  --el-menu-hover-bg-color: #1a2230;
  --el-menu-active-color: #e2ac2c;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid #e6e9ef;
}
.title {
  font-size: 17px;
  font-weight: 600;
  color: #1f2a3a;
}
.right {
  display: flex;
  align-items: center;
  gap: 14px;
}
.user {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #5b6678;
  font-size: 13px;
}
.main {
  background: #f4f6f9;
  padding: 20px;
}
</style>
