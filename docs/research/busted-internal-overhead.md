# busted 内部开销研究：monopoly 全量套件与 `--list` 的加载阶段

日期：2026-10-21。环境：WSL Ubuntu，Lua 5.4.6（`/usr/bin/lua5.4`），busted 2.3.0-1、
penlight 1.15.0-1、mediator_lua 1.1.2-0（`~/.luarocks/lib/luarocks/rocks-5.4/` 各目录）。
基准项目：monopoly @ `9bc18c4`（547 个 spec、6262 个测试），克隆到只读 scratch
`/tmp/busted-research/mono`；未改动 `~/dev/github/monopoly` 与 `~/.luarocks`。
所有 scratch 材料在 `/tmp/busted-research/`（本机临时目录，随重启消失）。

## 0. 结论摘要

1. `--list`（只加载不跑测试体）约 33 s CPU 中，**约 22 s（约三分之二）花在
   `busted.getTrace` 里的一次 `debug.traceback`**；它不是"目录名拆分"慢，而是 Lua 5.4
   `luaL_traceback` 的 `pushglobalfuncname` 为**每个栈帧**扫描整个
   `package.loaded` 找全局函数名，而 busted 生成的约 21 帧里只有前 2–3 帧被
   `trimTrace` 保留、其余全部丢弃。
2. 此前采样剖析器（`debug.sethook` count 钩子）把热点指到
   `pl.path.dirname`/`rejectAll`/mediator，是**假象**：count 钩子只计 Lua 指令，
   C 函数内的时间（`debug.traceback`）不可见。用 `os.clock` 包含式计时的实测份额
   为：getTrace 21.9 s（其中 traceback 21.7 s）、publish 0.49 s、rejectAll 0.057 s。
3. 补丁 P1（Lua 侧只生成"会被保留的那几帧"的 traceback，格式逐字节对齐
   `luaL_traceback`）把 `--list` 从约 33 s 降到约 16 s（-49%），全量套件从约
   52 s 降到约 31 s（-39%），`--list` 输出逐字节一致、全量 TAP 的 ok/not ok
   名单逐字一致。
4. `rejectAll` / mediator / `dirname` 缓存的优化方向都正确但量级在百分之一以下，
   不值得单独上游 PR；可作为同一 PR 的附属提交或不做。
5. 上游（lunarmodules/busted）维护活跃（2026 年仍在合并 PR），但没有任何
   issue/PR 处理加载/注册期成本；CONTRIBUTING 明确欢迎 "Inefficient blocks of
   code" 类 PR。建议上游化 P1。

## 1. 测量方法

mutator 对 Lua 项目以 `.busted` 配置直接调 `busted.runner`（`src/mutator/runner.py`
`_lua_plan`，第 302–318 行）。复现命令（在 `~/dev/github/mutator` 下）：

```bash
.venv/bin/python /tmp/busted-research/scripts/bench.py <mono路径> <scratch树> <list|full> <重复数> [输出前缀]
```

`bench.py` 组装 `LUA_PATH="$M/?.lua;$M/?/init.lua;<scratch树>/share/lua/5.4/?.lua;...;$HOME/.luarocks/share/lua/5.4/?.lua;..."`
后用 `_lua_plan` 得到命令并计时。

### 1.1 monopoly 守卫与 scratch 树的冲突

monopoly 的 helper（`test/busted_helper.lua` →
`tools/packages/test_runner/cache_loader.lua`）要求 `package.path` 每个模板都在
仓根、mutation worker 或"受控 Lua 环境"内（`cache_loader.lua:132-141`
`_is_controlled_template`，要求路径在某个 `luarocks config rocks_trees` 根之下，
`:173` 报错 `"search path outside repo root/controlled environment"`）。因此直接把
`/tmp/busted-research/...` 前置到 `LUA_PATH` 被拒绝，实测报错：

```
busted: failed running the specified helper (test/busted_helper.lua), error:
tools/packages/test_runner/cache_loader.lua:173: search path outside repo
root/controlled environment: /tmp/busted-research/tree-orig/?.lua
```

