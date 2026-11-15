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
| 4a | 渠道基础与 Telegram：配对（只能在本机批准）、对话历史、聊天内命令与审批按钮、语音转文字、图片、实时事件 | ✅ |
| 4b | Web 页面（PWA，手机可用）、Webhook（签名校验）、插件提供的渠道、真实模型验证套件 | ✅ |
| 5 | 对着 Anthropic 的 always-on 文章补齐记忆：每次唤醒一段新对话（带着任务进度索引与"已经告诉过你什么"）、压缩、`search_history`、每夜"做梦"整理记忆（可撤销、有花费上限） | ✅ |
| 6 | 评测（`pa eval`：场景文件、先验证判分本身、再用真实模型多次试验）、launchd 常驻服务（`pa service`）、加固与最终验证 | ✅ |

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

在手机上用它（Telegram，不需要公网地址）：

```bash
# 1. 在 Telegram 里找 @BotFather 创建机器人，把令牌填进 ~/.personal-agent/.env 的 TELEGRAM_BOT_TOKEN（不要贴进聊天）
# 2. 在 ~/.personal-agent/config.toml 里写  [telegram]  enabled = true   （连不上 Telegram 的地区再加 proxy）
uv run pa channels test telegram       # 只问机器人"你是谁"，确认令牌与网络没问题
uv run pa serve                        # 常驻；给机器人发一句话，它会回一个配对码
uv run pa channels approve telegram ABCD-EFGH   # 只在你自己的电脑上批准；陌生人的消息永远不会进到 agent
```

之后手机上直接聊天即可：文字、语音（需要 `brew install ffmpeg` 才能处理 Telegram 的 Ogg 语音）和图片都行；`/status`、`/approvals` 查看状态，需要你点头的动作会带 ✅/❌ 按钮；它主动找你的消息会发到你最近用的那个聊天 App。

在手机或任意浏览器上用它（网页，一个不用构建的 PWA）：

```bash
# 1. ~/.personal-agent/config.toml 里写  [web]  enabled = true
uv run pa serve                        # 常驻（网页默认只监听 127.0.0.1:8765）
uv run pa web pair --label phone       # 打印一次性配对链接（只有这台电脑能发出；10 分钟有效、只能用一次）
uv run pa web open                     # 在这台电脑上直接打开并自动配对
uv run pa web sessions                 # 已配对的浏览器；pa web revoke <id> 收回某一个
```

