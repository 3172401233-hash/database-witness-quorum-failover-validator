# database-witness-quorum-failover-validator

验证 witness 仲裁、quorum 变化和旧主 fencing 的纯 Python 模拟器。

## 范围

本项目**只模拟仲裁状态**，不实现真实复制、存储、网络或心跳协议。输入一组
集群快照（副本日志位置、witness 状态、epoch、已确认写入位置、网络分区、
旧主心跳和 fencing token），输出哪些副本可以被提升、各自的 quorum 证据以及
阻止原因。

## 仲裁规则

- 故障切换期间投票成员固定为两个副本加一个 witness，共 3 票，严格多数阈值为 2。
- witness 只能参与 quorum 投票，不持有日志，不能独立决定数据最新位置。
- 候选必须在自己的网络连通分区内拿到多数票，孤立副本即使日志更新也不能提升。
- 候选时间线必须与已确认写入一致，且日志位置必须覆盖全部已确认写入，
  时间线分叉或日志落后都会阻止提升，避免违反已确认数据。
- 旧主仍存活且可达时必须先 fencing；新一轮 fencing token 必须严格大于
  旧 token。旧主恢复后若仍持有旧 token，`check_old_primary` 会拒绝其写入。
- 多个候选同时合规时，按日志位置、epoch、连通票数排序给出候选顺序。

## 运行测试

```bash
python -m unittest
```

测试覆盖四节点全部 15 种网络分区组合、witness 分区/故障、旧主恢复 fencing、
时间线分叉以及“日志更多但孤立的副本不能被错误提升”的安全场景。
