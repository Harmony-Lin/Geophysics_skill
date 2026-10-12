# 基于 obs.taup + scipy 搭建标准 Geiger 地震定位器：方法论详解

---

## 1. 概述

地震绝对定位的目标是确定一次地震的四个发震参数：**发震时刻** $t_0$、**经度** $\lambda$、**纬度** $\phi$ 和**震源深度** $z$。自 Geiger（1910）提出以来，基于走时的迭代线性反演一直是绝对定位的"标准方法"，至今仍是 HYPOINVERSE、VELEST、NonLinLoc 等商业/学术程序的核心算法。

本文档从方法论角度，系统阐述如何用 Python 生态中的两个核心库——**obs.taup**（走时正演引擎）和 **scipy.optimize**（非线性最小二乘求解器）——搭建一个完整的 Geiger 定位器。整个流程可以概括为：

> **数据 → 预处理 → 震相拾取 → 震相关联 → obs.taup 计算理论走时 → 构建走时残差 → scipy 最小二乘迭代 → 绝对定位结果**

---

## 2. 正演问题：理论走时计算

### 2.1 物理基础

在 1D 球对称速度模型假设下，地震波走时可以通过射线理论精确计算。射线参数 $p$ 沿路径守恒：

$$
p = \frac{r \sin i}{v(r)}
$$

其中 $r$ 为地球半径、$i$ 为出射角、$v(r)$ 为当地波速。TauP 采用 Buland & Chapman（1983）的 $\tau$-$p$ 方法思想，在射线参数域中处理走时积分，避免时距曲线多值问题。

### 2.2 obs.taup 的角色

`obs.taup` 是 ObsPy 对 TauP Toolkit 的 Python 封装，核心类为 `TauPyModel`。在 Geiger 定位器中，它承担**正演模块**的职责：

- **输入**：猜测的震源位置（经纬度、深度）、台站位置、速度模型名称、候选震相列表
- **输出**：每个候选震相的理论走时、射线参数、出射角、入射角、震中距等

```python
from obspy.taup import TauPyModel

model = TauPyModel(model="ak135")

arrivals = model.get_travel_times(
    source_depth_in_km=depth_guess,
    distance_in_degree=dist_deg,
    phase_list=["P", "S"]
)
```

### 2.3 震中距计算

理论走时需要震中距作为输入。利用 `obspy.geodetics.gps2dist_azimuth` 计算大圆距离：

```python
from obspy.geodetics import gps2dist_azimuth

dist_m, az, baz = gps2dist_azimuth(event_lat, event_lon, sta_lat, sta_lon)
dist_deg = dist_m / 111194.9  # 转换为度
```

### 2.4 震相匹配

`get_travel_times` 返回的 `arrivals` 列表按走时从小到大排列。定位中通常只需要第一个匹配震相（如 "P" 或 "S"）：

```python
# 获取第一个P波震相
p_arrival = None
for a in arrivals:
    if a.phase_name == "P":
        p_arrival = a
        break
```

---

## 3. 反演问题：最小二乘迭代

### 3.1 待求参数

一次绝对定位需要求解 4 个未知参数：

$$
\boldsymbol{\theta} = (t_0,\; \lambda_0,\; \phi_0,\; z_0)
$$

### 3.2 观测方程

假设已完成震相拾取和关联，得到 $N$ 个台站的观测到时 $t_i^{\text{obs}}$（$i = 1, \dots, N$）。给定当前猜测的震源参数 $\boldsymbol{\theta}^{(k)}$，第 $i$ 个台站的理论到时为：

$$
t_i^{\text{cal}} = t_0 + T_i(\lambda_0, \phi_0, z_0)
$$

其中 $T_i$ 是由 obs.taup 计算的理论走时。

### 3.3 走时残差

$$
r_i = t_i^{\text{obs}} - t_i^{\text{cal}}
$$

定位目标是最小化加权残差平方和：

$$
\Phi(\boldsymbol{\theta}) = \sum_{i=1}^{N} w_i \cdot r_i^2
$$

### 3.4 线性化：泰勒展开

走时 $T_i$ 与震源参数之间是非线性关系，需要在当前猜测值附近做一阶泰勒展开：

