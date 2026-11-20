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

# 全角数字→半角数字の変換テーブル
ZEN_TO_HAN = str.maketrans("０１２３４５６７８９", "0123456789")

# ブラウザ偽装セッション（Cookieを維持して自然なアクセスにする）
session = requests.Session(impersonate="chrome")


def fetch_soup(url: str) -> BeautifulSoup | None:
    """指定URLのHTMLを取得してBeautifulSoupオブジェクトを返す"""
    try:
        res = session.get(url, headers=HEADERS, timeout=15)
        res.raise_for_status()
        return BeautifulSoup(res.content, "html.parser")
    except Exception as e:
        print(f"[WARN] 取得失敗: {url} ({e})")
        return None


def to_float(val: str) -> float:
    """文字列を安全にfloatへ変換する"""
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def normalize_date_str(val: str) -> str:
    """YYYYMMDD / YYYY/MM/DD / YYYY-MM-DD / YYYY/M/D 等を YYYYMMDD 形式に正規化する"""
    s = val.strip().translate(ZEN_TO_HAN)
    if not s:
        return ""
    m = re.match(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$", s)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        return digits
    return ""


def map_wind_direction(classes: list[str]) -> str:
    """
    boatrace.jp の風向アイコンクラス (is-wind1 〜 is-wind16) を
    アプリ用の風向テキスト（追い風・向かい風・左横風・右横風・無風）へ変換する
    """
    wind_num = None
    for c in classes:
        m = re.match(r"^is-wind(\d+)$", c)
        if m:
            wind_num = int(m.group(1))
            break

    if wind_num is None or wind_num == 17:
        return "無風"
    # boatrace.jp 共通方位アイコン基準（1:北〜16、水面レイアウトに応じた簡易マッピング）
    if wind_num in (1, 2, 15, 16):
        return "追い風"
    if wind_num in (7, 8, 9, 10):
        return "向かい風"
    if wind_num in (3, 4, 5, 6):
        return "左横風"
    return "右横風"


def parse_racelist(hd: str, rno: int) -> dict | None:
    """出走表ページをパースして6艇分の基本データを返す"""
    url = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={JCD}&hd={hd}"
    soup = fetch_soup(url)
    if not soup:
        return None

    # 非開催日・中止日チェック
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

        # 出走表テーブル列から各成績を抽出
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

        # 今節成績（進入コース行: 2行目、着順行: 4行目）から節間着順と外枠3着以内を抽出
        series_ranks = []
        series_outer_top3 = 0
        trs = tbody.select("tr")
        if len(trs) >= 4:
            course_tds = trs[1].select("td")
            rank_tds = trs[3].select("td")
            for c_idx, r_td in enumerate(rank_tds):
                r_txt = r_td.get_text(strip=True).translate(ZEN_TO_HAN)
                if re.match(r"^[1-6]$", r_txt):
                    rank_val = int(r_txt)
                    series_ranks.append(rank_val)
                    # 対応する進入コースが4〜6コースで3着以内なら外枠好走としてカウント
                    if c_idx < len(course_tds):
                        c_txt = course_tds[c_idx].get_text(strip=True).translate(ZEN_TO_HAN)
                        if re.match(r"^[4-6]$", c_txt) and rank_val <= 3:
                            series_outer_top3 += 1

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
            "seriesRanks": series_ranks,
            "seriesOuterTop3": series_outer_top3,
            "exhibitionTime": None,
            "tilt": None,
            "exhibitionST": None,
            "partsChanged": [],
        })

    return {
        "rno": rno,
        "racers": racers,
        "weather": {},
        "result": None,
    }


