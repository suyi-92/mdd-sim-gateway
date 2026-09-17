<p align="center">
  <img src="assets/logo-lockup.svg" width="520" alt="MDD Sim Gateway">
</p>

<p align="center"><strong>在 VMware Linux 客户机中，以本地源码构建方式运行两条 SIM 通信线路。</strong></p>

<p align="center">
  <a href="README.en.md">English</a> ·
  <a href="#先安装后接设备推荐">快速开始</a> ·
  <a href="docs/INSTALL.md">完整安装说明</a> ·
  <a href="docs/TROUBLESHOOTING.md">故障排查</a> ·
  <a href="docs/upstream-sync/README.md">上游同步维护</a> ·
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
2. 让受管驱动入口探测刚接入的 SCR Prime：

   ```bash
   sudo mddctl driver install
   ```

   该命令先验证正式 checkout 和 active generation，再尝试发行版原生驱动；只有 USB 可见而
   PC/SC 不可见时，才事务安装仅含补丁 03 的固定 CCID。没有插卡时只警告，reader 成功枚举后
   仍需插入 SIM 并确认 ATR。Quectel 通常会被 ModemManager 自动热发现。
3. 插卡后用 `pcsc_scan` 确认 reader 和 ATR，断开再接回 SCR Prime 并确认 reader 自动恢复，
   然后运行：

   ```bash
   sudo mddctl doctor
   ```

4. 打开 `https://<VM 的 DHCP 保留地址>:8443`：把 SCR Prime 建成 **PC/SC、VoWiFi-only**
   线路；把 Quectel 建成 **modem、4G + VoWiFi** 线路，并填写该 SIM 的 APN/4G 设置，使
   NetworkManager 建立 GSM profile、bearer 和 IP。

Quectel 热发现后仍需在 WebUI 建线；SCR Prime 后插时运行一次 `sudo mddctl driver install`，
不要为了驱动重跑完整 bootstrap/install。若客户机启用了防火墙，按首次安装器打印的精确端口
放行。

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
3. 复用已有健康的本机 rootful Docker CE 或 docker.io；正常停止的服务只尝试启动。只有确认无本机 Docker 或冲突安装时才经 APT 模拟和禁止移除包保护首次安装 docker.io。
   通用依赖单独安装，不升级或替换已有 Docker，不重启健康 daemon，也不改写 Docker 配置或清理其他项目容器。
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

