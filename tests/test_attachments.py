"""附件可用性检查测试：链接提取、文件头核验、状态分类与端到端流程。"""

from __future__ import annotations

import collections
import functools
import http.server
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.attachments import MAX_ATTACHMENT_BYTES, inspect_attachment
from app.main import app
from app.models import AttachmentStatus
from app.parser import extract_attachment_links

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample-site"


# --------------------------------------------------------------------------- #
# extract_attachment_links
# --------------------------------------------------------------------------- #
def test_extract_attachment_links_keeps_only_same_site_pdf_zip():
    html = """
    <html><body>
      <a href="/files/a.pdf">招标文件</a>
      <a href="files/b.zip?x=1">模板压缩包</a>
      <a href="/files/a.pdf">招标文件重复引用</a>
      <a href="/files/c.PDF">大写扩展名</a>
      <a href="https://external.example.net/files/d.pdf">外站附件</a>
      <a href="/detail/other.html">普通页面链接</a>
      <a href="/files/readme.txt">非附件类型</a>
    </body></html>
    """
    links = extract_attachment_links(html, "http://localhost:8080/detail/n1.html")
    urls = [u for u, _ in links]
    assert urls == [
        "http://localhost:8080/files/a.pdf",
        "http://localhost:8080/detail/files/b.zip?x=1",  # 相对路径基于详情页解析
        "http://localhost:8080/files/c.PDF",
    ]
    assert dict(links)["http://localhost:8080/files/a.pdf"] == "招标文件"


# --------------------------------------------------------------------------- #
# inspect_attachment：MockTransport 覆盖各类响应形态
# --------------------------------------------------------------------------- #
def _pdf_bytes(size: int = 64) -> bytes:
    body = b"%PDF-1.4\n" + b"x" * max(0, size - 8)
    return body


def _zip_bytes(size: int = 64) -> bytes:
    return b"PK\x03\x04" + b"z" * max(0, size - 4)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def _routes(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path == "/ok.pdf":
        return httpx.Response(200, content=_pdf_bytes())
    if path == "/ok.zip":
        return httpx.Response(200, content=_zip_bytes())
    if path == "/tiny.pdf":  # 小于嗅探窗口的小文件
        return httpx.Response(200, content=b"%PDF-")
    if path == "/missing.pdf":
        return httpx.Response(404, content=b"not found")
    if path == "/gone.zip":
        return httpx.Response(410, content=b"gone")
    if path == "/error.pdf":
        return httpx.Response(500, content=b"server error")
    if path == "/disguised.pdf":
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<!doctype html><html><body><h1>login</h1></body></html>",
        )
    if path == "/notpdf.pdf":
        return httpx.Response(200, content=b"this is plain text, not a pdf")
    if path == "/empty.pdf":
        return httpx.Response(200, content=b"")
    if path == "/declared-big.pdf":
        return httpx.Response(
            200,
            headers={"content-length": str(MAX_ATTACHMENT_BYTES + 1)},
            content=_pdf_bytes(),
        )
    if path == "/stream-big.pdf":  # 无 Content-Length，流式累计超限
        async def body():
            yield _pdf_bytes(128)
            remaining = MAX_ATTACHMENT_BYTES
            while remaining > 0:
                chunk = b"y" * 65536
                remaining -= len(chunk)
                yield chunk
        return httpx.Response(200, content=body())
    if path == "/slow.pdf":
        raise httpx.ReadTimeout("read timed out", request=req)
    if path == "/offsite.pdf":
        return httpx.Response(302, headers={"location": "https://external.example.net/f.pdf"})
    if path == "/hop.pdf":
        return httpx.Response(302, headers={"location": "/ok.pdf"})
    if path == "/loop.pdf":
        return httpx.Response(302, headers={"location": "/loop.pdf"})
    return httpx.Response(404, content=b"unknown")


@pytest.fixture
def client():
    return _mock_client(_routes)


