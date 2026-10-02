#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Shared, bounded ownership of the secondary Quectel AT port used by MMS.
MMS_AT_PORT_RULE="${MDD_UDEV_RULES_DIR:-/etc/udev/rules.d}/78-mdd-mms-at-port.rules"

# Do not replace or delete an unrelated file placed at the managed pathname.
mms_at_port_rule_owned() {
  local pending="${MMS_AT_PORT_RULE}.pending"
  [ ! -L "$pending" ] || return 1
  if [ -e "$pending" ]; then
    [ -f "$pending" ] && grep -Fxq '# MDD Sim Gateway: MMS AT-port apply pending' "$pending" || return 1
  fi
  [ ! -L "$MMS_AT_PORT_RULE" ] || return 1
  [ ! -e "$MMS_AT_PORT_RULE" ] && return 0
  [ -f "$MMS_AT_PORT_RULE" ] || return 1
  grep -q '^# MDD Sim Gateway:' "$MMS_AT_PORT_RULE"
}

# A first installation has no outer update rollback. Persist application intent before
# publishing/removing the rule, so an interrupted/failed run cannot make a retry a false no-op.
mark_mms_at_port_rule_pending() {
  local pending="${MMS_AT_PORT_RULE}.pending" temporary
  temporary=$(mktemp "$pending.tmp.XXXXXX") || return 1
  if ! printf '%s\n' '# MDD Sim Gateway: MMS AT-port apply pending' > "$temporary"; then
    rm -f "$temporary"
    return 1
  fi
  mv -fT -- "$temporary" "$pending" || { rm -f "$temporary"; return 1; }
}

# Re-evaluate tty udev properties and let ModemManager re-probe with them. Both are needed: a
# changed rules file only reaches the udev database on the next event for the device, and
# ModemManager reads ID_MM_PORT_IGNORE when it probes a port.
reapply_modem_port_rules() {
  if have udevadm; then
    udevadm control --reload-rules 2>/dev/null || {
      warn "could not reload MMS AT-port udev rules"
      return 1
    }
    udevadm trigger --action=change --subsystem-match=tty 2>/dev/null || {
      warn "could not reapply MMS AT-port properties to tty devices"
      return 1
    }
    # Do not restart ModemManager until the triggered events have completed. A bounded
    # timeout cannot prove the new properties are active, so let the caller roll back.
    udevadm settle --timeout=10 2>/dev/null || {
      warn "MMS AT-port udev events did not settle within 10 seconds"
      return 1
    }
  fi
  if have systemctl && systemctl is-active ModemManager.service >/dev/null 2>&1; then
    systemctl restart ModemManager.service || return 1
  fi
}

ensure_mms_at_port_rule() {
  [ -d "$(dirname "$MMS_AT_PORT_RULE")" ] || return 0
  mms_at_port_rule_owned || { warn "refusing to modify an unowned MMS rule"; return 1; }
  local rule_file temporary had_pending=0 changed=0
  [ ! -f "${MMS_AT_PORT_RULE}.pending" ] || had_pending=1
  rule_file=$MMS_AT_PORT_RULE
  temporary=$(mktemp "$rule_file.tmp.XXXXXX") || return 1
  if ! cat >"$temporary" <<'RULE'
# MDD Sim Gateway: let the gateway own a Quectel module's secondary AT port for MMS uploads.
# ModemManager keeps the primary AT port and QMI; a module with a single AT port is unaffected.
ACTION!="remove", SUBSYSTEM=="tty", ATTRS{idVendor}=="2c7c", ENV{ID_MM_PORT_TYPE_AT_SECONDARY}=="1", ENV{ID_MM_PORT_IGNORE}="1"
RULE
  then
    warn "could not write the staged MMS AT-port rule"
    rm -f "$temporary"
    return 1
  fi
  if [ ! -f "$rule_file" ] || ! cmp -s "$temporary" "$rule_file"; then
    # Stage in the destination directory and rename only a complete, mode-checked rule.
    # A disk/permission failure must not truncate the current rule before rollback.
    chmod 0644 "$temporary" || { rm -f "$temporary"; return 1; }
    mark_mms_at_port_rule_pending || { rm -f "$temporary"; return 1; }
    if ! mv -fT -- "$temporary" "$rule_file"; then
      rm -f "$temporary"
      if [ "$had_pending" -eq 0 ]; then rm -f "${MMS_AT_PORT_RULE}.pending"; fi
      return 1
    fi
    changed=1
  fi
  if [ "$changed" -eq 1 ] || [ -f "${MMS_AT_PORT_RULE}.pending" ]; then
    info "releasing the modem's secondary AT port for MMS (ModemManager restarts)…"
    reapply_modem_port_rules || { rm -f "$temporary"; return 1; }
    rm -f "${MMS_AT_PORT_RULE}.pending" || { rm -f "$temporary"; return 1; }
  fi
  rm -f "$temporary"
  return 0
}

# Uninstall: give the port back. Removing the file alone would leave ID_MM_PORT_IGNORE in the
# udev database, and the port ignored, until the next reboot.
remove_mms_at_port_rule() {
  mms_at_port_rule_owned || { warn "refusing to modify an unowned MMS rule"; return 1; }
  if [ ! -f "$MMS_AT_PORT_RULE" ] && [ ! -f "${MMS_AT_PORT_RULE}.pending" ]; then return 0; fi
  mark_mms_at_port_rule_pending || return 1
  rm -f "$MMS_AT_PORT_RULE" || return 1
  info "returning the modem's secondary AT port to ModemManager (ModemManager restarts)…"
  reapply_modem_port_rules || return 1
  rm -f "${MMS_AT_PORT_RULE}.pending"
}

# Called while MDD writers are stopped. Keep the exact previous host rule for rollback.
snapshot_mms_at_port_rule() {
  local target=$1
  mms_at_port_rule_owned || return 1
  rm -f -- "$target" "$target.absent"
  if [[ -f "$MMS_AT_PORT_RULE" ]]; then
    cp -p -- "$MMS_AT_PORT_RULE" "$target"
  else
    : > "$target.absent"
  fi
}

restore_mms_at_port_rule() {
  local target=$1 changed=0
  mms_at_port_rule_owned || return 1
  if [[ -f "$target.absent" && ! -L "$target.absent" ]]; then
    if [[ -f "$MMS_AT_PORT_RULE" ]]; then rm -f -- "$MMS_AT_PORT_RULE" || return 1; changed=1; fi
  elif [[ -f "$target" && ! -L "$target" ]]; then
    if ! cmp -s "$target" "$MMS_AT_PORT_RULE"; then
      cp -p -- "$target" "$MMS_AT_PORT_RULE" || return 1
      changed=1
    fi
  else
    return 1
  fi
  if ((changed)) || [[ -f "${MMS_AT_PORT_RULE}.pending" ]]; then
    reapply_modem_port_rules || return 1
    rm -f "${MMS_AT_PORT_RULE}.pending" || return 1
  fi
  return 0
}
