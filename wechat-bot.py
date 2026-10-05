#!/usr/bin/env python3
"""微信群友 / AI 伴侣（独立版）

不经过 DSH，不经过 OneBot —— 直接读微信、直接调 DeepSeek。
这样每轮只发「人设 + 这个会话最近几条」约 2k token，而不是 DSH 那份两万 token 的
agent 脚手架，成本差一个数量级。

    wechatauto（读/发微信） ──> 本文件 ──> DeepSeek API

要点：
- **每个会话一套独立上下文**，互不串味。群聊和私聊各自累计，单独滚动。
- 每个会话有独立的回复策略：主人私聊「说话就回」，群聊「被叫到 / 抽中概率」。
- 上下文落盘（contexts.json），重启不丢。

用法：
    python wechat-bot.py [--config config.json]
"""
import argparse
import glob
import json
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from wechatauto import WeChatDB
from wechatauto.db import Listener
from wechatauto.guia import quick_send

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG: Dict[str, Any] = {
    # ── DeepSeek ──────────────────────────────────────────────────────────
    "baseUrl": "https://api.deepseek.com",
    "model": "deepseek-flash",
    # 思考强度：off | low | high | max
    # 实测（deepseek-flash，同一句话）：
    #   off   0.8s  输出 8 token    "当然是你，毕竟我眼神不太好。"
    #   low   1.7s  输出 269 token  "当然是我，你只配当鱼饲料。"
    #   high  1.8s  输出 288 token  "照镜子去，除了我还能有谁？"
    # low 比 high 几乎不省，但质量明显强过 off。按 100 次/天算也就 ¥3/月，
    # 相比 DSH 那条路（¥60~300/月）可以忽略，所以默认开 low。
    # ⚠️ 开启思考后 temperature 不生效（官方明确说明），回复的随机性会略降。
    "thinking": "low",
    "temperature": 1.15,
    "maxTokens": 400,
    # key 的三种给法，按优先级：
    #   ① 环境变量 DEEPSEEK_API_KEY（推荐）
    #   ② 同目录下的 .credentials.yaml（已 gitignore，格式：DEEPSEEK_API_KEY: sk-xxx）
    #   ③ 直接填在下面的 apiKey 里（不推荐，会被提交上去）
    "credentialsFile": os.path.join(HERE, ".credentials.yaml"),
    "credentialsRef": "DEEPSEEK_API_KEY",
    "apiKey": "",

    # ── 人设 ──────────────────────────────────────────────────────────────
    "personaFile": os.path.join(HERE, "personas", "groupmate.md"),
    # 人设文件是给"会工具调用的 agent"写的，这里补一句把它拉回纯聊天
    "runtimeNote": (
        "你现在在一个微信聊天窗口里，对面是人。\n"
        "你没有工具、没有文件、没有任何系统权限，也看不见别的地方在聊什么。\n"
        "**直接说话就行**：不要写 [QQ] 之类的标记，不要输出任何格式符号。\n"
        "**不要写括号里的动作或旁白**（比如「（叹气）」），你只有台词。"
    ),

    # ── 每个会话的策略 ────────────────────────────────────────────────────
    # mode: "always" 说话就回 ｜ "wake" 被叫到或抽中概率才回 ｜ "off" 完全不理
    "chats": [
        {"name": "主人", "mode": "always", "kind": "private"},
        # ⚠️ 改成你自己的群名（微信里显示的那个名字，不是群号）
        {"name": "群聊A", "mode": "wake", "kind": "group"},
        {"name": "群聊B", "mode": "wake", "kind": "group"},
    ],
    # 命中即唤醒（正文包含任一即算被叫）
    # 群里唤醒它的词（@它 或 正文包含这些词）。
    # ⚠️ 账号自己的微信昵称会被自动加进来，不用重复填。
    "nicknames": ["机器人", "小助手"],
    # 没被叫到时，按这个概率随机搭话
    "wakeProbability": 0.05,
    # 唤醒后开的"会话跟随"窗口（秒）。**默认关**。
    # 关着的时候，群里只有「被叫到」和「抽中概率」才会回，其余一律只记录。
    # ⚠️ 开这个要小心：窗口必须只在**真正被叫到/抽中**时续期，绝不能因为
    #    "跟随而回复"也续期 —— 那样窗口永远关不掉，一被叫就变成每句都回。
    "followupSeconds": 0,

    # ── 上下文 ────────────────────────────────────────────────────────────
    # 每个会话带最近多少条历史（一问一答算两条）
    "historyLimit": 12,
    "contextFile": os.path.join(HERE, "data", "contexts.json"),

    # ── 行为保护（防封号 / 防刷屏）────────────────────────────────────────
    "sendDelayMin": 1.5,
    "sendDelayMax": 8.0,
    "dailySendLimit": 600,
    "quietHours": [2, 7],
    # 收到消息后先"看一会儿"再决定，避免秒回（像机器人）也顺便攒上下文
    "readDelay": 0.6,
    # 自己的 sender_id（群里自己的消息是它）。认错了会自问自答。
    "selfSenderId": 2,

    # ── 等他说完（去抖）──────────────────────────────────────────────────
    # 真人看到"在吗 / 我想问个事 / 算了"不会连回三次 —— 会等对方停下来再一起回。
    # 收到消息后等 debounceSeconds 秒，期间又来消息就把计时重置；
    # 但最多只等 debounceMaxSeconds 秒（免得对方滔滔不绝时一直不吭声）。
    "debounceSeconds": 3.0,
    "debounceMaxSeconds": 9.0,

    # ── 发送节奏（wechatauto 的防封号限速）────────────────────────────────
    # wechatauto 内置「120 秒内最多写 N 次」的突发限制，撞上就强制等约 49 秒。
    # 那是作者故意做的防封号措施，**不是卡死**。
    #   natural（默认）  N=6   打字间隔 2.5~6.0s   最保守
    #   fast             N=20  打字间隔 0.6~1.4s   快 4 倍，但更像机器人
    #   custom           用下面的 rhythmBurst 自己定
    "rhythmProfile": "natural",
    "rhythmBurst": 0,
    "rhythmWindow": 0,

    # ── 主动找人（真人不会永远只回不主动）──────────────────────────────────
    # 有 proactive: true 的会话才会主动开口。群聊不主动。
    "proactive": {
        "enabled": True,
        "checkSeconds": 30,
        # 他好久没说话 → 她找个烂借口开口（分钟区间，随机取）
        "silenceMin": [50, 160],
        # 她发了消息他多久没回 → 追一句
        "nudgeMin": [6, 20],
        # 追了还不回 → 开始生气
        "angryMin": [45, 110],
        # 生气了还不回 → 最后轻轻发一条，然后彻底沉默
        "lastMin": [200, 400],
        # 每天最多主动几次
        "dailyLimit": 5,
        # 两次主动之间至少隔多久（分钟）
        "minGapMin": 40,
        # 到点了也不是必发 —— 真人有"想发又没发"的时候
        "chance": 0.65,
        # 深夜不主动（本地时间区间，空数组 = 不限）
        "quietHours": [1, 8],
    },

    # ── 拟人化保护（比打字速度重要得多）──────────────────────────────────
    # wechatauto 的 rhythm 只管"打字多快、连发多密"。真正让风控起疑的是**行为模式**：
    # 24 小时在线、秒回、每次都回、延迟永远是 1~5 秒固定区间 —— 这几条比打字速度显眼得多。
    "humanize": {
        "enabled": True,
        # 睡觉时间（本地小时，跨零点写 [22, 7]）。真人这时候基本不活跃。
        "sleepHours": [2, 8],
        # 睡觉时收到消息：大部分拖很久才回（半梦半醒），或者干脆不回（等早上）
        "sleepDelayMin": 40,
        "sleepDelayMax": 150,
        # 睡觉时不回的概率。**谁睡觉看手机啊** —— 70% 就是不回。
        # 但不会变成"永远不回"：他连着发会把她吵醒（见 sleepEscalateFactor）。
        "sleepSilentChance": 0.70,
        # 每多收一条，不回概率乘这个系数 → 70% / 38% / 21% / 12% / 6%
        "sleepEscalateFactor": 0.55,
        # 多久以内的消息算"连着发"（分钟）
        "sleepEscalateWindowMin": 12,
        # 但作息低谷不是"装死"：他说了这些词就会把她弄醒。
        # （她自己就失眠，听到"睡不着"不可能继续睡。）
        "sleepWakeWords": ["睡不着", "失眠", "睡不着觉", "难受", "好难受", "想你了",
                           "好想你", "出事了", "怎么办", "哭了", "救", "急", "怕"],
        # 被叫醒之后也要迷糊一会儿（秒）
        "sleepWakeDelayMin": 6,
        "sleepWakeDelayMax": 25,
        # 平时偶尔"已读不回" —— 真人不总是回。但调高了她就冷，保持在 1% 以下。
        "ignoreChance": 0.008,
        # 偶尔"想了半天才回"，而不是永远落在 1~5 秒那个固定区间
        "slowReplyChance": 0.10,
        "slowReplyMin": 3,
        "slowReplyMax": 10,
    },

    # ── 作息表（到点主动说话）──────────────────────────────────────────────
    # 每一项都有随机抖动，而且**每天的抖动是固定的**（存进状态文件），
    # 不然每次检查都掷骰子，同一件事会在几分钟里反复"到点"。
    "schedule": {
        "enabled": True,
        # 起床（她凌晨一点睡，早上七点起）
        "wake": {"enabled": True, "at": "07:00", "jitterMin": 40},
        # 饭点 —— 到点主动问他吃了没
        "meals": [
            {"enabled": True, "name": "早饭", "at": "09:00", "jitterMin": 50},
            {"enabled": True, "name": "午饭", "at": "12:40", "jitterMin": 60},
            {"enabled": True, "name": "晚饭", "at": "18:40", "jitterMin": 60},
        ],
        # 睡觉（凌晨一点）
        "sleep": {"enabled": True, "at": "01:00", "jitterMin": 45},
        # 事件过期多久就不补发了（分钟）。
        # 不加这个的话，重启一次她就会把今天已经过掉的作息**全部补发一遍**
        # —— 早上五点给你发「晚安」。超过这个窗口就只标记已处理，不发。
        "catchUpMinutes": 60,
        # 一周里挑 1~2 天失眠：睡不着，想被哄
        "insomnia": {"enabled": True, "from": "23:30", "to": "00:45",
                     "minDays": 1, "maxDays": 2, "aheadMin": 2, "aheadMax": 6},
        # 空闲时段随机搭话：跟他沉默多久无关，就是她自己想说。
        # 每天挑 1~3 个随机时刻，避开饭点前后 avoidMinutes 分钟（免得刚问完吃了没又来一句）。
        "randomChats": {
            "enabled": True,
            "from": "09:30", "to": "23:30",
            "minPerDay": 1, "maxPerDay": 3,
            "avoidMinutes": 45,
        },
    },

    "logFile": os.path.join(HERE, "logs", "wechat-bot.log"),
}


