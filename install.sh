#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# VMware-only installer. Control/WebUI run locally under systemd; Docker is used only for Engine.
set -Eeuo pipefail
umask 077

readonly ORIGIN_URL="https://github.com/suyi-92/mdd-sim-gateway.git"
readonly ENGINE_STABLE_IMAGE="mdd-sim-gateway/engine:latest"
readonly NODE_BUILD_IMAGE="node:22.14.0-bookworm-slim@sha256:745403dc46b5ab4c998502b07a12cbf020cf2c30645427a68ec0718f02d647de"
readonly PCSC_VERSION="2.3.3"
readonly CCID_VERSION="1.6.2"
readonly CCID_SHA256="6d5e6a6884090831ed155ee75cbc03aed252bd8158d94f507a94f05ebaba296c"
readonly VPCD_VERSION="0.8"
readonly VPCD_SHA256="b428c399d5f014a350db0e8e5947ce69392429cc1aebdf3830af3c7f8078b18f"
readonly VPCD_SLOTS="4"
readonly SINGBOX_VERSION="1.13.15"
readonly SINGBOX_SHA256_AMD64="a3a3ff223b23c3f4731d0a17cb0ef94c97ce257c70721a5b07dc7ca079203c9f"
readonly XRAY_VERSION="26.3.27"
readonly XRAY_SHA256_AMD64="23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae"
readonly LPAC_VERSION="2.3.0"
readonly LPAC_COMMIT="c2fcf5e4b21c712d54e35a11da2ad9ad134fb821"
readonly CMAKE_VERSION="3.31.12"
readonly CMAKE_SHA256_AMD64="0dc2e9a6860f06bf10bd8fadc03e35d9eeb4df46e33763a7e480e987758f385c"

info() { printf '==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*" >&2; }
die() { printf 'xx  %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
  cat <<'EOF'
Internal VMware installer. Use bootstrap.sh for normal installation.

  install.sh install --source PATH [--install-dir PATH] [--data-dir PATH]
                     [--ref vmware|COMMIT] [--require-scr-prime]
                     [--require-cellular] [--configure-firewall]
                     [--no-start] [--yes]

Internal commands used by mddctl:
  install.sh prepare  --source PATH --build-root PATH [--no-cache]
  install.sh verify   --source PATH --build-root PATH --sha COMMIT
  install.sh activate --source PATH --build-root PATH --sha COMMIT
  install.sh health   --source PATH
  install.sh driver   --source PATH
EOF
}

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "this installer must be invoked through sudo"

action=${1:-}
[[ -n "$action" ]] || { usage; exit 2; }
shift
case "$action" in install|prepare|verify|activate|health|driver) ;; -h|--help|help) usage; exit 0 ;; *) die "unknown action: $action" ;; esac

source_dir=""
install_dir=/opt/mdd-sim-gateway
data_dir=/var/lib/mdd-sim-gateway
backup_dir=/var/backups/mdd-sim-gateway
state_dir=/etc/mdd-sim-gateway
cache_dir=/var/cache/mdd-sim-gateway
build_root=""
sha=""
ref=vmware
require_scr_prime=0
require_cellular=0
configure_firewall=0
no_start=0
assume_yes=0
no_cache=0

