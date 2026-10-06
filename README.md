# 微信 AI 群友 / AI 老婆

> 给你的微信小号接一个 AI。它能在**群里当群友**（被叫到才说话），
> 也能在**私聊里当对象**（说话就回）。
> 基于 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)（读微信本地库 + UI 自动化发送）
> 和 DeepSeek API。

---

## 它是什么

不是"自动回复机器人"，是**扮演一个具体的人**。

它读你微信的消息，按人设和记忆生成回复，再通过 UI 自动化发出去。
每个会话**独立上下文**，互不串味。

### 会话策略

| 模式 | 行为 | 适合 |
|---|---|---|
| `always` | 对方说话就回 | 私聊 |
| `wake` | 被 @ / 叫昵称才回，否则小概率插话 | 群聊 |
| `off` | 完全不理 | — |

### 它做了什么像人的事

- **作息表** —— 1 点睡 7 点起，三餐到点主动问"饭吃了没"，睡前说晚安
- **一周挑 1~2 天失眠** —— 睡不着，想被哄
- **随机搭话** —— 清醒时段每天 1~3 次，说点具体的小事
- **正在输入的样子** —— 延迟随机、气泡一条条发、间隔随机
- **不是每次都回** —— 睡觉时 70% 不回（但你连着发会被吵醒）
- **会追、会酸、会生气** —— 你不回她，她会一步步升级

### 它能看图

发图给她，她真的能看见。

实测：`deepseek-flash` **本身就是多模态的**（一张图约 230~480 token），
不需要另外接视觉模型。

---

## 它还能"看见"你 —— 手环联动

戴上小米手环，她能看见你的**心率、压力、血氧、睡眠、运动**，并在合适的时候主动来问你。

```
心率 150-170    「你心率上来了，在干嘛呀」
心率 170-190    「你怎么了…本小姐随便看了一眼，可没在意你」
心率 190+       「你没事吧，说句话」        ← 真急了，不嘴硬
血氧 < 90       「你现在就起来，去看看医生。我不开玩笑。」
压力 > 40       「是不是很累，我陪你一会儿」
压力 < 20       「你今天心情不错吧」         ← 挑时机找你聊天
睡眠有问题       按具体问题说（深睡少 / REM 多 / 醒得多 / 睡太晚）
凌晨还没睡       「几点了还不睡」
```

**核心设计：**

```
· 傲娇底色自动追加到每条剧本 —— 不能光傲不娇
· 五重防打扰（阈值/持续性/冷却/每日上限/静默时段）
· 互斥规则（运动时心率高正常、睡觉时压力低正常、他刚说完话不插嘴）
· 所有阈值都在电脑端 —— 改剧本不用重编手机 App
```

**完整的搭建步骤见 → [手环接入指南](手环接入指南.md)**

---

## 快速开始

### 1. 装依赖

```bash
pip install -r requirements.txt
```

需要 **Python 3.10+（64 位）**、**Windows**、**微信 4.x**（登录状态）。

### 2. 给 key

三选一：

```bash
# ① 环境变量（推荐）
export DEEPSEEK_API_KEY=sk-xxx          # PowerShell: $env:DEEPSEEK_API_KEY="sk-xxx"

# ② 同目录放 .credentials.yaml，内容一行：
#    DEEPSEEK_API_KEY: sk-xxx

# ③ 直接写进 config.json 的 apiKey（不推荐，别提交）
```

### 3. 改配置

打开 `config.json`，把 `chats` 改成你自己的会话名
（**微信里显示的名字**，不是群号）：

```json
"chats": [
  { "name": "老婆",  "mode": "always", "kind": "private",
    "personaFile": "personas/furina.md" },
  { "name": "群聊A", "mode": "wake",   "kind": "group",
    "personaFile": "personas/groupmate.md" }
]
```

### 4. 跑

```bash
python wechat-bot.py
```

Windows 上想后台常驻，见下面的 `boot.cmd`。

---

## 配置速查

