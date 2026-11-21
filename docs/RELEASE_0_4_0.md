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

## 上线步骤

1. 备份整个实例、各空间的数据库、持久卷和私有配置；停掉旧进程。
2. 更新代码，执行 `uv sync --frozen`，用原来的部署方式启动 `uv run pa serve`。数据库会自动从 schema 12 升到 14。
3. 在工作台里，为每个 Grok / Muse / Host，分别建立连接。worker token 存到执行端的私密配置里；服务端的数据库，只保存它的哈希。
4. Grok：在部署环境里设置 Routine URL/key，再把对应的**变量名**填到连接里；安装本项目的工具或 Bridge，并配置原生的 Routine 指令。
5. Muse：先在原生会话里，验证工具调用和领取；只有账号的原生定时确实已经配好了，才选 `native_schedule`。网络可以用现有的 HTTPS，或者能连通的 Tailscale 路径。
6. 先用没有副作用的任务，验证同一个 task ID 的领取、文字 / 小文件的回传和手机验收，再测试取消、断线和 token 轮换之后的恢复。
7. 自有 Host 按接入文档，选 CLI、专用的状态目录和干净的 Git 工作区。不是 Git 的目录，只有显式加了 `--allow-direct-workdir`，才会直接执行。

回退必须恢复升级前的数据库快照，并切回旧代码；0.3.2 会拒绝 schema 14，不能只回退包版本。保持单个服务进程加持久卷的部署方式。

## 详细文档

使用和设计详见 [MULTI_AGENT.md](MULTI_AGENT.md)，具体的命令、Grok/Muse 的提示模板和 Host 的恢复，详见 [CLOUD_AGENTS.md](CLOUD_AGENTS.md)。
