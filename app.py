# app.py — 覆蓋版：查詢/報表/預算/雲端
import os, sys, re, json, configparser, calendar
from datetime import datetime
from urllib.parse import parse_qs
from typing import Optional, Tuple

from flask import Flask, request, Response

import calendar
from openai import AzureOpenAI
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent, ImageMessageContent, AudioMessageContent,
    PostbackEvent, FollowEvent, JoinEvent, MemberJoinedEvent
)
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage, QuickReply, QuickReplyItem,
    PostbackAction, MessageAction, DatetimePickerAction,
    PushMessageRequest, FlexMessage
)

import expense_service
import export_service
from ocr_handler import OCRHandler
from ai_parser import parse_expense
from utils_fx_date import (
    init_from_config, get_fx_rate, HOME_CCY, parse_date_zh, now_local
)
import flex_ui

# =========================
# 設定（config.ini + 環境變數）
# =========================
config = configparser.ConfigParser()
read_ok = config.read("config.ini")
if not read_ok:
    print("[FATAL] 找不到 config.ini，請確認檔案存在於工作目錄")
    sys.exit(1)

# 初始化 FX 與時區等
init_from_config(config)

# 建暫存資料夾（語音檔等）
os.makedirs("temp", exist_ok=True)

# ===== Azure OpenAI 設定 =====
AOAI_ENDPOINT = config["AzureOpenAI"].get("END_POINT")
AOAI_KEY = config["AzureOpenAI"].get("API_KEY")
AOAI_API_VERSION = config["AzureOpenAI"].get("API_VERSION", "2024-08-01-preview")
AOAI_TEXT_DEPLOYMENT = config["AzureOpenAI"].get("TEXT_DEPLOYMENT", "gpt-4o-sindy-20250815")
AOAI_VISION_DEPLOYMENT = config["AzureOpenAI"].get("VISION_DEPLOYMENT", AOAI_TEXT_DEPLOYMENT)
AOAI_WHISPER_DEPLOYMENT = config["AzureOpenAI"].get("WHISPER_DEPLOYMENT", "whisper20250815")

missing = []
if not AOAI_ENDPOINT: missing.append("AzureOpenAI.END_POINT")
if not AOAI_KEY: missing.append("AzureOpenAI.API_KEY")
if missing:
    print(f"[FATAL] config.ini 缺少必要設定：{', '.join(missing)}")
    sys.exit(1)

# 同步到環境變數供其他模組使用
os.environ["AOAI_ENDPOINT"] = AOAI_ENDPOINT
os.environ["AOAI_KEY"] = AOAI_KEY
os.environ["AOAI_API_VERSION"] = AOAI_API_VERSION
os.environ["AOAI_TEXT_DEPLOYMENT"] = AOAI_TEXT_DEPLOYMENT
os.environ["AOAI_VISION_DEPLOYMENT"] = AOAI_VISION_DEPLOYMENT
os.environ["AOAI_WHISPER_DEPLOYMENT"] = AOAI_WHISPER_DEPLOYMENT

# AzureOpenAI 客戶端
_aoai_client = AzureOpenAI(
    api_key=AOAI_KEY, api_version=AOAI_API_VERSION, azure_endpoint=AOAI_ENDPOINT
)
_aoai_client_for_audio = _aoai_client

print(f"[AOAI] api_version={AOAI_API_VERSION}")
print(f"[AOAI] text={AOAI_TEXT_DEPLOYMENT}, vision={AOAI_VISION_DEPLOYMENT}, whisper={AOAI_WHISPER_DEPLOYMENT}")

# ===== Flask / LINE =====
app = Flask(__name__)

channel_access_token = (
    os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    or config.get("Line", "CHANNEL_ACCESS_TOKEN", fallback=None)
)
channel_secret = (
    os.getenv("LINE_CHANNEL_SECRET")
    or config.get("Line", "CHANNEL_SECRET", fallback=None)
)
if not channel_secret or not channel_access_token:
    print("[FATAL] 缺少 LINE 憑證，請在 config.ini 或環境變數補齊：LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET")
    sys.exit(1)

handler = WebhookHandler(channel_secret)
configuration = Configuration(access_token=channel_access_token)
line_client = ApiClient(configuration)
messaging_api = MessagingApi(line_client)
messaging_blob = MessagingApiBlob(line_client)

# OCR handler
ocr_handler = OCRHandler(configuration=configuration)

# =========================
# 小工具
# =========================
HELP_TEXT = (
    "📖 使用說明：\n"
    "1️⃣ 直接輸入「品項 金額」即可記帳，例如：午餐 120\n"
    "　　- 支援日期：今天/昨天/前天、2025-09-01\n"
    "　　- 支援幣別：咖啡 350 JPY、晚餐 15 USD\n"
    "　　- 收入：輸入『薪資 50000』、『退款 300』\n"
    "\n"
    "2️⃣ 查詢 → 顯示總計＋分類彙總＋最近5筆（附CSV下載）\n"
    "3️⃣ 預算 → 設定本月或自訂區間總預算，入帳後提醒剩餘/超支\n"
    "4️⃣ 匯出 → 本月或自訂日期區間，產生 CSV\n"
    "5️⃣ 清空 → 個人或群組帳本資料刪除（群組需輸入「清空 確認」）\n"
    "\n"
    "📌 個人的記帳有快速功能表，可直接點選查詢、預算、匯出。\n"
    "\n"
    "📸 也支援收據OCR（拍照）、語音輸入 → 自動辨識金額/品項/日期/幣別。\n"
)

def _normalize_text(s: str) -> str:
    mapping = str.maketrans("０１２３４５６７８９　", "0123456789 ")
    s = (s or "").translate(mapping)
    s = re.sub(r"^@\S+\s+", "", s)     # 去掉 @機器人 提及
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _is_complete_expense_format(text: str) -> bool:
    """
    檢查文字是否為完整的記帳格式（品項 金額）
    例如：午餐 100、咖啡 80、薪資 50000
    """
    # 基本格式：品項 + 空格 + 數字（後面可能有其他文字）
    if re.search(r"\s+[0-9]+(?:\.[0-9]{1,2})?\s", text) or re.search(r"\s+[0-9]+(?:\.[0-9]{1,2})?$", text):
        # 確保不是純數字
        if not re.match(r"^[0-9]+(?:\.[0-9]{1,2})?\s*$", text.strip()):
            return True
    
    # 包含幣別的格式：品項 + 空格 + 數字 + 空格 + 幣別
    if re.search(r"\s+[0-9]+(?:\.[0-9]{1,2})?\s+[A-Za-z]{3}", text):
        return True
    
    return False

