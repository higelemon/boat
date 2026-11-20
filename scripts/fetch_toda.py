import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from curl_cffi import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
JCD = "02"  # 戸田の場コード
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.boatrace.jp/",
}

# ブラウザ偽装セッションを作成（Cookieを保持して自然なアクセスにする）
session = requests.Session(impersonate="chrome")


def fetch_soup(url: str) -> BeautifulSoup | None:
    try:
        res = session.get(url, headers=HEADERS, timeout=15)
        res.raise_for_status()
        return BeautifulSoup(res.content, "html.parser")
    except Exception as e:
        print(f"[WARN] 取得失敗: {url} ({e})")
        return None


def parse_racelist(hd: str, rno: int) -> dict | None:
    url = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={JCD}&hd={hd}"
    soup = fetch_soup(url)
    if not soup:
        return None

    # 非開催日チェック
    if "データがありません" in soup.text or not soup.select("tbody.is-fs12"):
        return None

    racers = []
    tbodies = soup.select("tbody.is-fs12")

    for idx, tbody in enumerate(tbodies[:6], start=1):
        # 登録番号・級別
        info_div = tbody.select_one("div.is-fs11")
        info_text = info_div.get_text(" ", strip=True) if info_div else ""
        reg_match = re.search(r"(\d{4})", info_text)
        cls_match = re.search(r"(A1|A2|B1|B2)", info_text)

        # 選手名
        name_el = tbody.select_one("div.is-fs18 a")
        name = name_el.get_text(strip=True).replace("\u3000", " ") if name_el else ""

        # F数・L数・平均ST / 全国勝率 / 当地勝率 / モーター / ボート
        tds = tbody.select("tr:first-child td")

        def get_lines(td_idx: int) -> list[str]:
            if td_idx < len(tds):
                return [s.strip() for s in tds[td_idx].stripped_strings]
            return []

        fl_st = get_lines(3)       # 例: ['F0', 'L0', '0.15']
        nat_stats = get_lines(4)   # 例: ['6.54', '48.20', '65.10']
        loc_stats = get_lines(5)   # 例: ['6.12', '42.00', '58.00']
        motor_stats = get_lines(6) # 例: ['32', '38.50', '55.20']
        boat_stats = get_lines(7)  # 例: ['15', '35.10', '50.00']

        def to_float(val: str) -> float:
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0

        racers.append({
            "boat": idx,
            "regNo": reg_match.group(1) if reg_match else "",
            "rank": cls_match.group(1) if cls_match else "",
            "name": name,
            "fCount": fl_st[0] if len(fl_st) > 0 else "F0",
            "avgST": fl_st[2] if len(fl_st) > 2 else "-",
            "natWinRate": to_float(nat_stats[0]) if len(nat_stats) > 0 else 0.0,
            "nat2Rate": to_float(nat_stats[1]) if len(nat_stats) > 1 else 0.0,
            "locWinRate": to_float(loc_stats[0]) if len(loc_stats) > 0 else 0.0,
            "motorNo": motor_stats[0] if len(motor_stats) > 0 else "",
            "motor2Rate": to_float(motor_stats[1]) if len(motor_stats) > 1 else 0.0,
            "boat2Rate": to_float(boat_stats[1]) if len(boat_stats) > 1 else 0.0,
            "exhibitionTime": None,  # 直前情報で補完
        })

    return {
        "rno": rno,
        "racers": racers,
        "weather": {}
    }


def merge_beforeinfo(hd: str, race_data: dict) -> dict:
    rno = race_data["rno"]
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={JCD}&hd={hd}"
    soup = fetch_soup(url)
    if not soup:
        return race_data

    # 展示タイムの取得
    tbodies = soup.select("tbody.is-fs12")
    for idx, tbody in enumerate(tbodies[:6]):
        tds = tbody.select("tr:first-child td")
        if len(tds) > 4:
            ex_text = tds[4].get_text(strip=True)
            if re.match(r"^\d\.\d{2}$", ex_text):
                race_data["racers"][idx]["exhibitionTime"] = float(ex_text)

    # 水面気象情報（風速・波高など）
    weather_el = soup.select_one("div.weather1")
    if weather_el:
        wind_el = weather_el.select_one("div.is-wind span.weather1_bodyUnitLabelData")
        wave_el = weather_el.select_one("div.is-wave span.weather1_bodyUnitLabelData")
        race_data["weather"] = {
            "windSpeed": wind_el.get_text(strip=True) if wind_el else "",
            "waveHeight": wave_el.get_text(strip=True) if wave_el else "",
        }

    return race_data


def main():
    # 引数で日付指定（YYYYMMDD）がなければ今日のJST日付を使用
    if len(sys.argv) > 1 and re.match(r"^\d{8}$", sys.argv[1]):
        hd = sys.argv[1]
    else:
        hd = datetime.now(JST).strftime("%Y%m%d")

    print(f"=== 戸田競艇データ取得開始: {hd} ===")

    # 先にトップページへ一度アクセスしてCookieを取得しておく
    fetch_soup("https://www.boatrace.jp/")
    time.sleep(1)

    races = {}

    for rno in range(1, 13):
        print(f"Fetching {rno}R...")
        race_data = parse_racelist(hd, rno)
        if not race_data:
            if rno == 1:
                print(f"{hd} は戸田の開催がありません。終了します。")
                sys.exit(0)
            continue

        time.sleep(1)
        race_data = merge_beforeinfo(hd, race_data)
        races[str(rno)] = race_data
        time.sleep(1)

    output = {
        "date": hd,
        "updatedAt": datetime.now(JST).isoformat(),
        "venue": "戸田",
        "jcd": JCD,
        "races": races,
    }

    os.makedirs("data", exist_ok=True)
    out_path = f"data/toda_{hd}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 最新データとして固定名でも保存
    with open("data/toda_latest.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"保存完了: {out_path} ({len(races)}レース)")


if __name__ == "__main__":
    main()
