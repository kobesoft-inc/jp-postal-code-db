#!/usr/bin/env python3
"""日本郵便の郵便番号データをダウンロードし、SQLite3データベースを生成する。

KEN_ALL(住所の郵便番号)とJIGYOSYO(大口事業所個別番号)の2つのデータソースを
生データの列構成のまま持つのではなく、以下の4テーブルに正規化する。

- prefectures      : 都道府県コード(prefecture_code) -> 都道府県名
- cities           : 市区町村コード -> 都道府県コード, 市区町村名
- postal_codes     : 郵便番号 -> 都道府県コード, 市区町村コード, 町名(住所続き)
- town_details     : 同じ町名が複数の郵便番号を持つ場合の、郵便番号ごとの補足情報
  （丁目範囲・番地範囲・地区名・京都の通り名等。詳細は後述）
- offices          : 郵便番号(大口事業所個別番号) -> 都道府県コード, 市区町村コード, 住所,
  事業所名, 有効フラグ(is_enabled)
  - 住所は町名以下を分割せず、JIGYOSYOの町域名+番地等欄をそのままつなげて格納する。
  - 廃止された個別番号（修正コード「5」）も除外せずに取り込み、is_enabledに0を立てる。
    is_enabledにインデックスを作成しているため、有効なものだけの絞り込みは高速に行える。

postal_codesの町名は、アプリケーションからそのまま住所文字列に使えるよう、以下の正規化を行う。

- 「以下に掲載がない場合」「〇〇の次に番地がくる場合」のような、町名が存在しない
  ことを表す自然言語の記述は空文字列に変換する。
- 「（１〜１９丁目）」のような丁目・番地の範囲や、「（その他）」のような補足の
  括弧書きは、町名としては不要な情報のため除去する。

上記の括弧書きは、同じ町名が複数の郵便番号に分かれる場合の判別情報でもあるため、
捨てずに town_details テーブルに退避する。実データを調べると、この判別情報は
「１〜１９丁目」のような単純な丁目範囲は全体の一部（1,046組中43組）に過ぎず、
大半は「南/北」「その他」「特定の小地区名」「京都の通り名（〜上る/下る等）」のような
丁目番号では表現できない自由記述になっている。そのため town_details は、括弧書きの
生テキストをそのまま保持することを基本とし、単純な丁目範囲として機械的に読み取れる
場合に限り chome_from/chome_to も付与する（それ以外は NULL のままで、番号の抽出はしない）。
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
    prefecture_code TEXT PRIMARY KEY,
    name            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cities (
    city_code       TEXT PRIMARY KEY,
    prefecture_code TEXT NOT NULL REFERENCES prefectures (prefecture_code),
    name            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cities_prefecture_code ON cities (prefecture_code);

CREATE TABLE IF NOT EXISTS postal_codes (
    postal_code     TEXT NOT NULL,
    prefecture_code TEXT NOT NULL REFERENCES prefectures (prefecture_code),
    city_code       TEXT NOT NULL REFERENCES cities (city_code),
    town            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postal_codes_postal_code ON postal_codes (postal_code);
CREATE INDEX IF NOT EXISTS idx_postal_codes_city_code ON postal_codes (city_code);

CREATE TABLE IF NOT EXISTS town_details (
    postal_code TEXT NOT NULL,
    city_code   TEXT NOT NULL,
    town        TEXT NOT NULL,
    detail      TEXT NOT NULL,
    chome_from  INTEGER,
    chome_to    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_town_details_city_town ON town_details (city_code, town);

CREATE TABLE IF NOT EXISTS offices (
    postal_code     TEXT NOT NULL,
    prefecture_code TEXT NOT NULL REFERENCES prefectures (prefecture_code),
    city_code       TEXT NOT NULL REFERENCES cities (city_code),
    address         TEXT NOT NULL,
    name            TEXT NOT NULL,
    is_enabled      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_offices_postal_code ON offices (postal_code);
CREATE INDEX IF NOT EXISTS idx_offices_city_code ON offices (city_code);
CREATE INDEX IF NOT EXISTS idx_offices_is_enabled ON offices (is_enabled);
"""

# 町名が存在しないことを表す自然言語の記述（KEN_ALLの慣習表記）
NO_TOWN_PATTERNS = [
    re.compile(r"^以下に掲載がない場合$"),
    re.compile(r".*の次に.*番地.*くる場合$"),
]

# 丁目・番地の範囲や補足を表す括弧書き（例: （１〜１９丁目）, （その他）, （次のビルを除く））
PAREN_PATTERN = re.compile(r"[（(][^（）()]*[）)]")
PAREN_CONTENT_PATTERN = re.compile(r"[（(]([^（）()]*)[）)]")

