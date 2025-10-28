import atexit
import os
from typing import Optional

from dotenv import load_dotenv
from google.cloud.sql.connector import Connector, IPTypes

load_dotenv()  # 載入 .env

_connector: Optional[Connector] = None


def _get_connector() -> Connector:
    global _connector
    if _connector is None:
        _connector = Connector()
        atexit.register(_connector.close)
    return _connector


def _get_connection_name() -> Optional[str]:
    for env_key in ("INSTANCE_CONNECTION_NAME", "CLOUD_SQL_CONNECTION_NAME"):
        value = os.getenv(env_key)
        if value:
            return value
    return None


def get_db():
    """建立並回傳一條新的 DB 連線（autocommit=True）。使用端記得 close()。"""
    connection_name = _get_connection_name()
    if not connection_name:
        raise ValueError("CLOUD_SQL_CONNECTION_NAME environment variable not set.")

    connect_kwargs = {
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASS"),
        "db": os.getenv("DB_NAME"),
        "charset": "utf8mb4",
        "autocommit": True,
    }

    ip_type_env = os.getenv("CLOUD_SQL_IP_TYPE", "PUBLIC").upper()
    ip_type = IPTypes.PRIVATE if ip_type_env == "PRIVATE" else IPTypes.PUBLIC

    connector = _get_connector()
    return connector.connect(
        connection_name,
        "pymysql",
        **connect_kwargs,
        ip_type=ip_type,
    )

# 是否在查無此 LINE 使用者時自動建立一筆 users（預設 True；設 .env: AUTO_CREATE_USER_IF_MISSING=0 可關閉）
_AUTO_CREATE = os.getenv("AUTO_CREATE_USER_IF_MISSING", "1") != "0"

def _get_user_id_by_line(line_user_id: str) -> Optional[int]:
    conn = get_db()
    try:
        cur = conn.cursor()
        # 這裡用你實際的欄位：line_id
        cur.execute("SELECT id FROM users WHERE line_id=%s", (line_user_id,))
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else None
    finally:
        conn.close()

def _create_user_by_line(line_user_id: str) -> int:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (line_id, created_at) VALUES (%s, NOW())",
            (line_user_id,)
        )
        new_id = cur.lastrowid
        cur.close()
        return int(new_id) if new_id is not None else 0
    finally:
        conn.close()

def resolve_user_id_by_line(line_user_id: str) -> int:
    """
    依 users.line_id 取得 users.id。
    若找不到且允許自動建立，會先插入一筆再回傳其 id。
    """
    uid = _get_user_id_by_line(line_user_id)
    if uid is not None:
        return uid
    if _AUTO_CREATE:
        return _create_user_by_line(line_user_id)
    raise RuntimeError("找不到對應的 users.id，請先建立使用者與 LINE 綁定")

def insert_expense(
    line_user_id: str,
    item: str,
    amount: float,
    spent_date: Optional[str],
    image_url: Optional[str],
) -> int:
    """
    寫入 expenses：
    - created_at 使用 NOW()
    - spent_date 若為 None → 使用 CURRENT_DATE（等同 created_at 的日期）
    回傳新建 expenses.id。
    """
    user_id = resolve_user_id_by_line(line_user_id)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO expenses (user_id, item, amount, created_at, image_url, spent_date)
            VALUES (%s, %s, %s, NOW(), %s, COALESCE(%s, CURRENT_DATE))
            """,
            (user_id, item, float(amount), image_url, spent_date),
        )
        new_id = cur.lastrowid
        cur.close()
        return int(new_id) if new_id is not None else 0
    finally:
        conn.close()

# 若你已經有內部 user_id，可直接呼叫這支：
def insert_expense_raw(
    user_id: int,
    item: str,
    amount: float,
    spent_date: Optional[str],
    image_url: Optional[str],
) -> int:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO expenses (user_id, item, amount, created_at, image_url, spent_date)
            VALUES (%s, %s, %s, NOW(), %s, COALESCE(%s, CURRENT_DATE))
            """,
            (user_id, item, float(amount), image_url, spent_date),
        )
        new_id = cur.lastrowid
        cur.close()
        return int(new_id) if new_id is not None else 0
    finally:
        conn.close()
