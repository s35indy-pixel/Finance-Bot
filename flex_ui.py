# flex_ui.py — 修正版（補 box.contents、對齊 postback 行為）
import os
from typing import List, Dict, Any, Optional
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, FlexMessage
)

# ====== 共同色票 ======
COLOR_PRIMARY = "#00B900"
COLOR_TEXT_SUB = "#6C6C6C"
COLOR_LABEL = "#A6A6A6"
COLOR_BG_SOFT = "#F5F7F9"
COLOR_BADGE = "#EEF9F0"
COLOR_BADGE_TEXT = "#167C2A"
COLOR_DIVIDER = "#E6E8EB"

# ====== 小工具 ======
def _client():
    import configparser
    config = configparser.ConfigParser()
    config.read("config.ini")
    
    # 優先使用環境變數，其次使用 config.ini
    access_token = (
        os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        or config.get("Line", "CHANNEL_ACCESS_TOKEN", fallback=None)
    )
    
    if not access_token:
        raise ValueError("LINE_CHANNEL_ACCESS_TOKEN not found in environment variables or config.ini")
    
    return ApiClient(Configuration(access_token=access_token))

def _fmt_money(v: float) -> str:
    try:
        return f"{float(v):,.2f}"
    except Exception:
        return str(v) or "0.00"

def _category_badge(text: str) -> Dict:
    return {
        "type": "box", "layout": "horizontal",
        "backgroundColor": COLOR_BADGE, "cornerRadius": "lg",
        "paddingAll": "6px",
        "contents": [
            {"type": "text", "text": str(text) if text else "—", "size": "xs", "weight": "bold", "color": COLOR_BADGE_TEXT}
        ]
    }

def _kv_row(label: str, value: str) -> Dict:
    return {
        "type": "box", "layout": "baseline", "contents": [
            {"type": "text", "text": str(label) or "—", "size": "sm", "color": COLOR_LABEL, "flex": 3},
            {"type": "text", "text": str(value) or "—", "size": "sm", "wrap": True, "flex": 7}
        ]
    }

def _divider() -> Dict:
    # 使用官方 separator，避免空 box 觸發 "At least one block must be specified"
    return {"type": "separator", "margin": "md"}

# ====== 1) 預覽卡（入帳前） ======
def build_preview_bubble(data: Dict) -> Dict:
    """
    期待欄位：
      item, amount, ccy, home_ccy, fx, spent_date, category, pending_id
    """
    amount = float(data.get("amount", 0))
    ccy = str(data.get("ccy", "TWD"))
    fx = float(data.get("fx", 1.0))
    home_ccy = str(data.get("home_ccy", "TWD"))
    subtitle = f"{_fmt_money(amount)} {ccy}（≈ {_fmt_money(amount*fx)} {home_ccy}）"
    cat = data.get("category") or "（未設定）"
    return {
      "type": "bubble",
      "size": "mega",
      "body": {
        "type": "box", "layout": "vertical", "spacing": "lg", "paddingAll": "16px",
        "contents": [
          {"type": "box", "layout": "horizontal", "justifyContent": "space-between", "contents": [
              {"type": "text", "text": "支出預覽", "weight": "bold", "size": "lg"},
              _category_badge(cat)
          ]},
          {"type": "text", "text": str(data.get("item", "（未命名）")), "size": "md", "wrap": True},
          {"type": "text", "text": subtitle, "size": "sm", "color": COLOR_TEXT_SUB},
          {"type": "box", "layout": "vertical", "spacing": "sm", "backgroundColor": COLOR_BG_SOFT,
           "cornerRadius": "lg", "paddingAll": "12px",
           "contents": [
              _kv_row("日期", data.get("spent_date") or "—"),
              _kv_row("匯率", f"1 {ccy} ≈ {fx:.4f} {home_ccy}")
           ]}
        ]
      },
      "footer": {
        "type": "box", "layout": "horizontal", "spacing": "md",
        "contents": [
          {
            "type": "button", "style": "primary", "height": "sm", "color": COLOR_PRIMARY,
            # 對齊 app.py 的 postback 行為
            "action": {"type": "postback", "label": "✅ 入帳", "data": f"act=confirm&pid={data.get('pending_id', 0)}"}
          },
          {
            "type": "button", "style": "secondary", "height": "sm",
            "action": {"type": "postback", "label": "✏️ 修改", "data": f"act=edit_menu&pid={data.get('pending_id', 0)}"}
          },
          {
            "type": "button", "style": "secondary", "height": "sm",
            "color": "#CCCCCC", "action": {"type": "postback", "label": "🗑 取消", "data": f"act=cancel&pid={data.get('pending_id', 0)}"}
          }
        ]
      }
    }

