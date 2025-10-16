# ai_parser.py —— 穩定 JSON + 幣別正規化 + 詳細除錯
from __future__ import annotations
import os, json, re, configparser
from datetime import datetime, date
from typing import Tuple, Optional, Dict, Any
from openai import AzureOpenAI

_CLIENT: AzureOpenAI | None = None
_TEXT_MODEL: str | None = None

# ===== 幣別映射：中文/俗稱/符號 → ISO 4217 =====
_CCY_MAP = {
    "台幣":"TWD","臺幣":"TWD","新台幣":"TWD","新臺幣":"TWD","nt":"TWD","nt$":"TWD","ntd":"TWD","twd":"TWD","$":"TWD","＄":"TWD",
    "日幣":"JPY","日元":"JPY","日圓":"JPY","円":"JPY","¥":"JPY","jpy":"JPY","yen":"JPY",
    "美金":"USD","美元":"USD","usd":"USD","us$":"USD",
    "人民幣":"CNY","rmb":"CNY","cny":"CNY",
    "港幣":"HKD","hkd":"HKD",
    "歐元":"EUR","eur":"EUR","€":"EUR",
    "韓元":"KRW","韓幣":"KRW","krw":"KRW","₩":"KRW",
    "新幣":"SGD","新加坡幣":"SGD","sgd":"SGD",
    "英鎊":"GBP","gbp":"GBP","£":"GBP",
}

# ===== 讀設定（優先環境變數，否則 config.ini）=====
def _load_settings_from_config() -> tuple[str, str, str, str]:
    ep = os.environ.get("AOAI_ENDPOINT")
    key = os.environ.get("AOAI_KEY")
    ver = os.environ.get("AOAI_API_VERSION")
    dep = os.environ.get("AOAI_TEXT_DEPLOYMENT")
    if ep and key and ver:
        return ep, key, ver, (dep or "gpt-4o-mini")
    cfg = configparser.ConfigParser(); cfg.read("config.ini")
    sec = cfg["AzureOpenAI"]
    ep = sec.get("END_POINT","").strip()
    key = sec.get("API_KEY","").strip()
    ver = sec.get("API_VERSION","2024-08-01-preview").strip()
    dep = sec.get("TEXT_DEPLOYMENT", sec.get("VISION_DEPLOYMENT","gpt-4o-mini")).strip() or "gpt-4o-mini"
    if not ep or not key:
        raise RuntimeError("Azure OpenAI END_POINT 或 API_KEY 未設定")
    return ep, key, ver, dep

def _ensure_client() -> tuple[AzureOpenAI, str]:
    global _CLIENT, _TEXT_MODEL
    if _CLIENT and _TEXT_MODEL: return _CLIENT, _TEXT_MODEL
    ep, key, ver, dep = _load_settings_from_config()
    _CLIENT = AzureOpenAI(api_key=key, api_version=ver, azure_endpoint=ep)
    _TEXT_MODEL = dep
    return _CLIENT, _TEXT_MODEL

# ===== 小工具 =====
def _first_json_blob(s: str) -> Optional[str]:
    m = re.search(r"\{.*\}", s, flags=re.S)
    return m.group(0) if m else None

