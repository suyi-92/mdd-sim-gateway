# 读卡与 eSIM 身份恢复修复（2026-09-16）

开发基线：`vmware` / `4fabd94e3d9f2379b4251f65e257d284237cafe4` / `1.9.4-vmware.6`。
开始时工作树干净。开发阶段仅修改开发 checkout，未变更正式配置、驱动、服务和线路。
后续用户已授权提交、推送和受管更新；交付版本为 `1.9.4-vmware.7`。

## 源码证据与处理

| 缺口 | 修复入口 | 自动验证 |
| --- | --- | --- |
| native hotplug 错用新设备默认开关 | `control/app/main.py` 的统一启动资格与启动预检 | 原生已启用线路忽略新设备默认关闭；停用、删除、PIN、错卡门禁保留 |
| 首读失败后 present 无边沿，maintenance 内同名换卡不重探 | reader 身份状态、2–60 秒退避、维护窗口后直接验证、60 秒身份复核 | 首读失败后成功、锁竞争、同名且持续 present 换卡、超时旧结果不能覆盖新代 |
| 六次出口启动失败耗尽后 STOPPED 无恢复意图 | hotplug pending ticket 与 STOPPED 协调 | 六次失败后保留 60 秒恢复时间，后续健康采样安排重试；用户停止使代次失效 |
| maintenance 先于 LPA busy 协调旧身份 | busy/reader 锁门禁与读卡任务所有权 | busy 不安排插卡协调；失败不启动旧卡；每条线路启动/停止串行 |
| 新 IMSI/MCC/MNC 只更新内存，线路继续使用旧国家 | `_verified_subscription_update` 共用于插卡、eSIM 刷新和显式 PIN 验证 | 实际刷新入口写回保存线路；保留名称、PIN、端口和显式出口覆盖；订阅改变清除自动学习号码/IMS home domain |
| EF_ICCID 失败后的 AT 缓存被当成实时身份 | `host/vpcd_modem_bridge.py` 来源字段、Control 新鲜度/进程检查、orchestrator 重建握手 | direct-card / baseband-cache 分离；旧时间、旧进程启动代、空卡、未就绪通道不提供实时证明 |
| 同 eUICC 缓存 active 冲突，同名换卡与延迟页面响应 | cached API 唯一 SE 校准、`webui/src/views/Esim.jsx` 请求/身份代次 | 仅校准当前 ICCID 唯一所属 SE，其他 SE 未确认；旧 HTTP 成功/失败/finally、旧 WS 代次及卸载隔离 |

同 ICCID 下的新订阅输入会进入已有的通话保护重建协调，保留活动通话及未知通话状态的等待规则。
没有根据 profile 昵称推断国家，也没有硬编码 PH。当前硬件是否确实提供 PH IMSI 未验证。

## 失败与等待语义

- 插卡探测等待最多 8 秒后，monitor 返回并发布 `read_timeout` 失败状态；其他 reader 继续扫描。
  仍在底层执行的 PC/SC 调用继续持有该 reader 的锁，不能用取消 Python 等待来假装底层调用已结束。
  同 reader 不创建重复探测；结果晚到且卡片行已换代时丢弃结果。
- 因此，底层 PC/SC 调用若永不返回，本轮不会自动强杀 pcscd 或重启整组线路；这属于明确的 reader 会话阻塞，
  不应显示为成功。该极端实机场景仍需验证受管恢复行为。
- 身份读取不提交 PIN。已确认需要 PIN 的卡进入人工确认状态；正常启动仍走身份与 PIN 预检。
- 出口失败前六次使用原有 6 秒首次等待、后续 5 秒间隔；耗尽后保留 60 秒重试意图。
  每次重新检查线路意图和身份；用户 Stop 取消当前代次，显式 Start 或选择目标 profile 是新意图。
- 同一 modem 的 busy 期间历史身份仅供展示，不能满足自动启动资格。读卡、失败、未确认、缓存时间和切换反馈均保留在界面。

## 验证记录

最小失败证据：新增的 native 默认开关、busy reconcile 两例在修改前失败，修改后通过；
来源字段、订阅更新、刷新失败保护的四例在实现前报错，实现后通过。
其他新增回归见 `tests/test_identity_reconciliation.py`，既有场景继续由读卡恢复、绑定恢复、设备状态与 eSIM 切换测试覆盖。

最终门禁结果另以执行后更新的条目为准：

- Linux Shell 语法、Python compileall、标识符扫描和 diff 检查通过；完整 unittest 1477 项，2 项跳过，
  其余通过（包含 VMware install contract）。删除线路串行停止的最终调整另经 103 项线路生命周期回归通过。
- WebUI：执行锁文件 `npm ci`，prebuild 单测及生产构建通过。
- 两套隔离浏览器回归：eSIM 身份乱序/卸载与既有改名/恢复；1440、900、390 像素通过。
  使用虚构 API 数据，无生产连接。窄屏 profile 名称不再被动作按钮挤没，反馈槽保留。
- amd64 `--no-cache --pull` 构建独立标签 `mdd-recovery-check:20260916` 成功。
  核对基线源码 SHA、版本、amd64、runtime/base 两指纹、Asterisk 20.7.0、128 项模块完整集合及摘要、
  Python 依赖、独立最小容器 `/dev/net/tun + NET_ADMIN` 创建/删除 TUN 均通过。
  该镜像不是正式活动代：源码标签指向上述开发基线，本轮 Control/host/WebUI 修复仍未提交，不能用基线标签冒充整轮修复已交付。

## 尚未执行的实机验收

正式活动代 doctor/HTTPS/systemd/Engine 身份、SCR 整机拔插三轮、SCR 只换 SIM 三轮、
DJI/Quectel 原 profile 与 PH profile 切换三轮及另一条健康线路隔离：均未验证。
每轮应分别记录物理在场、身份确认、显示一致、自动启动、SWu、IMS 的时间与失败终态；
具备条件后再独立验证普通号码呼入/呼出、双向音频和短信。
本轮自动测试和构建不能替代这些验收；未操作生产也不能据此宣称现场已经恢复。

## 交付阶段

用户已授权推送并更新。提交前远端刷新成功，开发基线与 `origin/vmware` 无分叉。
当前执行环境的 `no-new-privileges` 阻止 sudo 提权，`sudo -n /usr/local/sbin/mddctl doctor --json`
在预检阶段被拒绝；正式更新与部署后验收因此未执行，不能记为已部署。
不通过其他入口绕过权限限制；需在允许 sudo 的本机终端执行受管 `sudo mddctl update --yes`。
前述无缓存镜像验证属于开发基线版本，正式交付提交的产物必须由受管更新重新构建并核验。