def _reply_text(event, text: str, quick: QuickReply|None=None):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text, quick_reply=quick)]
            )
        )

def _qr_export_menu():
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label="本月", data="act=emenu&mode=month")),
        QuickReplyItem(action=PostbackAction(label="選起始日", data="act=emenu&mode=range")),
        QuickReplyItem(action=PostbackAction(label="手動輸入日期", data="act=emenu&mode=manual")),  # ← 新增
    ])

def _qr_group_clear_confirm() -> QuickReply:
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label="⚠️ 確定清空", data="act=gclear&confirm=yes")),
        QuickReplyItem(action=PostbackAction(label="取消", data="act=gclear&confirm=no")),
    ])

def _qr_user_clear_confirm() -> QuickReply:
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label="⚠️ 確定清空（個人）", data="act=uclear&confirm=yes")),
        QuickReplyItem(action=PostbackAction(label="取消", data="act=uclear&confirm=no")),
    ])

def _resolve_context(event) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    src = event.source
    if getattr(src, "group_id", None):
        return "group", src.group_id, getattr(src, "user_id", None)
    if getattr(src, "room_id", None):
        return "room", src.room_id, getattr(src, "user_id", None)
    if getattr(src, "user_id", None):
        return "user", src.user_id, src.user_id
    return None, None, None

def _qr_query_menu():
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label="本月", data="act=qmenu&mode=month")),
        QuickReplyItem(action=PostbackAction(label="選起始日", data="act=qmenu&mode=range")),
        QuickReplyItem(action=PostbackAction(label="手動輸入日期", data="act=qmenu&mode=manual")),  # ← 新增
    ])


def _qr_budget_menu():
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label="設定本月總預算", data="act=budget&mode=month")),
        QuickReplyItem(action=PostbackAction(label="設定自訂區間總預算", data="act=budget&mode=range")),
        QuickReplyItem(action=PostbackAction(label="查目前餘額", data="act=budget&mode=status")),
    ])

def quick_reply_actions() -> QuickReply:
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="📖 查詢", text="查詢")),
        QuickReplyItem(action=MessageAction(label="💰 預算", text="預算")),
        QuickReplyItem(action=MessageAction(label="📂 匯出", text="匯出")),
        QuickReplyItem(action=MessageAction(label="❓ 說明", text="說明")),
        QuickReplyItem(action=MessageAction(label="🗑 清空", text="清空")), 
    ])

def quick_reply_main(pid: int, item: str, amount: float) -> QuickReply:
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label="✅ 確認", data=f"act=confirm&pid={pid}")),
        QuickReplyItem(action=PostbackAction(label="✏️ 修改", data=f"act=edit_menu&pid={pid}")),
        QuickReplyItem(action=PostbackAction(label="❌ 取消", data=f"act=cancel&pid={pid}")),
    ])

def quick_reply_edit_prompt(pid: int) -> QuickReply:
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label="返回選單", data=f"act=edit_menu&pid={pid}")),
        QuickReplyItem(action=PostbackAction(label="取消此筆", data=f"act=cancel&pid={pid}")),
    ])

def quick_reply_pick_date(pid: int) -> QuickReply:
    today = str(now_local().date())
    return QuickReply(items=[
        QuickReplyItem(action=DatetimePickerAction(
            label="📅 選日期", data=f"act=pick_date&pid={pid}",
            mode="date", initial=today, max=today, min="2020-01-01"
        )),
        QuickReplyItem(action=PostbackAction(label="返回選單", data=f"act=edit_menu&pid={pid}")),
        QuickReplyItem(action=PostbackAction(label="取消此筆", data=f"act=cancel&pid={pid}")),
    ])

def quick_reply_edit_menu(pid: int) -> QuickReply:
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label="改金額", data=f"act=edit_amt&pid={pid}")),
        QuickReplyItem(action=PostbackAction(label="改品項", data=f"act=edit_item&pid={pid}")),
        QuickReplyItem(action=PostbackAction(label="改日期", data=f"act=edit_date&pid={pid}")),
        QuickReplyItem(action=PostbackAction(label="改類別", data=f"act=edit_cat&pid={pid}")),
        QuickReplyItem(action=PostbackAction(label="返回", data=f"act=back&pid={pid}")),
    ])

# === 日曆小工具：挑「起始日」與「結束日」 ===
def _qr_pick_start(kind: str) -> QuickReply:
    today = str(now_local().date())
    return QuickReply(items=[
        QuickReplyItem(action=DatetimePickerAction(
            label="📅 選起始日",
            data=f"act=pick_start&kind={kind}",
            mode="date",
            initial=today,
            max=today,
            min="2020-01-01",
        ))
    ])

def _qr_pick_end(kind: str, start_date: str) -> QuickReply:
    # 結束日不得小於起始日
    today = str(now_local().date())
    initial = start_date if start_date <= today else today
    return QuickReply(items=[
        QuickReplyItem(action=DatetimePickerAction(
            label="📅 選結束日",
            data=f"act=pick_end&kind={kind}&start={start_date}",
            mode="date",
            initial=initial,
            max=today,
            min=start_date,
        ))
    ])


