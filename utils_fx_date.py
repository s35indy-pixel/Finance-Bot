# utils_fx_date.py
# 功能：多幣別/日期解析 + 匯率（含快取、歷史匯率 fallback、Apilayer/Frankfurter/ER-API）
from __future__ import annotations

import os
import re
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

try:
    import pytz  # 可選；若沒裝也能運作
except Exception:
    pytz = None

# ========= 由 init_from_config() 或環境變數初始化 =========
HOME_CCY      = os.getenv("HOME_CCY", "TWD").upper()
FX_PROVIDER   = os.getenv("FX_PROVIDER", "exchangerate_host").lower()
FX_ACCESS_KEY = os.getenv("FX_ACCESS_KEY", "").strip()
FX_CACHE_FILE = os.getenv("FX_CACHE_FILE", "fx_cache.json")
LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "Asia/Taipei")
LOCAL_TZ      = pytz.timezone(LOCAL_TZ_NAME) if (pytz is not None) else None

def init_from_config(config) -> None:
    """在 app.py 讀完 config.ini 後呼叫這個，統一初始化設定"""
    global HOME_CCY, FX_PROVIDER, FX_ACCESS_KEY, FX_CACHE_FILE, LOCAL_TZ_NAME, LOCAL_TZ
    if config and "FX" in config:
        HOME_CCY      = config["FX"].get("HOME_CURRENCY", HOME_CCY).upper()
        FX_PROVIDER   = config["FX"].get("PROVIDER", FX_PROVIDER).lower()
        FX_ACCESS_KEY = config["FX"].get("ACCESS_KEY", FX_ACCESS_KEY).strip()
        FX_CACHE_FILE = config["FX"].get("CACHE_FILE", FX_CACHE_FILE)
    if config and "MySQL" in config:
        LOCAL_TZ_NAME = config["MySQL"].get("TZ", LOCAL_TZ_NAME)

    if pytz:
        try:
            LOCAL_TZ = pytz.timezone(LOCAL_TZ_NAME)
        except Exception:
            LOCAL_TZ = pytz.timezone("Asia/Taipei")

    # 提供給可能用到的模組（例如 ocr_handler）
    os.environ["HOME_CCY"] = HOME_CCY

# ========= 文字解析：日期 / 幣別 / 金額 =========
CURRENCY_SYMS = {
    "NT$": "TWD", "TWD": "TWD", "NTD": "TWD", "$": "TWD", "＄": "TWD",
    "USD": "USD", "US$": "USD", "ＵＳＤ": "USD",
    "JPY": "JPY", "￥": "JPY", "¥": "JPY",
    "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP",
    "KRW": "KRW", "₩": "KRW", "HKD": "HKD", "AUD": "AUD",
    "CAD": "CAD", "SGD": "SGD",
}
DATE_WORDS = {"今天": 0, "今日": 0, "昨天": -1, "前天": -2}
WEEK_MAP   = {"一":0,"二":1,"三":2,"四":3,"五":4,"六":5,"日":6,"天":6}

def now_local() -> datetime:
    if LOCAL_TZ and pytz:
        return datetime.now(LOCAL_TZ)
    return datetime.now()

def parse_date_zh(s: str) -> str | None:
    """支援：今天/昨天/前天、週五/星期五/禮拜五、YYYY-MM-DD、YYYY/MM/DD、M/D"""
    s = (s or "").strip()

    if s in DATE_WORDS:
        d = now_local().date() + timedelta(days=DATE_WORDS[s])
        return d.strftime("%Y-%m-%d")

    m = re.search(r"(?:週|星期|禮拜)\s*([一二三四五六日天])", s)
    if m:
        target = WEEK_MAP[m.group(1)]
        today_w = now_local().weekday()  # 0=Mon
        delta = (today_w - target) if today_w >= target else (today_w - target + 7)
        d = now_local().date() - timedelta(days=delta)
        return d.strftime("%Y-%m-%d")

    m = re.search(r"(20\d{2})[\/\-\.](\d{1,2})[\/\-\.](\d{1,2})", s)
    if m:
        y, mn, d = map(int, m.groups())
        return f"{y:04d}-{mn:02d}-{d:02d}"

    m = re.search(r"\b(\d{1,2})[\/\-\.](\d{1,2})\b", s)
    if m:
        y = now_local().year
        mn, d = map(int, m.groups())
        return f"{y:04d}-{mn:02d}-{d:02d}"

    return None

def detect_currency(text: str) -> str | None:
    up = (text or "").upper()
    for code in {"TWD","USD","JPY","EUR","GBP","KRW","HKD","AUD","CAD","SGD"}:
        if re.search(rf"\b{code}\b", up):
            return code
    for sym, code in CURRENCY_SYMS.items():
        if sym in text:
            return code
    return None

def parse_amount_currency_and_date(text: str):
    """
    從句子抽 item/amount/currency/date：
      例：'昨天 咖啡 120 JPY'、'8/15 晚餐 15 USD'
    """
    text = (text or "").strip()

    m_amt = re.search(r"([1-9]\d{0,2}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)", text)
    amount = float(m_amt.group(1).replace(",", "")) if m_amt else None

    ccy = detect_currency(text) or HOME_CCY

    date = parse_date_zh(text)
    if not date:
        for tok in re.split(r"\s+", text):
            date = parse_date_zh(tok)
            if date:
                break

    cleaned = text
    if m_amt: cleaned = cleaned.replace(m_amt.group(1), " ")
    for token in {ccy} | set(CURRENCY_SYMS.keys()) | set(DATE_WORDS.keys()):
        cleaned = cleaned.replace(token, " ")
    cleaned = re.sub(r"(20\d{2}[\/\-\.]\d{1,2}[\/\-\.]\d{1,2})", " ", cleaned)
    cleaned = re.sub(r"\b\d{1,2}[\/\-\.]\d{1,2}\b", " ", cleaned)
    item = re.sub(r"\s+", " ", cleaned).strip()

    return item or None, amount, ccy, date

