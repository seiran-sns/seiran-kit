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
| `cron` | 毎時 DB を R2 へバックアップする（`scripts/backup-db.sh`）。新しい版への自動更新（`scripts/auto-update.py`） |
| `tunnel` | Cloudflare Tunnel。公開ホスト名の向き先は `http://lb:80` |

更新は blue-green で無停止に行います（`scripts/switch.sh`）。動いていない方の色を新しい版で起動し、
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
scripts/deploy.sh              # 最新版（latest）で起動する。版を指定するなら scripts/deploy.sh 0.3.2
```

起動したら `https://<LOCAL_DOMAIN>/` を開き、案内に従って管理者アカウントを作成します。

JWT の署名鍵と AP/ATP の鍵ペアは初回起動時に生成され、`config-data` ボリュームの `secrets.toml` に
保存されます。このボリュームを失うと連合先から同じインスタンスとして扱われなくなるので、
DB とは別に保管してください（DB のバックアップには含まれません）。

## 版の更新

新しい版は `cron` コンテナが自動で入れます。3分ごとに GHCR（`ghcr.io/seiran-sns/seiran`）のタグを見て、
新しい版があれば `scripts/switch.sh` で無停止に切り替えます。どの版へ上げるかは次のとおりです。

- 1.0.0 以降: 同じメジャー版の中なら、その最新版へ直接上げます。いまのメジャー版の最終版にいれば、
  次のメジャー版の最新版へ上げます（メジャー版をまたぐときは各メジャー版の最終版を経由します）。
- 0.x: 公開されている版を1つずつ順に上げます（0.x はマイグレーションの互換が「1つ前の版」までのため）。
- 版を下げることはありません。

新しい版が起動しなかったときは、動いている版のまま止まり、その版を `.auto-update-failed` に記録して
以後は上げません。原因を直したら、ファイルを消すか手で上げてください。経過は `docker compose logs cron`
で見られます。

`.env` に `UPDATE_NOTICE_TOKEN`（投稿させたいアカウントの「設定 → アプリトークン」で発行）を書くと、
自動更新が済むたびにそのアカウントで通知を投稿します。文面は `UPDATE_NOTICE_TEXT` で変えられます
（`.env.example` 参照）。手で `deploy.sh` を実行したときは投稿しません。

手で上げる・戻すときは、タグを指定して `scripts/deploy.sh` を実行します。動かす版は `.env` には
書きません（`deploy.sh` は pull したイメージのラベルから実際の版を読み、`0.3.2` のような確定した版の
イメージで動かします）。

```bash
git pull                       # compose ファイル等の更新を取り込む（自動では取り込みません）
scripts/deploy.sh              # latest へ
scripts/deploy.sh 0.3.1        # 版を指定して（戻すときも同じ）
```

版を戻すときは、マイグレーションが前にしか進まないので、戻した版が新しいスキーマで動ける範囲に
限ります。戻した後も自動更新は新しい版へ上げ直すので、止めたいときは `.auto-update-failed` に
上げたくない版を書いておいてください。

この構成は seiran **0.3.0 以降**のイメージ用です。

## 公開するパスを Tunnel で絞る

seiran のイメージは、外部に公開する URL パスの許可リスト（正規表現）を
`/usr/share/seiran/allowed-paths.txt` に持っています。Cloudflare Tunnel の公開ホスト名に、この行だけを
パスのルールとして登録すると、それ以外のパス（`/wp-login.php` や `/.env` を狙う脆弱性スキャン等）は
Cloudflare で 404 になり、サーバーの回線にも seiran にも届きません。

1. 登録する正規表現を出す（節ごとに全行を `|` で繋いだ1本ずつ）:
   `scripts/tunnel_check.py --print <版>`
2. Zero Trust ダッシュボードの Tunnel の「公開ホスト名」に、出力の各ホスト名（`LOCAL_DOMAIN` と
   `*.LOCAL_DOMAIN`）のパスとして、その1本を文字列を変えずに登録する（向き先は `http://lb:80`）。
   許可リストの各行を1つずつルールにしてもよい。パスの無い `LOCAL_DOMAIN` のルールは消す。
   どれにも当たらないものは最後の受け皿（`http_status:404`）に落ちる。

新しい版で公開するパスが増えると、ingress に足すまでその URL は Cloudflare で 404 になります。
これを防ぐため、`.env` に `CLOUDFLARE_API_TOKEN`（権限は Cloudflare Tunnel の読み取りだけ）を書くと、
自動更新は上げる前にその版の許可リストと ingress を照合し、足りないルールがあれば上げずに
`UPDATE_ALERT_DM_TO` 宛てに DM で知らせます（送り元は `UPDATE_NOTICE_TOKEN` のアカウント）。
ingress を直せば次の回（3分ごと）で上がります。ingress を書き換えるのは人で、スクリプトは読むだけです。

```bash
scripts/tunnel_check.py 1.0.16   # その版に対して ingress が足りているか（足りないルール・不要になったルールを表示）
```

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
- `docker/lb/active.conf` は `switch.sh` が書く、いま動いている色の記録です。消すと次の `deploy.sh` が
  動いている色を見失い、それを作り直して止めたり、止めずに残したりするので、消さないでください。
- compose のプロジェクト名は `seiran-prod` です。ボリューム名（`seiran-prod_db-data` 等）がこれに
  紐づくので変えないでください。

## ライセンス

[AGPL-3.0](LICENSE)
