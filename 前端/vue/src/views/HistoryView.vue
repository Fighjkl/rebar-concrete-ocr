<template>
  <el-card class="box-card">
    <template #header>
      <div class="header">
        <span>历史记录</span>
        <el-button size="small" @click="loadList" :loading="loading">刷新</el-button>
      </div>
    </template>

    <el-table :data="docs" v-loading="loading" stripe>
      <el-table-column label="PDF 名称" prop="originalName" />
      <el-table-column label="页数" width="80">
        <template #default="{ row }">{{ row.pageCount || 0 }}</template>
      </el-table-column>
      <el-table-column label="OCR 缓存" width="120">
        <template #default="{ row }">
          {{ row.ocrCachedCount || 0 }} / {{ row.pageCount || 0 }}
        </template>
      </el-table-column>
      <el-table-column label="台账" width="130">
        <template #default="{ row }">
          <el-tag v-if="recognitionStore.isRunning(row.pdfName)" type="warning" size="small">⏳ 识别中</el-tag>
          <el-tag v-else-if="row.ledgerRecognized" type="success" size="small">已生成</el-tag>
          <el-tag v-else type="info" size="small">未生成</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="tagType(row.lastStatus)">{{ statusText(row.lastStatus) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="180">
        <template #default="{ row }">{{ formatDate(row.updatedAt || row.createdAt) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200">
        <template #default="{ row }">
          <el-button size="small" type="primary" @click="goView(row)">查看</el-button>
          <el-popconfirm title="确定删除该文档？" @confirm="onDelete(row)">
            <template #reference>
              <el-button size="small" type="danger">删除</el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-if="!loading && docs.length === 0" description="暂无历史记录" />
  </el-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRecognitionStore } from '@/stores/recognition'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { historyList, deleteLibrary } from '@/api/library'
import { formatDate, statusText, tagType } from '@/utils/format'

const docs = ref([])
const loading = ref(false)
const router = useRouter()
const recognitionStore = useRecognitionStore()

async function loadList() {
  loading.value = true
  try {
    const res = await historyList()
    docs.value = (res && res.data) || []
  } finally {
    loading.value = false
  }
}

function goView(row) {
  if (!row.pdfName) return
  router.push('/history/' + encodeURIComponent(row.pdfName))
}

async function onDelete(row) {
  await deleteLibrary(row.pdfName)
  ElMessage.success('删除成功')
  loadList()
}

onMounted(loadList)
</script>

<style scoped>
.box-card { margin: 24px; }
.header { display: flex; justify-content: space-between; align-items: center; }
</style>