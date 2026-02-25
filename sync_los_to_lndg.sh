#!/usr/bin/env bash
set -Eeuo pipefail

##################################################
# LOAD .env
##################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/.env"

##################################################
# CONFIG
##################################################

LOS_API="https://localhost:8443"
LNDG_API="${LNDG_BASE_URL%/}/api"

: "${LNDG_USER:?Missing LNDG_USER}"
: "${LNDG_PASS:?Missing LNDG_PASS}"

DRY_RUN=FALSE
DRY_RUN="${DRY_RUN,,}"

USE_LOS_TARGET_ELEGIBILITY=false
USE_LOS_SOURCE_ELEGIBILITY=false

# VARIÁVEIS TELEGRAM
SEND_SYNC_TO_LNDG_CHANGES_TELEGRAM=${SEND_SYNC_TO_LNDG_CHANGES_TELEGRAM:-true}
SEND_SYNC_TO_LNDG_ERROR_TELEGRAM=${SEND_SYNC_TO_LNDG_ERROR_TELEGRAM:-true}

: "${TELEGRAM_SYNC_TO_LNDG_TOKEN:?Missing TELEGRAM_SYNC_TO_LNDG_TOKEN}"
: "${TELEGRAM_SYNC_TO_LNDG_CHAT_ID:?Missing TELEGRAM_SYNC_TO_LNDG_CHAT_ID}"

LOS_HASH_FILE="/tmp/los_channels.hash"
ERROR_STATE_FILE="/tmp/sync_los_to_lndg.error_state"
ERROR_THRESHOLD=2

##################################################
# TELEGRAM
##################################################

send_telegram() {
  local message="$1"

  log "📨 Enviando mensagem para Telegram..."

  response=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_SYNC_TO_LNDG_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_SYNC_TO_LNDG_CHAT_ID}" \
    --data-urlencode "text=${message}" \
    -d "parse_mode=HTML")

  if echo "$response" | jq -e '.ok == true' >/dev/null 2>&1; then
    log "✅ Telegram enviado com sucesso"
  else
    log "❌ Falha ao enviar Telegram"
    log "Resposta da API:"
    log "$response"
  fi
}

##################################################
# ERROR HANDLER
##################################################

get_error_counter() {
  if [[ -f "$ERROR_STATE_FILE" ]]; then
    cat "$ERROR_STATE_FILE"
  else
    echo 0
  fi
}

increment_error_counter() {
  local current
  current=$(get_error_counter)
  current=$((current + 1))
  echo "$current" > "$ERROR_STATE_FILE"
  echo "$current"
}

reset_error_counter() {
  echo 0 > "$ERROR_STATE_FILE"
}

handle_error() {
  local exit_code=$?
  local line_no=$1
  local last_command="${BASH_COMMAND}"
  local hostname="$(hostname)"
  local script_name="$(basename "$0")"
  local timestamp="$(date '+%d/%m/%Y %H:%M:%S')"

  local severity="CRITICAL"
  local category="UNKNOWN"
  local service="UNKNOWN"
  local human_message="Unexpected failure"

  ##################################################
  # SERVICE DETECTION
  ##################################################

  if [[ "$last_command" == *"/api/channels"* ]]; then
    service="LNDg API"
  elif [[ "$last_command" == *"/api/rebalance"* ]]; then
    service="LightningOS API"
  fi

  ##################################################
  # CURL ERROR MAPPING
  ##################################################

  case "$exit_code" in
    6)
      category="NETWORK"
      human_message="Could not resolve host"
      ;;
    7)
      category="NETWORK"
      human_message="Failed to connect to host (service down or port blocked)"
      ;;
    22)
      category="HTTP"
      human_message="HTTP response >= 400 (API returned error)"
      ;;
    28)
      category="NETWORK"
      human_message="Operation timeout"
      ;;
    35)
      category="TLS"
      human_message="TLS/SSL handshake failure"
      ;;
    52)
      category="NETWORK"
      human_message="Empty reply from server"
      ;;
    56)
      category="NETWORK"
      human_message="Connection reset by peer"
      ;;
    127)
      category="SYSTEM"
      human_message="Command not found"
      ;;
    1)
      category="GENERAL"
      human_message="General error (possible jq, variable, or logic failure)"
      ;;
  esac

  ##################################################
  # ERROR COUNTER LOGIC
  ##################################################

  local error_count
  error_count=$(increment_error_counter)

  log "❗ Consecutive error count: $error_count"

  if [[ "$SEND_SYNC_TO_LNDG_ERROR_TELEGRAM" == "true" && "$error_count" -ge "$ERROR_THRESHOLD" ]]; then

    send_telegram "🚨 <b>SYNC LOS → LNDg FAILURE</b>

