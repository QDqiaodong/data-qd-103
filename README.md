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

## 多记录仪离线共识（独立 v2 入口）

一次运输可放置多台记录仪，采样时刻不同、读数有固定误差半宽，也可能断测或互相矛盾。新入口与 v1 完全独立，schema_version=2，不平均记录仪温度：

```sh
.venv/bin/python -m cold_exposure.consensus \
  --trips data/consensus/trips.csv \
  --assignments data/consensus/assignments.csv \
  --readings data/consensus/readings.csv \
  --max-gap-seconds 900 \
  --output run/consensus/report.json
```

Python 调用为 `cold_exposure.consensus_pipeline.run_consensus(trips, assignments, readings, output=..., max_gap_seconds=600)`。要点（完整定义见 `docs/consensus.md`）：

- 每台记录仪独立形成左保持区间，原始样本间隔超限时整段未知，不在首末样本之外外推；全部记录仪边界与运输起止合并为统一半开时间分区。
- 每台有效记录仪在一个分区至多贡献一票，票为闭温度带 `[v-u,v+u]`；共识集合 S 是被至少 q 个带包含的全部温度，精确输出全部最大闭连通分量，不填平支持不足的空洞，不取交集/均值/包络，不逐秒或按温度网格离散。
- 五类秒数 within/above/below/mixed/unknown 完整划分运输时长；另独立给出 definitely_outside 与 possibly_outside（前者必然包含于后者）。`below_quorum` 与 `no_consensus` 是正常结果而非失败。
- `supported_degree_minutes_min/max` 仅描述有共识支持的秒数，全程无共识时为 null，未知秒单独统计，不制造融合点温度。
- 状态按优先级 `confirmed_excursion` / `possible_excursion` / `incomplete` / `within_limits` 生成；连续同侧确定越界可跨支持者合并，侧别切换、within、mixed、unknown 均打断。
- 三份输入各捕获一次字节，哈希与解析使用相同内容；报告含三份输入 SHA256、完整分区及每台记录仪的原始夹持证据（含 CSV 记录序号）。校验、分析、序列化全部完成后才经同目录临时文件加 `os.replace` 原子替换；失败时旧报告保持完整、非零退出，可修正后直接重跑。相同输入与参数重跑得到字节一致的 JSON。

该共识是合成演示分析策略，不是统计置信区间、真实温度保证或食品药品放行标准。`data/consensus/` 下为覆盖单点共识、分离分量、部分越界、断测、窗口外夹持和侧别切换等场景的原创合成数据。

## 结构与业务契约

- `cold_exposure/input.py：运输与读数契约、时区和数值校验`
- `cold_exposure/exposure.py：区间切分、覆盖与暴露语义`
- `cold_exposure/pipeline.py：逐区间 JSON 与汇总 CSV`
- `cold_exposure/consensus_input.py / consensus_engine.py / consensus_pipeline.py / consensus.py：v2 多记录仪共识的校验、端点扫描、汇总与独立 CLI`
- `docs/contract.md`：完整字段/格式、业务键、边界、输出含义与当前约束。
- `docs/consensus.md`：v2 共识定义、未知语义、暴露边界、输出结构、兼容性与失败恢复。
- `data/`：小型确定性合成输入和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界和失败行为测试。

当前范围：使用明确的离线左保持模型，无末端外推或线性插值。示例阈值不代表食品/药品放行标准；未知与不合格分开，单进程全量计算。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
