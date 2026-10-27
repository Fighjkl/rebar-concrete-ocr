<template>
  <el-card class="box-card">
    <template #header>
      <span>上传 PDF</span>
    </template>

    <div
      class="dropzone"
      :class="{ 'is-active': pdfDragActive }"
      @dragover.prevent="pdfDragActive = true"
      @dragleave.prevent="pdfDragActive = false"
      @drop.prevent="onPdfDrop"
      @click="triggerPdfInput"
    >
      <el-icon class="el-icon--upload"><upload-filled /></el-icon>
      <div class="el-upload__text">将 PDF 拖到此处或<em>点击上传</em></div>
      <div class="el-upload__tip">支持 PDF 文件，最大 50MB</div>
      <input
        ref="pdfInput"
        type="file"
        accept="application/pdf,.pdf"
        hidden
        @change="onPdfInput"
      />
    </div>

    <div v-if="pdfFile" class="selected">
      已选择 <strong>{{ pdfFile.name }}</strong>
      <el-select v-model="materialType" placeholder="选择材料类别" style="width:150px;margin-left:12px" aria-label="PDF 文件类别">
        <el-option label="钢筋" value="rebar" />
        <el-option label="混凝土" value="concrete" />
        <el-option label="钢梁" value="steel_beam" />
      </el-select>
      <el-button type="primary" :loading="loading" :disabled="!materialType" @click="onUpload" style="margin-left:12px;">
        上传
      </el-button>
    </div>

    <el-alert
      v-if="lastResult"
      type="success"
      :closable="false"
      style="margin-top:16px;"
    >
      上传成功：<strong>{{ lastResult.originalName }}</strong>
      共 {{ lastResult.pageCount }} 页
      <el-button type="primary" link @click="goView">前往识别</el-button>
    </el-alert>
  </el-card>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { uploadPdf } from '@/api/library'

const router = useRouter()
const loading = ref(false)
const pdfFile = ref()
const pdfInput = ref()
const pdfDragActive = ref(false)
const lastResult = ref(null)
const materialType = ref('')

function triggerPdfInput() { pdfInput.value && pdfInput.value.click() }

function onPdfInput(e) {
  const file = e.target.files && e.target.files[0]
  if (file) pdfFile.value = file
  e.target.value = ''
}

function onPdfDrop(e) {
  pdfDragActive.value = false
  const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]
  if (!file) return
  if (!/\.pdf$/i.test(file.name)) {
    ElMessage.warning('请选择 PDF 文件')
    return
  }
  pdfFile.value = file
}

async function onUpload() {
  if (loading.value) return
  if (!pdfFile.value) {
    ElMessage.warning('请先选择 PDF')
    return
  }
  if (!materialType.value) {
    ElMessage.warning('请先选择 PDF 材料类别')
    return
  }
  const uploadingFile = pdfFile.value
  const doSend = async (overwrite) => {
    const fd = new FormData()
    fd.append('file', uploadingFile)
    fd.append('dpi', '200')
    fd.append('image_format', 'PNG')
    fd.append('material_type', materialType.value)
    if (overwrite) fd.append('overwrite', 'true')
    return uploadPdf(fd)
  }
  loading.value = true
  try {
    let res
    try {
      res = await doSend(false)
    } catch (e) {
      if (e && e.code === 409) {
        const ok = await ElMessageBox.confirm(
          `「${pdfFile.value.name}」已存在，覆盖会删除该卷已有的 OCR 缓存和台账，确定覆盖吗？`,
          '同名 PDF',
          { type: 'warning', confirmButtonText: '覆盖', cancelButtonText: '取消' }
        ).catch(() => false)
        if (!ok) return
        res = await doSend(true)
      } else {
        throw e
      }
    }
    const data = (res && res.data) || {}
    if (data.materialType !== materialType.value) {
      throw new Error(`材料类别未正确保存：选择 ${materialType.value}，服务端返回 ${data.materialType || '空值'}`)
    }
    lastResult.value = data
    ElMessage.success('上传成功')
    if (pdfFile.value === uploadingFile) pdfFile.value = null
  } catch (e) {
    // axios interceptor already toasted
  } finally {
    loading.value = false
  }
}

function goView() {
  if (!lastResult.value || !lastResult.value.pdfName) return
  router.push('/history/' + encodeURIComponent(lastResult.value.pdfName))
}
</script>

<style scoped>
.box-card { margin: 24px auto; max-width: 720px; }
.dropzone {
  border: 2px dashed #d9d9d9;
  border-radius: 6px;
  padding: 36px 20px;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.2s;
}
.dropzone.is-active,
.dropzone:hover { border-color: #409EFF; }
.el-icon--upload { font-size: 48px; color: #409EFF; margin-bottom: 8px; }
.el-upload__text { color: #606266; font-size: 14px; }
.el-upload__text em { color: #409EFF; font-style: normal; }
.el-upload__tip { font-size: 12px; color: #909399; margin-top: 6px; }
.selected { margin-top: 16px; color: #303133; }
</style>
