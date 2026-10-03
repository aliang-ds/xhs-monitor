# -*- coding: utf-8 -*-
"""
选品销量监控 · 单机版
零第三方依赖（只用 Python 标准库），Windows / macOS 都能跑。

命令速查：
  python monitor.py init                       建库 + 在数据目录生成示例监控清单
  python monitor.py add --url <商品链接> --title <标题> --price <客单价> [--shop <店铺>]
  python monitor.py list                       列出当前监控池
  python monitor.py fetch                      采集一次（真实模式，需要登录态）
  python monitor.py fetch --mock               采集一次（模拟模式）
  python monitor.py backfill 8 --start 2026-09-25 --mock    灌历史数据，先跑通逻辑
  python monitor.py report [--day 2026-10-02] [--split p75|median|value:200]
  python monitor.py verify                     自检

数据目录默认 ~/.workbuddy/xhs-monitor-data，可用 --dir 或环境变量 XHS_DATA_DIR 改。
数据目录与脚本目录分离，重装或升级脚本都不会丢监控数据。

每个子命令都支持 --dir。示例：
  python monitor.py init --dir ./data
  python monitor.py report --day 2026-10-02 --dir ./data

铁律：采不到记 NULL，绝不记 0。
"""

import argparse
import csv
import hashlib
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(os.path.expanduser("~"), ".workbuddy", "xhs-monitor-data")
DATA_DIR = os.environ.get("XHS_DATA_DIR") or DEFAULT_DATA

# 判定阈值
BOOM_THRESHOLD = 50      # 爆品值小于它算"小"
MIN_SALES_STRONG = 20    # 日销达到它 + 连续3天增长 = 证据强
MIN_SALES_MID = 10       # 日销达到它 = 证据中

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id       TEXT PRIMARY KEY,
  title    TEXT,
  url      TEXT,
  shop     TEXT,
  price    REAL,
  note     TEXT,
  added_at TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id  TEXT,
  ts          TEXT,
  day         TEXT,
  sold_total  INTEGER,
  price       REAL,
  status      TEXT,
  note        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_snap ON snapshots(product_id, ts);
