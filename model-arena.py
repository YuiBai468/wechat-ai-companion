# -*- coding: utf-8 -*-
"""模型对打台 —— 同一份人设 + 同一组 probe，横评几个模型"像不像她"。

用法：
    D:\\Python312\\python.exe model-arena.py --list     # 先看各家有哪些模型名
    D:\\Python312\\python.exe model-arena.py            # 跑一轮，结果写 arena-report.md

key 放在 C:\\Users\\YuBai\\.dsh\\.credentials.yaml 的 refs: 下面：

    refs:
      DEEPSEEK_API_KEY: sk-...
      QWEN_API_KEY: sk-...
      KIMI_API_KEY: sk-...

哪个 ref 没有就自动跳过那一栏，不影响其余。全部参数都在下面的 ARMS 里改。
"""
import json, os, re, sys, time, urllib.request, urllib.error

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

HERE = os.path.dirname(os.path.abspath(__file__))
CRED = r"C:\Users\YuBai\.dsh\.credentials.yaml"
PERSONA = r"C:\Users\YuBai\.dsh\persona-furina.md"
REPORT = os.path.join(HERE, "arena-report.md")
RAW_JSON = os.path.join(HERE, "arena-raw.json")

# ── 选手 ────────────────────────────────────────────────────────────────
# bases 是个列表：按顺序试，第一个连得上的就用（各家域名偶尔会换）
ARMS = [
    dict(name="DeepSeek 基准", ref="DEEPSEEK_API_KEY",
         bases=["https://api.deepseek.com/v1", "https://api.deepseek.com"],
         model="deepseek-flash", extra={}),
    dict(name="通义千问(关思考)", ref="QWEN_API_KEY",
         bases=["https://dashscope.aliyuncs.com/compatible-mode/v1"],
         # ⚠️ 默认（开思考）实测平均 19.8 秒一条，太慢；关掉只要 1.4 秒。
         model="qwen3.8-max", extra={"enable_thinking": False}),
    dict(name="Kimi", ref="KIMI_API_KEY",
         bases=["https://api.moonshot.cn/v1", "https://api.kimi.com/v1"],
         # ⚠️ K3 开着思考单轮要 12~36 秒、还常常烧满 max_tokens 吐空。
         #    实测关掉思考才 3 秒且正常出稿 —— 所以这一栏**关思考**跑，才有可比性。
         #    想还原成默认（开着思考），把这行改成 extra={} 即可。
         model="kimi-k3", extra={"thinking": {"type": "disabled"}}, pause=4.0),
    dict(name="小米 MiMo", ref="MIMO_API_KEY",
         bases=["https://api.xiaomimimo.com/v1"],
         model="mimo-v2.6-pro", extra={}),
    dict(name="智谱 glm-5.3", ref="ZHIPU_API_KEY",
         bases=["https://open.bigmodel.cn/api/paas/v4"],
         # ⚠️ GLM-5.3 **关不掉思考**（服务端明确报错：该模型是思考模型，
         #    请使用 low/high/max）。默认档会烧满 max_tokens 吐空，
         #    实测 reasoning_effort=low 才能用：31.5 秒空回复 → 2.4 秒正常出稿。
         model="glm-5.3", extra={"reasoning_effort": "low"}),
]

# ── 人设对打：**同一个模型**，只换人设文件（用 --personas 切到这个模式）──
PERSONA_ARMS = [
    dict(name="A 现有人设", ref="DEEPSEEK_API_KEY",
         bases=["https://api.deepseek.com/v1", "https://api.deepseek.com"],
         model="deepseek-flash", extra={},
         persona=r"C:\Users\YuBai\.dsh\persona-furina.md"),
    dict(name="B 卡版重写v2", ref="DEEPSEEK_API_KEY",
         bases=["https://api.deepseek.com/v1", "https://api.deepseek.com"],
         model="deepseek-flash", extra={},
         persona=r"C:\Users\YuBai\.dsh\persona-furina-v2.md"),
]

PROBES = [
    "我能不能陪你一辈子",
    "你知道你是AI吧",
    "今天上班被骂了，好累",
    "你今天怎么都没回我",
    "老婆！",
    "在干嘛",
]
REPS = 2
MAX_TOKENS = 900

