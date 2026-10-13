"""immich_conn / llm_conn：各一张单行表（id 恒为 1，由 CHECK 约束保证）。"""
from __future__ import annotations

from dataclasses import dataclass

from app.db.connection import Database


@dataclass(frozen=True)
class ImmichConn:
    url: str = ""
    api_key: str = ""
    verify_ssl: bool = True


@dataclass(frozen=True)
class LLMConn:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 180
    max_tokens: int = 128000
    multimodal: bool = True


class ConnectionStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------- immich

    def get_immich(self) -> ImmichConn:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT url, api_key, verify_ssl FROM immich_conn WHERE id = 1"
            ).fetchone()
        if row is None:
            return ImmichConn()
        return ImmichConn(
            url=row["url"], api_key=row["api_key"], verify_ssl=bool(row["verify_ssl"])
        )

    def set_immich(self, conn_cfg: ImmichConn) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO immich_conn (id, url, api_key, verify_ssl) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "url = excluded.url, api_key = excluded.api_key, "
                "verify_ssl = excluded.verify_ssl",
                (conn_cfg.url, conn_cfg.api_key, int(conn_cfg.verify_ssl)),
            )

    # ---------------------------------------------------------------- llm

    def get_llm(self) -> LLMConn:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT base_url, api_key, model, timeout, max_tokens, "
                "multimodal FROM llm_conn WHERE id = 1"
            ).fetchone()
        if row is None:
            return LLMConn()
        return LLMConn(
            base_url=row["base_url"], api_key=row["api_key"], model=row["model"],
            timeout=row["timeout"],
            max_tokens=row["max_tokens"], multimodal=bool(row["multimodal"]),
        )

    def set_llm(self, conn_cfg: LLMConn) -> None:
        if conn_cfg.timeout < 1:
            raise ValueError("timeout must be >= 1")
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO llm_conn "
                "(id, base_url, api_key, model, timeout, max_tokens, multimodal) "
                "VALUES (1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "base_url = excluded.base_url, api_key = excluded.api_key, "
                "model = excluded.model, "
                "timeout = excluded.timeout, max_tokens = excluded.max_tokens, "
                "multimodal = excluded.multimodal",
                (conn_cfg.base_url, conn_cfg.api_key, conn_cfg.model,
                 conn_cfg.timeout, conn_cfg.max_tokens,
                 int(conn_cfg.multimodal)),
            )
