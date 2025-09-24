import os
import re
import json
import time
import random
import requests
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import gspread
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager


# ========= 設定 =========
# キーワードは環境変数 KEYWORD を優先。未設定なら "日産"
KEYWORD = os.getenv("KEYWORD", "日産")

# 出力先スプレッドシートID（ニュース一覧＋本文/コメントを同じシートに書きます）
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1RglATeTbLU1SqlfXnNToJqhXLdNoHCdePldioKDQgU8")

# 各ニュースサイトの書込み先ワークシート名
WS_GOOGLE = "Google"
WS_YAHOO = "Yahoo"
WS_MSN   = "MSN"

# 本文は最大10ページ、コメントは最大10ページ分（Yahooニュースのみ対応）
MAX_BODY_PAGES = 10
MAX_COMMENT_PAGES = 10

# 右側に追加する固定ヘッダ群
BODY_HEADERS = [f"本文({i}ページ)" for i in range(1, MAX_BODY_PAGES + 1)]
COMMENT_HEADERS = ["コメント数", "コメント（以降に1件ずつ横並び）"]

# ========= ユーティリティ =========
def format_datetime(dt_obj: datetime) -> str:
    return dt_obj.strftime("%Y/%m/%d %H:%M")

def get_gspread_client():
    # 環境変数 GCP_SERVICE_ACCOUNT_KEY があれば使用。なければ credentials.json を参照
    credentials_json_str = os.environ.get('GCP_SERVICE_ACCOUNT_KEY')
    credentials = json.loads(credentials_json_str) if credentials_json_str else json.load(open('credentials.json', 'r', encoding='utf-8'))
    return gspread.service_account_from_dict(credentials)

def ensure_header(worksheet):
    """左4列の基本ヘッダ＋右側の本文/コメントヘッダが無ければ作る（或いは拡張する）。"""
    base_headers = ["タイトル", "URL", "投稿日", "引用元"]
    current = worksheet.get_all_values()
    if not current:
        # 新規
        worksheet.append_row(base_headers + BODY_HEADERS + COMMENT_HEADERS)
        return

    header = current[0]
    # 左4列が無ければ初期化
    if len(header) < 4 or header[:4] != base_headers:
        worksheet.update('A1', [base_headers + BODY_HEADERS + COMMENT_HEADERS])
        return

    # 右側の本文・コメントヘッダが足りなければ追記（上書き）
    desired = base_headers + BODY_HEADERS + COMMENT_HEADERS
    if header != desired:
        worksheet.update('A1', [desired])

def column_index_to_letter(idx: int) -> str:
    """1-based index -> Excel列名"""
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

def setup_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

# ========= ニュース取得（一覧作成） =========
# 参考: 既存 main.py の実装方針:contentReference[oaicite:4]{index=4}

def get_google_news_with_selenium(keyword: str) -> list[dict]:
    driver = setup_driver()
    url = f"https://news.google.com/search?q={keyword}&hl=ja&gl=JP&ceid=JP:ja"
    driver.get(url)
    time.sleep(5)
    for _ in range(3):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    driver.quit()

    data = []
    articles = soup.find_all("article")
    for article in articles:
        try:
            a_tag = article.select_one("a.JtKRv")
            time_tag = article.select_one("time.hvbAAd")
            source_tag = article.select_one("div.vr1PYe")

            if not a_tag or not time_tag:
                continue

            title = a_tag.text.strip()
            href = a_tag.get("href")
            url = "https://news.google.com" + href[1:] if href and href.startswith("./") else href
            dt_utc = datetime.strptime(time_tag.get("datetime"), "%Y-%m-%dT%H:%M:%SZ")
            pub_date = format_datetime(dt_utc + timedelta(hours=9))
            source = source_tag.text.strip() if source_tag else "N/A"

            if title and url:
                data.append({"タイトル": title, "URL": url, "投稿日": pub_date, "引用元": source})
        except:
            continue
    print(f"✅ Googleニュース件数: {len(data)} 件")
    return data