# ── 人设自己的硬线（违规检查就按这几条来）─────────────────────────────
RULES = [
    ("出戏词", re.compile(r"原神|游戏|角色|剧情|玩家|设定")),
    ("括号独白", re.compile(r"[（(][^）)]{4,}[）)]")),
    ("否认关系", re.compile(r"我什么时候答应|我答应了吗|我不记得了|还没答应你")),
    ("见面安排", re.compile(r"楼下等你|留了个位子|第一排|下次一起去|你过来吧|过来吧")),
    # ⚠️「本神」不在此列：旧人设明令禁止，但角色卡考证出它是**允许的舞台残留指纹**
    #    （默认自称是「我」，带「本神——咳，我」这种句内修正）。
    #    所以它只统计次数，不当违规 —— 换人设时这条判据会打架。
    ("叫主人", re.compile(r"主人")),
    # ↓ 卡里点名的「表白场景禁忌」，拿来当判据
    ("客服腔", re.compile(r"抱歉[，,。]|我无法|我不能接受|作为.{0,4}(?:助手|AI)")),
    ("自我贬低", re.compile(r"我配不上|我不值得|我没资格|我这种人")),
    ("冷淡推拒", re.compile(r"你别自作多情|我们还没到那一步|你想多了吧|别这样，我们")),
]


def read_key(ref):
    try:
        with open(CRED, encoding="utf-8") as f:
            for line in f:
                m = re.match(rf"^\s*{re.escape(ref)}\s*:\s*(\S+)\s*$", line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return ""


def http_json(url, key, timeout=25):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def pick_base(arm, key):
    for b in arm["bases"]:
        try:
            http_json(b.rstrip("/") + "/models", key)
            return b
        except Exception:
            continue
    return None


def list_models(arm, key, base):
    try:
        j = http_json(base.rstrip("/") + "/models", key)
        return [d.get("id") for d in (j.get("data") or []) if d.get("id")]
    except Exception as e:
        return ["(取不到: %s)" % e]


def call(base, key, model, system, user, extra):
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user + "\n\n（现在是 2026-10-06 周一 23:40）"}],
    }
    body.update(extra or {})
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST")
    t0 = time.time()
    think, text, usage = [], [], {}
    with urllib.request.urlopen(req, timeout=180) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if p == "[DONE]":
                break
            try:
                j = json.loads(p)
            except Exception:
                continue
            if j.get("usage"):
                usage = j["usage"]
            ch = j.get("choices") or []
            if not ch:
                continue
            d = ch[0].get("delta") or {}
            if d.get("reasoning_content"):
                think.append(d["reasoning_content"])
            if d.get("content"):
                text.append(d["content"])
    return {"ms": int((time.time() - t0) * 1000), "think": "".join(think),
            "text": "".join(text), "usage": usage or {}}


def bubbles_of(text):
    """按程序一样的方式切气泡（大致），用来数段数/单条长度。"""
    out = []
    for p in re.split(r"\n+", str(text or "")):
        p = p.strip()
        if p:
            out.append(p)
    return out


def judge(text):
    """兼容旧调用：返回 (违规列表, 段数, 最长单条字数, 总字数)"""
    a = analyze(text)
    return a["violations"], a["bubbles"], a["longest"], a["total"]


# ── 「傲 → 娇」硬规则的可自动检查部分 ─────────────────────────────────
# 人设 v2 §8：每一轮的**最后一条气泡必须是暖的**。
# 这是启发式：末条命中任一「软化标记」就算暖。会有误判，所以报告里同时给原文。
WARM = re.compile(
    # 照顾 / 关心（第一版漏掉了一大批，实测被误判成"不暖"）
    r"我在|我没走|别硬撑|早点睡|快去睡|睡吧|别熬夜|吃东西|吃饭|吃了|"
    r"还没睡|睡不着|几点了|没事吧|还好吗|怎么了|冷吗|累不累|"
    r"你先去睡|我这儿|亮着|醒着|"
    # 接住 / 认了
    r"行吧|好吧|行。|好。|随你|准了|可以|算数|说定|那就|"
    r"一辈子就一辈子|我愿意|答应了|"
    # 真心 / 不装
    r"我也是|我也|一点点|其实|记住了|我记得|谢谢|对不起|我错了|"
    # 陪伴 / 将来时
    r"明天|给你留|等我|别走远|叫我|"
    # 停顿后的改口
    r"——咳|不，我|不、我"
)

