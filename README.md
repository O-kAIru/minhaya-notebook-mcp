# みんはや問題帳 MCP（試作 v0.1）

非公式のローカルMCPサーバー。Python 3.11以上 / 公式MCP SDK / SQLite。

iPhone・iPadでみんはやを使う人向けに、作問と過去問整理を会話から行う非公式MCPです。
問題の作成は接続元AIが担当し、このサーバーが問題帳の保存・検索・復習を担当します。

## できること

- オリジナル問題の作成をAIに依頼するプロンプト
- 問題文、答え、読み、解説、ジャンル、タグ、出典の保存・編集・検索
- 同じ問題の重複追加防止。同じ問題文に異なる答えがあれば取込を中断
- 問題の状態管理：下書き `draft` → 確認済み `ready` → 入力済みとの報告 `entered`
- CSVの取り込み・書き出し。取り込みは既定でプレビュー
- アプリに手入力・コピーするときの問題文・答え・読みの取り出し
- 正誤の記録と、未復習・苦手問題を優先した復習

みんはやアプリへの直接登録と、公式過去問の自動取得は未実装です。
`entered` はユーザーの入力完了報告を記録する状態で、アプリと同期・検証するものではありません。
公式API、一括取り込み形式、現在のiPhone版を操作する方法は確認できていません。
CSVはこの問題帳で扱うデータ形式です。

まだ手元に過去問がない場合は、オリジナル問題の作成から始められます。
今後スクリーンショットから取り込む場合は、画像を読める接続元AIで文字を起こし、
問題文と答えを確認してから保存します。サーバー自体にはOCR・画像取込機能はありません。

## 構成

```text
会話するAI ──MCP── このサーバー ── SQLiteのローカル問題帳
                              └── 入力用データ・CSV
```

サーバーから外部へのネットワーク通信はありません。
接続元AIが出典確認のために行う検索や画像処理は、そのAI側の機能です。
サーバー自体は追加のAI APIキーを使いません。

## どう作っているか

Pythonの関数を公式MCP SDKの `@mcp.tool()` で登録し、AIから呼び出せるようにしています。
例えば、入力用テキストを取り出すツールは次の形です。

```python
@mcp.tool()
def get_registration_text(quiz_id: str) -> dict:
    return store.registration_text(quiz_id)
```

接続時にAIはツール名・説明・引数の型を取得し、必要な関数を呼び出します。
引数のスキーマやMCPの通信処理はSDKが担当します。
この試作では標準入力・標準出力（stdio）を使うため、HTTPサーバーを公開する必要はありません。

| ファイル | 担当 |
|---|---|
| `minhaya_mcp/server.py` | 8個のツールと作問用プロンプトの登録、MCPサーバーの起動 |
| `minhaya_mcp/store.py` | SQLiteへの保存、検索、編集、CSV処理、復習履歴 |
| `tests/test_notebook.py` | 一時DBでの動作確認と、別プロセスを起動するMCP接続テスト |
| `pyproject.toml` | 必要なPythonバージョン、依存ライブラリ、起動コマンド |
| `uv.lock` | 再現用に固定した依存ライブラリのバージョン |

例えば「歴史の問題を作って」と依頼すると、接続元AIが問題を考え、
`create_quiz` を呼んで下書きを保存します。「苦手問題を出して」なら
`get_review_queue` で候補を取り出し、AIが出題します。
作問用プロンプトはAIへの指示文で、サーバー自体に言語モデルは組み込んでいません。

## 実行

Python 3.11以上と、公式のMCP Python SDKを使います。

```sh
git clone https://github.com/O-kAIru/minhaya-notebook-mcp.git
cd minhaya-notebook-mcp
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m minhaya_mcp.server
```

最後のコマンドは、MCPクライアントからの標準入力を待ちます。
起動時に何も表示されないのが正常です。
`uv` を利用できる場合は `uv run --frozen --python 3.12 minhaya-mcp` でも起動できます。
同梱の `uv.lock` は依存関係を固定するファイルです。

## MCPクライアントに渡す設定例

