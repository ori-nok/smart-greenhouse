#!/usr/bin/env bash
# Сборка и запуск хост-тестов прошивок. Использование: bash firmware/tests/run_tests.sh
set -euo pipefail
cd "$(dirname "$0")"
python3 ../../tools/sync_firmware.py --check || { echo "Копии общих файлов разошлись: python3 tools/sync_firmware.py"; exit 1; }
OUT=${TMPDIR:-/tmp}/gh_fw_tests; mkdir -p "$OUT"
FLAGS="-std=gnu++11 -Wall -Wextra -Wno-unused-parameter -Werror"
g++ $FLAGS -I../shared test_logic.cpp -o "$OUT/test_logic" && "$OUT/test_logic"
for pair in "SOIL:gh-soil-01" "TANK:gh-tank-01" "CLIMATE:gh-climate-01"; do
  S=${pair%%:*}; N=${pair##*:}
  g++ $FLAGS -x c++ -DSCEN_$S -DSKETCH="\"../$N/sketch.ino\"" -Istubs -I../$N run_sketch.cpp -o "$OUT/run_$N"
  "$OUT/run_$N" > "$OUT/$N.log" 2> "$OUT/$N.err"
  echo "built+ran $N"
done
python3 check_logs.py "$OUT"
