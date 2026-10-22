# monopoly `src/ui` 变异体为何是其他目录的两倍成本

日期：2026-10-22。环境：WSL2 Ubuntu，Lua 5.4 + busted 2.3.0（`~/.luarocks`），
mutator @ `90bd430`（`.venv`），monopoly @ `9bc18c4`。实验 worktree
`/tmp/mono-ui`（已删除）；全量运行的部分快照与覆盖索引取自 `/tmp/mono-full`
（只读）。脚本与原始日志在 `/tmp/uicost/`（本机临时目录，随重启消失）。
未修改 mutator 与 monopoly 源码。

## 0. 结论摘要

1. **根因是 spec 文件的执行顺序加上 `--no-keep-going`，而不是 ui 测试本身更慢。**
   默认（`--full-suite`）每个变异体都跑全量套件并带 `--no-keep-going`
   （`src/mutator/engine.py:304`），被杀的变异体在第一个失败的测试处停下。
   busted 按 `.busted` 的 ROOT 顺序、在每个根目录内按路径字典序执行
   （`busted/modules/test_file_loader.lua:50` `table.sort(fileList)`），所以
   `test/behavior/ui/` 排在 `rules`、`turn` 等目录之后，**要到全量 35.0 s CPU
   的第 24.5 s 才开始执行**。因此杀死一个 ui 变异体之前，必须先跑完约 70% 的
   非 ui 套件。
2. ui spec 本身很便宜：187 个文件共 9.7 s CPU，其中 8.7 s 花在加载/注册
   （auto-insulation 让每个文件重新 require 模块图），测试体只占 0.7 s；单个
   测试平均 4.6 ms，非 ui 为 6.1 ms。
3. **超时不是成本来源。** 样本中 74 个变异体没有一个超时；超时阈值为
   452.6 s（覆盖运行 45.3 s × 10），而一个存活者实际需要 37–55 s，余量约 8–12 倍。
4. 在这 4 个样本文件上，`--select-specs` 的**结果逐点一致**（58 killed /
   16 survived），但墙钟**没有变快**（861 s 对 808 s）：子集运行本身只要
   0.35 s/个，而 16 次存活者全量确认在单个 worker 上**串行**执行，耗时 672 s。
5. 推荐修复（按收益排序）：(1) mutator 在全量模式下**让覆盖该行的 spec 排在最前**
   （模型估算 ui 每变异体 CPU 从 24.5 s 降到约 3.4 s，非 ui 从 9.9 s 降到约
   3–4.7 s）；(2) 存活者全量确认与子集 baseline **并行化**；(3) monopoly 把
   排在 ui 之前的 fuzz spec（约 7.3 s，占套件 21%）移出默认任务或移到末尾。

## 1. 测量方法

### 1.1 每个 spec 文件的耗时（无覆盖钩子）

在 worktree 中写了一个诊断用 busted 输出 handler（`tmp/filetime.lua`、
`tmp/filetime2.lua`，订阅 `file start/end`、`test start/end`，用 `os.clock`
记录），然后以 mutator 生成的同一条命令（`_lua_plan` 输出加 `-o tmp/filetime.lua`）
运行一次原仓全量：

```
returncode 0，墙钟 37.4 s；handler 内 suiteEnd 35.3 s CPU，按文件求和 35.0 s
```

按执行顺序汇总（`/tmp/uicost/a3.py`）：

