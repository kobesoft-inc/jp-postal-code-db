#!/usr/bin/env python3
"""日本郵便の郵便番号データをダウンロードし、SQLite3データベースを生成する。

KEN_ALL(住所の郵便番号)とJIGYOSYO(大口事業所個別番号)の2つのデータソースを
生データの列構成のまま持つのではなく、以下の4テーブルに正規化する。

- prefectures      : 都道府県コード(prefecture_code) -> 都道府県名
- cities           : 市区町村コード -> 都道府県コード, 市区町村名
- postal_codes     : 郵便番号 -> 都道府県コード, 市区町村コード, 町名(住所続き)
- offices          : 郵便番号(大口事業所個別番号) -> 都道府県コード, 市区町村コード, 町名,
  番地等, 事業所名, 有効フラグ(is_enabled)
  - 廃止された個別番号（修正コード「5」）も除外せずに取り込み、is_enabledに0を立てる。
    is_enabledにインデックスを作成しているため、有効なものだけの絞り込みは高速に行える。

postal_codesの町名は、アプリケーションからそのまま住所文字列に使えるよう、以下の正規化を行う。

- 「以下に掲載がない場合」「〇〇の次に番地がくる場合」のような、町名が存在しない
  ことを表す自然言語の記述は空文字列に変換する。
- 「（１〜１９丁目）」のような丁目・番地の範囲や、「（その他）」のような補足の
  括弧書きは、町名としては不要な情報のため除去する。

officesの町名(town)は、JIGYOSYOの町域名欄をそのまま使うのではなく、postal_codesの
町名一覧と最長一致させて正規化する。JIGYOSYOの町域名は、同じ町を指していても
postal_codes側と表記が異なることがある（例:「北１条西」と「北一条西」のような
算用数字/漢数字の違い、「字」「大字」の有無）ため、町域名+番地等欄を連結した文字列に対して、
同一市区町村内のpostal_codes.townのうち最長一致するものを探し、一致すればその表記に
置き換える。一致するものが無ければ、JIGYOSYOの値をそのまま使う。
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

CREATE TABLE IF NOT EXISTS offices (
    postal_code     TEXT NOT NULL,
    prefecture_code TEXT NOT NULL REFERENCES prefectures (prefecture_code),
    city_code       TEXT NOT NULL REFERENCES cities (city_code),
    town            TEXT NOT NULL,
    detail          TEXT NOT NULL,
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


def clean_town(raw_town):
    town = raw_town.strip()
    for pattern in NO_TOWN_PATTERNS:
        if pattern.match(town):
            return ""
    town = PAREN_PATTERN.sub("", town).strip()
    return town


# --- officesのtown/detailを、postal_codesの町名一覧に最長一致させて正規化する -------

_KANJI_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_ARABIC_TO_KANJI = {v: k for k, v in _KANJI_DIGITS.items()}
_FULLWIDTH_DIGITS = "０１２３４５６７８９"
_KANJI_NUM_RE = re.compile(r"[一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九]")
_ARABIC_NUM_RE = re.compile(r"[０-９]+")


def _kanji_token_to_int(token):
    if token == "十":
        return 10
    if len(token) == 1:
        return _KANJI_DIGITS[token]
    if token[0] == "十":
        return 10 + _KANJI_DIGITS[token[1]]
    if token[-1] == "十":
        return _KANJI_DIGITS[token[0]] * 10
    return _KANJI_DIGITS[token[0]] * 10 + _KANJI_DIGITS[token[2]]


def _int_to_kanji(n):
    if n < 10:
        return _ARABIC_TO_KANJI[n]
    if n == 10:
        return "十"
    tens, ones = divmod(n, 10)
    s = ("" if tens == 1 else _ARABIC_TO_KANJI[tens]) + "十"
    return s + _ARABIC_TO_KANJI[ones] if ones else s


def kanji_numerals_to_arabic(text):
    """町名中の漢数字(一〜九十、十一〜九十九)を全角算用数字に変換する。"""
    to_fullwidth = str.maketrans("0123456789", _FULLWIDTH_DIGITS)
    return _KANJI_NUM_RE.sub(lambda m: str(_kanji_token_to_int(m.group(0))).translate(to_fullwidth), text)


def arabic_numerals_to_kanji(text):
    """町名中の全角算用数字(１〜99)を漢数字に変換する。"""
    from_fullwidth = str.maketrans(_FULLWIDTH_DIGITS, "0123456789")

    def repl(match):
        n = int(match.group(0).translate(from_fullwidth))
        return _int_to_kanji(n) if 1 <= n <= 99 else match.group(0)

    return _ARABIC_NUM_RE.sub(repl, text)


def build_town_candidates(postal_codes):
    """市区町村コードごとに、町名の表記ゆれ候補(表記->正規の町名)を、
    最長一致で見つけられるよう長い順に並べたリストとして作る。
    """
    towns_by_city = {}
    for _postal_code, _prefecture_code, city_code, town in postal_codes:
        if town:
            towns_by_city.setdefault(city_code, set()).add(town)

    candidates_by_city = {}
    for city_code, towns in towns_by_city.items():
        variant_to_town = {}
        for town in towns:
            variants = {town, kanji_numerals_to_arabic(town), arabic_numerals_to_kanji(town)}
            variants |= {"字" + v for v in variants} | {"大字" + v for v in variants}
            for variant in variants:
                variant_to_town.setdefault(variant, town)
        candidates_by_city[city_code] = sorted(variant_to_town.items(), key=lambda item: -len(item[0]))
    return candidates_by_city


def normalize_office_town(candidates_by_city, city_code, raw_town, raw_detail):
    """JIGYOSYOの町域名+番地等欄を、postal_codesの町名一覧に最長一致させて正規化する。
    一致するものが無ければ、元の(town, detail)をそのまま返す。
    """
    combined = raw_town + raw_detail
    for variant, town in candidates_by_city.get(city_code, ()):
        if combined.startswith(variant):
            return town, combined[len(variant):].strip()
    return raw_town, raw_detail


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
        postal_code = row[2]
        pref_name, city_name = row[6], row[7]
        prefecture_code, city_code = jis_code[:2], jis_code

        prefectures.setdefault(prefecture_code, pref_name)
        cities.setdefault(city_code, (prefecture_code, city_name))

        town = clean_town(row[8])
        key = (postal_code, city_code, town)
        if key in seen_postal_codes:
            continue
        seen_postal_codes.add(key)
        postal_codes.append((postal_code, prefecture_code, city_code, town))

    town_candidates_by_city = build_town_candidates(postal_codes)

    for row in fetch_csv_rows(jigyosyo_url, encoding="cp932"):
        jis_code = row[0]
        name = row[2]
        pref_name, city_name, raw_town, raw_detail = row[3], row[4], row[5], row[6]
        postal_code = row[7]
        prefecture_code, city_code = jis_code[:2], jis_code
        is_enabled = 0 if row[12] == JIGYOSYO_ABOLISHED_CODE else 1

        prefectures.setdefault(prefecture_code, pref_name)
        cities.setdefault(city_code, (prefecture_code, city_name))

        town, detail = normalize_office_town(
            town_candidates_by_city, city_code, raw_town.strip(), raw_detail.strip()
        )
        offices.append((postal_code, prefecture_code, city_code, town, detail, name.strip(), is_enabled))

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM offices")
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
            "INSERT INTO offices (postal_code, prefecture_code, city_code, town, detail, name, is_enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
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
