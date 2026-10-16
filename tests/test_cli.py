"""CLI 层测试。

CLI 是整套代码里唯一没有业务测试覆盖的部分，而它恰恰踩过两个坑：
1. 在 `Annotated` 里给 `typer.Option` 传默认值 → 启动即 `AttributeError: 'NoneType'
   object has no attribute 'isidentifier'`（Typer 会把第一个位置参数当成参数声明）；
2. 在 GBK 控制台输出 ✓/✗ → `UnicodeEncodeError` 直接把命令打崩。
这两条都值得钉成回归测试——构建命令树不需要真的执行命令，成本极低。
"""

from __future__ import annotations

from typer.main import get_command

from webmoniter.cli import FAIL_MARK, OK_MARK, app

EXPECTED_COMMANDS = {
    "run",
    "check",
    "targets",
    "channels",
    "changes",
    "test-notify",
    "silence",
    "doctor",
    "version",
}


def build_command_tree():
    """复现 `webmoniter xxx` 启动时的参数解析路径（会走 get_click_param）。"""
    return get_command(app)


def option_declarations(command_name: str) -> set[str]:
    command = build_command_tree().commands[command_name]
    # Argument 没有 opts 属性，用 getattr 兜住
    return {decl for param in command.params for decl in getattr(param, "opts", ())}


def test_command_tree_builds() -> None:
    """参数声明写错（例如 Annotated 里传了默认值）会在这里就炸，而不是等用户执行。"""
    command = build_command_tree()
    assert command is not None
    assert command.commands, "没有注册任何子命令"


def test_all_commands_are_registered() -> None:
    assert EXPECTED_COMMANDS <= set(build_command_tree().commands)


def test_config_option_is_declared_on_every_command() -> None:
    """每个带 --config 的命令都要能正常解析出这个选项（长短写法都在）。"""
    for name in ("run", "check", "targets", "changes", "silence", "doctor"):
        decls = option_declarations(name)
        assert "--config" in decls, f"{name} 缺少 --config"
        assert "-c" in decls, f"{name} 缺少 -c"


def test_status_marks_are_gbk_encodable() -> None:
    """✓/✗（U+2713/U+2717）不在 cp936 里，写进 GBK 控制台会崩；别再改回去。"""
    for mark in (OK_MARK, FAIL_MARK):
        assert mark.encode("gbk")
