"""Planner 是纯函数：给定 Analysis + Snapshot + 开关，产出 Effect 列表。
不碰 DB、不碰网络、互不引用。"""
from app.classify.effectors import plan_all
from app.classify.effectors.base import EffectContext, Gates, PlanOutcome
from app.classify.parse import Classification
from app.classify.snapshot import AssetSnapshot
from app.store.tags import TagDef as StoreTagDef

# 全局效应器（settings.enabled_effects）：只有这三个。评分/描述按标签开关
# （tags.write_rating / write_description）控制，不在这个集合里。
ALL = {"tag", "visibility", "album"}


def _tag(**kw):
    base = dict(id=1, name="cat", description="", kind="primary", enabled=True,
                archive=False, album=None, min_confidence=None)
    base.update(kw)
    return StoreTagDef(**base)


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


def _plan(ctx=None, *, last_written=None, has_system_archive=False):
    """返回 (effects, outcome)。outcome 必须由测试自己持有才算得清 skipped。"""
    outcome = PlanOutcome()
    effects = plan_all(ctx or _ctx(), last_written=last_written or {},
                       outcome=outcome, has_system_archive=has_system_archive)
    return effects, outcome


def _ratings(effects):
    return [e for e in effects if e.action == "rating_set"]


def _visibility(effects):
    return [e for e in effects if e.action in ("archive", "timeline")]


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


def test_per_tag_switch_blocks_rating_and_description():
    """标签声明不写评分/描述 → 两个 effect 都不产出，但标签照常写。

    这是 v12 的语义：评分/描述由标签维度控制，不再看 enabled_effects。
    """
    effects, _ = _plan(_ctx(tag_def=_tag(write_rating=False,
                                         write_description=False)))
    assert _ratings(effects) == []
    assert not [e for e in effects if e.action == "description_set"]
    assert [e.action for e in effects if e.action == "tag_add"] == ["tag_add"]


def test_per_tag_switch_does_not_add_skip_marker():
    """不写评分 ≠ 跳过评分：用户写过值也不该被记成 skipped（那会打「保留人工信息」）。

    跳过标记的语义是"我们想写但被你写的值挡住"，这里是"压根没打算写"。
    """
    effects, outcome = _plan(_ctx(
        tag_def=_tag(write_rating=False, write_description=False),
        snapshot=AssetSnapshot(rating=5, description="我写的")))
    assert _ratings(effects) == []
    assert outcome.skipped == []


def test_tag_switches_are_independent():
    """两个开关互相独立：关评分不影响描述，反之亦然。"""
    effects, _ = _plan(_ctx(tag_def=_tag(write_rating=False)))
    assert _ratings(effects) == []
    assert len([e for e in effects if e.action == "description_set"]) == 1

    effects, _ = _plan(_ctx(tag_def=_tag(write_description=False)))
    assert len(_ratings(effects)) == 1
    assert not [e for e in effects if e.action == "description_set"]


def test_no_tag_def_keeps_writing_metadata():
    """模型给的 tag 没匹配上任何标签（tag_def=None）→ 无可读开关，照常写。"""
    effects, _ = _plan(_ctx(tag_def=None))
    assert len(_ratings(effects)) == 1
    assert len([e for e in effects if e.action == "description_set"]) == 1


def test_gate_manual_override_blocks_metadata():
    effects, _ = _plan(_ctx(gates=Gates(manual_override=True)))
    assert not [e for e in effects
                if e.action in ("rating_set", "description_set")]


def test_gate_skip_immich_blocks_everything():
    """skip_immich：连标签都不写（沿用旧 apply 路径的语义），只有队列收尾动 ——
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


# ---------------------------------------------------- visibility planner
# 语义沿用已删除的 tests/test_classify_apply.py：
#   · archive=True 且非 archive → archive（from=当前值）
#   · archive=False 且 has_system_archive 且非 timeline → timeline（from=当前值）
#   · 已是目标可见性 / 无本系统归档史 → 不产出（用户自行归档不被还原）


def test_visibility_archive_branch_from_current():
    effects, _ = _plan(_ctx(archive=True,
                            snapshot=AssetSnapshot(visibility="timeline")))
    vis = _visibility(effects)
    assert len(vis) == 1
    assert vis[0].action == "archive"
    assert vis[0].payload == {"from_visibility": "timeline"}


def test_visibility_archive_defaults_from_timeline():
    """当前可见性未知（None）→ from 兜底 timeline。"""
    effects, _ = _plan(_ctx(archive=True))          # snapshot visibility=None
    assert _visibility(effects)[0].payload == {"from_visibility": "timeline"}


def test_visibility_timeline_branch_with_system_archive():
    """本系统归档过 → archive=False 且当前非 timeline → 拉回 timeline。"""
    effects, _ = _plan(
        _ctx(archive=False, snapshot=AssetSnapshot(visibility="archive")),
        has_system_archive=True)
    vis = _visibility(effects)
    assert len(vis) == 1
    assert vis[0].action == "timeline"
    assert vis[0].payload == {"from_visibility": "archive"}


def test_visibility_timeline_defaults_from_archive():
    effects, _ = _plan(_ctx(archive=False),        # snapshot visibility=None
                       has_system_archive=True)
    assert _visibility(effects)[0].payload == {"from_visibility": "archive"}


def test_visibility_noop_when_already_archive():
    """archive=True 但当前已是 archive → 不产出（不做无用写入）。"""
    effects, _ = _plan(_ctx(archive=True,
                            snapshot=AssetSnapshot(visibility="archive")))
    assert _visibility(effects) == []


def test_visibility_noop_when_already_timeline():
    """has_system_archive 但当前已是 timeline → 不产出。"""
    effects, _ = _plan(
        _ctx(archive=False, snapshot=AssetSnapshot(visibility="timeline")),
        has_system_archive=True)
    assert _visibility(effects) == []


def test_visibility_no_timeline_without_system_archive():
    """无本系统归档史 → 即使当前是 archive 也不拉回 timeline
    （用户自行归档的图不被还原）。"""
    effects, _ = _plan(
        _ctx(archive=False, snapshot=AssetSnapshot(visibility="archive")),
        has_system_archive=False)
    assert [e for e in effects if e.action == "timeline"] == []


def test_visibility_disabled_produces_nothing():
    effects, _ = _plan(_ctx(archive=True, enabled={"tag"}),
                       has_system_archive=True)
    assert _visibility(effects) == []

