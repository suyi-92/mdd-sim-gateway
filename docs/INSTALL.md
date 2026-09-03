# VMware 安装、更新与迁移

本文只描述 `vmware` 分支。它不支持 WSL/usbipd、ARM64、Docker Control、GitHub Release
资产或云端编译。

## 1. 固定部署模型

```text
Windows x86_64 + VMware Workstation
└─ x86_64 Linux 客户机（桥接网络）
   ├─ systemd: mdd-sim-gateway-control
   ├─ systemd: mdd-sim-gateway-orchestrator
   ├─ rootful Docker: 每条 SIM 一个 Engine
   ├─ USB: SCR Prime 04d9:c001
   └─ USB: Quectel 类复合蜂窝模块
```

支持的客户机版本只有：

- Ubuntu 24.04
- Ubuntu 26.04
- Debian 12
- Debian 13

其他发行版、架构、容器客户机和非 systemd init 会明确停止。

## 2. 创建 VM

建议值：

- 4 vCPU；
- 8 GiB RAM；
- 64 GiB 动态磁盘；
- 一张桥接网卡；
- USB 3.1 控制器。

硬门禁：RAM 少于 4 GiB、根文件系统可用空间少于 12 GiB或根文件系统总容量少于
20 GiB 时停止。RAM 少于 8 GiB或可用空间少于 25 GiB 时警告。

扩大 VMware 虚拟磁盘后，还要在客户机内扩展分区、LVM（如有）和文件系统：

```bash
lsblk -f
findmnt /
df -hT /
```

目标设备名必须来自当前 `lsblk`；不要照抄别人的 `/dev/sda3`。安装器以 `df` 看到的根
文件系统为准。

## 3. 桥接网络与 DHCP 保留

1. Workstation 中选择 Bridged，不使用 NAT。
2. 记录 VM 网卡 MAC。
3. 在路由器中为该 MAC 创建 DHCP 地址保留。
4. 客户机启动后记录：

   ```bash
   ip -4 route show default
   ip -4 route get 1.1.1.1
   ip -br address
   ```

安装器在安装 NetworkManager 前也会记录默认网卡、网关、源地址和 JSON 快照：

```text
/etc/mdd-sim-gateway/network/
```

如果主网卡原本不由 NetworkManager 管理，安装器写入
`/etc/NetworkManager/conf.d/90-mdd-cellular-only.conf`，只让 NetworkManager 管理 GSM
设备。如果主网卡本来就由 NetworkManager 管理，则保留原管理方式。安装后默认网卡、网关
或源地址变化会移除 MDD 策略、恢复原后端状态并停止，不继续部署一个可能失联的网关。

## 4. USB 直通

在 Workstation 图形界面中完成：

- 连接三体电子 SCR Prime `04d9:c001` 到客户机；
- 连接**整个** Quectel USB 复合设备到客户机；
- 为这两个具体设备启用随 VM 连接；
- 不启用“所有新 USB 自动连接”；
- 确认 Windows VMware USB Arbitration Service 正常；
- 确认连接到 VM 后 Windows 不再占用设备。

只透传某个 Windows COM 口不能提供 USB 控制、QMI/MBIM、WWAN 和全部 tty 接口。

客户机中的预安装检查：

```bash
lsusb -d 04d9:c001
lsusb
lsusb -t
```

## 5. 一键安装

以普通用户执行：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/mdd-sim-gateway/vmware/bootstrap.sh) install --require-scr-prime --require-cellular
```

安全边界：

1. 流式 bootstrap 不接受 root 身份；
2. 先集中执行一次 `sudo -v`；
3. Git 源码由普通用户克隆到 `mktemp` 目录；
4. 核对 remote 精确等于 `https://github.com/suyi-92/mdd-sim-gateway.git`；
5. root 只执行已下载到本地的 `install.sh`。

### 参数

| 参数 | 作用 |
|---|---|
| `install` | 全新安装或幂等重复安装 |
| `update` | 下载完整最新源码并运行其中的新版 `scripts/mddctl update` |
| `doctor` | 转发到已安装的 `mddctl doctor` |
| `--install-dir PATH` | 受管源码，默认 `/opt/mdd-sim-gateway` |
| `--data-dir PATH` | 运行数据，默认 `/var/lib/mdd-sim-gateway` |
| `--ref vmware` | 安装当前远端分支 |
| `--ref <40 位 commit>` | 安装精确提交；提交必须可获取 |
| `--require-scr-prime` | SCR Prime 全链路任一门禁失败即停止 |
| `--require-cellular` | `mmcli -L` 无 modem 即停止 |
| `--configure-firewall` | 明确授权写入精确 MDD 规则 |
| `--no-start` | 构建并安装 unit，但不启动服务 |
| `--dry-run` | 只显示参数和预检意图 |
| `--yes` | 接受普通确认；不跳过硬门禁 |
| `--no-cache` | 仅 update 使用，强制 Engine 无缓存构建 |
| `--json` | 仅 doctor 使用，输出脱敏 JSON |

