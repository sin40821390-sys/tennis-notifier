"""
テニスコート空き通知（都立浮間公園・北区・板橋区）

3つの予約サイトを確認し、条件に合う空き枠をメールで通知する。
  ・新しく出た空き → 「新しい空き」として場所・日時を記載
  ・以前通知して、まだ空いている枠 → 本文の後半に一覧で記載
新しい空きが1件もない回はメールを送らない。

通知条件（どちらかに当てはまれば対象）
  ケース1: 土日・祝日 → 時間帯は問わない
  ケース2: 開始時刻が EVENING_START_HOUR 時以降の枠（曜日問わず）
"""
import datetime as dt
import json
import os
import smtplib
import sys
import time
import traceback
import unicodedata
from collections import defaultdict
from email.mime.text import MIMEText
from pathlib import Path

import jpholiday
from playwright.sync_api import sync_playwright

# ===== 共通設定 =====
EVENING_START_HOUR = 18   # ケース2: この時刻以降に「開始」する枠を対象にする
STATE_FILE = Path(__file__).with_name("state.json")
WEEKDAYS = "月火水木金土日"
JST = dt.timezone(dt.timedelta(hours=9))

# ===== サイト別設定 =====
TOKYO_URL = "https://kouen.sports.metro.tokyo.lg.jp/web/"
TOKYO_PURPOSE = "1000_1030"   # テニス（人工芝）
TOKYO_PARK = "1100"           # 浮間公園
TOKYO_MAX_WEEKS = 8

KITA_URL = "https://kita-yoyaku.openreaf02.jp/"
KITA_PURPOSE = "003"          # テニス
KITA_FACILITIES = {           # 対象施設（予約サイト上の施設コード）
    "016": "中央公園庭球場",
    "018": "桐ケ丘体育館庭球場",
    "017": "滝野川体育館庭球場",
    "019": "新河岸川庭球場",
    "020": "浮間舟渡庭球場",
}

ITA_URL = "https://www.itabashi-shisetsu-yoyaku.jp/eshisetsu/menu/Welcome.cgi"
ITA_EXCLUDE = "加賀"          # 施設名にこの文字を含む施設は除外
ITA_MAX_PAGES = 3             # 31日表示 × 最大3ページ

SITE_LABEL = {
    "tokyo": "都立 浮間公園",
    "kita": "北区",
    "itabashi": "板橋区",
}
SITE_LINK = {
    "tokyo": TOKYO_URL,
    "kita": KITA_URL,
    "itabashi": ITA_URL,
}


def jst_today() -> dt.date:
    return dt.datetime.now(JST).date()


def hm_to_float(s: str) -> float:
    """'19:00' / '１９時' → 19.0、'18:30' → 18.5"""
    s = unicodedata.normalize("NFKC", s).replace("時", "").strip()
    if ":" in s:
        h, m = s.split(":")
        return int(h) + int(m) / 60
    return float(s)


def slot(site, place, date, start, end, note=""):
    return {"site": site, "place": place, "date": date, "start": start, "end": end, "note": note}


# ---------------------------------------------------------------
# 都立公園（浮間公園）
# ---------------------------------------------------------------
TOKYO_PARSE_JS = """
() => [...document.querySelectorAll('#week-info tbody tr')].flatMap(tr => {
  const label = (tr.querySelector('th')?.innerText || '').trim();
  return [...tr.querySelectorAll('td[id]')].map(td => ({
    id: td.id, label,
    status: td.querySelector('img')?.alt || '',
    count: (td.querySelector('.calendar-availability span')?.innerText || '').trim(),
  }));
})
"""