# ───────────────────────────────── 基础 ─────────────────────────────────
CFG: Dict[str, Any] = {}
CFG_PATH = ""
CFG_MTIME = [0.0]
_logLock = threading.Lock()


def reload_config() -> None:
    """改 config.json 后自动生效，不用重启（调试时很省事）。"""
    if not CFG_PATH:
        return
    try:
        mt = os.stat(CFG_PATH).st_mtime
    except Exception:
        return
    if mt == CFG_MTIME[0]:
        return
    CFG_MTIME[0] = mt
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            new = json.load(f)
        CFG.update(new)
        log("配置已热重载")
    except Exception as e:
        log(f"配置热重载失败（忽略）: {e}")


def log(*a: Any) -> None:
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + " ".join(str(x) for x in a)
    with _logLock:
        print(line, flush=True)
        try:
            with open(CFG["logFile"], "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def read_credential(path: str, ref: str) -> str:
    """从 .credentials.yaml 里抠一条 `KEY: value`（不想为此引入 yaml 依赖）。"""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(rf"^\s*{re.escape(ref)}\s*:\s*(\S+)\s*$", line)
                if m:
                    return m.group(1)
    except Exception as e:
        log(f"凭据读取失败: {e}")
    return ""


def load_persona(path: Optional[str] = None) -> str:
    p = path or CFG.get("personaFile") or ""
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        log(f"人设文件读不到（{p}）：{e}")
        return ""


# ─────────────────────── 发送：恢复窗口 + 超时 + 串行 ───────────────────────
# 发消息是 UI 自动化（切会话→写输入框→点发送→回读数据库）。它有两个坑：
#   1) 微信窗口最小化 / 被挪到屏幕外时，控件树找不到元素，quick_send 会卡在那里
#      不回 —— 整个 worker 线程就此僵住，后面所有消息全部堵死。
#   2) 两个发送同时跑会互相打架，所以必须串行。
_send_lock = threading.Lock()
_send_stats = {"ok": 0, "timeout": 0, "error": 0, "stuck": 0}
# 真卡住之后先别急着继续发 —— 晾一会儿，让微信那边的 UI 状态自己恢复
_send_paused_until = [0.0]
# 复用一个 WeChatGUI 实例。quick_send 每次都 new 一个，虽然构造是惰性的，
# 但重复初始化没有意义；这里自己持有，顺便把"发送前拉到前台"也做掉。
_wx = [None]
# 最近几次发送的耗时，用来自适应降低气泡数（发送慢的时候别一次发五条）
_recent_send_secs: List[float] = []


def _restore_wechat_window() -> bool:
    """把微信主窗口从最小化/屏幕外拉回来，并尽量拉到前台。

    实测日志里 rect 经常是 (-31991, -32000, ...) —— 那是 Windows 给最小化窗口的
    坐标。UI 自动化在这种状态下会找不到发送按钮，然后**重跑一次布局校准**，
    那一轮要 50~70 秒。所以发送前先把它摆正，能省掉大部分慢路径。
    """
    try:
        import win32con
        import win32gui
    except Exception:
        return False          # 没装 pywin32 就算了，不影响主流程
    found = []

    def cb(h, _):
        try:
            if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) in ("微信", "WeChat"):
                found.append(h)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return False
    if not found:
        return False
    h = found[0]
    changed = False
    try:
        r = win32gui.GetWindowRect(h)
        offscreen = r[0] < -10000 or r[1] < -10000
        if win32gui.IsIconic(h) or offscreen:
            if win32gui.IsIconic(h):
                win32gui.ShowWindow(h, win32con.SW_RESTORE)
            # 最小化过的窗口恢复后仍可能是离屏坐标，挪回主屏
            if offscreen or win32gui.IsIconic(h):
                win32gui.SetWindowPos(h, 0, 80, 60, 1100, 800,
                                      win32con.SWP_NOZORDER | win32con.SWP_SHOWWINDOW)
            log("  微信窗口是最小化/离屏，已拉回来")
            changed = True
        # 有别的窗口挡着时，wechatauto 会自己去"最小化遮挡窗口"，过程很慢。
        # 这里先试着拉到前台，把它的活儿干在前面。
        if not win32gui.IsIconic(h):
            win32gui.SetForegroundWindow(h)
            changed = True
    except Exception as e:
        log(f"  摆正微信窗口失败: {e}")
    if changed:
        time.sleep(0.4)
    return changed


def _get_gui():
    """复用一个 WeChatGUI 实例（拿不到就回退到 quick_send）。"""
    if _wx[0] is None:
        try:
            from wechatauto.guia import WeChatGUI
            _wx[0] = WeChatGUI()
        except Exception as e:
            log(f"  WeChatGUI 初始化失败，回退 quick_send：{e}")
            _wx[0] = False
    return _wx[0] or None


def _do_send(text: str, who: str, verify: bool):
    gui = _get_gui()
    if gui is not None:
        return gui.send_msg(text, who, verify)
    return quick_send(text, who=who, verify=verify)


def _xml_unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
             .replace("&apos;", "'").replace("&amp;", "&"))


def _image_b64(raw: bytes) -> str:
    """图片字节 → base64 data URL（喂给视觉模型）。

    实测 deepseek-flash 本身就能看图（一张图 ~230-480 token，很便宜），
    不需要换 deepseek-v4-flash-vision-exp。
    """
    import base64
    if not raw:
        return ""
    if raw[:3] == b"\xff\xd8\xff":
        mime = "jpeg"
    elif raw[:4] == b"\x89PNG":
        mime = "png"
    elif raw[:4] == b"GIF8":
        mime = "gif"
    else:
        mime = "jpeg"
    return f"data:image/{mime};base64," + base64.b64encode(raw).decode()


def _image_bytes(db, user: str, local_id) -> Optional[bytes]:
    """把微信里的一张图拿到手（返回原始字节）。

    坑踩了很多次，最后是这条路：
      ① MediaDownloader 直接下载 → 得到 .wxgf，那是**加密的新格式**，
         decrypt_image 不认（它只认旧的 V1/V2 .dat）
      ② download_image_original 会去操作界面（滚动、截图）→ **会卡住**，
         而且和 bot 抢同一个微信窗口，绝对不能在主流程里调
      ③ 最后走通了：微信自己在 cache 里存了 .dat（头就是 V2_MAGIC），
         文件名是 md5 —— 用 image_status() 拿到 md5，glob 找文件，decrypt_image() 解密
    """
    if not user or local_id in (None, "", 0, "0"):
        return None
    try:
        from wechatauto.media import MediaDownloader
        dl = MediaDownloader(db)
        st = dl.image_status(user, int(local_id)) or {}
        md5 = str(st.get("md5") or "").strip()
        if len(md5) < 16:
            return None
        root = str(getattr(db, "account_dir", "") or "")
        cands = []
        for suffix in ("_b.dat", ".dat", "_t.dat"):
            hit = glob.glob(os.path.join(root, "**", md5 + suffix), recursive=True)
            if hit:
                cands = hit
                break
        for p in cands:
            try:
                raw = dl.decrypt_image(p)
            except Exception:
                continue
            if raw and (raw[:3] == b"\xff\xd8\xff" or raw[:4] == b"\x89PNG" or raw[:4] == b"GIF8"):
                return raw
    except Exception as e:
        log(f"  图片解密失败: {e}")
    return None


def _extract_appmsg_text(xml: str) -> str:
    """从微信的 appmsg XML 里抠出可读文字。

    引用消息（appmsg type=57）的结构大致是：
        <appmsg><title>人打的字</title>
          <refermsg><displayname>谁</displayname><content>被引用的原文</content></refermsg>
        </appmsg>
    链接/文件卡片是 <title> 标题 + <des> 描述。
    抠不出来（表情、红包、转账）就返回空字符串 —— 调用方会跳过。
    """
    if "<" not in xml:
        return xml.strip()

    def grab(tag: str, pattern: str) -> str:
        m = re.search(pattern, xml, re.S)
        return _xml_unescape(m.group(1).strip()) if m else ""

    title = grab("title", r"<title>(.*?)</title>")
    refer = grab("content", r"<refermsg>.*?<content>(.*?)</content>")
    des = grab("des", r"<des>(.*?)</des>")

    # 被引用的原文可能本身还是 XML（引用里套引用），再剥一层
    if refer.startswith("<"):
        refer = _extract_appmsg_text(refer)

    parts = []
    if refer:
        parts.append(f"（引用：{refer[:100]}）")
    if title:
        parts.append(title[:200])
    elif des:
        parts.append(des[:200])
    return " ".join(parts).strip()


