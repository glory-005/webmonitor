"""命令行入口。

    webmoniter run                       # 常驻监控
    webmoniter run --once                # 所有目标检查一轮后退出（适合计划任务）
    webmoniter check gov_notice          # 手动检查一个目标（默认不发通知）
    webmoniter check --show-diff         # 看逐行差异
    webmoniter check price_watch --show-text   # 看抽取结果，排查选择器
    webmoniter targets                   # 列出监控目标
    webmoniter channels                  # 列出已配置的通知渠道
    webmoniter changes --limit 20        # 最近的变更记录
    webmoniter test-notify               # 给所有渠道发测试消息
    webmoniter silence gov_notice --hours 4   # 维护窗口内临时静默告警
    webmoniter doctor                    # 环境自检
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from webmoniter import __version__
from webmoniter.config import ConfigError, load_targets
from webmoniter.logging_setup import setup_logging
from webmoniter.models import NotificationMessage, Target
from webmoniter.notify.registry import build_notifiers, discover_channels
from webmoniter.runtime import Runtime
from webmoniter.scheduler import MonitorScheduler
from webmoniter.settings import Settings, load_settings
from webmoniter.storage import Storage
from webmoniter.utils import format_elapsed, iso, utcnow
from webmoniter.watcher import CheckResult

#: 状态符号刻意避开 ✓/✗（U+2713/U+2717）：Windows 控制台默认代码页是 GBK(cp936)，
#: rich 写这两个字符会直接 UnicodeEncodeError 崩掉（doctor 就这么崩过）。
OK_MARK = "√"
FAIL_MARK = "×"


def _harden_stdio() -> None:
    """让输出不因编码问题崩掉，并让重定向输出变成 UTF-8。

    * 交互式控制台：保留本地编码（中文在 GBK 控制台下显示正常），只把不可编码
      字符（✓、emoji、版面字符）降级成 '?'——否则 rich 会 UnicodeEncodeError 直接崩。
    * 重定向到文件/管道：统一写 UTF-8，与 ruff / uv 等工具保持一致，
      这样 `> report.txt` 出来的报告在别的工具（含本仓库的协作流程）里能直接读。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pytest capture 之类没有 reconfigure 的对象
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # 已分离 / 已关闭的流
            continue


_harden_stdio()


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


#: 排查/提 issue 时用 WEBMONITER_PLAIN_TRACEBACK=1 拿到未格式化的完整堆栈
#: （默认走 Typer 的 rich 面板：好看但会折行，复制出来难读）。
PLAIN_TRACEBACK = _env_flag("WEBMONITER_PLAIN_TRACEBACK")

#: `--config/-c` 的公共声明。用 Annotated 而不是"函数调用做默认值"：
#: 既不触发 ruff B008，也让每个命令各自生成独立的 Option 元数据（不复用同一实例）。
#:
#: ⚠️ 陷阱：在 Annotated 里，`typer.Option(...)` 的**第一个位置参数是参数声明**，
#: 不是默认值。写 `typer.Option(None, "--config", ...)` 会让 Typer 把 None 当成一个
#: 声明（`param_decls=(None, "--config", ...)`），启动时报
#: `AttributeError: 'NoneType' object has no attribute 'isidentifier'`。
#: 默认值只由函数签名上的 `= None` 提供。
ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="配置文件路径，默认 config/targets.yaml"),
]

app = typer.Typer(
    add_completion=False,
    help="网页变更实时监控：httpx 抓取 + BeautifulSoup 解析 + 飞书/钉钉通知",
    # 只关掉 rich 面板渲染，不动 show_locals：默认 False 更安全（避免把 webhook 之类
    # 局部变量打进堆栈里）
    pretty_exceptions_enable=not PLAIN_TRACEBACK,
)
console = Console(highlight=False)


@app.callback()
def _bootstrap() -> None:
    """任何命令都先进入安静模式（WARNING），避免 loguru 默认 DEBUG 刷屏。

    `run` / `check` / `changes` / `silence` 会按需再配置一次（覆盖这里）。
    """
    setup_logging("WARNING", None)


STATUS_STYLE = {
    "baseline": "cyan",
    "unchanged": "green",
    "noise": "yellow",
    "changed": "bold yellow",
    "error": "bold red",
}


# --------------------------------------------------------------------------- 公共
def _fail(message: str, code: int = 1) -> None:
    console.print(f"[bold red]{FAIL_MARK}[/bold red] {message}")
    raise typer.Exit(code)


def _load(settings: Settings) -> list[Target]:
    try:
        return load_targets(settings.resolve(settings.config_path))
    except ConfigError as exc:
        _fail(str(exc))
        raise  # 让类型检查明白这里不会继续


