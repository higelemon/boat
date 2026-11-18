from datetime import datetime, timedelta
import io
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import lhafile
import pandas as pd
import requests

DATA_DIR = '.'  // CSVを保存するディレクトリ（GitHubリポジトリ内）

def to_half_width(s):
  """全角英数字・記号を半角に変換"""
  return s.translate(
      str.maketrans(
          '０１２３４５６７８９．－ＦＬＳＫ',
          '0123456789.-FLSK',
      )
  )

def download_and_parse_lzh(date_str):
  """mbraceの公式結果アーカイブ(kYYMMDD.lzh)を取得・解凍し、戸田(02場)の全レースを漏れなく抽出"""
  yy = date_str[2:4]
  mm = date_str[4:6]
  dd = date_str[6:8]
  date_formatted = f'{date_str[:4]}-{mm}-{dd}'

  url = f'https://www1.mbrace.or.jp/od2/K/{date_str[:6]}/k{yy}{mm}{dd}.lzh'
  headers = {'User-Agent': 'Mozilla/5.0'}

  res = None
  for _ in range(2):
    try:
      r = requests.get(url, headers=headers, timeout=12)
      if r.status_code == 200 and len(r.content) > 100:
        res = r
        break
      elif r.status_code == 404:
        return date_formatted, [], []
    except Exception:
      continue

  if res is None:
    return date_formatted, [], [f'{date_formatted}: LZHダウンロード失敗']

  try:
    lzh = lhafile.Lhafile(io.BytesIO(res.content))
    txt_name = lzh.namelist()[0]
    text = lzh.read(txt_name).decode('cp932', errors='ignore')
  except Exception as e:
    return date_formatted, [], [f'{date_formatted}: LZH解凍エラー ({e})']

  m_toda = re.search(r'02KBGN(.*?)02KEND', text, re.DOTALL)
  if not m_toda:
    return date_formatted, [], []

  toda_text = m_toda.group(1)
  lines = toda_text.splitlines()

  day_rows = []
  warnings = []
  curr_rno = None
  weather, wind_dir, wind_speed = '晴', '無風', 0.0
  course_st = [None] * 6
  ranks = {}
  payout = 0
  in_result_table = False

  def flush_race():
    if curr_rno is None:
      return
    if len(ranks) >= 3:
      day_rows.append({
          '日付': date_formatted,
          'レース番号': curr_rno,
          '天気': weather,
          '風向': wind_dir,
          '風速': wind_speed,
          '1コースST': course_st[0],
          '2コースST': course_st[1],
          '3コースST': course_st[2],
          '4コースST': course_st[3],
          '5コースST': course_st[4],
          '6コースST': course_st[5],
          '1着': ranks.get(1),
          '2着': ranks.get(2),
          '3着': ranks.get(3),
          '配当': payout,
      })

  for raw_line in lines:
    line = to_half_width(raw_line)
    m_head = re.match(r'^\s*(\d{1,2})R\s+.*?H\d+m\s+(\S+)\s+風\s+(\S+)\s+(\d+)m', line)
    if m_head:
      flush_race()
      curr_rno = int(m_head.group(1))
      weather = m_head.group(2)
      wind_dir = m_head.group(3)
      wind_speed = float(m_head.group(4))
      course_st = [None] * 6
      ranks = {}
      payout = 0
      in_result_table = False
      continue

    if curr_rno is None:
      continue

    if re.match(r'^\s*-{20,}', line):
      in_result_table = True
      continue

    if in_result_table:
      m_res = re.match(
          r'^\s*(0[1-6]|[FLSK転落失妨エ不欠]{1,2})\s+([1-6])\s+(\d{4})\s+(.+?)\s+\d+\s+\d+\s+[\d.-]+\s+([1-6])\s+([FL]?\d*\.\d+|---)',
          line,
      )
      if m_res:
        chaku_str = m_res.group(1)
        waku = int(m_res.group(2))
        course = int(m_res.group(5))
        st_val = m_res.group(6)

        if st_val != '---':
          st_val = re.sub(r'^([FL]?)0\.', r'\1.', st_val)
        else:
          st_val = None

        if chaku_str in ['01', '02', '03']:
          ranks[int(chaku_str)] = waku
        if 1 <= course <= 6:
          course_st[course - 1] = st_val
        continue

    if '3連単' in line or '３連単' in raw_line:
      in_result_table = False
      m_pay = re.search(r'[1-6]-[1-6]-[1-6]\s+(\d+)', line)
      if m_pay and payout == 0:
        payout = int(m_pay.group(1))

  flush_race()
  return date_formatted, day_rows, warnings

def update_yearly_data():
  today = datetime.now()
  current_year = today.year
  csv_filename = os.path.join(DATA_DIR, f'demetodadata_{current_year}.csv')

  # 1. 既存のCSVがあれば読み込み、最新の日付を特定する
  existing_df = pd.DataFrame()
  if os.path.exists(csv_filename):
    try:
      existing_df = pd.read_csv(csv_filename, encoding='utf-8-sig')
      if not existing_df.empty and '日付' in existing_df.columns:
        latest_date_str = existing_df['日付'].max()
        start_date = datetime.strptime(latest_date_str, '%Y-%m-%d') + timedelta(days=1)
      else:
        start_date = datetime(current_year, 1, 1)
    except Exception:
      start_date = datetime(current_year, 1, 1)
  else:
    start_date = datetime(current_year, 1, 1)

  end_date = today

  # まだ取得すべき新しい日付がない場合は終了
  if start_date > end_date:
    print(f'[-] {current_year}年のデータは既に最新です。')
    return

  # 取得対象の日付リストを作成
  dates = []
  curr = start_date
  while curr <= end_date:
    dates.append(curr.strftime('%Y%m%d'))
    curr += timedelta(days=1)

  print(f'[*] {current_year}年の差分データ取得中（{start_date.strftime("%Y-%m-%d")} ～ {end_date.strftime("%Y-%m-%d")} / 全 {len(dates)} 日）...')

  new_rows = []
  with ThreadPoolExecutor(max_workers=4) as executor:
    futures = {executor.submit(download_and_parse_lzh, d): d for d in dates}
    for future in as_completed(futures):
      date_fmt, day_rows, _ = future.result()
      if day_rows:
        new_rows.extend(day_rows)
        print(f'[+] 差分取得成功: {date_fmt} ({len(day_rows)}レース)')

  if not new_rows:
    print('[-] 新規に取得できるデータはありませんでした。')
    return

  new_df = pd.DataFrame(new_rows)
  
  # 既存データと結合して重複排除・ソート
  if not existing_df.empty:
    combined_df = pd.concat([existing_df, new_df]).drop_duplicates(subset=['日付', 'レース番号'], keep='last')
  else:
    combined_df = new_df

  combined_df = combined_df.sort_values(['日付', 'レース番号']).reset_index(drop=True)
  combined_df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
  print(f'[★] 完了: {csv_filename} を更新しました（総レコード数: {len(combined_df)}）')

if __name__ == '__main__':
  update_yearly_data()
