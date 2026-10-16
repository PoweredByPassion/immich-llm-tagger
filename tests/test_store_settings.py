import json
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import migrate
from app.store.settings import DEFAULTS, SettingsStore


@pytest.fixture()
def store(tmp_path: Path) -> SettingsStore:
    db = Database(tmp_path / "t.db")
    migrate(db)
    return SettingsStore(db)


def test_defaults_present_without_writing(store: SettingsStore) -> None:
    assert store.get("queue_tag_name") == "未分类"
    assert store.get("poll_interval_seconds") == 300
    assert store.get("webhook_enabled") is True
    assert store.get("active_prompt_version_id") is None


def test_defaults_table_is_complete() -> None:
    """DEFAULTS 必须与 spec §4.1 + §8.1 逐键**逐值**一致。

    只断言 key 集合是不够的：实测把 11 个默认值里的 6 个各改一次，
    整套测试仍然全绿。默认值是契约的一部分，必须钉住具体的值。
    运行时新增的内部键（非用户配置契约）记录在此，与 spec §4.1 分开：
    - last_retention_run: None（M6 retention 上次清理时间，epoch 秒）
    """
    assert DEFAULTS == {
        "last_retention_run": None,
        "queue_tag_name": "未分类",
        "webhook_enabled": True,
        "webhook_token": None,
        "poll_enabled": True,
        "poll_interval_seconds": 300,
        "default_min_confidence": 0.8,
        "fallback_tag_name": "other",
        "thumb_preset": "large",
        "thumb_quality": 85,
        "record_retention_days": 90,
        "active_prompt_version_id": None,
        "enabled_effects": ["tag", "visibility", "album", "rating", "description"],
        "skip_marker_tag_name": None,
        "apply_max_attempts": 3,
    }


def test_set_then_get_roundtrip(store: SettingsStore) -> None:
    store.set("poll_interval_seconds", 60)
    assert store.get("poll_interval_seconds") == 60


def test_values_persist_across_instances(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    migrate(db)
    SettingsStore(db).set("thumb_preset", "small")
    assert SettingsStore(db).get("thumb_preset") == "small"


def test_get_all_returns_merged_view(store: SettingsStore) -> None:
    store.set("thumb_preset", "small")
    merged = store.get_all()
    assert merged["thumb_preset"] == "small"
    assert merged["queue_tag_name"] == "未分类"


def test_unknown_key_raises(store: SettingsStore) -> None:
    with pytest.raises(KeyError):
        store.get("nope")


def test_set_unknown_key_raises(store: SettingsStore) -> None:
    """set() 与 get() 一样必须拒绝未知 key。

    否则拼错 key 会静默写入脏数据，且永远读不到（get 只认 DEFAULTS 里的 key）。
    """
    with pytest.raises(KeyError):
        store.set("nope", 1)


def test_malformed_stored_value_raises(tmp_path: Path) -> None:
    """库里若出现非 JSON 值，读取必须报错，不能静默吞成 None。

    静默返回 None 会把「配置坏了」表现成「配置是默认值」，排查时极难定位。
    """
    db = Database(tmp_path / "t.db")
    migrate(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('thumb_preset', 'not-json')"
        )
    with pytest.raises(json.JSONDecodeError):
        SettingsStore(db).get("thumb_preset")


def test_webhook_token_generated_on_demand(store: SettingsStore) -> None:
    token = store.webhook_token()
    assert len(token) >= 32
    assert store.webhook_token() == token  # 稳定，不会每次重新生成


def test_rotate_webhook_token_changes_it(store: SettingsStore) -> None:
    before = store.webhook_token()
    after = store.rotate_webhook_token()
    assert after != before
    assert store.webhook_token() == after


def test_active_prompt_version_id_roundtrip(store: SettingsStore) -> None:
    store.set_active_prompt_version(7)
    assert store.get("active_prompt_version_id") == 7
    store.set_active_prompt_version(None)
    assert store.get("active_prompt_version_id") is None


def test_new_effector_settings_have_defaults(tmp_path: Path):
    from app.db.connection import Database
    from app.db.migrate import migrate
    from app.store.settings import SettingsStore

    d = Database(tmp_path / "t.db"); migrate(d)
    s = SettingsStore(d)
    assert s.get("enabled_effects") == [
        "tag", "visibility", "album", "rating", "description"]
    assert s.get("apply_max_attempts") == 3
    # 默认 None = 「未显式配置」→ 由调用方按语言解析（§8.1 的
    # 「保留人工信息 / manual-kept」）。见下方 resolve_skip_marker_tag_name。
    assert s.get("skip_marker_tag_name") is None


def test_resolve_skip_marker_tag_name_follows_language(tmp_path: Path):
    from app.db.connection import Database
    from app.db.migrate import migrate
    from app.store.settings import SettingsStore, resolve_skip_marker_tag_name

    d = Database(tmp_path / "t.db"); migrate(d)
    s = SettingsStore(d)
    assert resolve_skip_marker_tag_name(s, "zh") == "保留人工信息"
    assert resolve_skip_marker_tag_name(s, "en") == "manual-kept"
    s.set("skip_marker_tag_name", "我改过的名字")
    assert resolve_skip_marker_tag_name(s, "en") == "我改过的名字"   # 显式配置优先


def test_enabled_effects_roundtrip_is_json(tmp_path: Path):
    from app.db.connection import Database
    from app.db.migrate import migrate
    from app.store.settings import SettingsStore

    d = Database(tmp_path / "t.db"); migrate(d)
    s = SettingsStore(d)
    s.set("enabled_effects", ["tag", "rating"])
    assert s.get("enabled_effects") == ["tag", "rating"]


def test_get_enabled_effects_returns_defensive_copy(tmp_path: Path):
    """get() 必须返回可变默认值的副本，别把模块级 DEFAULTS 交出去。

    enabled_effects 是 DEFAULTS 里第一个可变容器默认值：若直接返回共享的
    list 对象，调用方一次 in-place 修改（remove/append）就会永久污染整个
    进程的 DEFAULTS —— 之后所有实例都读到被改过的默认值。
    """
    from app.db.connection import Database
    from app.db.migrate import migrate
    from app.store.settings import DEFAULTS, SettingsStore

    d = Database(tmp_path / "t.db"); migrate(d)
    s = SettingsStore(d)
    got = s.get("enabled_effects")
    got.remove("rating")
    assert DEFAULTS["enabled_effects"] == [
        "tag", "visibility", "album", "rating", "description"]
    assert s.get("enabled_effects") == [
        "tag", "visibility", "album", "rating", "description"]


def test_get_all_returns_defensive_copy(tmp_path: Path):
    """get_all() 合并视图里的可变默认值同样必须是副本。"""
    from app.db.connection import Database
    from app.db.migrate import migrate
    from app.store.settings import DEFAULTS, SettingsStore

    d = Database(tmp_path / "t.db"); migrate(d)
    s = SettingsStore(d)
    merged = s.get_all()
    merged["enabled_effects"].append("x")
    assert DEFAULTS["enabled_effects"] == [
        "tag", "visibility", "album", "rating", "description"]
    assert s.get_all()["enabled_effects"] == [
        "tag", "visibility", "album", "rating", "description"]
