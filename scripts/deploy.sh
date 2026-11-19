#!/bin/bash
# seiran を指定のタグの版へ無停止で切り替える。初回の起動もこれで行う。
#
# タグを省くと latest を使う。タグは pull したイメージのラベル
# （org.opencontainers.image.version）で実際の版に直してから動かすので、latest や 0.3 の
# ような浮動タグを指定しても、コンテナは 0.3.2 のような確定した版のイメージで動く
# （自動更新がいま動いている版を知るのにも使う）。
#
# db・redis を起動してから、本体の切り替えを switch.sh に任せ、最後に lb・cron・tunnel を
# 起動する（compose ファイルの変更もここで反映される。db・redis は上で起動済みなので --no-deps）。
# cron のイメージはビルドし直さない。docker/cron を変えたら
# `docker compose build cron && docker compose up -d --no-deps cron` を手で実行する。
#
# Usage: scripts/deploy.sh [タグ]（例: scripts/deploy.sh / scripts/deploy.sh 0.3.2）
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

log() { echo "[deploy] $*"; }

tag="${1:-latest}"
log "${seiran_image}:${tag} を取得します。"
docker pull -q "${seiran_image}:${tag}" >/dev/null
version="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' "${seiran_image}:${tag}")"
if [ -z "$version" ]; then
    log "イメージに版のラベルがありません。タグ ${tag} のまま動かします。"
    version="$tag"
fi

"${compose[@]}" up -d db redis
"$kit_dir/scripts/switch.sh" "$version"
"${compose[@]}" up -d --no-deps lb cron tunnel
