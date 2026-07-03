#!/usr/bin/env python3
"""KEN_ALL/JIGYOSYOのCSV本文（zipの中身）のMD5を標準出力に表示する。

ダウンロードした zip ファイル自体のMD5は使わない。zipの内部タイムスタンプ等の
メタデータにより、CSVの中身が同じでも zip 全体のMD5は変わり得るため。
"""

import argparse
import hashlib

from build_db import JIGYOSYO_URL, KEN_ALL_URL, fetch_csv_text

# (URL, エンコーディング)
SOURCES = {
    "ken_all": (KEN_ALL_URL, "utf-8"),
    "jigyosyo": (JIGYOSYO_URL, "cp932"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=SOURCES, default="ken_all", help="対象データソース")
    args = parser.parse_args()
    url, encoding = SOURCES[args.source]
    csv_text = fetch_csv_text(url, encoding)
    print(hashlib.md5(csv_text.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    main()
