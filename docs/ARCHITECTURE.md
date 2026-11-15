# 架构设计

本项目是一个**本机常驻的个人 agent**：模型能力来自 OpenAI（一把通用的 `OPENAI_API_KEY`，可选自定义 `base_url`），工程理念来自 Anthropic 最新的 always-on / automations / memory / managed-agents 指南（提炼见 [research/anthropic-guidance.md](research/anthropic-guidance.md)，同类项目对比见 [research/reference-analysis.md](research/reference-analysis.md)）。

## 设计原则

1. **简单优先。** 一个 agent loop + 围绕它的五个原语。不堆框架。
2. **状态在外，harness 无状态。** 事件、会话日志、计时器、发件箱、记忆全部落盘；任何 worker 崩溃后可由租约过期 + 会话日志恢复。
3. **能力裁剪优先于审批，审批优先于提示词。** 安全靠结构（沙箱、只读、画像、白名单），不靠"请不要……"。
4. **默认安静、默认安全、默认私密。** 后台 run 不主动发话；`supervised` 模式；无遥测；tracing 关闭。
5. **可插拔但有边界。** 工具（MCP）、技能（Agent Skills）、渠道（适配器）、沙箱（后端）、模型端点都可替换；核心不变量（审计、策略、预算、确认送达）不可被插件绕过。
6. **确定性的事交给代码，判断的事交给模型。** 书签/台账提交、去重、送达确认、预算、偏好读取是代码；"这条值不值得说"是模型。

## 总览

```mermaid
flowchart LR
  subgraph Sources[事件来源]
    CLI[CLI]:::ch
    WEB[Web PWA]:::ch
    TG[Telegram]:::ch
    HOOK[Webhook]:::ch
    TIMER[Timers<br/>follow-up / cron]
  end

  Sources -->|"快速确认 + 去重"| INBOX[(Event Inbox<br/>SQLite)]
  TIMER -.->|"到期 → 幂等写入"| INBOX
  INBOX -->|"按会话批处理 · 租约"| WORKER[Worker]

  subgraph Brain[Harness · 无状态]
    CTX[上下文装配<br/>偏好 fresh · 记忆索引 · 技能清单]
    LOOP["Agent loop<br/>(OpenAI Agents SDK)"]
    POL[Policy Engine<br/>floors → profile → ask → taint → mode]
    BUD[BudgetGuard]
  end
  WORKER --> CTX --> LOOP
  LOOP <--> POL
  LOOP <--> BUD

  subgraph Hands[双手 · 可抛弃]
    SBX[Sandbox<br/>seatbelt / docker]
    NET[http_request / web_fetch<br/>SSRF + 密钥占位符]
    MCP[MCP servers]
    SK[Skills]
  end
  LOOP -->|"execute(name,input)->str"| Hands

  subgraph State[持久状态]
    SLOG[(Session log<br/>append-only)]
    MEM[("Memory<br/>preferences(ro) · state(rw)")]
    RUNS[(runs · approvals · audit)]
  end
  LOOP <--> SLOG
  CTX --> MEM
  LOOP <--> MEM

  LOOP -->|send_message / final text| OUTBOX[(Outbox)]
  OUTBOX --> DISP[Dispatcher<br/>静默时段 · 限流 · 重试]
  DISP -->|"送达确认 → on_sent 提交"| Sources
  DISP --> MEM

  classDef ch fill:#eef,stroke:#88a;
```

一次 run 的生命周期：

1. 事件进入**收件箱**（来自渠道、webhook 或到期的计时器）。
2. **Worker** 以租约领取同一会话的一批事件，合并连续用户消息。
3. **Harness** 创建/恢复会话，**强制重新读取偏好**（读不到则停止并报告），装配上下文，调用 agent loop。
4. 每次工具调用经过 **Policy Engine**：放行 / 拒绝 / 需要批准。需要批准时 run **挂起**（`RunState` 存库），审批请求进入**发件箱**；用户答复作为新事件回到收件箱，worker 恢复该 run。
5. run 结束于三种结局之一：发出消息、决定静默、延迟（挂起）。
6. **Dispatcher** 投递发件箱，**确认送达后**才执行 `on_sent` 提交（书签、台账），并把已发消息镜像进会话。
7. 夜间的**做梦**任务回看转录，纠正与整理记忆。

