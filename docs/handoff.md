# 交接履约集市（hand-off fulfillment marketplace）

第三个、与 v1/v2 完全独立的入口（`python -m cold_exposure.handoff`，仅用 Python
3.11+ 标准库与内置 `sqlite3`）。它回答的问题是：一趟运输在途中多次转交承运方时，
已有 v2 多记录仪共识证据里的每一秒、每次越界和每一分暴露，究竟发生在谁的保管
期间。它**只产出合成履约依据，不判定赔偿责任或法律归属**；v1
（`python -m cold_exposure`）与 v2（`python -m cold_exposure.consensus`）的文件、
CLI 与数值行为均保持不变。

## 输入

1. 既有 v2 共识报告（UTF-8 JSON，`schema_version = 2`），由
   `cold_exposure.consensus` 生成，内含每趟运输的完整共识分区与分量。
2. 交接 CSV（UTF-8，精确有序表头）：
   `handoff_id,trip_id,carrier,started_at,ended_at`

两份输入都按字节各读取一次，同一批字节同时用于 SHA-256 哈希、解析和入库归档；
原文件之后被移动或删除，数据库内仍保留原文与来源位置，可纯 SQL 复核。

### 交接 CSV 契约（任何拒绝都带文件名与 CSV 数据行号，首数据行为 1）

- `handoff_id` 全局唯一，非空、无首尾空白；`carrier` 同样非空、无首尾空白。
- `trip_id` 必须存在于共识报告，否则拒绝（运输须存在）。
- 时间沿用既有 UTC 整秒契约：带时区、归一化到 UTC、不允许亚秒。
- 每个交接区间为**左闭右开 `[started_at, ended_at)`、非空**，且整体落在所属运输
  窗口 `[trip_start, trip_end)` 之内。
- **同一运输内的交接区间不得重叠**（端点相接合法，如一个在 t 结束、另一个在 t
  开始）；不同运输的区间时刻相同不算重叠。
- 交接可以稀疏；没有任何交接覆盖的秒保持**未分配（unassigned）**，绝不静默塞给
  某个承运方。仅含表头的交接文件合法，结果是整库全部未分配。

### 对 v2 共识报告的复核（不信任式读取）

集市不把整趟四舍五入指标按比例分账，并且在切分前先验证报告自身：

- `schema_version` 必须为 2，模型标识匹配，至少一趟运输；JSON 重复键拒绝。
- 每趟运输的分区时间线必须从运输起点连续铺到终点：左闭右开、非空、无空洞、无
  重叠；分区秒数与其窗口一致，分区秒数之和等于运输时长。
- 用与 v2 引擎相同的端点扫描语义，从存储的 `consensus_components` 重算
  `classification`、`disjoint_from_allowed_range` 与
  `not_contained_in_allowed_range`，与报告值逐分区比对。
- 五类秒数、`definitely_outside_seconds`、`possibly_outside_seconds`、
  `unknown_seconds`、`supported_seconds` 必须能由分区重新汇总得到；六位小数的
  `supported_degree_minutes_min/max` 必须等于分区精确 degree-second 求和后再
  `/60` 量化的结果（全程无支持时必须为 JSON `null`）。

## 归属事实：按交接与 UTC 午夜切原分区

对每趟运输，把三类边界合并成一条统一时间线：原共识分区的全部边界、覆盖该运输
的各交接区间起止、运输窗口内的每个 **UTC 午夜**。相邻边界形成的每个原子切片即
一条**归属事实（attribution fact）**，它同时落在唯一一个原共识分区、唯一一个
UTC 日历日，以及唯一一个交接区间（或未分配）之内。

- 事实区间左闭右开、非空；同运输事实首尾相接、互不重叠，其并集恰为整个运输
  窗口（逐运输切片完整覆盖）。
- 每条事实带外键 `(trip_id, partition_index)` 回溯到原共识分区，再经分区外键
  回到运输；交接字段通过 `handoff_id` 外键追溯，未分配时 `handoff_id`/`carrier`
  为 NULL 且 `is_unassigned = 1`（表内 `CHECK` 强制）。
- 可加指标在事实层面守恒：秒数、确定/可能越界秒、未知秒，以及精确
  **degree-second 上下界**。有共识支持的分区，事实的 degree-second 界为
  `分区每秒距离界 × 切片秒数`；未知分区的界保持 NULL，**未知不补零**。
