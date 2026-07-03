#!/usr/bin/env python3
"""KEN_ALLのCSV本文（zipの中身）のMD5を標準出力に表示する。

ダウンロードした zip ファイル自体のMD5は使わない。zipの内部タイムスタンプ等の
メタデータにより、CSVの中身が同じでも zip 全体のMD5は変わり得るため。
"""

import argparse
import hashlib

from build_db import KEN_ALL_URL, fetch_csv_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=KEN_ALL_URL, help="KEN_ALL CSV(zip)のダウンロードURL")
    args = parser.parse_args()
    csv_text = fetch_csv_text(args.url)
    print(hashlib.md5(csv_text.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    main()
