import os
import re
import json
import time
import requests
from typing import List, Tuple
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By


# ==========================
# Google Sheets 接続
# ==========================
def get_gspread_client():
    key_str = os.environ.get("GCP_SERVICE_ACCOUNT_KEY")
    if not key_str:
        raise RuntimeError("環境変数 GCP_SERVICE_ACCOUNT_KEY が設定されていません。")
    creds_dict = json.loads(key_str)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)


# ==========================
# Chromium/Driver のパス解決（環境差吸収）
# ==========================
def resolve_chrome_paths() -> Tuple[str, str]:
    # chromium / chromium-browser / google-chrome の順に探索
    chrome_candidates = [
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome",
    ]
    chrome_binary = next((p for p in chrome_candidates if os.path.exists(p)), None)
    if not chrome_binary:
        raise RuntimeError("Chrome/Chromium 実行ファイルが見つかりませんでした。")

    # chromedriver は一般にここ
    driver_candidates = ["/usr/bin/chromedriver", "/snap/bin/chromium.chromedriver"]
    driver_binary = next((p for p in driver_candidates if os.path.exists(p)), None)
    if not driver_binary:
        raise RuntimeError("chromedriver が見つかりませんでした。")

    return chrome_binary, driver_binary


def create_driver():
    chrome_binary, driver_binary = resolve_chrome_paths()
    chrome_options = Options()
    chrome_options.binary_location = chrome_binary
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--lang=ja-JP")
    chrome_options.add_argument("--window-size=1280,1800")

    service = Service(driver_binary)
    return webdriver.Chrome(service=service, options=chrome_options)


# ==========================
# HTTP ユーティリティ（簡易リトライ）
# ==========================
UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/117.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

def get_with_retry(url: str, headers=None, params=None, tries=3, sleep=1.0):
    headers = headers or UA_HEADERS
    for i in range(tries):
        r = requests.get(url, headers=headers, params=params, timeout=20)
        if r.status_code == 200:
            return r
        time.sleep(sleep)
    return r  # 最後のレスポンスを返す


# ==========================
# コメント全件取得（cursor でページング）
# ==========================
ARTICLE_ID_RE = re.compile(r"/articles/([0-9A-Za-z]+)")

def fetch_all_comments(article_url: str) -> List[str]:
    m = ARTICLE_ID_RE.search(article_url)
    if not m:
        return []
    article_id = m.group(1)
    base = f"https://news.yahoo.co.jp/comment/plugin/v1/full/{article_id}"

    comments: List[str] = []
    cursor = None

    while True:
        params = {"sort": "time", "count": 100}
        if cursor:
            params["cursor"] = cursor

        res = get_with_retry(base, headers=UA_HEADERS, params=params)
        if res.status_code != 200:
            break

        try:
            data = res.json()
            result = data.get("result", {})
            batch = [c.get("comment", "") for c in result.get("comments", [])]
            comments.extend(batch)
            cursor = result.get("next")
            if not cursor:
                break
        except Exception:
            break

    return comments


# ==========================
# 記事本文・発行日時・引用元
# ==========================
def fetch_article_details(url: str):
    res = get_with_retry(url, headers=UA_HEADERS)
    if res.status_code != 200:
        return "", "", "", []

    soup = BeautifulSoup(res.text, "html.parser")

    # 本文はページ型で複数パターンがあるため候補を順に拾う
    body_sel_candidates = [
        "div.article_body p",
        "article p",
        "div.yjS p",
        "div.article_body",
    ]
    body_texts = []
    for sel in body_sel_candidates:
        nodes = soup.select(sel)
        if nodes:
            body_texts = [n.get_text(strip=True) for n in nodes]
            break
    body = " ".join([t for t in body_texts if t])

    # 引用元・発行日時候補
    source = ""
    pubdate = ""
    if soup.select_one("span.source"):
        source = soup.select_one("span.source").get_text(strip=True)
    elif soup.select_one("a[href*='news.yahoo.co.jp/publish']"):
        source = soup.select_one("a[href*='news.yahoo.co.jp/publish']").get_text(strip=True)

    if soup.select_one("time"):
        pubdate = soup.select_one("time").get_text(strip=True)

    comments = fetch_all_comments(url)
    return body, source, pubdate, comments


