#!/usr/bin/env bash
# Validate generated e-invoices with the official tools (needs Docker):
#   - KoSIT validator + XRechnung configuration: EN 16931 and XRechnung rules (UBL and CII)
#   - Mustang: Factur-X / ZUGFeRD, including the French reform rules (BR-FR)
# Usage: scripts/validate-einvoices.sh [tools-dir]   (files are produced by the EN16931Tests with EINVOICE_DUMP)
set -euo pipefail
TOOLS="${1:-/srv/multikanban/tools}"
OUT="$TOOLS/out"
mkdir -p "$OUT"
rm -f "$OUT"/*.xml
cd "$(dirname "$0")/.."
docker compose run --rm --no-deps -v "$PWD:/app" -v "$OUT:/dump" -e EINVOICE_DUMP=/dump --entrypoint "" api \
  python manage.py test timetracking.tests.EN16931Tests timetracking.tests.FatturaPATests
cd "$TOOLS"
docker run --rm -v "$PWD:/w" -w /w eclipse-temurin:21-jre java -jar validator-*-standalone.jar \
  -s xrechnung-config/scenarios.xml -r xrechnung-config -o out/reports out/*.xml
for f in out/facturx-*.xml out/xrechnung-*.xml; do
  echo "Mustang $(basename "$f"): $(docker run --rm -v "$PWD:/w" -w /w eclipse-temurin:21-jre java -jar Mustang-CLI-*.jar \
    --action validate --source "$f" --no-notices 2>&1 | grep -oE '<summary status="[a-z]+"' | head -1)"
done