def _quick_send_safe(text: str, who: str, verify: bool, soft: float, hard: float):
    """带两级超时的发送。

    为什么要两级：实测 wechatauto 在探测不到输入框时会**自动重跑布局校准**，
    整轮要 30~45 秒 —— 那不是卡死，是在干活。所以：
      - soft 秒没回来 → 只是打条日志，**继续等**（锁不能放）
      - hard 秒还没回来 → 才判定真卡住
    关键是**锁必须等线程真的结束才能放**，否则两个 UI 自动化会同时跑、互相打架。

    返回 (结果, 耗时, 状态)：OK / SLOW / TIMEOUT
    """
    box: Dict[str, Any] = {}

    def run():
        try:
            box["r"] = _do_send(text, who, verify)
        except Exception as e:                  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=run, daemon=True, name="wxsend")
    t0 = time.time()
    t.start()
    t.join(soft)
    if not t.is_alive():
        if "e" in box:
            return None, time.time() - t0, f"ERR {box['e']}"
        return box.get("r"), time.time() - t0, "OK"

    log(f"    发送超过 {soft:.0f}s（多半在重跑布局校准），继续等…")
    t.join(max(1.0, hard - soft))
    if t.is_alive():
        return None, time.time() - t0, "TIMEOUT"
    if "e" in box:
        return None, time.time() - t0, f"ERR {box['e']}"
    return box.get("r"), time.time() - t0, "SLOW"


# ─────────────────────────────── 上下文仓库 ───────────────────────────────
class ContextStore:
    """每个会话一套独立上下文。互不共享、各自滚动、落盘。

    用户明确要求：**不要重复用上下文** —— 群 A 的聊天不会被带进群 B，
    私聊也不会被带进群聊。
    """

    def __init__(self, path: str, limit: int) -> None:
        self.path = path
        self.limit = limit
        self.lock = threading.Lock()
        self.data: Dict[str, List[Dict[str, str]]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self.data = {k: v for k, v in raw.items() if isinstance(v, list)}
                log(f"上下文已载入：{len(self.data)} 个会话")
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"上下文读取失败（忽略）：{e}")

    def _save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            log(f"上下文落盘失败：{e}")

    def add(self, chat: str, role: str, content: str) -> None:
        with self.lock:
            lst = self.data.setdefault(chat, [])
            lst.append({"role": role, "content": content})
            # 滚动窗口：只留最近 limit 条
            if len(lst) > self.limit:
                del lst[: len(lst) - self.limit]
            self._save()

    def get(self, chat: str) -> List[Dict[str, str]]:
        with self.lock:
            return [dict(m) for m in self.data.get(chat, [])]

    def reset(self, chat: str) -> None:
        with self.lock:
            self.data.pop(chat, None)
            self._save()


# ──────────────────────────────── DeepSeek ────────────────────────────────
def _user_content(text: str, images: Optional[List[str]] = None):
    """构造 user 消息内容。有图片时用多模态数组格式。

    实测 deepseek-flash 本身就能看图，不需要换 vision 专用模型。
    """
    imgs = [u for u in (images or []) if u]
    if not imgs:
        return text
    parts: List[Dict[str, Any]] = [{"type": "text", "text": text or "（他发了一张图）"}]
    for u in imgs[:3]:                      # 一轮最多带 3 张，防爆 token
        parts.append({"type": "image_url", "image_url": {"url": u}})
    return parts


class LLM:
    def __init__(self, api_key: str) -> None:
        self.key = api_key
        self.url = CFG["baseUrl"].rstrip("/") + "/chat/completions"
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.last_in = 0
        self.last_out = 0
        self.last_cached = 0

    def chat(self, system: str, history: List[Dict[str, str]], user: str,
             images: Optional[List[str]] = None) -> str:
        # 滚动窗口有可能从一条 assistant 消息开始（把对应的 user 挤掉了），
        # 那样 API 会觉得上下文不完整。丢掉开头连续的 assistant。
        h = list(history)
        while h and h[0].get("role") != "user":
            h.pop(0)
        body: Dict[str, Any] = {
            "model": CFG["model"],
            "max_tokens": int(CFG.get("maxTokens") or 200),
            "stream": False,
            "messages": [{"role": "system", "content": system}] + h
                        + [{"role": "user", "content": _user_content(user, images)}],
        }
        mode = str(CFG.get("thinking") or "low").strip().lower()
        if mode in ("off", "disabled", "none", "false", "0"):
            # 关掉思考，这时 temperature 才生效
            body["thinking"] = {"type": "disabled"}
            body["temperature"] = float(CFG.get("temperature") or 1.15)
        else:
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = mode if mode in ("low", "high", "max") else "low"

        req = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                j = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            log(f"  API HTTP {e.code}: {detail}")
            return ""
        except Exception as e:
            log(f"  API 出错: {e}")
            return ""

        try:
            u = j.get("usage") or {}
            self.calls += 1
            self.last_in = int(u.get("prompt_tokens") or 0)
            self.last_out = int(u.get("completion_tokens") or 0)
            self.tokens_in += self.last_in
            self.tokens_out += self.last_out
            self.last_cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        except Exception:
            pass

        try:
            msg = j["choices"][0]["message"]
        except Exception:
            log(f"  返回结构异常: {str(j)[:200]}")
            return ""
        text = (msg.get("content") or "").strip()
        return text