def _select(targets: list[Target], target_id: str | None) -> list[Target]:
    if target_id is None:
        return [target for target in targets if target.enabled]
    selected = [target for target in targets if target.id == target_id]
    if not selected:
        _fail(f"没有 id 为 {target_id!r} 的目标（用 `webmoniter targets` 查看）")
    return selected


def _results_table(results: list[CheckResult]) -> Table:
    table = Table(title="检查结果", show_lines=False)
    table.add_column("目标", style="cyan", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("耗时", justify="right")
    table.add_column("详情")
    table.add_column("已通知", style="magenta")
    for result in results:
        style = STATUS_STYLE.get(result.status, "white")
        table.add_row(
            result.target_id,
            f"[{style}]{result.status}[/{style}]",
            format_elapsed(result.elapsed_ms),
            result.detail,
            ",".join(result.notified) or "-",
        )
    return table


async def _check_async(
    settings: Settings, targets: list[Target], *, notify: bool
) -> list[CheckResult]:
    runtime = await Runtime.create(settings, targets)
    try:
        scheduler = MonitorScheduler(
            targets=targets, watchers=runtime.watchers, settings=settings, health=runtime.health
        )
        results: list[CheckResult] = []
        for target in targets:
            result = await scheduler.run_one(target.id, notify=notify)
            if result is not None:
                results.append(result)
        return results
    finally:
        await runtime.aclose()


# ------------------------------------------------------------------------- 命令
@app.command()
def run(
    config: ConfigOpt = None,
    once: bool = typer.Option(False, "--once", help="所有目标检查一轮后退出（可用计划任务代替常驻）"),
    notify: bool = typer.Option(True, "--notify/--no-notify", help="是否发送通知"),
    log_level: str | None = typer.Option(None, "--log-level", help="覆盖日志级别，如 DEBUG"),
) -> None:
    """启动常驻监控（APScheduler）。"""
    settings = load_settings(config)
    setup_logging(log_level or settings.log_level, settings.resolve(settings.log_dir))
    targets = _load(settings)

    async def main() -> None:
        runtime = await Runtime.create(settings, targets)
        try:
            scheduler = MonitorScheduler(
                targets=targets, watchers=runtime.watchers, settings=settings, health=runtime.health
            )
            if once:
                results = await scheduler.run_all_once(notify=notify)
                console.print(_results_table(results))
                await runtime.health.tick()
                return
            await scheduler.run_forever()
        finally:
            await runtime.aclose()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]已手动停止[/yellow]")


@app.command()
def check(
    target_id: str | None = typer.Argument(None, help="目标 id；省略则检查全部启用目标"),
    config: ConfigOpt = None,
    notify: bool = typer.Option(
        False,
        "--notify",
        help="真的发送通知。默认只比对不发消息，但**会推进状态**（这条变更之后不会再告警）",
    ),
    show_diff: bool = typer.Option(False, "--show-diff", help="打印逐行 diff"),
    show_text: bool = typer.Option(False, "--show-text", help="打印抽取到的文本片段（排查选择器）"),
) -> None:
    """手动检查一次，不启动调度器。

    默认只比对、不发消息；但状态照样推进（last_hash 落库）——
    手动 check 会把这次变更「消费掉」，之后常驻进程不会再就它告警。
    """
    settings = load_settings(config)
    setup_logging("WARNING", None)
    targets = _select(_load(settings), target_id)
    results = asyncio.run(_check_async(settings, targets, notify=notify))
    if not results:
        console.print("[yellow]没有可检查的目标（可能都被禁用了，或抓取器不可用）[/yellow]")
        return
    console.print(_results_table(results))

    for result in results:
        if show_diff and result.unified:
            console.print(Panel(result.unified, title=f"{result.target_id} 差异", border_style="yellow"))
        if show_text and result.excerpt:
            console.print(
                Panel(
                    result.excerpt,
                    title=f"{result.target_id} 抽取内容（截断）",
                    border_style="cyan",
                )
            )

    if any(not result.ok for result in results):
        raise typer.Exit(1)


@app.command()
def targets(config: ConfigOpt = None) -> None:
    """列出配置中的监控目标。"""
    settings = load_settings(config)
    items = _load(settings)
    table = Table(title=f"监控目标（{settings.resolve(settings.config_path)}）")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("名称")
    table.add_column("间隔", justify="right")
    table.add_column("抓取")
    table.add_column("抽取规则")
    table.add_column("通知渠道", style="magenta")
    table.add_column("启用", justify="center")
    for target in items:
        rules = "\n".join(rule.describe() for rule in target.extract) or "整页文本"
        table.add_row(
            target.id,
            target.display_name,
            f"{target.interval}s",
            target.fetch,
            rules,
            ",".join(target.notify.channels) or "-",
            OK_MARK if target.enabled else FAIL_MARK,
        )
    console.print(table)


