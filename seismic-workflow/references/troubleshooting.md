# 常见报错排查手册

按阶段组织。每条给出：现象 → 原因 → 处置。

## 通用

### stdout 不是合法 JSON
- **原因**：Python 导入错误（traceback 打到 stderr 之外的情况极少）或脚本崩溃。
- **处置**：查看 stderr 尾部；若为 `ModuleNotFoundError`，按 `docs/install.md` 安装依赖后重试一次；仍失败则报告用户。

### 权重/数据文件下载失败
- **关键词**：`download`、`ConnectionError`、`timeout`
- **处置**：环境离线。告知用户从有网机器拷贝 `~/.cache/seisbench/`（模型权重）。
  不要反复重试网络请求。

## Phase 1 数据获取

### P1a 事件解析（`p01_resolve_event.py`）

| JSON 现象 | 原因 | 处置 |
|------|------|------|
| `resolution="insufficient_input"` | 用户没给出可定位事件的最小信息 | 把 `question` 念给用户，等回答后带上参数重跑 |
| `resolution="not_found"` | 关键词在该目录里查不到 | 换关键词（地名用英文/拼音、加上年份或震级）重跑一次；仍无则让用户给事件 ID |
| `resolution="ambiguous"` + `needs_user_input=true` | 命中多个事件 | 把 `question` 与 `candidates` 交给用户选择，不要自己挑 |
| `resolution="resolved"` 但 `needs_user_input=true` | 脚本按震级自动挑了主震 | 把 `question` 念给用户确认后再下载 |
| `某目录: event query failed (ValueError: Could not parse ...)` | ISC/EMSC 的按 ID 查询返回非 QuakeML | 脚本已自动回退到时间窗查询；若仍失败，用 `--event-datacenter USGS` 或改用 `--text` |
| `no event matches the query` | 参数组合过严 | 放宽 `--min-magnitude`、扩大 `--within-km` 或时间范围 |
| 中文/`→` 输出乱码 | Windows 控制台默认 GBK | `common.py` 已强制 UTF-8；若仍乱码按 UTF-8 读取 stdout |

### P1b 波形下载（`p01_fetch_waveforms.py`）

| 报错/现象 | 原因 | 处置 |
|------|------|------|
| `no station found inside the requested annulus` | 该震中距范围内没有符合 `--channel` 的台站，或数据中心不覆盖该区域 | 按 JSON `hint` 扩大 `--max-radius`、放宽 `--channel`（`BH*,HH*,EH*`）或换 `--datacenter`，重试一次 |
| 报错/现象 | 原因 | 处置 |
|------|------|------|
| `unrecognized arguments: --min-radius` | 脚本版本过旧（旧版 `p01_fetch_waveforms.py` 没有震中距参数） | 用仓库里当前的脚本，不要用拷贝出去的旧副本；`--help` 里应能看到 `epicentral radius selection` 参数组 |
| `--max-radius must be larger than --min-radius` | 参数写反或单位搞错 | 检查是否把 km 当成了度：`--radius-unit deg` 时数值要按度给 |
| `max_stations_used > max_stations_requested` | 最近的 N 个台站没有归档数据（高增益/加速度通道常见） | 属正常自动放宽；把实际用了多少台站写进给用户的报告 |
| `FDSN returned an empty stream` 且 `hint` 提到 `--max-stations` | 限制台站数后所有档位都无数据 | 提高或去掉 `--max-stations`，或放宽 `--channel` / 换 `--datacenter` |
| `ObsPyDeprecationWarning: IRIS` | `IRIS` 是旧别名 | 无害；新名称为 `EARTHSCOPE`，可改用后者消除告警 |
| `FDSN returned an empty stream` | 查询成功但没有波形 | 看 `request_failures`；缩小时间窗/台站模式，或换数据中心重试一次 |
| `n_stations` 很大 / 下载极慢 | `--network "*"` + 大半径匹配到上千通道 | 加 `--max-stations 60`（保留最近台站），或用更具体的 `--channel` |
| `n_missing_channels` 占多数 | 台站有元数据但当时无数据（触发式记录、未归档） | 少量缺失正常；>50% 时换数据中心（如加州加 `NCEDC,SCEDC`）或缩小时段 |
| `n_traces_with_response` 为 0 | StationXML 与波形不匹配 | 检查 `--station-level` 是否为 `response`；确认 `stations.xml` 覆盖下载的台站 |
| `xxx: cannot initialise FDSN client` | 该数据中心临时故障或端点已失效 | 脚本对客户端初始化自动重试 3 次；仍失败就换数据中心（见 SKILL.md 数据中心表） |
| `HTTPError 503` / 超时 | 数据中心限流 | 等待后重试一次；仍失败换数据中心，或加 `--timeout 300` |
| `GEOFON`/`GFZ`/`INGV`/`ORFEUS`/`KNMI`/`NIED`/`CENC` 一直失败 | 这些 FDSN 端点当前不可用（实测：`data.earthquake.cn`、`hinet.bosai.go.jp`、`fnet.bosai.go.jp` 全部 404） | 不要反复重试，直接改用 `IRIS`(EarthScope) / `EIDA` / `NCEDC` / `SCEDC` / `RESIF` / `GEONET` |
| 日本事件只能下到十几个台站 | Hi-net/K-NET (NIED) 不提供公开 FDSN | 属预期；靠 IRIS 上的 `JP`/`PS`/`G` 等台网，必要时扩大 `--max-radius` |
| 中国大陆事件台站极少 | CENC FDSN 端口失效，IRIS 上大陆台站稀疏 | 属预期；扩大 `--max-radius`，或改用区域研究目标/本地数据（场景 B） |
| 本地扫描 `no waveform files` | 目录/扩展名不匹配 | 检查 `--from-local` 路径；支持 mseed/sac |

