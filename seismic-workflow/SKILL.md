---
name: seismic-workflow
description: >
  天然地震完整处理工作流：波形下载、PhaseNet震相拾取、GaMMA关联、
  NonLinLoc绝对定位、HypoDD相对定位、目录质检与制图。
  当用户需要处理地震波形数据、构建地震目录或进行精定位时使用。
license: MIT
compatibility: Python 3.11+, GMT 6（可选）, NonLinLoc/HypoDD（可选，缺失时自动回退纯 Python 定位）
metadata:
  version: 1.0.0
  domain: seismology
---

# Seismic Workflow Skill

你是天然地震数据处理操作员。严格按本文件的阶段顺序执行，**不得跳过任何检查点**。
所有脚本输出结构化 JSON 到 stdout；你只解析 stdout 的 JSON 字段，stderr 日志仅供人类阅读。

## 全局约定

- 工作目录：用户指定的项目根目录。所有路径均为相对该目录的相对路径。
- 数据布局（固定，勿改动）：

```
data/
├── raw/          # P1 波形 (miniSEED)
├── stations/     # P1 台站元数据 (StationXML / CSV)
├── catalog/      # 输入事件目录（可选）
├── preprocessed/ # P2 预处理后波形
├── picks/        # P3 震相拾取结果 CSV
├── assoc/        # P4 关联结果
├── locate/       # P5/P6 定位结果
└── qc/           # P8 质检图表与报告
```

- 每个脚本调用后：读取 stdout JSON 的 `status` 字段。
  - `"ok"` → 继续下一阶段；
  - 非 `"ok"` → 按"错误处理"小节执行，禁止自行编造参数重试超过 1 次。
- 环境探测：首次进入工作流前运行 `python scripts/detect_env.py`,
  根据返回的 `backends` 决定 P5/P6 使用哪个定位后端（见 Phase 5）。

---

## Phase 0: 环境探测

```bash
python scripts/detect_env.py
```

记录 stdout JSON 中：
- `backends.nll` / `backends.hypodd`：是否存在编译型定位程序二进制
- `packages.pygmt`：是否可用 PyGMT（否则制图回退 Cartopy+Matplotlib）
- `seisbench_cache`：PhaseNet 权重是否已缓存

若 `packages.obspy` 或 `packages.seisbench` 为 false：停止并报告用户缺少依赖，
提示参考 `docs/install.md`，不要尝试 pip install（零交互原则）。

**检查点 0**：确认 `obspy` 与 `seisbench` 均为 true 后才进入 Phase 1。

---

## Phase 1: 数据获取

### 场景 A：从 FDSN 数据中心下载

```bash
python scripts/fetch_waveforms.py \
  --network XX --station "*" --channel "HH*" \
  --start "2024-01-01T00:00:00" --end "2024-01-02T00:00:00" \
  --out data/raw --stations-out data/stations \
  --datacenter IRIS
```

常用 `--datacenter` 取值：`IRIS`、`USGS`、`INGV`、`GFZ`、`NIED`。
中国区域数据可指定 `--datacenter http://data.earthquake.cn/fdsnws`（如可访问）。

### 场景 B：本地已有波形文件

```bash
python scripts/fetch_waveforms.py --from-local <波形目录> \
  --out data/raw --stations-out data/stations
```

脚本会扫描 miniSEED/SAC 文件并自动提取台站元数据。

**成功判据**：stdout JSON `status=="ok"` 且 `n_files>0`、`n_stations>0`。

**错误处理**：
- FDSN 报无数据错误（JSON `error` 含 "no data"）：缩小时间窗或台站范围重试一次；仍失败则报告用户。
- 网络超时：报告用户当前环境可能离线，建议使用场景 B。

**检查点 1**：确认 `data/raw/` 下文件数 = JSON `n_files` 且均 >0 字节后才进入 Phase 2。

---

## Phase 2: 数据预处理

```bash
python scripts/preprocess.py \
  --input data/raw --output data/preprocessed \
  --stations data/stations/stations.xml \
  --remove-response --detrend --demean \
  --filter "0.5-20" --resample 100
```

- `--filter` 格式为 `<低频>-<高频>` Hz 带通；拾取用默认 0.5–20 Hz 即可。
- `--resample` 单位 Hz；不要低于原采样率的一半。
- 若无仪器响应文件，去掉 `--remove-response`（此时震级计算 ML 将不可用，需在报告中注明）。
- `--filter` 与 `--resample` 可同时使用，但 `--resample` 会自动调整 `--filter` 的截止频率，以避免过采样。

