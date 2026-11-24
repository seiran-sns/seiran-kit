#!/usr/bin/env python3
"""seiran の版が公開するパスと、Cloudflare Tunnel の ingress が合っているかを確かめる。

seiran のイメージは、外部に公開する URL パスの許可リストを /usr/share/seiran/allowed-paths.txt
に持つ（https://github.com/seiran-sns/seiran の docs/architecture.md 3.1節）。ingress に
このリストの行だけをルールとして登録しておくと、それ以外のパス（脆弱性スキャン等）は
Cloudflare で 404 になり、自宅の回線にも seiran にも届かない。

ingress の書き換えは運用者が行う。このスクリプトは読むだけで、版が必要とするルールが
ingress に揃っているかを判定する。auto-update.py は揃っていない版へは上げない。

判定:
- 許可リストの `[apex]` の各行は、ホスト名が LOCAL_DOMAIN、`[subdomains]` の各行は
  `*.LOCAL_DOMAIN` のルールのパスとして、文字列がそのまま登録されていること。
- そのホスト名にパスの無いルール（全部を通す）があれば、絞っていないので揃っているとみなす。
- 転送先が http_status:* のルール（404 を返す等）は数えない。
- ingress にあって許可リストに無い行は、古い版のためのものでありうるので問題にしない。

手で確かめるとき（ingress を直した後など）:

    scripts/tunnel_check.py 1.0.16     # その版のイメージを取得して照合する

.env に CLOUDFLARE_API_TOKEN（権限は「アカウント → Cloudflare Tunnel → 読み取り」だけ）、
TUNNEL_TOKEN、LOCAL_DOMAIN が要る。アカウントと Tunnel の ID は TUNNEL_TOKEN から読む。
"""

import base64
import importlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from typing import NamedTuple

ALLOWLIST_PATH = "/usr/share/seiran/allowed-paths.txt"
API = "https://api.cloudflare.com/client/v4"


class CheckError(Exception):
    """照合できなかった（イメージ・API の取得に失敗した）。

    config_error は、設定を直さない限り次回も失敗するもの（トークンの誤り・権限不足等）。
    """

    def __init__(self, msg: str, config_error: bool = False):
        super().__init__(msg)
        self.config_error = config_error


class Result(NamedTuple):
    missing: list  # [(ホスト名, パスの正規表現)]
    extra: list  # ingress にあって許可リストに無いもの [(ホスト名, パス)]
    unrestricted: list  # パスで絞っていないホスト名


def parse_allowlist(text: str) -> dict:
    sections: dict = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def image_allowlist(image_ref: str):
    """イメージの許可リストを節ごとに返す。許可リストを持たない古い版なら None。"""
    r = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "cat",
         image_ref, ALLOWLIST_PATH],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        if "No such file" in r.stderr:
            return None
        raise CheckError(f"{image_ref} から許可リストを読めませんでした: {r.stderr.strip()}")
    return parse_allowlist(r.stdout)


def tunnel_ids(tunnel_token: str):
    """TUNNEL_TOKEN（base64 の JSON。a: アカウント、t: Tunnel、s: 秘密）から ID を得る。"""
    try:
        padded = tunnel_token + "=" * (-len(tunnel_token) % 4)
        data = json.loads(base64.b64decode(padded))
        return data["a"], data["t"]
    except Exception:
        raise CheckError("TUNNEL_TOKEN からアカウントと Tunnel の ID を読めませんでした。",
                         config_error=True) from None


def tunnel_ingress(api_token: str, tunnel_token: str) -> list:
    account, tunnel = tunnel_ids(tunnel_token)
    req = urllib.request.Request(
        f"{API}/accounts/{account}/cfd_tunnel/{tunnel}/configurations",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        # 4xx はトークン・権限・ID の誤りで、直さない限り続く
        raise CheckError(f"Cloudflare API が HTTP {e.code} を返しました: {e.read()[:300]!r}",
                         config_error=400 <= e.code < 500) from None
    except Exception as e:
        raise CheckError(f"Cloudflare API に接続できませんでした: {e}") from None
    if not body.get("success"):
        raise CheckError(f"Cloudflare API がエラーを返しました: {body.get('errors')}")
    config = (body.get("result") or {}).get("config") or {}
    return config.get("ingress") or []


def compare(required: dict, ingress: list, domain: str) -> Result:
    hosts = {"apex": domain, "subdomains": f"*.{domain}"}
    missing, extra, unrestricted = [], [], []
    for section, lines in required.items():
        host = hosts.get(section)
        if host is None:
            # この kit が知らない節。足りないものとして知らせ、黙って通さない
            missing += [(f"[{section}]", line) for line in lines]
            continue
        rules = [r for r in ingress
                 if r.get("hostname") == host
                 and not str(r.get("service", "")).startswith("http_status:")]
        if any(not r.get("path") for r in rules):
            unrestricted.append(host)
            continue
        paths = [r["path"] for r in rules]
        missing += [(host, line) for line in lines if line not in paths]
        extra += [(host, p) for p in paths if p not in lines]
    return Result(missing, extra, unrestricted)


def check(image_ref: str, env) -> Result | None:
    """版を照合する。許可リストを持たない版なら None。env は名前から値を引く関数。"""
    required = image_allowlist(image_ref)
    if required is None:
        return None
    for name in ("CLOUDFLARE_API_TOKEN", "TUNNEL_TOKEN", "LOCAL_DOMAIN"):
        if not env(name):
            raise CheckError(f".env に {name} がありません。", config_error=True)
    ingress = tunnel_ingress(env("CLOUDFLARE_API_TOKEN"), env("TUNNEL_TOKEN"))
    return compare(required, ingress, env("LOCAL_DOMAIN"))


def format_rules(rules: list) -> str:
    return "\n".join(f"{host}  {path}" for host, path in rules)


def main() -> int:
    # auto-update.py と同じく、kit の .env から値を読む（ファイル名に - があるので import 文は使えない）
    sys.dont_write_bytecode = True
    auto_update = importlib.import_module("auto-update")

    if len(sys.argv) != 2:
        print("Usage: scripts/tunnel_check.py <版>", file=sys.stderr)
        return 2
    image = auto_update.env_value("SEIRAN_IMAGE") or "seiranorg/seiran"
    ref = f"{image}:{sys.argv[1]}"
    if subprocess.run(["docker", "pull", "-q", ref], stdout=subprocess.DEVNULL).returncode != 0:
        print(f"{ref} を取得できませんでした。", file=sys.stderr)
        return 1
    try:
        result = check(ref, auto_update.env_value)
    except CheckError as e:
        print(e, file=sys.stderr)
        return 1
    if result is None:
        print(f"{ref} は許可リストを持たない版です（照合しません）。")
        return 0
    if result.unrestricted:
        print("パスで絞っていないホスト名（全部のパスが seiran へ届きます）:")
        print("\n".join(f"  {h}" for h in result.unrestricted))
    if result.extra:
        print("この版の許可リストに無いルール（古い版のためのもの。この版へ上げたら消してよい）:")
        print("\n".join(f"  {h}  {p}" for h, p in result.extra))
    if result.missing:
        print("足りないルール（ホスト名とパスをそのまま ingress に足してください）:")
        print("\n".join(f"  {h}  {p}" for h, p in result.missing))
        return 1
    print(f"ingress は {sys.argv[1]} が公開するパスを満たしています。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
