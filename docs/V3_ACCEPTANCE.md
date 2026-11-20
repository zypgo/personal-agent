# V3 验证记录

日期：2026-11-17。产品迭代 V3.0，包版本 0.3.0；基线是 `main@2a357130cd91fe4bb5487fbf73c3f5ef17975e8a`。这份文档把“已经自动验证通过的”，和“上线前要在你自己的环境里验收的”，分开写。

## 已通过

| 检查 | 结果 |
| --- | --- |
| `scripts/check.sh`，Python 3.13.5 | Ruff 格式 / lint、mypy strict、5427 项 Python 测试通过，50 项按环境跳过 |
| `scripts/check.sh --python 3.11`，Python 3.11.18 | 5427 项通过，50 项按环境跳过 |
| Node 前端测试 | 109 项通过，运行实际的渲染器和表单逻辑，DOM 用的是测试替身 |
| `uv build` | wheel 和 sdist 构建成功 |

50 项跳过，是 27 项真实付费模型测试，加 23 项 macOS 专属测试。测试环境装了官方 gitleaks，相关的发布保护测试，实际运行了。没有靠放宽版本约束、关掉权限断言或跳过失败的测试，来取得这个结果。

## 故障与权限的证据

| 约束 | 主要的测试位置 |
| --- | --- |
| 私人 / 项目隔离、角色与邀请、授权版本 | `test_collaboration_identity.py`、`test_v3_collaboration_http.py` |
| 工具、压缩、媒体、定时器、发送后记忆更新的撤权边界 | `test_v3_authorization_boundaries.py` |
| 渠道账号范围、固定回送、绑定撤销、共同 owner 审批 | `test_v3_channel_identity.py`、`test_v3_channel_controls.py` |
| 丢失 ACK、重复结果、旧 generation、取消、uncertain、清理 | `test_execution_v3.py`、`test_v3_execution_http.py` |
| Runner 收据、限额、路径 / 链接拒绝、低磁盘、重启核对 | `test_v3_nodes.py` |
| 真正 ASGI 与 HTTP transport 的 Runner 回报闭环，8 MiB 输入 / 输出 | `test_v3_execution_http.py` |
| 子空间 Worker 停止后才释放锁、异常取消恢复、并发 / 预算 | `test_v3_runtime.py` |
| 跨空间快照、完整性检查、拒绝递归目标、恢复后作废旧授权 | `test_v3_backup.py` |
| 管理员为原成员恢复设备入口、不创建身份或提升角色 | `test_v3_member_reentry_cli.py` |
| 前端身份回执、任务表单、只读角色、日志、修订与问题回答 | `tests/js/store.test.mjs`、`tests/js/workbench.test.mjs` |

以上节点端到端测试，用的是注入的执行后端；它们覆盖了真实的 HTTP / 数据库协议，但不代表真的跑了 Docker 容器。这次更新了受函数签名变动影响的 mutation anchor，并通过了所有 mutation 列表的完整性检查；没有重新运行全部历史的 mutation campaign。0.3.0 新增保护代码的专门变异清单和结果，见下一节。

## 回收时补充的变异验证

2026-11-17，本机 PR 回收流程。之前没有覆盖 0.3.0 新增保护代码的变异清单；新写了 `scripts/mutation/lists/v03_collaboration_execution.py`（202 条），每条故意改坏一处保护代码，要求对应的测试变红。覆盖：邀请和恢复码的一次性、授权版本和成员 / 设备 / 渠道绑定；持久任务的审批人、撤权、取消、修订和代际栅栏；Runner 计划和路径校验（`..`、反斜杠、控制字符、凭据目录、作业目录自身、链接）；Docker 后端的权限和资源限额；节点邀请；Web 路由的角色和审批人检查；渠道固定回送；服务端启动校验；全实例恢复之后的授权栅栏；工具守卫的撤权检查。

第一轮对 215 条草稿运行：82 条被抓住，**133 条存活**。逐条判断之后：

