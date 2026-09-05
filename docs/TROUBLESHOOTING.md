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

若 USB 已出现而 PC/SC reader 仍不存在，使用受管的后插设备修复入口：

```bash
sudo mddctl driver install
```

该入口先验证正式 checkout、active-commit、激活链接、build manifest 和 Engine 身份；任一
证据异常都会在驱动变化前停止。验证通过后才复用安装器的驱动事务，且只应用
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

`mddctl driver install` 在没有 ATR 时只警告，因为驱动安装不应依赖卡片在场；reader 不可见仍
会失败。首次安装显式使用 `--require-scr-prime` 时会继续强制 ATR 和真实拔插，`--yes` 不能
跳过这项。

在 WebUI 连续保存同一条原生 reader 线路时，Control 会重建该线路的 Engine。正常重建必须先
有序停止旧容器、等待其 PC/SC 连接全部关闭，并经过短暂的 reader 静默窗口后再启动新容器；不得
直接强制删除正在执行 PIN、IKE 或 IMS-AKA APDU 的旧容器。若旧版本在保存后出现
`Card absent or mute`，先在 VMware 中重新连接该 reader 恢复现场，再更新到包含有序停止保护的
版本；不要反复保存或重启 pcscd。

新卡第一次完成 IMS 注册后，运营商可能才在 `P-Associated-URI` 中下发可拨号码。Control 需要
有序重建一次 Engine，使最终注册、主叫身份与拨号计划使用该权威身份。这不是掉卡或自动恢复；
WebUI 在整个收敛窗口内应显示“正在确认 IMS 身份”，只在重注册稳定后显示“已开启”。同一张卡若
连续发生两次以上身份重建，才按异常重连检查 Control 日志。

若主机 `pcsc_scan` 正常，而 Engine 的 `pin_status.json` 只报告
`Failed to establish context` / `Service was stopped`，检查容器是否同时只读挂载宿主
`libpcsclite.so.1`。这通常是容器 client 与宿主 pcscd 私有 IPC 版本不兼容，不是 SIM 或 CCID
补丁失败；不要把主机 socket 暴露给任意版本的容器客户端，也不要手工复制未知库。正常修复走
源码更新，由 Control 从受支持发行版的 `libpcsclite1` 包清单验证并挂载匹配库。

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

界面中的“蜂窝数据（4G）”只控制 NetworkManager 数据 bearer，不控制基站注册。若只需模块保持
注册到蜂窝网络而不使用移动数据，应关闭飞行模式并关闭蜂窝数据；模块射频保持开启，但受管
数据 profile 会持久设为 `autoconnect=no`，重启或重新插拔模块后也不得抢先自动建立数据连接。

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
- backup 不覆盖已有归档或摘要；请换新路径。每条线路精确的 `instances/<实例>/run/` 是不恢复的
  进程临时树，其中的 SWu 控制 FIFO 会被排除；其他位置的 symlink、FIFO、socket 或设备节点
  仍会拒绝；
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

若旧版本更新在切换前报 `unsupported data path type: instances/<实例>/run/swu.ctl`，说明旧归档
helper 把运行时 FIFO 当成持久数据。不要手工删除 FIFO 或重复运行旧 `mddctl update`；使用最新
流式 bootstrap `update`，让下载源码中的新版 manager 与新版归档 helper 一起完成事务更新。

设置页的备份/恢复任务会短暂断开 WebUI，因为 `mddctl` 必须停止 Control、orchestrator 和
Engine。页面恢复后需重新登录。若任务没有被接取或失败，检查有界的
`mdd-sim-gateway-data-<操作 ID>.service` journal；不要绕过页面去改 `/var/lib` 或伪造摘要。

## 14. VoWiFi、通话和短信

如果国家出口测试的 DNS 与 STUN 目标全部超时，但同一节点的 TCP 连接正常，先区分临时节点
测试和常驻的 sing-box → Xray bridge。Xray-backed REALITY/XHTTP 出口应在 TUN 启动前经
宿主直连 TCP DNS 固定节点拨号 IPv4，并继续保留原始 REALITY/TLS server name；旧实现让
Xray 在 TUN 启动后再发 UDP DNS，查询会被送回同一 bridge，表现为节点可连但所有 UDP 探针
沉默超时。不要手工编辑 `sing-box.json`/`xray.json`、删除测试或改成直连绕过；更新到包含该
保护的版本后重新运行国家出口 UDP 测试。取证时只比较 listener、relay、传输类型和通过/失败，
不得输出节点地址、server name、链接或凭据。

单次失败后立即重试成功通常是冷启动 UDP generation 的首代丢包，不能只因一次 503 就判定节点
不支持 UDP。新版测试会完整尝试每个配置的 DNS/STUN 目标，整轮静默后再做一次有界确认；
两轮全部超时才报失败，并保留每个目标的最终错误。

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

