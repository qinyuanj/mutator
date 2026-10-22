# monopoly 非 `ui` 变异体成本：时间花在哪里

日期：2026-10-22。环境：WSL2 Ubuntu，AMD Ryzen 5 5500（6 核 12 线程），15 GB RAM
（与另一个调研 agent 共用，负载期间 load average 最高到 16）。工具 mutator
`main @90bd430`，目标 monopoly `@9bc18c4`（非 `ui` 共 305 个文件、7350 个已执行
变异体）。实验在 worktree `/tmp/mono-nonui` 中进行，`--max-workers 4`。参考数据是
`/tmp/mono-full/.metrics/mutate` 里的全量快照（12 worker、无 `--select-specs`），
只读使用。脚本与日志在 `/tmp/nonui-work/`（本机临时目录）。

## 0. 结论摘要

1. **每个变异体都跑全量套件（547 个 spec、6262 个测试），按字母序 fail-fast。**
   被杀死的变异体要先跑完排在杀死它的 spec 之前的所有 spec：`turn.*` 的
   spec 排在套件 38–42% 处，所以即使被杀也要 13–32 s；存活者（7.8%）则跑完整个
   套件，47–58 s（4 worker 实测）。
2. **超时长尾约占非 `ui` 墙钟的 30%。** 7 个文件各耗 618–763 s（都在一次
   10× 超时，即约 560–600 s 附近），另有 3 个文件耗 300–365 s；这 10 个文件
   合计约 95 min，占 321 min 的 30%。文件之间串行执行，其余 11 个 worker
   在这段时间里空等。超时变异体还是**内存炸弹**：`phase_wait.lua:22` 的两个
   变异体让回合循环永不结束，RSS 每分钟涨约 350 MB，8 分钟涨到 2.8 GB。
3. **`--select-specs` 在每个变异体上的结果完全正确，变异体本身也快了 30 倍以上，
   但按现在的实现，全量规模下墙钟反而更长**：存活者的全量确认在一个 worker 上
   串行执行，子集基线也是串行，每个有存活者的文件还要额外跑一次全量 pristine
   检查。把这三处修掉后，推算非 `ui` 全量约 60–75 min（现为 321 min）。
4. 实测 99/99 个变异体在 `--select-specs` 下与全量快照逐 mutation_id 一致
   （另有 68 个在默认模式下复跑，也全部一致）。

## 1. 每个变异体跑了什么、花在哪

### 1.1 命令

默认路径（`engine.py` `_apply_selected`）：标准 Lua 命令加 `--no-keep-going`。
没有 `--select-specs` 时 `plans=None`，每个变异体都跑 `.busted` 的
`ROOT = { "test/behavior", "test/contract" }` 全集。busted 的 `test_file_loader`
对每个 root 做 `table.sort`，所以执行顺序是固定的字母序；第一个失败会把
`busted.skipAll` 置真（`runner.lua:116/126`），后续 spec 不再执行
（`core.lua:313`）。

### 1.2 加载与执行的拆分

busted 按文件交替进行加载（顶层 + describe 体）和执行，二者在时间轴上无法
截成前后两段。覆盖率索引按 spec 文件记录了“开始到结束”的时长（加载 + 执行，
已被 luacov 放大）：547 个 spec 合计 60.2 s，启动开销 `overhead` 1.04 s。
pristine 全量套件在负载下实测（`tap_timed.py`，4 个其他 lua 进程并行）：

```
exit=0 tests=6262 first_ok=0.67 total=125.9s maxrss=567MB cpu=83.3s
marks={100: 2.6, 1000: 13.3, 2000: 38.9, 3000: 69.4, 4000: 104.5, 5000: 117.9, 6000: 124.6}
```

启动（busted + helper 到第一个 `ok`）只要 0.67 s；时间几乎全花在各个 spec 文件上。
**所以真正的成本是“跑了多少个 spec”，不是进程启动。**

按顶层目录累积的执行份额（覆盖率索引，字母序）：

| 目录 | spec 数 | 累积份额 |
|---|---:|---:|
| `test/behavior/app` … `player` | 102 | 0.13 |
| `test/behavior/rules` | 117 | 0.36 |
| `test/behavior/scenarios` | 17 | 0.45 |
| `test/behavior/turn` | 87 | 0.66 |
| `test/behavior/ui` | 187 | 0.98 |

### 1.3 按命名空间：覆盖 spec 数与首个覆盖 spec 的位置

`order2.py` 用 mutator 自己的 `sites_in_file` + `load_history` 把参考快照里的
7350 个变异体映射到覆盖率索引（7303 个有 spec 成员，47 个落在 fallback 行）：

