import os
import json
import requests
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ========== Google Sheets 接続 ==========
def get_gspread_client():
    credentials_json_str = os.environ.get("GCP_SERVICE_ACCOUNT_KEY")
    if not credentials_json_str:
        raise RuntimeError("環境変数 GCP_SERVICE_ACCOUNT_KEY が設定されていません。")

    credentials_dict = json.loads(credentials_json_str)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)
    gc = gspread.authorize(credentials)
    return gc


# ========== Yahooニューススクレイピング ==========
def scrape_yahoo_news(keyword: str, limit: int = 50):
    print(f"🔎 Yahooニュース検索開始: {keyword}")

    url = f"https://news.yahoo.co.jp/search?p={keyword}&ei=utf-8"
    headers = {"User-Agent": "Mozilla/5.0"}

    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        print(f"❌ リクエスト失敗: {res.status_code}")
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    articles = []

    no = 1
    for item in soup.select("li a"):
        href = item.get("href")
        title = item.get_text(strip=True)

        # Yahooニュース記事URLのみ対象
        if not href or "news.yahoo.co.jp" not in href:
            continue

        # 引用元や発行日時の取得（検索結果の構造による）
        parent = item.find_parent("li")
        source = parent.select_one("span").get_text(strip=True) if parent and parent.select_one("span") else ""
        pubdate = parent.select_one("time").get_text(strip=True) if parent and parent.select_one("time") else ""

        row = [
            no,         # No.
            title,      # タイトル
            href,       # URL
            source,     # 引用元
            pubdate,    # 発行日時
            "",         # ポジネガ（後で分析用）
            "",         # カテゴリ（後で分類用）
            "",         # 本文（記事詳細を別途取得する必要あり）
            "",         # コメント数（別処理）
            ""          # コメント（別処理）
        ]
        articles.append(row)
        print(f"{no}. {title} ({href})")
        no += 1

        if len(articles) >= limit:
            break

    print(f"✅ {len(articles)} 件取得")
    return articles


# ========== シートへの書き込み ==========
def write_to_sheet(sh, keyword: str, articles: list):
    sheet_name = keyword
    try:
        worksheet = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=sheet_name, rows="2000", cols="10")

    # ヘッダー行
    headers = [
        "No.", "タイトル", "URL", "引用元", "発行日時",
        "ポジネガ", "カテゴリ", "本文", "コメント数", "コメント"
    ]
    worksheet.update("A1:J1", [headers])

    # 記事データ
    if articles:
        worksheet.update(f"A2:J{len(articles)+1}", articles)


# ========== メイン処理 ==========
def main():
    keyword = "トヨタ"  # 固定
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")

    if not spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID が未設定です。")

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)

    articles = scrape_yahoo_news(keyword, limit=50)

    if articles:
        write_to_sheet(sh, keyword, articles)
        print("✅ スプレッドシートに書き込みました。")
    else:
        print("⚠️ 記事が取得できませんでした。")


if __name__ == "__main__":
    main()
