from pathlib import Path

import pytest

from app.config import BootstrapConfig
from app.db.connection import Database
from app.db.migrate import migrate
from app.store.connections import ConnectionStore, ImmichConn, LLMConn
from app.store.seed import seed_from_env


def _bootstrap(**overrides) -> BootstrapConfig:
    base = dict(
        immich_url="http://immich:2283",
        immich_api_key="ik",
        llm_base_url="http://llm:8000",
        llm_api_key="lk",
        llm_model="vision-model",
        data_dir="/data",
    )
    base.update(overrides)
    return BootstrapConfig(**base)  # type: ignore[arg-type]


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "t.db")
    migrate(d)
    return d


def test_seeds_connections_when_empty(db: Database) -> None:
    seed_from_env(db, _bootstrap())
    conn = ConnectionStore(db)
    assert conn.get_immich() == ImmichConn(
        url="http://immich:2283", api_key="ik", verify_ssl=True
    )
    assert conn.get_llm() == LLMConn(
        base_url="http://llm:8000", api_key="lk", model="vision-model",
        timeout=180, max_tokens=128000,
    )


def test_does_not_overwrite_existing_values(db: Database) -> None:
    conn = ConnectionStore(db)
    conn.set_immich(ImmichConn(url="http://manual", api_key="mk"))

    seed_from_env(db, _bootstrap(immich_url="http://from-env"))

    assert ConnectionStore(db).get_immich().url == "http://manual"


def test_does_not_overwrite_when_only_api_key_is_set(db: Database) -> None:
    """只设了 api_key、url 还空着时，也必须认为「已配置」而不覆盖。

    守卫若只查 url（丢掉 `not immich.api_key`），会把手填的 key 冲掉 —— 静默数据丢失。
    """
    ConnectionStore(db).set_immich(ImmichConn(url="", api_key="MANUAL"))
    seed_from_env(
        db, _bootstrap(immich_url="http://from-env", immich_api_key="ENV")
    )
    conn = ConnectionStore(db).get_immich()
    assert conn.url == ""            # 没有被 seed 填上
    assert conn.api_key == "MANUAL"  # 关键：手填的 key 未被冲掉


def test_does_not_overwrite_when_only_llm_model_is_set(db: Database) -> None:
    """同上的 llm 侧：只设了 model、base_url 还空着。"""
    ConnectionStore(db).set_llm(LLMConn(base_url="", api_key="", model="MANUAL"))
    seed_from_env(
        db, _bootstrap(llm_base_url="http://from-env", llm_model="ENV")
    )
    assert ConnectionStore(db).get_llm().model == "MANUAL"


def test_seeds_when_only_url_is_provided_in_env(db: Database) -> None:
    """只给了 URL、没给 API Key / model 时不能崩。

    `from_env()` 对未设置的变量返回 None，而 api_key / model 列是 NOT NULL。
    实现里的 `bootstrap.immich_api_key or ""` 正是为此 —— 去掉它，
    在只配了 IMMICH_URL 的部署（compose 里很常见）上首次启动会抛
    `sqlite3.IntegrityError: NOT NULL constraint failed`。
    """
    seed_from_env(db, _bootstrap(immich_api_key=None, llm_model=None))
    conn = ConnectionStore(db)
    assert conn.get_immich().api_key == ""
    assert conn.get_llm().model == ""


def test_second_seed_is_noop(db: Database) -> None:
    seed_from_env(db, _bootstrap(immich_url="http://first"))
    seed_from_env(db, _bootstrap(immich_url="http://second"))
    assert ConnectionStore(db).get_immich().url == "http://first"


def test_seeds_only_the_empty_side(db: Database) -> None:
    """llm 已配置、immich 为空时，immich 仍要被 seed，llm 不受影响。

    守卫作用域若写错（immich 侧额外要求 llm 也为空），这个场景会静默漏 seed。
    """
    ConnectionStore(db).set_llm(
        LLMConn(base_url="http://manual", api_key="mk", model="manual")
    )
    assert seed_from_env(db, _bootstrap()) is True
    conn = ConnectionStore(db)
    assert conn.get_immich().url == "http://immich:2283"
    assert conn.get_llm().base_url == "http://manual"   # 未被覆盖


