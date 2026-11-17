# 0.3.1：用成熟组件维护 V3 的基础能力

本次在 V3 主分支上升级 API 契约、浏览器请求和 Markdown、正文提取、自有 Docker Runner 与加密备份。控制面、执行节点和持久状态的职责继续沿用 [V3](V3.md)：一个已有 Railway 服务及卷、自有电脑、个人或少量可信协作者。本次没有增加数据库 schema，也不需要新购买云沙箱或托管中间件。

## 替换范围

| 层 | 采用的实现与锁定版本 | 本项目继续负责的边界 |
| --- | --- | --- |
| V3 HTTP API | FastAPI 0.163.1、Pydantic 2.14.3 | 身份、空间授权、读完请求体后的再次鉴权、Guard、错误格式、no-store |
| 浏览器请求 | TanStack Query Core 5.108.0 | 会话和空间隔离、撤权清屏、写请求不重试、产物不缓存 |
| Markdown | markdown-it 15.0.3 | token → DOM 渲染、URL 白名单、禁用原始 HTML 和外部图片、复杂度限制 |
| 前端构建 | Vite 8.3.8、TypeScript 5.9.3、openapi-typescript 7.13.0 | 可重现静态产物、契约漂移检查、现有 PWA 和 CSP |
| 本地正文提取 | Trafilatura 2.3.1 | 受控下载、SSRF、长度和并发限制、安全格式化、代码页与故障回退、日志与缓存隐私 |
| 自有执行节点 | Docker SDK for Python 7.2.0 | 固定资源 ID、收据、计划摘要、截止时间、未知状态、停止和清理 |
| 加密归档 | restic 0.19.1 | 一致快照、文件清单校验、恢复后撤权和冻结旧任务 |

版本来自本次锁文件和官方稳定发行。TypeScript 采用与当前类型生成器兼容的 5.9.3，没有强制忽略 peer dependency 冲突。Python 依赖由 `uv.lock` 固定，前端由 `package-lock.json` 固定；升级依赖必须重新生成并验证契约和静态产物。

### 保留的实现

OpenAI Agents SDK 仍位于 `harness/` 端口后。Inbox、Timers、Memory、Permissions、Outbox，以及 SQLite 中的任务尝试和授权版本继续由应用维护。新组件只接管通用解析、传输、构建和存储工作，不自行决定审批、不修改任务计划，也不在网络错误后重复执行副作用。

原聊天接口、SSE 与渠道协议继续使用现有 Starlette 路由；本次 FastAPI 覆盖的是 V3 协作和节点 API。没有将整个产品改写为另一种 Agent 框架。

## API 契约

`channels/web/contracts.py` 定义请求 DTO、响应文档和路由清单；`openapi.py` 从相同清单构建路由与 OpenAPI。全部 V3 JSON 写接口实际使用严格 DTO 校验，拒绝把字符串隐式转换为布尔值或整数。

调用顺序仍是：

1. 根据现有会话、空间角色或节点凭据授权。
2. 有界读取 JSON 请求体。
3. 再次检查会话和成员资格，防止读取期间撤权。
4. 严格校验 DTO，执行业务操作。

为保持这个顺序，接口显式调用 DTO 校验，不使用会提前读取 body 的自动参数注入。未知附加字段继续忽略；客户端不能通过伪造 actor、approver 或授权版本获得权限。已有错误结构、状态码、HEAD、修订别名和节点协议保留。

登录后可读取 `GET /api/v3/openapi.json`；该接口返回 `private, no-store`，不开放 Swagger/ReDoc。离线导出不创建应用、不打开数据库、不读取实例凭据：

```bash
uv run --frozen python scripts/export_api_schema.py
```

契约包含 36 个操作、32 条路径。响应保留原有 `JSONResponse`，模型用于文档和类型生成；含扩展字段的任务、节点记录没有被强行裁剪为封闭结构。

## 浏览器与 Markdown

`frontend/query.ts` 将 GET 的并发合并、状态和取消交给 Query Core。查询以身份、空间和会话代次分隔；已完成的数据立即过期，后续读取仍请求服务端。退出、切换空间或遇到 401/403 时取消旧请求并清除私有数据；权限丢失通知同步触发清屏，避免并发请求中先返回的取消错误掩盖撤权。

写请求直接发送一次，不进入 Query 的持久化或离线重放机制；响应丢失会显示失败/未知，不能据此自动重发。文件下载不进入查询缓存。浏览器存储和 Service Worker 仍只保留允许的公开壳资源。

