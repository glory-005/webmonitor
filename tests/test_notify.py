"""通知层测试：加签算法、消息拼装、渠道发现、业务码判定。

加签是这类项目里最容易写错的地方（飞书与钉钉的 hmac key / 消息体、时间戳单位都不一样），
所以这里把「公式」写成断言钉住。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse

import httpx
import respx

from webmoniter.models import ChannelConfig, NotificationMessage
from webmoniter.notify import dingtalk, feishu
from webmoniter.notify.dingtalk import DingTalkNotifier, build_markdown
from webmoniter.notify.feishu import FeishuNotifier, build_card
from webmoniter.notify.registry import discover_channels

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=yyy"

MESSAGE = NotificationMessage(
    target_id="t1",
    kind="changed",
    title="某公告列表 内容有变化",
    lines=["**变化量**：新增 1 行", "+ 招标公告"],
    url="https://example.com/page",
    level="warning",
    mention_all=True,
)


# ----------------------------------------------------------------- 加签算法
def test_feishu_sign_uses_string_to_sign_as_key_and_empty_body() -> None:
    timestamp = 1700000000
    secret = "topsecret"
    string_to_sign = f"{timestamp}\n{secret}"
    expected = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode()

    assert feishu.sign(timestamp, secret) == expected
    assert len(base64.b64decode(feishu.sign(timestamp, secret))) == 32


def test_dingtalk_sign_uses_secret_as_key_and_urlencodes() -> None:
    timestamp_ms = 1700000000000
    secret = "SECtopsecret"
    string_to_sign = f"{timestamp_ms}\n{secret}"
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode()

    signature = dingtalk.sign(timestamp_ms, secret)
    assert urllib.parse.unquote_plus(signature) == expected
    # 必须做过 urlencode：把原始 base64 再编码一次应当得到同一个结果
    assert urllib.parse.quote_plus(expected) == signature


def test_the_two_algorithms_are_not_interchangeable() -> None:
    """同一个 secret，两个平台的签名必须不同——防止抄代码时把参数顺序搞反。"""
    assert feishu.sign(1700000000, "s") != dingtalk.sign(1700000000, "s")


# ------------------------------------------------------------------- 消息拼装
def test_feishu_card_shape() -> None:
    card = build_card(MESSAGE)
    assert card["header"]["template"] == "orange"
    assert card["header"]["title"]["content"] == MESSAGE.title
    body = card["elements"][0]["text"]["content"]
    assert '<at user_id="all">' in body
    assert MESSAGE.url in body


def test_dingtalk_markdown_shape() -> None:
    text = build_markdown(MESSAGE)
    assert text.startswith("@所有人")
    assert MESSAGE.url in text


async def test_keyword_is_prepended_to_title() -> None:
    config = ChannelConfig(name="feishu_ops", kind="feishu", webhook=FEISHU_WEBHOOK, keyword="监控")
    async with httpx.AsyncClient() as client:
        notifier = FeishuNotifier(config, client)
        assert notifier.decorate_title("某公告列表 内容有变化").startswith("[监控]")
        assert notifier.decorate_title("监控：某公告列表有变化") == "监控：某公告列表有变化"


# --------------------------------------------------------------- 渠道发现
def test_discover_channels_from_env() -> None:
    environ = {
        "FEISHU_WEBHOOK_OPS": FEISHU_WEBHOOK,
        "FEISHU_SECRET_OPS": "secret1",
        "DINGTALK_WEBHOOK_OPS": DINGTALK_WEBHOOK,
        "DINGTALK_KEYWORD_OPS": "监控",
        "FEISHU_WEBHOOK_": "空的后缀应被忽略",
        "DINGTALK_WEBHOOK_DEV": "   ",
        "UNRELATED": "x",
    }
    found = discover_channels(environ)

    assert set(found) == {"feishu_ops", "dingtalk_ops"}
    assert found["feishu_ops"].kind == "feishu"
    assert found["feishu_ops"].secret == "secret1"
    assert found["dingtalk_ops"].keyword == "监控"
    assert found["dingtalk_ops"].secret is None


def test_discover_channels_returns_nothing_when_unset() -> None:
    assert discover_channels({}) == {}


# --------------------------------------------------- 发送：请求体与业务码判定
async def test_feishu_send_attaches_sign_and_parses_business_code() -> None:
    config = ChannelConfig(name="feishu_ops", kind="feishu", webhook=FEISHU_WEBHOOK, secret="s3cr3t")
    with respx.mock:
        route = respx.post(FEISHU_WEBHOOK).mock(
            return_value=httpx.Response(200, json={"code": 0, "msg": "success"})
        )
        async with httpx.AsyncClient() as client:
            result = await FeishuNotifier(config, client).send(MESSAGE)

        assert result.ok
        payload = json.loads(route.calls.last.request.content)
        assert payload["msg_type"] == "interactive"
        assert payload["timestamp"].isdigit() and len(payload["timestamp"]) == 10  # 秒
        assert payload["sign"]


async def test_feishu_send_treats_nonzero_code_as_failure() -> None:
    config = ChannelConfig(name="feishu_ops", kind="feishu", webhook=FEISHU_WEBHOOK)
    with respx.mock:
        respx.post(FEISHU_WEBHOOK).mock(
            return_value=httpx.Response(200, json={"code": 19021, "msg": "sign match fail"})
        )
        async with httpx.AsyncClient() as client:
            result = await FeishuNotifier(config, client).send(MESSAGE)

    # HTTP 200 但业务失败——只看状态码的实现会误判成成功
    assert not result.ok
    assert "19021" in result.detail


async def test_dingtalk_send_signs_query_and_checks_errcode() -> None:
    config = ChannelConfig(name="dingtalk_ops", kind="dingtalk", webhook=DINGTALK_WEBHOOK, secret="SEC")
    with respx.mock:
        route = respx.post(url__regex=r"https://oapi\.dingtalk\.com/robot/send.*").mock(
            return_value=httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        )
        async with httpx.AsyncClient() as client:
            result = await DingTalkNotifier(config, client).send(MESSAGE)

        assert result.ok
        request_url = str(route.calls.last.request.url)
        assert "timestamp=" in request_url and "sign=" in request_url
        payload = json.loads(route.calls.last.request.content)
        assert payload["msgtype"] == "markdown"
        assert payload["at"]["isAtAll"] is True


async def test_dingtalk_send_reports_error_code() -> None:
    config = ChannelConfig(name="dingtalk_ops", kind="dingtalk", webhook=DINGTALK_WEBHOOK)
    with respx.mock:
        respx.post(url__regex=r"https://oapi\.dingtalk\.com/robot/send.*").mock(
            return_value=httpx.Response(200, json={"errcode": 310000, "errmsg": "keywords not in content"})
        )
        async with httpx.AsyncClient() as client:
            result = await DingTalkNotifier(config, client).send(MESSAGE)

    assert not result.ok
    assert "310000" in result.detail
