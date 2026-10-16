"""全局设置：从环境变量 / .env 读取（前缀 WEBMONITER_）。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from loguru import logger
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def absolute_path(candidate: Path | str) -> Path:
    """相对路径统一按进程 CWD 解析。

    必须与 pydantic-settings 的行为一致：它解析相对 `env_file` 就是按 CWD 的。
    若这里改成按 PROJECT_ROOT 解析，就会出现「Settings 读 CWD/.env、而渠道扫描 /
    cookies_env 读 PROJECT_ROOT/.env」的两套基准不一致问题。
    """
    path = Path(candidate).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


#: .env 查找顺序：显式指定 → 当前目录 → 项目根目录（全部转成绝对路径，两套机制基准一致）
ENV_FILES: tuple[Path, ...] = tuple(
    absolute_path(candidate)
    for candidate in (
        os.environ.get("WEBMONITER_ENV_FILE"),
        Path(".env"),
        PROJECT_ROOT / ".env",
    )
    if candidate
)


def env_file_paths() -> list[Path]:
    """实际存在的 .env 文件（去重、绝对路径）。"""
    seen: set[Path] = set()
    paths: list[Path] = []
    for item in ENV_FILES:
        try:
            resolved = Path(item).resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        paths.append(resolved)
    return paths


def load_dotenv_into_environ() -> list[Path]:
    """把 .env 注入 os.environ（不覆盖真实环境变量），返回加载了哪些文件。

    为什么必须做：pydantic-settings 只是把 .env 当「配置源」，**不会**写回 os.environ，
    而通知渠道扫描（FEISHU_WEBHOOK_*）和 Cookies（cookies_env）都是直接读 os.environ 的。
    不注入的话，用户照 .env.example 配好 .env 却会发现「一个渠道都没有」、Cookie 永远为空。
    """
    loaded: list[Path] = []
    for path in env_file_paths():
        try:
            load_dotenv(path, override=False)
            loaded.append(path)
        except Exception as exc:  # 编码错误 / 权限问题都不该让进程起不来
            logger.warning("加载 {} 失败（已忽略）: {}: {}", path, type(exc).__name__, exc)
    return loaded


#: 导入本模块即完成注入，保证任何读 os.environ 的地方都能看到 .env
ENV_FILES_LOADED: list[Path] = load_dotenv_into_environ()


def load_environ(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """环境视图。

    * 不传 `environ`：`.env` 内容 + `os.environ` 的合并视图（真实环境变量优先）。
    * 传了 `environ`：**完全替换**，既不合并 `.env` 也不合并 `os.environ`
      —— 这是为了测试能精确构造环境（`discover_channels({})` 必须返回空）。
    """
    if environ is not None:
        return {key: value for key, value in environ.items() if isinstance(value, str)}
    merged: dict[str, str] = {}
    for path in env_file_paths():
        try:
            values = dotenv_values(path)
        except Exception as exc:
            logger.warning("读取 {} 失败（已忽略）: {}: {}", path, type(exc).__name__, exc)
            continue
        for key, value in values.items():
            if value is not None:
                merged[key] = value
    merged.update(os.environ)
    return merged


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WEBMONITER_",
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    config_path: Path = Path("config/targets.yaml")
    db_path: Path = Path("data/webmoniter.db")
    artifacts_dir: Path = Path("data/artifacts")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    tz: str = "Asia/Shanghai"

    #: 每个通知渠道每分钟最多发多少条（官方频控通常 20/分钟，留点余量）
    rate_per_minute: int = 18
    #: 同时在途的抓取请求数
    concurrency: int = 5
    #: 共享 httpx 客户端的默认超时（秒）；每个 target 可用自己的 timeout 覆盖
    default_timeout: float = 20.0
    default_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    #: 通知正文里最多放多少行 diff
    max_notify_lines: int = 15
    #: 单条通知最多发送几次（含首次），用于扛住网络抖动
    notify_retry_attempts: int = 3
    #: 变更时留存的抽取文本上限（字节）
    max_artifact_bytes: int = 2_000_000
    #: 自监控：每隔多少秒检查一次「是否还有成功检查」
    health_interval: int = 600
    #: 超过多少小时没有任何成功检查就告警（防静默失效）
    health_silence_hours: float = 6.0
    #: 自检告警发到哪些渠道（逗号分隔）。留空则用全部已配置渠道
    health_channels: str = ""

    @property
    def health_channel_list(self) -> list[str]:
        return [name.strip() for name in self.health_channels.split(",") if name.strip()]

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    def resolve(self, path: Path) -> Path:
        """相对路径按项目根目录解析（避免受启动时 cwd 影响）。"""
        return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def load_settings(config_path: Path | None = None, *, reload: bool = False) -> Settings:
    """读取设置；可用 CLI 参数覆盖配置文件路径。"""
    if reload:
        get_settings.cache_clear()
    settings = get_settings()
    if config_path is not None:
        settings = settings.model_copy(update={"config_path": config_path})
    return settings


__all__ = [
    "ENV_FILES",
    "ENV_FILES_LOADED",
    "PROJECT_ROOT",
    "Settings",
    "absolute_path",
    "env_file_paths",
    "get_settings",
    "load_dotenv_into_environ",
    "load_environ",
    "load_settings",
]
