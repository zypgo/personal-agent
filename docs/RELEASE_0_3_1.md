# 0.3.1 发布与验证记录

日期：2026-11-17。基于 V3 主分支 `89ce88cb155a262ba3b30f6c4e632df639d7d42b`。

## 改了什么

- V3 API 用 FastAPI 和严格的 Pydantic DTO，提供经过鉴权的 OpenAPI；读取 body 之前和之后的授权检查，都保留了。
- 浏览器用 TanStack Query Core、markdown-it、TypeScript/Vite；带生成的类型、固定的 bundle、漂移检查和第三方许可证。
- 网页正文用本地的 Trafilatura；基础读取器保留；限制输入大小和并发，避开表格跨行跨列造成的膨胀，禁止依赖库的日志输出正文，并清理缓存。
- Runner 默认用官方的 Docker SDK，CLI 后端保留；修了“启动响应不确定时，可能重复启动”的风险，收据、资源归属和取消核对保持不变。
- restic 加密归档，接在已经校验过的一致快照后面；恢复仍然会撤权并冻结旧的工作，保留策略默认只预览。
- Railway 镜像带着固定 digest 的 restic 和 OpenSSH，运行不需要 Node；没有新增数据库 schema 或云资源。

实现和操作步骤：[OPEN_SOURCE_ADAPTERS.md](OPEN_SOURCE_ADAPTERS.md)。

## 实际验证

| 验证 | 结果 |
| --- | --- |
| Python 3.13.5，完整质量门，4 workers | 5,656 passed，50 skipped |
| Python 3.11.18，完整兼容检查，2 workers | 5,656 passed，50 skipped |
| Ruff 格式与 lint | 通过 |
| mypy strict | 175 个源文件通过 |
| 运行时契约 → OpenAPI → TypeScript | 一致 |
| Vite bundle 与许可证重新生成 | 逐字节一致 |
| 浏览器 JavaScript 测试 | 123 passed |
| restic 0.19.1 真实本地回环 | 初始化、加密备份、列表、全量校验、恢复撤权、保留策略，都通过 |
| wheel / sdist | 构建成功；包含新增的模块、静态产物、源码构建输入和许可证 |
| 新增变更的 gitleaks 扫描 | 没发现密钥 |
| 实际启动 HTTP 服务端 | `/healthz`、页面和两份 bundle 返回 200；配对、V3 鉴权、OpenAPI 0.3.1、no-store、退出撤权，都通过 |

50 项跳过的，是需要真实付费模型或 macOS 工具的用例。这个环境里没有模型 API 密钥，所以没有做真实的模型调用；也没有 Docker daemon，所以容器参数，是用官方 SDK 序列化来验证的，执行和异常路径用的是协议替身，没有声称做过真实 Docker 容器的验收。

第一次用较高并行度检查时，出现过 pytest worker 异常退出，没有断言错误，具体的退出原因，没完全查清。降低并行数之后，同样的用例全部通过；`PA_TEST_WORKERS` 只调整并行数，不会屏蔽用例。小样本的生命周期诊断确认：FastAPI 有界的 callable 缓存，会持有 endpoint 所属的实例，和测试里的 `gc.freeze()` 有交互；没有观察到路由或响应的 TypeAdapter 无限累积。不同版本的完整检查，要一个接一个地运行。

## 运行与部署的边界

网页和 API 能启动，不代表模型密钥或渠道凭据已经配好。用已有的模型服务、Telegram/钉钉，或者你自己的 Docker 节点时，按对应的文档，检查真实的连接。

这份记录，不能当作 Railway 已经部署成功的证明。已有的服务，要实际核对 main 的构建、持久卷、健康检查和网页登录；没有确认线上状态之前，不要标成线上可用。

Railway 上，服务进程是 PID 1，而 `backup-all` 又要求引擎停止，所以这个版本没有在线的整实例快照。正常部署，优先用已有的卷备份，restic 用于已经取得的离线副本，或已经生成好的一致快照。管理员的恢复入口，也要求当前配置有效，详见 [RAILWAY.md](RAILWAY.md)。

新的提交用的是 noreply 作者信息，发布之前，提交元数据和变更内容，都单独核对过；第三方许可证里上游作者的署名，保留。
