# 华为健康主动联系接入

华为健康数据不单独调度，只作为现有 `ContactSchedule` 的一种素材来源。

## 事件类型

- `sleep_completed`：睡眠完成后约 20–140 分钟，使用现有 `greeting` 目的。
- `workout_completed`：运动完成后 2 小时内，使用现有 `care` 目的。
- `recovery_trend`：最近 3 天静息心率上升 ≥ 7% 或 HRV 下降 ≥ 15%，使用 `care` 目的。
- `weekly_review`：每周最多一次，使用 `chat` 目的。

## 约束

- 只使用华为接口已完成的记录，不把云端延迟数据说成实时状态。
- 沿用主动联系总开关、目的开关、免打扰、每日上限、冷却、忙碌/休息闸门。
- 事件 ID 和现有 `execution_tasks` / `proactive_contacts` 去重。
- 健康素材只提供自由表达的由头：可以关心、打趣、分享角色自己，也可以跳过。
- 禁止逐项汇报、监控口吻、诊断、吓唬、饮食作息说教、反复催促。
- 不进入群聊或其他私聊窗口；绑定按当前私聊角色隔离。

## 配置

在「感知 → 健康感知 → 华为运动健康账号」：

- `health_enabled`：健康摘要总开关。
- `health_proactive_enabled`：允许健康事件参与既有主动联系。

这两个开关都开启，且现有主动联系总开关与对应目的开关开启时，健康事件才可能形成联系机会。

## 主要代码

- `astrbot_host/huawei_health/service.py`：把云端数据整理为睡眠、运动、周回顾和恢复趋势事件。
- `companion_v2/proactive/health_events.py`：把事件投影成现有 `ContactIntent`。
- `companion_v2/proactive/scheduling.py`：在现有候选择序中读取健康素材。
- `companion_v2/host/application.py`：注入感知配置读取器。
- `astrbot_host/multi_session.py`：把华为健康服务绑定到现有联系调度器。
