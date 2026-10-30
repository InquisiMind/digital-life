"""渐进式说话人注册表（2026-10-30 zhp：自动注册声纹，不需要提前录 15 秒）。

人的做法：第一次听到声音分不清谁是谁，但能听出"有区别"；跟你聊天的那个人
就是"对话者"（交互确认身份），旁边说的是"背景"。下次同样的声音出现直接识别。

技术：每句成句 → 提取声纹嵌入 → 与已有说话人比对（余弦>阈值=同一人）
→ 匹配到 → 更新该说话人的质心；匹配不到 → 新建说话人。
角色判定：头 N 句说了话且触发了 agent 回应的说话人 = "user"，
其余 = "background"。支持手动改标签（"这是我"/"这是背景"）。

存储：apps/<iid>/data/speaker_registry.json（含每个说话人的质心向量 + 元信息）
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# 同一说话人判定阈值（余弦）：实测同人不同句 ~0.6-0.8，不同人 ~0.1-0.3
MATCH_THRESHOLD = 0.55
# 自动成为 user 需要的最少"触发过 agent 回应"句数
AUTO_USER_MIN_SENTENCES = 2


class SpeakerProfile:
    """一个说话人的声纹档案。"""

    def __init__(self, speaker_id: str, label: str = "", role: str = "unknown"):
        self.speaker_id = speaker_id
        self.label = label or speaker_id
        self.role = role  # "user" | "background" | "unknown"
        self.centroid: np.ndarray | None = None  # 声纹质心（累积平均）
        self.count = 0                     # 累计句数
        self.agent_interactions = 0        # 触发过 agent 回应的句数
        self.last_seen = 0.0

    def update(self, embedding: np.ndarray) -> None:
        """增量更新质心（running mean）。"""
        if self.centroid is None:
            self.centroid = embedding.copy()
        else:
            # 指数移动平均，新数据权重略高（适应微小变化）
            self.centroid = 0.8 * self.centroid + 0.2 * embedding
            # 归一化保持余弦可比
            norm = np.linalg.norm(self.centroid)
            if norm > 0:
                self.centroid /= norm
        self.count += 1
        self.last_seen = time.time()

    def similarity(self, embedding: np.ndarray) -> float:
        if self.centroid is None:
            return 0.0
        return float(np.dot(embedding, self.centroid) /
                     (np.linalg.norm(embedding) * np.linalg.norm(self.centroid)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "label": self.label,
            "role": self.role,
            "centroid": self.centroid.tolist() if self.centroid is not None else None,
            "count": self.count,
            "agent_interactions": self.agent_interactions,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SpeakerProfile":
        p = cls(d["speaker_id"], d.get("label", ""), d.get("role", "unknown"))
        if d.get("centroid"):
            p.centroid = np.array(d["centroid"])
        p.count = d.get("count", 0)
        p.agent_interactions = d.get("agent_interactions", 0)
        p.last_seen = d.get("last_seen", 0)
        return p


class SpeakerRegistry:
    """一个实例的说话人注册表。自动聚类 + 角色判定 + 持久化。"""

    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        self._profiles: dict[str, SpeakerProfile] = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self._load()

    # ── 持久化 ──

    def _path(self) -> Path:
        from infrastructure.config import get_project_root
        return get_project_root() / "apps" / self.instance_id / "data" / "speaker_registry.json"

    def _load(self) -> None:
        try:
            path = self._path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            for d in data.get("profiles", []):
                p = SpeakerProfile.from_dict(d)
                self._profiles[p.speaker_id] = p
                num = int(p.speaker_id.split("_")[-1]) if p.speaker_id.startswith("spk_") else 0
                self._next_id = max(self._next_id, num + 1)
        except Exception:
            logger.debug("speaker registry load failed", exc_info=True)

    def _save(self) -> None:
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {"profiles": [p.to_dict() for p in self._profiles.values()]}
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.debug("speaker registry save failed", exc_info=True)

    # ── 核心操作 ──

    def identify(self, embedding: np.ndarray) -> tuple[SpeakerProfile, float]:
        """将一段嵌入匹配到说话人。已知 → 返回 (profile, 相似度)；
        未知 → 新建 profile 并返回 (新profile, 0.0)。"""
        with self._lock:
            best: SpeakerProfile | None = None
            best_sim = 0.0
            for p in self._profiles.values():
                if p.centroid is None:
                    continue
                sim = p.similarity(embedding)
                if sim > best_sim:
                    best_sim = sim
                    best = p
            if best is not None and best_sim >= MATCH_THRESHOLD:
                best.update(embedding)
                self._save()
                return best, best_sim
            # 新说话人
            spk_id = f"spk_{self._next_id}"
            self._next_id += 1
            new = SpeakerProfile(spk_id)
            new.update(embedding)
            self._profiles[spk_id] = new
            self._save()
            logger.info("speaker registry: NEW speaker %s (total=%d)", spk_id, len(self._profiles))
            return new, 0.0

    def note_agent_interaction(self, speaker_id: str) -> None:
        """该说话人的一句话触发了 agent 回应 → 加分。"""
        with self._lock:
            p = self._profiles.get(speaker_id)
            if p is None:
                return
            p.agent_interactions += 1
            if p.role == "unknown" and p.agent_interactions >= AUTO_USER_MIN_SENTENCES:
                p.role = "user"
                logger.info("speaker registry: %s auto-promoted to USER (%d interactions)",
                            p.speaker_id, p.agent_interactions)
            self._save()

    def set_role(self, speaker_id: str, role: str) -> bool:
        """手动设置角色（"这是我"/"这是背景"）。"""
        with self._lock:
            p = self._profiles.get(speaker_id)
            if p is None:
                return False
            # 设为 user 时其他 user 降为 background（只有一个对话者）
            if role == "user":
                for other in self._profiles.values():
                    if other.speaker_id != speaker_id and other.role == "user":
                        other.role = "background"
            p.role = role
            self._save()
            return True

    def set_label(self, speaker_id: str, label: str) -> bool:
        """设置显示名（"张三"/"背景"等）。"""
        with self._lock:
            p = self._profiles.get(speaker_id)
            if p is None:
                return False
            p.label = label
            self._save()
            return True

    def get_user_profile(self) -> SpeakerProfile | None:
        """当前被识别为"user"的说话人（声纹门禁用）。"""
        for p in self._profiles.values():
            if p.role == "user":
                return p
        return None

    def is_user(self, speaker_id: str) -> bool:
        p = self._profiles.get(speaker_id)
        return p is not None and p.role == "user"

    def should_inject(self, speaker_id: str) -> bool:
        """该说话人的话是否该注入 agent。"""
        p = self._profiles.get(speaker_id)
        if p is None:
            return True  # 未知说话人 → 先给机会（可能就是用户第一句话）
        return p.role in ("user", "unknown")  # background 不注入

    def list_speakers(self) -> list[dict[str, Any]]:
        """列出所有说话人（前端显示/手动标注用）。"""
        return [
            {"speaker_id": p.speaker_id, "label": p.label, "role": p.role,
             "count": p.count, "interactions": p.agent_interactions}
            for p in self._profiles.values()
        ]

    def clear(self) -> None:
        """清空注册表。"""
        with self._lock:
            self._profiles.clear()
            self._next_id = 1
            self._save()


# ── 实例级单例 ──

_registries: dict[str, SpeakerRegistry] = {}
_registry_lock = threading.Lock()


def get_speaker_registry(instance_id: str) -> SpeakerRegistry:
    global _registries
    with _registry_lock:
        if instance_id not in _registries:
            _registries[instance_id] = SpeakerRegistry(instance_id)
        return _registries[instance_id]