while (($#)); do
  case "$1" in
    --source) (($# >= 2)) || die "--source requires PATH"; source_dir=$2; shift 2 ;;
    --install-dir) (($# >= 2)) || die "--install-dir requires PATH"; install_dir=$2; shift 2 ;;
    --data-dir) (($# >= 2)) || die "--data-dir requires PATH"; data_dir=$2; shift 2 ;;
    --build-root) (($# >= 2)) || die "--build-root requires PATH"; build_root=$2; shift 2 ;;
    --sha) (($# >= 2)) || die "--sha requires COMMIT"; sha=$2; shift 2 ;;
    --ref) (($# >= 2)) || die "--ref requires a value"; ref=$2; shift 2 ;;
    --require-scr-prime) require_scr_prime=1; shift ;;
    --require-cellular) require_cellular=1; shift ;;
    --configure-firewall) configure_firewall=1; shift ;;
    --no-start) no_start=1; shift ;;
    --yes|-y) assume_yes=1; shift ;;
    --no-cache) no_cache=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

validate_path() {
  local value=$1 label=$2
  [[ "$value" == /* && "$value" != / ]] || die "$label must be an absolute non-root path"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* && "$value" != *[[:space:]]* ]] || \
    die "$label may not contain whitespace"
}
validate_path "$install_dir" "install directory"
validate_path "$data_dir" "data directory"
[[ -z "$source_dir" ]] || validate_path "$source_dir" "source directory"
[[ -z "$build_root" ]] || validate_path "$build_root" "build root"
[[ -z "$sha" || "$sha" =~ ^[0-9a-f]{40}$ ]] || die "--sha must be an exact lowercase commit id"

sha256_file() { sha256sum "$1" | awk '{print $1}'; }
download_verified() {
  local url=$1 destination=$2 expected=$3 actual
  curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$destination" "$url"
  actual=$(sha256_file "$destination")
  [[ "$actual" == "$expected" ]] || die "checksum mismatch for $url (expected $expected, got $actual)"
}

load_managed_paths() {
  local env_file=/etc/mdd-sim-gateway/managed.env
  [[ -r "$env_file" ]] || return 0
  # The file is root-owned and written by this installer; reject anything except simple values.
  local line key value
  while IFS= read -r line; do
    [[ "$line" =~ ^([A-Z_]+)=([^[:space:]]+)$ ]] || continue
    key=${BASH_REMATCH[1]}; value=${BASH_REMATCH[2]}
    case "$key" in
      INSTALL_DIR) install_dir=$value ;;
      DATA_DIR) data_dir=$value ;;
      BACKUP_DIR) backup_dir=$value ;;
      STATE_DIR) state_dir=$value ;;
      CACHE_DIR) cache_dir=$value ;;
    esac
  done < "$env_file"
}
[[ "$action" == install ]] || load_managed_paths

validate_managed_layout() {
  local label value first second i j
  for label in install_dir data_dir backup_dir state_dir cache_dir; do
    value=${!label}
    validate_path "$value" "$label"
    printf -v "$label" '%s' "$(realpath -m "$value")"
  done
  local paths=("$install_dir" "$data_dir" "$backup_dir" "$state_dir" "$cache_dir")
  for ((i = 0; i < ${#paths[@]}; i++)); do
    for ((j = i + 1; j < ${#paths[@]}; j++)); do
      first=${paths[$i]}; second=${paths[$j]}
      case "$first" in "$second"|"$second"/*) die "managed directories may not overlap: $first and $second" ;; esac
      case "$second" in "$first"|"$first"/*) die "managed directories may not overlap: $first and $second" ;; esac
    done
  done
}
validate_managed_layout

detect_distro() {
  [[ -r /etc/os-release ]] || die "/etc/os-release is missing"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) distro_key="${ID}-${VERSION_ID}" ;;
    *) die "unsupported system: ${PRETTY_NAME:-${ID:-unknown} ${VERSION_ID:-}}; supported: Ubuntu 24.04/26.04 and Debian 12/13" ;;
  esac
}

preflight_host() {
  [[ $(uname -m) == x86_64 ]] || die "only x86_64 guests are supported"
  [[ $(ps -p 1 -o comm= | tr -d ' ') == systemd ]] || die "systemd must be PID 1"
  [[ -c /dev/net/tun ]] || die "/dev/net/tun is unavailable; enable the TUN device before installing"

  local mem_kib free_bytes root_bytes
  mem_kib=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
  ((mem_kib >= 4 * 1024 * 1024)) || die "at least 4 GiB RAM is required"
  ((mem_kib >= 8 * 1024 * 1024)) || warn "less than 8 GiB RAM; the source Engine build may be slow or fail"
  free_bytes=$(df -PB1 / | awk 'NR==2 {print $4}')
  root_bytes=$(df -PB1 / | awk 'NR==2 {print $2}')
  ((free_bytes >= 12 * 1024 * 1024 * 1024)) || die "at least 12 GiB free disk space is required"
  ((free_bytes >= 25 * 1024 * 1024 * 1024)) || warn "less than 25 GiB free disk; 64 GiB dynamic disk is recommended"
  ((root_bytes >= 20 * 1024 * 1024 * 1024)) || die "the root filesystem is smaller than 20 GiB; expand it before installing"

  local virt
  virt=$(systemd-detect-virt 2>/dev/null || true)
  [[ "$virt" == vmware ]] || warn "guest is reported as '${virt:-physical}', not VMware"

  if systemctl is-active --quiet mdd-sim-gateway-control.service 2>/dev/null; then
    : # an idempotent reinstall owns its listener
  elif ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -Eq '(^|:|\])8443$'; then
    die "TCP port 8443 is already in use"
  fi
}

network_value() {
  local kind=$1
  case "$kind" in
    dev) ip -4 route show default | awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="dev") {print $(i+1); exit}}' ;;
    gateway) ip -4 route show default | awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="via") {print $(i+1); exit}}' ;;
    source) ip -4 route get 1.1.1.1 2>/dev/null | awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}' ;;
  esac
}

prepare_network_guard() {
  install -d -m 0700 "$state_dir/network"
  network_dev_before=$(network_value dev)
  network_gateway_before=$(network_value gateway)
  network_source_before=$(network_value source)
  [[ -n "$network_dev_before" && -n "$network_source_before" ]] || die "could not identify the bridged default route and SSH address"
  printf '%s\n' "$network_dev_before" > "$state_dir/network/default-interface"
  printf '%s\n' "$network_gateway_before" > "$state_dir/network/default-gateway"
  printf '%s\n' "$network_source_before" > "$state_dir/network/default-source"
  ip -j address show dev "$network_dev_before" > "$state_dir/network/address-before.json"
  ip -j route show default > "$state_dir/network/routes-before.json"
  networkmanager_was_active=0
  systemctl is-active --quiet NetworkManager.service 2>/dev/null && networkmanager_was_active=1
  networkd_was_active=0
  systemctl is-active --quiet systemd-networkd.service 2>/dev/null && networkd_was_active=1
  networking_was_active=0
  systemctl is-active --quiet networking.service 2>/dev/null && networking_was_active=1
  primary_was_nm_managed=0
  if have nmcli && nmcli -t -f DEVICE,STATE device status 2>/dev/null | grep -Eq "^${network_dev_before}:(connected|connecting)"; then
    primary_was_nm_managed=1
  fi
  printf 'networkmanager_was_active=%s\nnetworkd_was_active=%s\nnetworking_was_active=%s\nprimary_was_nm_managed=%s\n' \
    "$networkmanager_was_active" "$networkd_was_active" "$networking_was_active" \
    "$primary_was_nm_managed" > "$state_dir/network/backend-before"

  # A newly installed NetworkManager must not claim the bridged management NIC. If it already
  # manages that NIC, preserve that arrangement and merely add cellular management.
  install -d -m 0755 /etc/NetworkManager/conf.d
  rm -f "$state_dir/network/policy-existed" "$state_dir/network/90-mdd-cellular-only.conf.before"
  if [[ -f /etc/NetworkManager/conf.d/90-mdd-cellular-only.conf ]]; then
    cp -a /etc/NetworkManager/conf.d/90-mdd-cellular-only.conf \
      "$state_dir/network/90-mdd-cellular-only.conf.before"
    touch "$state_dir/network/policy-existed"
  fi
  if ((primary_was_nm_managed == 0)); then
    cat > /etc/NetworkManager/conf.d/90-mdd-cellular-only.conf <<'EOF'
[device-mdd-cellular]
match-device=type:gsm
managed=1

[device-mdd-noncellular]
match-device=except:type:gsm
managed=0
EOF
  else
    rm -f /etc/NetworkManager/conf.d/90-mdd-cellular-only.conf
  fi
}

restore_network_guard() {
  if [[ -f "$state_dir/network/policy-existed" ]]; then
    cp -a "$state_dir/network/90-mdd-cellular-only.conf.before" \
      /etc/NetworkManager/conf.d/90-mdd-cellular-only.conf || \
      warn "could not restore the previous NetworkManager policy file"
  else
    rm -f /etc/NetworkManager/conf.d/90-mdd-cellular-only.conf
  fi
  if ((networkmanager_was_active)); then systemctl restart NetworkManager.service >/dev/null 2>&1 || true
  else systemctl disable --now NetworkManager.service >/dev/null 2>&1 || true
  fi
  if ((networkd_was_active)); then systemctl restart systemd-networkd.service >/dev/null 2>&1 || true; fi
  if ((networking_was_active)); then systemctl restart networking.service >/dev/null 2>&1 || true; fi
}

verify_network_guard() {
  local dev_after gateway_after source_after
  dev_after=$(network_value dev); gateway_after=$(network_value gateway); source_after=$(network_value source)
  if [[ "$dev_after" != "$network_dev_before" || "$gateway_after" != "$network_gateway_before" || "$source_after" != "$network_source_before" ]]; then
    warn "network changed during package installation; rolling back the MDD NetworkManager policy"
    restore_network_guard
    network_guard_armed=0
    die "default route or SSH address changed (before ${network_source_before}/${network_dev_before}, after ${source_after:-none}/${dev_after:-none})"
  fi
  info "bridged management address preserved: $source_after via $dev_after"
}

install_packages() {
  info "installing ${distro_key} packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y \
    ca-certificates curl wget git jq openssl coreutils util-linux iproute2 usbutils \
    docker.io modemmanager network-manager dbus pcscd pcsc-tools libccid \
    python3 python3-dev python3-venv python3-pip build-essential pkg-config swig \
    libpcsclite-dev libcurl4-openssl-dev libssl-dev libffi-dev \
    autoconf automake libtool help2man flex meson ninja-build patch perl \
    libusb-1.0-0-dev zlib1g-dev unzip cmake nftables
  systemctl enable --now docker.service ModemManager.service NetworkManager.service
  systemctl enable --now pcscd.socket >/dev/null 2>&1 || systemctl start pcscd.service
  docker info >/dev/null
  if docker info --format '{{json .SecurityOptions}}' 2>/dev/null | grep -qi rootless; then
    die "rootless Docker is not supported; install the distro rootful docker.io daemon"
  fi
}

install_modemmanager_dropin() {
  local binary
  binary=$(command -v ModemManager)
  install -d -m 0755 /etc/systemd/system/ModemManager.service.d
  cat > /etc/systemd/system/ModemManager.service.d/90-mdd-command-interface.conf <<EOF
[Service]
ExecStart=
ExecStart=$binary --debug
ExecStartPost=-/usr/bin/busctl call org.freedesktop.ModemManager1 /org/freedesktop/ModemManager1 org.freedesktop.ModemManager1 SetLogging s INFO
EOF
  systemctl daemon-reload
  systemctl restart ModemManager.service
}

git_operation_in_progress_at() {
  local repository=$1 name path
  for name in MERGE_HEAD REBASE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG; do
    path=$(git -C "$repository" rev-parse --path-format=absolute --git-path "$name")
    [[ -e "$path" ]] && return 0
  done
  path=$(git -C "$repository" rev-parse --path-format=absolute --git-path rebase-merge)
  [[ -d "$path" ]] && return 0
  path=$(git -C "$repository" rev-parse --path-format=absolute --git-path rebase-apply)
  [[ -d "$path" ]] && return 0
  return 1
}

install_source_checkout() {
  [[ -n "$source_dir" ]] || die "install requires --source"
  source_dir=$(realpath "$source_dir")
  git -C "$source_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "--source is not a Git checkout"
  [[ $(git -C "$source_dir" remote get-url origin) == "$ORIGIN_URL" ]] || die "source remote does not match $ORIGIN_URL"
  [[ $(git -C "$source_dir" config --bool --get remote.origin.promisor 2>/dev/null || true) != true ]] || \
    die "--source is a partial clone; provide a complete vmware checkout for managed installation"
  [[ -z $(git -C "$source_dir" status --porcelain --untracked-files=normal) ]] || die "source checkout is dirty"
  local source_sha incoming current
  source_sha=$(git -C "$source_dir" rev-parse HEAD)
  [[ "$ref" == vmware || "$ref" =~ ^[0-9a-fA-F]{40}$ ]] || die "invalid --ref"
  if [[ "$ref" == vmware ]]; then
    [[ $(git -C "$source_dir" branch --show-current) == vmware ]] || die "vmware installs require a vmware source branch"
    [[ $(git -C "$source_dir" rev-parse --verify origin/vmware) == "$source_sha" ]] || \
      die "vmware source HEAD does not exactly match origin/vmware"
  else
    [[ "$source_sha" == "${ref,,}" ]] || die "source HEAD does not match the requested exact commit"
  fi

  install -d -m 0755 "$(dirname "$install_dir")"
  if [[ ! -e "$install_dir" ]]; then
    incoming="${install_dir}.incoming.$$"
    [[ "$incoming" == "$(dirname "$install_dir")/"* ]] || die "unsafe incoming checkout path"
    rm -rf -- "$incoming"
    # Clone the reviewed Git tree instead of copying the directory wholesale: ignored .env,
    # venv, build output or runtime data in a manually supplied source checkout must not leak
    # into the root-owned managed installation.
    git clone --no-hardlinks --no-checkout "$source_dir" "$incoming"
    git -C "$incoming" remote set-url origin "$ORIGIN_URL"
    git -C "$incoming" switch -C vmware "$source_sha"
    chown -R root:root "$incoming"
    mv -- "$incoming" "$install_dir"
  else
    git -C "$install_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$install_dir is not a Git checkout"
    [[ $(git -C "$install_dir" remote get-url origin) == "$ORIGIN_URL" ]] || die "managed checkout remote mismatch"
    [[ $(git -C "$install_dir" branch --show-current) == vmware ]] || die "managed checkout is not on vmware"
    git_operation_in_progress_at "$install_dir" && die "a Git operation is in progress in the managed checkout"
    [[ -z $(git -C "$install_dir" status --porcelain=v1 --untracked-files=normal) ]] || \
      die "managed checkout is dirty; use the latest bootstrap update for a managed upgrade"
    current=$(git -C "$install_dir" rev-parse --verify 'HEAD^{commit}')
    [[ "$current" == "$source_sha" ]] || \
      die "install does not update an existing managed checkout; run the latest bootstrap update"
  fi
  source_dir=$install_dir
  sha=$(git -C "$source_dir" rev-parse HEAD)
}

ensure_singbox() {
  if have sing-box && sing-box version 2>/dev/null | grep -q "$SINGBOX_VERSION"; then return; fi
  local temp archive="sing-box-${SINGBOX_VERSION}-linux-amd64.tar.gz"
  temp=$(mktemp -d /tmp/mdd-singbox.XXXXXX)
  download_verified "https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/${archive}" "$temp/$archive" "$SINGBOX_SHA256_AMD64"
  tar xzf "$temp/$archive" -C "$temp"
  install -m 0755 "$temp/sing-box-${SINGBOX_VERSION}-linux-amd64/sing-box" /usr/local/bin/sing-box
  rm -rf -- "$temp"
}

ensure_xray() {
  if have xray && xray version 2>/dev/null | grep -q "$XRAY_VERSION"; then return; fi
  local temp asset=Xray-linux-64.zip
  temp=$(mktemp -d /tmp/mdd-xray.XXXXXX)
  download_verified "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/${asset}" "$temp/$asset" "$XRAY_SHA256_AMD64"
  unzip -q "$temp/$asset" -d "$temp/xray"
  install -m 0755 "$temp/xray/xray" /usr/local/bin/xray
  rm -rf -- "$temp"
}

pcsc_dropdir() {
  local value
  value=$(pkg-config libpcsclite --variable=usbdropdir 2>/dev/null || true)
  printf '%s' "${value:-/usr/lib/pcsc/drivers}"
}

publish_pcsc_maintenance() {
  install -d -m 0700 "$data_dir/orchestrator"
  : > "$data_dir/orchestrator/pcsc-maintenance"
  chmod 0600 "$data_dir/orchestrator/pcsc-maintenance"
}

ensure_vpcd() {
  local drop serial marker temp source
  drop=$(pcsc_dropdir); serial="$drop/serial"; marker="$serial/.mdd-vpcd-slots-$VPCD_SLOTS"
  [[ -f "$marker" ]] && return
  info "building vsmartcard VPCD with $VPCD_SLOTS logical slots"
  temp=$(mktemp -d /tmp/mdd-vpcd.XXXXXX)
  download_verified "https://github.com/frankmorgner/vsmartcard/archive/refs/tags/virtualsmartcard-${VPCD_VERSION}.tar.gz" "$temp/source.tar.gz" "$VPCD_SHA256"
  tar xf "$temp/source.tar.gz" -C "$temp"
  source="$temp/vsmartcard-virtualsmartcard-${VPCD_VERSION}/virtualsmartcard"
  (cd "$source" && autoreconf -vif . >/dev/null && \
    ./configure --enable-serialconfdir=/etc/reader.conf.d --enable-serialdropdir="$serial" \
      --enable-vpcdslots="$VPCD_SLOTS" --disable-dependency-tracking >/dev/null && \
    make -C src/vpcd >/dev/null && make -C src/ifd-vpcd >/dev/null && \
    make -C src/ifd-vpcd install >/dev/null)
  ldconfig
  rm -rf -- "$temp"
  install -d -m 0755 "$serial"
  rm -f "$serial/.mdd-vpcd-slots-"* 2>/dev/null || true
  touch "$marker"
  if [[ -f /etc/reader.conf.d/vpcd ]]; then mv -f /etc/reader.conf.d/vpcd /etc/reader.conf.d/.vpcd.mdd-disabled; fi
  publish_pcsc_maintenance
  systemctl restart pcscd.service
}

ensure_cmake() {
  local version major minor home temp
  version=$(cmake --version 2>/dev/null | awk 'NR==1 {print $3}')
  major=${version%%.*}; minor=${version#*.}; minor=${minor%%.*}
  if [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]] && ((major > 3 || (major == 3 && minor >= 31))); then
    command -v cmake; return
  fi
  home="$cache_dir/tools/cmake-$CMAKE_VERSION"
  if [[ ! -x "$home/bin/cmake" ]]; then
    temp=$(mktemp -d /tmp/mdd-cmake.XXXXXX)
    download_verified "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-linux-x86_64.tar.gz" "$temp/cmake.tgz" "$CMAKE_SHA256_AMD64"
    tar xzf "$temp/cmake.tgz" -C "$temp"
    install -d -m 0755 "$(dirname "$home")"
    mv "$temp/cmake-${CMAKE_VERSION}-linux-x86_64" "$home"
    rm -rf -- "$temp"
  fi
  printf '%s' "$home/bin/cmake"
}

lpac_binary_valid() {
  local binary=$1 drivers
  [[ -x "$binary" ]] || return 1
  drivers=$(LPAC_APDU=stdio LPAC_HTTP=stdio "$binary" driver list 2>/dev/null) || return 1
  grep -Fq '"pcsc"' <<<"$drivers" && grep -Fq '"curl"' <<<"$drivers"
}

ensure_lpac() {
  local destination="$data_dir/lpac" source="$cache_dir/sources/lpac-$LPAC_VERSION" cmake_bin build temp candidate
  lpac_binary_valid "$destination/lpac" && return
  info "building lpac $LPAC_VERSION from pinned source"
  install -d -m 0755 "$(dirname "$source")"
  if [[ ! -d "$source/.git" ]]; then git clone --filter=blob:none --branch "v$LPAC_VERSION" --single-branch https://github.com/estkme-group/lpac.git "$source"; fi
  [[ $(git -C "$source" rev-parse HEAD) == "$LPAC_COMMIT" ]] || die "lpac source commit mismatch"
  for candidate in "$source_dir"/patches/lpac/*.patch; do
    [[ -f "$candidate" ]] || continue
    if patch -p1 -d "$source" -N --dry-run < "$candidate" >/dev/null 2>&1; then patch -p1 -d "$source" -N < "$candidate"; fi
  done
  cmake_bin=$(ensure_cmake); build="$source/build-mdd"; temp=$(mktemp -d /tmp/mdd-lpac.XXXXXX)
  rm -rf -- "$build"
  "$cmake_bin" -S "$source" -B "$build" -DCMAKE_BUILD_TYPE=Release -DSTANDALONE_MODE=ON \
    -DLPAC_WITH_APDU_PCSC=ON -DLPAC_WITH_HTTP_CURL=ON -DLPAC_WITH_APDU_AT=OFF \
    -DLPAC_WITH_APDU_QMI=OFF -DLPAC_WITH_APDU_QMI_QRTR=OFF -DLPAC_WITH_APDU_UQMI=OFF \
    -DLPAC_WITH_APDU_MBIM=OFF -DLPAC_WITH_APDU_GBINDER=OFF
  "$cmake_bin" --build "$build" --parallel "$(nproc)"
  DESTDIR="$temp" "$cmake_bin" --install "$build"
  candidate=$(find "$temp" -type f -name lpac -perm /111 -print -quit)
  [[ -n "$candidate" ]] || die "lpac binary was not produced"
  rm -rf -- "$destination.tmp"
  install -d -m 0700 "$destination.tmp"
  install -m 0755 "$candidate" "$destination.tmp/lpac"
  rm -rf -- "$destination"
  mv "$destination.tmp" "$destination"
  rm -rf -- "$temp"
  lpac_binary_valid "$destination/lpac" || die "lpac binary is missing the required PC/SC or curl driver"
}

pcsc_scan_capture() {
  LC_ALL=C timeout "${1:-10}" pcsc_scan -n 2>&1 || true
}
scr_prime_pcsc_visible() { pcsc_scan_capture 8 | grep -Eiq 'SCR[[:space:]_-]*Prime'; }

validate_scr_prime_reader() {
  local output=$1
  grep -Eiq 'SCR[[:space:]_-]*Prime' <<<"$output" || \
    die "SCR Prime disappeared during PC/SC validation"
  if grep -Eiq 'ATR:[[:space:]]*[0-9A-F]' <<<"$output"; then
    return 0
  fi
  if ((require_scr_prime)); then
    die "insert a SIM in SCR Prime so its ATR can be validated"
  fi
  warn "SCR Prime reader is visible, but no card ATR is available; continuing because hardware acceptance was not required"
}

tree_hash() {
  local root=$1
  python3 - "$root" <<'PY'
import hashlib, os, stat, sys
root = os.path.realpath(sys.argv[1])
if not os.path.isdir(root):
    raise SystemExit(f"tree is missing: {root}")
digest = hashlib.sha256()
items = []
for base, directories, files in os.walk(root, followlinks=False):
    directories.sort(); files.sort()
    items.extend(os.path.join(base, name) for name in directories)
    items.extend(os.path.join(base, name) for name in files)
for path in sorted(items):
    metadata = os.lstat(path)
    relative = os.path.relpath(path, root).replace(os.sep, "/")
    if stat.S_ISLNK(metadata.st_mode):
        kind = "l"; size = metadata.st_size
    elif stat.S_ISDIR(metadata.st_mode):
        kind = "d"; size = 0
    elif stat.S_ISREG(metadata.st_mode):
        kind = "f"; size = metadata.st_size
    else:
        raise SystemExit(f"tree contains an unsupported path: {relative}")
    digest.update(f"{kind}\0{relative}\0{stat.S_IMODE(metadata.st_mode):o}\0{size}\0".encode())
    if kind == "l":
        digest.update(os.fsencode(os.readlink(path)))
    elif kind == "f":
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
print(digest.hexdigest())
PY
}

install_scr_prime_ccid() {
  (
  local drop bundle backup timestamp temp source stage built package_version before_hash after_hash metadata
  local package_owned=0 hold_was_present=0 hold_added=0 replaced=0 completed=0
  drop=$(pcsc_dropdir); bundle="$drop/ifd-ccid.bundle"; timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup="$state_dir/driver-backups/$timestamp"; metadata="$state_dir/scr-prime-driver.json"
  package_version=$(dpkg-query -W -f='${Version}' libccid 2>/dev/null || true)
  apt-mark showhold 2>/dev/null | grep -qx libccid && hold_was_present=1

  info "system libccid does not expose SCR Prime; building CCID $CCID_VERSION with patch 03 only"
  temp=$(mktemp -d /tmp/mdd-ccid.XXXXXX); stage="$temp/stage"
  driver_install_cleanup() {
    local code=${1:-1}
    trap - EXIT HUP INT TERM
    if ((completed == 0 && replaced)); then
      rm -rf -- "$bundle"
      if [[ -d "$backup/ifd-ccid.bundle" ]]; then cp -a -- "$backup/ifd-ccid.bundle" "$bundle"; fi
      ((hold_added == 0)) || apt-mark unhold libccid >/dev/null 2>&1 || true
      systemctl restart pcscd.service >/dev/null 2>&1 || true
      rm -rf -- "$backup"
    fi
    rm -rf -- "$temp" "$bundle.mdd-new"
    ((completed)) || rm -f "$data_dir/orchestrator/pcsc-maintenance"
    exit "$code"
  }
  trap 'driver_install_cleanup $?' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  download_verified "https://github.com/LudovicRousseau/CCID/archive/refs/tags/${CCID_VERSION}.tar.gz" "$temp/source.tar.gz" "$CCID_SHA256"
  tar xf "$temp/source.tar.gz" -C "$temp"; source="$temp/CCID-$CCID_VERSION"
  patch -p1 -d "$source" < "$source_dir/patches/ccid/03_scr_prime_reader.patch"
  meson setup "$source/builddir" "$source" --prefix=/usr
  ninja -C "$source/builddir"
  DESTDIR="$stage" ninja -C "$source/builddir" install
  built=$(find "$stage" -type d -path '*/ifd-ccid.bundle' -print -quit)
  [[ -n "$built" ]] || die "CCID build did not produce ifd-ccid.bundle"

  install -d -m 0700 "$backup"
  if [[ -d "$bundle" ]]; then
    cp -a -- "$bundle" "$backup/ifd-ccid.bundle"
    before_hash=$(tree_hash "$bundle")
    if [[ -n "$package_version" ]] && dpkg-query -L libccid 2>/dev/null | grep -Fq "$bundle/"; then package_owned=1; fi
  else
    before_hash=missing
  fi
  install -d -m 0755 "$drop"
  rm -rf -- "$bundle.mdd-new"; cp -a -- "$built" "$bundle.mdd-new"
  rm -rf -- "$bundle"; mv "$bundle.mdd-new" "$bundle"; replaced=1
  after_hash=$(tree_hash "$bundle")
  systemctl restart pcscd.service
  scr_prime_pcsc_visible || die "patched CCID was installed but does not expose SCR Prime"
  if ((package_owned && hold_was_present == 0)); then
    apt-mark hold libccid >/dev/null
    hold_added=1
  fi
  python3 - "$metadata" "$drop" "$backup" "$package_version" "$before_hash" "$after_hash" \
    "$package_owned" "$hold_was_present" "$hold_added" <<'PY'
import json, os, sys, tempfile
path, drop, backup, package_version, before_hash, after_hash = sys.argv[1:7]
package_owned, hold_was_present, hold_added = (value == "1" for value in sys.argv[7:10])
payload = {
    "managed": True, "device": "04d9:c001", "ccid_version": "1.6.2",
    "patches": ["03_scr_prime_reader.patch"], "dropdir": drop,
    "backup_path": backup, "package_version": package_version,
    "before_hash": before_hash, "installed_hash": after_hash,
    "overwrote_package": package_owned, "hold_was_present": hold_was_present,
    "libccid_held": hold_added,
}
fd, tmp = tempfile.mkstemp(prefix=".scr-prime-", dir=os.path.dirname(path))
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, sort_keys=True, indent=2)
    stream.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
  completed=1
  trap - EXIT HUP INT TERM
  rm -rf -- "$temp"
  )
}

scr_prime_gate() {
  local present=0 output metadata="$state_dir/scr-prime-driver.json" drop expected actual
  lsusb -d 04d9:c001 >/dev/null 2>&1 && present=1
  if ((present == 0)); then
    ((require_scr_prime == 0)) && { warn "SCR Prime 04d9:c001 is not passed through to the VM"; return; }
    die "SCR Prime 04d9:c001 is not visible; connect the complete USB device to this VMware guest"
  fi
  publish_pcsc_maintenance
  scr_gate_cleanup() { rm -f "$data_dir/orchestrator/pcsc-maintenance"; }
  trap scr_gate_cleanup EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  systemctl restart pcscd.service
  if [[ -f "$metadata" ]]; then
    read -r drop expected < <(python3 - "$metadata" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
if (value.get("managed") is not True or value.get("device") != "04d9:c001" or
        value.get("patches") != ["03_scr_prime_reader.patch"]):
    raise SystemExit("invalid SCR Prime driver metadata")
print(value.get("dropdir", ""), value.get("installed_hash", ""))
PY
)
    [[ "$drop" == /* && "$expected" =~ ^[0-9a-f]{64}$ ]] || die "invalid SCR Prime driver metadata"
    actual=$(tree_hash "$drop/ifd-ccid.bundle")
    [[ "$actual" == "$expected" ]] || die "installed SCR Prime driver no longer matches its MDD metadata"
    scr_prime_pcsc_visible || die "recorded patched driver is present, but pcsc_scan cannot see SCR Prime"
    info "SCR Prime is using the verified CCID patch 03 installation"
    printf 'patched\n' > "$state_dir/scr-prime-mode"
  elif scr_prime_pcsc_visible; then
    info "SCR Prime is supported by the native libccid package"
    printf 'native\n' > "$state_dir/scr-prime-mode"
  else
    install_scr_prime_ccid
    scr_prime_pcsc_visible || die "patched CCID installed, but pcsc_scan still does not list SCR Prime"
    printf 'patched\n' > "$state_dir/scr-prime-mode"
  fi

  output=$(pcsc_scan_capture 45)
  validate_scr_prime_reader "$output"
  if ((require_scr_prime)); then
    [[ -t 0 ]] || die "SCR Prime hot-plug acceptance needs an interactive terminal"
    printf 'Unplug SCR Prime, then press Enter. The installer will verify disappearance: '
    read -r _
    local deadline=$((SECONDS + 60))
    while lsusb -d 04d9:c001 >/dev/null 2>&1 && ((SECONDS < deadline)); do sleep 1; done
    ! lsusb -d 04d9:c001 >/dev/null 2>&1 || die "SCR Prime did not disappear within 60 seconds"
    printf 'Reconnect SCR Prime to this VM, then press Enter: '
    read -r _
    deadline=$((SECONDS + 90))
    while ! lsusb -d 04d9:c001 >/dev/null 2>&1 && ((SECONDS < deadline)); do sleep 1; done
    lsusb -d 04d9:c001 >/dev/null 2>&1 || die "SCR Prime did not reappear within 90 seconds"
    deadline=$((SECONDS + 45))
    while ! scr_prime_pcsc_visible && ((SECONDS < deadline)); do sleep 2; done
    scr_prime_pcsc_visible || die "SCR Prime did not recover in PC/SC after hot-plug"
  fi
  scr_gate_cleanup
  trap - EXIT HUP INT TERM
}

cellular_gate() {
  local deadline listing=""
  listing=$(mmcli -L 2>/dev/null || true)
  if grep -q '/Modem/' <<<"$listing"; then
    info "ModemManager detected a cellular modem"
    return
  fi
  if ((require_cellular == 0)); then
    warn "no Quectel-class modem is visible to ModemManager"
    return
  fi
  deadline=$((SECONDS + 90))
  while ((SECONDS < deadline)); do
    listing=$(mmcli -L 2>/dev/null || true)
    grep -q '/Modem/' <<<"$listing" && break
    sleep 3
  done
  if ! grep -q '/Modem/' <<<"$listing"; then
    die "no cellular modem detected; pass the complete Quectel USB composite device to the guest"
  fi
  info "ModemManager detected a cellular modem"
}

engine_fingerprint() {
  local source=$1 kind=$2
  env PCSC_VERSION="$PCSC_VERSION" sh "$source/tools/engine-fingerprint.sh" "$kind"
}

relocate_venv() {
  local root=$1 old_prefix=$2 new_prefix=$3
  [[ -d "$root/bin" && -f "$root/pyvenv.cfg" ]] || die "Control venv is incomplete before relocation"
  python3 - "$root" "$old_prefix" "$new_prefix" <<'PY'
import os
import sys

root, old_prefix, new_prefix = sys.argv[1:]
old = os.fsencode(old_prefix)
new = os.fsencode(new_prefix)
paths = [os.path.join(root, "pyvenv.cfg")]
paths.extend(
    entry.path for entry in os.scandir(os.path.join(root, "bin"))
    if entry.is_file(follow_symlinks=False)
)
changed = 0
for path in paths:
    with open(path, "rb") as stream:
        content = stream.read()
    if old not in content:
        continue
    with open(path, "wb") as stream:
        stream.write(content.replace(old, new))
    changed += 1
if changed == 0:
    raise SystemExit("Control venv did not contain its staging prefix")
for path in paths:
    with open(path, "rb") as stream:
        if old in stream.read():
            raise SystemExit(f"Control venv still references its staging path: {path}")
with open(os.path.join(root, "bin", "pip"), "rb") as stream:
    if not stream.readline().startswith(b"#!" + new + b"/bin/python"):
        raise SystemExit("Control venv pip shebang was not relocated")
PY
}

verify_prepared_build() {
  local source=$1 root=$2 expected_sha=$3 expected_version runtime_fp base_fp image
  [[ -f "$root/READY" && -f "$root/webui/index.html" && -x "$root/venv/bin/python" && \
     -f "$root/manifest.json" ]] || return 1
  [[ $(git -C "$source" rev-parse HEAD 2>/dev/null) == "$expected_sha" ]] || return 1
  expected_version=$(tr -d '\r\n' < "$source/VERSION")
  runtime_fp=$(engine_fingerprint "$source" runtime) || return 1
  base_fp=$(engine_fingerprint "$source" base) || return 1
  image="mdd-sim-gateway/engine:$expected_sha"
  docker image inspect "$image" >/dev/null 2>&1 || return 1
  [[ $(docker image inspect "$image" --format '{{.Architecture}}') == amd64 ]] || return 1
  [[ $(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}') == "$expected_sha" ]] || return 1
  [[ $(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.version"}}') == "$expected_version" ]] || return 1
  [[ $(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.source"}}') == \
     "https://github.com/suyi-92/mdd-sim-gateway" ]] || return 1
  [[ $(docker image inspect "$image" --format '{{index .Config.Labels "io.mdd-sim-gateway.runtime-fp"}}') == "$runtime_fp" ]] || return 1
  [[ $(docker image inspect "$image" --format '{{index .Config.Labels "io.mdd-sim-gateway.base-fp"}}') == "$base_fp" ]] || return 1
  "$root/venv/bin/pip" check >/dev/null 2>&1 || {
    warn "build verification failed: Control venv console scripts or dependencies are invalid"
    return 1
  }
  python3 - "$root/manifest.json" "$expected_sha" "$expected_version" "$image" "$runtime_fp" "$base_fp" \
    "$(docker image inspect "$image" --format '{{.Id}}')" \
    "$(docker image inspect "$image" --format '{{.Size}}')" "$(tree_hash "$root/webui")" <<'PY'
import json, sys
path, sha, version, image, runtime_fp, base_fp, image_id, image_size, webui_hash = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
except (OSError, ValueError):
    raise SystemExit(1)
expected = {
    "source_commit": sha, "version": version, "image": image,
    "architecture": "amd64", "runtime_fp": runtime_fp, "base_fp": base_fp,
    "source_repository": "https://github.com/suyi-92/mdd-sim-gateway",
    "image_id": image_id, "image_size": int(image_size), "webui_hash": webui_hash,
}
if any(value.get(key) != item for key, item in expected.items()):
    raise SystemExit(1)
if not str(value.get("asterisk", "")).startswith("Asterisk "):
    raise SystemExit(1)
if not isinstance(value.get("asterisk_modules"), int) or value["asterisk_modules"] <= 20:
    raise SystemExit(1)
PY
}

prepare_build() {
  [[ -n "$source_dir" ]] || die "prepare requires --source"
  source_dir=$(realpath "$source_dir")
  sha=${sha:-$(git -C "$source_dir" rev-parse HEAD)}
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "invalid source commit"
  build_root=${build_root:-$cache_dir/builds/$sha}
  validate_path "$build_root" "build root"
  if [[ -f "$build_root/READY" && $no_cache -eq 0 ]]; then
    if verify_prepared_build "$source_dir" "$build_root" "$sha"; then
      info "reusing locally verified build $sha"
      return
    fi
    warn "cached build identity check failed; rebuilding $sha"
  fi
  local temp="${build_root}.tmp.$$" runtime_fp base_fp image="mdd-sim-gateway/engine:$sha" version module_count asterisk_version
  local image_id image_size webui_hash
  [[ "$temp" == "$(dirname "$build_root")/"* ]] || die "unsafe build staging path"
  rm -rf -- "$temp"; install -d -m 0755 "$temp/venv" "$temp/webui"

  info "building Control virtual environment"
  python3 -m venv --clear "$temp/venv"
  "$temp/venv/bin/pip" install --disable-pip-version-check --no-cache-dir -r "$source_dir/control/requirements.txt"
  "$temp/venv/bin/pip" check

  info "building WebUI in fixed Node container $NODE_BUILD_IMAGE"
  docker run --rm --network bridge -v "$source_dir/webui:/src:ro" -v "$temp/webui:/out" \
    "$NODE_BUILD_IMAGE" sh -euc '
      mkdir /work; cp /src/package.json /src/package-lock.json /src/index.html /src/vite.config.js /work/;
      cp -a /src/src /src/public /work/; cd /work; npm ci; npm run build; cp -a dist/. /out/'
  [[ -f "$temp/webui/index.html" ]] || die "WebUI build did not produce index.html"

  runtime_fp=$(engine_fingerprint "$source_dir" runtime)
  base_fp=$(engine_fingerprint "$source_dir" base)
  version=$(tr -d '\r\n' < "$source_dir/VERSION")
  info "building Engine image for commit $sha"
  local build_args=(docker build --pull --label "org.opencontainers.image.revision=$sha" \
    --build-arg "PCSC_VERSION=$PCSC_VERSION" --build-arg "RUNTIME_FP=$runtime_fp" \
    --build-arg "BASE_FP=$base_fp" --build-arg "MDD_VERSION=$version" \
    -t "$image" -f "$source_dir/engine/Dockerfile" "$source_dir/engine")
  ((no_cache)) && build_args=(docker build --pull --no-cache --label "org.opencontainers.image.revision=$sha" \
    --build-arg "PCSC_VERSION=$PCSC_VERSION" --build-arg "RUNTIME_FP=$runtime_fp" \
    --build-arg "BASE_FP=$base_fp" --build-arg "MDD_VERSION=$version" \
      -t "$image" -f "$source_dir/engine/Dockerfile" "$source_dir/engine")
  "${build_args[@]}"
  [[ $(docker image inspect "$image" --format '{{.Architecture}}') == amd64 ]] || die "Engine image architecture is not amd64"
  [[ $(docker image inspect "$image" --format '{{index .Config.Labels "io.mdd-sim-gateway.runtime-fp"}}') == "$runtime_fp" ]] || die "Engine runtime fingerprint mismatch"
  [[ $(docker image inspect "$image" --format '{{index .Config.Labels "io.mdd-sim-gateway.base-fp"}}') == "$base_fp" ]] || die "Engine base fingerprint mismatch"
  [[ $(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}') == "$sha" ]] || die "Engine source identity mismatch"
  [[ $(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.version"}}') == "$version" ]] || die "Engine product version mismatch"
  [[ $(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.source"}}') == \
     "https://github.com/suyi-92/mdd-sim-gateway" ]] || die "Engine source repository label mismatch"
  asterisk_version=$(docker run --rm --entrypoint /usr/sbin/asterisk "$image" -V)
  [[ "$asterisk_version" == Asterisk\ * ]] || die "Engine Asterisk version could not be verified"
  module_count=$(docker run --rm --entrypoint /bin/sh "$image" -c "find /usr/lib64/asterisk/modules /usr/lib/asterisk/modules -type f -name '*.so' 2>/dev/null | wc -l")
  ((module_count > 20)) || die "Engine Asterisk module count is unexpectedly low: $module_count"
  docker run --rm --entrypoint python3 "$image" -c 'import jinja2, requests, smartcard, cryptography'
  docker run --rm --cap-add NET_ADMIN --device /dev/net/tun --entrypoint /bin/sh "$image" -c \
    'test -c /dev/net/tun; ip tuntap add dev mdd-build-test mode tun; ip link delete mdd-build-test'
  image_id=$(docker image inspect "$image" --format '{{.Id}}')
  image_size=$(docker image inspect "$image" --format '{{.Size}}')
  webui_hash=$(tree_hash "$temp/webui")
  python3 - "$temp/manifest.json" "$sha" "$version" "$image" "$runtime_fp" "$base_fp" \
    "$asterisk_version" "$module_count" "$image_id" "$image_size" "$webui_hash" <<'PY'
import datetime, json, os, sys
path, sha, version, image, runtime_fp, base_fp, asterisk, modules, image_id, image_size, webui_hash = sys.argv[1:]
with open(path, "w", encoding="utf-8") as stream:
    json.dump({"source_commit": sha, "version": version, "image": image,
               "source_repository": "https://github.com/suyi-92/mdd-sim-gateway",
               "architecture": "amd64", "runtime_fp": runtime_fp, "base_fp": base_fp,
               "asterisk": asterisk.strip(), "asterisk_modules": int(modules),
               "image_id": image_id, "image_size": int(image_size),
               "webui_hash": webui_hash,
               "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
              stream, sort_keys=True, indent=2)
    stream.write("\n")
os.chmod(path, 0o644)
PY
  install -d -m 0755 "$(dirname "$build_root")"
  rm -rf -- "$build_root"
  mv "$temp" "$build_root"
  relocate_venv "$build_root/venv" "$temp/venv" "$build_root/venv"
  touch "$build_root/READY"
  if ! verify_prepared_build "$source_dir" "$build_root" "$sha"; then
    rm -f "$build_root/READY"
    die "prepared build identity verification failed"
  fi
  info "verified local build: $build_root"
}

write_managed_state() {
  install -d -m 0700 "$state_dir" "$data_dir" "$backup_dir" "$cache_dir"
  cat > "$state_dir/managed.env" <<EOF
INSTALL_DIR=$install_dir
DATA_DIR=$data_dir
BACKUP_DIR=$backup_dir
STATE_DIR=$state_dir
CACHE_DIR=$cache_dir
ORIGIN_URL=$ORIGIN_URL
BRANCH=vmware
REQUIRE_SCR_PRIME=$require_scr_prime
REQUIRE_CELLULAR=$require_cellular
EOF
  chmod 0600 "$state_dir/managed.env"
}

activate_build() {
  [[ -n "$source_dir" ]] || source_dir=$install_dir
  source_dir=$(realpath "$source_dir")
  sha=${sha:-$(git -C "$source_dir" rev-parse HEAD)}
  build_root=${build_root:-$cache_dir/builds/$sha}
  verify_prepared_build "$source_dir" "$build_root" "$sha" || die "build is incomplete or does not match source $sha: $build_root"
  local image="mdd-sim-gateway/engine:$sha" lan_ip
  docker image inspect "$image" >/dev/null || die "verified Engine image is missing: $image"
  docker tag "$image" "$ENGINE_STABLE_IMAGE"
  ln -sfn "$build_root/venv" "$source_dir/.venv.new"; mv -Tf "$source_dir/.venv.new" "$source_dir/.venv"
  ln -sfn "$build_root/webui" "$source_dir/webui/dist.new"; mv -Tf "$source_dir/webui/dist.new" "$source_dir/webui/dist"
  lan_ip=$(network_value source)

  cat > /etc/systemd/system/mdd-sim-gateway-control.service <<EOF
[Unit]
Description=MDD Sim Gateway native Control and WebUI
After=network-online.target docker.service pcscd.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=$source_dir/control
Environment=MDD_DATA=$data_dir
Environment=MDD_REPO_DIR=$source_dir
Environment=MDD_VENV_DIR=$source_dir/.venv
Environment=MDD_WEBUI=$source_dir/webui/dist
Environment=MDD_HTTP_PORT=8443
Environment=MDD_BIND=0.0.0.0
Environment=MDD_ADVERTISE_ADDR=$lan_ip
Environment=MDD_ENGINE_IMAGE=$ENGINE_STABLE_IMAGE
Environment=MDD_MANAGER_URL=https://host.docker.internal:8443
Environment=MDD_PCSCD_DIR=/run/pcscd
Environment=PYTHONUNBUFFERED=1
ExecStart=$source_dir/.venv/bin/python run.py
Restart=on-failure
RestartSec=3
User=root
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/mdd-sim-gateway-orchestrator.service <<EOF
[Unit]
Description=MDD Sim Gateway host egress and modem orchestrator
After=network-online.target docker.service pcscd.service ModemManager.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
WorkingDirectory=$source_dir
Environment=PYTHONUNBUFFERED=1
ExecStart=$source_dir/.venv/bin/python $source_dir/host/mdd_orchestrator.py --data $data_dir --repo $source_dir
Restart=always
RestartSec=3
User=root
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  install -m 0755 "$source_dir/scripts/mddctl" /usr/local/sbin/mddctl
  systemctl daemon-reload
  systemctl enable mdd-sim-gateway-control.service mdd-sim-gateway-orchestrator.service >/dev/null
  printf '%s\n' "$sha" > "$state_dir/active-commit"
  info "activated local build $sha"
}

firewall_rule_specs() {
  local python="$source_dir/.venv/bin/python" rules
  [[ -x "$python" ]] || die "Control venv is unavailable; cannot calculate exact firewall ports"
  rules=$(PYTHONPATH="$source_dir" MDD_DATA="$data_dir" "$python" - <<'PY'
from copy import deepcopy
from control.app import config

data = config.load()

def order(item):
    try:
        index = int(item.get("index", 1_000_000))
    except (TypeError, ValueError):
        index = 1_000_000
    return index, str(item.get("id") or "")

selected = []
for instance in sorted(data.get("instances", {}).values(), key=order)[:2]:
    if isinstance(instance.get("ports"), dict):
        selected.append(instance["ports"])

# On a fresh deployment predict the same collision-aware blocks that auto-provision will use.
# Placeholders reserve each prediction before asking for the next one.
working = deepcopy(data)
while len(selected) < 2:
    block = config.alloc_ports_auto(working)
    selected.append(block)
    placeholder = f"__firewall_prediction_{len(selected)}"
    working.setdefault("instances", {})[placeholder] = {
        "id": placeholder, "index": 1_000_000 + len(selected), "ports": block}

print("8443/tcp|MDD Control")
for number, ports in enumerate(selected[:2], 1):
    web = int(ports.get("webrtc", 8089 + (number - 1) * 10))
    start = int(ports.get("rtp_start", 10000 + (number - 1) * 2000))
    end = start + config.rtp_span(ports) - 1
    print(f"{web}/tcp|MDD line {number} WebRTC")
    print(f"{start}:{end}/udp|MDD line {number} RTP")
PY
) || die "could not calculate exact firewall ports"
  printf '%s\n' "$rules"
}

firewall_ports() {
  local rules
  rules=$(firewall_rule_specs) || die "could not calculate exact firewall ports"
  printf 'Required inbound ports for the two configured or predicted lines:\n'
  while IFS='|' read -r spec comment; do
    printf '  %-22s %s\n' "$spec" "$comment"
  done <<< "$rules"
}

configure_firewall_rules() {
  local rules
  rules=$(firewall_rule_specs) || die "could not calculate exact firewall ports"
  firewall_ports
  if ((configure_firewall == 0)); then
    info "firewall changes were not requested; printed required ports only"
    return 0
  fi
  if have ufw && ufw status 2>/dev/null | grep -q '^Status: active'; then
    touch "$state_dir/firewall-created"
    chmod 0600 "$state_dir/firewall-created"
    while IFS='|' read -r spec comment; do
      if ! ufw status | grep -Eq "^${spec//\//\\/}[[:space:]]+ALLOW"; then
        ufw allow "$spec" comment "$comment"
        grep -Fqx "$spec" "$state_dir/firewall-created" || printf '%s\n' "$spec" >> "$state_dir/firewall-created"
      fi
    done <<< "$rules"
    return
  fi
  if nft list ruleset 2>/dev/null | grep -q .; then
    if nft list table inet mdd_sim_gateway >/dev/null 2>&1 && [[ ! -f "$state_dir/firewall-nft-created" ]]; then
      die "nftables table inet mdd_sim_gateway already exists but is not recorded as MDD-owned"
    fi
    {
      printf 'table inet mdd_sim_gateway {\n  chain input {\n'
      printf '    type filter hook input priority -5; policy accept;\n'
      while IFS='|' read -r spec comment; do
        local range protocol
        range=${spec%/*}; range=${range/:/-}; protocol=${spec##*/}
        printf '    %s dport %s accept comment "%s"\n' "$protocol" "$range" "$comment"
      done <<< "$rules"
      printf '  }\n}\n'
    } > "$state_dir/mdd-sim-gateway.nft"
    if [[ -f "$state_dir/firewall-nft-created" ]]; then
      nft delete table inet mdd_sim_gateway >/dev/null 2>&1 || true
    fi
    nft -f "$state_dir/mdd-sim-gateway.nft"
    touch "$state_dir/firewall-nft-created"
    chmod 0600 "$state_dir/firewall-nft-created"
    warn "nftables rules were loaded from $state_dir/mdd-sim-gateway.nft; integrate this file into your distro persistence policy"
  else
    info "no active UFW/nftables policy detected; no firewall change was needed"
  fi
}

https_health_ready() {
  local deadline=$((SECONDS + 30))
  while ((SECONDS < deadline)); do
    if curl --fail --silent --insecure --connect-timeout 2 --max-time 4 \
        https://127.0.0.1:8443/api/auth/status >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

health_check() {
  systemctl is-active --quiet mdd-sim-gateway-control.service || die "Control service is not active"
  systemctl is-active --quiet mdd-sim-gateway-orchestrator.service || die "orchestrator service is not active"
  https_health_ready || \
    die "HTTPS health check did not become ready within 30 seconds; inspect the Control journal"
  docker image inspect "$ENGINE_STABLE_IMAGE" >/dev/null || die "stable Engine image is missing"
  docker run --rm --cap-add NET_ADMIN --device /dev/net/tun --entrypoint /bin/sh "$ENGINE_STABLE_IMAGE" -c 'test -c /dev/net/tun' >/dev/null
  if [[ -f "$state_dir/managed.env" ]]; then
    local req_scr req_cell
    req_scr=$(awk -F= '$1=="REQUIRE_SCR_PRIME" {print $2}' "$state_dir/managed.env")
    req_cell=$(awk -F= '$1=="REQUIRE_CELLULAR" {print $2}' "$state_dir/managed.env")
    if [[ "$req_scr" == 1 ]]; then
      lsusb -d 04d9:c001 >/dev/null || die "SCR Prime USB health gate failed"
      scr_prime_pcsc_visible || die "SCR Prime PC/SC health gate failed"
    fi
    if [[ "$req_cell" == 1 ]]; then mmcli -L 2>/dev/null | grep -q '/Modem/' || die "cellular modem health gate failed"; fi
  fi
  info "HTTPS, systemd, Docker/TUN and required hardware health checks passed"
}

case "$action" in
  prepare)
    detect_distro
    prepare_build
    ;;
  verify)
    [[ -n "$source_dir" && -n "$build_root" && -n "$sha" ]] || die "verify requires --source, --build-root and --sha"
    source_dir=$(realpath "$source_dir")
    verify_prepared_build "$source_dir" "$build_root" "$sha" || die "verified build does not match source $sha"
    ;;
  activate)
    activate_build
    ;;
  health)
    source_dir=${source_dir:-$install_dir}
    health_check
    ;;
  driver)
    [[ -n "$source_dir" ]] || die "driver requires --source"
    source_dir=$(realpath "$source_dir")
    [[ -r "$source_dir/patches/ccid/03_scr_prime_reader.patch" ]] || \
      die "verified SCR Prime CCID patch is missing from the active source"
    detect_distro
    for dependency in curl tar patch meson ninja systemctl lsusb pcsc_scan; do
      have "$dependency" || die "SCR Prime driver installation dependency is missing: $dependency"
    done
    install -d -m 0700 "$state_dir"
    scr_prime_gate
    info "SCR Prime driver validation completed"
    ;;
  install)
    detect_distro
    install -d -m 0700 "$state_dir"
    preflight_host
    if ((assume_yes)); then info "--yes accepted; checksum, network, hardware and health gates remain mandatory"; fi
    prepare_network_guard
    network_guard_armed=1
    network_guard_cleanup() {
      local code=${1:-1}
      trap - EXIT HUP INT TERM
      if ((network_guard_armed)); then
        warn "installation stopped before the network guard passed; restoring the previous network policy"
        restore_network_guard
      fi
      exit "$code"
    }
    trap 'network_guard_cleanup $?' EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    install_packages
    verify_network_guard
    network_guard_armed=0
    trap - EXIT HUP INT TERM
    install_modemmanager_dropin
    install_source_checkout
    write_managed_state
    ensure_singbox
    ensure_xray
    ensure_vpcd
    ensure_lpac
    scr_prime_gate
    cellular_gate
    build_root="$cache_dir/builds/$sha"
    prepare_build
    activate_build
    configure_firewall_rules
    if ((no_start == 0)); then
      info "starting Control and orchestrator services"
      if ! systemctl restart mdd-sim-gateway-orchestrator.service mdd-sim-gateway-control.service; then
        systemctl --no-pager --full status mdd-sim-gateway-control.service \
          mdd-sim-gateway-orchestrator.service >&2 || true
        die "could not start MDD systemd services"
      fi
      health_check
    else
      info "services installed but not started (--no-start)"
    fi
    info "installation complete; manage this VM with mddctl"
    ;;
esac
