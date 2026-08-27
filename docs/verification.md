# 最小验证（Minimal Verification）用户指南

本文档说明如何使用 `tests/verify_skill.py` 对 `seismic-workflow` skill 的完整流程
（SKILL.md 检查点 0–8）进行端到端最小验证。

---

## 1. 验证内容概览

验证脚本在**合成数据**上串接执行 skill 的全部阶段，并对每个检查点做断言：

| 检查点 | 阶段 | 断言内容 |
|--------|------|----------|
| P0 | 环境探测 | `obspy` 与 `seisbench` 均可用 |
| P1 | 数据准备 | 合成波形已就位，11 个 miniSEED 文件均非空 |
| CP2 | 预处理 (P2) | `data/preprocessed/` 产出非空 |
| CP3 | 拾取 (P3) | picks CSV 行数与 JSON `n_picks` 一致 |
| CP4 | 关联 (P4) | events CSV 存在且事件数 ≥ 1 |
| CP5 | 定位 (P5) | 定位成功数 ≥ 关联事件数的 50% |
| CP7 | 震级 (P7) | 最终目录含 lat/lon/depth 列 |
| CP8 | 质检 (P8) | `report.md` + ≥5 张 PNG 图件（含拾取样例图） |

P6（HypoDD 相对定位）默认跳过（`--skip hypodd`），因二进制缺失时自动回退。

## 2. 前置条件

### 2.1 Python 环境

- 项目根目录下的 `.venv/`（Python 3.14）
- 必需包：`obspy`、`seisbench`、`torch`、`pandas`、`numpy`、`pyproj`
- GaMMA（P4 关联必需），Windows + Python 3.14 下按以下顺序安装：

```powershell
# 1) 先装好运行时依赖（不要让 pip 从源码编译 scikit-learn==1.7）
& .venv\Scripts\python.exe -m pip install scikit-learn numba pyproj tqdm

# 2) 再用 --no-deps 安装 GaMMA 本体（绕过其 setup.py 中钉死的 sklearn==1.7）
& .venv\Scripts\python.exe -m pip install --no-deps git+https://github.com/wayneweiqiang/GaMMA.git
```

> **为什么不能直接官方命令安装？** GaMMA 的 `setup.py` 声明
> `scikit-learn==1.7`，该版本无 Python 3.14 wheel，pip 会尝试源码编译，
> 连带从源码编译 scipy，最终因 Windows 缺 Fortran 编译器而失败。
> pip 报的 "gmma requires scikit-learn==1.7" 版本冲突警告可忽略。

### 2.2 合成测试数据（默认数据集：2024 Salcha M5.3 地震序列）

验证依赖 `tests/tmp_data/` 下的确定性合成数据，场景取自真实事件：
**2024 年 1 月 19 日阿拉斯加 Salcha Mw 5.3 地震**（USGS 事件号 `ak024vrx24o`）。
默认数据集参数记录如下（与 `tests/make_synthetic.py` 中常量一致）：

| 参数 | 值 |
|------|-----|
| 主震 | 2024-01-19 21:34:35.071 UTC, Mw 5.3 (mww) |
| 震中 | 64.4909°N, 146.9641°W |
| 深度 | 10.5 km |
| 余震序列 | 2024-01-19 ~ 02-28，M1.5–4.1 共约 100+ 个，集中于主震 ~6 km 内 |
| 测试用余震子集 | 10 个 M2.5–3.7 事件（真实 USGS 参数） |
| 台站 | AK 网络真实坐标 13 台 + IU COLA（HDA/PS08 近台 ~9–13 km；Fairbanks 城区台阵 ~60–90 km） |

若数据不存在，先生成：

```powershell
& .venv\Scripts\python.exe tests/make_synthetic.py
```

产出：
- `tests/tmp_data/raw/event_00..10.mseed` — 11 个事件（主震+10 余震）× 14 台站波形
- `tests/tmp_data/stations.xml` 与 `tests/tmp_data/stations/stations.csv`

PhaseNet 权重首次运行会自动下载到 `~/.cache/seisbench/`；离线环境请从有网机器拷贝。

## 3. 运行验证

在项目根目录执行：

```powershell
# 标准运行：结束后自动删除临时工作区
& .venv\Scripts\python.exe tests/verify_skill.py

# 调试运行：保留临时工作区，便于查看各阶段日志与中间产物
& .venv\Scripts\python.exe tests/verify_skill.py --keep
```

