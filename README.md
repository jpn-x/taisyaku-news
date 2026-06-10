# 日証金 貸借取引情報ダイジェスト

[日本証券金融のお知らせページ](https://www.taisyaku.jp/news/) に日々掲載される
増担保金徴収措置・貸借取引規制などのPDFを自動で読み込み、
コード番号・銘柄名・内容の要点をペライチでまとめる非公式サイト。

公開ページ: https://jpn-x.github.io/taisyaku-news/

## 仕組み

1. GitHub Actions が平日 17:15 / 19:15 / 翌朝 07:15（JST）に起動
2. `scripts/update.py` がニュース一覧をスクレイピングし、新着PDFをダウンロード
3. `pdfplumber` でテキスト・表を抽出し、要約を生成
   - `ANTHROPIC_API_KEY` シークレットが設定されていれば Claude API（Haiku）で要約
   - 未設定でもルールベース抽出（表から銘柄・担保金率、本文から要点文）で動作
4. `data/news.json` に蓄積し、`docs/index.html` を再生成して GitHub Pages で公開

## ローカル実行

```bash
pip install -r requirements.txt
python scripts/update.py            # 直近30日分を処理
DAYS_BACK=90 python scripts/update.py  # 遡る日数を変更
```

## 注意

本サイトは公開PDFを自動要約した非公式まとめです。
売買判断には必ず原本PDF・公式発表をご確認ください。
