#!/usr/bin/env bash
# Local rootful Docker policy. Also vendored by vmware-ubuntu-bootstrap.
# No context writes, daemon configuration changes, package removals or restarts.

docker_local() {
  env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY \
    -u DOCKER_CERT_PATH -u DOCKER_API_VERSION -u BUILDKIT_HOST \
    BUILDX_BUILDER=default docker --host unix:///var/run/docker.sock "$@"
}

docker_package_status() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true
}

docker_socket_is_local() {
  [[ -S /var/run/docker.sock && $(stat -Lc %u /var/run/docker.sock) == 0 \
    && $(readlink -f /var/run/docker.sock) == /run/docker.sock ]]
}

docker_local_healthy() {
  docker_socket_is_local || return 1
  local result
  local -a fields
  result=$(timeout 5 env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS \
    -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH -u DOCKER_API_VERSION \
    docker --host unix:///var/run/docker.sock info \
      --format $'{{.OSType}}\n{{.ID}}\n{{.DockerRootDir}}\n{{json .SecurityOptions}}\n{{.OperatingSystem}}' 2>/dev/null) || return 1
  mapfile -t fields <<<"$result"
  # Keep this pre-package check independent of Python/jq (Debian minimal hosts).
  [[ ${#fields[@]} == 5 && "${fields[0]}" == linux && -n "${fields[1]}" \
    && "${fields[2]}" == /* && "${fields[3]}" == \[*\] \
    && "${fields[3],,}" != *rootless* && "${fields[4],,}" != *desktop* \
    && "${fields[4],,}" != *podman* ]]

}

docker_installation_evidence() {
  local name
  for name in docker dockerd containerd runc dockerd-rootless.sh podman; do
    type -P "$name" >/dev/null 2>&1 && return 0
  done
  for name in /var/lib/docker /var/lib/containerd /etc/docker /run/docker.sock \
    /snap/docker /var/snap/docker /run/user/*/docker.sock; do
    [[ -e "$name" || -L "$name" ]] && return 0
  done
  return 1
}

docker_detect() {
  # stdout is a single state; diagnostics go through the caller.
  local package status ce=0 distro=0 evidence=0 incomplete=0 load active unit
  for package in docker-ce docker.io docker-ce-cli docker-ce-rootless-extras \
    docker-desktop podman-docker moby-engine moby-cli containerd.io containerd runc; do
    status=$(docker_package_status "$package")
    [[ -z "$status" || "$status" == *"not-installed" ]] && continue
    evidence=1
    # Residual configuration is evidence on a fresh host, but does not make a
    # verified installed daemon ambiguous (e.g. a previously removed docker.io).
    [[ "$status" != *"ok config-files" ]] || continue
    [[ "$status" == 'install ok installed' || "$status" == 'hold ok installed' ]] || incomplete=1
    [[ "$package" != docker-ce ]] || ce=1
    [[ "$package" != docker.io ]] || distro=1
  done
  load=$(systemctl show docker.service -p LoadState --value 2>/dev/null || true)
  active=$(systemctl show docker.service -p ActiveState --value 2>/dev/null || true)
  unit=$(systemctl show docker.service -p UnitFileState --value 2>/dev/null || true)
  if ((incomplete || (ce && distro))) || [[ "$load" == masked || "$unit" == masked* || "$active" == failed ]]; then
    printf 'broken\n'; return
  fi
  if ((ce || distro)); then
    if [[ "$load" != loaded ]] || ! command -v dockerd >/dev/null 2>&1 \
        || ! docker_local --version 2>/dev/null | grep -q '^Docker version '; then
      printf 'broken\n'; return
    fi
    if ((ce)); then
      for package in docker-ce-cli containerd.io; do
        status=$(docker_package_status "$package")
        if [[ "$status" != 'install ok installed' && "$status" != 'hold ok installed' ]]; then
          printf 'broken\n'; return
        fi
      done
    fi
    if docker_local_healthy; then
      ((ce)) && printf 'healthy-ce\n' || printf 'healthy-distro\n'
    elif [[ "$active" == inactive ]]; then
      ((ce)) && printf 'stopped-ce\n' || printf 'stopped-distro\n'
    else
      printf 'broken\n'
    fi
    return
  fi
  if ((evidence)) || [[ "$load" != not-found ]] || docker_installation_evidence; then
    printf 'unknown\n'
  else
    printf 'absent\n'
  fi
}

docker_existing_error() {
  printf '%s\n' '已有 Docker 异常，停止安装；不会更换软件包来源。' \
    '需要本机 rootful Docker：CLI-only、rootless-only、远程 daemon、Docker Desktop、Podman 或残留/不完整安装不能替代它。' \
    '请检查 dpkg-query -W docker-ce docker-ce-cli docker.io containerd.io containerd；systemctl status docker.service；journalctl -u docker.service -n 60。' \
    '检查 /var/run/docker.sock 所有者及链接目标。修复现有安装后重试；不需要修改用户的默认 context。' >&2
  return 1
}

docker_start_existing() {
  printf '%s\n' '启动现有 Docker（保留来源和配置）'
  timeout 35 systemctl start docker.service || { docker_existing_error; return 1; }
  local deadline=$((SECONDS + 30))
  while ((SECONDS < deadline)); do
    docker_local_healthy && return 0
    sleep 1
  done
  docker_existing_error
}

docker_safe_apt_install() {
  local policy=$1 plan
  shift
  plan=$(LC_ALL=C apt-get --simulate --no-remove install "$@") || return 1
  if grep -Eq '^Remv ' <<<"$plan"; then
    printf 'APT 计划移除软件包，已拒绝。\n' >&2; return 1
  fi
  if [[ "$policy" == preserve ]] && grep -Eq '^(Inst|Conf) (docker[^ ]*|containerd[^ ]*|runc|moby-[^ ]*|podman[^ ]*)( |:)' <<<"$plan"; then
    printf 'APT 计划改变已有 Docker/容器运行时，已拒绝。\n' >&2; return 1
  fi
  env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-remove "$@"
}

ensure_local_docker() {
  local state
  state=$(docker_detect)
  case "$state" in
    healthy-*) printf '复用现有 Docker（%s）\n' "${state#healthy-}" ;;
    stopped-*) docker_start_existing ;;
    absent)
      printf '首次安装 Docker（发行版 docker.io）\n'
      apt-get update -qq || return 1
      docker_safe_apt_install fresh docker.io || return 1
      systemctl enable docker.service || return 1
      docker_start_existing
      ;;
    *) docker_existing_error ;;
  esac
}