def get_yahoo_news_with_selenium(keyword: str) -> list[dict]:
    driver = setup_driver()
    search_url = f"https://news.yahoo.co.jp/search?p={keyword}&ei=utf-8&categories=domestic,world,business,it,science,life,local"
    driver.get(search_url)
    time.sleep(5)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    driver.quit()

    articles = soup.find_all("li", class_=re.compile("sc-1u4589e-0"))
    articles_data = []
    for article in articles:
        try:
            title_tag = article.find("div", class_=re.compile("sc-3ls169-0"))
            title = title_tag.text.strip() if title_tag else ""
            link_tag = article.find("a", href=True)
            url = link_tag["href"] if link_tag else ""
            time_tag = article.find("time")
            date_str = time_tag.text.strip() if time_tag else ""
            formatted_date = ""

            if date_str:
                # （例）"2025/09/24 09:30（火）" の(曜日)を除去
                date_str = re.sub(r'\([月火水木金土日]\)', '', date_str).strip()
                try:
                    dt_obj = datetime.strptime(date_str, "%Y/%m/%d %H:%M")
                    formatted_date = format_datetime(dt_obj)
                except:
                    formatted_date = date_str

            # 引用元（レイアウトの変化に強めの代替ロジック）
            source_text = ""
            source_tag = article.find("div", class_="sc-n3vj8g-0 yoLqH")
            if source_tag:
                inner = source_tag.find("div", class_="sc-110wjhy-8 bsEjY")
                if inner and inner.span:
                    candidate = inner.span.text.strip()
                    if not candidate.isdigit():
                        source_text = candidate
            if not source_text or source_text.isdigit():
                alt_spans = article.find_all(["span", "div"], string=True)
                for s in alt_spans:
                    text = s.text.strip()
                    if 2 <= len(text) <= 20 and not text.isdigit() and re.search(r'[ぁ-んァ-ン一-龥A-Za-z]', text):
                        source_text = text
                        break

            if title and url:
                articles_data.append({
                    "タイトル": title,
                    "URL": url,
                    "投稿日": formatted_date if formatted_date else "取得不可",
                    "引用元": source_text or "Yahoo"
                })
        except:
            continue

    print(f"✅ Yahoo!ニュース件数: {len(articles_data)} 件")
    return articles_data

def parse_relative_time_msn(pub_label: str, base_time: datetime) -> str:
    label = pub_label.strip().lower()
    try:
        if "分前" in label or "minute" in label:
            m = re.search(r"(\d+)", label)
            if m:
                dt = base_time - timedelta(minutes=int(m.group(1)))
                return format_datetime(dt)
        elif "時間前" in label or "hour" in label:
            h = re.search(r"(\d+)", label)
            if h:
                dt = base_time - timedelta(hours=int(h.group(1)))
                return format_datetime(dt)
        elif "日前" in label or "day" in label:
            d = re.search(r"(\d+)", label)
            if d:
                dt = base_time - timedelta(days=int(d.group(1)))
                return format_datetime(dt)
        elif re.match(r'\d+月\d+日', label):
            dt = datetime.strptime(f"{base_time.year}年{label}", "%Y年%m月%d日")
            return format_datetime(dt)
        elif re.match(r'\d{4}/\d{1,2}/\d{1,2}', label):
            dt = datetime.strptime(label, "%Y/%m/%d")
            return format_datetime(dt)
        elif re.match(r'\d{1,2}:\d{2}', label):
            t = datetime.strptime(label, "%H:%M").time()
            dt = datetime.combine(base_time.date(), t)
            if dt > base_time:
                dt -= timedelta(days=1)
            return format_datetime(dt)
    except:
        pass
    return "取得不可"

def get_last_modified_datetime(url):
    try:
        response = requests.head(url, timeout=5)
        if 'Last-Modified' in response.headers:
            dt = parsedate_to_datetime(response.headers['Last-Modified'])
            # 念のためJST表記
            jst = dt.astimezone(tz=timedelta(hours=9))
            return format_datetime(jst)
    except:
        pass
    return "取得不可"

