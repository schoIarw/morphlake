"""Server-rendered administration pages for tokens, transfers, and monitoring."""

# ruff: noqa: E501

from __future__ import annotations

import hmac
import html
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from morphlake.admin_store import AdminStore
from morphlake.auth import get_admin_store
from morphlake.config import Settings, get_settings
from morphlake.errors import MorphLakeError

router = APIRouter(prefix="/admin", tags=["administration"])
basic = HTTPBasic()


def require_admin(
    credentials: Annotated[HTTPBasicCredentials, Depends(basic)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    valid_user = secrets.compare_digest(credentials.username, settings.admin_username)
    valid_password = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=401,
            detail="Invalid administrator credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _csrf(settings: Settings) -> str:
    return hmac.digest(
        settings.token_pepper.encode("utf-8"), b"morphlake-admin-form", "sha256"
    ).hex()


def _verify_csrf(value: str, settings: Settings) -> None:
    if not secrets.compare_digest(value, _csrf(settings)):
        raise MorphLakeError("csrf_invalid", "Invalid administration form token", 403)


@router.get("", response_class=HTMLResponse)
def dashboard(
    _: Annotated[str, Depends(require_admin)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
) -> HTMLResponse:
    tokens = store.list_tokens()
    stats = store.transfer_stats("day")
    upload_count = sum(row["request_count"] for row in stats if row["operation"] == "upload")
    download_count = sum(row["request_count"] for row in stats if row["operation"] == "download")
    body = f"""
    <div class="cards">
      {_card("有效 Token", sum(row["status"] == "active" for row in tokens))}
      {_card("今日上传", upload_count)}
      {_card("今日下载", download_count)}
      {_card("待同步审计", store.unsynced_event_count())}
    </div>
    <p>管理 Token 生命周期、上传下载配额、传输统计和 Prometheus 监控。</p>
    """
    return HTMLResponse(_page("MorphLake 管理台", body))


@router.get("/tokens", response_class=HTMLResponse)
def tokens_page(
    _: Annotated[str, Depends(require_admin)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    rows = "".join(_token_row(row, _csrf(settings)) for row in store.list_tokens())
    body = f"""
    <section class="panel">
      <h2>分配 Token</h2>
      <form method="post" action="/admin/tokens" class="grid-form">
        <input type="hidden" name="csrf" value="{_csrf(settings)}">
        <label>业务域<input name="business_domain" required maxlength="128"></label>
        <label>部门<input name="department" required maxlength="128"></label>
        <label>使用人姓名<input name="assignee_name" required maxlength="128"></label>
        <label>手机号码<input name="phone" required maxlength="32"></label>
        <label>周期（秒）<input name="period_seconds" type="number" min="1"
          value="{settings.default_rate_period_seconds}" required></label>
        <label>周期上传次数<input name="upload_requests_limit" type="number" min="0"
          value="{settings.default_upload_requests}" required></label>
        <label>周期下载次数<input name="download_requests_limit" type="number" min="0"
          value="{settings.default_download_requests}" required></label>
        <label>周期上传字节<input name="upload_bytes_limit" type="number" min="0"
          value="{settings.default_upload_bytes}" required></label>
        <label>周期下载字节<input name="download_bytes_limit" type="number" min="0"
          value="{settings.default_download_bytes}" required></label>
        <label>过期时间（可选）<input name="expires_at" type="datetime-local"></label>
        <label class="wide">备注<textarea name="notes" maxlength="1000"></textarea></label>
        <button type="submit">生成 Token</button>
      </form>
      <p class="hint">0 表示不限制。Token 明文只在创建成功页面显示一次。</p>
    </section>
    <section class="panel"><h2>Token 列表</h2>
      <div class="table-wrap"><table><thead><tr>
        <th>前缀</th><th>业务域</th><th>部门</th><th>使用人</th><th>手机</th>
        <th>备注</th><th>状态</th><th>创建/过期</th><th>限流设置</th><th>操作</th>
      </tr></thead><tbody>{rows}</tbody></table></div>
    </section>
    """
    return HTMLResponse(_page("Token 管理", body))


@router.post("/tokens", response_class=HTMLResponse)
def create_token(
    admin: Annotated[str, Depends(require_admin)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    csrf: Annotated[str, Form()],
    business_domain: Annotated[str, Form()],
    department: Annotated[str, Form()],
    assignee_name: Annotated[str, Form()],
    phone: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
    expires_at: Annotated[str, Form()] = "",
    period_seconds: Annotated[int, Form(ge=1)] = 60,
    upload_requests_limit: Annotated[int, Form(ge=0)] = 60,
    download_requests_limit: Annotated[int, Form(ge=0)] = 120,
    upload_bytes_limit: Annotated[int, Form(ge=0)] = 1_073_741_824,
    download_bytes_limit: Annotated[int, Form(ge=0)] = 5_368_709_120,
) -> HTMLResponse:
    _verify_csrf(csrf, settings)
    normalized_expiry = None
    if expires_at:
        normalized_expiry = datetime.fromisoformat(expires_at).replace(tzinfo=UTC).isoformat()
    created = store.create_token(
        business_domain=business_domain,
        department=department,
        assignee_name=assignee_name,
        phone=phone,
        notes=notes,
        allocated_by=admin,
        expires_at=normalized_expiry,
        period_seconds=period_seconds,
        upload_requests_limit=upload_requests_limit,
        download_requests_limit=download_requests_limit,
        upload_bytes_limit=upload_bytes_limit,
        download_bytes_limit=download_bytes_limit,
    )
    body = f"""
    <section class="panel token-created"><h2>Token 创建成功</h2>
      <p>请立即复制并安全交付给使用人，系统不会再次显示完整 Token。</p>
      <pre>{html.escape(created.plaintext)}</pre>
      <p>范围：{html.escape(created.identity.business_domain)} /
      {html.escape(created.identity.department)}；前缀：{created.identity.token_prefix}</p>
      <a class="button" href="/admin/tokens">返回 Token 管理</a>
    </section>
    """
    return HTMLResponse(_page("Token 创建成功", body), status_code=201)


@router.post("/tokens/{token_id}/status/{action}")
def update_token(
    token_id: str,
    action: str,
    _: Annotated[str, Depends(require_admin)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    csrf: Annotated[str, Form()],
) -> RedirectResponse:
    _verify_csrf(csrf, settings)
    status = {"enable": "active", "disable": "disabled", "delete": "deleted"}.get(action)
    if status is None:
        raise MorphLakeError("invalid_token_action", "Unsupported token action", 400)
    store.set_token_status(token_id, status)
    return RedirectResponse("/admin/tokens", status_code=303)


@router.post("/tokens/{token_id}/limits")
def update_token_limits(
    token_id: str,
    _: Annotated[str, Depends(require_admin)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    csrf: Annotated[str, Form()],
    period_seconds: Annotated[int, Form(ge=1)],
    upload_requests_limit: Annotated[int, Form(ge=0)],
    download_requests_limit: Annotated[int, Form(ge=0)],
    upload_bytes_limit: Annotated[int, Form(ge=0)],
    download_bytes_limit: Annotated[int, Form(ge=0)],
) -> RedirectResponse:
    _verify_csrf(csrf, settings)
    store.update_token_limits(
        token_id,
        period_seconds=period_seconds,
        upload_requests_limit=upload_requests_limit,
        download_requests_limit=download_requests_limit,
        upload_bytes_limit=upload_bytes_limit,
        download_bytes_limit=download_bytes_limit,
    )
    return RedirectResponse("/admin/tokens", status_code=303)


@router.get("/transfers", response_class=HTMLResponse)
def transfers_page(
    _: Annotated[str, Depends(require_admin)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    period: Annotated[str, Query(pattern="^(day|week|month)$")] = "day",
) -> HTMLResponse:
    stat_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(row[key]))}</td>"
            for key in (
                "token_prefix",
                "business_domain",
                "department",
                "operation",
                "status",
                "request_count",
                "byte_count",
            )
        )
        + "</tr>"
        for row in store.transfer_stats(period)
    )
    detail_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(row.get(key) or ''))}</td>"
            for key in (
                "occurred_at",
                "token_prefix",
                "operation",
                "business_domain",
                "department",
                "filename",
                "byte_count",
                "status",
                "error_code",
            )
        )
        + "</tr>"
        for row in store.recent_transfers()
    )
    body = f"""
    <section class="panel"><h2>周期统计</h2>
      <p><a href="?period=day">天</a> · <a href="?period=week">周</a> ·
      <a href="?period=month">月</a></p>
      <div class="table-wrap"><table><thead><tr><th>Token</th><th>业务域</th><th>部门</th>
      <th>操作</th><th>状态</th><th>条数</th><th>字节数</th></tr></thead>
      <tbody>{stat_rows}</tbody></table></div>
    </section>
    <section class="panel"><h2>最近传输明细（SQLite缓存，长期明细进入Paimon）</h2>
      <div class="table-wrap"><table><thead><tr><th>时间</th><th>Token</th><th>操作</th>
      <th>业务域</th><th>部门</th><th>文件</th><th>字节</th><th>状态</th><th>错误</th>
      </tr></thead><tbody>{detail_rows}</tbody></table></div>
    </section>
    """
    return HTMLResponse(_page("传输统计", body))


@router.get("/monitoring", response_class=HTMLResponse)
def monitoring_page(
    _: Annotated[str, Depends(require_admin)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    queries = {
        "服务实例": 'up{job=~"morphlake.*"}',
        "请求速率": "sum(rate(morphlake_http_requests_total[5m]))",
        "上传速率": 'sum(rate(morphlake_transfer_bytes_total{operation="upload",status="success"}[5m]))',
        "下载速率": 'sum(rate(morphlake_transfer_bytes_total{operation="download",status="success"}[5m]))',
        "审计积压": "morphlake_transfer_audit_backlog",
        "索引成功时间": "morphlake_index_last_success_timestamp_seconds",
    }
    results: dict[str, Any] = {}
    error = None
    if settings.prometheus_url:
        try:
            for name, query in queries.items():
                response = httpx.get(
                    f"{settings.prometheus_url.rstrip('/')}/api/v1/query",
                    params={"query": query},
                    timeout=5,
                )
                response.raise_for_status()
                results[name] = response.json().get("data", {}).get("result", [])
        except (httpx.HTTPError, ValueError) as exc:
            error = str(exc)
    else:
        error = "PROMETHEUS_URL 未配置"
    cards = "".join(_card(name, _prometheus_value(results.get(name, []))) for name in queries)
    detail = html.escape(json.dumps(results, ensure_ascii=False, indent=2))
    body = f"""
    {f'<div class="alert">{html.escape(error)}</div>' if error else ""}
    <div class="cards">{cards}</div>
    <section class="panel"><h2>Prometheus 查询结果</h2><pre>{detail}</pre></section>
    """
    return HTMLResponse(_page("系统监控", body))


def _token_row(row: dict[str, Any], csrf: str) -> str:
    token_id = html.escape(row["token_id"])
    action = "enable" if row["status"] == "disabled" else "disable"
    action_text = "启用" if action == "enable" else "停用"
    return f"""
    <tr><td>{html.escape(row["token_prefix"])}</td>
    <td>{html.escape(row["business_domain"])}</td><td>{html.escape(row["department"])}</td>
    <td>{html.escape(row["assignee_name"])}</td><td>{html.escape(row["phone"])}</td>
    <td>{html.escape(row["notes"])}</td>
    <td><span class="status {row["status"]}">{row["status"]}</span></td>
    <td>{html.escape(row["created_at"])}<br>{html.escape(row["expires_at"] or "永不过期")}</td>
    <td><details><summary>{row["period_seconds"]}秒；上传 {row["upload_requests_limit"]}次 /
      {row["upload_bytes_limit"]}B；下载 {row["download_requests_limit"]}次 /
      {row["download_bytes_limit"]}B</summary>
      <form method="post" action="/admin/tokens/{token_id}/limits" class="limit-form">
      <input type="hidden" name="csrf" value="{csrf}">
      <label>周期秒<input type="number" name="period_seconds" min="1"
        value="{row["period_seconds"]}" required></label>
      <label>上传次数<input type="number" name="upload_requests_limit" min="0"
        value="{row["upload_requests_limit"]}" required></label>
      <label>上传字节<input type="number" name="upload_bytes_limit" min="0"
        value="{row["upload_bytes_limit"]}" required></label>
      <label>下载次数<input type="number" name="download_requests_limit" min="0"
        value="{row["download_requests_limit"]}" required></label>
      <label>下载字节<input type="number" name="download_bytes_limit" min="0"
        value="{row["download_bytes_limit"]}" required></label><button>保存</button></form>
      </details></td>
    <td><form class="inline" method="post" action="/admin/tokens/{token_id}/status/{action}">
      <input type="hidden" name="csrf" value="{csrf}"><button>{action_text}</button></form>
      <form class="inline" method="post" action="/admin/tokens/{token_id}/status/delete">
      <input type="hidden" name="csrf" value="{csrf}"><button class="danger">删除</button></form></td></tr>
    """


def _prometheus_value(result: list[dict[str, Any]]) -> str:
    if not result:
        return "—"
    if len(result) == 1:
        value = result[0].get("value", [None, "—"])[1]
        return str(value)
    return f"{len(result)} series"


def _card(title: str, value: Any) -> str:
    return f'<div class="card"><span>{html.escape(str(title))}</span><strong>{html.escape(str(value))}</strong></div>'


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{html.escape(title)} · MorphLake</title><style>
    :root{{--red:#d62127;--ink:#222;--muted:#666;--line:#ead4d5;--soft:#fff7f7}}
    *{{box-sizing:border-box}}body{{margin:0;background:#f7f7f8;color:var(--ink);
    font:14px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
    header{{background:#111;color:white;padding:16px 28px;display:flex;align-items:center;gap:30px}}
    header b{{font-size:20px}}nav a{{color:#ddd;text-decoration:none;margin-right:20px}}nav a:hover{{color:white}}
    main{{max-width:1440px;margin:24px auto;padding:0 20px}}h1{{font-size:26px}}h2{{margin-top:0}}
    .panel{{background:white;border:1px solid #e5e5e5;border-radius:8px;padding:20px;margin:18px 0}}
    .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}}
    .card{{background:white;border-left:4px solid var(--red);padding:16px;border-radius:6px}}
    .card span{{display:block;color:var(--muted)}}.card strong{{display:block;font-size:25px;margin-top:6px}}
    .grid-form{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
    label{{color:var(--muted)}}input,textarea{{display:block;width:100%;padding:9px;margin-top:5px;border:1px solid #ccc;border-radius:4px}}
    textarea{{min-height:70px}}.wide{{grid-column:1/-1}}button,.button{{background:var(--red);color:white;border:0;border-radius:4px;padding:9px 14px;text-decoration:none;cursor:pointer}}
    .limit-form{{min-width:360px;display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px}}
    button.danger{{background:#555}}.inline{{display:inline-block;margin:2px}}.hint{{color:var(--muted)}}
    .table-wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #eee;padding:10px;text-align:left;white-space:nowrap}}
    th{{background:var(--soft)}}.status{{padding:3px 8px;border-radius:12px}}.active{{background:#e4f5e7}}.disabled{{background:#fff2cf}}
    .alert{{background:#fff1d6;border:1px solid #efc36d;padding:12px;border-radius:6px}}pre{{white-space:pre-wrap;word-break:break-all;background:#151515;color:#eee;padding:16px;border-radius:6px}}
    @media(max-width:800px){{.grid-form{{grid-template-columns:1fr}}}}
    </style></head><body><header><b>MorphLake</b><nav><a href="/admin">概览</a>
    <a href="/admin/tokens">Token</a><a href="/admin/transfers">传输统计</a>
    <a href="/admin/monitoring">监控</a></nav></header>
    <main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""