路径必须是绝对非根路径，不能包含换行或空白。

## 6. 安装阶段

### 6.1 预检

- `uname -m == x86_64`；
- systemd 是 PID 1；
- `/dev/net/tun` 是字符设备；
- 8443/TCP 未被非 MDD 进程占用；
- rootful Docker；
- RAM、根文件系统和空间达到门槛；
- 虚拟化类型不是 VMware 时警告。

### 6.2 发行版软件包

使用 apt 安装发行版版本的：

```text
docker.io
modemmanager
network-manager
pcscd / pcsc-tools / libccid
python3 / python3-venv / build-essential
Git / curl / wget / jq
PCSC、USB、OpenSSL、libcurl、autotools、Meson、Ninja 编译依赖
```

安装器不会写 `/etc/docker/daemon.json`，不会切换 Docker 数据目录，不会删除非 MDD 容器、
镜像或卷。rootless Docker 会停止，因为 Engine 需要 TUN、NET_ADMIN 和主机 PC/SC socket。

ModemManager drop-in 使用 `--debug` 开启受保护的 command interface，启动后立即通过 D-Bus
把运行日志降回 INFO：

```text
/etc/systemd/system/ModemManager.service.d/90-mdd-command-interface.conf
```

### 6.3 固定依赖

安装器按固定版本和 SHA-256 获取 sing-box、Xray-core、CCID、vsmartcard、lpac 和必要时的
CMake。Engine Dockerfile还会按固定提交取得 Asterisk 与 pjproject。首次安装需要网络，不是
离线安装；固定版本和摘要用于阻止上游文件被静默替换。

### 6.4 SCR Prime

流程：

1. `lsusb -d 04d9:c001`；
2. 启动 pcscd；
3. 有超时的 `pcsc_scan -n`；
4. 能列出 SCR Prime：记录 `native`；
5. USB 可见、PC/SC 不可见：下载 CCID 1.6.2，验证 SHA-256，只应用
   `03_scr_prime_reader.patch`，在临时 DESTDIR 构建；
6. 备份原 `ifd-ccid.bundle`，原子替换并记录 root-only JSON 元数据；
7. 只有确实替换发行版 bundle 时才 `apt-mark hold libccid`；
8. 发布 `orchestrator/pcsc-maintenance` 后重启 pcscd；
9. 再次确认 reader；普通安装或 `mddctl driver install` 在未插卡时警告并继续；
10. 显式 `--require-scr-prime` 时还必须确认 ATR，并按终端提示拔出、确认 USB 消失、重新连接
    并确认 PC/SC 自动恢复。

若首次安装时设备尚未直通，之后 USB 已可见但 PC/SC 不可见，不要重跑完整安装。执行：

```bash
sudo mddctl driver install
```

该命令在驱动变化前验证受管 checkout 与完整 active generation，并复用相同的备份、原子替换、
哈希记录和失败回滚。

Engine 不是独立运行 pcscd，而是访问宿主 `/run/pcscd`。pcsc-lite 的客户端/daemon socket
协议并不保证任意版本互通；不匹配时 `SCardEstablishContext` 会返回 service stopped，即使主机
`pcsc_scan` 正常。因此 Control 创建 Engine 时还会从 `libpcsclite1` 包清单解析真实库文件，
验证它是 root 所有、不可由组/其他用户写入的 x86_64 ELF，再只读覆盖容器的
`libpcsclite.so.1`。验证失败时在创建容器前停止，不从任意路径加载库。

补丁元数据：

```text
/etc/mdd-sim-gateway/scr-prime-driver.json
/etc/mdd-sim-gateway/driver-backups/<UTC 时间>/
/etc/mdd-sim-gateway/scr-prime-mode
```

元数据包含设备 ID、CCID 版本、唯一补丁、usbdropdir、包版本、备份路径和安装前后哈希，
不包含 SIM 身份。

### 6.5 Quectel

验收顺序：

