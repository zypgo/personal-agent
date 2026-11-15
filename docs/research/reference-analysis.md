# 同类开源项目对比与取舍

> 调研日期：2026-11-15。克隆位置：`references/`（锁定 commit 见 [`references/SOURCES.lock`](../../references/SOURCES.lock)）。
> 星标数、提交时间来自当日 GitHub API；安全评级与"已知问题"部分明确标注了出处（第三方评测），未经本项目独立复现。

## 一、选择标准

1. **维护活跃**：近一周内有提交，未归档。
2. **同类**：个人常驻 agent / agent harness / 其关键子系统（记忆、审批、定时、技能）。
3. **许可证可借鉴思路**：均为 MIT 或 Apache-2.0（Anthropic skills 仓库按子目录各自授权，我们只读规范，不复制技能内容）。

## 二、总览

| 项目 | 语言 / 许可 | ★ | 最近提交 | 体量 | 我们读它的原因 |
|------|------------|---|---------|------|---------------|
| [openai/openai-agents-python](https://github.com/openai/openai-agents-python) v0.24.0 | Python / MIT | 30.7k | 2026-11-15 | 119MB | **我们的执行引擎**：Session 协议、`RunState` 延迟审批、MCP、测试工具 |
| [anthropics/claude-quickstarts](https://github.com/anthropics/claude-quickstarts) `managed-agents/daily-brief` | Python / MIT | 21.0k | 2026-11-06 | 5MB | Anthropic 无人值守运行的**参考实现**（运行步骤、书签/台账/运行记录） |
| [anthropics/skills](https://github.com/anthropics/skills) + [agentskills.io 规范](https://agentskills.io/specification) | — / 子目录各异 | 194.9k | 2026-11-14 | 5MB | Agent Skills 格式与渐进披露 |
| [anthropics/claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python) | Python / MIT | 8.5k | 2026-11-13 | 2MB | 权限模式、hooks、`can_use_tool` 的分层语义 |
| [HKUDS/nanobot](https://github.com/HKUDS/nanobot) | Python / MIT | 50.1k | 2026-11-14 | 84MB | 紧凑的 Python 常驻 agent：Dream、SQLite 会话、cron、配对 |
| [qwibitai/nanoclaw](https://github.com/qwibitai/nanoclaw) | TypeScript / MIT | 31.1k | 2026-11-14 | 36MB | 容器隔离 + 凭据网关（占位符） |
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | Python / MIT | 263.5k | 2026-11-15 | 1.4GB（sparse） | 审批流水线、cron 的错过/重试/送达队列、FTS5 会话检索、发送者配对 |
| [openclaw/openclaw](https://github.com/openclaw/openclaw) | TypeScript / MIT | 394.5k | 2026-11-15 | 14.7GB（sparse） | 网关/渠道/技能/配对/exec 审批的事实标准 |
| [zeroclaw-labs/zeroclaw](https://github.com/zeroclaw-labs/zeroclaw) | Rust / Apache-2.0 | 33.4k | 2026-11-14 | 292MB（sparse） | **审批规则优先级**与"默认受监督"的实现 |

**考察过但未采用**：[Letta](https://github.com/letta-ai/letta)（MemGPT 系记忆框架，较重，且 Anthropic 的结论是文件式记忆 + 做梦已足够）、[mem0](https://github.com/mem0ai/mem0)（向量记忆层，引入外部依赖与隐私面）、[goose](https://github.com/block/goose)（编码向 agent，Rust）、IronClaw / QwenPaw / AstrBot / CowAgent / PicoClaw / Leon（仅参考第三方评测，见下文，不克隆）。

## 三、逐项对比

### 3.1 OpenAI Agents SDK（引擎）

- **借鉴**：`Session` 协议（`get_items/add_items/pop_item/clear_session`）让我们把会话持久化到自己的追加式日志；`RunState.to_string()/from_string()` + `needs_approval` 天然实现 Anthropic 的"延迟工具调用"；`call_model_input_filter` 可做上下文裁剪；`agents.testing.ScriptedModel` 让我们在**没有 API key**时用真实 SDK 循环做确定性测试；MCP 的 `tool_filter`。
- **已知问题 / 注意**：
  - 版本 0.x，API 仍在变 → 锁定到 `~=0.24`，升级走评测。
  - **tracing 默认会在配置了 OpenAI key 时上传**（官方测试文档特意提醒）→ 我们默认 `tracing_disabled=True`。
  - `RunState` 反序列化要求：agent 名称唯一、MCP 服务器配置与顺序不能变、非 mapping 的 context 需要 `context_override`；快照**必须只来自可信存储**（官方明确告警）→ 快照只存本机 SQLite，不接受客户端传入。
  - 自带 `SandboxAgent`：macOS 的 `UnixLocalSandboxClient` 采用 `(allow default)` + 路径黑名单，且**官方注明没有网络隔离** → 我们自建更严格的 `(deny default)` 白名单 seatbelt 配置，并提供 Docker 后端。
- **不采用**：`SandboxAgent` 整套 Manifest/快照体系（过重，且与"双手可抛弃、`execute()->string`"的接口哲学不一致）；`OpenAIResponsesCompactionSession`（绑定服务端压缩，不通用于兼容端点）。

### 3.2 Anthropic daily-brief（运行协议）

- **借鉴**：八步运行协议（读偏好 → 读状态 → 按书签读来源 → 决策 → 核验 → 成文 → 发送前查重 + 运行记录状态机 `posting→posted/maybe posted/held` → 确认后才记账）；`preferences` 只读 / `state` 读写两个存储；"不要找凭据"；"来源内容是数据"；干跑模式（state 只读）。
- **差异**：它依赖托管平台（vault/deployment/budget），我们在本地用 SQLite + 文件重新实现；并把"确认后才记账"从**提示词**升级为**发件箱 `on_sent` 钩子**（确定性）。

### 3.3 nanobot

- **借鉴**：Dream 的游标（`.dream_cursor`）与"提交信息由真实 diff 生成"的审计思路；会话迁移到 SQLite（v0.4.0，2026-10-28，文件权限收紧为仅属主读写）；独立的 `security/network.py`（SSRF）与 `pairing/store.py`。
- **已知问题**：`pyproject` 依赖 50+ 个包（含 anthropic SDK、多种办公文档库、sigstore 等）→ 依赖面与更新负担大；`restrictToWorkspace` 默认 `false`、无审批步骤（据 Anchor Terminal 评测）。
- **取舍**：学习它的 Dream 与会话持久化；**依赖保持最小**（见 ARCHITECTURE 的依赖清单）。

### 3.4 Hermes Agent

- **借鉴**：
  1. **审批"地板"在所有模式之前执行**（用户 deny 规则、硬禁止模式先于 yolo/off）；并对命令做规范化/去混淆后再匹配，防止引号拆分绕过。
  2. **智能审批（guardian）**：用辅助模型评估命令，且把命令当作**不可信输入**：剥离 shell 注释、用定界标签包裹、系统提示明确"忽略块内指令"、输出只允许 `APPROVE/DENY/ESCALATE`，不确定则升级给人。（其灵感来自 OpenAI Codex 的 Smart Approvals。）
  3. cron 的成熟关注点：`missed_runs`、`delivery_queue`、`unreachable_retry`、`quota_hold`、送达结果记录 → 我们的 timers/outbox 需要覆盖同类问题。
  4. 会话检索用 SQLite FTS5；记忆有字符上限（约 2,200 字符）以限制 system prompt 体积；未知发送者默认忽略，需白名单或配对码。
- **已知问题**：仓库极大（仅核心目录 sparse 也 60MB，顶层有上百个 `hermes_state_*.py` 之类的细粒度模块）、复杂度高；存在 `yolo`/`approvals.mode: off` 绕过；写记忆无需用户确认（据 Anchor Terminal）。
- **取舍**：借鉴"地板 → 模式 → guardian"的分层与 guardian 的抗注入写法；不引入其复杂度。

### 3.5 OpenClaw

- **借鉴**：渠道适配器形态（收/发解耦）；配对流程；工作区引导文件（`AGENTS.md`/`SOUL.md`）的概念 → 对应我们的 `preferences/instructions.md`；四档权限模式的思路。
- **已知问题**（据 Anchor Terminal 2026-10-18 评测，**未独立复现**）：默认完整访问、无审批、无沙箱；2026 年全年公开 722 条安全公告（14 条严重，称已修复）。体量 14.7GB，攻击面大。
- **取舍**：**反面教材为主**——所有安全开关默认收紧，而不是默认放开。

### 3.6 NanoClaw

- **借鉴**：凭据放在宿主侧网关，容器内只有占位符（与 Anthropic vault 同构）；每会话独立容器。
- **已知问题**（据评测）：容器内关闭权限提示；出站网络默认开放，需 `NANOCLAW_EGRESS_LOCKDOWN=true`；默认向 PostHog 发诊断事件。
- **取舍**：采用占位符 + 代理思想；**默认无网络、无遥测**。

### 3.7 ZeroClaw

- **借鉴**：审批优先级——`ReadOnly` 拦截一切；**`always_ask` 压过 Full 自治与 `auto_approve`**（支持 `*`）；默认 Supervised，仅一小撮只读工具免询问；询问无应答按拒绝处理（评测记载为 120 秒）。
- **已知问题**（据评测）：执行的命令不写审计日志；web fetch/search 免询问；沙箱内工具仍有完整网络。
- **取舍**：采用同样的优先级；**补上审计日志**；web 抓取走 SSRF 防护 + 污点升级。

### 3.8 第三方评测里的其他反例（仅作警示，不克隆）

| 项目 | 评测指出的问题 | 我们的对策 |
|------|---------------|-----------|
| IronClaw | 有审批步骤但**默认开启 auto-approve** | 安全默认值：`supervised` |
| QwenPaw | guard 开、沙箱关——**guard 不是沙箱** | 策略（policy）与隔离（sandbox）分开设计、分开自检（`pa doctor`） |
| PicoClaw / Leon | 长期无提交 / 默认每 30 分钟自动跑任务、遥测默认开 | 不使用；无自动任务、遥测默认关 |

## 四、汇总：我们从中得到的设计决策

| # | 决策 | 主要依据 |
|---|------|---------|
| D1 | 引擎用 OpenAI Agents SDK，但**封装在 `harness/` 端口之后**，业务逻辑不直接依赖 SDK 细节 | SDK 0.x 仍在变 |
| D2 | 会话日志追加式、自有 SQLite；压缩为"投影" | Anthropic D + C（做梦需要原始转录） |
| D3 | 延迟审批用 SDK `RunState` 序列化到本机 SQLite；**从不接受外部传入的快照** | SDK 官方安全告警 |
| D4 | 策略分层：地板 → 画像裁剪 → `always_ask` → 污点升级 → 模式 → allow 规则；guardian 只做"auto"模式的一环且按不可信输入加固 | Hermes + ZeroClaw + Anthropic auto mode |
| D5 | 后台 run 默认**裁剪能力**而非依赖审批；不可信事件（webhook）触发的 run 默认只读 + 只能通知 | OpenInstinct 的做法 + Anthropic B |
| D6 | 沙箱 `(deny default)` 白名单 + 无网络；网络出口只有带 SSRF 防护与密钥注入的 `http_request`/`web_fetch` | SDK 本地沙箱的缺口 + NanoClaw 网关 |
| D7 | 发件箱 `on_sent` 钩子承担"确认后才记账" | Anthropic B 的规则三/四 |
| D8 | 依赖最小化；遥测默认关；tracing 默认关 | nanobot 依赖膨胀、SDK 默认上传 trace、NanoClaw/Leon 默认遥测 |
| D9 | 审计日志默认开（每次工具调用的决策与结果，已脱敏） | ZeroClaw 缺口 + Anthropic E（透明） |
| D10 | 未知发送者默认拒绝，配对码 + 本机确认 | Hermes / OpenClaw / nanobot 一致做法 |

## 五、来源与授权说明

- 本项目**没有复制**上述任何仓库的源代码；仅参考其设计、接口形态与文档中的公开结论，并用自己的实现重写。
- 引用的第三方评测：[Anchor Terminal — Best harnesses for personal agents（2026-10-18）](https://www.anchorterminal.com/blog/best-harnesses-for-personal-agents)、[Personal Agent Bench — 15 personal agents](https://personalagentbench.com/open-source-personal-agents/)。其结论可能随版本变化，使用前请以对应仓库当前状态为准。

## 六、迭代 4 补充调研：渠道、配对、Webhook、Web 认证、语音（2026-11-15）

> 方法同前：只读 `references/` 里的实现与文档，比较设计与已知坑，用自己的代码重写，**不复制源码**。

### 6.1 Telegram 渠道

| 项目 | 做法 | 我们的取舍 |
|------|------|-----------|
| nanobot（`channels/telegram/runtime.py`，2200+ 行） | 基于 python-telegram-bot；Markdown→HTML 转换 + 在 4000 字符处切分，HTML 展开后超 4096 再用更小预算重切；流式编辑消息；429 读取 `retry_after`；`allowed_updates` 只收 `message`/`callback_query` | **只用 httpx 直连 Bot API**（不引入大依赖）；同样"先切 Markdown 再转 HTML、解析失败回退纯文本"；**不做流式编辑**（编辑消息会让"送达确认"语义变模糊） |
| hermes-agent（`plugins/platforms/telegram`） | 适配器接口统一；未知发送者默认忽略，需白名单或配对码 | 采用统一 `Channel`/`Service` 形态（已有） |
| openclaw | DM 默认"配对"策略 | 见 6.2 |

踩坑清单（来自这些实现的处理痕迹 + Bot API 文档）：
1. 消息上限 4096 字符；Markdown 渲染成 HTML 会变长，必须**切分在转换之前**，且切在围栏代码块中间时要补全围栏。
2. `getUpdates` 与 webhook 互斥（409）；两个进程同时轮询也会 409 → 退避并给出明确提示，不要死循环。
3. `offset` 必须在**事件已落库之后**才推进；崩溃重放靠收件箱的 `(source, external_id)` 唯一索引去重。
4. 429 带 `retry_after`；网络层"请求发出后超时"= **结果未知**，不能当成"没发出"重发（我们的 `ChannelError` 语义已覆盖）。
5. 多段消息发送到一半失败：整体重发会重复前几段 → 按"结果未知"处理，由发件箱标 `maybe_sent`，不盲目重发。
6. Bot token 会出现在请求 URL 里：日志与异常必须脱敏（`redact` 已含 Telegram token 模式）。
7. 中国大陆直连 `api.telegram.org` 常不通：httpx 默认读取 `HTTPS_PROXY`/`ALL_PROXY`，另提供 `api_base`（自建 Bot API 服务器或反代）与 `proxy` 配置。

### 6.2 发送者配对（谁能和代理说话）

| 项目 | 机制 |
|------|------|
| nanobot `pairing/store.py` | JSON 文件；8 位码（`ABCD-EFGH`），10 分钟过期；同一发送者重复请求返回同一个码；`approve/deny/revoke`；存储不可读时**拒绝**（fail closed） |
| openclaw `docs/channels/pairing.md` | 1 小时过期；**只在创建请求时回复一次**（约每小时每发送者一次）；每个渠道账号**最多 3 个待审批请求**，其余忽略 |

**采用**：8 位无歧义字符码；1 小时过期；同一发送者幂等；只回复一次（回复成功才记录）；每渠道最多 3 个待审批；陌生人的消息**永不产生事件**（不花钱、不进入模型）；批准只能在本机 CLI（`pa channels approve`）；存储放 SQLite（原子、可并发），读取失败即拒绝。

### 6.3 Webhook 入口

hermes-agent `gateway/platforms/webhook.py` 的要点（我们全部采纳）：
- **每条路由必须有非空密钥**（空密钥的 HMAC 等于公开密钥），启动即校验；
- 签名方案在路由上**固定**（`github` / `gitlab` / `standard` / `generic`），不自动探测，避免被降级；
- 只有"绑定时间戳"的通用方案可防重放；纯 body 签名（GitHub 就是）靠**投递 ID 去重**；
- 请求体大小在读取**之前/期间**限制；每路由限速（默认 30/分）；常量时间比较；
- 幂等缓存按"路由 + 投递 ID"。

我们的差异：webhook 事件一律 `Trust.UNTRUSTED`，由 `untrusted` 画像处理（只能读与通知），内容用 `<untrusted>` 围栏。

### 6.4 Web 渠道与认证

nanobot 的 WebSocket 渠道：默认绑定 `127.0.0.1`，绑 `0.0.0.0` 必须配 token；token 可经 **query 参数** 传递（会进入访问日志与 Referer）。

**我们的做法更严**：一次性邀请码放在 URL **片段**（`#`，不会发给服务器，也不进日志）→ 页面脚本换取 `HttpOnly; SameSite=Strict` 的会话 cookie；服务器只存 token 的 SHA-256；会话可吊销；写请求要求自定义头 + `Origin` 校验；校验 `Host`（防 DNS 重绑）；严格 CSP；关闭 uvicorn 访问日志。

### 6.5 语音转写（OpenAI 官方文档，2026-11 核对）

- 推荐文件转写模型：`gpt-transcribe`（按音频分钟计费，约 $0.0045/分钟）；`whisper-1`、`gpt-4o-*-transcribe` 将于 **2027-02-26** 下线 → 默认用 `gpt-transcribe`，模型名可配置。
- 文件上限 25 MB；官方支持格式 `mp3 mp4 mpeg mpga m4a wav webm`（**未列出 ogg**，而 Telegram 语音是 OGG/Opus）→ 有 `ffmpeg` 时先转 m4a，没有就原样上传并在失败时给出明确提示。
- 费用不属于任何一次 run，因此新增 `spend` 记账表并计入每日预算。

### 6.6 迭代 4 的设计决策

| # | 决策 | 依据 |
|---|------|------|
| D11 | 渠道 = `Channel`（发）+ `Service`（收）的统一适配器；内置 Telegram/Web 与插件提供的渠道走同一接口 | hermes/openclaw 的适配器形态；用户要求可插拔 |
| D12 | 所有渠道共享会话 `main`；回复回到发话的渠道；后台消息发往 `notify.channel`（可设 `auto`=最近一次对话的渠道） | 个人代理是"一个人、一个持续的对话" |
| D13 | 新增 `transcript`（面向主人的对话时间线），与模型可见的会话日志分离 | `/new`、压缩、墓碑都不应让手机上的历史消失 |
| D14 | 陌生发送者零成本零注入面：不产生事件、不进模型 | openclaw/nanobot/hermes 一致做法 |
| D15 | 渠道文本命令（`/approve` `/deny` `/new` `/status`…）在渠道层处理，不经模型 | CLI 已有同样的命令 |
| D16 | 图片入模型时只携带最近 N 张，更早的换成文字占位 | 控制 token 与请求体积 |

### 6.7 迭代 4b 的实现决策（Web 页面、Webhook、插件渠道）

| # | 决策 | 依据 |
|---|------|------|
| D17 | 网页只监听回环地址；手机访问靠 Tailscale 的 HTTPS（`tailscale serve`），不在程序里做证书与公网暴露 | nanobot 默认回环、绑 `0.0.0.0` 必须有令牌；个人 agent 不该自己扛公网 |
| D18 | 配对码放在 URL 片段，页面换成 `HttpOnly; SameSite=Strict` 会话；库里只存 SHA-256；邀请 10 分钟、一次性、最多 5 个未用 | nanobot 的 query 令牌会进日志与 Referer（6.4） |
| D19 | **Origin 逐字比较（含端口）**，而不只依赖 SameSite | cookie 属于"主机"不是端口：同机另一个端口上的页面与我们是 same-site，它发的请求会带上我们的 cookie |
| D20 | 页面不用构建链：原生 ES 模块 + 一个 Service Worker；脚本从不把文字变成标记；CSP `default-src 'none'` | 个人项目不需要前端工具链；被注入的"图片"也带不走东西 |
| D21 | 网页是"拉取"渠道：`send` 只确认"消息已在页面会读的地方"；实时靠 SSE，重连按数据库补齐，不另存状态 | 避免"页面上有而数据库里没有"的状态；Web Push 留作后续 |
| D22 | Webhook 独立监听端口（与聊天页分开），对外只说很少的话：没有路由 / 密钥不可用 / 签名错 / 过期，一律 404 | 陌生人探测不该得知路由是否存在；原因写进日志，`pa webhooks` 可见 |
| D23 | 投递 id 的来源逐方案钉死：GitHub 取 `X-GitHub-Delivery`；GitLab 取 `Idempotency-Key`/`webhook-id`；`generic` 取签名本身；都没有时按"路由 + 正文哈希 + 日期"兜底 | **GitLab 的 `X-Gitlab-Webhook-UUID` 标识的是钩子本身、每次都一样**，拿来当 id 会把第二个事件当成重复（变异验证抓到的坑） |
| D24 | 深层嵌套的 JSON 保留为文本、超大数字保留为文本；`prompt`（主人写的）放在围栏**外**，投递内容放在围栏**内** | 解析器本身不该成为攻击面；可信与不可信的指令分开 |

另外三条在实现中被**真实 API** 证实或纠正的发现：

1. **SDK 默认的 `prompt_cache_key` 是按会话生成的**，让每个新对话的 7.6k token 前缀（指令 + 工具）都重新计费。我们按画像共用一个 key（`personal-agent:<profile>`），另一个对话的第二条消息 80% 以上命中缓存。
2. **无状态模式（`store=false`）下回放含加密推理项的历史是成立的**（`pa doctor --live`），因此不需要 `reasoning_item_ids = "omit"`。
3. **配置里的错误文字会回显输入值**：pydantic 的校验错误带 `input_value`，把真密钥贴进 `secret_env` 会被原样打印到终端。现在凡"环境变量名"字段只接受大写字母名字，且 `ConfigError` 从不回显输入。
