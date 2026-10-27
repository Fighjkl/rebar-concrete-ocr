# 最小源码发布

新仓库从当前工作文件生成独立源码快照，不继承旧仓库历史或远程地址。
未提交的钢筋/混凝土运行代码也会进入快照。准备命令不会删除本地数据。

```bash
python scripts/prepare-github-release.py --dest dist/github-source
```

目标目录必须尚不存在，已有目录不会覆盖。快照包含 Python 包及其规则 JSON、
模板、HTML/JS/CSS，`no_entry_ocr/` 的运行代码、两种材料的命令行入口、Java
运行源码与配置、Vue 源码与依赖锁、启动脚本和部署说明。

`tools/__init__.py` 与 `tools/compare_ocr_ledgers.py` 是生产链路的直接依赖，
必须保留。其他本机工具与环境不随最小源码发布。

不复制 PDF、业务 Excel/PPT、扫描图片、OCR 原文/索引/checkpoint/台账、
模型权重与模型缓存、业务数据库、测试样本、开发测试、实验记录、日志、依赖目录、
构建产物、账号凭据或开发机设置。新环境应按 [部署说明](DEPLOYMENT.md) 安装依赖，
准备自己的模型和业务输入。

`SOURCE_MANIFEST.json` 保存每个发布文件的 SHA256 和大小，可核对准备时的
实际源码。该清单不含业务数据，也不包含旧 Git 历史。

发布前应在快照中检查源码编译、所需模块及规则资源是否齐全，并检查 Git
待提交文件。原项目的 ignore 规则无法自动排除已被跟踪的业务资料，因此新仓库
使用独立快照，而不是直接推送旧分支。
