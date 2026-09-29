# 代码评审 · sky_eyes 前后端

> 评审时间：2026-09-11 ｜ 范围：`pipeline.py` / `detector.py` / `multimodal_understanding.py` / `scripts/export_demo_data.py` / `web/src/**`
> 方法：逐文件阅读 + 实际运行验证（不是凭印象）

**结论**：整体结构是清楚的，链路能端到端跑通，demo 也能离线运行。但有 **2 个必修项**、**3 个应修项**。其中后端有一个**潜伏的静默误判**，前端有一个**演示时会白屏**的风险。

> **修复状态（2026-09-12）**：必修 2 项 + 应修 3 项**已全部修复并验证**；另外顺手修了 B5 / B6 / F2 / F8。
> 修复过程中测试又抓出**第三个 bug（B1b）**，是本次评审时漏掉的。详见文末「修复记录」。

---

## 一、必修

### B1 · 严重等级会静默误判为「高」 ⚠

**位置**：`pipeline.py:69`、`pipeline.py:110-113`

```python
_BARE_SEVERITY_RE = re.compile(r"[低中高重]")

def _severity_from_short_answer(text):
    match = _BARE_SEVERITY_RE.search(text or "")   # 裸字符类匹配
    return _DEGREE_MAP.get(match.group(0)) if match else None
```

这是**裸字符类匹配**，只要文本里出现「严重」两个字的「重」就会命中，并被归一化成「高」。实测：

| 输入 | 抽取结果 |
| --- | --- |
| `'无法判断严重程度'` | **高** ❌ |
| `'这是一起严重的事故'` | **高** ❌ |
| `'图片模糊，无法确定'` | None ✓ |
| `'低'` | 低 ✓ |

**当前没触发**——本次 18 帧里 5 个追问答案都是干净的「低」。所以这是**潜伏缺陷**，不是现存数据错误。

**为什么必须修**：在我们的处置建议规则里，「高」对应的是**「立即派警 + 同步通知急救」**。把「无法判断」误读成「高」，是会让系统做出错误动作的那一类错误。虽然我之前定过「宁可高报不可低报」的原则，但那是针对**模型输出**的取舍，不能用来给**解析 bug** 开脱。

**修法**：严格匹配单字答案，不达标就老实返回 `None` 走「未判定」，不要猜。

```python
_STRICT_SEVERITY_RE = re.compile(r"^\s*(?:是|为|属于)?\s*([低中高])\s*[档级]?\s*$")
```

---

### F1 · 没有错误边界，一崩就是白屏 ⚠

**位置**：`web/src/App.tsx`、`web/src/main.tsx`

整棵树没有任何 `ErrorBoundary`。任何一个组件在渲染时抛异常（比如数据字段缺失、`data.events[0]` 越界），React 18 会**卸载整棵树**，用户看到纯白页面，控制台之外没有任何提示。

**为什么必须修**：这是个要给客户/领导现场演示的东西。演示当天白屏 = 演示事故，而且当场很难短时间定位。加一层 ErrorBoundary 的成本是 20 行，收益是"最坏情况下显示一句可读的报错"。

**修法**：包一层 ErrorBoundary，兜底 UI 显示错误信息 + 「重载数据」按钮。

---

## 二、应修

### B2 · 帧号基准不一致（off-by-one）

**位置**：`detector.py:151` + `web/src/hooks/usePlaybackSync.ts:53`

```python
# detector.py —— 先自增，再处理
frame_index += 1
...
"frame_index": frame_index,
"time_sec": round((frame_index - 1) / fps, 3),   # 时间按 0-based 算
```

```ts
// usePlaybackSync.ts —— 算出的是 0-based 帧号
const frame = Math.round(v.currentTime * fps)
const point = mapRef.current.get(frame) ?? mapRef.current.get(frame - 1) ?? ...
```

元数据里的 `frame_index` 是 **1-based**（首帧=1），而 `time_sec` 是 **0-based** 时间。前端拿 0-based 帧号去查 1-based 的表。

**实测确认**：296 个时间轴点位**全部**满足 `frame_index - round(time_sec × fps) = 1`。

**影响**：
- 检测框比实际晚 1 帧（40ms）出现——肉眼几乎看不出
- 但 HUD 上显示的帧号与元数据、与 ROI 文件名（`accident_frame_0049_roi.jpg`）**差 1**，排查问题时会被误导