def test_env_without_llm_leaves_llm_empty_but_reports_seeded(db: Database) -> None:
    """env 只给了 immich：immich 写入、llm 保持空，且 wrote 仍为 True。

    钉住「只 seed 一侧时返回值的单侧语义」—— 两块 `wrote = True` 各删一个都要被发现。
    """
    assert seed_from_env(db, _bootstrap(llm_base_url=None)) is True
    conn = ConnectionStore(db)
    assert conn.get_immich().url == "http://immich:2283"
    assert conn.get_llm() == LLMConn()


def test_env_without_immich_leaves_immich_empty_but_reports_seeded(db: Database) -> None:
    """env 只给了 llm：llm 写入、immich 保持空，且 wrote 仍为 True。

    与上一个测试对称，专门钉住 llm 块的 `wrote = True`。只删它时，只配了 LLM 的
    部署（immich 手填）会 seed 成功却返回 False，Task 10 会误报「未 seed」——
    实测：缺这个对称用例时，删 llm 块的 `wrote = True` 全套测试仍然通过。
    """
    assert seed_from_env(db, _bootstrap(immich_url=None)) is True
    conn = ConnectionStore(db)
    assert conn.get_llm().base_url == "http://llm:8000"
    assert conn.get_immich() == ImmichConn()


def test_skips_blank_env_values(db: Database) -> None:
    """env 为空时**什么都不该写**。

    这里把两侧的**触发字段**（immich_url / llm_base_url）都置空；api_key 仍保留
    非空值（"ik"），这样「守卫被改坏成照样写」时会写出非空 api_key 而被这组
    整对象比对抓到。断言必须覆盖**全部字段**：旧版只查 url / model，而这两处在
    「什么都没写」和「写了空值」两种情况下都是空的 —— 守卫被改坏时测试仍通过。
    """
    seed_from_env(db, _bootstrap(immich_url="", llm_base_url=""))
    conn = ConnectionStore(db)
    assert conn.get_immich() == ImmichConn()
    assert conn.get_llm() == LLMConn()


def test_returns_whether_anything_was_seeded(db: Database) -> None:
    assert seed_from_env(db, _bootstrap()) is True
    assert seed_from_env(db, _bootstrap()) is False


def test_from_env_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """六个环境变量名都必须钉住 —— 拼错一个就会在生产上静默拿不到配置。"""
    monkeypatch.setenv("IMMICH_URL", "http://immich")
    monkeypatch.setenv("IMMICH_API_KEY", "ik")
    monkeypatch.setenv("LLM_BASE_URL", "http://llm")
    monkeypatch.setenv("LLM_API_KEY", "lk")
    monkeypatch.setenv("LLM_MODEL", "vision")
    monkeypatch.setenv("DATA_DIR", "/tmp/xyz")
    cfg = BootstrapConfig.from_env()
    assert cfg.immich_url == "http://immich"
    assert cfg.immich_api_key == "ik"
    assert cfg.llm_base_url == "http://llm"
    assert cfg.llm_api_key == "lk"
    assert cfg.llm_model == "vision"
    assert cfg.data_dir == "/tmp/xyz"


def test_from_env_returns_none_for_unset_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("IMMICH_URL", "IMMICH_API_KEY", "LLM_BASE_URL",
                 "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    cfg = BootstrapConfig.from_env()
    assert cfg.immich_url is None
    assert cfg.immich_api_key is None
    assert cfg.llm_base_url is None
    assert cfg.llm_api_key is None
    assert cfg.llm_model is None


def test_from_env_defaults_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert BootstrapConfig.from_env().data_dir == "/data"


def test_db_path_joins_data_dir() -> None:
    """db_path 是 Task 10 的 app 工厂用来定位库文件的公开契约。

    它若与 migrate() 打开的路径不一致，就会出现两个不同的库文件。
    """
    assert BootstrapConfig(data_dir="/data").db_path == Path("/data/autotag.db")
    assert BootstrapConfig(data_dir="/tmp/x").db_path == Path("/tmp/x/autotag.db")
    # dataclass 自身的默认值，与 from_env 的兜底是**两处**，都要钉
    assert BootstrapConfig().data_dir == "/data"
