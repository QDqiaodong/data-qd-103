# data-qd-103 — Cold-chain Exposure / 冷链温度暴露分析

对虚构运输批次计算可解释的温度越界时长与已知覆盖率，不把断测当作合格。

已实现：运输窗口裁剪、带时区读数排序、左保持区间、最长有效采样间隔、未知区间、上/下限越界秒数和 degree-minutes、连续同侧越界时长、时间加权均温、逐区间证据及来源哈希。

## 独立安装

需要 Python >=3.11（先确认 python3 --version）。运行依赖全部为标准库。 构建工具也已固定版本。
```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
```

首次安装可能需要联网；安装后核心演示与测试可离线运行。每仓独立 `.venv`，不使用其他项目源代码或环境。macOS/Linux 使用相同命令；具体已验证环境不由此声明。

## 演示、输出与测试

在仓库根目录执行。首次 `run/` 不存在即为干净数据状态；需要干净重演可指定新的输出路径。

```sh
.venv/bin/python -m cold_exposure --output run/report
.venv/bin/python -m cold_exposure --output run/replay
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -c 'import json; print(json.load(open("run/report/exposure.json"))["trips"])'
```

T1 共 1800 秒，范围内 600 秒、超上限 1200 秒，已知均温 8°C，越界 30°C·min、最长连续超温 1200 秒；T2 全部 1200 秒未知，为 incomplete。

相同输入重放得到一致 JSON；允许同路径全量重算。输入契约失败在创建新报告前报错，已有报告不会因此被伪装为新结果。运行报告均在忽略的 `run/` 内。

## 结构与业务契约

- `cold_exposure/input.py：运输与读数契约、时区和数值校验`
- `cold_exposure/exposure.py：区间切分、覆盖与暴露语义`
- `cold_exposure/pipeline.py：逐区间 JSON 与汇总 CSV`
- `docs/contract.md`：完整字段/格式、业务键、边界、输出含义与当前约束。
- `data/`：小型确定性合成输入和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界和失败行为测试。

当前范围：使用明确的离线左保持模型，无末端外推或线性插值。示例阈值不代表食品/药品放行标准；未知与不合格分开，单进程全量计算。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
