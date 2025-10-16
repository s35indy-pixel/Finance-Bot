# expense_service.py — 使用者 / 帳本 / 記帳（含預算＆對話狀態）
from __future__ import annotations
import json
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime, date, timedelta

import logging

from mysql.connector import Error as MySQLError

from db import get_db
from utils_fx_date import HOME_CCY, now_local

# ==============
# 初始化 & 資料表
# ==============

_SCHEMA_SQL = [
    # 使用者
    """
    CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        line_user_id VARCHAR(64) NOT NULL UNIQUE,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    # 會話帳本（對應 user / group / room）
    """
    CREATE TABLE IF NOT EXISTS ledgers (
        id INT AUTO_INCREMENT PRIMARY KEY,
        context_type VARCHAR(16) NOT NULL,
        context_id   VARCHAR(64) NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_ctx (context_type, context_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    # 暫存待確認
    """
    CREATE TABLE IF NOT EXISTS pending_ex (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        ledger_id INT NOT NULL,
        item VARCHAR(255) NOT NULL,
        amount DECIMAL(12,2) NOT NULL,
        currency_code VARCHAR(3) NOT NULL DEFAULT 'TWD',
        fx_rate DECIMAL(12,6) NULL,
        amount_home DECIMAL(12,2) NULL,
        spent_date DATE NULL,
        category VARCHAR(64) NULL,
        is_income TINYINT(1) NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_user_ledger_created (user_id, ledger_id, created_at),
        CONSTRAINT fk_p_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_p_ledger FOREIGN KEY (ledger_id) REFERENCES ledgers(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    # 正式支出/收入（若你原本就有此表，這段只會確保欄位存在）
    """
    CREATE TABLE IF NOT EXISTS expenses (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        ledger_id INT NOT NULL,
        item VARCHAR(255) NOT NULL,
        amount DECIMAL(12,2) NOT NULL,
        currency_code VARCHAR(3) NOT NULL DEFAULT 'TWD',
        fx_rate DECIMAL(12,6) NULL,
        amount_home DECIMAL(12,2) NULL,
        spent_date DATE NULL,
        category VARCHAR(64) NULL,
        is_income TINYINT(1) NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_ledger_created (ledger_id, created_at),
        INDEX idx_ledger_date (ledger_id, spent_date),
        CONSTRAINT fk_e_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
        CONSTRAINT fk_e_ledger FOREIGN KEY (ledger_id) REFERENCES ledgers(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    # 總預算（僅總額；分類預算若未來需要可另開 budgets_cat）
    """
    CREATE TABLE IF NOT EXISTS budgets (
        id INT AUTO_INCREMENT PRIMARY KEY,
        context_type VARCHAR(16) NOT NULL,
        context_id   VARCHAR(64) NOT NULL,
        ledger_id INT NOT NULL,
        start_date DATE NOT NULL,
        end_date   DATE NOT NULL,
        total_amount DECIMAL(12,2) NOT NULL,
        currency_code VARCHAR(3) NOT NULL DEFAULT 'TWD',
        created_by INT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_budget (ledger_id, start_date, end_date),
        INDEX idx_budget_lookup (ledger_id, start_date, end_date),
        CONSTRAINT fk_b_ledger FOREIGN KEY (ledger_id) REFERENCES ledgers(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    # 對話狀態（查詢/報表/預算流程）
    """
    CREATE TABLE IF NOT EXISTS user_states (
        id INT AUTO_INCREMENT PRIMARY KEY,
        context_type VARCHAR(16) NOT NULL,
        context_id   VARCHAR(64) NOT NULL,
        line_user_id VARCHAR(64) NOT NULL,
        kind VARCHAR(16) NOT NULL,
        step VARCHAR(16) NOT NULL,
        payload JSON NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_ctx_user_created (context_type, context_id, line_user_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
]

logger = logging.getLogger(__name__)


def _ensure_schema():
    """Ensure required tables exist. Skip gracefully if DB is unreachable."""
    db = None
    try:
        db = get_db()
    except MySQLError as exc:  # pragma: no cover - defensive during startup
        logger.warning(
            "Skipping schema initialization because DB is unavailable: %s", exc
        )
        return
    except Exception:  # pragma: no cover - unexpected setup failure
        logger.exception("Unexpected error while establishing DB connection")
        raise

    try:
        with db.cursor() as cur:
            for sql in _SCHEMA_SQL:
                cur.execute(sql)
        db.commit()
    except MySQLError:
        logger.exception("Failed while ensuring DB schema")
        raise
    finally:
        if db is not None:
            db.close()

_ensure_schema()

# ==============
# 小工具
# ==============

def _amount_in_home(row: Dict[str, Any]) -> float:
    """用於預算計算：優先 amount_home，否則 amount * fx_rate(或1)"""
    ah = row.get("amount_home")
    if ah is not None:
        try: return float(ah)
        except Exception: return 0.0
    try:
        amt = float(row.get("amount") or 0)
        fx = float(row.get("fx_rate") or 1.0)
        return round(amt * fx, 2)
    except Exception:
        return 0.0

# =======================
# 使用者 / 帳本 取得或建立
# =======================

def get_or_create_user(line_user_id: str) -> int:
    """
    users 表用 line_id (VARCHAR) 做唯一鍵；回傳 users.id (INT)
    """
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE line_id=%s LIMIT 1", (line_user_id,))
            row = cur.fetchone()
            if row:
                return int(row[0] if not isinstance(row, dict) else row["id"])
            cur.execute("INSERT INTO users(line_id) VALUES(%s)", (line_user_id,))
            db.commit()
            return int(cur.lastrowid)
    finally:
        db.close()

def _get_or_create_ledger(context_type: str, context_id: str) -> int:
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id FROM ledgers WHERE context_type=%s AND context_id=%s",
                (context_type, context_id),
            )
            row = cur.fetchone()
            if row:
                return int(row[0] if isinstance(row, tuple) else row["id"])
            # 生成預設名稱
            if context_type == "user":
                name = f"個人帳本-{context_id[:8]}"
            elif context_type == "group":
                name = f"群組帳本-{context_id[:8]}"
            elif context_type == "room":
                name = f"聊天室帳本-{context_id[:8]}"
            else:
                name = f"帳本-{context_id[:8]}"
            
            cur.execute(
                "INSERT INTO ledgers(name, context_type, context_id) VALUES(%s,%s,%s)",
                (name, context_type, context_id),
            )
            db.commit()
            return int(cur.lastrowid)
    finally:
        db.close()

def resolve_active_ledger(context_type: str, context_id: str, line_user_id: Optional[str]) -> Tuple[int, int]:
    """
    回傳 (user_id, ledger_id)。個人/群組/聊天室皆以 (context_type, context_id) 作為共享帳本。
    """
    # user id 以 line_user_id 建立（群組中也需要）
    uid = get_or_create_user(line_user_id or f"{context_type}:{context_id}")
    lid = _get_or_create_ledger(context_type, context_id)
    return uid, lid

# =======================
# 暫存 pending 流程
# =======================

def create_pending_ex_ctx(
    *,
    context_type: str,
    context_id: str,
    line_id: str,
    item: str,
    amount: float,
    currency_code: str,
    fx_rate: Optional[float],
    amount_home: Optional[float],
    spent_date: Optional[str | date],
    note: Optional[str] = None,  # 保留參數位移相容
    category: Optional[str] = None,
    is_income: Optional[bool] = None,
) -> Dict[str, Any]:
    uid, lid = resolve_active_ledger(context_type, context_id, line_id)

    if isinstance(spent_date, date):
        spent_date = spent_date.isoformat()

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pending_ex
                (user_id, ledger_id, item, amount, currency_code, fx_rate, amount_home, spent_date, category, is_income)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (uid, lid, item, float(amount), currency_code.upper(),
                 (fx_rate if fx_rate is not None else None),
                 (float(amount_home) if amount_home is not None else None),
                 spent_date, category, (1 if is_income else 0) if is_income is not None else None),
            )
            db.commit()
            pid = int(cur.lastrowid)

            cur.execute("SELECT * FROM pending_ex WHERE id=%s", (pid,))
            row = cur.fetchone()
            # 轉成 dict
            if row and not isinstance(row, dict):
                desc = cur.description
                row = {desc[i][0]: row[i] for i in range(len(desc))}
            return row or {"id": pid, "item": item, "amount": amount, "currency_code": currency_code,
                           "amount_home": amount_home, "spent_date": spent_date, "category": category,
                           "is_income": is_income}
    finally:
        db.close()

def get_latest_pending_valid_ctx(context_type: str, context_id: str, line_id: str) -> Optional[Dict[str, Any]]:
    uid, lid = resolve_active_ledger(context_type, context_id, line_id)
    db = get_db()
    try:
        with db.cursor() as cur:
            # 取 20 分鐘內最後一筆
            cur.execute(
                """
                SELECT * FROM pending_ex
                 WHERE user_id=%s AND ledger_id=%s
                   AND created_at >= (NOW() - INTERVAL 20 MINUTE)
                 ORDER BY id DESC LIMIT 1
                """, (uid, lid)
            )
            row = cur.fetchone()
            if not row:
                return None
            if not isinstance(row, dict):
                desc = cur.description
                row = {desc[i][0]: row[i] for i in range(len(desc))}
            return row
    finally:
        db.close()

def update_pending_ex(pid: int, **kwargs) -> Optional[Dict[str, Any]]:
    if not kwargs:
        return get_pending_by_id(pid)

    fields, params = [], []
    for k, v in kwargs.items():
        if k not in {"item","amount","currency_code","fx_rate","amount_home","spent_date","category","is_income"}:
            continue
        fields.append(f"{k}=%s")
        # bool → tinyint
        if k == "is_income" and v is not None:
            v = 1 if bool(v) else 0
        params.append(v)
    if not fields:
        return get_pending_by_id(pid)

    params.append(pid)
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(f"UPDATE pending_ex SET {', '.join(fields)} WHERE id=%s", params)
            db.commit()
            cur.execute("SELECT * FROM pending_ex WHERE id=%s", (pid,))
            row = cur.fetchone()
            if not row:
                return None
            if not isinstance(row, dict):
                desc = cur.description
                row = {desc[i][0]: row[i] for i in range(len(desc))}
            return row
    finally:
        db.close()

def get_pending_by_id(pid: int) -> Optional[Dict[str, Any]]:
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM pending_ex WHERE id=%s", (pid,))
            row = cur.fetchone()
            if not row:
                return None
            if not isinstance(row, dict):
                desc = cur.description
                row = {desc[i][0]: row[i] for i in range(len(desc))}
            return row
    finally:
        db.close()

def cancel_pending(pid: int) -> bool:
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM pending_ex WHERE id=%s", (pid,))
            affected = cur.rowcount or 0
        db.commit()
        return affected > 0
    finally:
        db.close()

def confirm_pending(pid: int) -> Optional[Dict[str, Any]]:
    """相容舊名稱"""
    return confirm_pending_ex(pid)

def confirm_pending_ex(pid: int) -> Optional[Dict[str, Any]]:
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM pending_ex WHERE id=%s", (pid,))
            row = cur.fetchone()
            if not row:
                return None
            if not isinstance(row, dict):
                desc = cur.description
                row = {desc[i][0]: row[i] for i in range(len(desc))}
            # 寫入 expenses
            cur.execute(
                """
                INSERT INTO expenses
                (user_id, ledger_id, item, amount, currency_code, fx_rate, amount_home, spent_date, category, is_income)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    row["user_id"], row["ledger_id"], row["item"], row["amount"],
                    row["currency_code"], row.get("fx_rate"), row.get("amount_home"),
                    row.get("spent_date"), row.get("category"),
                    None if row.get("is_income") is None else (1 if row.get("is_income") else 0),
                )
            )
            cur.execute("DELETE FROM pending_ex WHERE id=%s", (pid,))
            db.commit()

            # 回傳剛剛寫入 expenses 的資料（簡化：直接用 row 為準）
            saved = dict(row)
            saved.pop("id", None)  # pending id
            saved["id"] = None     # 不查回 exp id，避免多一次 round trip
            return saved
    finally:
        db.close()

# =======================
# 對話狀態（查詢/報表/預算）
# =======================

def push_state(context_type: str, context_id: str, line_user_id: str,
               kind: str, step: str, payload):
    """
    user_states 表固定用 line_id 欄位
    """
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO user_states(context_type, context_id, line_id, kind, step, payload) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (context_type, context_id, line_user_id, kind, step, json.dumps(payload or {}, ensure_ascii=False)),
            )
        db.commit()
    finally:
        db.close()

def pop_latest_state(context_type: str, context_id: str, line_user_id: str):
    """
    讀取並刪除最近一筆狀態；固定用 line_id 欄位
    """
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM user_states
                 WHERE context_type=%s AND context_id=%s AND line_id=%s
                 ORDER BY id DESC LIMIT 1
                """,
                (context_type, context_id, line_user_id)
            )
            row = cur.fetchone()
            if not row:
                return None
            if not isinstance(row, dict):
                desc = cur.description
                row = {desc[i][0]: row[i] for i in range(len(desc))}
            cur.execute("DELETE FROM user_states WHERE id=%s", (row["id"],))
        db.commit()
        try:
            p = row.get("payload")
            if isinstance(p, (bytes, bytearray)):
                row["payload"] = json.loads(p.decode("utf-8"))
            elif isinstance(p, str):
                row["payload"] = json.loads(p or "{}")
            else:
                row["payload"] = p or {}
        except Exception:
            row["payload"] = {}
        return row
    finally:
        db.close()

# =========
# 預算功能
# =========

def set_budget_total_ctx(context_type: str, context_id: str, line_user_id: str,
                         start: str, end: str, amount: float,
                         currency_code: str = HOME_CCY) -> int:
    """
    設定（或覆蓋）該會話帳本在指定區間的總預算。
    """
    uid, lid = resolve_active_ledger(context_type, context_id, line_user_id)
    db = get_db()
    try:
        with db.cursor() as cur:
            # 先嘗試更新；若無則插入（用 unique key 保障）
            cur.execute(
                """
                INSERT INTO budgets (context_type, context_id, ledger_id, start_date, end_date, total_amount, currency_code, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE total_amount=VALUES(total_amount), currency_code=VALUES(currency_code)
                """,
                (context_type, context_id, lid, start, end, float(amount), currency_code, uid)
            )
        db.commit()
        return lid
    finally:
        db.close()

def _active_budget_for(ledger_id: int, on_date: date) -> Optional[Dict[str, Any]]:
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM budgets
                 WHERE ledger_id=%s AND start_date<=%s AND end_date>=%s
                 ORDER BY id DESC LIMIT 1
                """,
                (ledger_id, on_date, on_date)
            )
            row = cur.fetchone()
            if not row:
                return None
            if not isinstance(row, dict):
                desc = cur.description
                row = {desc[i][0]: row[i] for i in range(len(desc))}
            return row
    finally:
        db.close()

def _sum_spending_in_range(ledger_id: int, start: date, end: date) -> float:
    """
    計算該帳本在 [start, end] 的「支出總和」(收入不算)，以本幣計。
    """
    db = get_db()
    try:
        with db.cursor(dictionary=True) as cur:
            cur.execute(
                """
                SELECT amount, fx_rate, amount_home, is_income
                  FROM expenses
                 WHERE ledger_id=%s AND spent_date>=%s AND spent_date<=%s
                """,
                (ledger_id, start, end)
            )
            rows = cur.fetchall() or []
    finally:
        db.close()

    total = 0.0
    for r in rows:
        # 跳過收入
        inc = r.get("is_income")
        try:
            inc = (bool(int(inc)) if inc is not None else False)
        except Exception:
            inc = bool(inc)
        if inc:
            continue
        total += _amount_in_home(r)
    return round(total, 2)

def render_budget_status_ctx(context_type: str, context_id: str, line_user_id: str) -> str:
    """
    顯示今日所屬區間的預算狀態（若沒有，顯示最近一筆設定）。
    """
    _, lid = resolve_active_ledger(context_type, context_id, line_user_id)
    today = now_local().date()

    b = _active_budget_for(lid, today)
    if not b:
        # 找最近一筆
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute("SELECT * FROM budgets WHERE ledger_id=%s ORDER BY id DESC LIMIT 1", (lid,))
                row = cur.fetchone()
                if row and not isinstance(row, dict):
                    desc = cur.description
                    row = {desc[i][0]: row[i] for i in range(len(desc))}
                b = row
        finally:
            db.close()
        if not b:
            return "目前尚未設定預算。可用「預算」→ 選擇本月 / 自訂區間。"

    start = b["start_date"]; end = b["end_date"]
    total = float(b["total_amount"])
    spent = _sum_spending_in_range(lid, start, end)
    remain = total - spent
    pct = (spent / total * 100) if total > 0 else 0
    state = "✅ 進度良好" if remain >= 0 else "⚠️ 已超過預算！"
    remain_abs = abs(remain)
    if remain >= 0:
        return f"{state}\n期間：{start} ~ {end}\n總預算：{total:,.2f} {HOME_CCY}\n已用：{spent:,.2f}（{pct:.0f}%）\n還剩：{remain:,.2f} {HOME_CCY}"
    else:
        return f"{state}\n期間：{start} ~ {end}\n總預算：{total:,.2f} {HOME_CCY}\n已用：{spent:,.2f}（{pct:.0f}%）\n超支：{remain_abs:,.2f} {HOME_CCY}"

def format_budget_alert_for_expense(expense_row: Dict[str, Any]) -> Optional[str]:
    """
    入帳後提醒用：
    - 找到與 spent_date 所屬的預算
    - 計算新累計 vs 總額 → 回覆「還剩/超支」
    """
    lid = int(expense_row.get("ledger_id"))
    d = expense_row.get("spent_date")
    spent_dt = d if isinstance(d, date) else (datetime.strptime(d, "%Y-%m-%d").date() if d else now_local().date())

    b = _active_budget_for(lid, spent_dt)
    if not b:
        return None

    total = float(b["total_amount"])
    start = b["start_date"]; end = b["end_date"]
    spent = _sum_spending_in_range(lid, start, end)
    remain = total - spent
    pct = (spent / total * 100) if total > 0 else 0

    if remain >= 0:
        return f"✅ 預算狀態：還剩 {remain:,.2f} {HOME_CCY}（已用 {pct:.0f}%）"
    else:
        return f"⚠️ 預算警示：已超過 {abs(remain):,.2f} {HOME_CCY}！"

# =========
# 其它工具
# =========

def list_expenses_in_range(ledger_id: int, start: date, end: date) -> List[Dict[str, Any]]:
    db = get_db()
    try:
        with db.cursor(dictionary=True) as cur:
            cur.execute(
                """
                SELECT * FROM expenses
                 WHERE ledger_id=%s AND spent_date>=%s AND spent_date<=%s
                 ORDER BY spent_date ASC, id ASC
                """,
                (ledger_id, start, end)
            )
            return cur.fetchall() or []
    finally:
        db.close()
