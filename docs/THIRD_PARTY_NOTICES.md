# 来源、参考与许可记录

本项目的发布清单包括FUN-001适配实现、测试、通用启动脚本与文档。设计参考范围如下。

## FUNF Pro relay

[zaoan0/funf-pro-relay](https://github.com/zaoan0/funf-pro-relay)提供了FUNF中继架构、授权、停止和设备持有者流程的参考。上游采用MIT许可证，原许可文本保存在[licenses/funf-pro-relay-MIT.txt](../licenses/funf-pro-relay-MIT.txt)。

本项目的目标是FUN-001非Pro，协议封装与控制语义根据该型号的本地App及通信证据复核，采用Windows本机BLE路径。

## SVAKOM BLE参考链

[vickyldr/svakom-ble-ai](https://github.com/vickyldr/svakom-ble-ai)提供了SVAKOM BLE分析、本机Python中继与AI控制组织方式的参考。检查时未见其明确开源许可证声明。

开发阶段直接检阅过[daningzi50-hub/svakom-sl278h-ble](https://github.com/daningzi50-hub/svakom-sl278h-ble)，其README明确引用上一项目，并声明MIT，根目录缺少独立LICENSE文件。该仓库还致谢了[buttplugio/stpihkal](https://github.com/buttplugio/stpihkal)。

这些SVAKOM项目在此作为设计与方法参考列出。当前发布清单分发本项目的FUN-001实现和测试。后续引入第三方源文件时，应单独核对授权范围并携带相应声明。

## 运行时依赖

Python依赖通过requirements文件安装，各依赖的许可信息随其发行包提供。

项目代码的MIT许可见[LICENSE](../LICENSE)。官方App素材、原始通信记录和用户设备资料由各自权利人与使用者管理。