CREATE INDEX IF NOT EXISTS idx_snap_day ON snapshots(product_id, day);
"""


def set_dir(d):
    global DATA_DIR
    DATA_DIR = os.path.abspath(os.path.expanduser(d))
    os.makedirs(DATA_DIR, exist_ok=True)


def db_path():
    return os.path.join(DATA_DIR, "monitor.db")


def products_csv():
    return os.path.join(DATA_DIR, "products.csv")


def out_dir():
    return os.path.join(DATA_DIR, "out")


def state_path():
    return os.path.join(DATA_DIR, "storage_state.json")


def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(db_path())
    c.executescript(SCHEMA)
    return c


# ---------------------------------------------------------------- 监控清单

SAMPLE_PRODUCTS = [
    # id, 标题, 链接, 店铺, 客单价, 备注, 起始累计, 日销, 日趋势
    ("p01", "小红书运营SOP手册", "https://www.xiaohongshu.com/goods-detail/p01", "运营笔记铺", 9.9, "", 900, 60, 1.12),
    ("p02", "直播话术模板合集", "https://www.xiaohongshu.com/goods-detail/p02", "主播充电站", 19.9, "", 3000, 30, 1.00),
    ("p03", "短视频脚本库", "https://www.xiaohongshu.com/goods-detail/p03", "内容弹药库", 39.0, "", 1500, 120, 1.15),
    ("p04", "爆款标题生成器", "https://www.xiaohongshu.com/goods-detail/p04", "工具小卖部", 1.0, "", 40000, 200, 1.00),
    ("p05", "记账模板", "https://www.xiaohongshu.com/goods-detail/p05", "效率杂货铺", 8.71, "", 2, 1, 1.00),
    ("p06", "娱乐主播互动宝典", "https://www.xiaohongshu.com/goods-detail/p06", "直播研究所", 98.0, "", 6, 1, 1.00),
    ("p07", "小红书封面素材包", "https://www.xiaohongshu.com/goods-detail/p07", "设计便利店", 29.0, "", 8000, 45, 0.98),
    ("p08", "家校沟通话术", "https://www.xiaohongshu.com/goods-detail/p08", "老师的小店", 12.0, "", 120, 8, 1.05),
    ("p09", "AI提示词合集", "https://www.xiaohongshu.com/goods-detail/p09", "AI工具箱", 49.0, "", 2600, 90, 1.10),
    ("p10", "违禁词检测表", "https://www.xiaohongshu.com/goods-detail/p10", "合规小助手", 6.6, "", 500, 15, 1.02),
    ("p11", "探店脚本模板", "https://www.xiaohongshu.com/goods-detail/p11", "探店研究所", 25.0, "", 15000, 3, 0.85),
    ("p12", "冷启动起号日记", "https://www.xiaohongshu.com/goods-detail/p12", "起号记录本", 88.0, "", 400, 22, 1.08),
]

MOCK_PARAMS = {p[0]: (p[6], p[7], p[8]) for p in SAMPLE_PRODUCTS}


def ensure_csv():
    p = products_csv()
    if not os.path.exists(p):
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["id", "title", "url", "shop", "price", "note"])
            for s in SAMPLE_PRODUCTS:
                w.writerow([s[0], s[1], s[2], s[3], s[4], s[5]])
        return True
    return False


def sync_products(c):
    """把 products.csv 里的清单同步进库。CSV 是唯一的事实来源。"""
    n = 0
    with open(products_csv(), encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not row.get("id"):
                continue
            c.execute("INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?)",
                      (row["id"], row["title"], row["url"], row["shop"],
                       float(row["price"] or 0), row.get("note", ""), now_ts()))
            n += 1
    c.commit()
    return n


def cmd_init(args):
    c = conn()
    created = ensure_csv()
    n = sync_products(c)
    print(f"数据目录：{DATA_DIR}")
    print(f"监控清单：{products_csv()}" + ("（刚生成示例 12 个，换成你自己的）" if created else ""))
    print(f"已入库：{n} 个商品")
    print(f"数据库：{db_path()}")


def extract_id(url):
    """从商品链接里取 ID。短链（xhslink.com）取不出来，需要先用浏览器展开成真实链接。"""
    m = re.search(r"goods-detail/([A-Za-z0-9_-]+)", url or "")
    if m:
        return m.group(1)
    if url and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", url.strip()):
        return url.strip()
    return None


def cmd_add(args):
    c = conn()
    ensure_csv()
    pid = extract_id(args.url)
    if not pid:
        print("取不出商品 ID。请给完整的 goods-detail 链接；短链先用浏览器打开，复制跳转后的地址。")
        return
    exists = c.execute("SELECT title FROM products WHERE id=?", (pid,)).fetchone()
    if exists:
        print(f"已在监控池里：{exists[0]}（id={pid}）")
        return
    with open(products_csv(), "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow([pid, args.title or pid, args.url, args.shop or "",
                                args.price or 0, args.note or ""])
    n = sync_products(c)
    print(f"已加入监控池：{args.title or pid}（id={pid}）　当前共 {n} 个")
    print("提醒：新加的商品没有历史基线，今天这一天会标成「不完整」，明天起才算得准。")


def cmd_list(args):
    c = conn()
    c.row_factory = sqlite3.Row
    ensure_csv()
    sync_products(c)
    rows = list(c.execute("SELECT * FROM products ORDER BY id"))
    if not rows:
        print("监控池是空的。用 add 加商品，或先跑 init。")
        return
    print(f"监控池：{len(rows)} 个　（数据目录 {DATA_DIR}）\n")
    print(wpad("ID", 10) + wpad("商品", 24) + wrpad("客单价", 10) + wpad("店铺", 16) + "快照数")
    for r in rows:
        n = c.execute("SELECT COUNT(*) FROM snapshots WHERE product_id=?", (r["id"],)).fetchone()[0]
        print(wpad(r["id"], 10) + wpad((r["title"] or "")[:20], 24)
              + wrpad(f"{r['price']:.2f}", 10) + wpad((r["shop"] or "")[:12], 16) + str(n))


# ---------------------------------------------------------------- 采集层

def fetch_live(product):
    """
    真实采集：起一个真实浏览器打开商品页，从渲染后的 DOM 里读数字。
    不破解 x-s 签名——成本高、对方一改就得重写。

    首次使用：
        pip install playwright
        playwright install chromium
        playwright codegen --save-storage=<数据目录>/storage_state.json https://www.xiaohongshu.com
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "fail", "未安装 playwright：pip install playwright && playwright install chromium"

    sp = state_path()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=sp if os.path.exists(sp) else None)
            page = ctx.new_page()
            page.goto(product["url"], timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            sold = None
            for sel in ["text=/已售/", "[class*=sold]", "[class*=sales]"]:
                try:
                    txt = page.locator(sel).first.inner_text(timeout=2000)
                    digits = "".join(ch for ch in txt if ch.isdigit())
                    if digits:
                        sold = int(digits)
                        break
                except Exception:
                    continue

            price = None
            try:
                ptxt = page.locator("[class*=price]").first.inner_text(timeout=2000)
                price = float("".join(ch for ch in ptxt if ch.isdigit() or ch == "."))
            except Exception:
                price = product.get("price")
            browser.close()

            if sold is None:
                return None, "fail", "没读到销量，登录态可能失效，或选择器要改"
            return {"sold_total": sold, "price": price}, "ok", ""
    except Exception as e:
        return None, "fail", f"{type(e).__name__}: {e}"[:120]


def _noise(key):
    """确定性抖动。抖动只能挂在「天」上，挂在「小时」上会让同一天不同整点的
    历史基数不一致，累计销量就会倒退。"""
    x = int(hashlib.md5(key.encode()).hexdigest()[:8], 16)
    return 0.9 + (x % 200) / 1000.0


def mock_value(pid, day_index, hour):
    """确定性模拟。硬保证累计销量随时间严格单调不减。"""
    if pid not in MOCK_PARAMS:
        return None, "fail", "模拟池里没这个商品（只有示例 12 个商品有模拟数据）"
    base_total, daily_base, trend = MOCK_PARAMS[pid]

    cum = float(base_total)
    for d in range(day_index):
        cum += daily_base * (trend ** d) * _noise(f"{pid}|d{d}")
    today_total = daily_base * (trend ** day_index) * _noise(f"{pid}|d{day_index}")

    hour_w = [0.2, 0.1, 0.1, 0.1, 0.1, 0.2, 0.4, 0.7, 1.0, 1.3, 1.4, 1.3,
              1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 1.9, 2.2, 2.0, 1.2, 0.6]
    upto = sum(hour_w[:hour + 1]) / sum(hour_w)
    cum += today_total * upto

    # 注入真实的坑：p03 第5天连着 3 小时采不到；p07 偶发失败
    if pid == "p03" and day_index == 5 and hour in (14, 15, 16):
        return None, "fail", "网络超时（模拟）"
    if pid == "p07" and (int(hashlib.md5(f"{pid}|d{day_index}|{hour}".encode()).hexdigest()[:8], 16) % 97) == 0:
        return None, "fail", "接口返回不完整（模拟）"

    return {"sold_total": int(cum), "price": None}, "ok", ""


def record(c, product, data, status, note, ts):
    c.execute("INSERT OR REPLACE INTO snapshots (product_id, ts, day, sold_total, price, status, note)"
              " VALUES (?,?,?,?,?,?,?)",
              (product["id"], ts, ts[:10],
               data["sold_total"] if data else None,
               data.get("price") if data else None, status, note))


def load_products(c):
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute("SELECT * FROM products ORDER BY id")]


