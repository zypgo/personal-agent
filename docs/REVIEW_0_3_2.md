# 0.3.2：代码审查与修复

这次顺着真实的使用路径检查了一遍：模型和工具调用、会话和记忆、Inbox/Outbox、定时任务、浏览器配对、多人权限、审批、SSE、前端请求和离线缓存、自有 Runner、正文提取、备份恢复。服务端、自有执行节点、SQLite 和已有的模型、渠道接口都保持不变，没有增加云资源，也没有数据库迁移。

## 修复的问题

| 优先级 | 原来的问题和影响 | 怎么修的，回归证据 |
| --- | --- | --- |
| P1 | 身份恢复只撤销了会话和 V3 邀请，旧的 `pa web pair` 链接仍然能重新登录。 | 恢复事务会同时作废目标身份、或签发者相关的 `web_invites`；别人的邀请仍然有效。`tests/test_v3_recovery.py`。 |
| P1 | SSE 批量发送，只在批次开始时检查权限。慢客户端在两帧之间被撤权，仍然能收到后面的私有内容；被移除再重新加入，旧连接也可能继续用。 | 每一帧都检查会话、身份，以及连接建立时的成员版本；一旦失效，就发 `signout` 并释放订阅。覆盖 hello、历史、活动、草稿和审批。`tests/test_web_stream_authorization.py`。 |
| P1 | 旧的聊天审批，在读取请求体期间被移除、又以 owner 重新加入之后，仍然可能生效。 | 审批请求绑定原来的身份和成员版本，读取完再复核；旧请求不能借用新的授权。`tests/test_v3_authorization_boundaries.py`。 |
| P1 | Service Worker 只排除了字面上的 `/api/`，`/%61pi/history` 这类编码过的路径，可能把已经鉴权的响应存进离线缓存。 | 只缓存明确列出的公开页面和静态文件；未知的、私有的、编码过的路径，一律走网络。静态内容的摘要变了，缓存版本也跟着变，激活时删除旧缓存。`tests/js/sw.test.mjs`。 |
| P2 | Outbox 提前把整个批次标成 `sending`；发第一条时进程中断，后面从没发过的消息，也被恢复成 `maybe_sent`，不再投递。 | 每次只领取马上要发的一条，批次的工作上限保留；还没发的条目，继续是 pending。`tests/test_dispatcher.py`。 |
| P2 | 必须送达的消息，重试用完之后，数据库里已经是 failed，报告里却还写着 retrying。 | 按持久化之后返回的最终状态，来分类报告。`tests/test_dispatcher.py`。 |
| P2 | 最近一轮的工具交换，超过会话窗口时，会从工具结果的中间截断；SDK 会清掉孤立的结果，已经执行的动作的上下文就丢了。 | 最新的一轮，即使超过条目上限，也保留完整的 user/tool 交换，不会把截断当成新的历史。`tests/test_sessions.py`。 |
| P2 | 一次性的 `schedule(at=...)`，遇到没有偏移的时间，会忽略 `timezone`，跨时区的提醒会错几个小时。 | 没有偏移的时间，用显式指定的时区，没指定就用 agent 的时区；时间字符串自己带偏移的，以它为准。`tests/test_schedule_tool.py`。 |
| P2 | Docker SDK 的关闭等待，被取消或超时之后，client 可能永远不会关闭。 | 在同一个执行线程里，提前排好最终的关闭；等待方退出，不会取消关闭；重复调用可以等待同一个结果。`tests/test_v3_docker_sdk.py`。 |
| P2 | 已经失去当前项目权限的人，空间路由会阻止他自己退出登录。 | 登出按账户会话处理，不依赖所选的空间，仍然保留同源和 CSRF 防护。`tests/test_v3_collaboration_http.py`。 |
| P2 | 还有其他 owner 的时候，自己退出项目已经成功写入，但随后读取成员列表时没有权限，接口返回 403，页面误报失败。 | 返回成功的、空的成员列表；工作台立即清空旧项目的内容，切换到可以访问的空间。`tests/test_v3_collaboration_http.py`、`tests/js/workbench.test.mjs`。 |
| P2 | HTTP 200 的截断 JSON，或者代理返回的 HTML 页面，被请求层转成 null，当成了成功，写操作可能因此被错误地确认。 | 明确报告“响应不完整”；写入仍然只发送一次，不自动重试；401/403 仍然立即清理权限状态，204/205 的空响应仍然有效。`tests/js/query.test.mjs`。 |
| P2 | Live 测试忽略了已经注入的环境密钥，只查固定的 `.env`；没有这个文件时，整套测试都被跳过，容易误以为真实验证已经完成。 | 环境变量优先，文件只作为可选的回退；明确开启 live 却缺少密钥，会失败。密钥不会复制到文件里。`tests/test_live_credentials.py`。 |

## 复现

```bash
uv sync --frozen
npm ci
scripts/check.sh

# OPENAI_API_KEY 由终端或私有运行环境注入，不放入命令参数或仓库。
PA_LIVE=1 uv run pytest tests/live -q

# 仅验证真实 Web 进程、模型、重启恢复。
PA_LIVE=1 uv run pytest tests/live/test_live_web.py -q

# 可选：对已校验的自有 restic 二进制做真实本地验收。
PA_TEST_RESTIC_BINARY=/trusted/bin/restic uv run pytest tests/test_v3_restic.py -q
```

默认的离线质量门，不会调用付费模型。Live 测试要显式开启才会运行，并且继续受每次运行和场景预算的约束。0.3.2 没有数据库 schema 升级；更新代码和已锁定的依赖，重启已有的服务就可以。