async def test_pdf_and_zip_pass_magic_check(client):
    for url in ("http://site/ok.pdf", "http://site/ok.zip", "http://site/tiny.pdf"):
        verdict = await inspect_attachment(client, url)
        assert verdict.status == AttachmentStatus.AVAILABLE, url
        assert "文件头校验通过" in verdict.reason


async def test_http_errors_are_unavailable(client):
    for url, status in (("/missing.pdf", 404), ("/gone.zip", 410), ("/error.pdf", 500)):
        verdict = await inspect_attachment(client, f"http://site{url}")
        assert verdict.status == AttachmentStatus.UNAVAILABLE
        assert verdict.http_status == status
        assert str(status) in verdict.reason


async def test_login_html_disguise_is_unavailable(client):
    verdict = await inspect_attachment(client, "http://site/disguised.pdf")
    assert verdict.status == AttachmentStatus.UNAVAILABLE
    assert "HTML" in verdict.reason


async def test_magic_mismatch_and_empty_body_are_unavailable(client):
    verdict = await inspect_attachment(client, "http://site/notpdf.pdf")
    assert verdict.status == AttachmentStatus.UNAVAILABLE
    assert "%PDF-" in verdict.reason

    verdict = await inspect_attachment(client, "http://site/empty.pdf")
    assert verdict.status == AttachmentStatus.UNAVAILABLE
    assert "为空" in verdict.reason


async def test_oversize_declared_and_streaming_are_unknown(client):
    verdict = await inspect_attachment(client, "http://site/declared-big.pdf")
    assert verdict.status == AttachmentStatus.UNKNOWN
    assert "上限" in verdict.reason
    assert verdict.bytes_read == 0  # 预检超限，未读取正文

    verdict = await inspect_attachment(client, "http://site/stream-big.pdf")
    assert verdict.status == AttachmentStatus.UNKNOWN
    assert "上限" in verdict.reason
    assert verdict.bytes_read > MAX_ATTACHMENT_BYTES  # 流式累计超限即停


async def test_timeout_is_unknown(client):
    verdict = await inspect_attachment(client, "http://site/slow.pdf")
    assert verdict.status == AttachmentStatus.UNKNOWN
    assert "超时" in verdict.reason


async def test_offsite_redirect_stops_reading(client):
    verdict = await inspect_attachment(client, "http://site/offsite.pdf")
    assert verdict.status == AttachmentStatus.UNKNOWN
    assert "站外" in verdict.reason
    assert "external.example.net" in verdict.reason


async def test_same_site_redirect_is_followed(client):
    verdict = await inspect_attachment(client, "http://site/hop.pdf")
    assert verdict.status == AttachmentStatus.AVAILABLE


async def test_redirect_loop_is_unknown(client):
    verdict = await inspect_attachment(client, "http://site/loop.pdf")
    assert verdict.status == AttachmentStatus.UNKNOWN
    assert "重定向" in verdict.reason


# --------------------------------------------------------------------------- #
# 端到端：本地静态服务器 + FastAPI
# --------------------------------------------------------------------------- #
class _CountingHandler(http.server.SimpleHTTPRequestHandler):
    """记录每个路径的 GET 次数，用于验证“同一文件只下载一次”。"""

    hits: collections.Counter = collections.Counter()

    def log_message(self, *args):  # 静默测试输出
        pass

    def do_GET(self):
        type(self).hits[self.path] += 1
        super().do_GET()