## 模块布局

下面是目标布局；已经落地的在行尾标 ✅，其余见文末「实现状态」。

```
src/personal_agent/
  app.py           ✅ 容器：把所有长寿命组件装配在一起（build_app）
  config.py        ✅ TOML 配置 + 环境变量；密钥只来自环境/.env
  paths.py         ✅ PA_HOME 目录布局与权限（0700）
  clock.py         ✅ 时钟抽象（测试可手动拨时间）与时区
  db.py            ✅ SQLite（WAL、事务、迁移）
  logs.py          ✅ 私有滚动日志，输出前脱敏
  doctor.py        ✅ `pa doctor`：静态检查 + `--live` 真实调用
  core/
    events.py      ✅ 事件模型与信任等级（owner / untrusted）
    inbox.py       ✅ 收件箱：去重、租约、批处理、毒丸
    sessions.py    ✅ 追加式会话日志；SDK Session 适配
    timers.py      ✅ follow-up 与 schedule；时区/夏令时正确；错过策略；恰好一次触发
    approvals.py   ✅ 审批请求/答复/过期/被覆盖；挂起 run 的状态存取
    budget.py      ✅ 单 run 与每日上限；今日临时加额
    outbox.py      ✅ 发件箱；on_sent 提交；maybe_sent
    runs.py        ✅ run 记录、用量、状态机
    worker.py      ✅ 领取批次、续租、失败归因
    dispatcher.py  ✅ 投递与重试；静默时段与每小时限流
  harness/
    runner.py      ✅ Agents SDK 封装：偏好 fresh 读取、幂等落盘事件、实时记账、静默/投递规则、
                      审批挂起/恢复、模型调用前的预算检查
    instructions.py ✅ 系统指令 + 每次唤醒的上下文块；事件 → 会话条目（不可信内容围栏）
    prompts/       ✅ 运行协议与角色提示（system.md、profile_*.md）
    models.py      ✅ 模型端点工厂、重试策略、隐私默认（store=false + 加密推理回放）
    pricing.py     ✅ 价格表与成本（含长上下文加价）
    errors.py      ✅ 失败归因：环境问题不消耗事件的重试次数
    runctx.py      ✅ 每次 run 传给工具的上下文
    compaction.py  会话压缩（投影，不删日志）
  memory/
    store.py       ✅ VirtualFS：/memories 挂载、路径防护、大小上限、版本
    templates.py   ✅ 首次启动的记忆模板
    search.py      FTS5（+可选向量）检索
    dreaming.py    夜间整理
  security/
    redact.py      ✅ 密钥与敏感信息脱敏、注入线索
    policy.py      ✅ 分层策略引擎
    guardian.py    ✅ `auto` 模式的评审模型（失败即升级给人）
    sandbox.py     seatbelt / docker 后端
    broker.py      密钥占位符与出站代理
    ssrf.py        URL/IP 校验
    audit.py       ✅ 审计日志（每次工具调用的决策与结局）
  tools/
    base.py        ✅ 工具类别、运行画像、注册表
    guard.py       ✅ 包住每一个工具：策略 → 审计 → 超时 → 输出脱敏与截断 → 污点标记
    builtin.py     ✅ `memory`、`send_message`（含 `after_sent` 书签）、`schedule`
    …              files、shell、web_fetch、web_search、http_request、search_history、skills
  plugins/         MCP 装载、Skills 装载、渠道/工具注册（entry points）
  channels/        ✅ base（Channel 协议）、cli；web（Starlette + PWA）、telegram、webhook
  daemon.py        ✅ 单实例锁 + worker / scheduler / dispatcher / 巡检循环 + 优雅停机；渠道服务（迭代 4）挂在这里
  cli.py           ✅ `pa` 命令：init / doctor / chat / ask / serve / status / runs / run / approvals / approve / deny /
                      timers / audit / budget / sessions / outbox / memory / prefs
  evals/           场景评测框架
```

