import os
import csv
import re
import time
import datetime
import requests
from bs4 import BeautifulSoup

JCD = "02"  # ボートレース戸田の場コード
DATA_DIR = "demetodadata"
JST = datetime.timezone(datetime.timedelta(hours=9))
today_jst = datetime.datetime.now(JST).date()
target_year = today_jst.year

DEME_CSV_PATH = os.path.join(DATA_DIR, f"demetodadata_{target_year}.csv")
RESULT_CSV_PATH = os.path.join(DATA_DIR, f"raceresult_{target_year}.csv")

DEME_COLUMNS = ["日付", "レース番号", "1着", "2着", "3着", "配当"]
RESULT_COLUMNS = [
    "日付", "レース番号", "気温", "水温", "天気", "風向", "風速",
    "1コースST", "2コースST", "3コースST", "4コースST", "5コースST", "6コースST"
]

# 公式サイトの風向クラス番号（is-wind1 ～ is-wind17）と16方位の対応表
WIND_DIR_MAP = {
    "1": "北",
    "2": "北北東",
    "3": "北東",
    "4": "東北東",
    "5": "東",
    "6": "東南東",
    "7": "南東",
    "8": "南南東",
    "9": "南",
    "10": "南南西",
    "11": "南西",
    "12": "西南西",
    "13": "西",
    "14": "西北西",
    "15": "北西",
    "16": "北北西",
    "17": "無風"
}

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

        if curr.month == 12:
            curr = datetime.date(curr.year + 1, 1, 1)
        else:
            curr = datetime.date(curr.year, curr.month + 1, 1)

    return sorted(list(held_dates))

def load_existing_csv(filepath):
    """既存CSVを読み込んで辞書のリストとして返す"""
    records = []
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("日付") and row.get("レース番号"):
                    row["日付"] = row["日付"].strip()
                    row["レース番号"] = int(row["レース番号"])
                    records.append(row)
    return records

def save_merged_csv(filepath, columns, existing_records, new_records):
    """既存データと新規データを結合・重複排除・ソートして保存"""
    merged_map = {}
    for rec in existing_records + new_records:
        key = (rec["日付"], int(rec["レース番号"]))
        merged_map[key] = rec

    sorted_records = sorted(merged_map.values(), key=lambda x: (x["日付"], int(x["レース番号"])))

    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted_records)

    return len(sorted_records)

def get_fetch_start_date(existing_records):
    """既存レコードの最新日の翌日（データなしなら当年1月1日）を返す"""
    if len(existing_records) > 0:
        last_date_str = max(r["日付"] for r in existing_records)
        last_date = datetime.datetime.strptime(last_date_str, "%Y-%m-%d").date()
        return last_date + datetime.timedelta(days=1)
    return datetime.date(target_year, 1, 1)

def parse_deme_records(soup, target_date):
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

def parse_raceresult_page(soup, target_date, race_no):
    """各レース結果ページ（raceresult）から気象データと1～6コースSTを抽出"""
    weather_wrap = soup.select_one(".weather1_body")
    if not weather_wrap:
        return None  # 中止・不成立または未確定レース

    temp = ""
    water_temp = ""
    weather = ""
    wind_dir = ""
    wind_speed = ""

    for unit in weather_wrap.select(".weather1_bodyUnit"):
        label_el = unit.select_one(".weather1_bodyUnitLabelTitle")
        data_el = unit.select_one(".weather1_bodyUnitLabelData")
        label_text = label_el.get_text(strip=True) if label_el else ""
        data_text = data_el.get_text(strip=True) if data_el else ""

        if "気温" in label_text:
            m = re.search(r"([-+]?\d+(?:\.\d+)?)", data_text)
            temp = m.group(1) if m else ""
        elif "水温" in label_text:
            m = re.search(r"([-+]?\d+(?:\.\d+)?)", data_text)
            water_temp = m.group(1) if m else ""
        elif "風速" in label_text:
            m = re.search(r"(\d+(?:\.\d+)?)", data_text)
            wind_speed = m.group(1) if m else ""
        elif "is-weather" in unit.get("class", []):
            weather = label_text
        elif "is-windDirection" in unit.get("class", []):
            img_el = unit.select_one(".weather1_bodyUnitImage")
            if img_el:
                for cls in img_el.get("class", []):
                    m_w = re.match(r"^is-wind(\d+)$", cls)
                    if m_w:
                        wind_dir = WIND_DIR_MAP.get(m_w.group(1), "")
                        break

    course_sts = ["", "", "", "", "", ""]
    st_spans = soup.select(".table1_boatImage1 .table1_boatImage1TimeInner")

    for idx, span in enumerate(st_spans[:6]):
        raw_st = span.get_text(strip=True)
        m_st = re.search(r"([FL]?\.\d{2})", raw_st)
        if m_st:
            course_sts[idx] = m_st.group(1)
        else:
            course_sts[idx] = raw_st

    return {
        "日付": target_date.strftime("%Y-%m-%d"),
        "レース番号": race_no,
        "気温": temp,
        "水温": water_temp,
        "天気": weather,
        "風向": wind_dir,
        "風速": wind_speed,
        "1コースST": course_sts[0],
        "2コースST": course_sts[1],
        "3コースST": course_sts[2],
        "4コースST": course_sts[3],
        "5コースST": course_sts[4],
        "6コースST": course_sts[5]
    }

