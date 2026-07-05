# jp-postal-code-db

日本郵便が公開している郵便番号データを、そのまま使えるSQLite3データベースにして配布しています。
毎日自動チェックし、元データが更新されていれば追随します（[GitHub Actions](.github/workflows/update-db.yml)）。

- **住所の郵便番号**（KEN_ALL）→ `postal_codes` テーブル
- **事業所個別番号**（JIGYOSYO。大口事業所・私書箱に割り当てられる専用の郵便番号）→ `offices` テーブル

## ダウンロード

以下のURLから常に最新版を取得できます（[Releases](https://github.com/kobesoft-inc/jp-postal-code-db/releases)）。

```
https://github.com/kobesoft-inc/jp-postal-code-db/releases/latest/download/jp_postal_code.db
```

```bash
curl -L -o jp_postal_code.db \
  https://github.com/kobesoft-inc/jp-postal-code-db/releases/latest/download/jp_postal_code.db
```

## テーブル構成

都道府県・市区町村はマスタテーブルに分離し、`postal_codes`・`offices`はそこへのコード参照だけを
持つ形にしています。

### prefectures（都道府県マスタ）

| カラム | 内容 |
| --- | --- |
| prefecture_code | 都道府県コード（2桁、例: `13`） |
| name | 都道府県名（例: 東京都） |

### cities（市区町村マスタ）

| カラム | 内容 |
| --- | --- |
| city_code | 市区町村コード（5桁 = 都道府県コード2桁 + 市区町村コード3桁。例: `13101`） |
| prefecture_code | 都道府県コード（`prefectures.prefecture_code` を参照） |
| name | 市区町村名（例: 千代田区） |

### postal_codes（郵便番号 → 住所）

| カラム | 内容 |
| --- | --- |
| postal_code | 郵便番号（7桁、ハイフンなし） |
| prefecture_code | 都道府県コード（`prefectures.prefecture_code` を参照） |
| city_code | 市区町村コード（`cities.city_code` を参照） |
| town | 町名・住所続き（正規化済み。町名が存在しない場合は空文字列） |

`postal_code`・`city_code`にインデックスがあるため、どちらのキーでの検索も高速です。

```sql
SELECT pr.name AS pref, c.name AS city, p.town
FROM postal_codes p
JOIN prefectures pr ON pr.prefecture_code = p.prefecture_code
JOIN cities c ON c.city_code = p.city_code
WHERE p.postal_code = '1000001';
-- 東京都 千代田区 千代田
```

**town列の正規化について**: 元データの町名欄には、「以下に掲載がない場合」「〇〇市の次に
番地がくる場合」のように町名が存在しないことを表す説明文や、「大通西（２０〜２８丁目）」の
ように丁目・番地の範囲を表す括弧書きが含まれることがあります。これらはそのまま検索・表示に
使いづらいため、前者は空文字列に、後者は括弧部分を除去して町名部分のみ（この例では「大通西」）
になるよう正規化しています。そのため、同じ町名で複数の郵便番号を持つケース（丁目違いなど）が
そのままデータに残っています（この判別情報は捨てておらず、`town_details`に退避しています）。

### town_details（同じ町名が複数の郵便番号を持つ場合の判別情報）

| カラム | 内容 |
| --- | --- |
| postal_code | 郵便番号（7桁、ハイフンなし） |
| city_code | 市区町村コード（`cities.city_code` を参照） |
| town | 町名（`postal_codes.town` と同じ表記） |
| detail | 元データの括弧書きの生テキスト（例:「１〜１９丁目」「南」「その他」「烏丸通今出川上る」等） |
| chome_from, chome_to | detailが単純な丁目範囲として機械的に読み取れる場合のみ整数が入る。それ以外はNULL |

同じ`(city_code, town)`が1つの郵便番号にしか対応しない場合（全体の99%以上）は、
このテーブルにはデータが無く、`postal_codes`から直接一意に郵便番号が引けます。

このテーブルが必要になるのは、同じ町名が複数の郵便番号に分かれているケースです。
実際のデータを調べると、その判別情報のほとんどは丁目番号では表現できません
（該当する1,046組のうち、単純な丁目範囲で説明できるのは43組のみ）。「南/北」のような方角、
「その他」という残り一括、特定の小地区名、京都特有の通り名（「烏丸通今出川上る」等）など、
自由記述に近いバリエーションがあるため、`detail`には元の生テキストをそのまま保持しています。
`chome_from`/`chome_to`は、丁目範囲として機械的に読み取れた場合のみの補助的な情報であり、
それ以外の判別は文字列としての`detail`を手がかりにする必要があります。

```sql
-- 「大通西」で複数の郵便番号に分かれているケースを確認する
SELECT * FROM town_details WHERE city_code = '01101' AND town = '大通西';
-- postal_code=0600042, detail=１〜１９丁目, chome_from=1,  chome_to=19
-- postal_code=0640820, detail=２０〜２８丁目, chome_from=20, chome_to=28
```

### offices（事業所個別番号 → 住所・事業所名）

配達物数の多い事業所や私書箱利用者に割り当てられる、専用の郵便番号です。1つの建物・組織に
複数の郵便番号が割り当てられることもあれば、逆に同じ郵便番号を複数の部署・関連組織で共有して
いることもあります（例: 同一キャンパス内の大学本部・各学部）。廃止された番号も含めて収録して
います。

| カラム | 内容 |
| --- | --- |
| postal_code | 郵便番号（7桁、ハイフンなし。同じ番号が複数行に出てくることがある） |
| prefecture_code | 都道府県コード（`prefectures.prefecture_code` を参照） |
| city_code | 市区町村コード（`cities.city_code` を参照） |
| address | 町名から続く住所（町域名+丁目・番地・建物名等。元データのまま、分割・正規化はしていない） |
| name | 事業所名・私書箱利用者名（漢字） |
| is_enabled | 現在有効な個別番号かどうか（1=有効, 0=廃止済み） |

現存する番号だけが欲しい場合は`is_enabled = 1`で絞り込んでください。インデックスがあるため
高速です。

```sql
-- 現存する番号だけを検索（インデックスが使われる）
SELECT pr.name AS pref, c.name AS city, o.address, o.name
FROM offices o
JOIN prefectures pr ON pr.prefecture_code = o.prefecture_code
JOIN cities c ON c.city_code = o.city_code
WHERE o.postal_code = '1008798'
  AND o.is_enabled = 1;
```

## 更新頻度

毎日09:00 JSTに、日本郵便側のデータが更新されているかをチェックし、更新があった場合のみ
DBを再生成して最新版をリリースします（更新が無い日は何もしません）。過去のリリースは残さず、
常に最新版のみを公開しています。

## ライセンス

このリポジトリのコード（`build_db.py`等）は MIT License です。

郵便番号データ自体の利用条件は日本郵便の定める規約に従います。JIGYOSYO（事業所個別番号）
データについては、日本郵便が著作権を主張しないことが明記されており、自由に配布できます。

## 自分でビルドする場合

Python 3系のみで動作します（追加の依存ライブラリは不要）。

```bash
python3 build_db.py -o jp_postal_code.db
```

日本郵便のサイトから最新の郵便番号データ（KEN_ALL・JIGYOSYO）をダウンロードし、
カレントディレクトリに `jp_postal_code.db` を生成します。

- データソース: [郵便番号データダウンロード](https://www.post.japanpost.jp/zipcode/dl/utf-zip.html) /
  [事業所個別番号データダウンロード](https://www.post.japanpost.jp/zipcode/dl/jigyosyo/index-zip.html)
- 自動更新の仕組み（MD5チェック・ワークフロー詳細）は
  [`.github/workflows/update-db.yml`](.github/workflows/update-db.yml) を参照してください。
