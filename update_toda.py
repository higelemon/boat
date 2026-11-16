import os
import io
import re
import time
import ftplib
import datetime
import requests
from bs4 import BeautifulSoup
import pandas as pd

# ==============================================================================
# 環境変数（GitHub Secretsから自動読み込み）
# ==============================================================================
FTP_HOST = os.environ["FC2_FTP_HOST"]      # 例: yourname.web.fc2.com
FTP_USER = os.environ["FC2_FTP_USER"]      # 例: yourname
FTP_PASS = os.environ["FC2_FTP_PASS"]      # FTPパスワード
REMOTE_DIR = "demetodadata"                # FC2上の保存先フォルダ名

JCD = "02"  # ボートレース戸田の場コード
JST = datetime.timezone(datetime.timedelta(hours=9))
today_jst = datetime.datetime.now(JST).date()
target_year = today_jst.year
csv_filename = f"demetodadata_{target_year}.csv"
columns_order = ["日付", "レース番号", "1着", "2着", "3着", "配当"]

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Connection": "keep-alive"
})

def fetch_soup(url, retries=3):
    for attempt in range(retries):
        try:
            res = session.get(url, timeout=15)
            res.raise_for_status()
            res.encoding = res.apparent_encoding
            return BeautifulSoup(res.text, "html.parser")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
            else:
                raise e

def get_held_dates_in_range(start_dt, end_dt):
    """指定期間内のボートレース戸田の開催日だけを月別スケジュールから抽出"""
    held_dates = set()
    curr = datetime.date(start_dt.year, start_dt.month, 1)
    while curr <= end_dt:
        ym = f"{curr.year}{curr.month:02d}"
        url = f"https://www.boatrace.jp/owpc/pc/race/monthlyschedule?ym={ym}"
        try:
            soup = fetch_soup(url)
            time.sleep(0.5)
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if f"jcd={JCD}" in href and "hd=" in href:
                    m = re.search(r"hd=(\d{8})", href)
                    if m:
                        d_obj = datetime.datetime.strptime(m.group(1), "%Y%m%d").date()
                        if start_dt <= d_obj <= end_dt:
                            held_dates.add(d_obj)
        except Exception as e:
            print(f"スケジュール取得エラー ({ym}): {e}")

        # 翌月へ
        if curr.month == 12:
            curr = datetime.date(curr.year + 1, 1, 1)
        else:
            curr = datetime.date(curr.year, curr.month + 1, 1)

    return sorted(list(held_dates))

def parse_race_records(soup, target_date):
    """1日結果一覧ページから「日付, レース番号, 1着, 2着, 3着, 配当」を抽出"""
    day_records = []
    date_str = target_date.strftime("%Y-%m-%d")

    for tr in soup.select("table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue

        race_label = tds[0].get_text(strip=True)
        m_race = re.match(r"^(\d{1,2})R$", race_label)
        if not m_race:
            continue

        race_no = int(m_race.group(1))
        comb_raw = tds[1].get_text(strip=True)
        nums = re.findall(r"[1-6]", comb_raw)
        payout_raw = tds[2].get_text(strip=True).replace("¥", "").replace(",", "")

        if len(nums) >= 3 and payout_raw.isdigit():
            day_records.append({
                "日付": date_str,
                "レース番号": race_no,
                "1着": int(nums[0]),
                "2着": int(nums[1]),
                "3着": int(nums[2]),
                "配当": int(payout_raw)
            })
    return day_records

def main():
    print(f"=== ボートレース戸田 自動差分更新開始 ({today_jst} JST) ===")

    # 1. FC2サーバーにFTP接続して現在のCSVを取得
    ftp = ftplib.FTP(FTP_HOST, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)  # パッシブモード（GitHub Actionsからの接続に必須）

    # demetodadataフォルダへ移動（なければ作成）
    try:
        ftp.cwd(REMOTE_DIR)
    except ftplib.error_perm:
        ftp.mkd(REMOTE_DIR)
        ftp.cwd(REMOTE_DIR)

    existing_df = pd.DataFrame(columns=columns_order)
    buf = io.BytesIO()

    try:
        ftp.retrbinary(f"RETR {csv_filename}", buf.write)
        buf.seek(0)
        existing_df = pd.read_csv(buf, encoding="utf-8-sig")
        print(f"既存ファイル '{csv_filename}' を取得しました（現在 {len(existing_df)} レース）")
    except ftplib.error_perm:
        print(f"'{csv_filename}' がサーバー上にまだ存在しないため新規作成します。")

    # 2. 差分取得の開始日を決定
    if len(existing_df) > 0:
        last_date_str = str(existing_df["日付"].max())
        last_date = datetime.datetime.strptime(last_date_str, "%Y-%m-%d").date()
        fetch_start_date = last_date + datetime.timedelta(days=1)
    else:
        fetch_start_date = datetime.date(target_year, 1, 1)

    if fetch_start_date > today_jst:
        print("すでに最新日付まで取得済みです。更新を終了します。")
        ftp.quit()
        return

    print(f"差分取得対象期間: {fetch_start_date} ～ {today_jst}")

    # 3. 対象期間の開催日を確認し、レース結果を取得
    held_dates = get_held_dates_in_range(fetch_start_date, today_jst)
    print(f"対象期間内の戸田開催日数: {len(held_dates)} 日")

    if len(held_dates) == 0:
        print("新規の開催日がありませんでした。FTPアップロードをスキップします。")
        ftp.quit()
        return

    new_records = []
    for target_date in held_dates:
        hd = target_date.strftime("%Y%m%d")
        list_url = f"https://www.boatrace.jp/owpc/pc/race/resultlist?jcd={JCD}&hd={hd}"
        try:
            soup = fetch_soup(list_url)
            recs = parse_race_records(soup, target_date)
            if len(recs) > 0:
                new_records.extend(recs)
                print(f"  [{target_date}] {len(recs)} レース取得")
            else:
                print(f"  [{target_date}] 確定結果なし（中止または未確定）")
            time.sleep(0.5)
        except Exception as e:
            print(f"  [{target_date}] 取得エラー: {e}")

    if len(new_records) == 0:
        print("追加できる確定レース結果がありませんでした。")
        ftp.quit()
        return

    # 4. 既存データと結合・重複排除・ソート
    new_df = pd.DataFrame(new_records, columns=columns_order)
    combined_df = pd.concat([existing_df, new_df], ignore_index=True)
    combined_df.drop_duplicates(subset=["日付", "レース番号"], keep="last", inplace=True)
    combined_df.sort_values(by=["日付", "レース番号"], ascending=[True, True], inplace=True)

    # 5. FC2サーバーへ上書きアップロード
    out_buf = io.BytesIO()
    out_buf.write(combined_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"))
    out_buf.seek(0)

    ftp.storbinary(f"STOR {csv_filename}", out_buf)
    ftp.quit()

    print(f"完了！ '{REMOTE_DIR}/{csv_filename}' を更新しました（累計 {len(combined_df)} レース）")

if __name__ == "__main__":
    main()