def fetch_tokyo(page) -> list[dict]:
    page.goto(TOKYO_URL, wait_until="domcontentloaded")
    page.select_option("#purpose-home", TOKYO_PURPOSE)
    page.wait_for_selector(f'#bname-home option[value="{TOKYO_PARK}"]', state="attached")
    page.select_option("#bname-home", TOKYO_PARK)
    with page.expect_navigation():
        page.click("#btn-go")
    page.wait_for_selector("#week-info tbody td[id]", state="attached")

    found = {}
    for week in range(TOKYO_MAX_WEEKS):
        cells = page.evaluate(TOKYO_PARSE_JS)
        for c in cells:
            if c["status"] == "空き":
                found[c["id"]] = c
        if week > 0 and cells and all(c["status"] == "受付期間外" for c in cells):
            break
        if week == TOKYO_MAX_WEEKS - 1:
            break
        first_id = cells[0]["id"] if cells else ""
        page.evaluate("getWeekInfoAjax(4, 0, 0)")  # 「次週>>」
        page.wait_for_function(
            "(prev) => { const td = document.querySelector('#week-info tbody td[id]');"
            " return td && td.id !== prev; }",
            arg=first_id,
        )
        time.sleep(1)

    out = []
    for sid, c in found.items():
        d = dt.datetime.strptime(sid.split("_")[0], "%Y%m%d").date()
        h = hm_to_float(c["label"])
        out.append(slot("tokyo", "浮間公園 テニス（人工芝）", d, h, h + 2,
                        f"空き{c['count']}面" if c["count"] else ""))
    return out


# ---------------------------------------------------------------
# 北区
# ---------------------------------------------------------------
# 時間帯の列は colspan ではなく td の幅で表現されているため、幅から時刻を計算する
KITA_PARSE_JS = """
() => {
  const hdr = [...document.querySelectorAll('table.koma-table')].find(t => t.querySelector('th'));
  if (!hdr) return [];
  const hours = [...hdr.querySelectorAll('th')].map(th => parseInt(th.innerText, 10)).filter(n => !isNaN(n));
  const start = hours[0], n = hours.length;
  const out = [];
  document.querySelectorAll('table.koma-table').forEach(t => {
    const nameTd = t.querySelector('td.name');
    if (!nameTd) return;
    const href = nameTd.querySelector('a')?.getAttribute('href') || '';
    const code = (href.match(/ShisetsuCode=(\\d+)/) || [])[1] || '';
    const tds = [...t.querySelectorAll('td:not(.name)')];
    const ws = tds.map(td => parseFloat(td.style.width) || 0);
    const unit = ws.reduce((a, b) => a + b, 0) / n;
    let acc = 0;
    tds.forEach((td, i) => {
      const s = start + Math.round(acc / unit * 2) / 2;
      acc += ws[i];
      const e = start + Math.round(acc / unit * 2) / 2;
      if (td.innerText.trim() === '○')
        out.push({ code, name: nameTd.innerText.replace(/\\s+/g, ' ').trim(), start: s, end: e });
    });
  });
  return out;
}
"""


def fetch_kita(page) -> list[dict]:
    page.goto(KITA_URL, wait_until="domcontentloaded")
    with page.expect_navigation():
        page.click("a[onclick*=\"gotoPage('sentaku'\"]")       # 空き状況を確認
    with page.expect_navigation():
        page.click("a[onclick*=\"gotoPage('jyouken'\"]")       # 条件を指定して検索
    if page.input_value("select[name=Mokuteki01]") != KITA_PURPOSE:
        with page.expect_navigation():                         # 目的を変えると再読込される
            page.select_option("select[name=Mokuteki01]", KITA_PURPOSE)
    page.select_option("select[name=ShisetsuCode]", "0")       # 一旦全施設を表示し、KITA_FACILITIES で絞り込む
    with page.expect_navigation():
        page.click("input[name=searchBtn]")

    # 今日から翌月末まで（北区の受付期間）を1日ずつ確認
    today = jst_today()
    first_next = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    last = (first_next + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)

    out = []
    d = today
    while d <= last:
        page.goto(f"{KITA_URL}index.php?op=kensaku_koma&UseDate={d:%Y%m%d}",
                  wait_until="domcontentloaded")
        for c in page.evaluate(KITA_PARSE_JS):
            if c["code"] not in KITA_FACILITIES:
                continue
            out.append(slot("kita", c["name"], d, c["start"], c["end"]))
        d += dt.timedelta(days=1)
        time.sleep(0.5)
    return out


