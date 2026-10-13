---
name: seismic-workflow
description: >
  天然地震完整处理工作流：波形下载、PhaseNet震相拾取、GaMMA关联、
  纯 Python Geiger 绝对定位、relocDD-py 相对定位、目录质检与制图。
  当用户需要处理地震波形数据、构建地震目录或进行精定位时使用。
license: MIT
compatibility: Python 3.11+, GMT 6（可选）, relocDD-py（纯 Python，安装阶段 git 安装，见 docs/install.md）
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
- 环境探测：首次进入工作流前运行 `python scripts/p00_detect_env.py`，
  根据返回的 `backends` 决定 P6 是否执行（见 Phase 6）。

---

## Phase 0: 环境探测

```bash
python scripts/p00_detect_env.py
```

记录 stdout JSON 中：

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

Phase 1 明确分为两步，**不要混在一起做**：

1. **先确定事件**（步骤 1–3）：把用户的话变成约束 → 用 `scripts/p01_resolve_event.py`
   在 FDSN 权威目录里解析出唯一事件，并得到一份现成的下载计划；信息不足或存在歧义时
   **必须先问用户**。
2. **再下载数据**（步骤 4–5）：用 `download_plan` 执行
   `scripts/p01_fetch_waveforms.py`，按震中距挑选台站并下载波形 + 台站响应。

#### 步骤 1：从用户的话里抽取约束

用户可能提供以下任意形式的信息，你需要提取关键参数：

- **事件 ID/编号**：如 USGS `us7000abcd`、NCEDC `nc75265261`、EMSC `20240101_123456`
- **发震时间**：UTC 时间（如 `2024-01-01T12:34:56`）或日期+时段
- **震中位置**：经纬度或地名/区域（如 "能登半岛"、"加州 Templeton 附近"）
- **震级**：Mw/Ms/Ml 等（如 `Mw 7.6`）或强度描述（"7 级左右"）
- **研究目的**：主震定位、余震序列、前震分析、背景噪声等

**信息不足时必须先问用户**：如果用户的描述无法唯一确定一个事件（既无事件 ID，也无具体
时间/地区/震级，例如"上次那个大地震"），先向用户提一个具体问题补齐信息，再进入步骤 2。
不要凭猜测挑一个事件就下载。

#### 步骤 2：用脚本在权威目录里解析出唯一事件

```bash
python scripts/p01_resolve_event.py \
  --start 2024-01-01T07:00:00Z --end 2024-01-01T08:00:00Z \
  --latitude 37.5 --longitude 137.2 --min-magnitude 6.5 \
  --text "2024年1月1日 日本能登半岛 7.6级地震"
```

可用输入（至少提供一个）：`--event-id`、`--start/--end`、
`--latitude/--longitude [--within-km]`、`--min-magnitude`、`--catalog`。
脚本依次查询 `USGS`→`ISC`→`EMSC` 事件目录。

**脚本不做自然语言检索**：`--text` 只作为溯源信息记录下来，不参与查询。因此你必须先把
用户的话（地名/日期/震级）转换成时间窗、震中坐标或事件编号，再交给本脚本；
**若已用浏览器工具查到事件 ID，优先用 `--event-id`**（一次命中，最省时间）。

读 stdout JSON 的 `resolution` 字段，按下表行动：

| `resolution`        | `needs_user_input` | 你的动作                                                       |
| ------------------- | ------------------ | -------------------------------------------------------------- |
| `resolved`        | `false`          | 已唯一确定，直接用 `download_plan` 进入步骤 4                  |
| `resolved`        | `true`           | 脚本按震级自动挑出了主震，先把 `question` 念给用户确认        |
| `ambiguous`       | `true`           | 把 `question` 与候选列表 (`candidates`) 交给用户选择         |
| `not_found`       | `true`           | 按 `question` 让用户补充信息；或换一组关键词/时间窗重跑一次 |
| `insufficient_input` | `true`          | 按 `question` 向用户索取缺失的参数（时间/地区/事件 ID）      |

**你必须把 `question` 真正念给用户并等待回答**，不得自行假定；用户确认后再继续。

`event` 字段是权威参数（`event_id`、`origin_time`、`latitude`、`longitude`、
`depth_km`、`magnitude`、`place`），后续步骤一律以它为准。
`download_plan` 里已经给好时间窗、震中距、通道和推荐数据中心，**直接照抄即可**。