- 共识分量单独存表（`consensus_component`）。任何按承运方/日的汇总只对事实表
  求和，绝不连接分量表，因此一个含多分量的分区不会把同一秒重复计量。

## 承运方每日物化（`carrier_daily`）

按 `(carrier, UTC 日)` 分组，未分配时间以 `carrier IS NULL AND is_unassigned = 1`
单独成组。每组物化：

- `duration_seconds`、`definitely_outside_seconds`、
  `possibly_outside_seconds`、`unknown_seconds`（真实未知计数，**不补零**）、
  `supported_seconds = duration - unknown`；
- `degree_minutes_min/max`：组内精确 degree-second 求和后 `/60`，按
  `ROUND_HALF_UP` 量化为六位小数字符串；当该组**没有任何有支持的秒（全未知）**
  时两界均为 SQL `NULL`，绝不制造零暴露；
- `trip_count` 为该组涉及的**去重运输数**（`COUNT(DISTINCT trip_id)`，在物化时
  计算，不依赖会重复的连接），以及 `handoff_count`。

## SQLite 结构（schema_version = 1）

| 表/视图 | 作用 |
| --- | --- |
| `ingestion_run` | 单行：集市 schema 版本、v2 `max_gap_seconds`、各表行数、总时长/越界/未知/未分配秒摘要与政策说明；摘要在复核时由事实表重算比对 |
| `source_document` | 两份原文（`consensus_json`、`handoff_csv`）、SHA-256、字节数与全文 |
| `source_location` | 每份原文的调用时来源路径（按传入原样记录，非机器解析依赖）与记录位置：CSV 记表头行号/首数据行号/数据行数，JSON 记运输条数 |
| `trip` | 运输窗口（ISO 文本 + epoch 秒）、阈值、quorum、时长与三类越界/未知秒 |
| `consensus_partition` | 原共识分区/分量窗口、分类、两个越界标志、每秒精确 degree-second 界 |
| `consensus_component` | 每个分区的共识分量（独立成行，避免连接重复计量） |
| `handoff` | 交接 ID、运输、承运方、CSV 行号与半开窗口 |
| `attribution_fact` | 原子归属事实：分区外键、交接外键、承运方/未分配、UTC 日、各可加指标 |
| `carrier_daily` | 承运方×UTC 日物化（含未分配组、NULL 全未知界、去重运输数） |
| `v_carrier_daily` / `v_attribution_fact` | 稳定排序的只读查询视图 |

外键约束在每个连接开启（`PRAGMA foreign_keys = ON`），建库后运行
`PRAGMA foreign_key_check`。

## 确定性、原子发布与失败恢复

- 无时钟读取、无机器绝对路径、无随机量；相同输入字节重建得到逻辑一致、SQL 转储
  相同的数据库（测试用 `iterdump()` 比较两个独立库）。
- `build` 先在目标同目录写隐藏临时文件，完整建表、装载并通过全部库内复核后，才
  用一次 `os.replace` 替换目标；任何读、解析、计算、写入或复核失败都删除临时
  文件，**旧库保持字节不变**，可修正后直接重跑。目标为符号链接时拒绝。
- `verify` 以只读 URI（`mode=ro`）打开既有库，执行：外键检查；事实对每趟运输的
  完整无叠覆盖；事实不越出原分区；秒/越界/未知指标对运输守恒；精确
  degree-second 界对每个原分区守恒；`carrier_daily` 可由事实表完全重算（含全
  未知 NULL 规则）；源文档哈希与归档字节一致；以及**用归档原文重建整个模型并与
  已存事实逐行比对**。因此移走输入文件后仍可纯 SQL 复核。

## 命令与 Python API

```sh
# 1) 由三份合成 CSV 生成 v2 共识报告（旧入口，保持不变）
python tools/build_handoff_example.py --output-dir examples/handoff/generated

# 2) 建库（临时构建 + 原子替换）
python -m cold_exposure.handoff build \
    --consensus examples/handoff/generated/consensus.json \
    --handoffs examples/handoff/generated/handoffs.csv \
    --output run/handoff/marketplace.db

# 3) 只读复核（输入移走后仍可运行）
python -m cold_exposure.handoff verify --database run/handoff/marketplace.db
```

