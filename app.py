#!/usr/bin/env python3
"""
股票复盘 Web 应用
从 Obsidian 知识库读取历史复盘数据，支持发起新复盘。
"""

import os, re, json, datetime, subprocess, sys, threading
from pathlib import Path
from collections import OrderedDict
def fetch_realtime_quotes(codes):
    """从腾讯财经获取实时行情（盘中实时数据）"""
    codes_str = ",".join(f"sz{c}" if c.startswith(("0","3")) else f"sh{c}" for c in codes)
    url = f"http://qt.gtimg.cn/q={codes_str}"
    try:
        r = requests.get(url, timeout=10)
        r.encoding = "gbk"
        lines = r.text.strip().split("\n")
        quotes = {}
        for line in lines:
            if "=" not in line:
                continue
            parts = line.split("=", 1)[1].strip().strip('";')
            fields = parts.split("~")
            if len(fields) < 6:
                continue
            code = fields[2]
            name = fields[1]
            try:
                price = float(fields[3])
                prev_close = float(fields[4])
                open_price = float(fields[5])
                volume = int(fields[6]) if fields[6] else 0
                change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
                high = float(fields[33]) if len(fields) > 33 and fields[33] else price
                low = float(fields[34]) if len(fields) > 34 and fields[34] else price
            except (ValueError, IndexError):
                continue
            quotes[code] = {
                "name": name, "price": price, "prev_close": prev_close,
                "open": open_price, "high": high, "low": low,
                "volume": volume, "change_pct": change_pct,
            }
        return quotes
    except Exception as e:
        print(f"  [realtime fetch error: {e}]")
        return {}



import requests
import markdown
from flask import Flask, render_template, jsonify, request

# ── 路径配置 ──
BASE = Path(__file__).resolve().parent
TEMPLATES = BASE / "templates"

VAULT = Path("/Users/andy/Documents/industry-research")
TRENDS = VAULT / "04-Trends"

app = Flask(__name__, template_folder=str(TEMPLATES))

# ── 持仓配置 ──
PORTFOLIO_CFG = OrderedDict([
    ("000725", {"name": "京东方A", "market": "sz"}),
    ("000938", {"name": "紫光股份", "market": "sz"}),
    ("000002", {"name": "万科A", "market": "sz"}),
    ("300458", {"name": "全志科技", "market": "sz"}),
])

# ── 工具函数 ──

def market_prefix(code):
    return f"sh{code.strip()}" if code.strip().startswith(("6", "9")) else f"sz{code.strip()}"

def fetch_kline(code, days=120):
    """从新浪 API 获取日线数据"""
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
    """计算技术指标，返回最新值"""
    if not kline_data or len(kline_data) < 20:
        return None
    
    closes = [float(d.get("close", 0)) for d in kline_data]
    highs = [float(d.get("high", 0)) for d in kline_data]
    lows = [float(d.get("low", 0)) for d in kline_data]
    volumes = [float(d.get("volume", 0)) for d in kline_data]
    dates = [d.get("day", "") for d in kline_data]
    
    n = len(closes)
    if n < 20:
        return None
    
    last = closes[-1]
    
    # MA
    ma5 = sum(closes[-5:]) / 5 if n >= 5 else None
    ma20 = sum(closes[-20:]) / 20 if n >= 20 else None
    ma60 = sum(closes[-60:]) / 60 if n >= 60 else None
    
    # MACD
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
    
    # RSI
    def calc_rsi(data, period=14):
        if len(data) < period + 1:
            return 50
        gains, losses = 0, 0
        for i in range(-period, 0):
            diff = data[i] - data[i - 1]
            if diff > 0:
                gains += diff
            else:
                losses -= diff
        avg_gain = gains / period
        avg_loss = losses / period
        if avg_loss == 0:
            return 100
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    rsi = calc_rsi(closes, 14)
    
    # 量比
    vol_ma5 = sum(volumes[-5:]) / 5 if n >= 5 else 1
    vol_ratio = round(volumes[-1] / vol_ma5, 2) if vol_ma5 > 0 else 1
    
    # 涨跌幅
    change_pct = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if n >= 2 else 0
    
    return {
        "close": round(last, 2),
        "change_pct": change_pct,
        "ma5": round(ma5, 2) if ma5 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "dif": round(dif[-1], 3),
        "dea": round(dea[-1], 3),
        "macd": round(macd[-1], 3),
        "macd_signal": macd_signal,
        "rsi": round(rsi, 1),
        "vol_ratio": vol_ratio,
        "high": max(highs[-5:]),
        "low": min(lows[-5:]),
    }


