# 0.4.0：云端与自有 Agent 协作

日期：2026-11-18。基线：0.3.2（`fcb005975acba81c26dac6f7767046af2e7cb615`）。

## 本版交付

优先解决从手机向已有 Grok Bot / Muse 派发任务、追踪执行并接收成果的问题，同时让自有机器上的 Codex、Cursor 和 Personal Agent 进入同一协作流程。使用现有 Railway 或自有控制服务、SQLite 和已有执行端，不引入新的付费云沙箱。

| 能力 | 实现与验收点 |
| --- | --- |
| Grok 自动派发 | 批准计划后先保存 wake，再调用官方 Routine Bearer webhook。HTTP 200 只表示接收；领取、执行、回传和验收分别记录。超时或未知响应不会自动重发。 |
| Muse 接入 | 每个 Muse 使用独立 worker token，经 HTTP / OpenAPI / MCP / 单文件 Bridge 主动领取任务；支持手动启动和经账号验证的原生定时。没有假定供应商公开了任意会话唤醒 API。 |
| 多 Agent 任务图 | 不可变计划、revision / hash、一次批准、依赖门控、必需步骤失败传播。工作台支持并行、顺序、并行后汇总；API 可表达一般 DAG。 |
| 手机操作 | 建立和编辑连接、授予使用权、轮换/撤销 token、下载接入说明；查看进度、回答问题、下载文件、验收或拒绝结果、核对未知执行。 |
| 自有 Agent Host | Codex app-server、官方 Cursor ACP SDK、独立 Personal Agent profile；保存进程/session/turn 身份、原生审批、续租、取消和恢复。 |
| 代码成果 | 每个 attempt 使用固定基线的 detached Git worktree；回传二进制 patch、新文件 tar 与 SHA-256 清单。源仓库不自动 stash、commit、merge 或 push。 |
| 小规模协作 | 保留私人/项目空间、成员权限和独立设备身份；连接授予权由资源所有者控制，项目管理员不能因此读取私人任务。 |

## 实现结构

原有 Inbox、Timers、Memory、Permissions、Outbox 和 Docker Runner 保留。`orchestration/` 是持久任务与授权账本；`channels/web/agents.py` 提供浏览器和 worker 接口；`agent_adapters/` 负责外部系统协议；工作台及对话工具只通过同一账本提交或查看任务。

成熟组件用于协议和生命周期基础：官方 MCP SDK 提供无状态 HTTP 工具传输，官方 ACP Python SDK 负责 Cursor framing/dispatch，psutil 跟踪本机进程身份及已见后代。无需引入另一个 Agent 框架替换当前 Harness。

运行依赖新增 `agent-client-protocol>=1.0,<1.1`、`psutil>=7.2.2,<8`，均纳入 `uv.lock`。开发依赖补充 psutil 类型定义。Codex 协议测试使用本地 CLI `0.167.0-alpha.4` 生成的 schema，来源与版本见 `tests/fixtures/codex_schema/SOURCE.md`。

## 故障和权限审查

| 审查项 | 本版处理 |
| --- | --- |
| 领取响应丢失造成重复开工 | 稳定 worker nonce、原子领取与持久 attempt；同 nonce 恢复原执行，未知占用禁止另起执行。 |
| webhook 超时后反复唤醒 | 网络调用前持久化 wake；未知投递保留资源占用；停止核对由资源所有者执行。 |
| 取消但未领取的唤醒永久占用 | 独立唤醒停止确认；保留原 HTTP 回执与审计，解除占用后仍不重发原计划。 |
| 手动连接挤占自动唤醒候选 | Grok/wake mode 筛选在候选数量限制之前完成。 |
| token 在请求读取期间被轮换 | 修改前在数据库事务中重新鉴权，旧连接不能跨授权代际领取新任务。 |
| 轮换后本地 Host 无法恢复 | 当前同 Agent 凭据可读取旧 attempt 的最小停止证明；旧任务正文、结果和 lease 仍受代际限制。凭据更新保留本地 journal。 |
| CLI EOF/强杀被误判为完成 | 必须有原生终态及进程清理证据；不确定时记录 `unknown` / `uncertain` 并停止重放。 |
| 同目录并发写入 | Host/profile 与本机 canonical 工作目录锁；Git 独立 worktree；脏源目录在领取之前拒绝。 |
| 其他成员借用已有 CLI 私人环境 | Codex/Cursor 默认限资源所有者请求；显式共享 profile、共享 Agent、空间可见任务三项同时满足才接受其他成员。 |
| 上游结果提升执行权限 | 依赖作为不可信数据；Personal Agent 使用结构化 taint，peer URL 不成为 owner-provided URL，既有 allow 规则不能消除逐动作审批。 |
| 文件/结果跨任务泄露 | 当前 attempt 与已验收的直接依赖范围；不可变文件、大小及空间配额、hash 核验；结果与文件原子提交。 |
| 密钥通过文件名或回执泄露 | 已知连接秘密从进度和正文脱敏，含已知秘密的文件名/内容拒绝存储。 |
| API 连接器拿不到依赖文件 | OpenAPI 包含文件下载 operation，并可返回 base64 JSON；MCP 与 Bridge 使用同样授权规则。 |

