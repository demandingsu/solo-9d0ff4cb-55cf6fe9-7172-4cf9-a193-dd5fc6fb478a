"""Pydantic 数据模型：请求 / 响应 / 内部记录。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class ProjectStatus(str, Enum):
    """投标状态。

    OPEN         可投标（存在有效截止时间，且未取消）
    CLOSED       不可投标（公告明确取消采购）
    PENDING      待核实（抓取/解析失败，或无法得到有效截止时间，不能把旧时间当最终期限）
    NOT_FOUND    列表页未发现与该项目编号相关的同站点公告
    """

    OPEN = "open"
    CLOSED = "closed"
    PENDING = "pending"
    NOT_FOUND = "not_found"


class NoticeKind(str, Enum):
    ORIGINAL = "original"          # 原公告
    CORRECTION = "correction"      # 更正公告（含澄清、变更、延期）
    CANCELLATION = "cancellation"  # 取消公告（更正的一种，且明确取消采购）


class FetchRequest(BaseModel):
    """采集请求。"""

    list_url: HttpUrl = Field(..., description="采购公告列表页 URL")
    project_code: str = Field(..., min_length=1, description="项目编号，如 GSB-2026-001")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class NoticeRecord(BaseModel):
    """单条公告详情的解析结果（按发布时间排序）。"""

    url: HttpUrl
    kind: NoticeKind
    title: str
    published_at: datetime | None = None
    deadline: datetime | None = Field(
        None, description="该公告声明的投标截止时间；未提及则为 None，合并时沿用已生效时间"
    )
    cancelled: bool = False
    cancellation_reason: str | None = None


class SourceLink(BaseModel):
    """来源链接（列表页 + 参与合并的详情页）。"""

    role: str  # list / original / correction / cancellation
    url: HttpUrl
    title: str | None = None


class FetchResponse(BaseModel):
    """采集响应。"""

    project_code: str
    list_url: HttpUrl
    status: ProjectStatus
    can_bid: bool = Field(..., description="当前是否可投标")
    effective_deadline: datetime | None = Field(
        None, description="有效投标截止时间；待核实/未找到时为 None，绝不返回旧时间冒充最终期限"
    )
    cancelled: bool = False
    notices: list[NoticeRecord] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fetched_at: datetime


# --------------------------------------------------------------------------- #
# 附件可用性检查
# --------------------------------------------------------------------------- #
class AttachmentStatus(str, Enum):
    """单个附件的检查结论。

    AVAILABLE    可用（文件头校验通过，且未超过读取上限）
    UNAVAILABLE  不可用（404/服务器拒绝、返回登录 HTML 伪装页、文件格式不符）
    UNKNOWN      待核实（超时、超过读取上限、跳转站外、关联详情页读取失败等，
                 无法下结论；存在待核实项时不得报告整项目附件均可用）
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class AttachmentCheckRequest(BaseModel):
    """附件可用性检查请求。"""

    list_url: HttpUrl = Field(..., description="采购公告列表页 URL")
    project_code: str = Field(..., min_length=1, description="项目编号，如 GSB-2026-001")
    timeout: float = Field(10.0, ge=1.0, le=60.0, description="单次请求超时时间（秒）")


class AttachmentSource(BaseModel):
    """附件在公告中的引用位置（同一文件可被多条公告引用）。"""

    notice_url: HttpUrl
    notice_title: str | None = None
    notice_kind: NoticeKind | None = None
    link_text: str | None = None


class AttachmentFinding(BaseModel):
    """单个附件文件（按 URL 去重，只下载一次）的检查结论。"""

    url: HttpUrl
    status: AttachmentStatus
    reason: str = Field(..., description="人类可读的结论原因")
    http_status: int | None = Field(None, description="附件请求最终 HTTP 状态码")
    content_type: str | None = None
    bytes_read: int = Field(0, description="实际读取的字节数（流式，最多 max_bytes）")
    sources: list[AttachmentSource] = Field(
        default_factory=list, description="引用该附件的来源公告（逐公告保留引用）"
    )


class AttachmentRef(BaseModel):
    """公告维度下的附件引用及其结论（结论与 finding 共享）。"""

    url: HttpUrl
    link_text: str | None = None
    status: AttachmentStatus
    reason: str


class NoticeAttachmentReport(BaseModel):
    """单条公告的附件检查概况。"""

    url: HttpUrl
    title: str | None = None
    kind: NoticeKind | None = None
    fetched: bool = Field(..., description="详情页是否读取成功；失败时其附件情况列为待核实")
    reason: str | None = Field(None, description="详情页读取失败的原因")
    attachments: list[AttachmentRef] = Field(default_factory=list)


class AttachmentCheckResponse(BaseModel):
    """附件可用性检查响应。"""

    project_code: str
    list_url: HttpUrl
    all_available: bool = Field(
        ...,
        description="整项目附件是否均可用；只要存在不可用/待核实附件或关联详情读取失败即为 False",
    )
    total_attachments: int = Field(..., description="去重后的附件文件数")
    available: int
    unavailable: int
    unknown: int = Field(..., description="待核实附件数")
    failed_notices: int = Field(..., description="读取失败的关联详情页数（计入待核实）")
    max_bytes: int = Field(..., description="单个附件的流式读取上限（字节）")
    notices: list[NoticeAttachmentReport] = Field(default_factory=list)
    findings: list[AttachmentFinding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    checked_at: datetime