# ====== 2) 查詢列表（最近 N 筆） ======
def _record_bubble(item: Dict) -> Dict:
    """
    期待欄位：
      id, item, amount, ccy(or currency_code), date(or spent_date), category
    """
    ccy = item.get("ccy") or item.get("currency_code") or ""
    date_str = item.get("date") or item.get("spent_date") or "—"
    return {
      "type": "bubble",
      "size": "micro",
      "body": {
        "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
        "contents": [
          {"type": "text", "text": item.get("item") or "—", "weight": "bold", "size": "md", "wrap": True},
          {"type": "text", "text": f"{_fmt_money(item.get('amount', 0))} {ccy}", "size": "sm", "color": COLOR_TEXT_SUB},
          {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
              _kv_row("日期", date_str),
              _kv_row("類別", item.get("category") or "—")
          ]}
        ]
      },
      "footer": {
        "type": "box", "layout": "horizontal", "spacing": "md",
        "contents": [
          {
            "type": "button", "style": "primary", "height": "sm", "color": COLOR_PRIMARY,
            "action": {"type": "postback", "label": "✏️ 編輯", "data": f"act=edit_record&id={item.get('id')}"}
          },
          {
            "type": "button", "style": "secondary", "height": "sm",
            "action": {"type": "postback", "label": "🧾 明細", "data": f"act=detail&id={item.get('id')}"}
          }
        ]
      }
    }

def build_query_carousel(items: List[Dict]) -> Dict:
    bubbles = [_record_bubble(x) for x in (items or [])[:5]]
    if not bubbles:
        bubbles = [build_empty_bubble("沒有找到符合條件的紀錄")]
    return {"type": "carousel", "contents": bubbles}

# ====== 3) 預算卡（修正：內層 box 補 contents） ======
def build_budget_bubble(used: float, budget: float, ccy: str = "TWD") -> Dict:
    pct = 0 if budget <= 0 else min(100, round(used / budget * 100))
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "本月預算提醒", "weight": "bold", "size": "lg"},
                {"type": "text", "text": f"已用：{used:.0f} {ccy}", "size": "sm", "color": COLOR_TEXT_SUB},
                {"type": "text", "text": f"預算：{budget:.0f} {ccy}（{pct}%）", "size": "sm", "color": COLOR_TEXT_SUB},
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "box",
                            "layout": "horizontal",
                            "height": "12px",
                            "backgroundColor": "#EEEEEE",
                            "cornerRadius": "md",
                            "contents": [
                                {
                                    "type": "box",
                                    "layout": "vertical",
                                    "height": "12px",
                                    "backgroundColor": COLOR_PRIMARY,
                                    "cornerRadius": "md",
                                    "flex": max(1, pct),
                                    "contents": [ { "type": "filler" } ]  # ★ 關鍵：補 contents
                                },
                                {
                                    "type": "box",
                                    "layout": "vertical",
                                    "height": "12px",
                                    "backgroundColor": "#FFFFFF00",  # 透明
                                    "cornerRadius": "md",
                                    "flex": max(1, 100 - pct),
                                    "contents": [ { "type": "filler" } ]  # ★ 關鍵：補 contents
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }


# ====== 5) 查詢選單卡 ======
def build_query_menu_bubble() -> Dict:
    """建立查詢選單的 Flex Message"""
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "12px",
            "contents": [
                {"type": "text", "text": "📊 查詢選單", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "請選擇查詢範圍：", "size": "md"}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "action": {"type": "postback", "label": "📅 本月", "data": "query=month"}
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {"type": "postback", "label": "📆 選擇起始日", "data": "query=date_picker"}
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {"type": "postback", "label": "✏️ 手動輸入日期", "data": "query=manual"}
                }
            ]
        }
    }

