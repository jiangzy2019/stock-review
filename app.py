#!/usr/bin/env python3
"""
股票复盘 Web 应用 — 部署版
支持本地 Obsidian 知识库和云端 JSON 双模式。
"""

import os, re, json, datetime, sys, html
from pathlib import Path
from collections import OrderedDict

import requests
import yfinance as yf
from flask import Flask, render_template, jsonify, request

BASE = Path(__file__).resolve().parent
TEMPLATES = BASE / "templates"

# ── 本地知识库 / 云端回退 ──
VAULT = Path(os.environ.get("OBSIDIAN_VAULT", "/Users/andy/Documents/industry-research"))
TRENDS = VAULT / "04-Trends"
BUNDLED = TEMPLATES / "reviews_bundle.json"
STORAGE = BASE / "reviews_store.json"  # newly created reviews go here

app = Flask(__name__, template_folder=str(TEMPLATES))

PORTFOLIO_CFG = OrderedDict([
    ("000725", {"name": "京东方A", "market": "sz"}),
    ("000938", {"name": "紫光股份", "market": "sz"}),
    ("000002", {"name": "万科A", "market": "sz"}),
    ("300458", {"name": "全志科技", "market": "sz"}),
])

# ── 工具函数 ──

def market_prefix(code):
    return f"sh{code.strip()}" if code.strip().startswith(("6", "9")) else f"sz{code.strip()}"


def fetch_kline_global(code, days=120):
    """使用 Yahoo Finance 获取全球可访问的行情数据（备用）"""
    try:
        ticker = yf.Ticker("000725.SZ" if code == "000725" else
                          "000938.SZ" if code == "000938" else
                          "000002.SZ" if code == "000002" else
                          "300458.SZ")
        hist = ticker.history(period="6mo")
        if hist is None or hist.empty or len(hist) < 20:
            return None
        data = []
        for idx, row in hist.iterrows():
            data.append({
                "day": idx.strftime("%Y-%m-%d"),
                "open": str(round(row["Open"], 2)),
                "high": str(round(row["High"], 2)),
                "low": str(round(row["Low"], 2)),
                "close": str(round(row["Close"], 2)),
                "volume": str(int(row["Volume"]))
            })
        return data
    except Exception:
        return None

def fetch_kline(code, days=120):
    url = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": market_prefix(code), "scale": 240, "ma": "no", "datalen": min(days, 500)}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if not isinstance(data, list) or len(data) < 20:
            return None
        return data
    except Exception:
        return None

