# NonLinLoc 控制文件参数详解

包装器 `scripts/run_nll_locate.py` 会用模板 `assets/nll_control_template.conf`
自动生成控制文件，你通常**不需要**手写。需要调参时参考本文。

## 文件结构（SELECT 行序列）

```
CONTROL 54321 54321
TRANS  SIMPLE  lat0 lon0  0.0 / 111.19 0.0 / 0.0 -111.19
INCLUDE  <path/to/model>.tvel  (由 LOCSIG/模型行引用)
LOCPHASE ...
LOCSEARCH ...        # 搜索方法
LOCGRID ...          # 网格定义
LOCPHASEID  P   P p GPN
LOCQUAL2ERR  0.1 0.5 1.0 2.0 99999.9
LOCPHSTAT ...
LOCANGLES ...
LOCMETH ...          # 定位统计方法
LOCGAU ...           # 误差模型
LOCFILES ...         # 关键：观测文件/走时表/输出路径
```

## 关键参数速查

| 参数 | 默认 | 说明 | 何时调整 |
|------|------|------|---------|
| LOCSEARCH OCT | 网格八叉树 | 兼顾精度与速度 | 一般不改 |
| LOCGRID X/Y/Z | ±50 km, 深 0–40 km | 搜索范围 | 事件远离台网时扩大 |
| LOCPHSTAT RMS | 残差统计 | — | 不改 |
| LOCGAU 0.1 | 拾取误差 σ=0.1 s | 与 PhaseNet 精度匹配 | 手动拾取可设 0.05 |
| LOCQUAL2ERR | 权重→秒映射 | — | 不改 |

## 常见问题

- **`ERROR: no travel-time grid`**：走时表未生成或路径错误。
  包装器已处理；若手动运行需先跑 `Grid2Time`。
- **深度贴边（=0 或 =max）**：扩大 Z 范围或检查速度模型浅层。
- **RMS 异常大（>2 s）**：多半是速度模型不合适，见
  `velocity_model_guide.md`。

## 输出解析

`.hyp` 文件中 `HYPOCENTER` 行包含 Lat/Lon/Z/RMS/Gap；
包装器 `parse_hyp()` 已将其转为 CSV，无需手工读取。