**修法**（改前面一处即可）：`const frame = Math.round(v.currentTime * fps) + 1`，让 HUD 和 ROI 编号对齐，语义统一到 1-based。

---

### B3 · 每次导出都重转码，白等 29 秒

**位置**：`scripts/export_demo_data.py:306`

```python
codec = probe_codec(src_video)               # 看的是【源】
if codec.lower() in {"avc1", "h264"} and not args.reencode:
    shutil.copy2(...)
else:
    transcode_h264(src_video, ...)           # 每次都会走到这里
```

源文件 `output/detections.mp4` 仍是 `FMP4`（我们从没重跑检测），所以**每次运行导出脚本都会重新转码**。

**实测**：源 `FMP4` / 目标 `h264`；这一轮我跑了 3 次导出，白等约 87 秒。

**修法**：目标已存在 && 目标是 H.264 && 目标不比源旧 → 直接跳过。

---

### F4 · 简报的「生成时间」每次渲染都在变

**位置**：`web/src/components/IncidentBrief.tsx`

```tsx
{t('brief.generatedAt')} {new Date().toLocaleString()}
```

写在 render 里。点「复制文本」会触发 `setCopied(true)` → re-render → 纸面上的生成时间跳一下。虽然不影响功能，但一份"公文"的时间戳在眼皮底下变，观感不好。

**修法**：`useState(() => new Date().toLocaleString())` 固化一次。

---

## 三、可延后（整洁性 / 打磨）

> ⚠ **这是 2026-09-12 评审当时的快照**，保留原样作为记录。
> 各项的**最新状态以第六节「修复记录」为准**；P5/P6 阶段修掉的项（F3 / F5 / F7 / F9 等）
> 记在 `DEMO_PLAN.md` 的 P6 记录里，不在本文再维护一份——**两个文档各记一份清单，
> 迟早有一份会过期**，本文的「仍未处理」就真的过期过一次（见第六节末）。

### 后端

| 编号 | 位置 | 问题 |
| --- | --- | --- |
| ~~B4~~ | `multimodal_understanding.py:154-186` | ~~`generate_images()` 33 行，本项目用不到~~ → **已于 2026-09-22 删除** |
| ~~B4~~ | `multimodal_understanding.py:145-151` | ~~`understand_image_and_question()` 旧兼容垫片，无人调用~~ → **已于 2026-09-22 删除** |
| ~~B4~~ | `main.py` | ~~已被 `detector.py` 完全取代~~ → **已于 2026-09-22 删除**（顺带消掉一个"误运行就覆盖 `output/`"的入口） |
| B5 | `detector.py:130-140` | avc1 失败时，第一个 writer 对象没 `release()` 就新建第二个，可能留半个文件句柄。加一行 `writer.release()` 即可 |
| B6 | 全局 | **没有任何测试**。等级抽取这条链已经出过两次真问题（中文 `max` 按码点取错、事件级取首帧），而这些函数是**纯字符串处理**，写测试不需要 GPU 和网络 |

### 前端

| 编号 | 位置 | 问题 |
| --- | --- | --- |
| F2 | `Dashboard.tsx` | rAF 以 25fps 驱动父组件重渲染。`AlertFeed`/`SourceList` 已 memo，但 `StatCard`×4 和 `EventTimeline` 仍在每帧重跑。给 `StatCard` 加 `memo` 是零成本的 |
| F3 | `usePlaybackSync.ts` | 视频暂停时 rAF 仍在空转（`frame !== prevFrame` 挡住了 setState，但循环没停）。长时间挂着会有无谓唤醒 |
| F5 | `System.tsx` | 界面上「静态演示 / 实时接入」两个 chip，但实时那个只是 disabled，**从未真正探测过** `/api/health`（`probeLive()` 是死代码）。技术上可以解释为"未实现"，但界面没写明，容易被当成"已实现但坏了"<br>→ **【2026-09-22 更正】本条已失效，且原来的措辞是错的**。P5 把实时模式真正做完：`probeLive()` 就是 `demoStore.ts` 的探活入口，`/api/health` 会被真正调用，开关可用、任务能起、SSE 能接。**"当时没接线"不等于"函数是死的"**——把前者写成后者，会让后来的人真的去删一个在用的函数 |
| F6 | `export_demo_data.py` + `VideoStage.tsx` | `demo.json` 里 `video.src` 硬编码 `media/detections.mp4`。用 `--no-video` 导出后，前端会显示"视频加载失败"。应在数据里标记视频是否存在<br>→ **2026-09-22 复核：仍未处理**（`VideoStage.tsx:103` 直接显示"视频加载失败"） |
| F7 | `index.html` | `document.title` 写死中文，切到 EN 后标题不变 |
| F8 | `Events.tsx:27,32` | 两处 `eslint-disable-next-line react-hooks/exhaustive-deps`，但项目**没有配置 ESLint**，注释是无效的。同时掩盖了 `statusOf` 未进 `useMemo` 依赖的事实（虽因 `current` 在 deps 里、实际行为正确） |
| F9 | `index.css` | `--font-mono: 'JetBrains Mono', ...` 但从未加载该字体，实际永远走 `ui-monospace` 回退。名字有误导性 |
| F10 | 大屏 | 详情页有 `?brief=1` 可分享，但大屏没有"定位到第 N 秒"的链接，演示时没法把某个事件直接发给别人<br>→ **2026-09-22 复核：仍未处理** |