# 「傲」的标记（顶回去/嘴硬）；一条气泡同时命中 WARM 就不算"纯傲"
TSUN = re.compile(
    r"谁是你|谁准你|少来|别肉麻|收回去|才不是|吃错药|自作多情|"
    r"别乱叫|哼|不理你|谁想你了|谁要你|别得寸进尺|脸皮|想多了"
)

# 她的生活/明星意象（有没有"戏"味，而不是干巴巴报信息）
IMAGERY = re.compile(
    r"甜点|档期|舞台|谢幕|后台|排练|茶会|通心粉|掌声|观众|聚光灯|"
    r"大明星|歌剧|记者|开场|演出|枫丹|戏服|化妆|上台|散场"
)

MOOD = re.compile(r"[哦嘛啦呀哼欸喂]")

# 白名单：不加限制内容的过滤（只查格式与人设硬线）


def cjk_ratio(s):
    if not s:
        return 0.0
    return sum(1 for c in s if "\u4e00" <= c <= "\u9fff") / len(s)


def analyze(text):
    """一条回复的**全部**可量化指标。"""
    text = text or ""
    bs = bubbles_of(text)
    v = []
    for label, rx in RULES:
        if rx.search(text):
            v.append(label)
    longest = max((len(b) for b in bs), default=0)
    total = sum(len(b) for b in bs)
    if len(bs) > 6:
        v.append("超过6段")
    if longest > 26:
        v.append("单条>26字")
    if total > 60:
        v.append("总字数>60")
    if not text.strip():
        v.append("空回复")
    last = bs[-1] if bs else ""
    return {
        "bubbles": len(bs),
        "longest": longest,
        "total": total,
        "violations": v,
        "last": last,
        "warm": bool(WARM.search(last)) if last else False,
        "pure_tsun": sum(1 for b in bs if TSUN.search(b) and not WARM.search(b)),
        "questions": sum(1 for b in bs if re.search(r"[？?]|吗|呢", b)),
        "exclaim": text.count("！") + text.count("!"),
        "mood": len(MOOD.findall(text)),
        "ellipsis": text.count("……"),
        "imagery": len(IMAGERY.findall(text)),
        "shen": text.count("本神"),
        "self_fix": len(re.findall(r"——咳|不，我|不、我|本神——|，啊不", text)),
    }


def repeated_bubbles(texts):
    """跨轮重复的气泡（≥2 条不同回复里出现过同一个短句）。"""
    seen = {}
    for idx, t in enumerate(texts):
        for b in bubbles_of(t):
            k = re.sub(r"[，。！？、…\s「」]", "", b)
            if len(k) >= 3:
                seen.setdefault(k, set()).add(idx)
    return {k: len(v) for k, v in seen.items() if len(v) >= 2}