替代方案：monopoly 自带扩展点 `MONOPOLY_LUAROCKS_EXE`
（`tools/foundation/lua_env.lua:65-73`），把该变量指到包装脚本
`/tmp/busted-research/scripts/luarocks-wrap`，让它在 `config rocks_trees` 时额外
报告一个 rocks-tree 形态的 scratch 根（`<root>/share/lua/5.4/`，`_is_controlled_template`
恰好要求这种后缀）。这样 patched busted 树通过守卫被实际装载（已用探针确认
`busted.core` 来自 scratch 树），且不写任何 luarocks 状态。
（`LUAROCKS_CONFIG` 指到裸替换配置会损坏 luarocks 自身加载，已排除。）

### 1.2 包含式计时（真实的成本分解）

在 scratch 树中给关键函数包 `os.clock` 计时器（`/tmp/busted-research/rt-instr`，
`brprof.lua` + 对 core/block/test_file_loader/init 的插桩），`--list` 全量一次：

| 计时点 | 累计 CPU | 调用次数 |
|---|---|---|
| `file executor`（每 spec 文件顶层+describe 体总时长） | 44.5 s / 总 44.9 s | 547 |
| `busted.getTrace` | **21.93 s** | 7229 |
| 其中 `debug.traceback`（getTrace:81） | **21.67 s** | 7229 |
| `loadTestFile`（loadfile 编译） | 0.18 s | 547 |
| `busted.publish`（顶层，mediator 派发） | 0.49 s | 35191 |
| `block.rejectAll` | 0.057 s | 10471 |

7229 = 6262 个 it/pending + 约 967 个嵌套 describe（每次注册都取 trace，
`busted/core.lua:266-271`）。**剩余的约 22.6 s 是 spec 顶层/describe 体自身的
`require` 与 fixture 构造，不在 busted 框架内。**

### 1.3 根因复现（微基准）

`/tmp/busted-research/scripts/tb_micro.lua`（Lua 5.4.6，20 层深的调用栈，
`debug.traceback('', 2)`）：

```
baseline package.loaded                 0.077 ms/call
+400 modules x 25 fields                4.566 ms/call
registry._LOADED swapped to {}          0.008 ms/call
```

即向 `package.loaded` 加入 400 个模块（共 10000 个函数字段）后单次 traceback
慢约 60 倍；把 `debug.getregistry()._LOADED` 临时换掉又恢复廉价。这解释为何
monopoly（加载数百个模块后）单帧成本高、且采样剖析器看不到（时间全在 C 内）。

## 2. 上游现状（GitHub 调查，2026-10-21）

### lunarmodules/busted

- 最新发布 **v2.3.0（2026-01-07）**；
  <https://github.com/lunarmodules/busted/releases/tag/v2.3.0>。无更新版本。
- master（`22f8089f`，2026-08-25）相对 v2.3.0 只改
  `busted/init.lua`（多次 finally，#753）、`busted/modules/cli.lua`（参数引用，
  #751/#766）与 CI/文档；**热点路径（core.lua / block.lua / context.lua /
  status.lua / filter_loader.lua）与 v2.3.0 完全一致**（本地
  `git diff v2.3.0 master -- busted/` 验证）。本地安装树与 v2.3.0 的差异只有
  缺少 `busted/languages/id.lua`（rockspec 未随源码更新列表）+ 上述两处，
  不影响本研究。
- 搜索 `slow / performance / getTrace / speed / rejectAll / large test suite /
  startup time / traceback / cache / perf / optimize` 全部 issue/PR：**没有任何
  一个针对加载/注册期成本、getTrace、rejectAll 或 mediator 开销**。相关的只有
  古老的 #48/#278（每文件进程/线程并行，已关闭）、#49（应实现性能测试，仍
  open）。
- 维护活跃度：最近合并 PR 均由 Tieske / alerque 处理——#780（2026-07-30 当日
  合并）、#766（2026-01-21 当日）、#760（2025-12-29 当日）；也有积压案例
  #753（2025-06 → 2026-08 才合并）。6 个 open PR。CONTRIBUTING.md 明确把
  "Inefficient blocks of code" 列为欢迎修改
  <https://github.com/lunarmodules/busted/blob/master/CONTRIBUTING.md>；测试要求
  是"先跑仓库自身 busted 套件"，CI（`.github/workflows/busted.yml`）在
  Lua 5.1–5.5 + LuaJIT × penlight 1.15.0 矩阵上 dogfood 自身。

