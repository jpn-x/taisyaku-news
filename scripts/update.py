#!/usr/bin/env python3
"""日本証券金融（taisyaku.jp）のニュース一覧を取得し、PDFの中身を要約して
data/news.json に蓄積、docs/index.html（GitHub Pages）を生成する。

環境変数:
  ANTHROPIC_API_KEY  あれば Claude API で要約。なければルールベース抽出。
  DAYS_BACK          何日前まで遡って処理するか（デフォルト 30）
"""
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pdfplumber

BASE = "https://www.taisyaku.jp"
NEWS_URL = BASE + "/news/"
ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "news.json"
DOCS_DIR = ROOT / "docs"
HEADERS = {"User-Agent": "Mozilla/5.0 (taisyaku-news-digest; +https://github.com/jpn-x/taisyaku-news)"}
DAYS_BACK = int(os.environ.get("DAYS_BACK", "30"))
JST = timezone(timedelta(hours=9))


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


# ---------------------------------------------------------------- news list

def parse_news_list(html: str) -> list[dict]:
    """ニュース一覧ページの JS 埋め込み JSON.stringify({...}) を抽出する。"""
    entries = []
    parts = html.split("JSON.stringify({")
    for i in range(1, len(parts)):
        body = parts[i].split("})")[0]

        def field(name: str) -> str:
            m = re.search(r'"%s"\s*:\s*"([^"]*)"' % name, body)
            return m.group(1) if m else ""

        # label は JS 変数 eblog_label で渡されるので、直前のチャンクから拾う
        labels = re.findall(r'eblog_label\s*=\s*"([^"]*)"', parts[i - 1])
        url = field("url")
        if not url:
            continue
        entries.append({
            "date": field("datetime"),
            "label": labels[-1] if labels else field("label"),
            "title": field("title"),
            "url": url if url.startswith("http") else BASE + url,
            "file_size": field("file_size"),
        })
    # url で重複排除（ページ内に同じ項目が複数回出現することがある）
    seen, unique = set(), []
    for e in entries:
        if e["url"] not in seen:
            seen.add(e["url"])
            unique.append(e)
    return unique


# ---------------------------------------------------------------- pdf text

