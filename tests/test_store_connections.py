from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import migrate
from app.store.connections import ImmichConn, LLMConn, ConnectionStore


@pytest.fixture()
def store(tmp_path: Path) -> ConnectionStore:
    db = Database(tmp_path / "t.db")
    migrate(db)
    return ConnectionStore(db)


def test_immich_defaults_are_empty(store: ConnectionStore) -> None:
    # 用字面量构造来钉住**每一个**字段的默认值，而不是逐字段 spot-check
    assert store.get_immich() == ImmichConn(url="", api_key="", verify_ssl=True)


def test_llm_defaults(store: ConnectionStore) -> None:
    assert store.get_llm() == LLMConn(
        base_url="", api_key="", model="",
        timeout=180, max_tokens=128000,
    )


def test_immich_roundtrip(store: ConnectionStore) -> None:
    store.set_immich(ImmichConn(url="http://immich:2283", api_key="k", verify_ssl=False))
    got = store.get_immich()
    assert got.url == "http://immich:2283"
    assert got.api_key == "k"
    assert got.verify_ssl is False


def test_llm_roundtrip(store: ConnectionStore) -> None:
    store.set_llm(LLMConn(base_url="http://llm:8000", api_key="k", model="m",
                          timeout=60, max_tokens=512))
    got = store.get_llm()
    assert got == LLMConn(base_url="http://llm:8000", api_key="k", model="m",
                          timeout=60, max_tokens=512)


def test_llm_multimodal_roundtrip(store: ConnectionStore) -> None:
    """multimodal 在 get/set 里各是一份显式列名清单，漏掉任一处都会静默丢值。"""
    store.set_llm(LLMConn(base_url="http://llm:8000", api_key="k", model="m",
                          multimodal=False))
    assert store.get_llm() == LLMConn(base_url="http://llm:8000", api_key="k",
                                      model="m", multimodal=False)


def test_llm_timeout_must_be_positive(store: ConnectionStore) -> None:
    with pytest.raises(ValueError):
        store.set_llm(LLMConn(base_url="u", api_key="k", model="m", timeout=0))


def test_llm_accepts_boundary_value_of_one(store: ConnectionStore) -> None:
    """timeout 的最小**合法**值是 1。

    守卫写成 `< 1` 才是对的；写成 `<= 1` 会把合法的 1 误拒。
    """
    store.set_llm(
        LLMConn(base_url="u", api_key="k", model="m", timeout=1)
    )
    got = store.get_llm()
    assert got.timeout == 1


def test_immich_update_changes_every_field(store: ConnectionStore) -> None:
    """第二次 set 必须更新**所有**字段。

    只 set 一次只能覆盖 INSERT 分支；`ON CONFLICT ... DO UPDATE` 的 SET 子句
    若漏掉某个字段，编辑该字段会被静默丢弃（实测：漏掉 verify_ssl 时全套测试仍绿）。
    """
    store.set_immich(ImmichConn(url="http://a", api_key="k1", verify_ssl=True))
    store.set_immich(ImmichConn(url="http://b", api_key="k2", verify_ssl=False))
    assert store.get_immich() == ImmichConn(
        url="http://b", api_key="k2", verify_ssl=False
    )


def test_llm_update_changes_every_field(store: ConnectionStore) -> None:
    """同上：llm 的 UPDATE 分支此前完全没被覆盖。"""
    store.set_llm(LLMConn(base_url="http://a", api_key="k1", model="m1",
                          timeout=60, max_tokens=100))
    store.set_llm(LLMConn(base_url="http://b", api_key="k2", model="m2",
                          timeout=120, max_tokens=200))
    assert store.get_llm() == LLMConn(
        base_url="http://b", api_key="k2", model="m2",
        timeout=120, max_tokens=200,
    )


def test_single_row_is_enforced(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    migrate(db)
    store = ConnectionStore(db)
    store.set_immich(ImmichConn(url="a", api_key="k", verify_ssl=True))
    store.set_immich(ImmichConn(url="b", api_key="k", verify_ssl=True))
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM immich_conn").fetchone()["c"]
    assert n == 1
    assert store.get_immich().url == "b"
