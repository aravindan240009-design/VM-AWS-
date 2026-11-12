"""Application configuration loaded from environment and .env."""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = "http://127.0.0.1:8000/v1"
    llm_api_key: str = "local-dev-key"
    llm_model: str = "huihui-ai/Qwen3-8B-abliterated"

    app_host: str = "127.0.0.1"
    app_port: int = 8080
    max_agent_steps: int = 12

    enable_shell_tool: bool = True
    enable_python_tool: bool = True
    enable_web_tools: bool = True
    fast_no_think_default: bool = True

    data_dir: Path = BASE_DIR / "data"
    workspace_dir: Path = BASE_DIR / "workspace"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "chat_harness.db"


settings = Settings()