def extract_pdf(data: bytes) -> tuple[str, list]:
    texts, tables = [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
            tables.extend(page.extract_tables() or [])
    return "\n".join(texts), tables


# ---------------------------------------------------------------- summarize

CODE_PAT = r"\d{4}|\d{3}[A-Z]|\d{2}[A-Z]\d|\d[A-Z]\d{2}"


def _table_rows(table: list) -> list[tuple[str, str, str]] | None:
    """表から (コード, 銘柄名, 補足) の行を抽出。「該当なし」の表は None。"""
    rows = []
    for row in table:
        cells = [(c or "").replace("\n", "").strip() for c in row]
        if any("該当なし" in c for c in cells):
            return None
        idx = next((j for j, c in enumerate(cells) if re.fullmatch(CODE_PAT, c)), None)
        if idx is None:
            continue
        rest = [c for c in cells[idx + 1:] if c]
        name = rest[0] if rest else ""
        note = " / ".join(rest[1:])
        rows.append((cells[idx], name, note))
    return rows


def mashitanpo_summary(text: str, tables: list) -> dict:
    """「貸借取引銘柄別増担保金徴収措置の実施等について」専用パーサー。
    表は順に 1(1)実施 / 1(2)変更 / 2 解除 に対応する。"""
    sections = ["増担保の新規実施", "増担保の変更", "増担保の解除"]
    flat = re.sub(r"\s+", "", text)
    date_m = re.search(r"解除日（申込日）：([0-9０-９]+年[0-9０-９]+月[0-9０-９]+日)", flat)
    stocks, points = [], []
    for i, table in enumerate(tables[:3]):
        sec = sections[i] if i < len(sections) else "対象銘柄"
        rows = _table_rows(table)
        if rows is None or not rows:
            points.append(f"{sec}: 該当なし")
            continue
        suffix = f"（解除日: {date_m.group(1)}）" if i == 2 and date_m else ""
        points.append(f"{sec}: " + "、".join(f"{n}（{c}）" for c, n, _ in rows) + suffix)
        for code, name, note in rows:
            stocks.append({"code": code, "name": name, "note": f"{note} ｜ {sec}".strip(" ｜")})
    return {"stocks": stocks, "points": points, "method": "rule"}


def rule_based_summary(title: str, text: str, tables: list) -> dict:
    """APIキーなしでも動くフォールバック要約。"""
    if "増担保金徴収措置の実施等" in title and len(tables) >= 3:
        return mashitanpo_summary(text, tables)

    stocks, seen_codes = [], set()

    def add_stock(code: str, name: str, note: str = ""):
        if code and code not in seen_codes:
            seen_codes.add(code)
            stocks.append({"code": code, "name": name.strip(), "note": note})

    for table in tables:
        for code, name, note in (_table_rows(table) or []):
            add_stock(code, name, note)

    # タイトル中の「銘柄名（コード）」
    for m in re.finditer(r"([^\s（(、]+)[（(](%s)[）)]" % CODE_PAT, title):
        add_stock(m.group(2), m.group(1))

    # 重要そうな文を抜粋（改行で分断された文を結合してから分割する）
    flat = re.sub(r"\s+", "", text)
    keywords = ["実施", "解除", "変更", "停止", "措置", "困難", "お願い", "注意",
                "返済", "制限", "逆日歩", "品貸", "規制", "選定", "取消"]
    points, seen_points = [], set()
    for sentence in flat.split("。"):
        s = sentence.strip()
        # 「１．」「（２）」などの見出し・既出文・短すぎ/長すぎは除外
        if re.match(r"^[（(]?[0-9０-９一二三四五六七八九十]+[．.）)]", s):
            continue
        # 文書番号・宛名・挨拶などの定型ヘッダー、括弧の途中で切れた文は除外
        if re.search(r"社発第|貸発第|参加者代表者殿|ご清栄|ご高配|下記のとおりご通知", s):
            continue
        if s.startswith(("）", ")", "※")):
            continue
        if s.endswith("について") or s in seen_points:
            continue
        if 20 <= len(s) <= 180 and any(k in s for k in keywords):
            seen_points.add(s)
            points.append(s + "。")
        if len(points) >= 5:
            break
    return {"stocks": stocks, "points": points, "method": "rule"}


def claude_summary(title: str, text: str) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    prompt = f"""以下は日本証券金融（日証金）が公表した通知PDFの本文です。
個人投資家向けに要点をまとめてください。必ず次のJSONだけを出力してください。

{{
  "stocks": [{{"code": "証券コード", "name": "銘柄名", "note": "市場・担保金率など補足"}}],
  "points": ["要点1", "要点2", ...]
}}

ルール:
- stocks: 対象銘柄をすべて列挙。「該当なし」の区分は含めない。
- points: 措置の種類（実施/変更/解除/注意喚起など）、適用日・解除日、投資家への影響を3〜5項目で簡潔に。
- 専門用語はなるべく平易に。

タイトル: {title}

本文:
{text[:8000]}"""
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        content = result["content"][0]["text"]
        m = re.search(r"\{.*\}", content, re.S)
        parsed = json.loads(m.group(0))
        return {
            "stocks": parsed.get("stocks", []),
            "points": parsed.get("points", []),
            "method": "claude",
        }
    except Exception as exc:  # API失敗時はルールベースに落とす
        print(f"  Claude API failed: {exc}", file=sys.stderr)
        return None


def summarize(title: str, text: str, tables: list) -> dict:
    return claude_summary(title, text) or rule_based_summary(title, text, tables)


# ---------------------------------------------------------------- site

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>日証金 貸借取引情報ダイジェスト</title>
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="favicon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="favicon-192.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta name="theme-color" content="#0a3d6e">
<style>
  :root {{
    --bg: #f4f6f9; --card: #ffffff; --ink: #1a2433; --sub: #5b6b7f;
    --accent: #0a5ca8; --line: #dde4ec; --chip: #e3eefb;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
         font-family: "Hiragino Sans", "Yu Gothic UI", "Meiryo", sans-serif; line-height: 1.65; }}
  header {{ background: linear-gradient(135deg, #0a3d6e, #0a5ca8); color: #fff; padding: 28px 16px; }}
  .wrap {{ max-width: 880px; margin: 0 auto; padding: 0 16px; }}
  header h1 {{ margin: 0; font-size: 1.45rem; }}
  header p {{ margin: 6px 0 0; opacity: .85; font-size: .85rem; }}
  .toolbar {{ display: flex; gap: 10px; margin: 18px auto; max-width: 880px; padding: 0 16px; }}
  .toolbar input {{ flex: 1; padding: 10px 14px; border: 1px solid var(--line); border-radius: 8px;
                    font-size: .95rem; }}
  .day {{ margin: 26px 0 10px; font-size: 1.05rem; font-weight: 700; color: var(--accent);
          border-left: 4px solid var(--accent); padding-left: 10px; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px;
           padding: 16px 18px; margin: 10px 0; }}
  .card h3 {{ margin: 0 0 4px; font-size: 1rem; }}
  .card h3 a {{ color: var(--ink); text-decoration: none; }}
  .card h3 a:hover {{ color: var(--accent); text-decoration: underline; }}
  .meta {{ font-size: .78rem; color: var(--sub); margin-bottom: 8px; }}
  .chip {{ display: inline-block; background: var(--chip); color: var(--accent);
           border-radius: 999px; padding: 1px 10px; font-size: .75rem; margin-right: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 8px 0; font-size: .88rem; }}
  th, td {{ border: 1px solid var(--line); padding: 5px 10px; text-align: left; }}
  th {{ background: #eef3f9; font-weight: 600; }}
  td.code {{ font-variant-numeric: tabular-nums; font-weight: 700; color: var(--accent); white-space: nowrap; }}
  ul.points {{ margin: 8px 0 0; padding-left: 20px; }}
  ul.points li {{ margin: 3px 0; font-size: .9rem; }}
  .pdf-link {{ font-size: .8rem; }}
  .new-badge {{ display: inline-block; background: #e0322e; color: #fff; font-size: .72rem;
                font-weight: 700; border-radius: 6px; padding: 2px 9px; margin-left: 10px;
                letter-spacing: .8px; vertical-align: 2px; animation: pulse 1.8s ease-in-out infinite; }}
  @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: .5; }} }}
  .card.latest {{ border-left: 4px solid #e0322e; }}
  footer {{ text-align: center; color: var(--sub); font-size: .78rem; padding: 30px 0 40px; }}
  footer a {{ color: var(--accent); }}
  .hidden {{ display: none; }}
</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>日証金 貸借取引情報ダイジェスト</h1>
    <p>日本証券金融のお知らせ（増担保金徴収措置・貸借取引規制など）のPDFを自動要約 ｜ 最終更新: {updated} JST</p>
  </div>
</header>
<div class="toolbar">
  <input id="q" type="search" placeholder="銘柄コード・銘柄名・キーワードで絞り込み（例: 3135）">
</div>
<main class="wrap" id="list">
{items}
</main>
<footer>
  出典: <a href="https://www.taisyaku.jp/news/" target="_blank" rel="noopener">日本証券金融株式会社 お知らせ</a>
  ｜ 本ページは公開PDFを自動要約した非公式まとめです。正確な内容は必ず原本PDFをご確認ください。
</footer>
<script>
document.getElementById('q').addEventListener('input', function () {{
  const q = this.value.trim().toLowerCase();
  document.querySelectorAll('.card').forEach(c => {{
    c.classList.toggle('hidden', q && !c.textContent.toLowerCase().includes(q));
  }});
  document.querySelectorAll('.day').forEach(d => {{
    let el = d.nextElementSibling, visible = false;
    while (el && !el.classList.contains('day')) {{
      if (el.classList.contains('card') && !el.classList.contains('hidden')) visible = true;
      el = el.nextElementSibling;
    }}
    d.classList.toggle('hidden', !visible);
  }});
}});
</script>
</body>
</html>
"""


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def render_item(item: dict, latest: bool = False) -> str:
    s = item.get("summary") or {}
    parts = [f'<div class="card{" latest" if latest else ""}">']
    parts.append(f'<h3><a href="{esc(item["url"])}" target="_blank" rel="noopener">{esc(item["title"])}</a></h3>')
    meta = f'<span class="chip">{esc(item.get("label") or "お知らせ")}</span>'
    meta += f'<a class="pdf-link" href="{esc(item["url"])}" target="_blank" rel="noopener">原本PDF</a>'
    if item.get("file_size"):
        meta += f' （{esc(item["file_size"])}）'
    parts.append(f'<div class="meta">{meta}</div>')
    stocks = s.get("stocks") or []
    if stocks:
        rows = "".join(
            f'<tr><td class="code">{esc(st.get("code", ""))}</td>'
            f'<td>{esc(st.get("name", ""))}</td>'
            f'<td>{esc(st.get("note", ""))}</td></tr>'
            for st in stocks)
        parts.append(f'<table><tr><th>コード</th><th>銘柄名</th><th>補足</th></tr>{rows}</table>')
    points = s.get("points") or []
    if points:
        lis = "".join(f"<li>{esc(p)}</li>" for p in points)
        parts.append(f'<ul class="points">{lis}</ul>')
    if not stocks and not points:
        parts.append('<p class="meta">（要約なし — 原本PDFをご確認ください）</p>')
    parts.append("</div>")
    return "\n".join(parts)


def generate_site(items: list[dict]) -> None:
    items = sorted(items, key=lambda x: x["date"], reverse=True)
    latest_date = items[0]["date"] if items else ""
    chunks, current_date = [], None
    for item in items:
        is_latest = item["date"] == latest_date
        if item["date"] != current_date:
            current_date = item["date"]
            try:
                d = datetime.strptime(current_date, "%Y-%m-%d")
                label = f'{d.year}年{d.month:02d}月{d.day:02d}日（{"月火水木金土日"[d.weekday()]}）'
            except ValueError:
                label = current_date
            badge = '<span class="new-badge">NEW</span>' if is_latest else ""
            chunks.append(f'<div class="day">{label}{badge}</div>')
        chunks.append(render_item(item, latest=is_latest))
    html = HTML_TEMPLATE.format(
        updated=datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        items="\n".join(chunks),
    )
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")


# ---------------------------------------------------------------- main

def main() -> None:
    known = []
    if DATA_FILE.exists():
        known = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    known_urls = {item["url"] for item in known}

    print(f"Fetching {NEWS_URL} ...")
    html = fetch(NEWS_URL).decode("utf-8", "replace")
    entries = parse_news_list(html)
    print(f"  {len(entries)} entries found on page")

    cutoff = (datetime.now(JST) - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    new_items = [e for e in entries
                 if e["date"] >= cutoff and e["url"] not in known_urls
                 and e["url"].lower().endswith(".pdf")]
    print(f"  {len(new_items)} new PDFs to process (since {cutoff})")

    for entry in new_items:
        print(f"  -> {entry['date']} {entry['title']}")
        try:
            pdf_data = fetch(entry["url"])
            text, tables = extract_pdf(pdf_data)
            entry["summary"] = summarize(entry["title"], text, tables)
        except Exception as exc:
            print(f"     failed: {exc}", file=sys.stderr)
            entry["summary"] = {"stocks": [], "points": [], "method": "error"}
        entry["processed_at"] = datetime.now(JST).isoformat(timespec="seconds")
        known.append(entry)

    known.sort(key=lambda x: (x["date"], x.get("processed_at", "")), reverse=True)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(known, ensure_ascii=False, indent=1), encoding="utf-8")

    generate_site(known)
    print(f"Done. total={len(known)} new={len(new_items)}")


if __name__ == "__main__":
    main()