$$
t_i^{\text{obs}} \approx t_0 + T_i(\boldsymbol{\theta}^{(k)}) +
\frac{\partial T_i}{\partial t_0}\Delta t_0 +
\frac{\partial T_i}{\partial \lambda}\Delta \lambda +
\frac{\partial T_i}{\partial \phi}\Delta \phi +
\frac{\partial T_i}{\partial z}\Delta z
$$

整理得到线性观测方程：

$$
r_i \approx
\frac{\partial T_i}{\partial t_0}\Delta t_0 +
\frac{\partial T_i}{\partial \lambda}\Delta \lambda +
\frac{\partial T_i}{\partial \phi}\Delta \phi +
\frac{\partial T_i}{\partial z}\Delta z
$$

### 3.5 雅可比矩阵

定义设计矩阵（Jacobian 矩阵）$\mathbf{A}$ 和参数修正向量 $\Delta\boldsymbol{\theta}$：

$$
\mathbf{A} = \begin{bmatrix}
1 & \frac{\partial T_1}{\partial \lambda} & \frac{\partial T_1}{\partial \phi} & \frac{\partial T_1}{\partial z} \\
1 & \frac{\partial T_2}{\partial \lambda} & \frac{\partial T_2}{\partial \phi} & \frac{\partial T_2}{\partial z} \\
\vdots & \vdots & \vdots & \vdots \\
1 & \frac{\partial T_N}{\partial \lambda} & \frac{\partial T_N}{\partial \phi} & \frac{\partial T_N}{\partial z}
\end{bmatrix}, \quad
\Delta\boldsymbol{\theta} = \begin{bmatrix}
\Delta t_0 \\
\Delta \lambda \\
\Delta \phi \\
\Delta z
\end{bmatrix}
$$

矩阵形式：$\mathbf{r} = \mathbf{A} \cdot \Delta\boldsymbol{\theta}$

其中偏导数 $\frac{\partial T_i}{\partial \lambda}$ 等物理上对应**慢度矢量在相应方向上的投影**，表示走时对震源参数变化的敏感度。

### 3.6 加权最小二乘解

引入对角权重矩阵 $\mathbf{W} = \text{diag}(w_1, \dots, w_N)$，加权最小二乘解为：

$$
\Delta\boldsymbol{\theta} = (\mathbf{A}^T \mathbf{W} \mathbf{A})^{-1} \mathbf{A}^T \mathbf{W} \mathbf{r}
$$

更新参数：

$$
\boldsymbol{\theta}^{(k+1)} = \boldsymbol{\theta}^{(k)} + \Delta\boldsymbol{\theta}
$$

重复迭代直到 $\|\Delta\boldsymbol{\theta}\|$ 小于阈值或 RMS 残差收敛。

---

## 4. 完整迭代算法流程

### 4.1 算法伪代码

```
输入: 观测到时 picks[i] = (sta_lat, sta_lon, t_obs, phase)
输入: 速度模型 model
输入: 初始猜测 theta_0 = (t0, lat, lon, depth)
输入: 权重函数 w_func
输入: 收敛阈值 tol, 最大迭代次数 max_iter

输出: 定位结果 theta_opt

初始化:
    theta = theta_0
    计算初始 RMS 残差

for k = 1 to max_iter:
    1. 正演: 对每个台站 i
       - 用 obs.taup 计算理论走时 T_i(theta)
       - 计算理论到时 t_i^cal = t0 + T_i

    2. 残差: r_i = t_i^obs - t_i^cal

    3. 收敛检查:
       - RMS = sqrt(sum(w_i * r_i^2) / sum(w_i))
       - if RMS < tol: 退出循环

    4. 构建雅可比矩阵 A:
       - 数值微分: 对每个参数扰动 delta，重新调用 obs.taup 计算 T_i
       - dT/dparam ≈ [T_i(param+delta) - T_i(param)] / delta

    5. 计算权重 W:
       - 基于 P/S 震相类型
       - 基于残差大小（迭代重加权）

    6. 求解修正量:
       - delta_theta = (A^T W A)^(-1) A^T W r

    7. 更新参数:
       - theta = theta + alpha * delta_theta

    8. 检查参数变化:
       - if ||delta_theta|| < tol: 退出循环

输出: theta_opt, RMS, 协方差矩阵
```

