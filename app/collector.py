"""采集编排：列表页发现 → 详情页抓取 → 字段解析 → 业务合并。

``scan_project`` 是采集与附件检查共用的前置流程：
列表页发现候选链接 → 并发抓取详情 → 项目归属判定，
返回属于目标项目的公告（含详情页原始 HTML）与关联失败页面。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

from app.merger import MergeInput, merge_notices
from app.models import (
    FetchRequest,
    FetchResponse,
    NoticeKind,
    NoticeRecord,
    ProjectStatus,
    SourceLink,
)
from app.parser import ParseError, extract_detail_links, parse_detail

_MAX_CONCURRENCY = 5
_USER_AGENT = "TenderCollector/1.0 (+local procurement announcement API)"
# 项目编号匹配时容忍大小写/空格/分隔符差异，如 “GSB-2026-001” 与 “GSB 2026 001”
_CODE_NORM = str.maketrans({" ": "", "　": "", "_": "", "/": "", "\\": "", "-": ""})
# 合理公告年份窗口，过滤“近三年（2023年1月1日…）”这类业绩/资质说明里的日期
_MIN_YEAR = 2000
_MAX_YEAR = 2100


def _normalize_code(code: str) -> str:
    return code.strip().lower().translate(_CODE_NORM)


def _code_matches(page_code: str | None, requested: str, title: str, row_context: str) -> bool:
    target = _normalize_code(requested)
    if page_code and _normalize_code(page_code) == target:
        return True
    # 详情页没有结构化编号时，退化为标题/列表行上下文包含匹配
    haystack = _normalize_code(f"{title} {row_context}")
    return target in haystack


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    # 4xx/5xx（如详情页下线返回 404）一律视为抓取失败
    response.raise_for_status()
    return response.text


@dataclass
class ScannedPage:
    """成功抓取且判定属于目标项目的详情页（保留原始 HTML，供附件提取复用）。"""

    record: NoticeRecord
    html: str


@dataclass
class FailedPage:
    """属于目标项目但抓取/解析失败的详情页。"""

    url: str
    link_text: str
    reason: str  # 抓取失败 / 解析失败


@dataclass
class ProjectScan:
    """``scan_project`` 的输出：本轮采集命中的原公告及更正（含取消）。"""

    pages: list[ScannedPage] = field(default_factory=list)
    failures: list[FailedPage] = field(default_factory=list)
    candidates: int = 0  # 列表页发现的同站候选详情链接数


async def scan_project(
    client: httpx.AsyncClient, list_url: str, project_code: str
) -> ProjectScan:
    """发现并发抓取列表页中属于 ``project_code`` 的同站公告详情页。

    列表页抓取失败直接抛 502；详情页失败不抛出，而是沉淀到
    ``ProjectScan.failures`` 中，由调用方按业务规则处理（待核实）。
    """
    try:
        list_html = await _fetch(client, list_url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"列表页抓取失败：{list_url}（{exc.__class__.__name__}）",
        ) from exc

    # 发现同站点详情链接（URL, 链接文本, 行上下文）
    candidates = extract_detail_links(list_html, list_url)
    if not candidates:
        return ProjectScan()

    target_code = _normalize_code(project_code)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def fetch_one(url: str, link_text: str, context: str) -> tuple[str, str, str, str]:
        async with semaphore:
            try:
                html = await _fetch(client, url)
                return url, html, link_text, context
            except httpx.HTTPError:
                return url, "", link_text, context  # 空 html 表示失败

    results = await asyncio.gather(*(fetch_one(u, t, c) for u, t, c in candidates))

    scan = ProjectScan(candidates=len(candidates))
    for url, html, link_text, context in results:
        # 列表行上下文包含目标项目编号 → 该链接属于本项目（即使详情页抓不回来）
        row_mentions_project = target_code in _normalize_code(context)

        if not html:
            if row_mentions_project:
                scan.failures.append(FailedPage(url=url, link_text=link_text, reason="详情页抓取失败"))
            continue
        try:
            data = parse_detail(html, url)
        except ParseError:
            if row_mentions_project:
                scan.failures.append(FailedPage(url=url, link_text=link_text, reason="详情页解析失败"))
            continue

        if not _code_matches(data.project_code, project_code, data.title, context):
            continue  # 同站点但属于其他项目，忽略

        kind = {
            "original": NoticeKind.ORIGINAL,
            "correction": NoticeKind.CORRECTION,
            "cancellation": NoticeKind.CANCELLATION,
        }[data.kind_hint]
        scan.pages.append(
            ScannedPage(
                record=NoticeRecord(
                    url=url,
                    kind=kind,
                    title=data.title,
                    published_at=data.published_at,
                    deadline=data.deadline,
                    cancelled=data.cancelled,
                    cancellation_reason=data.cancellation_reason,
                ),
                html=html,
            )
        )
    return scan


def build_client(timeout: float, follow_redirects: bool = True) -> httpx.AsyncClient:
    """构造采集/检查共用的 HTTP 客户端。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=follow_redirects,
    )


async def collect(req: FetchRequest) -> FetchResponse:
    list_url = str(req.list_url)
    async with build_client(req.timeout) as client:
        scan = await scan_project(client, list_url, req.project_code)

    if scan.candidates == 0:
        return _empty_response(req, warnings=["列表页未发现任何同站点详情链接"])

    notices = [page.record for page in scan.pages]
    failures = sorted({f.url for f in scan.failures})

    # 业务合并
    merge_result = merge_notices(MergeInput(notices=notices, failures=failures))

    # 组装来源链接
    sources: list[SourceLink] = [
        SourceLink(role="list", url=list_url, title="采购公告列表页")
    ]
    role_map = {
        NoticeKind.ORIGINAL: "original",
        NoticeKind.CORRECTION: "correction",
        NoticeKind.CANCELLATION: "cancellation",
    }
    for notice in merge_result.ordered_notices:
        sources.append(
            SourceLink(role=role_map[notice.kind], url=notice.url, title=notice.title)
        )
    for failed_url in failures:
        sources.append(SourceLink(role="failed", url=failed_url, title="抓取失败-待核实"))

    return FetchResponse(
        project_code=req.project_code,
        list_url=req.list_url,
        status=merge_result.status,
        can_bid=merge_result.can_bid,
        effective_deadline=merge_result.effective_deadline,
        cancelled=merge_result.cancelled,
        notices=merge_result.ordered_notices,
        sources=sources,
        warnings=merge_result.warnings,
        fetched_at=datetime.now(timezone.utc),
    )


def _empty_response(req: FetchRequest, warnings: list[str]) -> FetchResponse:
    return FetchResponse(
        project_code=req.project_code,
        list_url=req.list_url,
        status=ProjectStatus.NOT_FOUND,
        can_bid=False,
        effective_deadline=None,
        cancelled=False,
        notices=[],
        sources=[
            SourceLink(role="list", url=str(req.list_url), title="采购公告列表页")
        ],
        warnings=warnings,
        fetched_at=datetime.now(timezone.utc),
    )
