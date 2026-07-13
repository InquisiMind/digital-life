"""Smoke tests for the per-instance DB layer (v2).

Validates schema creation + core CRUD for the 4 per-instance DBs and the
``domain.persistence`` port declarations. No live-runtime dependency.
"""

from __future__ import annotations

from pathlib import Path

from domain.persistence import AuditPort, MemoryPort, VitalsPort, WorkflowPort
from infrastructure.persistence.instance.memory import MemoryDB
from infrastructure.persistence.instance.runtime_log import RuntimeLogDB
from infrastructure.persistence.instance.vitals import VitalsDB
from infrastructure.persistence.instance.workflow import WorkflowDB


def test_runtime_log_wake_turn_injection_roundtrip(tmp_path: Path) -> None:
    db = RuntimeLogDB(db_path=tmp_path / "runtime_log.db", instance_id="t1")

    wid = db.create_wake(
        meta={
            "trigger_type": "message",
            "trigger_chat_id": "oc_a",
            "system_prompt_ref": "persona/zero@abc123",
        }
    )
    assert db.next_wake_seq() == 2

    db.inject(wake_id=wid, wake_seq=1, sys_tool="system_context", content="ctx-v1")
    db.inject(wake_id=wid, wake_seq=1, sys_tool="system_context", content="ctx-v2")  # dedup
    db.inject(wake_id=wid, wake_seq=1, sys_tool="chat_stream",
              scope_id="oc_a", content="cs-v1")
    db.inject(wake_id=wid, wake_seq=1, sys_tool="chat_stream",
              scope_id="oc_a", content="cs-v2")  # append

    injections = db.list_injections(wid, before_call=0)
    kinds = sorted((i["sys_tool"], i["content"]) for i in injections)
    assert kinds == [
        ("chat_stream", "cs-v1"),
        ("chat_stream", "cs-v2"),
        ("system_context", "ctx-v2"),
    ], kinds

    db.append_turn(wake_id=wid, wake_seq=1, llm_call_seq=0, role="user", content="hi")
    db.append_turn(
        wake_id=wid, wake_seq=1, llm_call_seq=0, role="assistant",
        content=None, tool_calls=[{"id": "c1", "function": {"name": "send_msg"}}],
        finish_reason="tool_calls",
    )
    db.append_turn(
        wake_id=wid, wake_seq=1, llm_call_seq=0, role="tool",
        content="sent ok", tool_name="send_msg", tool_call_id="c1",
    )
    db.inject(wake_id=wid, wake_seq=1, sys_tool="entity_recall",
              content="recall-1", injected_before_call=1)

    db.end_wake(wid, meta_updates={"end_reason": "normal"})
    wake = db.get_wake(wid)
    assert wake["meta_json"]["end_reason"] == "normal"

    msgs = db.render_input_for_call(wid, 1, persona_loader=lambda ref: f"P[{ref}]")
    assert msgs[0]["role"] == "system"
    # fake-injection pairs: assistant_msg (placeholder, content=None) + tool_msg (real content).
    # Only tool_msg carries `name`/`content` of interest; assistant placeholders have None content.
    fake_inj_tools = [m for m in msgs if m.get("_is_fake") and m.get("role") == "tool"]
    assert any(m["name"] == "system_context" and m["content"] == "ctx-v2" for m in fake_inj_tools)
    assert any(m["role"] == "user" and m["content"] == "hi" for m in msgs)
    # call_seq=1: assistant+tool of call 0 must appear (prior output),
    # entity_recall (inj_group 1) must appear BEFORE call 1's would-be assistant.
    roles = [m["role"] for m in msgs]
    assert "assistant" in roles
    assert any(m.get("name") == "entity_recall" and m.get("content") == "recall-1" for m in msgs)
    assert msgs[-1].get("_is_fake")  # entity_recall is the last item before call 1


def test_runtime_log_meta_filter(tmp_path: Path) -> None:
    db = RuntimeLogDB(db_path=tmp_path / "runtime_log.db", instance_id="t1")
    db.create_wake(meta={"trigger_chat_id": "oc_a", "trigger_type": "message"})
    db.create_wake(meta={"trigger_chat_id": "oc_b", "trigger_type": "group_message"})
    db.create_wake(meta={"trigger_chat_id": "oc_a", "trigger_type": "group_message"})

    assert len(db.list_wakes(chat_id="oc_a")) == 2
    assert len(db.list_wakes(trigger_type="group_message")) == 2
    assert len(db.list_wakes(chat_id="oc_a", trigger_type="message")) == 1


