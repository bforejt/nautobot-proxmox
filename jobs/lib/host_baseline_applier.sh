# shellcheck shell=bash
# NFV Host Baseline applier — jobs/lib/host_baseline_applier.sh
#
# Uploaded by the `Host Baseline (SoT-driven)` job over the SSH session's
# STDIN (`bash -s` reads it; it is never written to the node's disk) and
# followed by ONE step payload: a function that declares the step's inputs as
# locals — secrets included, as shell-quoted literals rendered by the job —
# and calls the primitives below, then the call of that function. This file
# holds definitions only, so nothing executes before the last line arrives: a
# truncated upload runs nothing. Mechanics come from the tester's
# field-verified post-deploy script; every value comes from the SoT.
#
# Contract with the job (jobs/lib/host_baseline.py):
#   - results are stdout lines "@@NFV@@ <json>" (step, item, status, detail);
#     status is ok | changed | would_change | warning | failed | skipped | info
#   - DRY_RUN=1 (also the default when unset) reports, writes nothing
#   - secrets never travel on an argv: pveum/pvesh get none (the AD bind
#     password goes to PVE's own realm credential file, SNMPv3 passphrases to
#     snmpd's persistent file with snmpd stopped — both via bash builtins), and
#     nothing echoes them. The one secret that crosses back is a freshly
#     created API token value, on its own "token" event the job consumes
#     without logging.
#   - no `set -x`, ever (nfv_init forces it off).

nfv_paths() {
  : "${NFV_SNMPD_CONF:=/etc/snmp/snmpd.conf}"
  : "${NFV_SNMP_PERSIST:=/var/lib/snmp/snmpd.conf}"
  : "${NFV_STATE_DIR:=/var/lib/nfv-baseline}"
  : "${NFV_REALM_PW_DIR:=/etc/pve/priv/realm}"
  : "${NFV_IFACES:=/etc/network/interfaces}"
  : "${NFV_RUN_DIR:=/run/nfv-baseline}"
  : "${NFV_DMI_SERIAL:=/sys/class/dmi/id/product_serial}"
  : "${NFV_SYSNET:=/sys/class/net}"
  : "${NFV_BONDING:=/proc/net/bonding}"
}

nfv_init() {
  set +x
  set -o pipefail
  umask 022
  export LC_ALL=C
  export PATH="${PATH:-}:/usr/sbin:/usr/bin:/sbin:/bin"
  nfv_paths
  if [ "$(id -u)" != 0 ] && [ "${NFV_ALLOW_NONROOT:-0}" != 1 ]; then
    nfv_emit init root failed "the applier must run as root (host_ssh_username must be root)"
    exit 1
  fi
}

nfv_step_paths() {  # read-only: the files and directories this applier works on
  local name
  for name in NFV_SNMPD_CONF NFV_SNMP_PERSIST NFV_STATE_DIR NFV_REALM_PW_DIR NFV_IFACES NFV_RUN_DIR \
              NFV_DMI_SERIAL NFV_SYSNET NFV_BONDING; do
    nfv_emit paths "$name" info "${!name}"
  done
}

# ---- output ------------------------------------------------------------

