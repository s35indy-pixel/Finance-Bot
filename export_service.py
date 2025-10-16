# export_service.py — 匯出 / 報表 / 快照（支援個人與群組帳本）
from __future__ import annotations
from typing import Iterable, Tuple, List, Optional, Dict, Any
from datetime import datetime, date, timedelta
import io
import csv
import math
import calendar
import decimal

from flask import Response
from utils_fx_date import HOME_CCY, now_local, get_fx_rate
from db import get_db
import expense_service

# =========================
# 基本工具
# =========================
def _d2f(v) -> float:
    if isinstance(v, decimal.Decimal):
        return float(v)
    try:
        return float(v or 0.0)
    except Exception:
        return 0.0

def default_year_month() -> Tuple[int, int]:
    n = now_local()
    return n.year, n.month

def _month_range_utc(year: int, month: int) -> Tuple[datetime, datetime]:
    """使用 created_at 的 [start, end) UTC 範圍查詢（DB 用 >= start AND < end）"""
    # 這裡不轉時區，直接用日期邏輯，created_at 通常是 datetime
    start = datetime(year, month, 1, 0, 0, 0)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0)
    return start, end

def _range_utc_by_ymd(start: str, end: str) -> Tuple[datetime, datetime]:
    """包含 end 的整日區間：轉為 [start_00:00:00, end_+1day_00:00:00)"""
    s_y, s_m, s_d = [int(x) for x in start.split("-")]
    e_y, e_m, e_d = [int(x) for x in end.split("-")]
    # 修正不合法日期（如 2025-09-31）
    last_s = calendar.monthrange(s_y, s_m)[1]
    last_e = calendar.monthrange(e_y, e_m)[1]
    s_d = min(s_d, last_s)
    e_d = min(e_d, last_e)
    s = datetime(s_y, s_m, s_d, 0, 0, 0)
    e = datetime(e_y, e_m, e_d, 23, 59, 59) + timedelta(seconds=1)
    return s, e

# 讓 app.py 可引用
def month_range(year: int, month: int) -> Tuple[datetime, datetime]:
    return _month_range_utc(year, month)

def range_utc(start: str | None, end: str | None, year: int | None = None, month: int | None = None):
    if start and end:
        return _range_utc_by_ymd(start, end)
    if year and month:
        return _month_range_utc(year, month)
    y, m = default_year_month()
    return _month_range_utc(y, m)

# FX 小工具（給 app.py 預估本幣）
def _fx_for(base_ccy: str | None, date_str_or_date: str | date | None) -> float | None:
    base = (base_ccy or HOME_CCY).upper()
    if base == HOME_CCY:
        return 1.0
    try:
        if isinstance(date_str_or_date, date):
            d = date_str_or_date
        elif isinstance(date_str_or_date, str) and date_str_or_date:
            y, m, dd = [int(x) for x in date_str_or_date.split("-")]
            d = date(y, m, dd)
        else:
            d = now_local().date()
        return float(get_fx_rate(base, HOME_CCY, d))
    except Exception:
        return None