## 数据与目录（`PA_HOME`，默认 `~/.personal-agent`）

```
config.toml          用户配置（不含密钥）
.env                 密钥（0600）
state.db             收件箱 / 会话日志 / 计时器 / 发件箱 / runs / approvals / audit / memory_versions
memory/
  preferences/       用户拥有，agent 只读（instructions.md、profile.md …）
  state/             agent 拥有，可读写（MEMORY.md 索引、notes/、tasks/、ledger.md、bookmarks.json、proposals.md、runs/）
skills/              用户安装的 Agent Skills
workspace/           shell / files 工具的工作目录（沙箱内唯一可写处）
logs/ run/           日志、锁文件
```

> 个人数据永远不进 git 仓库。`.gitignore` 已排除 `.env`、`*.db`、`.pa-home/`。

## 关键机制

### 事件收件箱（`core/inbox.py`）
- 表 `events`：`source / kind / session_key / payload / external_id / trust / status / attempts / lease_expires_at`。
- **去重**：`(source, external_id)` 唯一索引；webhook 用其投递 ID，计时器用 `timer:{id}:{due}`。
- **租约恢复**：worker 崩溃后租约过期，事件回到 `pending` 且 `attempts+1`；超过上限进入 `dead` 并通知用户。
- **批处理**：同一会话的连续用户消息合并为一次 run。
- **信任等级**：渠道适配器标注 `owner` 或 `untrusted`；`untrusted` 事件（webhook、第三方邮件）触发的 run 从一开始就被视为"污点"。

### 计时器（`core/timers.py`）
- 与收件箱分表。`followup`（agent 给自己定的截止）与 `schedule`（cron，含时区）。
- scheduler 轮询到期项，在**同一事务**里写入收件箱并推进状态（followup→fired；schedule→下次时间）。
- 错过策略：`fire_once`（默认，补一次）或 `skip`；绝不"补发一堆"。
- "今天/昨天"在**用户时区**中计算，并写进每个后台 run 的首条消息（Anthropic B 中的常见 bug）。

### 会话日志（`core/sessions.py`）
- `session_events` 只追加。模型可见的历史是**投影**：最近一次 `compaction` 之后的条目 + 压缩摘要。原始转录永不删除（做梦与审计需要）。
- 适配 SDK `Session` 协议：`get_items/add_items/pop_item/clear_session`；`pop_item` 与 `clear_session` 以墓碑事件实现，不物理删除。

### 记忆（`memory/`）
- 两个挂载点：`/memories/preferences`（对 agent **只读**）与 `/memories/state`（读写）。
- 工具命令与返回格式对齐 Anthropic memory tool 规范：`view / create / str_replace / insert / delete / rename`；所有路径经 `resolve()` + 前缀校验，拒绝穿越与 URL 编码穿越；单文件与总量封顶；`view` 输出封顶并支持 `view_range`。
- **只存查不到的东西**；外部系统里已有的只存指针。
- 注入层：`preferences/instructions.md`（每次 run 都 fresh 读取）+ `state/MEMORY.md` 前 200 行/25KB（索引）。其余按需读取。
- 每次写入记录到 `memory_versions`（actor = agent / user / dream），可回滚；记忆内容当作**不可信数据**注入（防记忆投毒），并扫描常见注入模式。
- 偏好变更：agent 只能写 `state/proposals.md`；用户用 `pa prefs proposals` 接受。