nfv_json() {  # JSON string literal of $1 (control characters dropped)
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  s=$(printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037')
  printf '"%s"' "$s"
}

nfv_emit() {  # nfv_emit STEP ITEM STATUS DETAIL [extra JSON members]
  local extra=${5:-}
  printf '%s{"step":%s,"item":%s,"status":%s,"detail":%s%s}\n' "${NFV_PREFIX:-@@NFV@@ }" \
    "$(nfv_json "$1")" "$(nfv_json "$2")" "$(nfv_json "$3")" "$(nfv_json "$4")" "${extra:+,$extra}"
}

nfv_tail() {  # last lines of a command's output, one line, bounded
  printf '%s' "$1" | tail -n "${2:-4}" | tr '\n' ' ' | cut -c1-400
}

nfv_dry() {  # true unless DRY_RUN=0 (fail safe: unset means dry)
  [ "${DRY_RUN:-1}" != 0 ]
}

nfv_done() {
  nfv_emit done payload ok "payload finished"
}

nfv_in_list() {  # nfv_in_list NEEDLE ITEM...
  local needle=$1 item
  shift
  for item in "$@"; do [ "$item" = "$needle" ] && return 0; done
  return 1
}

nfv_json_match() {  # stdin: a JSON list; args KEY VALUE ... -> rc 0 when one element matches all
  python3 -c '
import json, sys
args = sys.argv[1:]
pairs = list(zip(args[::2], args[1::2]))
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(2)
items = data if isinstance(data, list) else []
sys.exit(0 if any(isinstance(e, dict) and all(str(e.get(k)) == v for k, v in pairs) for e in items) else 1)
' "$@"
}

nfv_write_file() {  # nfv_write_file PATH MODE VAR_NAME — atomic; content from the NAMED variable
  local path=$1 mode=$2 tmp
  tmp="$(dirname -- "$path")/.$(basename -- "$path").nfv.$$"
  if ! ( umask 077; printf '%s' "${!3}" > "$tmp" ); then rm -f -- "$tmp"; return 1; fi
  if chmod "$mode" -- "$tmp" && mv -f -- "$tmp" "$path"; then return 0; fi
  rm -f -- "$tmp"
  return 1
}

nfv_run() {  # nfv_run STEP ITEM argv... — one planned command (never carries a secret)
  local step=$1 item=$2 out rc
  shift 2
  if nfv_dry; then
    nfv_emit "$step" "$item" would_change "would run: $*"
    return 0
  fi
  out=$("$@" 2>&1 </dev/null)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    nfv_emit "$step" "$item" changed "ran: $*"
    return 0
  fi
  nfv_emit "$step" "$item" failed "rc=$rc from: $* — $(nfv_tail "$out")"
  return "$rc"
}

# ---- observe (read-only) -------------------------------------------------

nfv_raw() {  # nfv_raw ITEM argv... — stdout of a read-only command, base64, with its rc
  local item=$1 out rc
  shift
  out=$("$@" 2>/dev/null </dev/null)
  rc=$?
  printf '%s{"step":"observe","item":%s,"status":"info","rc":%d,"b64":"%s"}\n' "${NFV_PREFIX:-@@NFV@@ }" \
    "$(nfv_json "$item")" "$rc" "$(printf '%s' "$out" | base64 -w0)"
}

nfv_step_observe() {  # inputs: OBS_PKGS (array), OBS_REALM (may be empty)
  nfv_raw identity.hostname hostname
  nfv_raw identity.serial cat "$NFV_DMI_SERIAL"
  nfv_raw identity.uid id -u
  nfv_raw pve.version pveversion
  nfv_raw packages dpkg-query -W -f='${Package} ${db:Status-Abbrev}\n' "${OBS_PKGS[@]}"
  nfv_raw net.interfaces cat "$NFV_IFACES"
  nfv_raw net.pending sh -c 'if [ -e "$1" ]; then echo present; else echo absent; fi' sh "$NFV_IFACES.new"
  nfv_raw net.links ip -o link show
  nfv_raw net.physical sh -c 'for d in "$1"/*; do [ -e "$d/device" ] && echo "${d##*/}"; done; true' sh "$NFV_SYSNET"
  nfv_raw net.bonding sh -c 'for f in "$1"/*; do [ -e "$f" ] || continue; echo "=== ${f##*/}"; cat "$f"; done; true' sh "$NFV_BONDING"
  nfv_raw boot.cmdline cat /proc/cmdline
  nfv_raw pve.realms pvesh get /access/domains --output-format json
  if [ -n "${OBS_REALM:-}" ]; then
    nfv_raw pve.realm pvesh get "/access/domains/$OBS_REALM" --output-format json
  fi
  nfv_raw pve.sync_jobs pvesh get /cluster/jobs/realm-sync --output-format json
  nfv_raw pve.users pveum user list --full 1 --output-format json
  nfv_raw pve.acls pveum acl list --output-format json
  nfv_raw pve.groups pveum group list --output-format json
  nfv_raw pve.roles pveum role list --output-format json
}

# ---- packages ------------------------------------------------------------

nfv_service_enabled() {  # nfv_service_enabled STEP UNIT — enabled and active
  local step=$1 unit=$2 out rc
  if systemctl is-enabled --quiet "$unit" 2>/dev/null && systemctl is-active --quiet "$unit" 2>/dev/null; then
    nfv_emit "$step" "$unit" ok "enabled and active"
    return 0
  fi
  if nfv_dry; then
    nfv_emit "$step" "$unit" would_change "would run: systemctl enable --now $unit"
    return 0
  fi
  out=$(systemctl enable --now "$unit" 2>&1 </dev/null)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    nfv_emit "$step" "$unit" changed "enabled and started"
    return 0
  fi
  nfv_emit "$step" "$unit" failed "systemctl enable --now $unit rc=$rc: $(nfv_tail "$out")"
  return 1
}

nfv_apt_candidates() {  # PKG... -> lines "pkg <version|none|unknown>" from apt-cache policy; never fails
  local p cand
  for p in "$@"; do
    cand=""
    if command -v apt-cache >/dev/null 2>&1; then
      cand=$(apt-cache policy "$p" 2>/dev/null </dev/null | awk '/^  Candidate:/ {print $2; exit}')
    fi
    case "$cand" in "") cand=unknown ;; "(none)") cand=none ;; esac
    printf '%s %s\n' "$p" "$cand"
  done
}