#### 步骤 2b：（可选）联网检索补充权威参数

脚本已经查过 FDSN 事件目录，通常不需要再联网。仅当出现下列情况时用浏览器工具补充：

| 平台                    | 用途                              |
| ----------------------- | --------------------------------- |
| USGS ComCat             | 全球 M4.5+，看 `eventid` 与目录 |
| ISC Bulletin            | 重新定位、参考深度                |
| EMSC (`seismicportal.eu`) | 欧地中海、实时事件              |
| GCMT                    | 震源机制、`Mw`                  |
| 中国地震台网 (CEIC)     | 中国及周边事件的官方参数          |

核实后把坐标/时间/震级**回填给 `p01_resolve_event.py`**（`--start --end --latitude
--longitude --min-magnitude`）重新解析一次，让脚本自己确定唯一事件，不要手工拼下载命令。

#### 步骤 3：选择合适的台网与数据中心

| 目标区域               | 推荐数据中心 (`--datacenter`，实测可用)          | 推荐台网代码                                           | 说明                                             |
| ---------------------- | -------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------ |
| **美国加州**     | `NCEDC,SCEDC,IRIS`                               | `BK`, `CI`, `NC`, `PG`, `SB`                     | NCEDC/SCEDC 台站与波形最全，首选               |
| **美国其他/Alaska** | `IRIS`                                           | `US`, `TA`, `AK`, `CN`                           | EarthScope 覆盖全国                            |
| **日本**         | `IRIS`, `GEONET`                                 | `JP`, `JU`, `VU`, `XJ`, `PS`                     | **Hi-net/K-NET 不开放 FDSN**；靠 IRIS 镜像的 F-net/JP |
| **台湾**         | `IRIS`, `GEONET`                                 | `TW`, `TA`, `TP`                                 | 中央气象局台网经 IRIS 分发                     |
| **中国大陆**     | `IRIS`                                           | `CB`, `KC`, `G`, `II`, `IC`                      | **CENC 的 FDSN 端口已失效**；大陆台站稀疏     |
| **欧洲/地中海**  | `EIDA`, `RESIF`, `IRIS`                          | `GE`, `GR`, `HT`, `IV`, `MN`, `CH`               | `GFZ`/`INGV` 端点当前不可达，用 EIDA/RESIF 兜底 |
| **新西兰**       | `GEONET`, `IRIS`                                 | `NZ`, `IU`                                       | GeoNet FDSN 可用                               |
| **南美**         | `IRIS`, `USP`                                    | `BL`, `IU`, `BR`                                 | 覆盖偏疏，需放大 `--max-radius`              |
| **全球通用备选** | `IRIS`（EarthScope 聚合）                         | `*` (所有可用)                                       | 兜底方案，速度较慢                             |

**台网选择策略：**

1. **优先选择目标区域的国家/区域台网**（数据延迟低、响应文件完整）
2. **补充全球台网 (IU/II/GE)** 用于远场记录和深度约束
3. **通道选择**：优先 `BH*` (宽频带, 20-100 Hz) > `HH*` (高增益, 100 Hz) > `EH*` (极高增益) > `SH*` (强震)
4. **避免选择**：临时实验台网 (如 `XA`, `XB` 等，除非用户明确要求)、已知噪声大的台站
5. `--datacenter` **支持逗号分隔的多数据中心**，脚本按顺序取"第一个声明该台站的数据中心"，
   不会重复下载同一通道。推荐的组合已在 `download_plan.datacenters` 里给好。

> **注意**：上面的可用性是对 2025 年实测的结论。`GEOFON`/`GFZ`/`INGV`/`ORFEUS`/`KNMI`/
> `NIEP`/`NIED`(`hinet.bosai.go.jp`, `fnet.bosai.go.jp`) 与 `data.earthquake.cn` 的 FDSN
> 端口当前均不可用；遇到时不要反复重试，直接换 `IRIS`/`EIDA`/`NCEDC`。
> 某个数据中心失败**不会**中断下载，最终 JSON 的 `request_failures` 会列出原因。

#### 步骤 4：执行下载命令

**直接执行 `download_plan.argv` / `download_plan.command`**（推荐），或按下面的模板自己拼：

