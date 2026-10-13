# relocDD-py 相对定位完整操作流程

## 概述

relocDD-py（https://github.com/katie-biegel/relocDD-py，GPL-3.0，作者 Katherine Biegel）
是 HypoDD 的**纯 Python 移植版**，无需编译型 Fortran 二进制。
安装阶段由 `packages/relocdd-py/install.py` 从 git 安装（上游没有 `pyproject.toml`，
仓库补了一份，见 `docs/install.md`），安装后运行不需要联网；
依赖 numpy/scipy/matplotlib/obspy/utm。

双差定位分两步：
1. **ph2dt**：基于目录重合拾取（phase.dat）生成事件对差分走时 `dte.ct`
2. **hypoDD**：用差分走时反演事件相对位置，输出 `hypoDD.reloc`

> 注意：relocDD-py 的 ph2dt **只做目录差分走时，不做波形互相关**，因此无需波形文件。

包装器 `scripts/p06_run_hypodd.py` 已封装全流程；本文供调参时参考。

## 前置条件

- 绝对定位目录（Phase 5 输出，`catalog_abs.csv`）作为初始位置
- PhaseNet 拾取目录（`phasenet_picks.csv`）用于构建 phase.dat
- 台站元数据（`stations.csv`）
- 事件数 ≥ 50（太少时事件对不足，收敛困难）

## 数据流

```
catalog_abs.csv + phasenet_picks.csv ──► phase.dat（事件行 + 震相行）
stations.csv ──► station.dat
relocdd_ph2dt_inp_template.txt ──► ph2dt.inp
relocdd_hypodd_inp_template.txt ──► hypoDD.inp
ph2dt() ──► dte.ct ──(重命名为)──► dt.ct
hypoDD() ──► hypoDD.reloc ──► catalog_dd.csv
```

### catalog_dd.csv 格式

包装器输出**整个输入目录**（不是只输出重定位成功的事件），列与 `catalog_abs.csv` 一致
（`event_id, time, latitude, longitude, depth_km, magnitude, magnitude_type, n_picks, rms_s`），
并追加一列 `relocation`：

- `hypoDD`：该事件的经纬度/深度已替换为 `hypoDD.reloc` 的双差解
- `absolute`：ph2dt 未能把它与任何事件配对，保留绝对定位解

JSON 中 `n_relocated` 只统计前者，`n_events_output` 为总行数。必须保留全量的原因是
Phase 7/8 把 catalog_dd.csv 当作最终目录，需要 `time`、`magnitude_type` 等只有绝对目录
才有的列；只写重定位事件会让震级计算因缺少 `time` 列直接失败。

### phase.dat 格式

事件行：`# year month day hour minute sec lat lon depth mag herr verr res cuspid`
- `sec` 为 `SSff`（整数秒 + 2 位小数，无小数点），如 `33.65` 写作 `3365`
- `cuspid` 必须为数值（去掉 `ev` 前缀并去除前导 0）

震相行：`station travel_time weight phase`
- `travel_time` 为**相对发震时刻的走时**（`(t_pick − t_origin).total_seconds()`，秒），
  **不是**当日秒数。ph2dt 读入后直接当作走时使用（`p_time = float(pha[1])`，不做任何
  时间扣除），其离群检验 `abs(Δt_obs − Δt_pred) ≤ aoffs/vel + 0.5`（vel：P=4.0、S=2.3 km/s）
  比较的必须是走时；若写入钟面秒数（0–86400 s），任意两事件都会判为离群，`outliers` 接近 100%，
  `linked event pairs = 0`
- `phase` 为 `P` 或 `S`
- 同一事件同一台站每个震相只允许一行；包装器对重复拾取取离发震时刻最近者，并丢弃不在 station.dat 中的台站（ph2dt 按 `2 × 台站数` 预分配每个事件的震相数组，超出行数会触发 `index N is out of bounds`）

### station.dat 格式

