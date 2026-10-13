"""首次启动用环境变量填充连接配置。已存在的值一律不覆盖。"""
from __future__ import annotations

from app.config import BootstrapConfig
from app.db.connection import Database
from app.store.connections import ConnectionStore, ImmichConn, LLMConn


def seed_from_env(db: Database, bootstrap: BootstrapConfig) -> bool:
    """返回是否真的写入了内容。"""
    store = ConnectionStore(db)
    wrote = False

    immich = store.get_immich()
    if not immich.url and not immich.api_key and bootstrap.immich_url:
        store.set_immich(
            ImmichConn(
                url=bootstrap.immich_url,
                api_key=bootstrap.immich_api_key or "",
                verify_ssl=True,
            )
        )
        wrote = True

    llm = store.get_llm()
    if not llm.base_url and not llm.model and bootstrap.llm_base_url:
        store.set_llm(
            LLMConn(
                base_url=bootstrap.llm_base_url,
                api_key=bootstrap.llm_api_key or "",
                model=bootstrap.llm_model or "",
            )
        )
        wrote = True

    return wrote