def cmd_fetch(args):
    c = conn()
    ensure_csv()
    products = load_products(c)
    ts = args.at or now_ts()
    ok = fail = 0
    for p in products:
        if args.mock:
            di = (datetime.strptime(ts[:10], "%Y-%m-%d") - datetime.strptime(args.start, "%Y-%m-%d")).days
            data, status, note = mock_value(p["id"], di, int(ts[11:13]))
        else:
            data, status, note = fetch_live(p)
        if data and data.get("price"):
            p["price"] = data["price"]
        record(c, p, data, status, note, ts)
        if status == "ok":
            ok += 1
        else:
            fail += 1
            print(f"  [失败] {p['title']} -> {note}")
    c.commit()
    print(f"采集完成 {ts}：成功 {ok}，失败 {fail}（失败记 NULL，没记 0）")


def cmd_backfill(args):
    c = conn()
    ensure_csv()
    products = load_products(c)
    start = datetime.strptime(args.start, "%Y-%m-%d")
    n = 0
    for d in range(args.days):
        day = start + timedelta(days=d)
        if day.date() > datetime.now().date():
            break
        for h in range(24):
            ts = day.strftime("%Y-%m-%d") + f" {h:02d}:00:00"
            if ts > now_ts():
                break
            for p in products:
                data, status, note = mock_value(p["id"], d, h)
                record(c, p, data, status, note, ts)
                n += 1
    c.commit()
    print(f"已回填 {n} 条快照（模拟数据）→ 现在跑 report 看看板")