```bash
python scripts/p01_fetch_waveforms.py \
  --network "*" --station "*" --channel "BH*,HH*" \
  --start "2024-01-01T12:33:00" --end "2024-01-01T12:40:00" \
  --latitude 37.5 --longitude 137.2 \
  --min-radius 0 --max-radius 500 \
  --datacenter IRIS,GEONET \
  --out data/raw --stations-out data/stations \
  --max-stations 60
```

**关键参数说明：**

- `--network` / `--station`：台网/台站代码或通配符；**按震中距选台时用 `"*"`**
- `--channel`：通道模式，推荐 `BH*,HH*` 覆盖宽频带和高增益
- `--start/--end`：UTC 时间，**必须来自 `download_plan`（已按事件时刻和模式算好）**
- `--latitude/--longitude`：震中坐标，**必须来自 `resolved_event`**
- `--min-radius/--max-radius`：**震中距范围，单位默认为 km**（`--radius-unit deg` 可切回
  FDSN 原生的"度"）。台站筛选在本地按 km 精确过滤，因此 `--max-radius 500` 就是 500 km。
- `--max-stations`：只保留最近的 N 个台站。**强烈建议设置**：`--max-radius 500` 且
  `--network "*"` 时很容易匹配到上千个通道，下载时间和磁盘都会失控。
  **注意"最近的"不等于"有数据的"**（高增益/加速度通道经常没归档）：若这 N 个台站返回
  空数据，脚本会自动放宽到 2N、4N 再试，最终 JSON 的 `max_stations_used` 会告诉你实际
  用到的是哪一档。
- `--datacenter`：逗号分隔的多个数据中心，按顺序回退（如 `NCEDC,SCEDC,IRIS`）
- `--station-level`：默认 `response`，**不要改**（Phase 2 去响应依赖它）
- `--location`：默认 `*`；需要限定空定位码时用 `--location ""`

**两种下载模式（互斥）：**

- **震中距模式（推荐）**：给 `--latitude/--longitude` + `--min-radius/--max-radius`，
  `--network "*"` 让脚本自己找台站；
- **模式匹配模式**：不给经纬度，用明确的 `--network`/`--station` 直接请求。

**仪器响应自动下载**：脚本使用 `level="response"` 调用 `get_stations()`，**自动下载完整的
StationXML（含仪器响应 Poles/Zeros/Gain/Sensitivity）**，保存为 `data/stations/stations.xml`，
并由它派生 `stations.csv`。Phase 2 的 `--remove-response` 直接读取此文件，**无需额外操作**。

#### 步骤 5：执行下载并验证

检查 stdout JSON：

- `status=="ok"` 且 `n_files>0`、`n_stations>0` → 成功，**同时产出 `data/stations/stations.xml`（含响应）和 `stations.csv`**
- `max_stations_used` 大于 `max_stations_requested`：最近的 N 个台站没有数据，脚本已自动
  放宽；把这个事实写进给用户的报告里。
- `n_stations_downloaded`（真正拿到波形的台站数）明显小于 `n_stations_requested`：
  单独看 `n_missing_channels` / `missing_channels`。少量缺失属正常（台站当时无数据、
  或数据未归档）；**缺失比例 >50% 时**按 troubleshooting 处理。
- `distance_km.min/max`：确认落在 `--min-radius`/`--max-radius` 之内；`max` 远小于
  `--max-radius` 说明该区域台站稀疏，需要扩大半径或换数据中心。
- `datacenter_stats` / `request_failures`：哪个数据中心给了多少台站、哪个失败了。
- 报错 `no station found inside the requested annulus`：扩大 `--max-radius`、放宽
  `--channel`（如 `BH*,HH*,EH*`）或换 `--datacenter` 后重试**一次**。
- 网络超时/连接失败：重试**一次**，再次失败后报告用户环境可能离线，建议切换场景 B。

**若 StationXML 下载失败**（stdout JSON 无 `stations_xml` 字段或 `n_stations=0`）：

- 直接报错，让用户决定后续动作。

### 场景 B：用户明确指出本地已有波形文件

```bash
python scripts/p01_fetch_waveforms.py --from-local <波形目录> \
  --out data/raw --stations-out data/stations
```

脚本会扫描 miniSEED/SAC 文件并自动提取台站元数据。

**成功判据**：stdout JSON `status=="ok"` 且 `n_files>0`、`n_stations>0`。

**错误处理**：