### Olivine-Labs/mediator_lua

- 仓库自 2020-02-24 起无推送；installed 1.1.2-0 == master（本地 diff 为空）。
- Tieske 2024-10 发起 PR #18 仅问 "@ajacksified is this still being maintained?"，
  2025-05-26 自己关闭："closed in favor of
  <https://github.com/Tieske/mediator2>"（Tieske/mediator2 最后推送 2026-03-24）。
  busted 目前仍依赖 mediator_lua，无任何 PR 替换。
- 顺带发现 installed 源码 `mediator.lua:110` 的 `Channel.publish` 写
  `if parent then`（引用的是全局 `parent`，恒为 nil），父通道事件传播实际从未
  触发——上游行为即"不传播"，任何保持现状的优化都是安全默认。
  <https://github.com/Olivine-Labs/mediator_lua/blob/master/src/mediator.lua#L110>

### lunarmodules/Penlight

- 最新发布 1.15.0（installed 即此版本）；master（`dfa483d`，2026-09-03）活跃，
  但相对 1.15.0 对 `lua/pl/path.lua` 只有错误信息改动（无性能改动）。
  `path.splitpath`（1.15.0 第 231–243 行；installed
  `~/.luarocks/share/lua/5.4/pl/path.lua:231`）是从尾部逐字符 `string.sub(P,i,i)`
  扫分隔符——就是采样剖析器看到的"热点"，但这是 getTrace 的从犯而非主因。

## 3. 各热点为什么存在（源码 + git 考古）

### 3.1 `busted.getTrace` 与 traceback

- 注册期调用点 `busted/core.lua:258-271`：每个 describe/it/pending/... 的
  publisher 都计算 `trace = busted.getTrace(ctx, 3, name)`，存入
  `element.trace`。
- 语义：`element.trace.short_src/currentline` 用于把测试归属到文件:行
  （TAP 输出 `busted/outputHandlers/TAP.lua:43-45` 打印
  `t.element.trace.short_src @ currentline`；`--list` 的
  `filter_loader.lua:83-93` 也靠它打印 `file:line: name`）；错误报告时
  `busted.safe`（`core.lua:172-181`）也调 getTrace，把
  `message/trace` 发布给输出 handler。
- `getTrace` 本体 `core.lua:65-86`：向上跳过 C 帧、luassert 帧和**与 busted 安装
  目录相同的帧**（找到 spec 帧），然后 `debug.traceback('', level)` 取全栈。
  目录跳过逻辑来自 commit `d2abcc7`（2015-01-02，"Fix getTrace() to search
  based on busted dirname"）
  <https://github.com/lunarmodules/busted/commit/d2abcc78ca5b929adc1394a6fa897fc13d8baea6>——
  之前用 `short_src:match('busted[/\\]')`，会把路径里恰含 busted 的 spec 文件误
  跳过；改成与 `path.dirname(自身 source)` 相等判断。
- traceback 的消费者：每个文件 loader 的 `getTrace` 包装都会先
  `trimTrace`（如 `busted/modules/files/lua.lua:5-9`）把 traceback **截断到第一个
  `[C]` 帧之前**；之后只有输出 handler 在失败/挂起时可能读 `trace.traceback`：
  失败用错误期取的 trace（`base.lua:68` `trace = debug or element.trace`），
  注册期 `element.trace.traceback` 只是回退（pending 且无 debug 参数时），
  且各 handler 还都要求 `options.verbose`
  （`TAP.lua:51`、`junit.lua:125`、`plainTerminal.lua:68`、`utfTerminal.lua:107`、
  `gtest.lua:118`）。**也就是说 7229 次里绝大多数 traceback 文本从未被打印。**
