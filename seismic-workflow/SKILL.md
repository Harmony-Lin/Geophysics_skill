---
name: seismic-workflow
description: >
  天然地震完整处理工作流：波形下载、PhaseNet震相拾取、GaMMA关联、
  NonLinLoc绝对定位、relocDD-py相对定位、目录质检与制图。
  当用户需要处理地震波形数据、构建地震目录或进行精定位时使用。
license: MIT
compatibility: Python 3.11+, GMT 6（可选）, NonLinLoc（可选）, relocDD-py（纯 Python，安装阶段 git 安装，见 docs/install.md）
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
├── stations/     # P1 台站元数据 (StationXML 必选；stations.csv 自动派生)
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
python scripts/p00_detect_env.py
```

记录 stdout JSON 中：

- `backends.nll`：是否存在编译型 NonLinLoc 定位程序二进制
- `backends.hypodd`：是否已安装 relocDD-py（纯 Python 双差定位，安装阶段 git 安装）
- `packages.pygmt`：是否可用 PyGMT（否则制图回退 Cartopy+Matplotlib）
- `seisbench_cache`：PhaseNet 权重是否已缓存

若 `packages.obspy` 或 `packages.seisbench` 为 false：停止并报告用户缺少依赖，
提示参考 `docs/install.md`，不要尝试 pip install（零交互原则）。

**检查点 0**：确认 `obspy` 与 `seisbench` 均为 true 后，报告用户环境检测通过，才进入 Phase 1。

---

## Phase 1: 数据获取

你需要询问用户数据来源，固定询问方式：

“现在让我们来指定需要解析的地震数据。你可以明确告诉我你想要研究的地震事件或告诉我本地波形数据的储存目录。在第一种情况下，我会从FDSN尝试下载相关的地震波形。“

### 场景 A：从 FDSN 数据中心下载

#### 步骤 1：解析用户提供的地震事件信息

用户可能提供以下任意形式的信息，你需要提取关键参数：

- **事件 ID/编号**：如 USGS `us7000abcd`、EMSC `20240101_123456`、中国地震台网 `20240101123456`
- **发震时间**：UTC 时间，精确到秒（如 `2024-01-01T12:34:56`）
- **震中位置**：经纬度（如 `35.6°N, 139.8°E`）或地名（如 "能登半岛"）
- **震级**：Mw/Ms/Ml 等（如 `Mw 7.6`）
- **研究目的**：主震定位、余震序列、前震分析、背景噪声等

**若用户仅提供模糊描述（如"2024年能登地震"），必须上网检索权威目录确定精确参数。**

#### 步骤 2：上网检索权威地震目录，确定精确时间区间与空间范围

| 目录平台                      | 访问方式                                                                                           | 适用场景           | 检索关键字段                                                                        |
| ----------------------------- | -------------------------------------------------------------------------------------------------- | ------------------ | ----------------------------------------------------------------------------------- |
| **USGS ComCat**         | `https://earthquake.usgs.gov/earthquakes/search/` 或 FDSN `event` 服务                         | 全球、M4.5+ 主震   | `eventid`, `time`, `latitude`, `longitude`, `depth`, `mag`, `magType` |
| **EMSC**                | `https://www.emsc-csem.org/Earthquake/` 或 `https://www.seismicportal.eu/fdsnws/event/1/query` | 欧地中海、全球实时 | `unid`, `time`, `lat`, `lon`, `depth`, `magnitude`                      |
| **ISC Bulletin**        | `https://www.isc.ac.uk/bulletin/search/`                                                         | 重新定位、参考深度 | `eventId`, `time`, `lat`, `lon`, `depth`, `magnitude`                   |
| **中国地震台网 (CENC)** | `http://www.ceic.ac.cn/` 或 `http://data.earthquake.cn/fdsnws/event/1/query`                   | 中国及周边区域     | `eventid`, `time`, `latitude`, `longitude`, `depth`, `magnitude`        |
| **GCMT**                | `https://www.globalcmt.org/`                                                                     | 震源机制、矩张量   | `eventname`, `time`, `lat`, `lon`, `depth`, `Mw`                        |

