"""抓取层：把 URL 变成 FetchResult。"""

from webmoniter.fetch.base import Fetcher, FetchResult
from webmoniter.fetch.http import HttpFetcher, decode_body

__all__ = ["FetchResult", "Fetcher", "HttpFetcher", "decode_body"]
