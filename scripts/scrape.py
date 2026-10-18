#!/usr/bin/env python3
"""抓取 qiyoujiage.com 首页 31 省油价，写 data/ + public/data/。

规则（诚实性硬要求）：
- 只取价格数字 + 调价时间，不抄文章。
- 解析失败 / 省份不足 31 / 价格越界 -> 直接失败退出，不写脏数据。
- 每天最多 1 次（由 GitHub Actions 定时触发），UA 明确标识。
- 预测/汇率上游取不到 -> forecast.json 写 no_data，不编造。
- 时间字段语义：last_checked_at 每次抓取都更新（即使价格无变化）；
  last_change_at 仅价格变化时更新；fetched_at/updated_at 兼容保留，与 last_change_at 同值。
- meta 的 forecast_status + data_quality.forecast 从 forecast 派生（每日任务与每小时
  forecast 任务都同步），不再写死 no_data。
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python <3.9 fallback, not expected
    ZoneInfo = None

ET = ZoneInfo("America/New_York") if ZoneInfo else timezone(timedelta(hours=-5))


def is_oil_trading_now(dt=None):
    """原油期货是否在交易时段内。NYMEX WTI / ICE Brent 近似统一口径：
    周日 18:00 ET 至周五 17:00 ET 连续交易，每日 17:00–18:00 ET 休市 1 小时，周六全天休市。
    ET 用 zoneinfo 自动处理夏令时（EDT UTC-4 / EST UTC-5），不硬编码偏移。
    dt 为空则取当前 UTC；naive dt 按 UTC 处理。"""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(ET)
    wd = et.weekday()  # Mon=0 ... Sun=6
    mins = et.hour * 60 + et.minute
    if wd == 5:  # 周六全天休市
        return False
    if wd == 6:  # 周日 18:00 后开市
        return mins >= 18 * 60
    if wd == 4:  # 周五 00:00–17:00 交易，17:00 后休市至周日
        return mins < 17 * 60
    # 周一–周四：除每日 17:00–18:00 休市外均交易
    return not (17 * 60 <= mins < 18 * 60)

CST = timezone(timedelta(hours=8))  # 北京时间，种子与展示统一 +08:00

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PUBLIC_DATA = ROOT / "public" / "data"
SOURCE_URL = "http://www.qiyoujiage.com/"
UA = "oil-price/1.0 (+https://github.com/fanzbus/oil-price; daily sync, contact via repo)"

PROVINCES = ["北京", "上海", "江苏", "天津", "重庆", "江西", "辽宁", "安徽", "内蒙古",
             "福建", "宁夏", "甘肃", "青海", "广东", "山东", "广西", "山西", "贵州",
             "陕西", "海南", "四川", "河北", "西藏", "河南", "新疆", "黑龙江",
             "吉林", "云南", "湖北", "浙江", "湖南"]


# ---- 成品油调价窗口：法定工作日表（国务院放假安排）----
# 兜底滚动用：源站“下次调价”未及时滚动时，按 10 个法定工作日规则推算下一窗口。
# 来源：国务院办公厅《关于2026年部分节假日安排的通知》（2025-11-04）。
# 注意：每年新通知发布后需补当年的表；未覆盖的年份按周一至周五估算。
CN_HOLIDAYS = {
    # 2026 年放假日（共 33 天；落在周末的也列入，无害）
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07",
}
CN_WORKDAYS = {
    # 2026 年调休上班日（周末变工作日）
    "2026-01-04", "2026-02-14", "2026-02-28",
    "2026-05-09", "2026-09-20", "2026-10-10",
}


def is_working_day(d):
    """法定工作日：调休上班日算工作日，放假日不算，其余按周一至周五。"""
    key = d.isoformat()
    if key in CN_WORKDAYS:
        return True
    if key in CN_HOLIDAYS:
        return False
    return d.weekday() < 5


def tenth_working_day_after(adj_day):
    """上次调价日（24时日）之后的第 10 个法定工作日，即下一调价日（24时日）。"""
    n, d = 0, adj_day + timedelta(days=1)
    while True:
        if is_working_day(d):
            n += 1
            if n == 10:
                return d
        d += timedelta(days=1)


def fetch_text(url, timeout=20):
    last = None
    for attempt in range(3):  # 失败重试 2 次
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            for enc in ("utf-8", "gbk", "gb2312"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed after 3 attempts: {url}: {last}")


def parse_prices(html):
    # 去脚本/样式，标签转空格，压缩空白
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    provinces = []
    for name in PROVINCES:
        m = re.search(rf"{re.escape(name)}\s*(\d+\.\d+)\s*(\d+\.\d+)\s*(\d+\.\d+)\s*(\d+\.\d+)", text)
        if not m:
            raise ValueError(f"province not found or unparsable: {name}")
        vals = [float(x) for x in m.groups()]
        if not all(3.0 <= v <= 15.0 for v in vals):
            raise ValueError(f"price out of range for {name}: {vals}")
        provinces.append({"province": name, "p92": vals[0], "p95": vals[1],
                          "p98": vals[2], "d0": vals[3]})
    if len(provinces) != 31:
        raise ValueError(f"expected 31 provinces, got {len(provinces)}")
    # 下次调价时间：如“下次油价10月15日24时调整”
    next_label = None
    m = re.search(r"下次油价\s*(\d+月\d+日\d+时)\s*调整", text)
    if m:
        next_label = m.group(1)
    status = "未知"
    if "搁浅" in text[:2000]:
        status = "搁浅（维持目前价格不变）"
    elif "下调" in text[:2000]:
        status = "预计下调"
    elif "上调" in text[:2000]:
        status = "预计上调"
    return provinces, next_label, status


# ---- 城市级价格（qiyoujiage.com 城市页，与省表同源）----
# 省拼音例外：陕西 shanxi-3（与山西 shanxi 区分）、内蒙古 neimenggu、黑龙江 heilongjiang、西藏 xizang
PROVINCE_PINYIN = {
    "云南": "yunnan", "陕西": "shanxi-3", "四川": "sichuan", "辽宁": "liaoning",
    "新疆": "xinjiang", "青海": "qinghai", "西藏": "xizang", "贵州": "guizhou",
    "内蒙古": "neimenggu", "黑龙江": "heilongjiang",
}
NON_UNIFIED = ["云南", "陕西", "四川", "辽宁", "新疆", "青海", "西藏", "贵州", "内蒙古", "黑龙江"]
CITY_PRICE_RE = re.compile(r"<dt>[^<]*?(92#|95#|98#|0#)[^<]*</dt>\s*<dd>([\d.]+)</dd>")
CITY_OIL_KEY = {"92#": "p92", "95#": "p95", "98#": "p98", "0#": "d0"}


def parse_city_list(html, prov_pinyin):
    """从省页解析城市链接：href="/{省}/{市}.shtml"，锚文本如“成都汽油价格”。

    只取恰好两段的链接，排除 /{省}/{市}/{区}.shtml 这类区县链接与 /jiayouzhan/ 前缀链接。
    返回 [(pinyin, city_name)]，按 pinyin 去重排序。
    """
    seen = {}
    pat = re.compile(rf'href="/{re.escape(prov_pinyin)}/([a-z0-9\-]+)\.shtml"[^>]*>([^<]+)</a>')
    for m in pat.finditer(html):
        pinyin, label = m.group(1), m.group(2).strip()
        name = re.sub(r"汽油价格\s*$", "", label).strip() or label
        seen[pinyin] = name
    return sorted(seen.items())


def parse_city_prices(html):
    """解析城市页 <div id="youjia"> 内 dl/dt/dd，返回 {p92, p95, p98, d0}（缺失为 None，不编 0）。"""
    out = {}
    for m in CITY_PRICE_RE.finditer(html):
        try:
            out[CITY_OIL_KEY[m.group(1)]] = float(m.group(2))
        except ValueError:
            continue
    return out


def fetch_cities():
    """抓 10 个非统一价省的全部城市页。失败时返回 status=error/partial，由调用方决定保留旧文件。

    校验：单价 3.0–15.0；95>92、98>95（均存在时）；任一不满足 -> status=anomaly，价格仍存入但前端不展示、标“待核对”。
    缺 92#（无基准价）或抓取失败 -> 跳过该市，记入 errors。
    """
    fetched_at = datetime.now(CST).isoformat(timespec="seconds")
    provinces_out, errors, total = {}, [], 0
    for prov in NON_UNIFIED:
        pinyin = PROVINCE_PINYIN[prov]
        try:
            prov_html = fetch_text(f"http://www.qiyoujiage.com/{pinyin}.shtml")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{prov} 省页抓取失败: {e}")
            continue
        cities = parse_city_list(prov_html, pinyin)
        if not cities:
            errors.append(f"{prov} 未发现城市链接")
            continue
        rows = []
        for city_pinyin, city_name in cities:
            url = f"http://www.qiyoujiage.com/{pinyin}/{city_pinyin}.shtml"
            try:
                prices = parse_city_prices(fetch_text(url))
                time.sleep(0.3)  # 小延迟，低频
            except Exception as e:  # noqa: BLE001
                errors.append(f"{prov}{city_name} 抓取失败: {e}")
                continue
            if prices.get("p92") is None:
                errors.append(f"{prov}{city_name} 缺 92# 价格，跳过")
                continue
            status = "ok"
            for v in prices.values():
                if v is not None and not (3.0 <= v <= 15.0):
                    status = "anomaly"
            p92, p95, p98 = prices.get("p92"), prices.get("p95"), prices.get("p98")
            if p92 is not None and p95 is not None and not (p95 > p92):
                status = "anomaly"
            if p95 is not None and p98 is not None and not (p98 > p95):
                status = "anomaly"
            rows.append({"city": city_name, "pinyin": city_pinyin,
                         "p92": p92, "p95": p95, "p98": p98, "d0": prices.get("d0"),
                         "source_url": url, "status": status})
            total += 1
        if rows:
            provinces_out[prov] = rows
        else:
            errors.append(f"{prov} 无可用城市数据")
    ok = bool(provinces_out) and total >= 60  # 期望约 124 城，低于 60 视为异常，不覆盖旧文件
    return {"fetched_at": fetched_at, "source": "qiyoujiage.com",
            "source_url": SOURCE_URL, "status": "ok" if ok else "error",
            "non_unified_provinces": NON_UNIFIED,
            "provinces": provinces_out, "count": total, "errors": errors}


def _yahoo_series(symbol):
    """Yahoo Finance 期货日线 JSON：返回 [(date, close)]，跳过空值。

    URL: query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d
    timestamp 为 UTC epoch 秒，转 YYYY-MM-DD。价格越界（<20 或 >200）视为脏数据抛错，触发回退。
    """
    raw = fetch_text(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d",
        timeout=20)
    data = json.loads(raw)
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError(f"Yahoo empty result: {symbol}")
    r0 = result[0]
    stamps = r0.get("timestamp") or []
    closes = ((r0.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    rows = []
    for ts, c in zip(stamps, closes):
        if c is None:
            continue
        if not (20.0 <= float(c) <= 200.0):
            raise ValueError(f"Yahoo price out of range: {symbol} {c}")
        d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        rows.append((d, float(c)))
    if len(rows) < 20:
        raise ValueError(f"Yahoo too few points: {symbol} {len(rows)}")
    return rows


def _fred_series(series_id):
    """FRED 每日现货价 CSV：DATE,VALUE，空值（节假日）跳过。返回 [(date, price)]。"""
    csv_text = fetch_text(
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd=2026-08-01",
        timeout=20)
    rows = []
    for line in csv_text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if v in ("", "."):
            continue
        try:
            rows.append((d, float(v)))
        except ValueError:
            continue
    return rows


def try_forecast(provinces=None):
    """按发改委规则估算下次调价。任一上游取不到即返回 no_data（不编造）。

    上游：主源 Yahoo Finance Brent 期货 (BZ=F) + WTI 期货 (CL=F) 日线 + ER-API USD/CNY；
    Yahoo 任一失败则回退 FRED Brent (DCOILBRENTEU) + WTI (DCOILWTICO) 日度现货价。
    Stooq 已于 2026-10-15 实测被 JS 验证墙拦截；FRED Brent 曾于 2026-10-02 单日 +18% 跳变
    （114.82 → 135.51），10-06 仍 125.44、比市场价高约 20 美元，故 FRED 仅作备用回退，不作主源。
    注意：Brent/WTI 只是官方一揽子（Brent/Dubai/Minas 等）的免费代理，不是官方篮子。
    当前为新周期初期（上次 2026-10-15 24:00 搁浅），用最近 20 个交易日滚动对比
    （近 10 日均值 vs 再往前 10 日均值）作近似，越接近调价日越准，置信度低。

    换算推导（写清系数，不拍脑袋）：
    原油成本（元/吨）= 一揽子均价（USD/桶）× 7.33 桶/吨 × 汇率（CNY/USD）。
    原油成本变化（元/吨）= 变化率 × 原油成本。精炼油调价 ≈ 原油成本变化 × 系数 1.0，
    系数 1.0 为估算假设（忽略加工利润率扣减、税费、流通费用变化），并以 ±30% 给区间。
    元/吨 → 元/升：汽油按 0.725 kg/L 约 1380 L/吨，柴油按 0.84 约 1190 L/吨。
    校验：150 元/吨 /1380 ≈ 0.11 元/升，/1190 ≈ 0.13 元/升，与源站“150 元/吨≈0.11-0.14”量级一致。
    """
    _trading = is_oil_trading_now()
    base = {"status": "no_data", "as_of": None, "change_rate_pct": None,
            "direction": None, "estimated_adjustment_yuan_per_ton": None,
            "market_status": "trading" if _trading else "closed",
            "market_status_note": "交易时段内" if _trading else "休市中，显示上一交易日收盘",
            "confidence": None,
            "formula": "10 个工作日一揽子原油价格变化率；按发改委规则，变化率对应调价金额不足 50 元/吨时不作调整（搁浅）。40 美元地板 / 130 美元天花板。本字段为规则估算，非官方预测。",
            "method": "Yahoo Finance Brent 期货 (BZ=F) + WTI 期货 (CL=F) 最近 10 个交易日均值 vs 再往前 10 个交易日均值，取两者变化率均值；汇率用 ER-API USD/CNY；换算见 forecast.json 内 derivation。Yahoo 取不到则回退 FRED；任一上游取不到则 status=no_data，不编造。",
            "note": "暂无预测数据：上游公开源取不到或不足 20 个交易日。",
            "sources": {"fx": "https://open.er-api.com/v6/latest/USD",
                       "oil_brent": "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?range=1mo&interval=1d",
                       "oil_wti": "https://query1.finance.yahoo.com/v8/finance/chart/CL=F?range=1mo&interval=1d",
                       "oil_note": "主源 Yahoo Finance（BZ=F / CL=F 期货日线）；FRED 保留为备用回退。Stooq 于 2026-10-15 实测被 JS 验证墙拦截。"},
            "disclaimer": "预测仅供参考，以国家发改委官方通知为准。Brent/WTI 为官方一揽子的免费代理，非官方篮子；系数 1.0 与 ±30% 区间均为估算假设。"}
    try:
        # 主源 Yahoo（更实时），任一失败则回退 FRED；两者都失败则 no_data
        oil_source = None
        brent = wti = None
        try:
            y_brent = _yahoo_series("BZ=F")
            y_wti = _yahoo_series("CL=F")
            if len(y_brent) >= 20 and len(y_wti) >= 20:
                brent, wti, oil_source = y_brent, y_wti, "yahoo"
        except Exception:  # noqa: BLE001 — 回退 FRED
            pass
        if oil_source is None:
            try:
                f_brent = _fred_series("DCOILBRENTEU")
                f_wti = _fred_series("DCOILWTICO")
                if len(f_brent) >= 20 and len(f_wti) >= 20:
                    brent, wti, oil_source = f_brent, f_wti, "fred"
            except Exception:  # noqa: BLE001
                pass
        if oil_source is None:
            return base
        fx = json.loads(fetch_text("https://open.er-api.com/v6/latest/USD", timeout=15))
        usd_cny = fx.get("rates", {}).get("CNY")
        if not usd_cny:
            return base

        def _avg(rows, sl):
            part = rows[sl]
            return sum(p for _, p in part) / len(part)
        b_recent, b_prev = _avg(brent, slice(-10, None)), _avg(brent, slice(-20, -10))
        w_recent, w_prev = _avg(wti, slice(-10, None)), _avg(wti, slice(-20, -10))
        b_change = (b_recent - b_prev) / b_prev * 100
        w_change = (w_recent - w_prev) / w_prev * 100
        c = (b_change + w_change) / 2
        basket = (b_recent + w_recent) / 2  # USD/桶
        crude_cost_per_ton = basket * 7.33 * usd_cny  # 元/吨
        est = round(c / 100 * crude_cost_per_ton, 0)  # 中心值
        low, high = min(round(est * 0.7, 0), round(est * 1.3, 0)), max(round(est * 0.7, 0), round(est * 1.3, 0))
        direction = "上调" if est >= 50 else ("下调" if est <= -50 else "搁浅")
        # 地板/天花板：以一揽子代理均价为准，同时标注 Brent 单项是否接近上限
        floor_hit = basket <= 40
        ceiling_hit = basket >= 130
        brent_near_ceiling = brent[-1][1] >= 120  # Brent 最新价接近 130，单独提示
        delta_gas = round(est / 1380, 3)
        delta_diesel = round(est / 1190, 3)
        delta = {"p92": delta_gas, "p95": delta_gas, "p98": delta_gas, "d0": delta_diesel}
        predicted = []
        if provinces:
            for r in provinces:
                predicted.append({
                    "province": r["province"],
                    "p92": round(r["p92"] + delta_gas, 2),
                    "p95": round(r["p95"] + delta_gas, 2),
                    "p98": round(r["p98"] + delta_gas, 2),
                    "d0": round(r["d0"] + delta_diesel, 2),
                })
        def _navg(key):
            if not provinces:
                return None
            return round(sum(r[key] for r in provinces) / len(provinces) + delta[key], 3)
        as_of = min(brent[-1][0], wti[-1][0])  # 取数据最后日期，非今天
        if oil_source == "yahoo":
            as_of_note = "Yahoo Finance 最后交易日（通常为今天或上一交易日）。"
            confidence = "低（新周期初期滚动近似 + 代理篮子 + 系数 1.0 假设，越接近调价日越准）"
            method = ("Yahoo Finance Brent 期货 (BZ=F) + WTI 期货 (CL=F) 最近 10 个交易日均值 vs 再往前 10 个交易日均值，"
                      "取两者变化率均值；汇率用 ER-API USD/CNY；换算见 forecast.json 内 derivation。")
            sources = {"fx": "https://open.er-api.com/v6/latest/USD",
                        "oil_brent": "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?range=1mo&interval=1d",
                        "oil_wti": "https://query1.finance.yahoo.com/v8/finance/chart/CL=F?range=1mo&interval=1d",
                        "oil_note": "主源 Yahoo Finance（BZ=F / CL=F 期货日线）；FRED 保留为备用回退。"}
        else:  # fred fallback
            as_of_note = "FRED 数据最后日期（滞后于今天），非抓取日期；Yahoo 取不到，已回退 FRED。"
            confidence = "低（新周期初期滚动近似 + 代理篮子 + 系数 1.0 假设 + FRED 滞后，越接近调价日越准；FRED Brent 曾出现单日跳变，仅供参考）"
            method = ("FRED Brent (DCOILBRENTEU) + WTI (DCOILWTICO) 最近 10 个交易日均值 vs 再往前 10 个交易日均值，"
                      "取两者变化率均值；汇率用 ER-API USD/CNY；换算见 forecast.json 内 derivation。Yahoo 取不到，已回退 FRED。")
            sources = {"fx": "https://open.er-api.com/v6/latest/USD",
                        "oil_brent": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU",
                        "oil_wti": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILWTICO",
                        "oil_note": "Yahoo 取不到，已回退 FRED（美联储）。FRED Brent 2026-10-02 曾出现单日 +18% 跳变，结果仅供参考。"}
        out = dict(base)
        out.update({
            "status": "estimated",
            "market_status": "trading" if _trading else "closed",
            "market_status_note": "交易时段内" if _trading else "休市中，显示上一交易日收盘",
            "oil_source": oil_source,
            "method": method,
            "sources": sources,
            "as_of": as_of,
            "as_of_note": as_of_note,
            "brent": {"latest": brent[-1][1], "latest_date": brent[-1][0],
                      "avg_recent_10": round(b_recent, 2), "recent_range": [brent[-10][0], brent[-1][0]],
                      "avg_prev_10": round(b_prev, 2), "prev_range": [brent[-20][0], brent[-11][0]],
                      "change_rate_pct": round(b_change, 2)},
            "wti": {"latest": wti[-1][1], "latest_date": wti[-1][0],
                    "avg_recent_10": round(w_recent, 2), "recent_range": [wti[-10][0], wti[-1][0]],
                    "avg_prev_10": round(w_prev, 2), "prev_range": [wti[-20][0], wti[-11][0]],
                    "change_rate_pct": round(w_change, 2)},
            "fx_usd_cny": usd_cny,
            "fx_updated": fx.get("time_last_update_utc"),
            "basket_usd_per_bbl": round(basket, 2),
            "crude_cost_cny_per_ton": round(crude_cost_per_ton, 0),
            "change_rate_pct": round(c, 2),
            "direction": direction,
            "estimated_adjustment_yuan_per_ton": est,
            "estimated_adjustment_range": {"low": low, "high": high},
            "delta_per_liter": delta,
            "delta_per_liter_range": {
                "p92": [round(min(low, high) / 1380, 3), round(max(low, high) / 1380, 3)],
                "p95": [round(min(low, high) / 1380, 3), round(max(low, high) / 1380, 3)],
                "p98": [round(min(low, high) / 1380, 3), round(max(low, high) / 1380, 3)],
                "d0": [round(min(low, high) / 1190, 3), round(max(low, high) / 1190, 3)]},
            "predicted_national_avg": {"p92": _navg("p92"), "p95": _navg("p95"),
                                       "p98": _navg("p98"), "d0": _navg("d0")},
            "predicted_prices": predicted,
            "confidence": confidence,
            "floor_ceiling": {"basket_vs_40": floor_hit, "basket_vs_130": ceiling_hit,
                              "brent_latest_near_130": brent_near_ceiling,
                              "note": "地板 40 / 天花板 130 按一揽子均价判断；Brent 单项接近 130 仅作提示，不触发天花板。"},
            "derivation": ("原油成本=一揽子均价(USD/桶)×7.33桶/吨×汇率；"
                           "原油成本变化=变化率×原油成本；精炼油调价≈原油成本变化×1.0（估算假设）±30% 区间；"
                           "元/吨→元/升：汽油÷1380，柴油÷1190。"),
            "note": "规则估算，非官方预测；正式结果以国家发改委通知为准。",
        })
        return out
    except Exception:  # noqa: BLE001
        return base


def _forecast_meta_fields(forecast):
    """从 forecast 字典派生 meta 的 forecast_status + data_quality.forecast 文案。

    status=estimated 即 estimated；否则 no_data + 暂无预测文案。只读字典，不碰网络。
    每小时 forecast 任务与每日价格任务共用，保证 meta 与 forecast.json 永远一致。"""
    if isinstance(forecast, dict) and forecast.get("status") == "estimated":
        est = forecast.get("estimated_adjustment_yuan_per_ton")
        rng = forecast.get("estimated_adjustment_range") or {}
        return ("estimated",
                f"预测已接入：as_of {forecast.get('as_of')}，方向{forecast.get('direction')}，"
                f"预计调整 {est} 元/吨（区间 {rng.get('low')}~{rng.get('high')}），"
                f"来源 {forecast.get('oil_source')}。")
    return ("no_data", "暂无预测数据（待上游公开源可用后自动填充）")


def main():
    if "--forecast-only" in sys.argv:
        # 每小时 Action：休市跳过，不写文件、不触发提交；交易时段内只更新 forecast.json
        if not is_oil_trading_now():
            print("market closed, skip")
            return
        provinces = None
        try:
            _p = json.loads((DATA / "prices.json").read_text(encoding="utf-8"))
            provinces = _p.get("provinces")
        except Exception:  # noqa: BLE001 — 无省表时预测仍可算，仅无分省预测价
            provinces = None
        try:
            forecast = try_forecast(provinces)
        except Exception:  # noqa: BLE001
            forecast = {"status": "no_data", "note": "预测计算失败。",
                           "market_status": "trading", "market_status_note": "交易时段内"}
        DATA.mkdir(parents=True, exist_ok=True)
        PUBLIC_DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "forecast.json").write_text(json.dumps(forecast, ensure_ascii=False, indent=2), encoding="utf-8")
        (PUBLIC_DATA / "forecast.json").write_text(json.dumps(forecast, ensure_ascii=False, indent=2), encoding="utf-8")
        # meta：forecast_status + data_quality.forecast 从新 forecast 派生；
        # 不碰 last_checked_at/last_change_at（那是价格抓取的口径）。
        # meta.json 损坏/缺失则跳过补丁，不让预测任务失败。
        f_status, f_text = _forecast_meta_fields(forecast)
        for _mp in (DATA / "meta.json", PUBLIC_DATA / "meta.json"):
            try:
                _meta = json.loads(_mp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            _meta["forecast_status"] = f_status
            _meta.setdefault("data_quality", {})["forecast"] = f_text
            _mp.write_text(json.dumps(_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"OK forecast-only status={forecast.get('status')} oil_source={forecast.get('oil_source')} as_of={forecast.get('as_of')} market={forecast.get('market_status')} meta forecast_status={f_status}")
        return
    html = fetch_text(SOURCE_URL)
    provinces, next_label, status = parse_prices(html)
    now = datetime.now(CST)
    fetched_at = now.isoformat(timespec="seconds")
    # next_adjustment：解析“10月15日24时” -> 次日 00:00；解析失败则 null
    next_iso = None
    if next_label:
        m = re.match(r"(\d+)月(\d+)日(\d+)时", next_label)
        if m:
            mo, day, hour = map(int, m.groups())
            year = now.year
            if hour == 24:
                hour = 0
                # 次日：简单处理（月末跨月由 fromisoformat 校验，失败则 null）
                try:
                    from datetime import timedelta
                    dt = datetime(year, mo, day, tzinfo=now.tzinfo) + timedelta(days=1)
                    next_iso = dt.replace(hour=0, minute=0, second=0).isoformat(timespec="seconds")
                except ValueError:
                    next_iso = None
            else:
                try:
                    next_iso = datetime(year, mo, day, hour, tzinfo=now.tzinfo).isoformat(timespec="seconds")
                except ValueError:
                    next_iso = None

    # 过期兜底：源站“下次调价”未及时滚动时（如 10/15 24时已过、源站仍显示旧窗口），
    # 按 10 个法定工作日规则滚动到未来窗口，避免页面长期显示过期时间。
    # 仅处理 24时格式（源站唯一格式）；其它格式保持原行为。
    next_rolled = False
    if next_iso:
        try:
            parsed = datetime.fromisoformat(next_iso)
            while (parsed <= now and parsed.hour == 0 and parsed.minute == 0
                   and parsed.second == 0):
                adj_day = (parsed - timedelta(days=1)).date()
                nxt = tenth_working_day_after(adj_day)
                parsed = datetime(nxt.year, nxt.month, nxt.day, tzinfo=now.tzinfo) + timedelta(days=1)
                next_label = f"{nxt.month}月{nxt.day}日24时"
                next_rolled = True
            next_iso = parsed.isoformat(timespec="seconds")
        except ValueError:
            pass

    prices = {"fetched_at": fetched_at, "source": "qiyoujiage.com",
              "source_url": SOURCE_URL, "unit": "元/升",
              "next_adjustment": next_iso, "next_adjustment_label": next_label,
              "next_adjustment_rolled": next_rolled,
              "adjustment_status": status,
              "note": "价格为事实数据，仅取数字与调价时间。历史从 2026-10-15 开始积累。",
              "provinces": provinces}
    DATA.mkdir(parents=True, exist_ok=True)
    PUBLIC_DATA.mkdir(parents=True, exist_ok=True)

    # 旧 meta：last_change_at 回退链用（历史缺失时），提前读一次，后面 meta 更新复用
    prev_meta = {}
    _meta_path_early = DATA / "meta.json"
    if _meta_path_early.exists():
        try:
            prev_meta = json.loads(_meta_path_early.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prev_meta = {}

    # 历史：只有变动才追加
    hist_path = DATA / "history.jsonl"
    last = None
    if hist_path.exists() and hist_path.read_text(encoding="utf-8").strip():
        try:
            last = json.loads(hist_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            last = None
    changed = (last is None or last.get("provinces") != provinces)

    # 时间语义（2026-10-18 拆分，旧 fetched_at 语义含糊）：
    # last_checked_at = 本次抓取时间，每次运行都更新，即使价格无变化；
    # last_change_at = 价格最后一次变化的时间 = 本次有变化则 now，否则上条历史快照时间；
    # fetched_at / updated_at 兼容保留，与 last_change_at 同值。
    last_checked_at = fetched_at
    if changed:
        last_change_at = fetched_at
    else:
        last_change_at = ((last or {}).get("fetched_at")
                          or prev_meta.get("last_change_at")
                          or prev_meta.get("fetched_at")
                          or fetched_at)
    prices.update({"fetched_at": last_change_at,
                   "last_change_at": last_change_at,
                   "last_checked_at": last_checked_at})

    if changed:
        with open(hist_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(prices, ensure_ascii=False) + "\n")
        print("history appended (changed)")
    else:
        print("history unchanged, not appended")

    (DATA / "prices.json").write_text(json.dumps(prices, ensure_ascii=False, indent=2), encoding="utf-8")

    # 城市级：失败保留旧文件，不写脏数据，不影响省表抓取
    cities_result = None
    try:
        cities_result = fetch_cities()
        if cities_result.get("status") == "ok":
            (DATA / "cities.json").write_text(json.dumps(cities_result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"cities OK count={cities_result['count']} provinces={len(cities_result['provinces'])} errors={len(cities_result['errors'])}")
        else:
            print(f"cities skipped (keep old): status={cities_result.get('status')} count={cities_result.get('count')} errors={cities_result.get('errors')}")
    except Exception as e:  # noqa: BLE001 — 城市失败不影响省表
        print(f"cities failed (keep old): {e}")

    # 预测先算：meta 的 forecast 字段从本次 forecast 派生（不再写死 no_data）
    try:
        forecast = try_forecast(provinces)
    except Exception:  # noqa: BLE001 — 预测失败不影响价格抓取
        forecast = {"status": "no_data", "note": "预测计算失败，仍保留价格数据。"}
    (DATA / "forecast.json").write_text(json.dumps(forecast, ensure_ascii=False, indent=2), encoding="utf-8")
    f_status, f_text = _forecast_meta_fields(forecast)

    # meta：保留 history_start；时间字段按新语义；forecast 字段从 forecast 派生
    meta_path = DATA / "meta.json"
    meta = dict(prev_meta)
    meta.update({"fetched_at": last_change_at, "updated_at": last_change_at,
                 "last_change_at": last_change_at, "last_checked_at": last_checked_at,
                 "forecast_status": f_status,
                 "source": "qiyoujiage.com", "source_url": SOURCE_URL,
                 "next_adjustment": next_iso, "next_adjustment_label": next_label,
                 "next_adjustment_rolled": next_rolled,
                 "adjustment_status": status,
                 "history_start": meta.get("history_start", "2026-10-15")})
    meta.setdefault("data_quality", {})["forecast"] = f_text
    if cities_result and cities_result.get("status") == "ok":
        meta["cities_status"] = "ok"
        meta.setdefault("data_quality", {})["cities"] = (
            f"城市级价格已接入：{cities_result['count']} 个城市（10 个非统一价省），"
            "来源 qiyoujiage.com 城市页；异常标“待核对”不展示，缺失显示 —。")
    (meta_path).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 同步到 public/data（Worker 通过 ASSETS 提供）
    for name in ["prices.json", "meta.json", "forecast.json"]:
        (PUBLIC_DATA / name).write_text((DATA / name).read_text(encoding="utf-8"), encoding="utf-8")
    for name in ["cities.json", "global.json"]:
        src = DATA / name
        if src.exists():
            (PUBLIC_DATA / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    if hist_path.exists():
        (PUBLIC_DATA / "history.jsonl").write_text(hist_path.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"OK last_checked_at={last_checked_at} last_change_at={last_change_at} provinces=31 next={next_label} status={status} changed={changed} rolled={next_rolled} forecast_status={f_status}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(2)