**检索操作指引（Agent 执行）：**

1. 使用浏览器工具访问上述平台，搜索用户提供的关键词（时间、地名、震级）
2. 确认**唯一匹配事件**，记录：`event_id`、`origin_time (UTC)`、`lat/lon`、`depth`、`magnitude`、`mag_type`
3. **确定下载时间窗**：
   - 主震研究（默认）：`origin_time - 60s` 到 `origin_time + 300s`（含 P/S 波完整波形）
   - 余震序列（若用户明确指定）：`origin_time - 86400s` (1天前) 到 `origin_time + 7天`，或用户指定时长
   - 背景噪声/前震：事件前 1-7 天连续波形
4. **确定空间范围**：以震中为中心，根据震级估算最大关注距离：
   - M < 4.0：50-100 km
   - M 4.0-5.5：100-200 km
   - M 5.5-7.0：200-500 km
   - M > 7.0：500-1000+ km（考虑面波传播）

#### 步骤 3：选择合适的台网与数据中心

| 目标区域               | 推荐数据中心 (优先级)                                  | 推荐台网代码                                           | 说明                  |
| ---------------------- | ------------------------------------------------------ | ------------------------------------------------------ | --------------------- |
| **全球/美国**    | IRIS (1), USGS (2)                                     | `IU`, `II`, `US`, `TA`, `AK`, `CN`, `IU` | GSN/USArray 覆盖全球  |
| **欧洲/地中海**  | ORFEUS/EIDA (1), GFZ (2), INGV (3)                     | `GE`, `GR`, `HT`, `IV`, `MN`, `Z3`         | 欧洲密集台网          |
| **中国大陆**     | CENC`http://data.earthquake.cn/fdsnws` (1), IRIS (2) | `CN`, `CD`, `BJ`, `XJ`, `XM`, `HK`         | 中国国家台网+区域台网 |
| **日本**         | NIED (1), IRIS (2)                                     | `JP`, `JU`, `VU`, `XJ`                         | Hi-net, F-net, K-NET  |
| **台湾**         | CWB (1), IRIS (2)                                      | `TW`, `TA`, `TP`                                 | 中央气象局台网        |
| **全球通用备选** | IRIS (聚合全球 FDSN)                                   | `*` (所有可用)                                       | 兜底方案，速度较慢    |

**台网选择策略：**

1. **优先选择目标区域的国家/区域台网**（数据延迟低、响应文件完整）
2. **补充全球台网 (IU/II/GE)** 用于远场记录和深度约束
3. **通道选择**：优先 `BH*` (宽频带, 20-100 Hz) > `HH*` (高增益, 100 Hz) > `EH*` (极高增益) > `SH*` (强震)
4. **避免选择**：临时实验台网 (如 `XA`, `XB` 等，除非用户明确要求)、已知噪声大的台站

#### 步骤 4：构造下载命令

```bash
python scripts/p01_fetch_waveforms.py \
  --network "CN,IU,II" --station "*" --channel "BH*,HH*" \
  --start "2024-01-01T12:33:00" --end "2024-01-01T12:40:00" \
  --out data/raw --stations-out data/stations \
  --datacenter IRIS \
  --min-radius 0 --max-radius 500 \
  --latitude 37.5 --longitude 137.2
```

**关键参数说明：**

- `--network`：逗号分隔的台网代码列表（如 `CN,JP,IU`）
- `--station`：台站代码，`*` 表示全部；可指定如 `N.AKIH,N.KTKB`
- `--channel`：通道模式，推荐 `BH*,HH*` 覆盖宽频带和高增益
- `--start/--end`：UTC 时间，**必须基于步骤 2 确定的精确时间窗**
- `--min-radius/--max-radius`：震中距范围（度或 km，脚本自动换算），基于步骤 2 估算
- `--latitude/--longitude`：震中坐标，**必须基于步骤 2 检索的权威坐标**
- `--datacenter`：按步骤 3 选择；中国区域**首选 CENC FDSN**，海外首选 IRIS

