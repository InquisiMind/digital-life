"""一次性修复：为白纸化时丢失 life affair 的实例重建根事务。

根因：白纸化清空 apps/{uuid}/data/state.db 的 affairs 表后重启，
bootstrap 的 ensure_life_affair() 没有被再次调用（重启≠boot），
导致 L4 tick 每 tick 在 _find_life_affair() → None → 静默 return，
实例永不唤醒、消息永不消费。

修法：对每个实例 set_current_instance_id 后调 ensure_life_affair()，
它会建 life affair + 初念 + birth 事件（幂等，已有则跳过）。
"""
import sys
sys.path.insert(0, "/Users/zhanghaopu/Documents/探索项目/digital-life")

from infrastructure.config import (
    discover_instances, set_current_instance_id, reset_current_instance_id,
    get_instance_state_db_path,
)
from domain.orchestration.lifecycle_orchestration.bootstrap.runtime import (
    ensure_life_affair, _find_life_affair,
)

targets = sys.argv[1:] or discover_instances()
print(f"instances: {targets}\n")

for uuid in targets:
    print(f"── {uuid}  db={get_instance_state_db_path(uuid)}")
    existing = None
    token = set_current_instance_id(uuid)
    try:
        try:
            existing = _find_life_affair()
        except Exception as e:
            print(f"   find error: {e!r}")
        if existing:
            print(f"   already has life affair: {existing}  (skip)")
        else:
            aid = ensure_life_affair()
            print(f"   ✅ created: {aid}")
    finally:
        reset_current_instance_id(token)
print("\ndone.")
