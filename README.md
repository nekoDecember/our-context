# Our Context

Obsidian のメモを Gitea から取り込み、LiteLLM で業務の文脈を整理する LLM Wiki アプリ。

Wiki、案件・人物の情報、困りごと・所感、確認事項、タスクとガント、AI への相談、Excel・報告文の出力を備える。

## 起動

```sh
git clone https://github.com/nekoDecember/our-context.git
cd our-context
python3 -m our_context.manage setup
```

`.env` に Gitea と LiteLLM の接続先を記入する。

```dotenv
APP_PORT=8096
APP_PUBLIC_URL=http://<MacのLAN-IP>:8096

LLM_BASE_URL=http://<LiteLLMホスト>:4000/v1
LLM_API_KEY=<LiteLLMのキー>
LLM_INGEST_MODEL=<LiteLLMのモデル別名>
LLM_QUERY_MODEL=<LiteLLMのモデル別名>

GITEA_BASE_URL=http://<Giteaホスト>:3000
GITEA_REPO=<所有者>/<リポジトリ名>
GITEA_BRANCH=main
GITEA_USERNAME=<トークンを発行した利用者名>
GITEA_READ_TOKEN=<読取トークン>
GITEA_ALLOWED_PATHS=work-log.md,context/*.md,context/**/*.md
```

`setup` がログイン用の `APP_PASSWORD` と Webhook 用の Secret を生成する。

```sh
docker compose up --build -d
docker compose ps
```

ブラウザで `http://<Mac の LAN IP>:8096` を開き、`.env` の `APP_PASSWORD` でログインする。設定画面で接続を確認し、「今すぐ同期」を実行する。

Gitea の Webhook URL は `http://<Mac の LAN IP>:8096/hooks/gitea`。Secret に `.env` の `GITEA_WEBHOOK_SECRET` を指定する。

**[Gitea・LiteLLM 接続ガイド](docs/connections.md)** に Docker のネットワーク、モデル名、トークン、対象パス、Webhook の登録、疎通確認、エラー対応を詳しく記載している。

## 使い方

1. Obsidian のノートに、日付・作業・判断・困りごと・所感などを書く。
2. Obsidian Git で commit・push する。
3. ホームと Wiki で更新を確認する。
4. 確認事項へ回答する。回答も原資料として保存される。
5. 相談画面で文脈を参照し、ガント表示や予定変更、報告作成を依頼する。
6. タスク画面で予定を編集し、変更案を適用する。
7. 報告画面から Excel と報告文を出力する。

初期の体制や役割、案件、業務ルールは「記録を追加」から入力できる。Gitea の `context/` に Markdown として置く方法も使える。

## 構成

API とワーカーの2コンテナで動く。業務データは `workspace` に保存する。

| 保存先 | 内容 |
|---|---|
| `workspace/raw` | 原資料の版と取得情報。 |
| `workspace/knowledge` | Wiki の Markdown、原資料の写し、Git 履歴。 |
| `workspace/state.sqlite3` | 公開版、ジョブ、確認事項、会話、出力履歴。 |
| `workspace/exports` | Excel と報告文。 |
| `workspace/backups` | バックアップ。 |

Wiki の運用は [Karpathy の LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)を採用する。タスク・プロジェクト・チームの目的、役割、判断、合意、所感を関連付ける。

## 検証

```sh
docker build --target test -t our-context:test .
docker run --rm our-context:test
```

模擬 Gitea・LiteLLM を使う統合テスト：

```sh
docker compose -f tests/compose.test.yaml up -d
docker compose -f tests/compose.test.yaml exec app python /tests/integration.py
```

テスト画面は `http://localhost:18096`、共有パスワードは `integration-only`。

実 Gitea・LiteLLM・ローカルモデルを使う E2E テスト：

```sh
python3 tests/e2e/run.py --local --reset --keep
```

検証環境の作成から、実際の push、Wiki 整理、確認への回答、予定変更、Excel 出力まで実行する。モデルの準備と接続方法は [実サービスのテスト手順](tests/e2e/README.md)に記載している。

## 文書

- [接続ガイド](docs/connections.md)
- [認証情報の検査記録](docs/credential-audit.md)
- [運用・バックアップ・復元](docs/operations.md)
- [テスト手順](docs/testing.md)
- [検証結果・画面の記録](docs/verification.md)
- [実サービスの E2E テスト報告](docs/e2e-report.md)
- [実装構成](docs/implementation.md)
- [要件定義](docs/requirements.md)
- [詳細設計原案](docs/detailed-design.md)