# —— 快照/報表 —— 
def _send_snapshot(event, ctype, cid, line_id, start: str|None, end: str|None):
    try:
        # 1) 取得原始快照資料
        print(f"[DEBUG] _send_snapshot: 查詢參數 - ctype={ctype}, cid={cid}, line_id={line_id}, start={start}, end={end}")
        raw = export_service.render_snapshot_for_context(
            line_id=line_id, context_type=ctype, context_id=cid,
            start=start, end=end, limit=5
        )
        print(f"[DEBUG] _send_snapshot: raw data length = {len(raw) if raw else 0}")
        print(f"[DEBUG] _send_snapshot: raw data content = {repr(raw)}")
    except Exception as e:
        print(f"[ERROR] _send_snapshot: {e}")
        flex_ui.reply_empty(event, "查詢資料時發生錯誤，請稍後再試。")
        return

    # 2) 解析期間
    if start and end:
        period = f"{start} ~ {end}"
    else:
        n = now_local()
        last_day = calendar.monthrange(n.year, n.month)[1]
        s = f"{n.year:04d}-{n.month:02d}-01"
        e = f"{n.year:04d}-{n.month:02d}-{last_day:02d}"
        period = f"{s} ~ {e}"

    # 3) 解析文字內容取得統計資料
    lines = (raw or "").splitlines()
    print(f"[DEBUG] _send_snapshot: lines count = {len(lines)}")
    print(f"[DEBUG] _send_snapshot: all lines = {lines}")
    if len(lines) < 3:
        # 沒有資料的情況
        print("[DEBUG] _send_snapshot: insufficient lines, showing empty message")
        flex_ui.reply_empty(event, "目前沒有符合條件的紀錄")
        return

    # 解析收入、支出、結餘
    total_in = total_out = net = 0.0
    top_cats = []
    recent_items = []
    
    for line in lines[2:]:  # 跳過前兩行標題
        if "收入：" in line:
            try:
                amount_str = line.split("收入：")[1].strip().replace(",", "")
                total_in = float(amount_str)
                print(f"[DEBUG] 解析收入: '{line}' -> '{amount_str}' -> {total_in}")
            except Exception as e:
                print(f"[DEBUG] 解析收入失敗: '{line}' -> {e}")
        elif "支出：" in line:
            try:
                amount_str = line.split("支出：")[1].strip().replace(",", "")
                total_out = float(amount_str)
                print(f"[DEBUG] 解析支出: '{line}' -> '{amount_str}' -> {total_out}")
            except Exception as e:
                print(f"[DEBUG] 解析支出失敗: '{line}' -> {e}")
        elif "結餘：" in line:
            try:
                amount_str = line.split("結餘：")[1].strip().replace(",", "")
                net = float(amount_str)
                print(f"[DEBUG] 解析結餘: '{line}' -> '{amount_str}' -> {net}")
            except Exception as e:
                print(f"[DEBUG] 解析結餘失敗: '{line}' -> {e}")
        elif line.startswith("  - ") and ":" in line:
            # 分類支出
            try:
                cat_part = line[4:].split(":")
                if len(cat_part) == 2:
                    cat_name = cat_part[0].strip()
                    amount_str = cat_part[1].strip().replace(",", "")
                    cat_amount = float(amount_str)
                    top_cats.append((cat_name, cat_amount))
            except:
                pass
        elif line.startswith("• ") and " " in line:
            # 最近紀錄 - 格式: • {created_at} {item} {amount} {currency}
            try:
                # 移除開頭的 "• "
                content = line[2:]
                # 找到最後兩個數字（amount 和 currency）
                parts = content.split(" ")
                if len(parts) >= 3:
                    # 取最後兩個部分作為 amount 和 currency
                    amount_str = parts[-2]
                    currency = parts[-1]
                    # 中間的部分是 item（可能包含空格）
                    item_name = " ".join(parts[1:-2]) if len(parts) > 3 else parts[1]
                    
                    # 清理 item 名稱，移除時間戳
                    if ":" in item_name and len(item_name.split(":")[0]) <= 2:
                        # 如果開頭看起來像時間戳，移除它
                        item_parts = item_name.split(" ", 1)
                        if len(item_parts) > 1 and ":" in item_parts[0]:
                            item_name = item_parts[1]
                    
                    recent_items.append({
                        "item": item_name.strip(),
                        "amount": float(amount_str),
                        "currency_code": currency
                    })
            except:
                pass

    # 4) 建立 CSV 下載連結
    base = os.getenv("PUBLIC_BASE_URL", request.host_url.rstrip("/"))
    qs = [f"ctype={ctype}", f"cid={cid}"]
    if start and end:
        qs += [f"start={start}", f"end={end}"]
    csv_url = f"{base}/api/ledger_csv?" + "&".join(qs)

    # 5) 使用 Flex Message 顯示
    data = {
        "period": period,
        "total_in": total_in,
        "total_out": total_out,
        "net": net,
        "top_cats": top_cats,
        "recent_items": recent_items,
        "csv_url": csv_url
    }
    print(f"[DEBUG] _send_snapshot: data = {data}")
    
    # 先發送純文字訊息
    text_message = f"""📖 查詢結果
期間：{data['period']}
收入：{data['total_in']:,.2f}
支出：{data['total_out']:,.2f}
結餘：{data['net']:,.2f}

分類支出：
"""
    for name, amount in data['top_cats']:
        text_message += f"  - {name}: {amount:,.2f}\n"
    
    text_message += "\n最近紀錄：\n"
    for item in data['recent_items']:
        text_message += f"  • {item['item']} {item['amount']:,.2f} {item['currency_code']}\n"
    
    text_message += f"\n📎 CSV 下載：{data['csv_url']}"
    
    # 嘗試發送 Flex Message
    try:
        flex_ui.reply_query_summary(event, data, messaging_api)
        print("[DEBUG] _send_snapshot: Flex Message sent successfully")
    except Exception as e:
        print(f"[ERROR] _send_snapshot: Flex Message failed: {e}")
        # 如果 Flex Message 失敗，回退到純文字訊息
        try:
            _reply_text(event, text_message)
            print("[DEBUG] _send_snapshot: Text message sent as fallback")
        except Exception as e2:
            print(f"[ERROR] _send_snapshot: Text message also failed: {e2}")
            _reply_text(event, f"顯示查詢結果時發生錯誤：{str(e2)}")



def _send_budget_hint_after_confirm(event, expense_row: dict):
    try:
        msg = expense_service.format_budget_alert_for_expense(expense_row)
        if msg:
            _reply_text(event, msg)
    except Exception as e:
        print("[budget hint error]", e)

# =========================
# 健康檢查（Azure App Service）
# =========================
@app.get("/healthz")
def healthz():
    return {"ok": True, "api_version": AOAI_API_VERSION}

# =========================
# LINE Webhook
# =========================
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return Response("invalid signature", status=400)
    except Exception as e:
        print("[ERROR webhook]", e)
        return Response("error", status=500)
    return "OK"