def merge_beforeinfo(hd: str, race_data: dict) -> dict:
    """直前情報ページから展示タイム・チルト・展示ST・部品交換・気象・風向・安定板装着を取得して結合する"""
    rno = race_data["rno"]
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={JCD}&hd={hd}"
    soup = fetch_soup(url)
    if not soup:
        return race_data

    # 展示タイム・チルト角度・部品交換の取得
    tbodies = soup.select("tbody.is-fs12")
    for idx, tbody in enumerate(tbodies[:6]):
        tds = tbody.select("tr:first-child td")
        # 展示タイム (td[4])
        if len(tds) > 4:
            ex_text = tds[4].get_text(strip=True)
            if re.match(r"^\d\.\d{2}$", ex_text):
                race_data["racers"][idx]["exhibitionTime"] = float(ex_text)
        # チルト角度 (td[5]) 例: -0.5, 0.0, 0.5, 1.5, 3.0
        if len(tds) > 5:
            tilt_text = tds[5].get_text(strip=True).replace("＋", "+").replace("－", "-")
            if re.match(r"^[+-]?\d+(\.\d+)?$", tilt_text):
                race_data["racers"][idx]["tilt"] = float(tilt_text)
        # 部品交換 (td[7]) 例: リング2, キャブ, 電気 など
        if len(tds) > 7:
            parts = [
                s.strip()
                for s in tds[7].stripped_strings
                if s.strip() and s.strip() not in ("-", "－", "なし")
            ]
            if parts:
                race_data["racers"][idx]["partsChanged"] = parts

    # スタート展示（各艇の展示ST）の取得
    for boat_img in soup.select("div.table1_boatImage1"):
        num_el = boat_img.select_one("span.table1_boatImage1Number")
        time_el = boat_img.select_one("span.table1_boatImage1Time")
        if not num_el or not time_el:
            continue
        boat_txt = num_el.get_text(strip=True).translate(ZEN_TO_HAN)
        st_txt = time_el.get_text(strip=True).translate(ZEN_TO_HAN)
        if not re.match(r"^[1-6]$", boat_txt):
            continue
        boat_idx = int(boat_txt) - 1
        if 0 <= boat_idx < len(race_data["racers"]):
            # 例: ".08" -> 0.08, "F.02" -> -0.02（フライングは負数で保持）
            m_f = re.match(r"^F\.?(\d{2})$", st_txt, re.IGNORECASE)
            m_n = re.match(r"^\.?(\d{2})$", st_txt)
            if m_f:
                race_data["racers"][boat_idx]["exhibitionST"] = -round(int(m_f.group(1)) / 100.0, 2)
            elif m_n:
                race_data["racers"][boat_idx]["exhibitionST"] = round(int(m_n.group(1)) / 100.0, 2)

    # 安定板装着の有無をチェック（ラベル・見出し領域に限定して誤検知を防ぐ）
    notice_texts = " ".join(
        el.get_text(" ", strip=True)
        for el in soup.select("span.label2, div.weather1, h3.title16_titleDetail__add2020")
    )
    stabilizer = "安定板" in notice_texts

    # 水面気象情報（天候・風向・風速・波高）
    weather_el = soup.select_one("div.weather1")
    if weather_el:
        cond_el = weather_el.select_one("div.is-weather span.weather1_bodyUnitLabelTitle")
        wind_el = weather_el.select_one("div.is-wind span.weather1_bodyUnitLabelData")
        wave_el = weather_el.select_one("div.is-wave span.weather1_bodyUnitLabelData")
        wind_dir_el = weather_el.select_one("div.is-windDirection p.weather1_bodyUnitImage")
        wind_dir = map_wind_direction(wind_dir_el.get("class", [])) if wind_dir_el else "無風"

        race_data["weather"] = {
            "condition": cond_el.get_text(strip=True) if cond_el else "現地発表",
            "windDir": wind_dir,
            "windSpeed": wind_el.get_text(strip=True) if wind_el else "0m",
            "waveHeight": wave_el.get_text(strip=True) if wave_el else "0cm",
            "stabilizer": stabilizer,
        }

    return race_data


def merge_raceresult(hd: str, race_data: dict) -> dict:
    """確定済みレースの結果（1〜3着の枠番・決まり手・3連単払戻金）を取得して結合する"""
    rno = race_data["rno"]
    url = f"https://www.boatrace.jp/owpc/pc/race/raceresult?rno={rno}&jcd={JCD}&hd={hd}"
    soup = fetch_soup(url)
    if not soup or "データがありません" in soup.text:
        return race_data

    # 着順テーブル（theadに「着」「枠」を含む table.is-w495）を特定して1〜3着の「枠番」を抽出
    order = []
    for table in soup.select("table.is-w495"):
        thead_text = table.select_one("thead").get_text(" ", strip=True) if table.select_one("thead") else ""
        if "着" in thead_text and "枠" in thead_text:
            for tbody in table.select("tbody"):
                tds = tbody.select("tr:first-child td")
                if len(tds) >= 2:
                    rank_txt = tds[0].get_text(strip=True).translate(ZEN_TO_HAN)
                    boat_txt = tds[1].get_text(strip=True).translate(ZEN_TO_HAN)
                    if rank_txt in ("1", "2", "3") and boat_txt.isdigit():
                        order.append(int(boat_txt))
                if len(order) >= 3:
                    break
            break

    if len(order) < 3:
        return race_data

    # 決まり手を抽出
    kimarite = ""
    for th in soup.select("th"):
        if "決まり手" in th.get_text():
            table = th.find_parent("table")
            if table:
                td = table.select_one("tbody td")
                if td:
                    kimarite = td.get_text(strip=True)
            break

    # 3連単の払戻金を抽出
    payout_3t = None
    for tbody in soup.select("table.is-w495 tbody"):
        if "3連単" in tbody.get_text():
            payout_el = tbody.select_one("span.is-payout1")
            if payout_el:
                digits = re.sub(r"[^\d]", "", payout_el.get_text().translate(ZEN_TO_HAN))
                if digits:
                    payout_3t = int(digits)
            break

    race_data["result"] = {
        "order": order,
        "combo": f"{order[0]}-{order[1]}-{order[2]}",
        "inNige": order[0] == 1,
        "kimarite": kimarite,
        "payout3t": payout_3t,
    }
    return race_data


