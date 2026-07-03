#!/usr/bin/env python3
"""日本郵便の郵便番号データをダウンロードし、SQLite3データベースを生成する。

KEN_ALL(住所の郵便番号)とJIGYOSYO(大口事業所個別番号)の2つのデータソースを
生データの列構成のまま持つのではなく、以下の4テーブルに正規化する。

- prefectures      : 都道府県コード -> 都道府県名
- cities           : 市区町村コード -> 市区町村名
- postal_codes     : 郵便番号 -> 都道府県コード, 市区町村コード, 町名(住所続き)
- offices : 郵便番号(大口事業所個別番号) -> 都道府県コード, 市区町村コード, 町名, 事業所名

町名はアプリケーションからそのまま住所文字列に使えるよう、以下の正規化を行う
（postal_codesのみ。officesの町名は個々の事業所固有の住所のため範囲表記が無く、正規化不要）。

- 「以下に掲載がない場合」「〇〇の次に番地がくる場合」のような、町名が存在しない
  ことを表す自然言語の記述は空文字列に変換する。
- 「（１〜１９丁目）」のような丁目・番地の範囲や、「（その他）」のような補足の
  括弧書きは、町名としては不要な情報のため除去する。
"""

import argparse
import csv
import io
import re
import sqlite3
import sys
import urllib.request
import zipfile

KEN_ALL_URL = "https://www.post.japanpost.jp/service/search/zipcode/download/utf/zip/utf_ken_all.zip"
JIGYOSYO_URL = "https://www.post.japanpost.jp/service/search/zipcode/download/office/zip/jigyosyo.zip"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prefectures (
    pref_code TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    name_kana TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cities (
    city_code TEXT PRIMARY KEY,
    pref_code TEXT NOT NULL REFERENCES prefectures (pref_code),
    name      TEXT NOT NULL,
    name_kana TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cities_pref_code ON cities (pref_code);

CREATE TABLE IF NOT EXISTS postal_codes (
    zip_code  TEXT NOT NULL,
    pref_code TEXT NOT NULL REFERENCES prefectures (pref_code),
    city_code TEXT NOT NULL REFERENCES cities (city_code),
    town      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postal_codes_zip_code ON postal_codes (zip_code);
CREATE INDEX IF NOT EXISTS idx_postal_codes_city_code ON postal_codes (city_code);

CREATE TABLE IF NOT EXISTS offices (
    zip_code  TEXT NOT NULL,
    pref_code TEXT NOT NULL REFERENCES prefectures (pref_code),
    city_code TEXT NOT NULL REFERENCES cities (city_code),
    town      TEXT NOT NULL,
    name      TEXT NOT NULL,
    name_kana TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_offices_zip_code ON offices (zip_code);
CREATE INDEX IF NOT EXISTS idx_offices_city_code ON offices (city_code);
"""

# 町名が存在しないことを表す自然言語の記述（KEN_ALLの慣習表記）
NO_TOWN_PATTERNS = [
    re.compile(r"^以下に掲載がない場合$"),
    re.compile(r".*の次に.*番地.*くる場合$"),
]

# 丁目・番地の範囲や補足を表す括弧書き（例: （１〜１９丁目）, （その他）, （次のビルを除く））
PAREN_PATTERN = re.compile(r"[（(][^（）()]*[）)]")


def clean_town(raw_town):
    town = raw_town.strip()
    for pattern in NO_TOWN_PATTERNS:
        if pattern.match(town):
            return ""
    town = PAREN_PATTERN.sub("", town).strip()
    return town


def fetch_csv_text(url, encoding="utf-8"):
    with urllib.request.urlopen(url) as res:
        data = res.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        csv_name = next(name for name in zf.namelist() if name.lower().endswith(".csv"))
        with zf.open(csv_name) as f:
            return f.read().decode(encoding)


def fetch_csv_rows(url, encoding="utf-8"):
    yield from csv.reader(io.StringIO(fetch_csv_text(url, encoding)))


# JIGYOSYO.CSVの修正コード（12列目）: 「5」は廃止された個別番号を表す
JIGYOSYO_ABOLISHED_CODE = "5"


def build_database(db_path, ken_all_url, jigyosyo_url):
    prefectures = {}
    cities = {}
    postal_codes = []
    seen_postal_codes = set()
    offices = []

    for row in fetch_csv_rows(ken_all_url):
        jis_code = row[0]
        zip_code = row[2]
        pref_kana, city_kana = row[3], row[4]
        pref_name, city_name = row[6], row[7]
        pref_code, city_code = jis_code[:2], jis_code

        prefectures.setdefault(pref_code, (pref_name, pref_kana))
        cities.setdefault(city_code, (pref_code, city_name, city_kana))

        town = clean_town(row[8])
        key = (zip_code, city_code, town)
        if key in seen_postal_codes:
            continue
        seen_postal_codes.add(key)
        postal_codes.append((zip_code, pref_code, city_code, town))

    for row in fetch_csv_rows(jigyosyo_url, encoding="cp932"):
        if row[12] == JIGYOSYO_ABOLISHED_CODE:
            continue

        jis_code = row[0]
        name_kana, name = row[1], row[2]
        pref_name, city_name, town = row[3], row[4], row[5]
        zip_code = row[7]
        pref_code, city_code = jis_code[:2], jis_code

        # JIGYOSYOには都道府県名・市区町村名のカナが無いため、KEN_ALLに無い
        # コードが例外的に出てきた場合のみ漢字表記をカナ代わりに補う
        prefectures.setdefault(pref_code, (pref_name, pref_name))
        cities.setdefault(city_code, (pref_code, city_name, city_name))

        offices.append((zip_code, pref_code, city_code, town.strip(), name.strip(), name_kana.strip()))

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM offices")
        conn.execute("DELETE FROM postal_codes")
        conn.execute("DELETE FROM cities")
        conn.execute("DELETE FROM prefectures")

        conn.executemany(
            "INSERT INTO prefectures (pref_code, name, name_kana) VALUES (?, ?, ?)",
            [(code, name, kana) for code, (name, kana) in prefectures.items()],
        )
        conn.executemany(
            "INSERT INTO cities (city_code, pref_code, name, name_kana) VALUES (?, ?, ?, ?)",
            [(code, pref_code, name, kana) for code, (pref_code, name, kana) in cities.items()],
        )
        conn.executemany(
            "INSERT INTO postal_codes (zip_code, pref_code, city_code, town) VALUES (?, ?, ?, ?)",
            postal_codes,
        )
        conn.executemany(
            "INSERT INTO offices (zip_code, pref_code, city_code, town, name, name_kana) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            offices,
        )
        conn.commit()

        print(
            f"prefectures: {len(prefectures)} 件, "
            f"cities: {len(cities)} 件, "
            f"postal_codes: {len(postal_codes)} 件, "
            f"offices: {len(offices)} 件 を {db_path} に書き込みました。",
            file=sys.stderr,
        )
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", default="jp_postal_code.db", help="出力先のSQLite3ファイルパス")
    parser.add_argument("--url", default=KEN_ALL_URL, help="KEN_ALL CSV(zip)のダウンロードURL")
    parser.add_argument("--jigyosyo-url", default=JIGYOSYO_URL, help="JIGYOSYO CSV(zip)のダウンロードURL")
    args = parser.parse_args()
    build_database(args.output, args.url, args.jigyosyo_url)


if __name__ == "__main__":
    main()
