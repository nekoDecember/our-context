# Gitea・LiteLLM 接続ガイド

## 1. 起動までの流れ

1. `.env` を作成する。
2. Gitea と LiteLLM の接続先を記入する。
3. Docker を起動する。
4. 画面の「設定・処理状況」で接続を確認する。
5. 初回同期を実行する。
6. Gitea に Webhook を登録する。

Mac のターミナルで実行する。

```sh
cd /Users/kokit/Documents/apps/agents/our-context
python3 -m our_context.manage setup
```

`.env` にログイン用の `APP_PASSWORD` と通知検証用の `GITEA_WEBHOOK_SECRET` が生成される。ファイルをエディターで開き、接続先を記入する。

```dotenv
APP_PORT=8096
APP_PUBLIC_URL=http://192.168.1.10:8096

LLM_BASE_URL=http://192.168.1.20:4000/v1
LLM_API_KEY=sk-your-litellm-key
LLM_INGEST_MODEL=team-model
LLM_QUERY_MODEL=team-model

GITEA_BASE_URL=http://192.168.1.30:3000
GITEA_REPO=your-team/work-notes
GITEA_BRANCH=main
GITEA_USERNAME=your-gitea-user
GITEA_READ_TOKEN=your-gitea-token
GITEA_ALLOWED_PATHS=work-log.md,context/*.md,context/**/*.md
```

IP アドレスと名前は説明用の例である。自分の環境の値に置き換える。

```sh
docker compose up --build -d
docker compose ps
docker compose logs --tail=50 app worker
```

`docker compose` が見つからず `docker-compose` が導入されている環境では、以降のコマンドを `docker-compose` に置き換える。この Mac では `docker-compose` を使用する。

ブラウザで `http://<Mac の LAN IP>:8096` を開き、`APP_PASSWORD` でログインする。Mac の IP は次のコマンドで確認できる。

```sh
ipconfig getifaddr en0
```

有線接続では、使用中のインターフェースの IP を確認する。`APP_PUBLIC_URL` もそのアドレスに合わせる。

## 2. Docker から見た接続先

Gitea と LiteLLM にアクセスするのは Docker 内の API とワーカーである。Docker 内の `localhost` は、そのコンテナを指す。

| Gitea / LiteLLM の稼働場所 | `.env` に設定するホストの例 |
|---|---|
| LAN 上の別サーバー | そのサーバーの LAN IP または社内 DNS 名。 |
| Mac 上のプロセス | Docker Desktop は `host.docker.internal`。Colima は `host.lima.internal` も利用できる。 |
| 同じ Docker ネットワークのコンテナ | Docker のサービス名。例：`http://litellm:4000/v1`。 |
| HTTPS で公開されたサービス | 通常利用している HTTPS URL。 |

Mac 上のサービスでは、Mac の LAN IP を使う方法も選べる。接続先サービスを該当インターフェースで待ち受けさせ、Docker 内から疎通を確認する。

別の Compose プロジェクトのサービス名を利用する場合は、共通の Docker ネットワークへ接続する。`compose.override.yaml` の例を示す。

```yaml
services:
  app:
    networks: [default, shared]
  worker:
    networks: [default, shared]
networks:
  shared:
    external: true
    name: your-existing-network
```

Gitea・LiteLLM 側も同じネットワークに参加させる。既存ネットワーク名は `docker network ls` で確認する。

アプリ専用ネットワークは `10.203.14.0/24`、テスト専用は `10.203.15.0/24` を使う。既存の範囲と重なる場合は `.env` の `OUR_CONTEXT_SUBNET`、テスト起動時の `OUR_CONTEXT_TEST_SUBNET` を変更する。`all predefined address pools have been fully subnetted` は、自動割り当て用の範囲を使い切ったことを示す。専用の範囲を指定して起動できる。

## 3. LiteLLM を接続する

### 3.1 設定値

| 変数 | 内容 |
|---|---|
| `LLM_BASE_URL` | `/v1` まで含む API の基準 URL。 |
| `LLM_API_KEY` | LiteLLM が発行したキー。キー認証を省略して運用している環境では空欄。 |
| `LLM_INGEST_MODEL` | Wiki 整理用のモデル別名。 |
| `LLM_QUERY_MODEL` | 質問回答用のモデル別名。空欄では取り込み用を使う。 |
| `LLM_TIMEOUT` | 1 回の呼出の待ち時間。初期値 120 秒。 |
| `LLM_MAX_ROUNDS` | 1 処理で調査を繰り返す回数。初期値 16。 |
| `LLM_MAX_INPUT_CHARS` | 1 回に送信する会話の文字数上限。初期値 80,000。 |

アプリは `LLM_BASE_URL` に `/chat/completions` を付けて呼び出す。

```text
設定値：http://litellm:4000/v1
呼出先：http://litellm:4000/v1/chat/completions
```

### 3.2 モデル名の選び方

LiteLLM の `model_list` に登録した `model_name` を指定する。例：

```yaml
model_list:
  - model_name: team-model
    litellm_params:
      model: your-provider/your-model
```