@app.command()
def channels() -> None:
    """列出从环境变量发现的通知渠道。"""
    found = discover_channels()
    if not found:
        console.print(
            "[yellow]没有发现任何渠道。[/yellow] 参考 .env.example 配置 "
            "FEISHU_WEBHOOK_<NAME> / DINGTALK_WEBHOOK_<NAME>"
        )
        return
    table = Table(title="通知渠道")
    table.add_column("渠道名", style="cyan")
    table.add_column("类型")
    table.add_column("加签", justify="center")
    table.add_column("关键词")
    table.add_column("webhook")
    for name, config in sorted(found.items()):
        masked = config.webhook[:60] + ("…" if len(config.webhook) > 60 else "")
        table.add_row(
            name,
            config.kind,
            OK_MARK if config.secret else FAIL_MARK,
            config.keyword or "-",
            masked,
        )
    console.print(table)


@app.command()
def changes(
    config: ConfigOpt = None,
    limit: int = typer.Option(20, "--limit", "-n", help="显示条数"),
) -> None:
    """查看最近的变更记录。"""
    settings = load_settings(config)
    setup_logging("WARNING", None)

    async def fetch() -> list[dict]:
        storage = Storage(settings.resolve(settings.db_path))
        await storage.init()
        try:
            return await storage.recent_changes(limit)
        finally:
            await storage.close()

    rows = asyncio.run(fetch())
    if not rows:
        console.print("[yellow]还没有任何变更记录[/yellow]")
        return
    table = Table(title=f"最近 {len(rows)} 条变更")
    table.add_column("时间", no_wrap=True)
    table.add_column("目标", style="cyan")
    table.add_column("+行", justify="right")
    table.add_column("-行", justify="right")
    table.add_column("相似度", justify="right")
    table.add_column("摘要")
    for row in rows:
        table.add_row(
            str(row.get("detected_at", "")),
            str(row.get("target_name") or row.get("target_id")),
            str(row.get("added", 0)),
            str(row.get("removed", 0)),
            f"{float(row.get('similarity') or 0):.4f}",
            str(row.get("summary", ""))[:60],
        )
    console.print(table)


@app.command("test-notify")
def test_notify(
    channel: str | None = typer.Argument(None, help="渠道名；省略则发给全部渠道"),
) -> None:
    """给渠道发一条测试消息，用于验证 webhook / 加签是否正确。"""
    found = discover_channels()
    if not found:
        _fail("没有发现任何渠道，请先配置 .env（参考 .env.example）")
    selected = [channel] if channel else sorted(found)
    unknown = [name for name in selected if name not in found]
    if unknown:
        _fail(f"未配置的渠道: {', '.join(unknown)}；已配置: {', '.join(sorted(found))}")

    message = NotificationMessage(
        target_id="__test__",
        kind="changed",
        title="WebMoniter 测试通知",
        lines=[
            "这是一条测试消息，收到即表示渠道配置正确。",
            f"发送时间：{iso(utcnow())}",
            f"版本：{__version__}",
        ],
        url="https://example.com",
        level="info",
    )

    async def send() -> list[tuple[str, bool, str]]:
        client = httpx.AsyncClient()
        try:
            notifiers = build_notifiers(client)
            results: list[tuple[str, bool, str]] = []
            for name in selected:
                notifier = notifiers.get(name)
                if notifier is None:
                    results.append((name, False, "未构造出通知器"))
                    continue
                result = await notifier.send(message)
                results.append((name, result.ok, result.detail))
            return results
        finally:
            await client.aclose()

    table = Table(title="测试通知结果")
    table.add_column("渠道", style="cyan")
    table.add_column("结果", justify="center")
    table.add_column("响应")
    failed = False
    for name, ok, detail in asyncio.run(send()):
        table.add_row(
            name, f"[green]{OK_MARK}[/green]" if ok else f"[red]{FAIL_MARK}[/red]", detail[:120]
        )
        failed = failed or not ok
    console.print(table)
    if failed:
        console.print(
            "[yellow]提示：[/yellow]HTTP 200 但业务码非 0 也算失败。常见原因：\n"
            "  1) 加签密钥不对，或服务器时间偏差超过 1 小时（检查 NTP）\n"
            "  2) 机器人安全设置选了「关键词」，但消息标题里没有该关键词\n"
            "  3) 机器人被移出群 / webhook 已重置\n"
            "  4) 触发频控（约 20 条/分钟）"
        )
        raise typer.Exit(1)