def get_msn_news_with_selenium(keyword: str) -> list[dict]:
    now = datetime.utcnow() + timedelta(hours=9)
    driver = setup_driver()
    url = f"https://www.bing.com/news/search?q={keyword}&qft=sortbydate%3d'1'&form=YFNR"
    driver.get(url)
    time.sleep(5)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    driver.quit()

    data = []
    cards = soup.select("div.news-card")
    for card in cards:
        try:
            title = card.get("data-title", "").strip()
            url = card.get("data-url", "").strip()
            source = card.get("data-author", "").strip()
            pub_label = ""
            pub_tag = card.find("span", attrs={"aria-label": True})
            if pub_tag and pub_tag.has_attr("aria-label"):
                pub_label = pub_tag["aria-label"].strip().lower()

            pub_date = parse_relative_time_msn(pub_label, now)
            if pub_date == "取得不可" and url:
                pub_date = get_last_modified_datetime(url)

            if title and url:
                data.append({
                    "タイトル": title,
                    "URL": url,
                    "投稿日": pub_date,
                    "引用元": source if source else "MSN"
                })
        except Exception:
            continue

    print(f"✅ MSNニュース件数: {len(data)} 件")
    return data

def append_news_list(worksheet, articles: list[dict]):
    """重複URLを除外して左4列（タイトル,URL,投稿日,引用元）を追記"""
    ensure_header(worksheet)
    rows = worksheet.get_all_values()
    existing_urls = set()
    if len(rows) > 1:
        for r in rows[1:]:
            if len(r) > 1 and r[1]:
                existing_urls.add(r[1])

    new_rows = []
    for a in articles:
        if a["URL"] not in existing_urls:
            new_rows.append([a["タイトル"], a["URL"], a["投稿日"], a["引用元"]])

    if new_rows:
        worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
        print(f"✅ {len(new_rows)}件を追記しました。")
    else:
        print("⚠️ 追記すべき新しい記事はありません。")

# ========= Yahoo本文・コメント 取得（右側へ追記） =========
# 参考: 既存 scrape_yahoo_news.py の実装方針（本文/コメントの取り方）:contentReference[oaicite:5]{index=5}

