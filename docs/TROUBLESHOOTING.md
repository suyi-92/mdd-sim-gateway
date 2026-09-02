# VMware 故障排查

先运行：

```bash
sudo mddctl status
sudo mddctl doctor
sudo mddctl logs
```

需要提交机器可读结果时使用：

```bash
sudo mddctl doctor --json
```

JSON 不包含 IMSI、ICCID、IMEI、号码、凭据或消息正文。分享其他日志前仍需人工复核。

## 1. 一键命令在下载或 sudo 前失败

- 必须以普通用户运行，不能先 `sudo bash`。
- 需要 `sudo`；最小系统若没有 Git，bootstrap 会在一次权限确认后通过 apt 安装 Git。
- `--ref` 只接受 `vmware` 或精确 40 位十六进制提交。
- 自定义源码/数据路径必须是无空白的绝对非根路径。

只看动作：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/mdd-sim-gateway/vmware/bootstrap.sh) install --dry-run --require-scr-prime --require-cellular
```

## 2. 系统或资源预检停止

支持范围只包含 x86_64 的 Ubuntu 24.04/26.04 和 Debian 12/13。检查：

```bash
uname -m
. /etc/os-release; printf '%s %s\n' "$ID" "$VERSION_ID"
ps -p 1 -o comm=
free -h
df -hT /
test -c /dev/net/tun && echo TUN_OK
ss -ltnp | grep ':8443'
```

虚拟硬盘显示 64 GiB、`df` 仍很小时，只扩大了 VMware 的磁盘层，没有扩分区/文件系统。
使用 `lsblk -f` 与 `findmnt /` 确定真实布局后再扩容；不要猜设备名。

## 3. 安装 NetworkManager 后地址或 SSH 变化

安装器会停止并报告 before/after 地址。证据位于：

```text
/etc/mdd-sim-gateway/network/default-interface
/etc/mdd-sim-gateway/network/default-gateway
/etc/mdd-sim-gateway/network/default-source
/etc/mdd-sim-gateway/network/address-before.json
/etc/mdd-sim-gateway/network/routes-before.json
/etc/mdd-sim-gateway/network/backend-before
```

检查：

```bash
ip -4 route show default
ip -4 route get 1.1.1.1
nmcli device status
networkctl status
```

若主网卡本来不由 NetworkManager 管理，确认以下策略仍存在且只允许 GSM：

```bash
sudo cat /etc/NetworkManager/conf.d/90-mdd-cellular-only.conf
```

不要为了让蜂窝上网而把桥接主网卡改成静态 IP；继续使用路由器 DHCP 保留。

## 4. SCR Prime：Windows 看得到，VM 看不到

这是 VMware 直通层问题，尚未进入 Linux 驱动：

1. Windows 服务中确认 VMware USB Arbitration Service 正常；
2. Workstation → VM → Removable Devices 中把 SCR Prime 连接到 guest；
3. 不要让 Windows 智能卡服务/厂商软件继续占用设备；
4. 客户机执行：

   ```bash
   lsusb -d 04d9:c001
   ```

未出现 `04d9:c001` 时，不要重装 libccid；驱动无法修复未直通的 USB。

## 5. SCR Prime：lsusb 可见，pcsc_scan 不可见

逐层检查：

```bash
sudo systemctl status pcscd --no-pager
sudo journalctl -u pcscd -n 100 --no-pager
timeout 15 pcsc_scan -n
sudo mddctl driver status
```

安装器只有在这组证据成立时才构建 CCID，并且只应用
`patches/ccid/03_scr_prime_reader.patch`。检查元数据：

```bash
sudo jq . /etc/mdd-sim-gateway/scr-prime-driver.json
apt-mark showhold | grep '^libccid$'
```

不要对 SCR Prime 使用 HSIC 的 `01_hsic_slot_status.patch`、
`02_hsic_malformed_atr.patch` 或旧 `patchall` 逻辑。

若要回到发行版驱动：

```bash
sudo mddctl driver restore
```

恢复命令拒绝以下情况：没有 MDD 元数据、备份路径越界、当前 bundle 不存在，或当前哈希与
MDD 安装后哈希不一致。遇到拒绝先保留现场，不要手工覆盖后再伪造元数据。

## 6. SCR Prime reader 可见但没有 ATR

- 确认 SIM 插入方向和接触；
- `pcsc_scan` 应列出 reader，插卡后才出现 ATR；
- 观察 pcscd 日志中 power-on/communication 错误；
- 将 SCR Prime 从 VM 断开再重新连接，确认无需重启客户机即可恢复。

`--require-scr-prime` 会要求真实拔插。`--yes` 不能跳过这项。

## 7. Quectel 在 Windows 中有 COM 口，客户机没有 modem

只传 COM 口不等于传完整复合 USB。客户机验收：

```bash
lsusb
lsusb -t
ls -l /dev/ttyUSB* /dev/cdc-wdm* /dev/wwan* 2>/dev/null
sudo systemctl status ModemManager --no-pager
mmcli -L
sudo journalctl -u ModemManager -n 150 --no-pager
```

应看到多个 tty 和可能的 QMI/MBIM/WWAN 接口，再看到 `/Modem/<n>`。若 Windows 仍占用
某一接口，在 Workstation 中断开并重新连接**整个设备**。

ModemManager unit 应包含 MDD drop-in：

```bash
systemctl cat ModemManager.service
```

它以 `--debug` 开启 command interface，随即 `SetLogging INFO`；持续 DEBUG 日志说明
ExecStartPost 失败，检查 `busctl` 与 D-Bus 服务名。

## 8. ModemManager 有对象，但 4G 没有 bearer/IP

```bash
mmcli -m <n>
mmcli -m <n> --simple-status
nmcli device status
nmcli connection show
ip -br address
ip route
```

检查 SIM 锁定、注册、APN、信号、QMI/MBIM 端口和 NetworkManager GSM profile。不要通过
修改桥接默认路由“修复”蜂窝 bearer；管理流量仍应走桥接网卡，蜂窝数据是对应设备能力。

## 9. WebUI 8443 无法访问

```bash
sudo systemctl status mdd-sim-gateway-control --no-pager
sudo journalctl -u mdd-sim-gateway-control -n 150 --no-pager
curl -k -v https://127.0.0.1:8443/api/auth/status
ss -ltnp | grep ':8443'
```

VM 内可访问、局域网不可访问时检查桥接地址与精确防火墙端口。默认前两条线路：

```text
8443/tcp
8089/tcp, 8099/tcp
10000-10011/udp
12000-12011/udp
```

使用手工端口或自动分配器因冲突跳号时，以实际线路配置为准。

## 10. 本地 Engine 构建失败

首次无缓存构建会下载 Fedora base、Asterisk/pjproject 固定提交和其他依赖。检查：

```bash
docker info
docker pull fedora:44@sha256:6c75d5bf57cb0fa5aa4b92c6a83c86c791644496d9ac230de7711f5b8ec3b898
df -h /
free -h
```

安装器不会自动改 `/etc/docker/daemon.json`。如所在网络需要企业镜像或代理，由管理员按组织
策略配置 Docker 后重试；不要关闭 TLS 校验或把固定源码提交改成未知镜像。

强制完整重建：

```bash
sudo mddctl update --no-cache
```

## 11. update 拒绝 dirty、分叉或 remote

```bash
sudo git -C /opt/mdd-sim-gateway status --short --branch
sudo git -C /opt/mdd-sim-gateway remote -v
sudo git -C /opt/mdd-sim-gateway rev-parse --path-format=absolute --git-path MERGE_HEAD
```

受管工作树不允许 staged、unstaged 或未忽略 untracked。不要用 `mddctl update` 覆盖手工
改动。remote 必须精确为 `https://github.com/suyi-92/mdd-sim-gateway.git`，分支必须为
`vmware`。分叉时工具不会 merge/rebase/reset/force；先在开发仓库处理并推送可快进历史。

