# deploy.sh / switch.sh が source する共通部分。
#
# kit のディレクトリへ移り、compose をそのディレクトリ基準で呼ぶ配列 `compose` を用意する。
#
# cron コンテナの中では kit が /seiran にマウントされているが、compose は `./logs` などの
# bind mount を project directory 基準の絶対パスにしてホストのデーモンへ渡す。/seiran の
# まま呼ぶとホストに無い /seiran/logs をマウントしてしまうので、コンテナの entrypoint が
# ホストと同じパスに /seiran への symlink を張り、そのパスを /run/seiran-kit-dir に書いておく。
# compose は symlink を解決せずにそのパスを使うので、ホストから呼んだときと同じパスになる。

kit_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -r /run/seiran-kit-dir ]; then
    kit_dir="$(cat /run/seiran-kit-dir)"
elif [ -f /.dockerenv ]; then
    echo "コンテナの中ではホスト上の kit のパス（/run/seiran-kit-dir）が要ります。" >&2
    exit 1
fi
cd "$kit_dir"

compose=(docker compose --project-directory "$kit_dir" -f "$kit_dir/docker-compose.yml")
active_conf=docker/lb/active.conf

# .env の値を読む（compose と同じく、シェルの環境変数があればそちらを優先する）。
env_value() {
    local name="$1"
    if [ -n "${!name:-}" ]; then
        printf '%s' "${!name}"
        return
    fi
    [ -f .env ] || return 0
    sed -n "s/^${name}=[\"']\{0,1\}\([^\"']*\).*/\1/p" .env | tail -n1
}

seiran_image="$(env_value SEIRAN_IMAGE)"
seiran_image="${seiran_image:-seiranorg/seiran}"

# いま LB が向いている色（blue / green）。まだ一度も切り替えていなければ空。
active_color() {
    [ -f "$active_conf" ] || return 0
    sed -n 's/.*seiran-\(blue\|green\):.*/\1/p' "$active_conf"
}