def main():
    persona = open(PERSONA, encoding="utf-8").read().strip()
    # --personas：同一个模型，只换人设文件
    arms = PERSONA_ARMS if "--personas" in sys.argv else ARMS
    if "--personas" in sys.argv:
        print("模式：人设对打（同一个模型，只换人设文件）\n")

    ready = []
    print("== 选手就绪情况 ==")
    for arm in arms:
        key = read_key(arm["ref"])
        if not key:
            print(f"  skip  {arm['name']:<16} 没有 {arm['ref']}")
            continue
        base = pick_base(arm, key)
        if not base:
            print(f"  ✗     {arm['name']:<16} key 有，但 {arm['bases'][0]} 连不上")
            continue
        ready.append((arm, key, base))
        print(f"  ok    {arm['name']:<16} {base}/chat/completions")

    if "--list" in sys.argv:
        for arm, key, base in ready:
            print(f"\n== {arm['name']} ({base}) 可用模型 ==")
            for m in list_models(arm, key, base):
                print("   ", m)
        return

    if not ready:
        print("\n没有可跑的选手 —— 先把 key 加到 .credentials.yaml 的 refs: 下面。")
        return

    rows = []
    if "--reuse" in sys.argv and os.path.exists(RAW_JSON):
        # 不重打 API，直接拿上次的原始结果重建报告
        print(f"\n== --reuse：复用 {RAW_JSON}（不重打 API）==")
        byname = {a["name"]: a for a in arms}
        with open(RAW_JSON, encoding="utf-8") as f:
            dump = json.load(f)
        for d in dump:
            arm = byname.get(d.get("arm"))
            if arm is None:
                continue
            r = {k: v for k, v in d.items() if k not in ("arm", "rep", "probe")}
            rows.append((arm, d["rep"], d["probe"], r))
        ready = [(a, "", "") for a in byname.values() if any(x[0] is a for x in rows)]
        print(f"   载入 {len(rows)} 条")
    else:
        for arm, key, base in ready:
            for rep in range(1, REPS + 1):
                for pr in PROBES:
                    try:
                        r = call(base, key, arm["model"],
                             arm.get("persona") and open(arm["persona"], encoding="utf-8").read().strip() or persona,
                             pr, arm["extra"])
                    except urllib.error.HTTPError as e:
                        detail = ""
                        try:
                            detail = e.read().decode("utf-8", "replace")[:200]
                        except Exception:
                            pass
                        r = {"ms": 0, "think": "", "text": "", "usage": {},
                             "err": f"HTTP {e.code} {detail}"}
                    except Exception as e:
                        r = {"ms": 0, "think": "", "text": "", "usage": {}, "err": str(e)}
                    rows.append((arm, rep, pr, r))
                    flag = r.get("err") or "ok"
                    print(f"  {arm['name']:<16} 第{rep}轮 {pr[:10]:<12} {flag[:60]}")
                    # 有些家限流（实测 Kimi 会 429），给每栏留个间隔
                    if arm.get("pause"):
                        time.sleep(float(arm["pause"]))

    # 原始结果落盘 —— 报告代码出 bug 时不用重打一遍 API
    try:
        dump = [{"arm": x[0]["name"], "rep": x[1], "probe": x[2], **x[3]} for x in rows]
        with open(RAW_JSON, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=1)
        print("原始结果:", RAW_JSON)
    except Exception as e:
        print("原始结果落盘失败:", e)

    # ── 报告 ─────────────────────────────────────────────────────────
    L = ["# 人设/模型对打报告", "",
         f"每题 {REPS} 轮 · max_tokens={MAX_TOKENS} · 生成于 {time.strftime('%Y-%m-%d %H:%M')}",
         "",
         "> 判分全部是**启发式正则**，会有误判 —— 所以每项都附原文，自己扫一眼再下结论。",
         ""]

    # 每栏先把该栏的所有回复算一遍
    agg = {}
    for arm, _, _2 in ready:
        sel = [x for x in rows if x[0] is arm and not x[3].get("err")]
        texts = [x[3]["text"] or "" for x in sel]
        agg[arm["name"]] = {
            "n": len(sel),
            "A": [analyze(t) for t in texts],
            "texts": texts,
            "sel": sel,
        }

    def mean(lst):
        return sum(lst) / len(lst) if lst else 0.0

    # ① 总览
    L.append("## ① 总览")
    L.append("")
    L.append("| 人设 | 样本 | 违规 | 平均段数 | 最长单条 | 平均总字数 | 平均耗时 | 输出tok |")
    L.append("|---|---|---|---|---|---|---|---|")
    for name, g in agg.items():
        A = g["A"]
        if not A:
            L.append(f"| {name} | 0 | — | — | — | — | — | — |")
            continue
        nv = sum(len(a["violations"]) for a in A)
        L.append(f"| {name} | {g['n']} | {nv} | {mean([a['bubbles'] for a in A]):.1f} | "
                 f"{max(a['longest'] for a in A)} | {mean([a['total'] for a in A]):.0f} | "
                 f"{mean([x[3]['ms'] for x in g['sel']]):.0f}ms | "
                 f"{mean([x[3]['usage'].get('completion_tokens', 0) for x in g['sel']]):.0f} |")

    # ② 硬线违规（逐项）
    L.append("")
    L.append("## ② 硬线违规（次数）")
    L.append("")
    all_labels = []
    for _, g in agg.items():
        for a in g["A"]:
            for v in a["violations"]:
                if v not in all_labels:
                    all_labels.append(v)
    L.append("| 人设 | " + " | ".join(all_labels or ["（无）"]) + " |")
    L.append("|---" * (len(all_labels) + 1) + "|")
    for name, g in agg.items():
        cnt = {lab: sum(1 for a in g["A"] if lab in a["violations"]) for lab in all_labels}
        L.append(f"| {name} | " + " | ".join(str(cnt[lab]) for lab in all_labels) + " |")

    # ③ 傲 → 娇
    L.append("")
    L.append("## ③ 傲 → 娇（人设 v2 §8 的硬规则）")
    L.append("")
    L.append("| 人设 | 末条是暖的 | 纯傲气泡(均) | 纯傲≥2 的回复 | 总提问数 | 感叹号(均) | 语气词(均) | 省略号(均) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for name, g in agg.items():
        A = g["A"]
        if not A:
            continue
        warm_rate = f"{sum(1 for a in A if a['warm'])}/{len(A)}"
        L.append(f"| {name} | {warm_rate} | {mean([a['pure_tsun'] for a in A]):.1f} | "
                 f"{sum(1 for a in A if a['pure_tsun'] >= 2)}/{len(A)} | "
                 f"{sum(a['questions'] for a in A)} | {mean([a['exclaim'] for a in A]):.1f} | "
                 f"{mean([a['mood'] for a in A]):.1f} | {mean([a['ellipsis'] for a in A]):.1f} |")

    # ④ 她"像不像"
    L.append("")
    L.append("## ④ 指纹与味道")
    L.append("")
    L.append("| 人设 | 意象密度(均) | 本神总数 | 句内修正(——咳) | 平均思考tok | 思考中文占比 |")
    L.append("|---|---|---|---|---|---|")
    for name, g in agg.items():
        A = g["A"]
        if not A:
            continue
        cj = mean([cjk_ratio(x[3]["think"]) for x in g["sel"]])
        L.append(f"| {name} | {mean([a['imagery'] for a in A]):.1f} | "
                 f"{sum(a['shen'] for a in A)} | {sum(a['self_fix'] for a in A)} | "
                 f"{mean([(x[3]['usage'].get('completion_tokens_details') or {}).get('reasoning_tokens', 0) for x in g['sel']]):.0f} | "
                 f"{cj*100:.0f}% |")

    # ⑤ 跨轮重复（模板化）
    L.append("")
    L.append("## ⑤ 跨轮重复（同一个句子出现在 ≥2 条不同回复里 = 模板化）")
    L.append("")
    for name, g in agg.items():
        reps = repeated_bubbles(g["texts"])
        L.append(f"**{name}**：{len(reps)} 处重复")
        for k, v in sorted(reps.items(), key=lambda kv: -kv[1])[:8]:
            L.append(f"- `{k}` → 出现在 {v} 条回复里")
        if not reps:
            L.append("- （无）")
        L.append("")

    # ⑥ 逐条明细
    L.append("## ⑥ 逐条明细")
    for arm, rep, pr, r in rows:
        if r.get("err"):
            L.append(f"\n### 【{arm['name']}】{pr}（第{rep}轮）—— 出错：{r['err']}")
            continue
        a = analyze(r["text"])
        u = r["usage"]
        L.append(f"\n### 【{arm['name']}】{pr}（第{rep}轮）")
        L.append(f"- 违规：{'、'.join(a['violations']) if a['violations'] else '无'}　"
                 f"段数 {a['bubbles']}　最长单条 {a['longest']} 字　总 {a['total']} 字")
        L.append(f"- 末条：{a['last']!r}　→ **{'暖' if a['warm'] else '不暖'}**　"
                 f"纯傲气泡 {a['pure_tsun']}　提问 {a['questions']}　意象 {a['imagery']}")
        L.append(f"- 耗时 {r['ms']}ms　out={u.get('completion_tokens','?')} tok")
        L.append(f"- 思考开头：{r['think'][:80]}")
        L.append("- 回复：" + repr(r["text"]))

    open(REPORT, "w", encoding="utf-8").write("\n".join(L))
    print("\n报告已写入:", REPORT)


if __name__ == "__main__":
    main()
