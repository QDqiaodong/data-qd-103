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

## 交接履约集市（独立 SQLite 入口，仅标准库）

一趟运输在途中可能多次转交承运方，整趟超温不能全部归给收货方。新增 `cold_exposure.handoff` 子包读取**既有 v2 共识 JSON**与交接 CSV（`handoff_id,trip_id,carrier,started_at,ended_at`），把原共识分区按交接区间与 UTC 午夜切成归属事实并写入 SQLite；v1/v2 旧入口完全不变。该入口只作合成履约依据，**不判赔责**。

```sh
python tools/build_handoff_example.py --output-dir examples/handoff/generated
python -m cold_exposure.handoff build \
    --consensus examples/handoff/generated/consensus.json \
    --handoffs examples/handoff/generated/handoffs.csv \
    --output run/handoff/marketplace.db
python -m cold_exposure.handoff verify --database run/handoff/marketplace.db
```

Python 调用为 `cold_exposure.handoff.build_database(consensus_json, handoffs_csv, output_db)` 与 `verify_database(path)`。要点（完整定义见 `docs/handoff.md`）：

- 交接 ID 全局唯一，`trip_id` 必须存在于共识报告；时间沿用 UTC 整秒契约，区间左闭右开、非空且在运输内；同运输交接不得重叠（端点相接合法），没有交接覆盖的秒保持**未分配**，绝不静默派给承运方。
- 对共识报告做不信任式复核：分区必须连续、完整、无重叠地覆盖运输，分类与两个越界标志按同一引擎从分量重算，五类秒数与暴露界必须能由分区重导；**禁止从已舍入的整趟指标按比例分账**，归属一律切原分区。
- 仓内分开保存运输、原共识分区/分量、交接维度、以及按交接和 UTC 午夜切出的归属事实；事实外键追到原分区，分量独立成表，查询只汇总事实表，**多分量连接不会重复计量**。
- 物化承运方每日时长、确定/可能越界秒、未知秒与精确暴露上下界；未知秒不补零，全未知组暴露界为 SQL `NULL`，运输数按 `COUNT(DISTINCT)` 去重；逐运输切片完整覆盖，各可加指标（含精确 degree-second 界）守恒。
- 两份原文连同 SHA-256、字节数与来源位置入库归档，移走输入文件后仍可纯 SQL 复核。临时库通过全部库内校验后才 `os.replace` 替换，失败保旧并清理临时文件；相同输入重建逻辑一致（SQL 转储相同）。
- 合成例覆盖一段跨午夜确定越界且中途转交、未分配空洞、一整趟全未知（暴露界 NULL）与双分量且上下界不同的运输；测试含独立手算的归属、日汇总与守恒断言，真实 CLI 贯通，并覆盖重叠拒绝、多分量防重复与写入失败保旧。

## 离线闪存镜像转录（CCLOG01，合成设备协议）

冷链记录仪交回的是整片闪存镜像：物理槽位写满后循环覆盖，重启又让秒计数器从头开始。新增 `cold_exposure.flash` 子包只信任所选头声明的有效采样序列，用人工校时锚点还原 UTC，输出原 v1/v2 分析器可直接读取的 `readings.csv` 和逐槽位审计 `transcription.json`。CCLOG01 是本项目原创的合成协议，不兼容任何厂商设备，不涉及真实硬件、串口、网络、数据库或真实运输数据；只用 Python 3.11+ 标准库。

```sh
python tools/build_flash_examples.py --output-dir examples/flash/generated
python -m cold_exposure.flash \
    --image examples/flash/generated/scenario1.bin \
    --anchors examples/flash/generated/scenario1.anchors.json \
    --output-dir run/flash/scenario1
```

Python 调用为 `cold_exposure.flash.transcribe(image, anchors, output_dir)`；分步 API 为 `capture_inputs`（每份输入只捕获一次字节）、`transcribe_capture`、`publish`。要点（完整协议见 `docs/flash.md`）：

