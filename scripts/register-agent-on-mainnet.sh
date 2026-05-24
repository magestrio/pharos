#!/usr/bin/env bash
#
# Register Vault8004 agent NFT in canonical ERC-8004 IdentityRegistry on Mantle.
# Wraps the full erc-8004-identity.4 procedure: generate calldata → open Safe
# TxBuilder → wait for execute → extract AGENT_ID → write .env.
#
# Spec: ~/Documents/brain/01-projects/vault8004/notes/register-agent-script.md
# Deps: cast (foundry), python3, scripts/extract-agent-id.sh,
#       contracts/script/RegisterAgent.s.sol
# Env:  AGENT_URI, SAFE_ADDRESS, MANTLE_RPC_URL  (in .env or shell)

set -euo pipefail

# ---------- constants ----------
IDENTITY_REGISTRY="0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
EXPECTED_SELECTOR="0xf2c298be"
NOTES_ADDRESSES="$HOME/Documents/brain/01-projects/vault8004/notes/addresses.md"
EXTRACT_RETRIES=5
EXTRACT_SLEEP_SECS=10

# ---------- CLI flags ----------
VERIFY_ONLY=0
NO_BROWSER=0
NO_CLIPBOARD=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --verify-only) VERIFY_ONLY=1 ;;
    --no-browser)  NO_BROWSER=1 ;;
    --no-clipboard) NO_CLIPBOARD=1 ;;
    --dry-run)     DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
      echo
      echo "Flags:"
      echo "  --verify-only   Check that .env AGENT_ID matches on-chain owner; no register."
      echo "  --no-browser    Don't open Safe TxBuilder URL, just print it."
      echo "  --no-clipboard  Don't copy data to clipboard, just print it."
      echo "  --dry-run       Steps 1-3 only (generate + show calldata, no tx hash prompt)."
      exit 0
      ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

# ---------- repo root + .env ----------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi

# ---------- helpers ----------
err() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "$*"; }
line() { printf '============================================================\n'; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || err "missing required command: $1"
}

is_addr() {
  [[ "$1" =~ ^0x[0-9a-fA-F]{40}$ ]]
}

is_txhash() {
  [[ "$1" =~ ^0x[0-9a-fA-F]{64}$ ]]
}

lc() { tr '[:upper:]' '[:lower:]' <<<"$1"; }

copy_to_clipboard() {
  local payload="$1"
  if [[ "$NO_CLIPBOARD" == 1 ]]; then return 1; fi
  if [[ "$OSTYPE" == darwin* ]] && command -v pbcopy >/dev/null 2>&1; then
    printf '%s' "$payload" | pbcopy && return 0
  elif command -v xclip >/dev/null 2>&1; then
    printf '%s' "$payload" | xclip -selection clipboard && return 0
  elif command -v xsel >/dev/null 2>&1; then
    printf '%s' "$payload" | xsel --clipboard --input && return 0
  fi
  return 1
}

open_url() {
  local url="$1"
  if [[ "$NO_BROWSER" == 1 ]]; then return 1; fi
  if [[ "$OSTYPE" == darwin* ]] && command -v open >/dev/null 2>&1; then
    open "$url" && return 0
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 && return 0
  fi
  return 1
}

# Write/replace a KEY=value line in .env atomically.
upsert_env() {
  local key="$1" value="$2"
  local tmp
  tmp="$(mktemp -t env.XXXXXX)"
  if grep -qE "^${key}=" .env 2>/dev/null; then
    # Use awk to avoid sed escaping pitfalls with arbitrary values.
    awk -v k="$key" -v v="$value" -F= '
      BEGIN{done=0}
      {
        if (!done && $1 == k) { print k "=" v; done=1 }
        else print
      }
      END { if (!done) print k "=" v }
    ' .env > "$tmp"
  else
    cp .env "$tmp"
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  mv "$tmp" .env
}

# ---------- Step 1: pre-flight ----------
info ""
line
info "STEP 1 — Pre-flight checks"
line

need_cmd cast
need_cmd python3
need_cmd forge