| 目录 | 文件 | 测试 | CPU | 开始于 |
|---|---:|---:|---:|---:|
| behavior/app … player | 102 | 1136 | 3.7 s | 0.0 s |
| behavior/rules | 117 | 1523 | 10.3 s | 3.7 s |
| behavior/scenarios/state/support/根 | 33 | 282 | 1.8 s | 14.0 s |
| behavior/turn | 87 | 1025 | 8.6 s | 15.9 s |
| **behavior/ui** | **187** | **2132** | **9.8 s** | **24.5 s** |
| contract/* | 21 | 164 | 0.8 s | 34.2 s |

最贵的单文件都不在 ui：`rules/items/test_item_timing_fuzz_b.lua` 1.84 s、
`turn/test_item_timing_loop_fuzz_c.lua` 1.75 s、`rules/items/test_item_timing_fuzz.lua`
1.39 s、`turn/test_item_timing_loop_fuzz_b.lua` 1.18 s、`..._loop_fuzz.lua` 1.11 s、
`turn/test_auto_runner_timeout.lua` 0.85 s，合计约 8.1 s（其中 5 个 fuzz 文件约
7.3 s），全部排在 ui 之前。

### 1.2 加载与执行的拆分

`filetime2.lua` 记录每个文件从 `file start` 到第一个 `test start` 的时间
（顶层 + describe 体，也就是 require 与注册），以及测试体时间（含
before/after_each）：

| 分组 | 总 CPU | 加载+注册 | 测试体 | 其他 | 测试数 | ms/测试 |
|---|---:|---:|---:|---:|---:|---:|
| 非 ui | 25.1 s | 14.7 s | 9.1 s | 1.3 s | 4130 | 6.08 |
| ui | 9.7 s | 8.7 s | 0.7 s | 0.3 s | 2132 | 4.56 |

ui spec 的成本几乎全在模块图重载上，测试体本身很轻；不存在 sleep、大型
fixture 或等待超时之类的问题。

### 1.3 静态模型：用全量运行的快照和覆盖索引推算每个变异体的成本

数据来源：`/tmp/mono-full/.metrics/mutate`（452 个快照）与
`target/coverage/lua/spec-coverage.json`（每行由哪些 spec 覆盖，以及每个 spec
在覆盖模式下的耗时）。用 `mutator.functions.sites_in_file` 把每个变异点映射到
行，再按 busted 的执行顺序找到“第一个覆盖该行的 spec”，用 1.1 的无钩子耗时
累计（`/tmp/uicost/a2.py`、`a4.py`）。被杀变异体的成本取下界“跑到第一个覆盖
spec 结束”，存活者取全量 35.0 s：

| 分组 | killed | survived | 覆盖被杀行的 spec 数（均值/中位） | 第一个覆盖 spec 结束于（均值/中位） | 估算每变异体 CPU |
|---|---:|---:|---:|---:|---:|
| 非 ui | 6776 | 574 | 17.4 / 7 | 7.8 / 7.9 s | **9.9 s** |
| ui | 2768 | 246 | 5.3 / 3 | 23.6 / 25.4 s | **24.5 s** |

模型比值为 2.5×，实测为 6.1/2.8 = 2.2×；按子目录排序也与实测一致
（模型：ui/render 703、ui/coord 247、rules/items 166、turn/waits 128、
ui/input 127、ui/ports 127 CPU 分钟；实测墙钟分钟排序为 render 155、coord 55、
rules.items 41、turn.waits 37、turn.phases 37、ui.input 34、ui.ports 29）。

ui 被杀的行里，覆盖它的 spec 平均只有 5.3 个、覆盖模式下合计 1.08 s，
少于非 ui 的 17.4 个、5.61 s。**所以问题不是 ui 的 spec 更多，而是这些 spec
排得太晚。** 另外 2768 个中有 641 个（23%）的第一个覆盖 spec 在 ui 目录之前，
但真正杀死它们的测试多数仍在 ui 目录里（见 1.4 的 ok 计数）。

快照中仍有 39 个非 ui、10 个 ui 变异点没有记录结果（`?`）。未跑的 ui 文件共
1045 个变异点，分布为 view 351、render 260、screens 202、state 189、
seams 21、screens_registry 13、schema 9。

### 1.4 实跑样本

以下 2 个 ui 文件和 2 个 rules 文件的变异点数相近：`src/ui/render/board/scene.lua`、
`src/ui/render/board/tile.lua`、`src/rules/items/post_effects.lua`、
`src/rules/items/steal.lua`。在 `/tmp/mono-ui` 用包装器
`/tmp/uicost/run_logged.py` 运行（它 monkeypatch `CommandRunner.run`，逐命令
记录耗时、超时、退出码和 TAP 的 ok/not ok 数，不改 mutator 源码）：

```bash
cd /tmp/mono-ui
RUNLOG=/tmp/uicost/full-suite.log python /tmp/uicost/run_logged.py \
  --timings --max-workers 4 src/ui/render/board/scene.lua src/ui/render/board/tile.lua \
  src/rules/items/post_effects.lua src/rules/items/steal.lua
```

`--timings`：

```
timing coverage: 45.4s total, 1 run(s)
timing worker-check: 42.6s total, 1 run(s)
timing mutant: 2669.9s total, 74 run(s), 36.08s each
timing mutation-run: 808.2s total
```

逐变异体（4 worker 并行；期间同机还跑着 1.2 的诊断，部分样本被拉高）：

| 类别 | n | 墙钟均值 | 范围 | 停止前 ok 的测试数 |
|---|---:|---:|---:|---:|
| rules 被杀 | 30 | 10.5 s | 6.0–16.9 s | 1226–1849 |
| ui 被杀 | 28 | 48.8 s（中位约 42 s） | 39.0–117.8 s | 4246–4696 |
| 存活（全量 6262） | 16 | 61.8 s（无干扰时约 52 s） | 46.7–127.8 s | 6262 |

ui 被杀变异体在跑过约 4250 个测试之后才失败，正是 ui 目录的起点
（排在它前面的测试共 4130 个）。结果：58 killed / 16 survived / 0 uncovered。

## 2. 超时（问题 2）

- 阈值：`max(2, 覆盖运行秒数 × 10)`（ADR 0001）。本次为 452.6 s；全量运行时
  覆盖运行约 60 s，阈值约 600 s。
- 样本的 74 个变异体中超时数为 **0**；单个变异体最长 127.8 s（存活者，且有
  CPU 争用），无争用时存活者约 37–55 s，阈值约为实际需要的 8–12 倍。
- 快照里超时记为 `killed`，无法与普通失败区分，所以从 `/tmp/mono-full` 无法
  回溯统计全量运行的超时数。如果有超时，每个会占用一个 worker 约 10 分钟。
  按实测 ui 281 分钟、约 2750 个变异体、12 worker 计，ui 与非 ui 的“每变异体
  墙钟 / 模型 CPU”比值都在 3 左右（主要来自争用），说明没有大批超时把 ui
  单独拖慢。建议 `--timings` 单独计数超时（见 4.1-4），下次全量就能直接回答。

## 3. `--select-specs`（问题 3）

用同样 4 个文件，并复用上一轮的覆盖索引：

```bash
RUNLOG=/tmp/uicost/sel.log python /tmp/uicost/run_logged.py --timings --max-workers 4 \
  --select-specs --reuse-coverage --mutate-all <同上 4 个文件>
```

```
timing baseline: 46.3s total, 13 run(s), 3.56s each        # 每个不同子集在原仓跑一次
timing worker-check: 135.0s total, 20 run(s), 6.75s each   # 每个子集命令在 worker 再跑一次
timing mutant: 25.5s total, 74 run(s), 0.35s each
timing survivor-confirmation: 671.6s total, 16 run(s), 41.97s each
timing mutation-run: 861.4s total
```

- **结果**：与全量模式逐个变异体比较（排序后 diff `KILLED/SURVIVED` 行）完全
  一致，exit 3（有存活者）。
- **耗时**：子集运行把每个变异体从 36 s 降到 0.35 s。但 16 个存活者都要做全量
  确认，而且 `run_mutants` 只在 `directories[0]` 上串行确认
  （`src/mutator/workers.py:729-749`），一项就占 672 s；子集 baseline（主进程
  串行，`engine.py:241`）与 worker pristine 检查对同一条命令各跑一次，又多出
  181 s。总墙钟反而比全量多 7%。
- 推广到全仓（模型，CPU 口径）：ui 每变异体约 0.54 s（子集）+ 2.86 s（存活者
  确认摊薄）≈ 3.4 s；非 ui 约 2.1 + 2.7 ≈ 4.8 s。**`--select-specs` 之后剩下的
  成本几乎都是存活者全量确认**，而它目前没有并行。
- ui 的正确性前提（spec 独立性）在这个样本上成立，但 4 个文件不足以证明全仓
  都成立。

## 4. 修复建议（问题 4）

以下估算以 1.3 的 CPU 模型为准（全量运行实测墙钟约为模型 CPU × 3 / 12 worker）。
全量首次运行的现状约为 10 小时。

### 4.1 mutator 侧

1. **全量模式下把覆盖 spec 排到最前（推荐，收益最大）。** 对一个被选中的
   变异点，命令改为显式传入全部 547 个 spec：先放覆盖该行的 spec，其余按原顺序。
   busted 遇到位置参数会忽略 `.busted` 的 ROOT，按参数顺序执行（`_lua_plans`
   传子集时已经这样用）。存活者仍然跑完整个集合，只是顺序不同；被杀者在覆盖它的
   spec 里就停下。
   - 估算（`a5.py`，CPU 每变异体，含存活者全量）：ui 24.5 s → 3.0–3.4 s
     （-86%）；非 ui 9.9 s → 2.8–4.7 s（-55% 至 -70%）。全仓首次运行从约
     10 小时降到约 3 小时量级，ui 不再比其他目录贵。
   - 粒度：如果每个变异点都按自己的覆盖集合排序，每种排序都是新命令，各需一次
     pristine 检查（约 40 s）。建议**按文件取覆盖 spec 的并集放最前**，每个文件只
     多一次 pristine 检查；平均每文件约 18 个变异体，每个省约 20 s，净收益明显。
   - 正确性风险：spec 之间若有顺序依赖（全局状态泄漏），被杀者的失败点或存活者
     的判定都可能变化。这个风险比 `--select-specs` 弱，因为 spec 集合不变、只有
     顺序变，但并非为零。pristine 检查只能证明“该顺序下原仓全过”，不能证明
     变异后结论不变。上线前应在若干目录上对全量跑一遍，逐 `mutation_id`
     比对 outcomes。
2. **并行化存活者全量确认与子集 baseline。** 确认阶段只用一个 worker 是
   `--select-specs` 最大的瓶颈；把存活者分发到全部 worker，并把“子集在原仓跑
   baseline”和“子集在 worker 做 pristine 检查”合并为一次。样本估算：
   672 s → 约 170 s（4 worker），baseline/check 181 s → 约 90 s，墙钟
   861 s → 约 360 s（比全量模式快 2.2×）。结果不变，因为执行的命令和超时都没变。
3. **增量运行要考虑存活者的成本。** 默认 `--since-last-run` 会把存活者排在
   最前面重跑（`engine.py:390`），而在全量模式下每个存活者都跑满全量。修复 1
   不会改善这部分；修复 2 只在 `--select-specs` 下生效。
4. **`--timings` 增加超时计数**（以及被杀/存活各自的均值）。成本很低，可以让
   下次全量直接回答问题 2；不影响结果。
5. **不建议仅为提速而收紧超时。** 当前没有证据表明超时在消耗时间，而缩短超时
   会把慢但正确的存活者变成 killed，违反“结果逐个一致”。

### 4.2 monopoly 测试套件侧

1. **fuzz spec 移出默认 `.busted` 任务或移到末尾**：5 个
   `*item_timing*fuzz*` 文件约 7.3 s（套件的 21%），全部排在 ui 之前。移走后
   每个被杀的 ui 变异体省约 7 s（现状下约 -30%），非 ui 中 rules/items 之后的
   目录也受益。可以单独放进一个 busted 任务，在 CI 里跑。代价：变异测试不再
   用 fuzz 杀变异体，可能出现新的存活者。这是测试范围的决定，属于 monopoly
   仓的选择。
2. **补测 ui 存活者**：做完修复 1 后，246 个 ui 存活者约占剩余 ui 成本的 85%
   （2.86 / 3.4 s）。每杀掉一个存活者就省一次全量（约 35 s CPU）；也可以把
   确认为等价变异的点标记排除。
3. **减轻 ui spec 的 require 图**：ui spec 的 8.7 s 几乎都是 auto-insulation
   下的模块重载。共享 fixture 改成按需 require、拆掉“全家桶”式支持模块，
   可以直接降低每个 ui spec 的固定成本。monopoly#197（`canonical()` 缓存）属于
   同一方向，已测 -8%。
4. 改目录名让 ui 字典序靠前属于投机做法，不建议（只是把问题转移给别的目录）。

### 4.3 busted 侧

- 执行顺序不需要改 busted：显式传文件参数就能控制顺序（见 4.1-1）。
- `--no-auto-insulate` 能大幅减少加载时间，但会改变 spec 之间的隔离语义，
  会引入与 `--select-specs` 同类甚至更强的独立性风险，**不建议**。
- lunarmodules/busted#782（fasttrace）合并后可以去掉 vendored 补丁，与本问题
  无关，按原计划推进。

### 4.4 排名与预期节省

| 排名 | 措施 | 侧 | 预计节省 | 结果风险 |
|---|---|---|---|---|
| 1 | 覆盖 spec 排在最前（按文件并集） | mutator | ui 每变异体 CPU -86%，非 ui -55% 至 -70%；全量约 10 h → 约 3 h；样本实测墙钟 -49%、结果一致（第 5 节） | 顺序依赖（弱于 `--select-specs`），需逐点对拍 |
| 2 | 存活者确认与子集 baseline 并行 | mutator | `--select-specs` 墙钟约 -58%（样本 861 s → 约 360 s） | 无 |
| 3 | fuzz spec 移出默认任务或移到末尾 | monopoly | ui 被杀变异体约 -30%（无修复 1 时）；有修复 1 时收益很小 | 测试范围变化，可能新增存活者 |
| 4 | 补测/排除 ui 存活者 | monopoly | 有修复 1 后 ui 剩余成本最多 -85% | 无（测试变强） |
| 5 | 减轻 ui spec 的 require 图 | monopoly | ui spec 固定成本的一部分（8.7 s 上限） | 无 |
| 6 | `--timings` 计超时 | mutator | 0（可观测性） | 无 |

## 5. 原型验证：覆盖 spec 排在最前（4.1-1）

`/tmp/uicost/run_reorder.py` 不改 mutator 源码，只 monkeypatch 两处：
`engine._lua_plans` 改为给每个文件生成一条命令，显式列出全部 547 个 spec，
其中该文件被选变异点的覆盖 spec 并集排在最前，其余按 busted 默认顺序；
`engine.run_mutants` 的 `confirmation` 置空，因为每个变异体本来就跑完整集合。
借 `--select-specs` 开关进入这条路径。busted 对显式文件参数保持参数顺序
（`test_file_loader.lua:17-18` 文件直接成表，`getAllTestFiles` 按参数依次拼接）。
在新 worktree `/tmp/mono-ro`（已删除）上运行，样本文件与 1.4 相同：

```bash
RUNLOG=/tmp/uicost/ro.log python /tmp/uicost/run_reorder.py --timings --max-workers 4 \
  --select-specs <同 1.4 的 4 个文件>
```

```
REORDER post_effects.lua: 18 covering specs first of 547
REORDER steal.lua: 3 / scene.lua: 1 / tile.lua: 3 covering specs first of 547
timing coverage: 44.7s total
timing worker-check: 165.9s total, 4 run(s), 41.48s each   # 每文件一种顺序，各一次
timing mutant: 777.3s total, 74 run(s), 10.50s each
timing mutation-run: 408.8s total
```

| 指标 | 全量（现状，1.4） | 覆盖 spec 排最前 |
|---|---:|---:|
| 结果 | 58 killed / 16 survived | **逐个一致**（排序后 diff 为空） |
| 被杀变异体墙钟 | rules 约 10.5 s，ui 约 42 s | **0.55 s**（0.34–1.33 s），失败前 ok 0–138 个 |
| 存活者墙钟 | 约 52 s | 约 45 s（全量顺序不同） |
| worker 检查 | 1 × 42.6 s | 4 × 41.5 s |
| 超时 | 0 | 0 |
| mutation-run 墙钟 | 808 s | **409 s（-49%）** |

样本中 ui 和 rules 的被杀成本都降到 1 秒以内，ui 不再特殊。剩余成本是：
16 个存活者 × 约 45 s（约占 mutant 阶段的 93%），以及每个文件多一次 pristine
检查（约 41 s，文件数多时按 4.1-1 的“每文件一次”计）。因此全仓的下一步收益来自：
- 补测或排除存活者（4.2-2）；
- pristine 检查：可以考虑把“同一 spec 集合的重新排序”视为已由覆盖运行证明，
  但这正好依赖 spec 独立性这个未经证明的前提，需要单独决定是否接受（类似
  ADR 0001 的例外）；
- 存活者全量运行仍在各 worker 并行（本原型没有串行确认的问题）。

样本只有 74 个变异体、4 个文件，不足以证明全仓没有顺序依赖。正式落地前应选
几个目录（含 ui/render、turn/waits）对全量跑一遍，逐 `mutation_id` 比对。

## 6. 复现材料索引（本机 /tmp，易失）

- `/tmp/uicost/run_reorder.py`、`ro.{out,log}`：第 5 节原型与其输出

- `/tmp/uicost/a2.py`、`a4.py`、`a5.py`：静态模型（读取 `/tmp/mono-full` 快照与覆盖索引）
- `/tmp/uicost/full.tsv`、`full2.tsv`：每个 spec 文件的耗时（含加载/测试体拆分）
- `/tmp/uicost/run_logged.py`：记录 `CommandRunner.run` 的包装器
- `/tmp/uicost/full-suite.{out,log}`、`sel.{out,log}`：样本实跑的输出与逐命令日志
- `/tmp/uicost/snap-full/`：全量模式样本的快照（与 `--select-specs` 对拍用）
- `/tmp/uicost/filetime.lua`、`filetime2.lua`：诊断用 busted 输出 handler（使用时须放在
  仓根内，例如 `tmp/`，才能通过 monopoly 的路径守卫）
