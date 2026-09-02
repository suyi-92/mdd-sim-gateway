# MDD Sim Gateway VMware 分支协作说明

## 适用范围

本文件适用于整个仓库。进入目录后如有更近的 `AGENTS.md` 或
`AGENTS.override.md`，优先遵守更近文件中的专用规则。

- `vmware` 是面向 VMware Workstation Linux 客户机的部署分支。
- 正式支持范围为 x86_64 的 Ubuntu 24.04/26.04 和 Debian 12/13。
- Control 与 WebUI 使用本机 Python venv + systemd；只有每条 SIM 的 Engine 使用
  rootful Docker。
- 首次安装和更新均在客户机本地从源码构建，不依赖 GitHub Actions、GitHub Release、
  预编译项目归档、Git LFS 资产或 Docker Control。
- 开始工作前阅读 `README.md`；安装、开发和排障分别以 `docs/INSTALL.md`、
  `docs/DEVELOPMENT.md`、`docs/TROUBLESHOOTING.md` 为准。

## 正式部署与开发目录

正式部署和开发必须使用两个独立 checkout，不得把运行目录当成开发目录。

| 路径 | 用途与权限 |
|---|---|
| `/opt/mdd-sim-gateway` | root 管理的正式 Git checkout；当前安装器以 `umask 077` 创建，普通用户不能进入 |
| `~/src/mdd-sim-gateway-dev` | 推荐的普通用户开发 checkout；在用户 `suyi` 的 VM 中解析为 `/home/suyi/src/mdd-sim-gateway-dev` |
| `/var/cache/mdd-sim-gateway` | root-only 构建缓存、提交专属产物和 update 临时 worktree |
| `/var/lib/mdd-sim-gateway` | root-only 数据库、证书、日志、线路状态和凭据 |
| `/var/backups/mdd-sim-gateway` | root-only 数据备份 |
| `/etc/mdd-sim-gateway` | root-only 安装、网络、驱动及当前版本元数据 |

- Codex 必须以普通用户在开发 checkout 中运行；不要以 root 启动整段 Codex 会话。
- 每次编辑前运行 `git rev-parse --show-toplevel`。若根目录解析为
  `/opt/mdd-sim-gateway`，停止源码编辑，只允许经用户请求执行只读诊断或
  `sudo mddctl ...` 管理命令。
- 不得对 `/opt/mdd-sim-gateway` 执行 `chown`、宽泛 `chmod`，也不得为了方便编辑将其
  复制成会被 systemd 直接运行的第二套正式目录。
- `/opt/mdd-sim-gateway` 显示 `drwx------ root:root` 或图形文件管理器提示“不是 root”是
  当前预期权限，不是安装损坏。需要核对正式源码时使用有界的
  `sudo git -C /opt/mdd-sim-gateway ...` 只读命令。
- 不直接修改 `/var/lib`、`/var/cache` 或 `/etc/mdd-sim-gateway` 中的生成状态来模拟修复。
  配置变更走 WebUI/API，生命周期操作走 `mddctl`，驱动恢复走
  `mddctl driver restore`。
- 普通代码更新不需要重新执行流式安装。开发 checkout 提交并推送 `vmware` 后，在
  正式系统执行 `sudo mddctl update`；只有全新机器安装或已安装管理入口无法恢复时才重新
  执行 bootstrap。
- `mddctl update` 只获取已推送的 `origin/vmware`。开发 checkout 中未推送的文件不会被
  正式系统看到。

## 开发前 Git 检查

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short
git log --oneline -5
git remote -v
```

- 修改源码时使用明确的可交付分支。日常获取 `origin/vmware` 只允许快进；发生分叉时停止，
  不自动 rebase、普通 merge、reset 或 force push。计划内同步新上游版本的普通 merge 是单独
  流程，不能拿来绕过日常分叉。
- 已存在未提交变化时先确认其来源并保留，不清理、覆盖或替换用户现场。
- 提交和推送是两个独立授权；用户未明确要求时不要自行提交或推送。
- 提交前检查 staged、unstaged、删除和未跟踪文件；不得遗漏，也不得把被忽略的运行数据
  误报为已清理。
- 不要创建 partial/promisor clone 作为安装器的本地 Git 传输源。bootstrap 必须保留完整的
  `vmware` 可达对象；`--filter=blob:none` 曾导致本地 `upload-pack` 无法补取对象并报
  `could not fetch ... from promisor remote`、`bad pack header`。
- `VERSION` 使用 `<上游版本>-vmware.<修订>`，例如 `1.7.0-vmware.1`。同步新上游版本时使用
  普通 merge 保留祖先关系，再按代码块迁移本分支修复，不重置历史。

提交信息使用以下格式，标题和条目必须来自实时差异，不写空泛总结：

```text
【苏忆】<本次提交主题>

