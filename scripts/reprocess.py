"""選定関係エントリ、および section データ付きの seigen エントリを news.json から削除して再処理させる。"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "news.json"
sys.stdout.reconfigure(encoding="utf-8")

data = json.loads(DATA.read_text(encoding="utf-8"))
before = len(data)

def should_reprocess(item: dict) -> bool:
    label = item.get("label", "")
    title = item.get("title", "")
    s = item.get("summary") or {}
    stocks = s.get("stocks") or []

    # 選定関係はすべて再処理（sentei_summary が追加されたため）
    if label == "選定関係" or "貸借取引対象銘柄" in title:
        return True

    # section データ付き seigen（見出しの日付が切れていた可能性）
    has_section = any(st.get("section") for st in stocks)
    if has_section and item.get("date", "") >= "2026-06-11":
        return True

    return False

kept = [item for item in data if not should_reprocess(item)]
removed = before - len(kept)
print(f"Removed {removed} entries (will be reprocessed). Kept {len(kept)}.")

DATA.write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
