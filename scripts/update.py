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


def seigen_summary(text: str, tables: list) -> dict:
    """「銘柄別制限措置」PDF専用パーサー。テキストからセクション見出しを検出して対応付ける。"""
    flat = re.sub(r"\s+", "", text)
    heading_re = re.compile(
        r"(?:"
        r"貸株利用等に関する注意喚起の通知"
        r"|貸株利用等に関する注意喚起の取消"
        r"|融資利用等に関する注意喚起の通知"
        r"|融資利用等に関する注意喚起の取消"
        r"|申込停止措置の実施[（(][^）)]{2,40}[）)]"
        r"|申込停止措置の一部解除[（(][^）)]{2,40}[）)]"
        r"|申込停止措置の解除[（(][^）)]{2,40}[）)]"
        r"|融資停止措置の実施[（(][^）)]{2,40}[）)]"
        r"|融資停止措置の解除[（(][^）)]{2,40}[）)]"
        r")"
    )
    headings = [m.group(0) for m in heading_re.finditer(flat)]
    stocks, points = [], []
    for i, table in enumerate(tables):
        rows = _table_rows(table) or []
        heading = headings[i] if i < len(headings) else None
        if not rows:
            if heading:
                points.append(f"{heading}: 該当なし")
            continue
        if heading:
            brief = "、".join(f"{n}（{c}）" for c, n, _ in rows[:4])
            if len(rows) > 4:
                brief += f" ほか{len(rows) - 4}銘柄"
            points.append(f"{heading}: {brief}")
        for code, name, note in rows:
            st: dict = {"code": code, "name": name, "note": note}
            if heading:
                st["section"] = heading
            stocks.append(st)
    return {"stocks": stocks, "points": points, "method": "rule"}


def rule_based_summary(title: str, text: str, tables: list) -> dict:
    """APIキーなしでも動くフォールバック要約。"""
    if "増担保金徴収措置の実施等" in title and len(tables) >= 3:
        return mashitanpo_summary(text, tables)
    if "銘柄別制限措置" in title and tables:
        return seigen_summary(text, tables)

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


# ---------------------------------------------------------------- category