- `n_files_failed > 0`：stderr 的 `[WARN]` 会列出无法读取的文件；确认波形文件完整后重试一次。
- `n_files == 0`：目录里没有可识别的 miniSEED/SAC 文件，向用户确认路径。

**检查点 1**：确认 `data/raw/` 下文件数 = JSON `n_files` 且均 >0 字节、`data/stations/stations.xml`
与 `stations.csv` 均已生成后，报告用户，才进入 Phase 2。
场景 A 还需报告 `n_stations_downloaded`（实际拿到波形的台站数）与 `distance_km` 范围。

## Phase 2: 数据预处理

```bash
python scripts/p02_preprocess.py \
  --input data/raw --output data/preprocessed \
  --stations data/stations/stations.xml \
  --remove-response --detrend --demean \
  --filter "0.5-20" --resample 100
```

- `--filter` 格式为 `<低频>-<高频>` Hz 带通；拾取用默认 0.5–20 Hz 即可。
- `--resample` 单位 Hz；不要低于原采样率的一半，否则高频截止会被折叠进带内。
- `--detrend` / `--demean` 默认开启，需要保留原始趋势时用 `--no-detrend` / `--no-demean` 关闭。
- `--remove-response` 输出 `output="VEL"`（**地面速度 m/s**），这是 Phase 7 Wood-Anderson 仿真所需的输入域。缺少 StationXML 时该选项会**直接报错退出**，不会静默跳过。
- 若无仪器响应文件，去掉 `--remove-response`（此时震级计算 ML 将不可用，需在报告中注明），并给 Phase 7 加 `--no-stations`。

**成功判据**：`status=="ok"` 且 `n_traces_processed == n_traces_input - n_traces_failed`；
去响应时还应满足 `remove_response_requested==true`、`response_removed==true`、
`output_units=="M/S"`，且 `n_traces_response_removed == n_traces_processed`。

**错误处理**：`n_traces_failed > 0` 时查看 stderr 中 `[WARN]` 行列出的台站；
去响应失败的 trace 会被整条丢弃（不写入输出目录，因此输出单位永远是单一的），
失败比例 >10% 则停止并报告用户（可能是响应文件缺失或格式损坏）。

**耗时与性能**：脚本按操作统计耗时（`read mseed` / `detrend linear` / `demean` /
`remove response` / `bandpass filter` / `resample` / `write mseed`，同一操作多次
调用累加），写入 stderr 的 `Phase 2 内部步骤耗时: ...` 与 JSON 的 `timings`。
obspy 的 evalresp 是纯 Python 实现，单条 21000 采样点 trace 的去响应约 70 ms；
同一批台站在各事件文件里重复出现（Templeton 数据集 5002 条 trace 只有 461 条不同的
道），因此脚本把**已取倒数的频率响应按道缓存**（`build_cached_remover`，输出与
`Trace.remove_response` 逐采样点一致），`response_cache` 字段给出实际评估/复用条数。
若 stderr 出现 `cached response removal unavailable`，说明 obspy 内部结构变化，
脚本已自动退回 `Trace.remove_response`（慢约 10 倍但结果相同）。

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

## Phase 5: 绝对定位（纯 Python Geiger 迭代）

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

**成功判据**：`status=="ok"` 且 `n_located >= 0.5 * n_events_input`。

**错误处理**：定位成功率 <50% 时，检查拾取质量（Phase 3 的 `n_picks` 是否异常少）,
或询问用户是否提供更合适的近台速度模型（`references/velocity_model_guide.md`）。

**检查点 5**：确认 `data/locate/catalog_abs.csv` 非空后才进入 Phase 6。

---

## Phase 6: 相对定位（可选，精定位）

仅当满足以下全部条件才执行，否则直接跳到 Phase 7 并在报告中注明"未做相对定位"：

1. 用户明确要求精定位；
2. 事件数 ≥ 50；
3. Phase 0 显示 `backends.hypodd == true`（即已安装 relocDD-py）。

### relocDD-py（纯 Python 双差定位，安装阶段 git 安装）

```bash
python scripts/p06_run_hypodd.py \
  --catalog data/locate/catalog_abs.csv \
  --picks data/picks/phasenet_picks.csv \
  --stations data/stations/stations.csv \
  --output-dir data/locate/hypodd
```

流程：由绝对定位目录 + PhaseNet 拾取生成 phase.dat/station.dat → relocDD-py
`ph2dt`（目录差分走时）→ relocDD-py `hypoDD`（反演）→ 解析 hypoDD.reloc 输出 CSV。
全部由包装器完成，无需编译型 Fortran 二进制。调参见 `references/hypodd_workflow.md`。

