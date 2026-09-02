<p align="center">
  <img src="assets/logo-lockup.svg" width="520" alt="MDD Sim Gateway">
</p>

<p align="center"><strong>在 VMware Linux 客户机中，以本地源码构建方式运行两条 SIM 通信线路。</strong></p>

<p align="center">
  <a href="README.en.md">English</a> ·
  <a href="#先安装后接设备推荐">快速开始</a> ·
  <a href="docs/INSTALL.md">完整安装说明</a> ·
  <a href="docs/TROUBLESHOOTING.md">故障排查</a> ·
  <a href="docs/ARCHITECTURE.md">架构</a>
</p>

## 先安装、后接设备（推荐）

**设备不是基础安装的前提。** `--require-scr-prime` 和 `--require-cellular` 只是“本次安装
必须通过对应硬件验收”的门禁，不是功能开关。不加它们不会关闭任何功能，也不需要以后重装
系统。

设备还没直通给 VM 时，先执行：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/mdd-sim-gateway/vmware/bootstrap.sh) install
```

此时缺少 SCR Prime 或 Quectel 只会打印警告并继续，普通 `install` 不会等待硬件。Control、
WebUI、pcscd、ModemManager、NetworkManager 和 Engine 仍会完成安装。

以后把两台设备接入客户机后，只需完成下面这些步骤：

1. 在 VMware Workstation 中把 SCR Prime 和**整个 Quectel USB 复合设备**连接到客户机，并在
   SCR Prime 中插入 SIM；不能只传 Windows COM 口。
2. 重新执行下面的硬件验收命令：

   ```bash
   bash <(wget -qO- https://raw.githubusercontent.com/suyi-92/mdd-sim-gateway/vmware/bootstrap.sh) install --require-scr-prime --require-cellular
   ```

   重复安装是幂等的；同一提交会复用已经验证的本地构建和 Docker 缓存。SCR Prime 会先尝试
   发行版原生驱动，必要时自动安装仅含补丁 03 的 CCID，并要求验证 ATR 和真实拔插恢复；
   Quectel 最多等待约 90 秒，直到 `mmcli -L` 能看到 modem。
3. 按 SCR Prime 验收过程的提示拔下、重新连接设备，然后运行：

   ```bash
   sudo mddctl doctor
   ```

4. 打开 `https://<VM 的 DHCP 保留地址>:8443`：把 SCR Prime 建成 **PC/SC、VoWiFi-only**
   线路；把 Quectel 建成 **modem、4G + VoWiFi** 线路，并填写该 SIM 的 APN/4G 设置，使
   NetworkManager 建立 GSM profile、bearer 和 IP。

只后插其中一台设备时，只加对应的 `--require-scr-prime` 或 `--require-cellular` 即可。Quectel
通常能被 ModemManager 热发现，但仍需在 WebUI 建线；SCR Prime 则应重新运行验收命令，因为
只有看到真实 `04d9:c001` 后，安装器才能判断是否需要 CCID 补丁。若客户机已启用防火墙，可在
硬件验收命令中再加 `--configure-firewall`，或按安装器打印的精确端口手工放行。

`vmware` 分支面向 Windows x86_64 宿主机上的 VMware Workstation。Control 与 WebUI
在 Linux 客户机中由 systemd 原生运行；只有每条 SIM 的 Engine 使用 rootful Docker。
项目不使用 GitHub Actions、GitHub Release 自动更新、预编译 Control/Engine/WebUI 资产或
Git LFS 交付包。首次安装和后续更新都在客户机本地从当前源码构建。

## 支持范围

| 项目 | 支持范围 |
|---|---|
| CPU | x86_64 / amd64 |
| 虚拟化 | VMware Workstation，桥接网络 |
| 客户机 | Ubuntu 24.04、Ubuntu 26.04、Debian 12、Debian 13 |
| Control / WebUI | Python venv + systemd，本机 8443/TCP |
| Engine | rootful Docker，一条 SIM 一个容器 |
| 智能卡 | 三体电子 SCR Prime `04d9:c001`，一张 SIM，VoWiFi-only |
| 蜂窝模块 | 一个 Quectel 类 USB 复合设备，另一张 SIM，4G + VoWiFi |
| 线路数 | 默认最多 13 条，管理员可设置 1–32；本部署同时运行两条 |

SCR Prime 没有蜂窝射频，因此不会出现 4G 开关。它只把 SIM 暴露为 PC/SC 智能卡，
VoWiFi 认证、通话和可用的短信功能由该路径完成。4G 数据来自另一台 Quectel 类模块。

## VMware 人工前置步骤

安装脚本无法修改 VMware Workstation 图形界面的 USB 和网络设置。启动客户机前完成：

1. VM 网卡使用“桥接”，不要使用 NAT；在路由器中按 VM 网卡 MAC 做 DHCP 地址保留。
2. 建议配置 4 vCPU、8 GiB RAM、64 GiB 动态磁盘；扩大虚拟磁盘后还必须扩展客户机根分区
   和文件系统，以 `df -h /` 为准。
3. VM 设置中启用 USB 3.1 控制器。
4. 从 Workstation 的可移动设备菜单，把 SCR Prime 和**整个 Quectel USB 复合设备**连接到
   客户机；不能只把 Windows COM 口映射进去。
5. 只为这两个确定的设备启用“随虚拟机连接”。不要启用“所有新 USB 设备自动连接”。
6. Windows 的 VMware USB Arbitration Service 必须运行。设备连接到 VM 后，Windows 不应
   再占用对应驱动。

## 安装过程与参数

上面的命令都应在客户机的普通用户终端执行；不要给 `wget` 或整个下载管道加 `sudo`。入口
脚本先以当前用户完整下载 `vmware` 单分支源码，再集中进行一次 `sudo` 权限确认。`install`
从本地文件启动 root 安装器；`update` 运行下载源码中的新版 `scripts/mddctl` 事务入口，因此旧
管理脚本无法自举时也不需要绕过更新门禁。它不会直接以 root 执行网络取得的标准输入。

安装会执行完整 Engine 源码构建。Asterisk、pjproject、pcsc-lite 和 Python 依赖的首次无缓存
构建可能需要几十分钟，具体取决于 CPU、内存、Docker Hub/GitHub 连接和软件源速度。
不要在构建期间关闭终端、暂停 VM 或断开网络。

### 一键入口参数

```text
install | update | doctor
--install-dir PATH
--data-dir PATH
--ref vmware|<40 位 commit>
--require-scr-prime
--require-cellular
--configure-firewall
--no-start
--dry-run
--yes
```

- `--install-dir`：受管 Git 工作树，默认 `/opt/mdd-sim-gateway`。
- `--data-dir`：运行数据，默认 `/var/lib/mdd-sim-gateway`。
- `--ref`：安装 `vmware` 或一个精确的 40 位提交；受管分支仍命名为 `vmware`，后续更新只
  允许快进到 `origin/vmware`。
- `--require-scr-prime`：USB、PC/SC、ATR 或热插拔任一门禁失败即停止。此门禁需要按提示
  实际拔插设备，`--yes` 不会绕过硬件验收。
- `--require-cellular`：ModemManager 未发现模块即停止。
- `--configure-firewall`：仅在明确指定时写入 MDD 自有的精确端口规则；否则只打印清单。
- `--no-start`：完成安装和构建，但不启动 MDD 服务。
- `--dry-run`：显示参数和预检意图，不修改系统。
- `--yes`：接受普通确认，不跳过 Git、网络、校验和、硬件或健康门禁。

## 安装器做什么

安装器会：

1. 只接受上述四个发行版和 x86_64，检查 systemd PID 1、内存、根文件系统、可用磁盘、
   `/dev/net/tun` 和 8443 端口；低于 4 GiB RAM、12 GiB 可用空间或 20 GiB 根文件系统会停止。
2. 在安装 NetworkManager 前记录默认路由、管理网卡、源地址和现有网络后端。若主网卡原本不由
   NetworkManager 管理，只允许它管理 GSM 设备；安装后路由或 SSH 地址变化会回滚并停止。
3. 从发行版安装 `docker.io`、ModemManager、NetworkManager、pcscd、libccid 和编译依赖；
   不替换 Docker daemon 配置，不清理或操作其他项目容器。
4. 固定版本并校验 SHA-256 后安装 sing-box、Xray-core，编译 vsmartcard VPCD 与 lpac。
5. 先检测 SCR Prime 是否已被系统 libccid 原生识别；只有 USB 可见而 PC/SC 不可见时，
   才构建 CCID 1.6.2 并且只应用 `03_scr_prime_reader.patch`。安装器不会对 SCR Prime 使用
   HSIC 的 `01_hsic_slot_status.patch` 或 `02_hsic_malformed_atr.patch`。
6. 在固定版本和 amd64 digest 的 Node 容器中执行 `npm ci && npm run build`，在临时 venv 中安装 Control，构建带
   当前提交 SHA 标签的 Engine；检查 amd64 架构、产品版本、源码和镜像身份、两类指纹、Asterisk、模块数量、
   Python 依赖以及 `/dev/net/tun + NET_ADMIN` 后才切换稳定版本。
7. 安装并启用原生 Control 与 host orchestrator systemd 服务，以及唯一管理入口 `mddctl`。

安装器不会下载项目的 Engine/Control tar 包，不会读取 GitHub Release API，也不会把
`webui/dist`、venv、Node 模块、运行数据或构建缓存提交到 Git。

## SCR Prime 驱动策略

SCR Prime 的验收链是：

```text
VMware USB 直通 → lsusb 04d9:c001 → pcscd → pcsc_scan → ATR → 拔插恢复
```

- 系统 libccid 已识别：记录 `native`，不打补丁、不 hold 软件包。
- USB 可见但 PC/SC 不可见：备份现有 bundle，应用补丁 03，记录包版本、备份路径、补丁集
  与安装前后哈希；只有确实覆盖发行版所属 bundle 时才新增 `libccid` hold。
- 驱动重启前发布 PC/SC 维护标记，避免控制面把计划内重载误判成物理拔卡。
- `mddctl update` 会重新探测发行版驱动；若已经原生支持 SCR Prime，则恢复发行版版本并
  解除 hold，否则继续使用经过哈希验证的补丁版本。
- `mddctl driver restore` 只有在 root 元数据证明文件由 MDD 修改且当前哈希仍匹配时才执行。

查看状态或恢复：

```bash
sudo mddctl driver status
sudo mddctl driver restore
```

## 日常管理

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

通常通过 `sudo mddctl ...` 执行。`doctor --json` 只输出服务、Docker/TUN、SCR Prime 与
蜂窝模块的布尔状态和源码版本，不输出 IMSI、ICCID、IMEI、号码、凭据或消息正文。

### 本地更新与回滚

`mddctl update` 不访问 Release API。它要求 `/opt` 中是受管的 `vmware` 工作树、remote 精确
匹配且没有进行中的 Git 操作，并在 fetch 前验证 HEAD/`active-commit`、两个激活 symlink、
READY/manifest、venv/WebUI 和 Engine 身份。只有当前 HEAD 是远端祖先时才允许 `--ff-only`
更新；分叉、认证失败和网络失败都会停止，不 merge、rebase 或强推。旧管理入口无法更新时，
运行最新流式 bootstrap `update`，由刚下载的新版 `scripts/mddctl` 执行同一事务。

新提交先在临时 Git worktree 中完成 Shell/Python 检查、单元测试、WebUI/venv/Engine 构建。
全部通过后才备份数据、停止服务、快进源码并切换产物。HTTPS、systemd、镜像身份、TUN 或
必需硬件门禁失败时，工具恢复旧提交、旧 venv/WebUI/Engine 和更新前数据快照，再启动旧版。

### 备份与整机迁移

`mddctl backup` 会停止 MDD，确认 Engine 全停，对 SQLite 执行 WAL checkpoint 与 integrity
check，再生成 root-only `tar.gz`、SHA-256 和不含秘密的 manifest；随后恢复此前运行状态。
`restore` 会校验摘要、manifest、归档路径和 SQLite，把原数据保留为
`.pre-restore-<时间>`，原子替换后执行健康检查，失败自动恢复旧数据。

> 备份包包含明文 SIM PIN、通知令牌、代理凭据和其他运行秘密。只能保存到 BitLocker、
> 加密移动盘或其他访问受控的加密介质。私有 Git 仓库不是加密存储。

整机迁移的首选方式仍是：`sudo mddctl stop`，关闭 Linux 客户机，再从 VMware 导出或复制
整个 VM。机器专属的 USB、桥接网卡 MAC、DHCP 保留和防火墙配置不会随数据归档迁移。

## 两条线路与端口

第一条线路使用 SCR Prime，配置为 VoWiFi-only；第二条线路使用 Quectel，配置为
4G + VoWiFi。自动分配器会探测真实 TCP/UDP 占用并为每条线路选择不同端口块。默认前两块为：

```text
8443/tcp              Control/WebUI
8089/tcp, 8099/tcp    WebRTC/WSS
10000-10011/udp       第 1 条线路 RTP/RTCP
12000-12011/udp       第 2 条线路 RTP/RTCP
```

安装器不会宽泛重写 UFW/nftables。以上是无冲突时的默认值；安装时会根据已有线路和真实
TCP/UDP 占用计算两条线路的精确清单。未指定 `--configure-firewall` 时只打印，不写规则。

## 验收状态

仓库内的静态、Python 和 WebUI 门禁可自动运行，但下面各项只能在真实 VMware 客户机和硬件
上确认，不能由源码审查替代：

- 四个发行版分别从全新 VM 安装、重复安装、无更新、真实快进和失败回滚；
- 安装前后桥接地址、默认路由和 DHCP 保留地址不变；
- SCR Prime 的 USB、PC/SC、ATR、拔插和宿主机重启恢复；
- Quectel 的 USB 拓扑、tty/WWAN、ModemManager、NetworkManager bearer 与 IP；
- 两条线路同时 IMS 注册、呼入/呼出、浏览器双向音频及各自可用的短信路径。

完整操作见 [安装文档](docs/INSTALL.md)，逐层诊断见
[故障排查](docs/TROUBLESHOOTING.md)。

## 安全与使用边界

- 仅供号码实名持有人在法律、运营商和套餐明确允许的范围内自用；不得用于诈骗、群呼、
  验证码收集、线路出租、代拨转接或向第三方提供电信服务。
- AKA 密钥始终留在 SIM/eSIM 内；项目不读取或保存 Ki/OP/OPc。
- `max_sim_lines` 默认 13、合法范围 1–32。降低上限保留已有记录，但超限线路不能启动。
- 支持包和 `doctor --json` 必须脱敏；分享日志和截图前仍需人工复核。
- Control 默认使用自签名 HTTPS。首次打开 `https://<VM 保留地址>:8443` 后立即创建管理员。

本项目以 GPL-3.0-only 发布。CCID 补丁属于 CCID 的 LGPL-2.1-or-later 衍生内容，详见
[patches/ccid/README.md](patches/ccid/README.md)、[NOTICE](NOTICE) 和
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。