# ========= 指令處理（查詢 / 報表 / 匯出 / 清空 / 說明）
def _handle_commands(event, text: str) -> bool:
    ctype, cid, line_id = _resolve_context(event)
    if not ctype:
        _reply_text(event, "無法辨識來源，請在群組或私聊使用。", quick_reply_actions()); return True

    # 說明
    if text in {"說明", "help", "HELP", "？"}:
        flex_ui.reply_help(event, messaging_api); return True

    # 查詢 / 預算 → 顯示 Flex Message
    if text == "查詢":
        flex_ui.reply_query_menu(event, messaging_api); return True
    if text == "預算":
        _reply_text(event, "預算功能：", _qr_budget_menu()); return True
    if text == "匯出":
        _reply_text(event, "請選擇範圍：", _qr_export_menu()); return True

    # 匯出（舊語法保留）
    if text.startswith("匯出") or text == "csv":
        _, ledger_id = expense_service.resolve_active_ledger(ctype, cid, line_id or cid)
        dt = now_local()
        # 支援「匯出 yyyy-mm-dd~yyyy-mm-dd」
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*[~\-]\s*(\d{4}-\d{2}-\d{2})", text)
        base = request.host_url.rstrip("/")
        if m:
            s, e = m.group(1), m.group(2)
            url = f"{base}/api/ledger/{ledger_id}/expenses.csv?start={s}&end={e}"
            _reply_text(event, f"📂 區間匯出：{s} ~ {e}\n{url}", quick_reply_actions()); return True
        url = f"{base}/api/ledger/{ledger_id}/expenses.csv?year={dt.year}&month={dt.month}"
        _reply_text(event, f"📂 本月匯出：\n{url}", quick_reply_actions()); return True

    norm = " ".join(text.strip().lower().split())

    # 個人清空：改為 QuickReply 兩段式
    if ctype == "user":
        if norm in {"清空", "刪除全部", "delete"}:
            _reply_text(
                event,
                "⚠️ 這會刪除『你的個人帳本』的所有紀錄且不可復原。\n請確認是否清空？",
                _qr_user_clear_confirm()
            ); 
            return True
        if norm in {"清空 確認", "刪除全部 確認", "delete confirm"}:  # 相容舊語法
            deleted = export_service.delete_user_data(line_id)
            _reply_text(event, f"🗑 已刪除 {deleted} 筆個人帳本資料。", quick_reply_actions()); 
            return True

    # 清空（群組/多人聊天室：兩段式確認，改用 QuickReply）
    if ctype in {"group", "room"}:
        if norm in {"清空", "刪除全部", "delete"}:
            _reply_text(
                event,
                "⚠️ 這會刪除『此群組帳本』的所有紀錄且不可復原。\n請確認是否清空？",
                _qr_group_clear_confirm()
            ); 
            return True

        if norm in {"清空 確認", "刪除全部 確認", "delete confirm"}:
            # 保留舊文案相容：若有人打舊語法一樣可用
            _, ledger_id = expense_service.resolve_active_ledger(ctype, cid, line_id or cid)
            deleted = export_service.delete_ledger_data(ledger_id)
            _reply_text(event, f"🗑 已刪除 {deleted} 筆此群組帳本資料。", quick_reply_actions()); 
            return True
    return False

# ========= 編輯流程（相容原本行為）
def _update_pending_ex_safe(pid: int, **kwargs):
    try:
        return expense_service.update_pending_ex(pid, **kwargs)
    except TypeError as e:
        m = re.search(r"unexpected keyword argument '(\w+)'", str(e))
        if m:
            kwargs.pop(m.group(1), None)
            return _update_pending_ex_safe(pid, **kwargs)
        raise

def _handle_stateful_input(event, text: str) -> bool:
    # 先解析來源
    ctype, cid, line_id = _resolve_context(event)
    if not (ctype and cid):
        return False

    # 讀取最近一次的對話狀態（若有就彈出）
    st = expense_service.pop_latest_state(ctype, cid, line_id or cid)
    if not st:
        return False

    # 重要：先解開 st，再使用 kind/step/payload
    kind = st.get("kind") or ""
    step = st.get("step") or ""
    payload = st.get("payload") or {}

    # ===== 查詢：手動輸入日期（單行 "YYYY-MM-DD ~ YYYY-MM-DD" 或兩步驟）=====
    if kind == "query" and step == "await_manual":
        txt = text.strip()

        # A) 一次輸入 "YYYY-MM-DD ~ YYYY-MM-DD"
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*[~\-]\s*(\d{4}-\d{2}-\d{2})", txt)
        if m:
            s, e = m.group(1), m.group(2)
            _send_snapshot(event, ctype, cid, line_id or cid, s, e)
            return True

        # B) 只輸入一個日期 → 視為起始日，下一步等結束日
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
            expense_service.push_state(ctype, cid, line_id or cid, "query", "await_manual_end", {"start": txt})
            _reply_text(event, f"起始日：{txt}\n請再輸入結束日（YYYY-MM-DD）。")
            return True

        # 其他 → 引導重新輸入
        expense_service.push_state(ctype, cid, line_id or cid, "query", "await_manual", {})
        _reply_text(event, "格式不正確，請輸入：2025-09-01 ~ 2025-09-30，或先輸入起始日（YYYY-MM-DD）。")
        return True

    if kind == "query" and step == "await_manual_end":
        start = (payload or {}).get("start")
        end = text.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            expense_service.push_state(ctype, cid, line_id or cid, "query", "await_manual_end", {"start": start})
            _reply_text(event, "結束日格式不正確，請輸入 YYYY-MM-DD。")
            return True

        _send_snapshot(event, ctype, cid, line_id or cid, start, end)
        return True

    # ===== 匯出：手動輸入日期（單行或兩步驟）=====
    if kind == "export" and step == "await_manual":
        txt = text.strip()

        # A) 一次輸入 "YYYY-MM-DD ~ YYYY-MM-DD"
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*[~\-]\s*(\d{4}-\d{2}-\d{2})", txt)
        if m:
            s, e = m.group(1), m.group(2)
            _, ledger_id = expense_service.resolve_active_ledger(ctype, cid, line_id or cid)
            base = os.getenv("PUBLIC_BASE_URL", request.host_url.rstrip("/"))
            url = f"{base}/api/ledger/{ledger_id}/expenses.csv?start={s}&end={e}"
            _reply_text(event, f"📂 區間匯出：{s} ~ {e}\n{url}", quick_reply_actions())
            return True

        # B) 只輸入一個日期 → 視為起始日，下一步等結束日
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
            expense_service.push_state(ctype, cid, line_id or cid, "export", "await_manual_end", {"start": txt})
            _reply_text(event, f"起始日：{txt}\n請再輸入結束日（YYYY-MM-DD）。")
            return True

        # 其他 → 引導重新輸入
        expense_service.push_state(ctype, cid, line_id or cid, "export", "await_manual", {})
        _reply_text(event, "格式不正確，請輸入：2025-09-01 ~ 2025-09-30，或先輸入起始日（YYYY-MM-DD）。")
        return True

    if kind == "export" and step == "await_manual_end":
        start = (payload or {}).get("start")
        end = text.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            expense_service.push_state(ctype, cid, line_id or cid, "export", "await_manual_end", {"start": start})
            _reply_text(event, "結束日格式不正確，請輸入 YYYY-MM-DD。")
            return True

        _, ledger_id = expense_service.resolve_active_ledger(ctype, cid, line_id or cid)
        base = os.getenv("PUBLIC_BASE_URL", request.host_url.rstrip("/"))
        url = f"{base}/api/ledger/{ledger_id}/expenses.csv?start={start}&end={end}"
        _reply_text(event, f"📂 區間匯出：{start} ~ {end}\n{url}", quick_reply_actions())
        return True

    # ===== 預算：等待輸入金額 =====
    if kind == "budget" and step == "await_amount":
        m = re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", text.strip())
        if not m:
            expense_service.push_state(ctype, cid, line_id or cid, "budget", "await_amount", payload)
            _reply_text(event, f"請輸入數字金額（{HOME_CCY}），例如：10000")
            return True

        amount = float(m.group(0))
        start = payload.get("start")
        end   = payload.get("end")
        if not (start and end):
            _reply_text(event, "期間遺失，請重新選擇預算期間。")
            return True

        expense_service.set_budget_total_ctx(
            ctype, cid, line_id or cid, start=start, end=end, amount=amount, currency_code=HOME_CCY
        )
        _reply_text(event, f"✅ 已設定預算：\n期間：{start} ~ {end}\n總額：{amount:,.2f} {HOME_CCY}")
        return True

    # 其它種類狀態可在這裡依需求擴充 …
    return False

