"""Session-authenticated, server-rendered MorphLake administration console."""

# ruff: noqa: E501

from __future__ import annotations

import html
import json
import math
import re
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.background import BackgroundTask

from morphlake.admin_api_client import AdminApiClient, ApiResult, get_admin_api_client
from morphlake.admin_store import AdminSession, AdminStore
from morphlake.auth import get_admin_store
from morphlake.config import Settings, get_settings
from morphlake.errors import MorphLakeError

router = APIRouter(prefix="/admin", tags=["administration"])
basic = HTTPBasic()
SESSION_COOKIE = "morphlake_admin_session"


def require_admin(
    credentials: Annotated[HTTPBasicCredentials, Depends(basic)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """Keep HTTP Basic protection for API docs in the API container."""
    valid = secrets.compare_digest(credentials.username, settings.admin_username)
    valid &= secrets.compare_digest(credentials.password, settings.admin_password)
    if not valid:
        raise HTTPException(
            401,
            "Invalid administrator credentials",
            {"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def require_admin_session(
    request: Request,
    store: Annotated[AdminStore, Depends(get_admin_store)],
) -> AdminSession:
    session = store.authenticate_admin_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(
            303,
            "Administrator login required",
            {"Location": f"/admin/login?next={request.url.path}"},
        )
    return session


def _verify_csrf(value: str, session: AdminSession) -> None:
    if not secrets.compare_digest(value, session.csrf_token):
        raise MorphLakeError("csrf_invalid", "Invalid administration form token", 403)


@router.get("/login", response_class=HTMLResponse)
def login_page(next: str = "/admin") -> HTMLResponse:
    return HTMLResponse(_login_page(next))


@router.post("/login", response_model=None)
def login(
    store: Annotated[AdminStore, Depends(get_admin_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/admin",
) -> RedirectResponse | HTMLResponse:
    valid = secrets.compare_digest(username, settings.admin_username)
    valid &= secrets.compare_digest(password, settings.admin_password)
    if not valid:
        return HTMLResponse(_login_page(next, "用户名或密码错误"), status_code=401)
    plaintext, _ = store.create_admin_session(username)
    destination = next if next.startswith("/admin") and not next.startswith("//") else "/admin"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        plaintext,
        max_age=settings.admin_session_ttl_seconds,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout")
def logout(
    request: Request,
    session: Annotated[AdminSession, Depends(require_admin_session)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    csrf: Annotated[str, Form()],
) -> RedirectResponse:
    _verify_csrf(csrf, session)
    store.delete_admin_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("", response_class=HTMLResponse)
def dashboard(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    tokens = store.list_tokens()
    stats = store.transfer_stats("day")
    uploads = sum(row["request_count"] for row in stats if row["operation"] == "upload")
    downloads = sum(row["request_count"] for row in stats if row["operation"] == "download")
    body = f"""
    <div class="cards">
      {_card("有效 Key", sum(row["status"] == "active" for row in tokens), "身份授权")}
      {_card("今日上传", uploads, "请求")}
      {_card("今日下载", downloads, "请求")}
      {_card("待同步审计", store.unsynced_event_count(), "Paimon outbox")}
    </div>
    <section class="panel welcome"><div><span class="eyebrow">MULTIMODAL DATA FOUNDATION</span>
      <h2>欢迎使用 MorphLake</h2><p>管理 Key、查看传输与监控，或通过左侧能力菜单直接调用 API 服务。</p></div>
      <a class="button" href="/admin/api/upload">上传多模态文件</a></section>
    <div class="quick-grid">
      {_quick("文件清单", "Key 自动限定权限范围，按类型、日期和文件名查询", "/admin/api/files")}
      {_quick("全文检索", "按关键字检索文档切片", "/admin/api/full-text")}
      {_quick("向量检索", "输入向量或上传文件查询 Top 10", "/admin/api/vector")}
    </div>"""
    return HTMLResponse(_page("工作台", body, session, settings, "dashboard"))


@router.get("/tokens", response_class=HTMLResponse)
def tokens_page(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    scopes = store.list_scope_options()
    rows = "".join(_token_row(row, session.csrf_token) for row in store.list_tokens(reveal=True))
    body = f"""
    <section class="panel"><div class="section-head"><div><h2>分配业务域 Key</h2>
      <p>Key 固定绑定业务域和部门；上传自动继承归属，查询自动限制到该业务域。</p></div></div>
      <form method="post" action="/admin/tokens" class="grid-form">
        {_csrf_input(session)}
        {_scope_assignment_inputs(scopes)}
        {_input("使用人姓名", "assignee_name", required=True, maxlength=128)}
        {_input("手机号码", "phone", required=True, maxlength=32)}
        {_input("周期（秒）", "period_seconds", "number", settings.default_rate_period_seconds, min=1, required=True)}
        {_input("周期上传次数", "upload_requests_limit", "number", settings.default_upload_requests, min=0, required=True)}
        {_input("周期下载次数", "download_requests_limit", "number", settings.default_download_requests, min=0, required=True)}
        {_input("周期上传字节", "upload_bytes_limit", "number", settings.default_upload_bytes, min=0, required=True)}
        {_input("周期下载字节", "download_bytes_limit", "number", settings.default_download_bytes, min=0, required=True)}
        {_input("过期时间（可选）", "expires_at", "datetime-local")}
        <label class="wide">备注<textarea name="notes" maxlength="1000" placeholder="用途、应用名称或交付说明"></textarea></label>
        <div class="wide form-actions"><button type="submit">生成 Key</button><span class="hint">配额填 0 表示不限制</span></div>
      </form>
    </section>
    <section class="panel"><h2>已分配 Key</h2><p class="hint">管理 Key 可查询全部数据；业务域 Key 仅能查询所属业务域。</p>
      <div class="table-wrap"><table><thead><tr>
      <th>Key</th><th>权限</th><th>业务范围</th><th>使用人</th><th>手机</th><th>备注</th><th>状态</th>
      <th>创建/过期</th><th>限流</th><th>操作</th><th></th>
      </tr></thead><tbody>{rows or _empty_row(11)}</tbody></table></div></section>"""
    return HTMLResponse(_page("Key 管理", body, session, settings, "tokens"))


@router.post("/tokens", response_class=HTMLResponse)
def create_token(
    session: Annotated[AdminSession, Depends(require_admin_session)],
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
    _verify_csrf(csrf, session)
    normalized = (
        datetime.fromisoformat(expires_at).replace(tzinfo=UTC).isoformat() if expires_at else None
    )
    created = store.create_token(
        business_domain=business_domain,
        department=department,
        assignee_name=assignee_name,
        phone=phone,
        notes=notes,
        allocated_by=session.username,
        expires_at=normalized,
        period_seconds=period_seconds,
        upload_requests_limit=upload_requests_limit,
        download_requests_limit=download_requests_limit,
        upload_bytes_limit=upload_bytes_limit,
        download_bytes_limit=download_bytes_limit,
    )
    body = f"""<section class="panel token-created"><span class="success-mark">✓</span>
      <h2>Key 创建成功</h2><p>可立即复制；后续也可在 Key 管理页安全查看。</p>
      <pre class="secret">{html.escape(created.plaintext)}</pre>
      <p class="hint">范围：{html.escape(created.identity.business_domain)} / {html.escape(created.identity.department)}</p>
      <a class="button" href="/admin/tokens">返回 Key 管理</a></section>"""
    return HTMLResponse(_page("Key 创建成功", body, session, settings, "tokens"), status_code=201)


@router.post("/tokens/{token_id}/status/{action}")
def update_token(
    token_id: str,
    action: str,
    session: Annotated[AdminSession, Depends(require_admin_session)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    csrf: Annotated[str, Form()],
) -> RedirectResponse:
    _verify_csrf(csrf, session)
    status = {"enable": "active", "disable": "disabled", "delete": "deleted"}.get(action)
    if status is None:
        raise MorphLakeError("invalid_token_action", "Unsupported token action", 400)
    store.set_token_status(token_id, status)
    return RedirectResponse("/admin/tokens", status_code=303)


@router.post("/tokens/{token_id}/limits")
def update_token_limits(
    token_id: str,
    session: Annotated[AdminSession, Depends(require_admin_session)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    csrf: Annotated[str, Form()],
    period_seconds: Annotated[int, Form(ge=1)],
    upload_requests_limit: Annotated[int, Form(ge=0)],
    download_requests_limit: Annotated[int, Form(ge=0)],
    upload_bytes_limit: Annotated[int, Form(ge=0)],
    download_bytes_limit: Annotated[int, Form(ge=0)],
) -> RedirectResponse:
    _verify_csrf(csrf, session)
    store.update_token_limits(
        token_id,
        period_seconds=period_seconds,
        upload_requests_limit=upload_requests_limit,
        download_requests_limit=download_requests_limit,
        upload_bytes_limit=upload_bytes_limit,
        download_bytes_limit=download_bytes_limit,
    )
    return RedirectResponse("/admin/tokens", status_code=303)


@router.post("/tokens/{token_id}/rotate")
def rotate_token(
    token_id: str,
    session: Annotated[AdminSession, Depends(require_admin_session)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    csrf: Annotated[str, Form()],
) -> RedirectResponse:
    _verify_csrf(csrf, session)
    store.rotate_token(token_id)
    return RedirectResponse("/admin/tokens", status_code=303)


@router.get("/api/upload", response_class=HTMLResponse)
def upload_page(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    body = (
        _api_intro(
            "上传与向量化",
            "业务域和部门自动取自 Key。文件由现有 API 写入 MinIO，并完成提取、切片和向量化。",
        )
        + f"""
    <section class="panel"><form method="post" action="/admin/api/upload"
      enctype="multipart/form-data" class="grid-form">
      {_csrf_input(session)}{_api_token_input()}
      <label>上传接口<select name="mode"><option value="auto">自动识别</option>
      <option value="document">文档</option><option value="image">图片</option>
      <option value="audio">音频</option></select></label>
      <label class="wide">选择文件<input type="file" name="file" required></label>
      <div class="wide form-actions"><button>上传并向量化</button></div>
    </form></section>"""
    )
    return HTMLResponse(_page("文件上传", body, session, settings, "upload"))


@router.post("/api/upload", response_class=HTMLResponse)
def upload_action(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    csrf: Annotated[str, Form()],
    api_token: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> HTMLResponse:
    _verify_csrf(csrf, session)
    paths = {
        "auto": "/api/v1/files",
        "document": "/api/v1/files/documents",
        "image": "/api/v1/files/images",
        "audio": "/api/v1/files/audio",
    }
    if mode not in paths:
        raise MorphLakeError("invalid_upload_mode", "Unsupported upload mode", 400)
    result = client.request(
        "POST",
        paths[mode],
        api_token,
        file=(
            file.filename or "upload",
            file.file,
            file.content_type or "application/octet-stream",
        ),
    )
    body = _result_panel(result, "上传完成")
    body += '<a class="button secondary" href="/admin/api/upload">继续上传</a>'
    return HTMLResponse(
        _page("上传结果", body, session, settings, "upload"),
        status_code=result.status_code,
    )


@router.get("/api/files", response_class=HTMLResponse)
def files_page(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    business_domain: Annotated[str, Query(max_length=255)] = "",
    department: Annotated[str, Query(max_length=255)] = "",
    media_type: Annotated[str, Query(pattern="^(|document|image|audio)$")] = "",
    filename: Annotated[str, Query(max_length=255)] = "",
    description: Annotated[str, Query(max_length=1000)] = "",
    start_date: Annotated[str, Query(pattern=r"^$|^\d{4}-\d{2}-\d{2}$")] = "",
    end_date: Annotated[str, Query(pattern=r"^$|^\d{4}-\d{2}-\d{2}$")] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    deleted: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    filters = {
        "business_domain": business_domain,
        "department": department,
        "media_type": media_type,
        "filename": filename,
        "description": description,
        "start_date": start_date,
        "end_date": end_date,
    }
    params = _compact({**filters, "limit": page_size, "offset": (page - 1) * page_size})
    try:
        result = client.request(
            "GET",
            "/api/v1/admin/files",
            store.default_admin_token(),
            params=params,
        )
    except MorphLakeError as exc:
        result = ApiResult(exc.status_code, {"error": {"code": exc.code, "message": exc.message}})

    payload = result.payload if isinstance(result.payload, dict) else {}
    items = payload.get("items", []) if 200 <= result.status_code < 300 else []
    total = int(payload.get("total") or 0)
    total_pages = max(1, math.ceil(total / page_size))
    media_options = "".join(
        f'<option value="{value}" {"selected" if media_type == value else ""}>{label}</option>'
        for value, label in (
            ("", "全部"),
            ("document", "文档"),
            ("image", "图片"),
            ("audio", "音频"),
        )
    )
    scope_filters = _scope_filter_selects(
        scopes=store.list_scope_options(include_admin=True),
        selected_domain=business_domain,
        selected_department=department,
    )
    body = (
        _api_intro(
            "文件清单",
            "管理员全域清单；默认显示最近文件，可按业务域、部门、文件名或文本概要筛选。",
        )
        + f"""
    <section class="panel"><form method="get" action="/admin/api/files" class="grid-form">
      {scope_filters}
      <label>文件类型<select name="media_type">{media_options}</select></label>
      {_input("文件名模糊匹配", "filename", value=filename)}
      {_input("描述/概要模糊匹配", "description", value=description)}
      {_input("开始日期", "start_date", "date", start_date)}
      {_input("结束日期", "end_date", "date", end_date)}
      <input type="hidden" name="page" value="1">
      <div class="wide form-actions"><button>筛选文件</button>
      <a class="button secondary" href="/admin/api/files">清除筛选</a></div>
    </form></section>"""
    )
    if deleted:
        body += f'<div class="notice-success">已删除 {deleted} 个文件。</div>'
    if 200 <= result.status_code < 300:
        return_to = _files_url({**filters, "page": page, "page_size": page_size})
        body += f"""<section class="panel"><div class="section-head"><h2>最近文件</h2>
          <div class="asset-toolbar"><span class="hint">共 {total} 条 · 第 {page} / {total_pages} 页</span>
          <form id="batch-delete-form" method="post" action="/admin/api/files/delete">
          {_csrf_input(session)}<input type="hidden" name="return_to" value="{html.escape(return_to)}">
          <button class="danger" disabled>删除所选 <span id="selected-count"></span></button></form></div></div>
          {_object_table(items if isinstance(items, list) else [], selectable=True)}
          {_pagination(filters, page, page_size, total_pages)}</section>"""
    else:
        body += _result_panel(result, "文件清单查询失败")
    return HTMLResponse(
        _page("文件清单", body, session, settings, "files"),
        status_code=result.status_code if result.status_code >= 500 else 200,
    )


@router.post("/api/files/delete", response_model=None)
def delete_files_action(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    csrf: Annotated[str, Form()],
    file_ids: Annotated[list[str], Form()],
    return_to: Annotated[str, Form()] = "/admin/api/files",
) -> RedirectResponse | HTMLResponse:
    _verify_csrf(csrf, session)
    result = client.request(
        "POST",
        "/api/v1/files/batch-delete",
        store.default_admin_token(),
        json_body={"file_ids": file_ids},
    )
    if 200 <= result.status_code < 300:
        destination = _safe_files_return(return_to)
        separator = "&" if "?" in destination else "?"
        return RedirectResponse(f"{destination}{separator}deleted={len(file_ids)}", status_code=303)
    return HTMLResponse(
        _page("文件删除失败", _result_panel(result, "文件删除失败"), session, settings, "files"),
        status_code=result.status_code,
    )


@router.post("/api/files", response_class=HTMLResponse)
def files_action(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    csrf: Annotated[str, Form()],
    api_token: Annotated[str, Form()],
    business_domain: Annotated[str, Form()] = "",
    department: Annotated[str, Form()] = "",
    media_type: Annotated[str, Form()] = "",
    filename: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    start_date: Annotated[str, Form()] = "",
    end_date: Annotated[str, Form()] = "",
    limit: Annotated[int, Form(ge=1, le=200)] = 50,
    offset: Annotated[int, Form(ge=0)] = 0,
) -> HTMLResponse:
    _verify_csrf(csrf, session)
    params = _compact(
        {
            "business_domain": business_domain,
            "department": department,
            "media_type": media_type,
            "filename": filename,
            "description": description,
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
            "offset": offset,
        }
    )
    result = client.request("GET", "/api/v1/admin/files", api_token, params=params)
    return HTMLResponse(
        _page("文件查询结果", _result_panel(result, "文件清单"), session, settings, "files"),
        status_code=result.status_code,
    )


@router.get("/api/full-text", response_class=HTMLResponse)
def full_text_page(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    body = _api_intro("全文检索", "检索范围自动取自 Key，仅需填写关键字和必要的日期范围。")
    body += _search_form(
        session,
        "/admin/api/full-text",
        '<label class="wide">关键字<input name="keyword" required maxlength="1000" '
        'placeholder="输入全文检索关键字"></label>',
        20,
    )
    return HTMLResponse(_page("全文检索", body, session, settings, "full-text"))


@router.post("/api/full-text", response_class=HTMLResponse)
def full_text_action(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    csrf: Annotated[str, Form()],
    api_token: Annotated[str, Form()],
    keyword: Annotated[str, Form()],
    start_date: Annotated[str, Form()] = "",
    end_date: Annotated[str, Form()] = "",
    limit: Annotated[int, Form(ge=1, le=200)] = 20,
) -> HTMLResponse:
    _verify_csrf(csrf, session)
    payload = _compact(
        {
            "keyword": keyword,
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
        }
    )
    result = client.request("POST", "/api/v1/search/full-text", api_token, json_body=payload)
    return HTMLResponse(
        _page(
            "全文检索结果",
            _result_panel(result, "全文检索结果"),
            session,
            settings,
            "full-text",
        ),
        status_code=result.status_code,
    )


@router.get("/api/vector", response_class=HTMLResponse)
def vector_page(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    common = f"""{_csrf_input(session)}{_api_token_input()}
      {_input("开始日期", "start_date", "date")}
      {_input("结束日期", "end_date", "date")}"""
    body = (
        _api_intro(
            "向量检索",
            "支持直接输入向量，或上传文档、图片、音频，由已有 API 向量化并返回 Top 10。",
        )
        + f"""
    <div class="split"><section class="panel"><h2>输入向量</h2>
      <form method="post" action="/admin/api/vector" class="grid-form">{common}
      <label>向量字段<select name="vector_field"><option value="text">文本</option>
      <option value="image">图片</option><option value="audio">音频</option></select></label>
      <label class="wide">向量<textarea name="vector" required
      placeholder="0.12, -0.03, 0.8 ..."></textarea></label>
      {_input("Top K", "limit", "number", 10, min=1, max=200)}
      <div class="wide form-actions"><button>向量匹配</button></div></form></section>
    <section class="panel"><h2>上传文件检索</h2>
      <form method="post" action="/admin/api/vector/file" enctype="multipart/form-data"
      class="grid-form">{common}<label class="wide">查询文件
      <input type="file" name="file" required></label>
      <div class="wide form-actions"><button>向量化并查询 Top 10</button></div></form></section></div>"""
    )
    return HTMLResponse(_page("向量检索", body, session, settings, "vector"))


@router.post("/api/vector", response_class=HTMLResponse)
def vector_action(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    csrf: Annotated[str, Form()],
    api_token: Annotated[str, Form()],
    vector: Annotated[str, Form()],
    vector_field: Annotated[str, Form()] = "text",
    start_date: Annotated[str, Form()] = "",
    end_date: Annotated[str, Form()] = "",
    limit: Annotated[int, Form(ge=1, le=200)] = 10,
) -> HTMLResponse:
    _verify_csrf(csrf, session)
    try:
        values = [float(item) for item in re.split(r"[\s,]+", vector.strip()) if item]
    except ValueError as exc:
        raise MorphLakeError(
            "invalid_vector", "Vector must contain numbers separated by commas or spaces", 400
        ) from exc
    if not values:
        raise MorphLakeError("invalid_vector", "Vector must not be empty", 400)
    payload = _compact(
        {
            "vector": values,
            "vector_field": vector_field,
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
        }
    )
    result = client.request("POST", "/api/v1/search/vector", api_token, json_body=payload)
    return HTMLResponse(
        _page(
            "向量检索结果",
            _result_panel(result, "Top K 匹配"),
            session,
            settings,
            "vector",
        ),
        status_code=result.status_code,
    )


@router.post("/api/vector/file", response_class=HTMLResponse)
def vector_file_action(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    csrf: Annotated[str, Form()],
    api_token: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    start_date: Annotated[str, Form()] = "",
    end_date: Annotated[str, Form()] = "",
) -> HTMLResponse:
    _verify_csrf(csrf, session)
    data = _compact(
        {
            "start_date": start_date,
            "end_date": end_date,
        }
    )
    result = client.request(
        "POST",
        "/api/v1/search/vector/file",
        api_token,
        data=data,
        file=(
            file.filename or "query",
            file.file,
            file.content_type or "application/octet-stream",
        ),
    )
    return HTMLResponse(
        _page(
            "文件向量检索结果",
            _result_panel(result, "Top 10 匹配"),
            session,
            settings,
            "vector",
        ),
        status_code=result.status_code,
    )


@router.get("/api/download", response_class=HTMLResponse)
def download_page(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    body = _api_intro(
        "文件下载", "通过文件 ID 调用已有下载接口；响应以流式方式转发，不在管理端落盘。"
    )
    body += f"""<section class="panel"><form method="post" action="/admin/api/download"
      class="grid-form">{_csrf_input(session)}{_api_token_input()}
      {_input("文件 ID", "file_id", required=True)}
      <div class="wide form-actions"><button>下载文件</button></div></form></section>"""
    return HTMLResponse(_page("文件下载", body, session, settings, "download"))


@router.post("/api/download")
def download_action(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    csrf: Annotated[str, Form()],
    api_token: Annotated[str, Form()],
    file_id: Annotated[str, Form()],
) -> StreamingResponse:
    _verify_csrf(csrf, session)
    try:
        response, http_client = client.download(file_id, api_token)
    except httpx.HTTPError as exc:
        raise MorphLakeError("api_unreachable", str(exc), 502) from exc
    if response.status_code >= 400:
        body = response.read().decode(errors="replace")
        status = response.status_code
        response.close()
        http_client.close()
        raise MorphLakeError("download_failed", body[:1000], status)
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"content-disposition", "content-length", "etag"}
    }

    def close() -> None:
        response.close()
        http_client.close()

    return StreamingResponse(
        response.iter_raw(),
        media_type=response.headers.get("content-type", "application/octet-stream"),
        headers=headers,
        background=BackgroundTask(close),
    )


@router.get("/api/files/{file_id}/preview")
def preview_proxy(
    file_id: str,
    session: Annotated[AdminSession, Depends(require_admin_session)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    api_key: Annotated[str | None, Header(alias="X-MorphLake-Key")] = None,
) -> JSONResponse:
    del session
    result = client.request(
        "GET", f"/api/v1/files/{file_id}/preview", api_key or store.default_admin_token()
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@router.get("/api/files/{file_id}/thumbnail")
def thumbnail_proxy(
    file_id: str,
    session: Annotated[AdminSession, Depends(require_admin_session)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    api_key: Annotated[str | None, Header(alias="X-MorphLake-Key")] = None,
) -> StreamingResponse:
    del session
    return _stream_api_response(
        client, f"/api/v1/files/{file_id}/thumbnail", api_key or store.default_admin_token()
    )


@router.get("/api/files/{file_id}/media")
def media_proxy(
    file_id: str,
    session: Annotated[AdminSession, Depends(require_admin_session)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    api_key: Annotated[str | None, Header(alias="X-MorphLake-Key")] = None,
) -> StreamingResponse:
    del session
    return _stream_api_response(
        client, f"/api/v1/files/{file_id}/download", api_key or store.default_admin_token()
    )


@router.get("/api/status", response_class=HTMLResponse)
def status_page(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    body = _api_intro("API 状态", f"管理端当前连接：{settings.api_base_url}")
    body += f"""<section class="panel"><form method="post" action="/admin/api/status"
      class="grid-form">{_csrf_input(session)}{_api_token_input()}
      <div class="wide form-actions"><button>检查组件连通性</button></div></form></section>"""
    return HTMLResponse(_page("API 状态", body, session, settings, "status"))


@router.post("/api/status", response_class=HTMLResponse)
def status_action(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[AdminApiClient, Depends(get_admin_api_client)],
    csrf: Annotated[str, Form()],
    api_token: Annotated[str, Form()],
) -> HTMLResponse:
    _verify_csrf(csrf, session)
    result = client.request("GET", "/health/ready", api_token)
    return HTMLResponse(
        _page(
            "API 状态",
            _result_panel(result, "组件连通状态"),
            session,
            settings,
            "status",
        ),
        status_code=result.status_code,
    )


@router.get("/transfers", response_class=HTMLResponse)
def transfers_page(
    session: Annotated[AdminSession, Depends(require_admin_session)],
    store: Annotated[AdminStore, Depends(get_admin_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    period: Annotated[str, Query(pattern="^(day|week|month)$")] = "day",
) -> HTMLResponse:
    stat_rows = _rows(
        store.transfer_stats(period),
        [
            "token_prefix",
            "business_domain",
            "department",
            "operation",
            "status",
            "request_count",
            "byte_count",
        ],
    )
    detail_rows = _rows(
        store.recent_transfers(),
        [
            "occurred_at",
            "token_prefix",
            "operation",
            "business_domain",
            "department",
            "filename",
            "byte_count",
            "status",
            "error_code",
        ],
    )
    body = f"""<section class="panel"><div class="section-head"><h2>周期统计</h2>
      <div class="tabs"><a href="?period=day">天</a><a href="?period=week">周</a>
      <a href="?period=month">月</a></div></div><div class="table-wrap"><table>
      <thead><tr><th>Token</th><th>业务域</th><th>部门</th><th>操作</th><th>状态</th>
      <th>条数</th><th>字节数</th></tr></thead><tbody>{stat_rows or _empty_row(7)}</tbody>
      </table></div></section><section class="panel"><h2>最近传输明细</h2>
      <p class="hint">管理库保留近期缓存，长期明细同步到 Paimon。</p>
      <div class="table-wrap"><table><thead><tr><th>时间</th><th>Token</th><th>操作</th>
      <th>业务域</th><th>部门</th><th>文件</th><th>字节</th><th>状态</th><th>错误</th>
      </tr></thead><tbody>{detail_rows or _empty_row(9)}</tbody></table></div></section>"""
    return HTMLResponse(_page("传输统计", body, session, settings, "transfers"))


@router.get("/monitoring", response_class=HTMLResponse)
def monitoring_page(
    session: Annotated[AdminSession, Depends(require_admin_session)],
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
    cards = "".join(
        _card(name, _prometheus_value(results.get(name, [])), "Prometheus") for name in queries
    )
    body = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    body += f"""<div class="cards">{cards}</div><section class="panel">
      <h2>Prometheus 查询结果</h2>
      <pre>{html.escape(json.dumps(results, ensure_ascii=False, indent=2))}</pre></section>"""
    return HTMLResponse(_page("系统监控", body, session, settings, "monitoring"))


def _search_form(session: AdminSession, action: str, special: str, limit: int) -> str:
    return f"""<section class="panel"><form method="post" action="{action}"
      class="grid-form">{_csrf_input(session)}{_api_token_input()}
      {_input("开始日期", "start_date", "date")}
      {_input("结束日期", "end_date", "date")}{special}
      {_input("返回条数", "limit", "number", limit, min=1, max=200)}
      <div class="wide form-actions"><button>开始检索</button></div></form></section>"""


def _stream_api_response(client: AdminApiClient, path: str, api_key: str) -> StreamingResponse:
    try:
        response, http_client = client.stream(path, api_key)
    except httpx.HTTPError as exc:
        raise MorphLakeError("api_unreachable", str(exc), 502) from exc
    if response.status_code >= 400:
        body = response.read().decode(errors="replace")
        status = response.status_code
        response.close()
        http_client.close()
        raise MorphLakeError("preview_failed", body[:1000], status)
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"content-disposition", "content-length", "etag", "cache-control"}
    }

    def close() -> None:
        response.close()
        http_client.close()

    return StreamingResponse(
        response.iter_raw(),
        media_type=response.headers.get("content-type", "application/octet-stream"),
        headers=headers,
        background=BackgroundTask(close),
    )


def _result_panel(result: ApiResult, title: str) -> str:
    ok = 200 <= result.status_code < 300
    payload = result.payload if isinstance(result.payload, dict) else {"result": result.payload}
    items = payload.get("items") if isinstance(payload, dict) else None
    if items is None and isinstance(payload, dict) and payload.get("file_id"):
        items = [payload]
    table = _object_table(items) if isinstance(items, list) else ""
    badge = "成功" if ok else "失败"
    details_open = "" if table else " open"
    return f"""<div class="result-head"><span class="status-pill {"ok" if ok else "error"}">
      {badge} · HTTP {result.status_code}</span></div><section class="panel"><h2>{html.escape(title)}</h2>
      {table}<details{details_open}><summary>JSON 响应</summary>
      <pre>{html.escape(json.dumps(payload, ensure_ascii=False, indent=2, default=str))}</pre>
      </details></section>"""


def _object_table(items: list[Any], *, selectable: bool = False) -> str:
    if not items:
        return '<div class="empty">没有匹配数据</div>'
    if any(isinstance(item, dict) and item.get("file_id") for item in items):
        return _asset_table(
            [item for item in items if isinstance(item, dict)], selectable=selectable
        )
    keys = list(dict.fromkeys(key for item in items if isinstance(item, dict) for key in item))
    head = "".join(f"<th>{html.escape(str(key))}</th>" for key in keys)
    rows = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(str(item.get(key, '')))}</td>" for key in keys)
        + "</tr>"
        for item in items
        if isinstance(item, dict)
    )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>'


def _asset_table(items: list[dict[str, Any]], *, selectable: bool = False) -> str:
    columns = [
        ("rank", "排名"),
        ("asset_preview", "预览"),
        ("filename", "文件名"),
        ("media_type", "类型"),
        ("summary_text", "文本摘要"),
        ("embedding_preview", "向量预览"),
        ("file_size", "大小"),
        ("business_domain", "业务域"),
        ("department", "部门"),
        ("created_at", "创建时间"),
        ("content_text", "命中内容"),
    ]
    available = {key for item in items for key in item}
    selected = [
        (key, label) for key, label in columns if key == "asset_preview" or key in available
    ]
    head = (
        '<th><input id="select-all-files" type="checkbox" aria-label="选择本页全部文件"></th>'
        if selectable
        else ""
    ) + "".join(f"<th>{label}</th>" for _, label in selected)
    rows = []
    for item in items:
        file_id = html.escape(str(item.get("file_id") or ""))
        cells = (
            [
                f'<td><input class="file-select" type="checkbox" name="file_ids" '
                f'value="{file_id}" form="batch-delete-form" aria-label="选择文件"></td>'
            ]
            if selectable
            else []
        )
        for key, _ in selected:
            if key == "asset_preview":
                value = _asset_preview_cell(item)
            elif key == "filename":
                filename = html.escape(str(item.get(key) or "file"))
                value = f"""<button type="button" class="filename-download download-file"
                  data-file-id="{file_id}" data-filename="{filename}" title="{filename}"
                  aria-label="下载 {filename}">{filename}</button>"""
            elif key == "embedding_preview":
                vector = item.get(key)
                if vector:
                    compact = ",<br>".join(f"{float(number):.5g}" for number in vector[:4])
                    dimension = item.get("embedding_dimension") or "?"
                    value = f'<code class="vector-preview">[{compact},<br>…]</code><small>{dimension} 维</small>'
                else:
                    value = '<span class="hint">暂无</span>'
            elif key in {"summary_text", "content_text"}:
                text = str(item.get(key) or "")
                short = text if len(text) <= 120 else f"{text[:120]}…"
                value = f'<span class="text-clip" title="{html.escape(text)}">{html.escape(short)}</span>'
            elif key == "file_size":
                value = html.escape(_human_bytes(item.get(key)))
            elif key == "created_at":
                value = _browser_datetime(item.get(key))
            else:
                value = html.escape(str(item.get(key) or ""))
            cells.append(f"<td>{value}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return f'<div class="table-wrap"><table class="asset-table"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _asset_preview_cell(item: dict[str, Any]) -> str:
    file_id = html.escape(str(item.get("file_id") or ""))
    media_type = item.get("media_type")
    if media_type == "image" and item.get("thumbnail_available"):
        return f"""<button type="button" class="media-tile thumbnail-open" data-file-id="{file_id}"
          aria-label="放大图片"><span class="media-placeholder">图片</span><img alt="图片缩略图"></button>"""
    if media_type == "audio":
        return f"""<button type="button" class="media-tile audio-open" data-file-id="{file_id}"
          aria-label="播放音频"><span class="media-icon">♪</span><small>音频</small></button>"""
    return f"""<button type="button" class="media-tile preview-open" data-file-id="{file_id}"
      aria-label="查看文本"><span class="media-icon">▤</span><small>文档</small></button>"""


def _browser_datetime(value: Any) -> str:
    raw = str(value or "")
    escaped = html.escape(raw)
    date_part, _, time_part = raw.replace(" ", "T").partition("T")
    time_part = time_part[:8]
    return f"""<time class="browser-datetime" datetime="{escaped}" title="{escaped}">
      <span>{html.escape(date_part)}</span><small>{html.escape(time_part)}</small></time>"""


def _human_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "")}


def _pagination(filters: dict[str, Any], page: int, page_size: int, total_pages: int) -> str:
    def link(target: int, label: str) -> str:
        if target < 1 or target > total_pages:
            return f'<span class="page-link disabled">{label}</span>'
        query = urlencode(_compact({**filters, "page": target, "page_size": page_size}))
        return f'<a class="page-link" href="/admin/api/files?{html.escape(query)}">{label}</a>'

    hidden = "".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(str(value))}">'
        for key, value in filters.items()
        if value not in (None, "")
    )
    options = "".join(
        f'<option value="{size}" {"selected" if size == page_size else ""}>{size} 条/页</option>'
        for size in (10, 20, 50, 100, 200)
    )
    return f"""<div class="table-footer"><form method="get" action="/admin/api/files"
      class="page-size-form">{hidden}<input type="hidden" name="page" value="1">
      <label>每页条数<select name="page_size">{options}</select></label><button class="secondary">应用</button>
      </form><nav class="pagination" aria-label="文件清单分页">
      {link(page - 1, "上一页")}<span>第 {page} 页</span>{link(page + 1, "下一页")}</nav></div>"""


def _files_url(values: dict[str, Any]) -> str:
    query = urlencode(_compact(values))
    return f"/admin/api/files?{query}" if query else "/admin/api/files"


def _safe_files_return(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path != "/admin/api/files":
        return "/admin/api/files"
    return value


def _rows(rows: list[dict[str, Any]], keys: list[str]) -> str:
    return "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(str(row.get(key) or ''))}</td>" for key in keys)
        + "</tr>"
        for row in rows
    )


def _empty_row(columns: int) -> str:
    return f'<tr><td colspan="{columns}" class="empty">暂无数据</td></tr>'


def _csrf_input(session: AdminSession) -> str:
    return f'<input type="hidden" name="csrf" value="{session.csrf_token}">'


def _api_token_input() -> str:
    return """<label class="wide token-field">API Key
      <input class="api-token" type="password" name="api_token" required autocomplete="off"
      placeholder="mlk_…"><small>仅保存在当前浏览器标签页；业务范围由 Key 自动确定</small></label>"""


def _input(
    label: str,
    name: str,
    kind: str = "text",
    value: Any = "",
    **attrs: Any,
) -> str:
    rendered = " ".join(
        f'{key}="{html.escape(str(val))}"' if val is not True else key
        for key, val in attrs.items()
        if val is not False
    )
    return f"""<label>{html.escape(label)}<input type="{kind}" name="{name}"
      value="{html.escape(str(value))}" {rendered}></label>"""


def _scope_assignment_inputs(scopes: list[dict[str, str]]) -> str:
    domains = sorted({row["business_domain"] for row in scopes})
    department_map: dict[str, set[str]] = {"": set()}
    for row in scopes:
        department_map[""].add(row["department"])
        department_map.setdefault(row["business_domain"], set()).add(row["department"])
    serialized_map = {domain: sorted(departments) for domain, departments in department_map.items()}
    data_map = html.escape(json.dumps(serialized_map, ensure_ascii=False), quote=True)
    domain_options = "".join(f'<option value="{html.escape(value)}">' for value in domains)
    department_options = "".join(
        f'<option value="{html.escape(value)}">' for value in serialized_map[""]
    )
    return f"""<label>业务域<input id="scope-create-business-domain" name="business_domain"
      required maxlength="128" list="known-business-domains" data-departments="{data_map}"
      placeholder="选择或输入业务域"></label>
      <label>部门<input id="scope-create-department" name="department" required maxlength="128"
      list="known-departments" placeholder="选择或输入部门"></label>
      <datalist id="known-business-domains">{domain_options}</datalist>
      <datalist id="known-departments">{department_options}</datalist>"""


def _scope_filter_selects(
    *,
    scopes: list[dict[str, str]],
    selected_domain: str,
    selected_department: str,
) -> str:
    domains = sorted({row["business_domain"] for row in scopes})
    if selected_domain and selected_domain not in domains:
        domains.append(selected_domain)
        domains.sort()
    department_map: dict[str, set[str]] = {"": set()}
    for row in scopes:
        domain = row["business_domain"]
        department = row["department"]
        department_map[""].add(department)
        department_map.setdefault(domain, set()).add(department)
    if selected_department:
        department_map.setdefault(selected_domain, set()).add(selected_department)
        department_map[""].add(selected_department)
    serialized_map = {domain: sorted(departments) for domain, departments in department_map.items()}
    data_map = html.escape(json.dumps(serialized_map, ensure_ascii=False), quote=True)

    def options(values: list[str], selected: str, empty_label: str) -> str:
        rows = [f'<option value="">{empty_label}</option>']
        rows.extend(
            f'<option value="{html.escape(value)}" '
            f"{'selected' if value == selected else ''}>{html.escape(value)}</option>"
            for value in values
        )
        return "".join(rows)

    departments = sorted(department_map.get(selected_domain, department_map[""]))
    return f"""<label>业务域（可选）<select id="scope-business-domain"
      name="business_domain" data-departments="{data_map}">
      {options(domains, selected_domain, "全部业务域")}</select></label>
      <label>部门（可选）<select id="scope-department" name="department">
      {options(departments, selected_department, "全部部门")}</select></label>"""


def _api_intro(title: str, text: str) -> str:
    return f"""<section class="intro"><span class="eyebrow">API CONSOLE</span>
      <h2>{html.escape(title)}</h2><p>{html.escape(text)}</p></section>"""


def _quick(title: str, text: str, href: str) -> str:
    return f"""<a class="quick" href="{href}"><strong>{title}</strong><span>{text}</span>
      <b>打开 →</b></a>"""


def _card(title: str, value: Any, note: str = "") -> str:
    return f"""<div class="card"><span>{html.escape(title)}</span>
      <strong>{html.escape(str(value))}</strong><small>{html.escape(note)}</small></div>"""


def _token_row(row: dict[str, Any], csrf: str) -> str:
    token_id = html.escape(row["token_id"])
    action = "enable" if row["status"] == "disabled" else "disable"
    action_text = "启用" if action == "enable" else "停用"
    plaintext = row.get("plaintext")
    if plaintext:
        key_view = f"""<div class="key-view"><button type="button" class="secondary key-copy"
          data-target="{token_id}" data-key="{html.escape(plaintext)}">复制</button></div>"""
    else:
        key_view = """<span class="hint">历史 Key 无法恢复</span>"""
    permission = "全域管理" if row["access_level"] == "admin" else "业务域"
    delete_form = ""
    if row["access_level"] != "admin":
        delete_form = f"""<form class="inline" method="post"
          action="/admin/tokens/{token_id}/status/delete"><input type="hidden" name="csrf"
          value="{csrf}"><button class="danger">删除</button></form>"""
    expires = _browser_datetime(row["expires_at"]) if row.get("expires_at") else "永不过期"
    return f"""<tr><td>{key_view}<code>{html.escape(row["token_prefix"])}</code></td>
      <td><span class="status-pill {"admin-key" if row["access_level"] == "admin" else ""}">{permission}</span></td>
      <td>{html.escape(row["business_domain"])}<br><span class="hint">{html.escape(row["department"])}</span></td>
      <td>{html.escape(row["assignee_name"])}</td><td>{html.escape(row["phone"])}</td>
      <td>{html.escape(row["notes"])}</td><td><span class="status-pill {row["status"]}">{row["status"]}</span></td>
      <td>{_browser_datetime(row["created_at"])}<br><span class="hint">{expires}</span></td>
      <td><details><summary>{row["period_seconds"]} 秒</summary>
      <form method="post" action="/admin/tokens/{token_id}/limits" class="limit-form">
      <input type="hidden" name="csrf" value="{csrf}">
      {_input("周期秒", "period_seconds", "number", row["period_seconds"], min=1, required=True)}
      {_input("上传次数", "upload_requests_limit", "number", row["upload_requests_limit"], min=0, required=True)}
      {_input("上传字节", "upload_bytes_limit", "number", row["upload_bytes_limit"], min=0, required=True)}
      {_input("下载次数", "download_requests_limit", "number", row["download_requests_limit"], min=0, required=True)}
      {_input("下载字节", "download_bytes_limit", "number", row["download_bytes_limit"], min=0, required=True)}
      <button>保存</button></form></details></td>
      <td><form class="inline" method="post" action="/admin/tokens/{token_id}/status/{action}">
      <input type="hidden" name="csrf" value="{csrf}"><button class="secondary">{action_text}</button></form></td>
      <td><form class="inline" method="post" action="/admin/tokens/{token_id}/rotate">
      <input type="hidden" name="csrf" value="{csrf}"><button class="secondary">重新生成</button></form>
      {delete_form}</td></tr>"""


def _prometheus_value(result: list[dict[str, Any]]) -> str:
    if not result:
        return "—"
    if len(result) == 1:
        return str(result[0].get("value", [None, "—"])[1])
    return f"{len(result)} series"


def _login_page(next_path: str, error: str = "") -> str:
    alert = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>登录 · MorphLake</title><style>{_CSS}</style></head><body class="login-body">
    <main class="login-shell"><section class="login-brand"><span class="logo-mark">M</span>
    <span class="eyebrow">PAIMON MULTIMODAL FOUNDATION</span>
    <h1>让多模态数据<br>清晰、稳定、可检索</h1><p>统一管理文件、向量、权限与运行状态。</p>
    </section><section class="login-card"><div><span class="eyebrow">ADMIN CONSOLE</span>
    <h2>登录管理系统</h2><p>使用管理员账号继续</p></div>{alert}
    <form method="post" action="/admin/login"><input type="hidden" name="next" value="{html.escape(next_path)}">
    {_input("用户名", "username", required=True, autocomplete="username")}
    {_input("密码", "password", "password", required=True, autocomplete="current-password")}
    <button class="login-button">登录</button></form><small>登录会话默认 8 小时有效</small>
    </section></main></body></html>"""


def _page(
    title: str,
    body: str,
    session: AdminSession,
    settings: Settings,
    active: str,
) -> str:
    groups = [
        ("管理", [("dashboard", "工作台", "/admin"), ("tokens", "Key 管理", "/admin/tokens")]),
        (
            "数据能力",
            [
                ("upload", "文件上传", "/admin/api/upload"),
                ("files", "文件清单", "/admin/api/files"),
                ("full-text", "全文检索", "/admin/api/full-text"),
                ("vector", "向量检索", "/admin/api/vector"),
                ("download", "文件下载", "/admin/api/download"),
            ],
        ),
        (
            "运维",
            [
                ("transfers", "传输统计", "/admin/transfers"),
                ("monitoring", "系统监控", "/admin/monitoring"),
                ("status", "API 状态", "/admin/api/status"),
            ],
        ),
    ]
    nav = "".join(
        f'<div class="nav-group"><small>{label}</small>'
        + "".join(
            f'<a class="{"active" if key == active else ""}" href="{url}">'
            f'<span class="nav-dot"></span>{name}</a>'
            for key, name, url in items
        )
        + "</div>"
        for label, items in groups
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{html.escape(title)} · MorphLake</title><style>{_CSS}</style></head><body>
    <header class="topbar"><a class="brand" href="/admin"><span class="logo-mark small">M</span>
    <b>MorphLake</b><em>管理台</em></a><div class="top-actions">
    <span class="api-target">API · {html.escape(settings.api_base_url)}</span>
    <span class="avatar">{html.escape(session.username[:1].upper())}</span>
    <span>{html.escape(session.username)}</span><form method="post" action="/admin/logout"
    onsubmit="sessionStorage.removeItem('morphlakeApiToken')">
    <input type="hidden" name="csrf" value="{session.csrf_token}"><button class="link-button">退出</button>
    </form></div></header><aside class="sidebar">{nav}</aside>
    <main class="content"><div class="page-head"><div><span class="eyebrow">MORPHLAKE CONSOLE</span>
    <h1>{html.escape(title)}</h1></div></div>{body}</main>
    <dialog id="media-modal" class="media-modal"><div class="modal-head"><h2></h2>
    <button type="button" class="modal-close" aria-label="关闭">×</button></div>
    <div class="modal-body"></div></dialog><script>{_CONSOLE_JS}</script>
    </body></html>"""


_CONSOLE_JS = r"""
const storedKey=()=>sessionStorage.getItem('morphlakeApiToken')||'';
document.querySelectorAll('.api-token').forEach(el=>{
  el.value=storedKey();
  el.addEventListener('input',()=>sessionStorage.setItem('morphlakeApiToken',el.value));
});
document.querySelectorAll('.key-copy').forEach(button=>button.addEventListener('click',()=>{
  const key=button.dataset.key||'',done=()=>{
    button.textContent='已复制';setTimeout(()=>button.textContent='复制',1200);
  };
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(key).then(done);}
  else{const input=document.createElement('input');input.value=key;document.body.append(input);
    input.select();document.execCommand('copy');input.remove();done();}
}));

const pad=value=>String(value).padStart(2,'0');
document.querySelectorAll('.browser-datetime').forEach(element=>{
  const date=new Date(element.dateTime);
  if(Number.isNaN(date.getTime()))return;
  element.querySelector('span').textContent=date.getFullYear()+'-'+pad(date.getMonth()+1)+'-'+pad(date.getDate());
  element.querySelector('small').textContent=pad(date.getHours())+':'+pad(date.getMinutes())+':'+pad(date.getSeconds());
});

const batchDeleteForm=document.getElementById('batch-delete-form');
const fileSelections=[...document.querySelectorAll('.file-select')];
const selectAll=document.getElementById('select-all-files');
if(batchDeleteForm){
  const deleteButton=batchDeleteForm.querySelector('button'),selectedCount=document.getElementById('selected-count');
  const refreshSelection=()=>{const count=fileSelections.filter(item=>item.checked).length;
    deleteButton.disabled=count===0;selectedCount.textContent=count?'('+count+')':'';
    if(selectAll){selectAll.checked=count>0&&count===fileSelections.length;selectAll.indeterminate=count>0&&count<fileSelections.length;}};
  fileSelections.forEach(item=>item.addEventListener('change',refreshSelection));
  if(selectAll)selectAll.addEventListener('change',()=>{fileSelections.forEach(item=>item.checked=selectAll.checked);refreshSelection();});
  batchDeleteForm.addEventListener('submit',event=>{
    const count=fileSelections.filter(item=>item.checked).length;
    if(!count||!window.confirm('确定删除所选 '+count+' 个文件？删除后将无法通过接口访问。')){event.preventDefault();return;}
  });
  refreshSelection();
}

const scopeDomain=document.getElementById('scope-business-domain');
const scopeDepartment=document.getElementById('scope-department');
if(scopeDomain&&scopeDepartment){
  const departmentMap=JSON.parse(scopeDomain.dataset.departments||'{}');
  scopeDomain.addEventListener('change',()=>{
    const values=departmentMap[scopeDomain.value]||departmentMap['']||[];
    scopeDepartment.replaceChildren();
    const all=document.createElement('option');all.value='';all.textContent='全部部门';
    scopeDepartment.append(all);
    values.forEach(value=>{const option=document.createElement('option');
      option.value=value;option.textContent=value;scopeDepartment.append(option);});
  });
}

const createScopeDomain=document.getElementById('scope-create-business-domain');
const createScopeDepartments=document.getElementById('known-departments');
if(createScopeDomain&&createScopeDepartments){
  const departmentMap=JSON.parse(createScopeDomain.dataset.departments||'{}');
  createScopeDomain.addEventListener('input',()=>{
    const values=departmentMap[createScopeDomain.value]||departmentMap['']||[];
    createScopeDepartments.replaceChildren();
    values.forEach(value=>{const option=document.createElement('option');
      option.value=value;createScopeDepartments.append(option);});
  });
}

const setBusy=(button,busy,label='处理中…')=>{
  if(!button)return;
  if(busy){button.dataset.original=button.innerHTML;button.disabled=true;
    button.innerHTML='<span class="spinner"></span>'+label;}
  else{button.disabled=false;button.innerHTML=button.dataset.original||button.innerHTML;}
};
document.querySelectorAll('form[action^="/admin/api/"]').forEach(form=>form.addEventListener('submit',event=>{
  if(event.defaultPrevented)return;
  if(form.dataset.submitting==='1'){event.preventDefault();return;}
  form.dataset.submitting='1';const button=form.querySelector('button[type="submit"],button:not([type])');
  const label=form.id==='batch-delete-form'?'删除处理中…':(form.enctype==='multipart/form-data'?'上传处理中…':'查询处理中…');
  setBusy(button,true,label);
  setTimeout(()=>{form.dataset.submitting='0';setBusy(button,false);},60000);
}));

const modal=document.getElementById('media-modal'),modalTitle=modal.querySelector('h2');
const modalBody=modal.querySelector('.modal-body');
const showModal=(title,node)=>{modalTitle.textContent=title;modalBody.replaceChildren(node);modal.showModal();};
modal.querySelector('.modal-close').addEventListener('click',()=>modal.close());
modal.addEventListener('click',event=>{if(event.target===modal)modal.close();});
const apiFetch=async(path)=>{
  const key=storedKey(),headers=key?{'X-MorphLake-Key':key}:{};
  const response=await fetch(path,{headers});
  if(!response.ok){let message='请求失败（HTTP '+response.status+'）';
    try{const body=await response.json();message=body.error?.message||body.detail||message;}catch(_e){}
    throw new Error(message);}
  return response;
};
const showError=error=>window.alert(error.message||String(error));

document.querySelectorAll('.thumbnail-open').forEach(button=>button.addEventListener('click',async()=>{
  const image=button.querySelector('img');
  try{
    if(!image||!image.src){const response=await apiFetch('/admin/api/files/'+encodeURIComponent(button.dataset.fileId)+'/thumbnail');
      const url=URL.createObjectURL(await response.blob());document.querySelectorAll('.thumbnail-open[data-file-id="'+CSS.escape(button.dataset.fileId)+'"] img').forEach(item=>{item.src=url;item.hidden=false;});}
    const source=document.querySelector('.thumbnail-open[data-file-id="'+CSS.escape(button.dataset.fileId)+'"] img');
    const large=document.createElement('img');large.className='modal-image';large.src=source.src;
    showModal('图片预览',large);
  }catch(error){showError(error);}
}));
document.querySelectorAll('.media-tile.thumbnail-open img').forEach(async image=>{
  const button=image.closest('button');
  try{const response=await apiFetch('/admin/api/files/'+encodeURIComponent(button.dataset.fileId)+'/thumbnail');
    image.src=URL.createObjectURL(await response.blob());image.hidden=false;}
  catch(_error){button.classList.add('preview-unavailable');}
});
document.querySelectorAll('.preview-open').forEach(button=>button.addEventListener('click',async()=>{
  setBusy(button,true,'读取中…');
  try{const response=await apiFetch('/admin/api/files/'+encodeURIComponent(button.dataset.fileId)+'/preview');
    const data=await response.json(),wrap=document.createElement('div');wrap.className='text-preview';
    const summary=document.createElement('section');summary.innerHTML='<h3>文本摘要</h3>';
    const summaryText=document.createElement('p');summaryText.textContent=data.summary_text||'暂无摘要';summary.append(summaryText);
    const content=document.createElement('section');content.innerHTML='<h3>文本内容</h3>';
    const pre=document.createElement('pre');pre.textContent=data.content_text||'暂无可展示文本';content.append(pre);
    wrap.append(summary,content);showModal(data.filename,wrap);
  }catch(error){showError(error);}finally{setBusy(button,false);}
}));
document.querySelectorAll('.audio-open').forEach(button=>button.addEventListener('click',async()=>{
  setBusy(button,true,'加载中…');
  try{const response=await apiFetch('/admin/api/files/'+encodeURIComponent(button.dataset.fileId)+'/media');
    const audio=document.createElement('audio');audio.controls=true;audio.autoplay=true;
    audio.src=URL.createObjectURL(await response.blob());showModal('音频播放',audio);
  }catch(error){showError(error);}finally{setBusy(button,false);}
}));
document.querySelectorAll('.download-file').forEach(button=>button.addEventListener('click',async()=>{
  setBusy(button,true,'下载中…');
  try{const response=await apiFetch('/admin/api/files/'+encodeURIComponent(button.dataset.fileId)+'/media');
    const link=document.createElement('a');link.href=URL.createObjectURL(await response.blob());
    link.download=button.dataset.filename||'download';document.body.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(link.href),10000);
  }catch(error){showError(error);}finally{setBusy(button,false);}
}));
"""


_CSS = """
:root{--sidebar:180px;--blue:#3b65f6;--blue2:#2450d6;--ink:#1c2434;--muted:#697386;--line:#e7e9ee;--bg:#f7f8fa;--white:#fff;--green:#16864b;--red:#c83d4a;--amber:#9b6b00}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.5 Lato,"Helvetica Neue",Arial,Helvetica,"Microsoft YaHei",sans-serif}a{color:var(--blue)}
.topbar{height:64px;background:var(--white);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 20px;position:fixed;z-index:20;top:0;left:0;right:0}.brand{display:flex;align-items:center;gap:8px;color:var(--ink);text-decoration:none}.brand b{font-size:16px}.brand em{font-style:normal;color:var(--muted);border-left:1px solid var(--line);padding-left:9px}.logo-mark{display:grid;place-items:center;width:44px;height:44px;border-radius:12px;background:var(--blue);color:#fff;font-weight:800;font-size:20px}.logo-mark.small{width:32px;height:32px;border-radius:9px;font-size:14px}.top-actions{display:flex;align-items:center;gap:8px;color:var(--muted)}.top-actions form{margin:0}.api-target{padding:4px 9px;background:#f2f3f5;border-radius:16px;font-size:11px}.avatar{display:grid;place-items:center;width:28px;height:28px;background:#eef2ff;color:var(--blue2);border-radius:50%;font-weight:700}.link-button{border:0;background:transparent;color:var(--muted);padding:5px 7px;cursor:pointer}
.sidebar{position:fixed;z-index:10;top:64px;bottom:0;left:0;width:var(--sidebar);background:var(--white);border-right:1px solid var(--line);padding:12px 8px;overflow:auto}.nav-group{margin-bottom:13px}.nav-group small{display:block;color:#8a93a3;font-size:11px;letter-spacing:.04em;padding:6px 12px}.nav-group a{display:flex;align-items:center;gap:9px;color:#4f596b;text-decoration:none;padding:8px 11px;margin:3px 0;border-radius:7px;transition:.15s}.nav-group a:hover{background:#f4f6fb;color:var(--blue2)}.nav-group a.active{background:#eef2ff;color:var(--blue2);font-weight:600}.nav-dot{width:6px;height:6px;border-radius:50%;background:#a5adba}.active .nav-dot{background:var(--blue);box-shadow:0 0 0 3px #3b65f620}
.content{margin-left:var(--sidebar);padding:80px 18px 28px;max-width:1720px}.page-head{display:flex;justify-content:space-between;align-items:end;margin-bottom:12px}.page-head h1{font-size:22px;line-height:1.3;margin:1px 0}.eyebrow{color:var(--blue);font-size:9px;font-weight:800;letter-spacing:.12em}.panel,.intro,.quick,.card{background:var(--white);border:1px solid var(--line);border-radius:16px;box-shadow:0 1px 2px #1c243408}.panel{padding:16px;margin:0 0 12px}.panel h2,.intro h2{margin:0 0 4px;font-size:16px}.panel p,.intro p{color:var(--muted);margin:3px 0 11px}.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.intro{padding:14px 16px;margin-bottom:12px}.intro p{margin-bottom:0}
.cards{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:10px;margin-bottom:12px}.card{padding:14px}.card span,.card small{display:block;color:var(--muted)}.card strong{display:block;font-size:24px;margin:3px 0}.welcome{display:flex;justify-content:space-between;align-items:center;padding:18px}.welcome p{margin-bottom:0}.quick-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.quick{display:flex;flex-direction:column;padding:16px;text-decoration:none;color:var(--ink);transition:.15s}.quick:hover{border-color:#c8d3fa;background:#fbfcff}.quick strong{font-size:15px}.quick span{color:var(--muted);margin:5px 0 10px}.quick b{color:var(--blue);font-size:12px}
.grid-form{display:flex;flex-wrap:wrap;align-items:flex-end;gap:10px 12px}.grid-form>label{flex:1 1 185px;min-width:160px;max-width:300px}.grid-form>.wide{flex:2 1 360px;max-width:none}.grid-form>.token-field{flex-basis:100%;max-width:none}.grid-form>.form-actions{flex:0 0 auto;min-width:auto;max-width:none}.split{display:grid;grid-template-columns:1fr 1fr;gap:12px}.wide{min-width:0}label{display:block;color:#515b6d;font-size:12px;font-weight:600}input,textarea,select{display:block;width:100%;height:34px;margin-top:4px;padding:5px 9px;border:1px solid #d7dbe3;border-radius:7px;background:#fff;color:var(--ink);font:inherit;font-size:13px;line-height:1.35;outline:none}input[type=file]{padding:4px 7px}input:focus,textarea:focus,select:focus{border-color:var(--blue);box-shadow:0 0 0 2px #3b65f618}textarea{height:64px;min-height:64px;resize:vertical;padding-top:7px}label small,.hint{color:var(--muted);font-size:11px;font-weight:400}.token-field{padding:9px 11px;background:#f7f8fb;border-radius:9px}.form-actions{display:flex;align-items:center;gap:8px;min-height:34px}
.key-view{display:grid;grid-template-columns:auto;gap:4px;margin-bottom:4px}.key-view button{min-height:30px;padding:4px 8px}.admin-key{background:#eef2ff;color:var(--blue2)}
button,.button{display:inline-flex;align-items:center;justify-content:center;min-height:32px;border:0;border-radius:7px;background:var(--blue);color:#fff;padding:6px 13px;text-decoration:none;font-family:inherit;font-size:13px;font-weight:600;line-height:1.2;cursor:pointer}button:hover,.button:hover{background:var(--blue2)}button:disabled{cursor:wait;opacity:.7}.secondary{background:#f1f3f7;color:#46536a}.secondary:hover{background:#e7eaf0;color:#253047}.danger{background:#fff0f1;color:var(--red)}.danger:hover{background:#ffe4e6}.inline{display:inline}.spinner{display:inline-block;width:13px;height:13px;margin-right:7px;border:2px solid #ffffff70;border-top-color:#fff;border-radius:50%;vertical-align:-2px;animation:spin .72s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
.table-wrap{overflow:auto;margin-top:9px}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{background:#fafafb;color:#606a7b;font-size:11px;font-weight:600}td{color:#344054}.asset-table td{vertical-align:middle}.asset-table input[type=checkbox]{width:16px;height:16px;margin:0}.asset-table .text-clip{display:block;max-width:280px;white-space:normal;line-height:1.4}.vector-preview{display:block;width:76px;white-space:normal;line-height:1.25;color:#344054}.vector-preview+small,.browser-datetime small{display:block;color:var(--muted);margin-top:2px}.browser-datetime span{display:block}.filename-download{display:block;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-height:0;padding:0;background:transparent;color:var(--blue2);font-weight:500;text-align:left;line-height:1.5}.filename-download:hover{background:transparent;color:var(--blue);text-decoration:underline}.asset-toolbar{display:flex;align-items:center;gap:10px}.asset-toolbar form{margin:0}.asset-toolbar button{min-height:28px;padding:4px 9px;font-size:12px}.media-tile{position:relative;width:58px;height:46px;padding:0;overflow:hidden;border:1px solid #dce3ef;border-radius:8px;background:#f4f6fa;color:var(--blue2);display:grid;place-items:center}.media-tile img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.media-placeholder{font-size:10px;color:var(--muted)}.media-icon{font-size:20px;line-height:1}.media-tile small{font-size:9px}.media-tile:hover{background:#eef2ff;border-color:#c3cfea}.preview-unavailable:after{content:'暂无缩略图';position:absolute;inset:0;display:grid;place-items:center;background:#f4f6fa;color:var(--muted);font-size:9px}
.table-footer{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-top:12px}.page-size-form{display:flex;align-items:flex-end;gap:6px}.page-size-form label{min-width:105px}.page-size-form select{height:30px;margin-top:2px}.page-size-form button{min-height:30px;padding:4px 9px}.pagination{display:flex;align-items:center;justify-content:flex-end;gap:8px}.page-link{padding:5px 10px;border-radius:7px;background:#f1f3f7;text-decoration:none}.page-link.disabled{color:#a5adba;background:#f7f8fa}.notice-success{padding:9px 12px;margin-bottom:12px;border:1px solid #bce5ca;border-radius:8px;background:#edf9f1;color:var(--green)}
.media-modal{width:min(920px,92vw);max-height:88vh;padding:0;border:0;border-radius:16px;box-shadow:0 24px 70px #10182755}.media-modal::backdrop{background:#10182799}.modal-head{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--line)}.modal-head h2{margin:0;font-size:16px}.modal-close{width:30px;height:30px;min-height:30px;padding:0;border-radius:50%;background:#f1f3f7;color:var(--ink);font-size:20px}.modal-body{padding:16px;max-height:calc(88vh - 55px);overflow:auto}.modal-image{display:block;max-width:100%;max-height:72vh;margin:auto;border-radius:8px}.modal-body audio{display:block;width:min(680px,100%);margin:30px auto}.text-preview{display:grid;gap:12px}.text-preview section{padding:13px;border:1px solid var(--line);border-radius:10px}.text-preview h3{margin:0 0 7px}.text-preview p{white-space:pre-wrap}.text-preview pre{max-height:50vh}.empty{text-align:center!important;color:var(--muted);padding:24px!important}.status-pill{display:inline-block;padding:3px 8px;border-radius:16px;background:#f1f3f7;font-size:11px}.status-pill.ok,.status-pill.active{background:#e7f8ee;color:var(--green)}.status-pill.error,.status-pill.deleted{background:#fff0f1;color:var(--red)}.status-pill.disabled{background:#fff6dc;color:var(--amber)}.result-head{margin-bottom:9px}.tabs{display:flex;gap:6px}.tabs a{padding:4px 8px;background:#f1f3f7;border-radius:7px;text-decoration:none}.limit-form{min-width:310px;display:grid;grid-template-columns:1fr 1fr;gap:7px;padding:10px}.alert{padding:9px 12px;background:#fff7e8;border:1px solid #f1d59b;border-radius:8px;color:#775310;margin-bottom:11px}pre{white-space:pre-wrap;word-break:break-all;background:#151a24;color:#dbe7ff;padding:13px;border-radius:9px;max-height:460px;overflow:auto}.secret{font-size:14px}.token-created{text-align:center;max-width:720px;margin:30px auto}.success-mark{display:grid;place-items:center;width:46px;height:46px;margin:0 auto 10px;border-radius:50%;background:#e7f8ee;color:var(--green);font-size:23px}
.login-body{min-height:100vh;background:#f4f5f7;display:grid;place-items:center;padding:20px}.login-shell{width:min(860px,100%);display:grid;grid-template-columns:1fr .9fr;overflow:hidden;border:1px solid var(--line);border-radius:18px;background:#fff;box-shadow:0 18px 55px #27324a18}.login-brand{color:#fff;padding:52px 44px;background:#315ee8}.login-brand .eyebrow{color:#dbe4ff}.login-brand h1{font-size:31px;line-height:1.25;margin:18px 0 10px}.login-brand p{color:#dbe4ff}.login-card{background:#fff;padding:44px 40px;display:flex;flex-direction:column;justify-content:center}.login-card h2{font-size:22px;margin:5px 0}.login-card p{color:var(--muted);margin:0 0 15px}.login-card form{display:grid;gap:11px}.login-button{width:100%;margin-top:3px}.login-card>small{color:var(--muted);margin-top:14px;text-align:center}
@media(max-width:1000px){.cards{grid-template-columns:repeat(2,1fr)}.split{grid-template-columns:1fr}.api-target{display:none}.grid-form>label{max-width:none}}@media(max-width:760px){.topbar{padding:0 12px}.top-actions>span:not(.avatar){display:none}.sidebar{position:fixed;top:64px;width:100%;height:48px;bottom:auto;display:flex;overflow-x:auto;padding:5px 7px}.nav-group{display:flex;margin:0}.nav-group small{display:none}.nav-group a{white-space:nowrap;padding:7px 9px}.content{margin-left:0;padding:124px 10px 24px}.grid-form{display:grid;grid-template-columns:1fr}.grid-form>label,.grid-form>.wide,.grid-form>.form-actions{max-width:none}.quick-grid{grid-template-columns:1fr}.cards{grid-template-columns:1fr 1fr}.welcome{align-items:flex-start;gap:14px;flex-direction:column}.table-footer{align-items:stretch;flex-direction:column}.pagination{justify-content:flex-start}.login-shell{grid-template-columns:1fr}.login-brand{display:none}.login-card{padding:34px 25px}.brand em{display:none}}@media(max-width:430px){.cards{grid-template-columns:1fr}.top-actions .avatar{display:none}}
"""
