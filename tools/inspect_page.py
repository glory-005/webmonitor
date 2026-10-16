"""诊断工具：用项目自身的抓取链路取回一个页面，打印结构轮廓，帮助给 targets.yaml 挑选择器。

用法：
    uv run python tools/inspect_page.py <url>
    uv run python tools/inspect_page.py <url> --save
    uv run python tools/inspect_page.py <url> --referer https://www.example.com/

它和 `webmoniter check` 用的是同一套实现（浏览器 UA、超时、指数退避重试、GBK/UTF-8
编码兜底），所以「这个脚本抓得到」基本等价于「监控抓得到」。

候选容器按「条目里是否带日期」优先排序（新闻列表带日期，页脚友情链接不带），
并把 yqlj/ljlink/footer 这类页脚容器降权——只按链接数排会把友情链接排到第一。
"""

from __future__ import annotations

import argparse
import asyncio
import re
from typing import NamedTuple

from bs4 import BeautifulSoup, Tag

from webmoniter.fetch.http import HttpFetcher
from webmoniter.models import Target
from webmoniter.settings import load_settings

CANDIDATE_TAGS = ("ul", "ol", "tbody", "table", "div")
MAX_REPORTED = 12

#: 页脚/友情链接/版权区的类名特征，这类容器降权（不丢弃，只排到最后）
NOISE_HINTS = ("yqlj", "ljlink", "friend", "footer", "copyright", "bottom", "links")
#: 2025-01-02 / 2025/01/02 / 2025年1月2日 / 1月2日 / 01-02
DATE_RE = re.compile(r"(\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}|\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?)")


class Candidate(NamedTuple):
    links: int
    items: int
    dated: float
    noise: bool
    node: Tag


def css_path(node: Tag, depth: int = 4) -> str:
    """拼一个尽量稳的选择器：优先 id，其次 class，最多往上 4 层。"""
    parts: list[str] = []
    current: Tag | None = node
    while current is not None and len(parts) < depth:
        name = current.name
        if not name or name == "[document]":
            break
        node_id = current.get("id")
        if node_id:
            parts.append(f"#{node_id}")
            break
        classes = current.get("class") or []
        if classes:
            parts.append(f"{name}.{'.'.join(classes[:2])}")
        else:
            parts.append(name)
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return " > ".join(reversed(parts))


def flat_text(node: Tag, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
    return text[:limit] + ("…" if len(text) > limit else "")


def describe(node: Tag) -> str:
    ident = node.get("id")
    classes = " ".join(node.get("class") or [])
    bits = [str(node.name)]
    if ident:
        bits.append(f"id={ident}")
    if classes:
        bits.append(f"class={classes}")
    return f"<{' '.join(bits)}>"


def is_noise(node: Tag) -> bool:
    haystack = " ".join([*(node.get("class") or []), str(node.get("id") or "")]).lower()
    return any(hint in haystack for hint in NOISE_HINTS)


def date_ratio(node: Tag) -> float:
    """条目里带日期的比例——新闻列表接近 1，纯链接区块接近 0。"""
    items = node.find_all("li") or node.find_all("a", href=True)
    if not items:
        return 0.0
    hits = sum(1 for item in items if DATE_RE.search(item.get_text(" ", strip=True)))
    return hits / len(items)


def collect(soup: BeautifulSoup) -> list[Candidate]:
    found: list[Candidate] = []
    for tag_name in CANDIDATE_TAGS:
        for node in soup.find_all(tag_name):
            link_count = len(node.find_all("a", href=True))
            if link_count < 3:
                continue
            found.append(
                Candidate(
                    links=link_count,
                    items=len(node.find_all("li")),
                    dated=date_ratio(node),
                    noise=is_noise(node),
                    node=node,
                )
            )
    # 优先：非页脚容器 → 条目带日期 → 链接多 → 选择器短（更好维护）
    found.sort(key=lambda c: (c.noise, -c.dated, -c.links, len(css_path(c.node))))
    return found


def report(soup: BeautifulSoup) -> None:
    title = soup.title.get_text(strip=True) if soup.title else "(没有 title)"
    print(f"\n页面标题: {title}")

    body = soup.body or soup
    print("\n== body 直属子节点 ==")
    for child in list(body.children)[:15]:
        if isinstance(child, Tag):
            print(f"  {describe(child)}")

    scored = collect(soup)
    print("\n== 候选列表容器（按「带日期比例 → 链接数」排序，可直接抄进 selector）==")
    seen: set[str] = set()
    shown = 0
    for cand in scored:
        path = css_path(cand.node)
        if path in seen:
            continue
        seen.add(path)
        flag = " [疑似页脚]" if cand.noise else ""
        print(f"  [{cand.links:3d} a / {cand.items:3d} li / 日期 {cand.dated:3.0%}]{flag}  {path}")
        print(f"        文本预览: {flat_text(cand.node)}")
        shown += 1
        if shown >= MAX_REPORTED:
            break

    if not scored:
        print("（没有找到含 3 个以上链接的容器，可能需要 fetch: browser）")
        return

    best = scored[0]
    print("\n== 最佳候选的前 3 个条目原文（看日期/点击数在不在条目里）==")
    items = best.node.find_all("li") or best.node.find_all("a", href=True)
    for item in items[:3]:
        snippet = re.sub(r"\s+", " ", str(item))[:300]
        print(f"  - {snippet}")

    print("\n== 可直接抄的配置片段 ==")
    print("    extract:")
    print("      - type: css")
    print(f'        selector: "{css_path(best.node)}"')
    print("        mode: text")


async def run(url: str, *, save: bool, referer: str | None) -> int:
    settings = load_settings()
    headers = {"Referer": referer} if referer else {}
    target = Target(id="inspect", url=url, headers=headers)
    fetcher = HttpFetcher(settings)
    try:
        result = await fetcher.fetch(target)
    finally:
        await fetcher.aclose()

    if not result.ok:
        print(f"抓取失败: {result.error or '响应为空'}（HTTP {result.status}）")
        print("提示：403 时先加 --referer 站点首页；仍失败就把该目标改成 fetch: browser")
        return 1

    print(f"HTTP {result.status} | {result.elapsed_ms} ms | {len(result.text)} 字符")
    if save:
        directory = settings.resolve(settings.artifacts_dir).parent / "debug"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "inspect-page.html"
        path.write_text(result.text, encoding="utf-8")
        print(f"原始 HTML 已保存: {path}")

    report(BeautifulSoup(result.text, "html.parser"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="用监控自身的抓取链路诊断页面结构")
    parser.add_argument("url", help="要诊断的页面 URL")
    parser.add_argument("--save", action="store_true", help="保存原始 HTML 到 data/debug/")
    parser.add_argument("--referer", default=None, help="附加 Referer 头（政务站常校验）")
    args = parser.parse_args()
    return asyncio.run(run(args.url, save=args.save, referer=args.referer))


if __name__ == "__main__":
    raise SystemExit(main())
