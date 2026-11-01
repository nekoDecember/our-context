# 実サービスによる E2E テスト

Gitea、LiteLLM、推論モデルを使って、Git の push から報告出力までを確認する。

## ローカルモデルで実行

macOS で、プロジェクトのルートから実行する。

```sh
python3 tests/e2e/run.py --local --reset --keep
```

スクリプトは現在のソースからアプリをビルドする。Gitea 28.2.0、LiteLLM 1.106.3、Ollama 0.40.13、Qwen2.5 7B Instruct を使う。Gitea・LiteLLM・アプリ・ワーカーは Docker、推論は Mac の GPU で動く。初回は Ollama の実行ファイルと約 4.7 GB のモデルを取得する。実際の推論で Wiki と回答を生成する。

`--reset` の初期化対象は `our-context-e2e` の専用ボリュームである。`--keep` は、実行後も画面の確認用に環境を稼働させる。通常の実行では、テスト終了時に専用サービスと推論プロセスを停止する。

## 既存の LiteLLM で実行

プロジェクトの `.env` に `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_INGEST_MODEL`、`LLM_QUERY_MODEL` を記入して実行する。

```sh
python3 tests/e2e/run.py --reset --keep
```

Gitea は検証用の環境を作成する。テストには同梱の架空の業務メモを使う。

## 確認する流れ

1. Gitea に非公開リポジトリ、読取トークン、Webhook を作成する。
2. Git で clone、commit、push する。
3. Gitea の配信 ID と、アプリが署名を検証した受信 ID を照合する。
4. 実モデルで業務メモを整理し、案件・チーム・人物・タスク・困りごとを確認する。
5. 原資料の保存と引用、対象ファイルの絞り込みを確認する。
6. 出典付きの質問回答と、相談からのガント表示を確認する。
7. AI の確認事項へ締め切りを回答し、Wiki と予定への反映を確認する。
8. 同じノートに進捗を追記して push し、状態の更新を確認する。
9. 予定の変更案を確認して適用する。
10. Excel、報告文、相談からの Excel 作成、相談の Wiki 保存、Wiki 点検を確認する。

Gitea が送った実際の通知を使う。Gitea の配信記録とアプリのデータベースを読取接続で照合する。

## 画面と結果

アプリは `http://<Mac の LAN IP>:19096`、Gitea は Mac 上の `http://localhost:13096` で開く。パスワードと接続先はテスト専用の `.env` に生成される。

- `tests/e2e/.env`：アプリの `APP_PASSWORD`、Gitea の `E2E_GITEA_PASSWORD`、読取トークン、モデル接続先。
- `tests/e2e/.runtime/outcome.json`：各段階の結果、commit、通知 ID、回答、所要時間。
- `tests/e2e/.runtime/wiki-initial.json`：初回に整理した Wiki。
- `tests/e2e/.runtime/wiki-final.json`：予定変更と相談の保存を終えた Wiki。
- `tests/e2e/.runtime/verification.json`：2回の通知の HTTP 202、commit、引用と原資料の照合結果。
- `tests/e2e/.runtime/weekly-report.xlsx`：出力した Excel。
- `tests/e2e/.runtime/weekly-report.md`：出力した報告文。
- `tests/e2e/.runtime/ollama.log`：推論サーバーのログ。

設定と生成データは Git の除外対象である。モデルの推論時間は Mac とモデルによって変わる。

## 終了

```sh
python3 tests/e2e/run.py --stop
```

通常運用のアプリは `8096`、専用テストのアプリは `19096` で動く。保存領域もそれぞれ独立している。

## 参照

- [Gitea の Docker 構成](https://docs.gitea.com/installation/install-with-docker/)
- [LiteLLM の Docker 構成](https://docs.litellm.ai/docs/proxy/docker_quick_start)
- [LiteLLM の Ollama 接続](https://docs.litellm.ai/docs/providers/ollama)
- [Ollama 0.40.13](https://github.com/ollama/ollama/releases/tag/v0.40.13)
- [Qwen2.5 7B Instruct](https://ollama.com/library/qwen2.5:7b-instruct)