def calc_indicators(kline_data):
    if not kline_data or len(kline_data) < 20:
        return None
    closes = [float(d.get("close", 0)) for d in kline_data]
    volumes = [float(d.get("volume", 0)) for d in kline_data]
    n = len(closes)
    if n < 20:
        return None
    last = closes[-1]
    ma20 = sum(closes[-20:]) / 20 if n >= 20 else None
    ma60 = sum(closes[-60:]) / 60 if n >= 60 else None

    def ema(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result

    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    dif = [e12[i] - e26[i] for i in range(n)]
    dea = ema(dif, 9)
    macd = [2 * (dif[i] - dea[i]) for i in range(n)]
    macd_signal = "金叉" if dif[-1] > dea[-1] else "死叉" if dif[-1] < dea[-1] else "粘合"

    def calc_rsi(data, period=14):
        if len(data) < period + 1:
            return 50
        gains, losses = 0, 0
        for i in range(-period, 0):
            diff = data[i] - data[i - 1]
            if diff > 0: gains += diff
            else: losses -= diff
        avg_gain = gains / period
        avg_loss = losses / period
        if avg_loss == 0: return 100
        return 100 - (100 / (1 + avg_gain / avg_loss))

    rsi = calc_rsi(closes, 14)
    vol_ma5 = sum(volumes[-5:]) / 5 if n >= 5 else 1
    vol_ratio = round(volumes[-1] / vol_ma5, 2) if vol_ma5 > 0 else 1
    change_pct = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if n >= 2 else 0

    return {
        "close": round(last, 2), "change_pct": change_pct,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "dif": round(dif[-1], 3), "dea": round(dea[-1], 3),
        "macd": round(macd[-1], 3), "macd_signal": macd_signal,
        "rsi": round(rsi, 1), "vol_ratio": vol_ratio,
        "high": max(d["high"] for d in kline_data[-5:]),
        "low": min(d["low"] for d in kline_data[-5:]),
    }

def generate_signal(code, indicators, user_price=None):
    if not indicators:
        return {"verdict": "数据不足", "color": "gray", "action": "无法判断", "reason": "历史数据不足20个交易日", "signals": []}
    price = user_price if user_price else indicators["close"]
    rsi = indicators["rsi"]
    macd_signal = indicators["macd_signal"]
    ma20, ma60 = indicators.get("ma20"), indicators.get("ma60")
    vol_ratio = indicators.get("vol_ratio", 1)

    signals = []
    if rsi > 75: signals.append({"type": "sell", "text": "RSI超买"})
    elif rsi > 70: signals.append({"type": "caution", "text": "RSI偏高"})
    elif rsi < 30: signals.append({"type": "buy", "text": "RSI超卖"})
    elif rsi < 35: signals.append({"type": "watch", "text": "RSI接近超卖"})
    if macd_signal == "金叉" and indicators["macd"] > 0:
        signals.append({"type": "bull", "text": "MACD金叉"})
    elif macd_signal == "死叉" and indicators["macd"] < 0:
        signals.append({"type": "bear", "text": "MACD死叉"})
    if ma20 and ma60:
        if price > ma20 > ma60: signals.append({"type": "bull", "text": "多头排列"})
        elif price < ma20 < ma60: signals.append({"type": "bear", "text": "空头排列"})
    if ma20 and price > ma20: signals.append({"type": "bull", "text": "站上MA20"})
    elif ma20 and price < ma20: signals.append({"type": "bear", "text": "跌破MA20"})
    if vol_ratio > 2: signals.append({"type": "watch", "text": "放量" + ("上涨" if indicators["change_pct"] > 0 else "下跌")})
    elif vol_ratio < 0.6: signals.append({"type": "info", "text": "地量"})

    buy_s = sum(1 for s in signals if s["type"] in ("buy", "bull"))
    sell_s = sum(1 for s in signals if s["type"] in ("sell", "bear", "caution"))

    if rsi < 30:
        return {"verdict": "强烈买入", "color": "green", "action": "🟢 强烈买入",
                "reason": f"RSI超卖({rsi})，技术性反弹机会", "signals": signals}
    if rsi > 75:
        return {"verdict": "卖出", "color": "red", "action": "🔴 建议卖出",
                "reason": f"RSI深度超买({rsi})，回调风险大", "signals": signals}

    if buy_s >= 3: return {"verdict": "买入", "color": "green", "action": "🟢 强烈买入",
                           "reason": f"多头信号充足 (RSI{rsi}, {macd_signal})", "signals": signals}
    if buy_s >= 2: return {"verdict": "谨慎买入", "color": "teal", "action": "🟡 谨慎买入",
                           "reason": f"偏多信号 (RSI{rsi}, {macd_signal})", "signals": signals}
    if sell_s >= 3: return {"verdict": "卖出", "color": "red", "action": "🔴 建议卖出",
                            "reason": f"空头信号密集 (RSI{rsi}, {macd_signal})", "signals": signals}
    if sell_s >= 2: return {"verdict": "考虑减仓", "color": "orange", "action": "🟠 考虑减仓",
                            "reason": f"偏空信号 (RSI{rsi}, {macd_signal})", "signals": signals}
    return {"verdict": "持有/观望", "color": "gray", "action": "⚪ 持有观望",
            "reason": f"信号中性 (RSI{rsi}, {macd_signal})", "signals": signals}

# ── 复盘数据读取 ──

def _load_bundled():
    """Fallback: 从打包的 JSON 读取历史复盘"""
    if not BUNDLED.exists():
        return []
    with open(BUNDLED, "r", encoding="utf-8") as f:
        return json.load(f)

def _load_storage():
    """读取云端生成的复盘记录"""
    if not STORAGE.exists():
        return []
    with open(STORAGE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_to_storage(review):
    """保存新复盘到本地存储"""
    store = _load_storage()
    store.insert(0, review)
    with open(STORAGE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False)

def list_reviews():
    """合并本地 + 云端复盘"""
    local = []
    if TRENDS.exists():
        for f in sorted(TRENDS.glob("复盘-*.md"), reverse=True):
            m = re.search(r"复盘-(\d{4}-\d{2}-\d{2})", f.name)
            if not m: continue
            content = f.read_text(encoding="utf-8", errors="replace")
            rows_found = []
            in_table = False
            for line in content.splitlines():
                if "收盘行情" in line or "行情" in line:
                    in_table = True; continue
                if in_table and line.startswith("|") and "---" not in line:
                    cells = [c.strip() for c in line.split("|") if c.strip()]
                    if len(cells) >= 4:
                        stock_name = cells[0].replace("**","")
                        signal_cell = cells[-1].strip() if len(cells) > 4 else ""
                        rows_found.append(f"{stock_name} {signal_cell}")
                elif in_table and not line.startswith("|"):
                    in_table = False
            local.append({"date": m.group(1), "signals": " | ".join(rows_found[:4]), "source": "obsidian"})
    # 合并云端
    cloud = _load_bundled()
    online = _load_storage()
    seen = set(r["date"] for r in local)
    for r in cloud + online:
        if r["date"] not in seen:
            local.append({"date": r["date"], "signals": "", "source": "bundled"})
            seen.add(r["date"])
    local.sort(key=lambda x: x["date"], reverse=True)
    return local

def md_to_html(md_text):
    """简单的 Markdown → HTML 转换"""
    lines = md_text.splitlines()
    html_parts = []
    in_table = False
    for line in lines:
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if not in_table:
                html_parts.append("<table>")
                in_table = True
            if set(line.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
                continue
            tag = "th" if any("---" in line for _ in [1]) else ("th" if html_parts[-1].count("<tr>") == 0 else "td")
            if html_parts[-1].count("<tr>") == 0 and tag == "td":
                tag = "th"
            row = "<tr>" + "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in cells) + "</tr>"
            html_parts.append(row)
        else:
            if in_table:
                html_parts.append("</table>")
                in_table = False
            if line.startswith("### "):
                html_parts.append(f"<h3>{html.escape(line[4:])}</h3>")
            elif line.startswith("## "):
                html_parts.append(f"<h2>{html.escape(line[3:])}</h2>")
            elif line.startswith("# "):
                html_parts.append(f"<h1>{html.escape(line[2:])}</h1>")
            elif line.startswith("---"):
                html_parts.append("<hr>")
            elif line.startswith("> "):
                html_parts.append(f"<blockquote>{html.escape(line[2:])}</blockquote>")
            elif line.startswith("- ") or line.startswith("* "):
                html_parts.append(f"<li>{html.escape(line[2:])}</li>")
            elif line.strip() == "":
                html_parts.append("<br>")
            else:
                html_parts.append(f"<p>{html.escape(line)}</p>")
    if in_table:
        html_parts.append("</table>")
    return "\n".join(html_parts)

# ── API 路由 ──

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/reviews")
def api_reviews():
    return jsonify({"reviews": list_reviews(), "total": len(list_reviews())})

@app.route("/api/review/<date>")
def api_review(date):
    # 先尝试从本地知识库读取
    if TRENDS.exists():
        f = TRENDS / f"复盘-{date}.md"
        if f.exists():
            content = f.read_text(encoding="utf-8", errors="replace")
            html_content = md_to_html(content)
            stocks = []
            current = None
            for line in content.splitlines():
                m = re.match(r"^## (.+?)（(\d{6})）", line)
                if m:
                    if current: stocks.append(current)
                    current = {"name": m.group(1), "code": m.group(2), "lines": []}
                elif current:
                    current["lines"].append(line)
            if current: stocks.append(current)
            return jsonify({"date": date, "content": content, "html": html_content, "stocks": stocks})
    # 回退到打包数据
    for review in _load_bundled() + _load_storage():
        if review["date"] == date:
            stocks_html = ""
            for s in review.get("stocks", []):
                stocks_html += f"<h2>{s['name']}（{s['code']}）</h2>" + "<br>".join(html.escape(l) for l in s.get("lines", [])) + "<br>"
            return jsonify({"date": date, "content": "", "html": stocks_html, "stocks": review.get("stocks", [])})
    return jsonify({"error": "未找到该日复盘"}), 404

@app.route("/api/portfolio")
def api_portfolio():
    stocks = []
    for code, info in PORTFOLIO_CFG.items():
        kline = fetch_kline(code, 60)
        indicators = calc_indicators(kline) if kline else None
        sd = {"code": code, "name": info["name"], "market": info["market"]}
        if indicators:
            sig = generate_signal(code, indicators)
            sd.update({"price": indicators["close"], "change_pct": indicators["change_pct"],
                       "ma20": indicators["ma20"], "ma60": indicators["ma60"],
                       "rsi": indicators["rsi"], "macd": indicators["macd"],
                       "macd_signal": indicators["macd_signal"], "vol_ratio": indicators["vol_ratio"],
                       "verdict": sig["verdict"], "color": sig["color"],
                       "action": sig["action"], "reason": sig["reason"], "signals": sig["signals"]})
        else:
            sd.update({"price": None, "verdict": "数据不足", "color": "gray", "signals": []})
        stocks.append(sd)
    return jsonify({"stocks": stocks, "time": datetime.datetime.now().strftime("%H:%M")})

@app.route("/api/review/run", methods=["POST"])
def api_review_run():
    data = request.get_json() or {}
    user_prices = data.get("prices", {})
    results = []
    for code, info in PORTFOLIO_CFG.items():
        kline = fetch_kline(code, 120)
        if not kline:
            kline = fetch_kline_global(code, 120)
        indicators = calc_indicators(kline) if kline else None
        user_price = user_prices.get(code)
        price_source = "manual" if user_price else "auto"
        price = float(user_price) if user_price else (indicators["close"] if indicators else None)
        sr = {"code": code, "name": info["name"], "price": price, "price_source": price_source}
        if indicators and price:
            sig = generate_signal(code, indicators, price)
            sr.update({"change_pct": indicators.get("change_pct", 0),
                       "ma20": indicators.get("ma20"), "ma60": indicators.get("ma60"),
                       "rsi": indicators.get("rsi"), "macd": indicators.get("macd"),
                       "macd_signal": indicators.get("macd_signal"),
                       "vol_ratio": indicators.get("vol_ratio"),
                       "verdict": sig["verdict"], "color": sig["color"],
                       "action": sig["action"], "reason": sig["reason"],
                       "signals": sig["signals"]})
        else:
            sr.update({"verdict": "数据不足", "color": "gray", "action": "无法判断",
                       "reason": "行情数据不足", "signals": []})
        results.append(sr)

    # 保存新复盘
    review = {
        "date": datetime.date.today().isoformat(),
        "title": f"复盘 {datetime.date.today().isoformat()}",
        "stocks": [{"name": PORTFOLIO_CFG[r["code"]]["name"], "code": r["code"],
                     "lines": [f"${r.get('price','')} | {r.get('verdict','')} | {r.get('reason','')}"]}
                    for r in results],
        "source": "web"
    }
    _save_to_storage(review)

    return jsonify({"stocks": results, "date": datetime.date.today().isoformat(),
                    "time": datetime.datetime.now().strftime("%H:%M")})

if __name__ == "__main__":
    os.makedirs(str(BASE / "templates"), exist_ok=True)
    port = int(os.environ.get("PORT", 5002))
    print(f"⚡ 股票复盘 Web App 启动中 (port={port})")
    print(f"   历史复盘: {'本地知识库' if TRENDS.exists() else '打包数据'}")
    app.run(host="0.0.0.0", port=port, debug=False)