## Phase 2 预处理

| 报错 | 原因 | 处置 |
|------|------|------|
| `response removal failed` | StationXML 与波形不匹配 | 保留 StationXML 排查台网/台站/通道是否覆盖；确无响应则去掉 `--remove-response` 重跑（注明 ML 不可用）|
| `station response file not found` | 缺少 `data/stations/stations.xml` | 用 P1 重新下载（`level="response"`），或手工放入数据中心下载的 StationXML；跳过 ML 时才用 `--no-stations` |
| 大量 trace failed (>10%) | 数据损坏或采样率异常 | 报告用户，附 stderr 中 [WARN] 台站列表 |
| `output_units=MIXED` | 仅部分 trace 去响应成功 | 说明响应覆盖不全，震级会不可靠；查 stderr 台站列表后补齐 StationXML 重跑 |

## Phase 3 拾取

| 报错 | 原因 | 处置 |
|------|------|------|
| `model weight load/download failed` | 离线无权重缓存 | 拷贝 seisbench 缓存后重试一次 |
| `n_picks == 0` | 滤波不当或信噪比低 | 用 `--filter "1-15"` 重跑 P2 再拾取一次 |
| CUDA out of memory | GPU 显存不足 | 改用 CPU（`--device cpu`）或减小 batch-size |

## Phase 4 关联

| 报错 | 原因 | 处置 |
|------|------|------|
| `GaMMA unavailable` | 未安装 gmma | 按 docs/install.md 安装；不要跳过 |
| `n_events == 0` | 台站稀疏/阈值过严 | config 中 `min_picks_per_eq: 6→5` 重试一次 |
| `unknown stations` 警告多 | stations.csv 与拾取台站不一致 | 检查台站表覆盖 |
| 同一输入两次运行事件数与拾取归属不同 | GaMMA 除 DBSCAN 外还含随机初始化（BFGS 初值），本身非确定性 | 属正常，不要依赖单次事件表；报告里注明。需可复现时固定随机种子（视 gmma 版本是否提供该选项）|
| 事件数明显多于原始数据段数（如 11 段波形 → 14 事件），并出现只有 3–5 个拾取的小事件 | `min_picks_per_eq: 3` / `min_stations: 3` 过松，噪声拾取自成一类 | 已修复默认值：`min_picks_per_eq: 6` / `min_stations: 4`。3–5 个拾取无法约束自由震源参数，这类事件在 P5 会定位失败或落到远端垃圾位置。本资料由 14→11 事件（与 11 段波形一致）|
| 事件深度全部等于 40.0 km（`bfgs_bounds[2]` 上界）| 台站距震中普遍 >250 km，深度与发震时刻近似退化，目标函数沿该方向近平坦，解被推到边界 | 不要据此判断震源深度；深度以 P5 绝对定位为准，并在报告中说明深度约束弱 |

> `bfgs_bounds[3]`（发震时刻）的单位是**秒**：GaMMA 在 `utils.py` 内先做
> `timestamp0 = min(t); t = t - timestamp0`，输出时再加回。不要按「微秒」或「日秒」处理。

## Phase 5 定位

