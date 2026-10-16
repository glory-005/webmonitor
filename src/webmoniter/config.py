"""加载并校验 config/targets.yaml。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import ValidationError

from webmoniter.models import Target
from webmoniter.utils import deep_merge


class ConfigError(RuntimeError):
    """配置不可用。"""


def load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 语法错误: {exc}") from exc
    if data is None:
        raise ConfigError(f"配置文件为空: {path}")
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射（dict）: {path}")
    return data


def load_targets(path: Path) -> list[Target]:
    """读取配置，把 defaults 合并进每个 target 并校验。"""
    data = load_raw(path)
    defaults = data.get("defaults") or {}
    raw_targets = data.get("targets") or []
    if not isinstance(defaults, dict):
        raise ConfigError("defaults 必须是映射（dict）")
    if not isinstance(raw_targets, list):
        raise ConfigError("targets 必须是列表")

    targets: list[Target] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_targets):
        if not isinstance(raw, dict):
            raise ConfigError(f"targets[{index}] 必须是映射（dict）")
        try:
            target = Target.model_validate(deep_merge(defaults, raw))
        except ValidationError as exc:
            raise ConfigError(f"targets[{index}]（id={raw.get('id', '?')}）配置非法:\n{exc}") from exc
        if target.id in seen:
            raise ConfigError(f"target id 重复: {target.id}")
        seen.add(target.id)
        targets.append(target)

    if not targets:
        raise ConfigError(f"{path} 里没有任何 target")
    enabled = [t for t in targets if t.enabled]
    logger.info("配置加载完成: {} 个目标（启用 {} 个）", len(targets), len(enabled))
    return targets


def config_hash(target: Target) -> str:
    """目标的抓取/抽取配置指纹。指纹变化时（比如换了选择器）应当重建基线。

    只包含真正影响「抽取到什么文本」的字段：改检查间隔、改通知渠道不该重建基线，
    否则会白白吞掉一次真实变更的告警。
    """
    payload = target.model_dump(
        mode="json",
        exclude={"enabled", "interval", "jitter", "notify", "tags", "name"},
    )
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


__all__ = ["ConfigError", "config_hash", "load_raw", "load_targets"]
