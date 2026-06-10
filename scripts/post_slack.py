#!/usr/bin/env python3
"""data/news.json の最新分（昨日〜今日）を Slack Incoming Webhook で投稿する。

投稿済みURLは data/slack_posted.json に記録し、二重投稿を防ぐ。
SLACK_WEBHOOK_URL が未設定の場合は何もせず正常終了する。
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "news.json"
POSTED_FILE = ROOT / "data" / "slack_posted.json"
SITE_URL = "https://jpn-x.github.io/taisyaku-news/"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_message(items: list[dict]) -> str:
    d = datetime.strptime(items[0]["date"], "%Y-%m-%d")
    lines = [f":newspaper: *日証金 貸借取引ダイジェスト（{d.month}/{d.day}分）*"]
    for it in items:
        s = it.get("summary") or {}
        lines.append(f"\n*<{it['url']}|{esc(it['title'])}>*")
        stocks = s.get("stocks") or []
        if stocks:
            names = "、".join(f'{esc(st["name"])}({st["code"]})' for st in stocks[:6])
            if len(stocks) > 6:
                names += f" ほか{len(stocks) - 6}銘柄"
            lines.append(f"対象: {names}")
        for p in (s.get("points") or [])[:2]:
            p = p if len(p) <= 90 else p[:90] + "…"
            lines.append(f"・{esc(p)}")
    lines.append(f"\n詳細・過去分 → {SITE_URL}")
    return "\n".join(lines)


def main() -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        print("SLACK_WEBHOOK_URL not set - skipping Slack post.")
        return

    items = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    posted = set(json.loads(POSTED_FILE.read_text(encoding="utf-8"))) if POSTED_FILE.exists() else set()

    now = datetime.now(JST)
    targets = {now.strftime("%Y-%m-%d"), (now - timedelta(days=1)).strftime("%Y-%m-%d")}
    new_items = [it for it in items if it["date"] in targets and it["url"] not in posted]
    if not new_items:
        print("No new items to post.")
        return

    new_items.sort(key=lambda x: x["date"])
    payload = json.dumps({"text": build_message(new_items)}).encode()
    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode()
    if body != "ok":
        print(f"Unexpected webhook response: {body}", file=sys.stderr)
        sys.exit(1)

    posted.update(it["url"] for it in new_items)
    POSTED_FILE.write_text(json.dumps(sorted(posted), ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"Posted {len(new_items)} items to Slack.")


if __name__ == "__main__":
    main()
