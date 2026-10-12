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

# relocDD-py 双差定位后端（上游无 setup.py/pyproject.toml，见 §2 注意事项）
python seismic-workflow/packages/relocdd-py/install.py
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

### relocDD-py（双差定位后端）
上游 https://github.com/katie-biegel/relocDD-py 没有 `setup.py` / `pyproject.toml`，
因此**不能**用 `pip install git+https://github.com/katie-biegel/relocDD-py.git`
（会报 `does not appear to be a Python project`）。改为在安装阶段把
仓库内手写的 `seismic-workflow/packages/relocdd-py/pyproject.toml` 补进 git checkout
后 editable 安装：

```bash
python seismic-workflow/packages/relocdd-py/install.py
```

脚本等价于以下手工步骤（默认固定在 commit `323071a`）：

```bash
git clone https://github.com/katie-biegel/relocDD-py.git seismic-workflow/.cache/relocdd-py
cp seismic-workflow/packages/relocdd-py/pyproject.toml seismic-workflow/.cache/relocdd-py/
pip install -e seismic-workflow/.cache/relocdd-py
```

- checkout 落在 `seismic-workflow/.cache/relocdd-py`（已 gitignore），用 `--ref` 可切换修订。
- 安装后 `import ph2dt` / `import hypoDD` 在任何工作目录下均可用，源码不再内嵌到
  `third_party/`。
- 首次安装需要联网一次（后续无需重复克隆）。

### SeisBench 权重（离线部署必做）
有网环境预跑一次拾取脚本，使权重缓存到 `~/.cache/seisbench/`，随后整体拷贝到离线机器。

## 3. 系统依赖（Linux 容器内）

- gcc / gfortran / make（编译 HypoDD、NonLinLoc）
- Java JRE ≥ 11（TauP）
- GMT 6
- NVIDIA 驱动 ≥535 + nvidia-container-toolkit（GPU 推理）