nfv_apt_sources_summary() {  # one bounded line: the node's configured apt sources
  { grep -hs '^deb ' /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null
    grep -hs -E '^(URIs|Suites):' /etc/apt/sources.list.d/*.sources 2>/dev/null
  } | tr -s ' \n' ' ' | cut -c1-300
}

nfv_apt_uninstallable() {  # PKG... -> "pkg (none) pkg2 (unknown)" for the ones apt cannot install
  local p cand
  local -a bad=()
  while read -r p cand; do
    case "$cand" in unknown|none) bad+=("$p ($cand)") ;; esac
  done < <(nfv_apt_candidates "$@")
  printf '%s' "${bad[*]:-}"
}

# apt-get update reports fetch failures (no DNS, no egress, a dead mirror) as
# W: lines and exits 0 unless Error-Mode=any — so a node with no Debian index
# used to fail only at install time with "Unable to locate package". Now the
# candidates are checked first and the step says what is actually wrong.
NFV_APT_HINT="the node's package index is missing or its sources lack Debian 'main' — check DNS/egress from the node and /etc/apt/sources.list.d/, then: apt-get -o APT::Update::Error-Mode=any update"

nfv_step_packages() {  # inputs: PKGS (array)
  local p status out rc bad upd_note=""
  local -a missing=()
  for p in "${PKGS[@]}"; do
    status=$(dpkg-query -W -f='${db:Status-Abbrev}' "$p" 2>/dev/null)
    case "$status" in ii*) ;; *) missing+=("$p") ;; esac
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    nfv_emit packages packages ok "present: ${PKGS[*]}"
  elif nfv_dry; then
    bad=$(nfv_apt_uninstallable "${missing[@]}")
    if [ -n "$bad" ]; then
      nfv_emit packages apt warning "apt has no installable candidate for: $bad — $NFV_APT_HINT; sources: $(nfv_apt_sources_summary)"
    fi
    nfv_emit packages packages would_change "would install: ${missing[*]}"
  else
    out=$(DEBIAN_FRONTEND=noninteractive apt-get -q -o APT::Update::Error-Mode=any update 2>&1 </dev/null)
    rc=$?
    if [ "$rc" -ne 0 ]; then
      upd_note="apt-get update rc=$rc: $(nfv_tail "$out")"
      nfv_emit packages apt-update warning "$upd_note (trying the install anyway)"
    fi
    bad=$(nfv_apt_uninstallable "${missing[@]}")
    if [ -n "$bad" ]; then
      nfv_emit packages packages failed "apt has no installable candidate for: $bad — $NFV_APT_HINT${upd_note:+ ($upd_note)}; sources: $(nfv_apt_sources_summary)"
      return 1
    fi
    out=$(DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o Dpkg::Options::=--force-confdef \
          -o Dpkg::Options::=--force-confold "${missing[@]}" 2>&1 </dev/null)
    rc=$?
    if [ "$rc" -ne 0 ]; then
      nfv_emit packages packages failed "apt-get install ${missing[*]} rc=$rc: $(nfv_tail "$out")"
      return 1
    fi
    nfv_emit packages packages changed "installed: ${missing[*]}"
  fi
  nfv_service_enabled packages lldpd
}

# ---- SNMP ----------------------------------------------------------------

nfv_mask_snmp() {  # stdin -> stdout with community strings and createUser passphrases masked
  sed -E \
    -e 's/^([[:space:]]*(ro|rw)community6?[[:space:]]+)[^[:space:]]+/\1<redacted>/' \
    -e 's/^([[:space:]]*com2sec6?[[:space:]]+([^[:space:]]+[[:space:]]+){2})[^[:space:]]+/\1<redacted>/' \
    -e '/^[[:space:]]*createUser/ s/"[^"]*"/"<redacted>"/g'
}

nfv_hex() {  # hex of a (non-secret) string, net-snmp style (lowercase, no separators)
  printf '%s' "$1" | od -An -tx1 | tr -d ' \n'
}

nfv_snmp_user_present() {  # NAME — a usmUser (or not-yet-consumed createUser) line for NAME
  local name=$1 esc=${1//./\\.}
  [ -f "$NFV_SNMP_PERSIST" ] || return 1
  grep -Eq "^(usmUser [^ ]+ [^ ]+ [^ ]+ (\"$esc\"|0x$(nfv_hex "$name")) |createUser $esc )" "$NFV_SNMP_PERSIST"
}

nfv_snmp_user_purge() {  # NAME — drop its usmUser/createUser lines (snmpd must be stopped)
  local name=$1 esc=${1//./\\.}
  [ -f "$NFV_SNMP_PERSIST" ] || return 0
  sed -i -E "/^usmUser [^ ]+ [^ ]+ [^ ]+ (\"$esc\"|0x$(nfv_hex "$name")) /d; /^createUser $esc /d" "$NFV_SNMP_PERSIST"
}

nfv_step_snmp() {  # inputs: SNMPD_CONF_CONTENT; arrays V3_NAMES V3_AUTH_PROTO V3_AUTH_PASS V3_PRIV_PROTO V3_PRIV_PASS V3_FP
  local restart=0 i name f stored diff_b64 out rc
  local -a need=() drop=()
  if [ -f "$NFV_SNMPD_CONF" ] && cmp -s "$NFV_SNMPD_CONF" <(printf '%s' "$SNMPD_CONF_CONTENT"); then
    nfv_emit snmp snmpd.conf ok "in sync with the SoT"
  else
    diff_b64=$(diff -u --label "$NFV_SNMPD_CONF (node)" --label "SoT render" \
      <(if [ -f "$NFV_SNMPD_CONF" ]; then nfv_mask_snmp < "$NFV_SNMPD_CONF"; fi) \
      <(printf '%s' "$SNMPD_CONF_CONTENT" | nfv_mask_snmp) | base64 -w0)
    if nfv_dry; then
      nfv_emit snmp snmpd.conf would_change "would rewrite it from the SoT (secrets masked in the diff)" "\"diff_b64\":\"$diff_b64\""
    else
      [ -f "$NFV_SNMPD_CONF" ] && cp -a -- "$NFV_SNMPD_CONF" "$NFV_SNMPD_CONF.nfv-baseline.bak"
      if ! nfv_write_file "$NFV_SNMPD_CONF" 0600 SNMPD_CONF_CONTENT; then
        nfv_emit snmp snmpd.conf failed "could not write $NFV_SNMPD_CONF"
        return 1
      fi
      restart=1
      nfv_emit snmp snmpd.conf changed "rewritten from the SoT, mode 0600 (previous copy: $NFV_SNMPD_CONF.nfv-baseline.bak)" "\"diff_b64\":\"$diff_b64\""
    fi
  fi

  for i in "${!V3_NAMES[@]}"; do
    name=${V3_NAMES[i]}
    stored=$(cat -- "$NFV_STATE_DIR/snmpv3/$name.fp" 2>/dev/null)
    if nfv_snmp_user_present "$name" && [ "$stored" = "${V3_FP[i]}" ]; then
      nfv_emit snmp "v3 user $name" ok "present, credentials unchanged"
    else
      need+=("$i")
    fi
  done
  for f in "$NFV_STATE_DIR"/snmpv3/*.fp; do
    [ -e "$f" ] || continue
    name=$(basename -- "$f" .fp)
    nfv_in_list "$name" "${V3_NAMES[@]}" || drop+=("$name")
  done
  if [ "${#need[@]}" -gt 0 ] || [ "${#drop[@]}" -gt 0 ]; then
    if nfv_dry; then
      for i in "${need[@]}"; do
        nfv_emit snmp "v3 user ${V3_NAMES[i]}" would_change "would (re)create it with snmpd stopped (missing, or its Secrets changed)"
      done
      for name in "${drop[@]}"; do
        nfv_emit snmp "v3 user $name" would_change "would remove it (no longer in the SoT)"
      done
    else
      out=$(systemctl stop snmpd 2>&1 </dev/null)
      rc=$?
      if [ "$rc" -ne 0 ]; then
        nfv_emit snmp snmpd failed "systemctl stop snmpd rc=$rc (SNMPv3 users are created with snmpd stopped): $(nfv_tail "$out")"
        return 1
      fi
      mkdir -p -m 0700 -- "$NFV_STATE_DIR/snmpv3"
      if [ ! -f "$NFV_SNMP_PERSIST" ]; then
        mkdir -p -- "$(dirname -- "$NFV_SNMP_PERSIST")"
        ( umask 077; : > "$NFV_SNMP_PERSIST" )
        id Debian-snmp >/dev/null 2>&1 && chown Debian-snmp:Debian-snmp -- "$NFV_SNMP_PERSIST"
      fi
      for name in "${drop[@]}"; do
        nfv_snmp_user_purge "$name"
        rm -f -- "$NFV_STATE_DIR/snmpv3/$name.fp"
        nfv_emit snmp "v3 user $name" changed "removed (no longer in the SoT)"
      done
      for i in "${need[@]}"; do
        name=${V3_NAMES[i]}
        nfv_snmp_user_purge "$name"
        if ! printf 'createUser %s %s "%s" %s "%s"\n' "$name" "${V3_AUTH_PROTO[i]}" "${V3_AUTH_PASS[i]}" \
               "${V3_PRIV_PROTO[i]}" "${V3_PRIV_PASS[i]}" >> "$NFV_SNMP_PERSIST"; then
          nfv_emit snmp "v3 user $name" failed "could not append createUser to $NFV_SNMP_PERSIST"
          return 1
        fi
        ( umask 077; printf '%s\n' "${V3_FP[i]}" > "$NFV_STATE_DIR/snmpv3/$name.fp" )
        nfv_emit snmp "v3 user $name" changed "(re)created: createUser ${V3_AUTH_PROTO[i]}/${V3_PRIV_PROTO[i]} in $NFV_SNMP_PERSIST with snmpd stopped (passphrases not shown)"
      done
      restart=1
    fi
  fi

  if nfv_dry; then
    if systemctl is-enabled --quiet snmpd 2>/dev/null && systemctl is-active --quiet snmpd 2>/dev/null; then
      nfv_emit snmp snmpd ok "enabled and active"
    else
      nfv_emit snmp snmpd would_change "would enable and (re)start snmpd"
    fi
    return 0
  fi
  systemctl enable snmpd >/dev/null 2>&1 || nfv_emit snmp snmpd warning "systemctl enable snmpd failed"
  if [ "$restart" = 1 ] || ! systemctl is-active --quiet snmpd 2>/dev/null; then
    out=$(systemctl restart snmpd 2>&1 </dev/null)
    rc=$?
    if [ "$rc" -ne 0 ]; then
      nfv_emit snmp snmpd failed "systemctl restart snmpd rc=$rc: $(nfv_tail "$out") — journalctl -u snmpd"
      return 1
    fi
  fi
  sleep 1
  if systemctl is-active --quiet snmpd; then
    nfv_emit snmp snmpd "$([ "$restart" = 1 ] && echo changed || echo ok)" "enabled and active$([ "$restart" = 1 ] && echo ' (restarted)')"
    return 0
  fi
  nfv_emit snmp snmpd failed "snmpd is not active after the restart — journalctl -u snmpd"
  return 1
}

# ---- AD realm ------------------------------------------------------------

nfv_realm_password() {  # STEP REALM — the bind password from the payload local REALM_BIND_PASSWORD
  local step=$1 realm=$2 file tmp
  file="$NFV_REALM_PW_DIR/$realm.pw"
  if [ -z "${REALM_BIND_PASSWORD:-}" ]; then
    nfv_emit "$step" "bind password" failed "the payload carries no bind password"
    return 1
  fi
  if [ -f "$file" ] && [ "$(< "$file")" = "$REALM_BIND_PASSWORD" ]; then
    nfv_emit "$step" "bind password" ok "PVE's credential file for $realm holds the SoT value"
    return 0
  fi
  if nfv_dry; then
    nfv_emit "$step" "bind password" would_change "would write PVE's credential file for $realm (value not shown)"
    return 0
  fi
  tmp="$file.tmp.$$"
  if mkdir -p -- "$NFV_REALM_PW_DIR" && ( umask 077; printf '%s' "$REALM_BIND_PASSWORD" > "$tmp" ) \
     && mv -f -- "$tmp" "$file"; then
    NFV_REALM_PW_CHANGED=1
    nfv_emit "$step" "bind password" changed "written to $file — where pveum --password would store it (value not shown, never on an argv)"
    return 0
  fi
  rm -f -- "$tmp"
  nfv_emit "$step" "bind password" failed "could not write $file"
  return 1
}

nfv_pve_group_exists() {  # GROUP
  pveum group list --output-format json 2>/dev/null </dev/null | nfv_json_match groupid "$1"
}

nfv_realm_sync() {  # STEP FORCE(0|1) ADMIN_GROUP argv... — the initial sync; failure is a warning
  local step=$1 force=$2 group=$3 out rc reason=""
  shift 3
  if [ "$force" = 1 ]; then
    reason="realm created or changed"
  elif [ "${NFV_REALM_PW_CHANGED:-0}" = 1 ]; then
    reason="bind password changed"
  elif [ -n "$group" ] && ! nfv_pve_group_exists "$group"; then
    reason="admin group $group not synced yet"
  fi
  if [ -z "$reason" ]; then
    nfv_emit "$step" "realm sync" ok "not needed (realm unchanged, admin group present — the scheduled sync job keeps it current)"
    return 0
  fi
  if nfv_dry; then
    nfv_emit "$step" "realm sync" would_change "would attempt: $* ($reason)"
    return 0
  fi
  out=$("$@" 2>&1 </dev/null)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    nfv_emit "$step" "realm sync" changed "synced ($reason)"
    return 0
  fi
  nfv_emit "$step" "realm sync" warning "initial realm sync failed (rc=$rc: $(nfv_tail "$out")) — AD unreachable or bind credentials wrong; re-run once AD answers"
  return 0
}

nfv_realm_probe() {  # STEP argv... — dry run only: PVE's own sync --dry-run (reads AD, writes nothing)
  local step=$1 out rc
  shift
  out=$("$@" 2>&1 </dev/null)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    nfv_emit "$step" "realm probe" ok "AD answers with the stored bind credentials (sync --dry-run)"
  else
    nfv_emit "$step" "realm probe" warning "sync --dry-run failed (rc=$rc: $(nfv_tail "$out")) — AD unreachable or the stored bind password is stale"
  fi
  return 0
}

nfv_group_acl() {  # STEP PATH GROUP ROLE — admin-group ACL, only once the group exists
  local step=$1 path=$2 group=$3 role=$4
  if pveum acl list --output-format json 2>/dev/null </dev/null \
       | nfv_json_match path "$path" type group ugid "$group" roleid "$role"; then
    nfv_emit "$step" "acl $path $group" ok "$group holds $role on $path"
    return 0
  fi
  if ! nfv_pve_group_exists "$group"; then
    if nfv_dry; then
      nfv_emit "$step" "acl $path $group" would_change "would grant $role on $path to $group once the group exists (needs a realm sync)"
    else
      nfv_emit "$step" "acl $path $group" warning "group $group does not exist on the node (realm sync failed, or the group filter excludes it) — $role on $path NOT granted; re-run once the group has synced"
    fi
    return 0
  fi
  nfv_run "$step" "acl $path $group" pveum acl modify "$path" --groups "$group" --roles "$role"
}

# ---- service-account tokens ------------------------------------------------

nfv_token() {  # STEP ACCOUNT USER TOKENID PRIVSEP(0|1) MODE(create|rotate) REASON
  local step=$1 account=$2 user=$3 tokenid=$4 privsep=$5 mode=$6 reason=$7 out rc value errf
  local item="token $user!$tokenid"
  if nfv_dry; then
    nfv_emit "$step" "$item" would_change "would $mode the token ($reason)"
    return 0
  fi
  if [ "$mode" = rotate ]; then
    out=$(pveum user token remove "$user" "$tokenid" 2>&1 </dev/null)
    rc=$?
    if [ "$rc" -ne 0 ]; then
      nfv_emit "$step" "$item" failed "token remove rc=$rc: $(nfv_tail "$out")"
      return 1
    fi
  fi
  mkdir -p -m 0700 -- "$NFV_RUN_DIR"
  errf=$(mktemp -p "$NFV_RUN_DIR")
  out=$(pveum user token add "$user" "$tokenid" --privsep "$privsep" --comment "managed by the Nautobot Host Baseline job" \
        --output-format json 2>"$errf" </dev/null)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    nfv_emit "$step" "$item" failed "token add rc=$rc: $(nfv_tail "$(cat -- "$errf")") (a re-run rotates a token left without stored Secrets)"
    rm -f -- "$errf"
    return 1
  fi
  rm -f -- "$errf"
  value=$(printf '%s' "$out" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("value") or "")' 2>/dev/null)
  out=""
  if [ -z "$value" ]; then
    nfv_emit "$step" "$item" failed "token add printed no value — the token exists without a captured secret; re-run to rotate it"
    return 1
  fi
  printf '%s{"step":%s,"item":%s,"status":"changed","event":"token","account":%s,"tokenid":%s,"detail":%s,"value":%s}\n' \
    "${NFV_PREFIX:-@@NFV@@ }" "$(nfv_json "$step")" "$(nfv_json "$item")" "$(nfv_json "$account")" \
    "$(nfv_json "$user!$tokenid")" "$(nfv_json "token ${mode}d ($reason); value handed to the job")" "$(nfv_json "$value")"
  value=""
}

# ---- network (applied LAST, under a rollback timer) -------------------------

nfv_step_network_apply() {  # inputs: NEW_IFACES ROLLBACK_SECONDS NET_TS
  local cur=$NFV_IFACES new="$NFV_IFACES.new" bak="$NFV_IFACES.nfv-baseline.$NET_TS"
  local unit="nfv-baseline-net-rollback-$NET_TS" marker="$NFV_RUN_DIR/net-rollback-$NET_TS.done"
  local applyunit="nfv-baseline-net-apply-$NET_TS" out rc
  if nfv_dry; then
    nfv_emit network apply skipped "dry run — nothing staged"
    return 0
  fi
  [ -e "$new" ] && nfv_emit network pending warning "discarding pending PVE GUI network changes staged in $new"
  if ! nfv_write_file "$new" 0644 NEW_IFACES; then
    nfv_emit network apply failed "could not stage $new"
    return 1
  fi
  if cmp -s "$new" "$cur"; then
    rm -f -- "$new"
    nfv_emit network apply ok "$cur already matches the SoT render"
    return 0
  fi
  out=$(ifup -a -s -i "$new" 2>&1 </dev/null)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # ifupdown2 exits 1 for warnings as well as errors. Since 3.3 it flags
    # `bridge-fd 0` ("valid attribute range: 2-255") — the stanza PVE itself
    # writes on every bridge and the one already in the node's file — so only
    # its own "error:" lines reject the render; warning-only output is
    # reported and the apply goes ahead. No recognizable line at all fails closed.
    if printf '%s\n' "$out" | grep -qiE '^error[: ]' || ! printf '%s\n' "$out" | grep -qiE '^warning[: ]'; then
      rm -f -- "$new"
      nfv_emit network syntax failed "ifupdown2 rejected the rendered file (rc=$rc): $(nfv_tail "$out") — nothing applied"
      return 1
    fi
    nfv_emit network syntax warning "ifupdown2 warned about the rendered file (rc=$rc) but reported no error — continuing (ifupdown2 >= 3.3 flags PVE's own bridge-fd 0): $(nfv_tail "$out")"
  fi
  if ! cp -a -- "$cur" "$bak"; then
    rm -f -- "$new"
    nfv_emit network apply failed "could not back up $cur — nothing applied"
    return 1
  fi
  mkdir -p -m 0700 -- "$NFV_RUN_DIR"
  out=$(systemd-run --unit="$unit" --description="NFV Host Baseline: restore $cur unless the job confirms" \
        --on-active="${ROLLBACK_SECONDS}s" /bin/sh -c "cp -a '$bak' '$cur' && ifreload -a; touch '$marker'" 2>&1 </dev/null)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    rm -f -- "$new"
    nfv_emit network rollback-timer failed "could not arm the rollback timer (rc=$rc: $(nfv_tail "$out")) — nothing applied"
    return 1
  fi
  nfv_emit network rollback-timer info "armed $unit.timer: restores $bak in ${ROLLBACK_SECONDS}s unless the job cancels it" \
    "\"unit\":$(nfv_json "$unit"),\"backup\":$(nfv_json "$bak"),\"marker\":$(nfv_json "$marker")"
  if ! mv -f -- "$new" "$cur"; then
    systemctl stop "$unit.timer" >/dev/null 2>&1
    nfv_emit network apply failed "could not move $new into place — nothing applied, timer cancelled"
    return 1
  fi
  out=$(systemd-run --unit="$applyunit" --description="NFV Host Baseline: ifreload -a" --wait --collect --quiet \
        /bin/sh -c 'ifreload -a' 2>&1 </dev/null)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    cp -a -- "$bak" "$cur"
    ifreload -a >/dev/null 2>&1 </dev/null
    systemctl stop "$unit.timer" >/dev/null 2>&1
    nfv_emit network apply failed "ifreload -a failed (rc=$rc; journalctl -u $applyunit) — restored $bak, reloaded, timer cancelled"
    return 1
  fi
  nfv_emit network apply changed "ifreload -a applied the SoT render (previous file: $bak); the job must reconnect and cancel $unit.timer"
}

nfv_step_network_confirm() {  # inputs: NET_TS (the apply's) — from a NEW session on the management IP
  local unit="nfv-baseline-net-rollback-$NET_TS" marker="$NFV_RUN_DIR/net-rollback-$NET_TS.done"
  if [ -e "$marker" ]; then
    nfv_emit network confirm failed "the rollback already ran ($marker) — the previous configuration is back"
    return 1
  fi
  systemctl stop "$unit.timer" >/dev/null 2>&1 </dev/null
  if [ -e "$marker" ] || systemctl is-active --quiet "$unit.service" 2>/dev/null; then
    nfv_emit network confirm failed "the rollback fired while confirming — the previous configuration is (being) restored"
    return 1
  fi
  if systemctl is-active --quiet "$unit.timer" 2>/dev/null; then
    nfv_emit network confirm failed "could not stop $unit.timer — it will restore the previous file"
    return 1
  fi
  nfv_emit network confirm changed "reconnected on the management IP; $unit.timer cancelled"
}

nfv_step_network_state() {  # read-only: the applied file, /proc/net/bonding and the links now
  nfv_raw net.interfaces cat "$NFV_IFACES"
  nfv_raw net.bonding sh -c 'for f in "$1"/*; do [ -e "$f" ] || continue; echo "=== ${f##*/}"; cat "$f"; done; true' sh "$NFV_BONDING"
  nfv_raw net.links ip -o link show
  nfv_raw net.addr ip -o -4 addr show
}