## 验证记录

完整质量门覆盖 Ruff 格式与 lint、200 个源模块的 strict Mypy、Python 3.13 / 3.11 离线回归，以及 OpenAPI 生成、TypeScript、前端 bundle/许可证一致性和 159 项 JavaScript 测试。最终数量记录在本次 PR 的验证区。

集成测试包含真实 TCP HTTP listener 与实际 Bridge：浏览器配对、建立连接、批准、领取、进度、问答、Bridge 重建、同一结果重试、文件和人工验收。另覆盖真实应用 HTTP 与 MCP 两代协议，以及真实 Store/HTTP 的 token 轮换和 Host 停止恢复。

原生协议测试启动受控 stdio 子进程，验证 Codex/ACP 合同、权限回答、取消、进程组及已见后代清理；Git 测试使用真实临时仓库，验证固定 worktree、源目录不变、二进制差异和封存结果。它们没有调用真实供应商模型或登录账号。

尚未完成 Grok/Muse/Codex/Cursor 真实账户联调；macOS 分支已实现并测试，未在真实 Mac 上验收。当前环境未完成浏览器截图验收。以上不由离线测试替代。

## 上线步骤

1. 备份整个实例、各空间数据库、持久卷与私有配置；停止旧进程。
2. 更新代码并执行 `uv sync --frozen`，使用原部署方式启动 `uv run pa serve`。数据库自动从 schema 12 升到 14。
3. 在工作台为每个 Grok/Muse/Host 分别建立连接。worker token 存入执行端私密配置；控制数据库只保存其哈希。
4. Grok：在部署环境设置 Routine URL/key，再把对应**变量名**填到连接中；安装本项目工具或 Bridge，并配置原生 Routine 指令。
5. Muse：先从原生会话验证工具调用和领取；只有账号的原生定时确已配置，才选 `native_schedule`。网络可使用现有 HTTPS 或可达的 Tailscale 路径。
6. 先用无副作用任务验证同一 task ID 的领取、文字/小文件回传和手机验收，再测取消、断线及 token 轮换后的恢复。
7. 自有 Host 按接入文档选择 CLI、专用状态目录和干净 Git 工作区。非 Git 目录仅在显式 `--allow-direct-workdir` 后直接执行。

回退必须恢复升级前的数据库快照并切回旧代码；0.3.2 会拒绝 schema 14，不能只回退包版本。保留单个控制进程及持久卷部署方式。

## 明确保留的限制

- Grok/Muse 账号已有的权限、原生工具、套餐和费用仍由供应商控制。取消按钮不是供应商级 kill，也不是停止扣费的证明。
- worktree 提供工作文件分离，不是操作系统安全沙箱；同一 SaaS 账号下多个 Bot 也不能视为独立沙箱。
- 每个 Agent 串行；未实现供应商账号级共享资源池或全局 SaaS 费用聚合。
- Personal Agent 子任务执行单次预算，专用 profile 跨 attempt 的日费用尚未聚合。
- 结果文件每空间限 128 MiB；没有自动删除未验收成果或按保留期自动清理协作账本。
- UI 提供三种常用任务布局；任意 DAG、同一 Agent 多个步骤等高级组合使用 API。

使用和设计详见 [MULTI_AGENT.md](MULTI_AGENT.md)，具体命令、Grok/Muse 提示模板与 Host 恢复详见 [CLOUD_AGENTS.md](CLOUD_AGENTS.md)。