### Harness（`harness/runner.py`）
- 每次 run 构造一个 SDK `Agent`（`instructions` 为可调用，run 开始时快照），通过 `Session` 适配器读写会话日志。
- **挂起/恢复**：策略返回 ASK → SDK 产生 `interruptions` → `RunState.to_string()` 存入 `run_states` → 审批请求入发件箱。用户答复事件到来后 `RunState.from_string()` + `approve/reject` → 继续。快照只来自本机 SQLite。
- **预算**：每次模型调用**之前**（`RunHooks.on_llm_start`）检查单 run/单日上限，调用之后只记账——先付费再丢弃答案是错的。超限 → `BudgetExceeded` → 标记 `paused_budget`（暂停而非失败）；单日上限会把事件延后到本地午夜（每天只通知一次），`pa budget extend` 可当天放行。
- **上下文裁剪**：`call_model_input_filter` 把较早的大体积工具输出替换为占位符（tool-result clearing）；会话过长时压缩为摘要。
- **提示缓存**：`instructions` 保持稳定；时间、触发事件等动态内容放进输入末尾。
- **模型端点**：`OpenAIResponsesModel`（默认）；`base_url` 可指向兼容端点，并可降级到 Chat Completions。

### 策略引擎（`security/policy.py`）

判定顺序（**先命中先生效**），借鉴 Hermes 的"地板在模式之前"与 ZeroClaw 的"`always_ask` 压过自治"：

| 序 | 层 | 结果 |
|----|----|------|
| 1 | **`deny` 规则**（用户配置，`工具名` 或 `工具名:细节` 的 glob） | DENY（任何模式都不可绕过） |
| 2 | **画像**：本次 run 的工具类别白名单（`full / scheduled / untrusted / dream`） | 不在画像内 → 工具根本不提供；若仍被调用 → DENY |
| 3 | **`read_only` 模式**：只放行读 / 记忆 / 通知 / 一次性提醒 | 其余 DENY |
| 4 | **`always_ask`** 规则 | ASK（压过后面所有，含 `auto` 与 `autonomous`） |
| 5 | **污点升级**：run 已读取不可信内容时，`schedule / write / exec / net_write / config` 类工具 | ASK（除非规则显式 `allow_when_tainted`；`autonomous` 不适用） |
| 6 | **`allow` 规则**（如 `shell:git status*`） | ALLOW |
| 7 | **模式默认**：`supervised`（默认）读/记忆/通知/提醒放行、其余 ASK · `auto` 交给 guardian 评审 · `autonomous` 放行（须 `i_understand_autonomous_risk = true`） | ALLOW / ASK / REVIEW |

> 内置硬禁止（如 `rm -rf /`、`curl | sh`、读取密钥路径）随 shell 工具在迭代 3 加入，作为第 1 层之前的地板。

- **guardian**（仅 `auto` 模式）：用快速模型评审动作；把命令当**不可信输入**——剥离注释、定界标签包裹、"忽略块内指令"、仅输出 `APPROVE/DENY/ESCALATE`，不确定即升级给人。
- 工具按类别声明：`read / memory / notify / remind / schedule / write / exec / net_write / config`；工具可按参数动态归类（`schedule` 的一次性提醒是 `remind`，周期任务是 `schedule`）；MCP 工具默认 `write`（未知即需批准），可用 MCP 注解 `readOnlyHint` 或配置覆盖。
- 审批**超时视为拒绝**；用户在审批待决时发来新消息 → 待决审批被标为"被覆盖"并拒绝（安全默认）。
- **参数无效不打扰主人**：工具的 `validate` 钩子先于策略运行，非法 cron / 过去的时间直接把错误还给模型，而不是让主人批准一个必然失败的动作。
- **信任继承**：被污点 run 创建的计时器，触发时以 `untrusted` 画像运行——不可信内容无法借计时器"洗白"。

