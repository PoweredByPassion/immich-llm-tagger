"""用量统计聚合（asset_records.usage JSON TEXT）。

usage 存 LLM 响应原始 dict（OpenAI 契约 prompt_tokens / completion_tokens /
total_tokens）。部分网关只回部分字段，tokens 以 prompt+completion 求和为主，
不硬性依赖 total_tokens。dry_run 记录计入（沙盒也是真实 LLM 调用）。
"""
from __future__ import annotations

from app.db.connection import Database

# usage 字段缺省按 0；json_extract 对 NULL usage 返回 NULL → COALESCE 0
_PT = "COALESCE(json_extract(usage, '$.prompt_tokens'), 0)"
_CT = "COALESCE(json_extract(usage, '$.completion_tokens'), 0)"
# total_tokens 不做硬性依赖：契约允许缺失，tokens 一律 prompt+completion 求和
_TOKENS = f"({_PT} + {_CT})"


def by_day(db: Database, limit: int = 30) -> list[dict]:
    """按天（UTC date(created_at)，与概览一致）聚合，日期降序。"""
    sql = (
        "SELECT date(created_at) AS day, COUNT(*) AS requests, "
        f"SUM({_PT}) AS prompt_tokens, SUM({_CT}) AS completion_tokens, "
        f"SUM({_TOKENS}) AS tokens "
        "FROM asset_records GROUP BY day ORDER BY day DESC LIMIT ?"
    )
    with db.connect() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def by_model(db: Database) -> list[dict]:
    """按模型聚合，tokens 降序；model 为 NULL/空的记录不参与。"""
    sql = (
        "SELECT model, COUNT(*) AS requests, "
        f"SUM({_PT}) AS prompt_tokens, SUM({_CT}) AS completion_tokens, "
        f"SUM({_TOKENS}) AS tokens "
        "FROM asset_records WHERE model IS NOT NULL AND model != '' "
        "GROUP BY model ORDER BY tokens DESC"
    )
    with db.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def by_job(db: Database, limit: int = 50) -> list[dict]:
    """按 job 聚合（join jobs 取 type/status），requests 降序。

    job_id 为 NULL 的记录归入 NULL 组（type/status 为 NULL）。
    """
    sql = (
        "SELECT r.job_id AS job_id, j.type AS type, j.status AS status, "
        "COUNT(*) AS requests, "
        f"SUM({_PT}) AS prompt_tokens, SUM({_CT}) AS completion_tokens, "
        f"SUM({_TOKENS}) AS tokens "
        "FROM asset_records r LEFT JOIN jobs j ON j.id = r.job_id "
        "GROUP BY r.job_id ORDER BY requests DESC LIMIT ?"
    )
    with db.connect() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def totals(db: Database) -> dict:
    """今日（UTC）与累计的请求数与 tokens。"""
    sql = (
        "SELECT COUNT(*) AS requests, "
        f"COALESCE(SUM({_PT}), 0) AS prompt_tokens, "
        f"COALESCE(SUM({_CT}), 0) AS completion_tokens, "
        f"COALESCE(SUM({_TOKENS}), 0) AS tokens "
        "FROM asset_records WHERE {where}"
    )
    with db.connect() as conn:
        all_row = dict(conn.execute(sql.format(where="1=1")).fetchone())
        today = dict(
            conn.execute(
                sql.format(where="date(created_at) = date('now')")
            ).fetchone()
        )
    return {"today": today, "all": all_row}