def fetch_single_day(hd: str, skip_existing: bool = False) -> bool:
    """指定された1日分（YYYYMMDD）の全12Rを取得してJSON保存する。保存成功ならTrue"""
    out_path = f"data/toda_{hd}.json"
    if skip_existing and os.path.exists(out_path):
        print(f"[SKIP] {out_path} は取得済みのためスキップします。")
        return False

    print(f"\n=== 戸田競艇データ取得開始: {hd} ===")
    races = {}

    for rno in range(1, 13):
        print(f"  [{hd}] Fetching {rno}R...")
        race_data = parse_racelist(hd, rno)
        if not race_data:
            if rno == 1:
                print(f"  [{hd}] 戸田の開催がありません。スキップします。")
                return False
            continue

        time.sleep(0.7)
        race_data = merge_beforeinfo(hd, race_data)
        time.sleep(0.7)
        race_data = merge_raceresult(hd, race_data)
        races[str(rno)] = race_data
        time.sleep(0.7)

    if not races:
        return False

    output = {
        "date": hd,
        "updatedAt": datetime.now(JST).isoformat(),
        "venue": "戸田",
        "jcd": JCD,
        "races": races,
    }

    os.makedirs("data", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  [OK] 保存完了: {out_path} ({len(races)}レース)")
    return True


def build_date_list(start_str: str, end_str: str) -> list[str]:
    """開始日〜終了日（YYYYMMDD）の日付リストを生成する（最大31日間ガード付き）"""
    start_dt = datetime.strptime(start_str, "%Y%m%d")
    end_dt = datetime.strptime(end_str, "%Y%m%d")
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    days = (end_dt - start_dt).days + 1
    if days > 31:
        print(f"[WARN] 期間が長すぎます（{days}日間）。サーバー負荷防止のため最大31日間に制限します。")
        end_dt = start_dt + timedelta(days=30)

    result = []
    cur = start_dt
    while cur <= end_dt:
        result.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return result


def main():
    today_hd = datetime.now(JST).strftime("%Y%m%d")
    arg1 = normalize_date_str(sys.argv[1]) if len(sys.argv) > 1 else ""
    arg2 = normalize_date_str(sys.argv[2]) if len(sys.argv) > 2 else ""
    skip_existing = os.environ.get("SKIP_EXISTING", "false").lower() == "true"

    # 日付リストを決定
    if arg1 and arg2:
        dates = build_date_list(arg1, arg2)
        is_bulk = True
    elif arg1:
        dates = [arg1]
        is_bulk = False
    else:
        dates = [today_hd]
        is_bulk = False

    print(f"対象日数: {len(dates)}日 ({dates[0]} 〜 {dates[-1]}) / skip_existing={skip_existing}")

    # 先にトップページへ一度アクセスしてCookieを取得
    fetch_soup("https://www.boatrace.jp/")
    time.sleep(1)

    saved_dates = []
    for hd in dates:
        if fetch_single_day(hd, skip_existing=skip_existing):
            saved_dates.append(hd)

    # toda_latest.json の更新（過去一括取得で古い日付に上書きしないよう制御）
    if saved_dates:
        latest_saved = max(saved_dates)
        should_update_latest = (not is_bulk and latest_saved == today_hd) or (not os.path.exists("data/toda_latest.json"))
        if should_update_latest:
            src_path = f"data/toda_{latest_saved}.json"
            with open(src_path, "r", encoding="utf-8") as f:
                latest_data = json.load(f)
            with open("data/toda_latest.json", "w", encoding="utf-8") as f:
                json.dump(latest_data, f, ensure_ascii=False, indent=2)
            print(f"[OK] data/toda_latest.json を {latest_saved} で更新しました。")

    print(f"\n=== 全処理完了: 新規保存 {len(saved_dates)}日分 ===")


if __name__ == "__main__":
    main()
