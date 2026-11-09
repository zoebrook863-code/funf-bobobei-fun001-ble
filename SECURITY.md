# 私人数据与问题报告

private目录包含设备地址、绑定、API与MCP凭据、个人连接配置和诊断数据。仅当前用户访问权限与.gitignore分别用于本机隔离和版本管理隔离。

提交前运行以下检查，并核对git diff与待提交文件列表。

```powershell
python tools/check_release.py
git diff --cached
git status --short
```

问题报告只提供必要的版本、脱敏错误类别与最小复现。分享前清除设备地址、设备ID、序列号、账号、密钥、完整URL、私人目录和抓包身份信息。

HTTP远程使用需要本人管理认证、TLS和可撤销的本机许可。多人共享凭据会扩展可控制者范围，需要由设备所有者明确决定。

硬件断链和停止风险见[安全说明](docs/SAFETY.md)。安全问题可通过仓库启用的私人报告渠道提交，公开issue使用脱敏复现。