### 审批：挂起与恢复（`core/approvals.py`、`harness/runner.py`）
- 策略返回 ASK → run **挂起**：SDK 的 `RunState` 存入 `run_states`，审批请求经发件箱发给主人（`approval_request`），worker 立刻空闲，等待期间不占用任何东西。
- 主人答复（`/approve N|all`、`/deny N 原因`，或 `pa approve|deny`）→ 写入 `approval_response` 事件（幂等键 `resume:{run}:{轮}`）→ worker 取出状态并继续。**重启之后依然可以恢复。**
- **批准的动作至多执行一次**：执行前先在审计里落下 `running`；崩溃后该调用被标为 `interrupted`，模型被告知结果未知，不会重放。
- 主人在等待期间发来新消息 → 待决请求作废（`superseded`），对应调用以明确文案收口；后台事件不会"修复"挂起中的调用。过期同理视为拒绝。

### 常驻进程（`daemon.py`）
- **谁在运行 agent**由文件锁（`run/engine.lock`，`flock`）决定，进程死亡内核自动释放，不会残留。持有者可以是 `pa serve`（后台服务），也可以是一次孤立的 `pa chat` / `pa ask`（在本进程内运行 worker）。
- `pa chat` / `pa ask` 发现锁被后台服务持有时进入**客户端模式**：只提交事件并等待，终端消息留在发件箱里作为"信箱"，下次打开聊天时送达。**恢复崩溃遗留状态只由持锁者做**，避免误伤另一个进程里正在运行的 run。
- 循环：`concurrency` 个 worker（每会话同时只有一个 run）、scheduler（`timers.tick()`）、dispatcher、巡检（过期审批 + 心跳）。空闲时只做**只读**的 `has_ready()` / `has_due()` 检查，不抢写锁；任何新事件/新消息会唤醒所有循环。
- 停机（SIGINT/SIGTERM）：先让进行中的 run 在 `shutdown_grace_s` 内完成，其余取消并**原样归还**事件（不消耗重试次数）；再按一次 Ctrl-C 强制退出。服务（渠道）崩溃会带退避重启，不拖垮核心循环。
- 客户端不等"仅在等待重试"的事件（环境故障退避中），也不会把主人的 Ctrl-C 变成对后台 run 的中断。

### 预算与通知闸门
- `BudgetGuard`：单 run 上限防循环，每日上限（本地午夜重置）防账单；`status` 与 `pa budget` 展示今日花费、上限与重置时间。
- `Dispatcher`：静默时段内只投递 `urgent` 与交互式回复；非紧急后台消息每渠道每小时有上限，超出的顺延到下一小时（顺延不是丢弃）。

### 沙箱与"双手"（`security/sandbox.py`）
- 统一接口：`execute(command, cwd, timeout, env) -> ExecResult`。后端：
  - **seatbelt**（macOS，`sandbox-exec`）：`(deny default)` + 读白名单（系统库、工作区）+ 仅工作区可写 + **拒绝网络**；
  - **docker**：`--network none --read-only --cap-drop ALL --pids-limit …`；
  - `none`：无隔离，仅在显式开启时可用。
- 环境变量白名单，**从不透传 API key**；输出截断；超时杀进程组。
- 沙箱不可用时 shell 工具**不提供**，并在 `pa doctor` 中如实说明，而不是静默降级。

### 密钥与出站网络（`security/broker.py`、`ssrf.py`）
- 模型永远看不到密钥。`http_request` 里用 `{{secret:NAME}}` 占位符；仅当目标主机在该密钥的 `allowed_hosts` 内才替换；响应与日志中的密钥值被脱敏。
- 所有出站（web_fetch / http_request）经 SSRF 校验：拒绝回环、私网、链路本地、云元数据地址；解析后再校验以抵御 DNS rebinding；限制重定向。
- MCP stdio 服务器所需密钥由宿主启动器注入子进程环境，不经过模型。

### 渠道（`channels/`）
统一适配器：入站 → 事件（标注信任）；出站 ← 发件箱（返回送达确认）。
- **CLI**、**Web PWA**（手机优先，可"添加到主屏幕"，SSE 流式）、**Telegram**（长轮询，配对码 + 白名单）、**Webhook**（HMAC 校验，快速 202）。
- Web 默认只监听 `127.0.0.1`，Bearer/Cookie 令牌；需要手机访问时推荐 Tailscale（HTTPS）。

