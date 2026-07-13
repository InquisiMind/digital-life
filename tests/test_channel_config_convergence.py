"""通道配置收敛后的回归测试。

覆盖的核心契约：
  1. ``parse_channels`` 只读 ``channels.<type>`` 段（不再兜底 messenger）
  2. ``channel_signatures`` 把凭据/域名变化映射成可 diff 的签名
  3. ConfigCenter 写入路径已统一到 ``channels.feishu.*`` / ``channels.wechat.*``
  4. 迁移脚本把 ``messenger`` 段搬到 ``channels.<type>``（幂等 + 占位清理）

历史背景：飞书 App ID 路径不一致曾导致「控制台改了不生效」的 bug（写入 messenger
侧、读取 channels 侧，parse_channels 的合并被占位 channels.feishu 短路）。
"""
from __future__ import annotations

from pathlib import Path

import yaml


# ─────────────────────────────────────────────────────────────────────
# parse_channels / channel_signatures
# ─────────────────────────────────────────────────────────────────────

def test_parse_channels_empty_returns_empty():
    from interfaces.ingress.registry import parse_channels
    assert parse_channels({}) == {}
    assert parse_channels(None) == {}  # type: ignore[arg-type]


def test_parse_channels_only_channels_section_messenger_ignored():
    """收敛后的核心契约：messenger 段不再兜底——只能从 channels 段取通道。"""
    from interfaces.ingress.registry import parse_channels
    cfg = {
        "channels": {
            "feishu": {"type": "feishu", "app_id": "cli_xxx", "feishu_domain": "https://x"},
        },
        "messenger": {"type": "feishu", "app_id": "cli_stale_should_be_ignored"},
    }
    result = parse_channels(cfg)
    assert set(result.keys()) == {"feishu"}
    assert result["feishu"]["app_id"] == "cli_xxx"


def test_parse_channels_skips_non_dict_channel_entries():
    from interfaces.ingress.registry import parse_channels
    cfg = {"channels": {"feishu": "garbage_string", "wechat": {"type": "wechat_clawbot"}}}
    result = parse_channels(cfg)
    assert set(result.keys()) == {"wechat"}


def test_parse_channels_no_channels_section_returns_empty():
    """旧实例若只有 messenger 段、先跑过迁移：parse 后应为空（不再兜底）。

    这正是迁移脚本存在的理由——保证旧实例升级后飞书通道不丢。
    """
    from interfaces.ingress.registry import parse_channels
    cfg = {"messenger": {"type": "feishu", "app_id": "cli_xxx"}}
    assert parse_channels(cfg) == {}


def test_channel_signatures_feishu_changes_on_secret():
    from interfaces.ingress.registry import channel_signatures
    cfg = {"channels": {"feishu": {"type": "feishu", "app_id": "cli_a"}}}
    sig1 = channel_signatures(cfg, {"FEISHU_APP_SECRET": "s1"})
    sig2 = channel_signatures(cfg, {"FEISHU_APP_SECRET": "s2"})
    assert "feishu" in sig1
    assert sig1["feishu"] != sig2["feishu"]


def test_channel_signatures_includes_domain_change():
    from interfaces.ingress.registry import channel_signatures
    base_cfg = {
        "channels": {
            "feishu": {"type": "feishu", "app_id": "cli_a", "feishu_domain": "https://open.feishu.cn"}
        }
    }
    intl_cfg = {
        "channels": {
            "feishu": {"type": "feishu", "app_id": "cli_a", "feishu_domain": "https://open.larksuite.com"}
        }
    }
    secrets = {"FEISHU_APP_SECRET": "s1"}
    assert channel_signatures(base_cfg, secrets) != channel_signatures(intl_cfg, secrets)


def test_channel_signatures_skips_incomplete_credentials():
    """缺失 app_id 或 secret 的通道不算入签名（与 _build_feishu 的 None 跳过策略一致）。"""
    from interfaces.ingress.registry import channel_signatures
    cfg = {"channels": {"feishu": {"type": "feishu", "app_id": ""}}}
    assert channel_signatures(cfg, {"FEISHU_APP_SECRET": "s"}) == {}


def test_channel_signatures_wechat_includes_token_and_bot_id():
    from interfaces.ingress.registry import channel_signatures
    cfg = {"channels": {"wechat": {"type": "wechat_clawbot", "domain": "https://x", "bot_id": "b1"}}}
    sig1 = channel_signatures(cfg, {"WECHAT_BOT_TOKEN": "t1"})
    sig2 = channel_signatures(cfg, {"WECHAT_BOT_TOKEN": "t2"})
    assert sig1["wechat"] != sig2["wechat"]


# ─────────────────────────────────────────────────────────────────────
# _build_feishu / _build_wechat_clawbot（从 standard 路径读取）
# ─────────────────────────────────────────────────────────────────────

