# 地震目录处理报告

> 本模板描述 `scripts/p08_qc_and_plot.py` 中 `_write_report()` 生成 `report.md`
> 的结构与字段含义，供实现与评审对照使用（`{{...}}` 为该脚本填充的运行时值）。

生成时间: {{GENERATED_AT}}

## 概要

- **事件总数**: {{N_EVENTS}}
- **台站数**: {{N_STATIONS}}（{{N_NETWORKS}} 个台网）
- **有震级的事件数**: {{N_WITH_MAG}} ({{PCT_MAG}}%)
- **目录时间范围**: {{TIME_MIN}} – {{TIME_MAX}} UTC
- **制图后端**: {{MAP_BACKEND}}（`pygmt` / `cartopy` / `matplotlib`）
- **底图图层**: {{MAP_LAYERS}}
- **定位后端备注**: {{LOC_BACKEND}}

## 统计

- 深度: 中位 {{MED_DEPTH}} km，范围 {{MIN_DEPTH}} – {{MAX_DEPTH}} km
- 震级 (ML): 范围 {{MAG_MIN}} – {{MAG_MAX}}，中位 {{MED_MAG}}，均值 {{MEAN_MAG}}
- 完整性震级 Mc: {{MC}}（b 值 {{B_VALUE}}）
- 走时残差 RMS: 均值 / 中位 / 最大（目录 `rms_s`，反映拾取绝对时刻离散水平）
- 单事件平均: 台站 / 参与道 / 拾取
- 单台 ML 异常剔除: {{N_REJECTED}} 条道（偏离中位 0.5 级以上）

## 事件目录与震级

{{EVENT_TABLE}}

- 表头：`事件 | 发震时刻 (UTC) | 纬度 | 经度 | 深度 (km) | ML | 台站数 | 参与道数 | 拾取数 | RMS (s) | 质量`
- `台站数`、`参与道数` 取自 p07 单台震级诊断文件
  `<catalog>.magnitude_diag.csv`，无诊断记录时回退为目录中的 `n_stations_used`
  （后者统计保留道数，可能大于台站数）
- 质量分级：台站数 ≥ 8 且方位角间隙 ≤ 180° → 好；台站数 ≥ 5 → 一般；
  台站数 < 5 或方位角间隙 > 270° → 待查；目录缺震级 → 无震级

## 单台震级质控

{{ML_QC_TABLE}}

- 表头：`事件 | 单台 ML 范围 | 单台 ML 中位 | 单台 ML 标准差 | 剔除道数 | 震中距 (km) | 方位角间隙 (°)`
- 单台 ML 范围保留全部逐道结果；中位/标准差为剔除偏离中位 0.5 级以上的道后的结果，
  与 p07 最终 ML 的口径一致
- 方位角间隙由台站经纬度计算，仅统计能在台站表中匹配到坐标的台站
- 未找到诊断文件时本节输出提示语并跳过

## 数据完整性检查

- 逐项列出 `✅` / `⚠️`：`event_id` 唯一、经纬度/深度/震级完整、坐标范围合理、
  发震时刻无重复、参与台站 ≥ 3

## 输入数据

- 事件目录 / 台站表 / 拾取文件（含 P、S 条数）/ 波形目录 / 震级诊断文件路径与规模

## 图件

![震中分布](epicenter_map.png)
![G-R关系](gr_relation.png)
![深度分布](depth_histogram.png)
![走时残差](residuals.png)
![拾取样例](pick_samples.png)