### 4.2 数值雅可比矩阵

由于 obs.taup 不提供解析偏导数，需要用**有限差分**近似：

```python
import numpy as np

def compute_jacobian(model, picks, theta, delta=0.01):
    """数值计算雅可比矩阵"""
    t0, lat, lon, depth = theta
    n_picks = len(picks)
    A = np.zeros((n_picks, 4))

    # 基准走时
    base_times = compute_travel_times(model, picks, theta)

    # 对每个参数扰动
    param_names = ['t0', 'lat', 'lon', 'depth']
    for j, name in enumerate(param_names):
        theta_pert = list(theta)
        if name == 't0':
            theta_pert[0] += delta  # 1秒
        elif name == 'lat':
            theta_pert[1] += delta  # 0.01度
        elif name == 'lon':
            theta_pert[2] += delta  # 0.01度
        elif name == 'depth':
            theta_pert[3] += delta  # 0.01km

        pert_times = compute_travel_times(model, picks, theta_pert)

        for i in range(n_picks):
            A[i, j] = (pert_times[i] - base_times[i]) / delta

    return A
```

> **注意**：数值雅可比矩阵需要 $4N + N$ 次正演调用（1次基准 + 4次扰动 x N个参数）。对于大规模台网，这会成为计算瓶颈。优化策略包括：使用 scipy 的 `approx_jac` 选项、减少参与雅可比计算的台站数、或使用解析梯度（如果速度模型允许）。

---

## 5. 权重策略

### 5.1 震相类型权重

P 波拾取通常比 S 波更精确（S 波初动更难识别），因此赋予不同权重：

| 震相 | 典型权重 | 理由 |
|------|----------|------|
| P | 1.0 | 初动清晰，拾取精度高 |
| S | 0.5~0.7 | S-P 时间误差较大 |
| pP, sP 等深部震相 | 0.3~0.5 | 识别难度大 |

### 5.2 迭代重加权（抗差估计）

对含有粗差的观测数据，使用迭代重加权最小二乘（IRLS）降低异常值影响：

```python
def robust_weight(residuals, sigma=1.345):
    # Tukey双权函数权重
    abs_r = np.abs(residuals)
    # 标准化
    median_r = np.median(abs_r)
    if median_r < 1e-10:
        return np.ones_like(residuals)
    std_r = median_r * 1.4826  # MAD估计

    u = abs_r / (sigma * std_r)
    w = np.where(u < 1.0, (1 - u**2)**2, 0.0)
    return w
```

### 5.3 台站覆盖权重

方位角覆盖好的台站权重可适当提高；对于方位角间隙大的区域，可考虑降低权重以避免定位偏差。

---

## 6. 定位精度评估

### 6.1 RMS 残差

$$
\text{RMS} = \sqrt{\frac{\sum_{i=1}^{N} w_i \cdot r_i^2}{\sum_{i=1}^{N} w_i}}
$$

RMS < 0.5s 通常认为定位质量良好；RMS > 1.0s 需要检查拾取质量或速度模型。

### 6.2 协方差矩阵与误差椭圆

参数修正的协方差矩阵为：

$$
\mathbf{C}_{\boldsymbol{\theta}} = \sigma^2 (\mathbf{A}^T \mathbf{W} \mathbf{A})^{-1}
$$

其中 $\sigma^2$ 为单位权方差。对角线元素给出各参数的方差估计：

- $C_{t_0 t_0}$ → 发震时刻误差
- $C_{\lambda \lambda}$ → 经度误差
- $C_{\phi \phi}$ → 纬度误差
- $C_{zz}$ → 深度误差

### 6.3 定位误差椭圆（水平面）

水平定位不确定性由经度-纬度协方差矩阵描述：

$$
\mathbf{C}_{\text{hor}} = \begin{bmatrix}
\sigma_\lambda^2 & \sigma_{\lambda\phi} \\
\sigma_{\lambda\phi} & \sigma_\phi^2
\end{bmatrix}
$$

