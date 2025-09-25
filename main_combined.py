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
    comments = []
    cursor = None
    base_url = f"https://news.yahoo.co.jp/comment/plugin/v1/full/{article_id}"

    while True:
        params = {"sort": "time"}
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
            if not cursor:
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
        return "", "", "", [], []

    soup = BeautifulSoup(res.text, "html.parser")

    body = " ".join([p.get_text(strip=True) for p in soup.select("div.article_body p, div.yjS p")])
    source = soup.select_one("span.source").get_text(strip=True) if soup.select_one("span.source") else ""
    pubdate = soup.select_one("time").get_text(strip=True) if soup.select_one("time") else ""

    comments = []
    m = re.search(r"/articles/([0-9a-f]+)", url)
    if m:
        article_id = m.group(1)
        comments = fetch_all_comments(article_id, headers)

    return body, source, pubdate, comments


# ========== Yahooニュース検索 (Selenium利用) ==========
def scrape_yahoo_news(keyword: str, limit: int = 3):
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

    rows = []
    for href in urls[:limit]:
        body, source, pubdate, comments = fetch_article_details(href)
        comment_count = len(comments)

        if comments:
            for c in comments:
                row = [
                    f"[{keyword}] {href.split('/')[-1]}",  # タイトル（簡易）
                    href,
                    source,
                    pubdate,
                    "",  # ポジネガ
                    "",  # カテゴリ
                    body,
                    comment_count,
                    c,  # コメント1件
                ]
                rows.append(row)
        else:
            row = [
                f"[{keyword}] {href.split('/')[-1]}",
                href,
                source,
                pubdate,
                "", "", body, 0, ""
            ]
            rows.append(row)

        print(f"{href} コメント数: {comment_count}")

    print(f"✅ {len(rows)} 行を出力")
    return rows


# ========== シートへの書き込み ==========
def write_to_sheet(sh, keyword: str, rows: list):
    sheet_name = keyword
    try:
        worksheet = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sh.add_worksheet(title=sheet_name, rows="20000", cols="9")

    headers = [
        "タイトル", "URL", "引用元", "発行日時",
        "ポジネガ", "カテゴリ", "本文", "コメント数", "コメント"
    ]
    worksheet.update("A1:I1", [headers])

    if rows:
        worksheet.update(f"A2:I{len(rows)+1}", rows)


# ========== メイン処理 ==========
def main():
    keyword = "トヨタ"
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID が未設定です。")

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)

    rows = scrape_yahoo_news(keyword, limit=3)

    if rows:
        write_to_sheet(sh, keyword, rows)
        print("✅ スプレッドシートに書き込みました。")
    else:
        print("⚠️ データが取得できませんでした。")


if __name__ == "__main__":
    main()