**仪器响应自动下载**：脚本内部使用 `level="response"` 调用 `get_stations()`，**自动下载完整的 StationXML（含仪器响应 Poles/Zeros/Gain/Sensitivity）**，保存为 `data/stations/stations.xml`。Phase 2 的 `--remove-response` 直接读取此文件进行去响应，**无需额外操作**。

常用 `--datacenter` 取值：`IRIS`、`USGS`、`INGV`、`GFZ`、`NIED`、`EIDA`、`CENC` (即 `http://data.earthquake.cn/fdsnws`)。

#### 步骤 5：执行下载并验证

运行上述命令，检查 stdout JSON：
- `status=="ok"` 且 `n_files>0`、`n_stations>0` → 成功，**同时产出 `data/stations/stations.xml`（含响应）和 `stations.csv`**
- `error` 含 "no data"：检查时间窗/台网/震中距是否过严，放宽后重试**一次**
- 网络超时/连接失败：重试**一次**，再次失败后报告用户环境可能离线，建议切换场景 B

**若 StationXML 下载失败**（stdout JSON 无 `stations_xml` 字段或 `n_stations=0`）：
- Phase 2 必须去掉 `--remove-response`，Phase 7 给 `p99_run_pipeline.py` 加 `--no-stations`，震级计算 (ML) 将不可用，报告中需注明
- **不要在缺少 StationXML 时保留 `--remove-response`**：p02 会直接失败（不再"警告后静默跳过"，否则 p07 会拿未物理化的 counts 算出看似正常的错误震级）
- 事后可手动从数据中心网站下载 StationXML 放入 `data/stations/` 补救

### 场景 B：用户明确指出本地已有波形文件

```bash
python scripts/p01_fetch_waveforms.py --from-local <波形目录> \
  --out data/raw --stations-out data/stations
```

脚本会扫描 miniSEED/SAC 文件并自动提取台站元数据。

**成功判据**：stdout JSON `status=="ok"` 且 `n_files>0`、`n_stations>0`。

**错误处理**：

- FDSN 报无数据错误（JSON `error` 含 "no data"）：缩小时间窗或台站范围重试一次；仍失败则报告用户。
- 网络超时：报告用户当前环境可能离线，建议使用场景 B。

**检查点 1**：确认 `data/raw/` 下文件数 = JSON `n_files` 且均 >0 字节后，报告用户，才进入 Phase 2。

## Phase 2: 数据预处理

```bash
python scripts/p02_preprocess.py \
  --input data/raw --output data/preprocessed \
  --stations data/stations/stations.xml \
  --remove-response --detrend --demean \
  --filter "0.5-20" --resample 100
```

- `--filter` 格式为 `<低频>-<高频>` Hz 带通；拾取用默认 0.5–20 Hz 即可。
- `--resample` 单位 Hz；不要低于原采样率的一半。
- `--detrend` / `--demean` 默认开启，需要保留原始趋势时用 `--no-detrend` / `--no-demean` 关闭。
- `--remove-response` 输出 `output="VEL"`（**地面速度 m/s**），这是 Phase 7 Wood-Anderson 仿真所需的输入域。缺少 StationXML 时该选项会**直接报错退出**，不会静默跳过。
- 若无仪器响应文件，去掉 `--remove-response`（此时震级计算 ML 将不可用，需在报告中注明），并给 Phase 7 加 `--no-stations`。
- `--filter` 与 `--resample` 可同时使用，但 `--resample` 会自动调整 `--filter` 的截止频率，以避免过采样。

**成功判据**：`status=="ok"` 且 `n_traces_processed == n_traces_input - n_traces_failed`；
去响应时还应满足 `remove_response_requested==true`、`response_removed==true`、
`output_units=="M/S"`，且 `n_traces_response_removed == n_traces_processed`。
`output_units=="MIXED"` 表示只有部分 trace 去响应成功，必须停下来排查。