`station_code lat lon [elev_km]`，台站代码优先取与 station.dat 中台站匹配的那一段（`AK.CCB.` → `CCB`）；本地震相文件常带位置码（`IU.COLA.00`），此时**不会**把尾部的 `00` 当作台站代码。

## 关键参数（relocdd_ph2dt_inp_template.txt）

```
MINWGHT 0        # 最小数据权重
MAXDIST 200      # 事件对质心-台站最大距离 km
MAXSEPE 10.0     # 事件对最大间距 km（核心参数）
MINSEPS 2
MAXNGH 10        # 最大邻居数
MINLNK 8         # 事件对最少连接数（核心参数）
MINOBS 8         # 事件对最少共享数据
MAXOBS 30        # 事件对最多共享数据
```

### 调参建议

| 症状 | 调整 |
|------|------|
| 重定位事件数远少于输入 | 降低 MINLNK 至 6，或增大 MAXSEPE |
| 事件对过少、反演失败 | 增大 MAXDIST；检查 phase.dat 台站/震相是否匹配 |
| 收敛失败 | 检查是否有离群事件；检查速度模型（hypoDD.inp 的 VEL/RATIO） |
| `ph2dt linked no event pairs`（`outliers` 接近 100%，`linked event pairs = 0`） | 先排查 phase.dat：震相列必须是**相对发震时刻的走时**（历史上包装器误写当日秒数，已修复，见上文格式说明）。确认走时正确后仍为 0，则属**参数无法解决**的情形：事件两两之间在空间**或发震时刻**上不聚集时全部差分走时被判为离群，没有任何 MINLNK/MINOBS/MAXSEPE 组合能造出事件对。用时间窗更短、空间更聚焦的资料重跑，或保留绝对定位目录 |

## 结果验证

1. 对比重定位前后深度分布：双差结果应更集中、呈层状分布。
2. 相对位置变化应 < 2–3 km；个别大幅漂移的事件需人工检查。
3. 用 `qc_and_plot.py` 的残差图确认定位改善。

## 安装与离线运行说明

relocDD-py 在安装阶段从 git 安装，源码不再内嵌到 `third_party/`：上游仓库没有
`setup.py`/`pyproject.toml`，`pip install git+https://github.com/katie-biegel/relocDD-py.git`
会直接报 `does not appear to be a Python project`，因此由
`packages/relocdd-py/install.py` 把仓库内手写的 `pyproject.toml` 补进 checkout
（默认 `seismic-workflow/.cache/relocdd-py`，固定在 commit `323071a`）后 `pip install -e`：

```bash
python seismic-workflow/packages/relocdd-py/install.py
```

安装后 `import ph2dt` / `import hypoDD` 在任何工作目录下均可用，运行阶段不再需要联网。
缺少依赖时补装即可：

```bash
pip install numpy scipy matplotlib obspy utm
```

`p00_detect_env.py` 通过 `importlib.util.find_spec("ph2dt")` 与
`importlib.util.find_spec("hypoDD")` 判断 `backends.hypodd` 是否可用。

> **本地补丁**：`p06_run_hypodd.py` 在导入前会就地修补已安装的 relocDD-py 源码（幂等，
> 日志出现 `[WARN] patched installed relocDD-py: <文件>`），共两类：
> 1. NumPy ≥ 2 不再允许 `int(tuple(np.argwhere(...))[0])` 这类 1 元素数组到标量的隐式转换
>    （抛 `TypeError: only 0-dimensional arrays can be converted to Python scalars`）。
>    上游 `hypoDD_ev.py` / `hypoDD_stat.py` / `hypoDD_doub.py` 读差分走时文件时用的正是该写法，
>    且被裸 `except` 吞掉，表现为 hypoDD 读到 0 条目录差分走时；改为
>    `int(np.argwhere(...).flat[0])`。
> 2. `ph2dt_ev.py` 中 `avoff/npair` 等除零（`npair == 0`，即目录里没有关联事件对）改为返回 0。
>
> 重新运行 `install.py` 拉取上游新代码后，这些补丁会在下次运行包装器时自动重新应用。
