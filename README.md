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
| 1 | 内核：SQLite 收件箱 / 会话日志 / 发件箱 / 记忆 + Harness + CLI 对话 | 🚧 |
| 2 | 常驻循环：worker / scheduler / dispatcher、策略引擎、延迟审批、预算 | 🚧 |
| 3 | 可插拔：MCP、Skills、沙箱、密钥代理、内置工具 | 🚧 |
| 4 | 渠道与多模态：Web PWA（手机可用）、Telegram、Webhook、语音/图像 | 🚧 |
| 5 | 记忆智能：做梦、检索、压缩、子代理 | 🚧 |
| 6 | 评测、launchd 常驻服务、加固 | 🚧 |

## 开发环境

```bash
uv sync --all-extras          # Python 3.13 + 全部依赖
uv run pytest                 # 测试（不需要 API key）
uv run pa --version
scripts/fetch_references.sh   # 重新拉取只读参考项目到 references/
```

## 安全约定

- 密钥只放 `.env`（已 git-ignore），**不要贴进聊天或提交**。
- 个人数据（记忆、会话、数据库）默认在 `~/.personal-agent`，不在仓库内。
- 遥测与 OpenAI 托管 tracing 默认关闭。
