# 0.4.0：和云端、自有 Agent 协作

日期：2026-11-18。基线：0.3.2（`fcb005975acba81c26dac6f7767046af2e7cb615`）。

## 这一版交付了什么

这一版先解决的是：从手机上，把任务派给已有的 Grok Bot / Muse，跟踪执行，并收下成果；同时让你自己电脑上的 Codex、Cursor 和 Personal Agent，也进入同一套协作流程。用的是现有的 Railway 或自有服务端、SQLite 和已有的执行端，没有引入新的付费云沙箱。

| 能力 | 做了什么，怎么验收 |
| --- | --- |
| Grok 自动派发 | 批准计划之后，先保存 wake，再调用官方的 Routine Bearer webhook。HTTP 200 只表示“收到了”；领取、执行、回传和验收，各自单独记录。超时或响应不明时，不会自动重发。 |
| Muse 接入 | 每个 Muse 用独立的 worker token，通过 HTTP / OpenAPI / MCP / 单文件 Bridge 主动领取任务；支持手动启动，和经过账号验证的原生定时。没有假定供应商公开了任意的会话唤醒 API。 |
| 多 Agent 任务图 | 不可变的计划、revision / hash、批准一次、依赖门控、必需步骤失败时向后传播。工作台支持并行、顺序、并行后汇总；API 可以表达一般的 DAG。 |
| 手机操作 | 建立和编辑连接、授予使用权、轮换 / 撤销 token、下载接入说明；查看进度、回答问题、下载文件、验收或拒绝结果、核对结果不明的执行。 |
| 自有 Agent Host | Codex app-server、官方 Cursor ACP SDK、独立的 Personal Agent profile；保存进程 / session / turn 的身份、原生审批、续租、取消和恢复。 |
| 代码成果 | 每个 attempt 用固定基线的 detached Git worktree；回传二进制 patch、新文件的 tar 和 SHA-256 清单。源仓库不会被自动 stash、commit、merge 或 push。 |
| 小规模协作 | 保留私人 / 项目空间、成员权限和独立的设备身份；连接的授予权，由资源所有者控制，项目管理员不能因此读取私人任务。 |

## 实现结构

原有的 Inbox、Timers、Memory、Permissions、Outbox 和 Docker Runner 都保留。`orchestration/` 是持久的任务和授权账本；`channels/web/agents.py` 提供浏览器和 worker 的接口；`agent_adapters/` 负责和外部系统的协议；工作台和对话工具，都只通过同一本账本，提交或查看任务。

协议和生命周期这类基础，用成熟的组件：官方 MCP SDK 提供无状态的 HTTP 工具传输，官方 ACP Python SDK 负责 Cursor 的 framing/dispatch，psutil 用来跟踪本机的进程身份，和已经看到的后代进程。不需要再引入另一个 Agent 框架，来替换现在的 Harness。

运行依赖新增了 `agent-client-protocol>=1.0,<1.1`、`psutil>=7.2.2,<8`，都已经写进 `uv.lock`。开发依赖补充了 psutil 的类型定义。Codex 协议测试用的是本地 CLI `0.167.0-alpha.4` 生成的 schema，来源和版本见 `tests/fixtures/codex_schema/SOURCE.md`。

## 故障和权限审查

| 审查项 | 这一版怎么处理 |
| --- | --- |
| 领取响应丢失，造成重复开工 | 稳定的 worker nonce、原子领取和持久的 attempt；同一个 nonce 恢复原来的执行，状态不明的占用，禁止另起执行。 |
| webhook 超时后反复唤醒 | 网络调用之前先持久化 wake；结果不明的投递，继续占着资源；由资源所有者来做停止核对。 |
| 已取消但没被领取的唤醒，永久占用 | 单独的“唤醒停止确认”；保留原来的 HTTP 回执和审计，解除占用之后，仍然不会重发原来的计划。 |
| 手动连接挤占自动唤醒的候选 | Grok/wake mode 的筛选，在候选数量限制之前完成。 |
| token 在请求读取期间被轮换 | 修改之前，在数据库事务里重新鉴权；旧连接不能跨授权代际领取新任务。 |
| 轮换之后，本地 Host 无法恢复 | 当前同一个 Agent 的凭据，可以读取旧 attempt 的最小停止证明；旧任务的正文、结果和 lease，仍然受代际限制。更新凭据时，保留本地 journal。 |
| CLI EOF / 被强杀，被误判成完成 | 必须有原生的终态，和进程清理的证据；不确定时，记录 `unknown` / `uncertain`，并停止重放。 |
| 同一个目录并发写入 | Host/profile 和本机 canonical 工作目录锁；Git 独立的 worktree；有未提交改动的源目录，在领取之前就拒绝。 |
| 其他成员借用已有的 CLI 私人环境 | Codex/Cursor 默认只接受资源所有者的请求；显式共享 profile、共享 Agent、空间可见任务这三项同时满足，才接受其他成员。 |
| 上游结果提升执行权限 | 依赖当作不可信的数据；Personal Agent 使用结构化的 taint，peer URL 不会变成 owner 提供的 URL，已有的 allow 规则，也不能消除逐个动作的审批。 |
| 文件 / 结果跨任务泄露 | 范围限定在当前 attempt，和已经验收的直接依赖；不可变的文件、大小和空间配额、hash 核验；结果和文件原子提交。 |
| 密钥通过文件名或回执泄露 | 已知的连接秘密，会从进度和正文里脱敏；文件名或内容里含已知秘密的，拒绝存储。 |
| API 连接器拿不到依赖文件 | OpenAPI 里包含文件下载的 operation，也可以返回 base64 JSON；MCP 和 Bridge 用同样的授权规则。 |

