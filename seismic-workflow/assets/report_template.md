# 地震目录处理报告

> 本模板由 `scripts/qc_and_plot.py` 自动填充生成。

## 概要

- **事件总数**: {{N_EVENTS}}
- **台站数**: {{N_STATIONS}}
- **有震级的事件数**: {{N_WITH_MAG}} ({{PCT_MAG}}%)
- **制图后端**: {{MAP_BACKEND}}
- **定位后端**: {{LOC_BACKEND}}

## 统计

- 中位深度: {{MED_DEPTH}} km
- 震级范围: {{MAG_MIN}} – {{MAG_MAX}}
- 完整性震级 Mc: {{MC}}
- b 值: {{B_VALUE}}
- 平均走时残差: {{MEAN_RMS}} s

## 图件

![震中分布](epicenter_map.png)
![G-R关系](gr_relation.png)
![深度分布](depth_histogram.png)
![走时残差](residuals.png)

## 注意事项

- 若未做仪器响应去除，ML 震级不可用（见 Phase 2/7 说明）。
- 纯 Python 定位后端深度误差典型为 2–5 km；如需亚公里级精度请使用 HypoDD。
- {{EXTRA_NOTES}}