🖥️ Host: ${hostname}
📄 Script: ${script_name}
🕒 Time: ${timestamp}

🔴 Severity: ${severity}
📂 Category: ${category}
🌐 Service: ${service}

💬 Description:
${human_message}

📍 Line: ${line_no}
🔢 Exit code: ${exit_code}

⚙️ Last command:
${last_command}

📊 Consecutive failures: ${error_count}"

  else
    log "ℹ️ Error threshold not reached → Telegram suppressed"
  fi

  exit $exit_code
}

trap 'handle_error $LINENO' ERR

##################################################
# LOG
##################################################

log() { echo -e "$1"; }

##################################################
# CHECK LOS HASH (skip if unchanged)
##################################################

log "🔎 Checking LOS state hash..."

LOS_CONFIG_RAW=$(curl -s -k "$LOS_API/api/rebalance/config")

LOS_CONFIG_NORMALIZED=$(echo "$LOS_CONFIG_RAW" | \
  jq -S '{econ_ratio}')

LOS_JSON_RAW=$(curl -s -k "$LOS_API/api/rebalance/channels")

LOS_JSON_NORMALIZED=$(echo "$LOS_JSON_RAW" | \
  jq -S '[.channels[] | {
        channel_id,
        target_outbound_pct,
        auto_enabled,
        excluded_as_source,
        econ_ratio_override,
        use_default_econ_ratio
      }]')

COMBINED_HASH_INPUT=$(printf "%s\n%s" \
  "$LOS_JSON_NORMALIZED" \
  "$LOS_CONFIG_NORMALIZED")

LOS_HASH=$(echo "$COMBINED_HASH_INPUT" | sha256sum | awk '{print $1}')

if [[ -f "$LOS_HASH_FILE" ]]; then
  OLD_HASH=$(cat "$LOS_HASH_FILE")
else
  OLD_HASH=""
fi

if [[ "$LOS_HASH" == "$OLD_HASH" ]]; then
  log "🟢 LOS unchanged → skipping sync"
  exit 0
fi

log "🟡 LOS change detected → continuing sync"

echo "$LOS_HASH" > "$LOS_HASH_FILE"
log ""

##################################################
# LOAD LNDg CHANNEL STATE (ONCE)
##################################################

log "📥 Carregando estado atual dos canais no LNDg..."

LNDG_JSON=$(curl -s \
  -u "$LNDG_USER:$LNDG_PASS" \
  "$LNDG_API/channels/?page_size=1000")

declare -A LNDG_IN_TARGET=()
declare -A LNDG_OUT_TARGET=()
declare -A LNDG_AR_ENABLED=()
declare -A LNDG_AR_AMT_TARGET=()
declare -A LNDG_AR_MAX_COST=()