## 验证记录

完整质量门覆盖：Ruff 格式和 lint、200 个源模块的 strict Mypy、Python 3.13 / 3.11 离线回归，以及 OpenAPI 生成、TypeScript、前端 bundle/许可证的一致性，和 159 项 JavaScript 测试。最终的数量，记录在这次 PR 的验证区。

集成测试包含：真实的 TCP HTTP listener 和实际的 Bridge：浏览器配对、建立连接、批准、领取、进度、问答、Bridge 重建、同一个结果的重试、文件和人工验收。还覆盖了真实应用的 HTTP 和 MCP 两代协议，以及真实 Store/HTTP 下的 token 轮换和 Host 停止恢复。

原生协议测试，启动受控的 stdio 子进程，验证 Codex/ACP 的合同、权限回答、取消、进程组和已见后代的清理；Git 测试用真实的临时仓库，验证固定的 worktree、源目录不变、二进制差异和封存的结果。它们没有调用真实供应商的模型，也没有登录账号。

还没有完成 Grok/Muse/Codex/Cursor 的真实账户联调；macOS 分支已经实现并测试过，但没有在真实的 Mac 上验收。当前环境没有完成浏览器截图验收。以上这些，离线测试替代不了。

## 上线步骤

1. 备份整个实例、各空间的数据库、持久卷和私有配置；停掉旧进程。
2. 更新代码，执行 `uv sync --frozen`，用原来的部署方式启动 `uv run pa serve`。数据库会自动从 schema 12 升到 14。
3. 在工作台里，为每个 Grok / Muse / Host，分别建立连接。worker token 存到执行端的私密配置里；服务端的数据库，只保存它的哈希。
4. Grok：在部署环境里设置 Routine URL/key，再把对应的**变量名**填到连接里；安装本项目的工具或 Bridge，并配置原生的 Routine 指令。
5. Muse：先在原生会话里，验证工具调用和领取；只有账号的原生定时确实已经配好了，才选 `native_schedule`。网络可以用现有的 HTTPS，或者能连通的 Tailscale 路径。
6. 先用没有副作用的任务，验证同一个 task ID 的领取、文字 / 小文件的回传和手机验收，再测试取消、断线和 token 轮换之后的恢复。
7. 自有 Host 按接入文档，选 CLI、专用的状态目录和干净的 Git 工作区。不是 Git 的目录，只有显式加了 `--allow-direct-workdir`，才会直接执行。

回退必须恢复升级前的数据库快照，并切回旧代码；0.3.2 会拒绝 schema 14，不能只回退包版本。保持单个服务进程加持久卷的部署方式。

## 明确保留的限制

- Grok/Muse 账号已有的权限、原生工具、套餐和费用，仍然由供应商控制。取消按钮不是供应商级的 kill，也不能证明已经停止扣费。
- worktree 提供的是工作文件的分离，不是操作系统级的安全沙箱；同一个 SaaS 账号下的多个 Bot，也不能当作独立的沙箱。
- 每个 Agent 串行执行；没有实现供应商账号级的共享资源池，或者全局的 SaaS 费用汇总。
- Personal Agent 子任务只执行单次的预算，专用 profile 跨 attempt 的日费用，还没有汇总。
- 结果文件每个空间限 128 MiB；没有自动删除未验收的成果，也没有按保留期自动清理协作账本。
- UI 提供三种常用的任务布局；任意 DAG、同一个 Agent 的多个步骤等高级组合，用 API。

使用和设计详见 [MULTI_AGENT.md](MULTI_AGENT.md)，具体的命令、Grok/Muse 的提示模板和 Host 的恢复，详见 [CLOUD_AGENTS.md](CLOUD_AGENTS.md)。
