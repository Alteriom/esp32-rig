#!/usr/bin/env bash
# Carry out what the portal asked a node for that needs the host's sudo: run by
# alteriom-hil-control.service when alteriom-hil-control.path sees
# <update dir>/control.json, which the node agent writes (alteriom_hil.farm_node).
#
#   restart          restart the farm service -- the node -- itself
#   logs             the tail of the node's service logs
#   configure        change remote settings (hil_config.REMOTE_SETTINGS) with
#                    `alteriom-hil-admin config set-many`, then restart the node
#   notify_set       point notifications at a channel, its credential sealed
#                    in the browser to this rig's key like a provider's link
#   notify_tune      turn one channel off or on, or choose what it says
#   notify_remove    stop sending notifications
#   notify_test      send one message down the configured channel
#   provider_set     store a provider's link that was sealed in the browser to
#                    this rig's key: decrypted with the private key straight
#                    into `alteriom-hil-admin providers set` -- a pipe, never an
#                    argument or a file -- then restart the node
#   provider_remove  delete a provider's stored link, then restart the node
#   provider_test    send ONE real test message with `alteriom-hil-admin
#                    providers test` (today's budget); report its one result
#                    line, restart nothing
#
# The outcome goes to control-result-<id>.json, which the agent reports to the
# portal -- for a restart, the agent that comes back. Nothing a provider command
# decrypts is ever in it: only the admin CLI's redacted confirmation, or an
# error that does not include the link. Runs as the runner user, with its
# non-interactive sudo, like node-update.sh.

set -euo pipefail

WORK=""
TAKEN=""
cleanup() {
  [ -z "$WORK" ] || rm -rf "$WORK"
  [ -z "$TAKEN" ] || rm -f "$TAKEN"
}
trap cleanup EXIT

main() {
  local dir="${ALTERIOM_HIL_UPDATE_DIR:-/var/lib/alteriom-hil/update}"
  local request="$dir/control.json" taken="$dir/control.taken.json"
  [ -f "$request" ] || exit 0
  mv -f "$request" "$taken"
  # The request can carry a sealed link: whatever happens below, it goes.
  TAKEN="$taken"

  local id action
  id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id", ""))' "$taken")"
  action="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("action", ""))' "$taken")"
  if ! [[ "$id" =~ ^[0-9a-f]{32}$ ]]; then
    echo "node-control: the request names no command id" >&2
    rm -f "$taken"
    exit 1
  fi

  # outcome STATUS DETAIL [RESULT_JSON_FILE]
  outcome() {
    python3 - "$dir/control-result-$id.json" "$id" "$1" "$2" "${3:-}" <<'PY'
import json, os, sys
from datetime import datetime, timezone
path, command, status, detail, result_file = sys.argv[1:6]
record = {"id": command, "status": status, "detail": detail[-8000:] or None,
          "at": datetime.now(timezone.utc).isoformat()}
if result_file:
    with open(result_file, encoding="utf-8") as handle:
        record["result"] = json.load(handle)
with open(path + ".tmp", "w", encoding="utf-8") as handle:
    json.dump(record, handle)
os.replace(path + ".tmp", path)
PY
  }

  local venv admin seal_key
  venv="${ALTERIOM_HIL_VENV:-/home/${USER:-runner}/.local/share/alteriom-hil/venv}"
  # The admin CLI is a console script in the venv now, not a file a
  # deploy copied (docs/public-release-plan.md, step 12d).
  admin="$venv/bin/alteriom-hil-admin"
  seal_key="${ALTERIOM_HIL_SEAL_KEY:-/etc/alteriom-hil/provider-seal.key}"
  # mktemp -d is 0700: only the runner user reads what is staged here, and
  # nothing staged here is ever the decrypted link.
  WORK="$(mktemp -d)"

  case "$action" in
    restart)
      outcome done "restarting alteriom-hil-farm"
      rm -f "$taken"
      sudo systemctl restart alteriom-hil-farm.service
      ;;
    logs)
      local lines
      lines="$(python3 -c 'import json,sys; n=json.load(open(sys.argv[1])).get("args", {}).get("lines", 200); print(max(10, min(int(n), 2000)))' "$taken")"
      sudo journalctl -u alteriom-hil-farm.service -u alteriom-hil-update.service -u alteriom-hil-control.service \
        -n "$lines" --no-pager -o short-iso > "$WORK/log.txt" 2>&1 || true
      python3 -c 'import json,sys; text=open(sys.argv[1], encoding="utf-8", errors="replace").read()[-400000:]; json.dump({"log": text}, open(sys.argv[2], "w"))' "$WORK/log.txt" "$WORK/result.json"
      outcome done "the last $lines lines" "$WORK/result.json"
      rm -f "$taken"
      ;;
    configure)
      python3 -c 'import json,sys; json.dump(json.load(open(sys.argv[1])).get("args", {}).get("settings", {}), open(sys.argv[2], "w"))' "$taken" "$WORK/settings.json"
      if sudo "$admin" config set-many --file "$WORK/settings.json" > "$WORK/applied.txt" 2>&1; then
        python3 -c 'import json,sys; lines=[l for l in open(sys.argv[1]).read().splitlines() if l.startswith("{")]; json.dump({"applied": json.loads(lines[-1]) if lines else {}}, open(sys.argv[2], "w"))' "$WORK/applied.txt" "$WORK/result.json"
        outcome done "settings applied; restarting alteriom-hil-farm" "$WORK/result.json"
        rm -f "$taken"
        sudo systemctl restart alteriom-hil-farm.service
      else
        outcome failed "$(tail -n 20 "$WORK/applied.txt")"
        rm -f "$taken"
      fi
      ;;
    provider_set)
      # The ciphertext to a file of its own (it is not the link: only this
      # rig's private key opens it), the provider and fingerprint to stdout.
      local fields provider fingerprint local_fingerprint
      fields="$(python3 -c '
