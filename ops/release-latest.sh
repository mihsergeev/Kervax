#!/bin/sh
# Двигать ли тег latest образов при выпуске <tag> (.github/workflows/release.yml).
#     git ls-remote --tags --refs origin 'refs/tags/v*' | sh ops/release-latest.sh v1.8.152
# stdin - вывод git ls-remote: "<sha>\trefs/tags/<имя>". Печатает true или false.
# true - только стабильной vX.Y.Z, старшей среди стабильных тегов: бэкпорт (v1.8.150 после
# v1.8.151) и пре-релиз (v2.0.0-rc1) latest не откатывают. Сравнение по числам: v1.10.0 > v1.9.9.
# Имя тега - все после первого refs/tags/: тег vx/refs/tags/v9.9.9 - это не v9.9.9.
# Самого <tag> нет в списке - ошибка: по неполному списку решать нельзя.
set -eu
tag=${1:?usage: git ls-remote --tags --refs origin | release-latest.sh <tag>}
stable='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
tags=$(cut -f2 | sed -n 's#^refs/tags/##p')
if ! printf '%s\n' "$tags" | grep -qxF -- "$tag"; then
    echo "release-latest: $tag is not among the repository tags" >&2
    exit 1
fi
if ! printf '%s\n' "$tag" | grep -Eq "$stable"; then
    echo false
    exit 0
fi
top=$(printf '%s\n' "$tags" | grep -E "$stable" | sort -t . -k 1.2,1n -k 2,2n -k 3,3n | tail -n 1)
if [ "$top" = "$tag" ]; then echo true; else echo false; fi
