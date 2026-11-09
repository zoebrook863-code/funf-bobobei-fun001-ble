# 只读设备发现

Windows电脑需要支持BLE Central。运行以下入口读取适配器能力。

```powershell
.\fun001.ps1 bluetooth-check
```

通过本人官方App的设备信息，或nRF Connect只读扫描，核对本人设备的完整BLE地址。保存绑定时使用隐藏输入。

```powershell
.\fun001.ps1 setup
```

绑定完成后，可使用inspect入口读取本人已绑定设备的服务与特征。

```powershell
.\fun001.ps1 inspect
```

此步骤用于扫描、连接和服务发现。设备信息和截图分享前需要清除地址、设备ID、序列号和账号信息。真实控制首次验收见[安全说明](SAFETY.md)。
