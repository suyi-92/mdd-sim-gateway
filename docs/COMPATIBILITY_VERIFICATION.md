# Docker 兼容与离线设备显示修复记录（2026-09-05）

## 1. 基线与发现

开发 checkout：`/home/suyi/src/mdd-sim-gateway-dev`，分支 `vmware`，基线 HEAD `1becf43efb634097d905902b7c2eb84f3fc6e8e9`。检查了根 AGENTS.md、README、安装/开发/排障文档与相关测试。已有未跟踪 `chatgpt_amd64.deb` 保持原样并排除于修复提交之外。后续交付已获用户授权提交、推送及 MDD 受管更新；开发修改仍仅在开发 checkout 进行。

`install.sh:install_packages()` 原先无条件安装 docker.io，并 `enable --now docker.service`，Docker 验证直接使用调用者的 context。`mddctl` 的 CLI 调用及 Control 的 `docker.from_env()` 也可能连接到不同 daemon。

## 2. 修改与设计

- `scripts/docker-local.sh` 是正式安装路径使用的状态判断与 APT 保护函数；健康 CE/docker.io 复用，正常停止服务只启动，masked/failed/不完整/不明安装失败停止。rootless 与健康 rootful 共存可复用；已移除包仅有配置残留不会遮蔽已核实的正常 daemon。
- 确认完全没有本机 Docker 或不明容器运行时/数据残留后，才模拟 APT 并以 `--no-remove` 首次安装 docker.io。通用依赖的模拟计划不得改变已有 Docker/runtime。
- `install.sh`、`scripts/mddctl` 和 `control/app/{engine,operations,runtime,sysinfo}.py` 统一连接本机 `unix:///var/run/docker.sock`。CLI 显式固定 endpoint/default builder，SDK 传入仅含本机 host 的连接环境；不修改用户默认 context，HTTP 代理不清空。
- 保留 Docker 配置、数据、权限与其他项目资源。未修改 SIM/USB/CCID/VoWiFi 业务；Git 来源、vmware/clean、更新事务、校验和、管理网络、Engine 身份/TUN/NET_ADMIN 门禁保留。
- 设备页与概览共用实际连接状态过滤；当前设备拔出后切换到其他已连接设备或空状态。离线历史记录需显式勾选显示，不删除 SIM/线路或硬件设置。版本更新到 `1.7.0-vmware.7`。
- 移除 `MANAGED_CHECKOUT_STATUS_KIND` 的未使用赋值，直接运行原检查并保留失败退出；原 Git/legacy activation/dirty 安全测试继续执行。
- README、INSTALL、DEVELOPMENT 与 TROUBLESHOOTING 同步说明安装顺序、异常诊断、设备历史与回归方式。

## 3. 实际验证

系统 Python 初次全量运行因缺少 fastapi/docker 等依赖失败，未计为通过。之后建立临时 venv，按仓库锁定依赖重新运行：

```bash
python3 -m venv /tmp/mdd-docker-network-compat-venv
/tmp/mdd-docker-network-compat-venv/bin/pip install -r control/requirements.txt httpx
/tmp/mdd-docker-network-compat-venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

结果：**1096 项，OK，2 项跳过**；跳过项是共享 Docker 测试中仅属于 bootstrap 的来源选择测试，在仓库 B 执行。包括原有 46 项 `test_vmware_install_contract.py`。新增测试实际运行正式 Docker helper 和 `install_packages()`，使用命令 mock 校验参数、连接环境、来源保持、停止服务启动、异常与 APT 冲突时的副作用。

以下检查通过：

```bash
for script in bootstrap.sh install.sh scripts/mddctl engine/entrypoint.sh scripts/docker-local.sh; do bash -n "$script"; done
python3 -m compileall -q control engine host scripts tests
sh tools/check-subscriber-identifiers.sh
shellcheck -S warning -e SC1091 scripts/mddctl install.sh scripts/docker-local.sh
(cd webui && npm ci && npm run build)
git diff --check
```

`scripts/mddctl` 原有 SC2034 已修复，相同 ShellCheck 命令包含该文件后通过。Git 安全测试仍覆盖正常/legacy activation、staged、tracked、额外 untracked、conflict 与活动版本校验。

额外执行 `node --test tests/webui_device_presence.mjs`：7 项行为测试通过。Playwright 对本地构建与虚构 API 执行真实浏览器回归，验证拔出后的选择切换、全部拔出、历史记录、重新接入、设置保留、零写请求及 1440/900/390px 视口；不使用生产账号或真实设备身份。

## 4. 未执行的真实 VMware 验收

源码验证期间未运行完整安装器、真实 APT 安装/卸载、网络切换或 VM 重启；后续只执行用户授权的 MDD 受管更新及健康检查，其结果单独报告。以下仍需专用环境验证：双 VM 同 LAN/异网段；真实 CE/docker.io 两种安装顺序与重复运行；其他项目容器连续性；停止服务启动、APT 维护脚本行为；无缓存 Engine 构建、实际 TUN/NET_ADMIN 能力与硬件链路。WebUI 本地构建和 mock 测试不能替代这些项目。

## 5. 使用与升级

新系统：通过本项目正常 bootstrap install 入口安装；仅空白 Docker 环境会安装 docker.io。已有 CE 或 docker.io：保持原包和代理，正常入口直接复用；异常状态按 TROUBLESHOOTING 诊断，不默认卸载重装。

已有受管 MDD：仍使用 `sudo mddctl update` 的受管事务，不能在 /opt 开发或通过 install 绕过更新门禁。源码经验证和推送后，通过该受管事务交付。重复运行不在 CE 与 docker.io 间切换。

与新版 bootstrap 共存：先 bootstrap 安装 CE → MDD 复用；先 MDD 安装 docker.io → bootstrap 保留。管理地址继续采用独立 VM MAC 与路由器 DHCP 保留；本次不改变 MDD 的管理地址/default-route 保护。

## 6. 剩余限制

支持可核实的本机 rootful CE/docker.io 和标准 root-owned socket endpoint；不自动迁移自定义、Desktop、rootless-only 或其他未知布局。双 VM 与真实 USB 拔插验收尚待专用环境；本次授权的推送、MDD 更新与运行检查在最终交付时分别报告。
