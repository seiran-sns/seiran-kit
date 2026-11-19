#!/bin/bash
# seiran 本体を指定の版へ blue-green で無停止に切り替える。db・redis・cron・tunnel には触れない
# （cron コンテナの中の自動更新から呼んでも、cron 自身を作り直して処理を途中で殺さないように）。
#
#   1. 動いていない方の色（blue / green）を新しい版で起動する
#   2. ヘルスチェックが通るのを待つ（通らなければ新しい方を止めて終わる。動いている方はそのまま）
#   3. LB の向き先を新しい方に書き換えて reload する（処理中のリクエストは古い方で終わる）
#   4. 古い方を止める（実行中のジョブと常駐処理を片付けてから終了する）
#
# 版を戻すときも、前の版を指定して同じように実行する。ただしマイグレーションは前にしか
# 進まないので、戻した版が新しいスキーマで動ける範囲に限る
# （https://github.com/seiran-sns/seiran の docs/coding_rules.md「マイグレーション」）。
#
# 普段は deploy.sh（タグから版を決め、db・redis 等も起動する）か auto-update.py から呼ばれる。
#
# Usage: scripts/switch.sh <版>（例: 0.3.2）
set -euo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

health_timeout_secs="${HEALTH_TIMEOUT_SECS:-600}"

log() { echo "[switch] $*"; }

version="${1:-}"
[ -n "$version" ] || { log "Usage: scripts/switch.sh <版>"; exit 1; }

# 手動の deploy.sh と自動更新が重なって active.conf を取り合わないようにする。
# cron コンテナからも bind mount 越しに同じファイルを見るので、ホストと排他できる。
exec 9>"$kit_dir/.switch.lock"
# 終了コード 75（EX_TEMPFAIL）は「起動に失敗した」と区別するため（auto-update.py が見る）。
flock -n 9 || { log "別の切り替えが実行中です。終わってから実行してください。"; exit 75; }

active="$(active_color)"
if [ "$active" = "blue" ]; then next="green"; else next="blue"; fi

log "版 ${version} を seiran-${next} で起動します（動いている方: ${active:-なし}）"
# compose の seiran-blue / seiran-green は seiran-local:<色> を使う（docker-compose.yml 参照）。
docker pull -q "${seiran_image}:${version}" >/dev/null
docker tag "${seiran_image}:${version}" "seiran-local:${next}"
"${compose[@]}" --profile "$next" up -d --no-deps --force-recreate "seiran-$next"

container="$("${compose[@]}" --profile "$next" ps -q "seiran-$next")"
deadline=$((SECONDS + health_timeout_secs))
while :; do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container")"
    case "$status" in
        healthy) break ;;
        unhealthy | exited | dead)
            log "seiran-${next} が起動しませんでした（${status}）。止めて終わります。"
            "${compose[@]}" --profile "$next" logs --tail 50 "seiran-$next" || true
            "${compose[@]}" --profile "$next" rm -sf "seiran-$next"
            exit 1
            ;;
    esac
    if [ "$SECONDS" -ge "$deadline" ]; then
        log "seiran-${next} が ${health_timeout_secs} 秒以内に healthy になりませんでした。止めて終わります。"
        "${compose[@]}" --profile "$next" logs --tail 50 "seiran-$next" || true
        "${compose[@]}" --profile "$next" rm -sf "seiran-$next"
        exit 1
    fi
    sleep 3
done
log "seiran-${next} が healthy になりました。LB の向き先を切り替えます。"

# 同じディレクトリに書いてから置き換える（書きかけのファイルを nginx に読ませない）。
printf 'set $seiran_backend seiran-%s:3000;\n' "$next" > "$active_conf.tmp"
mv "$active_conf.tmp" "$active_conf"
if [ -n "$("${compose[@]}" ps -q --status running lb)" ]; then
    "${compose[@]}" exec -T lb nginx -t -q
    "${compose[@]}" exec -T lb nginx -s reload
else
    "${compose[@]}" up -d --no-deps lb
fi

if [ -n "$active" ]; then
    log "seiran-${active} を止めます（実行中のジョブを終えるまで待ちます）。"
    "${compose[@]}" --profile "$active" stop "seiran-$active"
    "${compose[@]}" --profile "$active" rm -f "seiran-$active"
fi
log "完了しました。版 ${version} が seiran-${next} で動いています。"