**成功判据**：`status=="ok"` 且 `n_traces_processed == n_traces_input - n_traces_failed`。

**错误处理**：`n_traces_failed > 0` 时查看 stderr 中 `[WARN]` 行列出的台站；
失败比例 >10% 则停止并报告用户（可能是响应文件缺失或格式损坏）。

**检查点 2**：确认 `data/preprocessed/` 存在且非空后才进入 Phase 3。

---

## Phase 3: 震相拾取

```bash
python scripts/pick_phases.py \
  --input data/preprocessed \
  --model phasenet \
  --output data/picks/phasenet_picks.csv \
  --batch-size 32
```

- 有 GPU 时追加 `--device cuda`；CPU 默认即可（慢 5–10 倍属正常）。
- 可选模型：`phasenet`（默认）、`eqtransformer`。
- 权重未缓存时会自动下载；离线环境报下载错误时按错误处理执行。

**成功判据**：`status=="ok"` 且 `n_picks > 0`。

**错误处理**：
- JSON `error` 含 "download"/"cache"：权重离线缺失。请用户将 `~/.cache/seisbench/`
  从有网机器拷贝过来（见 docs/install.md 第 2 节），然后重试一次。
- `n_picks == 0`：检查 Phase 2 是否误用了不合适的滤波参数；用 `--filter "1-15"` 重跑 Phase 2 后再试一次。

**检查点 3**：确认 `data/picks/phasenet_picks.csv` 存在且行数 ≈ JSON `n_picks + 1`（含表头）后才进入 Phase 4。

---

## Phase 4: 震相关联

```bash
python scripts/run_association.py \
  --picks data/picks/phasenet_picks.csv \
  --stations data/stations/stations.csv \
  --config assets/gamma_config.json \
  --output data/assoc/events.csv
```

- GaMMA 需要 `stations.csv`（含 station, latitude, longitude, elevation 列）。
  若 Phase 1 只产出 StationXML，先运行：
  `python scripts/fetch_waveforms.py --xml-to-csv data/stations/stations.xml --out data/stations/stations.csv`
- `assets/gamma_config.json` 为默认配置（速度模型 vel_p=6.0, vel_s=0.58*vel_p 等）,
  小区域内可由用户提供自定义配置覆盖。

**成功判据**：`status=="ok"` 且 `n_events >= 1`。

**错误处理**：
- `n_events == 0`：降低检测阈值——编辑传入的 config 副本，将 `min_picks_per_eq` 从 8 降为 5,
  重试一次；仍为 0 则报告用户（数据信噪比或台站密度不足）。
- GaMMA 未安装（JSON `error` 含 "gmma"）：报告用户按 docs/install.md 安装，不要跳过此阶段。

**检查点 4**：确认 `data/assoc/events.csv` 存在且 `n_events` 与 CSV 数据行数一致后才进入 Phase 5。

---

## Phase 5: 绝对定位（可插拔后端）

先用 Phase 0 的 `backends.nll` 决定后端：

### 后端 A：NonLinLoc（存在 NLLoc 二进制时，生产环境首选）

```bash
bash scripts/run_nll_locate.sh \
  --events data/assoc/events.csv \
  --picks data/picks/phasenet_picks.csv \
  --stations data/stations/stations.csv \
  --velocity-model assets/iasp91.tvel \
  --control-template assets/nll_control_template.conf \
  --output-dir data/locate/nll
```

包装器负责生成 NLL 控制文件、调用二进制、解析 `.hyp` 输出为 CSV+JSON。
你不需要了解 NLL 控制文件语法；需要调参时阅读 `references/nll_input_format.md`。

### 后端 B：纯 Python 网格搜索（无 NLLoc 二进制时的回退）

```bash
python scripts/grid_locate.py \
  --events data/assoc/events.csv \
  --picks data/picks/phasenet_picks.csv \
  --stations data/stations/stations.csv \
  --velocity-model assets/iasp91.tvel \
  --output data/locate/catalog_abs.csv
```

内部使用 `obspy.taup` 计算理论走时 + numpy 三维网格搜索（无需 Java/GMT）。
已知局限：一维速度模型、无 PDF 不确定度输出，深度误差约 2–5 km。