1. ...
2. ...
```

正文保留 1–5 个编号条目。已获提交与推送授权时，先提交并确认提交存在于
`origin/vmware`，再报告交付完成；永不自动 force push。

## 不得回退的产品边界

- `max_sim_lines` 默认 13、合法范围 1–32；设置加载、API、自动建线和所有 Engine 启动入口
  必须共用同一解析逻辑。降低上限保留已有记录，但阻止超限线路启动。
- UICC 必须继续处理 `61xx`/`9Fxx` continuation、`6Cxx` 长度纠正和嵌套 EF_DIR；严格 USIM
  AID 选择器由 PIN、IKE、SIP 三条路径共用。
- 普通 PC/SC reader 可以没有 IMEI；缺少 SMSC 只能禁用主动 VoWiFi 短信，不能阻止通话。
- 端口分配同时探测真实 TCP 与 UDP 占用；Engine 启动失败必须清理 Docker `Created` 残留。
- eSIM profile 首次启用时继续等待陈旧 `disabled` 出口状态刷新。
- Fake-IP 环境下，ePDG 必须在选定国家出口内解析并固定真实地址；浏览器 SDP 必须过滤
  Fake-IP ICE candidate。
- CMLink UK 的 `10086` 保持原样，不改写为 `+4410086`；短号判断以 SIM 品牌自身规则为准。
- 保留 Feishu 通知、IKE SA rekey、原生 reader recovery、Xray 错误反馈和通知代理路由。
- 不重新引入 WSL/usbipd/PowerShell、Docker Control、Release API、网页自动更新、工作流或
  项目预编译资产。

## 修改与测试门禁

先运行与变更直接相关的测试，再在支持的 Linux 客户机中执行完整基础门禁：

```bash
bash -n bootstrap.sh install.sh scripts/mddctl engine/entrypoint.sh
python3 -m compileall -q control engine host scripts tests
python3 -m unittest discover -s tests -p 'test_*.py'
sh tools/check-subscriber-identifiers.sh
cd webui
npm ci
npm run build
```

- 全量 Python 测试必须以 Linux 结果为准。Windows 直接运行会因 Linux 专用依赖、
  `os.getloadavg`、Bash 路径语义、默认编码及 SQLite 文件锁产生环境性失败，不能据此修改
  产品逻辑或宣称 Linux 回归失败。
- 临时测试依赖使用隔离 venv；不要把本机 `.venv`、`__pycache__`、Node 模块或构建目录加入
  Git。
- 只修改 Markdown、`AGENTS.md` 或忽略规则时，不要求重新构建 Engine；仍需执行
  `git diff --check` 和针对文档约束的搜索检查。
- 修改 Control、host orchestrator 或安装/更新逻辑时，至少运行对应单元测试及
  `tests/test_vmware_install_contract.py`；涉及真实 systemd、Docker、网络或 USB 的结论还要在
  支持的 VM 中验证。
- 修改 WebUI 时必须按锁文件执行 `npm ci && npm run build`。异步状态应区分 loading、已确认
  空状态、运行状态和请求失败，不能在请求期间闪现虚假的“关闭/未配置”。
- WebUI 记录布局优先使用 `min-width:0`、行内值和容器查询；长节点名、诊断及敏感字段在自身
  单元内省略并保留可访问的完整说明，不得重新引入固定 1280px 页面宽度、额外结果行、重叠
  或无语义黑色投影。至少检查宽、中、窄三种容器宽度。
- 修改 Engine、`engine/Dockerfile`、Engine patch 或运行层输入后，必须至少完成一次 amd64
  无缓存构建，并核对源码提交、产品版本、Architecture、runtime/base fingerprint、Asterisk、
  模块数量、Python 依赖以及 `/dev/net/tun + NET_ADMIN` 最小容器门禁。
- 不得用“镜像构建成功”替代单元测试，也不得用单元测试替代 SCR Prime、Quectel、IMS、通话
  和双向音频实机验收。

## 安装器与构建经验

以下问题都曾在真实本地构建中发生，相关保护不得删除：

- `install.sh` 使用 `set -Eeuo pipefail`。可选分支不能用会返回非零的裸算术命令或
  `condition || return` 结束；默认只打印防火墙端口的路径必须显式 `return 0`，否则安装会在
  启动 systemd 前提前退出却看起来像正常结束。
- `PCSC_VERSION` 是 readonly。调用指纹脚本时使用
  `env PCSC_VERSION="$PCSC_VERSION" sh ...` 的子进程边界，不在当前 Bash 中用前缀赋值，避免
  `PCSC_VERSION: readonly variable` 和随后的 `prepared build identity verification failed`。
- Engine Docker build context 必须是 `engine/`，确保入口脚本、模板和 Asterisk patch 可见；
  `engine/.dockerignore` 必须排除 Python 缓存。
- Fedora 构建与运行阶段保持固定基础镜像摘要和完整元数据校验。`dnf` 当前禁用 zchunk，以避免
  某些代理/镜像链路破坏分片元数据；不能通过关闭 TLS、删除摘要或换成不明镜像绕过下载失败。
- Control venv 在临时目录创建后会移动到提交专属 build root。移动后必须重写 `pyvenv.cfg`
  与 console-script shebang，执行 `pip check`，再写 `READY`；失败时移除 `READY`。不要把可移动
  venv 当成普通目录直接 `mv` 后即宣称可用。
- 默认重试应复用已经完成的 Docker layer 和有效提交构建；只有显式 `--no-cache` 才强制完整
  重建。失败产物不得覆盖带 `READY` 的最后成功代。
- lpac 的构建能力检查必须使用 `LPAC_APDU=stdio LPAC_HTTP=stdio ... driver list`，验证 PC/SC
  与 curl driver 已编入，但不能在基础安装阶段实际访问读卡器。
- WebUI 在固定 Node 镜像的临时目录中执行 `npm ci && npm run build`，成功后才原子切换；不能
  把开发目录的 `webui/dist` 当作正式产物。
- 正式 checkout 中的 `.venv` 与 `webui/dist` 是安装器原子激活到
  `/var/cache/mdd-sim-gateway/builds/<提交>/` 的 symlink；`.gitignore` 必须用限定仓库位置且
  同时匹配目录和 symlink 的规则，不能使用只匹配目录的尾随 `/`。若旧安装因此把这两个链接
  报为未跟踪，只能运行最新流式 bootstrap 的 `update`；bootstrap 以普通用户下载完整最新源码，
  再运行其中的新版 `scripts/mddctl update`，复用临时 worktree、备份、激活和完整回滚事务。
  不得改用 `install` 绕过更新事务，不得手工删除或改写链接，也不得修改 `/opt` 权限、写本地
  Git exclude 或放宽 `mddctl update` 的 clean 门禁。

## 安装与启动诊断

- 不带 `--require-scr-prime` 和 `--require-cellular` 时，硬件缺席只警告并立即继续；不得等待
  90 秒或失败。只有明确要求的门禁才等待设备并在缺失时停止。
- `less than 8 GiB RAM` 是性能警告，不是安装错误；少于 4 GiB RAM、12 GiB 可用空间或
  20 GiB 根文件系统才是停止条件。
- `systemctl is-active` 为 active 不代表 HTTPS 已监听。安装、start、restart、restore 和 update
  的健康门禁需要等待最多 30 秒，让证书与应用初始化完成；`mddctl doctor` 仍是即时快照。
- 如果服务 active 但 `curl https://127.0.0.1:8443/api/auth/status` 暂时 connection refused，先
  等健康窗口并查看 Control journal，不要立刻重装。
