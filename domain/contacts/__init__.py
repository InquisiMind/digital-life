"""Contacts — 用户级社交关系映射 + 多平台 ID + 黑名单。

设计：
  - contacts 表以"用户"为单位（id UUID PK）
  - 每个用户可挂多个 platform_id（飞书 open_id/union_id、钉钉、微信...）
  - blocked 字段统一管理黑名单；不再分两张表
  - lookup 输入 (platform, platform_id)，输出 contact（含 name + blocked）
  - 模型只读，避免 prompt injection 伪造身份

接口：
  ensure_schema()
  list_contacts(include_blocked=True)
  get_contact(id)
  create_contact(name=, notes=, platform_ids=[...])
  update_contact(id, name=?, notes=?, platform_ids=?)
  del_contact(id)
  set_blocked(id, blocked, reason="")
  lookup_name(platform, platform_id)         — 简化接口，只返回 name
  is_blocked(platform, platform_id)
"""
from domain.contacts.store import (
    ensure_schema,
    list_contacts,
    get_contact,
    create_contact,
    update_contact,
    del_contact,
    merge_contacts,
    set_blocked,
    get_or_create_stub,
    lookup_name,
    lookup_many,
    lookup_kind,
    any_id_is_bot,
    is_blocked,
    upsert_chat,
    lookup_chat,
    search_chats,
    list_chats,
)

__all__ = [
    "ensure_schema",
    "list_contacts",
    "get_contact",
    "create_contact",
    "update_contact",
    "del_contact",
    "set_blocked",
    "get_or_create_stub",
    "lookup_name",
    "lookup_many",
    "lookup_kind",
    "any_id_is_bot",
    "is_blocked",
    "upsert_chat",
    "lookup_chat",
    "search_chats",
    "list_chats",
]