CMLink UK 的 `10086` 是其官方语音短号，保持原样拨打；不要改写成 `+4410086`。它属于
home-local number：只有 SIM 的 CMLink SPN 与共享 PLMN 同时匹配时，Engine 才在 IMS
Request-URI 中保留原短号并附加归属域 `phone-context`。该域从成功 REGISTER 返回的
`P-Associated-URI` 中与当前电话身份对应的 SIP URI 学习，严格校验后内部保存并重建该线路。
MVNO 可以同时使用标准 3GPP 认证 realm 和另一个电话身份域；若仍用前者作 `phone-context`，
上游可在 INVITE 后立即拒绝。SIP 形式的 home-local Request-URI 将该受验证域同时用于
`phone-context` 和 host 部分；该 host 还要由 Engine 的受管 resolve 条目固定到本次注册使用的
P-CSCF。缺少这条映射会让显式 URI 绕过认证域已有的 P-CSCF 映射，并可能在真正送达 IMS 前立即以
`CHANUNAVAIL` 结束。不要手工填写或打印学到的域名。该短号接通的是带音频的客服通话，不是 USSD；
不要隐藏麦克风/扬声器/DTMF，也不要把相同规则套到普通 EE 或其他 MVNO。

CTExcel 同样使用 EE 的 `234-33`，但普通英国 E.164 去话走另一条号码分析路径。只有同时匹配
该 PLMN 与 CTExcel SPN 时，Control 才为号码型 SIP Request-URI 启用 `user=phone`；缺少该参数
时可表现为 IMS 先返回 `100 Trying`，随后立即以无详细原因的 `487 Request Terminated` 结束。
部分 IMS 注册还会把 IMSI 派生的 IMPU 放在 `P-Associated-URI` 第一项，把号码对应的 `tel:` 与
`sip:` 公共身份放在后面。Engine 外呼必须从本次注册返回的身份中选择第一个带 E.164 号码的
公共身份，并在同一号码同时有 SIP 形式时优先保留其归属域；不能照搬第一项或从本地配置猜造
身份，否则 P-CSCF 虽已接受 INVITE，后续 TAS 仍可能立即终止呼叫。
英国号码应写成 `+44` 加去掉首个国内长途 `0` 后的号码，例如手机使用 `+447…`，不能写成
`+4407…`。CTExcel 的普通号码路由不得获得 CMLink 专属的 `10086`/`phone-context` 规则。

若浏览器显示“通话已拒接”，先对照通话记录的最终状态。出站 VoWiFi 未接通时，Asterisk 会在
本地 WebRTC leg 合成 `603 Decline` 来阻止 JsSIP 自动重拨；它不等于运营商真的返回拒接。
Control 的 `call_result` 才是界面最终结论。需要区分上游响应时，只短暂开启 PJSIP logger，
复现一次后立即关闭，并且只保留脱敏的消息方向、状态码和 URI 形状。

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

“浏览器听得到对端，但对端听不到浏览器”只证明 IMS 下行、Asterisk 转码和浏览器播放链路
可用，不能证明麦克风上行已经到达运营商。先确认浏览器为当前站点选择了正确且未静音的输入
设备；Engine 的 IMS endpoint 同时必须保持 `direct_media=no` 与 `rtp_symmetric=yes`。前者让
Asterisk 留在 WebRTC/AMR 两条媒体腿之间，后者从已收到的下行 RTP 学习运营商媒体中继实际使用的
地址与端口，并将浏览器麦克风的上行媒体送回同一位置。运营商 SDP 声明值与实际发包位置不一致时，
缺少对称 RTP 会形成“本端能听、对端静音”的已接通通话。

## 16. 重启恢复

分别测试：

1. `sudo reboot` 客户机；
2. 正常关闭客户机后重启 Windows 宿主机；
3. 打开 VM，观察两台 USB 是否按设备规则重新连接；
4. 运行 `mddctl doctor`；
5. 确认两条线路自动恢复。

USB 未自动连接属于 VMware 配置，不要通过放宽成“所有新 USB 自动连接”规避；只修复两个
确定设备的连接规则。

## 已有 Docker 异常时停止安装

先查看已有安装，而不是默认卸载重装：

```bash
sudo systemctl status docker.service --no-pager
sudo journalctl -u docker.service -n 60 --no-pager
dpkg-query -W docker-ce docker-ce-cli docker.io containerd.io containerd
sudo env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH docker --host unix:///var/run/docker.sock info
```

若 ExecStart 使用 `-H fd://`，还需检查 `docker.socket`；健康的其他监听布局不强制采用相同 socket unit。masked/failed 必须先查明原因；CLI-only、rootless-only、远程、Desktop、Podman 或遗留数据不能当成空白系统。安装器不会自动卸载、迁移、unmask、reset-failed 或重启健康 daemon。无需更改用户默认 context，HTTP 代理仍可保留。

本次兼容修复仅经隔离命令 mock、静态检查与仓库回归验证。真实双 VMware 安装顺序、已有容器持续运行、CE/docker.io 停启与重启恢复，以及 Engine 镜像构建/TUN/NET_ADMIN 验收须在专用 VM 执行；不得把单测当作实机证据。

## 已拔出的设备仍显示

默认设备列表只显示 `present` 未被明确标记为 false 的设备。后端保留历史记录用于重连恢复，不表示物理设备仍在线；勾选“显示已断开的设备”后才显示这些记录。取消勾选后应隐藏离线记录，全部拔出时应显示空状态。若更新后仍显示旧界面，刷新页面，必要时重新登录。

`1.7.0-vmware.7` 修复了此前仅在概览和侧栏过滤离线设备、设备页仍显示旧卡片的问题。回归包含当前设备拔出后的选择切换、全部拔出、重新接入、显式查看历史记录，以及 1440/900/390px 浏览器检查。浏览器测试使用虚构 API 数据，不替代真实 USB 拔插验收。