def test_memory_db_seg_digest_upsert(tmp_path: Path) -> None:
    db = MemoryDB(db_path=tmp_path / "memory.db", instance_id="t1")
    db.upsert_seg_digest(layer="L1", period="2026-06-08T13",
                        digest="v1", wake_seq=1, wake_id=1)
    db.upsert_seg_digest(layer="L1", period="2026-06-08T13",
                        digest="v2", wake_seq=1, wake_id=1)
    rows = db.list_recent_seg_digests()
    assert len(rows) == 1
    assert rows[0]["digest"] == "v2"


def test_memory_db_slow_var_latest_only(tmp_path: Path) -> None:
    db = MemoryDB(db_path=tmp_path / "memory.db", instance_id="t1")
    db.set_slow_var(kind="consciousness", content="A")
    db.set_slow_var(kind="consciousness", content="B")
    sv = db.get_slow_var("consciousness")
    assert sv and sv["content"] == "B"


def test_memory_db_chat_fact_order(tmp_path: Path) -> None:
    db = MemoryDB(db_path=tmp_path / "memory.db", instance_id="t1")
    db.append_chat_fact(chat_id="g1", speaker="u1", text="m1", said_at=1.0)
    db.append_chat_fact(chat_id="g1", speaker="bot", text="m2", said_at=2.0)
    facts = db.list_chat_facts("g1")
    assert [f["text"] for f in facts] == ["m1", "m2"]


def test_memory_db_contact_upsert_and_lookup(tmp_path: Path) -> None:
    db = MemoryDB(db_path=tmp_path / "memory.db", instance_id="t1")
    db.upsert_contact("c1", name="张", kind="human")
    # Second upsert updates not duplicates.
    db.upsert_contact("c1", notes="销售部")
    contacts = db.list_contacts()
    assert len(contacts) == 1
    assert contacts[0]["notes"] == "销售部"

    db.link_platform("c1", "lark", "ou_abc")
    found = db.find_by_platform("lark", "ou_abc")
    assert found and found["name"] == "张"


def test_memory_db_nurture_log(tmp_path: Path) -> None:
    db = MemoryDB(db_path=tmp_path / "memory.db", instance_id="t1")
    db.log_nurture(kind="chat", deltas={"mood": 1}, raw_text="嗨", source="lark")
    rows = db.list_nurture()
    assert len(rows) == 1
    assert rows[0]["deltas_json"] == {"mood": 1}


def test_vitals_energy_and_log(tmp_path: Path) -> None:
    db = VitalsDB(db_path=tmp_path / "vitals.db", instance_id="t1")
    assert db.snapshot()["energy"] == 70.0
    db.adjust_energy(-15.0, reason="sense")
    db.adjust_energy(-5.0, reason="tool")
    assert db.snapshot()["energy"] == 50.0
    assert len(db.list_log()) == 2


def test_workflow_events_timers_affairs(tmp_path: Path) -> None:
    db = WorkflowDB(db_path=tmp_path / "workflow.db", instance_id="t1")
    db.enqueue_event("ch_a", {"x": 1}, kind="k1")
    row = db.claim_next_event("ch_a")
    assert row and row["kind"] == "k1"
    assert db.claim_next_event("ch_a") is None

    db.arm_timer("tick", "2026-06-08T10:00:00")
    fired = db.fire_due_timers(now_iso="2026-06-08T11:00:00")
    assert len(fired) == 1

    db.upsert_affair(affair_id="a1", goal="g", status="in_progress", meta_json={"k": "v"})
    db.upsert_affair(affair_id="a1", status="done")  # update only
    affs = db.list_affairs()
    assert len(affs) == 1
    assert affs[0]["status"] == "done"


def test_domain_ports_are_protocol_interfaces() -> None:
    """The 4 ports declared in domain/persistence exist and are Protocols."""
    from typing import get_type_hints
    for port in (AuditPort, MemoryPort, VitalsPort, WorkflowPort):
        # All protocols descend from typing.Protocol's runtime base.
        bases = [b.__name__ for b in port.__mro__[1:]]
        assert "Protocol" in bases, port
