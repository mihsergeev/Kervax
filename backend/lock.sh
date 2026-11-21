#!/bin/sh
# Пересборка lock-файлов бэкенда из requirements*.in:
#     sh backend/lock.sh            - записать requirements.txt и requirements-dev.txt
#     sh backend/lock.sh --upgrade  - то же, но поднять все версии до свежих
#     sh backend/lock.sh --check    - CI: lock соответствует .in, ничего не пишет
# Нужен uv той версии, что в requirements-dev.in (pip install --require-hashes --no-deps
# -r backend/requirements-dev.txt ставит именно ее) и доступ к PyPI.
#
# Версии, уже записанные в lock, uv сохраняет, пока они подходят под .in: пересборка без
# --upgrade меняет только то, что поменяли в .in. --check сверяет пины с пересборкой, а хеши -
# с PyPI (ops/lock-hashes.py): каждый хеш - настоящий файл этой версии, среди них есть колесо.
# Хеши с пересборкой не сравниваются: PyPI иногда добавляет колеса к старой версии, и CI
# падал бы без правки с нашей стороны.
set -eu
cd "$(dirname "$0")"
mode=${1:-}
case "$mode" in ''|--upgrade|--check) ;; *) echo "usage: lock.sh [--upgrade|--check]" >&2; exit 2 ;; esac

compile() {   # <in> <out> [флаги uv]
    src=$1 out=$2
    shift 2
    uv pip compile --quiet --universal --python-version 3.12 --generate-hashes "$@" "$src" -o "$out"
}

if [ "$mode" = --check ]; then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    # копии текущих lock: uv берет из них версии, как при обычной пересборке
    cp requirements.txt requirements-dev.txt "$tmp/"
    compile requirements.in "$tmp/requirements.txt"
    compile requirements-dev.in "$tmp/requirements-dev.txt"
    pins() { grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*--hash=' "$1" | sed 's/[[:space:]]*\\$//'; }
    rc=0
    for f in requirements.txt requirements-dev.txt; do
        pins "$f" > "$tmp/$f.want"
        pins "$tmp/$f" > "$tmp/$f.got"
        if ! diff -u "$tmp/$f.want" "$tmp/$f.got"; then
            echo "backend/$f не соответствует requirements*.in: пересобери lock - sh backend/lock.sh" >&2
            rc=1
        fi
    done
    "${PYTHON:-python3}" ../ops/lock-hashes.py requirements.txt requirements-dev.txt || rc=1
    exit "$rc"
fi

# сначала runtime: requirements-dev.in берет его версии через -c requirements.txt
compile requirements.in requirements.txt ${mode:+"$mode"}
compile requirements-dev.in requirements-dev.txt ${mode:+"$mode"}
