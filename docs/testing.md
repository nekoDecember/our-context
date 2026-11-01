# テスト手順

実際の Gitea・LiteLLM・推論モデルを使う確認は [実サービスの E2E テスト](../tests/e2e/README.md)を使う。

## 1. 自動テスト

Docker でテストイメージを作成して実行する。

```sh
docker build --target test -t our-context:test .
docker run --rm our-context:test
```

確認する内容：原資料の保存、メモの訂正、出典の検査、失敗時の公開版維持、ログイン、CSRF、要求の重複、Webhook 署名、確認への回答、日程の試算と適用、Excel 出力、バックアップ復元、Git の取得、文章の表現。

## 2. 接続を含む統合テスト

テスト用サービスを起動する。

```sh
docker build --target app -t our-context:local .
docker compose -f tests/compose.test.yaml up -d
docker compose -f tests/compose.test.yaml exec app python /tests/integration.py
```

テスト用 Gitea は実際の Git リポジトリを HTTP で配信する。テスト用 LiteLLM は Chat Completions と tool calling の応答を返す。外部サービスの URL とキーはテスト用の値を使う。

統合テストでは、Gitea 接続、Git 取得、push 通知、通知重複、Wiki 更新、回答反映、予定変更、出典付き相談、ガント操作、会話からの Excel 作成、ダウンロード、報告文、Wiki 保存、点検を順に実行する。

## 3. 画面確認

`http://localhost:18096` を開く。共有パスワードは `integration-only`。

1. ホームの案件・困りごと・確認事項を見る。
2. Wiki 本文と元のメモを開く。
3. タスク一覧とガントを見る。
4. 予定の編集から変更案を適用する。
5. 相談画面で出典とガントへの移動を確認する。
6. Excel と報告文を出力する。
7. 設定画面で接続確認と処理履歴を開く。

## 4. テスト環境の終了

```sh
docker compose -f tests/compose.test.yaml down -v
```

このコマンドは `our-context-check` のテスト環境と専用データを削除する。

## 5. 実環境の確認

[接続ガイド](connections.md)に従って `.env` を設定し、短い業務メモを1件取り込む。使用するモデルで Wiki の整理、引用、質問回答を確認する。LAN IP で画面と認証済み API の到達性を確認し、結果を記録する。