def _handle_edit_mode(event, text: str) -> bool:
    ctype, cid, line_id = _resolve_context(event)
    p = expense_service.get_latest_pending_valid_ctx(ctype, cid, line_id or cid)
    if not p:
        return False

    def _preview(pd: dict) -> str:
        amt = float(pd.get("amount", 0) or 0)
        ccy = pd.get("currency_code") or HOME_CCY
        home = (f"（≈ {float(pd.get('amount_home', 0)):.2f} {HOME_CCY}）"
                if pd.get("amount_home") is not None else "")
        date_str = pd.get("spent_date") or "（未提供，預設今日）"
        cat = pd.get("category")
        cat_line = f"\n類別：{cat}" if cat else ""
        return f"項目：{pd.get('item')}\n金額：{amt:.2f} {ccy}{home}\n日期：{date_str}{cat_line}\n請確認、修改或取消。"

    # 純數字 → 改金額
    if re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", text):
        newp = _update_pending_ex_safe(
            p["id"], amount=float(text),
            item=p.get("item"), currency_code=p.get("currency_code") or HOME_CCY,
            fx_rate=p.get("fx_rate"), spent_date=p.get("spent_date"),
            category=p.get("category"), is_income=p.get("is_income"),
        ) or None
        if not newp:
            _reply_text(event, "已逾時失效，請重新輸入", quick_reply_actions()); return True
        _reply_text(event, _preview(newp), quick_reply_main(newp["id"], newp["item"], float(newp["amount"]))); return True

    # 能解析為日期 → 改日期
    d = parse_date_zh(text)
    if d:
        newp = _update_pending_ex_safe(
            p["id"], spent_date=d, item=p.get("item"),
            amount=p.get("amount"), currency_code=p.get("currency_code") or HOME_CCY,
            fx_rate=p.get("fx_rate"), category=p.get("category"), is_income=p.get("is_income"),
        ) or None
        if not newp:
            _reply_text(event, "日期修改失敗或逾時，請重新輸入", quick_reply_actions()); return True
        _reply_text(event, _preview(newp), quick_reply_main(newp["id"], newp["item"], float(newp["amount"]))); return True

    # 無數字 → 改品項（並重判類別/收入）
    if not re.search(r"\d", text):
        try:
            _, _, _, _, meta = parse_expense(text, default_currency=HOME_CCY)
            category = (meta or {}).get("category") or "其他"
            is_income = ((meta or {}).get("kind") == "income")
        except Exception:
            category, is_income = "其他", False

        newp = _update_pending_ex_safe(
            p["id"], item=text, category=category, is_income=is_income,
            amount=p.get("amount"), currency_code=p.get("currency_code") or HOME_CCY,
            amount_home=p.get("amount_home"), fx_rate=p.get("fx_rate"), spent_date=p.get("spent_date"),
        ) or None
        if not newp:
            _reply_text(event, "修改品項失敗或逾時，請重新輸入", quick_reply_actions()); return True
        _reply_text(event, _preview(newp), quick_reply_main(newp["id"], newp["item"], float(newp["amount"]))); return True

    return False

# ========= 解析並建立 pending
def _basic_parse(text: str):
    t = _normalize_text(text)
    date_val = None
    today = now_local().date()
    if t.startswith(("今天", "今日")):
        date_val, t = today, t[2:].strip()
    elif t.startswith("昨天"):
        date_val, t = today.fromordinal(today.toordinal()-1), t[2:].strip()
    elif t.startswith("前天"):
        date_val, t = today.fromordinal(today.toordinal()-2), t[2:].strip()

    m = re.match(r"^(.+?)\s+([0-9]+(?:\.[0-9]{1,2})?)\s+([A-Za-z]{3})$", t)
    if m: return m.group(1).strip(), float(m.group(2)), m.group(3).upper(), date_val
    m = re.match(r"^(.+?)\s+([0-9]+(?:\.[0-9]{1,2})?)$", t)
    if m: return m.group(1).strip(), float(m.group(2)), None, date_val
    m = re.match(r"^(.+?)([0-9]+(?:\.[0-9]{1,2})?)$", t)
    if m: return m.group(1).strip(), float(m.group(2)), None, date_val
    return None

def _handle_parse_and_store(event, text: str) -> bool:
    ctype, cid, line_id = _resolve_context(event)
    if not (ctype and cid and line_id):
        return False
    context_info = f"{ctype}:{cid}:{line_id}"

    parsed = None
    try:
        parsed = parse_expense(text, default_currency=HOME_CCY, context_info=context_info)
    except Exception as e:
        print("WARN parse_expense failed:", e)

    item = amount = ccy = date_val = None
    meta = {"kind": "expense", "category": None}

    if isinstance(parsed, (list, tuple)):
        if len(parsed) >= 5:
            item, amount, ccy, date_val, meta = parsed
        else:
            item, amount, ccy, date_val = parsed[:4]

    if (item is None) or (amount is None):
        bp = _basic_parse(text)
        if not bp:
            return False
        item, amount, ccy, date_val = bp

    if date_val is None:
        date_val = now_local().date()

    ccy = (ccy or HOME_CCY).upper()
    try:
        fx = 1.0 if ccy == HOME_CCY else (get_fx_rate(ccy, HOME_CCY, date_val) or 1.0)
    except Exception as e:
        print("WARN get_fx_rate failed:", e)
        fx = 1.0
    amount_home = round(float(amount) * float(fx), 2)

    kind = (meta or {}).get("kind") or "expense"
    category = (meta or {}).get("category") or ("薪資" if kind == "income" else "其他")
    is_income = (kind == "income")

    row = expense_service.create_pending_ex_ctx(
        context_type=ctype, context_id=cid, line_id=line_id,
        item=(item or "（未命名）")[:80], amount=float(amount),
        currency_code=ccy, fx_rate=(fx if ccy != HOME_CCY else 1.0),
        amount_home=amount_home, spent_date=str(date_val),
        note=None, category=category, is_income=is_income,
    )

    preview = (
        f"項目：{row['item']}"
        f"\n金額：{row['amount']:.2f} {row['currency_code']}（≈ {row['amount_home']:.2f} {HOME_CCY}）"
        + (f"\n日期：{row['spent_date']}" if row.get('spent_date') else "")
        + (f"\n類別：{row.get('category')}" if row.get('category') else "")
        + "\n請確認、修改或取消。"
    )
    _reply_text(event, preview, quick_reply_main(row['id'], row['item'], float(row['amount'])))
    return True