この場合、Our Context に設定する値は `team-model` である。接続先のモデルは tool calling に対応しているものを選ぶ。

取り込みと質問回答で同じモデルを使える。モデルを分ける場合は、Wiki の整理品質と質問時の応答速度をそれぞれ確認する。

### 3.3 接続確認

`.env` の変更後に実行する。

```sh
docker compose up -d
docker compose exec app python -m our_context.manage check-llm
```

画面の「設定・処理状況 → LiteLLM の接続を確認」でも試せる。通常応答と tool calling の結果を表示する。`tool_calling: true` を確認する。

接続確認の後、短いメモを1件登録して Wiki 整理を確認する。モデルが原資料を読み、複数の関連ページと出典を生成する処理までを確かめる。

出力の補正には `response_format: json_schema` を使う。ページの種類ごとの必須項目と、参照できる ID を指定する。この要求に HTTP 400・422 が返った場合は `json_object` に切り替え、アプリ側で同じ検査を行う。切り替え回数はジョブ結果の `usage.schema_fallbacks` に記録する。

ローカルモデルで数分の生成時間が必要な場合は、`.env` の `LLM_TIMEOUT` を調整する。同梱の実サービステストでは `LLM_TIMEOUT=300` を使う。

### 3.4 社内 CA を利用する場合

CA 証明書を app と worker にマウントし、`SSL_CERT_FILE` を指定する。Git 側の HTTPS にも必要な場合は `GIT_SSL_CAINFO` を同じファイルに設定する。

```yaml
services:
  app:
    environment:
      SSL_CERT_FILE: /certs/company-ca.pem
      GIT_SSL_CAINFO: /certs/company-ca.pem
    volumes:
      - ./company-ca.pem:/certs/company-ca.pem:ro
  worker:
    environment:
      SSL_CERT_FILE: /certs/company-ca.pem
      GIT_SSL_CAINFO: /certs/company-ca.pem
    volumes:
      - ./company-ca.pem:/certs/company-ca.pem:ro
```

## 4. Gitea を接続する

### 4.1 対象リポジトリ

Obsidian の業務ノートを保存している Gitea リポジトリを指定する。

```dotenv
GITEA_BASE_URL=https://gitea.example.internal
GITEA_REPO=your-team/work-notes
GITEA_BRANCH=main
GITEA_USERNAME=your-user
GITEA_READ_TOKEN=your-read-token
GITEA_ALLOWED_PATHS=work-log.md,context/*.md,context/**/*.md
```

- `GITEA_BASE_URL` は Gitea の基準 URL。
- `GITEA_REPO` は `所有者/リポジトリ名`。
- `GITEA_USERNAME` はトークンを発行した利用者名。
- `GITEA_READ_TOKEN` は対象リポジトリの読取を許可したアクセストークン。
- `GITEA_BRANCH` は Obsidian から push するブランチ。

アプリは API でリポジトリ情報を確認し、Git の HTTPS 接続で本文を取得する。API と Git の両方にアクセスできるトークンを使う。

### 4.2 対象パス

パスはリポジトリのルートから指定し、複数指定はカンマで区切る。

| 指定例 | 対象 |
|---|---|
| `work-log.md` | 日々追記する1ファイル。 |
| `context/*.md` | 初期のチーム・案件情報。 |
| `daily/*.md` | 日付ごとに分かれたノート。 |
| `notes/**/*.md` | notes 以下のノート。 |

拡張子 `.md` と `.txt` の通常ファイルを取り込む。初期は対象を小さくして、整理結果を確認しながら広げる。

### 4.3 Obsidian から push する

Obsidian Git の remote を対象 Gitea リポジトリに設定する。終業時に「Commit-and-sync」を実行する。

ノートの例：

```markdown
## 2026-11-01

### 認証改善 / #1234
- 結合テストの準備を進めた。
- 田中さんが環境構築で困っていると話していた。
- 検証は11月2日から3日に実施する予定。
- 締め切りは次の定例で確認する。
```

日付、案件名、人名があると対応する文脈を整理しやすい。自由なメモも取り込める。

### 4.4 初回同期

```sh
docker compose up -d
docker compose exec app python -m our_context.manage check-gitea
```

画面の「設定・処理状況」で「今すぐ同期」を実行する。処理履歴で次を確認する。

1. Gitea から取得。
2. Wiki の整理。
3. 出典の検査。
4. 完了。

ホームと Wiki にページが並ぶことを確認する。

### 4.5 Webhook の登録

Gitea で対象リポジトリを開き、「設定 → Webhooks → Webhook を追加 → Gitea」へ進む。

| 項目 | 値 |
|---|---|
| Target URL | `http://<Mac の LAN IP>:8096/hooks/gitea` |
| HTTP Method | `POST` |
| Content Type | `application/json` |
| Secret | `.env` の `GITEA_WEBHOOK_SECRET` |
| Trigger | Push Events |
| Branch | `GITEA_BRANCH` と同じブランチ |
| Active | 有効 |

Gitea が Docker ネットワーク内から到達する場合は、Target URL に共有ネットワーク上のアプリのサービス名も使える。接続元は Gitea サーバーである。