---

## 四、做得对、不要动的地方

评审不只是挑错，也说清楚哪些决策是对的，免得后面被"优化"掉：

1. **等级口径写进了 JSON**（`event_severity_rule: "max_of_representative_frames"`）。前端不需要猜后端怎么聚合的，改口径时也有痕迹可查。
2. **中文等级排序坑在 Python 和 TS 两侧都做了显式映射 + 注释**。这个坑很隐蔽（`中` < `低` < `高` 按码点），两侧都留了说明，是防止第三次踩进去的关键。
3. **demo 完全离线可跑，不依赖 GPU 和服务**。这是整个方案最重要的鲁棒性设计——服务器是共享的，别人一训练 Janus 就起不来。
4. **免责声明 + 规则生成的处置建议**。明确区分"模型说的"和"系统按规则建议的"，对交管客户这个分寸是必要的。
5. **重复段落检测（`_is_degenerate`）+ 追问补判 + 重试**，这三层都是被真实故障逼出来的，不是预防性过度设计。

---

## 五、修复优先级建议

| 优先级 | 项 | 理由 |
| --- | --- | --- |
| **必修** | B1 | 会静默把"无法判断"读成「高」，误导处置动作 |
| **必修** | F1 | 白屏是演示期的致命故障，且现场难以定位 |
| **应修** | B2 | 帧号与 ROI 文件名对不上，排查时误导 |
| **应修** | B3 | 每次导出白等 29 秒，影响迭代速度 |
| **应修** | F4 | 公文时间戳跳变，观感问题 |
| 可延后 | B4 / B5 / F2 / F3 / F5–F10 | 整洁性与打磨 |
| 建议补 | B6 | 给等级抽取补几个纯字符串用例，防第三次同类 bug |

**建议先做 B1 + F1 + B2 + B3 + F4 这五项**，都不大，加起来一次改完。改完再做 P4 数据看板更稳妥——否则 P4 要动 `Analytics.tsx`，而 F1 的错误边界最好是包在路由层、一次到位。

---

# 六、修复记录（2026-09-12）

## 先记一件事：测试抓出了评审漏掉的第三个 bug

写回归测试（B6）时，第一条用例就失败了——**评审时没看出来**：

```
_DEGREE_MAP = {"低": "低", "中": "中", "重": "高"}     # 没有「高」这个键
_severity_from_short_answer("高")  ->  None            # 模型答「高」被判成「未判定」
```

追问路径用的是 `_DEGREE_MAP.get(x)`（**无默认值**），所以模型回答「高」时取到 `None`，直接落到「未判定」。

**这正是本评审把 B1 列为必修的同一种失效模式，而且比 B1 更实在**——B1 是"把非答案误判成高"，B1b 是"把真正的**高**丢掉"。后者是纯粹的**漏报高等级**。

本次数据没暴露纯属运气：5 个追问答案**碰巧全是「低」**。只要当时有一帧答「高」，我们就会少报一起高等级事件。

> 这条也说明了一件事：**评审只能发现问题，不能证明没有问题的**。B1b 是"看一眼代码看不出来、但跑一条用例立刻暴露"的典型——所以 B6 不是可选项。

## 修复清单