早期安装可能仅显示以下两项未跟踪路径，因为当时 `.gitignore` 的尾随 `/` 只匹配目录，不能
匹配正式激活 symlink：

```text
?? .venv
?? webui/dist
```

这类旧安装不能先用旧版 `mddctl update` 取得修复，也不要删除、改写两个链接，不要修改
`/opt/mdd-sim-gateway` 权限或写 `.git/info/exclude`。运行最新流式 bootstrap 的事务更新：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/mdd-sim-gateway/vmware/bootstrap.sh) update
```

bootstrap 会以普通用户下载完整最新源码，再运行下载源码中的新版 `scripts/mddctl update`，而
不是旧 `/usr/local/sbin/mddctl`。新版预检只兼容这两个精确未跟踪 symlink，并核验其绝对 raw
target 与规范化目标都属于当前 HEAD/`active-commit` 的提交专属 build、READY 和完整产物身份。
任一额外文件、tracked/staged/conflict、链接越界或身份不一致都会在 fetch 前停止。成功后新
忽略规则使工作树自然干净；激活或健康失败则由同一事务恢复旧源码、链接、Engine、数据和原
运行状态。不要使用 bootstrap `install` 代替该更新事务。

## 12. update 构建通过但启动失败

工具应自动回滚并输出失败。检查：

```bash
sudo cat /etc/mdd-sim-gateway/active-commit
sudo cat /etc/mdd-sim-gateway/previous-commit 2>/dev/null
sudo mddctl status
sudo mddctl doctor
sudo journalctl -u mdd-sim-gateway-control -u mdd-sim-gateway-orchestrator -n 200 --no-pager
ls -l /var/backups/mdd-sim-gateway/pre-update-*
```

不要在自动回滚中途手工把源码切到新提交，否则会产生“新源码 + 旧服务”的混合状态。

## 13. backup 或 restore 被拒绝

- backup 输出不能在运行数据目录内部；
- backup 不覆盖已有归档或摘要；请换新路径，并且运行数据树不能含 symlink/设备节点；
- 必须有归档和同名 `.sha256`；
- manifest 必须为 `format=1`、`kind=mdd-sim-gateway-data`；
- 归档不能含绝对路径、`..`、symlink、hardlink 或设备节点；
- 所有 SQLite 必须通过 integrity check。

检查摘要：

```bash
sha256sum -c /path/to/backup.tar.gz.sha256
```

恢复失败后，旧数据应仍在 `.pre-restore-<时间>`；不要删除它，先运行 doctor 和查看 unit
日志。备份含明文凭据，只能放到受控加密介质。

## 14. VoWiFi、通话和短信

VoWiFi 链路：

```text
SIM/PCSC → UICC 选择与 AKA → 国家出口 UDP → ePDG/IKE → IMS/SIP → Asterisk/WebRTC
```

按层检查，不要只凭最后一条错误猜原因：

- SIM 的 MCC/MNC、SPN/GID 和运营商品牌；
- SWu/IKE 状态与重传；
- IMS Registration；
- Asterisk/AMI；
- `call_result` 中的 `DIALSTATUS` 与 Q.850。

Q.850 127 是未明确映射的互通失败，不能据此声称“本地未发送”或“运营商确定不支持”。
失败后的 `Return without Gosub` 通常是挂断处理噪声，不是 INVITE 未发出的证据。

CMLink UK 的 `10086` 是其官方短号，保持原样拨打；不要改写成 `+4410086`。判断短号时按
SIM 品牌自己的规则，不只看底层宿主网络。

普通 PC/SC reader 没有 IMEI 时允许注册，Engine 会省略 DEVICE_IDENTITY；若运营商强制
要求，WebUI 会提示风险。读不到 SMSC 时 VoWiFi 通话仍可启动，只禁用主动 VoWiFi 短信。

## 15. Fake-IP 或浏览器只有单向音频

国家出口启用时，host orchestrator 会在出口内部解析 ePDG 并把真实公网地址固定到对应
Engine；如果日志仍显示 Fake-IP，检查 sing-box/Xray 出口状态和 DNS 解析证据。

浏览器 SDP 会过滤 Fake-IP ICE candidate。双向音频仍失败时检查：

- 浏览器麦克风授权；
- WSS 8089/8099；
- 对应 RTP UDP 小范围；
- VM 桥接地址是否与 Control 的 advertise address 一致；
- 是否有 VPN/安全软件改写浏览器路由。

## 16. 重启恢复

分别测试：

1. `sudo reboot` 客户机；
2. 正常关闭客户机后重启 Windows 宿主机；
3. 打开 VM，观察两台 USB 是否按设备规则重新连接；
4. 运行 `mddctl doctor`；
5. 确认两条线路自动恢复。

USB 未自动连接属于 VMware 配置，不要通过放宽成“所有新 USB 自动连接”规避；只修复两个
确定设备的连接规则。