def _parse_date_iso(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s: return None
    try:
        s = s.replace("/", "-")
        return datetime.fromisoformat(s).date()
    except Exception:
        return None

def _norm_ccy(s: Optional[str]) -> Optional[str]:
    if not s: return None
    t = s.strip().lower().replace(" ", "")
    if t in _CCY_MAP: return _CCY_MAP[t]
    if re.fullmatch(r"[a-z]{3}", t): return t.upper()
    return s.upper()

# ===== FEW-SHOTS（小樣例，幫助模型穩定結構）=====
FEWSHOTS = [
    {"role":"user","content":"薪資 50000"},
    {"role":"assistant","content":'{"item":"薪資","amount":50000,"currency":"TWD","date":"","kind":"income","category":"薪資"}'},
    {"role":"user","content":"報銷 850"},
    {"role":"assistant","content":'{"item":"報銷","amount":850,"currency":"TWD","date":"","kind":"income","category":"其他收入"}'},
    {"role":"user","content":"晚餐 120"},
    {"role":"assistant","content":'{"item":"晚餐","amount":120,"currency":"TWD","date":"","kind":"expense","category":"餐飲"}'},
]

# ===== 主要 API =====
def parse_expense(
    text: str,
    *,
    default_currency: Optional[str] = None,
    context_info: Optional[str] = None  # 新增：用於調試
) -> Tuple[Optional[str], Optional[float], Optional[str], Optional[date], Dict[str, Any]]:
    """
    回傳 (item, amount, currency(ISO), date, meta)；失敗回 (None, None, None, None, {"kind":"expense","category":"其他"})
    meta: {"kind": "income"|"expense", "category": <str>}
    - 設定 PRINT_AI_PARSE=1 可在 console 看到模型呼叫與回覆
    - 設定 PRINT_AI_PARSE=2 在例外時 raise 方便除錯
    """
    client, model = _ensure_client()
    dbg = os.environ.get("PRINT_AI_PARSE","0")
    if dbg in ("1", "2"):
        print(f"[ai_parser] Context: {context_info}, Text: {text}")
    SYSTEM = (
        "You are an expense parser for a LINE bookkeeping bot. "
        "Return STRICT JSON with keys: item(string), amount(number), currency(string ISO 4217 or empty), "
        "date(YYYY-MM-DD or empty), kind(string: 'income'|'expense'), category(string). "
        "Map 台幣/新台幣/NT→TWD, 日幣/日元/円→JPY, 美金/USD→USD. "
        "If no date/currency, use empty string. No extra text. "
        "Decide 'kind' from text semantics: 薪資/收入/獎金/退款/報銷 = income；其餘多半為 expense. "
        "Choose category from a small, human-friendly set:\n"
        "Income: ['薪資','獎金','投資','退款','其他收入']\n"
        "Expense: ['餐飲','交通','住房','娛樂','健身','醫療','購物','教育','旅遊','其他']"
    )

    messages = [{"role":"system","content":SYSTEM}] + FEWSHOTS + [{"role":"user","content":text}]

    try:
        if dbg in ("1","2"):
            print(f"[ai_parser] call AzureOpenAI model={model} text={text}")
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},  # 強制 JSON
            messages=messages,
        )
        raw = resp.choices[0].message.content if resp and resp.choices else ""
        if dbg in ("1","2"): print("[ai_parser] raw:", raw)

        blob = _first_json_blob(raw or "") or raw or "{}"
        data = json.loads(blob)

        # ---- 主要欄位 ----
        item = (data.get("item") or "").strip() or None
        amount = None
        if data.get("amount") not in (None, ""):
            try:
                amount = float(str(data["amount"]).replace(",", ""))
            except Exception:
                amount = None

        ccy = _norm_ccy(data.get("currency"))
        if (not ccy) and default_currency:
            ccy = default_currency
        dt = _parse_date_iso(data.get("date") or "")

        # ---- kind / category 後備規則 ----
        kind = (data.get("kind") or "").strip().lower()
        if kind not in ("income","expense"):
            kw_income = ("薪資","收入","獎金","bonus","salary","退款","退稅","報銷","reimbursement")
            text_all = " ".join([text or "", item or ""])
            kind = "income" if any(k in text_all for k in kw_income) else "expense"

        category = (data.get("category") or "").strip()
        if not category:
            category = "薪資" if kind == "income" else "其他"

        meta = {"kind": kind, "category": category}

        if dbg in ("1","2"):
            print("[ai_parser] parsed:", {"item": item, "amount": amount, "currency": ccy, "date": dt, **meta})

        if amount is None:
            return None, None, None, None, {"kind": "expense", "category": "其他"}

        return item, amount, ccy, dt, meta

    except Exception as e:
        if dbg in ("1","2"): print("[ai_parser] error:", repr(e))
        if dbg == "2": raise
        return None, None, None, None, {"kind": "expense", "category": "其他"}