def update_deme_data():
    """1. 出目・配当CSV（demetodadata_年.csv）の差分更新"""
    print("\n--- [1/2] 出目・配当データの確認 ---")
    existing_records = load_existing_csv(DEME_CSV_PATH)
    print(f"既存 '{DEME_CSV_PATH}': {len(existing_records)} レース")

    start_date = get_fetch_start_date(existing_records)
    if start_date > today_jst:
        print("すでに最新日付まで取得済みです。")
        return

    print(f"差分取得対象期間: {start_date} ～ {today_jst}")
    held_dates = get_held_dates_in_range(start_date, today_jst)
    print(f"対象期間内の戸田開催日数: {len(held_dates)} 日")

    if len(held_dates) == 0:
        return

    new_records = []
    for target_date in held_dates:
        hd = target_date.strftime("%Y%m%d")
        list_url = f"https://www.boatrace.jp/owpc/pc/race/resultlist?jcd={JCD}&hd={hd}"
        try:
            soup = fetch_soup(list_url)
            recs = parse_deme_records(soup, target_date)
            if len(recs) > 0:
                new_records.extend(recs)
                print(f"  [{target_date}] {len(recs)} レース取得")
            else:
                print(f"  [{target_date}] 確定結果なし")
            time.sleep(0.5)
        except Exception as e:
            print(f"  [{target_date}] 取得エラー: {e}")

    if len(new_records) > 0:
        total = save_merged_csv(DEME_CSV_PATH, DEME_COLUMNS, existing_records, new_records)
        print(f"更新完了: '{DEME_CSV_PATH}'（累計 {total} レース）")

def update_raceresult_data():
    """2. 気象・コース別ST CSV（raceresult_年.csv）の差分更新"""
    print("\n--- [2/2] 気象・コース別STデータの確認 ---")
    existing_records = load_existing_csv(RESULT_CSV_PATH)
    print(f"既存 '{RESULT_CSV_PATH}': {len(existing_records)} レース")

    start_date = get_fetch_start_date(existing_records)
    if start_date > today_jst:
        print("すでに最新日付まで取得済みです。")
        return

    print(f"差分取得対象期間: {start_date} ～ {today_jst}")
    held_dates = get_held_dates_in_range(start_date, today_jst)
    print(f"対象期間内の戸田開催日数: {len(held_dates)} 日")

    if len(held_dates) == 0:
        return

    new_records = []
    for target_date in held_dates:
        hd = target_date.strftime("%Y%m%d")
        day_count = 0
        for rno in range(1, 13):
            url = f"https://www.boatrace.jp/owpc/pc/race/raceresult?rno={rno}&jcd={JCD}&hd={hd}"
            try:
                soup = fetch_soup(url)
                rec = parse_raceresult_page(soup, target_date, rno)
                if rec:
                    new_records.append(rec)
                    day_count += 1
                time.sleep(0.35)
            except Exception as e:
                print(f"  [{target_date} {rno}R] 取得エラー: {e}")
        print(f"  [{target_date}] {day_count} レースの気象・ST取得")

    if len(new_records) > 0:
        total = save_merged_csv(RESULT_CSV_PATH, RESULT_COLUMNS, existing_records, new_records)
        print(f"更新完了: '{RESULT_CSV_PATH}'（累計 {total} レース）")

def main():
    print(f"=== ボートレース戸田 自動差分更新開始 ({today_jst} JST) ===")
    os.makedirs(DATA_DIR, exist_ok=True)

    update_deme_data()
    update_raceresult_data()

    print("\n=== すべての更新処理が完了しました ===")

if __name__ == "__main__":
    main()