@app.command()
def silence(
    target_id: str = typer.Argument(..., help="目标 id"),
    hours: float = typer.Option(0.0, "--hours", help="静默多少小时；0 表示取消静默"),
    config: ConfigOpt = None,
) -> None:
    """临时静默某个目标的告警（例如已知的站点维护窗口）。"""
    settings = load_settings(config)
    setup_logging("WARNING", None)
    _select(_load(settings), target_id)  # 目标不存在就直接报错

    until = iso(utcnow() + timedelta(hours=hours)) if hours > 0 else None

    async def apply() -> None:
        storage = Storage(settings.resolve(settings.db_path))
        await storage.init()
        try:
            await storage.save_state(target_id, silenced_until=until)
        finally:
            await storage.close()

    asyncio.run(apply())
    if until:
        console.print(f"[yellow]已静默 {target_id}，至 {until}（UTC）[/yellow]")
    else:
        console.print(f"[green]已取消 {target_id} 的静默[/green]")


@app.command()
def doctor(config: ConfigOpt = None) -> None:
    """环境自检：配置、数据库、渠道、依赖。"""
    settings = load_settings(config)
    checks: list[tuple[bool, str]] = []

    # 1) 配置
    targets: list[Target] = []
    try:
        targets = load_targets(settings.resolve(settings.config_path))
        enabled = sum(1 for target in targets if target.enabled)
        checks.append((True, f"配置可加载：{len(targets)} 个目标（启用 {enabled} 个）"))
    except ConfigError as exc:
        checks.append((False, f"配置有问题：{exc}"))

    # 2) 数据库
    async def db_check() -> tuple[bool, str]:
        storage = Storage(settings.resolve(settings.db_path))
        age: float | None = None
        try:
            await storage.init()
            stats = await storage.stats()
            age = await storage.health_age_seconds()
        except Exception as exc:
            return False, f"数据库不可用：{type(exc).__name__}: {exc}"
        finally:
            await storage.close()
        detail = "，".join(f"{key}={value}" for key, value in stats.items())
        if age is not None:
            detail += f"，最近成功检查 {format_elapsed(age * 1000)} 前"
        return True, f"数据库可写（{settings.resolve(settings.db_path)}）：{detail}"

    checks.append(asyncio.run(db_check()))

    # 3) 渠道
    found = discover_channels()
    if not found:
        checks.append((False, "没有配置任何通知渠道（.env 里的 FEISHU_WEBHOOK_* / DINGTALK_WEBHOOK_*）"))
    else:
        for name, item in sorted(found.items()):
            if not item.secret and not item.keyword:
                checks.append(
                    (True, f"渠道 {name}（{item.kind}）既没有加签密钥也没有关键词，机器人可能拒收消息")
                )
            else:
                checks.append((True, f"渠道 {name}（{item.kind}，加签={'是' if item.secret else '否'}）"))

    # 4) 目标引用不到的渠道
    used = {channel for target in targets for channel in target.notify.channels}
    missing = sorted(used - set(found))
    if missing:
        checks.append((False, f"这些渠道被 targets.yaml 引用但未配置：{', '.join(missing)}"))
    elif used:
        checks.append((True, f"targets.yaml 引用的渠道都已配置：{', '.join(sorted(used))}"))

    # 5) Playwright（仅当有 browser 目标）
    browser_targets = [target.id for target in targets if target.fetch == "browser"]
    if browser_targets:
        try:
            import playwright  # noqa: F401

            checks.append((True, f"Playwright 已安装（browser 目标：{', '.join(browser_targets)}）"))
        except ImportError:
            checks.append(
                (False, f"目标 {', '.join(browser_targets)} 需要 browser 抓取，但未安装 Playwright："
                        "uv sync --extra browser && uv run playwright install chromium")
            )

    table = Table(title="doctor 自检")
    table.add_column("结果", justify="center", no_wrap=True)
    table.add_column("说明")
    problem = False
    for ok, message in checks:
        marker = f"[green]{OK_MARK}[/green]" if ok else f"[red]{FAIL_MARK}[/red]"
        table.add_row(marker, message)
        problem = problem or not ok
    console.print(table)

    if not found:
        console.print("[yellow]提示：[/yellow]先跑通 `webmoniter test-notify`，再启动 `webmoniter run`。")
    if problem:
        raise typer.Exit(1)


@app.command()
def version() -> None:
    """显示版本。"""
    console.print(f"webmoniter {__version__}")


if __name__ == "__main__":  # pragma: no cover
    app()