def _handle_fallback(event):
    _reply_text(event, "輸入「查詢 / 報表 / 預算」，或直接輸入『品項 金額』記帳，例如：晚餐 150。", quick_reply_actions())

# ========= 語音：下載 / Whisper 轉寫
def _download_line_audio(event) -> Optional[str]:
    message_id = event.message.id
    file_path = os.path.join("temp", f"{message_id}.m4a")
    try:
        with ApiClient(configuration) as api_client:
            blob_api = MessagingApiBlob(api_client)
            content_bytes = blob_api.get_message_content(message_id)
        with open(file_path, "wb") as f:
            if isinstance(content_bytes, (bytes, bytearray)):
                f.write(content_bytes)
            else:
                try:
                    for chunk in content_bytes:
                        if chunk: f.write(chunk)
                except Exception:
                    f.write(bytes(content_bytes))
        return file_path
    except Exception as e:
        print("[download audio error]", e)
        return None

def _transcribe_with_whisper(file_path: str) -> str:
    try:
        with open(file_path, "rb") as f:
            result = _aoai_client_for_audio.audio.transcriptions.create(
                model=AOAI_WHISPER_DEPLOYMENT, file=f, response_format="text", language="zh")
        return result or ""
    except Exception as e:
        print("[whisper error]", e)
        return ""

# ========= 路由：Webhook / 事件 =========
@handler.add(FollowEvent)
def on_follow(event: FollowEvent):
    _reply_text(event,
        "歡迎使用記帳助手！\n"
        "直接輸入「品項 金額」記帳，例如：午餐 120\n"
        "也支援幣別與日期，如：咖啡 350 JPY 8/15、昨天 晚餐 15 USD；收入：薪資/獎金/退款…\n\n" + HELP_TEXT,
        quick_reply_actions()
    )

@handler.add(MessageEvent, message=ImageMessageContent)
def on_image(event: MessageEvent):
    ocr_handler.handle_image_event(event)

@handler.add(MessageEvent, message=AudioMessageContent)
def on_audio(event: MessageEvent):
    ctype, cid, line_id = _resolve_context(event)
    if not ctype or not cid:
        _reply_text(event, "請在群組或一對一聊天使用。", quick_reply_actions()); return
    audio_path = _download_line_audio(event)
    if not audio_path:
        _reply_text(event, "下載語音檔失敗，請再試一次。", quick_reply_actions()); return
    transcript = _transcribe_with_whisper(audio_path).strip()
    if not transcript:
        _reply_text(event, "抱歉，聽不清楚。請再說一次，或改用文字輸入「品項 金額」。", quick_reply_actions()); return
    if _handle_parse_and_store(event, transcript): return
    _reply_text(event, f"我聽到：{transcript}\n請改成「品項 金額」格式，例如：晚餐 150。", quick_reply_actions())

@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event: MessageEvent):
    text = _normalize_text(event.message.text or "")
    if _handle_commands(event, text): return
    
    # 檢查是否為完整的記帳格式（品項 金額），如果是則優先處理
    if _is_complete_expense_format(text):
        if _handle_parse_and_store(event, text): return
    
    # 優先檢查是否處於編輯模式
    if _handle_edit_mode(event, text): return
    
    # 然後處理狀態化輸入（預算金額等）
    if _handle_stateful_input(event, text): return
    
    if _handle_parse_and_store(event, text): return
    _handle_fallback(event)

@handler.add(JoinEvent)
def on_join(event: JoinEvent):
    _reply_text(
        event,
        "大家好，我是記帳管家！🎉\n"
        + HELP_TEXT,
        quick_reply_actions()
    )

@handler.add(MemberJoinedEvent)
def on_member_joined(event: MemberJoinedEvent):
    _reply_text(
        event,
        "歡迎新成員加入！👋\n"
        + HELP_TEXT,
        quick_reply_actions()
    )