# =========================
# 低階查詢
# =========================
def _fetch_rows_by(
    *,
    user_id: int | None = None,
    ledger_id: int | None = None,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> List[Dict[str, Any]]:
    assert (user_id is not None) ^ (ledger_id is not None), "需要 user_id 或 ledger_id 其中之一"
    db = get_db()
    try:
        with db.cursor(dictionary=True) as cur:
            cols = [
                "id","user_id","ledger_id","item","amount","currency_code","fx_rate",
                "amount_home","spent_date","category","is_income","created_at"
            ]
            where = []
            params: List[Any] = []
            if user_id is not None:
                where.append("user_id=%s"); params.append(user_id)
            if ledger_id is not None:
                where.append("ledger_id=%s"); params.append(ledger_id)
            if start_utc and end_utc:
                where.append("created_at >= %s AND created_at < %s")
                params.extend([start_utc, end_utc])

            sql = f"""
                SELECT {", ".join(cols)}
                  FROM expenses
                 WHERE {" AND ".join(where) if where else "1=1"}
                 ORDER BY created_at ASC, id ASC
            """
            cur.execute(sql, params)
            rows = cur.fetchall() or []
    finally:
        db.close()

    # 型別修正
    out: List[Dict[str, Any]] = []
    for r in rows:
        r["amount"] = _d2f(r.get("amount"))
        r["amount_home"] = _d2f(r.get("amount_home")) if r.get("amount_home") is not None else None
        # 一些 DB 可能把 is_income 存 0/1
        v = r.get("is_income")
        if v is None:
            r["is_income"] = None
        else:
            try:
                r["is_income"] = bool(int(v))
            except Exception:
                r["is_income"] = bool(v)
        out.append(r)
    return out

# =========================
# 匯出（個人）
# =========================
def export_monthly_rows(year: int, month: int, user_id: int) -> List[Dict[str, Any]]:
    s, e = _month_range_utc(year, month)
    return _fetch_rows_by(user_id=user_id, start_utc=s, end_utc=e)

def export_range_rows(start: str, end: str, user_id: int) -> List[Dict[str, Any]]:
    s, e = _range_utc_by_ymd(start, end)
    return _fetch_rows_by(user_id=user_id, start_utc=s, end_utc=e)

def generate_csv(rows: Iterable[Dict[str, Any]]) -> bytes:
    # Windows Excel 友善：UTF-8 BOM + CRLF
    buf = io.StringIO(newline="")                          # 讓 csv 控制換行
    writer = csv.writer(buf, lineterminator="\r\n")        # 使用 CRLF

    writer.writerow([
        "id","user_id","ledger_id","item","amount","currency","fx_rate","amount_home",
        "spent_date","category","is_income","created_at"
    ])
    for r in rows:
        writer.writerow([
            r.get("id"),
            r.get("user_id"),
            r.get("ledger_id"),
            (r.get("item") or "").replace("\n", " ").strip(),
            _d2f(r.get("amount")),
            (r.get("currency_code") or "").upper(),
            r.get("fx_rate") if r.get("fx_rate") is not None else "",
            r.get("amount_home") if r.get("amount_home") is not None else "",
            r.get("spent_date") or "",
            r.get("category") or "",
            (1 if r.get("is_income") else 0) if r.get("is_income") is not None else "",
            r.get("created_at") or "",
        ])

    csv_text = buf.getvalue()
    bom = "\ufeff"                                         # UTF-8 BOM
    return (bom + csv_text).encode("utf-8")

# =========================
# 刪除資料
# =========================
def delete_user_data(line_user_id: str) -> int:
    uid = expense_service.get_or_create_user(line_user_id)
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM expenses WHERE user_id=%s", (uid,))
            affected = cur.rowcount or 0
        db.commit()
        return int(affected)
    finally:
        db.close()

def delete_ledger_data(ledger_id: int) -> int:
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM expenses WHERE ledger_id=%s", (ledger_id,))
            affected = cur.rowcount or 0
        db.commit()
        return int(affected)
    finally:
        db.close()


# =========================
# 查詢摘要（群組 / 房間 / 個人 with ledger）
# =========================
def _title_by(s: datetime, e: datetime, prefix: str = "") -> str:
    # e 是閉區間的下一秒 → 顯示需 -1 秒
    real_end = e - timedelta(seconds=1)
    period = f"{s.strftime('%Y-%m-%d')} ~ {real_end.strftime('%Y-%m-%d')}"
    return f"{prefix}查詢摘要（{period}）" if prefix else f"查詢摘要（{period}）"

def _render_report_text(rows: List[Dict[str, Any]], title: str = "查詢摘要") -> str:
    total_in = 0.0
    total_out = 0.0
    cats: Dict[str, float] = {}

    for r in rows:
        base = r["amount_home"] if r["amount_home"] is not None else r["amount"]
        amt = float(base or 0.0)
        inc = r.get("is_income") or (r.get("category") in {"薪資","獎金","投資","退款","其他收入"})
        if inc:
            total_in += amt
        else:
            total_out += amt
            cat = r.get("category") or "其他"
            cats[cat] = cats.get(cat, 0.0) + amt

    net = total_in - total_out
    cats_sorted = sorted(cats.items(), key=lambda kv: -kv[1])
    cats_lines = [f"  - {k}: {_fmt_money(v)}" for k, v in cats_sorted]

    # 最近 10 筆
    recent = rows[-10:]
    recent_lines = [
        f"• {r.get('created_at')} {r.get('item') or ''} {_fmt_money(r.get('amount'))} {(r.get('currency_code') or '').upper()}"
        for r in recent
    ]

    text = (
        f"📊 {title}\n"
        f"收入：{_fmt_money(total_in)}\n"
        f"支出：{_fmt_money(total_out)}\n"
        f"結餘：{_fmt_money(net)}\n\n"
        f"分類支出：\n" + ("\n".join(cats_lines) if cats_lines else "  - （無）")
    )
    if recent:
        text += "\n\n最近 10 筆：\n" + "\n".join(recent_lines)
    return text

def render_query_summary_for_context(
    line_id: str | None,
    context_type: str,
    context_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    year: int | None = None,
    month: int | None = None,
    context_display: str | None = None,
) -> str:
    # 解析目前聊天室帳本
    safe_line = line_id or context_id
    _, ledger_id = expense_service.resolve_active_ledger(context_type, context_id, safe_line)

    s, e = range_utc(start, end, year, month)
    rows = _fetch_rows_by(ledger_id=ledger_id, start_utc=s, end_utc=e)

    prefix = (context_display or f"{context_type}:{context_id}") + " "
    title = _title_by(s, e, prefix=prefix)
    return _render_report_text(rows, title=title)

# =========================
# 快照（本月或自訂區間；總計＋最近 N 筆）
# =========================
# export_service.py
def render_snapshot_for_context(
    line_id: str | None,
    context_type: str,
    context_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int = 5
) -> str:
    safe_line = line_id or context_id
    _, ledger_id = expense_service.resolve_active_ledger(context_type, context_id, safe_line)

    if start and end:
        s, e = _range_utc_by_ymd(start, end)
        real_end = (e - timedelta(seconds=1)).date().isoformat()
        title = f"{s.date().isoformat()} ~ {real_end}"
    else:
        y, m = default_year_month()
        s, e = _month_range_utc(y, m)
        real_end = (e - timedelta(seconds=1)).date().isoformat()
        title = f"{s.date().isoformat()} ~ {real_end}（本月）"

    rows = _fetch_rows_by(ledger_id=ledger_id, start_utc=s, end_utc=e)

    total_in = 0.0
    total_out = 0.0
    cats: Dict[str, float] = {}

    for r in rows:
        base = r["amount_home"] if r["amount_home"] is not None else r["amount"]
        amt = float(base or 0.0)
        inc = r.get("is_income") or (r.get("category") in {"薪資","獎金","投資","退款","其他收入"})
        if inc:
            total_in += amt
        else:
            total_out += amt
            cat = r.get("category") or "其他"
            cats[cat] = cats.get(cat, 0.0) + amt

    net = total_in - total_out

    # 分類（由大到小）
    cats_sorted = sorted(cats.items(), key=lambda kv: -kv[1])
    cats_lines = [f"  - {k}: {_fmt_money(v)}" for k, v in cats_sorted]

    recent = rows[-limit:] if limit else rows
    recent_lines = [
        f"• {r.get('created_at')} {r.get('item') or ''} {_fmt_money(r.get('amount'))} {(r.get('currency_code') or '').upper()}"
        for r in recent
    ]

    return (
        f"📖 查詢\n期間 : {title}\n"
        f"收入：{_fmt_money(total_in)}\n"
        f"支出：{_fmt_money(total_out)}\n"
        f"結餘：{_fmt_money(net)}\n\n"
        f"分類支出：\n" + ("\n".join(cats_lines) if cats_lines else "  - （無）") + "\n\n"
        f"最近 {len(recent)} 筆：\n" + ("\n".join(recent_lines) if recent_lines else "  - （無）")
    )

# =========================
# CSV 匯出（群組 / 房間 / 個人 with ledger）
# =========================
def _rows_for_csv(ledger_id: int, *, year: int | None = None, month: int | None = None, start: str | None = None, end: str | None = None):
    s, e = range_utc(start, end, year, month)
    return _fetch_rows_by(ledger_id=ledger_id, start_utc=s, end_utc=e)

def csv_bytes_for_ledger(ledger_id: int, *, year: int | None = None, month: int | None = None, start: str | None = None, end: str | None = None) -> bytes:
    rows = _rows_for_csv(ledger_id, year=year, month=month, start=start, end=end)
    return generate_csv(rows)

# =========================
# 個人 CSV 下載（相容 app.py 的呼叫方式）
# =========================
def handle_csv_download(line_user_id: str, *, year: int | None = None, month: int | None = None, start: str | None = None, end: str | None = None):
    uid = expense_service.get_or_create_user(line_user_id)
    if start and end:
        rows = export_range_rows(start, end, uid)
        title = f"expenses_{start.replace('-', '')}_{end.replace('-', '')}"
    else:
        if not (year and month):
            year, month = default_year_month()
        rows = export_monthly_rows(year, month, uid)
        title = f"expenses_{year:04d}_{month:02d}"
    content = generate_csv(rows)
    headers = {
        "Content-Disposition": f'attachment; filename="{title}.csv"',
        "Content-Type": "text/csv; charset=utf-8",
        "Cache-Control": "no-store",
    }
    return Response(content, headers=headers)