# ==========================
# 検索結果 すべて取得（ページ送り）
# ==========================
def scrape_all_article_urls(keyword: str) -> List[str]:
    driver = create_driver()
    search_url = f"https://news.yahoo.co.jp/search?p={keyword}&ei=utf-8"
    driver.get(search_url)
    time.sleep(2)

    urls = []
    seen_pages = 0
    while True:
        # そのページのリンクを回収
        links = driver.find_elements(By.CSS_SELECTOR, "a")
        for e in links:
            href = e.get_attribute("href")
            if href and "news.yahoo.co.jp/articles/" in href:
                urls.append(href)

        # 次へ（または「次のページ」）をクリック
        # ページのDOMは変わりやすいので複数候補を試す
        clicked = False
        next_selectors = [
            "a.Pagination__next",
            "a[aria-label='次へ']",
            "a:contains('次へ')",  # JSDOM互換ではないが保険
        ]
        for sel in next_selectors:
            try:
                # CSS :contains は Selenium では使えないため By.LINK_TEXT も試す
                if sel == "a:contains('次へ')":
                    elem = driver.find_element(By.LINK_TEXT, "次へ")
                else:
                    elem = driver.find_element(By.CSS_SELECTOR, sel)
                driver.execute_script("arguments[0].click();", elem)
                time.sleep(1.5)
                clicked = True
                break
            except Exception:
                continue

        seen_pages += 1
        if not clicked or seen_pages > 100:  # 無限ループ防止
            break

    driver.quit()
    # 重複排除
    return list(dict.fromkeys(urls))


# ==========================
# シート書き込み（追記・重複回避）
# ==========================
HEADERS = [
    "タイトル", "URL", "引用元", "発行日時",
    "ポジネガ", "カテゴリ", "本文", "コメント数", "コメント"
]

def ensure_worksheet_with_headers(sh, sheet_name: str):
    try:
        ws = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_name, rows="200000", cols="9")

    # ヘッダ未設定なら設定
    cell_a1 = ws.acell("A1").value
    if not cell_a1:
        ws.update("A1:I1", [HEADERS])
    return ws


def load_existing_url_comment_pairs(ws) -> set:
    """
    既存の (URL, コメント) の組み合わせ集合を取得。
    B列=URL, I列=コメント（2行目以降）を読み込む。
    """
    # B列とI列をまとめて取得（大量データだと時間がかかる点に注意）
    # 行数をまず取得
    last_row = len(ws.col_values(2))  # B列の最終行を基準
    if last_row <= 1:
        return set()

    rng = ws.get(f"B2:B{last_row}")  # URL
    rng2 = ws.get(f"I2:I{last_row}")  # コメント
    existing = set()
    # get は [[val], [val], ...] の形
    for (u_row, c_row) in zip(rng, rng2):
        u = u_row[0] if u_row else ""
        c = c_row[0] if c_row else ""
        if u or c:
            existing.add((u, c))
    return existing


def append_rows_dedup(ws, rows: List[List[str]]):
    """
    既存 (URL, コメント) と重複しない行のみを末尾に追記。
    追記は 5000 行チャンク。
    """
    existing_pairs = load_existing_url_comment_pairs(ws)
    to_append = []
    for r in rows:
        url = r[1] if len(r) > 1 else ""
        comment = r[8] if len(r) > 8 else ""
        pair = (url, comment)
        if pair not in existing_pairs:
            to_append.append(r)

    if not to_append:
        print("追記対象の新規行はありません（すべて重複）。")
        return

    # 追記：append_rows をチャンクで
    CHUNK = 5000
    for i in range(0, len(to_append), CHUNK):
        chunk = to_append[i:i + CHUNK]
        ws.append_rows(chunk, value_input_option="RAW")
        print(f"追記: {i+1}～{i+len(chunk)} 行")


# ==========================
# メイン
# ==========================
def main():
    keyword = "トヨタ"  # 固定
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID が未設定です。")

    # Sheets
    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = ensure_worksheet_with_headers(sh, keyword)

    # 検索 → 全ページ分の記事URL
    print(f"🔎 Yahooニュース検索開始: {keyword}")
    urls = scrape_all_article_urls(keyword)
    print(f"📑 収集URL数: {len(urls)}")

    # 各記事 → 本文 & コメント全件
    rows: List[List[str]] = []
    for idx, href in enumerate(urls, 1):
        body, source, pubdate, comments = fetch_article_details(href)
        comment_count = len(comments)
        title = f"[{keyword}] {href.split('/')[-1]}"  # タイトルは簡易（必要なら記事ページから抽出に変更）

        if comments:
            for c in comments:
                rows.append([
                    title,
                    href,
                    source,
                    pubdate,
                    "",  # ポジネガ
                    "",  # カテゴリ
                    body,
                    comment_count,
                    c
                ])
        else:
            rows.append([
                title, href, source, pubdate, "", "", body, 0, ""
            ])

        print(f"{idx}/{len(urls)}: {href} コメント数={comment_count}")

    # 追記（重複回避）
    append_rows_dedup(ws, rows)
    print("✅ 完了：シートに追記しました。")


if __name__ == "__main__":
    main()
