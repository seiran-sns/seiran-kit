#!/bin/bash

# kit は /seiran にマウントされているが、compose は bind mount のパスをホストのデーモンへ
# そのまま渡すので、/seiran のまま compose を呼ぶとホストに無いパスをマウントしてしまう。
# 自分のコンテナのマウント元（ホスト上の kit のパス）を調べ、同じパスに /seiran への
# symlink を張る。scripts/lib.sh と auto-update.py は /run/seiran-kit-dir のパスを使う。
host_kit_dir="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/seiran"}}{{.Source}}{{end}}{{end}}' "$(hostname)" 2>/dev/null || true)"
if [ -n "$host_kit_dir" ] && [ "$host_kit_dir" != /seiran ]; then
    mkdir -p "$(dirname "$host_kit_dir")"
    ln -sfn /seiran "$host_kit_dir"
    echo "$host_kit_dir" > /run/seiran-kit-dir
else
    echo "[entrypoint] ホスト上の kit のパスが分かりません。自動更新は使えません。" >&2
fi

# bind mountした外部のcrontabファイルが変更されたら即座にcrondに反映されるように監視する
# 監視などしないでも外部のcrontabファイルを直接crondの設定ファイルにマウントしてしまえばそれで良いのではないかと
# 思うかも知れないが、crondの設定ファイルはrootがオーナーである必要があり、bind mountファイルではそれを満たせない。
echo /seiran/docker/cron/crontab | entr -n busybox crontab /_ &

busybox crond -f -l 0 -L /dev/stderr