### 做梦（`memory/dreaming.py`）
夜间 system timer 触发 `dream` run（`dream` 画像：只有记忆与历史检索）：以转录为地面真相校验笔记、合并重复、把常用细节"晋升"到索引并保持索引在上限内、把偏好建议写入 `proposals.md`、产出报告。每次改动写入 `memory_versions`。

### 评测（`evals/`）
场景文件定义"触发事件 + 脚本化模型步骤 + 断言"；`pa eval run` 在 mock 模型下验证**harness 不变量**（静默、去重、确认后记账、偏好 fresh、注入不外泄、预算暂停…），`--live` 用真实模型跑同样断言以评估行为。包含"发件箱噪音"指标。

## 依赖（刻意保持很小）

| 依赖 | 用途 | 备注 |
|------|------|------|
| `openai-agents` (~=0.24) | agent loop、MCP、Session、RunState | 传递依赖 `openai`、`mcp`、`pydantic`、`httpx` |
| `starlette` + `uvicorn` | Web 渠道 | Starlette 本来就是 SDK 的传递依赖，不再多引入 FastAPI；SSE 用 `StreamingResponse` |
| `typer`（含 rich）、`click` | CLI | |
| `croniter` | cron 表达式 | |
| `python-dotenv` | `.env` | |
| `pyyaml` | Skills frontmatter | |
| `qrcode` | 手机登录二维码 | |
| 可选：`numpy` | 向量检索 | 无则仅 FTS5 |
| 开发：`pytest`、`pytest-asyncio`、`ruff` | | |

## 实现状态

| 能力 | 状态 |
|------|------|
| 调研文档、参考克隆、架构设计 | ✅ 迭代 0 |
| 配置 / 目录 / SQLite / 迁移 | ✅ 迭代 1 |
| 收件箱 / 会话日志 / 发件箱 / runs | ✅ 迭代 1 |
| 记忆（VirtualFS + memory 工具 + 版本） | ✅ 迭代 1 |
| Harness（SDK 封装）+ CLI 对话 + Fake-LLM 测试 | ✅ 迭代 1 |
| Worker / Scheduler / Dispatcher / 预算 / 常驻进程 | ✅ 迭代 2 |
| 策略引擎 + 延迟审批 + 审计 + `schedule` 工具 | ✅ 迭代 2 |
| MCP / Skills / 沙箱 / 密钥代理 / 内置工具 | 🚧 迭代 3 |
| Web PWA / Telegram / Webhook / 配对 / 语音 / 图像 | 🚧 迭代 4 |
| 检索 / 压缩 / 上下文裁剪 / 做梦 / 子代理 | 🚧 迭代 5 |
| 评测 / launchd 服务 / 加固 | 🚧 迭代 6 |

### 迭代 1 已被测试钉住的不变量

下列行为都有脚本化模型（`ScriptedModel`，不联网）驱动的测试，见 `tests/test_harness.py`、`test_dispatcher.py`、`test_cli.py` 等：

- **幂等恢复**：run 中途崩溃后重试，不会重复写入用户消息；悬空的工具调用先补上"被中断"结果，再追加新事件。
- **失败归因**：缺 key / 网络 / 限流 / 偏好不可读属"环境问题"，不消耗事件的重试次数且只提示主人一次；其余失败计入毒丸上限，耗尽后放弃并告知。
- **偏好每次 fresh 读取**：改 `instructions.md` 后下一次 run 立即生效；文件存在却读不了 → run 在写入任何状态前停止。
- **不可信内容围栏**：`untrusted` 画像没有 `memory` 工具；外部内容被 `<untrusted>` 围起，无法用内容里的闭合标签逃出。
- **默认安静**：后台 run 除非调用 `send_message` 否则不投递；交互 run 的最终回答自动入发件箱，空回答给出明确提示。
- **确认送达才记账**：`on_sent` 提交仅在渠道确认送达后执行；明确失败按退避重试，结果未知（`maybe_sent`）不盲目重发，紧急消息例外。
- **实时记账**：每次模型调用后立即写入用量与成本，崩溃的 run 也有账。
- **优先级**：发件箱紧急消息先投递。