`frontend/markdown-parser.ts` 使用 markdown-it 生成 token，已有 DOM 层只通过安全节点 API 构造页面。原始 HTML 不执行；链接校验协议；外部图片不自动加载；输入长度、嵌套层数和表格规模仍有上限。CommonMark 与旧解析器在个别嵌套语法上有差异，按标准语义展示，并通过原有注入与资源限制用例验证。

新增请求、解析、表单边界采用严格 TypeScript；现有 DOM 页面继续使用 JavaScript。生产包携带 `static/query.js`、`static/markdown-parser.js` 两份独立 bundle，**运行 Python 服务无需 Node、npm 或 CDN**。

维护前端时使用 Node 22.12 或更高版本：

```bash
npm ci
npm run build
npm run check
```

`check` 比较运行时 OpenAPI、提交的 schema、生成类型和实际 bundle，并运行 JS 回归测试。修改契约后重新导出 schema 和生成类型；生成产物必须随源码一起提交。第三方许可证随静态产物保留。

## 正文提取

配置项：

```toml
[tools]
html_extractor = "auto" # auto | basic | trafilatura
```

`auto` 对小片段保留原格式化器，对文章页尝试 Trafilatura。只解析已经经过现有 SSRF、DNS、重定向和下载大小检查的 HTML；不调用该库的下载器，也不将源 URL 交给它。解析放在线程中，避免阻塞事件循环；最多同时运行两个文章解析任务，容量不足直接回退。

超过 512,000 字符、5,000 个标签起始符、包含 `<pre>` 的代码页、带 rowspan/colspan 的表格或提取结果过短时，使用原基础读取器。表格跨度可能将很小的源文件展开成庞大内部网格，必须在进入文章解析器前回退。文章结果仍经过原安全 HTML 格式化器，保留代码缩进和链接策略。

禁用跨文档去重；每次解析结束调用上游提供的缓存清理接口，清除可能留存正文的缓存。上游日志不传播到应用日志，应用只记录通用失败原因，不记录网页正文或源地址。`basic` 可随时回退到旧读取器。

## 自有 Docker 节点

```bash
pa node run                  # 默认 SDK 后端
pa node run --backend cli    # 显式兼容后端
```

镜像仍由操作者预先拉取，两个后端都不自动下载。SDK 只替换 Docker 操作方式，保留 `Backend` 协议和原收据库：资源名、标签、计划摘要、启动意图先持久化；断线和重启检查同一个容器。已经记录启动意图后，无法证明未执行的情况进入 `uncertain`，不再次 `start`。

同步 SDK 调用使用有界工作槽位。取消 async 等待不表示底层调用已停止；在真实线程结束前不释放槽位。日志流单独关闭以打断阻塞读取，不因此把容器误标为已经停止。容器隔离、无网络、非 root、资源限额和本地截止时间与 V3 一致。

SDK 后端不继承环境代理凭据，也不自动把 Docker 客户端代理配置注入工作容器。SSH Docker context 使用显式 CLI 后端；任何远端 daemon 都必须能访问同一 bind mount 路径。这里只支持自己的可信 Docker 主机，不建立第三方云沙箱。完整节点操作见 [NODES.md](NODES.md)。

## 加密备份与恢复

