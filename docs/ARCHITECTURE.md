# VMware 架构

## 运行边界

```text
Windows / VMware Workstation
│
├─ Bridged vNIC ── router DHCP reservation
├─ USB passthrough: SCR Prime 04d9:c001
└─ USB passthrough: complete Quectel composite device
   │
   ▼
x86_64 Linux guest
├─ pcscd + libccid
│  ├─ SCR Prime physical PC/SC reader
│  └─ vsmartcard VPCD slots for the modem SIM
├─ ModemManager + NetworkManager
│  └─ Quectel modem, registration, GSM profile, bearer and IP
├─ mdd-sim-gateway-control.service
│  ├─ FastAPI / HTTPS / WebUI
│  ├─ persistent configuration and SQLite
│  └─ Docker API for managed Engine containers
├─ mdd-sim-gateway-orchestrator.service
│  ├─ hardware discovery and VPCD bridges
│  ├─ sing-box/Xray country exits
│  └─ desired/observed state reconciliation
└─ rootful Docker
   ├─ Engine line 1: SCR Prime SIM, VoWiFi-only
   └─ Engine line 2: Quectel SIM, VoWiFi plus separate 4G bearer
```

Control 永远在客户机本机运行，不存在 Docker Control 分支。Engine 需要独立网络命名空间、
TUN、NET_ADMIN、Asterisk 和每线路端口，因此继续使用容器。

## 数据与构建

```text
/opt/mdd-sim-gateway                  managed Git checkout
/var/lib/mdd-sim-gateway              runtime and secrets
/var/backups/mdd-sim-gateway          root-only data backups
/etc/mdd-sim-gateway                  machine/network/driver metadata
/var/cache/mdd-sim-gateway/builds     locally verified builds by commit SHA
```

WebUI 和 venv 通过 symlink 指向一个通过门禁的提交构建目录；Engine 使用
`mdd-sim-gateway/engine:<commit>`，验证后再更新 `:latest`。新构建不会先覆盖运行版本。

## 设备模型

物理设备和 SIM 线路分离：

- SCR Prime 是 `reader`，没有 IMEI 和蜂窝射频也合法。DEVICE_IDENTITY 可省略；若运营商
  强制要求，线路会由运营商拒绝而不是由本地固定假 IMEI。
- Quectel 是 `modem`，硬件 IMEI、蜂窝能力和 SIM 逻辑通道来自 ModemManager/AT 路径。
- `max_sim_lines` 是唯一容量来源，默认 13、范围 1–32。API、自动建线和 Engine 启动均调用
  同一配置；降低上限不删除已有记录。

UICC 选择器统一处理 APDU `61xx`、`9Fxx` 和 `6Cxx`，支持嵌套 EF_DIR，并严格选择 USIM
AID。PIN keeper、IKE/EAP-AKA 和 SIP/IMS-AKA 共用该选择逻辑，避免三条路径对同一张卡得出
不同 applet。

## 网络

桥接 vNIC 是管理平面默认路由。NetworkManager 只新增或保留蜂窝管理，不接管原本由其他
backend 管理的桥接网卡。蜂窝 bearer 是设备能力，不应替换管理默认路由。

每条 Engine 使用独立的 WebRTC 与小范围 RTP block。分配器同时探测 TCP 和 UDP 真实占用，
避免 Docker 创建后才发现冲突。国家出口按 SIM 国家建立独立 TUN，UDP 健康失败时该线路
fail-closed，不退回错误国家的默认网络。

宿主解析器返回 Fake-IP 时，orchestrator 在已选出口内解析真实 ePDG 地址，并把该地址固定到
对应 Engine。WebUI 软电话同时过滤 SDP 中的 Fake-IP candidate。

## SCR Prime 驱动决策

```text
USB absent ── require flag? ── stop / warn
USB present
  └─ pcsc_scan sees SCR Prime ── native, no hold
     └─ not seen ── CCID 1.6.2 + patch 03 only
                    ├─ backup + metadata + hash
                    ├─ maintenance marker
                    ├─ conditional libccid hold
                    └─ reader + ATR + hotplug gates
```

`mddctl driver restore` 是带证据的逆操作，不是无条件包重装。系统/项目更新会临时重探测
当前发行版 libccid；原生支持出现后自动回归发行版文件。

## 更新事务

```text
validate managed checkout
  → fetch origin/vmware
  → require fast-forward
  → temporary worktree
  → static tests + new venv/WebUI/Engine build
  → data backup
  → stop Control/orchestrator/managed Engines
  → ff-only source switch + atomic artifact activation
  → health gates
       success: keep one previous generation
       failure: old commit + artifacts + image + data, then revalidate
```

没有 GitHub Release API、网页更新按钮、后台轮询、`software_update` 通知或自动合并策略。

## 备份事务

备份停止写入方，确认 Engine 全停，执行 SQLite WAL checkpoint 和 integrity check，再把数据、
secret-free manifest 与外部 SHA-256 一起生成。恢复在临时目录验证所有归档成员，拒绝路径穿越
和链接/设备节点，保留 `.pre-restore-*` 后才原子替换；健康失败逆转数据切换。

## 权限与隐私

- Control 和 orchestrator 以 root + `UMask=0077` 运行，因为它们需要 PC/SC、Docker 和网络；
- 数据、驱动元数据和备份默认为 root-only；
- Docker 操作只针对 `io.mdd-sim-gateway.managed=true` 的 Engine；
- `doctor --json` 不读取或输出 SIM 身份、硬件 IMEI、号码、消息或凭据；
- 机器 USB/网络/防火墙状态不进入数据备份。
