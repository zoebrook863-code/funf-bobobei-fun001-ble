# FUNF 啵啵贝 FUN-001 · Windows 本机蓝牙与 MCP 控制

通过 **Windows 本机蓝牙** 控制 FUNF FUN-001 非 Pro 设备，提供 PowerShell、stdio MCP，以及可选的认证 HTTP MCP。

> **重要风险**
>
> 单台实测设备在 BLE 断链后持续动作，观察至断链后约10秒仍在运行，最后通过物理关机停止。软件停止依赖进程存活、可用的蓝牙链路和成功写入。使用者需要能立即触及设备电源键。初次验证请取下设备、放在桌面进行。完整边界见[安全与实测范围](docs/SAFETY.md)。

## 目录

```text
fun001.ps1       PowerShell 统一入口
src/             控制服务、BLE 与 MCP 实现
scripts/         登录启动与可选连接脚本
docs/            使用指南、协议、风险与致谢
tests/           假设备回归测试
requirements/    依赖清单
examples/        通用配置示例
tools/           发布前隐私检查
licenses/        第三方许可证
```

## 架构

```text
PowerShell 或本机 MCP 客户端
             │
       本机控制服务
             │
   Windows BLE 适配器
             │
    附近的 FUN-001 设备
```

BLE 连接始终由设备旁的 Windows 电脑持有。电脑和设备需要保持在线，并处于有效蓝牙范围内。可选远程 MCP 将请求转发到这台电脑，远端服务器承担认证转发用途。运行时控制路径使用电脑蓝牙，手机可用于首次只读查看本人设备地址。

## 支持与验收

| 项目 | 软件支持与实测边界 |
| --- | --- |
| 平台 | Windows，Python 3.11，支持 BLE Central 的适配器 |
| 设备 | FUNF FUN-001 非 Pro，单台设备的证据与观察 |
| 通道 | suction、vibration、electric，按本人授权限制 |
| 模式与强度 | 编码模式1–4、强度0–100，实际值受本机上限约束 |
| 已观察动作 | 震动模式1/值1、吸吮模式1/值1，以及两路各模式1/值1、电流0的一次短时组合 |
| 待验收 | 其他固件与设备、其他模式、较高强度、电流非零、长时间运行 |
| 状态证据 | 软件目标、ATT应答和人工物理观察分别记录 |

模式标签来自 App 资源分析，波形时序和实际物理强度尚待测量。最低非零编码也需要现场验收。

## 从空目录安装

在下载或克隆后的项目目录打开 PowerShell。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\fun001.ps1 bluetooth-check
.\fun001.ps1 setup
```

setup 隐藏输入本人设备的完整 BLE 地址，将绑定保存到仅当前用户可访问的 private 目录。绑定步骤只保存地址。地址可通过本人官方 App 或 nRF Connect 的只读扫描获得，步骤见[只读设备发现](docs/READONLY_BLE_GUIDE.md)。

设备放在桌面并开机，结束手机上占用设备的连接，然后启动控制台。

```powershell
.\fun001.ps1 control
```

初始授权状态为关闭。在 FUNF 控制台输入 grant，按提示确认现场条件，可建立震动模式1、上限1的临时授权。临时授权最长20分钟。

```text
grant
status
stop
quit
```

详细参数、单路与组合语义见[控制指南](docs/CONTROL_GUIDE.md)。

## MCP

本机 stdio MCP 由客户端启动以下程序，BLE 仍由单独运行的控制服务持有。

```powershell
.\.venv\Scripts\python.exe -B .\src\fun001_mcp.py
```

工具包含 status、project_status、project_audit、set_intensity、set_state、run_seconds、run_state_seconds、heartbeat、stop、emergency_stop、release_control，共11项。

定时输出使用 run_seconds 或 run_state_seconds，单次1–300秒，完成后尝试归零。普通设置的 TTL 最大1秒。本机许可决定通道、模式和强度上限，MCP按该范围执行。

客户端配置与认证 HTTP 入口见[HTTP MCP说明](docs/HTTP_MCP_GUIDE.md)。HTTP地址由使用者自行部署认证转发后获得。

## 常态许可与登录自启动

本人可在控制台输入 permit，阅读并确认持久许可设置。常态预设为吸吮、震动各模式1/上限1，电流上限0。该许可跨服务重启保存，以本人撤销结束。它建立新的零目标会话后接受新指令。

| 命令 | 效果 |
| --- | --- |
| stop | 归零并结束当前会话，常态许可允许后续重新就绪 |
| revoke | 归零并持久撤销常态许可 |
| MCP emergency_stop / release_control | 归零并持久撤销常态许可 |
| permit | 本机确认后开启或恢复常态许可 |

未确认输出会暂停自动恢复，需要本人检查并物理关机后在本机恢复。每条动作的期限和看门狗独立生效。

```powershell
.\fun001.ps1 startup-install
.\fun001.ps1 startup-status
.\fun001.ps1 startup-disable
```

登录启动项默认打开最小化的本机授权控制台。HTTP凭据已初始化时启动网关，私人隧道配置存在时启动可选连接通道。进程退出后按配置重试。具体流程见[启动说明](docs/STARTUP_GUIDE.md)。

## 测试

```powershell
.\fun001.ps1 test
```

测试使用假设备、临时运行目录和回环端口，覆盖协议、授权、时限、并发急停、未知结果、常态许可、MCP传输和安装配置。软件测试结果与硬件物理验收分别统计，见[测试说明](docs/TESTING.md)。

## 参考与致谢

- FUNF Pro 中继架构、授权与停止流程的参考来源。[zaoan0/funf-pro-relay](https://github.com/zaoan0/funf-pro-relay)
- SVAKOM BLE 逆向与本机 Python 中继方法的参考来源。[vickyldr/svakom-ble-ai](https://github.com/vickyldr/svakom-ble-ai)
- 本地开发阶段检阅的 SVAKOM 控制示例，其 README 同时引用了上一项目。[daningzi50-hub/svakom-sl278h-ble](https://github.com/daningzi50-hub/svakom-sl278h-ble)

本项目面向 FUN-001 非 Pro，协议编码依据该型号的本地 App 与通信证据复核，详见[协议说明](docs/PROTOCOL.md)。参考范围和许可记录见[第三方说明](docs/THIRD_PARTY_NOTICES.md)。

## 许可证与私人数据

本项目采用 [MIT License](LICENSE)，署名为项目贡献者。

公开文件清单包含实现、测试、通用示例和文档。设备绑定、凭据、个人服务器配置、运行日志和抓包保存在受限 private 目录。提交前运行隐私检查，按[安全说明](SECURITY.md)处理问题报告。
