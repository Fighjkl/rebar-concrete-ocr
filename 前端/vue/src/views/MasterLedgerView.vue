<template>
  <div class="master-layout">
    <div class="master-toolbar">
      <div class="cur-page">
        <el-icon style="margin-right:6px"><Document /></el-icon>
        {{ materialType === 'concrete' ? '混凝土台账' : '钢筋台账' }}
      </div>
      <el-radio-group v-model="materialType" style="margin-left:16px" @change="changeMaterial">
        <el-radio-button value="rebar" label="rebar">钢筋台账</el-radio-button>
        <el-radio-button value="concrete" label="concrete">混凝土台账</el-radio-button>
      </el-radio-group>
      <span class="spacer"></span>
      <el-button type="primary" plain :icon="Refresh" :loading="loading" @click="load">
        刷新
      </el-button>
      <el-dropdown @command="onExport" style="margin-left:8px">
        <el-button type="primary" :icon="Download">导出</el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="xlsx">导出 Excel（xlsx）</el-dropdown-item>
            <el-dropdown-item command="csv">导出 CSV</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button type="danger" plain :icon="Delete" :loading="clearing" @click="clearAllLedgers" style="margin-left:8px">
        清空全部台账
      </el-button>
    </div>

    <div class="master-hint">
      提示：双击单元格值可直接编辑（回车保存 / Esc 取消）；
      点击<span class="p-link">pN</span>跳转到源页；
      红框<span class="bad-tag">⚠</span>表示存在字段冲突或尚未确认匹配；空值标记为未识别。
    </div>

    <div v-if="loading && !data" class="loading-block">
      <el-icon class="is-loading"><Loading /></el-icon>
      <span>加载中...</span>
    </div>

    <div v-else-if="!data || data.empty" class="empty-state">
      <el-icon style="font-size:48px;color:#909399"><FolderOpened /></el-icon>
      <div class="title">当前类别还没有识别记录</div>
      <div class="subtitle">先到「上传识别」或「历史记录」里识别 PDF 后再来看总台账</div>
    </div>

    <div v-else class="table-scroll">
      <table class="master-table" :style="{ minWidth: (data.columns.length + 1) * 160 + 'px' }">
        <thead>
          <tr>
            <th class="sticky-left pdf-name">PDF</th>
            <th v-for="col in data.columns" :key="col" :title="col">{{ col === '验收数量' && materialType === 'concrete' ? '验收数量（m³）' : col }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIdx) in data.rows" :key="row.pdf_name + '-' + row.record_index">
            <td class="sticky-left pdf-name">
              <template v-if="isFirstOfPdf(rowIdx)">
                <a class="pdf-link" @click="openRow(row.pdf_name)">
                  {{ row.display_name || row.pdf_name }}
                </a>
                <span v-if="row.hallucination_count > 0" class="bad-badge" :title="'该 PDF 含 ' + row.hallucination_count + ' 处待核验'">
                  ⚠ {{ row.hallucination_count }}
                </span>
              </template>
            </td>
            <td v-for="col in data.columns" :key="col"
                :class="{ 'empty-cell': isEmptyCell(row.cells[col]) }"
                @dblclick.stop="startEdit(row.pdf_name, row.record_index, col, row.cells[col]?.value || '')">
              <div v-if="isEmptyCell(row.cells[col])" class="empty-marker">
                — <span v-if="row.cells[col]?.page" class="p-link" @click="openRow(row.pdf_name, row.cells[col].page, row.record_index, col)">p{{ row.cells[col].page }}</span>
              </div>
              <div v-else class="chip-list">
                <div class="mc-chip" :class="{ bad: row.cells[col].found_in_ocr === false }">
                  <span
                    class="mc-val"
                    :title="row.cells[col].found_in_ocr === false ? '⚠ 存在冲突或尚未确认匹配，点击查看来源' : (row.cells[col].page ? '来源页 p' + row.cells[col].page : '无来源页')"
                    @dblclick.stop="startEdit(row.pdf_name, row.record_index, col, row.cells[col].value)"
                  >{{ row.cells[col].value || '（空）' }}{{ row.cells[col].unit === 'm3' ? ' m³' : '' }}</span>
                  <span
                    v-if="row.cells[col].page"
                    class="mc-page"
                    @click="openRow(row.pdf_name, row.cells[col].page, row.record_index, col)"
                  >p{{ row.cells[col].page }}</span>
                </div>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 内联编辑 -->
    <el-dialog
      v-model="editVisible"
      title="编辑单元格"
      width="420px"
      :close-on-click-modal="false"
      @keydown.enter.stop="commitEdit"
      @keydown.esc.stop="cancelEdit"
    >
      <el-form label-width="80px">
        <el-form-item label="PDF">
          <span class="dialog-pdf">{{ editing.pdfName }}</span>
        </el-form-item>
        <el-form-item label="字段">
          <span>{{ editing.field }}</span>
        </el-form-item>
        <el-form-item label="新值">
          <el-input
            ref="editInput"
            v-model="editing.value"
            type="textarea"
            :rows="3"
            autofocus
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cancelEdit">取消 (Esc)</el-button>
        <el-button type="primary" :loading="editSaving" @click="commitEdit">保存 (Enter)</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  Refresh, Download, Document, FolderOpened, Loading, Delete
} from '@element-plus/icons-vue'
import { getMasterLedger, exportMasterLedger, patchMasterField, clearAllLedgers as clearAllLedgersApi } from '@/api/masterLedger'