| 命名空间 | 变异体 | 覆盖 spec 数 p50 | 子集执行份额均值 | 首个覆盖 spec 位置 p50 / p90（killed） |
|---|---:|---:|---:|---|
| rules.items | 976 | 7 | 0.076 | 0.165 / 0.227 |
| turn.waits | 413 | 6 | 0.072 | 0.424 / 0.628 |
| turn.phases | 120 | 7 | 0.082 | 0.380 / 0.620 |
| turn.policies | 374 | 11 | 0.107 | 0.380 / 0.424 |
| turn.scheduler | 121 | 18 | 0.128 | 0.365 / 0.493 |
| turn.actions | 265 | 6 | 0.055 | 0.380 / 0.639 |
| rules.land | 335 | 8 | 0.097 | 0.168 / 0.351 |
| turn.loop | 240 | 10 | 0.086 | 0.380 / 0.513 |
| 全部非 ui | 7350 | 7 | 0.088 | 0.160 / 0.424 |

“首个覆盖 spec 位置”是 fail-fast 下杀死时间的**下界**：在它之前的 spec 不执行
变异行，不可能失败。`turn.*` 的变异体至少要先跑完约 38% 的套件。而覆盖它的
spec 平均只占套件的 5–13%。

### 1.4 实测单变异体耗时（默认模式，4 worker）

`handlers.lua` + `choice_timeout_engine.lua`，68 个变异体（`default2.log`）：

- 杀死：6.7–31.9 s，大多在 13–28 s；
- 存活（14 个）：46.8–58.1 s（即全量套件在负载下的时长）；
- `timing mutant: 1641.9s total, 68 run(s), 24.15s each`，墙钟 9:16。
- 与 `/tmp/mono-full` 逐 id 比对：68/68 一致（`compare.py`）。

**回答问题 1：是的，成本由“每个变异体都从字母序开头跑全量套件”决定。**杀死的
变异体花一部分套件时间，存活者花整个套件时间。

## 2. `--select-specs`：结果一致，但现在的实现在全量规模下更慢

### 2.1 实测

同两个文件，`--reuse-coverage --select-specs`（`ss2.log`）：

```
timing baseline: 63.9s total, 24 run(s), 2.66s each
timing worker-check: 121.8s total, 28 run(s), 4.35s each
timing mutant: 48.2s total, 68 run(s), 0.71s each
timing survivor-confirmation: 586.8s total, 14 run(s), 41.91s each
timing mutation-run: 764.7s total
Elapsed 12:44.92
```

逐 id 比对 68/68 一致。变异体均值从 24.15 s 降到 0.71 s（-97%），但墙钟从
9:16 涨到 12:45。原因都在 `workers.py`/`engine.py`：

1. **存活者全量确认串行**（`run_mutants` 末尾）：`directory = directories[0]`
   上逐个 `_execute`，14 × 42 s = 587 s，其余 worker 空闲。
2. **每个有存活者的文件都做一次全量 pristine 检查**（同处
   `_check_worker(directory, …, full_command, …)`），约 45 s/次，不受
   `shared_checks` 去重。
3. **子集基线串行**（`_lua_plans` 里逐个 `_remember_baseline`），24 次 × 2.66 s，
   且它和随后对同一子集命令的 worker 检查证明的是同一件事。

超时文件（`phase_wait`/`registry`/`pre_move`，31 个变异体，`ss3.log`）：

```
timing mutant: 138.5s total, 31 run(s), 4.47s each
timing survivor-confirmation: 132.8s total, 3 run(s), 44.25s each
Elapsed 6:24.38
```

逐 id 比对 31/31 一致。参考全量快照里这三个文件合计 1904 s（12 worker），这里
384 s（4 worker，含一次 45.7 s 覆盖率）。子集模式下 2 个超时变异体分别在
44 s / 60 s 处超时（子集超时 = `max(2, 子集时长 × 10)`），全量模式下要约
560 s；`phase_wait` 的两个超时变异体在子集里被快速杀死，根本没有进入死循环。

### 2.2 全量规模推算

非 `ui`：1523 个不同的（文件，子集）对；109/293 个文件有存活者；574 个存活者。

| 项 | 现实现（串行） | 修复后（并行 / 去重） |
|---|---|---|
| 子集基线 | ~1523 × 2.5 s ≈ 63 min | 0（改用覆盖率索引的 `seconds_for` 定超时，由 worker 检查兼任） |
| 子集 worker 检查 | 每文件去重，≈ 1523 × 2.5 s / worker 数 | 每 run 去重 + 并行，≈ 5 min |
| 全量 pristine 检查（确认用） | 109 × 45 s ≈ 82 min | 每 run 1 次，< 1 min |
| 变异体（子集） | 7350 × ~1.5 s / 12 ≈ 15–25 min | 同左 |
| 存活者确认 | 574 × 43 s ≈ **411 min** | 574 × ~50 s / 12 ≈ 40 min |
| 合计 | **~9–10 h**（比默认更差） | **~60–75 min** |