# ────────────────────────────────── 主逻辑 ──────────────────────────────────
class Bot:
    def __init__(self) -> None:
        self.db = WeChatDB()
        self.me = self.db.get_self_info() or {}
        self.listener = Listener(self.db, interval=1.0)
        self.ctx = ContextStore(CFG["contextFile"], int(CFG.get("historyLimit") or 12))
        self.llm = LLM(CFG["_apiKey"])
        self.chat_by_name = {c["name"]: c for c in CFG.get("chats") or []}
        self._sys_cache: Dict[str, Any] = {}
        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        # 去抖缓冲：等他停下来再一起回，而不是每条都回一次
        self.pending: Dict[str, List[Dict[str, Any]]] = {}
        self.pending_at: Dict[str, float] = {}      # 这个会话最后收到消息的时刻
        self.pending_start: Dict[str, float] = {}   # 这个会话第一句的时刻
        self.deb_lock = threading.Lock()
        self.send_times: List[float] = []
        self.recent_sent: List[str] = []
        self.followup_until: Dict[str, float] = {}
        self.wxid_to_name: Dict[str, str] = {}
        # ── 主动找人用的状态 ──
        self.last_user_at: Dict[str, float] = {}     # 他最后一次说话
        self.recent_user_msgs: Dict[str, List[float]] = {}   # 他的消息时间戳（判断"连发"）
        # 每个会话最近的图片 (时间戳, local_id)。群聊里别人发的图 + 另一个人 @ 她，
        # 两条消息不同发送者、不会合并 —— 所以要靠这个"回头看"把图带上。
        self.recent_imgs: Dict[str, List[tuple]] = {}
        self.last_bot_at: Dict[str, float] = {}      # 她最后一次说话
        self.pro_stage: Dict[str, int] = {}          # 他这次沉默里她主动到第几步
        self.pro_need: Dict[str, Any] = {}           # （stage, 随机出来的阈值分钟）
        self.pro_times: List[float] = []             # 主动过的时间，算每日上限
        self.pro_busy = threading.Lock()

        log(f"模型 {CFG['model']} | 会话 {len(self.chat_by_name)} 个")
        for c in CFG.get("chats") or []:
            log(f"  - {c['name']}  策略={c.get('mode')}  人设={os.path.basename(self._persona_path(c['name']))}")

    # ── 人设（每个会话可以挂不同的）──
    def _persona_path(self, chat: str) -> str:
        conf = self.chat_by_name.get(chat) or {}
        return str(conf.get("personaFile") or CFG.get("personaFile") or "")

    def system_for(self, chat: str) -> str:
        """每个会话一套系统提示词 —— 私聊一套人设，群里另一套。

        按文件 mtime 缓存，改人设文件后下一条消息就生效。
        """
        conf = self.chat_by_name.get(chat) or {}
        p = self._persona_path(chat)
        note = str(conf.get("runtimeNote") or CFG.get("runtimeNote") or "")
        try:
            mt = os.stat(p).st_mtime
        except Exception:
            mt = 0.0
        key = f"{p}|{mt}|{len(note)}"
        c = self._sys_cache.get(chat)
        if c and c[0] == key:
            return c[1]
        sysm = (note + "\n\n---\n\n" + load_persona(p)).strip()
        self._sys_cache[chat] = (key, sysm)
        return sysm

    # ── 启动 ──
    def start(self) -> None:
        n = 0
        for c in CFG.get("chats") or []:
            name = c["name"]
            wx = self._wxid_of(name)
            if not wx:
                log(f"  ✗ 找不到聊天「{name}」——检查名字是否和微信里显示的一致")
                continue
            self.wxid_to_name[wx] = name
            try:
                self.listener.add_listener(wx, self._make_cb(wx, name))
                n += 1
            except Exception as e:
                log(f"  监听「{name}」失败: {e}")
        self.listener.start()
        threading.Thread(target=self._worker, daemon=True, name="worker").start()
        threading.Thread(target=self._proactive_loop, daemon=True, name="proactive").start()

        # 从上下文恢复"他说过话"的状态 —— last_user_at 是内存态，重启就没了，
        # 不恢复的话作息表（饭点、晚安、失眠）重启后永远不触发。
        # proactive: true 的会话**无条件**算作"认识他" —— 清空上下文之后也要照常发，
        # 不然刚清完她就不吭声了（踩过）。
        seeded = 0
        for name, conf in self.chat_by_name.items():
            if self.ctx.get(name) or conf.get("proactive"):
                self.last_user_at[name] = time.time()
                self.last_bot_at[name] = time.time()
                seeded += 1
        if seeded:
            log(f"恢复了 {seeded} 个会话的活跃状态（作息表要用）")

        log(f"开始监听 {n} 个会话")
        pc = CFG.get("proactive") or {}
        if pc.get("enabled"):
            who = [c["name"] for c in CFG.get("chats") or [] if c.get("proactive")]
            log(f"主动消息：开启，适用 {('、'.join(who)) or '(无)'}" if who else "主动消息：开启，但没有会话勾选 proactive")

    def _wxid_of(self, name: str) -> Optional[str]:
        try:
            for s in self.db.get_sessions(limit=200):
                u = s.get("username") or ""
                if not u:
                    continue
                if (self.db.get_nickname(u) or "") == name:
                    return u
        except Exception as e:
            log(f"  会话查找失败: {e}")
        return None

    # ── 入站 ──
    def _make_cb(self, wx: str, name: str):
        def cb(msg: Dict[str, Any], _lst: Any) -> None:
            try:
                self._on_message(wx, name, msg)
            except Exception as e:
                log(f"  处理消息出错: {e}")
        return cb

    def _on_message(self, chat: str, name: str, msg: Dict[str, Any]) -> None:
        # 每条入站消息都顺手检查一次配置有没有变。
        # 只在 worker 循环里检查是不够的：没有消息进队列时 worker 不跑，
        # 改了配置（比如删唤醒词）会一直不生效，直到下一条消息才姗姗来迟。
        reload_config()

        conf = self.chat_by_name.get(name) or {}
        mode = conf.get("mode") or "wake"
        if mode == "off":
            return

        # 自己的消息：群里自己是 sender_id=2，且正文没有 wxid 前缀。
        # 认不出来就会**自问自答**（模型把自己刚说的话当新消息再回一遍）。
        if int(msg.get("sender_id") or 0) == int(CFG.get("selfSenderId") or 2):
            return

        mtype = str(msg.get("type") or "")
        body = str(msg.get("content") or "")
        sender = str(msg.get("sender_username") or "")
        is_group = chat.endswith("@chatroom")
        # 图片：记下 local_id，待会儿用它去 cache 里找 .dat 解密
        img_lid = msg.get("local_id") if mtype == "图片" else None
        if img_lid:
            lst = self.recent_imgs.setdefault(name, [])
            lst.append((time.time(), img_lid))
            del lst[:-6]

        if is_group and not sender:
            m = re.match(r"^(wxid_[A-Za-z0-9_\-]+|\d+@chatroom):\s*", body)
            if m:
                sender = m.group(1)
                body = body[m.end():].lstrip()
            elif body and mtype == "文本":
                return                      # 群里文本却没有前缀 → 是自己

        if sender and sender == str(self.me.get("username") or ""):
            return

        norm = re.sub(r"\s+", "", body)
        if norm and norm in self.recent_sent:
            return                          # 回声

        # 卡片/引用/文件：**从 XML 里抠文字**，不要整段扔掉。
        # 微信 4.x 的引用消息正文是 appmsg XML，<title> 是人打的字，
        # <refermsg><content> 是被引用的原文。之前直接把这坨丢掉，
        # 模型只看到「[文件/链接/卡片]」，于是每句引用都回「我看不了链接」。
        if body.lstrip().startswith("<"):
            body = _extract_appmsg_text(body)
        if not body.strip():
            if img_lid:
                body = "[图片]"             # 图片留着，待会儿用视觉模型看
            else:
                return                      # 真·没内容（表情/红包/转账）就跳过

        if mtype and mtype != "文本" and not body.startswith("（引用"):
            body = f"[{mtype}] {body}"

        nick = self._nick(sender) or "某人"
        at_self = self._is_called(body)
        log(f"  入站 [{'群' if is_group else '私聊'}] {name} / {nick}: {body[:60]}")

        # 他说话了 → 主动消息的整条进度清零（生气也得有个了结）
        if not is_group:
            self.last_user_at[name] = time.time()
            # 记下时间戳 —— 睡觉时"他连发几条"要用它判断该不该被吵醒
            lst = self.recent_user_msgs.setdefault(name, [])
            lst.append(time.time())
            del lst[:-12]
            if self.pro_stage.get(name):
                log(f"    他回来了，主动进度从第 {self.pro_stage[name]} 步归零")
            self.pro_stage[name] = 0
            self.pro_need.pop(name, None)

        # 丢进去抖缓冲，不立刻处理 —— 等他停下来再一起回
        now = time.time()
        with self.deb_lock:
            self.pending.setdefault(name, []).append({
                "chat": name, "sender": nick, "body": body,
                "group": is_group, "at": at_self, "mode": mode,
                "img": img_lid, "chat_user": chat,
            })
            self.pending_at[name] = now
            self.pending_start.setdefault(name, now)
    def _nick(self, wxid: str) -> str:
        if not wxid:
            return ""
        try:
            return str(self.db.get_nickname(wxid) or wxid)
        except Exception:
            return wxid

    def _is_called(self, body: str) -> bool:
        """他是不是在叫它。

        ⚠️ **必须先剥掉引用部分** —— 引用别人（或引用聊天记录）时，
        被引用的原文里只要出现昵称就会误判成"被叫到"
        （踩过：引用了一段含 deepseek 的聊天记录，她就被唤醒了）。
        """
        s = re.sub(r"（引用：.*?）", "", body, flags=re.S)
        if not s.strip():
            s = body
        s = re.sub(r"\[[^\]]{1,12}\]", "", s)      # [图片] 之类的占位
        cands = list(CFG.get("nicknames") or [])
        me = self.me or {}
        for k in ("nick_name", "nickname", "alias"):
            v = str(me.get(k) or "").strip()
            if v:
                cands.append(v)
                cands.append(f"@{v}")
        for w in cands:
            w = str(w or "").strip()
            if w and w in s:
                return True
        return False
        for n in CFG.get("nicknames") or []:
            if n and n in body:
                return True
        # 群友 @ 的是**这个微信号的昵称**，不一定是你给 AI 起的名字。
        # 不自动带上它，别人 @ 半天都不会有反应。
        sn = str(self.me.get("nick_name") or "").strip()
        if sn and (f"@{sn}" in body or sn in body):
            return True
        return False

    # ── 决策 + 回复（单线程：quick_send 要驱动 UI，不能并发）──
    def _worker(self) -> None:
        """等他停下来，再一起回。

        真人不会对方每敲一句就回一句。收到消息后先等一小会儿：
        期间又来新消息就把计时重置，直到对方真的停下来了，才把攒下来的
        几段话合并成**一轮**去处理。但也设了总时长上限，免得对方滔滔不绝时她一直不吭声。
        """
        while True:
            reload_config()
            merged = None
            try:
                merged = self._take_ready()
            except Exception as e:
                log(f"  去抖检查出错: {e}")
            if merged is None:
                time.sleep(0.4)
                continue
            try:
                self._handle(merged)
            except Exception as e:
                log(f"  处理出错: {e}")
            time.sleep(float(CFG.get("readDelay") or 0.6))

    @staticmethod
    def _strip_repeat_prefix(body: str, sender: str) -> str:
        """去掉正文里**已经存在**的「昵称：」前缀。

        合并过的正文会被再加一次前缀，出现「主人：主人：你试试@他」这种脏数据
        （实测出现过，而且会一直留在上下文里污染后续对话）。
        """
        if not sender or not body:
            return body
        p = f"{sender}："
        while body.startswith(p):
            body = body[len(p):]
        return body

    def _take_ready(self) -> Optional[Dict[str, Any]]:
        """看看有没有哪个会话"说完了"。有就合并成一条返回。"""
        now = time.time()
        base = float(CFG.get("debounceSeconds") or 3.0)
        cap = float(CFG.get("debounceMaxSeconds") or 9.0)
        with self.deb_lock:
            for chat, items in list(self.pending.items()):
                if not items:
                    self.pending.pop(chat, None)
                    continue
                quiet = now - self.pending_at.get(chat, now)
                waited = now - self.pending_start.get(chat, now)
                if quiet < base and waited < cap:
                    continue
                # ⚠️ **只合并同一个人的连续消息**，不跨发送者。
                # 踩过的坑：群里别人 @ 了她、用户（主人）随口说一句，
                # 两条被合并成一批，结果**用户的话也被当成在叫她**，
                # 表现成"我在群里说话也会一直触发"。
                run = [items[0]]
                for x in items[1:]:
                    if x.get("sender") == run[0].get("sender"):
                        run.append(x)
                    else:
                        break
                rest = items[len(run):]
                if rest:
                    # 剩下的下一轮再处理（不丢）
                    self.pending[chat] = rest
                    self.pending_at[chat] = now
                    self.pending_start[chat] = now
                else:
                    self.pending.pop(chat, None)
                    self.pending_at.pop(chat, None)
                    self.pending_start.pop(chat, None)
                items = run
                if len(items) > 1:
                    log(f"  等他说完了（{len(items)} 条，等了 {waited:.1f}s），合并成一轮")
                first = items[0]
                if first["group"]:
                    body = "\n".join(
                        f'{i["sender"]}：{self._strip_repeat_prefix(i["body"], i["sender"])}'
                        for i in items)
                else:
                    body = "\n".join(self._strip_repeat_prefix(i["body"], i["sender"])
                                     for i in items)
                return {
                    "chat": chat,
                    "sender": first["sender"],
                    "body": body,
                    "group": first["group"],
                    "img": (next((i.get("img") for i in items if i.get("img")), None)
                            or self._recent_img(chat)),
                    "chat_user": first.get("chat_user") or "",
                    "at": any(i["at"] for i in items),
                    "mode": first["mode"],
                    "count": len(items),
                }
        return None

    def _handle(self, it: Dict[str, Any]) -> None:
        chat, sender, body = it["chat"], it["sender"], it["body"]
        now = time.time()
        if it.get("count", 1) > 1:
            log(f"  处理合并后的一轮（{it['count']} 条）：{body[:60]!r}")

        # 先记进这个会话自己的上下文（不管回不回，模型都需要看到）
        self.ctx.add(chat, "user", f"{sender}：{body}" if it["group"] else body)

        should = False
        by_wake = False        # 只有"真的被叫到/抽中"才允许续跟随窗口
        if it["mode"] == "always":
            should = True
            by_wake = True
        elif it["at"]:
            should = True
            by_wake = True
            log(f"    唤醒：被叫到")
        elif self.followup_until.get(chat, 0) > now:
            should = True
            log(f"    唤醒：会话跟随中")
        elif it["group"] and now - (self.last_bot_at.get(chat) or 0) < float(CFG.get("groupCooldownSeconds") or 45):
            # 群聊刚回过 → 这一小段时间内只理@，不抽概率。
            # 不加这个的话，群友随便聊两句她都要插嘴，看着像刷屏。
            log("    未唤醒（群里刚回过，冷却中）")
        elif random.random() < float(CFG.get("wakeProbability") or 0.05):
            should = True
            by_wake = True
            log(f"    唤醒：抽中概率")
        else:
            log(f"    未唤醒（只记录）")

        if not should:
            return

        if self._quiet() or self._over_limit():
            log(f"    静默 / 超限，不回")
            return

        # ── 拟人化：作息 / 不回 / 拖延 ─────────────────────────────────
        # rhythm 管的是"打字多快"，这里管的是"像不像一个有作息的人"。
        hz = CFG.get("humanize") or {}
        delay_override = 0.0
        wake_prompt = ""
        if hz.get("enabled") and not it.get("proactive"):
            if self._in_sleep(hz):
                words = hz.get("sleepWakeWords") or []
                called = [w for w in words if w and w in (it.get("body") or "")]
                if called:
                    # 她自己也失眠，听到这两句不可能继续睡 —— 被叫醒了
                    delay_override = random.uniform(float(hz.get("sleepWakeDelayMin") or 6),
                                                    float(hz.get("sleepWakeDelayMax") or 25))
                    log(f"    （作息低谷，但他说了「{called[0]}」，把她叫醒了：{delay_override:.0f}s）")
                    wake_prompt = ("**你本来在睡**（凌晨一点才睡），是被他这几句话弄醒的。"
                                   "所以语气是迷糊的、有点没睡够，但**马上就清醒了** —— "
                                   "因为你太清楚睡不着是什么滋味了。**不要写「我刚醒」这种解释**，"
                                   "直接接着他的话往下说，短，两句以内。")
                else:
                    # 他连着发会把她吵醒：第一条大概率不理，越连发越可能醒。
                    # 70% → 38% → 21% → 12% → 6%
                    win = float(hz.get("sleepEscalateWindowMin") or 12) * 60
                    nowt = time.time()
                    n = sum(1 for t in (self.recent_user_msgs.get(chat) or []) if nowt - t <= win)
                    base = float(hz.get("sleepSilentChance") or 0.70)
                    factor = float(hz.get("sleepEscalateFactor") or 0.55)
                    silent = base * (factor ** max(0, n - 1))
                    if random.random() < silent:
                        log(f"    （作息低谷：这条不回，{silent*100:.0f}%"
                            + (f"，他已连发 {n} 条" if n > 1 else "") + "）")
                        return
                    delay_override = random.uniform(float(hz.get("sleepDelayMin") or 40),
                                                    float(hz.get("sleepDelayMax") or 150))
                    log(f"    （作息低谷：被吵醒了，拖 {delay_override:.0f}s 再回"
                        + (f"，连发 {n} 条" if n > 1 else "") + "）")
            elif random.random() < float(hz.get("ignoreChance") or 0.02):
                log("    （这次不回 —— 真人不总是回）")
                return
            elif random.random() < float(hz.get("slowReplyChance") or 0.15):
                # ⚠️ **被 @ / 被叫到的时候不能拖** —— 群里你点名找她，
                # 她晾你 68 秒；而且这个 sleep 会阻塞唯一的 worker，
                # 后面所有消息都排队（踩过：连着三次 68/33/89 秒）。
                if by_wake and it.get("group"):
                    pass        # 直接回，不走拖延
                else:
                    delay_override = random.uniform(float(hz.get("slowReplyMin") or 3),
                                                    float(hz.get("slowReplyMax") or 10))
                    log(f"    （拖了一会儿才回：{delay_override:.0f}s）")

        if delay_override > 0:
            # 先拖够时间再生成 —— 这样她"回"的时候看到的是最新的上下文
            time.sleep(delay_override)

        sysm = self.system_for(chat)
        if wake_prompt:
            sysm = sysm + "\n\n---\n\n【现在的情况】\n" + wake_prompt
        imgs: List[str] = []
        lid = it.get("img")
        if lid:
            raw = _image_bytes(self.db, it.get("chat_user") or "", lid)
            if raw:
                b64 = _image_b64(raw)
                if b64:
                    imgs.append(b64)
                    log(f"    （带了张图，{len(raw) // 1024} KB）")
            else:
                log("    （图片没拿到，只能当纯文字回）")

        reply = self.llm.chat(sysm, self.ctx.get(chat)[:-1],
                              self.ctx.get(chat)[-1]["content"], images=imgs)
        if not reply:
            log(f"    模型没给出内容，跳过")
            return

        reply = self._clean(reply)
        if not reply:
            return

        self.ctx.add(chat, "assistant", reply)
        self._deliver(chat, reply, skip_delay=delay_override > 0)
        self.last_bot_at[chat] = time.time()

        # 续窗口：**只在这一轮是被叫到/抽中时**才续，否则跟随会自我延续、永不关闭
        win = float(CFG.get("followupSeconds") or 0)
        if win > 0 and by_wake:
            self.followup_until[chat] = time.time() + win
            log(f"    会话跟随窗口开到 {int(win)}s")

    def _clean(self, t: str) -> str:
        t = re.sub(r"^\[QQ\]|\[/QQ\]$", "", t.strip()).strip()
        # 偶尔会学聊天记录的格式给整段加个「芙：」前缀 —— 那是记录，不是消息内容
        t = re.sub(r"^\s*[\u4e00-\u9fa5A-Za-z]{1,4}[：:]\s*", "", t)
        t = re.sub(r"^\s*(?:作为|身为)[^，。]{0,12}(?:AI|人工智能|语言模型)[，,]\s*", "", t)
        t = t.replace("**", "").replace("`", "")
        t = re.sub(r"^[-*•]\s+", "", t, flags=re.M)
        # 🚨 括号里的内心独白 / 自我分析 —— **绝不能发出去**。
        # 实测漏过「（然后呢？我该怎么接。总不能真答应，也不能真拒绝……）」，
        # 对方会看见她在权衡利弊，那比直接拒绝还难受。
        t = re.sub(r"[（(][^）)]{0,140}?(?:该怎么|怎么办|该说|说什么|要不要|总不能|算了|装作|我是不是|应该|内心)[^）)]{0,140}?[）)]", "", t)
        t = re.sub(r"[（(]\s*(?:内心|心想|旁白|独白|os|OS)\b[^）)]{0,200}?[）)]", "", t)
        # 兜底：任何含问号、且超过 8 个字的括号内容，基本都是在纠结怎么写
        t = re.sub(r"[（(][^）)]{8,200}?[？?][^）)]{0,200}?[）)]", "", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()

    def _quiet(self) -> bool:
        q = CFG.get("quietHours") or []
        if len(q) != 2:
            return False
        h = time.localtime().tm_hour
        a, b = int(q[0]), int(q[1])
        return (a <= h < b) if a <= b else (h >= a or h < b)

    def _over_limit(self) -> bool:
        day = time.strftime("%Y-%m-%d")
        n = sum(1 for t in self.send_times if time.strftime("%Y-%m-%d", time.localtime(t)) == day)
        return n >= int(CFG.get("dailySendLimit") or 150)

    def _split_bubbles(self, text: str) -> List[str]:
        """把一轮回复拆成几个气泡 —— 真人聊天是一句一句发的，不是一段一段发的。

        模型现在被要求"一个念头一行"，这里就把每一行当成一个气泡。
        另外长行再按标点切一次，免得出现"一行 80 字"。
        """
        raw = [p.strip() for p in re.split(r"\n+", str(text or "")) if p.strip()]
        out: List[str] = []
        for p in raw:
            if len(p) <= 40:
                out.append(p)
                continue
            # 只按真正的句末标点切。**不含省略号** —— 中文里的「……」既能当停顿
            # 也能当句尾，按它切会把「……不过你问这个干嘛。」断成「…」+ 后半句，
            # 群里那条孤零零的「…」看起来像卡住了。
            p = re.sub(r"…{2,}", "…", p)
            for s in re.split(r"(?<=[。！？!?])", p):
                s = s.strip()
                if s:
                    out.append(s)

        # ── 再切一次：单条太长就拆 ────────────────────────────────────────
        # 真人不会一口气打完 40 个字再按发送。模型却经常这么干
        # （尤其是没有句末标点的长句，上面那步切不动）。
        # 先按逗号/顿号切，还是太长就硬切。
        limit = int(CFG.get("maxBubbleChars") or 26)
        if limit > 0:
            split2: List[str] = []
            for s in out:
                if len(s) <= limit:
                    split2.append(s)
                    continue
                parts = [x for x in re.split(r"(?<=[，,、；;：])", s) if x.strip()]
                # 悬空逗号只摘**气泡末尾**那一个 —— 拼在中间的逗号要留着，
                # 否则「排了三场戏第一场观众席只坐了一半」读起来是断的（踩过）。
                buf = ""
                for p in parts:
                    if len(buf) + len(p) <= limit:
                        buf += p
                    else:
                        if buf.strip():
                            split2.append(re.sub(r"[，,、；;：]\s*$", "", buf.strip()))
                        buf = p
                        while len(buf) > limit:      # 单段本身超长 → 硬切
                            split2.append(buf[:limit])
                            buf = buf[limit:]
                if buf.strip():
                    split2.append(re.sub(r"[，,、；;：]\s*$", "", buf.strip()))
            # 收尾：把过短的碎片并回上一条
            # （硬切会切出「普通人。」这种 4 字孤儿泡，很怪）
            minlen = int(CFG.get("minBubbleChars") or 6)
            merged: List[str] = []
            for s in split2:
                if merged and len(s) < minlen and len(merged[-1]) + len(s) <= limit + 4:
                    merged[-1] = merged[-1] + s
                else:
                    merged.append(s)
            out = merged

        cap = max(1, int(CFG.get("maxBubbles") or 3))
        if len(out) > cap:
            # ⚠️ 以前是把超出的尾巴用 \n 拼成一条 —— 结果生出一条 60 多字、
            # 内部还带换行的巨泡，正好和"气泡要短"相反（实测踩到）。
            # 现在：**宁可多几条短气泡，也不产生长气泡。**
            # 真人打一段长想法本来就是连着发好几条短消息。
            hard = max(cap, int(CFG.get("absoluteMaxBubbles") or (cap + 3)))
            if len(out) > hard:
                log(f"  气泡 {len(out)} 条超过硬上限 {hard}，截尾")
                out = out[:hard]
            if len(out) > cap:
                log(f"  气泡 {len(out)} 条（软上限 {cap}）—— 都是短句，照发")
        return out

    def _bubble_cap(self, chat: str = "") -> int:
        """这条回复最多几个气泡。

        **群聊单独限一条上限**（默认 2）—— 群里一次刷五条气泡很像机器人，
        也容易招人烦。私聊可以放开一点。
        另外发送慢的时候自适应降条数：慢（≤20s）砍到 3；很慢（>20s）砍到 2。
        """
        base = max(1, int(CFG.get("maxBubbles") or 3))
        # 判断这是不是群聊：
        #   ① 配置里显式写了 kind: "group"
        #   ② 会话名字本身是 @chatroom 号
        #   ③ 兜底：mode 是 wake 的一律当群（私聊配 always，群聊配 wake）
        conf = (self.chat_by_name.get(chat) or {}) if chat else {}
        is_group = (str(conf.get("kind") or "").lower() == "group"
                    or "@chatroom" in str(chat)
                    or str(conf.get("mode") or "").lower() == "wake")
        if is_group:
            base = min(base, max(1, int(CFG.get("groupMaxBubbles") or 2)))
        recent = _recent_send_secs[-5:]
        if not recent:
            return base
        avg = sum(recent) / len(recent)
        if avg > 20:
            return min(base, 2)
        if avg > 8:
            return min(base, 3)
        return base

    def _recent_img(self, chat: str):
        """这个会话最近几分钟内有没有图。

        群聊场景：别人发了张图，另一个人 @ 她「看这个」。
        两条消息发送者不同、不会合并，所以这一轮本身没图 —— 得回头找。
        """
        win = float(CFG.get("imageLookbackSeconds") or 180)
        now = time.time()
        for ts, lid in reversed(self.recent_imgs.get(chat, [])):
            if now - ts <= win:
                return lid
        return None

    def _in_sleep(self, hz: Dict[str, Any]) -> bool:
        """现在是不是她的作息低谷（睡觉时间）。"""
        hrs = hz.get("sleepHours") or []
        if len(hrs) != 2:
            return False
        h = time.localtime().tm_hour
        a, b = int(hrs[0]), int(hrs[1])
        return (a <= h < b) if a <= b else (h >= a or h < b)

    def _deliver(self, chat: str, text: str, skip_delay: bool = False) -> None:
        bubbles = self._split_bubbles(text)
        if not bubbles:
            return
        cap = self._bubble_cap(chat)
        if len(bubbles) > cap:
            # 超出的并进最后一条（保留换行，读起来还是分句的）
            log(f"  发送偏慢，气泡从 {len(bubbles)} 压到 {cap}")
            bubbles = bubbles[:cap - 1] + ["\n".join(bubbles[cap - 1:])]
        if time.time() < _send_paused_until[0]:
            left = int(_send_paused_until[0] - time.time())
            log(f"  ⏸ 发送通道冷却中（还剩 {left}s），这一轮放弃：{bubbles[0][:40]}")
            return
        soft = float(CFG.get("sendTimeoutSeconds") or 75.0)
        hard = float(CFG.get("sendHardTimeoutSeconds") or 150.0)
        first = random.uniform(float(CFG.get("sendDelayMin") or 0),
                               float(CFG.get("sendDelayMax") or 0))
        if skip_delay:
            first = 0.0        # 已经用"作息/拖延"的延迟等过了，别再加一次
        log(f"  出站 -> {chat}（{len(bubbles)} 个气泡，首条延迟 {first:.1f}s）")
        time.sleep(first)
        for i, b in enumerate(bubbles):
            if i > 0:
                gap = random.uniform(float(CFG.get("bubbleGapMin") or 0.5),
                                     float(CFG.get("bubbleGapMax") or 2.0))
                time.sleep(gap)
            _restore_wechat_window()
            # 锁必须等到线程结束才放（见 _quick_send_safe 的说明），
            # 这里只用它做串行保障，不做超时放弃。
            with _send_lock:
                res, secs, how = _quick_send_safe(b, chat, i == 0, soft, hard)
            if how == "TIMEOUT":
                _send_stats["timeout"] += 1
                # 真卡住了：停 5 分钟，别让 UI 自动化越堆越多
                _send_paused_until[0] = time.time() + float(CFG.get("sendPauseSeconds") or 300)
                log(f"    ⚠️ [{i+1}/{len(bubbles)}] 发送卡死（{secs:.0f}s），"
                    f"暂停发送 {int(CFG.get('sendPauseSeconds') or 300)}s 后重试：{b}")
                break
            if how.startswith("ERR"):
                _send_stats["error"] += 1
                log(f"    ✗ [{i+1}/{len(bubbles)}] 发送失败：{how}")
                continue
            _send_stats["ok"] += 1
            _recent_send_secs.append(secs)
            del _recent_send_secs[:-10]
            self.send_times.append(time.time())
            self.recent_sent.append(re.sub(r"\s+", "", b))
            del self.recent_sent[:-20]
            tail = ""
            if secs > 30:
                tail = "  ← wechatauto 在限速（防封号），不是卡死"
            elif how == "SLOW":
                tail = "  ⚠慢"
            log(f"    [{i+1}/{len(bubbles)}] {b}   -> {getattr(res, 'message', res)}"
                f"  ({secs:.1f}s){tail}")
        log(f"    本轮 token: in={self.llm.last_in} out={self.llm.last_out} "
            f"（发送 成功={_send_stats['ok']} 超时={_send_stats['timeout']} "
            f"失败={_send_stats['error']}）")

    # ── 主动找人 ──────────────────────────────────────────────────────────
    # 只会回不会主动的，永远像个客服。真人有"想找他""等他回""气他不回"这些动作。
    # 这里按"他多久没说话"分四个阶段，一次只走一步，走得越深越生气。

    _WAKE_PROMPT = (
        "你刚醒（早上七点）。给他发一条 —— **要有早上的感觉，而且要有劲儿**。\n"
        "按你平时那种浮夸又好玩的方式说，别干巴巴地报信息。\n"
        "**说你自己**：刚醒、还困、今天要干什么。他顺带提一句就行，不提也行。"
    )
    _MEAL_PROMPT = (
        "到{meal}的点了。你想问问他吃了没 —— 但**绝不直说「我在关心你」**，"
        "而且**问一句就够，不要追问**。\n\n"
        "要用你的语气：有点横、有点好玩、带一点自己的事。\n"
        "✅ 饭吃了没。我这边刚收工\n"
        "✅ 到点了。你吃什么，别又拿饼干糊弄\n"
        "✅ 喂。吃了没\n"
        "✅ 午饭。本大明星今天只有一杯茶，你最好吃点正经的\n"
        "❌ 你吃饭了吗？要记得好好吃饭哦           （客服）\n"
        "❌ 几点吃的？吃了什么？吃饱了吗？          （审问）"
    )
    _SLEEP_PROMPT = (
        "你要去睡了（凌晨一点）。这条的结构是固定的三步：\n"
        "1. **告诉他自己要去睡了**（不是问他睡没睡 —— 是你自己要下线了）\n"
        "2. **叮嘱他也早点睡**（用命令句或嫌弃的语气，别写得太温柔）\n"
        "3. **晚安**\n\n"
        "两三句以内，可以拆成两三个短气泡。语气是累的、随便的，不是深情告别。\n"
        "**绝对不要写成提问**。\n"
        "✅ 我去睡了\n"
        "✅ 你也早点睡，别熬\n"
        "✅ 晚安\n"
        "❌ 你睡了吗\n"
        "❌ 睡了没\n"
        "❌ 在吗，睡了吗"
    )
    _INSOMNIA_PROMPT = (
        "**你今天失眠。**怎么躺都不对，翻来覆去，脑子里全是事。"
        "你本来不想吵他，但最后还是发了。这条要**很短、很轻**，带一点示弱 —— "
        "但不能直接说「我睡不着，陪陪我」。"
        "可以是一句「……」，或者「你睡了吗」，或者莫名其妙的一句。"
    )
    _RANDOM_PROMPT = (
        "你现在闲着，没什么事。**突然想跟他说句话** —— 没有任何理由，就是想说。\n\n"
        "**「在干嘛」这种话是可以发的。** 有暗恋的人找对方搭话，开头就是这种没营养的。"
        "但光发一句太空了 —— 要么带一个**具体的东西**（刚发生的小事、突然想到的），"
        "要么**再补一句**（把真实意图露出来一点点）。\n\n"
        "1~2 句，可以拆成两三个很短的气泡。\n"
        "✅ 在干嘛\n"
        "✅ 喂\n"
        "✅ ……\n"
        "✅ 刚排完戏，脚疼\n"
        "✅ 今天路过那家店，还开着\n"
        "✅ 我吃了个特别难吃的东西，想骂人\n"
        "✅ 你猜我刚才看见什么了\n"
        "✅ 在干嘛。……没事。\n"
        "❌ 想你了（太直接，她说不出口）"
    )

    _PRO_STAGES = [
        # (用哪条间隔, 情境提示)
        ("silenceMin",
         "你已经有一阵子没跟他说话了。你想开口，但**绝不承认想他** —— 找一个很烂的借口"
         "（「我刚好路过」「你上次那个问题我想到答案了」），或者发一句最没内容的。"),
        ("nudgeMin",
         "你刚才发了消息，他到现在没回。你又发了一条 —— **假装不在意**，但明显在等他。"
         "可以问一句短的，或者干脆只发一个「？」。"),
        ("angryMin",
         "他还是没回。你**开始生气了** —— 但气里带着心虚。不要骂人，要那种"
         "「算了，当我没说」「你忙你的」的别扭；越生气越想装得不在乎。"),
        ("lastMin",
         "他到现在都没回。你已经不打算再问了，但最后还是没忍住发了一条 —— **很短，很轻**。"
         "可以带一点「你再不来就错过了」的暗示，但不要明说。"),
    ]

    def _pro_reason(self, stage: int) -> str:
        return self._PRO_STAGES[min(stage, len(self._PRO_STAGES) - 1)][1]

    # ── 作息表 ────────────────────────────────────────────────────────────
    def _sched_path(self) -> str:
        return os.path.join(os.path.dirname(CFG["contextFile"]), "schedule-state.json")

    def _sched_load(self) -> Dict[str, Any]:
        try:
            with open(self._sched_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _sched_save(self, d: Dict[str, Any]) -> None:
        try:
            p = self._sched_path()
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(p + ".tmp", p)
        except Exception as e:
            log(f"  作息状态保存失败: {e}")

    @staticmethod
    def _at_min(s: Any) -> Optional[int]:
        try:
            h, m = str(s).split(":")
            return (int(h) % 24) * 60 + int(m)
        except Exception:
            return None

    def _insomnia_today(self, sc: Dict[str, Any], st: Dict[str, Any]) -> bool:
        """今天是不是失眠日。

        做法：维护一个"接下来要失眠的日子"列表，用完就重新挑 1~2 天（2~6 天之后）。
        这样一周下来平均就是 1~2 次，而且**日子不固定** —— 真人失眠不会挑日子。
        """
        ins = sc.get("insomnia") or {}
        if not ins.get("enabled"):
            return False
        today = time.strftime("%Y-%m-%d")
        days = [d for d in (st.get("insomniaDays") or []) if d >= today]
        if not days:
            n = random.randint(int(ins.get("minDays") or 1), int(ins.get("maxDays") or 2))
            lo, hi = int(ins.get("aheadMin") or 2), int(ins.get("aheadMax") or 6)
            now = time.time()
            days = sorted({
                time.strftime("%Y-%m-%d", time.localtime(now + random.randint(lo, hi) * 86400))
                for _ in range(max(1, n))
            })
            log(f"  下次失眠日：{'、'.join(days)}")
        st["insomniaDays"] = days
        return today in days

    def _due_event(self, sc: Dict[str, Any], st: Dict[str, Any]):
        """返回一个"今天已经到点、但还没发"的作息事件 (key, 提示词)；没有就 None。"""
        today = time.strftime("%Y-%m-%d")
        fired_map = st.setdefault("fired", {})
        for d in list(fired_map.keys()):          # 只留今天的记录
            if d != today:
                fired_map.pop(d, None)
        fired = set(fired_map.get(today) or [])
        now = time.localtime()
        now_min = now.tm_hour * 60 + now.tm_min
        jit_map = st.setdefault("jitter", {})

        def due(name: str, base: Any, jitter: int) -> bool:
            b = self._at_min(base)
            if b is None:
                return False
            k = f"{today}:{name}"
            j = jit_map.get(k)
            if j is None:
                j = random.randint(-abs(jitter), abs(jitter))
                jit_map[k] = j
            return now_min >= b + j

        def stale(name: str, base: Any, jitter: int, catch: int) -> bool:
            """到点太久就算了，别补发（重启后最容易踩这个）。"""
            b = self._at_min(base)
            if b is None:
                return False
            j = jit_map.get(f"{today}:{name}") or 0
            return now_min > b + j + catch

        events = []
        w = sc.get("wake") or {}
        if w.get("enabled"):
            events.append(("起床", w.get("at") or "07:00", int(w.get("jitterMin") or 40), self._WAKE_PROMPT))
        for m in sc.get("meals") or []:
            if m.get("enabled"):
                nm = str(m.get("name") or "饭点")
                events.append((nm, m.get("at") or "12:00", int(m.get("jitterMin") or 60),
                               self._MEAL_PROMPT.replace("{meal}", nm)))
        s = sc.get("sleep") or {}
        if s.get("enabled"):
            events.append(("睡觉", s.get("at") or "02:00", int(s.get("jitterMin") or 50), self._SLEEP_PROMPT))

        catch = int(sc.get("catchUpMinutes") or 60)
        for name, base, jitter, prompt in events:
            if name in fired:
                continue
            if not due(name, base, jitter):
                continue
            if stale(name, base, jitter, catch):
                log(f"  作息 [{name}] 已经过了 {catch} 分钟以上，跳过（不补发）")
                fired_map.setdefault(today, []).append(name)
                continue
            return name, prompt

        # 失眠：单独处理，一晚上只发一次
        if "失眠" not in fired and self._insomnia_today(sc, st):
            ins = sc["insomnia"]
            lo = self._at_min(ins.get("from") or "00:20")
            hi = self._at_min(ins.get("to") or "01:40")
            if lo is not None and hi is not None:
                k = f"{today}:失眠"
                j = jit_map.get(k)
                if j is None:
                    j = random.randint(lo, hi)
                    jit_map[k] = j
                # 过了 21 点或者凌晨之后才算到点（允许跨零点）
                if (now_min >= j and now_min < 5 * 60) or (now_min >= 21 * 60 and j >= now_min):
                    if now_min >= j + catch and now_min < 12 * 60:
                        log("  作息 [失眠] 已经过了太久，跳过（不补发）")
                        fired_map.setdefault(today, []).append("失眠")
                    else:
                        return "失眠", self._INSOMNIA_PROMPT

        # ── 空闲随机搭话 ────────────────────────────────────────────────
        rc = sc.get("randomChats") or {}
        if rc.get("enabled"):
            slots = self._random_slots(sc, st, today, now_min, jit_map)
            for idx, t in enumerate(slots):
                name = f"闲聊{idx + 1}"
                if name in fired or now_min < t:
                    continue
                if now_min > t + catch:
                    fired_map.setdefault(today, []).append(name)
                    continue
                return name, self._RANDOM_PROMPT
        return None

    def _random_slots(self, sc: Dict[str, Any], st: Dict[str, Any], today: str,
                      now_min: int, jit_map: Dict[str, Any]) -> List[int]:
        """今天随机搭话的时刻（分钟）。一天只挑一次，存进状态文件。"""
        key = f"{today}:闲聊槽位"
        cached = jit_map.get(key)
        if isinstance(cached, list) and cached:
            return sorted(int(x) for x in cached)

        rc = sc.get("randomChats") or {}
        lo = self._at_min(rc.get("from") or "09:30") or (9 * 60 + 30)
        hi = self._at_min(rc.get("to") or "23:30") or (23 * 60 + 30)
        if hi <= lo:
            hi = lo + 60
        n = random.randint(int(rc.get("minPerDay") or 1), int(rc.get("maxPerDay") or 3))
        avoid = int(rc.get("avoidMinutes") or 45)

        # 已经被固定作息占掉的时段（前后 avoid 分钟都避开）
        blocked = []
        w = sc.get("wake") or {}
        if w.get("enabled"):
            blocked.append(self._at_min(w.get("at")) or 0)
        for m in sc.get("meals") or []:
            if m.get("enabled"):
                blocked.append(self._at_min(m.get("at")) or 0)
        s = sc.get("sleep") or {}
        if s.get("enabled"):
            blocked.append(self._at_min(s.get("at")) or 0)

        def ok(t: int) -> bool:
            for b in blocked:
                # 跨零点的作息（睡觉 02:00）要绕回来比
                d = min(abs(t - b), 1440 - abs(t - b))
                if d < avoid:
                    return False
            return True

        slots: List[int] = []
        # 把 [lo, hi] 等分成 n 段，每段里随机挑一个 —— 纯随机会扎堆
        # （实测抽到 20:11 / 21:08 / 22:52 全挤在晚上，真人不会一小时内发三条）。
        span = max(1, (hi - lo) // max(1, n))
        for seg in range(n):
            seg_lo = lo + seg * span
            seg_hi = (lo + (seg + 1) * span) if seg < n - 1 else hi
            for _ in range(40):
                t = random.randint(seg_lo, max(seg_lo, seg_hi))
                if not ok(t):
                    continue
                if any(abs(t - x) < 45 for x in slots):
                    continue
                slots.append(t)
                break
        slots.sort()
        jit_map[key] = slots
        if slots:
            log(f"  今天的随机搭话时刻：{'、'.join('%02d:%02d' % (t // 60, t % 60) for t in slots)}")
        return slots

    def _pro_active(self) -> bool:
        pc = CFG.get("proactive") or {}
        if not pc.get("enabled"):
            return False
        q = pc.get("quietHours") or []
        if len(q) == 2:
            h = time.localtime().tm_hour
            a, b = int(q[0]), int(q[1])
            if (a <= h < b) if a <= b else (h >= a or h < b):
                return False
        day = time.strftime("%Y-%m-%d")
        n = sum(1 for t in self.pro_times if time.strftime("%Y-%m-%d", time.localtime(t)) == day)
        if n >= int(pc.get("dailyLimit") or 5):
            return False
        if self.pro_times:
            gap = (time.time() - max(self.pro_times)) / 60.0
            if gap < float(pc.get("minGapMin") or 40):
                return False
        return True

    def _proactive_loop(self) -> None:
        while True:
            time.sleep(float((CFG.get("proactive") or {}).get("checkSeconds") or 30))
            try:
                self._maybe_proactive()
            except Exception as e:
                log(f"  主动消息检查出错: {e}")

    def _pro_targets(self) -> List[str]:
        """哪些会话允许她主动开口。"""
        return [n for n, c in self.chat_by_name.items() if c.get("proactive")]

    def _maybe_proactive(self) -> None:
        pc = CFG.get("proactive") or {}
        if not pc.get("enabled"):
            return
        if any(self.pending.values()):
            return                       # 他还在说话（或还没到去抖时限），别插队

        # ── 1) 作息表优先 ────────────────────────────────────────────────
        # 饭点、起床、睡觉、失眠 —— 这些是"约定好的时刻"，不受每日上限约束
        # （上限是用来防她话太多，不是用来取消她的作息的）。
        sc = CFG.get("schedule") or {}
        if sc.get("enabled"):
            st = self._sched_load()
            targets = [n for n in self._pro_targets() if (self.last_user_at.get(n) or 0) > 0]
            if targets:
                try:
                    ev = self._due_event(sc, st)
                except Exception as e:
                    log(f"  作息检查出错: {e}")
                    ev = None
                self._sched_save(st)     # 抖动值 / 失眠日要落盘，避免重复掷骰子
                if ev:
                    name, prompt = ev
                    today = time.strftime("%Y-%m-%d")
                    st.setdefault("fired", {}).setdefault(today, []).append(name)
                    self._sched_save(st)
                    chat = targets[0]
                    log(f"  作息 [{name}] -> {chat}")
                    with self.pro_busy:
                        self.pro_times.append(time.time())
                        self._pro_say(chat, prompt, scheduled=True)
                    return

        # ── 2) 空窗升级（他沉默久了 / 他没回 / 生气了）──────────────────
        if not self._pro_active():
            return
        now = time.time()
        for name in self._pro_targets():
            last_u = self.last_user_at.get(name) or 0.0
            if last_u <= 0:
                continue                 # 他从来没说过话，就别自作多情了
            last_b = self.last_bot_at.get(name) or 0.0
            stage = self.pro_stage.get(name, 0)
            if stage >= len(self._PRO_STAGES):
                continue                 # 这一轮已经沉默到底了，等他先说话

            key = self._PRO_STAGES[stage][0]
            lo_hi = pc.get(key) or [60, 120]
            need = self.pro_need.get(name)
            if not need or need[0] != stage:
                need = (stage, random.uniform(float(lo_hi[0]), float(lo_hi[1])))
                self.pro_need[name] = need
            ref = max(last_u, last_b)
            idle_min = (now - ref) / 60.0
            if idle_min < need[1]:
                continue
            if random.random() > float(pc.get("chance") or 0.65):
                # 到点了但这次没发 —— 真人也有"想发又放下手机"的时候。
                # 把阈值往前推一点，别每 30 秒都掷一次骰子（那样迟早必中）。
                self.pro_need[name] = (stage, need[1] + random.uniform(10, 40))
                continue

            with self.pro_busy:
                if not self._pro_active():
                    return
                self.pro_stage[name] = stage + 1
                self.pro_need.pop(name, None)
                self.pro_times.append(time.time())
                log(f"  主动 [{name}] 第 {stage+1} 步（他已安静 {idle_min:.0f} 分钟）")
                self._pro_say(name, self._pro_reason(stage))
            return                       # 一次只处理一个会话

    def _pro_say(self, chat: str, reason: str, scheduled: bool = False) -> None:
        if scheduled:
            # 作息事件自带结构要求（比如睡觉要「说要去睡 + 叮嘱他 + 晚安」），
            # 这里**不能**再压一个"1~2 句"—— 会把结构压掉，只发半截。
            extra = ("\n\n**这条是你主动发的，没有任何人跟你说话。** "
                     "按上面【现在的情况】里的要求写，**直接发内容**，不要解释你为什么发。")
        else:
            extra = ("\n\n**这条是你主动发的，没有任何人跟你说话。** 直接写你要发的内容，"
                     "1~2 句，短。不要解释你为什么发，不要写「我主动来找你」这种元话。")
        sysm = self.system_for(chat) + "\n\n---\n\n【现在的情况】\n" + reason + extra
        hist = self.ctx.get(chat)
        trigger = "（现在是你先开口。按上面的情境写出你要发的那条微信消息。）"
        reply = self.llm.chat(sysm, hist, trigger)
        if not reply:
            log("    模型没给出内容，跳过")
            return
        reply = self._clean(reply)
        if not reply:
            return
        self.ctx.add(chat, "assistant", reply)
        self.last_bot_at[chat] = time.time()
        self._deliver(chat, reply)


def main() -> None:
    global CFG, CFG_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--reset", action="store_true",
                    help="清空所有会话的上下文然后退出")
    ap.add_argument("--reset-chat", default="", metavar="名字",
                    help="只清空指定会话的上下文然后退出（如 --reset-chat 主人）")
    ap.add_argument("--list-chats", action="store_true",
                    help="列出各会话现在存了多少条上下文然后退出")
    args = ap.parse_args()
    CFG_PATH = os.path.abspath(args.config)
    try:
        CFG_MTIME[0] = os.stat(CFG_PATH).st_mtime
    except Exception:
        CFG_MTIME[0] = 0.0

    CFG = dict(DEFAULT_CONFIG)
    if os.path.exists(args.config):
        try:
            with open(args.config, encoding="utf-8") as f:
                CFG.update(json.load(f))
        except Exception as e:
            print(f"配置读取失败，用默认值: {e}", file=sys.stderr)
    else:
        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"已生成默认配置: {args.config}")

    # ── 上下文管理（不需要调模型，所以放在拿 key 之前）──
    if args.reset or args.reset_chat or args.list_chats:
        cpath = CFG["contextFile"]
        try:
            with open(cpath, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except FileNotFoundError:
            data = {}
        except Exception as e:
            print(f"上下文读不出来：{e}", file=sys.stderr)
            sys.exit(1)

        if args.list_chats:
            if not data:
                print("（还没有任何上下文）")
            for k, v in data.items():
                print(f"  {k}: {len(v)} 条")
            return
        if args.reset_chat:
            name = args.reset_chat
            if name in data:
                n = len(data.pop(name))
                with open(cpath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                print(f"已清空「{name}」（原来 {n} 条），其余会话保留")
            else:
                print(f"没有找到会话「{name}」")
                print("现有的:", "、".join(data.keys()) or "（无）")
        if args.reset:
            backup = f"{cpath}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                with open(cpath, encoding="utf-8") as src, open(backup, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
                print(f"已备份到 {backup}")
            except Exception:
                pass
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=1)
            print(f"已清空全部 {len(data)} 个会话的上下文")
        return

    # key 优先级：环境变量 > 配置文件里的 apiKey > .credentials.yaml
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        key = str(CFG.get("apiKey") or "").strip()
    if not key:
        key = read_credential(CFG["credentialsFile"], CFG["credentialsRef"])
    if not key:
        print("✗ 拿不到 API key。三种给法选一个：", file=sys.stderr)
        print("    ①  export DEEPSEEK_API_KEY=sk-xxx        （推荐）", file=sys.stderr)
        print("    ②  在 config.json 里填 apiKey", file=sys.stderr)
        print("    ③  在本目录放 .credentials.yaml，内容一行：DEEPSEEK_API_KEY: sk-xxx", file=sys.stderr)
        sys.exit(1)
    CFG["_apiKey"] = key

    os.makedirs(os.path.dirname(CFG["logFile"]), exist_ok=True)

    # 设置 wechatauto 的节奏档位（防封号限速）
    try:
        import wechatauto.rhythm as _rhythm
        prof = str(CFG.get("rhythmProfile") or "natural").strip().lower()
        if prof == "custom":
            kw = {}
            if int(CFG.get("rhythmBurst") or 0) > 0:
                kw["burst"] = int(CFG["rhythmBurst"])
            if float(CFG.get("rhythmWindow") or 0) > 0:
                kw["window"] = float(CFG["rhythmWindow"])
            p = _rhythm.configure(**kw)
            print(f"wechatauto 节奏：custom burst={p.burst} window={p.window}")
        elif prof:
            p = _rhythm.set_profile(prof)
            print(f"wechatauto 节奏：{p.name} burst={p.burst} window={p.window}")
    except Exception as e:
        print(f"设置 wechatauto 节奏失败（忽略）：{e}", file=sys.stderr)

    log("=" * 60)
    log(f"启动 key={key[:6]}...{key[-4:]}")
    try:
        Bot().start()
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("已停止")


if __name__ == "__main__":
    main()
