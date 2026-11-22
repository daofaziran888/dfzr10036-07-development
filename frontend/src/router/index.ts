import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ConflictDiffView from '@/views/ConflictDiffView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/conflict_diff', name: 'conflict_diff', component: ConflictDiffView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