while read -r row; do
  chan_id="$(jq -r '.chan_id' <<<"$row")"
  [[ -z "$chan_id" || "$chan_id" == "null" ]] && continue

  LNDG_IN_TARGET["$chan_id"]="$(jq -r '.ar_in_target' <<<"$row")"
  LNDG_OUT_TARGET["$chan_id"]="$(jq -r '.ar_out_target' <<<"$row")"
  LNDG_AR_ENABLED["$chan_id"]="$(jq -r '.auto_rebalance' <<<"$row")"
  LNDG_AR_AMT_TARGET["$chan_id"]="$(jq -r '.ar_amt_target' <<<"$row")"
  LNDG_AR_MAX_COST["$chan_id"]="$(jq -r '.ar_max_cost' <<<"$row")"
done < <(jq -c '.results[]' <<<"$LNDG_JSON")

log "✅ ${#LNDG_IN_TARGET[@]} canais carregados do LNDg"
log ""

log "📥 Carregando config global do LOS..."

LOS_CONFIG=$(curl -s -k "$LOS_API/api/rebalance/config")

LOS_DEFAULT_ECON_RATIO="$(jq -r '.econ_ratio' <<<"$LOS_CONFIG")"

if [[ -z "$LOS_DEFAULT_ECON_RATIO" || "$LOS_DEFAULT_ECON_RATIO" == "null" ]]; then
  log "❌ Não foi possível obter econ_ratio default do LOS"
  exit 1
fi

log "✅ Default econ_ratio LOS: $LOS_DEFAULT_ECON_RATIO"
log ""

##################################################
# PATCH LNDg (somente se mudar)
##################################################

patch_lndg_if_needed() {
  local chan_id="$1"
  local new_in="$2"
  local new_out="$3"
  local new_ar="$4"
  local new_max_cost="$5"
  local alias="$6"

  local cur_in="${LNDG_IN_TARGET[$chan_id]:-}"
  local cur_out="${LNDG_OUT_TARGET[$chan_id]:-}"
  local cur_ar="${LNDG_AR_ENABLED[$chan_id]:-}"
  local cur_amt="${LNDG_AR_AMT_TARGET[$chan_id]:-}"
  local cur_cost="${LNDG_AR_MAX_COST[$chan_id]:-}"

  if [[ "$cur_in" == "$new_in" &&
        "$cur_out" == "$new_out" &&
        "$cur_ar" == "$new_ar" &&
        "$cur_cost" == "$new_max_cost" ]]; then
    log "   ↪ Nenhuma mudança necessária (LNDg já está correto)"
    return
  fi

  log "   🔄 Diferença detectada:"
  log "      In  : $cur_in → $new_in"
  log "      Out : $cur_out → $new_out"
  log "      AR  : $cur_ar → $new_ar"
  log "      MaxCost : $cur_cost → $new_max_cost"

  if [[ "$DRY_RUN" == "true" ]]; then
    log "   🚧 DRY RUN → PUT ignorado"
    return
  fi

  log "   🔧 PUT LNDg (/channels/$chan_id/)"

  curl -s -X PUT \
    -u "$LNDG_USER:$LNDG_PASS" \
    "$LNDG_API/channels/$chan_id/" \
    -H "Content-Type: application/json" \
    -d "{
      \"ar_in_target\": $new_in,
      \"ar_out_target\": $new_out,
      \"auto_rebalance\": $new_ar,
      \"ar_amt_target\": $cur_amt,
      \"ar_max_cost\": $new_max_cost
    }" >/dev/null

  if [[ "$SEND_SYNC_TO_LNDG_CHANGES_TELEGRAM" == "true" ]]; then
    target_fill=$((100 - new_in))

    send_telegram "🔄 <b>SYNC LOS → LNDg CHANGE</b>

  Channel: ${alias}
  SCID: ${chan_id}

  In Target:
  ${cur_in}% → ${new_in}% (encher até ${target_fill}%)

  Out Target:
  ${cur_out}% → ${new_out}%

  Auto Rebalance:
  ${cur_ar} → ${new_ar}

  Max Cost ($econ_source):
  ${cur_cost}% → ${new_max_cost}%"

  fi
}

##################################################
# LOOP LOS
##################################################

#log "🧪 Simulando erro para teste... Descomentar o false"
# TEST MODE
#false