登録後、ノートに1行追記して push する。Gitea の配信履歴に `202` が表示され、アプリの処理履歴に同期ジョブが現れることを確認する。

Gitea の「Test Delivery」は署名と到達性の確認に使う。実際の Wiki 更新は対象ブランチへの push で確認する。

### 4.6 LAN 内への Webhook が制限される場合

利用中の Gitea の設定一覧で `ALLOWED_HOST_LIST` のセクションを確認する。現行の設定一覧では `[security]`、旧版では `[webhook]` に配置されている。既存の値を保ち、対象ホストとポートを追記する。[Gitea の設定一覧](https://docs.gitea.com/administration/config-cheat-sheet/)

Docker で Gitea を運用している場合の環境変数名は次の形になる。

```dotenv
GITEA__security__ALLOWED_HOST_LIST=<既存の設定値>,192.168.1.10:8096
```

設定変更後に Gitea を再起動し、配信を再実行する。管理権限が必要な操作である。

`[webhook]` を使用する版では環境変数名を `GITEA__webhook__ALLOWED_HOST_LIST` に合わせる。

Gitea 28.2.0 では、プライベート IP への送信に IP アドレスまたは CIDR の明示許可が必要である。Docker のサービス名を使う場合も、名前と接続先の IP 範囲を指定する。専用ネットワークが `10.203.16.0/24` の例：

```dotenv
GITEA__security__ALLOWED_HOST_LIST=app:8096,10.203.16.0/24
```

許可範囲には、実際にアプリを配置した IP または専用ネットワークを指定する。

### 4.7 通知の取りこぼしと履歴変更

アプリは起動時と15分ごとにブランチの最新位置を確認する。通知が抜けた場合も未反映の変更を回収する。画面から手動同期も実行できる。

force push で履歴が分岐した場合は、「設定・処理状況 → 履歴の分岐を再同期する」から再同期を実行する。現在の対象ファイルを読み直して Wiki を更新する。原資料の過去版は保存される。

## 5. よくある接続エラー

| 表示・状況 | 確認すること |
|---|---|
| Connection refused | 接続先プロセスの起動、待受アドレス、ポート。 |
| 名前解決エラー | Docker 内から解決できるホスト名、共有ネットワーク。 |
| Gitea HTTP 401 / 403 | トークン、利用者名、対象リポジトリの読取権限。 |
| Gitea HTTP 404 | 基準 URL、`所有者/リポジトリ名`、トークンの参照権限。 |
| API は成功し Git が失敗 | `GITEA_USERNAME`、トークンの Git 読取権限、Git 用 HTTPS 経路。 |
| Wiki に変化がない | 対象ブランチ、対象パス、push 済みか、処理履歴。 |
| Webhook HTTP 403 | Secret の一致、Gitea 形式の通知、署名ヘッダー。 |
| Webhook の送信失敗 | Gitea からアプリへの到達性、LAN IP、`ALLOWED_HOST_LIST`。 |
| LiteLLM HTTP 401 / 403 | LiteLLM のキーとモデル利用権限。 |
| LiteLLM HTTP 404 | `/v1` の位置、基準 URL、モデル別名。 |
| LiteLLM HTTP 400 | モデルの tool calling 対応、LiteLLM 側のパラメーター対応。 |
| タイムアウト | モデルの応答時間、`LLM_TIMEOUT`、入力の大きさ。 |
| 出力上限・入力上限 | 初回資料やメモの取り込み範囲を分ける。 |
| TLS 証明書エラー | 社内 CA のマウント、`SSL_CERT_FILE`、`GIT_SSL_CAINFO`。 |
| 回答の反映失敗 | 処理履歴の失敗理由を確認し、接続を直して再実行する。 |

設定変更後は `docker compose up -d` でコンテナを更新する。`docker compose restart` は既存コンテナの環境変数を保つ。

## 6. テスト用サービスで試す

テスト用 Gitea リポジトリと LiteLLM API を同じコンテナで用意している。固定のメモを使い、Git の取得、通知、Wiki 整理、質問回答、出力を確認できる。

```sh
docker compose build
docker compose -f tests/compose.test.yaml up -d
```

テスト画面は `http://localhost:18096`。ログイン用パスワードは `integration-only`。テストデータは専用の Docker ボリュームへ保存される。

```sh
docker compose -f tests/compose.test.yaml exec app python /tests/integration.py
```

統合テストの実行コマンドは [テスト手順](testing.md)に記載している。

## 7. 参照

- [Gitea：Webhooks](https://docs.gitea.com/usage/repository/webhooks/)
- [LiteLLM：Function Calling](https://docs.litellm.ai/docs/completion/function_call)
- [LiteLLM：Structured Outputs](https://docs.litellm.ai/docs/completion/json_mode)
- [Obsidian Git](https://github.com/Vinzent03/obsidian-git)
- [Lima：ホストへの接続](https://lima-vm.io/docs/config/network/user/)
- [Docker Compose：restart](https://docs.docker.com/reference/cli/docker/compose/restart/)