# ====== 6) 查詢結果摘要卡 ======
def build_query_summary_bubble(data: Dict) -> Dict:
    """
    期待欄位：
      period: str (期間)
      total_in: float (收入總計)
      total_out: float (支出總計)
      net: float (結餘)
      top_cats: List[Tuple[str, float]] (分類支出)
      recent_items: List[Dict] (最近幾筆紀錄)
      csv_url: str (CSV下載連結)
    """
    # 建立包含分類支出的查詢結果 Flex Message
    contents = [
      {"type": "text", "text": "📖 查詢結果", "weight": "bold", "size": "lg"},
      {"type": "text", "text": f"期間：{data.get('period', '無')}", "size": "sm"},
      {"type": "text", "text": f"收入：{data.get('total_in', 0):,.2f}", "size": "sm"},
      {"type": "text", "text": f"支出：{data.get('total_out', 0):,.2f}", "size": "sm"},
      {"type": "text", "text": f"結餘：{data.get('net', 0):,.2f}", "size": "sm", "weight": "bold"}
    ]
    
    # 添加分類支出
    top_cats = data.get('top_cats', [])
    if top_cats:
      contents.append({"type": "text", "text": "分類支出：", "weight": "bold", "size": "sm"})
      for cat_name, cat_amount in top_cats:  # 顯示所有分類
        contents.append({"type": "text", "text": f"  • {cat_name}: {cat_amount:,.2f}", "size": "sm"})
    
    return {
      "type": "bubble",
      "body": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "paddingAll": "12px",
        "contents": contents
      },
      "footer": {
        "type": "box",
        "layout": "vertical",
        "spacing": "sm",
        "contents": [
          {
            "type": "button",
            "style": "primary",
            "action": {"type": "uri", "label": "📎 下載 CSV", "uri": data.get("csv_url", "")}
          }
        ]
      }
    }

# ====== 6) 說明卡 ======
def build_help_carousel() -> Dict:
    """建立使用說明的 Flex Message Carousel"""
    return {
        "type": "carousel",
        "contents": [
            # 📖 記帳方式
            {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box", 
                    "layout": "vertical", 
                    "spacing": "md",
                    "contents": [
                        {"type": "text", "text": "📖 快速記帳", "weight": "bold", "size": "lg"},
                        {"type": "text", "text": "輸入：午餐 120\n支援日期、幣別\n收入：薪資 50000", "size": "sm", "wrap": True},
                        {"type": "text", "text": "📸 上傳收據 → OCR\n🎙 語音 → 自動轉文字", "size": "sm", "wrap": True, "color": "#6C6C6C"}
                    ]
                }
            },
            
            # 📊 查詢功能
            {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box", 
                    "layout": "vertical", 
                    "spacing": "md",
                    "contents": [
                        {"type": "text", "text": "📊 查詢功能", "weight": "bold", "size": "lg"},
                        {"type": "text", "text": "顯示收入 / 支出 / 結餘\n分類支出彙總\n最近5~10筆紀錄", "size": "sm", "wrap": True},
                        {"type": "text", "text": "📎 提供 CSV 下載連結", "size": "sm", "wrap": True, "color": "#6C6C6C"}
                    ]
                }
            },
            
            # 💰 預算提醒
            {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box", 
                    "layout": "vertical", 
                    "spacing": "md",
                    "contents": [
                        {"type": "text", "text": "💰 預算功能", "weight": "bold", "size": "lg"},
                        {"type": "text", "text": "設定本月 / 自訂區間總預算\n入帳後自動提醒\n✅ 還剩 / ⚠️ 超支", "size": "sm", "wrap": True}
                    ]
                }
            },
            
            # 📂 匯出 / 清空
            {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box", 
                    "layout": "vertical", 
                    "spacing": "md",
                    "contents": [
                        {"type": "text", "text": "📂 匯出 & 清空", "weight": "bold", "size": "lg"},
                        {"type": "text", "text": "匯出 CSV：本月或自訂區間\n⚠️ 清空：刪除所有紀錄（需確認）", "size": "sm", "wrap": True}
                    ]
                }
            },
            
            # 👤 個人 vs 👥 群組
            {
                "type": "bubble",
                "size": "mega",
                "body": {
                    "type": "box", 
                    "layout": "vertical", 
                    "spacing": "md",
                    "contents": [
                        {"type": "text", "text": "👤 個人帳本 vs 👥 群組帳本", "weight": "bold", "size": "lg"},
                        {"type": "text", "text": "👤 個人：只記錄自己\n👥 群組：所有成員共用\n📊 查詢/預算 依對話類型", "size": "sm", "wrap": True}
                    ]
                }
            }
        ]
    }

