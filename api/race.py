from __future__ import annotations

import re
import traceback
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests as std_requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, make_response, request

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except Exception:
    cffi_requests = None
    HAS_CURL_CFFI = False

app = Flask(__name__)

JST = timezone(timedelta(hours=9))
JCD = "02"  # ボートレース戸田
TODA_XML_BASE = "https://www.boatrace-toda.jp/xml/kaisai"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.boatrace.jp/",
}
ZEN_TO_HAN = str.maketrans("０１２３４５６７８９＋－．", "0123456789+-.")


def to_float(val: str | None) -> float | None:
    if val is None:
        return None
    s = str(val).translate(ZEN_TO_HAN).strip()
    if not s or s in ("-", "－"):
        return None
    m = re.search(r"[+-]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def fetch_bytes(url: str, timeout: int = 10) -> bytes | None:
    """curl_cffi（ブラウザ偽装）を優先して取得し、失敗時は標準requestsへフォールバック"""
    if HAS_CURL_CFFI and cffi_requests is not None:
        try:
            res = cffi_requests.get(url, headers=HEADERS, impersonate="chrome", timeout=timeout)
            if res.status_code == 200 and res.content:
                return res.content
        except Exception:
            pass
    try:
        res = std_requests.get(url, headers=HEADERS, timeout=timeout)
        if res.status_code == 200 and res.content:
            return res.content
    except Exception:
        pass
    return None


def map_wind_direction(classes: list[str]) -> str:
    wind_num = None
    for c in classes:
        m = re.match(r"^is-wind(\d+)$", c)
        if m:
            wind_num = int(m.group(1))
            break
    if wind_num is None or wind_num == 17:
        return "無風"
    if wind_num in (1, 2, 15, 16):
        return "追い風"
    if wind_num in (7, 8, 9, 10):
        return "向かい風"
    if wind_num in (3, 4, 5, 6):
        return "左横風"
    return "右横風"


def parse_wind_dir_text(txt: str | None) -> str:
    s = str(txt or "")
    if "向" in s:
        return "向かい風"
    if "追" in s:
        return "追い風"
    if "左" in s:
        return "左横風"
    if "右" in s:
        return "右横風"
    return "無風"


def expand_table_grid(tbody) -> list[list[str]]:
    grid: list[list[str]] = []
    spans: dict[tuple[int, int], str] = {}
    for r_idx, tr in enumerate(tbody.select("tr")):
        row: list[str] = []
        c_idx = 0
        for td in tr.select("td"):
            while (r_idx, c_idx) in spans:
                row.append(spans.pop((r_idx, c_idx)))
                c_idx += 1
            text = td.get_text(strip=True).translate(ZEN_TO_HAN)
            rowspan = int(td.get("rowspan", 1) or 1)
            colspan = int(td.get("colspan", 1) or 1)
            for dc in range(colspan):
                row.append(text)
                for dr in range(1, rowspan):
                    spans[(r_idx + dr, c_idx + dc)] = text
            c_idx += colspan
        while (r_idx, c_idx) in spans:
            row.append(spans.pop((r_idx, c_idx)))
            c_idx += 1
        grid.append(row)
    return grid


def fetch_racelist_from_toda_xml(hd: str, rno: int) -> list[dict] | None:
    """戸田公式XMLから出走表を取得するフォールバック"""
    url = f"{TODA_XML_BASE}/{hd}/race_table_{rno:02d}.xml"
    raw = fetch_bytes(url, timeout=6)
    if not raw:
        return None
    try:
        text = raw.decode("utf-8", errors="ignore").lstrip("\ufeff").strip()
        root = ET.fromstring(text)
        racers = []
        for idx, rec in enumerate(root.findall(".//record"), start=1):
            tb_raw = rec.findtext("teiban") or rec.get("teiban") or str(idx)
            tb_digits = re.sub(r"\D", "", tb_raw)
            tb = int(tb_digits) if tb_digits else idx
            if not (1 <= tb <= 6):
                continue
            racers.append({
                "boat": tb,
                "regNo": (rec.findtext("touban") or rec.findtext("toban") or "").strip(),
                "rank": (rec.findtext("kyu") or "B1").strip(),
                "name": (rec.findtext("name") or f"選手{tb}").strip(),
                "fCount": (rec.findtext("f_cnt") or rec.findtext("fcount") or "F0").strip(),
                "avgST": (rec.findtext("p_st") or "0.17").strip(),
                "natWinRate": to_float(rec.findtext("zsyo")) or 5.0,
                "nat2Rate": to_float(rec.findtext("z2ren")) or 32.0,
                "locWinRate": to_float(rec.findtext("jsyo")) or 0.0,
                "motorNo": (rec.findtext("mno") or rec.findtext("motor_no") or "-").strip(),
                "motor2Rate": to_float(rec.findtext("m2ren")) or 32.0,
                "boat2Rate": to_float(rec.findtext("b2ren")) or 32.0,
                "seriesRanks": [],
                "seriesOuterTop3": 0,
                "exhibitionTime": None,
                "tilt": None,
                "exhibitionST": None,
                "partsChanged": [],
                "oriIsshu": None,
                "oriMawari": None,
                "oriChokusen": None,
            })
        racers.sort(key=lambda x: x["boat"])
        return racers if len(racers) == 6 else None
    except Exception:
        return None


def fetch_racelist(hd: str, rno: int) -> list[dict] | None:
    url = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={JCD}&hd={hd}"
    raw = fetch_bytes(url)
    if raw:
        soup = BeautifulSoup(raw, "html.parser")
        tbodies = soup.select("tbody.is-fs12")
        if "データがありません" not in soup.text and len(tbodies) >= 6:
            racers = []
            for idx, tbody in enumerate(tbodies[:6], start=1):
                info_div = tbody.select_one("div.is-fs11")
                info_text = info_div.get_text(" ", strip=True) if info_div else ""
                reg_match = re.search(r"(\d{4})", info_text)
                cls_match = re.search(r"(A1|A2|B1|B2)", info_text)

                name_el = tbody.select_one("div.is-fs18 a")
                name = name_el.get_text(strip=True).replace("\u3000", " ") if name_el else ""

                tds = tbody.select("tr:first-child td")

                def get_lines(td_idx: int) -> list[str]:
                    if td_idx < len(tds):
                        return [s.strip() for s in tds[td_idx].stripped_strings]
                    return []

                fl_st = get_lines(3)
                nat_stats = get_lines(4)
                loc_stats = get_lines(5)
                motor_stats = get_lines(6)
                boat_stats = get_lines(7)

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
                            if c_idx < len(course_tds):
                                c_txt = course_tds[c_idx].get_text(strip=True).translate(ZEN_TO_HAN)
                                if re.match(r"^[4-6]$", c_txt) and rank_val <= 3:
                                    series_outer_top3 += 1

                racers.append({
                    "boat": idx,
                    "regNo": reg_match.group(1) if reg_match else "",
                    "rank": cls_match.group(1) if cls_match else "B1",
                    "name": name,
                    "fCount": fl_st[0] if len(fl_st) > 0 else "F0",
                    "avgST": fl_st[2] if len(fl_st) > 2 else "0.17",
                    "natWinRate": to_float(nat_stats[0]) or 0.0 if len(nat_stats) > 0 else 0.0,
                    "nat2Rate": to_float(nat_stats[1]) or 0.0 if len(nat_stats) > 1 else 0.0,
                    "locWinRate": to_float(loc_stats[0]) or 0.0 if len(loc_stats) > 0 else 0.0,
                    "motorNo": motor_stats[0] if len(motor_stats) > 0 else "-",
                    "motor2Rate": to_float(motor_stats[1]) or 0.0 if len(motor_stats) > 1 else 0.0,
                    "boat2Rate": to_float(boat_stats[1]) or 0.0 if len(boat_stats) > 1 else 0.0,
                    "seriesRanks": series_ranks,
                    "seriesOuterTop3": series_outer_top3,
                    "exhibitionTime": None,
                    "tilt": None,
                    "exhibitionST": None,
                    "partsChanged": [],
                    "oriIsshu": None,
                    "oriMawari": None,
                    "oriChokusen": None,
                })
            return racers

    # boatrace.jp 側が取れない場合は戸田公式XMLへフォールバック
    return fetch_racelist_from_toda_xml(hd, rno)


def fetch_beforeinfo_from_toda_xml(hd: str, rno: int) -> dict:
    url = f"{TODA_XML_BASE}/{hd}/race_table_before_{rno:02d}.xml"
    raw = fetch_bytes(url, timeout=6)
    if not raw:
        return {"boats": {}, "weather": {}}
    try:
        text = raw.decode("utf-8", errors="ignore").lstrip("\ufeff").strip()
        root = ET.fromstring(text)
        boats_info: dict[int, dict] = {}
        for idx, rec in enumerate(root.findall(".//record"), start=1):
            tb_raw = rec.get("teiban") or rec.findtext("teiban") or rec.findtext("waku") or str(idx)
            tb_digits = re.sub(r"\D", "", tb_raw)
            tb = int(tb_digits) if tb_digits else idx
            info: dict = {}
            tt = to_float(rec.findtext("ttime"))
            sst_raw = (rec.findtext("sst") or "").translate(ZEN_TO_HAN).strip()
            tl = to_float(rec.findtext("tiltc"))
            if tt is not None and tt > 0:
                info["exhibitionTime"] = tt
            if tl is not None:
                info["tilt"] = tl
            if sst_raw:
                m_f = re.match(r"^F\.?(\d{1,2})$", sst_raw, re.IGNORECASE)
                if m_f:
                    info["exhibitionST"] = -round(int(m_f.group(1)) / 100.0, 2)
                else:
                    sst_val = to_float(sst_raw)
                    if sst_val is not None:
                        info["exhibitionST"] = sst_val
            if info:
                boats_info[tb] = info

        weather = {}
        w_cond = (root.findtext(".//tenko") or root.findtext(".//weather") or "").strip()
        w_dir = parse_wind_dir_text(root.findtext(".//kazamuki") or root.findtext(".//wind_dir"))
        w_spd = to_float(root.findtext(".//fusoku") or root.findtext(".//wind_speed"))
        w_wave = to_float(root.findtext(".//hako") or root.findtext(".//wave"))
        if w_cond or w_spd is not None:
            weather = {
                "condition": w_cond or "現地発表",
                "windDir": w_dir,
                "windSpeed": w_spd if w_spd is not None else 0,
                "waveHeight": w_wave if w_wave is not None else 0,
                "stabilizer": False,
            }
        return {"boats": boats_info, "weather": weather}
    except Exception:
        return {"boats": {}, "weather": {}}


def fetch_beforeinfo(hd: str, rno: int) -> dict:
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={JCD}&hd={hd}"
    raw = fetch_bytes(url)
    if raw:
        soup = BeautifulSoup(raw, "html.parser")
        if "データがありません" not in soup.text:
            boats_info: dict[int, dict] = {}
            for idx, tbody in enumerate(soup.select("tbody.is-fs12")[:6], start=1):
                info: dict = {}
                tds = tbody.select("tr:first-child td")
                if len(tds) > 4:
                    ex_text = tds[4].get_text(strip=True).translate(ZEN_TO_HAN)
                    if re.match(r"^\d\.\d{2}$", ex_text):
                        info["exhibitionTime"] = float(ex_text)
                if len(tds) > 5:
                    tilt_text = tds[5].get_text(strip=True).translate(ZEN_TO_HAN)
                    if re.match(r"^[+-]?\d+(?:\.\d+)?$", tilt_text):
                        info["tilt"] = float(tilt_text)
                if len(tds) > 7:
                    parts = [
                        s.strip()
                        for s in tds[7].stripped_strings
                        if s.strip() and s.strip() not in ("-", "－", "なし")
                    ]
                    if parts:
                        info["partsChanged"] = parts
                if info:
                    boats_info[idx] = info

            for boat_img in soup.select("div.table1_boatImage1"):
                num_el = boat_img.select_one("span.table1_boatImage1Number")
                time_el = boat_img.select_one("span.table1_boatImage1Time")
                if not num_el or not time_el:
                    continue
                boat_txt = num_el.get_text(strip=True).translate(ZEN_TO_HAN)
                st_txt = time_el.get_text(strip=True).translate(ZEN_TO_HAN)
                if not re.match(r"^[1-6]$", boat_txt):
                    continue
                b_no = int(boat_txt)
                m_f = re.match(r"^F\.?(\d{2})$", st_txt, re.IGNORECASE)
                m_n = re.match(r"^\.?(\d{2})$", st_txt)
                if m_f:
                    boats_info.setdefault(b_no, {})["exhibitionST"] = -round(int(m_f.group(1)) / 100.0, 2)
                elif m_n:
                    boats_info.setdefault(b_no, {})["exhibitionST"] = round(int(m_n.group(1)) / 100.0, 2)

            notice_texts = " ".join(
                el.get_text(" ", strip=True)
                for el in soup.select("span.label2, div.weather1, h3.title16_titleDetail__add2020")
            )
            stabilizer = "安定板" in notice_texts

            weather: dict = {}
            weather_el = soup.select_one("div.weather1")
            if weather_el:
                cond_el = weather_el.select_one("div.is-weather span.weather1_bodyUnitLabelTitle")
                wind_el = weather_el.select_one("div.is-wind span.weather1_bodyUnitLabelData")
                wave_el = weather_el.select_one("div.is-wave span.weather1_bodyUnitLabelData")
                wind_dir_el = weather_el.select_one("div.is-windDirection p.weather1_bodyUnitImage")
                weather = {
                    "condition": cond_el.get_text(strip=True) if cond_el else "現地発表",
                    "windDir": map_wind_direction(wind_dir_el.get("class", [])) if wind_dir_el else "無風",
                    "windSpeed": to_float(wind_el.get_text(strip=True)) if wind_el else 0,
                    "waveHeight": to_float(wave_el.get_text(strip=True)) if wave_el else 0,
                    "stabilizer": stabilizer,
                }

            if boats_info or weather:
                return {"boats": boats_info, "weather": weather}

    return fetch_beforeinfo_from_toda_xml(hd, rno)


def fetch_original_xml(hd: str, rno: int) -> dict[int, dict]:
    url = f"{TODA_XML_BASE}/{hd}/race_table_original_{rno:02d}.xml"
    raw = fetch_bytes(url, timeout=6)
    if not raw:
        return {}

    result: dict[int, dict] = {}
    try:
        text = raw.decode("utf-8", errors="ignore").lstrip("\ufeff").strip()
        root = ET.fromstring(text)
        for idx, rec in enumerate(root.findall(".//record"), start=1):
            tb_raw = rec.get("teiban") or (rec.findtext("teiban") or rec.findtext("waku") or str(idx))
            tb_digits = re.sub(r"\D", "", tb_raw)
            if not tb_digits:
                continue
            tb = int(tb_digits)

            def pick(tags: list[str], min_v: float, max_v: float) -> float | None:
                for tag in tags:
                    val = to_float(rec.findtext(tag) or rec.get(tag))
                    if val is not None and min_v <= val <= max_v:
                        return val
                return None

            ori = {}
            isshu = pick(["rnd", "isshu", "isshutime", "otime1"], 30.0, 45.0)
            mawari = pick(["cnr", "mawari", "mawaritime", "otime2"], 4.0, 7.5)
            chokusen = pick(["str", "chokusen", "chokusentime", "otime3"], 5.8, 8.8)
            if isshu is not None:
                ori["oriIsshu"] = isshu
            if mawari is not None:
                ori["oriMawari"] = mawari
            if chokusen is not None:
                ori["oriChokusen"] = chokusen
            if ori:
                result[tb] = ori
    except Exception:
        return {}
    return result


def fetch_odds3t(hd: str, rno: int) -> dict[str, float]:
    xml_url = f"{TODA_XML_BASE}/{hd}/odds_{rno:02d}.xml"
    raw_xml = fetch_bytes(xml_url, timeout=5)
    if raw_xml:
        try:
            text = raw_xml.decode("utf-8", errors="ignore").lstrip("\ufeff").strip()
            root = ET.fromstring(text)
            odds_map: dict[str, float] = {}
            for elem in root.iter():
                m = re.match(r"^rt3_(\d)(\d)(\d)$", elem.tag, re.IGNORECASE)
                if m:
                    val = to_float(elem.text)
                    if val is not None and val > 0:
                        odds_map[f"{m.group(1)}-{m.group(2)}-{m.group(3)}"] = val
            if odds_map:
                return odds_map
        except Exception:
            pass

    url = f"https://www.boatrace.jp/owpc/pc/race/odds3t?rno={rno}&jcd={JCD}&hd={hd}"
    raw = fetch_bytes(url, timeout=8)
    if not raw:
        return {}
    soup = BeautifulSoup(raw, "html.parser")
    if "データがありません" in soup.text:
        return {}

    tbody = soup.select_one("tbody.is-p3-0")
    if not tbody:
        return {}

    odds_3t: dict[str, float] = {}
    for row in expand_table_grid(tbody):
        for g in range(6):
            c = g * 3
            if c + 2 >= len(row):
                continue
            b1 = g + 1
            b2_txt, b3_txt, o_txt = row[c], row[c + 1], row[c + 2]
            if re.match(r"^[1-6]$", b2_txt) and re.match(r"^[1-6]$", b3_txt):
                val = to_float(o_txt.replace(",", ""))
                if val is not None and val > 0:
                    odds_3t[f"{b1}-{b2_txt}-{b3_txt}"] = val
    return odds_3t


def fetch_raceresult(hd: str, rno: int) -> dict | None:
    url = f"https://www.boatrace.jp/owpc/pc/race/raceresult?rno={rno}&jcd={JCD}&hd={hd}"
    raw = fetch_bytes(url, timeout=8)
    if not raw:
        return None
    soup = BeautifulSoup(raw, "html.parser")
    if "データがありません" in soup.text:
        return None

    order: list[int] = []
    for table in soup.select("table.is-w495"):
        thead = table.select_one("thead")
        thead_text = thead.get_text(" ", strip=True) if thead else ""
        if "着" in thead_text and "枠" in thead_text:
            for tbody in table.select("tbody"):
                tds = tbody.select("tr:first-child td")
                if len(tds) >= 2:
                    rank_txt = tds[0].get_text(strip=True).translate(ZEN_TO_HAN)
                    boat_txt = tds[1].get_text(strip=True).translate(ZEN_TO_HAN)
                    if rank_txt in ("1", "2", "3") and re.match(r"^[1-6]$", boat_txt):
                        order.append(int(boat_txt))
                if len(order) >= 3:
                    break
            break

    kimarite = ""
    for th in soup.select("th"):
        if "決まり手" in th.get_text():
            table = th.find_parent("table")
            if table:
                td = table.select_one("tbody td")
                if td:
                    kimarite = td.get_text(strip=True)
            break

    payout_3t = None
    for tbody in soup.select("table.is-w495 tbody"):
        if "3連単" in tbody.get_text():
            if len(order) < 3:
                nums = [
                    int(el.get_text(strip=True).translate(ZEN_TO_HAN))
                    for el in tbody.select("span.numberSet1_number")
                    if re.match(r"^[1-6]$", el.get_text(strip=True).translate(ZEN_TO_HAN))
                ]
                if len(nums) >= 3:
                    order = nums[:3]
            payout_el = tbody.select_one("span.is-payout1")
            if payout_el:
                digits = re.sub(r"[^\d]", "", payout_el.get_text().translate(ZEN_TO_HAN))
                if digits:
                    payout_3t = int(digits)
            break

    if len(order) < 3:
        return None

    return {
        "order": order[:3],
        "combo": f"{order[0]}-{order[1]}-{order[2]}",
        "inNige": order[0] == 1,
        "kimarite": kimarite,
        "payout3t": payout_3t,
    }


def fetch_race_bundle(hd: str, rno: int) -> dict | None:
    with ThreadPoolExecutor(max_workers=5) as ex:
        fut_list = ex.submit(fetch_racelist, hd, rno)
        fut_before = ex.submit(fetch_beforeinfo, hd, rno)
        fut_ori = ex.submit(fetch_original_xml, hd, rno)
        fut_odds = ex.submit(fetch_odds3t, hd, rno)
        fut_res = ex.submit(fetch_raceresult, hd, rno)

        racers = fut_list.result()
        if not racers:
            return None

        before = fut_before.result()
        ori_map = fut_ori.result()
        odds3t = fut_odds.result()
        result = fut_res.result()

    before_boats = before.get("boats", {})
    for rc in racers:
        b_no = rc["boat"]
        if b_no in before_boats:
            rc.update(before_boats[b_no])
        if b_no in ori_map:
            rc.update(ori_map[b_no])

    return {
        "date": f"{hd[:4]}-{hd[4:6]}-{hd[6:8]}",
        "rno": rno,
        "updatedAt": datetime.now(JST).isoformat(),
        "cffi": HAS_CURL_CFFI,
        "weather": before.get("weather") or {},
        "racers": racers,
        "odds3t": odds3t,
        "result": result,
    }


def build_cors_json(payload: dict, status: int = 200, cache_control: str = "no-store"):
    resp = make_response(jsonify(payload), status)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Cache-Control"] = cache_control
    return resp


@app.route("/", methods=["GET", "OPTIONS"])
@app.route("/api/race", methods=["GET", "OPTIONS"])
def race_endpoint():
    if request.method == "OPTIONS":
        return build_cors_json({}, 204)

    try:
        raw_date = request.args.get("date", "")
        raw_rno = request.args.get("rno", "")

        hd = re.sub(r"\D", "", raw_date)
        if len(hd) != 8:
            return build_cors_json(
                {"error": "date は YYYYMMDD または YYYY-MM-DD 形式で指定してください"},
                400,
            )

        try:
            rno = int(raw_rno)
            if not (1 <= rno <= 12):
                raise ValueError
        except ValueError:
            return build_cors_json({"error": "rno は 1〜12 の数字で指定してください"}, 400)

        data = fetch_race_bundle(hd, rno)
        if not data:
            return build_cors_json(
                {"error": f"{hd} の戸田 {rno}R データが見つかりません（非開催日など）"},
                404,
            )

        cache_hdr = "public, max-age=3600, s-maxage=86400" if data.get("result") else "no-store"
        return build_cors_json(data, 200, cache_control=cache_hdr)
    except Exception as e:
        return build_cors_json(
            {"error": str(e), "trace": traceback.format_exc().splitlines()[-3:]},
            500,
        )
