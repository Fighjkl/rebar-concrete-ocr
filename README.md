# 钢筋与混凝土报审表 OCR

这是一个本地运行的钢筋与混凝土报审表 OCR 台账工作台：上传 PDF 并选择材料后，由 Python 完成页面分类、表单提取、附件 OCR 和自动匹配；Java 保存业务状态、人工补正、核验和 Excel 导出；Vue 提供上传、历史、单卷台账、总台账和原图定位界面。

钢筋网页使用原 `rebar-hybrid-v3` 流程，混凝土使用独立 pipeline、worker、页头分类器和业务匹配器。两者复用图像处理、B.0.6 几何模板和模型执行：报审表用 Paddle、进场表用 GLM-OCR、附件用 Paddle 整页 OCR。混凝土新增分类规则不会进入钢筋分类器，详见[材料分流与边界](docs/MATERIAL_ROUTING.md)。模型权重、PDF、OCR 结果和业务数据库均由运行环境单独准备或生成，不随仓库提交。

## 已实现功能

- 钢筋/混凝土材料分流；原钢筋 12 列台账与混凝土 10 列台账分别匹配、核验和导出。
- 混凝土报审表与进场清单提取、配比报告/质量证明书/送货小票匹配，方量采用 m³，候选歧义保留人工核验。

- 自适应进场数量清单/进场检验记录提取，保留物理行、字段坐标、单位和置信度依据。
- 工程材料报审表公共字段提取，支持日期、使用部位、材料名称和文件编号证据。
- 质量证明书、铭牌、送货单的分类、逐卡/逐行读字和证据保存。
- 两条四表关联路径：清单质保书编号优先，或按规格/牌号寻找铭牌和送货单；规格严格匹配，牌号按有效强度数字匹配。
- 同页送货单补全、多个铭牌逐卡登记、证书/铭牌冲突保留双方原文和定位。
- 重复证号合并、全占位模板行过滤、缺失/未匹配/多候选/冲突诊断和字段核验策略。
- 12 列最终台账、字段来源和原图定位；Java 侧人工补正、核验后导出 Excel/CSV。
- 异步识别队列、同卷任务复用、混合流程与材料类别的缓存身份校验；诊断图片按 compact 策略按需恢复。

流程边界见[材料分流说明](docs/MATERIAL_ROUTING.md)，模型与服务安装见[部署说明](docs/DEPLOYMENT.md)。

## 环境准备

需要 Python 3.10+、JDK 17+、Maven 和 Node.js 20.19+。Python 依赖在 `pyproject.toml`/`requirements.txt`，包括 PaddleOCR/PaddleX、pypdfium2、FastAPI 和 XlsxWriter。Paddle 计算后端按硬件单独安装；当前生产管线使用 Linux/WSL 的 `fcntl` 文件锁。GLM 推理依赖另装在 `.venv-glm/`，权重单独放在 `models/GLM-OCR/`；具体步骤见[部署说明](docs/DEPLOYMENT.md)。`pip install -e .` 不会安装 PyTorch/Transformers 或下载 GLM 权重。

```bash
python -m pip install -e .
npm --prefix 前端/vue ci
```

已有 `ocr` conda 环境可以直接复用。首次安装可能需要联网下载 Python、Maven 和 npm 依赖；模型权重由各运行环境自行准备。

## 启动

在仓库根目录打开三个终端。Python 端口固定使用 8000，Java 使用 8080，Vue 默认使用 5173。

```bash
# 终端 1：Python OCR/匹配服务
PYTHONPATH=src:. python -m material_ocr serve \
  --port 8000 --data-dir runs/web --model-cache models/paddlex_cache
```

钢筋整卷识别使用 `rebar-hybrid-v3`；混凝土通过 `autodl_test_paddle_anchors.py --material concrete --mode full` 进入独立业务流程。默认设备为 `gpu:0`，模型缓存使用 `models/paddlex_cache`，GLM 子进程按 `MATERIAL_OCR_GLM_PYTHON` 配置；CPU 服务可增加 `--device cpu --profile complete`。

```bash
# 终端 2：Java 业务服务（本地 H2，无需 MySQL）
OCR_BASE_URL=http://127.0.0.1:8000 bash 前端/bin/start-local.sh
```

```bash
# 终端 3：Vue 开发服务
cd 前端/vue
npm run dev
```

浏览器打开 Vue 终端输出的地址，通常为 `http://127.0.0.1:5173`。Java 本地数据写入 `前端/runtime/h2/`，文件写入 `前端/image/`；这些目录均为本地产物。

停止各终端服务使用 `Ctrl+C`。已有环境启动前先检查实际进程与活动任务，避免重复启动占用端口。

生产构建：

```bash
npm --prefix 前端/vue run build
cd 前端/java/business && mvn package
```

## 常用命令与接口

```bash
# 旧版 Paddle 命令行分析流程；当前网页分流见 docs/MATERIAL_ROUTING.md
PYTHONPATH=src:. python -m material_ocr analyze \
  --pdf data/input.pdf --out runs/new_analysis --material rebar

# 复用已保存 OCR，只重放匹配
PYTHONPATH=src:. python -m material_ocr replay \
  --pdf data/input.pdf --records runs/source/work/inspection/records_all_pages.json \
  --index runs/source/work/attachments/attachment_index.json \
  --out runs/new_replay --material rebar

# 只检查分类和进场表提取，不执行附件 OCR/四表匹配
PYTHONPATH=src:. python -m material_ocr.worker inspection \
  --pdf data/input.pdf --out runs/inspection_only --device gpu:0 \
  --model-cache models/paddlex_cache --layout adaptive
```

网页主入口使用 Vue hash 路由：`/#/upload`、`/#/history`、`/#/history/<pdf_name>`、`/#/library/master`。Python 端提供 `/api/library/<id>/recognition`、`/api/library/<id>/ledger` 等接口，Java 端负责向 Vue 转发并保存业务副本。重新识别前请确认没有未导出的人工补正或正在运行的同卷任务。

## 交付边界

仓库只同步运行源码、配置、依赖锁文件、前端/Java 必需资源和说明文档。`tools/__init__.py` 和 `tools/compare_ocr_ledgers.py` 是生产匹配器的直接依赖，一并交付；其他研发工具不交付。以下内容必须留在本地：业务 PDF、`runs/` OCR/台账缓存、`models/` 模型权重、`前端/image/library/`、`前端/runtime/`、日志、构建目录、实验记录和浏览器缓存。新合作者应使用自己的 PDF 和缓存重新运行，并把验证结果记录在本地 `experiments/`。

当前工作文件可通过 `python scripts/prepare-github-release.py --dest dist/github-source` 生成独立发布快照，不携带旧 Git 历史。详见[最小源码发布](docs/MINIMAL_RELEASE.md)。