def fetch_yahoo_article_body_and_comments(base_url: str) -> tuple[list[str], list[str]]:
    """本文（最大10ページ）とコメント一覧を返す。本文はページごとに1要素。"""
    headers_req = {'User-Agent': 'Mozilla/5.0'}
    article_bodies = []

    # 本文（最大10ページ）
    for page in range(1, MAX_BODY_PAGES + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        try:
            res = requests.get(url, headers=headers_req, timeout=10)
            soup = BeautifulSoup(res.text, 'html.parser')

            article_body_container = soup.find('article')
            if article_body_container:
                body_elements = article_body_container.find_all('p')
                body_text = '\n'.join([p.get_text(strip=True) for p in body_elements])
            else:
                body_text = ''

            if not body_text:
                break
            if article_bodies and body_text == article_bodies[-1]:
                # 同一ページ本文の重複ループを停止
                break

            article_bodies.append(body_text)
        except Exception:
            break

    # コメント（最大10ページ, Selenium）
    comments = []
    try:
        driver = setup_driver()
        for comment_page in range(1, MAX_COMMENT_PAGES + 1):
            comment_url = f"{base_url}/comments?page={comment_page}"
            driver.get(comment_url)
            time.sleep(2)

            soup_comments = BeautifulSoup(driver.page_source, 'html.parser')
            # 既存コードのセレクタ（変化の可能性はある）
            comment_elements = soup_comments.find_all('p', class_='sc-169yn8p-10')
            page_comments = [p.get_text(strip=True) for p in comment_elements]
            if not page_comments:
                break

            # 次ページの先頭が直前と同じなら終わり
            if comments and page_comments and page_comments[0] == comments[-1]:
                break

            comments.extend(page_comments)
        driver.quit()
    except Exception:
        pass

    return article_bodies, comments

def enrich_yahoo_articles_in_sheet(worksheet):
    """URLがYahooドメインの行について、右側に本文/コメントを一括追記"""
    ensure_header(worksheet)
    values = worksheet.get_all_values()
    if len(values) <= 1:
        print("（Yahoo）対象行なし")
        return

    # 行ごとの追加データを作る（E列以降）
    rows_to_update = []  # (row_index, list_of_values_for_right_side)
    for i, row in enumerate(values[1:], start=2):  # 2行目から
        if len(row) < 2:
            continue
        url = row[1]
        if not url or "news.yahoo.co.jp" not in url:
            continue

        # すでに本文/コメントが入っている場合はスキップ（必要に応じて上書きしたい場合は条件を外す）
        if len(row) >= 5 and any(cell.strip() for cell in row[4:]):
            continue

        print(f"  - Yahoo本文/コメント取得: R{i} URL={url}")
        bodies, comments = fetch_yahoo_article_body_and_comments(url)
        right = []

        # 本文(1..10)
        for idx in range(MAX_BODY_PAGES):
            right.append(bodies[idx] if idx < len(bodies) else "")

        # コメント数
        right.append(len(comments))
        # コメント（横並び）
        if comments:
            right.extend(comments)
        else:
            right.append("")  # 「コメント（以降…）」の位置

        rows_to_update.append((i, right))

    if not rows_to_update:
        print("（Yahoo）追記対象なし")
        return

    # 行ごとに長さを揃えて、一括 update
    max_len = max(len(r[1]) for r in rows_to_update)
    padded = []
    for row_idx, right in rows_to_update:
        if len(right) < max_len:
            right = right + [""] * (max_len - len(right))
        padded.append((row_idx, right))

    start_col_idx = 5  # E列
    end_col_idx = start_col_idx + max_len - 1
    end_col_letter = column_index_to_letter(end_col_idx)

    # gspreadは矩形の一括更新を行うので、スパースを埋める
    start_row = min(r for r, _ in padded)
    end_row = max(r for r, _ in padded)
    total_rows = end_row - start_row + 1

    # 行番号→データ のマップを作り、欠ける行は空で埋める
    row_map = {r: v for r, v in padded}
    empty_row = [""] * max_len
    matrix = []
    for r in range(start_row, end_row + 1):
        matrix.append(row_map.get(r, empty_row))

    rng = f"{column_index_to_letter(start_col_idx)}{start_row}:{end_col_letter}{end_row}"
    worksheet.update(rng, matrix, value_input_option="USER_ENTERED")
    print(f"✅ Yahoo本文/コメントを {rng} に一括追記しました（{len(rows_to_update)}行）。")


# ========= メイン =========
def main():
    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    # ワークシート確保
    try:
        ws_google = sh.worksheet(WS_GOOGLE)
    except gspread.exceptions.WorksheetNotFound:
        ws_google = sh.add_worksheet(title=WS_GOOGLE, rows="1000", cols="100")

    try:
        ws_yahoo = sh.worksheet(WS_YAHOO)
    except gspread.exceptions.WorksheetNotFound:
        ws_yahoo = sh.add_worksheet(title=WS_YAHOO, rows="2000", cols="200")

    try:
        ws_msn = sh.worksheet(WS_MSN)
    except gspread.exceptions.WorksheetNotFound:
        ws_msn = sh.add_worksheet(title=WS_MSN, rows="1000", cols="100")

    # 1) ニュース一覧の更新（左4列）
    print("\n--- Google News ---")
    google_list = get_google_news_with_selenium(KEYWORD)
    if google_list:
        append_news_list(ws_google, google_list)

    print("\n--- Yahoo! News ---")
    yahoo_list = get_yahoo_news_with_selenium(KEYWORD)
    if yahoo_list:
        append_news_list(ws_yahoo, yahoo_list)

    print("\n--- MSN News ---")
    msn_list = get_msn_news_with_selenium(KEYWORD)
    if msn_list:
        append_news_list(ws_msn, msn_list)

    # 2) Yahoo シートの URL に対して本文・コメントを右側に追記
    print("\n--- Yahoo! 本文/コメント追記 ---")
    enrich_yahoo_articles_in_sheet(ws_yahoo)

    print("\n--- 完了 ---")

if __name__ == "__main__":
    main()