# 括弧書きが単純な丁目範囲/単一丁目として機械的に読み取れる場合のパターン
CHOME_RANGE_PATTERN = re.compile(r"^([0-9０-９]+)[〜～\-‐−~]([0-9０-９]+)丁目$")
CHOME_SINGLE_PATTERN = re.compile(r"^([0-9０-９]+)丁目$")
_FULLWIDTH_TO_HALFWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")


def clean_town(raw_town):
    town = raw_town.strip()
    for pattern in NO_TOWN_PATTERNS:
        if pattern.match(town):
            return ""
    town = PAREN_PATTERN.sub("", town).strip()
    return town


def extract_detail(raw_town):
    """町名欄の括弧書き（複数あれば「、」で連結）を取り出す。無ければ空文字列。"""
    return "、".join(PAREN_CONTENT_PATTERN.findall(raw_town))


def parse_chome_range(detail):
    """detailが単純な丁目範囲/単一丁目であれば(from, to)を返す。読み取れなければNone。"""
    match = CHOME_RANGE_PATTERN.match(detail)
    if match:
        return (
            int(match.group(1).translate(_FULLWIDTH_TO_HALFWIDTH)),
            int(match.group(2).translate(_FULLWIDTH_TO_HALFWIDTH)),
        )
    match = CHOME_SINGLE_PATTERN.match(detail)
    if match:
        n = int(match.group(1).translate(_FULLWIDTH_TO_HALFWIDTH))
        return (n, n)
    return None


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

    # (city_code, town) -> { postal_code: detail生テキスト }
    details_by_town = {}

    for row in fetch_csv_rows(ken_all_url):
        jis_code = row[0]
        postal_code = row[2]
        pref_name, city_name = row[6], row[7]
        prefecture_code, city_code = jis_code[:2], jis_code

        prefectures.setdefault(prefecture_code, pref_name)
        cities.setdefault(city_code, (prefecture_code, city_name))

        raw_town = row[8]
        town = clean_town(raw_town)
        key = (postal_code, city_code, town)
        if key in seen_postal_codes:
            continue
        seen_postal_codes.add(key)
        postal_codes.append((postal_code, prefecture_code, city_code, town))

        if town:
            details_by_town.setdefault((city_code, town), {})[postal_code] = extract_detail(raw_town)

    # 同じ(city_code, town)が複数の郵便番号を持つ場合のみ、判別情報をtown_detailsに残す
    town_details = []
    for (city_code, town), postal_code_to_detail in details_by_town.items():
        if len(postal_code_to_detail) <= 1:
            continue
        for postal_code, detail in postal_code_to_detail.items():
            chome_range = parse_chome_range(detail) if detail else None
            chome_from, chome_to = chome_range if chome_range else (None, None)
            town_details.append((postal_code, city_code, town, detail, chome_from, chome_to))

    for row in fetch_csv_rows(jigyosyo_url, encoding="cp932"):
        jis_code = row[0]
        name = row[2]
        pref_name, city_name = row[3], row[4]
        address = (row[5] + row[6]).strip()
        postal_code = row[7]
        prefecture_code, city_code = jis_code[:2], jis_code
        is_enabled = 0 if row[12] == JIGYOSYO_ABOLISHED_CODE else 1

        prefectures.setdefault(prefecture_code, pref_name)
        cities.setdefault(city_code, (prefecture_code, city_name))

        offices.append((postal_code, prefecture_code, city_code, address, name.strip(), is_enabled))

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM offices")
        conn.execute("DELETE FROM town_details")
        conn.execute("DELETE FROM postal_codes")
        conn.execute("DELETE FROM cities")
        conn.execute("DELETE FROM prefectures")

        conn.executemany(
            "INSERT INTO prefectures (prefecture_code, name) VALUES (?, ?)",
            list(prefectures.items()),
        )
        conn.executemany(
            "INSERT INTO cities (city_code, prefecture_code, name) VALUES (?, ?, ?)",
            [(code, prefecture_code, name) for code, (prefecture_code, name) in cities.items()],
        )
        conn.executemany(
            "INSERT INTO postal_codes (postal_code, prefecture_code, city_code, town) VALUES (?, ?, ?, ?)",
            postal_codes,
        )
        conn.executemany(
            "INSERT INTO town_details (postal_code, city_code, town, detail, chome_from, chome_to) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            town_details,
        )
        conn.executemany(
            "INSERT INTO offices (postal_code, prefecture_code, city_code, address, name, is_enabled) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            offices,
        )
        conn.commit()

        print(
            f"prefectures: {len(prefectures)} 件, "
            f"cities: {len(cities)} 件, "
            f"postal_codes: {len(postal_codes)} 件, "
            f"town_details: {len(town_details)} 件, "
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
