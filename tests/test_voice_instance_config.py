"""实例级语音凭据（管理台「语音对话」段 → app.yaml perception.stream_asr）。

覆盖三件事：
1. voice_output._xfyun_creds 实例级优先、残缺回退全局 voice_sense.yaml；
2. 管理台 schema（voice section + secret 掩码）；
3. update_config 落盘 dotted path + secret 留空保留。

全部走 tmp_path，绝不触碰真实 apps/。
"""

from __future__ import annotations

import yaml

import application.console.config_center as cc
import infrastructure.config as ic
import infrastructure.perception.voice_output as vo


def _write_yaml(path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


# ────────────────────────── _xfyun_creds 解析链 ──────────────────────────

def test_xfyun_creds_instance_first(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "get_project_root", lambda: tmp_path)
    _write_yaml(
        tmp_path / "apps" / "iid" / "config" / "app.yaml",
        {"perception": {"stream_asr": {"app_id": "a1", "api_key": "k1", "api_secret": "s1"}}},
    )
    assert vo._xfyun_creds("iid") == ("a1", "k1", "s1")


def test_xfyun_creds_partial_instance_falls_back_global(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "get_project_root", lambda: tmp_path)
    _write_yaml(
        tmp_path / "apps" / "iid" / "config" / "app.yaml",
        {"perception": {"stream_asr": {"app_id": "a1"}}},  # 缺 key/secret → 不生效
    )
    _write_yaml(
        tmp_path / "config" / "voice_sense.yaml",
        {"web_voice": {"stream_asr": {"app_id": "g1", "api_key": "gk", "api_secret": "gs"}}},
    )
    assert vo._xfyun_creds("iid") == ("g1", "gk", "gs")


def test_xfyun_creds_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "get_project_root", lambda: tmp_path)
    assert vo._xfyun_creds("ghost") == ("", "", "")


def test_xfyun_creds_none_resolves_global(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "get_project_root", lambda: tmp_path)
    _write_yaml(
        tmp_path / "config" / "voice_sense.yaml",
        {"web_voice": {"stream_asr": {"app_id": "g1", "api_key": "gk", "api_secret": "gs"}}},
    )
    assert vo._xfyun_creds(None) == ("g1", "gk", "gs")


# ────────────────────────── 管理台 schema ──────────────────────────

def _voice_fields() -> dict[str, cc.ConfigField]:
    return {f.key: f for f in cc.FIELDS if f.section == "voice"}


def test_voice_section_registered():
    assert "voice" in cc.SECTION_META
    fields = _voice_fields()
    assert {"voice.stream_asr_provider", "voice.stream_asr_app_id",
            "voice.xfyun_api_key", "voice.xfyun_api_secret"} <= set(fields)
    # secret 凭据落 yaml dotted path（读取侧 load_config / _xfyun_creds 同源）
    assert fields["voice.xfyun_api_key"].path == "perception.stream_asr.api_key"
    assert fields["voice.xfyun_api_key"].secret is True
    assert fields["voice.stream_asr_app_id"].restart_required is False


def test_voice_secret_masked_in_payload():
    wf = cc.ConfigCenterWorkflow()
    ycfg = {"perception": {"stream_asr": {"app_id": "a1", "api_key": "k1", "api_secret": "s1"}}}
    app_id_payload = wf._field_payload(_voice_fields()["voice.stream_asr_app_id"], {}, ycfg)
    key_payload = wf._field_payload(_voice_fields()["voice.xfyun_api_key"], {}, ycfg)
    assert app_id_payload["value"] == "a1"
    assert key_payload["value"] == ""           # secret 不回传明文
    assert key_payload["configured"] is True    # 但「已配置」状态可见


# ────────────────────────── update_config 落盘 ──────────────────────────

def test_update_config_writes_and_preserves_blank_secret(tmp_path, monkeypatch):
    app_yaml = tmp_path / "apps" / "iid" / "config" / "app.yaml"
    monkeypatch.setattr(cc, "get_instance_config_path", lambda eid=None: app_yaml)
    monkeypatch.setattr(cc, "get_instance_env_path", lambda eid=None: tmp_path / "secrets.env")

    wf = cc.ConfigCenterWorkflow()
    # 第一笔：填 app_id，两个 secret 留空 → 不得产生空 key
    wf.update_config({"values": {
        "voice.stream_asr_app_id": "myapp",
        "voice.xfyun_api_key": "",
        "voice.xfyun_api_secret": None,
    }}, "iid")
    cfg = yaml.safe_load(app_yaml.read_text(encoding="utf-8"))
    sa = cfg["perception"]["stream_asr"]
    assert sa["app_id"] == "myapp"
    assert "api_key" not in sa and "api_secret" not in sa

    # 第二笔：填真实 secret（模拟管理台输入凭据）
    wf.update_config({"values": {
        "voice.xfyun_api_key": "k9",
        "voice.xfyun_api_secret": "s9",
    }}, "iid")
    sa = yaml.safe_load(app_yaml.read_text(encoding="utf-8"))["perception"]["stream_asr"]
    assert sa["api_key"] == "k9" and sa["api_secret"] == "s9"


def test_written_creds_flow_into_xfyun_creds(tmp_path, monkeypatch):
    """端到端：管理台写入 → _xfyun_creds 同一实例读到（ASR/TTS 同源）。"""
    monkeypatch.setattr(ic, "get_project_root", lambda: tmp_path)
    app_yaml = tmp_path / "apps" / "iid" / "config" / "app.yaml"
    monkeypatch.setattr(cc, "get_instance_config_path", lambda eid=None: app_yaml)
    monkeypatch.setattr(cc, "get_instance_env_path", lambda eid=None: tmp_path / "secrets.env")

    cc.ConfigCenterWorkflow().update_config({"values": {
        "voice.stream_asr_app_id": "a9",
        "voice.xfyun_api_key": "k9",
        "voice.xfyun_api_secret": "s9",
    }}, "iid")
    assert vo._xfyun_creds("iid") == ("a9", "k9", "s9")