**错误处理**：`n_traces_failed > 0` 时查看 stderr 中 `[WARN]` 行列出的台站；
失败比例 >10% 则停止并报告用户（可能是响应文件缺失或格式损坏）。

**检查点 2**：确认 `data/preprocessed/` 存在且非空后才进入 Phase 3。

---

## Phase 3: 震相拾取

```bash
python scripts/p03_pick_phases.py \
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

**检查点 3**：确认 `data/picks/phasenet_picks.csv` 存在且行数 ≈ JSON `n_picks + 1`（含表头）后，报告用户，才进入 Phase 4。

---

## Phase 4: 震相关联

```bash
python scripts/p04_run_association.py \
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
- 除 `events.csv` 外还会在同目录写出 `assignments.csv`（`pick_index,event_id`），
  记录每个拾取归属的事件。Phase 5 优先用它挑选事件自身的拾取；
  若该文件缺失，Phase 5 退化为 ±60 s/+120 s 时间窗选择，会把同一拾取重复分给多个事件，
  使 `rms_s` 虚高数十秒（实测中位数 29 s vs 正确 1.2 s）。

**成功判据**：`status=="ok"` 且 `n_events >= 1`。

**错误处理**：

- `n_events == 0`：降低检测阈值——编辑传入的 config 副本，将 `min_picks_per_eq` 从 6 降为 5,
  重试一次；仍为 0 则报告用户（数据信噪比或台站密度不足）。
- 事件数明显多于波形文件数、或出现远离台阵的孤立事件：
  提高 `min_picks_per_eq`（默认 6）与 `min_stations`（默认 4）。
  只有 3–5 个拾取的事件无法约束自由震源参数，会被定位到任意远端位置且 `rms_s` 极大。
- GaMMA 未安装（JSON `error` 含 "gmma"）：报告用户按 docs/install.md 安装，不要跳过此阶段。

**检查点 4**：确认 `data/assoc/events.csv` 存在且 `n_events` 与 CSV 数据行数一致后，报告用户，才进入 Phase 5。

---

## Phase 5: 绝对定位（可插拔后端）

先用 Phase 0 的 `backends.nll` 决定后端：

### 后端 A：NonLinLoc（存在 NLLoc 二进制时，生产环境首选）

```bash
bash scripts/p05_run_nll_locate.sh \
  --events data/assoc/events.csv \
  --picks data/picks/phasenet_picks.csv \
  --stations data/stations/stations.csv \
  --velocity-model assets/iasp91.tvel \
  --control-template assets/nll_control_template.conf \
  --output-dir data/locate/nll
```

包装器负责生成 NLL 控制文件、调用二进制、解析 `.hyp` 输出为 CSV+JSON。
你不需要了解 NLL 控制文件语法；需要调参时阅读 `references/nll_input_format.md`。

### 后端 B：纯 Python Geiger 迭代定位（无 NLLoc 二进制时的回退）

```bash
python scripts/p05_grid_locate.py \
  --events data/assoc/events.csv \
  --picks data/picks/phasenet_picks.csv \
  --stations data/stations/stations.csv \
  --velocity-model assets/iasp91.tvel \
  --output data/locate/catalog_abs.csv
```

内部使用 `obspy.taup` 计算理论走时 + Geiger 迭代加权最小二乘反演（无需 Java/GMT）：
从 GaMMA 事件位置框中心出发，逐次求解阻尼法方程 $\Delta\theta=(A^TW A+\lambda I)^{-1}A^TW r$，
以 Tukey 双权函数做抗差迭代（$c=1.345$），迭代 3–50 次后收敛。
事件拾取取自 Phase 4 的 `assignments.csv`（存在时）；理论走时用 IASP91 的
（深度, 震中距）网格插值，并缓存到 `seismic-workflow/.cache/`。
已知局限：一维速度模型、无 PDF 不确定度输出，深度误差约 2–5 km。
`--cell-km` 为兼容旧调用保留，现已废弃（仅记录到日志，不影响计算）。

**成功判据**（两后端相同）：`status=="ok"` 且 `n_located >= 0.5 * n_events_input`。

