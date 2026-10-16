"""抽取层测试：用 BeautifulSoup 按规则取值。"""

from __future__ import annotations

from webmoniter.extract import build_soup, extract, resolve_path
from webmoniter.models import ExtractRule

HTML = """
<html><head><style>.a{color:red}</style><script>var x=1;</script></head>
<body>
  <div class="list">
    <ul><li>公告一</li><li>公告二</li></ul>
  </div>
  <a class="next" href="/page/2">下一页</a>
  <span id="count">共 42 条</span>
  <div class="ad">广告内容</div>
</body></html>
"""


def test_css_text_mode() -> None:
    soup = build_soup(HTML, strip_selectors=["script", "style", ".ad"])
    text = extract(soup, HTML, [ExtractRule(type="css", selector="div.list ul")])
    assert "公告一" in text
    assert "公告二" in text
    assert "广告内容" not in text  # 被 strip_selectors 摘掉
    assert "var x" not in text


def test_css_attr_mode() -> None:
    soup = build_soup(HTML)
    rule = ExtractRule(type="css", selector="a.next", mode="attr", attr="href")
    assert extract(soup, HTML, [rule]) == "/page/2"


def test_regex_group() -> None:
    soup = build_soup(HTML)
    rule = ExtractRule(type="regex", pattern=r"共\s*(\d+)\s*条", group=1)
    assert extract(soup, HTML, [rule]) == "42"


def test_json_rule_sorts_keys() -> None:
    payload = '{"data": {"status": "ok", "n": 3}}'
    rule = ExtractRule(type="json", path="data")
    text = extract(build_soup(payload), payload, [rule])
    # 键排序后输出稳定，字段顺序变化不会产生假变更
    assert text.index('"n"') < text.index('"status"')


def test_missing_selector_yields_empty_string() -> None:
    """选择器失效时必须返回空串——上层据此判定为错误，而不是「内容被清空」。"""
    soup = build_soup(HTML)
    rule = ExtractRule(type="css", selector="div.does-not-exist")
    assert extract(soup, HTML, [rule]) == ""


def test_no_rules_falls_back_to_whole_page_text() -> None:
    soup = build_soup(HTML, strip_selectors=["script", "style"])
    text = extract(soup, HTML, [])
    assert "公告一" in text
    assert "var x" not in text


def test_resolve_path() -> None:
    obj = {"data": {"services": [{"status": "ok"}, {"status": "down"}]}}
    assert resolve_path(obj, "data.services[1].status") == "down"
    assert resolve_path(obj, "data.services[5].status") is None
    assert resolve_path(obj, "data.missing") is None
    assert resolve_path(obj, "data.services.status") is None
