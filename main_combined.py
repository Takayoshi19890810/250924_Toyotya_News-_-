import os
import json
import time
import requests
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ========== Google Sheets 接続 ==========
def get_gspread_client():
    """GitHub Secrets から認証情報を読み込み、gspread クライアントを返す"""
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
    """Yahooニュース検索結果から記事一覧を取得する"""
    print(f"🔎 Yahooニュース検索開始: {keyword}")

    url = f"https://news.yahoo.co.jp/search?p={keyword}&ei=utf-8"
    headers = {"User-Agent": "Mozilla/5.0"}

    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        print(f"❌ リクエスト失敗: {res.status_code}")
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    articles = []

    for a in soup.select("a.sc-fjdhpX"):
        title = a.get_text(strip=True)
        link = a.get("href")
        if not title or not link:
            continue

        articles.append([title, link])
        if len(articles) >= limit:
            break

    print(f"✅ {len(articles)} 件取得")
    return articles


# ========== シートへの書き込み ==========
def write_to_sheet(sh, keyword: str, articles: list):
    """取得した記事リストをスプレッドシートに書き込む"""
    sheet_name = keyword
    try:
        worksheet = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="5")

    # ヘッダー行
    worksheet.update("A1:B1", [["タイトル", "URL"]])

    # 記事データ
    if articles:
        worksheet.update(f"A2:B{len(articles)+1}", articles)


# ========== メイン処理 ==========
def main():
    keyword = os.environ.get("KEYWORD", "日産")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")

    if not spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID が未設定です。")

    # gspread クライアント準備
    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)

    # Yahooニュースを取得
    articles = scrape_yahoo_news(keyword, limit=50)

    # 書き込み
    write_to_sheet(sh, keyword, articles)
    print("✅ 完了しました。")


if __name__ == "__main__":
    main()
