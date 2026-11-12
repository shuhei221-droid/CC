# Gmail接続テストの追加（2026-11-13）

Gmail設定画面 `/settings/gmail` に「自分宛てにテスト送信」を追加します。
公開されていない個人のメールを営業連絡先として登録する必要はありません。
先に作成した「Gmail接続テスト」キャンペーンは停止中のままで構いません。この機能では使用しません。

## 更新の反映

1. Vercelのこのプロジェクトの環境変数に `GMAIL_TEST_RECIPIENTS` を追加し、値を `shuhei221@gmail.com`、対象をProductionとして保存します。
2. `OUTREACH_MODE=disabled`、`LEED_SCHEDULER=0` を維持します。`LEED_SECRET_KEY` と既存Gmail設定は変更しません。
3. 更新ZIPを展開し、GitHubの `shuhei221-droid/leed-outreach-os` のルートで `Add file → Upload files` を開きます。
4. 展開したフォルダ内の `outreach`、`migrations`、`migrations_postgres` の3フォルダを、フォルダごとドラッグしてコミットします。ZIPそのものや外側のフォルダはアップロードしません。既存のフォルダを削除する必要はありません。
5. VercelでこのコミットのProductionデプロイがReadyになるのを待ちます。自動デプロイされない場合は最新コードをデプロイします。
6. Gmail設定画面を再読み込みします。初回起動時に新しいテーブルが作成され、既存のログイン・Gmail接続情報は保持されます。

変更する実行ファイルは6件です。

- outreach/__init__.py
- outreach/gmail_test.py
- outreach/templates/gmail.html
- outreach/static/gmail-test.js
- migrations/019_gmail_diagnostics.sql
- migrations_postgres/004_gmail_diagnostics.sql

## 送信・返信の確認

1. `/settings/gmail` の「自分宛てにテスト送信」で `shuhei221@gmail.com` を選びます。
2. 「宛先・文面を確認」を押します。送信元が `shuhei.e@leed-inc.co.jp` であることを確認してください。
3. 件名は `【LEED Outreach OS】Gmail連携テスト`、本文は `Gmail APIの送信確認メールです。受信できましたら「受信しました」と返信してください。` です。自由文・添付・CC/BCCはありません。
4. 宛先と文面を確認するチェックを入れ、「確認メールを1通送信」を押します。実際にGmailから外部へ1通送信します。
5. 「Gmailで送信受付済み」はGoogleの受付確認です。配達完了を保証する表示ではありません。受信側の受信トレイ・迷惑メールを確認し、そのメールに返信します。
6. テスト履歴の「このテストの返信を確認」で返信検出件数を確認します。同じテストスレッド内で、宛先アドレスから来たメッセージのFromヘッダーを照合します。送信者の本人認証や内容分類を行うものではありません。返信本文は取得・保存しません。

## 制限とログ

- 初期状態は許可先なし。環境変数で明示した自分の管理下にあるテスト先のみ、最大10アドレスまで登録できます（カンマ区切り）。ワイルドカード不可。
- 管理者ログイン・CSRF・10分有効の宛先／文面確認・送信承認が必要です。
- Gmail接続先と送信元が一致する必要があります。確認後のアカウント・認証情報変更は拒否します。
- 同じ送信元と宛先の組み合わせでは、データベースに保持される限り1回だけ実行できます。通常操作でのリセット・再送は提供しません。
- 並行操作でも送信枠をDBで一度だけ取得します。タイムアウトや応答不明・実行中断は自動再送しません。`unknown` または残った `sending` は、Googleの送信済みで人が確認してください。結果不明時の自動照合は未実装です。
- 企業に紐付く送信停止済みアドレスも拒否します。
- `gmail_diagnostics` に確認・送信受付・返信検出を保存し、監査ログに各操作を記録します。営業企業、営業ジョブ、営業返信、KPIには含めません。
- `OUTREACH_MODE=disabled` でもこの診断だけは外部送信します。営業送信は無効のままです。テストを無効にするには `GMAIL_TEST_RECIPIENTS` を空にして再デプロイしてください。

## 検証と未実施

テストでは実際のGoogleや第三者への送信を行わず、プロバイダー応答を代替しています。
追加10件のHTTP統合テストをSQLiteとローカルPostgreSQLで実行し、送信内容、許可先、CSRF、承認、有効期限、設定変更、送信停止、並行二重送信、結果不明、再起動後の再送防止、返信照合を検証します。
UIは実際のJavaScriptとDOM/API代替で確認します。実ブラウザでの表示・Vercel/Neonでの反映・実Gmail送信は未実施です。
実行結果は同梱の `docs/gmail-diagnostic-test-results.txt` を参照してください。

Google公式API:
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
- https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.threads/get
