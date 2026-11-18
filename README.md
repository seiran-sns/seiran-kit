# seiran-kit

[seiran](https://github.com/seiran-sns/seiran) のインスタンスを、公開イメージ（Docker Hub の
[`seiranorg/seiran`](https://hub.docker.com/r/seiranorg/seiran)）から立てるための本番サーバー用ファイル一式です。
ソースコードのチェックアウトもビルド環境も要りません。

## 構成

`docker-compose.yml` が次のサービスを動かします。

| サービス | 役割 |
|---|---|
| `db` | PostgreSQL 16 + pg_bigm（`docker/Dockerfile.postgres` をこのホストでビルド） |
| `redis` | ジョブキュー・リアルタイム配信を新旧のプロセスで共有する（追記ログで永続化） |
| `lb` | 動いている方の色（blue / green）にだけリクエストを流す nginx |
| `seiran-blue` / `seiran-green` | seiran 本体（`SEIRAN_ROLE=all`）。片方ずつ動かす |
| `cron` | 毎時 DB を R2 へバックアップする（`scripts/backup-db.sh`） |
| `tunnel` | Cloudflare Tunnel。公開ホスト名の向き先は `http://lb:80` |

更新は blue-green で無停止に行います（`scripts/deploy.sh`）。動いていない方の色を新しい版で起動し、
`/health` が通ったら LB の向き先を切り替えて古い方を止めます。新しい方が起動しなければそこで止まり、
動いている方には触れません。

## 必要なもの

- Docker Engine と Docker Compose v2
- Cloudflare Tunnel のトークン
- バックアップ先の S3 互換ストレージ（Cloudflare R2 を想定）

## 初回の起動

```bash
git clone https://github.com/seiran-sns/seiran-kit.git
cd seiran-kit
cp .env.example .env
# .env を編集する（LOCAL_DOMAIN・POSTGRES_PASSWORD・TUNNEL_TOKEN・バックアップ先など）
scripts/deploy.sh
```

起動したら `https://<LOCAL_DOMAIN>/` を開き、案内に従って管理者アカウントを作成します。

JWT の署名鍵と AP/ATP の鍵ペアは初回起動時に生成され、`config-data` ボリュームの `secrets.toml` に
保存されます。このボリュームを失うと連合先から同じインスタンスとして扱われなくなるので、
DB とは別に保管してください（DB のバックアップには含まれません）。

## 版の更新

```bash
git pull                       # compose ファイル等の更新を取り込む
# .env の SEIRAN_VERSION を新しい版に書き換える
scripts/deploy.sh
```

版を戻すときも、`SEIRAN_VERSION` を前の版にして同じように実行します。ただしマイグレーションは
前にしか進まないので、戻した版が新しいスキーマで動ける範囲に限ります。

この構成は seiran **0.3.0 以降**のイメージ用です。0.3.0 でイメージから nginx を外し、seiran 本体が
フロントエンドも配るようになりました（LB の転送先が `:80` から `:3000` に変わっています）。0.2 系から
上げるときは、先に `git pull` してから `SEIRAN_VERSION` を 0.3.0 以降にして `scripts/deploy.sh` を実行
してください。切り替えの間も、動いている 0.2 系の色はそのまま応答を続けます。

1.0.0 以降は、同じメジャー版の中ならどの版からでも直接更新できます。メジャー版をまたぐときは、
各メジャー版の最終版（そのメジャー版のタグ、例: `1`）を順に経由してください。

## バックアップと復元

- バックアップ: `cron` が毎時48分に `scripts/backup-db.sh` を実行し、`${BACKUP_OBJECT_S3URL}_latest`
  を上書きします。手動で `scripts/backup-db.sh <SUFFIX>` を実行すると `_<SUFFIX>` にも残します。
- 復元: `scripts/restore-db.sh [SUFFIX]`（既定 `latest`）。稼働中の `db` の既存データを消して
  置き換えるので、実行前に `seiran-blue` / `seiran-green` を止めてください。

## アクセスログ

seiran 本体が `./logs` に `access.log.YYYY-MM-DD` で日ごとに書き出し、14日分残します。形式は nginx の
combined に処理時間（秒）を足したものなので、nginx 向けの解析ツール（GoAccess 等）でそのまま読めます。

```bash
tail -f logs/access.log.$(date -u +%F)
```

接続元は、Cloudflare Tunnel と LB を経由していても実際のクライアントの IP になります。クエリ文字列は
トークンを含むことがあるので記録しません。`docker compose logs` で見えるのは seiran の動作ログで、
アクセスログは含みません。

## 運用メモ

- `db` はループバック（`127.0.0.1:${DB_PUBLISH_PORT:-5432}`）にだけ公開します。psql は SSH トンネル経由で
  繋いでください。
- `docker/lb/active.conf` は `deploy.sh` が書く、いま動いている色の記録です。消すと次の `deploy.sh` が
  動いている色を見失い、それを作り直して止めたり、止めずに残したりするので、消さないでください。
- compose のプロジェクト名は `seiran-prod` です。ボリューム名（`seiran-prod_db-data` 等）がこれに
  紐づくので変えないでください。

## ライセンス

[AGPL-3.0](LICENSE)