| 编号 | 项 | 改法 | 验证 |
| --- | --- | --- | --- |
| **B1** | 等级静默误判 | 裸字符类 `[低中高重]` → 严格整串匹配 `_STRICT_SEVERITY_RE`，不达标返回 `None` 走「未判定」 | 用例覆盖：`'无法判断严重程度'` / `'这是一起严重的事故'` 均返回 `None` |
| **B1b** | `_DEGREE_MAP` 缺「高」键 | 补齐为 `{低,轻,中,高,重}`，并把取值改成带去默认值的 `.get(raw, raw)` | `_severity_from_short_answer("高") == "高"` |
| **F1** | 无错误边界 | 新增 `ErrorBoundary.tsx`，包在 Layout 的 `<Outlet/>` 外层（**保留顶部导航**，出错还能切页继续演示） | 临时注入抛错实测：错误页正确渲染，导航仍在，堆栈精确定位到 `Analytics.tsx:109` |
| **B2** | 帧号 off-by-one | `usePlaybackSync` 里 `Math.round(currentTime*fps)` → `+ 1`，与 1-based 的 `frame_index`、ROI 文件名对齐 | 数据不变量 `frame_index = round(time_sec*fps) + 1` 对全部 296 点位成立，现在恒等命中 |
| **B3** | 每次导出重转码 | 判据从**源**文件改看**目标**文件（已是 H.264 且不比源旧就跳过） | 实测 **29 秒 → 4.8 秒** |
| **F4** | 简报时间戳跳变 | `new Date()` 从 render 挪进 `useState(() => ...)` 固化一次 | — |
| B5 | writer 双开未释放 | fallback 前加 `writer.release()` | — |
| B6 | 无测试 | 新增 `tests/test_severity.py`，10 条用例，纯字符串无需 GPU/网络 | **10/10 通过** |
| F2 | StatCard 每帧重渲染 | 加 `memo` | — |
| F8 | 失效的 eslint-disable | 把取值逻辑内联进 `useMemo`，依赖变完整，注释删掉 | — |

## 顺带修正的语义问题

`_DEGREE_MAP` 只有「中度/重度」，缺了「轻度」这个对称项。已补上 `轻 → 低`，并同步扩展 `SEVERITY_RE` 的字符类。现在「轻度 / 低」「中度 / 中」「重度 / 高」三组都能正确归一。

## 仍未处理（2026-09-22 逐条复核过）

这份清单**曾经整体过期过一次**，值得留个教训：原来它同时列着 B4 / F3 / F5 / F7 / F9，
而其中 F3、F5、F7、F9 早在 P5/P6 就修完了（记在 `DEMO_PLAN.md` 的 P6 记录里）。
本文没有再维护第二份状态清单，于是它一直挂着"未处理"，
**读的人会以为那些问题还在**——文档与代码不一致，比没有文档更坏。

复核方法：不靠回忆，逐条回代码里查（`Grep` 工具，不用 shell `grep`）。

| 编号 | 2026-09-22 状态 | 依据 |
| --- | --- | --- |
| B4 | ✅ 已清除 | `generate_images()` / `understand_image_and_question()` / `main.py` 三个文件级/函数级死代码已删；`probeLive()` 见 F5 更正——它**从来不是**死代码 |
| B5 | ✅ 已修 | 见上方修复清单 |
| B6 | ✅ 已修，且已远超原范围 | 从 10 条涨到 **6 套共 169 条**（等级 11 / 事故类型 20 / 告警 73 / 任务清单 22 / 复核 25 / 标注 18） |
| F2 / F8 | ✅ 已修 | 见上方修复清单 |
| F3 | ✅ 已修 | P6：rAF 只在播放期间运行 |
| F5 | ✅ 已修，且原文措辞有误 | P5：实时模式真正接通，`probeLive()` 在用 |
| F6 | ❌ **仍未处理** | `VideoStage.tsx` 仍直接显示"视频加载失败"，没有"视频是否存在"的标记 |
| F7 / F9 | ✅ 已修 | P6（记在 `DEMO_PLAN.md`） |
| F10 | ❌ **仍未处理** | 大屏仍没有"定位到第 N 秒"的可分享链接 |

**只剩两条**：F6（`--no-video` 导出时播放器指向不存在的文件）与 F10（大屏无可分享定位链接）。
两条都不影响演示正确性——默认导出是带视频的，演示也一直是人工点开事件。要做的话都不大。