- 安装返回后若服务 inactive，先检查安装日志是否在“只打印防火墙端口”后提前结束，再查看：

```bash
sudo systemctl --no-pager --full status \
  mdd-sim-gateway-control.service mdd-sim-gateway-orchestrator.service
sudo journalctl -u mdd-sim-gateway-control -u mdd-sim-gateway-orchestrator \
  -n 200 --no-pager
```

- `mddctl status` 用于概览，`mddctl doctor` 用于即时健康快照，`mddctl logs` 用于有界日志。
  需要机器可读结果时优先使用 `mddctl doctor --json`。

## SCR Prime 与 Quectel

- SCR Prime 固定为 `04d9:c001`，只提供 PC/SC/VoWiFi，没有 4G 开关。
- VMware 必须直通完整 USB 设备。依次证明 `lsusb`、pcscd reader、`pcsc_scan` 和插卡 ATR；
  USB 可见不等于 PC/SC 可见，reader 可见也不等于卡已插入。
- 系统 libccid 已识别时保持 native；否则只构建固定 CCID 并应用
  `03_scr_prime_reader.patch`。严禁应用 HSIC 专用 patch、`patch2` 或 `patchall`。
- 驱动操作先发布 PC/SC maintenance marker；只有 MDD 元数据和当前哈希证明文件由本项目修改
  时，`mddctl driver restore` 才能恢复、解除 hold 并重启 pcscd。
- Quectel 必须把完整 USB 复合设备连接到 VM，不能只传 Windows COM 口。验收顺序为
  `lsusb/lsusb -t`、tty/WWAN、`mmcli -L`、modem/SIM、NetworkManager GSM profile、bearer/IP。