- 为何贵：Lua 5.4.6 `lauxlib.c` 的 `luaL_traceback`（第 131–160 行）对每个栈帧
  调 `pushfuncname`（第 100 行起），后者**先**调 `pushglobalfuncname`
  （第 79 行起）：对 `registry[LUA_LOADED_TABLE]`（即 `package.loaded`）做
  `findfield`（第 52 行起）递归两层线性扫描。规模 = 帧数 × `package.loaded`
  总字段数。上游 Lua 自己也承认：commit `3dd8ea5`（2025-04-03，进入
  v5.5.0）把 namewhat 判断挪到 global 查找之前，提交信息原文
  "'pushglobalfuncname' can be quite slow (as it traverses all globals and all
  loaded modules), so try first to get a name from the code"
  <https://github.com/lua/lua/commit/3dd8ea54daa77345a8f193e871f6792722d8e131>。
  Lua 5.4.7/5.4.8/5.5.0 该函数都在（逐版核对 lauxlib.c），5.5 只是部分缓解
  （有 namewhat 的帧跳过扫描；匿名闭包帧仍全扫）。
- 上游已有测试锚定裁短格式：`spec/cl_spec.lua:400-427` "Tests stack trackback"
  断言 `--verbose` 失败输出里的 `stack traceback:\n\t<file>:<line>: in
  function <file>:<line>` 逐字一致。

### 3.2 `block.rejectAll`

- `busted/block.lua:25-33`：每个块执行前（`exec`，`:35-40`；it 块经
  `init.lua:29-31`）把 `element.env[descriptor]` 改成抛错函数，防止
  before_each/it 体内再调 describe/it/... 等顶层注册器（"不允许在当前上下文里
  再注册块"），并禁掉 `randomize`。
- 语义依赖：当前实现按**每个元素**动态决定拒绝哪些描述符——
  `root.env[descriptor]`（该描述符确实在根环境导出过）且
  `(env ~= _G and env[descriptor] or rawget(env, descriptor))`（元素执行环境可见
  该名字，含 strict 兼容的 rawget 分支）。条件出自 commit `5dbe16f`（2016-01-11，
  "Fix for allowing `strict` usage in a test file"）
  <https://github.com/lunarmodules/busted/commit/5dbe16f5d5f8951480a878ffd15e168f5db5cf32>，
  rejectAll 本身出自 `1a8c61d`（2015-02-19，"Refactor context block execution"）。
- 每个元素 `pairs(busted.executors)`（约 17 项）+ 两次环境查找，10471 次共
  0.057 s——**量级太小，不构成优化对象**。若做，可预计算
  `root.env` 命中的描述符列表缓存，把每次循环从 pairs 全表缩小到候选集；语义
  风险：第三方 helper 在运行中 `busted.register` 新描述符会使缓存过期。

### 3.3 mediator 派发

- `busted/core.lua:104-114` 的 publish/subscribe 直通 mediator；输出 handler、
  helper、过滤器（filter_loader 的每个 `busted.subscribe`）都是订阅者。派发成本
  是 `Mediator.publish`（`mediator.lua:153-155`）→ `getChannel`（`:131-139`，按
  命名空间逐层 `getChannel`，miss 时 `addChannel` 建通道）→ `Channel.publish`
  （`:96-115`，遍历 callbacks、构造 result 表、谓词判断）。
- `--list` 模式 35191 次顶层 publish 仅 0.49 s（含所有订阅者回调）——不是瓶颈。
  顺带：`filter_loader` 的 `filterTags`（`filter_loader.lua:30-38`）即使
  `options.tags` 为空也对每个 it 计算 `getFullName`（沿父链拼名），ms 级。
- 优化方向（若做）：`publish` 前给命名空间做通道缓存（key = 拼接串）、避免
  miss 时建永久通道；`status.lua:1-12` 的 `get_status` 每次重建 `smap` 表也可提
  成局部常量。均为微优化。

### 3.4 `dirname` / splitpath

- `getTrace` 栈走每帧 `path.dirname(info.source)` → `splitpath` 逐字符扫描。
  每个源串重复多次（同文件每个注册一遍）。缓存到"每个 source 一次"后该项归零，
  但绝对值也小（在 21.9 s 的 getTrace 里除 traceback 外只剩约 0.26 s，其中还含
  `debug.traceback` 外的 getinfo 与 getFile 遍历）。低风险附属优化。

## 4. 补丁原型与测量