def generate_signal(code, indicators, user_price=None):
    """根据技术指标生成买卖信号"""
    if not indicators:
        return {"verdict": "数据不足", "color": "gray", "verdict_cn": "数据不足", "action": "无法判断", "reason": "历史数据不足20个交易日"}
    
    price = user_price if user_price else indicators["close"]
    rsi = indicators["rsi"]
    macd_signal = indicators["macd_signal"]
    ma20 = indicators.get("ma20")
    ma60 = indicators.get("ma60")
    vol_ratio = indicators.get("vol_ratio", 1)
    
    signals = []
    
    # RSI 信号
    if rsi > 75:
        signals.append({"type": "sell", "text": "RSI超买"})
    elif rsi > 70:
        signals.append({"type": "caution", "text": "RSI偏高"})
    elif rsi < 30:
        signals.append({"type": "buy", "text": "RSI超卖"})
    elif rsi < 35:
        signals.append({"type": "watch", "text": "RSI接近超卖"})
    
    # MACD 信号
    if macd_signal == "金叉" and indicators["macd"] > 0:
        signals.append({"type": "bull", "text": "MACD金叉"})
    elif macd_signal == "死叉" and indicators["macd"] < 0:
        signals.append({"type": "bear", "text": "MACD死叉"})
    
    # 均线信号
    if ma20 and ma60:
        if price > ma20 > ma60:
            signals.append({"type": "bull", "text": "多头排列"})
        elif price < ma20 < ma60:
            signals.append({"type": "bear", "text": "空头排列"})
    if ma20 and price > ma20:
        signals.append({"type": "bull", "text": "站上MA20"})
    elif ma20 and price < ma20:
        signals.append({"type": "bear", "text": "跌破MA20"})
    
    # 成交量信号
    if vol_ratio > 2:
        signals.append({"type": "watch", "text": "放量"+ ("上涨" if indicators["change_pct"] > 0 else "下跌")})
    elif vol_ratio < 0.6:
        signals.append({"type": "info", "text": "地量"})
    
    # 综合判断
    buy_signals = sum(1 for s in signals if s["type"] in ("buy", "bull"))
    sell_signals = sum(1 for s in signals if s["type"] in ("sell", "bear", "caution"))
    
    if buy_signals >= 3 and sell_signals <= 1:
        verdict = "买入"
        color = "green"
        action = "🟢 强烈买入"
        reason = f"多头信号充足 (RSI{rsi}, {macd_signal})"
    elif buy_signals >= 2:
        verdict = "谨慎买入"
        color = "teal"
        action = "🟡 谨慎买入"
        reason = f"偏多信号 (RSI{rsi}, {macd_signal})"
    elif sell_signals >= 3 and buy_signals <= 1:
        verdict = "卖出"
        color = "red"
        action = "🔴 建议卖出"
        reason = f"空头信号密集 (RSI{rsi}, {macd_signal})"
    elif sell_signals >= 2:
        verdict = "考虑减仓"
        color = "orange"
        action = "🟠 考虑减仓"
        reason = f"偏空信号 (RSI{rsi}, {macd_signal})"
    else:
        verdict = "持有/观望"
        color = "gray"
        action = "⚪ 持有观望"
        reason = f"信号中性 (RSI{rsi}, {macd_signal})"
    
    # 特殊情况：超卖/超买覆盖
    if rsi < 30:
        action = "🟢 强烈买入"
        verdict = "强烈买入"
        color = "green"
        reason = f"RSI超卖({rsi})，技术性反弹机会"
    elif rsi > 75:
        action = "🔴 建议卖出"
        verdict = "卖出"
        color = "red"
        reason = f"RSI深度超买({rsi})，回调风险大"
    
    return {
        "verdict": verdict,
        "color": color,
        "action": action,
        "reason": reason,
        "signals": signals,
    }


