# Railway：一个现有服务，一个持久卷

V3 把聊天、渠道、模型 API、身份、审批、SQLite 队列和工作空间的状态，都放在服务端；要执行的命令交给你自己电脑上的 Runner。这套配置不需要云沙箱、Redis、托管数据库、对象存储，也不需要第二个服务。模型调用还是走你自己配置的 API 供应商；计算和存储按你已有的 Railway 计费规则算钱。

## 1. 在已有服务上设置

| 设置 | 值 |
| --- | --- |
| 构建 | 仓库根目录的 `Dockerfile` |
| 启动命令 | 保留 Docker 默认命令，或用 `python -m personal_agent.deploy` |
| 持久卷 | 把现有的 Volume 挂到 `/data` |
| 副本数 | **1**。不要跨区域复制同一个 SQLite 数据库 |
| Healthcheck | `/healthz`，启动后等 90 秒 |
| Restart | `ON_FAILURE`，最多重试 5 次 |
| Serverless / Sleep | 关掉，这样定时任务和渠道消息才能一直收得到 |
| 从 SIGTERM 到 SIGKILL | 45 秒，比如设 `RAILWAY_DEPLOYMENT_DRAINING_SECONDS=45` |

`railway.toml` 里也写了上面这些设置，只是为了兼容。2026-11-17 查阅的[官方配置文档](https://docs.railway.com/config-as-code)说：Config as Code 已经弃用；已有服务的 `railway.toml` / `railway.json` 兼容到 2026-12-01，新服务已经不能再启用。所以请直接在**已有服务**的设置里核对上面这张表，别指望兼容文件一直有效。

服务变量：

```dotenv
PA_HOME=/data
PA_CONTROL_ONLY=1
PA_WEB_PUBLIC_URL=https://agent.example.test
OPENAI_API_KEY=YOUR_EXISTING_PROVIDER_KEY
RAILWAY_DEPLOYMENT_DRAINING_SECONDS=45
```

`PA_WEB_PUBLIC_URL` 填 Railway 分配的域名，或你已有的自定义域名。没设的话，会用 `https://$RAILWAY_PUBLIC_DOMAIN`。它只能包含协议、主机和可选的端口，不能带账号、密码、路径、查询参数或片段。服务监听 Railway 注入的 `PORT`。

别把密钥写进仓库或 Dockerfile，继续用 Railway 的私有变量。如果用兼容的服务，可以在 `/data/config.toml` 里配置 `model.api_key_env` 和 `model.base_url`。

第一次启动不需要运行 `pa init`。程序会自己创建私有目录和数据库；环境变量覆盖不会重写已有的 `config.toml`。`PA_CONTROL_ONLY=1` 会关掉本地 shell，容器启动入口还会拒绝已启用的 Python 插件和 stdio MCP。需要外部工具时，可以配远程的 HTTP MCP；要执行命令，就用配对好的 Runner。

### 卷和进程权限

据 [Railway 官方说明](https://docs.railway.com/volumes)，卷在运行时才挂载，属于 root。镜像先以 root 身份初始化 `/data` 的目录权限，然后清掉附加组，切换到 UID/GID `10001`，再加载应用。

迁移已有数据时，要先停掉旧服务，再确认目录里的文件都属于这个服务账号。不要把迁移放进 pre-deploy command：那个阶段没有挂载持久卷。

带卷的服务重新部署时会短暂中断，因为同一个卷不能同时给新旧两个实例用。SQLite、Inbox、Outbox、任务尝试和 Runner 的收据，让恢复过程有迹可查。健康检查只用来判断部署是否就绪（见[官方说明](https://docs.railway.com/deployments/healthchecks)）；程序只对健康检查路径接受 Railway 专用的 Host。

### 未登录的入口按来源地址限流

注册、恢复、节点配对这三个不用登录的入口，共用一个按来源地址计算的次数：每次调用都算，60 秒内最多 10 次。旧的浏览器配对另有一个只统计失败次数的计数，额度一样。

应用读到的是 TCP 对端地址，不读任何转发头。但 Railway 在应用前面有一层边缘代理，[官方文档](https://docs.railway.com/networking/public-networking/specs-and-limits)（2026-11-17 查阅）说它用 `X-Real-IP` 传递访客地址。所以在 Railway 上，所有访客看起来都来自同一个代理地址，共用一个计数。

有人不停地请求时，这几个入口会对所有人短暂返回 429（大约一分钟）。已登录用户的接口和节点领任务的请求，不受这个限流影响。

## 2. 第一次打开网页和手机

通过 Railway 的 SSH / 终端进入这个**已有的服务**，用部署入口的管理员模式。它会先切换到同一个服务账号，免得用 root 运行的命令产生后面访问不了的 SQLite / WAL 文件。

```bash
python -m personal_agent.deploy admin web pair --label phone
```

把终端给出的一次性短时链接，在你自己的浏览器里打开。之后在已登录的设置页管理成员、空间和节点；成员用自己的身份和邀请，不共用 owner 的浏览器会话。手机直接访问公开的 HTTPS 地址就行，不用另外买隧道服务。

## 3. 连接你自己的电脑

在服务端生成一个节点邀请，授权给指定的空间：

```bash
python -m personal_agent.deploy admin node invite --space personal-owner --label laptop
```

在你自己的 Mac / Linux 上安装同版本的项目，先装好并启动 Docker。默认的工作镜像要你自己拉取一次：

```bash
docker pull python:3.12-slim
pa node pair --url https://agent.example.test
pa node run
```

`pair` 用隐藏输入的方式读一次性配对码，配对码不放在命令行参数里。节点只主动向服务端发起出站的 HTTPS，不开放入站端口，也不需要把 Docker API 暴露到公网。恢复、撤销和限制的细节见 [NODES.md](NODES.md)。

## 4. 备份、升级与回滚

升级前，先停止提交新任务；等正在运行的任务做完，或者在网页里请求取消，等节点确认。

备份要对整个 `/data` 做一致的快照，包括根数据库、各空间自己的数据库、记忆文件、工作区和产物。单独拷贝一个正在使用的 `state.db`，会漏掉 WAL 和其他空间。在线的 `pa db backup` 只处理当前 App 的数据库，不能代替整个服务端卷的备份。在 Railway 上，优先用已有卷的备份功能。

`backup-all` 要求服务进程已经停了，而且目标位置在 `PA_HOME` 之外。这个镜像里，服务进程就是 PID 1，停掉它，容器就结束了。所以，**SSH 进正在运行的服务，执行一条 `backup-all`，并不是完整的维护流程**。这个版本没有在线的整实例快照，也不能自动切换到维护用的容器。

需要 restic 时，在你自己的主机上处理已经安全取得的离线数据副本。自托管部署可以停掉服务，执行 `pa db backup-all /trusted-backups/snapshot`，再用 `pa db restore-all /trusted-backups/snapshot --to /trusted-restores/home` 恢复到新目录来校验。示例里的目标目录，它的上级目录要事先建好，并且服务账号可写；别当成镜像里本来就有的挂载点。完整说明见 [V3.md](V3.md)。

恢复之后，仍然只启动一个服务实例。正常重启时，节点会检查同一个固定的容器，继续上传收据。如果把服务端数据库回滚到较早的快照，必须先停掉 Runner，核对还活着的容器，不能把旧快照当成最新的执行情况。数据库迁移之后再回退代码时，旧代码可能拒绝更高的 schema 版本；要把升级前的备份整套恢复，而不是只切换 Git 版本。

`restore-all` 默认会让快照里的浏览器会话、节点和没用过的邀请失效，并更新身份授权的版本。恢复后，要从管理员终端重新配对浏览器，检查成员和渠道的授权，再给 Runner 发新的邀请。旧的任务、执行资源和收据还留着，方便核对；旧的批准不会因为回滚而自动恢复执行。这种重新授权，跟普通重启不一样：正常重启继续用原来有效的凭据。

部署入口的 `admin` 子命令也会先校验当前配置。如果当前的 `config.toml` 已经损坏，或者缺少有效的 HTTPS 配置，这个入口就不能直接用来恢复。这时要在一个明确使用服务 UID/GID 的独立维护环境里处理离线副本，再按现有的卷恢复流程切换；不能用 root 在正在运行的卷里直接写新数据库。

要恢复非管理员成员的原身份：在可信的服务终端先运行 `python -m personal_agent.deploy admin web principals`，核对 ID 和 active/disabled 状态；然后运行 `python -m personal_agent.deploy admin web pair --principal EXISTING_PRINCIPAL_ID --no-qr`。设备链接只登录那个已经存在、有效的身份，不会创建新的人，也不会改项目角色。把它交给对应的成员，让他打开一次。具体流程见 [COLLABORATION.md](COLLABORATION.md)。

### 0.3.1 的加密归档

镜像里带了 restic 0.19.1 和 OpenSSH 客户端。已经做好离线的一致快照后，可以用 `python -m personal_agent.deploy admin db restic ...` 加密归档。这不会让运行中的主目录自动变成一致快照。恢复必须走本项目的 `restic restore`，它会继续执行撤权和任务冻结。命令、密码文件和保留策略见[基础组件升级](OPEN_SOURCE_ADAPTERS.md#加密备份与恢复)。

归档支持已有的本地磁盘或你自己的 SFTP，不会创建存储服务，也不会自动上传。用 SFTP 时，先给服务账号配好 SSH 主机校验和凭据，账号的 home 是 `/data`。只存在同一个卷里的归档，扛不住整个卷丢失。浏览器的 bundle 已经打包进镜像，不需要 Node，也不用运行时的 CDN。