const router = useRouter()
const route = useRoute()
const materialType = ref(route.query.material_type === 'concrete' ? 'concrete' : 'rebar')

const loading = ref(false)
const clearing = ref(false)
const data = ref(null)

let loadSequence = 0
async function load() {
  const sequence = ++loadSequence
  loading.value = true
  data.value = null
  try {
    const res = await getMasterLedger(materialType.value)
    if (sequence !== loadSequence) return
    data.value = (res && res.data) || null
  } catch (e) {
    ElMessage.error('加载总台账失败')
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}

function changeMaterial() {
  editVisible.value = false
  router.replace({ query: { ...route.query, material_type: materialType.value } })
  load()
}

function isEmptyCell(cell) {
  return !cell || !cell.value
}

function isFirstOfPdf(rowIdx) {
  const rows = (data.value && data.value.rows) || []
  if (rowIdx === 0) return true
  return rows[rowIdx].pdf_name !== rows[rowIdx - 1].pdf_name
}

async function clearAllLedgers() {
  if (!confirm('确定清空【所有 PDF】的台账吗？\n\n将删除各卷台账并保留 OCR 缓存，下次识别会重新提取字段并自动匹配。\n\n此操作不可撤销。')) return
  clearing.value = true
  try {
    const res = await clearAllLedgersApi()
    const d = (res && res.data) || {}
    ElMessage.success('已清空 ' + (d.cleared_pdf_count || 0) + ' 卷台账')
    await load()
  } catch (e) {
    ElMessage.error('清空失败: ' + e.message)
  } finally {
    clearing.value = false
  }
}

function openRow(pdfName, page, recordIndex, field) {
  router.push({
    path: '/history/' + encodeURIComponent(pdfName),
    query: page ? { jump: page, record: recordIndex, field } : {}
  })
}

function onExport(fmt) {
  if (!data.value || data.value.empty) {
    ElMessage.warning('暂无可导出数据')
    return
  }
  if (data.value.rows.some(r => !r.review_confirmed)) {
    ElMessage.warning('请先打开各单卷台账，核验关键字段并确认导出')
    return
  }
  exportMasterLedger(fmt, materialType.value)
}

// 双击 chip → 弹出编辑对话框
const editVisible = ref(false)
const editInput = ref(null)
const editSaving = ref(false)
const editing = reactive({ pdfName: '', field: '', recordIndex: 0, value: '' })

function startEdit(pdfName, recordIndex, field, value) {
  editing.pdfName = pdfName
  editing.field = field
  editing.recordIndex = recordIndex
  editing.value = value || ''
  editVisible.value = true
  nextTick(() => {
    if (editInput.value && editInput.value.focus) editInput.value.focus()
  })
}

async function commitEdit() {
  if (editSaving.value) return
  editSaving.value = true
  try {
    await patchMasterField(editing.pdfName, editing.recordIndex, editing.field, editing.value)
    ElMessage.success('已保存')
    editVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error('保存失败')
  } finally {
    editSaving.value = false
  }
}

function cancelEdit() {
  editVisible.value = false
}

onMounted(() => {
  load()
})
</script>

<style scoped>
/* === 总台账 — Word 文档表格风格 === */
/* 边框 1.5px 实线黑 · 0 圆角 · 表头灰底 · 字用衬线 · 数据等宽 */

.master-layout {
  padding: 24px 28px;
  background: var(--bg);
  min-height: calc(100vh - 48px);
  color: var(--ink);
  font-family: var(--sans);
}

/* 工具栏和 hint 仍走卡片风格 — 因为是 UI 控制区 */
.master-toolbar {
  display: flex;
  align-items: center;
  padding: 12px 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 12px;
}
.cur-page {
  display: inline-flex;
  align-items: center;
  font-size: var(--type-h2);
  line-height: var(--type-h2-lh);
  color: var(--ink);
  font-weight: 500;
  font-family: var(--serif);
}
.spacer { flex: 1; }
.master-toolbar .el-button { border-radius: var(--radius-sm); }
.master-toolbar .el-button--primary {
  background: var(--accent);
  border-color: var(--accent);
}
.master-toolbar .el-button--primary:hover {
  background: var(--accent-mid);
  border-color: var(--accent-mid);
}

.master-hint {
  margin-bottom: 16px;
  padding: 8px 14px;
  font-size: 12px;
  color: var(--ink-mid);
  background: var(--bg-soft);
  border-left: 3px solid var(--accent-mid);
  border-radius: 0;
  font-family: var(--sans);
}
.master-hint .p-link {
  color: var(--accent);
  font-family: var(--mono);
  margin: 0 2px;
}
.master-hint .bad-tag { color: var(--warn); font-weight: 600; margin: 0 2px; }

.loading-block,
.empty-state {
  text-align: center;
  padding: 80px 20px;
  background: #fff;
  border: 1.5px solid #1A1A1A;
  border-radius: 0;
  color: var(--ink-mute);
}
.empty-state .title {
  margin-top: 18px;
  font-size: 18px;
  color: var(--ink);
  font-family: var(--serif);
  font-weight: 600;
}
.empty-state .subtitle {
  margin-top: 6px;
  font-size: 13px;
  color: var(--ink-mid);
}

.table-scroll {
  background: #fff;
  border: 1.5px solid #1A1A1A;
  border-radius: 0;
  overflow: auto;
  max-height: calc(100vh - 260px);
}

/* 核心：Word 表格本体 */
.master-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
  line-height: 1.65;
  table-layout: fixed;
  font-family: var(--serif);
  color: var(--ink);
}
.master-table th,
.master-table td {
  border: 1px solid #1A1A1A;
  padding: 7px 10px;
  vertical-align: top;
  word-break: break-all;
  line-height: 1.55;
}