: "${MANTLE_RPC_URL:?MANTLE_RPC_URL not set in .env or shell}"
: "${SAFE_ADDRESS:?SAFE_ADDRESS not set in .env or shell (Gnosis Safe owning the NFT)}"

if [[ "$VERIFY_ONLY" != 1 ]]; then
  : "${AGENT_URI:?AGENT_URI not set in .env or shell (ipfs://<cid> of metadata)}"
fi

is_addr "$SAFE_ADDRESS" || err "SAFE_ADDRESS is not a valid 0x+40hex address: $SAFE_ADDRESS"

info "  IDENTITY_REGISTRY: $IDENTITY_REGISTRY"
info "  SAFE_ADDRESS:      $SAFE_ADDRESS"
info "  MANTLE_RPC_URL:    ${MANTLE_RPC_URL%%\?*}"  # strip query string from logs
[[ "$VERIFY_ONLY" != 1 ]] && info "  AGENT_URI:         $AGENT_URI"

# ---------- --verify-only path ----------
if [[ "$VERIFY_ONLY" == 1 ]]; then
  info ""
  line
  info "VERIFY-ONLY — check .env AGENT_ID matches on-chain owner"
  line
  [[ -n "${AGENT_ID:-}" ]] || err "AGENT_ID not set in .env — nothing to verify"

  on_chain_owner=$(cast call "$IDENTITY_REGISTRY" 'ownerOf(uint256)(address)' \
    "$AGENT_ID" --rpc-url "$MANTLE_RPC_URL" 2>&1) \
    || err "ownerOf($AGENT_ID) reverted — token may not exist. RPC said: $on_chain_owner"

  on_chain_owner_clean=$(echo "$on_chain_owner" | awk '{print $1}')
  if [[ "$(lc "$on_chain_owner_clean")" == "$(lc "$SAFE_ADDRESS")" ]]; then
    info "  AGENT_ID=$AGENT_ID owner = $on_chain_owner_clean (matches SAFE_ADDRESS)"
    info "  OK"
    exit 0
  else
    err "AGENT_ID=$AGENT_ID owner = $on_chain_owner_clean, expected $SAFE_ADDRESS"
  fi
fi

# Existing AGENT_ID guard
if [[ -n "${AGENT_ID:-}" ]]; then
  info ""
  info "AGENT_ID=$AGENT_ID already in .env."
  read -r -p "Re-register a new agent NFT? (y/N): " ans
  case "${ans:-N}" in
    y|Y|yes|YES) info "  continuing — old AGENT_ID will be overwritten on success" ;;
    *) info "  exiting (no changes made)"; exit 0 ;;
  esac
fi

# ---------- Step 2: generate calldata ----------
info ""
line
info "STEP 2 — Generate calldata via forge script"
line

forge_out=$(cd contracts && AGENT_URI="$AGENT_URI" forge script script/RegisterAgent.s.sol 2>&1) \
  || { echo "$forge_out" >&2; err "forge script failed"; }

