# 钢筋与混凝土的识别边界

网页上传时选择材料类别，Java 保存并转发 `material_type=rebar|concrete`。
Python 在 `web/library.py` 根据材料类别选择处理入口。

| 环节 | 钢筋 | 混凝土 |
| --- | --- | --- |
| 网页入口 | `web/hybrid.py`，`rebar-hybrid-v3` | `web/paddle_anchors.py`，以 `--material concrete` 启动独立流程 |
| 整卷管线 | `materials/rebar/upstream_pipeline.py` | `materials/concrete/pipeline.py` |
| 子进程 | `materials/rebar/upstream_worker.py` | `materials/concrete/upstream_worker.py` |
| 页头分类 | `upstream_legacy/materials/rebar/routing.py` | `materials/concrete/routing.py` |
| 业务解析与匹配 | `materials/rebar/` 的提取、证号/规格四表匹配及无进场表分支 | `materials/concrete/extraction.py`、`matching.py` 等专用规则 |
| 台账 | 12 列钢筋台账、原吨位/规格约束 | 10 列混凝土台账、立方米数量约束 |

表格中的 Python 路径均相对于 `src/material_ocr/`。

共享 OCR batch 接受每次调用的 `header_classifier`。默认继续使用钢筋分类器；
混凝土 worker 显式传入自己的分类器，普通页头识别和 300 DPI 重试均使用该函数。
混凝土材料进货清单的标题与列头扩展只存在于混凝土分类器中，不修改共享全局函数。

两种材料共享图像与方向处理、OCR 模型、B.0.6 报审表几何模板，以及 Paddle
报审表识别、GLM 进场表识别、Paddle 附件整页识别的执行能力。共享几何和模型
不意味着共享材料特有的分类词表、字段规则或匹配结果。

钢筋保留按质保书编号或规格关联质量证明书、铭牌及送货单的原流程；没有进场表时
使用原铭牌索引分支。混凝土有质保编号时优先按编号找配比报告，缺编号时按规格、
工程部位找候选，再依据明确编号关联质量证明书和送货小票。同一编号对应多票时，
只有进场方量与小票累计量唯一相等才自动选票；歧义和缺失保留给人工核验。

混凝土封存上游校验材料、分类器版本、代码、PDF、模型、参数和文件摘要。
钢筋上游及旧版未标材料的结果不能直接作为混凝土结果复用。整卷缓存仍有包级
代码指纹，修改混凝土代码可能使钢筋旧缓存需要重算；这不会改变钢筋分类规则。

新仓库只交付源码。各材料上传文件、OCR 缓存、模型和数据库在部署环境独立生成。