```python
from cold_exposure.handoff import build_database, verify_database
model = build_database("consensus.json", "handoffs.csv", "marketplace.db")
summary = verify_database("marketplace.db")
```

## 合成示例与独立手算

`examples/handoff/source/` 是三份原创合成 CSV（运输/指派/读数），
`examples/handoff/handoffs.csv` 是交接输入；`tools/build_handoff_example.py`
确定性生成 `examples/handoff/generated/`（git 忽略），含 v2 共识报告与交接副本。
场景覆盖：一段跨 UTC 午夜的确定越界且中途转交承运方、未分配空洞、一整趟全未知
运输、以及一趟含两个分离共识分量且暴露上下界不同的运输。允许范围 `A=[2,8]`。

### 运输 HA（2026-05-01 23:00 → 2026-05-02 01:00，7200 秒）

共识分区：`[23:00,24:00)` above（10°C，3600 秒）、
`[00:00,00:30)` within（4°C，1800 秒）、`[00:30,01:00)` above（10°C，1800 秒）。
交接：H1 Northwind `[23:00,23:30)`；H2 Polar `[23:45,00:30)`；
`[23:30,23:45)` 与 `[00:30,01:00)` 为未分配空洞。

按交接×午夜切出的归属（节选关键边界）：

| 归属 | 区间(UTC) | 日 | 秒 | 分类 |
| --- | --- | --- | --- | --- |
| Northwind/H1 | 23:00–23:30 | 05-01 | 1800 | above |
| 未分配 | 23:30–23:45 | 05-01 | 900 | above |
| Polar/H2 | 23:45–24:00 | 05-01 | 900 | above |
| Polar/H2 | 00:00–00:30 | 05-02 | 1800 | within |
| 未分配 | 00:30–01:00 | 05-02 | 1800 | above |

above 时 `d(10)=10-8=2`，within 时 `d(4)=0`。手算承运方日：

- Northwind / 05-01：1800 秒，确定越界 1800，暴露 `1800×2/60 = 60.000000`
  degree-minutes。
- Polar / 05-01：900 秒 above → `30.000000`；Polar / 05-02：1800 秒 within →
  `0.000000`（界为零而非 NULL，因为该组有支持秒）。
- 未分配 / 05-01：900 秒 above → `30.000000`；未分配 / 05-02：1800 秒 above
  （60.000000）+ 来自 HB 的 900 秒未知。

### 运输 HB（23:30 → 次日 00:30，3600 秒，整趟 unknown）

两台记录仪各只有单个读数，无相邻样本夹持，整段 `below_quorum` 未知。交接 H3
Acme `[23:45,00:15)` 跨午夜。Acme 在 05-02、05-03 各得 900 秒：
`unknown_seconds=900`、`supported_seconds=0`、暴露界**均为 NULL**；首尾各 900
秒未分配且同样未知。这验证“未知不补零、全未知为 NULL”。

### 运输 HC（00:00 → 00:20，1200 秒，双分量 mixed）

四个记录仪的带为 `[-1,1]`、`[0,1]`、`[9,10]`、`[9,11]`，quorum=2 的共识分量是
`[0,1]` 与 `[9,10]`，整体与 A 不相交（确定越界）。在 `[0,1]` 上 `d` 从 2 降到 1，
在 `[9,10]` 上 `d` 从 1 升到 2，故每分区距离下界 1、上界 2。交接 H4 Polar
`[00:05,00:15)`：

- Polar / 05-03 持有 600 秒：确定越界 600，暴露界
  `600×1/60 = 10.000000`、`600×2/60 = 20.000000`（上下界不同）。
- 未分配 / 05-03 持有另外 600 秒，界同样为 10/20；与 HB 尾部 900 秒未知合在同一
  未分配日组，该组仍有支持秒，所以只对未知部分不计暴露、界不置 NULL。

### 守恒核对（测试中独立断言）

- 事实总秒数 `7200 + 3600 + 1200 = 12000`；承运方日时长之和同为 12000。
- 确定越界秒 `HA 5400 + HB 0 + HC 1200 = 6600`；未知秒合计 3600（全部来自 HB）。
- 每个原分区的精确 degree-second 上下界在其全部切片上求和后与分区原值一致；
  直接对 `attribution_fact` 求和为 1200 秒，而连接 `consensus_component` 的朴素
  查询会翻倍成 2400 秒——测试据此固定“查询不能因多分量连接重复计量”。