def reply_help(event, messaging_api=None):
    """發送使用說明 Flex Message"""
    import requests
    import os
    
    # 直接使用 HTTP 請求，繞過 LINE Bot SDK 的序列化問題
    flex_message_dict = {
        "type": "flex",
        "altText": "使用說明",
        "contents": build_help_carousel()
    }
    
    request_body = {
        "replyToken": event.reply_token,
        "messages": [flex_message_dict]
    }
    
    # 使用 LINE API 直接發送
    # 從 app.py 獲取 access token
    import configparser
    config = configparser.ConfigParser()
    config.read("config.ini")
    
    channel_access_token = (
        os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        or config.get("Line", "CHANNEL_ACCESS_TOKEN", fallback=None)
    )
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel_access_token}"
    }
    
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=headers,
            json=request_body
        )
        if response.status_code == 200:
            print("[DEBUG] reply_help: Flex Message sent successfully via direct API")
        else:
            print(f"[ERROR] reply_help: API request failed: {response.status_code} - {response.text}")
            # 回退到純文字
            _reply_text_fallback(event, "📖 使用說明：\n1️⃣ 直接輸入「品項 金額」即可記帳，例如：午餐 120\n2️⃣ 查詢 → 顯示總計＋分類彙總＋最近5筆（附CSV下載）\n3️⃣ 預算 → 設定本月或自訂區間總預算\n4️⃣ 匯出 → 本月或自訂日期區間，產生 CSV\n5️⃣ 清空 → 個人或群組帳本資料刪除\n📸 也支援收據OCR（拍照）、語音輸入 → 自動辨識金額/品項/日期/幣別。")
    except Exception as e:
        print(f"[ERROR] reply_help: Direct API failed: {e}")
        # 回退到純文字
        _reply_text_fallback(event, "📖 使用說明：\n1️⃣ 直接輸入「品項 金額」即可記帳，例如：午餐 120\n2️⃣ 查詢 → 顯示總計＋分類彙總＋最近5筆（附CSV下載）\n3️⃣ 預算 → 設定本月或自訂區間總預算\n4️⃣ 匯出 → 本月或自訂日期區間，產生 CSV\n5️⃣ 清空 → 個人或群組帳本資料刪除\n📸 也支援收據OCR（拍照）、語音輸入 → 自動辨識金額/品項/日期/幣別。")

# ====== 7) 空狀態卡 ======
def build_empty_bubble(message: str = "目前沒有資料") -> Dict:
    return {
      "type": "bubble",
      "body": {
        "type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
        "contents": [
          {"type": "text", "text": "提示", "weight": "bold", "size": "lg"},
          {"type": "text", "text": message, "size": "md", "wrap": True, "color": COLOR_TEXT_SUB}
        ]
      }
    }

# ====== Reply helpers ======
def reply_preview(event, data: Dict):
    with _client() as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                replyToken=event.reply_token,
                messages=[FlexMessage(altText="支出預覽", contents=build_preview_bubble(data))]
            )
        )

def reply_query_list(event, items: List[Dict]):
    with _client() as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                replyToken=event.reply_token,
                messages=[FlexMessage(altText="查詢結果", contents=build_query_carousel(items))]
            )
        )

