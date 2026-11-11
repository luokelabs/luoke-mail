#!/bin/sh
# 给 luoke-core 的 app/build_app.sh 用：把要进 螺壳.app 的东西放到 LUOKE_OUT。
#   luoke_mail/           进螺壳自带 Python 的 site-packages，不再带自己的 Python 和 luoke_mem
#   luoke-accessory.json  进 Contents/Resources/mail
#   LICENSE、NOTICE       进 Contents/Resources/licenses/luoke-mail
# scripts/package-app.sh 不受影响，还是出 螺壳邮件助手.app。
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
: "${LUOKE_OUT:?要给 LUOKE_OUT}"
mkdir -p "$LUOKE_OUT"
rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' "$repo/luoke_mail" "$LUOKE_OUT/"
cp "$repo/packaging/accessory.json" "$LUOKE_OUT/luoke-accessory.json"
cp "$repo/LICENSE" "$repo/NOTICE" "$LUOKE_OUT/"
echo "luoke-mail $(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$LUOKE_OUT/luoke-accessory.json") -> $LUOKE_OUT"
