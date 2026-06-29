# 编排器实战表现报告 — wildlife_education_website 重构

- 日期：2026-06-28
- 目标仓库：`wildlife_education_website/Team_project_MVP_ver`（鸟类科普静态站，简历项目）
- 编排器：`agent_corporation_framework`（Claude 规划/评审 + Codex 实现，`--decompose`）
- 本次共 3 次启动：run1（鉴权失败）/ run2（WinError 崩溃，s1 实现成功）/ run3（修复后完整跑）

---

## 一、结果总览（run3，20260628-171445）

| 子任务 | 内容 | 轮数 | gate | 评审 | 结论 |
|---|---|---|---|---|---|
| s1 | 设计系统 tokens/字体/导航/页脚 | 1 | ✅ | pass | ✅ 完成 |
| s6 | 重写 README（部署文档） | 1 | ✅ | pass | ✅ 完成 |
| s2 | 首页升级为响应式着陆页 | 1 | ✅ | pass | ✅ 完成 |
| s4 | 去 jQuery + Leaflet npm 化 + 三页容错 | 1 | ✅ | pass | ✅ 完成 |
| s3 | 游戏页重构 + 修配对 bug | 3 | ✅×3 | revise×3 | ❌ 到达轮数上限 |
| s5 | 全站质量收尾 | 0 | — | — | ⏭ 因依赖 s3 被跳过 |

- **完成度：4/6 子任务** 实质交付；s3 核心已完成、仅余 1 个运行时 bug；s5 未跑但其客观项多已被顺带满足。
- **客观 gate 通过率：7/7 轮 = 100%**（每一轮 Codex 产物都过 `npm run build` + 结构/命名/卫生检查）。
- **评审通过率：4/7 verdict = pass**；s3 三轮均 revise。
- **成本：≈ $2.76**（run3 累计），其中 s3 单独烧掉 ≈ $1.23 且最终失败（最贵且未交付）。run2 崩溃前另耗 ≈ $0.53。
- **耗时：≈ 23 min**（run3 墙钟），run2 崩溃前另浪费 ≈ 6 min。

---

## 二、出现过的 Bug

### 1. 托管会话内 headless `claude` 鉴权失效（环境 + 框架体验）
现象：run1 第一步拆 DAG 即 `Not logged in · Please run /login`。根因：当前是宿主托管子会话（`CLAUDE_CODE_CHILD_SESSION` 等），token 运行时注入、不以独立 CLI 可读形式持久化。
- 规避：注入 `ANTHROPIC_API_KEY` + 剥离 `CLAUDE_CODE_*` / `ANTHROPIC_BASE_URL` 后子进程独立鉴权成功。
- 框架短板：仅 `sys.exit("[claude] 调用失败 ... Not logged in")`，无任何"如何修"的引导。

### 2. 评审步骤 `WinError 206 文件名或扩展名太长`（框架真 bug，已修）
现象：run2 中 s1 实现+gate 全部成功，进入 Claude 评审时崩溃，整个引擎中止 → s2–s5 未跑。
根因：`agents.py:ClaudeClient.ask_text` 把整个 prompt（评审时含**完整 diff**）作为命令行参数传给 `claude`，超出 Windows ~32K 命令行长度上限。
- **已修**：prompt 改走 stdin（`claude -p` 无参数从 stdin 读），`process.run` 增加 `input` 透传时不再强制 `stdin=DEVNULL`。已用 6.96 万字符 prompt 验证通过，run3 评审全程无崩溃。

### 3. 业务侧 bug —— **评审抓到、gate 抓不到**（这是亮点，见第四节）
- s3-r1：重构把 message box 样式迁到 `.message-box` CSS 类，但代码漏加基础类，消息框退化为**无样式纯文本**（回归 bug）。
- s3-r3：`game-bird-images.js` 占位图用裸字符串路径，Vite 不 emit → 运行时**占位图 404**（破图）。`gallery.js` 用的是正确的 `new URL(..., import.meta.url)`。**此 bug 仍遗留在工作区**，1 行可修。