三棵树都在 `/tmp/busted-research/rt-{p1,p2,p3}`（diff 存
`/tmp/busted-research/patches/*.diff`）：

- **P1**：新增 `busted/fasttrace.lua`（`/tmp/busted-research/patches/fasttrace.lua`），
  用 Lua 逐帧 `debug.getinfo(level,'Slntf')` 只格式化 `trimTrace` 会保留的帧
  （从 level 起到首个 C 帧前，上限 10 帧——与 `luaL_traceback` 的 `LEVELS1=10`
  未跳帧窗口一致；超出即返回 nil 回退 `debug.traceback`），文本格式逐字节复刻
  `lauxlib.c`（`short_src:line: in <namewhat 'name' | main chunk | function
  <file:linedefined>>`、`(...tail calls...)`）。`core.lua:81` 改为
  `fasttrace.trimmed(level, false) or debug.traceback('', level)`。
  唯一的语义偏差类：5.4 的 `pushfuncname` **先查全局名后查 namewhat**，P1
  （approx 模式）跳过全局名查找——若某个被保留帧的函数恰是
  `package.loaded` 里的值（如模块缓存把 chunk 函数存为
  `package.loaded[modname]`），该帧显示名由 `function 'mod.name'` 退化为
  namewhat/<file:line> 形式。fasttrace 另提供 exact 模式（对每个被保留帧做
  一次单帧全局名查找）用于对拍验证，不作为生产配置（Lua 侧扫描
  `package.loaded` 每次约 2 ms，会吃掉大部分收益）。
- **P2** = P1 + `dirname` 按 source 字符串缓存（`core.lua:73/76`）。
- **P3**：`core.lua:81` 改为先临时把 `registry._LOADED` 换成空表再
  `debug.traceback` 再恢复（pcall 包裹保异常安全）。思路是"跳过全局名查找"，
  但文本不保证一致（任何被保留帧若在 loaded 中，名字丢失）。

### 4.1 文本等价性验证

`/tmp/busted-research/rt-verify`：在 getTrace 里同时生成原版与 P1 版并逐字节
比较，7229 次注册全量核对（两轮，`verify.log`/`verify2.log`）：

- **approx 模式（P1 生产配置，不做全局名查找）：7228/7229 一致**；唯一差异
  样例：某 spec 顶层 `local register = require('test.support...prompts')`，
  monopoly 的 cache_loader 把模块 chunk 函数本身存进了
  `package.loaded['test.support...prompts']`，故 5.4 原版把该帧打印为
  `in function 'test.support...prompts'`（global 查找优先于 namewhat），P1
  打印为 `in local 'register'`。这正是 3.1 所述的偏差类 (a) 的实例：仅当
  "被保留帧的函数恰是 package.loaded 里的值"时显示名不同。
- **exact 模式（每个被保留帧都补一次全局名查找）：7229/7229 完全一致**，
  nil 回退 0 次。即 P1 的所有差异都严格限定在上述一个可枚举的类里，
  且只影响 traceback 文本中的函数名装饰，不影响 `short_src:currentline`。
- 附带观察：P1 的格式等价于 **Lua 5.5** 的 `pushfuncname` 新顺序（namewhat
  → main → global，commit 3dd8ea5）在"函数不在 package.loaded 中"时的输出。
- 注意 TAP/`--list` 输出本身不含 traceback 文本（仅 verbose 失败/pending 才
  打印），故输出 diff 无法区分 P1/P3，等价性只能靠上述对拍仪器验证。

### 4.2 基准结果

测量（每配置重复多次、交错执行以摊平负载噪声；CPU 为子进程 user+system）：

`--list`（3 轮 × 4 树，`/tmp/busted-research/bench-list.log`）：

| 树 | CPU 三次 | 最小值 |
|---|---|---|
| rt-orig | 35.1 / 31.5 / 34.3 s | 31.5 s |
| rt-p1 | 17.0 / 16.1 / 18.9 s | **16.1 s（-49%）** |
| rt-p2 | 17.7 / 16.5 / 17.2 s | 16.5 s |
| rt-p3 | 17.4 / 17.2 / 19.4 s | 17.2 s |

