<template>
  <el-container class="layout">
    <el-header class="topbar">
      <div class="brand" @click="$router.push('/upload')">
        <span class="logo-dot"></span>
        <span class="logo-text">PDF 识别</span>
        <span class="logo-sub">整卷 PDF 台账 · 两阶段推理 · 源页追溯</span>
      </div>
      <el-menu :default-active="$route.path" mode="horizontal" router class="menu">
        <el-menu-item index="/upload">上传识别</el-menu-item>
        <el-menu-item index="/history">历史记录</el-menu-item>
        <el-menu-item index="/library/master">总台账</el-menu-item>
      </el-menu>
      <span class="spacer"></span>
      <span class="status" :class="apiStatus">{{ apiStatusText }}</span>
    </el-header>
    <el-main class="main">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
import { ref, onMounted } from 'vue'

const apiStatus = ref('online')
const apiStatusText = ref('检测中…')

async function checkService() {
  try {
    const r = await fetch('/')
    if (r.ok) {
      apiStatus.value = 'online'
      apiStatusText.value = 'API 在线'
    } else throw new Error()
  } catch {
    apiStatus.value = 'offline'
    apiStatusText.value = 'API 离线'
  }
}

onMounted(() => {
  checkService()
  setInterval(checkService, 30000)
})
</script>

<style scoped>
.layout { min-height: 100vh; }
.topbar {
  display: flex;
  align-items: center;
  background: #fff;
  border-bottom: 1px solid #ebeef5;
  padding: 0 16px;
  height: 48px;
  gap: 16px;
}
.brand { display: flex; align-items: center; gap: 8px; cursor: pointer; }
.logo-dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: #409eff;
  display: inline-block;
}
.logo-text { font-size: 14px; font-weight: 600; color: #303133; }
.logo-sub { font-size: 12px; color: #909399; margin-left: 4px; }
.menu { flex: 1; border-bottom: none; }
.spacer { flex: 1; }
.status::before { content: "●"; margin-right: 4px; font-size: 12px; }
.status.online { color: #67c23a; }
.status.online::before { color: #67c23a; }
.status.offline { color: #f56c6c; }
.status.offline::before { color: #f56c6c; }
.main { padding: 0; background: #fafafa; }
</style>