其特征值和特征向量给出误差椭圆的半轴方向和长短轴。

### 6.4 方位角间隙（GAP）

$$
\text{GAP} = \max(\text{相邻台站方位角之差})
$$

GAP > 180° 时定位结果不可靠；GAP < 90° 为良好覆盖。

### 6.5 台站数要求

| 质量等级 | 最少台站数 | RMS 要求 | GAP 要求 |
|----------|-----------|----------|----------|
| 优 | >= 8 | < 0.3s | < 90° |
| 良 | >= 5 | < 0.5s | < 120° |
| 可接受 | >= 3 | < 1.0s | < 180° |
| 差 | < 3 | > 1.0s | > 180° |

---

## 7. 实际实现要点

### 7.1 速度模型选择

| 模型 | 适用场景 | 特点 |
|------|----------|------|
| iasp91 | 全球地震学 | 经典标准模型 |
| ak135 | 全球/区域 | 含海洋层，速度结构更细致 |
| prem | 全球 | 适用于大地震 |
| jb | 区域 | 简单模型 |
| 自定义1D | 区域研究 | 根据当地地震层析结果定制 |

> **重要**：如果研究区速度结构与全球模型差异较大（如青藏高原、俯冲带），建议使用区域 1D 模型或走时校正表，否则系统性偏差可达数公里。

### 7.2 初始猜测策略

| 参数 | 初始猜测方法 |
|------|-------------|
| 发震时刻 $t_0$ | 第一个 P 波到时 - 走时估计值 |
| 经纬度 | 最先接收到地震的台站位置，或 P-S 到时差反推的粗略位置 |
| 深度 | 先设 10km（地壳事件）或 33km（莫霍面附近） |

### 7.3 收敛准则

常用的多重收敛判据：

```python
converged = (
    rms < tol_rms and                    # RMS残差足够小
    np.max(np.abs(delta_theta)) < tol_param and  # 参数变化足够小
    k > min_iterations                    # 至少迭代一定次数
)
```

典型值：`tol_rms = 0.1s`，`tol_param = 1e-4`，`min_iterations = 3`。

### 7.4 阻尼与松弛

当雅可比矩阵接近奇异（台站覆盖差）时，引入阻尼因子保证数值稳定性：

$$
\Delta\boldsymbol{\theta} = (\mathbf{A}^T \mathbf{W} \mathbf{A} + \epsilon \mathbf{I})^{-1} \mathbf{A}^T \mathbf{W} \mathbf{r}
$$

其中 $\epsilon$ 为阻尼参数（Tikhonov 正则化）。松弛因子 $\alpha \in (0, 1]$ 控制每步更新幅度，防止超调：

$$
\boldsymbol{\theta}^{(k+1)} = \boldsymbol{\theta}^{(k)} + \alpha \cdot \Delta\boldsymbol{\theta}
$$

### 7.5 常见问题与调试

| 问题 | 可能原因 | 解决方案 |
|------|----------|----------|
| 不收敛 | 初始猜测太远 | 改进初始值，增加最大迭代次数 |
| 深度震荡 | 深度与发震时刻耦合 | 固定深度先定位，或增加深度阻尼 |
| RMS 偏大 | 速度模型不准/拾取粗差 | 检查拾取质量，考虑区域速度模型 |
| 方位角偏差 | 台站覆盖不均 | 检查 GAP，剔除低质量台站 |
| 矩阵奇异 | 台站数不足/共线 | 增加台站，使用阻尼最小二乘 |

---

## 8. 完整代码框架

### 8.1 核心定位函数

