# jp-postal-code-sqlite3

日本郵便が公開している郵便番号データ（KEN_ALL）をダウンロードし、SQLite3データベースに変換するツールです。

## 使い方

Python 3系のみで動作します（追加の依存ライブラリは不要です）。

```bash
python3 build_db.py -o jp_postal_code.db
```

実行すると、日本郵便のサイトから最新の郵便番号データ（UTF-8版CSV）をダウンロードし、
カレントディレクトリに `jp_postal_code.db` を生成します。

## データソース

- 日本郵便 郵便番号データダウンロード（UTF-8版）
  https://www.post.japanpost.jp/zipcode/dl/utf-zip.html

## テーブル構成

KEN_ALLの列構成をそのまま持つのではなく、都道府県・市区町村をマスタテーブルに分離し、
`postal_codes` は 郵便番号 → 都道府県コード・市区町村コード・住所続き（町名） という
入力しやすい形に正規化しています。

### prefectures（都道府県マスタ）

| カラム | 内容 |
| --- | --- |
| pref_code | 都道府県コード（2桁、例: `13`） |
| name | 都道府県名（例: 東京都） |
| name_kana | 都道府県名カナ |

### cities（市区町村マスタ）

| カラム | 内容 |
| --- | --- |
| city_code | 市区町村コード（5桁、全国地方公共団体コードの上位5桁。例: `13101`） |
| pref_code | 都道府県コード（`prefectures.pref_code` を参照） |
| name | 市区町村名（例: 千代田区） |
| name_kana | 市区町村名カナ |

### postal_codes（郵便番号）

| カラム | 内容 |
| --- | --- |
| zip_code | 郵便番号（7桁、ハイフンなし） |
| pref_code | 都道府県コード（`prefectures.pref_code` を参照） |
| city_code | 市区町村コード（`cities.city_code` を参照） |
| town | 町名・住所続き（正規化済み。存在しない場合は空文字列） |

`zip_code` / `city_code` にインデックスを作成しているため、検索は高速に行えます。

```sql
SELECT pr.name AS pref, c.name AS city, p.town
FROM postal_codes p
JOIN prefectures pr ON pr.pref_code = p.pref_code
JOIN cities c ON c.city_code = p.city_code
WHERE p.zip_code = '1000001';
```

### town（住所続き）の正規化ルール

日本郵便のCSVには、町名の欄に自然言語の説明や範囲表記が含まれる箇所があるため、
住所として利用しやすいよう以下のルールで正規化しています。

- 「以下に掲載がない場合」「〇〇市の次に番地がくる場合」など、町名が存在しない
  ことを表す説明文は空文字列に変換します（郵便番号は市区町村までしか特定できないことを表します）。
- 「大通西（２０〜２８丁目）」のような丁目・番地の範囲や、「（その他）」「（次のビルを除く）」
  のような補足の括弧書きは除去し、町名部分（この例では「大通西」）のみを残します。
  これにより同じ町名で複数の郵便番号を持つケース（丁目違いなど）はそのままデータとして残ります。
- 上記の正規化によって郵便番号・市区町村・町名が完全に一致する重複行が生まれた場合は
  1件に集約しています。

なお、「川尻４０地割、川尻４１地割」のように読点区切りで複数の地名を列挙している
（括弧を使わない）ケースは、範囲表記ではなく実在の地名の可能性があるため正規化の対象外とし、
CSVの値をそのまま town に格納しています。

## 自動更新（GitHub Actions）

`.github/workflows/update-db.yml` により、毎月1日 09:00 JST（`cron: "0 0 1 * *"` UTC）に
自動で `build_db.py` を実行し、最新の `jp_postal_code.db` を生成してリポジトリに
コミット・pushします。差分が無い月はコミットしません。

`workflow_dispatch` にも対応しているため、GitHubのActionsタブから手動実行も可能です。

補足:

- 生成したDBファイル本体（約8MB）をリポジトリにそのままコミットしていくため、
  毎月の更新分だけリポジトリのサイズが増えていきます。サイズが問題になる場合は
  GitHub Releasesへのアップロード方式への切り替えを検討してください。
- ワークフローが `contents: write` 権限でpushするため、リポジトリの
  Settings > Actions > General > Workflow permissions が「Read and write」に
  なっている必要があります（ワークフロー内で明示指定しているため、通常は
  リポジトリ側の初期設定が read-only でも上書きされます）。

## ライセンス

このリポジトリのコードは MIT License です。
郵便番号データ自体の利用条件は日本郵便の定める規約に従います。