# ---------------------------------------------------------------
# 板橋区
# ---------------------------------------------------------------
ITA_PARSE_JS = """
() => {
  const tbl = [...document.querySelectorAll('table')]
    .find(t => t.querySelector('td.facmdstime') && !t.querySelector('table'));
  if (!tbl) return null;
  let y = parseInt(document.querySelector('select[name=optYear]').value, 10);
  let lastM = parseInt(document.querySelector('select[name=optMonth]').value, 10);
  const out = []; let dates = []; let fac = '';
  for (const tr of tbl.querySelectorAll('tr')) {
    const days = [...tr.querySelectorAll('th p.day')];
    if (days.length) {
      let yy = y, lm = lastM;
      dates = days.map(p => {
        const m = p.innerText.match(/(\\d+)月\\s*(\\d+)日/);
        const mo = +m[1], d = +m[2];
        if (mo < lm) yy += 1;
        lm = mo;
        return `${yy}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      });
      continue;
    }
    const f = tr.querySelector('td.facmds');
    if (f) fac = f.innerText.replace(/\\s+/g, ' ').trim();
    const t = tr.querySelector('td.facmdstime');
    if (!t) continue;
    const time = t.innerText.replace(/\\s+/g, '');
    const cells = [...tr.querySelectorAll('td')].filter(td => td !== t && !td.classList.contains('facmds'));
    cells.forEach((td, i) => {
      if ((td.querySelector('img')?.alt || '') === '空いています')
        out.push({ name: fac, date: dates[i], time });
    });
  }
  return out;
}
"""


def fetch_itabashi(page) -> list[dict]:
    page.goto(ITA_URL, wait_until="domcontentloaded")
    with page.expect_navigation():
        page.click("a[onclick*=\"isSubmitDataSet('Guest')\"]")                       # ゲストで入る
    with page.expect_navigation():
        page.click("a[onclick*=\"oneClickSubmit('txtFunctionCode','Enter','Yoyaku')\"]")  # 空き照会・予約
    with page.expect_navigation():
        page.click("tr[onclick*=\"'txtSelectKey','Enter','0'\"]")                    # 利用目的から絞り込む
    with page.expect_navigation():
        page.click("tr[onclick*=\"'E00'\"]")                                         # スポーツ（屋外）
    with page.expect_navigation():
        page.click("tr[onclick*=\"'E06'\"]")                                         # 硬式テニス

    rows = page.locator('tr[onclick*="chng_chkboxImage"]')
    for i in range(rows.count()):
        if ITA_EXCLUDE not in rows.nth(i).inner_text():
            rows.nth(i).click()                                                      # 施設にチェック
    with page.expect_navigation():
        page.click("a[onclick*=\"isSubmitDataSet('Enter')\"]")                       # 次に進む

    page.click("a[onclick*=\"chng_Calradioshow('month'\"]")                         # 31日表示
    with page.expect_navigation():
        page.click("a[onclick*=\"isSubmitDataSetConfirm('Setup')\"]")                # 表示

    found = {}
    prev_first = None
    for _ in range(ITA_MAX_PAGES):
        cells = page.evaluate(ITA_PARSE_JS) or []
        first = cells[0]["date"] if cells else None
        if first is not None and first == prev_first:
            break                       # これ以上先は表示できない
        prev_first = first
        for c in cells:
            found[(c["name"], c["date"], c["time"])] = c
        nxt = page.locator("a[onclick*=\"isSubmitDataSet('OffsetNext')\"]").first
        if nxt.count() == 0:
            break
        try:
            with page.expect_navigation(timeout=30_000):
                nxt.click()
        except Exception:
            break
        time.sleep(1)

    out = []
    for c in found.values():
        d = dt.date.fromisoformat(c["date"])
        s, e = c["time"].split("～")
        out.append(slot("itabashi", c["name"], d, hm_to_float(s), hm_to_float(e)))
    return out


# ---------------------------------------------------------------
# 共通処理
# ---------------------------------------------------------------
FETCHERS = {"tokyo": fetch_tokyo, "kita": fetch_kita, "itabashi": fetch_itabashi}


def matches(s: dict) -> bool:
    d = s["date"]
    if d < jst_today():
        return False
    return d.weekday() >= 5 or jpholiday.is_holiday(d) or s["start"] >= EVENING_START_HOUR


