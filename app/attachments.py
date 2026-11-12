"""附件可用性检查：从公告详情提取本站附件，流式核验文件是否真实可下载。

检查规则（对应采购发布员场景：“链接能打开，却下载不到文件”）：
* 只检查详情页中指向**本站**的 PDF/ZIP 附件链接；
* 同一文件链接只下载一次，结论共享给所有引用它的公告；
* 流式读取，单个附件最多读取 10 MiB，超限即停并列为**待核实**；
* 核对文件头（PDF：``%PDF-``；ZIP：``PK`` 签名），格式不符列为**不可用**；
* 200 响应但返回 HTML（登录页/错误页伪装）同样列为**不可用**；
* 附件链接跳转到站外地址时**停止读取**，列为**待核实**；
* 404 / 其他 4xx/5xx 列为**不可用**；超时、网络错误列为**待核实**；
* 关联详情页读取失败时，该公告的附件情况列为**待核实**，
  存在任何待核实项即不得报告整项目附件均可用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx

from app.collector import _MAX_CONCURRENCY, build_client, scan_project
from app.models import (
    AttachmentCheckRequest,
    AttachmentCheckResponse,
    AttachmentFinding,
    AttachmentRef,
    AttachmentSource,
    AttachmentStatus,
    NoticeAttachmentReport,
)
from app.parser import extract_attachment_links, same_site

# 单个附件流式读取上限：10 MiB
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
# 文件头嗅探窗口（覆盖 BOM/前导空白 + 常见魔数长度）
_SNIFF_BYTES = 512
# 同站重定向最大跟随次数
_MAX_REDIRECTS = 5
# 流式读取块大小
_CHUNK_SIZE = 65536

_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


@dataclass
class AttachmentVerdict:
    """单个附件 URL 的原始检查结论（内部使用）。"""

    status: AttachmentStatus
    reason: str
    http_status: int | None = None
    content_type: str | None = None
    bytes_read: int = 0


def _looks_like_html(head: bytes) -> bool:
    """判断响应内容头部是否为 HTML（登录页/错误页伪装成附件）。"""
    sample = head.lstrip()
    if sample.startswith(b"\xef\xbb\xbf"):  # UTF-8 BOM
        sample = sample[3:].lstrip()
    lowered = sample.lower()
    return lowered.startswith(b"<!doctype html") or lowered.startswith(b"<html")


def _sniff_head(head: bytes, url: str) -> AttachmentVerdict | None:
    """核对文件头；返回 None 表示头部符合预期，否则给出不可用结论。"""
    sample = head.lstrip()
    if sample.startswith(b"\xef\xbb\xbf"):
        sample = sample[3:].lstrip()
    if not sample:
        return AttachmentVerdict(
            status=AttachmentStatus.UNAVAILABLE,
            reason="响应内容为空，附件不可下载",
        )
    if _looks_like_html(head):
        return AttachmentVerdict(
            status=AttachmentStatus.UNAVAILABLE,
            reason="服务器返回 HTML 页面（疑似登录页或错误页），附件实际不可下载",
        )
    ext = urlparse(url).path.lower().rsplit(".", 1)[-1]
    if ext == "pdf" and not sample.startswith(b"%PDF-"):
        return AttachmentVerdict(
            status=AttachmentStatus.UNAVAILABLE,
            reason="文件头不是 %PDF- 签名，文件格式不符或已损坏",
        )
    if ext == "zip" and sample[:4] not in _ZIP_SIGNATURES:
        return AttachmentVerdict(
            status=AttachmentStatus.UNAVAILABLE,
            reason="文件头不是 PK 签名，文件格式不符或已损坏",
        )
    return None


async def inspect_attachment(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int = MAX_ATTACHMENT_BYTES,
) -> AttachmentVerdict:
    """流式检查单个附件 URL 是否真实可下载。

    手动处理重定向：同站跳转继续跟随（限 ``_MAX_REDIRECTS`` 次），
    跳转站外则立即停止读取并列为待核实。
    """
    current = url
    redirects = 0
    while True:
        try:
            async with client.stream("GET", current, follow_redirects=False) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    target = urljoin(current, location) if location else ""
                    if not target or not same_site(target, url):
                        return AttachmentVerdict(
                            status=AttachmentStatus.UNKNOWN,
                            reason=f"附件链接跳转至站外地址，已停止读取，需人工核实：{target or '未知目标'}",
                            http_status=resp.status_code,
                        )
                    redirects += 1
                    if redirects > _MAX_REDIRECTS:
                        return AttachmentVerdict(
                            status=AttachmentStatus.UNKNOWN,
                            reason="同站重定向次数过多，已停止读取，需人工核实",
                            http_status=resp.status_code,
                        )
                    current = target
                    continue

                status = resp.status_code
                content_type = resp.headers.get("content-type")
                if status == 404:
                    return AttachmentVerdict(
                        status=AttachmentStatus.UNAVAILABLE,
                        reason="HTTP 404：附件不存在或已被删除",
                        http_status=status,
                        content_type=content_type,
                    )
                if status >= 400:
                    return AttachmentVerdict(
                        status=AttachmentStatus.UNAVAILABLE,
                        reason=f"HTTP {status}：服务器拒绝提供附件",
                        http_status=status,
                        content_type=content_type,
                    )
                if status != 200:
                    return AttachmentVerdict(
                        status=AttachmentStatus.UNKNOWN,
                        reason=f"HTTP {status}：非预期响应，需人工核实",
                        http_status=status,
                        content_type=content_type,
                    )

                # Content-Length 预检：已知超限则不读取正文
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    return AttachmentVerdict(
                        status=AttachmentStatus.UNKNOWN,
                        reason=f"附件声明大小 {int(declared)} 字节，超过 10 MiB 读取上限，已停止读取，需人工核实",
                        http_status=status,
                        content_type=content_type,
                    )

                # 流式读取：先嗅探文件头，再累计大小直至结束或超限
                head = bytearray()
                head_ok = False
                total = 0
                oversize = False
                async for chunk in resp.aiter_bytes(_CHUNK_SIZE):
                    total += len(chunk)
                    if not head_ok:
                        head.extend(chunk[: _SNIFF_BYTES - len(head)])
                        if len(head) >= _SNIFF_BYTES:
                            verdict = _sniff_head(bytes(head), url)
                            if verdict is not None:
                                verdict.http_status = status
                                verdict.content_type = content_type
                                verdict.bytes_read = total
                                return verdict  # 头部已判定不可用，停止读取
                            head_ok = True
                    if total > max_bytes:
                        oversize = True
                        break

                if oversize:
                    return AttachmentVerdict(
                        status=AttachmentStatus.UNKNOWN,
                        reason="附件超过 10 MiB 读取上限，已停止读取，需人工核实",
                        http_status=status,
                        content_type=content_type,
                        bytes_read=total,
                    )
                if not head_ok:
                    # 小文件（不足嗅探窗口）：流已结束，用全部内容核对文件头
                    verdict = _sniff_head(bytes(head), url)
                    if verdict is not None:
                        verdict.http_status = status
                        verdict.content_type = content_type
                        verdict.bytes_read = total
                        return verdict
                ext = urlparse(url).path.lower().rsplit(".", 1)[-1].upper()
                return AttachmentVerdict(
                    status=AttachmentStatus.AVAILABLE,
                    reason=f"文件头校验通过（{ext}），附件可下载",
                    http_status=status,
                    content_type=content_type,
                    bytes_read=total,
                )
        except httpx.TimeoutException:
            return AttachmentVerdict(
                status=AttachmentStatus.UNKNOWN,
                reason="请求超时，附件可用性需人工核实",
            )
        except httpx.HTTPError as exc:
            return AttachmentVerdict(
                status=AttachmentStatus.UNKNOWN,
                reason=f"网络错误（{exc.__class__.__name__}），附件可用性需人工核实",
            )


@dataclass
class _NoticeAttachments:
    """单条公告与其附件链接（检查前）。"""

    page_url: str
    title: str
    kind: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (附件URL, 链接文本)


async def check_attachments(req: AttachmentCheckRequest) -> AttachmentCheckResponse:
    """附件可用性检查编排：沿用本轮采集的公告集合，逐附件流式核验。"""
    list_url = str(req.list_url)
    async with build_client(req.timeout) as client:
        scan = await scan_project(client, list_url, req.project_code)

        # 1) 从每条公告详情提取本站附件链接；同一 URL 全局只登记一次下载任务
        per_notice: list[_NoticeAttachments] = []
        unique_urls: list[str] = []
        seen: set[str] = set()
        for page in scan.pages:
            notice_url = str(page.record.url)
            links = extract_attachment_links(page.html, notice_url)
            per_notice.append(
                _NoticeAttachments(
                    page_url=notice_url,
                    title=page.record.title,
                    kind=page.record.kind.value,
                    links=links,
                )
            )
            for attachment_url, _ in links:
                if attachment_url not in seen:
                    seen.add(attachment_url)
                    unique_urls.append(attachment_url)

        # 2) 并发检查附件（同一文件链接只下载一次）
        semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

        async def inspect_one(attachment_url: str) -> AttachmentVerdict:
            async with semaphore:
                return await inspect_attachment(client, attachment_url)

        verdicts = dict(
            zip(unique_urls, await asyncio.gather(*(inspect_one(u) for u in unique_urls)))
        )

    # 3) 组装逐公告报告与去重后的逐附件结论
    findings: list[AttachmentFinding] = []
    notice_reports: list[NoticeAttachmentReport] = []

    # 附件 URL → 引用它的来源公告
    sources_by_url: dict[str, list[AttachmentSource]] = {u: [] for u in unique_urls}
    for notice in per_notice:
        for attachment_url, link_text in notice.links:
            sources_by_url[attachment_url].append(
                AttachmentSource(
                    notice_url=notice.page_url,
                    notice_title=notice.title,
                    notice_kind=notice.kind,
                    link_text=link_text or None,
                )
            )

    for attachment_url in unique_urls:
        verdict = verdicts[attachment_url]
        findings.append(
            AttachmentFinding(
                url=attachment_url,
                status=verdict.status,
                reason=verdict.reason,
                http_status=verdict.http_status,
                content_type=verdict.content_type,
                bytes_read=verdict.bytes_read,
                sources=sources_by_url[attachment_url],
            )
        )

    for notice in per_notice:
        refs = [
            AttachmentRef(
                url=attachment_url,
                link_text=link_text or None,
                status=verdicts[attachment_url].status,
                reason=verdicts[attachment_url].reason,
            )
            for attachment_url, link_text in notice.links
        ]
        notice_reports.append(
            NoticeAttachmentReport(
                url=notice.page_url,
                title=notice.title,
                kind=notice.kind,
                fetched=True,
                attachments=refs,
            )
        )

    # 关联详情页读取失败 → 该公告附件情况列为待核实
    warnings: list[str] = []
    for failure in scan.failures:
        notice_reports.append(
            NoticeAttachmentReport(
                url=failure.url,
                title=failure.link_text or None,
                kind=None,
                fetched=False,
                reason=f"{failure.reason}，该公告的附件情况列为待核实",
            )
        )
        warnings.append(f"关联详情页读取失败，其附件情况待核实：{failure.url}")

    available = sum(1 for f in findings if f.status == AttachmentStatus.AVAILABLE)
    unavailable = sum(1 for f in findings if f.status == AttachmentStatus.UNAVAILABLE)
    unknown = sum(1 for f in findings if f.status == AttachmentStatus.UNKNOWN)

    if scan.candidates == 0:
        warnings.append("列表页未发现任何同站点详情链接")
    if not notice_reports:
        warnings.append("列表页未发现该项目的公告，无法检查附件")
    elif not findings and not scan.failures:
        warnings.append("该项目公告中未发现指向本站的 PDF/ZIP 附件链接")

    # 关键约束：存在任何待核实项（附件未知/详情失败）或不可用附件，
    # 都不得报告整项目附件均可用；未发现公告时同样不得报告。
    all_available = (
        bool(notice_reports)
        and unavailable == 0
        and unknown == 0
        and not scan.failures
    )

    return AttachmentCheckResponse(
        project_code=req.project_code,
        list_url=list_url,
        all_available=all_available,
        total_attachments=len(findings),
        available=available,
        unavailable=unavailable,
        unknown=unknown,
        failed_notices=len(scan.failures),
        max_bytes=MAX_ATTACHMENT_BYTES,
        notices=notice_reports,
        findings=findings,
        warnings=warnings,
        checked_at=datetime.now(timezone.utc),
    )
