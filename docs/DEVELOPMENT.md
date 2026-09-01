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
- 不把运行数据、`.venv`、`node_modules`、`webui/dist`、build cache 或备份加入 Git；
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
- NetworkManager 默认路由保护；
- Git 精确 remote、clean、fast-forward-only；
- backup/restore 校验和与路径安全；
- doctor JSON 不含用户身份字段；
- 仓库无 workflows、Release manifest 和 LFS。

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