- 15 条在逻辑上等价：要么有另一道检查，会用不同的报错文字，拒绝同一个请求；要么调用方根本构造不出那种状态，任何测试都区分不了。它们没有收录，原因逐条写在清单文件的开头。
- 其余 118 条，每一条都是当时没有测试把守的保护。为它们补了 78 个测试函数（参数化之后 139 项，分在 11 个新文件里）：`test_v3_plan_validation.py`、`test_v3_execution_fences.py`、`test_v3_docker_backend.py`、`test_v3_node_invitations.py`、`test_v3_identity_pins.py`、`test_v3_http_boundaries.py`、`test_v3_channel_routing.py`、`test_v3_guard_revocation.py`、`test_v3_restore_fence.py`、`test_v3_control_plane.py`、`test_v3_space_config.py`。恢复码的缺陷（见 [COLLABORATION.md](COLLABORATION.md)），另外还有 `test_v3_recovery.py` 里的 3 个测试。补测试时，有 1 条变异拆成了 3 条更细的变异。

补完测试，整份重跑（分三路，各路 `65 killed, 0 survived`、`71 killed, 0 survived`、`66 killed, 0 survived`），202 条全部被抓住。这只说明这 202 处保护，各有测试把守，不代表清单之外没有缺陷；真实 Docker、手机、Railway 和渠道的验收，仍然见下一节。

最终代码树上的质量门（本机 macOS）：`scripts/check.sh`（Python 3.13.16）的格式、lint、mypy strict、5592 项 Python 测试（28 项跳过）和 109 项 Node 前端测试，全部通过；`scripts/check.sh --python 3.11`（Python 3.11.15）5592 项通过、28 项跳过；`uv build` 构建 wheel 和 sdist 成功，sdist 含 `Dockerfile`、`railway.toml` 和 `.dockerignore`。这 28 项跳过，是 27 项真实付费模型测试，加 1 项需要 GNU `timeout` 的容器截止脚本测试（本机没有安装）；上表里，作者环境中跳过的 23 项 macOS 专属测试，在本机上实际运行了，所以通过的数量，和上表不同。

运行：`uv run python scripts/mutation/run.py scripts/mutation/lists/v03_collaboration_execution.py`；可以按名字前缀（`"identity:"`、`"http:"`、`"files:"` 等）分几路并行，本机分三路，大约 25 分钟。`tests/test_mutation_lists.py` 会在每次普通测试里，检查这些锚点仍然恰好出现一次。

## 上线前要实际验收的

当前的开发环境没有 Docker daemon；本地的浏览器进程也受限。真实的手机布局、Docker 镜像 / 容器、生产 Railway Volume、真实渠道消息或付费模型，都还没有验收。

在已有的 Railway 服务和你自己的电脑上，按 [RAILWAY.md](RAILWAY.md) 和 [NODES.md](NODES.md) 完成下面几步：

1. 备份升级前完整的 home，和部署的环境变量；只启动一个服务进程。
2. 用两个测试身份，分别进入私人空间和共同项目，确认历史、资料、成果、流和审批的边界。
3. 在手机上提交一个带明确输入 / 输出的小任务，资源所有者批准，你自己的 Docker Runner 执行，手机下载成果。
4. 断开 Runner 的网络，或者重启 Runner，恢复同一个 attempt；离线取消保持待确认，结果不明的，不能自动重放。
5. 撤销成员 / 设备 / 节点，核对之后的工具、流、任务领取和通知，都按当前的授权停下来。
6. 用你自己的测试渠道，验证固定回送；不要在多人共享的聊天里，发送配对码或恢复码。
7. 把整个实例的快照，恢复到一个独立的新目录；核对内容、旧凭据失效、管理员 / 原成员重新配对，以及对暂停任务的人工复核。

这些步骤，是合并之后的部署验收条件。这个 PR 不会自动部署，不会创建收费的云沙箱，也不声称单实例高可用，或者对任意外部动作的 exactly-once。