import base64, binascii, json, re, sys
args = json.load(open(sys.argv[1])).get("args", {})
sealed = args.get("sealed")
try:
    data = base64.b64decode(sealed, validate=True) if isinstance(sealed, str) else b""
except (binascii.Error, ValueError):
    data = b""
open(sys.argv[2], "wb").write(data)
provider = args.get("provider") if isinstance(args.get("provider"), str) else ""
fingerprint = args.get("fingerprint") if isinstance(args.get("fingerprint"), str) else ""
print(re.sub(r"[^a-z0-9_]", "", provider)[:32], re.sub(r"[^0-9a-f]", "", fingerprint)[:64])
' "$taken" "$WORK/sealed.bin")"
      rm -f "$taken"
      provider="${fields%% *}"
      fingerprint="${fields#* }"
      if [ "$provider" != "callmebot" ]; then
        outcome failed "this node cannot store a link for the provider ${provider:-(none)}"
        return 0
      fi
      if [ "$(stat -c %s "$WORK/sealed.bin")" -ne 384 ]; then
        outcome failed "the sealed link is not an RSA-3072 ciphertext; nothing was changed"
        return 0
      fi
      local_fingerprint="$(sudo "$admin" providers seal-key --fingerprint 2>/dev/null || true)"
      if ! [[ "$local_fingerprint" =~ ^[0-9a-f]{64}$ ]]; then
        outcome failed "this rig has no seal key; run: sudo alteriom-hil-admin providers seal-key"
        return 0
      fi
      if [ "$fingerprint" != "$local_fingerprint" ]; then
        outcome failed "sealed for the key ${fingerprint:0:16}..., and this rig's key is ${local_fingerprint:0:16}...: reload the rig's page and set the link again"
        return 0
      fi
      # Decrypted into the admin CLI's stdin and nowhere else. openssl's own
      # errors are dropped (a failed decrypt has no plaintext to show, and a
      # generic reason is all the portal needs); the CLI prints the link only
      # redacted, and only its confirmation or error lines are kept.
      local codes reason
      set +e
      sudo openssl pkeyutl -decrypt -inkey "$seal_key" -in "$WORK/sealed.bin" \
          -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 -pkeyopt rsa_mgf1_md:sha256 2>/dev/null \
        | sudo "$admin" providers set callmebot > "$WORK/stored.txt" 2>&1
      codes="${PIPESTATUS[*]}"
      set -e
      if [ "${codes%% *}" != "0" ]; then
        outcome failed "this rig's key could not open the sealed link; nothing was changed"
        return 0
      fi
      if [ "${codes##* }" != "0" ]; then
        reason="$(grep -E '^error: ' "$WORK/stored.txt" | tail -n 3 || true)"
        outcome failed "${reason:-storing the link failed (status ${codes##* }); nothing was changed}"
        return 0
      fi
      outcome done "$(grep -E '^(stored|added) ' "$WORK/stored.txt" | tr '\n' ' ' | sed 's/ *$//'); restarting alteriom-hil-farm"
      sudo systemctl restart alteriom-hil-farm.service
      ;;
    notify_set)
      # The same shape as provider_set: the ciphertext to a file of its own,
      # the channel and its plain settings to stdout. What the credential is
      # -- a bot token, a webhook URL -- only this rig's private key can say.
      local fields channel chat_id format notify_id local_fingerprint fingerprint
      fields="$(python3 -c '