# Parse `to:` (first token after "to:") and `data:` (the 0x... line that follows "data:").
TO_PARSED=$(echo "$forge_out" | awk '/^[[:space:]]*to:/ { for (i=1;i<=NF;i++) if ($i ~ /^0x[0-9a-fA-F]{40}$/) { print $i; exit } }')
DATA_PARSED=$(echo "$forge_out" | awk '
  /^[[:space:]]*data:[[:space:]]*$/ { flag=1; next }
  flag && /^[[:space:]]*0x[0-9a-fA-F]+[[:space:]]*$/ {
    gsub(/[[:space:]]/, "")
    print
    exit
  }
')

[[ -n "$TO_PARSED" ]]   || { echo "$forge_out" >&2; err "could not parse 'to:' from forge output"; }
[[ -n "$DATA_PARSED" ]] || { echo "$forge_out" >&2; err "could not parse 'data:' from forge output"; }

if [[ "$(lc "$TO_PARSED")" != "$(lc "$IDENTITY_REGISTRY")" ]]; then
  err "parsed to=$TO_PARSED != IDENTITY_REGISTRY=$IDENTITY_REGISTRY"
fi

if [[ "${DATA_PARSED:0:10}" != "$EXPECTED_SELECTOR" ]]; then
  err "data does not start with expected selector $EXPECTED_SELECTOR (got ${DATA_PARSED:0:10})"
fi

info ""
line
info "SAFE TRANSACTION DATA"
line
printf 'To:        %s\n' "$IDENTITY_REGISTRY"
printf 'Value:     0\n'
printf 'Data:      %s\n' "$DATA_PARSED"
line

# ---------- Step 3: open Safe TxBuilder + clipboard ----------
SAFE_URL="https://app.safe.global/apps/open?safe=mantle:${SAFE_ADDRESS}&appUrl=https://apps-portal.safe.global/tx-builder"

CLIP_OK=0
if copy_to_clipboard "$DATA_PARSED"; then
  CLIP_OK=1
fi

BROWSER_OK=0
if open_url "$SAFE_URL"; then
  BROWSER_OK=1
fi

if [[ "$DRY_RUN" == 1 ]]; then
  info ""
  info "Safe TxBuilder URL: $SAFE_URL"
  [[ "$CLIP_OK" == 1 ]] && info "Data copied to clipboard." || info "(data not copied — clipboard tool unavailable or --no-clipboard)"
  info ""
  info "--dry-run: stopping before Step 4."
  exit 0
fi

info ""
line
info "NEXT STEPS (manual)"
line
if [[ "$BROWSER_OK" == 1 ]]; then
  info "1. Safe TxBuilder opened in browser."
else
  info "1. Open Safe TxBuilder manually:"
  info "   $SAFE_URL"
fi
info "2. Click \"New Transaction\" → \"Enter Custom Data\"."
info "3. Fill in:"
info "   - To Address: $IDENTITY_REGISTRY"
info "   - ETH Value:  0"
if [[ "$CLIP_OK" == 1 ]]; then
  info "   - Data:       <paste from clipboard, already copied>"
else
  info "   - Data:       (paste the Data hex printed above)"
fi
info "4. Add transaction → Create Batch → Send Batch."
info "5. Sign with 2 of 3 signers (A + B, NOT C cold backup)."
info "6. Execute."
info "7. Copy the executed tx hash from Safe UI."
line

# ---------- Step 4: wait for tx hash ----------
info ""
read -r -p "Paste executed tx hash (or 'cancel' to exit): " TX_HASH
case "$TX_HASH" in
  cancel|CANCEL|"") info "cancelled — no changes made"; exit 0 ;;
esac
is_txhash "$TX_HASH" || err "invalid tx hash format (expected 0x + 64 hex): $TX_HASH"

# ---------- Step 5: extract AGENT_ID ----------
info ""
line
info "STEP 5 — Wait for confirmation + extract AGENT_ID"
line

EXTRACT_OUT=""
attempt=0
while (( attempt < EXTRACT_RETRIES )); do
  attempt=$((attempt + 1))
  info "  attempt $attempt/$EXTRACT_RETRIES — running extract-agent-id.sh ..."
  if EXTRACT_OUT=$(./scripts/extract-agent-id.sh "$TX_HASH" 2>&1); then
    info "  receipt found"
    break
  else
    info "  not ready yet (${EXTRACT_OUT##*$'\n'}). Sleeping ${EXTRACT_SLEEP_SECS}s ..."
    EXTRACT_OUT=""
    sleep "$EXTRACT_SLEEP_SECS"
  fi
done

if [[ -z "$EXTRACT_OUT" ]]; then
  err "extract-agent-id.sh failed after $EXTRACT_RETRIES attempts.
       Tx hash: $TX_HASH
       Debug manually: ./scripts/extract-agent-id.sh $TX_HASH"
fi

NEW_AGENT_ID=$(echo "$EXTRACT_OUT" | awk -F= '/^AGENT_ID=/ { print $2; exit }')
NEW_OWNER=$(   echo "$EXTRACT_OUT" | awk -F= '/^owner=/    { print $2; exit }')