# ── 复盘文件解析 ──

def list_reviews():
    """列出所有历史复盘文件"""
    if not TRENDS.exists():
        return []
    files = sorted(TRENDS.glob("复盘-*.md"), reverse=True)
    reviews = []
    for f in files:
        m = re.search(r"复盘-(\d{4}-\d{2}-\d{2})", f.name)
        if not m:
            continue
        date_str = m.group(1)
        content = f.read_text(encoding="utf-8", errors="replace")
        # 提取标题行 / 摘要
        title = ""
        for line in content.splitlines():
            if line.startswith("# "):
                title = line.replace("# ", "").strip()
                break
        # 提取行情摘要行
        summary = ""
        for line in content.splitlines():
            if "|" in line and "收盘" not in line and line.startswith("|") and summary == "":
                row_cells = [c.strip() for c in line.split("|") if c.strip()]
                if len(row_cells) >= 4:
                    summary = " | ".join(row_cells[:5])
        # 提取信号摘要
        signals_summary = ""
        rows_found = []
        in_table = False
        for line in content.splitlines():
            if "收盘行情" in line or "行情概览" in line:
                in_table = True
                continue
            if in_table and line.startswith("|") and "---" not in line:
                cells = [c.strip() for c in line.split("|") if c.strip()]
                if len(cells) >= 4:
                    # 提取股票名和信号
                    stock_name = cells[0].split("**")[1] if "**" in cells[0] else cells[0]
                    signal_cell = cells[-1].strip() if len(cells) > 4 else ""
                    rows_found.append(f"{stock_name} {signal_cell}")
            elif in_table and not line.startswith("|"):
                in_table = False
        signals_summary = " | ".join(rows_found[:4])
        
        reviews.append({
            "date": date_str,
            "title": title,
            "summary": summary,
            "signals": signals_summary,
            "path": str(f),
            "line_count": len(content.splitlines()),
        })
    return reviews


def parse_review(date_str):
    """解析单篇复盘文件"""
    f = TRENDS / f"复盘-{date_str}.md"
    if not f.exists():
        return None
    content = f.read_text(encoding="utf-8", errors="replace")
    html = markdown.markdown(content, extensions=["tables", "fenced_code"])
    
    # 提取各股票分析段落
    stocks = []
    current_stock = None
    for line in content.splitlines():
        m = re.match(r"^## (.+?)（(\d{6})）", line)
        if m:
            if current_stock:
                stocks.append(current_stock)
            current_stock = {"name": m.group(1), "code": m.group(2), "lines": []}
        elif current_stock:
            current_stock["lines"].append(line)
    if current_stock:
        stocks.append(current_stock)
    
    return {
        "date": date_str,
        "content": content,
        "html": html,
        "stocks": stocks,
    }


# ── API 路由 ──

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/reviews")
def api_reviews():
    reviews = list_reviews()
    return jsonify({"reviews": reviews, "total": len(reviews)})


@app.route("/api/review/<date>")
def api_review(date):
    data = parse_review(date)
    if not data:
        return jsonify({"error": "未找到该日复盘"}), 404
    return jsonify(data)


