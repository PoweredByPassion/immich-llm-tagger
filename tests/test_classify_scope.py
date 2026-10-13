"""Task 4: Scope 匹配器 + 队列扫描的测试。

覆盖计划的 4 条失败模式 + 变异清单。
FakeClient 参照 tests/test_client_immich.py 的真实形状（id 是 UUID 对象）。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.classify.scope import ScopeRule, in_scope, load_scope_rules, scan_queue_assets
from app.db.connection import Database
from app.db.migrate import migrate


# --------------------------------------------------------------- fixtures


@pytest.fixture()
def db(tmp_path):
    db = Database(tmp_path / "test.db")
    migrate(db)
    return db


def rule(library_id="", path_prefix="", enabled=True):
    return ScopeRule(library_id=library_id, path_prefix=path_prefix, enabled=enabled)


class FakeClient:
    """记录 ensure_tags / iter_assets 的入参，返回预置资产。"""

    def __init__(self, assets, tag_id="11111111-1111-1111-1111-000000000001"):
        self.assets = assets
        self.tag_id = tag_id
        self.ensure_tags_calls: list[list[str]] = []
        self.search_dtos = []

    def ensure_tags(self, names):
        self.ensure_tags_calls.append(list(names))
        return {n: self.tag_id for n in names}

    def iter_assets(self, dto, size=1000):
        self.search_dtos.append(dto)
        yield from self.assets


def asset(aid=None, library_id=None, original_path=None):
    return SimpleNamespace(
        id=aid or uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        library_id=library_id,
        original_path=original_path,
    )


# --------------------------------------------------------------- in_scope


class TestInScope:
    def test_no_rules_means_everything_in_scope(self):
        # 最危险的变异点：无启用规则 → True（全在范围）
        assert in_scope([], library_id="lib-1", path="/photos/a.jpg") is True

    def test_disabled_rules_do_not_count(self):
        # 只有禁用规则 = 等效无规则 → True
        assert in_scope([rule(enabled=False)], library_id=None, path=None) is True

    def test_library_id_match(self):
        rules = [rule(library_id="lib-1")]
        assert in_scope(rules, library_id="lib-1", path=None) is True
        assert in_scope(rules, library_id="lib-2", path=None) is False

    def test_path_prefix_match(self):
        rules = [rule(path_prefix="/foo")]
        assert in_scope(rules, library_id=None, path="/foo") is True
        assert in_scope(rules, library_id=None, path="/foo/bar.jpg") is True

    def test_path_segment_boundary(self):
        # /foo 不匹配 /foobar：前缀命中后下一字符必须是 / 或到结尾
        rules = [rule(path_prefix="/foo")]
        assert in_scope(rules, library_id=None, path="/foobar/x.jpg") is False

    def test_or_semantics_when_both_set(self):
        rules = [rule(library_id="lib-1", path_prefix="/foo")]
        assert in_scope(rules, library_id="lib-1", path="/other") is True
        assert in_scope(rules, library_id="lib-9", path="/foo/bar") is True
        assert in_scope(rules, library_id="lib-9", path="/other") is False

    def test_any_rule_hits(self):
        rules = [rule(library_id="lib-1"), rule(path_prefix="/bar")]
        assert in_scope(rules, library_id="x", path="/bar/a.jpg") is True

    def test_none_values_do_not_match(self):
        rules = [rule(library_id="lib-1")]
        assert in_scope(rules, library_id=None, path=None) is False


# ----------------------------------------------------------- load_scope_rules


class TestLoadScopeRules:
    def test_loads_enabled_rules(self, db):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO scope_rules (library_id, path_prefix, enabled) VALUES (?,?,1)",
                ("lib-1", "/foo"),
            )
            conn.execute(
                "INSERT INTO scope_rules (library_id, path_prefix, enabled) VALUES (?,?,0)",
                ("lib-2", "/bar"),
            )
        rules = load_scope_rules(db)
        assert rules == [ScopeRule(library_id="lib-1", path_prefix="/foo", enabled=True)]

    def test_empty_table(self, db):
        assert load_scope_rules(db) == []


# ---------------------------------------------------------- scan_queue_assets


class TestScanQueueAssets:
    def test_search_params_and_str_ids(self, db):
        aid = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        client = FakeClient([asset(aid, "lib-1", "/foo/a.jpg")])
        result = scan_queue_assets(client, db, "未分类", [])
        # ensure_tags 按名调用
        assert client.ensure_tags_calls == [["未分类"]]
        # 搜索参数：type=IMAGE，tag_ids 是真实 tag id（不是名字）
        dto = client.search_dtos[0]
        from app.clients._immich_gen.models.asset_type_enum import AssetTypeEnum
        from app.clients._immich_gen.types import Unset

        assert dto.type_ is AssetTypeEnum.IMAGE
        assert dto.tag_ids == [client.tag_id]
        # page/size 由 iter_assets 接管，FakeClient 不翻页故为 UNSET
        # UUID → str
        assert result == [str(aid)]
        assert all(isinstance(i, str) for i in result)

    def test_out_of_scope_filtered(self, db):
        client = FakeClient(
            [
                asset(uuid.UUID(int=1), "lib-1", "/foo/a.jpg"),
                asset(uuid.UUID(int=2), "lib-9", "/other/b.jpg"),  # 范围外
                asset(uuid.UUID(int=3), None, "/foo/c.jpg"),
            ]
        )
        rules = [rule(library_id="lib-1"), rule(path_prefix="/foo")]
        result = scan_queue_assets(client, db, "未分类", rules)
        assert result == [str(uuid.UUID(int=1)), str(uuid.UUID(int=3))]

    def test_all_filtered_out(self, db):
        client = FakeClient([asset(uuid.UUID(int=1), "lib-9", "/x.jpg")])
        rules = [rule(library_id="lib-1")]
        assert scan_queue_assets(client, db, "未分类", rules) == []


# ------------------------------------------------------- exclusions (M5 Task 2)

class FakeDetailClient(FakeClient):
    """get_asset 返回带 tags 的资产详情（album 排除需要判断资产所在相册时，
    用 get_asset 的 additional_properties['albums'] 或标签判定）。"""

    def __init__(self, assets, details=None, tag_id="11111111-1111-1111-1111-000000000001"):
        super().__init__(assets, tag_id)
        self.details = details or {}
        self.get_asset_calls: list[str] = []

    def get_asset(self, asset_id):
        self.get_asset_calls.append(str(asset_id))
        return self.details[str(asset_id)]


def detail(aid, tags=(), album_ids=()):
    d = asset(aid)
    d.tags = [SimpleNamespace(name=t) for t in tags]
    d.additional_properties = {"albums": [{"id": a, "albumName": a} for a in album_ids]}
    return d


class TestLoadExclusions:
    def test_loads_enabled_rules(self, db):
        from app.classify.scope import load_exclusions
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO exclusions (type, value, enabled) VALUES ('path','/foo',1)")
            conn.execute(
                "INSERT INTO exclusions (type, value, enabled) VALUES ('album','旅行',0)")
        rules = load_exclusions(db)
        assert rules == [("path", "/foo")]

    def test_empty_table(self, db):
        from app.classify.scope import load_exclusions
        assert load_exclusions(db) == []


class TestExclusionFiltering:
    def test_path_prefix_segment_boundary(self, db):
        # /foo 排除 /foo/bar.jpg 但不排除 /foobar/1.jpg
        a1, a2 = uuid.UUID(int=1), uuid.UUID(int=2)
        client = FakeClient([
            asset(a1, "lib-1", "/foo/bar.jpg"),
            asset(a2, "lib-1", "/foobar/1.jpg"),
        ])
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO exclusions (type, value, enabled) VALUES ('path','/foo',1)")
        result = scan_queue_assets(client, db, "未分类", [])
        assert result == [str(a2)]

    def test_reviewed_excluded(self, db):
        # reviewed 规则排除 asset_state.reviewed=1 的资产；布尔不反演
        a1, a2 = uuid.UUID(int=1), uuid.UUID(int=2)
        client = FakeClient([
            asset(a1, "lib-1", "/foo/a.jpg"),
            asset(a2, "lib-1", "/foo/b.jpg"),
        ])
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO exclusions (type, value, enabled) VALUES ('reviewed','',1)")
            conn.execute(
                "INSERT INTO asset_state (asset_id, reviewed) VALUES (?, 1)",
                (str(a1),))
        result = scan_queue_assets(client, db, "未分类", [])
        assert result == [str(a2)]

    def test_disabled_exclusion_passes_through(self, db):
        a1 = uuid.UUID(int=1)
        client = FakeClient([asset(a1, "lib-1", "/foo/a.jpg")])
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO exclusions (type, value, enabled) VALUES ('path','/foo',0)")
        assert scan_queue_assets(client, db, "未分类", []) == [str(a1)]

    def test_no_exclusions_no_get_asset_calls(self, db):
        # 无排除规则时不应触发逐个 get_asset（N+1 只在有 album/tag 规则时发生）
        a1 = uuid.UUID(int=1)
        client = FakeDetailClient(
            [asset(a1, "lib-1", "/foo/a.jpg")],
            {str(a1): detail(a1)})
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO exclusions (type, value, enabled) VALUES ('reviewed','',0)")
        result = scan_queue_assets(client, db, "未分类", [])
        assert result == [str(a1)]
        assert client.get_asset_calls == []

    def test_album_exclusion_via_get_asset(self, db):
        a1, a2 = uuid.UUID(int=1), uuid.UUID(int=2)
        client = FakeDetailClient(
            [asset(a1, "lib-1", "/p/a.jpg"), asset(a2, "lib-1", "/p/b.jpg")],
            {
                str(a1): detail(a1, album_ids=["旅行"]),
                str(a2): detail(a2, album_ids=[]),
            })
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO exclusions (type, value, enabled) VALUES ('album','旅行',1)")
        result = scan_queue_assets(client, db, "未分类", [])
        assert result == [str(a2)]
        assert set(client.get_asset_calls) == {str(a1), str(a2)}

    def test_tag_exclusion_via_get_asset(self, db):
        a1, a2 = uuid.UUID(int=1), uuid.UUID(int=2)
        client = FakeDetailClient(
            [asset(a1, "lib-1", "/p/a.jpg"), asset(a2, "lib-1", "/p/b.jpg")],
            {
                str(a1): detail(a1, tags=["风景"]),
                str(a2): detail(a2, tags=[]),
            })
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO exclusions (type, value, enabled) VALUES ('tag','风景',1)")
            conn.execute(
                "INSERT INTO tags (name, kind) VALUES ('风景','primary')")
        result = scan_queue_assets(client, db, "未分类", [])
        assert result == [str(a2)]

    def test_out_of_scope_event_logged(self, db):
        a1 = uuid.UUID(int=1)
        client = FakeClient([asset(a1, "lib-1", "/foo/a.jpg")])
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO exclusions (type, value, enabled) VALUES ('path','/foo',1)")
        scan_queue_assets(client, db, "未分类", [])
        with db.connect() as conn:
            row = conn.execute(
                "SELECT source, event_type, payload FROM events "
                "WHERE event_type='asset_out_of_scope'").fetchone()
        assert row is not None
        import json
        payload = json.loads(row["payload"])
        assert payload["asset_id"] == str(a1)
        assert payload["rule_type"] == "path"


# ------------------------------------------------- M6 Task 1: iter_scope_assets

class TestIterScopeAssets:
    def test_iterates_without_tag_requirement(self, db):
        """无队列标签要求：dto.tag_ids 不设（UNSET），只按 type=IMAGE 搜索。"""
        from app.classify.scope import iter_scope_assets
        from app.clients._immich_gen.types import Unset

        client = FakeClient([asset(uuid.UUID(int=1), "lib-1", "/foo/a.jpg")])
        rules = load_scope_rules(db)  # 空 → 全在范围
        got = list(iter_scope_assets(client, db, rules))
        assert got == [str(uuid.UUID(int=1))]
        dto = client.search_dtos[0]
        from app.clients._immich_gen.models.asset_type_enum import AssetTypeEnum
        assert dto.type_ is AssetTypeEnum.IMAGE
        assert isinstance(dto.tag_ids, Unset)

    def test_out_of_scope_filtered_and_str(self, db):
        from app.classify.scope import iter_scope_assets
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO scope_rules (library_id, path_prefix, enabled) "
                "VALUES ('lib-1', '', 1)")
        client = FakeClient([
            asset(uuid.UUID(int=1), "lib-1", "/foo/a.jpg"),
            asset(uuid.UUID(int=2), "lib-9", "/other/b.jpg"),
        ])
        got = list(iter_scope_assets(client, db, load_scope_rules(db)))
        assert got == [str(uuid.UUID(int=1))]
        assert all(isinstance(i, str) for i in got)

    def test_no_ensure_tags_call(self, db):
        """iter 不建/不查队列标签（入队阶段才 ensure）。"""
        from app.classify.scope import iter_scope_assets
        client = FakeClient([])
        list(iter_scope_assets(client, db, []))
        assert client.ensure_tags_calls == []
