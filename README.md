# personal-agent

一个**跑在你自己电脑上的常驻个人 agent**：

- **模型**：OpenAI（一把通用的 `OPENAI_API_KEY`；可选 `OPENAI_BASE_URL` 指向兼容端点）。
- **引擎**：[OpenAI Agents SDK](https://github.com/openai/openai-agents-python)，封装在 `harness/` 端口之后。
- **理念**：Anthropic 最新的 always-on / agent automations / agent memory / Managed Agents / trustworthy agents 指南——事件收件箱、计时器、记忆、权限、发件箱五个原语；大脑 / 双手 / 会话解耦；默认安静、默认安全、默认私密。
- **可插拔**：MCP 服务器、Agent Skills、渠道适配器、沙箱后端、模型端点。

> 调研与取舍：[docs/research/anthropic-guidance.md](docs/research/anthropic-guidance.md) · [docs/research/reference-analysis.md](docs/research/reference-analysis.md)
> 架构：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)（含各能力的实现状态）

## 当前进度

| 迭代 | 内容 | 状态 |
|------|------|------|
| 0 | 调研（Anthropic 指南 + 9 个同类开源项目）、架构设计、脚手架、origin 仓库 | ✅ |
| 1 | 内核：SQLite 收件箱 / 会话日志 / 发件箱 / 记忆 + Harness + CLI 对话 | ✅ |
| 2 | 常驻循环：worker / scheduler / dispatcher、策略引擎、延迟审批、审计、预算、`pa serve` | ✅ |
| 3a | 双手与出站：沙箱 shell（seatbelt）、硬性底线、文件工具、SSRF 安全的 `web_fetch` / `http_request`、托管 `web_search`、密钥代理、"地址会带走数据"的出站控制 | ✅ |
| 3b | 可插拔：MCP（定义先审阅再启用）、Agent Skills（渐进披露）、Python 插件注册表、外部文字清洗 | ✅ |
| 4 | 渠道与多模态：Web PWA（手机可用）、Telegram、Webhook、语音/图像 | 🚧 |
| 5 | 记忆智能：做梦、检索、压缩、子代理 | 🚧 |
| 6 | 评测、launchd 常驻服务、加固 | 🚧 |

## 快速开始

```bash
uv sync --all-extras
uv run pa init                # 创建 ~/.personal-agent（配置、数据库、记忆模板、空的 .env）
# 用编辑器把 OPENAI_API_KEY=sk-... 填进 ~/.personal-agent/.env（不要贴进聊天）
uv run pa doctor              # 检查环境；加 --live 会做两次真实调用（几美分）
uv run pa chat                # 对话；/help 查看命令，Ctrl-D 退出
uv run pa ask "你好"           # 单次提问
uv run pa prefs edit          # 写下你的长期规则；agent 每次运行都会重新读取，且无权修改
uv run pa status              # 后台是否在运行、队列、定时任务、待批准、花费
```

让它在后台替你干活（定时任务、事件）：

```bash
uv run pa serve               # 前台常驻；Ctrl-C 优雅停止。之后另开终端 pa chat 就是在和它对话
uv run pa timers add brief "给我今天的简报" --cron "0 8 * * 1-5"
uv run pa approvals           # agent 想做的、等你点头的事；pa approve / pa deny 回答（聊天里是 /approve /deny）
uv run pa audit               # 它实际做了什么：每次工具调用与策略判定
uv run pa budget              # 今日花费与上限；pa budget extend 2 当天加额度并放行暂停的任务
```

默认模式 `supervised`：读取、记忆、通知放行，任何会改变世界的动作都先问你；想放开某类动作，在 `config.toml` 里写规则，例如 `[policy] allow = ["schedule"]`（让它不用问就能建定时任务）；`deny` 规则任何模式下都不可绕过。

给它更多能力（都是**先看后用**）：

```bash
uv run pa skills                       # 已安装的技能；pa skills install <文件夹 | https 地址> 先审阅再安装
uv run pa mcp add files -- npx -y @modelcontextprotocol/server-filesystem ~/Documents
uv run pa mcp tools files              # 连上去看它有哪些工具、各会被怎样处理
uv run pa mcp trust files              # 读过工具的描述并接受；新增/改动的工具以后会再问你
uv run pa plugins                      # Python 插件（装了也要在 config.toml 的 [plugins] enabled 里列出才生效）
```

想先试用而不动你的主目录：`PA_HOME=./.pa-home uv run pa init`（`.pa-home/` 已被 git 忽略）。

## 开发环境

```bash
uv sync --all-extras          # Python 3.13 + 全部依赖
uv run pytest                 # 测试（不需要 API key，模型由脚本化的 ScriptedModel 代替）
uv run ruff check src tests && uv run ruff format --check src tests
uv run pa --version
scripts/fetch_references.sh   # 重新拉取只读参考项目到 references/
```

## 安全约定

- 密钥只放 `.env`（已 git-ignore），**不要贴进聊天或提交**。
- 个人数据（记忆、会话、数据库）默认在 `~/.personal-agent`，不在仓库内。
- 遥测与 OpenAI 托管 tracing 默认关闭。
