# V3 验证记录

日期：2026-11-17。产品迭代 V3.0，包版本 0.3.0；基线为 `main@2a357130cd91fe4bb5487fbf73c3f5ef17975e8a`。本文区分已通过的自动验证与上线前需要在用户环境执行的验收。

## 已通过

| 检查 | 结果 |
| --- | --- |
| `scripts/check.sh`，Python 3.13.5 | Ruff 格式/lint、mypy strict、5427 项 Python 测试通过，50 项按环境跳过 |
| `scripts/check.sh --python 3.11`，Python 3.11.18 | 5427 项通过，50 项按环境跳过 |
| Node 前端测试 | 109 项通过，运行实际渲染器与表单逻辑，DOM 使用测试替身 |
| `uv build` | wheel 与 sdist 构建成功 |

50 项跳过由 27 项真实付费模型测试、23 项 macOS 专属测试组成。测试环境安装了官方 gitleaks，相关发布保护测试实际运行。没有通过放宽版本约束、关闭权限断言或跳过失败测试取得结果。

## 故障与权限证据

| 约束 | 主要测试位置 |
| --- | --- |
| 私人/项目隔离、角色与邀请、授权版本 | `test_collaboration_identity.py`、`test_v3_collaboration_http.py` |
| 工具、压缩、媒体、定时器、发送后记忆更新的撤权边界 | `test_v3_authorization_boundaries.py` |
| 渠道账号范围、固定回送、绑定撤销、共同 owner 审批 | `test_v3_channel_identity.py`、`test_v3_channel_controls.py` |
| 丢失 ACK、重复结果、旧 generation、取消、uncertain、清理 | `test_execution_v3.py`、`test_v3_execution_http.py` |
| Runner 收据、限额、路径/链接拒绝、低磁盘、重启核对 | `test_v3_nodes.py` |
| 真正 ASGI 与 HTTP transport 的 Runner 回报闭环，8 MiB 输入/输出 | `test_v3_execution_http.py` |
| 子空间 Worker 停止后才释放锁、异常取消恢复、并发/预算 | `test_v3_runtime.py` |
| 跨空间快照、完整性检查、拒绝递归目标、恢复后作废旧授权 | `test_v3_backup.py` |
| 管理员为原成员恢复设备入口、不创建身份或提升角色 | `test_v3_member_reentry_cli.py` |
| 前端身份回执、任务表单、只读角色、日志、修订与问题回答 | `tests/js/store.test.mjs`、`tests/js/workbench.test.mjs` |

以上节点端到端测试使用注入的执行后端；覆盖真实 HTTP/数据库协议，不代表已运行真实 Docker 容器。此次更新了被函数签名变动影响的 mutation anchor，并通过所有 mutation 列表的完整性检查；没有重新运行全部历史 mutation campaign。

## 上线前实际验收

当前开发环境没有 Docker daemon；本地浏览器进程受限。尚未进行真实手机布局、Docker 镜像/容器、生产 Railway Volume、真实渠道消息或付费模型验收。

在已有 Railway 服务和自有电脑上，按 [RAILWAY.md](RAILWAY.md) 与 [NODES.md](NODES.md) 完成：

1. 备份升级前完整 home 与部署环境变量；只启动一个控制进程。
2. 两个测试身份分别进入私人空间和共同项目，确认历史、资料、成果、流和审批的边界。
3. 手机提交带明确输入/输出的小任务，资源所有者批准，自有 Docker Runner 执行，手机下载成果。
4. 断开 Runner 网络或重启 Runner，恢复同一 attempt；离线取消保持待确认，未知结果不能自动重放。
5. 撤销成员/设备/节点，核对后续工具、流、任务领取和通知都按当前授权停止。
6. 用自己的测试渠道验证固定回送；不在多人共享聊天中发送配对码或恢复码。
7. 将全实例快照恢复到独立新目录；核对内容、旧凭据失效、管理员/原成员重新配对，以及暂停任务的人工复核。

这些步骤是合并后部署验收条件。本 PR 不自动部署、不创建收费云沙箱，也不宣称单实例高可用或任意外部动作 exactly-once。