### 4. 次要：评审产物编码
评审 `.txt` 落盘含中文时为 GBK/乱码，`s3_round2_review` 解析为非法 JSON（被 JsonAgent 重试机制吞掉）。建议产物统一 UTF-8 落盘。

---

## 三、需要改进的点（按优先级）

1. **stdout 缓冲导致进度不可见**：重定向到文件时 `run.py` 的 print 全程不刷新，`orch.log` 直到进程退出才有内容，只能靠 `runs/<id>/` 产物盯进度。建议 `print(..., flush=True)` 或 `python -u`。
2. **缺"续跑/跳过已完成子任务"**：没有 resume 机制，每次从头重拆 DAG。本次靠"提交 s1 + 改写 task 只做剩余"绕过。建议支持 `--resume <run-id>` 或对 gate 已满足的子任务快速短路。
3. **失败级联过粗**：s3 卡住导致 s5（全站 QA 巡检）被整体跳过，而 s5 大部分与 s3 无强耦合。建议依赖更细化，或失败子任务降级为"warning 继续"选项。
4. **`--max-rounds 3` 对"评审驱动收敛"偏紧**：当 gate 无法验证运行时正确性时，每轮只修掉评审的 1 条意见、又暴露新的一条，3 轮不够。建议对交互密集型子任务调高轮数，或让评审一次性列全 findings。
5. **Codex 修复偏窄**：修了 `gallery.js` 占位图却漏掉 `game-bird-images.js` 同类问题；建议指令里强调"同类问题全仓搜索修复"。
6. **gate 设计盲区（我方）**：gate 只验构建/结构/卫生，验不了 DOM/运行时行为——这正是 s3 必须靠 LLM 评审、且无法自证的原因。建议补一个 headless 冒烟测试（jsdom/Playwright）让 Codex 能自查交互正确性，减少评审往返。
7. **鉴权体验**：框架可检测"托管子会话"场景并提示用 `ANTHROPIC_API_KEY`，而非裸退出。

---

## 四、性能与表现评估

**DAG 拆解（Claude 规划）— 优秀。** 拓扑清晰（s1/s6 根 → s2/s3/s4 → s5），brief 精准到文件与**行号**（如直接点名 game.js 配对 bug 的错误布尔表达式），验收标准客观可判。两次拆解（迁移版 / 收尾版）都合理。

**Codex 实现 — 强，但有盲点。** 大头一次过：Vite 迁移、设计系统、去 jQuery、外部请求容错全部 round-1 通过且质量获评审认可。弱点：①重构引入回归（无样式消息框）；②修复偏窄（漏同类占位图问题）。

**Claude 评审 — 本次最大价值点。** 评审抓到 **2 个 gate 完全放过的真·运行时 bug**（无样式消息框、占位图 404）。即"gate 保证能 build，评审保证真的对"，构成有效的纵深防御。s3 的"失败"不是空转，而是评审在持续发现真实缺陷。

**作为无人值守编码的可用性 — 良好但有上限。** 一次启动、无人干预交付了一个非平凡重构的 ~80%，过程留快照可回滚、改动不自动提交（安全模型到位）。上限来自：Windows/鉴权摩擦（已部分修复）、无 resume、失败级联、以及验证层缺运行时测试。

**综合评分（主观）**
- 规划质量：9/10
- 实现质量：7.5/10
- 评审有效性：9/10
- 工程健壮性/可观测性：5/10（缓冲、无 resume、级联、鉴权/平台摩擦）
- 端到端可用性：7/10

---

## 五、交付物当前状态

- 分支 `refactor/vite-portfolio`：s1 已 commit；s2/s4/s6 + s3 部分改动留在工作区（未提交，21 文件 +1071/-665）。
- `node gate.mjs` 当前 **7/7 全过**，`npm run build` 成功，dist 出 5 页。
- **唯一已知遗留 bug**：`src/pages/game/game-bird-images.js:8` 占位图路径，1 行可修（改成 `new URL('../../assets/images/bird-placeholder.svg', import.meta.url).href`）。
- s5 未跑：无 console.log、无中文注释已满足；alt/对比度/死链未系统巡检。