P1 与 P2 差 <3%（dirname 缓存无可见收益）；P3 与 P1 基本同速，但 P1 文本可验
证等价，P3 不能。

全量套件（2 轮 × 4 树，`/tmp/busted-research/bench-full.log` +
`bench-full2.log`）：

| 树 | CPU（2 轮） | 最小值 | ok / not ok（两轮一致） |
|---|---|---|---|
| rt-orig | 57.6 / 51.7 s | 51.7 s | 6259 / 3 |
| rt-p1 | 32.2 / 31.4 s | **31.4 s（-39%）** | 6259 / 3 |
| rt-p2 | 36.0 / 31.3 s | 31.3 s | 6259 / 3 |
| rt-p3 | 31.4 / 32.7 s | 31.4 s | 6259 / 3 |

全量模式插桩分解（rt-instr，一次，总 42.0 s CPU）：getTrace 14.7 s（n=7232，
其中 traceback 14.5 s）、publish 0.40 s、rejectAll 0.075 s。n 仅比 `--list`
多 3（恰为 3 个工件失败的错误路径调用）——无真实失败时注册期 trace 仍是
getTrace 的绝对主体；若套件有失败，错误路径的 getTrace 同样走快路径。

注意：全量模式下 rt-orig 基线出现 3 个固有失败（
`TestLogWarnsHandler test_preserves_captured_info_lines_by_default` 等三个，
ok 6259 / not ok 3），失败信息为
`模块 busted.runner 从被核对 tree 以外解析: /tmp/busted-research/rt-orig/...`——
这三个测试嵌套启动 test_runner 子进程，子进程从真实 `~/.luarocks` 解析 busted，
与外层 scratch 树不一致触发受控环境校验。这是**本实验装置（scratch 树 + 包装
luarocks）的固定工件**，在基线与全部补丁树上应逐字节一致地复现；判定标准改
为"P1/P2/P3 与 rt-orig 的 ok/notok 计数及 not ok 名单完全相同"。（真实环境下
`~/dev/github/monopoly` 基线为 6261 ok / 0 not ok，与任务口径一致；scratch 装置
下 6262 = 6259 ok + 3 not ok，无 pending。）

### 4.3 正确性核对

- `--list` 输出：3 轮 × 3 个补丁树与 rt-orig 全部 6262 行，`cmp` 逐字节一致
  （`/tmp/busted-research/out/list-{orig,p1,p2,p3}-{1,2,3}.0`）。
- 全量：两轮四树均为 ok 6259 / not ok 3，且 not ok 名单逐字一致（即上述三个
  工件失败，无任何其它差异）。
- busted 自身测试套件（dogfood，v2.3.0 源码树 + P1 补丁）：见 4.4。

### 4.4 busted 自身套件回归

对 v2.3.0 源码树打 P1 补丁后跑 busted 自己的套件（`selftest.sh`：Lua 5.4 +
`~/.luarocks` 依赖，`bin/busted -o TAP`），`/tmp/busted-research/selftest-{orig,p1}.log`：

- orig 与 p1 均为 **1675 ok / 2 not ok**，失败集逐字节一致：
  `fixtures: path() returns the absolute fixture path [normalized]`——该测试用
  正则 `^.-busted[/\]spec[/\]fixtures...` 匹配仓库路径，我们的 scratch 目录
  不叫 `busted` 故必然失败（纯环境工件，与本补丁无关）。
- 关键：`spec/cl_spec.lua:400-427` 的三条 "Tests stack trackback" 格式断言
  （`--verbose` 下失败输出的 `stack traceback:` 文本逐字一致）在 p1 下通过，
  证明 P1 的 Lua 侧格式复刻在真实失败路径上与 `luaL_traceback` 输出一致。

## 5. 语义风险清单