（“~1.5 s / 变异体”取实测 0.71–4.47 s 的中间；12 worker 下 SMT 争用会把单个
变异体拉长，因此给区间。）

**回答问题 2：** 结果在已测的 99 个变异体上逐 mutation_id 与全量快照一致
（killed/survived/uncovered 均一致），但直接打开 `--select-specs` 跑全量会比
默认更慢；需要先修 2.1 的三处。正确性前提不变：子集结果对“独立的 spec 文件”
成立，存活由全量确认兜底（GLOSSARY “full-suite confirmation”）。

## 3. 超时变异体

参考快照的文件级墙钟（快照 `:tested-at` 相邻差，`snap.py`）里，非 `ui` 耗时
超过 250 s 的 10 个文件：

| 墙钟 | killed | survived | 文件 |
|---:|---:|---:|---|
| 763 s | 118 | 16 | src/rules/items/phase.lua |
| 719 s | 20 | 12 | src/turn/scheduler/await.lua |
| 667 s | 14 | 2 | src/turn/phases/registry.lua |
| 634 s | 14 | 5 | src/turn/waits/await_transitions.lua |
| 623 s | 11 | 0 | src/rules/ports/contract_helper.lua |
| 619 s | 6 | 1 | src/turn/phases/pre_move.lua |
| 618 s | 8 | 0 | src/turn/phases/phase_wait.lua |
| 365 s | 94 | 24 | src/state/visual_hold/init.lua |
| 364 s | 96 | 10 | src/turn/loop/init.lua |
| 306 s | 49 | 7 | src/rules/items/obstacle_clear_walk.lua |

合计约 95 min（非 `ui` 321 min 的 30%）。前 7 个文件的墙钟落在一次全量超时
（覆盖率时长约 56–62 s × 10）附近，说明**每个文件至少有一个超时变异体**；
文件内的其他变异体几十秒就跑完，剩下的时间里只有那个超时的 worker 在跑。
快照不记录单个变异体的耗时，所以只能给出下界：≥ 7 个文件、≥ 7 个超时变异体，
每个约 560–600 s 的墙钟，而且基本无法与其他工作并行。

`phase_wait.lua` 复现（4 worker，`phase_wait.log`）：8 个变异体里 6 个在
7–24 s 被杀死，`:22 and -> or` 和 `:22 == -> ~=` 让 `resolve_result` 永远返回
`wait_action_anim`，回合循环停不下来。两个进程 8 分钟后 RSS 分别是 2.40 GB 和
2.77 GB，还在涨，我手动 `kill -9` 了它们（mutator 记为 killed，与参考结果一致）。
12 个 worker 同时撞上这类变异体会吃掉十几 GB 内存，这和此前全量运行被 OOM
杀掉的现象吻合。

## 4. 其他杠杆

| 杠杆 | 预计节省（非 ui） | 正确性风险 |
|---|---|---|
| **A. 修 `--select-specs` 的三处串行**（2.1），并把它作为 Lua 默认 | 321 → ~60–75 min | 与现在的 `--select-specs` 相同：子集杀死依赖 spec 独立性；存活仍由全量确认。已实测 99/99 一致 |
| **B. 每变异体内存上限**（`Popen(preexec_fn=resource.setrlimit(RLIMIT_AS, …))`，例如 max(2 GB, 3 × pristine 峰值)） | 消除 OOM；超时变异体提前以内存错误失败，省下大部分 ~560 s | Lua 分配失败会抛 `not enough memory`，测试失败计为 killed，和超时同类。pristine 峰值 0.57–0.77 GB，Lua 的 VSZ≈RSS（实测 672160 vs 670836 KB），所以 RLIMIT_AS 不会误伤。仅当合法存活的变异体需要超过上限的内存时，才会把 survived 改成 killed |
| **C. 跨文件共享 worker 队列**（文件之间不再串行屏障；或至少让下一个文件的变异体填满空闲 worker） | 默认模式下大部分长尾（≈95 min 中的大头）；A 实施后收益变小 | 无语义变化；需要多文件同时 overlay，pool 的 `prepare/validate` 要按文件区分 |
| **D. 降低超时倍数 / 子集化超时** | A 已经自动给出子集超时（实测 44–60 s 对 560 s） | ADR 0001 已记录超时窗口例外；直接把全量 `--timeout-factor` 调低会扩大这个窗口，不推荐单独做 |
| **E. 覆盖 spec 优先的全量顺序**（`busted <覆盖 spec…> <其余 spec…>`，test_file_loader 保留参数顺序，只对目录内排序） | 与 A 量级相近：杀死成本约等于子集，存活者只跑一次全量（省掉 A 中重复执行的子集前缀和一次进程启动） | 存活判定所用的全量顺序不再是字母序；有顺序依赖的套件会与默认结果不同。每种顺序都要单独做 pristine 检查，否则只能借用子集基线来证明前缀能通过。比 A 复杂，收益只是 A 的边际 |
| **F. 12 vs 更少 worker** | 6 核 12 线程；4 worker 下杀死的变异体约 17 s、存活者约 50 s；12 worker 会遇到 SMT 争用（12 个 worker 的吞吐量这次没有测，因为 RAM 与另一个 agent 共用）| pristine 0.6–0.8 GB × 12 ≈ 9 GB，**必须配 B** 才安全；有了 A，变异体本身多在 2 s 以内，worker 数的边际收益变小 |
| **G. 常驻 busted / fork server** | 启动只要 0.67 s；默认模式下可忽略，A 模式下约 7350 × 0.5 s / 12 ≈ 5 min | 需要 fork（luaposix 没装，要引入 C 依赖）；busted 的 insulation 会让每个 spec 重新加载，常驻省不掉加载成本。不推荐 |

