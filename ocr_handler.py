# ocr_handler.py — Azure OpenAI Vision OCR + 類別/收入判斷 → 建立 pending（含 ledger 支援）
import os
import json
import base64
import re
from typing import Optional, Dict, Any, Tuple
from ai_parser import parse_expense

from linebot.v3.webhooks import MessageEvent
from linebot.v3.messaging import (
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    QuickReply,
    QuickReplyItem,
    PostbackAction,
)
from openai import AzureOpenAI

from utils_fx_date import get_fx_rate, HOME_CCY
import expense_service


class OCRHandler:
    def __init__(self, configuration):
        self.configuration = configuration
        os.makedirs("temp", exist_ok=True)

        # 直接讀取 config.ini，不依賴環境變數
        import configparser
        config = configparser.ConfigParser()
        config.read("config.ini")
        
        self.aoai_endpoint = config["AzureOpenAI"].get("END_POINT", "")
        self.aoai_key = config["AzureOpenAI"].get("API_KEY", "")
        self.aoai_api_version = config["AzureOpenAI"].get("API_VERSION", "2024-08-01-preview")
        self.aoai_vision_deploy = config["AzureOpenAI"].get("VISION_DEPLOYMENT", "gpt-4o-mini")

        if not self.aoai_endpoint or not self.aoai_key:
            raise ValueError("Azure OpenAI 設定不完整，請檢查 config.ini")

        self._aoai_client = AzureOpenAI(
            api_key=self.aoai_key,
            api_version=self.aoai_api_version,
            azure_endpoint=self.aoai_endpoint,
        )

    # ===== QuickReply（與文字流程一致） =====
    def _qr_main(self, pid: int) -> QuickReply:
        return QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label="✅ 確認", data=f"act=confirm&pid={pid}")),
            QuickReplyItem(action=PostbackAction(label="✏️ 修改", data=f"act=edit_menu&pid={pid}")),
            QuickReplyItem(action=PostbackAction(label="❌ 取消", data=f"act=cancel&pid={pid}")),
        ])

    def _reply_text(self, event, text, quick_reply=None):
        with ApiClient(self.configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=text, quick_reply=quick_reply)],
                )
            )

    # ========= 來源解析（user / group / room）=========
    def _resolve_context(self, event) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        回傳 (context_type, context_id, line_id)
        - user: 個人聊天，context_id = user_id
        - group: 群組，context_id = group_id
        - room: 多人房，context_id = room_id
        - 有些群組拿不到 user_id → 以 context_id 退回當作 line_id（用於建立成員/預設識別）
        """
        src = getattr(event, "source", None)
        if not src:
            return None, None, None
        group_id = getattr(src, "group_id", None)
        room_id = getattr(src, "room_id", None)
        user_id = getattr(src, "user_id", None)

        if group_id:
            return "group", group_id, (user_id or group_id)
        if room_id:
            return "room", room_id, (user_id or room_id)
        if user_id:
            return "user", user_id, user_id
        return None, None, None

    # ========= 下載 LINE 圖片 =========
    def _save_line_image(self, event) -> Optional[str]:
        message_id = event.message.id
        file_path = os.path.join("temp", f"{message_id}.jpg")
        try:
            with ApiClient(self.configuration) as api_client:
                blob_api = MessagingApiBlob(api_client)
                content_bytes = blob_api.get_message_content(message_id)
            with open(file_path, "wb") as f:
                if isinstance(content_bytes, (bytes, bytearray)):
                    f.write(content_bytes)
                else:
                    try:
                        for chunk in content_bytes:
                            if chunk:
                                f.write(chunk)
                    except Exception:
                        f.write(bytes(content_bytes))
            return file_path
        except Exception as e:
            print(f"[download image error] {e}")
            return None

    # ========= Azure OpenAI Vision：OCR/理解（取 item/amount/currency/date）=========
    def _vision_extract(self, image_path: str) -> Dict[str, Any]:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")

        system_prompt = (
            "You are a receipt parser for a personal bookkeeping bot. "
            "Extract: item (short), amount (number), currency_code (3-letter ISO like TWD/USD/JPY if determinable), "
            "date (YYYY-MM-DD), and full_text (raw OCR text). "
            "If multiple amounts, pick the final payable/total. "
            "Respond ONLY JSON with keys: item, amount, currency_code, date, full_text."
        )
        user_instruction = (
            "Read the receipt image and return strict JSON: "
            "{\"item\": string, \"amount\": number|string, \"currency_code\": string|null, "
            "\"date\": \"YYYY-MM-DD\"|null, \"full_text\": string}"
        )

        resp = self._aoai_client.chat.completions.create(
            model=self.aoai_vision_deploy,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_instruction},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                },
            ],
        )

        content = resp.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except Exception:
            data = {}

        # 正規化
        item = (data.get("item") or "").strip() or None
        amount = data.get("amount")
        try:
            amount = float(str(amount).replace(",", "")) if amount not in (None, "") else None
        except Exception:
            amount = None
        date = (data.get("date") or "").strip() or None
        currency_code = (data.get("currency_code") or "").upper().strip() or None
        if currency_code and len(currency_code) != 3:
            currency_code = None

        full_text = data.get("full_text") or ""
        return {"item": item, "amount": amount, "currency_code": currency_code, "date": date, "full_text": full_text}

    # ========= 依文字自動判斷 收入/支出 與 類別 =========
    def _guess_income_and_category(self, item: str, ocr_text: str) -> Tuple[bool, str]:
        text_all = f"{item or ''} {ocr_text or ''}"

        # 收入關鍵字
        if re.search(r"薪|salary", text_all, re.IGNORECASE):
            return True, "薪資"
        if re.search(r"獎|bonus", text_all, re.IGNORECASE):
            return True, "獎金"
        if re.search(r"投資|配息|股利|利息", text_all, re.IGNORECASE):
            return True, "投資"
        if re.search(r"退款|退費|退稅|報銷|reimbursement|refund", text_all, re.IGNORECASE):
            return True, "退款"
        if re.search(r"收入|入帳|入賬", text_all, re.IGNORECASE):
            return True, "其他收入"

        # 支出類別（常見關鍵字）
        if re.search(r"早餐|午餐|晚餐|宵夜|餐|咖啡|飲料|便當|food|meal|cafe|coffee", text_all, re.IGNORECASE):
            return False, "餐飲"
        if re.search(r"捷運|公車|客運|計程車|高鐵|火車|加油|停車|uber|taxi|油|交通|bus|mrt", text_all, re.IGNORECASE):
            return False, "交通"
        if re.search(r"房租|租金|房貸|水電|瓦斯|網路|管理費|住宿|hotel|airbnb", text_all, re.IGNORECASE):
            return False, "住房"
        if re.search(r"電影|演唱會|遊戲|遊樂|娛樂|netflix|disney", text_all, re.IGNORECASE):
            return False, "娛樂"
        if re.search(r"健身|運動|瑜珈|gym", text_all, re.IGNORECASE):
            return False, "健身"
        if re.search(r"醫療|看診|藥|藥局|牙醫|健保|掛號|clinic|hospital|pharmacy", text_all, re.IGNORECASE):
            return False, "醫療"
        if re.search(r"衣|鞋|包|家電|購|買|shopping|amazon|momo|蝦皮", text_all, re.IGNORECASE):
            return False, "購物"
        if re.search(r"學費|補習|課程|教材|書|教育|tuition|course", text_all, re.IGNORECASE):
            return False, "教育"
        if re.search(r"旅館|機票|訂房|旅行|旅遊|tour|ticket|flight", text_all, re.IGNORECASE):
            return False, "旅遊"

        return False, "其他"

    # ========= 對外：由 app.py 呼叫 =========
    def handle_image_event(self, event: MessageEvent):
        ctype, cid, line_id = self._resolve_context(event)
        if not ctype:
            self._reply_text(event, "無法辨識訊息來源，請在群組或私聊中使用。")
            return

        file_path = self._save_line_image(event)
        if not file_path:
            self._reply_text(event, "抱歉，下載圖片時發生錯誤，請再試一次。")
            return

        try:
            parsed = self._vision_extract(file_path)
        except Exception as e:
            print(f"[Vision error] {e}")
            self._reply_text(event, "辨識失敗，請拍更清楚或補光後再試一次。")
            return

        if not (parsed.get("item") or parsed.get("amount")):
            self._reply_text(event, "沒有擷取到有效資訊，請重新上傳清晰的收據。")
            return

        item_str = parsed.get("item") or "（未命名）"
        amount_val = parsed.get("amount")
        date_str = parsed.get("date")
        ccy = parsed.get("currency_code") or HOME_CCY

        # ===== 決定 收入/支出 + 類別（先用 OpenAI，結果太弱再用關鍵字覆蓋） =====
        try:
            # 給 LLM 更多上下文（品項 + 金額 + 幣別）
            llm_text = f"{item_str} {amount_val or ''} {parsed.get('currency_code') or ''}".strip()
            _, _, _, _, meta = parse_expense(llm_text, default_currency=HOME_CCY)
            category = (meta or {}).get("category")
            is_income = ((meta or {}).get("kind") == "income")
        except Exception:
            category = None
            is_income = None

        # 若 LLM 沒分出來或只給「其他/未分類」，就用規則再判一次
        if not category or category in ("其他", "未分類", "Other", "Misc"):
            inc2, cat2 = self._guess_income_and_category(item_str, parsed.get("full_text", ""))
            # 關鍵字規則抓到更明確的，就覆蓋
            if cat2 and cat2 not in ("其他", "未分類"):
                category = cat2
                is_income = inc2

        # 最後保底
        if category is None:
            category = "其他"
        if is_income is None:
            is_income = (category in ("薪資", "獎金", "投資", "退款", "其他收入"))


        # 匯率與本幣金額
        fx = get_fx_rate(ccy, HOME_CCY, date_str) if amount_val is not None else None
        if fx is None and ccy == HOME_CCY:
            fx = 1.0
        amount_home = round(amount_val * fx, 2) if (amount_val is not None and fx is not None) else None

        # 建立 pending（與文字/語音流程一致；多帶 category / is_income；含 ledger）
        try:
            row = expense_service.create_pending_ex_ctx(
                context_type=ctype,
                context_id=cid,
                line_id=line_id,
                item=item_str,
                amount=float(amount_val or 0),
                currency_code=ccy,
                fx_rate=fx if fx is not None else (1.0 if ccy == HOME_CCY else None),
                amount_home=amount_home,
                spent_date=date_str,
                note=None,
                category=category,        # ★ 新增
                is_income=is_income,      # ★ 新增
            )
        except Exception as e:
            print(f"[create pending ex ctx error] {e}")
            self._reply_text(event, "暫存失敗，請稍後再試或改用文字輸入。")
            return

        # 回覆統一版 QuickReply（Postback）
        amt_part = f"\n金額：{row['amount']:.2f} {row.get('currency_code') or HOME_CCY}"
        home_part = f"（≈ {row['amount_home']:.2f} {HOME_CCY}）" if row.get("amount_home") is not None else ""
        date_part = f"\n日期：{row.get('spent_date')}" if row.get("spent_date") else ""
        cat_part = f"\n類別：{row.get('category')}" if row.get('category') else ""
        preview = f"項目：{row.get('item')}{amt_part}{home_part}{date_part}{cat_part}\n請確認、修改或取消。"
        self._reply_text(event, preview, quick_reply=self._qr_main(row["id"]))

    # 文字事件不需攔截，統一走 app.py 的文字邏輯
    def should_handle_text(self, user_id: str, text: str) -> bool:
        return False

    def handle_text_event(self, event: MessageEvent):
        return