```python
import numpy as np
from obspy.taup import TauPyModel
from obspy.geodetics import gps2dist_azimuth
from scipy.optimize import least_squares

class GeigerLocator:
    # 基于 obs.taup + scipy 的 Geiger 地震绝对定位器

    def __init__(self, model_name='ak135'):
        self.model = TauPyModel(model=model_name)

    def _gps2dist_deg(self, lat1, lon1, lat2, lon2):
        # 计算台站-震中距(度)
        dist_m, _, _ = gps2dist_azimuth(lat1, lon1, lat2, lon2)
        return dist_m / 111194.9

    def _compute_theoretical_times(self, picks, theta):
        # 用 obs.taup 计算理论到时
        t0, lat, lon, depth = theta
        n = len(picks)
        t_cal = np.full(n, np.nan)

        for i, pick in enumerate(picks):
            sta_lat, sta_lon, t_obs, phase = pick
            dist_deg = self._gps2dist_deg(lat, lon, sta_lat, sta_lon)

            arrivals = self.model.get_travel_times(
                source_depth_in_km=depth,
                distance_in_degree=dist_deg,
                phase_list=[phase]
            )

            if arrivals:
                t_cal[i] = t0 + arrivals[0].time
            else:
                t_cal[i] = np.nan

        return t_cal

    def residuals(self, params, picks, weights=None):
        # 残差函数（供 scipy.optimize 使用）
        t_cal = self._compute_theoretical_times(picks, params)
        t_obs = np.array([p[2] for p in picks])

        mask = ~np.isnan(t_cal) & ~np.isnan(t_obs)
        r = t_obs[mask] - t_cal[mask]

        if weights is not None:
            w = np.array(weights)[mask]
            r = r * np.sqrt(w)

        return r

    def locate(self, picks, theta0=None, weights=None,
               tol=1e-4, max_iter=50):
        # 执行 Geiger 迭代定位
        if theta0 is None:
            # 简单初始猜测
            first_pick = picks[0]
            theta0 = [first_pick[2], first_pick[0], first_pick[1], 10.0]

        # 使用 scipy 的 Levenberg-Marquardt 方法
        result = least_squares(
            self.residuals,
            x0=theta0,
            args=(picks, weights),
            method='lm',
            xtol=tol,
            ftol=1e-6,
            max_nfev=max_iter * 10
        )

        return result
```

### 8.2 使用示例

```python
# 1. 初始化定位器
locator = GeigerLocator(model_name='ak135')

# 2. 准备拾取数据
# picks = [(sta_lat, sta_lon, t_obs, phase), ...]
picks = [
    (35.6, 139.7, 1.234, 'P'),   # 台站1: P波到时
    (36.1, 140.2, 2.567, 'S'),   # 台站1: S波到时
    (37.0, 139.5, 1.890, 'P'),   # 台站2
    (38.2, 141.0, 3.210, 'S'),   # 台站2
    # ... 更多台站
]

# 3. 执行定位
result = locator.locate(picks, theta0=[0.0, 36.0, 140.0, 15.0])

# 4. 查看结果
print(f'发震时刻: {result.x[0]:.3f} s')
print(f'纬度: {result.x[1]:.4f}')
print(f'经度: {result.x[2]:.4f}')
print(f'深度: {result.x[3]:.2f} km')
print(f'RMS残差: {np.sqrt(np.sum(result.fun**2)/len(result.fun)):.3f} s')
print(f'成功收敛: {result.success}')
```

### 8.3 完整 Geiger 迭代（含雅可比矩阵）

如果需要手动控制迭代过程（如自定义权重策略、诊断输出），可以使用完整版：

