import { createRouter, createWebHashHistory } from 'vue-router'
import { useAuthStore } from './stores/auth'
import LoginView from './views/LoginView.vue'
import LayoutView from './views/LayoutView.vue'
import DashboardView from './views/DashboardView.vue'
import UsersView from './views/UsersView.vue'
import SubscriptionsView from './views/SubscriptionsView.vue'
import SettingsView from './views/SettingsView.vue'
import AnalyticsView from './views/AnalyticsView.vue'
import TradeStatsView from './views/TradeStatsView.vue'
import AuditView from './views/AuditView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView },
    {
      path: '/',
      component: LayoutView,
      children: [
        { path: '', redirect: '/dashboard' },
        { path: 'dashboard', name: 'dashboard', component: DashboardView },
        { path: 'users', name: 'users', component: UsersView },
        { path: 'subscriptions', name: 'subscriptions', component: SubscriptionsView },
        { path: 'settings', name: 'settings', component: SettingsView },
        { path: 'analytics', name: 'analytics', component: AnalyticsView },
        { path: 'trade-stats', name: 'trade-stats', component: TradeStatsView },
        { path: 'audit', name: 'audit', component: AuditView },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  if (to.name === 'login') return true
  const auth = useAuthStore()
  const ok = await auth.fetchMe()
  if (!ok) return { name: 'login' }
  return true
})

export default router
