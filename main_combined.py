import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By


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


# ========== コメント全件取得 ==========
def fetch_all_comments(article_id: str, headers: dict):
    """Yahooニュースのコメントを全件取得"""
    comments = []
    cursor = None
    base_url = f"https://news.yahoo.co.jp/comment/plugin/v1/full/{article_id}"

    while True:
        params = {"sort": "time"}  # 時系列順
        if cursor:
            params["cursor"] = cursor

        res = requests.get(base_url, headers=headers, params=params)
        if res.status_code != 200:
            break

        try:
            data = res.json()
            result = data.get("result", {})
            comments_batch = [c.get("comment", "") for c in result.get("comments", [])]
            comments.extend(comments_batch)

            cursor = result.get("next")
            if not cursor:  # 次ページが無ければ終了
                break
        except Exception:
            break

    return comments


# ========== Yahooニュース本文・コメント取得 ==========
def fetch_article_details(url: str):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/117.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        return "", "", "", 0, []

    soup = BeautifulSoup(res.text, "html.parser")

    # 本文
    body = " ".join([p.get_text(strip=True) for p in soup.select("div.article_body p, div.yjS p")])

    # 引用元・発行日時
    source = soup.select_one("span.source").get_text(strip=True) if soup.select_one("span.source") else ""
    pubdate = soup.select_one("time").get_text(strip=True) if soup.select_one("time") else ""

    # コメント全件取得
    comments = []
    comment_count = 0
    m = re.search(r"/articles/([0-9a-f]+)", url)
    if m:
        article_id = m.group(1)
        comments = fetch_all_comments(article_id, headers)
        comment_count = len(comments)

    return body, source, pubdate, comment_count, comments


# ========== Yahooニュース検索 (Selenium利用) ==========
def scrape_yahoo_news(keyword: str, limit: int = 20):
    print(f"🔎 Yahooニュース検索開始: {keyword}")

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--lang=ja-JP")

    driver = webdriver.Chrome(options=chrome_options)
    search_url = f"https://news.yahoo.co.jp/search?p={keyword}&ei=utf-8"
    driver.get(search_url)
    time.sleep(3)

    elems = driver.find_elements(By.CSS_SELECTOR, "a")
    urls = []
    for e in elems:
        href = e.get_attribute("href")
        if href and "news.yahoo.co.jp/articles/" in href:
            urls.append(href)

    driver.quit()
    urls = list(dict.fromkeys(urls))

    articles = []
    no = 1
    for href in urls[:limit]:
        body, source, pubdate, comment_count, comments = fetch_article_details(href)
        row = [
            no,
            f"[{keyword}] {href.split('/')[-1]}",  # タイトル（簡易版）
            href,
            source,
            pubdate,
            "",  # ポジネガ
            "",  # カテゴリ
            body,
            comment_count,
            "\n".join(comments),  # 全件を1セルに格納（改行区切り）
        ]
        articles.append(row)
        print(f"{no}. {href} コメント数: {comment_count}")
        no += 1

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
    keyword = "トヨタ"
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID が未設定です。")

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)

    articles = scrape_yahoo_news(keyword, limit=5)  # コメント全件取得なので記事数は少なめ推奨

    if articles:
        write_to_sheet(sh, keyword, articles)
        print("✅ スプレッドシートに書き込みました。")
    else:
        print("⚠️ 記事が取得できませんでした。")


if __name__ == "__main__":
    main()
