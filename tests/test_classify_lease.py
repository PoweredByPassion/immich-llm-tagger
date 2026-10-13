"""Task 5: 资产租约的测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.classify.lease import claim, lease_seconds_for, release, renew
from app.db.connection import Database
from app.db.migrate import migrate


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    migrate(d)
    return d


def _row(db: Database, asset_id: str):
    with db.connect() as conn:
        return conn.execute(
            "SELECT lease_owner, lease_expires_at FROM asset_state WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()


# 1. 全新 asset：claim 返回 True（忘了先 INSERT 会失败）
def test_claim_fresh_asset_returns_true(db: Database) -> None:
    assert claim(db, "asset-1", owner=1, lease_seconds=300) is True
    row = _row(db, "asset-1")
    assert row is not None
    assert row["lease_owner"] == 1
    assert row["lease_expires_at"] is not None


# 2. 同一 owner 重复 claim：刷新自己的租约，返回 True（幂等续期语义）
def test_claim_again_by_same_owner_refreshes(db: Database) -> None:
    assert claim(db, "asset-1", owner=1, lease_seconds=300) is True
    first = _row(db, "asset-1")["lease_expires_at"]
    assert claim(db, "asset-1", owner=1, lease_seconds=300) is True
    assert _row(db, "asset-1")["lease_expires_at"] >= first


# 3. 别人持有未过期租约时，claim 返回 False
def test_claim_blocked_by_other_owner_live_lease(db: Database) -> None:
    assert claim(db, "asset-1", owner=1, lease_seconds=300) is True
    assert claim(db, "asset-1", owner=2, lease_seconds=300) is False
    assert _row(db, "asset-1")["lease_owner"] == 1


# 4. 租约已过期时，另一个 owner 可以抢到
def test_claim_after_lease_expired(db: Database) -> None:
    assert claim(db, "asset-1", owner=1, lease_seconds=300) is True
    # 把过期时间直接设到过去，模拟时间流逝
    with db.connect() as conn:
        conn.execute(
            "UPDATE asset_state SET lease_expires_at = datetime('now', '-1 second') "
            "WHERE asset_id = 'asset-1'"
        )
    assert claim(db, "asset-1", owner=2, lease_seconds=300) is True
    assert _row(db, "asset-1")["lease_owner"] == 2


# 5. release 只对自己的 owner 生效
def test_release_wrong_owner_is_noop(db: Database) -> None:
    claim(db, "asset-1", owner=1, lease_seconds=300)
    release(db, "asset-1", owner=2)
    row = _row(db, "asset-1")
    assert row["lease_owner"] == 1
    assert row["lease_expires_at"] is not None
    release(db, "asset-1", owner=1)
    row = _row(db, "asset-1")
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None


# 6. renew 延长过期时间；错误 owner 返回 False
def test_renew_extends_only_for_owner(db: Database) -> None:
    claim(db, "asset-1", owner=1, lease_seconds=300)
    assert renew(db, "asset-1", owner=2, lease_seconds=300) is False
    assert _row(db, "asset-1")["lease_owner"] == 1
    assert renew(db, "asset-1", owner=1, lease_seconds=600) is True
    row = _row(db, "asset-1")
    assert row["lease_owner"] == 1
    # 续租 600s > 300s，过期时间应明显在未来
    with db.connect() as conn:
        left = conn.execute(
            "SELECT CAST((julianday(lease_expires_at) - julianday('now')) * 86400 AS INT) "
            "FROM asset_state WHERE asset_id = 'asset-1'"
        ).fetchone()[0]
    assert 590 < left <= 600


def test_renew_fresh_asset_fails(db: Database) -> None:
    # 不存在的 asset 无人持有租约，renew 不能凭空创建租约
    assert renew(db, "ghost", owner=1, lease_seconds=300) is False


# 7. lease_expires_at 符合 GLOB 格式（CHECK 约束通过；isoformat 会被挡）
def test_lease_timestamp_format_passes_check(db: Database) -> None:
    claim(db, "asset-1", owner=1, lease_seconds=300)
    row = _row(db, "asset-1")
    assert row["lease_expires_at"] is not None
    with db.connect() as conn:
        # isoformat 的 T 形式应被 CHECK 挡下
        with pytest.raises(Exception):
            conn.execute(
                "UPDATE asset_state SET lease_expires_at = ? WHERE asset_id = 'asset-1'",
                (__import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),),
            )


# 8. lease_seconds_for 公式的确切数值
@pytest.mark.parametrize(
    ("llm_timeout", "max_retries", "expected"),
    [
        (30, 4, 270),   # 默认 retries=4
        (10, 0, 130),
        (60, 2, 300),
        (0, 4, 120),
    ],
)
def test_lease_seconds_for_formula(llm_timeout: int, max_retries: int, expected: int) -> None:
    assert lease_seconds_for(llm_timeout, max_retries) == expected


def test_llm_max_retries_constant_matches_llm_client_default() -> None:
    from app.classify.lease import LLM_MAX_RETRIES
    from app.clients.llm import LLMClient
    import inspect

    sig = inspect.signature(LLMClient.chat)
    assert sig.parameters["retries"].default == LLM_MAX_RETRIES