@pytest.fixture(scope="module")
def static_server():
    handler = functools.partial(_CountingHandler, directory=str(SAMPLE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}/list.html"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def api_client():
    with TestClient(app) as c:
        yield c


def _check(api_client, list_url, code):
    resp = api_client.post(
        "/api/v1/attachments/check",
        json={"list_url": list_url, "project_code": code, "timeout": 5},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_project_001_mixed_attachment_outcomes(api_client, static_server):
    data = _check(api_client, static_server, "GSB-2026-001")

    # 5 个去重附件：2 正常 + 登录伪装 + 内容损坏 + 404
    assert data["total_attachments"] == 5
    assert data["available"] == 2
    assert data["unavailable"] == 3
    assert data["unknown"] == 0
    assert data["failed_notices"] == 0
    assert data["all_available"] is False
    assert data["max_bytes"] == MAX_ATTACHMENT_BYTES

    findings = {f["url"].rsplit("/", 1)[-1]: f for f in data["findings"]}
    assert findings["GSB-2026-001-tender.pdf"]["status"] == "available"
    assert findings["GSB-2026-001-docs.zip"]["status"] == "available"
    assert findings["needs-login.pdf"]["status"] == "unavailable"
    assert "HTML" in findings["needs-login.pdf"]["reason"]
    assert findings["broken.pdf"]["status"] == "unavailable"
    assert "%PDF-" in findings["broken.pdf"]["reason"]
    assert findings["missing.zip"]["status"] == "unavailable"
    assert findings["missing.zip"]["http_status"] == 404

    # 同一文件被原公告与更正公告引用：逐公告保留引用，但只下载一次
    tender = findings["GSB-2026-001-tender.pdf"]
    assert len(tender["sources"]) == 2
    source_urls = {s["notice_url"].rsplit("/", 1)[-1] for s in tender["sources"]}
    assert source_urls == {"g001-original.html", "g001-corr1.html"}
    assert _CountingHandler.hits["/files/GSB-2026-001-tender.pdf"] == 1

    # 逐公告报告：三条公告各自的附件引用与结论
    notices = {n["url"].rsplit("/", 1)[-1]: n for n in data["notices"]}
    assert set(notices) == {"g001-original.html", "g001-corr1.html", "g001-corr2.html"}
    assert all(n["fetched"] for n in notices.values())
    assert [a["url"].rsplit("/", 1)[-1] for a in notices["g001-original.html"]["attachments"]] == [
        "GSB-2026-001-tender.pdf",
        "GSB-2026-001-docs.zip",
    ]
    corr1_statuses = {a["url"].rsplit("/", 1)[-1]: a["status"] for a in notices["g001-corr1.html"]["attachments"]}
    assert corr1_statuses == {"GSB-2026-001-tender.pdf": "available", "needs-login.pdf": "unavailable"}


def test_project_002_all_attachments_available(api_client, static_server):
    data = _check(api_client, static_server, "GSB-2026-002")

    assert data["total_attachments"] == 1
    assert data["available"] == 1
    assert data["unavailable"] == 0
    assert data["unknown"] == 0
    assert data["all_available"] is True


def test_project_003_failed_detail_forces_not_all_available(api_client, static_server):
    data = _check(api_client, static_server, "GSB-2026-003")

    # 更正公告详情页 404 → 待核实项存在，绝不能报告整项目附件均可用
    assert data["failed_notices"] == 1
    assert data["all_available"] is False
    failed = [n for n in data["notices"] if not n["fetched"]]
    assert len(failed) == 1
    assert "g003-missing.html" in failed[0]["url"]
    assert "待核实" in failed[0]["reason"]
    assert any("待核实" in w for w in data["warnings"])


def test_unknown_project_is_not_reported_available(api_client, static_server):
    data = _check(api_client, static_server, "GSB-2099-NOPE")
    assert data["total_attachments"] == 0
    assert data["all_available"] is False
    assert any("未发现该项目的公告" in w for w in data["warnings"])


def test_get_endpoint_matches_post(api_client, static_server):
    resp = api_client.get(
        "/api/v1/attachments/check",
        params={"list_url": static_server, "project_code": "GSB-2026-002"},
    )
    assert resp.status_code == 200
    assert resp.json()["all_available"] is True


def test_bad_list_url_returns_502(api_client):
    resp = api_client.post(
        "/api/v1/attachments/check",
        json={"list_url": "http://127.0.0.1:1/never-listens.html", "project_code": "X", "timeout": 2},
    )
    assert resp.status_code == 502
