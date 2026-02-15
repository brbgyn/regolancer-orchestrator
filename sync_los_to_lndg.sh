#!/usr/bin/env bash
set -euo pipefail

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

DRY_RUN=false

USE_LOS_TARGET_ELEGIBILITY=false
USE_LOS_SOURCE_ELEGIBILITY=false

# VARIÁVEIS TELEGRAM
SEND_SYNC_TO_LNDG_CHANGES_TELEGRAM=${SEND_SYNC_TO_LNDG_CHANGES_TELEGRAM:-true}
SEND_SYNC_TO_LNDG_ERROR_TELEGRAM=${SEND_SYNC_TO_LNDG_ERROR_TELEGRAM:-true}

: "${TELEGRAM_SYNC_TO_LNDG_TOKEN:?Missing TELEGRAM_SYNC_TO_LNDG_TOKEN}"
: "${TELEGRAM_SYNC_TO_LNDG_CHAT_ID:?Missing TELEGRAM_SYNC_TO_LNDG_CHAT_ID}"

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

handle_error() {
  local exit_code=$?
  local line_no=$1

  if [[ "$SEND_SYNC_TO_LNDG_ERROR_TELEGRAM" == "true" ]]; then
    send_telegram "❌ <b>SYNC LOS → LNDg ERROR</b>

    Exit code: ${exit_code}
    Line: ${line_no}"

  fi

  exit $exit_code
}

trap 'handle_error $LINENO' ERR

##################################################
# LOG
##################################################

log() { echo -e "$1"; }

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

##################################################
# PATCH LNDg (somente se mudar)
##################################################

patch_lndg_if_needed() {
  local chan_id="$1"
  local new_in="$2"
  local new_out="$3"
  local new_ar="$4"
  local alias="$5"

  local cur_in="${LNDG_IN_TARGET[$chan_id]:-}"
  local cur_out="${LNDG_OUT_TARGET[$chan_id]:-}"
  local cur_ar="${LNDG_AR_ENABLED[$chan_id]:-}"
  local cur_amt="${LNDG_AR_AMT_TARGET[$chan_id]:-}"
  local cur_cost="${LNDG_AR_MAX_COST[$chan_id]:-}"

  if [[ "$cur_in" == "$new_in" &&
        "$cur_out" == "$new_out" &&
        "$cur_ar" == "$new_ar" ]]; then
    log "   ↪ Nenhuma mudança necessária (LNDg já está correto)"
    return
  fi

  log "   🔄 Diferença detectada:"
  log "      In  : $cur_in → $new_in"
  log "      Out : $cur_out → $new_out"
  log "      AR  : $cur_ar → $new_ar"

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
      \"ar_max_cost\": $cur_cost
    }" >/dev/null

  if [[ "$SEND_SYNC_TO_LNDG_CHANGES_TELEGRAM" == "true" ]]; then
    send_telegram "🔄 <b>SYNC LOS → LNDg CHANGE</b>
    Channel: ${alias}
    SCID: ${chan_id}
    In: ${cur_in} → ${new_in}
    Out: ${cur_out} → ${new_out}
    AR: ${cur_ar} → ${new_ar}"
  fi
}

##################################################
# LOOP LOS
##################################################

#log "🧪 Simulando erro para teste..."
#false

log "=================================================="
log " LightningOS → LNDg SYNC (smart mode)"
log " DRY_RUN = $DRY_RUN"
log "=================================================="
log ""

curl -s -k "$LOS_API/api/rebalance/channels" |
jq -c '.channels[]' |
while read -r channel; do

  chan_id="$(jq -r '.channel_id | tostring' <<<"$channel")"
  alias="$(jq -r '.peer_alias' <<<"$channel")"

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
  log ""
  log " [LNDg – estado alvo]"
  log "   In Target  : → ${in_target}%"
  log "   Out Target : → ${out_target}%"
  log "   AR         : → ${ar_label}"
  log "   Motivo     : ${ar_reason}"
  log ""

  patch_lndg_if_needed "$chan_id" "$in_target" "$out_target" "$ar_enabled" "$alias"
  log ""

done

log "=================================================="
log " Sync finalizado"
log "=================================================="
