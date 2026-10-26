#!/usr/bin/env bash
# Lance doc_audit.py sur les fixtures et affiche la ligne de score de chacune.
# Scores de référence (commit fcc24af) en commentaire : un écart = régression ou amélioration à expliquer.
set -euo pipefail
D="${1:-/tmp/doc-audit-fixtures}"
S="$(cd "$(dirname "$0")/.." && pwd)/plugins/audit-documentation/skills/audit-documentation/scripts/doc_audit.py"
for p in fastapi django go mono laravel; do      # 64 C | 99 A | 75 B | 65 C | 98 A
  printf "%-10s " "$p"; python3 "$S" "$D/single/$p" --single | sed -n 3p
done
echo "--- écosystème (attendu : platform-docs 63 C, shop-api 93 A, shop-app 89 B ; 'unrelated' absent)"
python3 "$S" "$D/eco/shop-api" --ecosystem | sed -n '3,7p'
