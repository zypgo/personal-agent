# Railway：一个现有服务，一个持久卷

V3 将聊天、渠道、模型 API、身份、审批、SQLite 队列和工作空间状态保留在控制面；命令执行发给自己电脑上的 Runner。此配置不申请云沙箱、Redis、托管数据库、对象存储或第二个服务。模型调用仍使用你配置的 API 供应商，计算与存储仍受已有 Railway 计费规则约束。

## 1. 在已有服务上设置

| 设置 | 值 |
| --- | --- |
| 构建 | 仓库根目录的 `Dockerfile` |
| 启动命令 | 保留 Docker 默认命令；或 `python -m personal_agent.deploy` |
| 持久卷 | 将现有 Volume 挂载到 `/data` |
| 副本 | **1**；不要跨区域复制同一 SQLite 控制面 |
| Healthcheck | `/healthz`，启动等待 90 秒 |
| Restart | `ON_FAILURE`，最多 5 次 |
| Serverless / Sleep | 关闭，保证定时任务与渠道接收持续工作 |
| SIGTERM 到 SIGKILL | 45 秒，例如 `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=45` |

`railway.toml` 保留上述设置的兼容声明。2026-11-17 查阅的[官方配置文档](https://docs.railway.com/config-as-code)指出：Config as Code 已弃用，既有服务的 `railway.toml` / `railway.json` 兼容期到 2026-12-01，新服务不能再启用这一机制。因此请在**已有服务**设置中核对上表，不依赖兼容文件长期生效。这里没有提交可能创建额外资源的 Infrastructure as Code，也没有自动部署。

服务变量：

```dotenv
PA_HOME=/data
PA_CONTROL_ONLY=1
PA_WEB_PUBLIC_URL=https://agent.example.test
OPENAI_API_KEY=YOUR_EXISTING_PROVIDER_KEY
RAILWAY_DEPLOYMENT_DRAINING_SECONDS=45
```

`PA_WEB_PUBLIC_URL` 填 Railway 已分配的域名或现有自定义域名；如未设置，会采用 `https://$RAILWAY_PUBLIC_DOMAIN`。只能有协议、主机和可选端口，不能包含账号、密码、路径、查询参数或片段。服务监听 Railway 注入的 `PORT`。别将密钥写入仓库或 Dockerfile；继续使用 Railway 私有变量，兼容服务可在 `/data/config.toml` 配置 `model.api_key_env` 和 `model.base_url`。

首次启动无需 `pa init`。程序创建私有目录和数据库，环境覆盖不会重写现有 `config.toml`。`PA_CONTROL_ONLY=1` 关闭本地 shell；容器启动入口还会拒绝启用的 Python 插件和 stdio MCP。需要外部工具时可以配置远程 HTTP MCP，执行命令则使用配对的 Runner。

### 卷与进程权限

[Railway 官方说明](https://docs.railway.com/volumes)中，卷在运行时挂载且由 root 拥有。镜像只用 root 初始化 `/data` 的目录权限，随后清除附加组并切换到 UID/GID `10001`，再加载应用。它不会递归修改旧文件权限或跟随符号链接；已有数据迁移时，须在停止旧服务后确认目录内文件属于这个服务账号。不要把迁移放进 pre-deploy command：该阶段没有挂载持久卷。

挂载卷的服务重新部署会有短暂中断，因为同一个卷不能同时由新旧实例运行；SQLite、Inbox、Outbox、任务尝试和 Runner 收据使恢复可追踪，但不宣称零停机。[健康检查说明](https://docs.railway.com/deployments/healthchecks)也说明检查只用于部署就绪，并不是持续监控。程序仅给健康检查路径接受 Railway 专用 Host，其他 API 继续校验精确 Host、Origin 和 CSRF 请求头。

### 未登录入口按来源地址限流

注册、恢复和节点配对这三个未登录入口共用一个按来源地址的计数（每次调用都算，60 秒内 10 次）；旧的浏览器配对另有一个只统计失败的计数，额度相同。应用读取的是 TCP 对端地址，不读任何转发头；而 Railway 在应用前面有一层边缘代理，[官方文档](https://docs.railway.com/networking/public-networking/specs-and-limits)（2026-11-17 查阅）说明它用 `X-Real-IP` 传递访客地址。所以在 Railway 上所有访客看起来来自同一个代理地址，共用一个计数。

配对码、恢复码都是高熵的一次性凭据，这不是认证绕过，而是可用性取舍：有人持续请求，会让这些入口对所有人短暂返回 429（约一分钟）。已登录用户的接口和节点的任务请求不受这个限流影响。不要为此盲目放开 uvicorn 的 `FORWARDED_ALLOW_IPS`：uvicorn 只认 `X-Forwarded-For`，而上述文档页没有列出这个头；信任可被伪造的转发头反而会让限流失效。若它成为实际问题，应作为带测试的单独变更，决定是否只在确认来自平台代理时读取 `X-Real-IP`。

## 2. 首次打开网页与手机

通过 Railway 的 SSH/终端进入这个**既有服务**，使用入口的管理员模式。它会先切换到同一服务账号，避免 root 命令产生随后无法访问的 SQLite/WAL 文件。

```bash
python -m personal_agent.deploy admin web pair --label phone
```

将终端给出的短时一次性链接在自己的浏览器打开。不要把邀请链接贴进日志、工单、截图或 PR。后续从已登录的设置页管理成员、空间与节点；成员使用自己的身份和邀请，不共享 owner 浏览器会话。手机直接访问公开 HTTPS 地址即可，无需购买隧道服务。

## 3. 连接自己的电脑

控制面生成节点邀请，授权到指定空间：

```bash
python -m personal_agent.deploy admin node invite --space personal-owner --label laptop
```

在自己的 Mac/Linux 上安装同版本项目，并先安装、启动 Docker。默认工作镜像由你主动拉取一次：

```bash
docker pull python:3.12-slim
pa node pair --url https://agent.example.test
pa node run
```

`pair` 会隐藏输入一次性配对码；码不放命令行参数。节点只向控制面发起出站 HTTPS，不开放入站端口，也不需要将 Docker API 暴露到公网。详细恢复、撤销和限制见 [NODES.md](NODES.md)。

## 4. 备份、升级与回滚

升级前停止提交新任务，等待正在运行的任务完成，或在网页请求取消并等待节点确认。备份整个 `/data` 的一致快照，包含根数据库、各空间自己的数据库、记忆文件、工作区与产物；单独拷贝一个正在使用的 `state.db` 会漏掉 WAL 和其它空间。在线 `pa db backup` 只处理当前 App 数据库，不能替代整个控制面的卷备份。Railway 上优先使用已有卷的备份功能。

`backup-all` 要求控制进程已经停止，且目标位于 `PA_HOME` 外。在本镜像中控制进程是 PID 1，停止它会结束容器，因此 **SSH 进入正常运行的服务后执行一条 `backup-all` 不是完整维护流程**。本版本未提供在线整实例快照或自动切换维护容器。需要 restic 时，在自有主机上处理已安全取得的离线数据副本；自托管部署则可停服务后执行 `pa db backup-all /trusted-backups/snapshot`，再用 `pa db restore-all /trusted-backups/snapshot --to /trusted-restores/home` 校验恢复到新目录。示例目标的父目录须预先创建并由服务账号可写，不能当作镜像中已经存在的挂载。完整说明见 [V3.md](V3.md)。

恢复后仍只启动一个控制实例。正常重启由节点检查同一个固定容器并继续上传收据；若将控制数据库回滚到较早快照，必须先停止 Runner 并核对仍存活的容器，不能将旧快照误认为最新执行事实。数据库迁移后回退代码时，旧代码可能拒绝更高 schema 版本；应成套恢复升级前备份，而不是只切换 Git 版本。

`restore-all` 默认使快照中的浏览器会话、节点和未使用邀请失效，并更新身份授权版本。恢复后从管理员终端重新配对浏览器，检查成员与渠道授权，再为 Runner 签发新邀请。旧任务、执行资源和收据仍保留供核对，旧批准不会因回滚自动恢复执行。这里的重新授权与普通重启不同：正常重启继续使用原有有效凭据。

部署入口的 `admin` 子命令也会先校验当前配置。若当前 `config.toml` 已损坏或缺少有效 HTTPS 配置，这个入口不能直接用于恢复；应在明确使用服务 UID/GID 的独立维护环境处理离线副本，再按现有卷恢复流程切换，不能用 root 在正在运行的卷内直接写入新数据库。

恢复非管理员成员的原身份时，在可信服务终端先运行 `python -m personal_agent.deploy admin web principals`，核对 ID 与 active/disabled 状态；然后运行 `python -m personal_agent.deploy admin web pair --principal EXISTING_PRINCIPAL_ID --no-qr`。设备链接仅登录该既有有效身份，不创建新人或修改项目角色；交给对应成员一次性打开。具体流程见 [COLLABORATION.md](COLLABORATION.md)。

### 0.3.1 的加密归档

镜像内置 restic 0.19.1 和 OpenSSH 客户端。对于已经创建好的离线一致快照，可用 `python -m personal_agent.deploy admin db restic ...` 加密归档；这不会使运行中主目录自动成为一致快照。恢复必须经过本项目的 `restic restore`，继续执行撤权和任务冻结。命令、密码文件和保留策略见 [基础组件升级](OPEN_SOURCE_ADAPTERS.md#加密备份与恢复)。

归档支持已有本地磁盘或自有 SFTP，不创建存储服务、不自动上传。使用 SFTP 时先为服务账号配置 SSH 主机校验和凭据，账号 home 为 `/data`。只留在同一卷内的归档不能抵御整卷丢失。浏览器 bundle 已随镜像打包，不需要 Node 或运行时 CDN。

## 验证范围

仓库测试覆盖环境覆盖、公开源校验、控制面禁用本地执行、权限切换顺序，以及节点重复交付、断网、丢失回执、重启恢复和取消流程。此 PR 不创建或部署 Railway 资源。实际域名、卷权限、Docker 镜像可用性和真实模型额度需在你已有环境启动后检查。