**错误处理**：定位成功率 <50% 时，检查拾取质量（Phase 3 的 `n_picks` 是否异常少）,
或询问用户是否提供更合适的近台速度模型（`references/velocity_model_guide.md`）。

**检查点 5**：确认 `data/locate/` 下产出目录文件（catalog_abs.csv 或 *.hyp 对应的 CSV）非空后才进入 Phase 6。

---

## Phase 6: 相对定位（可选，精定位）

仅当满足以下全部条件才执行，否则直接跳到 Phase 7 并在报告中注明"未做相对定位"：

1. 用户明确要求精定位；
2. 事件数 ≥ 50；
3. Phase 0 显示 `backends.hypodd == true`（即已安装 relocDD-py），或用户接受简化版 DD（精度有限）。

### 后端 A：relocDD-py（纯 Python 双差定位，安装阶段 git 安装）

```bash
bash scripts/p06_run_hypodd.sh \
  --catalog data/locate/catalog_abs.csv \
  --waveforms data/preprocessed \
  --picks data/picks/phasenet_picks.csv \
  --stations data/stations/stations.csv \
  --output-dir data/locate/hypodd
```

流程：由绝对定位目录 + PhaseNet 拾取生成 phase.dat/station.dat → relocDD-py
`ph2dt`（目录差分走时）→ relocDD-py `hypoDD`（反演）→ 解析 hypoDD.reloc 输出 CSV。
全部由包装器完成，无需编译型 Fortran 二进制。调参见 `references/hypodd_workflow.md`。

> 注意：relocDD-py 的 ph2dt 仅基于目录拾取（phase.dat）构建差分走时，**不做波形互相关**，
> 因此 `--waveforms` 仅为兼容保留、不参与计算。
>
> 依赖：relocDD-py 在安装阶段由 `packages/relocdd-py/install.py` 从 git 安装
> （上游无 `pyproject.toml`，见 `docs/install.md`），运行阶段不再联网。
> 若 `backends.hypodd == false`，改用后端 B 并在报告中注明。

### 后端 B：简化版双差（纯 Python 回退）

```bash
python scripts/p06_simple_dd.py \
  --catalog data/locate/catalog_abs.csv \
  --waveforms data/preprocessed \
  --stations data/stations/stations.csv \
  --output data/locate/catalog_dd.csv
```

适用数百事件的小数据集；大数据集性能与收敛性无保证。

**成功判据**：`status=="ok"` 且 `n_relocated > 0`。

**错误处理**：relocDD-py 反演失败（JSON `error` 含 "hypoDD failed"）时，检查
`data/locate/hypodd/hypodd.log`；若因配对过少，可调低 `assets/relocdd_ph2dt_inp_template.txt`
中的 `MINLNK` 或调高 `MAXSEP` 后重试一次；仍失败则保留绝对定位目录并注明。

**检查点 6**：若执行了本阶段，确认最终目录文件（catalog_dd.csv）存在且事件数 ≥ 绝对定位数的 80%。

---

## Phase 7: 震级计算

```bash
python scripts/p07_compute_magnitude.py \
  --catalog <上一步产出的最终目录CSV> \
  --waveforms data/preprocessed \
  --stations data/stations/stations.xml \
  --assume-units VEL \
  --output data/locate/catalog_final.csv
```

- 使用 Wood-Anderson 仿真计算 ML 近震震级，标定式为 **Bakun & Joyner (1984)**
  （`ML = log10(A) + log10(D/100) + 0.00301·(D−100) + 3.0`，A 为 WA 零到峰位移振幅 mm，
  D 为震源距 km），与 `obspy.signal.invsim.estimate_magnitude` 同源。
- `--assume-units` **必须显式声明**，因为 miniSEED 不保存 units 元数据：
  - `VEL`（默认）——上游 p02 以 `output="VEL"` 去响应，正常流程用这个；
  - `DISP`——输入是位移，脚本会先微分回速度；
  - `COUNTS`——输入仍是原始计数，脚本在此处调用 `remove_response(output="VEL")`。
  传错单位会得到量级完全错误的震级（约差 2–3 级），且不会报错。