def key(s: dict) -> str:
    return f"{s['site']}|{s['place']}|{s['date']:%Y%m%d}|{s['start']}"


def fmt_time(h: float) -> str:
    return f"{int(h)}:{int(round((h % 1) * 60)):02d}"


def fmt_date(d: dt.date) -> str:
    hol = jpholiday.is_holiday_name(d)
    return f"{d:%m/%d}({WEEKDAYS[d.weekday()]}{'・' + hol if hol else ''})"


def format_section(slots: list[dict]) -> str:
    """サイトごと → 日付・時間ごとにまとめて、同じ枠のコートは1行に並べる"""
    if not slots:
        return "  （なし）\n"
    lines = []
    for site in FETCHERS:
        group = [s for s in slots if s["site"] == site]
        if not group:
            continue
        lines.append(f"【{SITE_LABEL[site]}】")
        by_time = defaultdict(list)
        for s in group:
            by_time[(s["date"], s["start"], s["end"])].append(
                s["place"] + (f"（{s['note']}）" if s["note"] else ""))
        for (d, st, en), places in sorted(by_time.items()):
            lines.append(f"  {fmt_date(d)} {fmt_time(st)}〜{fmt_time(en)}  {'、'.join(sorted(places))}")
        lines.append("")
    return "\n".join(lines)


def build_mail(new: list[dict], still: list[dict], errors: list[str]) -> str:
    body = f"■ 新しい空き（{len(new)}件）\n\n{format_section(new)}\n"
    body += f"■ 以前通知済みで、まだ空いている枠（{len(still)}件）\n\n{format_section(still)}\n"
    if errors:
        body += "■ 今回確認できなかったサイト\n" + "\n".join(f"  {e}" for e in errors) + "\n\n"
    body += "■ 予約サイト\n" + "\n".join(f"  {SITE_LABEL[k]}: {v}" for k, v in SITE_LINK.items())
    body += f"\n\n通知条件: 土日祝は全時間帯 / 平日は{EVENING_START_HOUR}時以降に開始する枠\n"
    return body


def send_mail(subject: str, body: str) -> None:
    user = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("MAIL_TO") or user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, password)
        s.send_message(msg)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    data = json.loads(STATE_FILE.read_text())
    return data if isinstance(data, dict) else {}   # 旧形式（リスト）は破棄


def main() -> int:
    state = load_state()                 # {site: [通知済みキー, ...]}
    new_state, targets, errors = {}, [], []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for site, fetch in FETCHERS.items():
            result = None
            for attempt in range(3):     # サイトが時々エラーを返すので最大3回
                ctx = browser.new_context(locale="ja-JP")
                page = ctx.new_page()
                page.set_default_timeout(60_000)
                page.on("dialog", lambda d: d.accept())
                try:
                    result = fetch(page)
                    break
                except Exception:
                    print(f"[{site}] 取得失敗 ({attempt + 1}/3)", file=sys.stderr)
                    traceback.print_exc()
                    time.sleep(20)
                finally:
                    ctx.close()
            if result is None:
                errors.append(f"{SITE_LABEL[site]}（エラーのため取得できず）")
                new_state[site] = state.get(site, [])   # 前回の記録を引き継ぐ
                continue
            hits = [s for s in result if matches(s)]
            print(f"[{site}] 空き {len(result)} 件 / 条件一致 {len(hits)} 件")
            targets += hits
            new_state[site] = sorted({key(s) for s in hits})
        browser.close()

    notified = {k for keys in state.values() for k in keys}
    new = [s for s in targets if key(s) not in notified]
    still = [s for s in targets if key(s) in notified]
    print(f"新規 {len(new)} 件 / 継続 {len(still)} 件")

    if new:
        body = build_mail(new, still, errors)
        print(body)
        send_mail(f"【テニス空き通知】新しい空き {len(new)}件", body)
        print("メール送信しました")

    STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=1))
    return 1 if len(errors) == len(FETCHERS) else 0


if __name__ == "__main__":
    sys.exit(main())