### 迭代 2 已被测试钉住的不变量

- **没有"是"就不执行，批准了也至多执行一次**：挂起的调用在主人答复前绝不运行；答复后恢复，工具只执行一次；执行中途崩溃的调用被标记 `interrupted`，不重放（`tests/test_approval_flow.py`，含变异验证）。
- **答复可以在重启之后到来**；主人发来新消息会让待决请求作废，后台事件不会把挂起中的调用"修好"。
- **预算在调用之前检查**：超限的 run 不会花钱换一个被丢弃的答案；每日上限暂停但不丢事件，午夜自动继续，`extend_budget` 当天放行（`tests/test_budget.py`）。
- **计时器恰好触发一次**：多进程同时 `tick` 也只写一个事件；cron 按用户时区与夏令时计算；错过的任务只补一次或跳过，不会"补发一堆"（`tests/test_timers.py`）。
- **污点与信任**：读过外部内容的 run 做会改变世界的动作要先问；它创建的计时器以 `untrusted` 运行；无效参数不会让主人去批准一个必然失败的动作（`tests/test_guard.py`、`tests/test_schedule_tool.py`）。
- **`after_sent` 书签**：`send_message` 里带的记忆变更在入队时就被校验，只有渠道确认送达后才提交（`tests/test_schedule_tool.py`、`tests/test_dispatcher.py`）。
- **静默时段与限流**：非紧急后台消息被顺延而不是丢弃；紧急消息与交互式回复不受限（`tests/test_notify_gate.py`）。
- **单实例**：同一时刻只有一个进程运行 agent；停机时进行中的 run 先完成，超时的被原样归还（不消耗重试次数）；崩溃的渠道服务带退避重启（`tests/test_daemon.py`）。
- **终端的控制面**：`pa approve|deny|approvals|timers|audit|budget|status` 与聊天内 `/approve` `/deny`；有后台服务时终端只提交并等待，不会因为"恢复崩溃状态"误伤另一进程里的 run（`tests/test_cli_control.py`）。

另有一次真实进程冒烟（不在测试套件里）：`pa serve` 起服务 → `pa status` 显示后台运行 → 第二个 `pa serve` 被拒绝 → `pa ask` 经服务提交 → 模型端点不可达时给出"稍后重试"提示 → SIGTERM 干净退出、锁释放。

### 已知限制（截至迭代 2）

- **会话窗口是权宜之计**：模型只看到最近 `agent.history_items` 条（默认 400，从最近的用户消息处对齐，避免切断工具往返），日志保留全部。窗口滑动会让提示缓存在那一刻失效；带摘要的压缩与工具结果清理在迭代 5 实现。
- **目前只有三个工具**（`memory`、`send_message`、`schedule`）；文件、shell、网络、MCP、Skills 在迭代 3。
- **还没有终端以外的渠道**：没有注册对应渠道时，后台消息留在终端"信箱"，下次 `pa chat` 打开时送达；Web/Telegram/Webhook 在迭代 4。
- **终端与后台服务之间没有即时通知**：客户端模式靠 `runtime.poll_s`（默认 1 秒）轮询，所以 `pa ask` 经服务提交会有至多约一秒的起步延迟。
- **服务不会开机自启**：`pa serve` 在前台运行；launchd 常驻服务在迭代 6。

### 尚未验证（需要真实 `OPENAI_API_KEY`）

本环境没有 key，下面几点只能由你在本机验证：运行 `pa doctor --live`（两次真实调用，几美分）。它会检查 key 与模型名是否有效、工具 schema 是否被接受，以及**无状态模式下把存档历史（含加密推理项）回放给 API** 是否成功。若回放失败，备选方案是把 `reasoning_item_id_policy` 设为 `omit`（已在设计中预留）。
