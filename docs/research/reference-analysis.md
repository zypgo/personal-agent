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