要让手机访问，最稳妥的办法是 [Tailscale](https://tailscale.com)：`tailscale serve --bg 8765` 会给这台电脑一个只有你的设备能访问的 `https://<电脑名>.<tailnet>.ts.net` 地址；把它写进 `[web] public_url`，`pa web pair` 打印的链接就会用它。手机 Safari 打开链接 →"添加到主屏幕"即可当 App 用。配对令牌放在地址的 `#` 之后（不会发给任何服务器、也不进日志），换成 HttpOnly 的 SameSite=Strict cookie 后立即从地址栏清掉。想直接监听别的网卡（`host = "0.0.0.0"`）？`pa doctor` 会指出风险：网页只有配对过的浏览器能进，但没有 HTTPS 就不该离开你信任的网络。

> 目前网页是"你打开它就能看到"（SSE 实时推送），**没有系统推送通知**；需要它主动叫你的话，同时开着 Telegram。iOS 的"主屏幕 App"与 Safari 不共享 cookie，所以要在主屏幕 App 里再配对一次。

让别的程序叫醒它（Webhook，例如 GitHub、CI、定时脚本）：

```toml
# config.toml
[webhooks]
enabled = true                       # 只监听 127.0.0.1:8766；要给外部服务用，用 tailscale funnel / 反向代理转发这一个端口

[webhooks.routes.github]
scheme = "github"                    # github | gitlab | standard（Standard Webhooks / Svix）| generic
secret_env = "GITHUB_WEBHOOK_SECRET" # 密钥放 .env，这里只写变量名
events = ["push", "pull_request"]    # 只放行这些事件，其余直接丢弃
prompt = "只告诉我失败的检查和需要我评审的 PR；没有就别打扰我。"
```

```bash
uv run pa webhooks secret github     # 生成一把新密钥，并告诉你该往 .env 的哪一行填（不写文件）
uv run pa webhooks                   # 各路由的地址、密钥是否就绪、最近投递/拒绝、监听端口是否可用
uv run pa webhooks send github --event push --body '{"ref":"refs/heads/main"}'   # 用真实密钥签名，发一次测试投递
```

收到的内容一律当作**不可信数据**（放进围栏里，`prompt` 是唯一的可信指令），agent 以最小权限的 `untrusted` 画像运行（只能看、只能告诉你，不能动手）。没有这条路由、密钥没配好、签名错误或已过期的请求，一律回同样的 404，不泄露路由是否存在，原因只写进日志（`pa webhooks` 会显示最近一次）；过大回 413、超速回 429，不在白名单里的事件和重复投递回 202（收到了，不处理）。`generic` 方案的签名自己算也很简单：

```bash
BODY='{"event":"deploy","status":"failed"}'; TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')
curl -X POST http://127.0.0.1:8766/hook/ROUTE -H "X-Webhook-Timestamp: $TS" \
     -H "X-Webhook-Signature: sha256=$SIG" -d "$BODY"
```

它怎么"记事"（按 Anthropic 的 always-on 文章）：agent 大部分时间在睡觉，每次醒来是一段**新的对话**——定时任务和 webhook 不会把一年的早晨堆进同一个上下文。它带着的东西来自文件：你的偏好、记忆索引、每个跨好几次唤醒的任务一份**进度文件**（只把"未完成任务"的一行索引放进上下文）、以及最近**已经发给你的消息**（免得每天早上把同一件事再说一遍）。对话太长时先压缩成摘要；更早说过的话用 `search_history` 查（原文永远在日志里）。每天夜里（默认 03:30，电脑睡着就醒来补）有一次"做梦"：回看新的对话记录，整理记忆、改正过时的笔记，只在有至少 20 条新记录时才跑，花费封顶 $0.30，不给它发消息、联网、动文件的权力，也不让它改你的偏好；删得太多会被整次撤销。

```bash
uv run pa dream                        # 开没开、几点、等着多少条新记录、上次做了什么
uv run pa dream log                    # 最近几次：它说了什么、改了哪些笔记
uv run pa dream run --dry-run          # 只打印它会读到的内容，不调用模型
uv run pa dream run                    # 现在就做一次
uv run pa memory history /memories/state/MEMORY.md   # 谁、何时改了这个文件
uv run pa memory restore 42            # 把某次改动撤销（版本号来自 history 或 dream log）
```

让它一直在后台（macOS，launchd）。文章里的 always-on agent 要活过它被启动的那个终端、活过一次登录、活过自己的崩溃：

```bash
uv run pa service install --dry-run    # 先看它会写什么：一个 LaunchAgent，不要 root，任务文件里没有任何密钥
uv run pa service install              # 登录后自动启动，异常退出后自动拉起；--keep-awake：接电源时不让 Mac 睡着
uv run pa service status               # 状态：加载了没有、在不在跑、跑了几次、上次怎么退出的（直接 pa service 也一样）
uv run pa service logs                 # 它自己的日志（每一行先去掉密钥）；-f 持续跟随；--launchd 看日志打开之前的崩溃
uv run pa service restart              # 等进行中的任务做完再重启（launchd 有 30 秒的重启节流，可能要等一会儿）
uv run pa service uninstall            # 停止并删掉任务文件；你的数据与日志都还在
```

两条要先知道的事：① macOS 不允许后台任务读「桌面 / 文稿 / 下载 / iCloud」文件夹（不弹窗，只是"Operation not permitted"），所以解释器或代码放在这些文件夹里时 `install` 会醒目地警告（任务能装上，但起不来）——把项目放到别处（比如 `~/Developer`），或在项目目录里 `uv tool install .` 装一份再从那份里 `pa service install`；② 后台任务不继承终端的环境变量，需要代理就写 `--pass-env HTTPS_PROXY`（名字像密钥的、或值里带着密钥 / 口令的——比如 `http://user:口令@代理`——一律拒绝：这类内容只放 `.env`，守护进程自己会读）。`pa doctor` 会指出任务没加载、指向的程序已不存在、或被 macOS 挡在门外。

检查它的行为（`pa eval`；做法来自 Anthropic 的 "Demystifying evals"：评**结果**，不评措辞）：

```bash
uv run pa eval list                    # 内置 6 个场景，加上你放在 ~/.personal-agent/evals/*.yaml 的
uv run pa eval show quiet-when-nothing-new   # 一个场景做什么、对它有什么要求（用大白话）
uv run pa eval run                     # 不联网不花钱：先验证"判分本身"抓得出毛病——好脚本全部通过，每个坏脚本都让它该失败的检查失败
uv run pa eval run --live --trials 3 --max-cost 1.0 --model gpt-6.1-sol --effort medium
                                       # 用真实模型把每个场景各跑 3 次；累计花到上限就停；报告里有通过率、花费、"安静的步骤里它发了几条消息"
```

一个场景是一页 YAML：`seed`（它已经知道什么：偏好、记忆、任务进度、已经发过的消息、配置）、`steps`（来了什么：你的消息 / 一个定时任务 / 一次 webhook）、`checks`（之后必须成立的事：说没说、说了几条、用了哪些工具、记忆里写了什么、你写的偏好有没有被动过、花了多少）。检查从不比对措辞，因为同一句话模型有很多种说法。每个步骤还有一条隐含检查：它的 run 必须以允许的方式结束——一个崩溃的 run 什么也没说，不能被当成"选择安静"而通过。内置场景覆盖文章里的几件事：同一条新闻只说一次、没有新东西就不打扰、源读不到不能当成"一切正常"、跨几次唤醒的任务进度、你的偏好被遵守、webhook 里夹带的指令只是数据。报告（JSON + Markdown）写在 `~/.personal-agent/evals/reports/`（0600，密钥已去掉）。

想先试用而不动你的主目录：`PA_HOME=./.pa-home uv run pa init`（`.pa-home/` 已被 git 忽略）。

## 开发环境

```bash
uv sync --all-extras          # Python 3.13 + 全部依赖
uv run pytest                 # 测试（不需要 API key，模型由脚本化的 ScriptedModel 代替）
node --test tests/js/*.test.mjs   # 网页前端的纯逻辑测试
uv run pa eval run            # 内置行为场景的"判分自检"（不联网）；--live 才用真实模型
PA_LIVE=1 uv run pytest tests/live   # 用真实 OPENAI_API_KEY 的端到端验证（会花钱；key 只从环境读）
uv run ruff check src tests && uv run ruff format --check src tests
uv run pa --version
scripts/fetch_references.sh   # 重新拉取只读参考项目到 references/
```

## 安全约定

- 密钥只放 `.env`（已 git-ignore），**不要贴进聊天或提交**。
- 个人数据（记忆、会话、数据库）默认在 `~/.personal-agent`，不在仓库内。
- 遥测与 OpenAI 托管 tracing 默认关闭。
