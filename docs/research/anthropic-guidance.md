# Anthropic 最新工程指南：提炼与落地映射

> 调研日期：2026-11-15。本文只做**转述与映射**，不复制原文。每条原则都标注来源，并指向本项目中落地它的模块。
> 实现状态见 [ARCHITECTURE.md](../ARCHITECTURE.md#实现状态)。

## 一、采用的一手资料

| # | 资料 | 日期 | 为什么相关 |
|---|------|------|-----------|
| A | [Building always-on agents](https://claude.com/resources/articles/building-always-on-agents)（W. Hu） | 2026-11 | **最贴近 personal agent 的工程指南**：把"常驻 agent"拆成五个原语（事件收件箱 / 计时器 / 记忆 / 权限 / 发件箱）。本项目的主骨架。 |
| B | [Building effective agent automations](https://claude.dev/blog/building-effective-agent-automations/)（L. Martin, C. Avilla）+ 参考实现 [`claude-quickstarts/managed-agents/daily-brief`](https://github.com/anthropics/claude-quickstarts/tree/main/managed-agents/daily-brief) | 2026-10-08 | 无人值守运行的六个组件与六条规则，以及"失败时怎么优雅失败"的具体运行步骤。 |
| C | [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory) | 2026-11-06 | 记忆分层、"做梦"（dreaming）、wiki 表征、对记忆做"显著性判断"的实验结论。 |
| D | [Scaling Managed Agents with stable interfaces](https://www.anthropic.com/research/managed-agents) | 2026 | **大脑 / 双手 / 会话**三者解耦；harness 无状态，可随时 `wake(sessionId)` 重启。 |
| E | [Trustworthy agents in practice](https://www.anthropic.com/research/trustworthy-agents) | 2026-04-09 | 五项原则：人类掌控、价值对齐、安全交互、透明、隐私。 |
| F | [Memory tool 文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) | 2026 | 记忆工具的命令集、返回格式、路径穿越防护、容量上限。 |
| G | 基础篇：*Building effective agents*、*Effective context engineering*、*Writing effective tools*、*Effective harnesses for long-running agents*、*Demystifying evals*、*Code execution with MCP*、Agent Skills | 2024-12 ~ 2026-01 | 简单优先、上下文是预算、工具即契约、进度文件交接、评测驱动。 |

> 说明：资料 A 的原文没有标注发布日期，从内容（提到 Opus 5.5 的成本数据）判断为 2026-11 前后。

## 二、核心结论（一句话版）

1. **常驻 agent 其实大部分时间在睡觉。** 它被事件唤醒、完成一次 *run*、把发生的事写下来、再睡。"Always-on"只是用户体验。（A）
2. **围绕同一个 agent loop，真正要设计的是外围原语**：何时醒（inbox/timers）、什么跨 run 延续（memory）、能做什么（permissions）、成果如何送达（outbox）。（A）
3. **一切状态都放在 harness 之外的持久存储里**，harness 是"牲畜而非宠物"：崩了就用 session log 重建。（D）
4. **无人值守 ≠ 无人负责。** 要用书签、台账、重新核验、送达确认、只读偏好、花费上限来"失败得体面"。（B）
5. **记忆只存"查不到的东西"**，顶层注入层要小，细节下沉并留链接；由"做梦"在后台纠错与整理。（A, C）
6. **对强模型：告诉它记忆是干什么的，别规定怎么用。** 硬规则（如"只记失败"）反而削弱效果。（C）
7. **权限靠结构，不靠提示词。** 只读凭据、egress 白名单、凭据代理、延迟工具调用，而不是"请不要……"。（A, B, E）
8. **从简单开始，只有简单方案不够时才加复杂度**；工具要少而精、返回要省 token。（G）

## 三、原则 → 实现映射

状态图例：✅ 已实现　🚧 计划中（迭代号）

### 3.1 五个原语（来源 A）

| 原语 | Anthropic 的要点 | 本项目的落地 | 状态 |
|------|------------------|--------------|------|
| **事件收件箱** | 所有来源写入持久收件箱而非直接调用 agent；handler 快速确认；收件箱是事实来源；单 worker；按 ID 去重；批处理连续消息；崩溃后可重试 | `core/inbox.py`：SQLite `events` 表 + `(source, external_id)` 唯一去重 + 租约（lease）崩溃恢复 + 按会话批处理 + 毒丸事件进入 `dead` | ✅ |
| **计时器** | 与收件箱分库；两类：follow-up（agent 自设的截止）与 schedule（周期性）；scheduler 轮询到期项→写入收件箱 | `core/timers.py`：独立 `timers` 表；croniter + 时区；错过窗口策略；到期事件用幂等键写入收件箱 | ✅ |
| **记忆** | 目录式文件；用户指令 / 用户笔记 / 过往消息 / 任务进度四类；只加载索引；记忆只存"查不到的" | `memory/`：`preferences/`（agent 只读，每次 run 重新读取）+ `state/`（读写）；`MEMORY.md` 索引按行数/字节封顶注入；**任务进度**：`state/tasks/<名>.md`，上下文里只有代码生成的"一行一条"未完成任务索引（`load_task_index`）；**过往消息**：最近 N 条已确认送达的消息（`<recently_sent>`），只给没有对话可回看的后台 run；**新对话**：每次定时器/webhook/系统事件新开一个会话（`starts_new_session`） | ✅ |
| **权限** | 凭据代理在 agent 环境之外；auto mode 分类器；延迟工具调用（run 结束 → 会话存档 → 答复作为事件 → 新 run 恢复） | `security/policy.py` 分层策略；`security/broker.py` 凭据占位符；基于 SDK `RunState` 的延迟审批 | ✅ |
| **发件箱** | run 有三种结局：发送 / 决定不发 / 延迟调用；需要时才建正式发件箱以便节奏控制与错误恢复；**默认安静，逐步赢得注意力** | `core/outbox.py` + dispatcher：送达确认、`maybe_sent` 状态、静默时段、限流；后台 run 默认不发话 | ✅ |

### 3.2 无人值守运行的六条规则（来源 B）

| 规则 | 落地方式 |
|------|----------|
| 按**书签**读取，而非固定时间窗 | `state/bookmarks.json`；`send_message(commit=…)` 在**送达确认后**由 harness 原子更新 |
| 读取失败要报"不可读"，**绝不当成"今天很安静"** | 运行协议（系统提示）+ 工具错误结构化为 `source_unreadable`；评测用例覆盖 |
| 发送前**重新核验**每一条 | 运行协议第 5 步；`ledger` 条目带最后已知状态 |
| 平台确认后才算已发送，再更新书签与台账 | outbox `on_sent` 提交钩子（确定性代码，不靠模型自觉） |
| **每次运行都重新读取偏好**，且偏好存储 agent 不可编辑 | harness 在 run 开头强制读取 `preferences/`，读不到则**停止并报告**；偏好目录对 agent 只读，改动走 `proposals.md` |
| 只读权限 + 每次运行的花费上限 | 工具分类 + `BudgetGuard`（达到上限是**暂停**而非失败） |

此外采纳的细节：来源内容一律视为数据而非指令；不得搜寻凭据；`notes` 只记"来源正常时的行为"，不记故障；时区决定"今天/昨天"；干跑（dry-run）模式。

### 3.3 记忆（来源 C）

| 结论 | 落地 |
|------|------|
| 顶层注入层必须小（Claude Code 只加载 `MEMORY.md` 前 200 行 / 25KB） | 注入前强制截断（`load_index`；任务索引同理，已完成的只计数不列出）；超限时提示整理 |
| 做梦 = 离线回看日志纠错、建 wiki/索引、晋升或降级笔记 | `harness/dreaming.py`：调度循环每天（默认 03:30，睡过了就补）排一个 system 事件；**代码**从原始日志（不受压缩与 `/new` 影响）确定性地生成摘要并维护游标；画像只有 `memory` 与 `search_history`；花费封顶、偏好只读；删得太多整次撤销、连续三晚失败暂停。注意：这是模型对记录的判断，护栏是"权限收窄 + 可撤销"，不是对笔记正确性的证明 |
| 做梦的修改要可审计 | `memory_versions` 表记录每次写入（actor=agent/user/dream/system，带 run 编号）；`pa dream log` 按 run 列出改动，`pa memory restore` 撤销单次改动，复核失败时 `revert_run` 整次撤销 |
| wiki 与搜索结合优于单独使用；也可以只用转录检索 | `search_history`：对完整对话记录（只含消息，不含工具结果）做 FTS5 trigram 检索，`around` 可看前后文；记忆文件用 `memory` 工具读写。向量检索没有做 |
| 偏好变更由 agent **提议**，用户确认 | `proposals.md` + `pa prefs proposals accept` |

### 3.3b 长对话：压缩与新对话（来源 A、G）

文章的立场：压缩和开新对话都会丢细节，所以**重要的事要在记忆里**，上下文保持精简；接着前面的事做的事件恢复原会话，其余一律新开。

| 要点 | 落地 |
|------|------|
| 续做的事件恢复会话，其余新开一个 | `Harness.starts_new_session`：主人的消息/审批答复继续；`timer:`/`webhook:`/`system:` 自己的会话新开（追加 `clear` 标记，不删日志）；重试、挂起审批中的会话不清 |
| 新对话仍要知道"主人已经听过什么" | `<recently_sent>`：最近已送达的消息（每条截短、去隐藏字符、不能闭合围栏） |
| 长对话压缩 | `harness/compaction.py`：追加标记 + 摘要（第一人称笔记，围进 `<conversation_summary>`）；切点在用户消息处；坏摘要不进去；失败不让 run 失败；花费记在当前 run |
| 压缩前先存记忆 | **没有做"压缩前强制存一次"**：做梦每晚读原始日志，原文不删；所以不依赖这一步 |

### 3.4 Harness 解耦（来源 D）

- **Session（追加式日志）**：`session_events` 表只追加；压缩与 `/new` 都只是追加一条标记（模型看到的是投影），原文留在日志里——做梦与 `search_history` 读的正是原文。✅
- **Harness（大脑，无状态）**：`harness/` 每次 run 从日志重建上下文；崩溃后由 worker 租约过期重试，对应 `wake(sessionId)`。
- **Sandbox（双手，可抛弃）**：`security/sandbox.py` 暴露 `execute(command) -> ExecResult`；seatbelt / docker 两种后端，失败只是工具错误，由模型决定是否重试。

### 3.5 可信 agent（来源 E）→ 默认值

| 原则 | 默认行为 |
|------|----------|
| 人类掌控 | 默认 `supervised`；未知发送者默认拒绝（配对码 + 本机确认）；审批超时视为拒绝 |
| 安全交互 | shell 在无网络沙箱中执行；密钥只经代理注入；SSRF 防护；egress 白名单 |
| 透明 | 追加式会话日志 + 审计日志（每次工具调用的决策与结果）；`pa runs show` |
| 隐私 | **遥测默认关闭**；OpenAI Agents SDK 的 tracing 默认禁用；日志与会话日志做密钥脱敏 |

## 四、本项目对 Anthropic 指南的两处"适配"与一处"偏离"

1. **适配：Managed Agents 的托管能力改为本地实现。** Anthropic 的参考实现依赖其托管平台（vault、deployment、memory store、预算）。本项目在本机用 SQLite + 文件 + OpenAI Agents SDK 复现同等语义。
2. **适配：模型从 Claude 换成 OpenAI。** 指南中关于"强模型自行判断显著性"的结论在换模型后未必成立，因此提供评测框架（`pa eval`）来验证，而不是照搬。
3. **偏离：后台 run 的审批。** 参考实现假设"无人能批准，所以不要停下来问"。本项目保留 Anthropic always-on 文章中的**延迟工具调用**：后台 run 遇到需要批准的动作会挂起并通过发件箱询问；同时默认给后台 run 更窄的工具画像（能力裁剪优先于审批）。