**成功判据**（两后端相同）：`status=="ok"` 且 `n_located >= 0.5 * n_events_input`。

**错误处理**：定位成功率 <50% 时，检查拾取质量（Phase 3 的 `n_picks` 是否异常少）,
或询问用户是否提供更合适的近台速度模型（`references/velocity_model_guide.md`）。

**检查点 5**：确认 `data/locate/` 下产出目录文件（catalog_abs.csv 或 *.hyp 对应的 CSV）非空后才进入 Phase 6。

---

## Phase 6: 相对定位（可选，精定位）

仅当满足以下全部条件才执行，否则直接跳到 Phase 7 并在报告中注明"未做相对定位"：
1. 用户明确要求精定位；
2. 事件数 ≥ 50；
3. Phase 0 显示 `backends.hypodd == true`，或用户接受简化版 DD（精度有限）。

### 后端 A：HypoDD（Fortran 二进制存在时）

```bash
bash scripts/run_hypodd.sh \
  --catalog data/locate/catalog_abs.csv \
  --waveforms data/preprocessed \
  --stations data/stations/stations.csv \
  --inp-template assets/hypodd_inp_template.txt \
  --output-dir data/locate/hypodd
```

流程：ph2dt 互相关 → hypoDD 反演，全部由包装器完成。调参见 `references/hypodd_workflow.md`。

### 后端 B：简化版双差（纯 Python 回退）

```bash
python scripts/simple_dd.py \
  --catalog data/locate/catalog_abs.csv \
  --waveforms data/preprocessed \
  --stations data/stations/stations.csv \
  --output data/locate/catalog_dd.csv
```

适用数百事件的小数据集；大数据集性能与收敛性无保证。

**成功判据**：`status=="ok"` 且 `rms_reduction > 0`。

**错误处理**：不收敛（JSON `error` 含 "converge"）时，增大 `--min-link 8` 重试一次；
仍失败则保留绝对定位目录并注明。

**检查点 6**：若执行了本阶段，确认最终目录文件（catalog_dd.csv）存在且事件数 ≥ 绝对定位数的 80%。

---

## Phase 7: 震级计算

```bash
python scripts/compute_magnitude.py \
  --catalog <上一步产出的最终目录CSV> \
  --waveforms data/preprocessed \
  --stations data/stations/stations.xml \
  --output data/locate/catalog_final.csv
```

- 使用 Wood-Anderson 仿真计算 ML 近震震级。
- 若 Phase 2 未做去响应，脚本会返回 `status=="warning"` 且 `magnitude_available=false`：
  此时保留无震级目录继续 Phase 8，并在报告中明确说明。

**检查点 7**：确认 `data/locate/catalog_final.csv` 存在且包含 lat/lon/depth/magnitude 列后才进入 Phase 8。

---

## Phase 8: 质检与制图

```bash
python scripts/qc_and_plot.py \
  --catalog data/locate/catalog_final.csv \
  --picks data/picks/phasenet_picks.csv \
  --stations data/stations/stations.csv \
  --output-dir data/qc
```

产出：
- `epicenter_map.png` 震中分布图（PyGMT 可用时出版级，否则 Cartopy/Matplotlib）
- `gr_relation.png` G-R 关系与完整性震级 Mc
- `depth_histogram.png` 深度分布直方图
- `residuals.png` 走时残差分布
- `report.md` 处理报告（基于 `assets/report_template.md`）

**检查点 8**：确认 `data/qc/report.md` 存在且引用的四张 PNG 均存在后，向用户汇报：
事件总数、Mc、平均走时残差、使用的定位后端、以及任何 warning。

---

## 完整流水线一键执行（推荐给确定性要求高的场景）

```bash
python scripts/run_pipeline.py \
  --input data/raw --workdir data \
  --skip hypodd   # 可选: none|hypodd|locate
```

该脚本串接 P2–P8 并在每阶段校验检查点，任一阶段失败即停并输出已完成阶段的 JSON 摘要。
当用户意图明确、无需中途决策时优先使用它，减少多轮工具调用。

## 参考文档（按需阅读，勿一次性全读）

- 速度模型选择困难时 → `references/velocity_model_guide.md`
- 需要调整 NonLinLoc 参数时 → `references/nll_input_format.md`
- HypoDD 定位效果不佳时 → `references/hypodd_workflow.md`
- 任何脚本报错时 → 先查 `references/troubleshooting.md` 再决定重试策略
