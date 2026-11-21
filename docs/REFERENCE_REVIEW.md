# 参考项目复核（2026-11-16）

改 Personal Agent 之前，我们读了三个外部项目。Personal Agent 里的所有代码，都是独立写的，没有从这些项目里引入任何代码。Personal Agent 的对比基线是 `215358b6ce2b6e66315552ecbd156f263234aa8b`。

分支快照、发布标签和重建版本，是三种不同的证据。所以下面的表格，把每个项目到底读了什么，写得很明确。

## 我们看了什么

| 参考项目 | 发布版本 | 我们读了什么 |
| --- | --- | --- |
| OpenClaw | 稳定版 `v2026.11.4`，2026-11-16 发布；发布提交 `d843d6af39a42cd3c30084edbeabeece732fd93a` | 一份更新的 `main` 快照，[`6f5d3b399b31855501cc3f38f1221f7165554c59`](https://github.com/openclaw/openclaw/tree/6f5d3b399b31855501cc3f38f1221f7165554c59)。下面的结论，说的是这份快照，不自动适用于稳定版。 |
| DeepSeek Harness | 看到的最新发布：`dsh-v0.2.2-rc.3`（2026-11-13），标为预发布 | `master` 分支，在 [`39163dd30f703a79079e2d2446545cff3c453738`](https://github.com/deepseek-ai/deepseek-harness/tree/39163dd30f703a79079e2d2446545cff3c453738)。这个项目自称是开发者预览版。 |
| Grok Bot 重建版 | 对已发布的 `0.18.0` 的非官方重建。原仓库已于 2026-08-28 归档。 | [`b-nnett/grok-bot-0.18-reconstructed@a9f633e09d49a85829b8236331b9e21f7e612634`](https://github.com/b-nnett/grok-bot-0.18-reconstructed/tree/a9f633e09d49a85829b8236331b9e21f7e612634)。它不是 xAI 的官方发布，也不是完整的原始源码。 |

发布来源：[OpenClaw 发布页](https://github.com/openclaw/openclaw/releases/tag/v2026.11.4)、[DeepSeek 发布页](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.3)。

## 对比说明了什么

| 方面 | 参考项目做得好的地方 | 对 Personal Agent 意味着什么 |
| --- | --- | --- |
| 替换运行时和服务商 | OpenClaw 注册了多个运行时，并从中选一个。DeepSeek 通过 Cordis，注册 LLM 适配器、agent 工厂、持久化，和许多别的服务。 | 清晰的服务商契约和能力检查，现在就有用。我们有意让 SDK runner 和 SQLite 存储紧密耦合。有一个端点 URL，并不等于它是个可以替换的 agent 运行时。 |
| 长时间运行的工作 | OpenClaw 能恢复会话和待发送的投递。DeepSeek 把定时、目标状态和会话日志，存在磁盘上。 | 别的项目也能可靠地存这些，所以这不是只有我们才有的。保留小而清楚的收件箱/发件箱状态机，并在它和每个渠道的衔接处做测试。 |
| “恢复”指什么 | DeepSeek 的调度器，会恢复待处理的收件箱投递。恢复出来的目标，一开始是“未启用”的，要有权限的人来恢复运行。本地、进程内的作业，不会因为有会话历史，就变成持久的。 | 把三件事分开：重放历史、恢复待处理的任务，和不在外部世界重复做一个动作。不要把它们当成同一件事。 |
| 渠道和委派 | OpenClaw 有更多的渠道插件，并支持子 agent 的生命周期。DeepSeek 提供多种子 agent 服务商、SDK/ACP 集成，以及更丰富的桌面/Web 配置。 | 声明出来的渠道能力契约，让我们能逐步加渠道。这个 PR 没有新增分布式的多 agent 调度器。 |
| 输出和交互 | Grok 重建出来的协议，把文件附件和其他有类型的输出，写得很明确。DeepSeek 和 OpenClaw 暴露了更多运行时和界面的状态。 | 加上真正的文件交付，和有限的、临时的流式输出。保存下来的最终消息，仍然是正式的历史。 |
| 隔离 | DeepSeek 在插件作用域之上，还有操作系统级的沙箱实现。OpenClaw 更大的插件面，意味着更多的信任和运维边界。 | 插件的生命周期、作用域和类型声明，本身不隔离进程。沙箱、审批和出站检查继续用。 |
| 采用的成本 | 更大的框架，有更多的扩展点和集成，也有更多要学的配置和恢复路径。DeepSeek 警告，预览期间会有破坏性变更。 | 先围绕已有的东西，做小的接口。整个换成某个框架，需要它自己的兼容性和恢复测试。 |

### OpenClaw：值得读的文件

- [运行时注册表](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/agents/harness/registry.ts)和[运行时选择](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/agents/harness/selection.ts)。
- [会话投递恢复](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/infra/session-delivery-queue-recovery.ts)和 [SQLite 出站队列](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/infra/delivery-queue-sqlite.ts)。
- [渠道目录注册表](https://github.com/openclaw/openclaw/blob/6f5d3b399b31855501cc3f38f1221f7165554c59/src/plugins/channel-catalog-registry.ts)。

### DeepSeek：值得读的文件，和它的局限

- [LLM 服务](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/llm/llm/src/index.ts)、[agent 服务](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/core/agent/src/index.ts)和[架构文档](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/docs/architecture.md)。
- [持久化](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/session/session-persistence/src/index.ts)、[定时恢复](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/schedule/schedule/src/runtime.ts)和[目标激活](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/goal/goal/src/index.ts)。
- [实验性的 webhook 文档](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/docs/subsystems/webhook.md)，描述的投递模型，和持久的收件箱不一样：不自动去重，也不重放。
- Cordis 的可逆副作用，清理的是注册、监听器和生命周期资源。它撤销不了已经发出的网络请求，也撤销不了任意的文件写入。agent 和插件的作用域，不是操作系统沙箱。见[安全声明](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/SAFETY.md)。

## Grok 的代码从哪里来，是哪个版本

[README](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/README.md) 把项目分成三部分：重建出来的运行时和控制面代码、原样保留的已发布渲染器，以及一部分能读懂的前端。它明确说了，已发布的前端，没有带原始源码或 source map。所以，把这个仓库说成是“从 source map 完整还原”，是不对的。

它的 [NOTICE](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/NOTICE.md) 没有授予上游源码的许可证。我们只借鉴了架构上的想法，和能观察到的协议边界。我们没有复制或运行任何重建出来的源码、渲染器资源、安装程序、账号 cookie 或认证传输。

这个重建项目，定义了文件作为输出怎么返回，并且在一次发送被接受的时候，存一条记录：[send-message 的 schema](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/source/host/runner/tools/send-message-schema.ts)、[接受记录](https://github.com/b-nnett/grok-bot-0.18-reconstructed/blob/a9f633e09d49a85829b8236331b9e21f7e612634/source/host/extensions/transcript/send-acceptance.ts)。

面向 Cursor/Claude Code/Codex/OpenRouter 的推理路由、被路由的 MCP 工具、本地用量跟踪、可选的本地 Docker 连接器，和路由设置界面，作者自己说是后来的实验。它们不能证明，官方的 Grok Bot 0.18 发布时带有这些功能。

我们还查了两个后续的仓库：

| 仓库 | 固定的 commit | 我们的发现 |
| --- | --- | --- |
| `ElizantOS/ant-bots` | [`1378374adf12e10cb4871c0d2bdac3343049ad6e`](https://github.com/ElizantOS/ant-bots/tree/1378374adf12e10cb4871c0d2bdac3343049ad6e) | 仍然建立在 0.18 的重建版上。它后来的动静，没有显示出有新的上游重建。 |
| `smartyua/Grok-Bot-reconstruction-` | [`2beb2f9409ed507385c09753d33977591aa59963`](https://github.com/smartyua/Grok-Bot-reconstruction-/tree/2beb2f9409ed507385c09753d33977591aa59963) | 它提到的 0.43，是拿一个已安装的应用做对比。重建的基础，仍然是 0.18。 |

我们没有找到更新的重建，能和已核实的更新的已发布版本对上。

## 我们采纳了什么，留到以后的是什么

这个 PR 采纳了其中有用的边界，用在现有的 Python 系统上：

- 明确的服务商和渠道能力；
- 单独的语音设置；
- 流式的进度，和已提交的历史分开；
- 通过现有的发件箱交付文件，文件创建之后不能改，下载需要认证。

[产物约定](ARTIFACTS.md)写了它的限制、文件保留多久，以及投递结果不确定的时候怎么办。

以后可能做的：验收义务（来自 Grok 的想法）、持久的后台作业状态、结构化的提问，和优先级通道。这些，这里都没有实现。
