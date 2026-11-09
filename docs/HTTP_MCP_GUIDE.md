# MCP接入

BLE服务运行在设备附近的Windows电脑。MCP客户端可以在同机使用stdio，或连接使用者自行部署的认证HTTP入口。

## 本机stdio

先启动本机控制服务并在本机建立授权。客户端配置示例如下，替换为自己的项目安装路径。

```toml
[mcp_servers.fun001-local-control]
command = "C:/path/to/project/.venv/Scripts/python.exe"
args = ["-B", "C:/path/to/project/src/fun001_mcp.py"]
startup_timeout_sec = 15
tool_timeout_sec = 330
enabled = true
```

## 本机HTTP

```powershell
.\fun001.ps1 http-init
.\fun001.ps1 http-mcp
```

网关绑定127.0.0.1的8769端口，路径/mcp。http-init首次生成当前用户受限目录中的独立凭据。客户端通过私密环境或凭据助手提供Bearer。

```toml
[mcp_servers.fun001-http-control]
url = "http://127.0.0.1:8769/mcp"
bearer_token_env_var = "FUN001_HTTP_TOKEN"
startup_timeout_sec = 20
tool_timeout_sec = 330
enabled = true
```

## 远程入口

可选连接通道配置见[启动说明](STARTUP_GUIDE.md)。远端由使用者配置自己的TLS与认证转发，转发到电脑的回环HTTP网关。上游Host应为127.0.0.1:8769，允许流式响应并及时传播断开。

公开URL示例为 `https://mcp.example.com/fun001-mcp`。凭据、设备绑定和隧道配置保存在自己的private目录，实际URL与凭据按使用者自己的账户配置管理。

## Codex逐工具审批

Codex的工具审批与设备许可分别设置。无交互客户端需要本人明确预批准受信任的工具。例如定时单路输出可配置如下。

```toml
[mcp_servers.fun001-http-control.tools.run_seconds]
approval_mode = "approve"
```

对run_state_seconds、heartbeat、stop、emergency_stop、release_control可按本人选择建立各自的同类配置。所有控制工具还受到本机许可、强度上限与动作时限约束。配置后重新加载客户端，并先做只读与无效会话验收。

配置字段以[OpenAI MCP官方说明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)为准。
