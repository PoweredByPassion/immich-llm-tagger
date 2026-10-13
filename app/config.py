"""环境变量 → 首次引导配置。

环境变量**只用于首次 seed**。之后一律以 DB 为准；改 env 不会覆盖 DB 里已有的值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BootstrapConfig:
    immich_url: str | None = None
    immich_api_key: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    data_dir: str = "/data"
    # 单密码访问认证。未设置（None/空）→ 完全旁路，本地部署零摩擦。
    auth_password: str | None = None

    @classmethod
    def from_env(cls) -> "BootstrapConfig":
        return cls(
            immich_url=os.environ.get("IMMICH_URL"),
            immich_api_key=os.environ.get("IMMICH_API_KEY"),
            llm_base_url=os.environ.get("LLM_BASE_URL"),
            llm_api_key=os.environ.get("LLM_API_KEY"),
            llm_model=os.environ.get("LLM_MODEL"),
            data_dir=os.environ.get("DATA_DIR", "/data"),
            auth_password=os.environ.get("AUTH_PASSWORD") or None,
        )

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "autotag.db"