@handler.add(PostbackEvent)
def on_postback(event: PostbackEvent):
    ctype, cid, line_id = _resolve_context(event)
    data = event.postback.data or ""
    kv = dict(p.split("=", 1) for p in data.split("&") if "=" in p)

    act = kv.get("act")
    pid = int(kv.get("pid", "0"))
    query = kv.get("query")  # 新增：處理 Flex Message 按鈕的 query 參數

    # 關鍵：先拿到 menu_mode，避免任何分支未定義
    menu_mode = kv.get("mode", None)
    kind = kv.get("kind", None)
    
    # 處理 Flex Message 查詢按鈕
    if query:
        if query == "month":
            n = now_local()
            last_day = calendar.monthrange(n.year, n.month)[1]
            start = f"{n.year:04d}-{n.month:02d}-01"
            end   = f"{n.year:04d}-{n.month:02d}-{last_day:02d}"
            _send_snapshot(event, ctype, cid, line_id, start, end)
            return
        elif query == "date_picker":
            expense_service.push_state(ctype, cid, line_id or cid, "query", "await_start", {})
            _reply_text(event, "請選擇起始日：", _qr_pick_start("query"))
            return
        elif query == "manual":
            expense_service.push_state(ctype, cid, (line_id or cid), "query", "await_manual", {})
            _reply_text(
                event,
                "請輸入日期區間（手動）：\n"
                "格式一：2025-09-01 ~ 2025-09-30\n"
                "格式二：先輸入起始日（如 2025-09-01），再輸入結束日。",
            )
            return

    if act == "emenu":
        if menu_mode == "month":
            n = now_local()
            last_day = calendar.monthrange(n.year, n.month)[1]
            start = f"{n.year:04d}-{n.month:02d}-01"
            end   = f"{n.year:04d}-{n.month:02d}-{last_day:02d}"
            _, ledger_id = expense_service.resolve_active_ledger(ctype, cid, line_id or cid)
            base = os.getenv("PUBLIC_BASE_URL", request.host_url.rstrip("/"))
            url = f"{base}/api/ledger/{ledger_id}/expenses.csv?start={start}&end={end}"
            _reply_text(event, f"📂 本月匯出：\n{url}", quick_reply_actions()); return

        if menu_mode == "range":
            expense_service.push_state(ctype, cid, line_id or cid, "export", "await_start", {})
            _reply_text(event, "請選擇起始日：", _qr_pick_start("export")); return

        if menu_mode == "manual":  # ← 新增：手動輸入日期
            # 設定對話狀態，等待使用者輸入日期字串
            expense_service.push_state(ctype, cid, (line_id or cid), "export", "await_manual", {})
            _reply_text(
                event,
                "請輸入日期區間（手動）：\n"
                "格式一：2025-09-01 ~ 2025-09-30\n"
                "格式二：先輸入起始日（如 2025-09-01），再輸入結束日。",
            )

    # —— 群組清空確認 —— 
    if act == "gclear":
        confirm = kv.get("confirm")
        if confirm == "yes":
            _, ledger_id = expense_service.resolve_active_ledger(ctype, cid, line_id or cid)
            deleted = export_service.delete_ledger_data(ledger_id)
            _reply_text(event, f"🗑 已刪除 {deleted} 筆此群組帳本資料。", quick_reply_actions())
            return
        else:
            _reply_text(event, "已取消清空。", quick_reply_actions())
            return

    # —— 個人清空確認 —— 
    if act == "uclear":
        confirm = kv.get("confirm")
        if confirm == "yes":
            deleted = export_service.delete_user_data(line_id or cid)
            _reply_text(event, f"🗑 已刪除 {deleted} 筆個人帳本資料。", quick_reply_actions())
            return
        else:
            _reply_text(event, "已取消清空。", quick_reply_actions())
            return

    # —— 查詢菜單 —— 
    if act == "qmenu":
        if menu_mode == "month":
            n = now_local()
            last_day = calendar.monthrange(n.year, n.month)[1]
            start = f"{n.year:04d}-{n.month:02d}-01"
            end   = f"{n.year:04d}-{n.month:02d}-{last_day:02d}"
            _send_snapshot(event, ctype, cid, line_id or cid, start, end); return
        if menu_mode == "range":
            expense_service.push_state(ctype, cid, line_id or cid, "query", "await_start", {})
            _reply_text(event, "請選擇起始日：", _qr_pick_start("query")); return
        if menu_mode == "manual":  # ← 新增：手動
            expense_service.push_state(ctype, cid, line_id or cid, "query", "await_manual", {})
            _reply_text(
                event,
                "請輸入日期區間（手動）：\n"
                "格式一：2025-09-01 ~ 2025-09-30\n"
                "格式二：先輸入起始日（如 2025-09-01），再輸入結束日。"
            )
            return

    
    # —— 日曆：挑起始日 → 再跳結束日 ——
    if act == "pick_start":
        kind = kv.get("kind")  # 'query' / 'budget'
        params = getattr(event.postback, "params", {}) or {}
        start_date = params.get("date") or params.get("datetime") or params.get("time")
        if not start_date:
            _reply_text(event, "未取得起始日，請再選一次。", _qr_pick_start(kind or "query")); return
        expense_service.push_state(ctype, cid, line_id or cid, kind or "query", "await_end", {"start": start_date})
        _reply_text(event, f"起始日：{start_date}\n請選擇結束日：", _qr_pick_end(kind or "query", start_date)); return
    
    # —— 日曆：挑結束日 → 完成查詢/報表 ——
    if act == "pick_end":
        kind = kv.get("kind")
        start = kv.get("start")
        params = getattr(event.postback, "params", {}) or {}
        end_date = params.get("date") or params.get("datetime") or params.get("time")
        if not start or not end_date:
            _reply_text(event, "未取得結束日，請再選一次。", _qr_pick_end(kind or "query", start or str(now_local().date()))); return
        if (kind or "query") == "query":
            _send_snapshot(event, ctype, cid, line_id or cid, start, end_date)
        elif (kind or "query") == "export":
            _, ledger_id = expense_service.resolve_active_ledger(ctype, cid, line_id or cid)
            base = os.getenv("PUBLIC_BASE_URL", request.host_url.rstrip("/"))
            url = f"{base}/api/ledger/{ledger_id}/expenses.csv?start={start}&end={end_date}"
            _reply_text(event, f"📂 區間匯出：{start} ~ {end_date}\n{url}", quick_reply_actions())
        else:  # 預算
            expense_service.push_state(ctype, cid, line_id or cid, "budget", "await_amount", {"start": start, "end": end_date})
            _reply_text(event, f"期間：{start} ~ {end_date}\n請輸入總預算金額（{HOME_CCY}）")
        return

    # —— 預算菜單 —— 
    if act == "budget":
        if menu_mode == "status":
            _reply_text(event, expense_service.render_budget_status_ctx(ctype, cid, line_id or cid)); return
        if menu_mode == "month":
            n = now_local()
            last_day = calendar.monthrange(n.year, n.month)[1]
            start = f"{n.year:04d}-{n.month:02d}-01"
            end   = f"{n.year:04d}-{n.month:02d}-{last_day:02d}"
            expense_service.push_state(ctype, cid, line_id or cid, "budget", "await_amount", {"start": start, "end": end})
            _reply_text(event, f"請輸入本月總預算金額（{HOME_CCY}）"); return
        if menu_mode == "range":
            expense_service.push_state(ctype, cid, line_id or cid, "budget", "await_start", {"_stage": "range_budget"})
            _reply_text(event, "請選擇起始日：", _qr_pick_start("budget")); return
    # —— 取消 / 確認 pending 與編輯菜單 —— 
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)

        if act == "confirm":
            saved = expense_service.confirm_pending_ex(pid) or expense_service.confirm_pending(pid)
            if not saved:
                api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="已逾時或處理失敗，請重新輸入。", quick_reply=quick_reply_actions())]
                )); return

            # 第一則：入帳訊息
            msg1 = f"✅ 已記錄：{saved.get('item')} {saved.get('amount')}"
            if saved.get("currency_code") and saved.get("amount_home"):
                msg1 = f"✅ 已記錄：{saved.get('item')} {saved.get('amount')} {saved.get('currency_code')}（≈ {saved.get('amount_home')} {HOME_CCY}）"

            # 第二則：預算提醒（如果有的話）
            hint = expense_service.format_budget_alert_for_expense(saved)
            msgs = [TextMessage(text=msg1)]
            if hint:
                msgs.append(TextMessage(text=hint))

            api.reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=msgs))
            return

        if act == "cancel":
            ok = expense_service.cancel_pending(pid)
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="❌ 已取消暫存項目" if ok else "已逾時失效，請重新輸入", quick_reply=quick_reply_actions())]
            )); return

        if act == "edit_menu":
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="要修改哪一個？", quick_reply=quick_reply_edit_menu(pid))]
            )); return

        if act == "edit_amt":
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請直接輸入新的金額，例如：150", quick_reply=quick_reply_edit_prompt(pid))]
            )); return

        if act == "edit_item":
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請直接輸入新的品項名稱，例如：晚餐", quick_reply=quick_reply_edit_prompt(pid))]
            )); return

        if act == "edit_date":
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請選擇日期或直接輸入（如：2025-08-15、昨天）", quick_reply=quick_reply_pick_date(pid))]
            )); return

        if act == "pick_date":
            sel_date_str = None
            try:
                params = getattr(event.postback, "params", None) or {}
                sel_date_str = params.get("date") or params.get("datetime") or params.get("time")
            except Exception:
                pass
            if not sel_date_str:
                api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="日期選擇失敗，請再試一次或改用文字輸入。", quick_reply=quick_reply_actions())]
                )); return
            p = expense_service.get_latest_pending_valid_ctx(ctype, cid, line_id or cid)
            newp = _update_pending_ex_safe(
                pid, spent_date=sel_date_str,
                item=p.get("item") if p else None,
                amount=p.get("amount") if p else None,
                currency_code=(p.get("currency_code") if p else None) or HOME_CCY,
                fx_rate=p.get("fx_rate") if p else None,
                category=p.get("category") if p else None,
                is_income=p.get("is_income") if p else None,
            ) or None
            if not newp:
                api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="日期修改失敗，請再試一次。", quick_reply=quick_reply_actions())]
                )); return
            txt = (
                f"項目：{newp.get('item')}\n"
                f"金額：{float(newp.get('amount',0)):.2f} {newp.get('currency_code') or HOME_CCY}"
                f"{('（≈ %.2f %s）' % (float(newp.get('amount_home',0)), HOME_CCY)) if newp.get('amount_home') is not None else ''}\n"
                f"日期：{sel_date_str}"
                f"{(chr(10)+'類別：'+newp.get('category')) if newp.get('category') else ''}\n"
                "請確認、修改或取消。"
            )
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=txt, quick_reply=quick_reply_main(newp["id"], newp["item"], float(newp["amount"])))]
            )); return

        if act == "edit_cat":
            cats = ["餐飲","交通","住房","娛樂","健身","醫療","購物","教育","旅遊","薪資","獎金","投資","退款","其他","其他收入"]
            items = [QuickReplyItem(action=PostbackAction(label=c, data=f"act=set_cat&pid={pid}&cat={c}")) for c in cats[:11]]
            items.append(QuickReplyItem(action=PostbackAction(label="自訂/返回", data=f"act=edit_menu&pid={pid}")))
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="請選擇類別（或直接輸入文字自訂）：", quick_reply=QuickReply(items=items))]
            )); return

        if act == "set_cat":
            cat = kv.get("cat") or "其他"
            income_cats = {"薪資","獎金","投資","退款","其他收入"}
            p = expense_service.get_latest_pending_valid_ctx(ctype, cid, line_id or cid)
            newp = _update_pending_ex_safe(
                pid, category=cat, is_income=(cat in income_cats),
                item=p.get("item") if p else None,
                amount=p.get("amount") if p else None,
                currency_code=(p.get("currency_code") if p else None) or HOME_CCY,
                fx_rate=p.get("fx_rate") if p else None,
                spent_date=p.get("spent_date") if p else None,
            ) or None
            if not newp:
                _reply_text(event, "修改失敗或逾時，請重新輸入", quick_reply_actions()); return
            amt = float(newp.get("amount", 0))
            ccy = newp.get("currency_code") or HOME_CCY
            home = f"（≈ {float(newp.get('amount_home',0)):.2f} {HOME_CCY}）" if newp.get("amount_home") is not None else ""
            date_str = newp.get("spent_date") or "（未提供，預設今日）"
            preview = f"項目：{newp.get('item')}\n金額：{amt:.2f} {ccy}{home}\n日期：{date_str}\n類別：{newp.get('category') or '其他'}\n請確認、修改或取消。"
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=preview, quick_reply=quick_reply_main(newp["id"], newp["item"], amt))]
            )); return

        if act == "back":
            _reply_text(event, "已返回。可繼續修改或確認。", quick_reply_actions()); return