# ========= 匯率：多供應商 + 快取 =========
def _cache_put(key: str, rate: float) -> None:
    try:
        cache = {}
        if os.path.exists(FX_CACHE_FILE):
            with open(FX_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
        cache[key] = {"rate": rate, "ts": datetime.utcnow().isoformat()}
        with open(FX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _cache_get(key: str) -> float | None:
    try:
        with open(FX_CACHE_FILE, "r", encoding="utf-8") as f:
            return float(json.load(f)[key]["rate"])
    except Exception:
        return None

def _rate_from_quotes(quotes: dict, base_ccy: str, home_ccy: str) -> float:
    """exchangerate_host / currencylayer 的 quotes 皆以 USD 為基準。"""
    base_ccy = base_ccy.upper()
    home_ccy = home_ccy.upper()
    if base_ccy == home_ccy:
        return 1.0
    # 若 base=USD，直接取 USD→home
    if base_ccy == "USD":
        return float(quotes[f"USD{home_ccy}"])
    # 若 home=USD，回傳 1 / (USD→base)
    if home_ccy == "USD":
        return 1.0 / float(quotes[f"USD{base_ccy}"])
    # 交叉：home/base = (USD→home) / (USD→base)
    usd_home = float(quotes[f"USD{home_ccy}"])
    usd_base = float(quotes[f"USD{base_ccy}"])
    return usd_home / usd_base

def get_fx_rate(base_ccy: str, home_ccy: str, date_str: str | None = None) -> float | None:
    """
    回傳 base_ccy→home_ccy 匯率。
    - exchangerate_host（Apilayer）：/historical 若失敗自動退回 /live
    - currencylayer（Apilayer）：/live，USD 基準，用交叉換算（免費層 HTTP）
    - frankfurter（ECB）：直接 from/to；歷史以 /YYYY-MM-DD
    - erapi（open.er-api.com）：latest，不保證歷史
    """
    if not base_ccy or not home_ccy:
        return None
    base_ccy = base_ccy.upper()
    home_ccy = home_ccy.upper()
    if base_ccy == home_ccy:
        return 1.0

    cache_key = f"{base_ccy}_{home_ccy}_{date_str or 'live'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        prov = FX_PROVIDER.lower()

        if prov == "exchangerate_host":
            # Apilayer：歷史請求常被免費層限制 → 歷史失敗就退回 live
            def _live_url():
                return "https://api.exchangerate.host/live?" + urllib.parse.urlencode({
                    "access_key": FX_ACCESS_KEY,
                    "currencies": f"{home_ccy},{base_ccy}",
                    "format": 1
                })
            def _hist_url(d: str):
                return "https://api.exchangerate.host/historical?" + urllib.parse.urlencode({
                    "access_key": FX_ACCESS_KEY,
                    "date": d,
                    "currencies": f"{home_ccy},{base_ccy}",
                    "format": 1
                })

            url = _hist_url(date_str) if date_str else _live_url()

            with urllib.request.urlopen(url, timeout=6) as resp:
                data = json.load(resp)

            if not data.get("success"):
                # 歷史被擋或其他錯誤 → 若本來是歷史就退回 live 再試一次
                if date_str:
                    with urllib.request.urlopen(_live_url(), timeout=6) as resp2:
                        data2 = json.load(resp2)
                    if not data2.get("success"):
                        raise RuntimeError(data.get("error", {}).get("info", "fx error"))
                    rate = _rate_from_quotes(data2["quotes"], base_ccy, home_ccy)
                else:
                    raise RuntimeError(data.get("error", {}).get("info", "fx error"))
            else:
                rate = _rate_from_quotes(data["quotes"], base_ccy, home_ccy)

        elif prov == "currencylayer":
            base_url = "http://api.currencylayer.com/live"
            q = urllib.parse.urlencode({
                "access_key": FX_ACCESS_KEY,
                "currencies": f"{home_ccy},{base_ccy}",
                "format": 1
            })
            with urllib.request.urlopen(f"{base_url}?{q}", timeout=6) as resp:
                data = json.load(resp)
            if not data.get("success"):
                raise RuntimeError(data.get("error", {}).get("info", "currencylayer error"))
            rate = _rate_from_quotes(data["quotes"], base_ccy, home_ccy)

        elif prov == "frankfurter":
            # 免金鑰；支持歷史
            if date_str:
                url = f"https://api.frankfurter.app/{date_str}?from={base_ccy}&to={home_ccy}"
            else:
                url = f"https://api.frankfurter.app/latest?from={base_ccy}&to={home_ccy}"
            with urllib.request.urlopen(url, timeout=6) as resp:
                data = json.load(resp)
            rate = float(data["rates"][home_ccy])

        elif prov in ("erapi", "open_er_api"):
            # 免金鑰；只保證 latest
            url = f"https://open.er-api.com/v6/latest/{base_ccy}"
            with urllib.request.urlopen(url, timeout=6) as resp:
                data = json.load(resp)
            if str(data.get("result", "")).lower() != "success":
                raise RuntimeError("erapi error")
            rate = float(data["rates"][home_ccy])

        else:
            raise RuntimeError(f"Unknown FX provider: {FX_PROVIDER}")

        _cache_put(cache_key, rate)
        return rate

    except Exception:
        # 若 API 失敗，嘗試用快取救援
        return _cache_get(cache_key)