def test_build_feishu_reads_app_id_from_channels(monkeypatch):
    """工厂应从 channels.feishu.app_id 读 app_id，缺失返回 None。"""
    from interfaces.ingress import registry as reg

    # 验证 builder 的判断逻辑：缺 secret 返回 None，不调用 FeishuAdapter
    builder = reg._build_feishu
    assert builder({"app_id": "cli_a"}, {}) is None  # 缺 secret
    assert builder({"app_id": ""}, {"FEISHU_APP_SECRET": "s"}) is None  # 缺 app_id
    assert builder({}, {}) is None  # 全空


# ─────────────────────────────────────────────────────────────────────
# ConfigCenter 写入侧：path 已对齐 channels.<type>.<field>
# ─────────────────────────────────────────────────────────────────────

def _config_paths(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """构造一个空实例的 config/env 路径，patch 掉 config_center 的依赖。"""
    iid = "alpha-test"
    config_dir = tmp_path / "apps" / iid / "config"
    config_dir.mkdir(parents=True)
    yaml_path = config_dir / "app.yaml"
    env_path = config_dir / "secrets.env"
    yaml_path.write_text(
        "display_name: alpha\n"
        "channels:\n"
        "  feishu:\n"
        "    type: feishu\n"
        "    app_id: \"{old}\"\n",
        encoding="utf-8",
    )
    env_path.write_text("FEISHU_APP_SECRET=old_secret\n", encoding="utf-8")
    monkeypatch.setattr(
        "application.console.config_center.get_instance_config_path",
        lambda employee_id=None: yaml_path,
    )
    monkeypatch.setattr(
        "application.console.config_center.get_instance_env_path",
        lambda employee_id=None: env_path,
    )
    return yaml_path, env_path


def test_config_center_writes_app_id_to_channels_feishu(tmp_path, monkeypatch):
    """ConfigField.key 仍是 messenger.app_id（前端标识符），但 path 写到 channels.feishu.app_id。"""
    from application.console.config_center import ConfigCenterWorkflow

    yaml_path, _ = _config_paths(tmp_path, monkeypatch)
    wf = ConfigCenterWorkflow()
    result = wf.update_config({"values": {"messenger.app_id": "cli_NEW_VALUE"}}, "alpha-test")
    assert result.status_code == 200

    written = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert written["channels"]["feishu"]["app_id"] == "cli_NEW_VALUE"
    # 反向验证：messenger 段不应该被写回（除非它原本就有）
    assert "messenger" not in written


def test_config_center_reads_app_id_from_channels_feishu(tmp_path, monkeypatch):
    """读路径同样对齐——控制台显示从 channels.feishu.app_id 取值。"""
    from application.console.config_center import ConfigCenterWorkflow

    yaml_path, _ = _config_paths(tmp_path, monkeypatch)
    yaml_path.write_text(
        "display_name: alpha\n"
        "channels:\n"
        "  feishu:\n"
        "    type: feishu\n"
        "    app_id: cli_from_channels\n"
        "    feishu_domain: https://open.feishu.cn\n",
        encoding="utf-8",
    )
    wf = ConfigCenterWorkflow()
    result = wf.config("alpha-test")
    feishu_section = next(s for s in result.payload["sections"] if s["key"] == "feishu")
    app_id_field = next(f for f in feishu_section["fields"] if f["key"] == "messenger.app_id")
    assert app_id_field["value"] == "cli_from_channels"
    assert app_id_field["origin"] == "local.yaml"


def test_config_center_writes_wechat_domain_to_channels_wechat_domain(tmp_path, monkeypatch):
    """微信域名字段：messenger.wechat_domain（key）→ channels.wechat.domain（path）。"""
    from application.console.config_center import ConfigCenterWorkflow

    yaml_path, _ = _config_paths(tmp_path, monkeypatch)
    wf = ConfigCenterWorkflow()
    result = wf.update_config(
        {"values": {"messenger.wechat_domain": "https://new.example.com"}},
        "alpha-test",
    )
    assert result.status_code == 200
    written = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert written["channels"]["wechat"]["domain"] == "https://new.example.com"


# ─────────────────────────────────────────────────────────────────────
# 迁移脚本纯函数
# ─────────────────────────────────────────────────────────────────────

def test_migrate_yaml_basic_messenger_to_channels():
    from scripts.migrate_messenger_to_channels import migrate_yaml

    cfg = {
        "display_name": "alpha",
        "messenger": {
            "type": "feishu",
            "app_id": "cli_a928",
            "feishu_domain": "https://open.feishu.cn",
        },
        "channels": {
            "wechat": {"type": "wechat_clawbot", "domain": "https://x"},
        },
    }
    new_cfg, report = migrate_yaml(cfg)
    assert report.changed
    assert new_cfg["channels"]["feishu"]["app_id"] == "cli_a928"
    assert new_cfg["channels"]["feishu"]["feishu_domain"] == "https://open.feishu.cn"
    assert new_cfg["channels"]["feishu"]["type"] == "feishu"
    assert new_cfg["channels"]["wechat"]["domain"] == "https://x"
    assert "messenger" not in new_cfg


def test_migrate_yaml_idempotent_second_run_noop():
    from scripts.migrate_messenger_to_channels import migrate_yaml

    cfg = {"messenger": {"app_id": "cli_x"}}
    new_cfg, _ = migrate_yaml(cfg)
    new_cfg2, report = migrate_yaml(new_cfg)
    assert not report.changed
    assert "messenger" not in new_cfg2


def test_migrate_yaml_placeholder_in_channels_replaced_by_real_messenger_value():
    """关键场景：init_instance 模板默认 channels.feishu.app_id 是占位 cli_xxxxxxxxx，
    用户随后在控制台把真值改写到 messenger.app_id（旧 bug 路径）。迁移时应清占位、
    用 messenger 真值替换——而不是被占位短路。
    """
    from scripts.migrate_messenger_to_channels import migrate_yaml

    cfg = {
        "channels": {
            "feishu": {
                "type": "feishu",
                "app_id": "cli_xxxxxxxxx",  # 占位
                "feishu_domain": "https://open.feishu.cn",
            }
        },
        "messenger": {"app_id": "cli_real_from_console"},
    }
    new_cfg, report = migrate_yaml(cfg)
    assert new_cfg["channels"]["feishu"]["app_id"] == "cli_real_from_console"
    assert any("占位→真值" in s for s in report.placeholders_cleaned)


def test_migrate_yaml_preserves_user_hand_edited_real_value():
    """channels.feishu.app_id 已是真值（用户在 channels 段手动配的）时，
    不应被 messenger.app_id 覆盖——保留生效路径优先。"""
    from scripts.migrate_messenger_to_channels import migrate_yaml

    cfg = {
        "channels": {"feishu": {"type": "feishu", "app_id": "cli_channels_real"}},
        "messenger": {"app_id": "cli_messenger_real"},
    }
    new_cfg, report = migrate_yaml(cfg)
    assert new_cfg["channels"]["feishu"]["app_id"] == "cli_channels_real"
    assert report.warnings  # 警告冲突


def test_migrate_yaml_merges_chat_ids():
    from scripts.migrate_messenger_to_channels import migrate_yaml

    cfg = {
        "channels": {"feishu": {"type": "feishu", "chat_ids": [{"chat_id": "oc_a"}]}},
        "messenger": {"chat_ids": [{"chat_id": "oc_a"}, {"chat_id": "oc_b"}]},
    }
    new_cfg, report = migrate_yaml(cfg)
    merged = new_cfg["channels"]["feishu"]["chat_ids"]
    # 去重后保留 oc_a，补入 oc_b
    assert {"chat_id": "oc_a"} in merged
    assert {"chat_id": "oc_b"} in merged
    assert sum(1 for x in merged if x == {"chat_id": "oc_a"}) == 1


def test_migrate_yaml_wechat_domain_field_rename():
    """messenger.wechat_domain 迁到 channels.wechat.domain（字段名也变）。"""
    from scripts.migrate_messenger_to_channels import migrate_yaml

    cfg = {
        "messenger": {"wechat_domain": "https://ilinkai.weixin.qq.com"},
        "channels": {},
    }
    new_cfg, _ = migrate_yaml(cfg)
    assert new_cfg["channels"]["wechat"]["domain"] == "https://ilinkai.weixin.qq.com"
    assert new_cfg["channels"]["wechat"]["type"] == "wechat_clawbot"


def test_migrate_instance_dry_run_does_not_write(tmp_path, monkeypatch):
    from scripts.migrate_messenger_to_channels import migrate_instance

    inst_dir = tmp_path / "apps" / "alpha"
    (inst_dir / "config").mkdir(parents=True)
    yaml_path = inst_dir / "config" / "app.yaml"
    yaml_path.write_text("messenger:\n  app_id: cli_x\n", encoding="utf-8")
    before = yaml_path.read_text(encoding="utf-8")
    report = migrate_instance(inst_dir, apply=False)
    assert report.changed  # dry-run 也报告了会改什么
    assert not (inst_dir / "config" / "app.yaml.bak").exists()
    # dry-run：磁盘文件不变
    assert yaml_path.read_text(encoding="utf-8") == before


def test_migrate_instance_apply_writes_and_backs_up(tmp_path):
    from scripts.migrate_messenger_to_channels import migrate_instance

    inst_dir = tmp_path / "apps" / "alpha"
    (inst_dir / "config").mkdir(parents=True)
    yaml_path = inst_dir / "config" / "app.yaml"
    yaml_path.write_text(
        "messenger:\n  app_id: cli_x\n", encoding="utf-8"
    )
    report = migrate_instance(inst_dir, apply=True)
    assert report.changed
    assert report.backed_up
    assert (inst_dir / "config" / "app.yaml.bak").exists()
    written = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert written["channels"]["feishu"]["app_id"] == "cli_x"
    assert "messenger" not in written
