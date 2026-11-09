# 控制指南

## 临时授权

在本机授权控制台执行 grant，完成现场提示后默认授权震动、模式1、上限1，最长20分钟。可用命令如下。

| 命令 | 用途 |
| --- | --- |
| functions | 查看通道、模式与编码范围 |
| grant | 默认临时授权 |
| grant 功能 模式 上限 | 选择临时功能、模式与强度上限 |
| grant3 吸吮上限 震动上限 电流上限 [模式或all] | 选择临时组合授权 |
| level 强度 [秒数] [模式] | 当前功能的定时输出 |
| state JSON [秒数] | 完整三路目标的定时输出 |
| status | 查看当前许可、链路、目标和结果 |
| stop | 全停并结束当前会话 |
| revoke | 全停并持久撤销常态许可 |
| quit | 结束当前控制服务 |

未验收配置需要额外本机确认，验收条件见[安全说明](SAFETY.md)。

## 常态许可

permit 在本机明确确认后保存常态许可。当前预设为 suction、vibration 各模式1/上限1，electric 上限0。许可跨重启保存，直到本人撤销。正常断开后会尝试重连，新的会话从零目标开始。

输出结果未知时进入暂停状态。本人先物理关机处理，再按控制台提示确认恢复。MCP的 emergency_stop 和 release_control 会持久撤销该许可。

普通 stop 后可以自动重新进入零目标就绪状态。重新连接或停止后，客户端读取 status，按 standing_retry_after 等待，再使用新的 session_id。

## MCP目标语义

set_state 和 run_state_seconds 接受完整目标，省略的通道归零。set_intensity 更新单路并保留其他路当前目标，省略function且level为0时执行全停。

模式范围1–4，强度编码0–100，每路按本机许可上限钳制。直接调用set_intensity/set_state时使用新的幂等key，相同key绑定相同内容，ttl满足0 < ttl <= 1。定时工具自行生成内部key，run_seconds/run_state_seconds的seconds为1–300。

以下是传给 run_state_seconds 的结构示例。会话标识需从 status 获取，实际输出由本人授权和现场验收范围决定。

```json
{
  "session_id": "<current-session>",
  "state": {
    "suction": {"level": 1, "mode": 1},
    "vibration": {"level": 1, "mode": 1},
    "electric": {"level": 0, "mode": 1}
  },
  "seconds": 1
}
```

applied_state表示执行阶段的最近一次应答目标，最终state对应清理结果。execution_outcome和zero_outcome分别记录执行与归零结果。unknown需要现场处理。定时截止时由看门狗先完成归零的情况会标明resident_command_ttl_stop。
