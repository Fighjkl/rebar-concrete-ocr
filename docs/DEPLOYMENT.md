# 新环境部署

需要 Linux/WSL、Python 3.10+、JDK 17+、Maven、Node.js 20.19+ 和 npm。
从克隆后的仓库根目录执行命令；运行服务前准备独立 Python 环境。

## Python 与模型

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

另外按 CPU/CUDA 硬件安装适配的 PaddlePaddle 计算后端。项目依赖中的
PaddleOCR/PaddleX 不替代该后端。Paddle 模型缓存放在
`models/paddlex_cache/`；离线部署时应事先准备 `official_models/` 下的模型。
模型维护和下载遵守各模型官方说明，权重不随此仓库提交。

GLM 使用单独的推理环境。先按 PyTorch 官方说明安装与硬件匹配的
torch/torchvision，再安装 GLM 依赖：

```bash
python -m venv .venv-glm
.venv-glm/bin/python -m pip install torch torchvision
.venv-glm/bin/python -m pip install -r requirements-glm.txt
.venv-glm/bin/python -c 'import torch, cv2, pypdfium2; from transformers import AutoProcessor, GlmOcrForConditionalGeneration; print(torch.__version__)'
```

GPU 环境需按实际 CUDA 版本替换上面的 PyTorch 安装命令。准备官方
[GLM-OCR](https://huggingface.co/zai-org/GLM-OCR) 完整模型到
`models/GLM-OCR/`，包含权重、模型配置、processor、tokenizer 和对话模板。
运行时读取本地模型，不通过 Git 传送权重。

默认 GLM 路径为 `.venv-glm/bin/python` 与 `models/GLM-OCR/`。
使用其他位置时，在启动 Python 服务前设置：

```bash
export MATERIAL_OCR_GLM_PYTHON=/absolute/path/to/glm-environment/bin/python
export MATERIAL_OCR_GLM_MODEL_DIR=/absolute/path/to/GLM-OCR
```

安装源码不会自动提供 Paddle 计算后端、GLM 推理环境及模型权重。

## 三个服务

三个终端均从仓库根目录开始。

```bash
# 终端一：激活已安装项目与 Paddle 的 Python 环境
PYTHONPATH=src:. python -m material_ocr serve \
  --port 8000 --data-dir runs/web --model-cache models/paddlex_cache
```

默认设备为 `gpu:0`。CPU 服务增加 `--device cpu --profile complete`，
GLM 推理环境也需支持所选设备。

```bash
# 终端二：本地 Java 业务服务与 H2 数据库
OCR_BASE_URL=http://127.0.0.1:8000 bash 前端/bin/start-local.sh
```

```bash
# 终端三：Vue
npm --prefix 前端/vue ci
npm --prefix 前端/vue run dev
```

访问 Vue 输出的地址，通常是 `http://127.0.0.1:5173/#/upload`。
上传自己的 PDF 并选择钢筋或混凝土。Java 使用 8080，Python 使用 8000。
运行数据写入 `runs/`、`前端/image/`、`前端/runtime/`，不进入 Git。

## 命令行

下面的命令会执行实际 OCR，需要已准备好的模型。每次使用新的输出目录。

```bash
python autodl_test_paddle_anchors.py --pdf /path/to/input.pdf \
  --material concrete --mode full --out runs/concrete-new \
  --glm-python .venv-glm/bin/python --model-dir models/GLM-OCR \
  --model-cache models/paddlex_cache
```

`--material rebar` 使用钢筋流程，默认材料也是钢筋。

## 构建

```bash
npm --prefix 前端/vue run build
```

在 `前端/java/business/` 执行 `mvn package` 构建 Java。
前端构建到 `前端/dist/`，本地 Java 配置可托管该目录。
MySQL 是可选部署方式，需配置数据库并使用 `前端/sql/init.sql`；
默认推荐通过本地启动脚本运行 H2。
