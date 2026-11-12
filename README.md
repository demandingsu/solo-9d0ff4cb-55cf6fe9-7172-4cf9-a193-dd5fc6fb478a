# 采购公告采集 API

传入**采购公告列表页 URL** 与**项目编号**，自动抓取该页指向的同站点原公告与更正公告，
从详情页提取发布时间、投标截止时间与取消采购信息，按发布时间合并后返回：
当前是否可投标、有效截止时间、项目状态以及全部来源链接。

另提供**附件可用性检查**：针对供应商反馈的“附件链接能打开，却下载不到文件”，
沿用同一轮采集发现的原公告及更正，从详情页提取指向本站的 PDF/ZIP 附件链接，
流式下载并核对文件头，逐附件给出可用 / 不可用 / 待核实结论及来源公告。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 只采集列表页指向的**同站点**详情链接，外站链接、登录/导航链接自动忽略 | `app/parser.py::extract_detail_links` |
| 按**发布时间**从早到晚合并原公告与更正公告 | `app/merger.py::merge_notices` |
| 更正公告**未提及截止时间**时，沿用此前已生效的截止时间 | `app/merger.py` |
| 更正公告给出新截止时间（如延期）时，以新时间覆盖 | `app/merger.py` |
| 公告明确**取消采购** → 状态 `closed`、不可投标，不再返回有效截止时间（旧时间仅在公告明细中保留） | `app/merger.py` |
| **任一关联详情页**抓取/解析失败（404、超时、结构异常等）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，**绝不把旧时间当作最终期限** | `app/merger.py` + `app/collector.py` |
| 发布时间缺失、缺少原公告、全部公告均无截止时间等信息链不完整情况，同样标记 `pending` | `app/merger.py` |

### 附件可用性检查规则

| 规则 | 实现位置 |
| --- | --- |
| 只检查详情页中指向**本站**的 `.pdf` / `.zip` 附件链接 | `app/parser.py::extract_attachment_links` |
| **同一文件链接只下载一次**，结论共享；引用关系逐公告保留在 `notices[].attachments` 与 `findings[].sources` | `app/attachments.py::check_attachments` |
| **流式读取最多 10 MiB**（`max_bytes`），超限即停并列为**待核实** | `app/attachments.py::inspect_attachment` |
| 核对**文件头**（PDF：`%PDF-`；ZIP：`PK` 签名），格式不符列为**不可用** | `app/attachments.py::_sniff_head` |
| 200 响应但内容为 **HTML（登录页/错误页伪装）** → 不可用 | `app/attachments.py::_looks_like_html` |
| 附件链接**跳转站外** → 停止读取，列为待核实；同站重定向限 5 次 | `app/attachments.py::inspect_attachment` |
| **404 / 其他 4xx/5xx** → 不可用；**超时、网络错误** → 待核实 | `app/attachments.py::inspect_attachment` |
| **关联详情页读取失败** → 该公告附件情况列为待核实；只要存在待核实或不可用项，`all_available` 即为 `false`，**绝不报告整项目附件均可用** | `app/attachments.py::check_attachments` |

### 项目状态（`status`）

- `open`：可投标，`effective_deadline` 为合并后的有效截止时间；
- `closed`：采购已取消，`can_bid=false`，无有效截止时间；
- `pending`：待核实，存在关联页面失败或信息不完整，`effective_deadline=null`；
- `not_found`：列表页未发现与该项目编号相关的公告。

### 附件结论（`findings[].status`）

- `available`：可用，文件头校验通过且未超读取上限；
- `unavailable`：不可用，404/服务器拒绝、返回登录 HTML 伪装页、文件格式不符；
- `unknown`：待核实，超时、超过 10 MiB 上限、跳转站外等无法下结论的情况。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 入口、路由、浏览器演示页
│   ├── collector.py   # 采集编排：列表发现 → 并发抓取 → 归属判定（scan_project 共用）
│   ├── attachments.py # 附件检查：流式下载、文件头核验、状态分类、逐公告汇总
│   ├── parser.py      # HTML 解析：同站链接发现、附件链接提取、三层字段提取
│   ├── merger.py      # 业务合并规则（时间继承、延期覆盖、取消、待核实）
│   └── models.py      # 请求/响应 Pydantic 模型
├── sample-site/       # 本地示例公告站点（nginx 静态托管）
│   ├── list.html      # 公告列表页（含外站/登录干扰链接）
│   ├── detail/        # 3 个项目的原公告/更正/取消详情页（含附件链接）
│   └── files/         # 附件样例：正常 PDF/ZIP、登录页伪装、内容损坏（404 为缺失文件）
├── nginx/default.conf # 示例站点 nginx 配置（缺失页面返回真实 404）
├── tests/             # 解析/合并/附件单测 + 本地静态服务器端到端测试（46 个用例）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 公告采集 API | http://localhost:8000 | Swagger 文档：http://localhost:8000/docs |
| 浏览器演示页 | http://localhost:8000/ | 填表即可发起采集 / 附件检查 |
| 示例公告站点 | http://localhost:8080/list.html | nginx 托管的静态采购公告 |

> 演示页默认填 `http://localhost:8080/list.html`（宿主机视角）；
> 若从 compose 网络内部调用，列表页地址请用 `http://sample-site/list.html`。

停止：`docker compose down`。

## API 用法

### 采集合并：POST（推荐）

```bash
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'
```

### 采集合并：GET（便于浏览器快速验证）