import base64, binascii, json, re, sys
args = json.load(open(sys.argv[1])).get("args", {})
sealed = args.get("sealed")
try:
    data = base64.b64decode(sealed, validate=True) if isinstance(sealed, str) else b""
except (binascii.Error, ValueError):
    data = b""
open(sys.argv[2], "wb").write(data)
settings = args.get("settings") if isinstance(args.get("settings"), dict) else {}
def clean(value, pattern, limit):
    return re.sub(pattern, "", value)[:limit] if isinstance(value, str) else ""
print(clean(args.get("channel"), r"[^a-z0-9_]", 32),
      clean(args.get("fingerprint"), r"[^0-9a-f]", 64),
      clean(settings.get("chat_id"), r"[^0-9-]", 32) or "-",
      clean(settings.get("format"), r"[^a-z]", 16) or "-",
      clean(settings.get("id"), r"[^0-9a-z]", 16) or "-")
' "$taken" "$WORK/sealed.bin")"
      rm -f "$taken"
      channel="$(printf %s "$fields" | cut -d" " -f1)"
      fingerprint="$(printf %s "$fields" | cut -d" " -f2)"
      chat_id="$(printf %s "$fields" | cut -d" " -f3)"
      format="$(printf %s "$fields" | cut -d" " -f4)"
      notify_id="$(printf %s "$fields" | cut -d" " -f5)"
      if [ "$channel" = "callmebot" ]; then
        # No credential to open: the link this rig already validates with is
        # the one it will notify through.
        rm -f "$WORK/sealed.bin"
        local cmb_args=(notify set --channel callmebot)
        [ "$(printf %s "$fields" | cut -d" " -f5)" != "-" ]           && cmb_args+=(--id "$(printf %s "$fields" | cut -d" " -f5)")
        if sudo "$admin" "${cmb_args[@]}" > "$WORK/notify.txt" 2>&1; then
          outcome done "$(grep -E '^channel ' "$WORK/notify.txt" | tail -n 1)"
        else
          outcome failed "$(grep -E '^error: ' "$WORK/notify.txt" | tail -n 2 || tail -n 2 "$WORK/notify.txt")"
        fi
        return 0
      fi
      if [ "$channel" != "telegram" ] && [ "$channel" != "webhook" ]; then
        outcome failed "this node cannot set the channel ${channel:-(none)}"
        return 0
      fi
      if [ "$(stat -c %s "$WORK/sealed.bin")" -ne 384 ]; then
        outcome failed "the sealed credential is not an RSA-3072 ciphertext; nothing was changed"
        return 0
      fi
      local_fingerprint="$(sudo "$admin" providers seal-key --fingerprint 2>/dev/null || true)"
      if ! [[ "$local_fingerprint" =~ ^[0-9a-f]{64}$ ]]; then
        outcome failed "this rig has no seal key; run: sudo alteriom-hil-admin providers seal-key"
        return 0
      fi
      if [ "$fingerprint" != "$local_fingerprint" ]; then
        outcome failed "sealed for a different key than this rig has; nothing was changed"
        return 0
      fi
      local set_args codes reason
      if [ "$channel" = "telegram" ]; then
        set_args=(notify set --channel telegram --chat-id "$chat_id")
      else
        set_args=(notify set --channel webhook)
        [ "$format" != "-" ] && set_args+=(--format "$format")
      fi
      # Named, it replaces that channel; unnamed, it adds one -- a rig can be
      # told to say things in several places.
      [ "$notify_id" != "-" ] && set_args+=(--id "$notify_id")
      set +e
      sudo openssl pkeyutl -decrypt -inkey "$seal_key" -in "$WORK/sealed.bin" \
          -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 -pkeyopt rsa_mgf1_md:sha256 2>/dev/null \
        | sudo "$admin" "${set_args[@]}" > "$WORK/notify.txt" 2>&1
      codes="${PIPESTATUS[*]}"
      set -e
      if [ "${codes%% *}" != "0" ]; then
        outcome failed "this rig's key could not open the sealed credential; nothing was changed"
        return 0
      fi
      if [ "${codes##* }" != "0" ]; then
        reason="$(grep -E '^error: ' "$WORK/notify.txt" | tail -n 3 || true)"
        outcome failed "${reason:-setting the channel failed (status ${codes##* }); nothing was changed}"
        return 0
      fi
      outcome done "$(grep -E '^channel ' "$WORK/notify.txt" | tail -n 1)"
      ;;
    notify_tune)
      # Neither turning a channel off nor choosing what it says is a
      # credential, so this carries none: only which channel, and what of it.
      local tune_fields tune_id tune_state tune_events tune_args
      tune_fields="$(python3 -c '