- NetworkManager 只管理蜂窝设备，不能接管桥接管理网卡；默认路由或 SSH 地址变化必须回滚并
  停止。VM 管理地址继续使用桥接网络和路由器 DHCP 保留，不在客户机写死静态地址。

## 国家出口与界面诊断

- 原生 Control 的国家出口诊断 SOCKS listener 使用 `127.0.0.1`。不要恢复 Docker Control
  时代的 `172.17.0.1` 默认值；状态发布和通知代理回退必须使用一致的 loopback 路径。
- “代理库节点 UDP 测试通过”只证明临时测试链可用，不证明已保存国家出口的常驻
  sing-box/Xray 链路可用。若国家出口的 DNS/STUN 全部超时，应优先检查常驻出口状态、
  inbound/outbound 与 SOCKS UDP ASSOCIATE 返回地址，而不是立即判定节点不支持 UDP。
- 国家出口测试会交错尝试 DNS 和 STUN，并为每个目标建立独立 UDP ASSOCIATE；任一目标响应
  即通过。诊断时保留每个目标的失败信息。
- 运行证据位于 `/var/lib/mdd-sim-gateway/orchestrator/` 下的 `proxy-status.json`、
  `sing-box.json` 和 `xray.json`。这些文件可能包含私有节点、服务器地址或凭据；只提取所需
  字段并脱敏，禁止整文件粘贴到对话、Issue、测试或提交。
- UI 必须分别呈现“已保存分配”和“运行节点”。测试 busy/result 要保留在对应记录内，不能只
  依赖会消失的 toast；敏感信息开关关闭时不得通过 tooltip、ARIA 文本或错误详情泄漏节点值。

## 更新、回滚与数据安全

- 正常正式管理只使用 `/usr/local/sbin/mddctl`；旧版管理入口无法取得自举修复时，唯一例外是
  最新流式 bootstrap `update` 运行其刚下载且已核验的 `scripts/mddctl`。不要在正式 checkout
  手工执行 pull、merge、reset 或直接替换 `.venv`/`webui/dist` symlink。
- `mddctl update` 预检要求正式 checkout 的 origin URL、`vmware` 分支、Git 操作状态和工作树
  完全符合受管元数据；还必须在 dry-run、fetch 和 no-op 判断前验证 HEAD、`active-commit`、两个
  激活 symlink 的绝对 raw target 与规范化目标、READY/manifest、venv/WebUI 和 Engine 身份。
  只允许当前 HEAD 到 `origin/vmware` 的快进。
- 新版本必须先在临时 worktree 完成静态检查、单元测试和构建，然后创建数据快照、停止服务、
  快进源码并切换已验证产物。健康失败时恢复旧源码、venv/WebUI、Engine tag、数据快照和更新前
  的服务运行状态。
- 自动回滚期间不要手工切换源码或重启部分服务，避免留下“新源码 + 旧服务”的混合状态。
- backup/restore 必须校验 SHA-256、manifest、归档路径安全和 SQLite；备份含明文凭据，只能存放
  在 BitLocker、加密移动盘或其他受控介质。
- 不删除最后一代已验证 build、Engine 镜像或更新前数据快照来换取临时磁盘空间；清理必须通过
  明确的管理操作并先确认当前运行身份。

## 隐私与日志

- 不把真实 IMSI、ICCID、IMEI、电话号码、用户名、密码、token、私有代理 URL/SNI、消息正文
  或完整生成配置写入测试、文档、提交信息和公开日志。
- 测试身份只能使用仓库约定的虚构范围；提交前运行
  `sh tools/check-subscriber-identifiers.sh`。
- `doctor --json` 被设计为可分享的布尔健康摘要；其他 journal、数据库、support bundle 和
  orchestrator 文件仍需逐项脱敏。
- 诊断拨号失败时按 SIM 品牌/MCC-MNC、SWu、IKE 重传、IMS Registration、Asterisk/AMI、
  `call_result` 的 `DIALSTATUS + Q.850` 顺序取证。Q.850 127 只是未明确映射的互通失败；
  `Return without Gosub` 通常是挂断噪声，不能据此宣称 INVITE 未发送。

## 完成标准

- 变更范围与用户请求一致，没有夹带运行数据、生成物、秘密或无关清理。
- `git diff --check` 通过，相关测试和本文件要求的构建门禁通过。
- 代码测试、VM 运行验证和真实硬件验收分别报告；没有执行的层级明确写为“未验证”，不得相互
  替代。
- 若已部署，确认 `sudo mddctl doctor`、HTTPS、systemd、Engine image identity 和 TUN；涉及
  SCR Prime、Quectel、国家出口或通话的改动还需执行对应实机链路。
- 最终报告保持简洁：先给结果，再列验证与仍需实机确认的事项；失败时只附最相关的有界日志。
