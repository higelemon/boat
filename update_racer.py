from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import os
import re
import time
from bs4 import BeautifulSoup
import requests

CSV_FILE = "racer/racer.csv"
SEARCH_URL = "https://www.boatrace.jp/owpc/pc/data/racersearch/index"
PROFILE_URL = "https://www.boatrace.jp/owpc/pc/data/racersearch/profile"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": SEARCH_URL,
    "Origin": "https://www.boatrace.jp",
}


def get_existing_racers():
    """既存のracer/racer.csvを読み込み、データ一覧と最大の登録番号を返す"""
    rows = []
    max_toban = 2000

    if os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode="r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if not row or not row[0].strip().isdigit():
                    continue
                rows.append(row)
                toban_num = int(row[0].strip())
                if toban_num > max_toban:
                    max_toban = toban_num

    return rows, max_toban


def get_new_racer_list(start_toban):
    """start_toban 以降の登録番号を持つ選手のみを自動分割検索で漏れなく取得"""
    session = requests.Session()
    res_get = session.get(SEARCH_URL, headers=HEADERS, timeout=15)
    res_get.raise_for_status()
    res_get.encoding = "utf-8"
    soup_get = BeautifulSoup(res_get.text, "html.parser")

    form = soup_get.find("form")
    if not form:
        raise RuntimeError("検索フォームの取得に失敗しました")

    base_payload = {
        inp.get("name"): (
            inp.find("option").get("value", "")
            if inp.name == "select" and inp.find("option")
            else inp.get("value", "")
        )
        for inp in form.find_all(["input", "select"])
        if inp.get("name")
    }
    base_payload["TDATP310A_2:TDATP310A_7"] = ""

    racer_dict = {}

    def fetch_range(start, end):
        payload = base_payload.copy()
        payload["TDATP310A_2:inTobanLeft"] = str(start)
        payload["TDATP310A_2:inTobanRight"] = str(end)

        res_post = session.post(SEARCH_URL, headers=HEADERS, data=payload, timeout=15)
        res_post.raise_for_status()
        res_post.encoding = "utf-8"
        soup_post = BeautifulSoup(res_post.text, "html.parser")

        items = soup_post.select("ul.photoGallery3 li")

        if len(items) >= 40 and start < end:
            mid = (start + end) // 2
            fetch_range(start, mid)
            fetch_range(mid + 1, end)
            return

        for li in items:
            num_el = li.select_one("p.photoGallery3_bodyNumber")
            name_el = li.select_one("p.photoGallery3_bodyName")
            if num_el and name_el:
                toban = num_el.get_text(strip=True)
                if toban.isdigit() and int(toban) >= start_toban:
                    name = " ".join(name_el.get_text().split())
                    racer_dict[toban] = name

        time.sleep(0.2)

    end_limit = ((start_toban // 100) + 5) * 100
    for s in range(start_toban, end_limit, 100):
        fetch_range(s, s + 99)

    return sorted(racer_dict.items(), key=lambda x: int(x[0]))


def fetch_profile_detail(toban, name):
    """個別プロフィールページから登録期・支部・出身地・性別を確実に取得"""
    url = f"{PROFILE_URL}?toban={toban}"
    for _ in range(3):
        try:
            res = requests.get(url, headers=HEADERS, timeout=15)
            res.raise_for_status()
            res.encoding = "utf-8"
            soup = BeautifulSoup(res.text, "html.parser")

            info = {}
            for dt in soup.find_all(["dt", "th"]):
                key = "".join(dt.get_text().split())
                val_tag = dt.find_next_sibling(["dd", "td"])
                if val_tag:
                    val_text = "".join(val_tag.get_text().split())
                    if not val_text:
                        img = val_tag.find("img")
                        if img and img.get("alt"):
                            val_text = img.get("alt").strip()
                    info[key] = val_text

            term = info.get("登録期", "")
            branch = info.get("支部", "")
            birthplace = info.get("出身地", "")
            gender = info.get("性別", "")

            if not gender:
                for img in soup.find_all("img", alt=True):
                    alt_val = img["alt"].strip()
                    if alt_val in ["男", "女", "男性", "女性"]:
                        gender = alt_val[0]
                        break

            if not gender:
                full_text = " ".join(soup.get_text().split())
                m = re.search(r"性\s*別\s*[:：]?\s*(男|女)", full_text)
                if m:
                    gender = m.group(1)

            if gender.startswith("男"):
                gender = "男"
            elif gender.startswith("女"):
                gender = "女"

            return [toban, name, term, branch, birthplace, gender]
        except Exception:
            time.sleep(0.5)

    return [toban, name, "", "", "", ""]


def main():
    existing_rows, max_toban = get_existing_racers()
    search_start = max_toban + 1
    print(f"[*] 現在の登録済み選手数: {len(existing_rows)} 名（最大登録番号: {max_toban}）")
    print(f"[*] 登録番号 {search_start} 以降の新人選手を検索中...")

    new_basics = get_new_racer_list(search_start)
    if not new_basics:
        print("[-] 新規登録された選手は見つかりませんでした。")
        return

    print(f"[+] {len(new_basics)} 名の新規選手が見つかりました。詳細を取得します...")

    new_results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_profile_detail, toban, name): toban
            for toban, name in new_basics
        }
        for future in as_completed(futures):
            row = future.result()
            new_results.append(row)
            print(f"  取得完了: {row}")

    combined_dict = {r[0].strip(): r for r in existing_rows}
    for r in new_results:
        combined_dict[r[0].strip()] = r

    sorted_rows = sorted(combined_dict.values(), key=lambda x: int(x[0].strip()))

    os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)
    with open(CSV_FILE, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["登録番号", "レーサー名", "登録期", "支部", "出身地", "性別"])
        writer.writerows(sorted_rows)

    print(f"[★] 完了！ {CSV_FILE} を更新しました（総選手数: {len(sorted_rows)} 名）")


if __name__ == "__main__":
    main()