import json, re, sys
args = json.load(open(sys.argv[1])).get("args", {})
settings = args.get("settings") if isinstance(args.get("settings"), dict) else {}
def clean(value, pattern, limit):
    return re.sub(pattern, "", value)[:limit] if isinstance(value, str) else ""
state = settings.get("enabled")
print(clean(settings.get("id"), r"[^0-9a-z]", 16) or "-",
      "on" if state is True else "off" if state is False else "-",
      clean(settings.get("events"), r"[^a-z_,]", 120) or "-")
' "$taken")"
      rm -f "$taken"
      tune_id="$(printf %s "$tune_fields" | cut -d" " -f1)"
      tune_state="$(printf %s "$tune_fields" | cut -d" " -f2)"
      tune_events="$(printf %s "$tune_fields" | cut -d" " -f3)"
      tune_args=(notify tune)
      [ "$tune_id" != "-" ] && tune_args+=(--id "$tune_id")
      [ "$tune_state" = "on" ] && tune_args+=(--on)
      [ "$tune_state" = "off" ] && tune_args+=(--off)
      [ "$tune_events" != "-" ] && tune_args+=(--events "$tune_events")
      if sudo "$admin" "${tune_args[@]}" > "$WORK/tune.txt" 2>&1; then
        outcome done "$(grep -E '^channel ' "$WORK/tune.txt" | tail -n 1)"
      else
        outcome failed "$(grep -E '^error: ' "$WORK/tune.txt" | tail -n 2 || tail -n 2 "$WORK/tune.txt")"
      fi
      ;;
    notify_remove)
      rm -f "$taken"
      if sudo "$admin" config set notify.enabled false > "$WORK/off.txt" 2>&1; then
        outcome done "notifications are off; the credential file is left for putting them back on"
        sudo systemctl restart alteriom-hil-farm.service
      else
        outcome failed "$(tail -n 5 "$WORK/off.txt")"
      fi
      ;;
    notify_test)
      rm -f "$taken"
      set +e
      sudo "$admin" notify test > "$WORK/test.txt" 2>&1
      local code="$?"
      set -e
      if [ "$code" = "0" ]; then
        outcome done "$(tail -n 1 "$WORK/test.txt")"
      else
        outcome failed "$(tail -n 1 "$WORK/test.txt")"
      fi
      ;;
    provider_remove)
      local provider
      provider="$(python3 -c 'import json,re,sys; p=json.load(open(sys.argv[1])).get("args", {}).get("provider"); print(re.sub(r"[^a-z0-9_]", "", p)[:32] if isinstance(p, str) else "")' "$taken")"
      rm -f "$taken"
      if [ "$provider" != "callmebot" ]; then
        outcome failed "this node cannot remove a link for the provider ${provider:-(none)}"
        return 0
      fi
      if sudo "$admin" providers remove callmebot > "$WORK/removed.txt" 2>&1; then
        outcome done "$(grep -E '^(removed|nothing stored) ' "$WORK/removed.txt" | tail -n 1); restarting alteriom-hil-farm"
        sudo systemctl restart alteriom-hil-farm.service
      else
        outcome failed "$(tail -n 5 "$WORK/removed.txt")"
      fi
      ;;
    provider_test)
      # One real message from this host, taken from today's budget. Only the
      # admin CLI's single result line is reported -- never its raw output --
      # and nothing restarts.
      local provider line code
      provider="$(python3 -c 'import json,re,sys; p=json.load(open(sys.argv[1])).get("args", {}).get("provider"); print(re.sub(r"[^a-z0-9_]", "", p)[:32] if isinstance(p, str) else "")' "$taken")"
      rm -f "$taken"
      if [ "$provider" != "callmebot" ]; then
        outcome failed "this node cannot send a test message for the provider ${provider:-(none)}"
        return 0
      fi
      set +e
      sudo "$admin" providers test callmebot > "$WORK/tested.txt" 2>&1
      code=$?
      set -e
      line="$(grep -E '^(test message queued|CallMeBot refused:|CallMeBot did not queue it:|could not reach |no reply from |error: )' "$WORK/tested.txt" | tail -n 1 | cut -c 1-300 || true)"
      if [ "$code" -eq 0 ] && [ -n "$line" ]; then
        outcome done "$line"
      else
        outcome failed "${line:-the test message command failed (status $code)}"
      fi
      ;;
    *)
      outcome failed "this node's control unit does not know $action"
      rm -f "$taken"
      ;;
  esac
}

main "$@"
