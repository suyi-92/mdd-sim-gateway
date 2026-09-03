#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Stream-safe entry point for the VMware edition. Run this as an ordinary user; it downloads the
# reviewed checkout before invoking its root installer or transactional update manager locally.
set -Eeuo pipefail

readonly MDD_REPOSITORY_URL="https://github.com/suyi-92/mdd-sim-gateway.git"
readonly MDD_DEFAULT_REF="vmware"

say() { printf '==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*" >&2; }
die() { printf 'xx  %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
MDD Sim Gateway VMware bootstrap

Usage:
  bootstrap.sh install [options]
  bootstrap.sh update [--no-cache] [--dry-run] [--yes]
  bootstrap.sh doctor [--json]

Install options:
  --install-dir PATH       managed Git checkout (default /opt/mdd-sim-gateway)
  --data-dir PATH          runtime data (default /var/lib/mdd-sim-gateway)
  --ref vmware|COMMIT      vmware branch or an exact 40-character commit
  --require-scr-prime      fail unless SCR Prime 04d9:c001 passes USB and PC/SC gates
  --require-cellular       fail unless a cellular modem is visible to ModemManager
  --configure-firewall     add only the documented MDD UFW/nftables rules
  --no-start               install and build without starting MDD services
  --dry-run                show checks and intended actions without changing the machine
  --yes                    accept non-hardware confirmations

Update options:
  --no-cache               force a clean Engine build
  --dry-run                download the latest manager, then preflight without fetching/switching the managed checkout
  --yes                    accept confirmations

Doctor options:
  --json                   emit redacted machine-readable output
EOF
}

validate_absolute_path() {
  local value=$1 label=$2
  [[ "$value" == /* ]] || die "$label must be an absolute path"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || die "$label contains a newline"
  [[ "$value" != "/" ]] || die "$label may not be /"
}

[[ ${EUID:-$(id -u)} -ne 0 ]] || die "run this bootstrap as an ordinary user, not as root"

action=${1:-}
[[ -n "$action" ]] || { usage; exit 2; }
case "$action" in install|update|doctor) shift ;; -h|--help|help) usage; exit 0 ;; *) die "unknown action: $action" ;; esac

install_dir=/opt/mdd-sim-gateway
data_dir=/var/lib/mdd-sim-gateway
ref=$MDD_DEFAULT_REF
require_scr_prime=0
require_cellular=0
configure_firewall=0
no_start=0
dry_run=0
assume_yes=0
no_cache=0
json=0

while (($#)); do
  case "$1" in
    --install-dir) (($# >= 2)) || die "--install-dir requires PATH"; install_dir=$2; shift 2 ;;
    --data-dir) (($# >= 2)) || die "--data-dir requires PATH"; data_dir=$2; shift 2 ;;
    --ref) (($# >= 2)) || die "--ref requires vmware or an exact commit"; ref=$2; shift 2 ;;
    --require-scr-prime) require_scr_prime=1; shift ;;
    --require-cellular) require_cellular=1; shift ;;
    --configure-firewall) configure_firewall=1; shift ;;
    --no-start) no_start=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    --yes|-y) assume_yes=1; shift ;;
    --no-cache) no_cache=1; shift ;;
    --json) json=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

validate_absolute_path "$install_dir" "--install-dir"
validate_absolute_path "$data_dir" "--data-dir"
[[ "$ref" == vmware || "$ref" =~ ^[0-9a-fA-F]{40}$ ]] || \
  die "--ref must be vmware or an exact 40-character commit"

if [[ "$action" != install ]]; then
  [[ "$install_dir" == /opt/mdd-sim-gateway && "$data_dir" == /var/lib/mdd-sim-gateway && \
     "$ref" == vmware && $require_scr_prime -eq 0 && $require_cellular -eq 0 && \
     $configure_firewall -eq 0 && $no_start -eq 0 ]] || \
    die "install-only options were supplied to $action"
fi
if [[ "$action" != update && $no_cache -eq 1 ]]; then die "--no-cache is valid only for update"; fi
if [[ "$action" != doctor && $json -eq 1 ]]; then die "--json is valid only for doctor"; fi

if ((dry_run)) && [[ "$action" == install ]]; then
  say "dry-run: action=$action install_dir=$install_dir data_dir=$data_dir ref=$ref"
  printf '    require_scr_prime=%s require_cellular=%s configure_firewall=%s no_start=%s\n' \
    "$require_scr_prime" "$require_cellular" "$configure_firewall" "$no_start"
  exit 0
fi

command -v sudo >/dev/null 2>&1 || die "sudo is required"
say "confirming administrator access once"
# A host may deliberately have both the distribution's PASSWD sudo-group rule and a later
# NOPASSWD provisioning rule. A command is passwordless there, while bare `sudo -v` can still
# prompt because it has no command to select the NOPASSWD tag. Exercise the real command policy
# non-interactively first; only ask for a password when that policy genuinely requires one.
sudo -n true 2>/dev/null || sudo -v || die "sudo authorization failed"

if [[ "$action" == doctor ]]; then
  command -v mddctl >/dev/null 2>&1 || die "mddctl is not installed; run install first"
  args=(doctor)
  ((json)) && args+=(--json)
  ((dry_run)) && args+=(--dry-run)
  exec sudo mddctl "${args[@]}"
fi

# A minimal image may not include Git.  Install only the downloader prerequisites after the one
# sudo confirmation; the repository itself is still cloned by the unprivileged caller.
if ! command -v git >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || die "git is missing and apt-get is unavailable"
  say "installing Git prerequisites"
  sudo env DEBIAN_FRONTEND=noninteractive apt-get update -qq
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y git ca-certificates
fi

stage=$(mktemp -d "${TMPDIR:-/tmp}/mdd-bootstrap.XXXXXX")
cleanup() { rm -rf -- "$stage"; }
trap cleanup EXIT HUP INT TERM

say "downloading the vmware source branch as the current user"
# install.sh and the downloaded update manager use this checkout as reviewed local source.
# Keep every object reachable from vmware locally: upload-pack deliberately cannot lazy-fetch a
# missing promisor object while it is serving another repository.
git -c advice.detachedHead=false clone --single-branch --branch vmware \
  "$MDD_REPOSITORY_URL" "$stage/repository"
actual_remote=$(git -C "$stage/repository" remote get-url origin)
[[ "$actual_remote" == "$MDD_REPOSITORY_URL" ]] || die "unexpected repository remote: $actual_remote"
[[ $(git -C "$stage/repository" branch --show-current) == vmware ]] || \
  die "downloaded source is not on vmware"
downloaded_head=$(git -C "$stage/repository" rev-parse --verify 'HEAD^{commit}')
[[ "$downloaded_head" =~ ^[0-9a-f]{40}$ && \
   $(git -C "$stage/repository" rev-parse --verify origin/vmware) == "$downloaded_head" ]] || \
  die "downloaded vmware source identity is inconsistent"
[[ $(git -C "$stage/repository" config --bool --get remote.origin.promisor 2>/dev/null || true) != true ]] || \
  die "downloaded source is a partial clone"
git -C "$stage/repository" fsck --full --no-dangling >/dev/null

if [[ "$ref" != vmware ]]; then
  git -C "$stage/repository" fetch --no-tags origin "$ref"
  resolved=$(git -C "$stage/repository" rev-parse --verify "${ref}^{commit}")
  [[ "$resolved" == "${ref,,}" ]] || die "requested commit did not resolve exactly"
  git -C "$stage/repository" switch -C vmware "$resolved"
fi

git -C "$stage/repository" diff --quiet
git -C "$stage/repository" diff --cached --quiet
[[ -z $(git -C "$stage/repository" status --porcelain=v1 --untracked-files=normal) ]] || \
  die "downloaded source checkout is not clean"
[[ -f "$stage/repository/install.sh" && ! -L "$stage/repository/install.sh" ]] || \
  die "downloaded source does not contain install.sh"
[[ -f "$stage/repository/scripts/mddctl" && ! -L "$stage/repository/scripts/mddctl" ]] || \
  die "downloaded source does not contain a regular scripts/mddctl"

if [[ "$action" == update ]]; then
  args=(update)
  ((no_cache)) && args+=(--no-cache)
  ((assume_yes)) && args+=(--yes)
  ((dry_run)) && args+=(--dry-run)
  say "starting the transactional update with the downloaded management source"
  sudo -H bash "$stage/repository/scripts/mddctl" "${args[@]}"
  exit 0
fi

args=(install --source "$stage/repository" --install-dir "$install_dir" --data-dir "$data_dir" --ref "$ref")
((require_scr_prime)) && args+=(--require-scr-prime)
((require_cellular)) && args+=(--require-cellular)
((configure_firewall)) && args+=(--configure-firewall)
((no_start)) && args+=(--no-start)
((assume_yes)) && args+=(--yes)

say "starting the local source build"
sudo -H bash "$stage/repository/install.sh" "${args[@]}"