def map_category(title: str, section: str, note: str) -> str:
    """PDFタイトル・セクション見出し・補足から措置カテゴリに分類。"""
    # 増担保金徴収措置専用PDF（noteに区分が入る）
    if "増担保金徴収措置" in title:
        return "増担保解除" if "解除" in (note or "") else "増担保実施"
    t = title or ""
    title_is_release = "解除" in t or "取消" in t
    # セクション見出しがある場合
    s = section or ""
    if s:
        if "解除" in s or "取消" in s:
            return "売り禁解除"
        # 「通知」セクションが解除PDFに含まれる場合は解除扱い
        if "通知" in s and title_is_release:
            return "売り禁解除"
        return "売り禁実施"
    # セクションなし: タイトルで判定（旧形式）
    if title_is_release:
        return "売り禁解除"
    if "実施" in t or "注意喚起" in t:
        return "売り禁実施"
    return "その他"


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
         font-family: "Hiragino Sans", "Yu Gothic UI", "Meiryo", sans-serif; line-height: 1.65;
         caret-color: transparent; }}
  input, textarea, [contenteditable] {{ caret-color: auto; }}

  /* Header */
  header {{ background: linear-gradient(135deg, #0a3d6e, #0a5ca8); color: #fff; padding: 28px 16px; }}
  .wrap {{ max-width: 880px; margin: 0 auto; padding: 0 16px; }}
  .header-top {{ display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }}
  header h1 {{ margin: 0; font-size: 1.45rem; }}
  header p {{ margin: 6px 0 0; opacity: .85; font-size: .85rem; }}
  .reload-btn {{ background: rgba(255,255,255,.18); border: 1px solid rgba(255,255,255,.4);
                color: #fff; border-radius: 7px; padding: 5px 14px; font-size: .82rem;
                cursor: pointer; white-space: nowrap; letter-spacing: .3px; font-family: inherit; }}
  .reload-btn:hover {{ background: rgba(255,255,255,.3); }}

  /* Month tabs (sticky) */
  .month-tabs-wrap {{
    background: #fff;
    border-bottom: 1px solid var(--line);
    position: sticky; top: 0; z-index: 100;
    box-shadow: 0 2px 8px rgba(0,0,0,.07);
  }}
  .month-tabs {{
    max-width: 880px; margin: 0 auto; padding: 0 12px;
    display: flex; gap: 2px; overflow-x: auto; scrollbar-width: none;
  }}
  .month-tabs::-webkit-scrollbar {{ display: none; }}
  .tab {{
    flex-shrink: 0; background: none; border: none;
    border-bottom: 3px solid transparent;
    padding: 10px 13px; font-size: .85rem; cursor: pointer;
    color: var(--sub); white-space: nowrap; font-family: inherit; transition: color .15s;
  }}
  .tab:hover {{ color: var(--accent); }}
  .tab.active {{ color: var(--accent); border-bottom-color: var(--accent); font-weight: 700; }}

  /* Search + Filter Row */
  .search-wrap {{ max-width: 880px; margin: 14px auto 0; padding: 0 16px; }}
  .search-row {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  #codeSearch {{
    width: 200px; padding: 8px 12px;
    border: 1.5px solid var(--line); border-radius: 7px;
    font-size: .9rem; font-family: inherit;
  }}
  #codeSearch:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(10,92,168,.12); }}
  .filter-btn {{
    background: var(--chip); border: 1.5px solid var(--accent); color: var(--accent);
    border-radius: 20px; padding: 6px 14px; font-size: .82rem; cursor: pointer;
    white-space: nowrap; font-family: inherit; transition: background .15s, color .15s;
  }}
  .filter-btn:hover {{ background: #c8ddf5; }}
  .filter-btn.active {{ background: var(--accent); color: #fff; }}
  .clear-btn {{
    background: none; border: 1px solid var(--line); color: var(--sub);
    border-radius: 20px; padding: 6px 12px; font-size: .82rem;
    cursor: pointer; font-family: inherit;
  }}
  .clear-btn:hover {{ background: var(--line); }}

  /* Legend */
  .legend-wrap {{ max-width: 880px; margin: 12px auto 0; padding: 0 16px; }}
  .legend {{
    background: #f8fafc; border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 14px; font-size: .84rem;
  }}
  .legend summary {{ cursor: pointer; font-weight: 600; color: var(--sub); user-select: none; }}
  .legend-body {{ padding: 8px 0 0 8px; line-height: 1.75; }}
  .legend-body p {{ margin: 2px 0; }}
  .legend-note {{ color: var(--sub); font-size: .8rem; margin-top: 8px !important; }}

  /* Result View */
  #resultView {{ margin-bottom: 20px; }}
  .result-head {{
    display: flex; align-items: center; gap: 12px; margin-bottom: 10px; flex-wrap: wrap;
  }}
  .result-title {{ font-weight: 700; font-size: 1rem; }}
  .back-btn {{
    background: none; border: 1px solid var(--line); border-radius: 6px;
    padding: 4px 10px; font-size: .8rem; cursor: pointer; color: var(--sub); font-family: inherit;
  }}
  .back-btn:hover {{ background: var(--line); }}
  .no-result {{ color: var(--sub); font-size: .9rem; padding: 16px 0; }}
  .cat-chip {{
    display: inline-block; border-radius: 4px; padding: 1px 7px;
    font-size: .77rem; font-weight: 600; white-space: nowrap;
  }}
  .cat-mashitan-on {{ background: #fff3cd; color: #7a5800; }}
  .cat-mashitan-off {{ background: #d1ecf1; color: #0c5460; }}
  .cat-urikin-on {{ background: #f8d7da; color: #721c24; }}
  .cat-urikin-off {{ background: #d4edda; color: #155724; }}
  .cat-other {{ background: var(--chip); color: var(--accent); }}

  /* Cards */
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
  .sec-label {{ font-size: .82rem; font-weight: 700; color: var(--accent);
               margin: 10px 0 3px; border-left: 3px solid var(--accent); padding-left: 8px; }}
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
    <div class="header-top">
      <h1>日証金 貸借取引情報ダイジェスト</h1>
      <button class="reload-btn" onclick="location.href=location.pathname+'?v='+Date.now()">&#8635; 強制リロード</button>
    </div>
    <p>日本証券金融のお知らせ（増担保金徴収措置・貸借取引規制など）のPDFを自動要約 ｜ 最終更新: {updated} JST</p>
  </div>
</header>

<!-- 月別タブ（スティッキー） -->
<div class="month-tabs-wrap">
  <div class="month-tabs" id="monthTabs">
    <button class="tab active" data-month="">全期間</button>
    {month_tabs}
  </div>
</div>

<!-- 検索 + フィルターボタン -->
<div class="search-wrap">
  <div class="search-row">
    <input id="codeSearch" type="search" placeholder="銘柄コードで検索（例: 3135）">
    <button class="filter-btn" data-cat="増担保実施">増担保実施</button>
    <button class="filter-btn" data-cat="増担保解除">増担保解除</button>
    <button class="filter-btn" data-cat="売り禁実施">売り禁実施</button>
    <button class="filter-btn" data-cat="売り禁解除">売り禁解除</button>
    <button id="clearBtn" class="clear-btn">✕ クリア</button>
  </div>
</div>

<!-- 措置の対象 凡例 -->
<div class="legend-wrap">
  <details class="legend">
    <summary>（措置の対象）イ・ロ・ハ の説明</summary>
    <div class="legend-body">
      <p><b>イ.</b> 制度信用取引の新規売り（自己の信用売りを含む。）に伴う貸株申込および融資返済申込み</p>
      <p><b>ロ.</b> 制度信用取引による買い（自己の信用買いを含む。以下同じ。）の現引きに伴う融資返済申込みおよび貸株申込み</p>
      <p><b>ハ.</b> 金融商品取引所の立会外取引およびPTSにおける立会外取引に類似する取引を利用した制度信用取引による買いの転売に伴う融資返済申込みおよび貸株申込み</p>
      <p class="legend-note">※ 弁済繰延期限到来分のロ.およびハ.は対象外。東証市場銘柄が対象の場合、PTS市場における貸借取引対象銘柄にも同じ措置が適用されます。</p>
    </div>
  </details>
</div>

<!-- 結果ビュー（コード検索 / フィルター時） -->
<div id="resultView" class="wrap hidden">
  <div class="result-head" id="resultHead"></div>
  <div id="resultBody"></div>
</div>

<!-- カードビュー -->
<main class="wrap" id="list">
{items}
</main>

<footer>
  出典: <a href="https://www.taisyaku.jp/news/" target="_blank" rel="noopener">日本証券金融株式会社 お知らせ</a>
  ｜ 本ページは公開PDFを自動要約した非公式まとめです。正確な内容は必ず原本PDFをご確認ください。
</footer>

<script>
(function() {{
  var ALL_STOCKS = STOCKS_JSON_PLACEHOLDER;

  var CAT_CLASS = {{
    '増担保実施': 'mashitan-on',
    '増担保解除': 'mashitan-off',
    '売り禁実施': 'urikin-on',
    '売り禁解除': 'urikin-off'
  }};

  var activeMonth = '';
  var activeFilter = '';
  var activeCode = '';

  // ---- Month tabs ----
  document.getElementById('monthTabs').addEventListener('click', function(e) {{
    var btn = e.target.closest('.tab');
    if (!btn) return;
    activeMonth = btn.dataset.month;
    document.querySelectorAll('.tab').forEach(function(b) {{
      b.classList.toggle('active', b === btn);
    }});
    clearSearchState();
    applyView();
  }});

  // ---- Code search ----
  document.getElementById('codeSearch').addEventListener('input', function() {{
    activeCode = this.value.trim();
    activeFilter = '';
    document.querySelectorAll('.filter-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    applyView();
  }});

  // ---- Filter buttons ----
  document.querySelectorAll('.filter-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var cat = this.dataset.cat;
      if (activeFilter === cat) {{
        activeFilter = '';
        this.classList.remove('active');
      }} else {{
        activeFilter = cat;
        document.querySelectorAll('.filter-btn').forEach(function(b) {{ b.classList.remove('active'); }});
        this.classList.add('active');
        activeCode = '';
        document.getElementById('codeSearch').value = '';
      }}
      applyView();
    }});
  }});

  // ---- Clear button ----
  document.getElementById('clearBtn').addEventListener('click', function() {{
    clearSearchState();
    applyView();
  }});

  function clearSearchState() {{
    activeFilter = '';
    activeCode = '';
    document.getElementById('codeSearch').value = '';
    document.querySelectorAll('.filter-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  }}

  function applyView() {{
    if (activeFilter) {{
      showFilterResult(activeFilter);
    }} else if (activeCode.length >= 2) {{
      showCodeResult(activeCode);
    }} else {{
      showCardView();
    }}
  }}

  function showCardView() {{
    document.getElementById('resultView').classList.add('hidden');
    document.getElementById('list').classList.remove('hidden');
    document.querySelectorAll('.card').forEach(function(c) {{
      c.classList.toggle('hidden', !!activeMonth && c.dataset.month !== activeMonth);
    }});
    document.querySelectorAll('.day').forEach(function(d) {{
      d.classList.toggle('hidden', !!activeMonth && d.dataset.month !== activeMonth);
    }});
  }}

  function showCodeResult(code) {{
    var q = code.toLowerCase();
    var results = ALL_STOCKS.filter(function(s) {{
      return (s.code && s.code.toLowerCase().indexOf(q) === 0) ||
             (s.name && s.name.indexOf(code) !== -1);
    }});
    if (!results.length) {{
      renderResult('コード「' + esc(code) + '」の規制履歴', '<p class="no-result">該当するデータがありません</p>');
      return;
    }}
    var rows = results.map(function(s) {{
      var cc = CAT_CLASS[s.category] || 'other';
      return '<tr>' +
        '<td style="white-space:nowrap">' + esc(s.date) + '</td>' +
        '<td class="code">' + esc(s.code) + '</td>' +
        '<td>' + esc(s.name) + '</td>' +
        '<td><span class="cat-chip cat-' + cc + '">' + esc(s.category) + '</span></td>' +
        '<td style="font-size:.8rem;color:#5b6b7f">' + esc(s.section || '') + '</td>' +
        '<td style="font-size:.8rem">' + esc(s.note || '') + '</td>' +
        '<td><a href="' + esc(s.docUrl) + '" target="_blank" rel="noopener">PDF</a></td>' +
        '</tr>';
    }}).join('');
    renderResult(
      'コード「' + esc(code) + '」の規制履歴（' + results.length + '件）',
      '<table><tr><th>日付</th><th>コード</th><th>銘柄名</th><th>区分</th><th>セクション</th><th>補足</th><th>原本</th></tr>' + rows + '</table>'
    );
  }}

  function showFilterResult(cat) {{
    var results = ALL_STOCKS.filter(function(s) {{ return s.category === cat; }});
    if (!results.length) {{
      renderResult(esc(cat) + ' 一覧', '<p class="no-result">該当するデータがありません</p>');
      return;
    }}
    var rows = results.map(function(s) {{
      return '<tr>' +
        '<td style="white-space:nowrap">' + esc(s.date) + '</td>' +
        '<td class="code">' + esc(s.code) + '</td>' +
        '<td>' + esc(s.name) + '</td>' +
        '<td style="font-size:.8rem;color:#5b6b7f">' + esc(s.section || '') + '</td>' +
        '<td style="font-size:.8rem">' + esc(s.note || '') + '</td>' +
        '<td><a href="' + esc(s.docUrl) + '" target="_blank" rel="noopener">PDF</a></td>' +
        '</tr>';
    }}).join('');
    renderResult(
      esc(cat) + ' 一覧（' + results.length + '件）',
      '<table><tr><th>日付</th><th>コード</th><th>銘柄名</th><th>セクション</th><th>補足</th><th>原本</th></tr>' + rows + '</table>'
    );
  }}

  function renderResult(title, bodyHtml) {{
    document.getElementById('list').classList.add('hidden');
    var rv = document.getElementById('resultView');
    rv.classList.remove('hidden');
    document.getElementById('resultHead').innerHTML =
      '<span class="result-title">' + title + '</span>' +
      '<button class="back-btn" id="backBtn">← 戻る</button>';
    document.getElementById('backBtn').addEventListener('click', function() {{
      clearSearchState();
      applyView();
    }});
    document.getElementById('resultBody').innerHTML = bodyHtml;
  }}

  function esc(s) {{
    return String(s || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }}
}})();
</script>
</body>
</html>
"""


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def render_item(item: dict, latest: bool = False) -> str:
    s = item.get("summary") or {}
    month = (item.get("date", "") or "")[:7]
    parts = [f'<div class="card{" latest" if latest else ""}" data-month="{month}">']
    parts.append(f'<h3><a href="{esc(item["url"])}" target="_blank" rel="noopener">{esc(item["title"])}</a></h3>')
    meta = f'<span class="chip">{esc(item.get("label") or "お知らせ")}</span>'
    meta += f'<a class="pdf-link" href="{esc(item["url"])}" target="_blank" rel="noopener">原本PDF</a>'
    if item.get("file_size"):
        meta += f' （{esc(item["file_size"])}）'
    parts.append(f'<div class="meta">{meta}</div>')
    stocks = s.get("stocks") or []
    if stocks:
        if any(st.get("section") for st in stocks):
            # セクション見出し付きでグループ化
            cur_sec, cur_rows, groups = None, [], []
            for st in stocks:
                sec = st.get("section") or ""
                if sec != cur_sec:
                    if cur_rows:
                        groups.append((cur_sec, cur_rows))
                    cur_sec, cur_rows = sec, []
                cur_rows.append(st)
            if cur_rows:
                groups.append((cur_sec, cur_rows))
            for sec_name, sec_stocks in groups:
                if sec_name:
                    parts.append(f'<div class="sec-label">{esc(sec_name)}</div>')
                rows = "".join(
                    f'<tr><td class="code">{esc(st.get("code",""))}</td>'
                    f'<td>{esc(st.get("name",""))}</td>'
                    f'<td>{esc(st.get("note",""))}</td></tr>'
                    for st in sec_stocks)
                parts.append(f'<table><tr><th>コード</th><th>銘柄名</th><th>補足</th></tr>{rows}</table>')
        else:
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

    # 月別タブ（出現順）
    seen_months: list[str] = []
    month_set: set[str] = set()
    for item in items:
        ym = (item.get("date", "") or "")[:7]
        if ym and ym not in month_set:
            month_set.add(ym)
            seen_months.append(ym)
    month_tabs = "\n    ".join(
        f'<button class="tab" data-month="{m}">{m.replace("-", "/")}</button>'
        for m in seen_months
    )

    # ALL_STOCKS JSON（コード検索・フィルター用）
    all_stocks = []
    for item in items:
        s = item.get("summary") or {}
        title = item.get("title", "")
        for st in (s.get("stocks") or []):
            cat = map_category(title, st.get("section", ""), st.get("note", ""))
            all_stocks.append({
                "date": item.get("date", ""),
                "month": (item.get("date", "") or "")[:7],
                "docTitle": title,
                "docUrl": item.get("url", ""),
                "code": st.get("code", ""),
                "name": st.get("name", ""),
                "note": st.get("note", ""),
                "section": st.get("section", ""),
                "category": cat,
            })
    stocks_json = json.dumps(all_stocks, ensure_ascii=False)

    # カードHTML
    chunks, current_date = [], None
    for item in items:
        is_latest = item["date"] == latest_date
        month = (item.get("date", "") or "")[:7]
        if item["date"] != current_date:
            current_date = item["date"]
            try:
                d = datetime.strptime(current_date, "%Y-%m-%d")
                label = f'{d.year}年{d.month:02d}月{d.day:02d}日（{"月火水木金土日"[d.weekday()]}）'
            except ValueError:
                label = current_date
            badge = '<span class="new-badge">NEW</span>' if is_latest else ""
            chunks.append(f'<div class="day" data-month="{month}">{label}{badge}</div>')
        chunks.append(render_item(item, latest=is_latest))

    # JSON はフォーマット後に置換（{} が format() と衝突するのを避ける）
    html = HTML_TEMPLATE.format(
        updated=datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        month_tabs=month_tabs,
        items="\n".join(chunks),
    )
    html = html.replace("STOCKS_JSON_PLACEHOLDER", stocks_json)

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