# ---------------------------------------------------------------- 计算层

def day_sales(c, pid, day):
    rows = list(c.execute("SELECT ts, sold_total FROM snapshots WHERE product_id=? AND day=?"
                          " AND status='ok' ORDER BY ts", (pid, day)))
    if len(rows) < 2:
        return None, False, None
    first_h = int(rows[0]["ts"][11:13])
    return max(rows[-1]["sold_total"] - rows[0]["sold_total"], 0), first_h <= 1, first_h


def series_days(c, pid, days, end_day):
    out = []
    for i in range(days):
        d = (datetime.strptime(end_day, "%Y-%m-%d") - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        s, complete, _ = day_sales(c, pid, d)
        out.append({"day": d, "sales": s, "complete": complete})
    return out


def compute(c, product, end_day, lookback=7):
    pid = product["id"]
    rows = list(c.execute("SELECT ts, sold_total, price FROM snapshots WHERE product_id=?"
                          " AND status='ok' ORDER BY ts DESC LIMIT 1", (pid,)))
    if not rows:
        return None
    latest = rows[0]
    sold_total = latest["sold_total"]

    today, t_complete, _ = day_sales(c, pid, end_day)
    yday = (datetime.strptime(end_day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    y_sales, _, _ = day_sales(c, pid, yday)
    series = series_days(c, pid, lookback, end_day)

    streak = 0
    vals = [s["sales"] for s in series if s["sales"] is not None]
    if vals and vals[-1] is not None:
        for i in range(len(vals) - 1, 0, -1):
            if vals[i - 1] is not None and vals[i] > vals[i - 1]:
                streak += 1
            else:
                break

    boom = value = None
    if today and today > 0 and sold_total:
        boom = sold_total / today
        price = product["price"] or latest["price"] or 0
        value = price * today * (today / sold_total)

    return {"id": pid, "title": product["title"], "shop": product["shop"],
            "price": product["price"] or latest["price"] or 0,
            "sold_total": sold_total, "today": today, "y_sales": y_sales,
            "today_complete": t_complete, "boom": boom, "value": value,
            "streak": streak, "series": series, "quadrant": None, "evidence": None,
            "valid_days": sum(1 for s in series if s["sales"] is not None and s["complete"])}


def value_line(items, split="p75"):
    """商品价值是重尾分布（头尾常差三个数量级），中位数会把一批低价值品划进「高」区。"""
    vals = sorted(i["value"] for i in items if i["value"])
    if not vals:
        return 0.0
    if split == "median":
        return vals[len(vals) // 2]
    if split.startswith("value:"):
        try:
            return float(split.split(":", 1)[1])
        except ValueError:
            pass
    return vals[min(len(vals) - 1, int(len(vals) * 0.75))]


def classify(items, split="p75"):
    mid = value_line(items, split)
    for i in items:
        if i["boom"] is None or i["value"] is None:
            i["quadrant"] = "—"
        else:
            small = i["boom"] < BOOM_THRESHOLD
            high = i["value"] >= mid
            i["quadrant"] = ("①" if high else "②") if small else ("③" if high else "④")
        t = i["today"] or 0
        if not i["today_complete"]:
            i["evidence"] = "不完整"
        elif t >= MIN_SALES_STRONG and i["streak"] >= 3:
            i["evidence"] = "强"
        elif t >= MIN_SALES_MID:
            i["evidence"] = "中"
        else:
            i["evidence"] = "弱"
    return mid


# ---------------------------------------------------------------- 输出层

def sparkline(series, w=110, h=26):
    vals = [s["sales"] if s["sales"] is not None else None for s in series]
    nums = [v for v in vals if v is not None]
    mx = max(nums) if nums else 1
    mx = max(mx, 1)
    out, seg, prev_i = [], [], None
    for i, v in enumerate(vals):
        if v is None:
            if seg:
                out.append(seg); seg = []
        else:
            if prev_i is not None and i - prev_i > 1:
                out.append(seg); seg = []
            x = w * i / max(len(vals) - 1, 1)
            seg.append((x, h - (v / mx) * (h - 4) - 2))
        prev_i = i
    if seg:
        out.append(seg)
    paths = "".join('<polyline fill="none" stroke="#c2410c" stroke-width="1.6" points="%s"/>'
                    % " ".join(f"{x:.1f},{y:.1f}" for x, y in s) for s in out if len(s) > 1)
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.4" fill="#c2410c"/>' for s in out for x, y in s)
    return f'<svg width="{w}" height="{h}" style="vertical-align:middle">{paths}{dots}</svg>'


def fmt(v, nd=1):
    return "—" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def _w(s):
    """显示宽度：中文算 2 宽，终端对齐才不会歪。"""
    return sum(2 if ord(c) > 127 else 1 for c in str(s))


def wpad(s, n):
    """左对齐填充"""
    return str(s) + " " * max(1, n - _w(s))


def wrpad(s, n):
    """右对齐填充"""
    return " " * max(1, n - _w(s)) + str(s)


QUAD_DESC = {
    "①": ("最优先研究", "#fee2e2", "链接新、近期强、客单价或效率也不错"),
    "②": ("继续观察", "#fef3c7", "刚起势但客单价太低，或绝对销量太少"),
    "③": ("学成熟模型", "#dbeafe", "价值在学它怎么包装建信任，不在跟它"),
    "④": ("优先级最低", "#f3f4f6", "累计好看但当前动销弱、客单价也低"),
}


def build_html(items, mid, end_day, split="p75"):
    os.makedirs(out_dir(), exist_ok=True)
    q1 = [i for i in items if i["quadrant"] == "①"]
    fails = sum(1 for i in items if i["today"] is None)

    rows = "".join(f"""<tr>
<td class="t">{i['title']}<div class="s">{i['shop']}</div></td>
<td class="n">¥{i['price']:g}</td><td class="n">{fmt(i['sold_total'],0)}</td>
<td class="n b">{fmt(i['today'],0)}</td><td class="n">{fmt(i['y_sales'],0)}</td>
<td class="n">{fmt(i['boom'])}</td><td class="n">{fmt(i['value'])}</td>
<td class="n">{sparkline(i['series'])}</td><td class="n q">{i['quadrant']}</td>
<td class="n e e{i['evidence']}">{i['evidence']}</td></tr>"""
        for i in sorted(items, key=lambda x: -(x["value"] or 0)))

    quads = ""
    for q in ["①", "②", "③", "④"]:
        name, bg, desc = QUAD_DESC[q]
        grp = [i for i in items if i["quadrant"] == q]
        lis = "".join(f"<li><b>{i['title']}</b>　日销 {fmt(i['today'],0)}　"
                      f"爆品值 {fmt(i['boom'])}　价值 {fmt(i['value'])}</li>"
                      for i in sorted(grp, key=lambda x: -(x["value"] or 0))) or "<li class='empty'>（空）</li>"
        quads += f"""<div class="qbox" style="background:{bg}">
<div class="qh">{q} {name}<span>{len(grp)} 个</span></div>
<div class="qd">{desc}</div><ul>{lis}</ul></div>"""

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>选品监控看板 {end_day}</title><style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;padding:28px;background:#f7f7f8;color:#1a1a1a}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:#6b7280;font-size:13px;margin-bottom:20px}}
.cards{{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}}
.card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:14px 18px;min-width:120px}}
.card .k{{font-size:26px;font-weight:700}} .card .l{{font-size:12px;color:#6b7280;margin-top:2px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:22px}}
.qbox{{border-radius:10px;padding:14px 16px;border:1px solid rgba(0,0,0,.06)}}
.qh{{font-weight:700;font-size:14px;margin-bottom:2px}}
.qh span{{float:right;font-weight:400;font-size:12px;color:#4b5563}}
.qd{{font-size:11.5px;color:#4b5563;margin-bottom:8px}}
.qbox ul{{margin:0;padding-left:18px;font-size:12.5px;line-height:1.9}}
.qbox li.empty{{color:#9ca3af;list-style:none;margin-left:-18px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;font-size:13px}}
th{{background:#f3f4f6;text-align:right;padding:10px 12px;font-weight:600;color:#374151;white-space:nowrap}}
th:nth-child(1){{text-align:left}}
td{{padding:9px 12px;border-top:1px solid #f0f0f1}}
td.n{{text-align:right;white-space:nowrap}}
td.t{{font-weight:600}} td.s{{font-weight:400;color:#9ca3af;font-size:11.5px;margin-top:2px}}
td.b{{font-weight:700;color:#c2410c}} td.q{{font-size:17px}} td.e{{font-size:12px}}
.e强{{color:#b91c1c;font-weight:700}} .e中{{color:#a16207}} .e弱{{color:#9ca3af}} .e不完整{{color:#2563eb}}
.note{{margin-top:22px;background:#fff;border-left:3px solid #c2410c;padding:14px 18px;font-size:13px;line-height:1.8;color:#374151;border-radius:0 8px 8px 0}}
</style></head><body>
<h1>选品销量监控看板</h1>
<div class="sub">数据截止 {end_day}　·　商品价值分界线（{split}）{mid:.1f}　·　爆品值阈值 {BOOM_THRESHOLD}</div>
<div class="cards">
<div class="card"><div class="k">{len(items)}</div><div class="l">监控中</div></div>
<div class="card"><div class="k">{len(q1)}</div><div class="l">① 最优先研究</div></div>
<div class="card"><div class="k">{sum(1 for i in items if i['evidence']=='强')}</div><div class="l">证据强度：强</div></div>
<div class="card"><div class="k">{fails}</div><div class="l">今日没采到（记为 —）</div></div>
</div>
<div class="grid">{quads}</div>
<table><thead><tr><th>商品</th><th>客单价</th><th>累计已售</th><th>今日</th><th>昨日</th>
<th>爆品值</th><th>商品价值</th><th>近7日</th><th>象限</th><th>证据</th></tr></thead>
<tbody>{rows}</tbody></table>
<div class="note">
<b>三条口径，别读错这张表。</b><br>
1. 表里的「—」是<b>这次没采到</b>，不是卖了 0 单。网络超时、页面没渲染出来、接口返回不完整，一律记 NULL。<br>
2. 今日销量 = 今日最后一条有效快照 − 今日<b>第一条</b>有效快照。第一条不在 0–1 点的话标「不完整」，说明程序半路才跑起来，不能跟完整自然日直接比。<br>
3. 爆品值 = 累计已售 ÷ 今日销量，越小越新；商品价值 = 客单价 × 今日销量 ×（今日销量 ÷ 累计已售），越高越好。<br>
<b>但一天 1 单也能算出漂亮的爆品值。</b>所以还要看绝对销量和连续性：连续 3 天以上增长、日销 ≥ {MIN_SALES_STRONG}、且当日数据完整，才算证据「强」。
</div></body></html>"""
    p = os.path.join(out_dir(), "report.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(html)
    return p


def build_md(items, mid, end_day, split="p75"):
    os.makedirs(out_dir(), exist_ok=True)
    lines = [f"# 选品监控日报 {end_day}", "",
             f"- 监控中：{len(items)} 个",
             f"- 四象限①（最优先研究）：{sum(1 for i in items if i['quadrant']=='①')} 个",
             f"- 证据强度「强」：{sum(1 for i in items if i['evidence']=='强')} 个",
             f"- 商品价值分界线（{split}）：{mid:.1f}；爆品值阈值：{BOOM_THRESHOLD}", "",
             "| 商品 | 客单价 | 累计已售 | 今日 | 昨日 | 爆品值 | 商品价值 | 象限 | 证据 |",
             "|---|---:|---:|---:|---:|---:|---:|:-:|:-:|"]
    for i in sorted(items, key=lambda x: -(x["value"] or 0)):
        lines.append(f"| {i['title']} | ¥{i['price']:g} | {fmt(i['sold_total'],0)} | {fmt(i['today'],0)} | "
                     f"{fmt(i['y_sales'],0)} | {fmt(i['boom'])} | {fmt(i['value'])} | {i['quadrant']} | {i['evidence']} |")
    lines += ["", "> 「—」= 这一次没采到，不是 0 单。",
              "> 今日销量 = 今日末条有效快照 − 今日首条有效快照；首条不在 0–1 点的记为「不完整」。"]

    # 给 agent 读的结构化摘要
    strong = [i for i in items if i["quadrant"] == "①"]
    lines += ["", "## 重点", ""]
    if strong:
        for i in sorted(strong, key=lambda x: -(x["value"] or 0)):
            lines.append(f"- **{i['title']}**：日销 {fmt(i['today'],0)}，累计 {fmt(i['sold_total'],0)}，"
                         f"爆品值 {fmt(i['boom'])}，商品价值 {fmt(i['value'])}，证据「{i['evidence']}」，"
                         f"连续增长 {i['streak']} 天")
    else:
        lines.append("- ①象限今天没有商品")
    miss = [i["title"] for i in items if i["today"] is None]
    if miss:
        lines.append(f"- 今天没采到（记为 —，不是 0 单）：{'、'.join(miss)}")
    lines.append(f"- 数据目录：{DATA_DIR}")

    p = os.path.join(out_dir(), "report.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return p


def cmd_report(args):
    c = conn()
    ensure_csv()
    products = load_products(c)
    end_day = args.day or today_str()
    items = [x for x in (compute(c, p, end_day) for p in products) if x]
    if not items:
        print("还没有有效快照。先跑 fetch，或 backfill 8 --start <日期> --mock 灌模拟数据。")
        return
    mid = classify(items, args.split)
    h = build_html(items, mid, end_day, args.split)
    m = build_md(items, mid, end_day, args.split)
    print(f"看板：{h}")
    print(f"日报：{m}\n")
    print(wpad("商品", 22) + wrpad("今日", 6) + wrpad("累计", 9)
          + wrpad("爆品值", 9) + wrpad("商品价值", 11) + "  象限  证据")
    for i in sorted(items, key=lambda x: -(x["value"] or 0)):
        print(wpad(i["title"][:18], 22) + wrpad(fmt(i["today"], 0), 6)
              + wrpad(fmt(i["sold_total"], 0), 9) + wrpad(fmt(i["boom"]), 9)
              + wrpad(fmt(i["value"]), 11) + f"   {i['quadrant']}    {i['evidence']}")


def cmd_verify(args):
    c = conn()
    c.row_factory = sqlite3.Row
    print("=== 1. 采集失败有没有被记成 0 ===")
    fails = list(c.execute("SELECT product_id, ts, note FROM snapshots WHERE status='fail' ORDER BY ts"))
    zero = c.execute("SELECT COUNT(*) FROM snapshots WHERE sold_total=0 AND status='ok'").fetchone()[0]
    print(f"失败快照 {len(fails)} 条；被记成 0 的销售快照 {zero} 条（0 应是真实 0 单，不是失败）")
    for r in fails[:5]:
        print(f"  {r['product_id']} {r['ts']}  {r['note']}")

    print("\n=== 2. 中间采不到时，日销量还算得对吗 ===")
    for pid, in c.execute("SELECT DISTINCT product_id FROM snapshots"):
        bad = list(c.execute("SELECT day FROM snapshots WHERE product_id=? AND status='fail'", (pid,)))
        if not bad:
            continue
        d = bad[0]["day"]
        prev_d = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        next_d = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        trio = []
        for dd in [prev_d, d, next_d]:
            s, cp, _ = day_sales(c, pid, dd)
            trio.append(f"{dd} 日销 {fmt(s,0)}{'' if cp else '(不完整)'}")
        print(f"  {pid} 有断采：{'　'.join(trio)}  ← 三天应接近，断掉那天不应暴涨暴跌")

    print("\n=== 3. 累计销量有没有倒退 ===")
    neg = 0
    for pid, in c.execute("SELECT DISTINCT product_id FROM snapshots"):
        prev = None
        for r in c.execute("SELECT sold_total FROM snapshots WHERE product_id=? AND status='ok' ORDER BY ts", (pid,)):
            if prev is not None and r["sold_total"] < prev:
                neg += 1
            prev = r["sold_total"]
    print(f"  倒退 {neg} 次。" + ("应为 0。" if neg == 0 else
          "不为 0 通常意味着链接改过品或商品串了，去商品页人工确认，别直接改代码。"))


def main():
    ap = argparse.ArgumentParser(description="选品销量监控")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("init", help="建库 + 生成示例清单")
    a = sub.add_parser("add", help="把商品加进监控池")
    a.add_argument("--url", required=True)
    a.add_argument("--title"); a.add_argument("--price", type=float)
    a.add_argument("--shop"); a.add_argument("--note")
    sub.add_parser("list", help="列出监控池")
    f = sub.add_parser("fetch", help="采集一次")
    f.add_argument("--mock", action="store_true"); f.add_argument("--at")
    f.add_argument("--start", default=today_str())
    b = sub.add_parser("backfill", help="灌历史模拟数据")
    b.add_argument("days", type=int)
    b.add_argument("--start", default=(datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))
    r = sub.add_parser("report", help="出看板")
    r.add_argument("--day"); r.add_argument("--split", default="p75")
    sub.add_parser("verify", help="自检")

    for p in sub.choices.values():
        p.add_argument("--dir", help=f"数据目录，默认 {DEFAULT_DATA}")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help(); return
    if getattr(args, "dir", None):
        set_dir(args.dir)
    {"init": cmd_init, "add": cmd_add, "list": cmd_list, "fetch": cmd_fetch,
     "backfill": cmd_backfill, "report": cmd_report, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    main()