> 注意：relocDD-py 的 ph2dt 仅基于目录拾取（phase.dat）构建差分走时，**不做波形互相关**，
> 因此无需波形文件。
>
> 依赖：relocDD-py 在安装阶段由 `packages/relocdd-py/install.py` 从 git 安装
> （上游无 `pyproject.toml`，见 `docs/install.md`），运行阶段不再联网。
> 若 `backends.hypodd == false`，跳过本阶段并在报告中注明"未做相对定位"。

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
- `pick_samples.png` Phase 3 拾取样例图：只画**一个**聚类事件（默认拾取数
  最多的那个，可用 `--event` 指定序号），每个拾取到该事件的台站一个子图，
  优先取该台垂直（Z）分量的波形（即 `--waveforms` 指向的预处理波形，与
  Phase 3 拾取所用数据一致），叠加 P（红）/S（蓝）拾取竖线。
  x 轴以该簇**最早拾取 t0** 为起点（不是震源时刻），窗口默认按事件跨度自动
  取（下限 30 s、上限 300 s），可用 `--window <秒>` 手动指定；窗口外的拾取
  不绘制，子图标题标注 `+N off-window`，报告也会给出未绘制的条数
- `report.md` 处理报告（结构见 `assets/report_template.md`）：概要、统计、
  **耗时表（各阶段 wall-clock 与占比 + 各阶段内部步骤明细）**、
  **逐事件目录与震级表**（每个事件的 ML、参与台站数/道数、拾取数、RMS 与质量分级）、
  单台震级质控表（单台 ML 离散度、震中距范围、方位角间隙）、数据完整性检查、
  输入数据来源与图件引用

**耗时（timing）**：每个阶段脚本都会在 stderr 打印自身各步骤耗时，JSON 里带
`elapsed_s`；一键流水线还会把每个阶段的耗时写进 `data/pipeline_logs/<阶段>.log`
末尾的 `---ELAPSED---` 段与 `data/pipeline_logs/timings.json`。P8 通过
`--timings <timings.json>`（默认在 `<output-dir>/../pipeline_logs/timings.json`
自动查找）把 P2–P7 的耗时与 P8 自身耗时合并成报告里的**耗时**表。
阶段脚本自己报的分步耗时（如 P2 的 `remove response`）会经 `timings.json` 的
`steps[].timings` 透传，P8 在耗时表下方展开成"各阶段内部步骤"明细行。
单独运行 P8（无 timings 文件）时不会报错，表内只列 P8 自身耗时。

**检查点 8**：确认 `data/qc/report.md` 存在且引用的五张 PNG 均存在后，向用户汇报：
事件总数、Mc、平均走时残差、使用的定位后端、各阶段耗时、以及任何 warning。

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

**逐阶段耗时**：每阶段结束都会在 stderr 打印 `=== Phase <名> 完成，耗时 <时长>`
并在结束时打印一行总耗时与各阶段明细；该耗时同时写入
`data/pipeline_logs/<阶段>.log`（末尾 `---ELAPSED---` 段）、
`data/pipeline_logs/timings.json`（含各阶段脚本自报的分步耗时）与顶层 JSON 的
`elapsed_s` / `phase_timings`，最后由 P8 汇总进 `report.md` 的"耗时"表。

**台站元数据**：`--stations-dir` 下必须有 `stations.xml`（StationXML）。
缺 XML 时脚本立即失败并给出提示——因为 P2 的 `--remove-response` 和 P7 的 ML
都依赖真实仪器响应，静默降级会产出"看起来正常但错 2–3 个震级单位"的结果。
`stations.csv` 若不存在，会由 `stations.xml` **自动派生**（无需手工维护）。
仅做拾取/定位、不需要震级时，可用 `--no-stations` 跳过响应去卷积与 ML
（仍需要 `stations.csv` 供关联/定位使用），此时 P7 上报 `magnitude_available=false`。

## 参考文档（按需阅读，勿一次性全读）

- 速度模型选择困难时 → `references/velocity_model_guide.md`
- relocDD-py 相对定位效果不佳时 → `references/hypodd_workflow.md`
- 任何脚本报错时 → 先查 `references/troubleshooting.md` 再决定重试策略