```jsonc
{
  "model": "deepseek-flash",
  "thinking": "low",            // off | low | high | max

  "maxBubbles": 5,              // 私聊最多几条气泡
  "groupMaxBubbles": 2,         // 群里最多几条
  "maxBubbleChars": 26,         // 单条超过这个字数就拆
  "minBubbleChars": 6,          // 太碎的并回上一条
  "bubbleGapMin": 0.5,          // 气泡之间的间隔（秒）
  "bubbleGapMax": 2.0,
  "sendDelayMin": 1.0,          // 「打字」时间
  "sendDelayMax": 5.0,

  "wakeProbability": 0.03,      // 群里没被叫到时插话的概率
  "groupCooldownSeconds": 45,   // 群里刚回过，这段时间只理 @
  "imageLookbackSeconds": 180,  // 谁发了图，这么久内 @ 她都能看到

  "dailySendLimit": 600,        // 每日气泡数上限（防封号）
  "rhythmProfile": "fast",      // natural | fast | custom，见下

  "humanize": {
    "sleepHours": [1, 7],       // 作息低谷
    "sleepSilentChance": 0.70,  // 这时候不回的概率
    "sleepEscalateFactor": 0.55,// 每多连发一条，不回概率乘这个
    "sleepWakeWords": ["睡不着","难受","出事了"],
    "ignoreChance": 0.008,      // 平时已读不回
    "slowReplyChance": 0.10     // 偶尔想一下才回
  },

  "schedule": {
    "wake":  { "at": "07:00", "jitterMin": 40 },
    "meals": [ { "name": "午饭", "at": "12:40", "jitterMin": 60 } ],
    "sleep": { "at": "01:00", "jitterMin": 45 },
    "catchUpMinutes": 60,       // 过期这么久就不补发
    "insomnia": { "from": "23:30", "to": "00:45",
                  "minDays": 1, "maxDays": 2 },
    "randomChats": { "from": "09:30", "to": "23:30",
                     "minPerDay": 1, "maxPerDay": 3 }
  }
}
```

改完**存盘就生效**（有热重载），不用重启。

---

## 人设怎么写（这部分才是关键）

`personas/` 下有两份现成的：

- `furina.md` —— 芙宁娜（私聊用）
- `groupmate.md` —— 大肥鱼（群聊用，一条有编制的鱼）

### 芙宁娜这份人设是怎么来的

