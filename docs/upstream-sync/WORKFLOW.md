# 同步流程与技巧

## 1. 平时开发，让下一次合并更容易

按功能或缺陷组织提交，在提交正文说明修复的行为、边界和验证方法。硬件兼容、安装器、前端布局
尽量各自形成清楚的变更，避免夹带大范围格式化；这样下一轮可以判断上游是否已经解决了同一问题。

为自有行为保留有意义的回归用例，例如无 SMSC 仍可通话、PIN/IKE/SIP 共用严格 USIM 选择器、
跨页通话不重建连接。测试应检查行为，不要只证明实现中出现某个函数名。

持续维护一份“必须长期保留的定制 / 等待上游吸收的临时修复”清单，每项关联源码、原因和测试。
当上游已有同等或更完整实现时，收敛重复代码，同时保留本分支的严格边界和回归用例。按审定的稳定版本
分批同步，比积累大量互不相关变化后一次处理更容易审查；不要因主干前进就自动部署尚未验收的提交。

版本使用 `<上游版本>-vmware.<修订>`。更新 VERSION 时同步前端 package/lock 及实际约束版本的测试。
纯文档维护通常不改产品版本；按已授权范围交付，不为文档变化专门重启正式服务。

## 2. 开始前重新核实现场

在普通用户的开发 checkout 内执行；若根目录是 `/opt/mdd-sim-gateway`，停止源码编辑。

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short
git log --oneline -5
git remote -v
git worktree list
git fetch --prune origin
git fetch --prune upstream
git rev-list --left-right --count vmware...origin/vmware
```

| 本地 / 远端独有提交数 | 处理 |
|---|---|
| `0 0` | 可用当前一致基线继续 |
| `0 N` | 工作树干净并复核远端后，仅允许快进同步 |
| `N 0` | 保留本地提交，核实归属和交付状态后按授权处理 |
| `N M`，两者都大于零 | 日常分支已分叉，停止自动同步并复核；不能借“合上游”绕过 |

发现来源不明的已暂存、未暂存、删除或未跟踪文件时保留现场。不要自动 stash、清理目录、
reset、rebase 或强推。先记录本轮分析、修改、提交、推送、部署的授权范围；已经明确授权的步骤
持续推进，不重复询问。对外发 Issue/评论属于单独动作，不能从维护分支的授权中推断。

## 3. 固定目标，先看历史再看功能

上游 main、tag 和更新通道可能继续移动。核对上游 VERSION、CHANGELOG、发布及修复提交，
将已审定的完整 SHA 写入本轮记录；后续 merge/build/验收都使用该 SHA。

```bash
vmware_base=$(git rev-parse vmware)
upstream_target='<填入已审定的完整40位SHA>'
git show --no-patch --format=fuller "$upstream_target"
git merge-base --all "$vmware_base" "$upstream_target"
git log --reverse --oneline "$vmware_base..$upstream_target"
git diff --stat "$vmware_base" "$upstream_target"
```

共同祖先可能不止一个。让 Git 的普通合并处理所有祖先，不人工挑选一个 merge-base 来重造基线。
两端文件差异也不等于“上游新增”：其中还包含 VMware 自有改动。结合提交历史，将每个功能记录为
新增、部分重叠、已有更完整实现、已存在或不适用。本次已合过 v1.7.0，就不能再把单飞书、rekey 等算成缺失。

直接使用固定上游 SHA 即可，main 无需充当中转分支。若前一次上游目标不是新目标的祖先，
先解释历史为何变化，再调整范围，不能默认接受远端改写后的历史。

开发仓库与正式仓库的对象完整性分别检查；分支 clean、配置没有 promisor 都不足以证明历史 blob 齐全。
如发现缺对象，按本次案例先隔离复现并验证恢复方案，不能直接套用一次性的完整获取选项。

## 4. 在隔离 worktree 中做普通合并

确认本地基线已经按上一步与 origin 对齐，且下列分支、目录不存在后执行。版本名应改为本轮值。

```bash
dev_repo=$(git rev-parse --show-toplevel)
cycle=vX.Y.Z
sync_branch="codex/vmware-sync-$cycle"
sync_dir="$(dirname "$dev_repo")/mdd-sim-gateway-sync-$cycle"
git worktree add -b "$sync_branch" "$sync_dir" origin/vmware
git -C "$sync_dir" merge --no-ff --no-commit "$upstream_target"
```

合并冲突时保留现场。在集成目录中用 `git status --short`、`git ls-files -u` 和 `git diff --cc`
检查；`git show :1:路径`、`:2:路径`、`:3:路径` 分别查看存在的祖先、VMware 和上游版本。
修改/删除冲突的某些 stage 不存在是正常现象。

按功能块解决，不对整棵树选择 ours/theirs。两边都有效的断言应合并，不能为通过测试删除自有契约。
如需中止，先确认隔离目录没有新增用户改动，再仅对这次未提交合并执行 `git merge --abort`；
不要清理其他 worktree 或恢复材料。普通合并与 abort 的行为见 [Git merge 文档](https://git-scm.com/docs/git-merge)。

反复遇到同类冲突时，可选择使用仓库范围的 `rerere` 复用历史解决记录；保持 `rerere.autoupdate=false`，
逐块审查复用结果后再暂存。旧解决方案可能已不适合新上游语义。[Git rerere 文档](https://git-scm.com/docs/git-rerere)

## 5. 每轮复核这些 VMware 契约

| 范围 | 应保留的行为与主要入口 |
|---|---|
| 部署边界 | 原生 Control/WebUI、systemd、rootful Docker Engine、本机源码构建；不复活 Docker Control、Actions、Release/网页更新入口 |
| SIM 与预检 | `control/app/sim.py`、`engine/pin_keeper.py`、`ami_usim.py`、`swu_ike.py`：严格 USIM、嵌套 EF_DIR、直接响应、61/9F、6C、有界失败；SELECT 接受不等于数据完整 |
| 身份与互斥 | PIN 前实时核对卡身份；eSIM 快速复用只接受同次操作、有效 reader 绑定且再次核对身份的证明；状态变化即失效 |
| 运营商规则 | DITO、CMLink、CTExcel 等必须按精确条件生效；不能以共享 PLMN 替代 SPN 判别；内部身份与归属域不暴露给浏览器 |
| 生命周期 | `main.py`、`engine.py`、host：纯改名免重启；运行字段变更仍有序重建；上限解析一致、Created 残留清理、reader 释放及宿主 PCSC 客户端匹配 |
| 数据/通知 | `store.py`、`notify_push.py`：幂等迁移、原 ID 补片、事务一致性、事件消费者、空通道禁用及独立重试；顶层和嵌套秘密/退役事件均过滤 |
| WebUI | 保留跨页连接和全局音频；区分 loading/空/失败；反馈留在记录内；只隐藏离线设备的运行展示，不删除保存意图 |
| Engine/产物 | 保留固定源码与摘要、自有补丁、amd64 Opus；模块清单、完整名称集合、指纹、manifest 和实际镜像一致；旧代继续使用旧验证器 |

无文本冲突不代表没有问题。特别检查自动合入的 Dockerfile、配置默认值、store、通知分发、API 客户端、
事件消费者和测试。搜索重复函数、悬空引用、失效路由及被重新引入的文件；同时检查差异的两个方向：

```bash
git diff --name-status "$vmware_base" HEAD
git diff --name-status "$upstream_target" HEAD
```

上面针对提交后的最终结果；尚未提交时应另外检查 staged/unstaged 内容，不能只看 HEAD。

## 6. 验证与交付

先跑相关测试，再按 [开发规范](../DEVELOPMENT.md)执行 Linux 基础门禁。测试依赖使用隔离 venv，
WebUI 按锁文件构建；真实数据只通过只读 SQLite backup 取得仓库外受限副本。

- 覆盖旧数据读写、重复迁移、记录保留、事务失败和副本恢复；测试副本不连接真实硬件或通知服务。
- WebUI 检查 1440/900/390px、空提示、异常、键盘和跨页状态；生产身份不得用于 fixtures 或截图。
- Engine 或其运行输入变化时，对最终候选提交完成要求的 amd64 无缓存构建，核对版本、两类指纹、
  精确模块集合、动态/Python 依赖及 TUN/NET_ADMIN。普通重试复用有效缓存，不把无缓存当默认动作。
- 新输入文件要进入相应 fingerprint；生成物和 Python 缓存不能影响指纹。模块集升级必须同步验证器和 manifest。
- 仅 Markdown 等文档改动时执行 diff、链接和文档约束检查，无需重跑 Engine 构建。

审查后创建普通合并提交，标题用 `【苏忆】`、正文 1–5 个编号条目；后续修复使用普通提交。
准确提交构建不通过时，修复后重新验证受影响门禁和最终身份。发布前确认结果同时包含原 VMware 基线和固定上游目标：

```bash
git merge-base --is-ancestor "$vmware_base" HEAD
git merge-base --is-ancestor "$upstream_target" HEAD
git diff --check
sh tools/check-subscriber-identifiers.sh
sh tools/check-subscriber-identifiers.sh --commits origin/vmware..HEAD
```

提交/推送已获授权且原开发目录仍干净时，从原目录将 vmware 快进到审定结果，再使用本次推送的 hook：

```bash
git -C "$dev_repo" switch vmware
git -C "$dev_repo" merge --ff-only "$sync_branch"
git -C "$dev_repo" -c core.hooksPath=hooks push origin vmware
git -C "$dev_repo" fetch origin vmware
git -C "$dev_repo" rev-list --left-right --count HEAD...origin/vmware
```

最后结果应为 `0 0`，另核对完整 SHA。推送前若远端又变化，重新审查，不强推。HTTPS 重试不关闭 TLS 校验，
不更换远端 URL，也不改全局 hooks 配置。是否删除本轮分支/worktree 在证据归档后另行决定，不自动清理。

集成期间如果继续产生新的 VMware 修复，发布基线必须纳入这些已交付提交。保留旧集成现场，重新审定
增量集成方案或从最新基线建新 worktree；更新本轮基线、最终 SHA 和受影响验证。不能为了让快进成立
而丢掉这些修复，也不能把旧候选测试结果直接当成新候选已通过。

## 7. 正式更新与停止条件

部署已获授权后，记录版本、测试结果、维护影响和恢复材料。如维护计划要求整机快照，必须先取得本轮
明确完成确认；已经确认的同轮前置条件不重复询问。项目数据归档不能替代含全部虚拟磁盘的整机恢复点。

只使用 `sudo /usr/local/sbin/mddctl update --yes`。不要在 /opt 手工 pull、切 symlink、改权限或拼接新旧产物。
管理器验证当前代、构建、测试、归档、激活及健康检查；事务内失败让其完成自动恢复，期间不要重启局部服务。
旧管理入口无法取得已核实的自举修复时，仅使用仓库规定的最新 bootstrap **update** 流程，不以 install 绕过事务。

更新后分别记录：系统/HTTPS/产物身份，USB/PCSC/ATR，出口 DNS/STUN，SWu/IMS，以及用户确认的普通号码
呼入呼出、双向音频、DTMF、跨页通话和短信送达。每层分别判定，没测的标为未验证。

分叉、未知改动、测试/迁移/恢复失败、身份或模块不符、空间不足、缺少约定的快照确认、核心业务失败，
都应停止进入下一阶段。成功返回后的业务失败按本轮约定处理，不自动重试同一失败版本。
本次约定为保留脱敏证据后由用户恢复整机快照，详见 [1.9.1 记录](2026-09-07-v1.9.1.md)。

持久证据放在仓库外受限目录，脱敏结论写入版本记录。/tmp 可在重启时消失；/var/tmp 和 VM 内其他磁盘
目录也会随整机快照回退。需要跨快照保留的脱敏记录先另存到宿主机；含凭据的完整归档只放受控加密介质。