两个安装器可任意先后运行：bootstrap 首次安装 CE 后 MDD 复用 CE；MDD 首次安装 docker.io 后新版 bootstrap 保留 docker.io。MDD 的构建、管理和运行统一使用 `unix:///var/run/docker.sock`，忽略调用者选择的远程/rootless context，但保留正常 HTTP 代理。不修改用户全局 context。异常状态诊断与升级操作见 [安装文档](docs/INSTALL.md#62-发行版软件包)。

## SCR Prime 驱动策略

SCR Prime 的验收链是：

```text
VMware USB 直通 → lsusb 04d9:c001 → pcscd → pcsc_scan → ATR → 拔插恢复
```

- 系统 libccid 已识别：记录 `native`，不打补丁、不 hold 软件包。
- USB 可见但 PC/SC 不可见：备份现有 bundle，应用补丁 03，记录包版本、备份路径、补丁集
  与安装前后哈希；只有确实覆盖发行版所属 bundle 时才新增 `libccid` hold。
- 驱动重启前发布 PC/SC 维护标记，避免控制面把计划内重载误判成物理拔卡。
- `mddctl driver install` 先验证受管 checkout 和 active generation，再复用相同的固定下载、
  备份、原子替换、哈希证据与失败回滚；用于首次安装完成后才接入 SCR Prime 的机器。
- Engine 同时只读挂载宿主 `libpcsclite1` 提供的客户端库。pcsc-lite 的 socket IPC 会拒绝不兼容
  的新旧版本，不能只挂 `/run/pcscd` 后假设容器内任意版本客户端都能与宿主 daemon 通信。
- `mddctl update` 会重新探测发行版驱动；若已经原生支持 SCR Prime，则恢复发行版版本并
  解除 hold，否则继续使用经过哈希验证的补丁版本。
- `mddctl driver restore` 只有在 root 元数据证明文件由 MDD 修改且当前哈希仍匹配时才执行。

查看状态或恢复：

```bash
sudo mddctl driver status
sudo mddctl driver install
sudo mddctl driver restore
```

## 设备拔出与历史记录

概览、设备页默认列表和侧栏计数只显示当前物理连接的设备。拔出当前设备后，详情立即跟随剩余设备；全部拔出时显示“未发现通信设备”。飞行模式、蜂窝数据关闭或未插 SIM 不等于设备拔出。

后端保留已知硬件及 SIM/线路设置，重新接入后自动恢复。需要查看或删除离线硬件记录时，在设备页勾选“显示已断开的设备”，再进入对应设备的“硬件”页；隐藏设备不会自动删除配置。

## 日常管理

```text
mddctl status
mddctl doctor [--json]
mddctl start|stop|restart|logs
mddctl update [--no-cache]
mddctl backup [--output PATH]
mddctl restore --input PATH
mddctl reset-admin [--dry-run]
mddctl export-backup --input PATH --output PATH.mddbackup
mddctl import-backup --input PATH.mddbackup
mddctl driver status|install|restore
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

忘记网页管理员密码时，在客户机终端运行 `sudo mddctl reset-admin`，按隐藏提示输入两次
10–256 字符的新密码。该命令核验受管安装，保留管理员名称和网关数据，短暂停止管理服务后
重设密码并撤销旧会话；写入或 HTTPS 健康失败会尝试恢复原认证数据及服务状态。可先运行
`sudo mddctl reset-admin --dry-run`，仅检查恢复前提。

“系统设置 → 备份与更新”中的每个备份现在可以**导出**为单个 `.mddbackup` 迁移包。
在另一台已安装相同或更新 VMware 版本的主机上**导入备份**，通过摘要、manifest、归档安全
及 SQLite 校验后进入本地备份列表，再明确确认**恢复**。导入阶段不会修改活动数据。
迁移包上限为 1 GiB，解包校验上限为 4 GiB / 100,000 项，同时保留至少 1 GiB 主机可用空间。
迁移后使用原主机的管理员密码；USB 直通、主机驱动、桥接网络和 DHCP 保留仍需在目标主机配置。

`mddctl backup` 会停止 MDD，确认 Engine 全停，对 SQLite 执行 WAL checkpoint 与 integrity
check，再生成 root-only `tar.gz`、SHA-256 和不含秘密的 manifest；随后恢复此前运行状态。
`restore` 会校验摘要、manifest、归档路径和 SQLite，把原数据保留为
`.pre-restore-<时间>`，原子替换后执行健康检查，失败自动恢复旧数据。

“系统设置 → 备份与更新”也可以创建本地备份，并从受管备份目录的安全列表中选择恢复。操作仍由
独立 systemd 任务调用 `mddctl`，不会让 Web 进程直接处理任意主机路径；恢复需要二次确认，且
备份和恢复期间网关服务、线路及当前通话会短暂中断。

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

## AI 实时字幕

浏览器 VoWiFi 通话可选显示对方讲话的原文和实时中文翻译。先在“系统设置 → 通话与 VoWiFi”
填写 OpenAI API key 并启用功能；通话接通后点击“字幕”才会开始，关闭字幕或挂断立即结束会话。
蜂窝模块的实验性呼叫没有浏览器音频，因此不提供字幕。

长期 API key 只保存在 root-only 网关配置中，设置 API 不会把它返回浏览器。每次开启字幕时，
Control 向 OpenAI 换取短期客户端密钥，浏览器再把单独的对方 WebRTC 音轨发送到
`gpt-realtime-translate`；本机麦克风不会进入该翻译会话，网关也不保存音频或字幕。此功能需要
浏览器能够直连 OpenAI、账户具备对应 API 权限，并会产生 OpenAI API 费用。AI 连接或识别失败
只关闭字幕，不中断原通话。

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

## 1.9.4 VMware 更新

`1.9.4-vmware.12` 新增通信设备“重新完整检测”：由宿主编排器统一重建 udev、ModemManager、
PC/SC 与每模块 SIM bridge 检测，Control 随后强制重读原生读卡器和虚拟卡槽；检测期间先清除
无法由当前硬件证明的旧 SIM/驻网快照，并在固定反馈区报告完成或失败，不再把网页刷新冒充硬件重扫。

`1.9.4-vmware.11` 修复 Quectel SIM 热拔后的陈旧状态：ModemManager 对象重建或射频动作
失败时仍立即发布当前 `sim-missing` 快照，不再沿用旧 ICCID、驻网和号码；设备蜂窝页同时新增
访问运营商（PLMN）扫描与自动/手动注册，可在关闭数据承载后明确选择中国移动等可用网络。

`1.9.4-vmware.10` 修复蜂窝模块短信存储耗尽：完整短信只有在指纹绑定到确切的持久化历史
记录后才回收模块副本，失败会有界退避重试；历史已删除的旧对象继续等待明确重导入，超过
24 小时仍未完成的网络分段才按陈旧残片清理，避免小容量 `ME` 存储塞满后阻断所有新短信。

`1.9.4-vmware.9` 为蜂窝-only 使用补齐可核验状态和恢复入口：菲律宾 MCC 与 `63` 国家码
同时匹配时，安全补齐 ModemManager 遗失的国际 `+`；设备主状态直接显示归属/漫游注册、制式、
访问网、信号及独立的数据 bearer；短信历史为空时可经前后端双重确认，原子重新导入该线路模块
仍保留的可显示短信，且不会向旧短信补发通知。

`1.9.4-vmware.8` 修复 `.7` 在真实 reader 上引入的 APDU 竞争：已确认卡不再每 60 秒主动重读，
maintenance 与失败重试遇到运行中 Engine 时改用其 `pin_status` 实时 ICCID 证据；只有身份确实
变化时才先停止旧线路并在 reader 空闲后完整重探。首次发现、真实插拔、未确认身份退避、eSIM
显式切换及 VPCD bridge 代次校验继续保留。

`1.9.4-vmware.7` 修复读卡器换卡与 eSIM 切换后的身份和自动恢复协调：在场但未确认的卡片退避重探，
启动失败保留恢复意图，用户停止取消当前代次；基带缓存不再充当实时身份，已验证订阅同步保存线路，
前端拒绝旧会话响应。自动验证及未执行的实机验收见
[本轮修复记录](docs/recovery-repair-2026-09-16.md)。

`1.9.4-vmware.6` 将当前线路旁的详情精简为运营商、线路名称、号码、国家和网络线路五项，
移除重复的承载网络。宽屏不再把五项等分铺满剩余区域，而是从下拉框后紧凑连续排列；中窄屏
继续按三列或两列换行，保留完整号码复制与下拉框末四位脱敏。

`1.9.4-vmware.5` 将“当前 SIM/线路”下拉框严格恢复为 `1.9.4-vmware.2` 的最大 680px
宽度、字段顺序和末四位脱敏号码，不再用新增信息撑宽或解除该控件的脱敏。新增的运营商、
线路名称、完整可复制号码、国家、承载网络和网络线路在宽屏时独立排列于下拉框后方，空间不足
时才移到下一行；概览中的完整号码复制保持不变。

`1.9.4-vmware.4` 将通话和短信的当前线路详情改为横向标签和值，并显示完整号码；线路
下拉选项也不再隐藏号码。通话、短信和概览中的号码现在都可直接点击复制，复制时逐字符保留
`+`、`*` 和 `#`，不会改写拨号串；窄屏布局仍在组件内自适应，不撑宽页面。
更新预检继续严格核对 Engine 镜像 ID、提交标签、版本、来源、双指纹与模块集合，同时兼容
Docker 对同一镜像先后报告压缩或展开 size 的差异，避免把 size 展示口径误判成镜像漂移。
若已安装的旧验证器因此拒绝更新，最新流式 bootstrap 仍先运行旧验证器，随后只允许其已下载、
完整且干净的 Git checkout 中的新版验证器复核同一活动代；两个验证器都拒绝时继续失败关闭。

`1.9.4-vmware.3` 在通话和短信的当前 SIM/线路选择器下方增加固定详情条，同时显示
运营商、线路名称、脱敏号码、国家、承载网络和实际网络线路。线路切换后详情同步刷新；
长节点名在它自己的单元内省略，宽、中、窄容器都不会撑宽页面。

`1.9.4-vmware.2` 将 ePDG 发布的 `no_retry` 拒绝识别为运营商授权/策略错误，
不再将已明确拒绝的线路长期显示为“正在建立隧道”。同一 SIM/PLMN 收到终止性拒绝后，
Control 会保留真实错误并停止 Engine，Engine 内部监督器也不再循环重试；用户手动重试、
换卡或修正运营商开通状态后仍可重新启动。并发识别同一多槽 VPCD 卡时，每次期望状态发布
使用独立临时文件，避免多个自动启动任务争用 `desired.json.tmp` 而使整条线路未恢复。

`1.9.4-vmware.1` 融合上游 1.9.2–1.9.4：支持每条线路自定义且防注入的 SIP User-Agent，
修复蜂窝长短信占位符/拼接重复导入，并按标准 WAP Push 标记执行可关闭、有限次数的 MMS 通知
清理；ICCID 不可读时仅允许用 IMSI 匹配蜂窝短信和通话，可读但不匹配仍拒绝。

该版本同时收紧出口故障归因和未知订阅节点账本，避免无证据换出口；保存当前订阅节点不再重启
共享 sing-box，通知通道使用独立有界线程池。ICCID 在 Control、PIN、SWu、IMS 四条路径都要求
完整 10 字节 BCD 与纯数字结果；蜂窝 profile 默认不自动连接且不成为宿主默认路由，旧 profile
和无端口关闭场景会被纠正。安装器增加 NetworkManager 自动 APN 所需的 provider 数据库，并把
`SWU_TUN_MTU` 传给受管 Engine。

ML307X/DITO 继续复用共享严格 USIM 选择器、已分配 VPCD 槽和桥接身份元数据。没有当前硬件 IMEI
证明时不会直接借用线路旧快照；同一 bridge 生命周期内已经验证的硬件 IMEI 仍可跨短暂读取失败
保留。网页自动更新、Release 选择、Docker Control、Actions/预编译交付和 ARM 专属限制仍不包含。

## 1.9.1 VMware 更新

`1.9.1-vmware.22` 把 WebUI 测试一并复制到固定 Node 构建目录，并在测试目录缺失时立即失败，
防止隔离更新把“0 项测试”误当成通过。

`1.9.1-vmware.21` 把全局软电话首次渲染回归纳入固定 Node 的 WebUI `prebuild` 门禁，确保
依赖安装完成后执行，并兼容更新事务的隔离源码工作树。

`1.9.1-vmware.20` 修复 AI 字幕版本中全局软电话漏导入 React `useCallback` 导致认证后白屏，
并增加实际执行全局软电话首次渲染的回归测试。

`1.9.1-vmware.19` 增加浏览器 VoWiFi 通话 AI 实时字幕：按需把对方独立音轨接入 OpenAI
Realtime Translation，同时显示源语言原文与中文译文；长期 API key 留在 Control，浏览器只取
短期密钥，字幕不落盘且故障不影响通话。

`1.9.1-vmware.18` 统一物理设备名称：大疆模块默认显示为 `DJI/Quectel EC25`，所有
SCR Prime 默认显示为“三体电子 SCR Prime 读卡器”，不再在概览、设备列表、线路选择和
eSIM 选择中混入 PC/SC 序列号或槽号。可在“设备 → 硬件 → 设备名称”设置每台设备的自定义
名称；清空后恢复自动名称。显示名称与底层 reader 身份分开保存，并随同一硬件的 USB 路径迁移。

`1.9.1-vmware.17` 修复模块与读卡器在 PC/SC 维护窗口内重新插拔或换卡后仍沿用旧线路身份：
VPCD 桥接的实时卡身份会重新绑定当前线路，缓存视图不再借用旧卡记录，安全条件满足时自动恢复
当前线路。同一无序列号模块换 USB 路径后，只在硬件 IMEI 和设备策略均一致时合并重复记录。
设置页“重启全部网关服务”改用受管 `mddctl restart`，失败或未真正重启不再永久显示处理中。

`1.9.1-vmware.5` 为被明确拒绝的 PFS CHILD 换密钥增加会话内无 KE 兼容提案，保留原有
加密/完整性算法、默认 PFS 和超时重传；IMS TCP 保活间隔调整为 30 秒。后台轮询超时后
会继续刷新，过期设备列表用黄色提示并保留最后确认的数据。新增受限稳定性日志及
`sudo mddctl stability --hours 24`，用于不同国家 eSIM 的整夜观察；取证方法见
[稳定性排障](docs/TROUBLESHOOTING.md#整夜稳定性观察)。

`1.9.1-vmware.4` 修复旧读卡器绑定导致 eSIM 切换误停其他 SCR 线路、以及新 profile 在自动
绑定前被误报卡不匹配的问题。现有配置随正常发现和启动流程自动纠正，保留线路数据与凭据。

`1.9.1-vmware.1` 融合上游读卡/PIN 兼容、晚到短信补片和提交报告修复，增加多飞书机器人、线路过滤、
改名免重启、最长 365 天保号间隔和国家出口搜索。DITO 的兼容 IKE 套件仅作用于其 PLMN。
Engine 保留 amd64 Opus，并通过 128 项模块清单、指纹及产物身份门禁精简运行镜像。
已有配置、离线硬件记录、原生部署和 `mddctl` 事务更新继续保留。实际硬件与通话验收仍须按下文执行。