# ========= 匯出 API（群組/房間 CSV 連結使用）
@app.get("/api/ledger_csv")
def api_ledger_csv():
    ctype = request.args.get("ctype", "group")
    cid = request.args.get("cid")
    start = request.args.get("start")
    end = request.args.get("end")
    if not cid:
        return Response("missing cid", status=400)
    _, ledger_id = expense_service.resolve_active_ledger(ctype, cid, cid)
    data = export_service.csv_bytes_for_ledger(ledger_id, start=start, end=end)
    fname = f"ledger_{ledger_id}_{(start or 'month')}_{(end or 'month')}.csv"
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})

# ========= 舊個人 CSV 端點（相容舊連結）
@app.route("/api/ledger/<int:ledger_id>/expenses.csv", methods=["GET"])
def api_export_ledger_csv_legacy(ledger_id: int):
    year  = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    start = request.args.get("start")
    end   = request.args.get("end")
    content = export_service.csv_bytes_for_ledger(ledger_id, year=year, month=month, start=start, end=end)
    if not content:
        return Response("no data", status=404)
    filename = f"ledger_{ledger_id}_{(start or '')}_{(end or '')}".strip("_") or f"ledger_{ledger_id}"
    return Response(content, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})

@app.route("/api/me/expenses.csv", methods=["GET"])
def api_export_my_csv():
    try:
        line_user_id = request.args.get("user_id")
        if not line_user_id:
            return {"error": "missing_user"}, 400

        year  = request.args.get("year", type=int)
        month = request.args.get("month", type=int)
        start = request.args.get("start")
        end   = request.args.get("end")

        if hasattr(export_service, "handle_csv_download"):
            return export_service.handle_csv_download(line_user_id, year=year, month=month, start=start, end=end)

        # 後備：僅本月
        uid = expense_service.get_or_create_user(line_user_id)
        y, m = export_service.default_year_month()
        y = int(year or y); m = int(month or m)
        rows = export_service.export_monthly_rows(y, m, uid)
        headers = {
            "Content-Disposition": f'attachment; filename="expenses_{y:04d}_{m:02d}.csv"',
            "Content-Type": "text/csv; charset=utf-8",
            "Cache-Control": "no-store",
        }
        return app.response_class(export_service.generate_csv(rows), headers=headers)
    except Exception as e:
        app.logger.exception("Export CSV failed"); return {"error": "export_failed", "message": str(e)}, 500

# ========= 主程式 =========
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