- 脚本自身的 ML 结果会**覆盖**上游目录里的 `magnitude` / `magnitude_type`
  （本阶段是震级的唯一权威来源），并输出
  `catalog_final.magnitude_diag.csv`（逐台站的振幅/震中距/单台震级，便于排查）。
- 若未找到 StationXML，脚本返回 `status=="warning"` 且 `magnitude_available=false`：
  此时保留无震级目录继续 Phase 8，并在报告中明确说明。
- 回归测试：`python tests/verify_ml.py`（真实 Templeton 数据，锚定公开的 ML 3.19）。
  修改本阶段任何常数/单位处理前后都应运行它。

**检查点 7**：确认 `data/locate/catalog_final.csv` 存在且包含 lat/lon/depth/magnitude 列后才进入 Phase 8。

---

## Phase 8: 质检与制图

```bash
python scripts/p08_qc_and_plot.py \
  --catalog data/locate/catalog_final.csv \
  --picks data/picks/phasenet_picks.csv \
  --waveforms data/preprocessed \
  --stations data/stations/stations.csv \
  --output-dir data/qc
```

产出：

- `epicenter_map.png` 震中分布图（含矢量底图、台站名标注、事件时间+震级标注；
  PyGMT 可用时出版级，否则用 Cartopy 增绘海陆（ocean/land/coastline）与行政边界
  （国界 + 省/州界）底图，再不行则退回纯 Matplotlib 点图）
- `gr_relation.png` G-R 关系与完整性震级 Mc
- `depth_histogram.png` 深度分布直方图
- `residuals.png` 走时残差分布
- `pick_samples.png` Phase 3 拾取样例图：选取拾取数最多的若干事件，
  叠加显示波形与 P/S 拾取竖线（样例数量有限，非全部事件）
- `report.md` 处理报告（结构见 `assets/report_template.md`）：概要、统计、
  **逐事件目录与震级表**（每个事件的 ML、参与台站数/道数、拾取数、RMS 与质量分级）、
  单台震级质控表（单台 ML 离散度、震中距范围、方位角间隙）、数据完整性检查、
  输入数据来源与图件引用

**检查点 8**：确认 `data/qc/report.md` 存在且引用的五张 PNG 均存在后，向用户汇报：
事件总数、Mc、平均走时残差、使用的定位后端、以及任何 warning。

---

## P2-P8完整流水线一键执行（推荐给确定性要求高的场景）

```bash
python scripts/p99_run_pipeline.py \
  --input data/raw --workdir data \
  --stations-dir data/stations \
  --skip hypodd   # 可选: none|hypodd|locate；--stations-dir 默认 <workdir>/stations
```

该脚本串接 P2–P8 并在每阶段校验检查点，任一阶段失败即停并输出已完成阶段的 JSON 摘要。
当用户意图明确、无需中途决策时优先使用它，减少多轮工具调用。

**台站元数据**：`--stations-dir` 下必须有 `stations.xml`（StationXML）。
缺 XML 时脚本立即失败并给出提示——因为 P2 的 `--remove-response` 和 P7 的 ML
都依赖真实仪器响应，静默降级会产出"看起来正常但错 2–3 个震级单位"的结果。
`stations.csv` 若不存在，会由 `stations.xml` **自动派生**（无需手工维护）。
仅做拾取/定位、不需要震级时，可用 `--no-stations` 跳过响应去卷积与 ML
（仍需要 `stations.csv` 供关联/定位使用），此时 P7 上报 `magnitude_available=false`。

## 参考文档（按需阅读，勿一次性全读）

- 速度模型选择困难时 → `references/velocity_model_guide.md`
- 需要调整 NonLinLoc 参数时 → `references/nll_input_format.md`
- relocDD-py 相对定位效果不佳时 → `references/hypodd_workflow.md`
- 任何脚本报错时 → 先查 `references/troubleshooting.md` 再决定重试策略
