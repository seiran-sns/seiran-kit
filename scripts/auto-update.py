#!/usr/bin/env python3
"""seiran の新しい版を GHCR のタグから見つけ、switch.sh で無停止に切り替える。

cron コンテナが定期的に実行する（docker/cron/crontab）。ホストから手で実行してもよい。

どの版へ上げるか（seiran の docs/coding_rules.md「マイグレーション」の互換範囲に合わせる）:
- 1.0.0 以降: 同じメジャー版に新しい版があれば、その最新版へ。いまのメジャー版の最終版に
  いれば、次のメジャー版の最新版へ（メジャー版をまたぐときは各メジャー版の最終版を経由する）。
- 0.x: 互換が「1つ前の版」までなので、公開されている版を1つずつ順に上げる。
- 下げない。latest・0・0.3 のような浮動タグと、接尾辞付きの版（フォーク・試験版）は見ない。

新しい版が起動しなかったら（switch.sh が失敗したら）その版を .auto-update-failed に記録し、
以後その版へは上げない（毎回壊れた版を起動し直さないように）。原因を直して手で上げるか、
ファイルを消せば再び試す。イメージの取得に失敗したときは記録せず、次回また試す。
"""

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

GHCR_REPO = "seiran-sns/seiran"
VERSION_LABEL = "org.opencontainers.image.version"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# 1回の実行で上げる版の数の上限（0.x を1つずつ上げるときに、長く止まらないように）。
MAX_STEPS = 10
# switch.sh が「別の切り替えが実行中」で終わったときの終了コード。
EX_TEMPFAIL = 75


def log(msg: str) -> None:
    print(f"[auto-update] {msg}", file=sys.stderr, flush=True)


def kit_dir() -> Path:
    # lib.sh と同じ: cron コンテナの中ではホストと同じパス（/seiran への symlink）を使う。
    marker = Path("/run/seiran-kit-dir")
    if marker.is_file():
        return Path(marker.read_text().strip())
    if Path("/.dockerenv").exists():
        log("コンテナの中ではホスト上の kit のパス（/run/seiran-kit-dir）が要ります。")
        sys.exit(1)
    return Path(__file__).resolve().parent.parent


KIT = kit_dir()
FAILED_FILE = KIT / ".auto-update-failed"


def env_value(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    env_file = KIT / ".env"
    value = ""
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            m = re.match(rf"^{name}=(.*)$", line)
            if not m:
                continue
            raw = m.group(1).strip()
            # .env は backup-db.sh がシェルとして読み込むので、空白を含む値は引用符で囲まれている。
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                value = raw[1:-1]
            else:
                value = raw.split(" #", 1)[0].strip()
    return value


def post_update_notice(previous, version) -> None:
    """UPDATE_NOTICE_TOKEN があれば、そのアカウントで更新の通知を投稿する。

    失敗しても更新自体は済んでいるので、ログに残すだけにする。cron コンテナは internal
    ネットワークにいるので LB（http://lb）へ直接送る。ホストから実行したときは届かない。
    """
    token = env_value("UPDATE_NOTICE_TOKEN")
    if not token:
        return
    template = env_value("UPDATE_NOTICE_TEXT") or "seiran {version} に更新しました。"
    text = template.replace("\\n", "\n").replace("{version}", fmt(version)).replace(
        "{previous}", fmt(previous))
    req = urllib.request.Request(
        "http://lb/api/notes/create",
        data=json.dumps({"text": text}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log(f"更新の通知を投稿しました（HTTP {r.status}）。")
    except Exception as e:
        log(f"更新の通知を投稿できませんでした: {e}")


def parse(tag: str):
    m = SEMVER.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None


def fmt(v) -> str:
    return ".".join(str(x) for x in v)


def ghcr_versions() -> list:
    token_url = f"https://ghcr.io/token?scope=repository:{GHCR_REPO}:pull"
    with urllib.request.urlopen(token_url, timeout=30) as r:
        token = json.load(r)["token"]
    url = f"https://ghcr.io/v2/{GHCR_REPO}/tags/list?n=1000"
    tags = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            tags += json.load(r).get("tags") or []
            # 続きがあれば Link: </v2/...?last=...>; rel="next"
            m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", ""))
            url = urllib.parse.urljoin(url, m.group(1)) if m else None
    return sorted({v for v in map(parse, tags) if v})


def compose(*args: str) -> list:
    return ["docker", "compose", "--project-directory", str(KIT),
            "-f", str(KIT / "docker-compose.yml"), *args]


def current_version():
    """LB が向いている色のコンテナのイメージのラベルから、いま動いている版を得る。"""
    conf = KIT / "docker/lb/active.conf"
    if not conf.is_file():
        return None
    m = re.search(r"seiran-(blue|green):", conf.read_text())
    if not m:
        return None
    color = m.group(1)
    container = subprocess.run(
        compose("--profile", color, "ps", "-q", f"seiran-{color}"),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not container:
        return None
    label = subprocess.run(
        ["docker", "inspect", "-f", f'{{{{index .Config.Labels "{VERSION_LABEL}"}}}}', container],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return parse(label)


def next_version(cur, versions):
    newer = [v for v in versions if v > cur]
    if not newer:
        return None
    same_major = [v for v in newer if v[0] == cur[0]]
    if same_major:
        return same_major[0] if cur[0] == 0 else same_major[-1]
    next_major = newer[0][0]
    return max(v for v in newer if v[0] == next_major)


def main() -> int:
    cur = current_version()
    if cur is None:
        log("動いている版が分かりません（まだ deploy.sh で起動していない等）。何もしません。")
        return 0
    try:
        versions = ghcr_versions()
    except Exception as e:  # ネットワーク・GHCR の一時的な失敗は次回に回す
        log(f"GHCR のタグ一覧を取得できませんでした: {e}")
        return 0
    start = cur
    cur, code = update(cur, versions)
    # 1回の実行で複数の版を上げたら（0.x）、最初と最後の版でまとめて1回だけ通知する。
    if cur != start:
        post_update_notice(start, cur)
    return code


def update(cur, versions):
    """上げられるところまで上げ、(最後に動いている版, 終了コード) を返す。"""
    failed = FAILED_FILE.read_text().strip() if FAILED_FILE.is_file() else ""
    image = env_value("SEIRAN_IMAGE") or "seiranorg/seiran"
    for _ in range(MAX_STEPS):
        target = next_version(cur, versions)
        if target is None:
            break
        if fmt(target) == failed:
            log(f"版 {failed} は以前の自動更新で起動しなかったので上げません"
                f"（{FAILED_FILE.name} を消すと再び試します）。")
            break
        log(f"版 {fmt(cur)} → {fmt(target)} へ上げます。")
        # 先に取得しておき、取得の失敗（レジストリへの反映待ち等）と起動の失敗を区別する。
        if subprocess.run(["docker", "pull", "-q", f"{image}:{fmt(target)}"],
                          stdout=subprocess.DEVNULL).returncode != 0:
            log(f"{image}:{fmt(target)} を取得できませんでした。次回また試します。")
            break
        code = subprocess.run([str(KIT / "scripts/switch.sh"), fmt(target)]).returncode
        if code == EX_TEMPFAIL:
            log("別の切り替え（手動の deploy.sh 等）が実行中です。次回また試します。")
            break
        if code != 0:
            FAILED_FILE.write_text(fmt(target) + "\n")
            log(f"版 {fmt(target)} への切り替えに失敗しました。{fmt(cur)} のまま動いています。")
            return cur, 1
        cur = target
    return cur, 0


if __name__ == "__main__":
    sys.exit(main())