[[ -n "$NEW_AGENT_ID" ]] || err "could not parse AGENT_ID from extract-agent-id.sh output:
$EXTRACT_OUT"
[[ "$NEW_AGENT_ID" =~ ^[0-9]+$ ]] || err "AGENT_ID '$NEW_AGENT_ID' is not numeric"
[[ -n "$NEW_OWNER" ]] || err "could not parse owner from extract-agent-id.sh output:
$EXTRACT_OUT"

if [[ "$(lc "$NEW_OWNER")" != "$(lc "$SAFE_ADDRESS")" ]]; then
  err "NFT owner ($NEW_OWNER) != SAFE_ADDRESS ($SAFE_ADDRESS).
       Someone registered between simulation and execute,
       or wrong Safe was used. Manual investigation required.
       AGENT_ID NOT written to .env."
fi

# ---------- Step 6: write .env ----------
info ""
line
info "STEP 6 — Write AGENT_ID to .env"
line

upsert_env "AGENT_ID" "$NEW_AGENT_ID"
info "  AGENT_ID=$NEW_AGENT_ID written to .env"

# ---------- Summary ----------
info ""
line
info "AGENT REGISTERED SUCCESSFULLY"
line
printf 'AGENT_ID:    %s\n' "$NEW_AGENT_ID"
printf 'Owner:       %s (Safe)\n' "$NEW_OWNER"
printf 'agentURI:    %s\n' "$AGENT_URI"
printf 'TxHash:      %s\n' "$TX_HASH"
printf 'Explorer:    https://mantlescan.xyz/tx/%s\n' "$TX_HASH"
printf 'NFT view:    https://mantlescan.xyz/token/%s?a=%s\n' "$IDENTITY_REGISTRY" "$NEW_AGENT_ID"
info ""
info "AGENT_ID written to .env"
info ""
info "NEXT STEPS:"
info "1. Update notes/addresses.md with AGENT_ID=$NEW_AGENT_ID under ERC-8004 section"
info "2. Run mainnet-deploy:"
info "   cd contracts && forge script script/Deploy.s.sol --rpc-url \$MANTLE_RPC_URL --broadcast --verify"
line

# ---------- Step 7: optional notes/addresses.md update ----------
info ""
ADDRESSES_LINE="AGENT_ID            = $NEW_AGENT_ID   // registered $(date +%Y-%m-%d), owner = SAFE"

if [[ ! -f "$NOTES_ADDRESSES" ]]; then
  info "notes/addresses.md not found at $NOTES_ADDRESSES — skipping auto-update."
  info "Add this line manually under the ERC-8004 section:"
  info "  $ADDRESSES_LINE"
  exit 0
fi

read -r -p "Update notes/addresses.md automatically? (Y/n): " upd
case "${upd:-Y}" in
  n|N|no|NO)
    info "Skipped. Add this line manually under the ERC-8004 section of $NOTES_ADDRESSES:"
    info "  $ADDRESSES_LINE"
    exit 0
    ;;
esac

if grep -qE '^AGENT_ID[[:space:]]*=' "$NOTES_ADDRESSES"; then
  info "AGENT_ID= line already present in $NOTES_ADDRESSES — leaving file untouched."
  info "Existing line:"
  grep -nE '^AGENT_ID[[:space:]]*=' "$NOTES_ADDRESSES" | head -1
  info "Manual edit required if you want to overwrite."
  exit 0
fi

# Insert ADDRESSES_LINE right after the REPUTATION_REGISTRY line within a fenced block.
tmp_notes="$(mktemp -t addresses.XXXXXX)"
awk -v new="$ADDRESSES_LINE" '
  { print }
  !done && /^REPUTATION_REGISTRY[[:space:]]*=/ { print new; done=1 }
' "$NOTES_ADDRESSES" > "$tmp_notes"

if ! grep -qF "$ADDRESSES_LINE" "$tmp_notes"; then
  rm -f "$tmp_notes"
  info "Could not find REPUTATION_REGISTRY anchor in $NOTES_ADDRESSES — leaving file untouched."
  info "Add manually under the ERC-8004 section:"
  info "  $ADDRESSES_LINE"
  exit 0
fi

mv "$tmp_notes" "$NOTES_ADDRESSES"
info "  $NOTES_ADDRESSES updated:"
info "  $ADDRESSES_LINE"
