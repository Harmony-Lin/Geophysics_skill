# 安装指南

## 1. Python 虚拟环境

推荐 Python 3.11+：

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

## 2. 注意事项

### PyGMT / GMT
PyGMT 依赖系统级 GMT 6（含 PROJ/GDAL/netCDF）。Windows 上建议通过 conda-forge 安装：
```bash
conda install -c conda-forge gmt pygmt
```
若使用 venv 而非 conda，需先单独安装 GMT 并配置 `GMT_LIBRARY_PATH`。

### torch GPU 版
默认 pip 安装为 CPU 版。GPU 版请按 https://pytorch.org 指引安装，例如：
```bash
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

### GaMMA
若 git 直装失败，可手动克隆：
```bash
git clone https://github.com/AI4EPS/gamma.git && pip install ./gamma
```

### SeisBench 权重（离线部署必做）
有网环境预跑一次拾取脚本，使权重缓存到 `~/.cache/seisbench/`，随后整体拷贝到离线机器。

## 3. 系统依赖（Linux 容器内）

- gcc / gfortran / make（编译 HypoDD、NonLinLoc）
- Java JRE ≥ 11（TauP）
- GMT 6
- NVIDIA 驱动 ≥535 + nvidia-container-toolkit（GPU 推理）