.master-table thead th {
  background: #E8E5DD;
  color: var(--ink);
  font-weight: 700;
  font-size: 13px;
  font-family: var(--serif);
  text-align: center;
  text-transform: none;
  letter-spacing: 0.04em;
  border-bottom: 1.5px solid #1A1A1A;
  position: sticky;
  top: 0;
  z-index: 2;
}

.master-table tbody tr:nth-child(even) td {
  background: #FAF9F6;
}

/* 粘性首列（PDF 名） — 像档案目录 */
.master-table th.sticky-left,
.master-table td.sticky-left {
  position: sticky;
  left: 0;
  background: #F4F2EC;
  z-index: 1;
  border-right: 1.5px solid #1A1A1A;
  text-align: left;
}
.master-table thead th.sticky-left {
  z-index: 3;
  background: #E8E5DD;
  text-align: left;
  font-family: var(--serif);
  font-weight: 700;
}

.master-table td.pdf-name {
  min-width: 180px;
  max-width: 220px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--ink);
  padding-left: 12px;
}
.master-table th.pdf-name {
  min-width: 180px;
  text-align: left;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ink-mid);
  font-weight: 600;
}

.pdf-link {
  color: var(--accent);
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
  text-decoration-thickness: 1px;
  font-family: var(--mono);
}
.pdf-link:hover { color: var(--accent-mid); }

.bad-badge {
  display: inline-block;
  margin-left: 6px;
  font-size: 10px;
  font-family: var(--mono);
  color: var(--warn);
  vertical-align: middle;
}

.chip-list { display: flex; flex-direction: column; gap: 3px; }
.mc-chip {
  display: inline;
  padding: 0;
  border: none;
  background: transparent;
}
.mc-val {
  font-family: var(--mono);
  font-size: 12.5px;
  color: var(--ink);
  cursor: text;
  user-select: text;
}
.mc-val[contenteditable="true"] {
  outline: none;
  background: var(--warn-soft);
  padding: 1px 4px;
  font-family: var(--sans);
}
.mc-page {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--ink-mid);
  margin-left: 4px;
  cursor: pointer;
  white-space: nowrap;
  text-decoration: none;
}
.mc-page::before { content: "("; color: var(--ink-mute); }
.mc-page::after { content: ")"; color: var(--ink-mute); }
.mc-page:hover { color: var(--accent); }

.chip-list .mc-chip + .mc-chip::before {
  content: " / ";
  color: var(--ink-mute);
}

/* 幻觉单元格：暖底 + 字色 */
.mc-chip.bad .mc-val {
  color: var(--warn);
  background: var(--warn-soft);
  padding: 0 3px;
  border-bottom: 1px dashed var(--warn);
}
.mc-chip.bad .mc-page { color: var(--warn); opacity: 0.7; }

.empty-cell { color: var(--ink-mute); text-align: center; font-style: italic; }
.empty-marker { color: var(--ink-mute); }

.dialog-pdf {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--ink-mid);
}

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border-mid); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--ink-mute); }
</style>
