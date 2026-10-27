import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/upload' },
  { path: '/upload', name: 'Upload', component: () => import('@/views/UploadView.vue') },
  { path: '/history', name: 'History', component: () => import('@/views/HistoryView.vue') },
  { path: '/library/master', name: 'MasterLedger', component: () => import('@/views/MasterLedgerView.vue') },
  { path: '/history/:pdfName', name: 'Result', component: () => import('@/views/ResultView.vue'), props: true }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

export default router