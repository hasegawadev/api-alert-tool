# 為替レート監視 ＆ Webhook自動通知ダッシュボード（api-alert-tool）

APIキー不要の公開為替レートAPI（ExchangeRate-API）から最新の為替レート（`USD/JPY`、`EUR/JPY`、`GBP/JPY`、`AUD/JPY`）をリアルタイム取得し、指定した目標レート（しきい値）の条件を満たした際に **Discord** または **Slack** の Incoming Webhook へ自動通知する Windows 向けデスクトップアプリケーションです。

モダンなダークモードGUI（KPIカード・リアルタイム履歴プレビュー表・実行ログ）を備え、取得した為替レート履歴を自動でCSV（Excel対応 UTF-8 with BOM）に記録します。

![デモ動作](demo.gif)

---

## 主な機能

- **APIキー・会員登録不要で即時利用可能**
  - オープンAPI（`https://open.er-api.com/v6/latest/{BASE}`）を採用し、通信タイムアウト制御および自動リトライ処理（指数バックオフ対応）を搭載しています。
- **単発取得 ＆ バックグラウンド定期監視モード**
  - 「今すぐ1回取得」による即時チェックと、指定秒数間隔（10〜86400秒）での「自動監視（開始／停止）」をマルチスレッド制御によりUIフリーズなしで実行します。
- **Discord / Slack Webhook 自動判別 ＆ テスト送信機能**
  - 入力された Webhook URL の形式から Discord（`content` 形式）と Slack（`text` 形式）を自動判別して通知を送信します。
  - 監視開始前に疎通確認ができる「テスト通知」ボタンと、URLのマスク表示／表示切替機能を備えています。
- **重複通知（スパム）防止ステート制御**
  - 定期監視中に一度目標レート条件を達成して通知した後は「通知済み状態」を保持し、レートが一度通常圏（条件外）に戻るまで連続スパム通知を自動的に抑止します。
- **KPIダッシュボード ＆ 取得履歴リアルタイムプレビュー**
  - 「最新為替レート」「目標レートとの差額」「監視ステータス」を3枚のKPIカードで大きく可視化。
  - 画面下部の `ttk.Treeview` テーブルで取得履歴の確認・カラムクリックによるソート・CSV一括クリアが可能です。
- **Excel対応CSVへの自動追記保存 ＆ 設定の永続化**
  - 取得結果を `output/rate_history.csv` へ `utf-8-sig`（BOM付きUTF-8）形式で自動追記し、Excelでそのまま文字化けなく開けます。
  - 通貨ペア・目標レート・Webhook URL・保存先フォルダなどの設定は `config.json` に自動保存され、次回起動時に復元されます。

---

## 技術スタック

| カテゴリ | 使用技術 / ライブラリ | 用途 |
| :--- | :--- | :--- |
| **言語** | Python 3.x | アプリケーション全体の実装（バックエンド／GUI分離設計） |
| **HTTP通信** | `requests` | 為替レートAPIからのJSON取得、Discord / Slack Webhook POST送信 |
| **データ処理** | `pandas` | 取得履歴データの構造化、CSV読み書き（`utf-8-sig` 出力） |
| **GUIフレームワーク** | `customtkinter` / `tkinter.ttk` | ダーク／ライト切替対応モダンUI、KPIカード、Treeviewテーブル |
| **パッケージング** | `PyInstaller` | Python環境不要で動作するWindows向け単体実行ファイル（`.exe`）のビルド |

---

## ディレクトリ構成

```text
api-alert-tool/
├── api_client.py      # バックエンドモジュール（API通信・しきい値判定・Webhook送信・CSV履歴保存）
├── gui.py             # フロントエンドモジュール（CustomTkinter製GUI・マルチスレッド定期監視制御）
├── SPEC.md            # 設計仕様書
├── README.md          # プロジェクト説明書（本ファイル）
├── requirements.txt   # 依存パッケージ一覧
├── .gitignore         # Git除外設定（.venv, config.json, output/, 納品物_*/ 等）
├── config.json        # ユーザー設定の永続化ファイル（初回実行・保存時に自動生成）
└── output/            # 取得履歴CSVの出力先ディレクトリ（デフォルト）
    └── rate_history.csv
```

---

## 使い方

### 1. 配布用実行ファイル（`.exe`）で使う場合
Pythonのインストールは不要です。`納品物_為替レート監視自動通知ツール/` 内の実行ファイル（`.exe`）をダブルクリックするだけで起動します。詳しい手順は同梱の `操作マニュアル.txt` をご参照ください。

### 2. Pythonソースコードから実行する場合

#### 環境構築と起動
```powershell
# 仮想環境の作成と有効化
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 依存ライブラリのインストール
pip install -r requirements.txt

# アプリケーションの起動
python gui.py
```

#### 基本操作手順
1. **監視条件の設定**:
   - **通貨ペア**: `USD/JPY`、`EUR/JPY`、`GBP/JPY`、`AUD/JPY` から選択します。
   - **目標レート**: 通知したい基準レート（例: `150.00`）を入力します。
   - **判定条件**: `以下になったら通知` または `以上になったら通知` を選択します。
   - **監視間隔（秒）**: 定期チェックの間隔（例: `60` 秒）を入力します。
2. **Webhook URL の設定（任意）**:
   - Discord または Slack の Incoming Webhook URL を入力し、**「テスト通知」** ボタンで通知が届くか確認できます（空欄のままでもレート監視・CSV記録ツールとして利用可能です）。
3. **監視の実行**:
   - **「今すぐ1回取得」**: 単発で最新レートを取得し、条件判定とCSV保存を行います。
   - **「自動監視を開始」**: 指定した間隔でバックグラウンド監視を継続します。停止する際は **「監視を停止」** をクリックします。
4. **CSV履歴の確認**:
   - **「保存先フォルダを開く」** ボタンから `rate_history.csv` を Excel 等で直接開いて確認できます。

---

## 実行ファイル（`.exe`）のビルド方法

`PyInstaller` を使用して、単体実行ファイルをビルドできます。

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --collect-all customtkinter --name "為替レート監視自動通知ツール" gui.py
```

---

## ライセンス・免責事項

- 為替レートデータは [ExchangeRate-API (Open Access)](https://www.exchangerate-api.com) より取得しています。
- 本ツールの取得レートは参考値であり、実際の金融取引・投資判断における正確性やリアルタイム性を保証するものではありません。