log "=================================================="
log " LightningOS → LNDg SYNC (smart mode)"
log " DRY_RUN = $DRY_RUN"
log "=================================================="
log ""

echo "$LOS_JSON_RAW" |
jq -c '.channels[]' |
while read -r channel; do

  chan_id="$(jq -r '.channel_id | tostring' <<<"$channel")"
  alias="$(jq -r '.peer_alias' <<<"$channel")"

  econ_ratio="$(jq -r '.econ_ratio' <<<"$channel")"

  # fallback caso venha null
  if [[ -z "$econ_ratio" || "$econ_ratio" == "null" ]]; then
    econ_ratio=0
  fi

  # converter para %
  new_max_cost="$(printf "%.0f" "$(echo "$econ_ratio * 100" | bc -l)")"

  target_outbound_pct="$(jq -r '.target_outbound_pct' <<<"$channel")"
  auto_enabled="$(jq -r '.auto_enabled' <<<"$channel")"
  excluded_as_source="$(jq -r '.excluded_as_source' <<<"$channel")"

  eligible_as_target="$(jq -r '.eligible_as_target' <<<"$channel")"
  eligible_as_source="$(jq -r '.eligible_as_source' <<<"$channel")"

  in_target=$((100 - target_outbound_pct))
  out_target=$([[ "$excluded_as_source" == "true" ]] && echo 100 || echo 5)

  if [[ "$auto_enabled" == "true" ]]; then
    ar_enabled=true
    ar_reason="auto_enabled = true (fallback)"
  else
    ar_enabled=false
    ar_reason="auto_enabled = false (fallback)"
  fi

  if [[ "$USE_LOS_TARGET_ELEGIBILITY" == "true" && "$eligible_as_target" == "true" ]]; then
    ar_enabled=true
    ar_reason="eligible_as_target = true → TARGET"
  fi

  if [[ "$USE_LOS_SOURCE_ELEGIBILITY" == "true" && "$eligible_as_source" == "true" ]]; then
    ar_enabled=false
    ar_reason="eligible_as_source = true → SOURCE"
  fi

  use_default="$(jq -r '.use_default_econ_ratio' <<<"$channel")"
  econ_override="$(jq -r '.econ_ratio_override // empty' <<<"$channel")"

  if [[ "$use_default" == "false" && -n "$econ_override" ]]; then
    effective_econ_ratio="$econ_override"
    econ_source="override"
  else
    effective_econ_ratio="$LOS_DEFAULT_ECON_RATIO"
    econ_source="default"
  fi

  new_max_cost=$(awk "BEGIN { printf \"%d\", $effective_econ_ratio * 100 }")

  ar_label=$([[ "$ar_enabled" == "true" ]] && echo "Enabled (TARGET)" || echo "Disabled (SOURCE)")

  log " Canal: $alias"
  log " Channel ID (SCID): $chan_id"
  log "--------------------------------------------------"
  log " [LightningOS]"
  log "   target_outbound_pct : ${target_outbound_pct}%"
  log "   auto_enabled        : $auto_enabled"
  log "   eligible_as_target  : $eligible_as_target"
  log "   eligible_as_source  : $eligible_as_source"
  log "   excluded_as_source  : $excluded_as_source (apenas Out Target)"
  log "   econ_ratio ($econ_source): $effective_econ_ratio"
  log ""
  log " [LNDg – estado alvo]"
  log "   In Target  : → ${in_target}%"
  log "   Out Target : → ${out_target}%"
  log "   AR         : → ${ar_label}"
  log "   Motivo     : ${ar_reason}"
  log "   Max Cost (LNDg)    : → ${new_max_cost}%"
  log ""

  patch_lndg_if_needed "$chan_id" "$in_target" "$out_target" "$ar_enabled" "$new_max_cost" "$alias"
  log ""

done

log "=================================================="
log " Sync finalizado"
log "=================================================="

reset_error_counter