restic 位于一致快照之后，不能直接对运行中的 agent 主目录执行本项目的归档命令。自托管主机先停止控制进程，并按 [V3](V3.md) 创建整实例快照。数据库、空间文件、工作区和产物仍由原快照机制统一校验。Railway 的 PID 1 停止会使容器退出，本版本没有在线整实例快照；正常部署优先用已有卷备份，restic 处理已安全取得的离线副本或已经生成的快照，见 [部署限制](RAILWAY.md#4-备份升级与回滚)。

本机安装[官方 restic](https://restic.readthedocs.io/en/stable/020_installation.html)。Railway 镜像已内置固定版本的官方 binary 和 OpenSSH 客户端。下面是自托管/离线副本的操作示例，需先创建可写的备份和恢复父目录；这些路径不是镜像自动挂载的位置。密码文件放在快照和仓库之外，权限为 `0600`，不要把密码写进命令行：

```bash
export RESTIC_REPOSITORY=/trusted-backups/encrypted
export RESTIC_PASSWORD_FILE=/trusted-secrets/restic-password

pa db backup-all /trusted-backups/snapshot-0.3.1
pa db restic --namespace personal init
pa db restic --namespace personal backup /trusted-backups/snapshot-0.3.1
pa db restic --namespace personal snapshots
pa db restic --namespace personal check
pa db restic --namespace personal restore FULL_64_CHARACTER_SNAPSHOT_ID --to /trusted-restores/home
```

`init` 只对新仓库运行一次。仅支持本地路径或 `sftp://user@host/path`，使用已有磁盘或自己的 SFTP 主机；没有申请对象存储。SFTP 依赖操作者已有的 SSH 密钥或 agent 及已验证的 host key，不禁用主机校验。镜像中服务账号的 home 为 `/data`，SSH 文件需由该账号可读并限制权限。Railway 管理命令通过 `python -m personal_agent.deploy admin db restic ...` 执行。

上传只包含 manifest 列明的文件，上传前后校验快照；不能在上传期间修改离线快照。归档超时后先查 `snapshots`，不要假定服务端没有保存。备份命名空间用于区分实例和保留策略，不能代替独立仓库密码或访问控制。

恢复先下载到临时目录，验证归档与 manifest，再调用现有 `restore-all` 逻辑创建新主目录。旧浏览器会话、邀请、节点和渠道授权失效；旧任务和计时器保持冻结，重新配对和核对后才能使用。不能绕过这一步直接启动 restic 下载目录。

保留策略默认预览，需要显式 `--apply` 才删除当前命名空间的旧快照引用：

```bash
pa db restic --namespace personal forget --keep-last 7
pa db restic --namespace personal forget --keep-last 7 --apply
```

不自动执行 `prune`、解锁、定时上传或删除其他命名空间。restic 密码丢失无法恢复。只备份在同一 Railway 卷内能帮助回滚，不能抵御整卷丢失；需要这种保障时使用已有的另一块磁盘或自有 SFTP 位置。

## 升级、回退与验证

1. 停止接收新任务，完成或明确取消正在执行的任务；保留不确定任务的收据和容器供核对。
2. 创建整实例一致快照。
3. 更新源码并执行 `uv sync --frozen`。既有 Railway 使用仓库 Dockerfile 构建；保持一个副本和同一个 `/data` 卷。
4. 启动控制面，检查 `/healthz`、网页登录、空间和模型配置；自有节点更新后使用原节点目录启动。
5. 用实际手机和既有渠道确认登录、审批、产物下载。新增组件不替你配置模型密钥、渠道凭据或 Docker daemon。

正文可切回 `basic`，Runner 可切回 `--backend cli`。API/前端回退需成套回退版本和锁文件；不要混用旧 bundle 与新契约。原始 `backup-all` / `restore-all` 仍可单独使用。恢复历史快照必须遵守撤权与任务核对流程。

开发质量门：

```bash
npm ci
scripts/check.sh
scripts/check.sh --python 3.11
# 可选真实本地加密归档测试，无外部服务和费用：
PA_TEST_RESTIC_BINARY=/absolute/path/to/restic uv run --frozen pytest tests/test_v3_restic.py -k real_restic
```

内存有限的开发机可设置 `PA_TEST_WORKERS=2`，仅减少测试并行数，不跳过用例；不同 Python 版本的全量检查依次运行。

离线测试不证明真实模型额度、Railway 已部署或 Docker 主机已配置。发布时分别记录测试结果、实际启动状态和外部环境检查；没有观测到的部署不标为完成。

## 上游来源

- [FastAPI：自定义 OpenAPI](https://fastapi.tiangolo.com/advanced/extending-openapi/)、[Pydantic：严格模式](https://docs.pydantic.dev/latest/concepts/strict_mode/)
- [TanStack Query Core](https://github.com/TanStack/query)、[markdown-it](https://github.com/markdown-it/markdown-it)、[Vite](https://vite.dev/guide/)
- [Trafilatura 用法](https://trafilatura.readthedocs.io/en/latest/usage-python.html)、[官方缓存清理实现](https://github.com/adbar/trafilatura/blob/v2.3.1/trafilatura/meta.py)
- [Docker SDK](https://docker-py.readthedocs.io/en/stable/)、[容器操作](https://docker-py.readthedocs.io/en/stable/containers.html)
- [restic 0.19.1 发行](https://github.com/restic/restic/releases/tag/v0.19.1)、[restic 恢复](https://restic.readthedocs.io/en/stable/050_restore.html)

来源核对日期：2026-11-17。Docker 中 restic 使用官方多平台镜像的固定 digest，不在启动时下载或自动更新。