def reply_budget(event, used: float, budget: float, ccy: str = "TWD"):
    with _client() as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                replyToken=event.reply_token,
                messages=[FlexMessage(altText="本月預算提醒", contents=build_budget_bubble(used, budget, ccy))]
            )
        )


def reply_query_menu(event, messaging_api=None):
    """發送查詢選單的 Flex Message"""
    import requests
    import os
    
    # 直接使用 HTTP 請求，繞過 LINE Bot SDK 的序列化問題
    flex_message_dict = {
        "type": "flex",
        "altText": "請選擇查詢範圍",
        "contents": build_query_menu_bubble()
    }
    
    request_body = {
        "replyToken": event.reply_token,
        "messages": [flex_message_dict]
    }
    
    # 使用 LINE API 直接發送
    # 從 app.py 獲取 access token
    import configparser
    config = configparser.ConfigParser()
    config.read("config.ini")
    
    channel_access_token = (
        os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        or config.get("Line", "CHANNEL_ACCESS_TOKEN", fallback=None)
    )
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel_access_token}"
    }
    
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=headers,
            json=request_body
        )
        if response.status_code == 200:
            print("[DEBUG] reply_query_menu: Flex Message sent successfully via direct API")
        else:
            print(f"[ERROR] reply_query_menu: API request failed: {response.status_code} - {response.text}")
            # 回退到純文字
            _reply_text_fallback(event, "請選擇查詢範圍：\n📅 本月\n📆 選擇起始日\n✏️ 手動輸入日期")
    except Exception as e:
        print(f"[ERROR] reply_query_menu: Direct API failed: {e}")
        # 回退到純文字
        _reply_text_fallback(event, "請選擇查詢範圍：\n📅 本月\n📆 選擇起始日\n✏️ 手動輸入日期")

def _reply_text_fallback(event, text):
    """純文字回退函數"""
    from linebot.v3.messaging import ReplyMessageRequest, TextMessage
    try:
        with _client() as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    replyToken=event.reply_token,
                    messages=[TextMessage(text=text)]
                )
            )
    except Exception as e:
        print(f"[ERROR] _reply_text_fallback failed: {e}")

def reply_query_summary(event, data: Dict, messaging_api=None):
    import requests
    import os
    
    # 直接使用 HTTP 請求，繞過 LINE Bot SDK 的序列化問題
    flex_message_dict = {
        "type": "flex",
        "altText": "查詢結果",
        "contents": build_query_summary_bubble(data)
    }
    
    request_body = {
        "replyToken": event.reply_token,
        "messages": [flex_message_dict]
    }
    
    # 使用 LINE API 直接發送
    # 從 app.py 獲取 access token
    import configparser
    config = configparser.ConfigParser()
    config.read("config.ini")
    
    channel_access_token = (
        os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
        or config.get("Line", "CHANNEL_ACCESS_TOKEN", fallback=None)
    )
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel_access_token}"
    }
    
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=headers,
            json=request_body
        )
        if response.status_code == 200:
            print("[DEBUG] reply_query_summary: Flex Message sent successfully via direct API")
        else:
            print(f"[ERROR] reply_query_summary: API request failed: {response.status_code} - {response.text}")
            # 回退到純文字
            _reply_text_fallback(event, f"查詢結果：\n期間：{data.get('period', '無')}\n收入：{data.get('total_in', 0):,.2f}\n支出：{data.get('total_out', 0):,.2f}\n結餘：{data.get('net', 0):,.2f}")
    except Exception as e:
        print(f"[ERROR] reply_query_summary: Direct API failed: {e}")
        # 回退到純文字
        _reply_text_fallback(event, f"查詢結果：\n期間：{data.get('period', '無')}\n收入：{data.get('total_in', 0):,.2f}\n支出：{data.get('total_out', 0):,.2f}\n結餘：{data.get('net', 0):,.2f}")

def reply_empty(event, message: str = "目前沒有資料"):
    with _client() as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                replyToken=event.reply_token,
                messages=[FlexMessage(altText="提示", contents=build_empty_bubble(message))]
            )
        )
