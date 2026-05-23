#!/usr/bin/env bash
#
# Pin Vault8004 agent metadata (PNG + JSON) to IPFS via Pinata V3 Files API.
# Requires PINATA_JWT in env (.env) — V3 key with Files: Write scope.
#
# Endpoint: POST https://uploads.pinata.cloud/v3/files
#   - multipart: file=@path, network=public, name=...
#   - response:  { "data": { "cid": "...", "id": "...", ... } }
#
# Single-file upload yields a CID for the file itself (no parent folder),
# so the agentURI references look like `ipfs://<cid>` — no /filename suffix.

set -euo pipefail

if [[ -z "${PINATA_JWT:-}" && -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
: "${PINATA_JWT:?PINATA_JWT not set — V3 key with Files: Write scope required}"

PNG_PATH="assets/agent/vault8004-agent-512.png"
JSON_TEMPLATE="assets/agent/agent.json"
[[ -f "$PNG_PATH" ]]      || { echo "missing $PNG_PATH" >&2; exit 1; }
[[ -f "$JSON_TEMPLATE" ]] || { echo "missing $JSON_TEMPLATE" >&2; exit 1; }

V3_URL="https://uploads.pinata.cloud/v3/files"

pin_file() {
  local path="$1" name="$2"
  curl -fsS -X POST "$V3_URL" \
    -H "Authorization: Bearer $PINATA_JWT" \
    -F "file=@${path}" \
    -F "network=public" \
    -F "name=${name}"
}

extract_cid() {
  python3 -c "import json,sys;print(json.load(sys.stdin)['data']['cid'])"
}

echo "[1/3] pinning PNG ..."
PNG_RESP=$(pin_file "$PNG_PATH" "vault8004-agent-512.png")
IMAGE_CID=$(echo "$PNG_RESP" | extract_cid)
echo "    IMAGE_CID=$IMAGE_CID"

echo "[2/3] patching JSON ..."
PATCHED_JSON=$(mktemp -t agent.json.XXXXXX)
trap 'rm -f "$PATCHED_JSON"' EXIT
sed "s|__IMAGE_CID__|${IMAGE_CID}|g" "$JSON_TEMPLATE" > "$PATCHED_JSON"

echo "[3/3] pinning JSON ..."
JSON_RESP=$(pin_file "$PATCHED_JSON" "vault8004-agent.json")
AGENT_URI_CID=$(echo "$JSON_RESP" | extract_cid)

echo
echo "==== DONE ===="
echo "IMAGE_CID     = $IMAGE_CID"
echo "AGENT_URI_CID = $AGENT_URI_CID"
echo "agentURI      = ipfs://$AGENT_URI_CID"
echo
echo "Verify:"
echo "  https://gateway.pinata.cloud/ipfs/$AGENT_URI_CID"
echo "  https://gateway.pinata.cloud/ipfs/$IMAGE_CID"
echo
echo "Save into notes/erc-8004.md ('Pinned CIDs') and notes/addresses.md."
