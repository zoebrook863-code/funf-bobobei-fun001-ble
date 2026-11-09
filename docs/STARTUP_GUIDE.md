# Windows登录启动与可选隧道

先完成依赖安装、本人设备绑定和本机控制验收。在项目目录执行以下命令。

```powershell
.\fun001.ps1 startup-install
.\fun001.ps1 startup-status
```

登录启动项由当前项目路径动态生成。授权控制台以最小化窗口启动。已初始化HTTP凭据时启动网关，存在private/tunnel.json时启动可选隧道。Configured与SupervisorRunning分别显示配置和守护状态。

```powershell
.\fun001.ps1 startup-start
.\fun001.ps1 startup-disable
```

start立即启动组件，disable取消后续登录自启动。普通grant或permit需要本人先在控制台确认。新的会话从零目标开始，进程退出后的重试使用已保存的设置。

## 可选SSH反向连接

先准备本人自己的SSH账号、密钥和已核对的known_hosts。示例文件为examples/tunnel.example.json。将自己的配置保存到private/tunnel.json。

```json
{
  "ssh_target": "user@example.invalid",
  "remote_port": 18769
}
```

ssh_target支持SSH主机别名或user@hostname，remote_port范围1024–65535。远端和本机转发端均绑定127.0.0.1。远端端口需要由本人确认空闲。

下面的检查只验证本地配置并显示目标，分享输出前需要清除个人部署信息。

```powershell
.\scripts\start-fun001-tunnel.ps1 -Check
```

前台运行连接通道可直接执行start-fun001-tunnel.ps1。使用startup-start时，守护程序在子进程退出后约5秒重试。SSH保活为15秒、2次。系统网络环境与远端转发由使用者自行配置。
