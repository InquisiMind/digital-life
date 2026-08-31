# Operations Docs

本目录保存当前有效的运行和维护步骤。执行具有删除、迁移或运行时数据影响的操作前，
必须确认用户授权并保护 `apps/{id}/data/`。

| 文档 | 何时加载 | 开发流程位置 |
| --- | --- | --- |
| [instances.md](instances.md) | 创建、配置、验证、路由或删除实例；排查飞书和控制台实例问题。 | 调研、实施、运行时验证。 |
| [memory-maintenance.md](memory-maintenance.md) | 检查记忆健康、去重、规则治理或召回质量。 | 调研、运维实施、验证。 |
| [decisions/](decisions/README.md) | 系统层修改与常驻件部署前必读：ADR 索引与规范。 | 全阶段。 |
| [OPS_CHANGELOG.md](OPS_CHANGELOG.md) | 查任何运行时改动/部署/事故记录时。 | 全阶段。 |

运行服务、查看日志和选择测试时，同时读取
[commands-and-testing.md](../development/commands-and-testing.md)。记忆结构变化还需读取
[memory-lifecycle-design.md](../architecture/memory-lifecycle-design.md)。