```bash
lsusb
lsusb -t
ls -l /dev/ttyUSB* /dev/cdc-wdm* /dev/wwan* 2>/dev/null
mmcli -L
mmcli -m <对象>
nmcli device status
```

`--require-cellular` 最多等待约 90 秒让 ModemManager 完成探测。必须看到 `/Modem/` 对象。
后续在 WebUI 中建立第二条 SIM 线路并打开 4G，NetworkManager 才按线路配置建立 GSM profile
和 bearer。

### 6.6 本地构建与原子切换

- Control：临时 venv，按 `control/requirements.txt` 安装；
- WebUI：固定到 amd64 manifest digest 的 `node:22.14.0-bookworm-slim` 容器中
  `npm ci && npm run build`；
- Engine：`mdd-sim-gateway/engine:<40 位提交>`；
- 构建目录：`/var/cache/mdd-sim-gateway/builds/<提交>/`；
- 激活：原子切换 `/opt/mdd-sim-gateway/.venv` 和 `webui/dist` symlink，再把已验证 Engine
  标为 `mdd-sim-gateway/engine:latest`。

Engine 门禁：

- Architecture 为 amd64；
- OCI source revision 等于当前提交；
- OCI product version 等于仓库 `VERSION`，镜像 ID/大小与构建 manifest 一致；
- runtime/base fingerprint 与当前源码一致；
- Asterisk 可执行并有合理模块数量；
- `jinja2`、`requests`、`pyscard`、`cryptography` 可导入；
- Control venv 通过 `pip check`，正式 WebUI 树哈希与构建 manifest 一致；
- 最小容器在 `/dev/net/tun + NET_ADMIN` 下可创建并删除 TUN。

## 7. 目录和服务

| 路径 | 内容 |
|---|---|
| `/opt/mdd-sim-gateway` | 受管 Git 工作树 |
| `/var/lib/mdd-sim-gateway` | 数据库、配置、证书、日志、线路状态、凭据 |
| `/var/backups/mdd-sim-gateway` | 默认备份位置 |
| `/etc/mdd-sim-gateway` | root-only 机器状态、网络和驱动元数据 |
| `/var/cache/mdd-sim-gateway` | 本地构建、源码依赖缓存、临时 update worktree |
| `/usr/local/sbin/mddctl` | 唯一管理入口 |

systemd units：

```text
mdd-sim-gateway-control.service
mdd-sim-gateway-orchestrator.service
```

安装后：

```bash
sudo mddctl status
sudo mddctl doctor
sudo mddctl doctor --json
```

然后在受信 LAN/VPN 打开 `https://<DHCP 保留地址>:8443` 并创建至少 10 字符的管理员密码。

## 8. 防火墙

默认前两条自动分配线路：

```text
8443/tcp
8089/tcp, 8099/tcp
10000-10011/udp
12000-12011/udp
```

未指定 `--configure-firewall` 时不写防火墙。安装器会读取已有线路，并用与自动建线相同的
TCP/UDP 占用探测预测不足两条时的端口块；如果 UFW 已启用，授权后只添加这份精确清单。
如果是自定义 nftables，安装器把 MDD 规则保存在 `/etc/mdd-sim-gateway/mdd-sim-gateway.nft`
并明确提示把它接入发行版持久化策略；管理员仍应复核自己的 chain/priority 语义。

线路使用手工 SIP base 或自动分配器跳过冲突端口时，端口会变化；以 WebUI 中保存的实际
port block 和 `mddctl doctor` 为准，再调整规则。

## 9. 更新

```bash
sudo mddctl update
sudo mddctl update --no-cache
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/mdd-sim-gateway/vmware/bootstrap.sh) update
```

前两条用于管理入口已经是当前版本的正常更新。若旧版 `mddctl` 因历史激活 symlink 忽略规则而
无法启动更新，使用第三条：bootstrap 会以普通用户完整下载最新 `vmware`，核对源码身份后运行
下载源码中的新版 `scripts/mddctl update`，但仍操作同一个受管 checkout 并复用下述事务。
`install` 不会快进已有正式 checkout。

预检：

- 受管目录是 Git 工作树；
- origin URL 精确匹配；
- 当前分支是 `vmware`；
- 没有 staged、unstaged、未忽略 untracked、冲突或进行中的 Git 操作。
- 当前 HEAD 与 `active-commit` 一致，`.venv` 与 `webui/dist` 都是指向
  `/var/cache/mdd-sim-gateway/builds/<HEAD>/{venv,webui}` 的安装器绝对 symlink；
