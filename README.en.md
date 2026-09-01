<p align="center">
  <img src="assets/logo-lockup.svg" width="520" alt="MDD Sim Gateway">
</p>

<p align="center"><strong>Run two locally built SIM communication lines in a VMware Linux guest.</strong></p>

<p align="center">
  <a href="README.md">中文</a> ·
  <a href="#install-first-attach-hardware-later">Quick start</a> ·
  <a href="docs/INSTALL.md">Detailed guide</a> ·
  <a href="docs/TROUBLESHOOTING.md">Troubleshooting</a>
</p>

## Install first, attach hardware later

**Hardware is not required for the base installation.** `--require-scr-prime` and
`--require-cellular` are acceptance gates for that installer run, not feature switches. Omitting
them does not disable either device and does not require reinstalling the guest later.

When neither device is passed through yet, run:

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/mdd-sim-gateway/vmware/bootstrap.sh) install
```

Missing SCR Prime and Quectel devices produce warnings and the ordinary `install` does not wait for
hardware. Control, WebUI, pcscd, ModemManager, NetworkManager, and Engine are still installed.

After attaching both devices to the guest:

1. Pass SCR Prime and the **complete Quectel USB composite device** through in VMware Workstation,
   and insert a SIM in SCR Prime. Passing only Windows COM ports is insufficient.
2. Rerun the idempotent installer with both hardware gates:

   ```bash
   bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/mdd-sim-gateway/vmware/bootstrap.sh) install --require-scr-prime --require-cellular
   ```

   The same commit reuses its verified local build and Docker cache. SCR Prime is checked against
   the distribution driver first; when required, CCID with patch 03 only is installed automatically,
   followed by ATR and physical unplug/replug validation. The cellular gate waits up to about 90
   seconds for `mmcli -L` to expose a modem.
3. Follow the SCR Prime unplug/replug prompts, then run `sudo mddctl doctor`.
4. Open `https://<reserved-VM-address>:8443`. Create SCR Prime as a **PC/SC, VoWiFi-only** line.
   Create Quectel as a **modem, 4G + VoWiFi** line and enter the SIM's APN/4G settings so
   NetworkManager can create its GSM profile, bearer, and IP.

When attaching only one device, rerun with only its corresponding `--require-scr-prime` or
`--require-cellular` gate. Quectel is normally hot-detected but still needs a WebUI line. SCR Prime
should be revalidated because its real `04d9:c001` presence is the evidence needed to decide whether
the CCID patch is required. If the guest firewall is active, add `--configure-firewall` to that
acceptance run or apply the exact printed ports manually.

The `vmware` branch targets VMware Workstation on an x86_64 Windows host. Control and WebUI run
natively in the guest under systemd; Docker is rootful and is used only for per-SIM Engine
containers. The project does not use GitHub Actions, GitHub Release updates, prebuilt project
archives, or Git LFS delivery assets. Installation and updates build the checkout locally.

## Supported topology

| Item | Scope |
|---|---|
| Guest OS | Ubuntu 24.04/26.04, Debian 12/13, x86_64 only |
| Network | VMware bridged NIC, router DHCP reservation by VM MAC |
| Reader line | Santi Electronics SCR Prime `04d9:c001`, one SIM, VoWiFi-only |
| Modem line | One Quectel-class USB composite modem, another SIM, 4G + VoWiFi |
| Control | Python venv + systemd, HTTPS on 8443/TCP |
| Engine | One rootful-Docker container per active SIM |
| Capacity | `max_sim_lines` defaults to 13 and accepts 1–32 |

SCR Prime is a PC/SC reader and has no cellular radio or 4G switch. Cellular service comes from
the separate Quectel-class modem.

## VMware prerequisites

Before starting the guest:

1. Use a bridged NIC and reserve the VM NIC MAC address in the router's DHCP server.
2. Allocate 4 vCPUs, 8 GiB RAM, and a 64 GiB dynamic disk. Grow the guest partition and root
   filesystem as well as the virtual disk; `df -h /` is authoritative.
3. Enable a USB 3.1 controller.
4. Connect SCR Prime and the complete Quectel USB composite device to the guest from Workstation's
   removable-device menu. Passing only Windows COM ports is insufficient.
5. Enable automatic connection only for those two exact devices, not every new USB device.
6. Keep VMware USB Arbitration Service running on Windows. Once connected to the guest, the
   devices must no longer be held by Windows drivers.

## Installer behavior and options

Run the commands above as an ordinary user inside the guest; do not pipe the download into `sudo`.
The bootstrap downloads a complete single-branch `vmware` checkout as the current user, asks for
sudo once, and invokes a local installer file. The first clean Engine build compiles Asterisk,
pjproject, pcsc-lite, and Python dependencies and may take tens of minutes.

Supported bootstrap options:

```text
install | update | doctor
--install-dir PATH
--data-dir PATH
--ref vmware|<40-character commit>
--require-scr-prime
--require-cellular
--configure-firewall
--no-start
--dry-run
--yes
```

Defaults are `/opt/mdd-sim-gateway` for source, `/var/lib/mdd-sim-gateway` for data,
`/var/backups/mdd-sim-gateway` for backups, and `/etc/mdd-sim-gateway` for root-owned machine
metadata. `--yes` never bypasses checksum, Git, network, hardware, or health gates.

## SCR Prime driver handling

The installer follows evidence rather than guessing from the distribution version:

1. `lsusb -d 04d9:c001` must prove VMware USB passthrough.
2. A bounded `pcsc_scan` checks the distribution libccid first. Native support is kept unchanged.
3. If USB sees the device but PC/SC does not, CCID 1.6.2 is downloaded with a fixed SHA-256 and
   **only** `03_scr_prime_reader.patch` is applied. HSIC patches 01 and 02 are never used.
4. The replaced bundle, package version, hashes, patch set, and backup path are recorded. `libccid`
   is held only when the distribution bundle was actually replaced.
5. PC/SC, ATR, and physical unplug/replug recovery must pass.

Use `sudo mddctl driver status` to inspect it and `sudo mddctl driver restore` to restore only a
driver that MDD metadata and hashes prove MDD changed. Every project update probes whether the
current distribution driver has acquired native SCR Prime support.

## Management

```text
mddctl status
mddctl doctor [--json]
mddctl start|stop|restart|logs
mddctl update [--no-cache]
mddctl backup [--output PATH]
mddctl restore --input PATH
mddctl driver status|restore
mddctl uninstall [--purge]
```

`mddctl update` fetches only `origin/vmware`. It requires the exact managed remote, the `vmware`
branch, a clean tree, no Git operation in progress, and a fast-forward relationship. It builds
and tests the new commit in a temporary worktree before stopping the running version. Activation
or health failure restores the previous commit, venv, WebUI, Engine image, and data snapshot.
It never merges, rebases, force-pushes, or calls a GitHub Release API.

`mddctl backup` stops MDD, confirms managed Engines are stopped, checkpoints and integrity-checks
SQLite, then produces a root-only tar.gz, SHA-256 sidecar, and secret-free manifest. Restore rejects
checksum failures, unsafe archive paths, links/devices, invalid manifests, and corrupt SQLite.

> Backup archives contain plaintext credentials. Store them only on BitLocker, encrypted removable
> media, or another access-controlled encrypted location. For whole-machine migration, stop MDD,
> shut down the guest, and export or copy the VMware VM instead.

`doctor --json` reports version and boolean health only; it must never print IMSI, ICCID, IMEI,
credentials, phone numbers, or message text.

## Ports and firewall

The allocator probes real TCP and UDP occupancy. The first two default line blocks require:

```text
8443/tcp              Control/WebUI
8089/tcp, 8099/tcp    WebRTC/WSS
10000-10011/udp       line 1 RTP/RTCP
12000-12011/udp       line 2 RTP/RTCP
```

The installer does not broadly rewrite UFW or nftables. The values above are the conflict-free
defaults; installation predicts the exact two blocks from saved lines and live TCP/UDP occupancy.
It prints that list without changing rules unless `--configure-firewall` is explicitly supplied.

## Verification boundary

Repository tests can verify source behavior and installer contracts. The following remain physical
acceptance gates and must be run separately on fresh VMs for all four distributions: clean and
repeat install, no-op and real fast-forward update, failed-build rollback, unchanged bridged route
and DHCP address, SCR Prime USB/PCSC/ATR/hotplug, Quectel tty/WWAN/ModemManager/NetworkManager bearer,
concurrent registration, inbound/outbound calls, browser two-way audio, supported SMS, guest reboot,
and Windows-host reboot recovery. Do not infer those results from source tests.

See [the detailed installation guide](docs/INSTALL.md),
[troubleshooting](docs/TROUBLESHOOTING.md), and [architecture](docs/ARCHITECTURE.md).

## Security and responsible use

- Use only for a verified subscriber where local law, the carrier, and the plan permit it. Do not
  use it for fraud, nuisance calling, verification-code collection, line rental, or telecom service
  for third parties.
- AKA keys remain inside the SIM/eSIM; the project does not read or store Ki/OP/OPc.
- Lowering `max_sim_lines` retains existing records but prevents out-of-limit lines from starting.
- Open `https://<reserved-VM-address>:8443` on a trusted LAN/VPN and create the administrator
  immediately after installation.

The project is GPL-3.0-only. The CCID patch is an LGPL-2.1-or-later derivative of CCID; see
[patches/ccid/README.md](patches/ccid/README.md), [NOTICE](NOTICE), and
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
