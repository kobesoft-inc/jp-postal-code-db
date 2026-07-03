# jp-postal-code-sqlite3

日本郵便が公開している郵便番号データ（住所の郵便番号 KEN_ALL、大口事業所個別番号 JIGYOSYO）を
ダウンロードし、SQLite3データベースに変換するツールです。

## 使い方

### 1. データをダウンロードする

常に最新のデータが [GitHub Releases](https://github.com/kobesoft-inc/jp-postal-code-sqlite3/releases) に
`jp_postal_code.db` として公開されています。以下のURLで常に最新版を取得できます。

```
https://github.com/kobesoft-inc/jp-postal-code-sqlite3/releases/latest/download/jp_postal_code.db
```

```bash
curl -L -o jp_postal_code.db \
  https://github.com/kobesoft-inc/jp-postal-code-sqlite3/releases/latest/download/jp_postal_code.db
```

### 2. テーブル構造

KEN_ALLの列構成をそのまま持つのではなく、都道府県・市区町村をマスタテーブルに分離し、
`postal_codes` は 郵便番号 → 都道府県コード・市区町村コード・住所続き（町名） という
入力しやすい形に正規化しています。

#### prefectures（都道府県マスタ）

| カラム | 内容 |
| --- | --- |
| pref_code | 都道府県コード（2桁、例: `13`） |
| name | 都道府県名（例: 東京都） |
| name_kana | 都道府県名カナ |

#### cities（市区町村マスタ）

| カラム | 内容 |
| --- | --- |
| city_code | 市区町村コード（5桁、全国地方公共団体コードの上位5桁。例: `13101`） |
| pref_code | 都道府県コード（`prefectures.pref_code` を参照） |
| name | 市区町村名（例: 千代田区） |
| name_kana | 市区町村名カナ |

#### postal_codes（郵便番号）

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

#### town（住所続き）の正規化ルール

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

#### offices（大口事業所個別番号）

配達物数の多い事業所・私書箱利用者に割り当てられる個別の郵便番号（JIGYOSYO）です。
1つの建物・組織に複数の郵便番号が割り当てられることもあれば、逆に同じ郵便番号を
複数の部署・関連組織で共有していることもあります（例: 同一キャンパス内の大学本部・各学部）。

| カラム | 内容 |
| --- | --- |
| zip_code | 郵便番号（7桁、ハイフンなし。一意ではない場合がある） |
| pref_code | 都道府県コード（`prefectures.pref_code` を参照） |
| city_code | 市区町村コード（`cities.city_code` を参照） |
| town | 町名（postal_codesと違い個別の実住所のため範囲表記は無く、正規化は行っていない） |
| name | 事業所名・私書箱利用者名（漢字） |
| name_kana | 事業所名・私書箱利用者名（半角カナ、原データのまま） |

元データの「修正コード」が「廃止」を表す行は取り込み対象外にしており、有効なものだけを格納しています。
小字名・丁目・番地・私書箱番号などの詳細な住所情報（元データの列）は取り込んでいません。
郵便番号・都道府県・市区町村・町名までの補完と事業所名の特定ができれば十分、という設計です。

```sql
SELECT pr.name AS pref, c.name AS city, b.town, b.name
FROM offices b
JOIN prefectures pr ON pr.pref_code = b.pref_code
JOIN cities c ON c.city_code = b.city_code
WHERE b.zip_code = '1008798';
```

## 自動更新（GitHub Actions）

`.github/workflows/update-db.yml` により、毎日 09:00 JST（`cron: "0 0 * * *"` UTC）に
以下を自動実行します。`workflow_dispatch` にも対応しているため、GitHubのActionsタブから
手動実行も可能です。

1. `check_source_md5.py` で日本郵便のKEN_ALL・JIGYOSYO各CSV本文のMD5を計算し、リポジトリ内の
   `ken_all.csv.md5` / `jigyosyo.csv.md5` に記録済みの値とそれぞれ比較する。
2. どちらのMD5も変化していなければ、そこで終了（DB生成・リリースは行わない）。
3. どちらかのMD5が変化していれば `build_db.py` でDBを再生成し、
   [GitHub Releases](https://github.com/kobesoft-inc/jp-postal-code-sqlite3/releases) に
   `jp_postal_code.db` を添付した新しいリリースを作成する
   （タグ名は `db-YYYY-MM-DD-<KEN_ALL MD5先頭8桁>-<JIGYOSYO MD5先頭8桁>`）。
   `ken_all.csv.md5` / `jigyosyo.csv.md5` の更新もリポジトリにコミットする。

DBファイル自体はリポジトリにはコミットせず、常にReleasesの最新版から取得する運用です
（`git`の履歴が肥大化しないための設計）。取得方法は [使い方](#使い方) を参照してください。

補足:

- CSVの中身ではなく zip ファイル自体のMD5を使うと、zip内部のタイムスタンプ等の
  メタデータの違いだけで「更新あり」と誤検知する可能性があるため、
  zipを展開したCSV本文のMD5で比較しています。
- ワークフローが `contents: write` 権限でpush・リリース作成を行うため、リポジトリの
  Settings > Actions > General > Workflow permissions が「Read and write」に
  なっている必要があります（ワークフロー内で明示指定しているため、通常は
  リポジトリ側の初期設定が read-only でも上書きされます）。

## 自分でDBをビルドする場合（開発者向け）

Python 3系のみで動作します（追加の依存ライブラリは不要です）。

```bash
python3 build_db.py -o jp_postal_code.db
```

実行すると、日本郵便のサイトから最新の郵便番号データ（KEN_ALL・JIGYOSYO）をダウンロードし、
カレントディレクトリに `jp_postal_code.db` を生成します。

データソース:

- 住所の郵便番号（KEN_ALL、UTF-8版CSV）
  https://www.post.japanpost.jp/zipcode/dl/utf-zip.html
- 大口事業所個別番号（JIGYOSYO、Shift-JIS/cp932のCSV。UTF-8版は無い）
  https://www.post.japanpost.jp/zipcode/dl/jigyosyo/index-zip.html

## ライセンス

このリポジトリのコードは MIT License です。
郵便番号データ自体の利用条件は日本郵便の定める規約に従います
（JIGYOSYOデータについては日本郵便が著作権を主張しないと明記されており、自由に配布可能です）。