@app.route("/api/portfolio")
def api_portfolio():
    """获取持仓股信息+实时行情"""
    codes = list(PORTFOLIO_CFG.keys())
    # 1. 批量获取实时行情（盘中价格、涨跌幅）
    realtime = fetch_realtime_quotes(codes)
    
    # 2. 批量获取 kline 计算技术指标
    stocks = []
    for code, info in PORTFOLIO_CFG.items():
        kline = fetch_kline(code, 60)
        indicators = calc_indicators(kline) if kline else None
        
        stock_data = {
            "code": code,
            "name": info["name"],
            "market": info["market"],
        }
        
        rt = realtime.get(code, {})
        has_realtime = "price" in rt
        indicator_valid = indicators is not None
        
        if has_realtime:
            # 使用实时行情（价格、涨跌幅）+ kline 指标（MA/RSI/MACD）
            price = rt["price"]
            stock_data.update({
                "price": price,
                "change_pct": rt["change_pct"],
                "realtime": True,
            })
            if indicator_valid:
                sig = generate_signal(code, indicators, price)
                stock_data.update({
                    "ma20": indicators["ma20"],
                    "ma60": indicators["ma60"],
                    "rsi": indicators["rsi"],
                    "macd": indicators["macd"],
                    "macd_signal": indicators["macd_signal"],
                    "vol_ratio": indicators["vol_ratio"],
                    "verdict": sig["verdict"],
                    "color": sig["color"],
                    "action": sig["action"],
                    "reason": sig["reason"],
                    "signals": sig["signals"],
                })
            else:
                stock_data.update({
                    "verdict": "实时获取",
                    "color": "gray",
                    "action": "指标计算中",
                    "reason": f"当前 {price}，数据不足20日",
                    "signals": [{"type": "info", "text": "实时行情"}],
                })
        elif indicator_valid:
            sig = generate_signal(code, indicators)
            stock_data.update({
                "price": indicators["close"],
                "change_pct": indicators["change_pct"],
                "realtime": False,
                "ma20": indicators["ma20"],
                "ma60": indicators["ma60"],
                "rsi": indicators["rsi"],
                "macd": indicators["macd"],
                "macd_signal": indicators["macd_signal"],
                "vol_ratio": indicators["vol_ratio"],
                "verdict": sig["verdict"],
                "color": sig["color"],
                "action": sig["action"],
                "reason": sig["reason"],
                "signals": sig["signals"],
            })
        else:
            stock_data.update({
                "price": None,
                "verdict": "数据不足",
                "color": "gray",
                "signals": [],
            })
        stocks.append(stock_data)
    
    return jsonify({"stocks": stocks, "time": datetime.datetime.now().strftime("%H:%M")})


@app.route("/api/review/run", methods=["POST"])
def api_review_run():
    """发起新复盘"""
    data = request.get_json() or {}
    user_prices = data.get("prices", {})  # {code: price}
    
    results = []
    for code, info in PORTFOLIO_CFG.items():
        kline = fetch_kline(code, 120)
        indicators = calc_indicators(kline) if kline else None
        
        user_price = user_prices.get(code)
        price_source = "manual" if user_price else "auto"
        price = float(user_price) if user_price else (indicators["close"] if indicators else None)
        
        stock_result = {
            "code": code,
            "name": info["name"],
            "price": price,
            "price_source": price_source,
        }
        
        if indicators and price:
            sig = generate_signal(code, indicators, price)
            stock_result.update({
                "change_pct": indicators.get("change_pct", 0),
                "ma20": indicators.get("ma20"),
                "ma60": indicators.get("ma60"),
                "rsi": indicators.get("rsi"),
                "macd": indicators.get("macd"),
                "macd_signal": indicators.get("macd_signal"),
                "vol_ratio": indicators.get("vol_ratio"),
                "high_5d": indicators.get("high"),
                "low_5d": indicators.get("low"),
                "verdict": sig["verdict"],
                "color": sig["color"],
                "action": sig["action"],
                "reason": sig["reason"],
                "signals": sig["signals"],
            })
        else:
            stock_result.update({
                "verdict": "数据不足",
                "color": "gray",
                "action": "无法判断",
                "reason": "行情数据不足",
                "signals": [],
            })
        results.append(stock_result)
    
    return jsonify({
        "stocks": results,
        "date": datetime.date.today().isoformat(),
        "time": datetime.datetime.now().strftime("%H:%M"),
    })


if __name__ == "__main__":
    os.makedirs(str(TEMPLATES), exist_ok=True)
    print("⚡ 股票复盘 Web App 启动中...")
    print(f"   历史复盘路径: {TRENDS}")
    app.run(host="0.0.0.0", port=5002, debug=False)