### 4.1 跨文件队列的离线模拟（杠杆 C）

模型：参考快照的 7350 个变异体，存活者耗时 = T（12 worker 争用下的全量套件
时长），被杀者耗时 = 1 s + T × min(1, 首个覆盖 spec 位置 + 0.05)；3 节中 7 个文件
各放一个 12T 的超时变异体；LPT 式贪心分配给 12 个 worker。比较“文件间屏障”
（现状）与“全局队列”两种调度：

| T | 屏障（现状） | 全局队列 | 屏障、无超时 | 全局、无超时 |
|---:|---:|---:|---:|---:|
| 50 s | 302 min | 176 min | 242 min | 170 min |
| 60 s | 360 min | 209 min | 289 min | 203 min |

按 T≈53 s 校准到实测 321 min：**全局队列约 186 min（-42%）**；只消掉超时、
保留屏障约 255 min（-20%）；全局队列下超时几乎不再影响墙钟（被其他文件的
变异体摊平）。这只是默认（全量）模式的估计，与杠杆 A 不叠加计算。

模型校验：快照不记录单变异体耗时，所以时长是建模得到的。用 1.4 节 4 worker
实测的 68 个变异体对拍：取 T=50 s（该负载下的全量时长）时，模型均值 24.1 s，
实测均值 24.1 s（模型中被杀者均值 17.3 s）。12 worker 的 T 只能用总墙钟反推，
没有直接测量。

## 5. 建议的前三项

1. **修 `--select-specs` 并设为 Lua 默认**（存活者确认放到 pool 里并行；全量
   pristine 检查每次 run 只做一次；去掉子集基线，用覆盖率索引的时长定超时，由
   每次 run 去重的 worker 检查兼任“子集通过”的证明）。预计非 `ui` 321 →
   ~60–75 min（-75–80%）。已有的实测：99 个变异体逐 id 一致。
2. **每变异体内存上限（RLIMIT_AS）**：不再 OOM，也让死循环类超时变异体在
   内存耗尽时提前失败。默认模式下这 7 个以上超时文件约有 70–95 min 的长尾，
   内存上限能省下其中一大部分（失败时间取决于增长速度：350 MB/min 时约 4 min 到
   2 GB）。和 A 叠加后，子集超时本来就只有 44–60 s，这一项的作用主要是安全。
3. **跨文件 worker 队列**（文件之间不设屏障）：默认模式下能回收超时与存活者
   拖出的空闲 worker 时间，约 95 min 中的大部分；在 A 之后，用来摊平存活者
   确认（~43 s/个）分布不均的尾巴。

## 6. 复现材料索引（本机 /tmp，易失）

- `/tmp/nonui-work/snap.py`、`files.json`：快照 → 文件墙钟
- `/tmp/nonui-work/order2.py`、`sites.json`：变异体 → 覆盖 spec / 位置
- `/tmp/nonui-work/tap_timed.py`、`mut-cmd.sh`：pristine 全量 TAP 计时
- `/tmp/nonui-work/compare.py`：与 `/tmp/mono-full` 逐 mutation_id 比对
- 日志：`phase_wait.log`（默认，超时复现）、`default2.log`（默认）、`ss2.log`、
  `ss3.log`（`--select-specs`）；快照副本 `run-default2/`、`run-ss2/`、`run-ss3/`
