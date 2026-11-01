# 日々の運用

## 起動と設定変更

```sh
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 app worker
```

`.env` を変更したときは `docker compose up -d` で反映する。コードを変更したときは `--build` を付ける。

## 日々の入力

Obsidian で追記し、Git プラグインで commit・push する。画面から補足や訂正も追加できる。AI の確認事項へ回答すると、原資料として保存されて Wiki に反映される。

## 処理に失敗したとき

「設定・処理状況」で失敗したジョブを開き、表示された接続先や原資料を確認する。「再実行」で同じ入力を処理できる。公開済みの Wiki は引き続き利用できる。

## バックアップ

アプリとワーカーを停止してバックアップする。

```sh
docker compose stop app worker
docker compose run --rm --no-deps app python -m our_context.manage backup
docker compose start app worker
```

バックアップは `workspace/backups/our-context-<日時>.tar.gz` に保存される。原資料、Wiki の Git 履歴、SQLite を含む。`.env` は別に保管する。

## 復元

空のディレクトリへ復元する。

```sh
docker compose stop app worker
docker compose run --rm --no-deps app python -m our_context.manage restore \
  --file /workspace/backups/our-context-<日時>.tar.gz \
  --destination /workspace/restored
```

ホスト側で保存領域を切り替える。

```sh
mv workspace workspace-before-restore
mv workspace-before-restore/restored workspace
docker compose up -d
```

復元後はログインし、Wiki・確認事項・タスクを確認する。以前の保存領域は `workspace-before-restore` に残る。

## Wiki を取り出す

`workspace/knowledge/wiki` に Markdown、`workspace/raw` に元資料が保存される。公開版は SQLite に記録される。通常は画面またはバックアップから取り出す。

## サービス停止

```sh
docker compose down
```

業務データはホストの `workspace` に保存される。