- 当前 READY、manifest、venv/WebUI、提交专属 Engine 与 stable Engine 身份全部有效。

然后：

1. 在 dry-run、fetch 和 no-op 判断前完成上述 active-generation 验证；
2. `fetch origin vmware`；
3. 当前 HEAD 等于远端：只重探测 SCR Prime 原生驱动并结束；
4. 当前 HEAD 必须是远端祖先，否则停止；
5. 在临时 worktree 执行 Bash 语法、Python compile、单元测试和全部本地构建；
6. 重探测发行版 SCR Prime 驱动；
7. 创建更新前数据备份并保留旧 Engine tag；
8. 停止 Control、orchestrator 和全部带 MDD Engine 标签的运行容器；
9. `merge --ff-only origin/vmware`；
10. 激活已验证产物并启动；
11. 检查 HTTPS、systemd、Docker/TUN 和必需硬件。

任何激活/健康失败都会在确认工作树仍干净后：

- 把受管 checkout 回到旧提交；
- 激活旧 venv/WebUI；
- 恢复旧 Engine tag；
- 恢复更新前数据快照；
- 按更新前的 active/inactive 状态恢复服务；原先两项服务都运行时再执行完整旧版本健康验证。

成功后保留一代旧提交身份、Engine 和数据备份。构建缓存不会被更新命令自动批量删除。

## 10. 备份、恢复和整机迁移

```bash
sudo mddctl backup
sudo mddctl backup --output /mnt/encrypted/mdd-data.tar.gz
sudo mddctl restore --input /mnt/encrypted/mdd-data.tar.gz
```

备份：停止服务和 Engine，SQLite WAL checkpoint + integrity check，排除 cache/update/tmp 以及
精确的 `instances/<实例>/run/` 进程临时树，拒绝其他位置的符号链接和特殊文件，生成 tar.gz、
`.sha256` 和 manifest，然后恢复原运行状态。

恢复：要求归档与同名 `.sha256`；拒绝绝对路径、`..`、symlink、hardlink、设备节点、错误
manifest 和损坏 SQLite；现有数据先移动到 `.pre-restore-<时间>`，新数据从临时目录原子替换。
健康失败会把失败数据另存并恢复旧数据。

WebUI 的“系统设置 → 备份与更新”可创建默认目录中的本地备份，并从通过 owner-only 普通文件、
同名摘要和安全名称筛选的列表中恢复。页面不会接受任意主机路径；Control 只提交请求，实际事务
由独立 systemd 任务调用 `mddctl`，所以停止 Control 与 orchestrator 不会杀死正在执行的备份。
恢复要求二次确认，会中断通话并在完成后要求重新登录。需要直接写入加密移动介质时仍使用上述
CLI `--output` 命令。

备份含明文凭据，只能放在受控加密介质。跨电脑首选停止 MDD、关闭 VM 后复制/导出整个
VM，因为 USB 自动连接、VM MAC、DHCP 保留和防火墙属于机器状态，不在数据归档内。

## 11. 卸载

```bash
sudo mddctl uninstall
sudo mddctl uninstall --purge
```

普通卸载保留数据与备份。`--purge` 要求输入 `PURGE`，删除源码、缓存、运行数据和备份。
卸载只删除带 MDD 标签的 Engine 容器/镜像和 MDD 自有 unit/规则；不卸载 Docker，不处理
其他项目。若 MDD 修改过 SCR Prime 驱动，会先按元数据和哈希安全恢复发行版 libccid。

## 12. 四发行版验收矩阵

每个系统必须分别记录：

| 门禁 | Ubuntu 24.04 | Ubuntu 26.04 | Debian 12 | Debian 13 |
|---|---:|---:|---:|---:|
| 全新一键安装 | 待实机 | 待实机 | 待实机 | 待实机 |
| 重复安装 | 待实机 | 待实机 | 待实机 | 待实机 |
| 无更新 / 快进 update | 待实机 | 待实机 | 待实机 | 待实机 |
| 构建/健康失败回滚 | 待实机 | 待实机 | 待实机 | 待实机 |
| 桥接地址与默认路由不变 | 待实机 | 待实机 | 待实机 | 待实机 |
| SCR USB / PCSC / ATR / 热插拔 | 待实机 | 待实机 | 待实机 | 待实机 |
| Quectel modem / bearer / IP | 待实机 | 待实机 | 待实机 | 待实机 |
| 两线路并发 IMS/通话/音频 | 待实机 | 待实机 | 待实机 | 待实机 |

只有真实完成后才能把对应“待实机”改为“通过”。
