# Lua 性能基准（Linux/WSL2 口径）

monopoly 固定在 `9bc18c4`：`src/` 567 个 Lua 文件、10578 个变异点、547 个
spec 文件、6262 个测试。测量机：WSL2 Ubuntu 24.04，12 核，Lua 5.4 +
Busted 2.3.0（`~/.luarocks` 用户 tree）。命令均在项目根执行
`mutator --timings <path>`，全新快照（先删 `.metrics/mutate` 与
`target/coverage`）。

旧的 Windows 口径记录已随平台移除（`bcbdc42`）；本文件是 Linux 重建版。

## 基线（优化前，`0d2d383` 时代行为 + `c4c0718` 修复）

`src/computer`（96 个变异体，12 worker），墙钟 **344 秒**：

| 阶段 | 数值 |
|---|---|
| 覆盖率收集 | 1 × 60.2 秒 |
| 基线（原仓全量套件） | 1 × 47.7 秒 |
| worker pristine 检查 | 12 × 92.2 秒（并行，互相抢核） |
| 变异体 | 96 × 7.16 秒 |
| 变异阶段墙钟 | 283.6 秒 |

结果：91 killed / 5 survived / 0 uncovered。

全量套件单次约 48.7 秒，其中仅加载约 31 秒：busted 的 auto-insulation
让每个 spec 文件重新加载全部模块（1163 个文件被编译 119603 次，357 MB），
而加载期 CPU 的约三分之二花在 `getTrace` 的一次 `debug.traceback` 上
（详见 `docs/research/busted-internal-overhead.md`）。

## 优化后（`0ab70dd` + `117e52d`）

同样的 `src/computer` 运行，墙钟 **194 秒（-44%）**，逐点结果与基线
**完全一致**（91/5/0，逐 mutation_id 比对三个快照的 outcomes）：

| 阶段 | 基线 | 优化后 | 手段 |
|---|---|---|---|
| 覆盖率收集 | 60.2 秒 | 44.6 秒 | 字节码缓存 + fasttrace |
| 基线 | 47.7 秒 | 0（播种） | 覆盖率运行兼作基线（ADR 0001） |
| worker pristine 检查 | 12 × 92.2 秒 | 1 × 39.9 秒 | 每命令一次（`--paranoid-workers` 可恢复） |
| 变异体均值 | 7.16 秒 | 5.43 秒 | 字节码缓存 + fasttrace |
| 变异阶段墙钟 | 283.6 秒 | 149.8 秒 | |
| 总墙钟 | 344 秒 | **194 秒** | |

`--select-specs --use-existing-coverage` 复验：复用索引（基线照跑，符合
设计），同样 91/5/0 逐点一致；变异体均值 0.23 秒（子集运行），5 个存活者
经全量确认（5 × 38.3 秒）。

## 推算

全量（10578 个变异点）首次运行：基线口径约 12 小时；按变异体均值等比例
推算优化后约 2.5–3 小时。

## 全量实测（2026-10-22，未跑完）

monopoly @9bc18c4 `src` 全量，跑到 452/567 文件（剩余 78 个文件全在
`src/ui/` 下）时人为停止。按快照写出时间累计（扣除一次因系统内存回收的
中断间隙）：

| 范围 | 变异体 | 耗时 | 每变异体墙钟 |
|---|---:|---:|---:|
| 非 `ui` | ~6900 | ~321 min | ~2.8s |
| `ui`（已跑部分） | ~2750 | ~281 min（其中 `ui.render` 155 min） | ~6.1s |

结论：等比例推算严重低估——`src/computer` 的变异体比全仓均值便宜得多。
全量首次运行实际在 10 小时量级，`src/ui` 单个变异体成本约为其他目录的
两倍。0 次 Baseline/worker 检查失败。`ui` 是否值得纳入变异测试另议。

## 待办测量

- monopoly `test/busted_helper.lua` 的 `canonical()` 按路径缓存（-8%，
  monopoly 仓 issue）：合入后重测本表。

## 覆盖 spec 先跑 + 内存上限（#17）

抽样 5 个文件（`src/ui/render/board/{scene,tile}.lua`、
`src/rules/items/{post_effects,steal}.lua`、`src/turn/phases/phase_wait.lua`），
4 worker：墙钟 **427 秒**（含覆盖率 49 秒、worker 检查 50 秒），变异体均值
12.1 秒；前 4 个文件原顺序下墙钟 808 秒、均值约 24 秒。`phase_wait` 原来被
超时变异体拖到 620–760 秒，现在约 1 分钟。114 个变异体与 `/tmp/mono-full`
全量快照逐 mutation_id 一致（66 killed / 16 survived 为本次实跑部分）。
剩余成本以存活者为主（每个都要跑完整套件）。