| 报错 | 原因 | 处置 |
|------|------|------|
| `NLLoc binary not found` | 无编译型程序 | 使用回退：`p05_grid_locate.py`（Geiger 迭代） |
| 定位成功率 <50% | 拾取质量差或速度模型差 | 查 P3 的 n_picks；参考 velocity_model_guide.md |
| `rms_s` 普遍 10–70 s，但典型残差只有约 1 s | 旧实现只导出 `assoc/events.csv`，GaMMA 的**逐拾取归属被丢弃**，P5 只能用 ±60 s/+120 s 时间窗重新圈拾取，同一拾取被多个相邻事件重复使用（938 条 (事件,拾取) 对 vs 644 个拾取），每个事件都被塞进不属于它的远距震相 | 已修复：P4 增写 `assoc/assignments.csv`（`pick_index,event_id`），P5 存在该文件时按归属取拾取，缺失则回退时间窗并告警。自检：`assignments.csv` 行数 ≲ 拾取数；本资料 `rms_s` 中位数 29.3 s → 1.2 s，参与拾取 938 → 399 |
| `rms_s` 单点极大（数十秒）但事件本身定位正常 | `rms_s` 是平方和开方，对少量离群拾取极敏感 | 不要用 `rms_s` 单独判定拟合优劣；改用 MAD 标度的中位残差（`1.4826·median|r|`）或以 `n_picks` 加权比较 |
| **结果整体偏离真实震中数十公里（约 50–90 km），解一律停在搜索框的同一个角（多为西北角）、深度贴边界** | **`_station_code()` 取的是 SEED 名称的最后一个字段，但名称是 `NET.STA.LOC` 顺序，于是返回的是位置码：`BK.PKD.00` → `00`。全网 36 个 `.00` 台站塌缩成同一个键，`setdefault` 只保留第一个（`BK.BCCR.00`，距震中约 560 km），大量拾取被赋上错误坐标 → 走时几何完全错误。表现为台站间距中位数异常大（289 km）、搜索框被推向单侧边界** | 已修复：`_station_code()` 取第二个字段（`NET.STA.LOC` 的 STA），并优先使用 `stations.csv` 的 `station_code` 列。自检：修复后台站码无碰撞且与拾取台站一一对应（本资料 157 台站 / 137 个被拾取台站 / 0 未匹配，修复前塌缩成 89 个码）。定标中位误差由 56 km 降至约 2 km |
| `travel_times()` 静默退化为常速度直射线走时 | 该函数按「每台站一个标量」使用，却按 1 元素 numpy 数组切片距离，`obspy.taup` 每次抛异常 → 落入 `except` 用 `hypot(dist, depth)/v` 估算；而 `iasp91.tvel` 顶部 `vp=5.8 / vs=3.36` 在 289 km 处比 IASP91 高估 P 走时约 14 s | 已修复：`travel_times()` 改为标量进出、显式包含区域震相（`Pn/Pg/Sn/Sg`），并预生成 IASP91 的 (depth, distance) 网格插值表（`seismic-workflow/.cache/iasp91_tt_table.npz`，深度用二次插值以保证深度导数光滑）；表外自动回退到实时 TauP |
| 解被拉到搜索框边界、真实震源处的残差比迭代解更小 | 模型选择用的 `rms_sel` 是**仅含震相权重的平方误差 RMS**，而这类资料约 1/3 的拾取属于其它震相/事件（残差数十秒），平方项被它们主导；`_ROBUST_AFTER = 1` 又让第一次 Gauss–Newton 迭代在无稳健权重时迈出离群驱动的一大步 | 已修复：模型选择改用 MAD 标度的中位残差 `1.4826·median|r|`（对 <50% 离群免疫，且不会因多剔除而虚降），`_ROBUST_AFTER` 改为 0。自检：真实震源处的目标函数值应低于迭代解，否则目标函数仍被离群值误导 |
| 深度系统性偏大（真值 4–7 km，解 11–17 km）| 最近台站也在 36 km 外、仅 3 个台站 <50 km，dT/dh 在 289 km 处仅约 0.007 s/km，深度与发震时刻近似退化 | 属分辨力限制而非程序错误；不要用远台资料报浅源深度，报告中注明深度约束弱、需近台资料 |
| 深度全部贴边界（旧记录）| 迭代未收敛或模型浅层偏差 | 先按上面第 3 行核对台站码映射是否正确，再检查速度模型；确认初值框（GaMMA 位置 ±0.5°）合理 |

## Phase 6 相对定位