| 补丁 | 可见行为变化 | 暴露面 |
|---|---|---|
| P1 fasttrace | 失败/pending 输出中的 traceback 文本理论上可能不同：被保留帧函数恰为 `package.loaded` 里的值时显示名由 `function 'mod.fn'` 退化为 namewhat/`<file:line>` 形式（5.4 原版优先全局名）；同一 trace 被 fennel/moonscript loader 二次重写时输入不同（输入等价则输出等价） | 仅 `--verbose` 下 pending/失败的 traceback 文本；已用 monopoly 7229 次注册全量对拍（7228/7229，唯一差异即此类；exact 模式 7229/7229）；busted 自身 `cl_spec.lua:400-427` 格式测试通过 |
| P2 dirname 缓存 | 无（纯函数缓存；source 串为键） | 无 |
| P3 _LOADED 交换 | traceback 文本**保证可能不同**（全局名全部消失）；且若 traceback 期间协程切换会看到被换的注册表（pcall 内不 yield，安全） | 所有打印 traceback 的场景——不建议上游 |
| rejectAll 预计算 | 运行中新 `busted.register` 的描述符不被拒绝 | 仅第三方 helper 动态注册 |
| mediator 通道缓存 | miss 时不再持久建空通道（订阅语义变化：先 subscribe 后 publish 顺序必须保持） | 顺序敏感的订阅者 |
| `status.lua` smap 提升 | 无 | 无 |

## 6. 上游化建议

1. **P1 值得提 PR**（lunarmodules/busted）：动机可引 Lua 官方 commit 3dd8ea5
   的提交信息 + 本研究的复现数据（6262 测试项目 `--list` 33 s → 16 s、全量
   52 s → 31 s）。需附带：
   对 `cl_spec.lua` traceback 测试保持通过；在 spec 里加一个"注册 trace 与
   `debug.traceback`+trim 等价"的对拍测试（可在测试内对比两者文本）。CI 矩阵
   覆盖 5.1–5.5+LuaJIT，需要注意：LuaJIT 与 5.1 的 `luaL_traceback` 没有
   `pushglobalfuncname`（该函数 5.2 引入），格式也略不同——PR 实现应仅在
   `_VERSION` 为 5.2+ 时启用 fasttrace，其余回退 `debug.traceback`，或按版本
   复刻对应格式。
2. P2 可作为同 PR 的独立提交（一行缓存，零风险）。
3. rejectAll / mediator / status 微优化不建议单独提：量级在 1% 以下，增加 review
   负担。
4. mediator 侧：仓库实质废弃（Tieske 已在 Tieske/mediator2 另起炉灶），
   `mediator.lua:110` 的 `parent` 全局 bug 可顺手报告但不必指望合并。
5. 维护者合并节奏：简单修复类 PR 当日~数日合并（#766/#780），较大功能 PR 可能
   积压数月（#753 用了 14 个月）。P1 属中等复杂度，预期需要 1–2 轮 review。

### 6.1 对 mutator 的落地路径

monopoly 守卫允许 worker（mutation 工作副本）内的搜索模板
（`cache_loader.lua:148-149` `in_worker` 分支）。因此 mutator 可以在生成 worker
时把打过 P1 的 busted 树拷进 worker 内（如 `worker/.mutator-busted/`；注意
`_lua_worker_setup`（`src/mutator/runner.py:293-295`）不对 `.toolcache` 路径做
worker 重定基，故不要放 `.toolcache` 下）并在该 worker 的 `LUA_PATH` 前置之，
无需等上游、也不触碰 `~/.luarocks`；本研究已用同形态（额外 rocks-tree 根）
证明装载与结果正确性。短期推荐此路径 + 并行提上游 PR；上游合并后回到官方
luarocks 钉版即可。

## 7. 复现材料索引（本机 /tmp，易失）

- `/tmp/busted-research/mono` — monopoly @9bc18c4 只读快照
- `/tmp/busted-research/rt-{orig,instr,p1,p2,p3,verify}` — 各 busted 树
- `/tmp/busted-research/patches/fasttrace.lua`、`patches/p{1,2,3}-core.diff`
- `/tmp/busted-research/scripts/{bench.py,run1.py,luarocks-wrap,tb_micro.lua,selftest.sh}`
- `/tmp/busted-research/{bench-list.log,bench-full.log,verify.log,verify2.log}`
- `/tmp/busted-research/out/` — 各轮 TAP/--list 输出（diff 用）
- 上游克隆：`/tmp/busted-research/upstream-{busted,penlight,mediator,lua}`
