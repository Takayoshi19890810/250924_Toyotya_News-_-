import os
import re
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


# ========== Yahooニュース本文・コメント取得 ==========
def fetch_article_details(url: str):
    """記事本文・発行日時・引用元・コメント数・コメント本文を取得"""
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        return "", "", "", 0, []

    soup = BeautifulSoup(res.text, "html.parser")

    # 本文
    body = " ".join([p.get_text(strip=True) for p in soup.select("div.article_body p, div.yjS p")])

    # 引用元・発行日時
    source = soup.select_one("span.source").get_text(strip=True) if soup.select_one("span.source") else ""
    pubdate = soup.select_one("time").get_text(strip=True) if soup.select_one("time") else ""

    # コメント取得
    comments = []
    comment_count = 0
    m = re.search(r"/articles/([0-9a-f]+)", url)
    if m:
        article_id = m.group(1)
        api_url = f"https://news.yahoo.co.jp/comment/plugin/v1/full/{article_id}"
        res_c = requests.get(api_url, headers=headers)
        if res_c.status_code == 200:
            try:
                data = res_c.json()
                comment_count = data.get("result", {}).get("comment_count", 0)
                for c in data.get("result", {}).get("comments", []):
                    comments.append(c.get("comment", ""))
            except Exception:
                pass

    return body, source, pubdate, comment_count, comments


# ========== Yahooニュース検索 ==========
def scrape_yahoo_news(keyword: str, limit: int = 30):
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

    for a in soup.select("a"):
        href = a.get("href")
        title = a.get_text(strip=True)
        if not href or "news.yahoo.co.jp/articles/" not in href:
            continue

        # 詳細情報を取得
        body, source, pubdate, comment_count, comments = fetch_article_details(href)

        row = [
            no,             # No.
            title,          # タイトル
            href,           # URL
            source,         # 引用元
            pubdate,        # 発行日時
            "",             # ポジネガ（後で分析用）
            "",             # カテゴリ（後で分類用）
            body,           # 本文
            comment_count,  # コメント数
            "\n".join(comments[:10])  # コメント（多すぎるので上位10件）
        ]
        articles.append(row)

        print(f"{no}. {title} ({href}) コメント数: {comment_count}")
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

    headers = [
        "No.", "タイトル", "URL", "引用元", "発行日時",
        "ポジネガ", "カテゴリ", "本文", "コメント数", "コメント"
    ]
    worksheet.update("A1:J1", [headers])

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

    articles = scrape_yahoo_news(keyword, limit=20)  # 記事数は20件程度に制限（負荷回避）

    if articles:
        write_to_sheet(sh, keyword, articles)
        print("✅ スプレッドシートに書き込みました。")
    else:
        print("⚠️ 記事が取得できませんでした。")


if __name__ == "__main__":
    main()
