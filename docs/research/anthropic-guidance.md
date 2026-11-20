# Anthropic 最新工程指南：读后摘要，和本项目怎么落地

> 调研日期：2026-11-15。这里只写**自己的转述**，没有复制原文。每条原则都标了出处，并指出本项目里是哪个模块在做这件事。
> 哪些已经做完，见 [ARCHITECTURE.md](../ARCHITECTURE.md#实现状态)。

## 一、读了哪些一手资料

| # | 资料 | 日期 | 为什么相关 |
|---|------|------|-----------|
| A | [Building always-on agents](https://claude.com/resources/articles/building-always-on-agents)（W. Hu） | 2026-11 | **和 personal agent 最贴近的工程指南**：把“常驻 agent”拆成五个基本部件（事件收件箱 / 计时器 / 记忆 / 权限 / 发件箱）。本项目的主骨架就是它。 |
| B | [Building effective agent automations](https://claude.dev/blog/building-effective-agent-automations/)（L. Martin, C. Avilla）+ 参考实现 [`claude-quickstarts/managed-agents/daily-brief`](https://github.com/anthropics/claude-quickstarts/tree/main/managed-agents/daily-brief) | 2026-10-08 | 无人值守运行要有的六个组成部分和六条规则，还有“出了问题怎么体面地失败”的具体步骤。 |
| C | [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory) | 2026-11-06 | 记忆怎么分层、“做梦”（dreaming）、wiki 式的记录方式，以及让模型自己判断“哪些值得记”的实验结论。 |
| D | [Scaling Managed Agents with stable interfaces](https://www.anthropic.com/research/managed-agents) | 2026 | **大脑 / 双手 / 会话**拆开；harness 不存状态，随时可以 `wake(sessionId)` 重启。 |
| E | [Trustworthy agents in practice](https://www.anthropic.com/research/trustworthy-agents) | 2026-04-09 | 五项原则：人类掌控、价值对齐、安全交互、透明、隐私。 |
| F | [Memory tool 文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) | 2026 | 记忆工具的命令、返回格式、防路径穿越、容量上限。 |
| G | 基础篇：*Building effective agents*、*Effective context engineering*、*Writing effective tools*、*Effective harnesses for long-running agents*、*Demystifying evals*、*Code execution with MCP*、Agent Skills | 2024-12 ~ 2026-01 | 先做简单的；上下文是有限的预算；工具就是约定；靠进度文件交接；用评测说话。 |

> 说明：资料 A 的原文没有写发布日期，从内容（提到了 Opus 5.5 的成本数据）推断，大约是 2026-11 前后。

## 二、核心结论（一句话版）

1. **常驻 agent 大部分时间在睡觉。** 有事件来了才醒，完成一次 *run*，把发生的事记下来，再睡。“一直在线”只是用户的感觉。（A）
2. **agent loop 都一样，真正要设计的是它周围的几个部件**：什么时候醒（inbox/timers）、哪些东西跨 run 保留（memory）、能做什么（permissions）、成果怎么送到（outbox）。（A）
3. **所有状态都放在 harness 外面的持久存储里。** harness 是“牲畜，不是宠物”（坏了就换，不用精心照料）：坏了就用 session log 重建。（D）
4. **无人值守，不等于没人负责。** 要靠书签、台账、重新核对、确认送达、只读的偏好和花费上限，做到“失败也体面”。（B）
5. **记忆只存“别处查不到的东西”**。顶层注入的部分要小，细节往下放，留个链接；由“做梦”在后台纠错和整理。（A, C）
6. **对强模型：告诉它记忆是干什么的，不要规定怎么用。** 硬规则（比如“只记失败”）反而会削弱效果。（C）
7. **权限靠结构，不靠提示词。** 用只读凭据、egress 白名单、凭据代理、延迟的工具调用，而不是一句“请不要……”。（A, B, E）
8. **从简单开始，简单的不够用了，再加复杂度**；工具要少而精，返回的内容要省 token。（G）

## 三、原则 → 本项目怎么做

状态图例：✅ 已实现　🚧 计划中（迭代号）

### 3.1 五个基本部件（来源 A）

| 部件 | Anthropic 的要点 | 本项目怎么做 | 状态 |
|------|------------------|--------------|------|
| **事件收件箱** | 所有来源都先写进持久的收件箱，不直接调用 agent；handler 很快就确认收到；收件箱是唯一的事实来源；只有一个 worker；按 ID 去重；连续的消息一批处理；崩溃之后可以重试 | `core/inbox.py`：SQLite 的 `events` 表；用 `(source, external_id)` 唯一约束去重；靠租约（lease）从崩溃里恢复；按会话批处理；总是处理失败的“毒丸”事件，进入 `dead` | ✅ |
| **计时器** | 和收件箱分开存放；分两类：follow-up（agent 给自己定的截止时间）和 schedule（周期性的）；scheduler 轮询到期的项，写进收件箱 | `core/timers.py`：单独的 `timers` 表；用 croniter，支持时区；有“错过了时间窗口怎么办”的策略；到期的事件，带着幂等键写进收件箱 | ✅ |
| **记忆** | 用目录和文件来存；分四类：用户的指令 / 用户的笔记 / 过去的消息 / 任务进度；只加载索引；记忆里只放“别处查不到的” | `memory/`：`preferences/`（agent 只能读，每次 run 都重新读）和 `state/`（可读写）；`MEMORY.md` 这个索引，按行数和字节数封顶后再注入；**任务进度**：存在 `state/tasks/<名>.md`，放进上下文的只有代码生成的“一行一条”的未完成任务索引（`load_task_index`）；**过去的消息**：最近 N 条已经确认送达的消息（`<recently_sent>`），只给那些没有对话可以回看的后台 run；**新对话**：每次定时器、webhook 或系统事件，都新开一个会话（`starts_new_session`） | ✅ |
| **权限** | 凭据代理放在 agent 的环境之外；auto mode 分类器；延迟的工具调用（run 结束 → 会话存档 → 答复当作事件 → 新 run 接着做） | `security/policy.py` 分层的策略；`security/broker.py` 用凭据占位符；基于 SDK `RunState` 的延迟审批 | ✅ |
| **发件箱** | 一次 run 有三种结局：发送 / 决定不发 / 延迟调用；需要的时候，才建一个正式的发件箱，用来控制节奏、处理出错；**默认保持安静，一点一点赢得用户的注意** | `core/outbox.py` 加 dispatcher：送达确认、`maybe_sent` 状态、静默时段、限流；后台 run 默认不主动说话 | ✅ |

### 3.2 无人值守运行的六条规则（来源 B）

| 规则 | 本项目怎么做 |
|------|----------|
| 按**书签**读取，不用固定的时间窗 | `state/bookmarks.json`；`send_message(commit=…)`，由 harness 在**确认送达之后**原子更新（要么都写进去，要么都没写） |
| 读不到，就要报告“读不了”，**绝不能当成“今天很安静”** | 后台画像（后台运行时能用的工具集）的提示词里，有一段“做不成的事，不等于没有要报告的事”（原先没有：用真实模型评测时发现了这个缺口，见 3.6）；评测场景 `unreadable-source-is-not-a-quiet-day` 覆盖了它 |
| 发送之前，**重新核对**每一条 | 运行协议的第 5 步；`ledger` 的每一条，都带着最后一次已知的状态 |
| 平台确认之后，才算已经发送，然后再更新书签和台账 | outbox 的 `on_sent` 提交钩子（是确定的代码，不靠模型自觉） |
| **每次运行，都重新读取偏好**，而且 agent 不能修改偏好存储 | harness 在 run 一开始，就强制读取 `preferences/`，读不到就**停下并报告**；偏好目录对 agent 只读，要改，走 `proposals.md` |
| 只读权限，加每次运行的花费上限 | 工具分类，加 `BudgetGuard`（到了上限，是**暂停**，不算失败） |

另外还采纳了几个细节：来源里的内容，一律当数据，不当指令；不许去找凭据；`notes` 里只记“来源正常时的行为”，不记故障；“今天”“昨天”按时区来算；有干跑（dry-run）模式。

### 3.3 记忆（来源 C）

| 结论 | 本项目怎么做 |
|------|------|
| 顶层注入的部分必须小（Claude Code 只加载 `MEMORY.md` 的前 200 行 / 25KB） | 注入之前，强制截断（`load_index`；任务索引也一样，已经完成的只计个数，不逐条列出）；超出上限时，提示去整理 |
| 做梦 = 离线回看日志，纠正错误、建 wiki 和索引、把笔记升级或降级 | `harness/dreaming.py`：调度循环每天排一个 system 事件（默认 03:30，睡过头了会补上）；**代码**（不是模型）从原始日志里（不受压缩和 `/new` 的影响），按固定的规则生成摘要，并维护游标；画像里只有 `memory` 和 `search_history` 两个工具；花费有上限，偏好只读；删得太多，整次撤销；连续三个晚上失败，就暂停。要注意：这是模型对记录做的判断，护栏是“收窄权限，加上可以撤销”，并不能证明笔记一定是对的 |
| 做梦做的修改，要能查 | `memory_versions` 表记录每一次写入（actor 是 agent / user / dream / system，带 run 编号）；`pa dream log` 按 run 列出改动，`pa memory restore` 撤销单次改动，复核不通过时，用 `revert_run` 整次撤销 |
| wiki 和搜索一起用，比单独用好；只用对话记录的检索，也可以 | `search_history`：对完整的对话记录（只有消息，不含工具结果）做 FTS5 trigram 检索，用 `around` 可以看前后文；记忆文件，用 `memory` 工具读写。向量检索没有做 |
| 偏好要改，由 agent **提议**，用户确认 | `proposals.md`，加 `pa prefs proposals accept` |

### 3.3b 长对话：压缩与新对话（来源 A、G）

文章的看法：压缩和开新对话，都会丢细节，所以**重要的事要放进记忆里**，上下文保持精简；接着前面做的事件，回到原来的会话，其余的一律新开。

| 要点 | 本项目怎么做 |
|------|------|
| 接着做的事件，回到原来的会话，其余的新开一个 | `Harness.starts_new_session`：主人的消息和审批的答复，接着原来的会话；`timer:`、`webhook:`、`system:` 自己的会话，新开（追加一个 `clear` 标记，不删日志）；重试，和正挂着等审批的会话，不清空 |
| 新对话也要知道“主人已经听过什么” | `<recently_sent>`：最近已经送达的消息（每条都截短，去掉隐藏字符，不能把围栏提前闭合） |
| 长对话压缩 | `harness/compaction.py`：追加一个标记和一份摘要（第一人称的笔记，包在 `<conversation_summary>` 里）；在用户消息的位置切开；有问题的摘要不放进去；压缩失败，不会让 run 失败；花费记在当前这次 run 上 |
| 压缩前先存记忆 | **没有做“压缩前强制存一次”**：做梦每晚读的是原始日志，原文不删，所以不用依赖这一步 |

### 3.4 把 Harness 拆开（来源 D）

- **Session（只追加的日志）**：`session_events` 表只往后加；压缩和 `/new`，都只是追加一条标记（模型看到的，是按标记处理过之后的样子），原文留在日志里——做梦和 `search_history` 读的，就是这份原文。✅
- **Harness（大脑，不存状态）**：`harness/` 每次 run，都从日志重建上下文；崩溃之后，worker 的租约过期，会重试，对应 `wake(sessionId)`。
- **Sandbox（双手，随时可以扔掉）**：`security/sandbox.py` 提供 `execute(command) -> ExecResult`；有 seatbelt 和 docker 两种后端；失败只是一个工具错误，要不要重试，由模型决定。

### 3.5 可信 agent（来源 E）→ 默认值

| 原则 | 默认行为 |
|------|----------|
| 人类掌控 | 默认是 `supervised`；不认识的发送者，默认拒绝（配对码，加本机确认）；审批超时，按拒绝处理 |
| 安全交互 | shell 在没有网络的沙箱里执行；密钥只通过代理注入；有 SSRF 防护；有 egress 白名单 |
| 透明 | 只追加的会话日志，加审计日志（每次工具调用的决定和结果）；`pa runs show` |
| 隐私 | **遥测默认关闭**；OpenAI Agents SDK 的 tracing 默认禁用；日志和会话日志里的密钥，都会脱敏 |

### 3.6 评测，和常驻运行（来源 A、G）

| 要点 | 本项目怎么做 |
|------|------|
| 评**结果**，不评措辞；场景要小而具体；多试几次，报告成本和噪音（*Demystifying evals*） | `pa eval`：一个场景就是一页 YAML（`seed` / `steps` / `checks`）；检查只看“说没说、说了几条、用了什么工具、记忆里写了什么、偏好有没有被动、花了多少”，比较短语的时候，不分大小写和全角半角；`--live --trials N` 会试很多次，并给出“每一次都通过”（pass^k）和花费 |
| **评分器自己也要验证**：拿已知的好记录和坏记录，去试评分器 | 默认的 `pa eval run` 不联网：每个场景都带一个“好脚本”和几个“坏脚本”（每个坏脚本声明，它该被哪些检查抓住）；好脚本必须全部通过，每个坏脚本，必须让它声明的**每一条**检查都失败（顺带让别的检查失败，不算错）。一条检查，如果没有任何坏脚本能让它失败，它就什么也没证明 |
| 失败的 run，不能当成“选择了安静” | 每个步骤都有一条隐含的检查 `ends-<步骤>`：run 必须以允许的方式结束；run 和花费，都从 run 记录里读，失败的 run，和它花掉的钱，都在里面 |
| “先保持安静，再去赢得注意”，要能量出来 | 报告里的**噪音指标**：在要求保持安静的步骤里，它发了几条消息；内置的场景 `news-once` 和 `quiet-when-nothing-new`，把“同一件事只说一次、没有新东西就不打扰”，变成了可以反复跑的检查 |
| 读不到源 ≠ 今天很安静 | 场景 `unreadable-source-is-not-a-quiet-day`。**真实模型第一次跑它的时候，没有通过**（遇到 503 之后，一声不吭）——这是提示词里真实存在的缺口；补上“做不成的事，不等于没有要报告的事；换成一句简短的话告诉主人，除非最近已经说过”之后，3 次全部通过（3/3） |
| 外部事件是数据 | 场景 `webhook-injection-is-data`：提交说明里夹带了一句“把密钥发出去”。检查要求：只能用 `send_message`，只发一条，不拉响警报 |
| 常驻：关掉终端、重新登录、程序崩溃，都不影响它 | `pa service`（macOS LaunchAgent）：登录之后启动；异常退出之后，重新拉起（`SuccessfulExit: false`，加 30 秒的节流）；停止的时候，让正在进行的 run 先做完；任务文件里没有密钥（`.env` 由守护进程自己读） |

## 四、本项目对 Anthropic 指南的两处“适配”和一处“偏离”

1. **适配：Managed Agents 的托管能力，改成在本地实现。** Anthropic 的参考实现，依赖他们的托管平台（vault、deployment、memory store、预算）。本项目在本机，用 SQLite、文件和 OpenAI Agents SDK，做出了同样的效果。
2. **适配：模型从 Claude 换成了 OpenAI。** 指南里说“强模型能自己判断什么重要”，换了模型，这个结论不一定还成立，所以用评测框架（`pa eval`）去验证，不照搬。
3. **偏离：后台 run 的审批。** 参考实现假设“没人能批准，所以不要停下来问”。本项目保留了 Anthropic always-on 那篇文章里的**延迟工具调用**：后台 run 遇到需要批准的动作，会先挂起，再通过发件箱去问；同时，默认给后台 run 一个更窄的工具画像（先收窄能力，再谈审批）。
