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

| 报错 | 原因 | 处置 |
|------|------|------|
| `FDSN request failed ... no data` | 时间窗/台站组合无数据 | 缩小范围重试一次 |
| `HTTPError 503` | 数据中心限流 | 等待后重试一次，仍失败换数据中心 |
| 本地扫描 `no waveform files` | 目录/扩展名不匹配 | 检查 `--from-local` 路径；支持 mseed/sac |

## Phase 2 预处理

| 报错 | 原因 | 处置 |
|------|------|------|
| `response removal failed` | StationXML 与波形不匹配 | 去掉 `--remove-response` 重跑（注明 ML 不可用）|
| 大量 trace failed (>10%) | 数据损坏或采样率异常 | 报告用户，附 stderr 中 [WARN] 台站列表 |

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
| `n_events == 0` | 台站稀疏/阈值过严 | config 中 `min_picks_per_eq: 8→5` 重试一次 |
| `unknown stations` 警告多 | stations.csv 与拾取台站不一致 | 检查台站表覆盖 |

## Phase 5 定位

| 报错 | 原因 | 处置 |
|------|------|------|
| `NLLoc binary not found` | 无编译型程序 | 使用回退：`grid_locate.py` |
| 定位成功率 <50% | 拾取质量差或速度模型差 | 查 P3 的 n_picks；参考 velocity_model_guide.md |
| 深度全部贴边界 | 搜索范围不足或模型浅层偏差 | 扩大深度范围；检查速度模型 |

## Phase 6 相对定位

| 报错 | 原因 | 处置 |
|------|------|------|
| `ph2dt/hypoDD binaries not found` | 无 Fortran 二进制 | 使用 `simple_dd.py` 或跳过 P6 |
| `ph2dt produced no dt.ct` | 波形命名/元数据不匹配 | 检查台站表与波形 ID 一致性 |
| `lsqr did not converge` | 事件对太少或离群 | `--min-link 8` 重试一次；仍失败保留绝对定位 |

## Phase 7 震级

| 报错 | 原因 | 处置 |
|------|------|------|
| `magnitude_available=false` | 未去响应 | 在报告中注明；继续 P8 |
| `response removal failed` | 波形已去响应但再次传入 | 提供原始波形重跑 |

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
