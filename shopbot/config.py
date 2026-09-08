from dataclasses import dataclass
import os

ADMIN_IDS = frozenset({1905862979, 5943392316})

@dataclass(frozen=True)
class Settings:
    bot_token: str
    imgbb_api_key: str
    github_token: str
    github_owner: str
    stars_per_credit: int
    support_url: str
    storage_repo_prefix: str
    storage_max_bytes: int
    storage_safe_bytes: int
    storage_max_file_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "")
        if not token:
            raise RuntimeError("BOT_TOKEN is required")
        result = cls(
            bot_token=token, imgbb_api_key=os.getenv("IMGBB_API_KEY", ""),
            github_token=os.getenv("GITHUB_TOKEN", ""), github_owner=os.getenv("GITHUB_OWNER", ""),
            stars_per_credit=int(os.getenv("STARS_PER_CREDIT", "1")),
            support_url=os.getenv("SUPPORT_URL", ""), storage_repo_prefix=os.getenv("STORAGE_REPO_PREFIX", "storage-"),
            storage_max_bytes=int(os.getenv("STORAGE_MAX_BYTES", str(3 * 1024**3))),
            storage_safe_bytes=int(os.getenv("STORAGE_SAFE_BYTES", str(int(2.8 * 1024**3)))),
            storage_max_file_bytes=int(os.getenv("STORAGE_MAX_FILE_BYTES", str(2 * 1024**3))),
        )
        if result.stars_per_credit <= 0: raise RuntimeError("STARS_PER_CREDIT must be positive")
        return result
