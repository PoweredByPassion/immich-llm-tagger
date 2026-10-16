"""Planner 是纯函数：给定 Analysis + Snapshot + 开关，产出 Effect 列表。
不碰 DB、不碰网络、互不引用。"""
from app.classify.effectors import plan_all
from app.classify.effectors.base import EffectContext, Gates, PlanOutcome
from app.classify.parse import Classification
from app.classify.snapshot import AssetSnapshot

ALL = {"tag", "visibility", "album", "rating", "description"}


def _ctx(**kw):
    analysis = kw.pop("analysis", Classification(
        tag="cat", confidence=0.9, sensitive=False, reason=None, markers=(),
        rating=4, description="一只猫"))
    snapshot = kw.pop("snapshot", AssetSnapshot())
    return EffectContext(
        asset_id="a1", analysis=analysis, snapshot=snapshot,
        tag_def=kw.pop("tag_def", None), archive=kw.pop("archive", False),
        enabled=kw.pop("enabled", set(ALL)),
        gates=kw.pop("gates", Gates()),
    )


def _plan(ctx=None, *, last_written=None):
    """返回 (effects, outcome)。outcome 必须由测试自己持有才算得清 skipped。"""
    outcome = PlanOutcome()
    effects = plan_all(ctx or _ctx(), last_written=last_written or {},
                       outcome=outcome)
    return effects, outcome


def _ratings(effects):
    return [e for e in effects if e.action == "rating_set"]


def test_rating_written_when_empty():
    effects, _ = _plan()
    assert len(_ratings(effects)) == 1
    assert _ratings(effects)[0].payload == {"old": None, "new": 4}


def test_rating_skipped_when_user_wrote_it():
    """用户手写 → 跳过 + 记 skipped（提交器据此打标记）。"""
    effects, outcome = _plan(_ctx(snapshot=AssetSnapshot(rating=5)))
    assert _ratings(effects) == []
    assert "rating" in outcome.skipped


def test_rating_not_skipped_when_value_is_ours():
    """当前值 == 我们上次写的 new → 是我们写的：跳过写入但不打标记。"""
    effects, outcome = _plan(_ctx(snapshot=AssetSnapshot(rating=4)),
                             last_written={"rating_set": 4})
    assert _ratings(effects) == []
    assert outcome.skipped == []


def test_explicit_reclassify_refreshes_our_own_value():
    effects, _ = _plan(
        _ctx(snapshot=AssetSnapshot(rating=3),
             gates=Gates(allow_metadata_overwrite=True)),
        last_written={"rating_set": 3})
    assert _ratings(effects)[0].payload == {"old": 3, "new": 4}


def test_disabled_effector_produces_nothing():
    effects, _ = _plan(_ctx(enabled={"tag"}))
    assert not [e for e in effects if e.action.startswith("rating")]
    assert not [e for e in effects if e.action.startswith("description")]


def test_gate_manual_override_blocks_metadata():
    effects, _ = _plan(_ctx(gates=Gates(manual_override=True)))
    assert not [e for e in effects
                if e.action in ("rating_set", "description_set")]


def test_gate_skip_immich_blocks_everything():
    """skip_immich：连标签都不写（现有 apply_plan 的语义），只有队列收尾动 ——
    而队列收尾由提交器的 Finalizer 负责，不在 Effect 列表里。"""
    effects, _ = _plan(_ctx(gates=Gates(skip_immich=True)))
    assert effects == []


def test_dry_run_gate_blocks_metadata():
    effects, _ = _plan(_ctx(gates=Gates(dry_run=True)))
    assert not [e for e in effects
                if e.action in ("rating_set", "description_set")]


def test_description_skipped_when_user_wrote_it():
    effects, outcome = _plan(_ctx(snapshot=AssetSnapshot(description="我写的")))
    assert not [e for e in effects if e.action == "description_set"]
    assert "description" in outcome.skipped
