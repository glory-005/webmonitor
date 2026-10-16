"""用 BeautifulSoup 按规则抽取内容。

抽取顺序：先按 strip_selectors 摘掉噪声节点，再逐条规则取值，最后用换行拼起来。
任何规则取不到值都不报错——而是返回空串，由上层判定为「抽取失败」并走错误通道，
避免把「选择器失效」误报成「页面内容被清空」。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

from bs4 import BeautifulSoup, FeatureNotFound
from loguru import logger

from webmoniter.models import ExtractRule

REGEX_FLAGS = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
}

_PATH_TOKEN_RE = re.compile(r"^([^\[\]]*)((?:\[\d+\])*)$")


def build_soup(html: str, parser: str = "html.parser", strip_selectors: Sequence[str] = ()) -> BeautifulSoup:
    """解析 HTML 并摘掉指定节点。"""
    try:
        soup = BeautifulSoup(html, parser)
    except FeatureNotFound:
        logger.warning("解析器 {} 不可用（可能需要 uv sync --extra fastparse），回退 html.parser", parser)
        soup = BeautifulSoup(html, "html.parser")

    for selector in strip_selectors:
        try:
            nodes = soup.select(selector)
        except Exception as exc:  # soupsieve 语法错误
            logger.warning("strip_selectors 中的选择器非法，已忽略: {!r}（{}）", selector, exc)
            continue
        for node in nodes:
            node.decompose()
    return soup


def _flags(rule: ExtractRule) -> int:
    value = 0
    for char in rule.flags:
        value |= REGEX_FLAGS.get(char.lower(), 0)
    return value


def _from_css(soup: BeautifulSoup, rule: ExtractRule) -> list[str]:
    assert rule.selector is not None
    try:
        nodes = soup.select(rule.selector)
    except Exception as exc:
        logger.warning("CSS 选择器非法: {!r}（{}）", rule.selector, exc)
        return []
    if not nodes:
        logger.debug("CSS 选择器未命中任何节点: {}", rule.selector)
        return []

    values: list[str] = []
    for node in nodes:
        if rule.mode == "html":
            values.append(str(node))
        elif rule.mode == "attr":
            attr = node.get(rule.attr) if rule.attr else None
            if attr is None:
                logger.debug("节点缺少属性 {}（selector={}）", rule.attr, rule.selector)
                continue
            values.append(attr if isinstance(attr, str) else " ".join(attr))
        else:
            values.append(node.get_text("\n", strip=True))
    return values


def _from_regex(html: str, rule: ExtractRule) -> list[str]:
    assert rule.pattern is not None
    try:
        pattern = re.compile(rule.pattern, _flags(rule))
    except re.error as exc:
        logger.warning("正则非法: {!r}（{}）", rule.pattern, exc)
        return []
    values: list[str] = []
    for match in pattern.finditer(html):
        try:
            values.append(match.group(rule.group))
        except IndexError:
            logger.warning("正则 {!r} 没有第 {} 个捕获组", rule.pattern, rule.group)
            break
    if not values:
        logger.debug("正则未命中: {}", rule.pattern)
    return values


def resolve_path(obj: Any, path: str) -> Any:
    """按 `a.b[0].c` 形式的路径取值，取不到返回 None。"""
    current = obj
    for token in path.split("."):
        match = _PATH_TOKEN_RE.match(token)
        if match is None:
            return None
        name, indexes = match.group(1), match.group(2)
        if name:
            if not isinstance(current, dict) or name not in current:
                return None
            current = current[name]
        for index in re.findall(r"\[(\d+)\]", indexes):
            if not isinstance(current, list) or int(index) >= len(current):
                return None
            current = current[int(index)]
    return current


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        # sort_keys 让 JSON 字段顺序变化不产生假变更
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return str(value)


def _from_json(html: str, rule: ExtractRule) -> list[str]:
    assert rule.path is not None
    try:
        payload = json.loads(html)
    except json.JSONDecodeError as exc:
        logger.warning("json 规则要求响应是 JSON，但解析失败: {}", exc)
        return []
    value = resolve_path(payload, rule.path)
    if value is None:
        logger.debug("JSON 路径未命中: {}", rule.path)
        return []
    return [_stringify(value)]


def extract(soup: BeautifulSoup, html: str, rules: Iterable[ExtractRule]) -> str:
    """按规则抽取出用于比对的文本。没有规则时退化为整页可见文本。"""
    rules = list(rules)
    if not rules:
        return soup.get_text("\n", strip=True)

    parts: list[str] = []
    for rule in rules:
        if rule.type == "css":
            parts.extend(_from_css(soup, rule))
        elif rule.type == "regex":
            parts.extend(_from_regex(html, rule))
        else:
            parts.extend(_from_json(html, rule))
    return "\n".join(part for part in parts if part and part.strip())


__all__ = ["build_soup", "extract", "resolve_path"]
