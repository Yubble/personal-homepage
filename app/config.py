"""个人主页服务配置：只管笔记发布这一摊，与零食店 Agent 的配置完全无关。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

# 仓库根目录（site/ 静态文件、.env、git 仓库都在这里）
ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """NOTES_TOKEN：笔记发布口令（简单口令即可，风险半径 = 冒名发笔记）。
    NOTES_GIT_PUSH：发布后是否自动 push（需要推送凭据，默认关）。"""

    notes_token: str = ""
    notes_git_push: bool = False

    # AI 校对（与零食店 Agent 共用同一把 key，字段名一致便于同步）
    llm_api_key: str = "sk-placeholder"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    model_config = {"env_file": str(ROOT_DIR / ".env"), "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
