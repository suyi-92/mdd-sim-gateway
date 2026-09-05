# VMware 分支开发规范

`vmware` 是部署分支。它以同步后的上游版本为基线，并使用
`<上游版本>-vmware.<修订>`，例如 `1.7.0-vmware.1`。同步下一上游版本时先做普通 merge，
再按代码块移植本分支修复；不重置分支、不把 Windows/WSL 提交整体 cherry-pick 进来。

## 产品边界

- 客户机：x86_64 Ubuntu 24.04/26.04、Debian 12/13；
- Control/WebUI：本机 venv + systemd；
- Engine：rootful Docker；
- 更新：干净受管 checkout 到 `origin/vmware` 的 `--ff-only`；
- 构建：客户机本地源码构建；
- 无 GitHub Actions、Release API、网页更新、预编译项目资产、Docker Control、WSL/usbipd；
- SCR Prime 自动路径只能应用 `03_scr_prime_reader.patch`；
- Engine 的 PC/SC socket 与客户端库必须成对来自宿主：容器只读挂载经过包路径、root 权限和
  x86_64 ELF 验证的 `libpcsclite1` 文件，不能依赖跨版本私有 IPC；
- `max_sim_lines` 默认 13、范围 1–32，并由所有入口共用。

## 开发前检查

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short
git log --oneline -5
```

不要在受管部署目录中开发；`mddctl update` 会拒绝任何非忽略变化。开发使用独立 checkout 或
worktree，再推送 `vmware`。

## 变更原则

- 保留上游 Feishu、IKE SA rekey、原生 reader recovery、Xray 错误反馈和通知路由；
- 修改 UICC 时同时验证 PIN、IKE 和 SIP 三条路径；
- 修改线路上限时同时验证 API、自动建线和所有 Engine 启动入口；
- 修改端口时同时探测 TCP 与 UDP，并覆盖 `Created` 容器清理；
- 修改运营商数字语音短号时，同时覆盖严格的 PLMN+SPN 匹配、Engine contract、渲染后的
  Request-URI 和 WebUI 最终状态；home-local 号码必须携带同一注册的 `P-Associated-URI` 中与该号码
  绑定的、经 DNS 形状校验的 `phone-context`，不能用通用认证 realm 猜测。显式 SIP URI 的 host
  使用该域时，还必须由受管 PJSIP resolve 条目固定到同一注册的 P-CSCF，不能绕过 IMS 下一跳走
  普通 DNS。它仍是有音频的普通通话，不能复用 USSD 的无音频交互；
- 修改异步操作界面时，覆盖列表中的单记录 busy 归属、固定反馈槽、按钮宽度，以及宽/中/窄
  容器下的对齐；结果出现时不能让旁边的开关、选项和按钮移位；
- 不把运行数据、`.venv`、`node_modules`、`webui/dist`、build cache 或备份加入 Git；
- 正式 `.venv` 与 `webui/dist` 是指向提交专属 build cache 的激活 symlink；忽略规则必须限定
  仓库位置并同时匹配真实目录和 symlink，不能写成只匹配目录的尾随 `/` 形式；
- 不添加 Engine/Control/WebUI tar 包或 Git LFS 资产；
- 不把 IMSI、ICCID、IMEI、号码、凭据、私有 URL 或消息正文写入测试和文档。

## 本地门禁

在支持的 Linux 客户机中执行：

```bash
bash -n bootstrap.sh install.sh scripts/mddctl engine/entrypoint.sh
python3 -m compileall -q control engine host tests
python3 -m unittest discover -s tests -p 'test_*.py'
sh tools/check-subscriber-identifiers.sh
cd webui
npm ci
npm run build
```

Engine、Dockerfile、patch 或运行层输入变化时，还必须至少执行一次 amd64 无缓存构建，并验证：

- source revision、version、Architecture；
- runtime/base fingerprint；
- Asterisk 和模块数量；
- Engine Python 依赖；
- `/dev/net/tun + NET_ADMIN` 最小容器。

只修改 Markdown 或忽略规则时不要求 Engine 构建。

## 安装器测试

`tests/test_vmware_install_contract.py` 固定以下边界：

- 普通用户下载、一次 sudo、本地 root 脚本；
- 四发行版与 x86_64；
- native Control / Docker Engine；
- 临时构建与原子切换；
- SCR Prime 原生优先和补丁 03 only；
- SCR Prime 后插安装入口在任何驱动变化前验证 active generation，且无卡时只跳过 ATR 门禁；
- Engine 创建参数包含宿主 PC/SC socket 及匹配的只读发行版 client library；不受管、可写或
  非 x86_64 ELF 路径必须在创建容器前拒绝；
- NetworkManager 默认路由保护；
- Git 精确 remote、clean、fast-forward-only；
- backup/restore 校验和与路径安全；精确排除每条线路的 `run/` 临时树，并继续拒绝其他位置的
  FIFO、socket、symlink 与设备节点；
- WebUI 数据操作只发布封闭的本地归档名称，由 orchestrator 通过独立 systemd transient unit
  调用 `mddctl`，动态测试必须证明没有 shell 或任意主机路径注入；
- doctor JSON 不含用户身份字段；
- 仓库无 workflows、Release manifest 和 LFS。

旧安装若因历史目录式忽略规则仅把 `.venv` 与 `webui/dist` 激活 symlink 报为未跟踪，兼容入口
只能是最新流式 bootstrap 的 `update`。bootstrap 下载完整最新 `vmware` 源码并运行其中的新版
`scripts/mddctl update`，由同一更新事务在 dry-run/fetch/no-op 前验证两个精确路径、绝对 raw
target、规范化目标、HEAD/`active-commit`、READY 与完整产物身份；其他 dirty 状态和 Git 操作
继续 fail closed。`install` 对已有正式 checkout 只接受同一提交的幂等重装，不负责快进更新。

脚本行为测试应通过 fake command PATH 或临时目录覆盖包管理器、systemctl、Git、Docker、
lsusb、pcsc_scan、mmcli 和网络输出。测试不得真实修改开发机 systemd、驱动或防火墙。

## 四 VM 和硬件验收

源码测试不代表硬件通过。发布前按 `docs/INSTALL.md` 的矩阵逐台执行：

- fresh/repeat install；
- no-op/fast-forward/failed update rollback；
- 默认路由、SSH 地址、DHCP 保留；
- SCR USB/PCSC/ATR/hotplug；
- Quectel tty/WWAN/modem/bearer；
- 两线路 IMS、呼入呼出、双向音频、短信；
- VM reboot 与 Windows host reboot。

结果和发行版镜像、内核、libccid、ModemManager、NetworkManager、Docker 版本一起记录。未执行
项目保持“待实机”，不得写成通过。

## 提交与交付

本仓库在 Server 超级项目中是独立子模块。先在 `vmware` 分支提交并推送子项目，确认提交在
团队可访问的 origin 后，再在父项目提交 gitlink。提交信息遵守父项目 `AGENTS.md` 的
`【苏忆】` 署名规则。永不强推。

## 设备显示回归

`tests/test_ui_device_presence.py` 在 Node.js 可用时运行 `tests/webui_device_presence.mjs`，直接执行 React 页面引用的 `webui/src/devicePresence.js`。测试不会删除已保存设备或配置。

构建 WebUI 后，可在临时目录安装 Playwright 并运行浏览器回归：

```bash
npm install --prefix /tmp/mdd-device-ui-check --no-audit --no-fund playwright
PLAYWRIGHT_BROWSERS_PATH=/tmp/mdd-device-ui-check/browsers /tmp/mdd-device-ui-check/node_modules/.bin/playwright install chromium
NODE_PATH=/tmp/mdd-device-ui-check/node_modules PLAYWRIGHT_BROWSERS_PATH=/tmp/mdd-device-ui-check/browsers node tests/webui_device_presence_browser.cjs
```

该脚本只服务本地构建资产与虚构 API 响应，不连接生产服务；覆盖拔出、剩余设备选择、空状态、历史记录、重连、无写请求和宽/中/窄视口。截图默认位于 `/tmp/mdd-device-ui-check/results`。
