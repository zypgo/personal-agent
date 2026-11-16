# 从手机使用 Personal Agent

手机通过现有网页聊天、发送图片和语音、查看审批及最近任务；电脑运行 agent，手机不需要模型 API key。

## 选择连接方式

| | 局域网 | Tailscale |
|---|---|---|
| 适合 | 手机和电脑连接同一个可信 Wi-Fi | 在家、出门或移动网络下使用 |
| 地址 | `http://192.168.x.x:8765` | `https://电脑名.tailnet.ts.net` |
| 加密 | HTTP；只用于可信局域网 | Tailscale 隧道 + HTTPS |
| 手机需要安装 | 无；用 Safari/Chrome | Tailscale；登录有权访问电脑的 tailnet |
| 语音 | 上传已有录音文件 | 上传录音或直接使用网页麦克风 |
| 配对 | 电脑生成的一次性链接/二维码 | 同样需要配对，不靠网络身份自动登录 |

## 方式一：同一个 Wi-Fi

在电脑的项目目录运行：

```bash
uv run pa web connect --via lan
```

只有一个私有 IPv4 地址时自动选择；有多个网卡时会列出候选，指定 Wi-Fi 地址即可：

```bash
uv run pa web connect --via lan --host 192.168.1.23
```

命令打开 `[web] enabled`，只监听指定私有地址，保留其余配置和注释，并打印手机配对二维码。默认沿用配置中的端口；可加 `--port 9000`。

随后重启前台 `pa serve`；如果已经安装后台服务，运行：

```bash
uv run pa service restart
```

首次使用、尚未启动服务时：

```bash
uv run pa serve
```

手机扫描二维码，或打开打印的完整链接。配对默认十分钟有效、只能使用一次；过期后重新运行 `uv run pa web pair --label phone`。

局域网 HTTP 无法启用浏览器麦克风和部分 PWA 能力。文字、图片、录音文件上传、审批和任务状态仍可使用。希望直接录音或加密访问时，选择 Tailscale。

## 方式二：Tailscale HTTPS

1. 在电脑和手机安装并登录 Tailscale，确认两台设备的访问策略允许连接。
2. 在电脑运行：

```bash
uv run pa web connect --via tailscale
```

命令检测 Tailscale 登录状态和 HTTPS 名称，验证已有 Serve 配置，再设置指向本机 agent 的私有 HTTPS 代理；之后将 HTTPS 地址写入配置，打印一次性二维码。macOS App 安装方式和 PATH 中的 `tailscale` CLI 均支持。

如果 tailnet 尚未开启 MagicDNS/HTTPS/Serve，按照 Tailscale 的提示先完成开启，再重试。命令不会使用 Funnel，不会覆盖已占用同一 HTTPS 地址的其他服务。如果 443 已经被使用，可选择其他端口：

```bash
uv run pa web connect --via tailscale --https-port 8443
```

重启 agent 后，手机保持 Tailscale 已连接，扫描二维码。网页麦克风仍需要浏览器授权。

Safari 可通过“分享 → 添加到主屏幕”使用；主屏幕 App 可能使用独立的 cookie，需要在新窗口里重新配对。Android Chrome 可通过浏览器的安装/添加到主屏幕入口使用。

设置命令操作的是 [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)，仅在 tailnet 策略允许的设备间提供服务；应用自己的配对、HttpOnly cookie、Host/Origin 检查仍然生效。

## 排查连接

```bash
uv run pa web check
uv run pa web
uv run pa doctor
```

`check` 分别检查本地监听和配置的手机地址，不修改设置。电脑检查通过不等于手机网络一定通畅：

- **局域网打不开**：确认两台设备同一网络；客用 Wi-Fi 常隔离客户端；检查电脑防火墙是否允许该端口；更换 Wi-Fi/IP 后重新运行 `connect --via lan --host ...`。
- **Tailscale 打不开**：检查两端连接状态、tailnet 访问规则、MagicDNS/HTTPS，以及 `tailscale serve status`。不要用 Funnel 暴露聊天页面。
- **配置已保存但仍打不开**：重新启动 agent；运行中的进程不会自动重载监听地址。
- **提示配对失效**：在电脑重新生成链接。不要在消息或公开截图里分享配对二维码。
- **关盖后无法连接**：服务仍运行在电脑上，睡眠或关机期间不能响应；需要保持电脑唤醒，或部署到常开的设备。

## 查看任务与管理设备

网页“设置 → Recent tasks”显示排队、执行、等待审批、预算暂停、下次重试和最终停止。点 Refresh status 从持久化状态重新读取；失败任务不会仅显示成仍在工作。

语音消息先持久化接收记录，再调用转录服务。相同消息 ID 的重投使用已经完成的转录；并发重投不会启动第二次调用。如果转录在确认结果前被中断，会明确提示重新发送新消息，不自动重复付费。外部服务在断线前是否已经计费无法从本地可靠判断。

```bash
uv run pa web sessions          # 查看已配对浏览器
uv run pa web revoke 3          # 撤销指定浏览器
uv run pa web revoke --all      # 全部退出
uv run pa web unpair            # 撤销尚未使用的链接
```

需要关闭本命令配置的 Tailscale 443 入口时，运行 `tailscale serve --bg --https=443 off`；使用其他端口时替换端口。浏览器会话的撤销独立于网络入口。

手机页面保留现有 SSE 重连机制；关闭网页后不提供系统级 Web Push。需要后台主动提醒时，可继续启用 Telegram 渠道。
