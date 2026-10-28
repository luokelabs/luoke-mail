#!/bin/sh
# 用 luoke-core 的 Python 配件打包脚本把 luoke_mail 打成「螺壳邮件助手.app」（开发版，ad-hoc 签名），放在 build.noindex/（.noindex：Spotlight 不收录，免得 macOS 按 bundle id 打开到这份副本）。
# 清单是 packaging/accessory.json（luoke-core V1 §8）；自带一份螺壳记忆的库（luoke_mem，顶层 ADR 0003），
# 取自并排的 ../luoke-mem。正式的签名公证由发布脚本做，不在这里。
#   scripts/package-app.sh            打包并检查（导入、签名）
#   scripts/package-app.sh --install  再拷到 ~/Applications 并注册（会替换那里已有的螺壳邮件助手.app，包括后台自动整理的启动器）
#   LUOKE_CORE=<luoke-core 仓库>       默认是并排的 ../luoke-core；LUOKE_MEM 同理
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
core=${LUOKE_CORE:-$repo/../luoke-core}
mem=${LUOKE_MEM:-$repo/../luoke-mem}
exec /usr/bin/python3 "$core/tools/package_python_app.py" --manifest "$repo/packaging/accessory.json" \
  --package "$repo/luoke_mail" --package "$mem/luoke_mem" --out "$repo/build.noindex" --check "$@"