| 报错 | 原因 | 处置 |
|------|------|------|
| `relocDD-py is not installed` | relocDD-py 未安装（上游无 `setup.py`/`pyproject.toml`，不能用 `pip install git+...` 直装） | `python seismic-workflow/packages/relocdd-py/install.py`（补入仓库内的 `pyproject.toml` 后 `pip install -e`，见 `docs/install.md`） |
| `failed to import relocDD-py` | 依赖缺失 | `pip install numpy scipy matplotlib obspy utm` |
| `index N is out of bounds for axis 1 with size M` | 单个事件的 phase 行数超过 ph2dt 的 `MAXOBS = 2 × 台站数` 预分配 | 包装器已按 (台站, 震相) 去重并剔除未列出台站，正常不应再出现；若出现请检查 phase.dat 是否有重复台站 |
| `status=warning: ph2dt linked no event pairs`（`outliers` 接近 100%） | ① 历史原因（已修复）：phase.dat 震相列写成**当日秒数**而非相对发震时刻的走时，ph2dt 的走时合理性检验实际在比较两个钟面时刻，任意事件对 100% 判为离群；② 现存原因：事件确实在空间**或发震时刻**上不聚集，或拾取质量差 | ① 确认包装器写的是 `t_pick − t_origin`（`write_phase_dat()`）；② 属参数无法解决的情形：调低 `MINLNK`／调高 `MAXSEPE` 无法创造被走时检验剔除的配对，用窗口更短、空间更聚焦的资料重跑，或保留绝对定位目录 |
| `relocDD-py hypoDD failed: cannot access local variable 'src_x' ...`（`hypodd.log` 中 `# Catalog P dtimes = 0`、`NCTP: 0`、`Final No. of Data: 0`） | 上游 `readcts_evpair()` 用 `int(tuple(np.argwhere(icusp==ic))[0])` 取事件下标；NumPy ≥ 2 对该 1 元素数组抛 `TypeError`，被裸 `except` 吞掉后**每个事件对都被跳过**，hypoDD 读到 0 条差分走时，聚类阶段 `src_x` 未赋值即被使用 | 包装器启动时会自动就地修补已安装源码为 `int(np.argwhere(...).flat[0])`（日志出现 `[WARN] patched installed relocDD-py: ...`）。若仍报此错，检查 checkout 是否只读或上游写法已变：`grep -rn "tuple(np.argwhere" seismic-workflow/.cache/relocdd-py` 应无输出 |
| `relocDD-py ph2dt failed: ...` （其它异常） | phase.dat/station.dat 不一致 | 查 `data/locate/hypodd/ph2dt.log`；确认台站代码可匹配 |
| `relocDD-py hypoDD failed` | 事件对太少或离群 | 调低 `MINLNK` 或调高 `MAXSEPE` 重试一次；仍失败保留绝对定位 |

> `npair=0` 时上游 `ph2dt_ev.py` 会做 `avoff/npair` 除零；除包装器已把该情况转为
> `status=warning` 并跳过反演外，包装器还会在导入前把该守卫直接写回已安装的源码。
> 两类补丁（`ph2dt_ev.py` 的 `avoff` 除零守卫、`methodTypes/{eventPair,stationPair,doublePair}`
> 下的 NumPy ≥ 2 标量转换兼容）都在 `p06_run_hypodd.py` 中定义（`_PATCHES`），
> 由 `_patch_installed_sources()` 幂等地应用到 `import` 到的 relocDD-py 源码上
> （日志出现 `[WARN] patched installed relocDD-py: <文件>`）。
> 因此重新运行 `packages/relocdd-py/install.py` 拉取上游代码后无需手工处理。

## Phase 7 震级

| 报错 | 原因 | 处置 |
|------|------|------|
| `magnitude_available=false` | 未去响应 / 未提供 StationXML | 在报告中注明；继续 P8 |
| `response removal failed` | 波形已去响应但再次传入 | 提供原始波形重跑 |
| ML 与公开震级差 2 级以上 | `--assume-units` 与上游输出单位不一致（miniSEED 不保存 units）| 确认 P2 为 `output="VEL"` 时传 `VEL`；用 `catalog_final.magnitude_diag.csv` 核对单台震级与震中距 |
| ML 系统性偏差 ~0.1–0.3 级 | WA 增益/灵敏度或振幅约定（零到峰 vs 峰到峰/2）被改动 | 运行 `python tests/verify_ml.py` 定位；常数应与 ObsPy `WOODANDERSON` 一致 |
| `KeyError: 'time'`（`--catalog` 指向 Phase 6 的 `catalog_dd.csv`） | 历史原因（已修复）：包装器只把 `hypoDD.reloc` 的 4 列写成 catalog_dd.csv（`event_id,latitude,longitude,depth_km,rms_s`），缺 `time`/`magnitude_type` 等列 | 包装器现在输出整个输入目录的列（`time` 等保留原值，仅替换重定位事件的经纬度/深度，并附 `relocation` 列）。若仍缺列，检查 `p06_run_hypodd.py` 的 Step 4 是否被改动 |

## Phase 8 制图

| 报错 | 原因 | 处置 |
|------|------|------|
| PyGMT import 失败 | 系统 GMT 缺失 | 自动回退 Matplotlib/Cartopy，无需干预 |
| 图件空白 | 目录为空 | 回查上游阶段 status |

## 上报格式

向用户报告错误时使用：
```
[Phase X] <脚本名> 失败
错误：<JSON error 字段>
已尝试：<重试动作>
建议：<下一步>
```