脚本会依次打印每条检查点的 `[PASS]` / `[FAIL]` 行，最后输出 JSON 汇总并
以退出码报告结果（0 = 全部通过，1 = 有失败）。

## 4. 输出位置

### 4.1 控制台输出（主要结果）

逐条检查点结果 + JSON 汇总，形如：

```json
{
  "status": "ok",
  "passed": 9,
  "failed": 0,
  "checks": [ ... ],
  "workspace": "(removed)"
}
```

### 4.2 临时工作区 `tests/tmp_data/verify_ws/`

加 `--keep` 时保留（否则运行结束即删除）。这是流水线的完整工作目录，
布局与 SKILL.md 规定的数据布局一致：

```
verify_ws/
└── data/
    ├── raw/              # 从 tmp_data/raw 拷入的合成波形
    ├── stations/         # stations.xml + stations.csv
    ├── preprocessed/     # P2 预处理后波形
    ├── picks/
    │   └── phasenet_picks.csv   # P3 拾取结果
    ├── assoc/
    │   └── events.csv           # P4 关联事件目录
    ├── locate/
    │   ├── catalog_abs.csv      # P5 绝对定位目录
    │   └── catalog_final.csv    # P7 加震级后的最终目录
    ├── qc/                      # P8 质检产物
    │   ├── report.md            # 处理报告
    │   ├── epicenter_map.png    # 震中分布图
    │   ├── gr_relation.png      # G-R 关系
    │   ├── depth_histogram.png  # 深度直方图
    │   └── residuals.png        # 走时残差
    └── pipeline_logs/           # 各阶段 stdout/stderr 日志（诊断首选）
        ├── preprocess.log
        ├── pick.log
        ├── associate.log
        └── ...
```

**排查失败时的阅读顺序**：先看控制台哪条检查点 FAIL → 打开对应的
`pipeline_logs/<阶段>.log`（JSON 为 stdout 结果，`---STDERR---` 之后是
人类可读日志/堆栈）→ 需要时再检查对应目录的中间 CSV。

## 5. 当前已知状态与限制

截至本文件编写时（2026-08-26）：**全部检查点通过（9/9）**

| 项目 | 状态 |
|------|------|
| P0/P1/P2/P3 | ✅ 通过 |
| P4 GaMMA | ✅ 通过（已适配 GMMA 1.2.17 新 API） |
| P5/P7/P8 | ✅ 通过（回退纯 Python 网格定位，NLLoc/HypoDD 二进制未装） |

P4 适配要点（GMMA 1.2.17 相对旧版的破坏性变更）：

- `dims` 必须精确为 `["x(km)", "y(km)", "z(km)"]`（`init_centers` 内部硬编码检查）
- 必需配置键：`z(km)`（深度搜索范围）、`bfgs_bounds`（反演边界）
- event 输出**没有** `latitude/longitude` 字段，位置在本地 AEQD 千米坐标
  `x(km)/y(km)/z(km)` 中，脚本已用 pyproj 逆投影转回经纬度
- `association()` 会向 stdout 打印进度文本，污染 JSON 契约——
  `run_pipeline.py` 已通过 `parse_last_json()` 容错解析
- 稀疏数据下需降低门限：`min_picks_per_eq=3`、`max_sigma11=5.0`

已知上游问题：GMMA 1.2.17 的 `GMM` 方法分支存在 bug
（`GaussianMixture.__init__` 未设置 `covariance_prior` 属性即被 `_base.py`
引用，触发 AttributeError）。默认配置使用 `BGMM` 方法可正常工作；
如需用 `GMM` 方法需手动补该属性。

另注意：合成数据每个事件只有 3–4 个台站拾取（PhaseNet 仅可靠拾取 P 波），
GaMMA 的多事件区分能力在此稀疏数据上有限，可能只关联出部分事件——
这不影响"流程贯通"的验证目标，但事件数可能少于合成的 10 个。

## 6. 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| `ModuleNotFoundError: No module named 'gamma'` | 未装 GaMMA，见 §2.1 |
| `all picks dropped: no pick matches a known station` | 台站名尾点不匹配（已修复）；若复现请检查 stations.csv 与 picks 的 station 列格式 |
| `n_events == 0` | 拾取数少于 `min_picks_per_eq`；编辑 `assets/gamma_config.json` 降低该值后重试 |
| PhaseNet 权重下载失败 | 离线环境，从有网机器拷贝 `~/.cache/seisbench/` |
| `scipy/gfortran` 编译错误 | 直接跑了官方安装命令，见 §2.1 正确安装方式 |