```bash
curl "http://localhost:8000/api/v1/collect?list_url=http://localhost:8080/list.html&project_code=GSB-2026-001"
```

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "status": "open",
  "can_bid": true,
  "effective_deadline": "2026-11-15T14:00:00",
  "cancelled": false,
  "notices": [ { "kind": "original", "published_at": "...", "deadline": "..." } ],
  "sources": [ { "role": "list", "url": "..." }, { "role": "correction", "url": "..." } ],
  "warnings": []
}
```

### 附件可用性检查：POST / GET

```bash
curl -X POST http://localhost:8000/api/v1/attachments/check \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'

curl "http://localhost:8000/api/v1/attachments/check?list_url=http://localhost:8080/list.html&project_code=GSB-2026-001"
```

响应关键字段（`findings` 为去重后的逐附件结论，`notices` 为逐公告引用视图）：

```json
{
  "project_code": "GSB-2026-001",
  "all_available": false,
  "total_attachments": 5,
  "available": 2,
  "unavailable": 3,
  "unknown": 0,
  "failed_notices": 0,
  "max_bytes": 10485760,
  "findings": [
    {
      "url": "http://localhost:8080/files/GSB-2026-001-tender.pdf",
      "status": "available",
      "reason": "文件头校验通过（PDF），附件可下载",
      "http_status": 200,
      "bytes_read": 433,
      "sources": [
        { "notice_url": ".../detail/g001-original.html", "notice_kind": "original" },
        { "notice_url": ".../detail/g001-corr1.html", "notice_kind": "correction" }
      ]
    },
    {
      "url": "http://localhost:8080/files/needs-login.pdf",
      "status": "unavailable",
      "reason": "服务器返回 HTML 页面（疑似登录页或错误页），附件实际不可下载"
    }
  ],
  "notices": [
    {
      "url": ".../detail/g001-original.html",
      "fetched": true,
      "attachments": [ { "url": ".../files/GSB-2026-001-tender.pdf", "status": "available" } ]
    }
  ],
  "warnings": []
}
```

## 示例站点内置的三个演示项目

| 项目编号 | 场景 | 采集预期结果 | 附件检查预期结果 |
| --- | --- | --- | --- |
| `GSB-2026-001` | 原公告 → 澄清更正（**未提截止时间，沿用原时间**）→ 延期更正（**新时间覆盖**） | `open`，有效截止时间 `2026-11-15 14:00` | 5 个去重附件：2 正常、登录页伪装、内容损坏、404；`all_available=false`；招标文件被两条公告引用但只下载一次 |
| `GSB-2026-002` | 原公告 → 取消公告（预算收回） | `closed`，不可投标，无有效截止时间 | 1 个正常附件，`all_available=true` |
| `GSB-2026-003` | 原公告 → 更正页**已下线（404）** | `pending` 待核实，`effective_deadline=null`，来源中列出失败链接 | 关联详情读取失败 → 待核实，`all_available=false` |

附件样例文件（`sample-site/files/`）：

| 文件 | 场景 |
| --- | --- |
| `GSB-2026-001-tender.pdf`、`GSB-2026-002-notice.pdf` | 正常 PDF（`%PDF-` 头） |
| `GSB-2026-001-docs.zip` | 正常 ZIP（`PK` 签名） |
| `needs-login.pdf` | **登录页伪装**：扩展名为 .pdf，内容实为登录 HTML（200 响应也判不可用） |
| `broken.pdf` | **故障附件**：内容为纯文本，文件头与 PDF 格式不符 |
| `missing.zip`（不存在） | **故障附件**：服务器返回 404 |

另外列表页还包含：外站链接、登录导航链接、其他项目公告（`GSB-2026-099`）、
无关项目的坏链接（`GSB-2025-888`），用于验证同站过滤、项目归属判定与失败隔离。

## 详情页解析策略（三层回退）

1. **结构化标记**：`data-field="published-at|deadline|cancelled|..."` 元素或
   `<meta property="article:published_time">`；
2. **标签匹配**：`发布时间 / 投标截止时间 / 响应文件递交截止时间 / 取消原因` 等
   中文标签，兼容 `th/td`、`dt/dd`、`li/p` 等布局，支持
   `2026-11-10 09:30`、`2026年11月15日 14时00分` 等常见写法；
3. **正文正则兜底**：在含“截止/递交/提交/受理”等时限语义的块级元素中识别日期，
   并排除“发布时间”句、资质业绩说明中的历史日期（如“近三年（2023年1月1日起）”）。

取消采购识别支持 `data-field="cancelled"` 标记、取消原因标签以及
“本项目取消采购 / 终止本次招标”等显式句式。

> 接入真实采购平台时，若页面结构不同，优先在详情页模板中补 `data-field` 标记，
> 或在 `app/parser.py` 顶部的标签/关键词元组中登记该平台的字段叫法。

## 本地开发与测试（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 A：托管示例公告页
cd sample-site && python -m http.server 8080
# 终端 B：启动 API
uvicorn app.main:app --reload --port 8000

# 测试（自带静态服务器，无需外部依赖）
pytest -q
```

## 设计说明

- 列表页抓取失败直接返回 `502`；详情页失败不影响整体响应，而是按业务规则沉淀为
  `pending` + 失败来源链接，便于投标专员人工核实。
- 关联归属判定结合详情页项目编号与列表行上下文（含编号列），
  即使详情页下线（只能看到列表行）也能正确判定失败是否与目标项目相关；
  无关项目的坏链接不会污染查询结果。
- 详情页并发抓取（上限 5），统一超时、自定义 UA、跟随重定向。
- 附件检查复用同一轮采集发现的公告集合（`scan_project`），不重复请求列表页与详情页；
  附件下载单独流式进行，同一 URL 只下载一次，手动处理重定向以便识别并阻断站外跳转。
- 附件结论宁缺毋滥：超时、超限、站外跳转、详情读取失败一律列为**待核实**，
  存在任何待核实/不可用项时 `all_available=false`，避免向发布员误报“全部可下载”。