- 两个 64 字节头副本独立校验（魔数、字段、保留字节、CRC32），按 generation 模 2^32 距离选头：半圈差拒绝、同代仅前 60 字节完全相同才规范选 A、两头全坏失败；新头有效但活动槽损坏时整次失败，不退回旧头掩盖损坏。
- 活动槽严格按 `(next_slot-count+j) mod capacity`、预期 seq 按 `(next_seq-count+j) mod 2^32` 读取，不扫描其余槽位；槽 CRC、seq 精确匹配、保留字节和温度边界（-10000..15000 百分度）任一错误都带槽号与绝对偏移拒绝。百分度转两位小数不经过 float。
- 同 `boot_id` 必须连续（A,B,A 拒绝）；段内 tick 差按模 2^32 计算，只接受 1..2^31-1，零差与半圈及以上拒绝；各 boot 段独立展开，不跨重启推算。
- 锚点为严格 UTF-8 JSON（拒绝重复键、布尔整数），每个有效 boot 段至少一个锚点、只能引用当前有效记录；同段多锚点必须推导出完全相同的 UTC 偏移，规范锚点取段内最早逻辑位置而不是 JSON 顺序。跨 boot 不假定墙钟单调，最终按 UTC 排序；同 sensor 同一 UTC 秒即使温度相同也拒绝。
- 输出目录必须原先存在性检查通过且不跟随符号链接；两文件在同级私有暂存目录写好后一次改名发布，失败无正式半套结果、暂存被清理、输入不变、同目标可重跑。相同输入字节在两个新目录得到字节一致的产物；审计含两份头的原始 hex、每记录物理槽/偏移/完整 24 字节 hex 与 CSV 行号映射，但不保存整片镜像、不证明设备真实测量。
- 转录不做运输窗口裁剪、插值、首尾补样、多记录仪融合或新增暴露指标；输出 CSV 表头仍为 `sensor_id,observed_at,temperature_c`，可直接配合既有 trips/assignments 进入 v1 或 v2 消费者。

## 结构与业务契约

- `cold_exposure/input.py：运输与读数契约、时区和数值校验`
- `cold_exposure/exposure.py：区间切分、覆盖与暴露语义`
- `cold_exposure/pipeline.py：逐区间 JSON 与汇总 CSV`
- `cold_exposure/consensus_input.py / consensus_engine.py / consensus_pipeline.py / consensus.py：v2 多记录仪共识的校验、端点扫描、汇总与独立 CLI`
- `cold_exposure/flash/：CCLOG01 合成闪存镜像的二进制解析、双头选择、环形选槽、tick 展开、锚点契约、暂存发布与独立 CLI`
- `cold_exposure/handoff/：交接履约集市，读取 v2 共识 JSON 与交接 CSV，按交接×UTC 午夜切归属事实并原子发布 SQLite，含只读复核`
- `docs/contract.md`：完整字段/格式、业务键、边界、输出含义与当前约束。
- `docs/consensus.md`：v2 共识定义、未知语义、暴露边界、输出结构、兼容性与失败恢复。
- `docs/handoff.md`：交接契约、归属切分、SQLite 表结构、承运方日物化、原子发布/复核与手算示例。
- `docs/flash.md`：CCLOG01 镜像/槽位/锚点协议、选头规则、时钟展开、审计结构与发布语义。
- `tools/build_flash_examples.py`：可重复生成小型原创合成镜像与锚点示例。
- `tools/build_handoff_example.py`：可重复生成交接集市所需的 v2 共识 JSON 与交接 CSV 合成例。
- `data/`：小型确定性合成输入和 `PROVENANCE.md`。
- `tests/`：真实业务值、重复运行、重要边界和失败行为测试。

当前范围：使用明确的离线左保持模型，无末端外推或线性插值。示例阈值不代表食品/药品放行标准；未知与不合格分开，单进程全量计算。

全部示例为原创合成数据，不代表真实客户、监管标准、实际测量或线上历史。项目没有真实账号、密钥或付费依赖；不包含第三方原始数据。代码及自制数据采用 MIT；依赖保留自身许可。