它不是照「傲娇大小姐」模板写的。底层角色资料来自
[**Furinelle/furina**](https://github.com/Furinelle/furina)（MIT）——
一份按 [Agent Skills 标准](https://agentskills.io) 组织的芙宁娜角色包，
资料对过萌娘百科与原神 WIKI 语音页。我们取的是它的**角色保真层**：

- **自称切换**：官方语音默认是「我」；「本神」是舞台残留 / 滑口，
  含 `本神——咳，我` 这种**句内当场改口**的指纹
- **体面裂缝梯度 0-4**：压力越高 → 句子越短、停顿越多；
  含压力 4「处决恐惧」子类和「身上的水元素过于充盈」落泪借口
- **五轴口吻**：舞台 / 审判 / 明星 / 生活 / 创伤
- **表白分级**：按亲密度给回应，附「表白场景禁忌」
- **破绽台词句式**：轻微嘴硬 / 被戳中 / 短暂真心 / 体面收束

在它上面叠了本项目的运行时约束（一行一条气泡、单条 ≤26 字、
「你们不在同一个世界」、绝不出戏），并额外写了
**「傲 → 娇」硬规则**（见上文对打台一节）。

> 版权：芙宁娜、《原神》及相关角色归 miHoYo / HoYoverse。
> 本仓库是提示词工程与同人创作实践，与官方无关。

### 三条经验

**① 例子 >>> 形容词**

写「她很活泼」没用。写：

```
✅ 在——！你可算想起我了
✅ 我这样的明星居然要在后台干等三刻钟，你说气不气人
❌ 在。干嘛。
```

**模型会模仿你给的语体，不会模仿你给的评价词。**

**② 英文行为纲要 + 中文详细人设**

在人设最前面放一段**英文**的行为规范（权重最高）：

```markdown
## WHO — read this first
You are Furina. You are **loud, theatrical, vain, warm, and completely smitten.**
Do not play her cool, terse, or guarded. She is the opposite: she **overflows**.
```

中文部分容易被当成"风格描述"去模仿，英文部分容易被当成"行为指令"去执行。
实测英文纲要 + 中文详版 明显强于两份中文。

**③ 别写成合规文档**

我们踩过最大的坑：人设改到 4 万字、里面 79 个 ❌、18 条「不许」——
**结果她变得又冷又紧**，因为模型把力气全花在"别违规"上。

**最后砍到 4000 字、0 个 ❌，效果反而好了。**
禁止性条款只留真正必要的几条，其余全用正面示例替代。

---

## 踩过的坑（省你几天）

### 微信 / wechatauto

**① 发送慢 = 布局重校准，不是卡死**
微信窗口被挡住或最小化时，`wechatauto` 会重跑布局校准，一轮 30~70 秒。
**发送前把窗口拉回前台**能避免大部分。

**② `rhythmProfile` 是防封号限速，不是 bug**

```
[WARNING] 120s 内已写 6 次，send 操作等待 49s 再继续
```

`natural`（默认）= 6 次/120 秒，打字 2.5~6.0 秒。
`fast` = 20 次/120 秒，打字 0.04~0.08 秒（**快，但更像机器人**）。

**③ 自己的消息会被当成别人发的 → 自问自答**
群里自己的消息 `sender_id == 2`，且正文**没有** `wxid_xxx:` 前缀（别人的都有）。

**④ 群消息正文带 `wxid_xxx:` 前缀**
不剥掉的话，昵称全是"某人"，而且 wxid 会被当聊天内容喂给模型。

**⑤ 唤醒词必须包含账号自己的昵称**
群友 @ 的是**账号昵称**，不是你给 AI 起的名字。

### 引用消息

微信 4.x 的引用消息正文是 appmsg XML：

```xml
<appmsg><title>人打的字</title>
  <refermsg><displayname>谁</displayname><content>被引用的原文</content></refermsg>
</appmsg>
```

**直接把这坨 XML 丢掉 → 模型只看到「[文件/链接/卡片]」→ 每句引用都回「我看不了链接」。**
要从 `<title>` 和 `<refermsg><content>` 里抠字。

### 识图（走了三条弯路）

**❌ 弯路一：`MediaDownloader()`**
要传 db：`MediaDownloader(db)`。

**❌ 弯路二：`download_image()`**
下来的是 `.wxgf` —— 微信 4.x 的**新加密格式**，
`decrypt_image()` 只认旧的 V1/V2 `.dat`，直接报"无法识别的图片加密格式"。

**❌ 弯路三：`download_image_original(scroll=True)`**
它会**操作界面**（滚动、截图）→ 会卡住，而且和主流程抢微信窗口。
**绝对不要在同步调用里做 UI 操作。**

**✅ 走通的路：微信自己的 `.dat` 缓存**

```
目录  <微信数据目录>/cache/<月>/Message/<hash>/Bubble/
文件  <md5>_b.dat        ← 文件名就是 md5
文件头 07 08 56 32 08 07  = V2_MAGIC ✓
```

`image_status(user, local_id)` 能拿到 md5，然后 glob 找文件、`decrypt_image()` 解密。

**不用下载、不用解密 CDN、不用碰界面。**

### 人设 / 输出

**⑥ 模型会跳戏成「百科模式」**
问角色相关的问题，它会答"XX 是《原神》里……"。人设里要明确禁止出现游戏相关词。

**⑦ 括号内心独白会漏出来**
实测漏过 `（然后呢？我该怎么接。总不能真答应，也不能真拒绝……）`——
**对方会看见她在权衡利弊，比直接拒绝还难受。** 代码层要清洗。

**⑧ 重启会把当天过掉的作息全部补发**
早上五点给你发「晚安」。要有过期窗口（`catchUpMinutes`）。

**⑨ 作息抖动不能每次重掷**
要存进状态文件，否则每次检查都掷骰子，同一件事几分钟内反复"到点"。

**⑩ 通用指令会压掉事件自带的结构**
「睡觉」应该是「我去睡了 → 你也早点睡 → 晚安」三段，
但通用指令里的"务必短，1~2 句"会把它压成一截。

**⑪ 「冷淡」是概率参数调出来的**
`sleepSilentChance: 0.45` + `ignoreChance: 0.02` 加起来就能让她像个陌生人。
**拟人化可以用「拖」，不能用「不理」。**

**⑫ 超上限时别把尾巴拼成长气泡**
我们会把多余气泡用 `\n` 拼成一条 —— 结果生出一条 60 多字、内部带换行的巨泡，
和"气泡要短"正好相反。**宁可多几条短的。**

---

### 手环 / 硬件（这部分坑最深）

**① Gadgetbridge 上游删掉了联网权限 —— 最隐蔽的一个**

```xml
<uses-permission android:name="android.permission.INTERNET" tools:node="remove" />
```

它主打"数据不出手机"，所以**主动把联网权限删掉**。

**症状**：推送设置全对、权限全给、手环连着、电脑端口开着 —— **就是没有任何数据，而且两边日志都什么都没有**。

因为 `HttpURLConnection` 抛异常时被"吞掉异常防止崩溃"的代码静静吃掉了。

**排查方法**：别猜，直接查编好的 APK：

```bash
aapt2 dump permissions app-debug.apk | grep INTERNET
```

**② 小米/MIUI 拦 adb 安装**

报 `INSTALL_FAILED_USER_RESTRICTED: Install canceled by user`，
手机通知栏显示「应用安装拦截」。

**解法**：开发者选项 → **「USB安装」**打开（注意不是"USB调试"，也不是"USB调试（安全设置）"，是单独的一项）。

**③ 批量同步只推最后一条样本 = 把一整天扔掉**

原来的写法：

```java
// ❌ 只发最后一条
HealthPush.pushSample(list.get(list.size() - 1), false);
```

一次同步几百上千条样本，结果只发了一条。

**改成整批发**（一天约 70KB，一次同步一发，完全可接受）。

**④ 逐条推 + 按"值变了就推" = 每秒一次 POST**

```java
// ❌ 值变了就推，无视间隔
if (!changed && !stale) return false;
```

心率每秒都在变 → 一天 86000 次请求。

**正确顺序是先卡频率**：

```java
// ✅ 间隔不到就不推，无论值变没变
if (now - prev[1] < minInterval * 1000L) return false;
```

**⑤ 重放历史数据时，别用墙上时钟算"持续多久"**

批量重放是**瞬间跑完**的。如果用 `System.currentTimeMillis()` 算"心率高了多久"，永远得到 0 秒 —— 一段真实 5 分钟的心率飙升会被判定为"没持续"。

**必须用样本自己的时间戳。**

**⑥ 睡眠数据不是实时的**

手环**只在实时流里推心率和步数**。压力、血氧、睡眠都是"存在手环上、等 App 来取"。

而 Gadgetbridge 默认只在**解锁手机时**才去取一次。要实时就得自己在设备服务里加定时抓取：

```java
onFetchRecordedData(RecordedDataTypes.TYPE_ACTIVITY
        | RecordedDataTypes.TYPE_STRESS
        | RecordedDataTypes.TYPE_SPO2
        | RecordedDataTypes.TYPE_SLEEP);
```

**⑦ 睡眠摘要要用"他真正醒来的时刻"判断时机**

睡眠数据什么时候到，取决于什么时候同步 —— 可能是几小时后。

如果不加判断，会出现**下午三点她说"你昨晚睡得不好"**。用摘要里的 `wake` 时间戳卡一个窗口（比如 2 小时）。

**⑧ Android 9+ 默认禁明文 HTTP**

发 `http://` 要在 manifest 里配：

```xml
<application android:networkSecurityConfig="@xml/network_security_config" ...>
<!-- network_security_config.xml -->
<base-config cleartextTrafficPermitted="true">
```

（Gadgetbridge 上游已经配好了，但你自己 fork 别的 App 要注意。）

**⑨ Tailscale 的登录回调进不了手机 App**

在电脑上打开手机的授权链接，服务端会显示设备已加入，**但手机 App 仍然是"未登录"状态** —— 因为 OAuth 回调是发给手机浏览器的，进不了 App。

**必须在手机的浏览器里完成整个登录流程。**

（而且 GitHub 在手机流量下经常打不开 —— 得先开梯子，登录完再换成 Tailscale。两个 VPN 抢一个槽位。）

**⑩ 截图预览分辨率和实际分辨率不一样**

用 adb 点击屏幕时，`read_image` 看到的预览图可能被缩放过。**必须按实际分辨率换算坐标**，否则点不中。

```powershell
# 预览 838x1862 → 实际 1080x2400
$scale = 1080 / 838   # 1.289
$realX = [int]($previewX * $scale)
```

---

## 成本

```
每条回复（纯文字）  ~2,400 token    ≈ ¥0.001
带一张图            ~2,500 token    ≈ ¥0.001
一天 100 条                        ≈ ¥0.2
一个月                             ≈ ¥6
```

`deepseek-flash` 命中缓存后约 **¥0.02/百万 token**。
只要你的人设文件稳定，绝大部分输入都是缓存命中。

---

## 后台常驻（Windows）

`boot.cmd` 会等微信启动、拉起 bot、挂了自动重启：

```bat
@echo off
set PY=python
set SCRIPT=%~dp0wechat-bot.py
:loop
  tasklist | findstr /i "Weixin.exe" >nul || (timeout /t 15 >nul & goto loop)
  "%PY%" "%SCRIPT%" >> "%~dp0logs\wechat-bot.out.log" 2>&1
  timeout /t 10 >nul
goto loop
```

想开机自启：把 `boot.cmd` 的快捷方式丢进
`shell:startup`（Win+R 输入即可打开）。

**注意**：从某些终端（比如 IDE、agent 环境）`Start-Process` 起的进程
会挂在那个进程树里，父进程一退就被杀。
要真正脱离，用 WMI：

```powershell
$si = ([wmiclass]'Win32_ProcessStartup').CreateInstance(); $si.ShowWindow = 0
([wmiclass]'Win32_Process').Create("cmd /c `"$dir\boot.cmd`"", $dir, $si)
```

---

## ⚠️ 免责声明

**用第三方程序操作微信违反微信用户协议，有封号风险。**

这个项目做了一些降低风险的设计（随机延迟、作息、概率不回、每日上限、
不主动加好友、不群发），但**不保证不被封**。

建议：

- **用小号**，别绑重要东西
- 别用来加好友、群发、营销
- 别在同一台机器上多开
- **自己承担风险**

本项目仅供学习和研究。

---

## 结构

```
wechat-ai-companion/
├── wechat-bot.py            # 主程序（单文件）
├── model-arena.py           # 对打台：横评模型/人设「像不像她」
├── console.html             # 实时看板（思考过程、成本、健康数据）
├── config.json              # 配置（存盘即生效）
├── 手环接入指南.md      # 小米手环 → Gadgetbridge → 电脑，完整搭建步骤
├── requirements.txt
├── boot.cmd                 # 后台常驻
├── personas/
│   ├── furina.md            # 芙宁娜（私聊）
│   └── groupmate.md         # 大肥鱼（群聊）
├── data/                    # 上下文（gitignore）
└── logs/                    # 日志（gitignore）
```

---

## 怎么知道改得好不好：对打台

改人设最容易的翻车方式是「凭感觉」。`model-arena.py` 让这件事可量化：

```bash
python model-arena.py --list       # 先看各家有哪些模型名
python model-arena.py              # 同一人设横评多个模型
python model-arena.py --personas   # **同一个模型，只换人设文件**
python model-arena.py --reuse      # 复用上次原始结果重算（判据改了不用重打 API）
```

六个维度的记分卡，写进 `arena-report.md`：

| 维度 | 查什么 |
|---|---|
| ① 总览 | 段数 / 单条最长 / 总字数 / 耗时 / 输出 token |
| ② 硬线违规 | 出戏词、括号独白、否认关系、见面安排、客服腔、空回复…… |
| ③ 傲 → 娇 | **末条气泡是不是暖的**（见下）、纯傲气泡数、提问数 |
| ④ 指纹与味道 | 意象密度、语气词、句内修正（`本神——咳，我`） |
| ⑤ 跨轮重复 | 同一个句子出现在 ≥2 条回复里 = 模板化 |
| ⑥ 逐条明细 | 24 条原文，自己扫 |

原始结果落在 `arena-raw.json`，所以报告代码出问题时能零成本重算。

### 一个实测结论：傲娇的「娇」是可以度量的

「傲娇」最常见的失败是**只有傲**：每轮都顶回去，末句停在嘴上，读起来是拒绝。

我们把它写成硬规则 ——「**每一轮的最后一条气泡必须是暖的**，傲最多占一条且不能是最后一条」——
然后跑对打验证（同模型，只换人设，6 题 × 2 轮）：

| | 末条是暖的 |
|---|---|
| 改之前 | **6/12**（50%） |
| 改之后 | **11/12**（92%） |

代价是平均耗时 +27%、输出 token +43%（她想的更多、写的更多）。

> 顺带一个坑：「末条暖不暖」这种判据第一版用了很窄的关键词表，把 `一辈子就一辈子`、
> `你先去睡。……我没走。` 这类明明很暖的句子判成了「不暖」，**误伤一半以上**。
> 所以报告里每一项都同时给原文 —— 正则只是筛，眼睛才是判据。

---

## License

MIT
