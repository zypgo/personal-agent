# 0.3.1 发布与验证记录

日期：2026-11-17。基于 V3 主分支 `89ce88cb155a262ba3b30f6c4e632df639d7d42b`。

## 变更

- V3 API 使用 FastAPI 和严格 Pydantic DTO，提供经过鉴权的 OpenAPI；保留读取 body 前后的授权检查。
- 浏览器使用 TanStack Query Core、markdown-it、TypeScript/Vite；带生成类型、固定 bundle、漂移检查和第三方许可证。
- 网页正文使用本地 Trafilatura；保留基础读取器，限制输入/并发，避开表格跨度膨胀，禁止依赖日志输出正文并清理缓存。
- Runner 默认使用官方 Docker SDK，保留 CLI 后端；修复启动响应不确定时重复启动的风险，保持收据、资源所有权和取消核对。
- restic 加密归档接在已校验的一致快照之后；恢复继续撤权并冻结旧工作，保留策略默认预览。
- Railway 镜像带固定 digest 的 restic 与 OpenSSH，运行不需要 Node；没有新增数据库 schema 或云资源。

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
| restic 0.19.1 真实本地回环 | 初始化、加密备份、列表、全量校验、恢复撤权、保留策略通过 |
| wheel / sdist | 构建成功；包含新增模块、静态产物、源码构建输入和许可证 |
| 新增变更的 gitleaks 扫描 | 未发现密钥 |
| 实际 HTTP 控制面启动 | `/healthz`、页面与两份 bundle 返回 200；配对、V3 鉴权、OpenAPI 0.3.1、no-store、退出撤权通过 |

50 项跳过为需要真实付费模型或 macOS 工具的用例。本环境没有模型 API 密钥，没有进行真实模型调用；没有 Docker daemon，因此容器参数使用官方 SDK 序列化验证，执行与异常路径使用协议替身，未声称真实 Docker 容器验收。

首次较高并行度检查曾出现 pytest worker 非正常退出，无断言错误，具体退出原因未完全确定。降低并行数后相同用例全部通过；`PA_TEST_WORKERS` 只调整并行数，不屏蔽用例。小样本生命周期诊断确认 FastAPI 的有界 callable 缓存会持有 endpoint 所属实例，与测试的 `gc.freeze()` 有交互，没有观察到路由或响应 TypeAdapter 无界累积。不同版本的完整检查应依次运行。

## 运行与部署边界

网页/API 可启动不代表模型密钥或渠道凭据已配置。使用既有模型服务、Telegram/钉钉或自己的 Docker 节点时，按对应文档检查真实连接。

本记录不作为 Railway 已部署成功的证明。既有服务需实际核对 main 构建、持久卷、健康检查和网页登录；没有确认线上状态前，不标记为线上可用。

Railway 的控制进程是 PID 1，`backup-all` 又要求引擎停止，本版本没有在线整实例快照。正常部署优先用已有卷备份，restic 用于已取得的离线副本或已生成的一致快照。管理员恢复入口也要求当前配置有效，详见 [RAILWAY.md](RAILWAY.md)。

新提交使用 noreply 作者信息，提交元数据与变更内容在发布前单独核对；第三方许可证中的上游作者署名保留。
