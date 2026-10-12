# 天然地震数据处理自动化框架

基于大语言模型 Agent Skills 的天然地震数据处理自动化框架。将地震学工作流（数据获取 → 拾取 → 关联 → 定位 → 震级 → 质检制图）编码为结构化 Skill，由 LLM Agent 作为"智能操作员"自主执行。

详见 `instructions.md`。

## 项目结构

```
Geophysics/
├── instructions.md          # 设计文档
├── requirements.txt         # Python 依赖
├── seismic-workflow/        # 核心 Skill（遵循 agentskills.io 标准）
│   ├── SKILL.md             # 技能定义（待编写）
│   ├── scripts/             # 各阶段包装脚本
│   ├── packages/            # 第三方包安装脚本（relocdd-py 等）
│   ├── references/          # 深度参考文档
│   └── assets/              # 模板与静态资源
├── docker/                  # Dockerfile 与 compose 编排
├── docs/                    # 安装与部署手册
├── tests/                   # 测试
└── data/                    # 数据目录（不入库）
```

## 快速开始

```bash
# 创建虚拟环境 (Python 3.11+)
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux

pip install -r requirements.txt

# relocDD-py（双差定位后端）：上游无 setup.py/pyproject.toml，
# 由仓库内补入的 pyproject.toml 做 git + pip install -e 安装，见 docs/install.md
python seismic-workflow/packages/relocdd-py/install.py
```

## License

MIT
