"""loguru 日志初始化：彩色控制台 + 按大小滚动的文件。"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <7}</level> | "
    "<cyan>{name}</cyan> - <level>{message}</level>"
)
FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {name}:{line} | {message}"


def setup_logging(level: str = "INFO", log_dir: Path | None = None, *, quiet: bool = False) -> None:
    """配置全局 logger。重复调用安全（先移除已有 sink）。"""
    logger.remove()
    if not quiet:
        logger.add(
            sys.stderr,
            level=level,
            colorize=True,
            format=CONSOLE_FORMAT,
            backtrace=False,
            diagnose=False,
        )
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "webmoniter.log",
            level=level,
            rotation="10 MB",
            retention="14 days",
            encoding="utf-8",
            enqueue=True,
            backtrace=False,
            diagnose=False,
            format=FILE_FORMAT,
        )


__all__ = ["setup_logging"]