クライアントのMCP設定で、以下の起動コマンドと環境変数を設定します。
`/path/to/minhaya-mcp` は実際の絶対パスに置き換えてください。

```json
{
  "mcpServers": {
    "minhaya-notebook": {
      "command": "/path/to/minhaya-mcp/.venv/bin/python",
      "args": ["-m", "minhaya_mcp.server"],
      "env": {
        "MINHAYA_DB_PATH": "/path/to/minhaya-mcp/data/quizzes.sqlite3"
      }
    }
  }
}
```

これは一般的なJSON形式の設定例です。クライアントによって設定形式は異なります。
現在のチャットやiPhone側へのMCP接続は、このファイルを作るだけでは有効になりません。

データベースの既定保存先は、このプロジェクトの `data/quizzes.sqlite3` です。
`MINHAYA_DB_PATH` で変更できます。バックアップするときはサーバーを終了し、DBファイルをコピーしてください。
CSVには問題データだけが入り、内部ID、日時、復習履歴は含みません。

## 会話での使い方

1. 「日本史の標準的な問題を5問作って。答えの読みと出典も保存して」
2. 「下書き一覧を見せて」「この問題の表現を直して」
3. 「確認したこの3問をreadyにして」
4. 「アプリに入力するため、この問題の問題文と答えを出して」
5. 「入力し終わったのでenteredにして」
6. 「苦手問題から1問ずつ出して。答えは回答するまで隠して」

## CSV

必須の見出しは `question,answer`。日本語の `問題文,答え` などにも対応します。
任意項目は `reading,explanation,category,tags,source_url,source_note,status`。
タグは `|` 区切りです。日本語の `、` も取り込み時に区切りとして扱います。
UTF-8で読み込み、見出しのBOMは取り除きます。
1回の取り込みは2MB・1000問までです。見出しのないCSVや未対応列はエラーになります。

```csv
question,answer,reading,explanation,category,tags,source_url,source_note,status
1時間は何分でしょう？,60分,ろくじゅっぷん,時間の単位の換算です。,一般,単位|練習,,動作確認用の自作例,draft
```

`import_quizzes_csv(csv_text=..., dry_run=true)` で追加件数・重複件数を確認し、
`dry_run=false` で保存します。途中の1問でもエラーなら、その取り込み全体を保存しません。
全角半角・空白・英字の大文字小文字を正規化して同一問題を判定します。
似た内容や言い換えの検出は接続元AIが検索結果を使って判断します。

書き出しでは、表計算ソフトで数式になり得る `= + - @` 始まりのセルに
先頭の `'` を加えます。元から `'` で始まるセルも、先頭の `'` を1個増やします。
この問題帳から書き出したCSVを再取り込みする場合に限り、
`restore_export_escapes=true` を指定すると元の文字列に戻します。
ほかで作られたCSVは、既定の `false` のまま文字列として取り込みます。
内部IDと復習履歴も含めたバックアップにはSQLiteファイルを使ってください。

## ツール一覧

| ツール | 役割 |
|---|---|
| `create_quiz` | 下書き保存 |
| `update_quiz` | 指定した項目の修正・状態変更 |
| `search_quizzes` | 検索・絞り込み・ページ取得 |
| `import_quizzes_csv` | CSVプレビュー・取り込み |
| `export_quizzes_csv` | 問題データのCSV書き出し |
| `get_registration_text` | アプリ入力用データの取り出し |
| `record_review` | 復習の正誤記録 |
| `get_review_queue` | 復習候補の取得 |

作問用プロンプト：`draft_quizzes(topic, count=5, difficulty="標準")`。

## 検証

```sh
.venv/bin/python -m unittest discover -s tests -v
```

データ保存、重複・答えの競合、CSVのプレビューと一括ロールバック、
CSV往復、検索、更新時の項目保持、復習履歴、MCP標準入出力での接続を確認します。
テストは一時DBを使い、実際の問題帳を変更しません。

## 参考

- [MCP公式Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP SDKの実行方法](https://py.sdk.modelcontextprotocol.io/run/)
- [みんはや公式サイト](https://minhaya.com/)

このツールはみんはや運営による提供ではありません。
