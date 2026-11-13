#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国内成品油调价预测（cn-fuel-price-forecast）

原理：国家发改委《石油价格管理办法》——汽柴油最高零售价每 10 个工作日调整一次，
挂靠国际市场原油价格变化。本脚本用国际原油（布伦特+WTI）10 工作日双周期均价变化，
按经验证过的传导系数换算成国内吨价涨跌幅度。

数据源（全部实测可用，国内直连）：
  1. 新浪财经国际期货 API —— 布伦特(OIL)/纽约(CL) 实时价 + 历史日线（2016 年至今）
  2. 汽油价格网 (http://www.qiyoujiage.com) —— 官方调价历史表 /tz/YYYY.shtml、各省现价
  3. chinese_calendar —— 中国法定工作日（决定调价窗口日期）

用法：
  python3 fuel_forecast.py            # 人话文本
  python3 fuel_forecast.py --json     # 机器可读
  python3 fuel_forecast.py --verify   # 跑历史回测（校准系数/准确率）
"""
import json
import os
import re
import sys
import bisect
import datetime as dt
import tempfile
import urllib.request

def _pick_cache():
    """优先 ~/.cache；cron 里 HOME 可能不可写 → 逐级兜底。"""
    cands = [os.environ.get("FUEL_FORECAST_CACHE"),
             os.path.expanduser("~/.cache/fuel_forecast"),
             os.path.join(tempfile.gettempdir(), "fuel_forecast_cache")]
    for c in cands:
        if not c:
            continue
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, ".w")
            open(probe, "w").close()
            os.remove(probe)
            return c
        except Exception:
            continue
    return os.path.join(tempfile.gettempdir(), "fuel_forecast_cache")


CACHE = _pick_cache()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# ── 校准出来的模型参数（2020-07 ~ 2024-12，72 个官方调价样本回归）─────────────
K_PER_DOLLAR = 58.56   # 布伦特/WTI 均价每变动 1 美元/桶 → 汽油吨价变动 元/吨（回测值）
LITERS_PER_TON = 1250  # 汽油零售端吨升折算（挂牌价实际换算 ≈ 1250 L/吨）
THRESHOLD = 50         # 元/吨；低于此幅度搁浅（纳入下次累加）
PERIOD = 10            # 工作日

OIL_SYMBOLS = {"OIL": "布伦特", "CL": "WTI纽约"}


# ── 基础工具 ────────────────────────────────────────────────────────────────
def http_get(url, referer=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw.decode("utf-8", "ignore")


def cache_path(name):
    os.makedirs(CACHE, exist_ok=True)
    return os.path.join(CACHE, name)


def cached_json(name, max_age_hours=6, producer=None):
    p = cache_path(name)
    if os.path.exists(p):
        age = (dt.datetime.now() - dt.datetime.fromtimestamp(os.path.getmtime(p))).total_seconds()
        if age < max_age_hours * 3600:
            with open(p, encoding="utf-8") as f:
                return json.load(f), True
    data = producer() if producer else None
    if data is not None:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    return data, False


# ── 数据源 1：新浪国际期货 ──────────────────────────────────────────────────
def sina_realtime():
    out = {}
    try:
        txt = http_get("https://hq.sinajs.cn/list=hf_OIL,hf_CL",
                       referer="https://finance.sina.com.cn")
        for line in txt.split(";"):
            m = re.search(r'hq_str_hf_(\w+)="([^"]*)"', line)
            if not m or not m.group(2):
                continue
            f = m.group(2).split(",")
            if len(f) < 14:
                continue
            out[m.group(1)] = {
                "last": float(f[0]), "time": f[6], "date": f[12],
                "name": f[13], "prev_settle": float(f[7]) if f[7] else None,
            }
    except Exception as e:
        out["_error"] = str(e)
    return out


def sina_daily(symbol):
    def producer():
        url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_" + symbol +
               "=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=" + symbol)
        txt = http_get(url, referer="https://finance.sina.com.cn")
        m = re.search(r"=\s*\((\[.*?\])\)", txt, re.S)
        return {x["date"]: float(x["close"]) for x in json.loads(m.group(1))}

    data, _ = cached_json("kline_%s.json" % symbol, 6, producer)
    return {dt.date.fromisoformat(k): v for k, v in data.items()}


def blend_close(oil, wti):
    ds, dw = sorted(oil), sorted(wti)

    def get(d):
        i = bisect.bisect_right(ds, d)
        if not i:
            return None
        b = oil[ds[i - 1]]
        iw = bisect.bisect_right(dw, d)
        w = wti[dw[iw - 1]] if iw else b
        return 0.5 * b + 0.5 * w

    return get


# ── 数据源 2：汽油价格网 ────────────────────────────────────────────────────
def official_history(years=None):
    years = years or [2026, 2025, 2024, 2023, 2022, 2021, 2020]

    def producer():
        rec = {}
        for y in years:
            try:
                html = http_get("http://www.qiyoujiage.com/tz/%d.shtml" % y)
            except Exception:
                continue
            for tb in re.findall(r"<table.*?</table>", html, re.S):
                for tr in re.findall(r"<tr.*?</tr>", tb, re.S):
                    cells = [re.sub(r"<[^>]+>", "", c).strip()
                             for c in re.findall(r"<t[hd].*?</t[hd]>", tr, re.S)]
                    if not cells or not re.match(r"\d{4}年\d+月\d+日", cells[0]):
                        continue
                    m = re.match(r"(\d{4})年(\d+)月(\d+)日", cells[0])
                    key = "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
                    val = None
                    if len(cells) > 1:
                        g = cells[1]
                        if "搁浅" in g:
                            val = 0
                        else:
                            mm = re.search(r"([涨跌])\s*(\d+)", g)
                            if mm:
                                val = int(mm.group(2)) * (1 if mm.group(1) == "涨" else -1)
                    if val is not None:
                        rec[key] = val
        return rec

    data, cached = cached_json("official_history.json", 12, producer)
    return data, cached


def site_status():
    def producer():
        info = {}
        try:
            h = http_get("http://www.qiyoujiage.com/92.shtml")
            txt = re.sub(r"<[^>]+>", "", h)
            m = re.search(r"今日92号汽油是(\d{4}年\d+月\d+日)24时[，,]([^，,。]*?)后?的价格", txt)
            if m:
                info["last_adjust_text"] = m.group(1)
                info["last_adjust_desc"] = m.group(2)[:40]
            m2 = re.search(r"新一次92汽油价格调整将在(\d{4}年\d+月\d+日)24时进行", txt)
            if m2:
                info["next_window_text"] = m2.group(1)
            prices = {}
            for tb in re.findall(r"<table.*?</table>", h, re.S)[:1]:
                for tr in re.findall(r"<tr.*?</tr>", tb, re.S):
                    c = [re.sub(r"<[^>]+>", "", x).strip()
                         for x in re.findall(r"<t[hd].*?</t[hd]>", tr, re.S)]
                    if len(c) == 2 and re.match(r"^[\u4e00-\u9fa5]{2,4}$", c[0]):
                        try:
                            prices[c[0]] = float(c[1])
                        except ValueError:
                            pass
            info["prices_92"] = prices
        except Exception as e:
            info["_error"] = str(e)
        return info

    data, cached = cached_json("site_status.json", 3, producer)
    return data, cached


# ── 工作日 / 窗口 ──────────────────────────────────────────────────────────
def is_workday(d):
    try:
        import chinese_calendar as cc
        return cc.is_workday(d)
    except Exception:
        return d.weekday() < 5


def workdays_before(d, n):
    out, cur = [], d - dt.timedelta(days=1)
    while len(out) < n:
        if is_workday(cur):
            out.append(cur)
        cur -= dt.timedelta(days=1)
    return out[::-1]


def next_window(last_adjust, k=PERIOD):
    cur, n = last_adjust, 0
    while n < k:
        cur += dt.timedelta(days=1)
        if is_workday(cur):
            n += 1
    return cur


def parse_cn_date(s):
    m = re.search(r"(\d{4})年(\d+)月(\d+)日", s or "")
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# ── 预测核心 ────────────────────────────────────────────────────────────────
def forecast(window_date):
    """按 10 工作日双周期均价变化算应调幅度。窗口未到时，剩余工作日按最新价延续估算。"""
    oil, wti = sina_daily("OIL"), sina_daily("CL")
    get = blend_close(oil, wti)
    today = dt.date.today()
    latest = get(today)
    cur_all = workdays_before(window_date, PERIOD)
    prev = workdays_before(window_date, 2 * PERIOD)[:PERIOD]
    cur_past = [d for d in cur_all if d <= today]
    if not cur_past:
        raise RuntimeError("本周期尚未开始")
    a = [get(d) for d in cur_past]
    b = [get(d) for d in prev]
    if None in a or None in b:
        raise RuntimeError("原油日线数据不足，无法计算周期均价")
    filled = len(cur_all) - len(cur_past)          # 剩余未走完的工作日
    ca = (sum(a) + latest * filled) / PERIOD        # 现价延续假设
    ca_done = sum(a) / len(a)                       # 已发生部分均价
    cb = sum(b) / PERIOD
    dp = ca - cb
    ypt = K_PER_DOLLAR * dp
    decision = "搁浅" if abs(ypt) < THRESHOLD else ("上调" if ypt > 0 else "下调")
    return {
        "window_date": window_date.isoformat(),
        "days_to_window": (window_date - today).days,
        "workdays_left": max(0, filled),
        "progress": "%d/%d" % (len(cur_past), PERIOD),
        "cur_period": [cur_all[0].isoformat(), cur_all[-1].isoformat()],
        "prev_period": [prev[0].isoformat(), prev[-1].isoformat()],
        "cur_avg_usd": round(ca, 2),
        "cur_avg_done_usd": round(ca_done, 2),
        "latest_usd": round(latest, 2),
        "prev_avg_usd": round(cb, 2),
        "delta_usd": round(dp, 2),
        "pct": round(dp / cb * 100, 2),
        "yuan_per_ton": round(ypt),
        "yuan_per_liter": round(ypt / LITERS_PER_TON, 3),
        "decision": decision,
        "provisional": filled > 0,
        "confidence": "低" if filled >= 6 else ("中" if filled > 0 else "高"),
    }


# ── 回测 ───────────────────────────────────────────────────────────────────
def backtest():
    official, _ = official_history()
    oil, wti = sina_daily("OIL"), sina_daily("CL")
    get = blend_close(oil, wti)
    rows = []
    for k, y in sorted(official.items()):
        d = dt.date.fromisoformat(k)
        if d < dt.date(2020, 7, 1):
            continue
        cur = workdays_before(d, PERIOD)
        prev = workdays_before(d, 2 * PERIOD)[:PERIOD]
        a = [get(x) for x in cur]
        b = [get(x) for x in prev]
        if None in a or None in b:
            continue
        ca, cb = sum(a) / PERIOD, sum(b) / PERIOD
        rows.append((k, ca - cb, y))
    if not rows:
        return {"error": "无回测样本"}
    num = sum(dp * y for _, dp, y in rows)
    den = sum(dp * dp for _, dp, y in rows)
    k = num / den
    mae = sum(abs(y - k * dp) for _, dp, y in rows) / len(rows)
    ok = tot = 0
    for _, dp, y in rows:
        if abs(y) < THRESHOLD:
            continue
        tot += 1
        ok += (dp > 0) == (y > 0)
    return {"samples": len(rows), "k_per_dollar": round(k, 2),
            "mae_yuan_per_ton": round(mae, 1),
            "direction_acc": "%d/%d = %.0f%%" % (ok, tot, ok / tot * 100) if tot else "n/a"}


# ── 组装输出 ───────────────────────────────────────────────────────────────
def parse_amount(desc):
    """从『下调92汽油345元/吨(0.27元/升)』解析吨价幅度。"""
    if not desc:
        return None
    m = re.search(r"(\d+)\s*元/吨", desc)
    if not m:
        return None
    v = int(m.group(1))
    return -v if ("下调" in desc or "跌" in desc) else v


def build():
    official, off_cached = official_history()
    site, site_cached = site_status()
    rt = sina_realtime()
    today = dt.date.today()

    last_date = parse_cn_date(site.get("last_adjust_text")) or (
        dt.date.fromisoformat(max(official)) if official else None)
    last_amt = official.get(last_date.isoformat()) if last_date else None
    if last_amt is None:                      # 官方表缺该年（如站点只有历年页）
        last_amt = parse_amount(site.get("last_adjust_desc"))

    notes = []
    win_site = parse_cn_date(site.get("next_window_text"))
    # 站点滞后判定：它写「下一次窗口」却已经过去 → 那一轮其实已落地
    stale_round = win_site if (win_site and win_site <= today and win_site != last_date) else None

    if stale_round:
        last_date, last_amt = stale_round, None
        win = next_window(stale_round)
        win_source = "自算（站点未更新 %s 那轮）" % stale_round.isoformat()
        notes.append("站点数据滞后：%s 那轮已落地，幅度待官方确认" % stale_round.isoformat())
    else:
        win, win_source = win_site, "站点"
        if last_date:
            calc_win = next_window(last_date)
            if win is None or win != calc_win:
                win_source = "自算（站点口径 %s）" % (win.isoformat() if win else "缺失")
                win = win or calc_win
            else:
                win_source = "站点+自算一致"

    result = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "today": today.isoformat(),
        "realtime": rt,
        "last_adjust": {
            "date": last_date.isoformat() if last_date else None,
            "yuan_per_ton": last_amt,
            "official": last_amt is not None,
            "site_desc": site.get("last_adjust_desc"),
            "site_text_date": site.get("last_adjust_text"),
        },
        "next_window": {"date": win.isoformat() if win else None, "source": win_source},
        "prices": site.get("prices_92", {}),
        "notes": notes,
        "cached": {"official_history": off_cached, "site": site_cached},
    }
    if win:
        try:
            result["forecast"] = forecast(win)
        except Exception as e:
            result["forecast"] = {"error": str(e)}

    # 最近一轮幅度官方未确认时，给模型估算值（明确标注为估算）
    la = result["last_adjust"]
    if la["yuan_per_ton"] is None and last_date:
        try:
            est = forecast(last_date)
            la["model_estimate"] = est["yuan_per_ton"]
            la["model_estimate_pct"] = est["pct"]
        except Exception:
            pass

    warns = []
    f = result.get("forecast") or {}
    # 一致性自检：拿「站点记载的最近一轮」对照模型对那轮的还原，方向相反即报警
    chk_date = parse_cn_date(site.get("last_adjust_text"))
    chk_amt = parse_amount(site.get("last_adjust_desc"))
    if chk_date and chk_amt is not None and abs(chk_amt) >= THRESHOLD:
        try:
            past = forecast(chk_date)
            if (past["yuan_per_ton"] > 0) != (chk_amt > 0):
                warns.append(
                    "数据源冲突：站点记 %s 为 %+d 元/吨，模型按同期原油行情还原为 %+d 元/吨，方向相反。"
                    "→ 本轮预测仅供参考，请人工核对（站点文案陈旧或原油数据异常）"
                    % (chk_date, chk_amt, past["yuan_per_ton"]))
        except Exception:
            pass
    if site.get("_error") or rt.get("_error"):
        warns.append("部分数据源抓取失败：%s" % (site.get("_error") or rt.get("_error")))
    if f.get("provisional"):
        warns.append("窗口还有 %d 个工作日，预测会随行情继续变动。" % f.get("workdays_left", 0))
    result["warnings"] = warns
    return result


def render(r):
    f = r.get("forecast") or {}
    L = ["【国内成品油调价预测】%s" % r["generated_at"]]
    la = r["last_adjust"]
    if la.get("date"):
        if la.get("yuan_per_ton") is not None:
            L.append("上次调价：%s  官方 %+d 元/吨" % (la["date"], la["yuan_per_ton"]))
        else:
            est = la.get("model_estimate")
            tail = ("（模型估算 %+d 元/吨，官方未确认）" % est) if est is not None else "（幅度未知）"
            L.append("上次调价：%s  %s" % (la["date"], tail))
        if la.get("site_desc") and la.get("site_text_date") != la.get("date"):
            L.append("  站点原文：%s %s" % (la.get("site_text_date"), la.get("site_desc")))
    nw = r["next_window"]
    if nw.get("date"):
        L.append("下个窗口：%s 24时（%s）" % (nw["date"], nw["source"]))
    for n in r.get("notes") or []:
        L.append("· " + n)
    if "error" in f:
        L.append("预测失败：%s" % f["error"])
    elif f:
        L.append("")
        L.append("本周期（%s ~ %s）进度 %s，布伦特/WTI 估算均价 %.2f 美元"
                 % (f["cur_period"][0], f["cur_period"][1], f.get("progress", "-"), f["cur_avg_usd"]))
        if f.get("provisional"):
            L.append("  已发生 %s 个工作日均价 %.2f，剩余 %d 天按现价 %.2f 延续估算"
                     % (f["progress"].split("/")[0], f["cur_avg_done_usd"],
                        f["workdays_left"], f["latest_usd"]))
        L.append("上周期（%s ~ %s）均价 %.2f 美元" % (*f["prev_period"], f["prev_avg_usd"]))
        L.append("变化：%+.2f 美元/桶（%+.2f%%）" % (f["delta_usd"], f["pct"]))
        L.append("→ 预测%s：%+d 元/吨，折合 92 号汽油 %+.3f 元/升"
                 % (f["decision"], f["yuan_per_ton"], f["yuan_per_liter"]))
        if f["decision"] == "搁浅":
            L.append("  （幅度不足 50 元/吨，按机制不调整，金额累加到下轮）")
        L.append("  置信度：%s" % f.get("confidence", "-"))
    rt = r.get("realtime") or {}
    if rt and "_error" not in rt:
        L.append("")
        L.append("实时行情：" + "  ".join(
            "%s %.2f" % (OIL_SYMBOLS.get(k, k), v["last"]) for k, v in rt.items() if isinstance(v, dict)))
    if r.get("warnings"):
        L.append("")
        for w in r["warnings"]:
            L.append("⚠ " + w)
    return "\n".join(L)


# ── cron 片段模式 ──────────────────────────────────────────────────────────
def snippet(trigger=(5, 1)):
    """只在调价周期第 5 个工作日、以及窗口前一天输出一段可并入日报的文本。
    其余日子输出空字符串（配合 `if [ -n "$OUT" ]` 使用，零打扰）。
    trigger 是"距窗口还剩几个工作日"，默认 (5, 1) = 周期中点 + 窗口前一天。
    """
    today = dt.date.today()
    sp = cache_path("snippet_state.json")
    try:
        with open(sp, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}

    win = None
    try:
        win = dt.date.fromisoformat(st["window_date"]) if st.get("window_date") else None
    except Exception:
        win = None

    r = None
    if win is None or today > win:          # 窗口过期/首次 → 刷新
        r = build()
        w = r["next_window"]["date"]
        win = dt.date.fromisoformat(w) if w else None
        st["window_date"] = w
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    if win is None:
        return ""

    left, cur = 0, today
    while cur < win:
        cur += dt.timedelta(days=1)
        if is_workday(cur):
            left += 1
    if left not in trigger:
        return ""

    if r is None:
        r = build()
    f = r.get("forecast") or {}
    if not f or "error" in f:
        return ""

    ypt, ypl = f["yuan_per_ton"], f["yuan_per_liter"]
    if abs(ypt) < THRESHOLD:
        head = "搁浅（不足50元/吨，本轮不调）"
    else:
        head = "%s %d 元/吨，92号约 %+.2f 元/升" % (
            "上调" if ypt > 0 else "下调", abs(ypt), ypl)

    kind = "周期中点" if left == max(trigger) else "窗口前一天"
    L = ["⛽ 油价调价（%s · %s）" % (win.isoformat(), kind),
         "下轮窗口：%s 24时" % win.isoformat(),
         "预计" + head,
         "数据完整度：周期 %s，置信度%s" % (f.get("progress", "-"), f.get("confidence", ""))]
    bj = (r.get("prices") or {}).get("北京")
    if bj and abs(ypt) >= THRESHOLD:
        L.append("北京92# 调价后约 %.2f 元/升（现 %.2f）" % (bj + ypl, bj))
    px = ["%s %.2f" % (OIL_SYMBOLS.get(k, k), v["last"])
          for k, v in (r.get("realtime") or {}).items() if isinstance(v, dict)]
    if px:
        L.append("原油：" + " / ".join(px))
    if [w for w in (r.get("warnings") or []) if "数据源冲突" in w]:
        L.append("⚠ 数据源互相矛盾，本轮数字仅供参考")

    st.update({"last_calc": today.isoformat(), "last_kind": kind,
               "direction": f.get("decision")})
    with open(sp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    return "\n".join(L)


if __name__ == "__main__":
    if "--verify" in sys.argv:
        print(json.dumps(backtest(), ensure_ascii=False, indent=2))
    elif "--snippet" in sys.argv:
        out = snippet()
        if out:
            print(out)
    else:
        res = build()
        if "--json" in sys.argv:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(render(res))