```python
import numpy as np
from scipy.linalg import lstsq

def geiger_full(picks, theta0, model,
                tol_rms=0.1, tol_param=1e-4,
                max_iter=50, damping=1e-6):
    # 完整 Geiger 迭代实现

    theta = np.array(theta0)
    n_picks = len(picks)

    for iteration in range(max_iter):
        # Step 1: 正演 - 计算理论走时
        t_cal = locator._compute_theoretical_times(picks, theta)
        t_obs = np.array([p[2] for p in picks])

        mask = ~np.isnan(t_cal) & ~np.isnan(t_obs)
        if mask.sum() < 4:
            raise ValueError('有效台站数不足，至少需要4个')

        t_cal_m = t_cal[mask]
        t_obs_m = t_obs[mask]

        # Step 2: 残差
        r = t_obs_m - t_cal_m

        # Step 3: 收敛检查
        rms = np.sqrt(np.mean(r**2))
        if rms < tol_rms and iteration > 3:
            print(f'收敛: iter={iteration}, RMS={rms:.4f}s')
            break

        # Step 4: 数值雅可比矩阵
        A = compute_jacobian_numerical(locator, picks, theta, mask, delta=0.01)

        # Step 5: 权重（此处用等权重，可替换为鲁棒权重）
        W = np.eye(mask.sum())

        # Step 6: 阻尼最小二乘
        ATA = A.T @ W @ A + damping * np.eye(4)
        ATr = A.T @ W @ r

        try:
            delta_theta = np.linalg.solve(ATA, ATr)
        except np.linalg.LinAlgError:
            # 矩阵奇异，增加阻尼
            delta_theta = np.linalg.solve(
                ATA + 1e-3 * np.eye(4), ATr
            )

        # Step 7: 参数更新（带松弛）
        alpha = 0.5
        theta_new = theta + alpha * delta_theta

        # Step 8: 检查参数变化
        if np.max(np.abs(delta_theta)) < tol_param:
            print(f'参数收敛: iter={iteration}, max_delta={np.max(np.abs(delta_theta)):.6f}')
            break

        theta = theta_new

        if iteration % 10 == 0:
            print(f'iter={iteration}, RMS={rms:.4f}s, '
                  f'depth={theta[3]:.1f}km')

    return theta, rms, iteration
```

---

## 9. 定位流程总结

完整的从数据到定位结果的工作流：

```
+-------------+    +-------------+    +-------------+
|  原始波形   |--->|  数据预处理   |--->|  震相拾取   |
|  (SEED/WAV) |    | (滤波/去均值  |    | (STA/LTA/  |
|             |    |  去仪器响应)  |    |  模板匹配)  |
+-------------+    +-------------+    +------+------+
                                             |
                                             v
+-------------+    +-------------+    +-------------+
|  精度评估   |<---|  Geiger迭代 |<---|  震相关联   |
| (RMS/协方差 |    | (obs.taup + |    | (到时配对   |
|  /GAP/台站数)|    |  scipy)     |    |  事件匹配)  |
+-------------+    +-------------+    +------+------+
                                             |
                                             v
                                      +-------------+
                                      |  绝对定位结果 |
                                      | (t0, lat,   |
                                      |  lon, depth) |
                                      +-------------+
```

---

## 10. 局限性与扩展方向

### 10.1 当前方法的局限

| 局限 | 说明 |
|------|------|
| 1D 速度模型 | obs.taup 基于 1D 球对称模型，无法反映三维横向不均匀性 |
| 振幅不可靠 | obs.taup 的振幅计算是实验性功能，不能用于震级估算 |
| 线性近似 | Geiger 法是局部线性化，初始猜测差时可能陷入局部极小 |
| 无误差模型 | 标准 Geiger 法假设观测误差高斯分布，实际可能不成立 |

### 10.2 后续改进方向

- **三维速度模型**：结合 Tomoaniso 等工具，使用 3D 速度模型修正走时
- **非线性定位**：使用网格搜索 + 局部优化的混合策略（如 NonLinLoc）
- **双差相对定位**：在绝对定位基础上，用 hypoDD / relocDD-py 做相对精定位
- **贝叶斯定位**：引入先验信息，用 MCMC 采样获得后验概率分布
- **联合反演**：用 VELEST 同时反演震源位置和台站校正/速度扰动

---

## 参考文献

1. Geiger, L. (1910). Erdbebenwellen. *Geophysikalische Zeitschrift*, 8, 436-499.
2. Buland, R., & Chapman, M. (1983). The traveltime integral for a spherically earth. *Geophysical Journal International*, 75(3), 759-773.
3. Havskov, J., & Ottemoller, L. (2010). Routine earthquake location using ObsPy. *Seismological Research Letters*, 81(1), 158-164.
4. Iwata, T., & Sekiguchi, H. (2004). A simulated annealing algorithm for the earthquake location problem. *Physics of the Earth and Planetary Interiors*, 143, 289-306.
5. Lin, P., & Shearer, P. M. (2015). Global catalogue comparison and improvement for small magnitude earthquakes. *Journal of Geophysical Research: Solid Earth*, 120(9), 6276-6296.
