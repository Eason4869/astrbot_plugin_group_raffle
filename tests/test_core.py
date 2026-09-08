# -*- coding: utf-8 -*-
"""核心逻辑自测：引擎加权抽样 / 分群配置 / cron 预览 / 活跃度防刷。

直接在插件根目录运行：python tests/test_core.py
（会在系统临时目录建测试数据库，不影响真实数据。）
"""

import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows GBK 控制台兼容
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from db import Database
from settings import (
    SettingsStore,
    MODE_ACTIVITY,
    MODE_SIGNUP,
    SCHED_DAILY,
    SCHED_WEEKLY,
    SCHED_CUSTOM,
    normalize_prizes,
)
from activity import ActivityTracker, activity_weight, window_days
from engine import draw, gather_candidates, DrawError
from scheduler import build_trigger, preview_next

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def new_db():
    tmp = os.path.join(tempfile.mkdtemp(prefix="gr_test_"), "test.db")
    return Database(tmp)


def test_prizes():
    print("[1] 等次解析")
    p = normalize_prizes("一等奖:1,二等奖:2，三等奖：3")
    check("中文逗号/冒号解析", p[0]["name"] == "一等奖" and p[2]["count"] == 3, str(p))
    bad = False
    try:
        normalize_prizes("一等奖:0")
    except ValueError:
        bad = True
    check("人数 0 报错", bad)
    bad = False
    try:
        normalize_prizes("乱填")
    except ValueError:
        bad = True
    check("非法格式报错", bad)


def test_settings_store():
    print("[2] 分群配置")
    db = new_db()
    store = SettingsStore(db, {"default_mode": "equal", "at_enabled": True,
                               "card_enabled": False, "default_template": "T<winners>"})
    g1 = store.get("aiocqhttp:100:111")
    g2 = store.get("aiocqhttp:100:222")
    check("默认模式跟随全局", g1.mode == "equal")
    g1.update({"mode": MODE_SIGNUP})
    check("群1 改为报名", store.get("aiocqhttp:100:111").mode == MODE_SIGNUP)
    check("群2 不受影响", g2.mode == "equal")
    g1.update({"cooldown_days": 14})
    check("冷却更新", store.get("aiocqhttp:100:111").cooldown_days == 14)
    g1.set_enabled(False)
    check("群停用", store.get("aiocqhttp:100:111").enabled is False)
    g1.set_enabled(True)
    desc = g1.describe()
    check("状态摘要可读", "报名参与" in desc and "14 天" in desc)
    db.close()


def test_activity():
    print("[3] 活跃度统计与防刷")
    db = new_db()
    tr = ActivityTracker(db, min_gap_seconds=5, daily_cap=10)
    umo = "aiocqhttp:1:1"
    # 正常 3 条
    t0 = 1_000_000.0
    for i in range(3):
        tr.record_message(umo, "u1", "甲", t0 + i * 10)
    # 刷屏 5 条（间隔 1 秒 < 5s；首条无记录会计入，其余被拦）
    for i in range(5):
        tr.record_message(umo, "u2", "乙", t0 + 100 + i)
    counts = tr.counts(umo, 7)
    check("u1 计数 3", counts.get("u1") == 3, str(counts))
    check("u2 刷屏仅首条计入", counts.get("u2", 0) == 1, str(counts))
    # 封顶
    for i in range(20):
        tr.record_message(umo, "u3", "丙", t0 + 1000 + i * 10)
    counts = tr.counts(umo, 7)
    check("日封顶 10", counts.get("u3") == 10, str(counts))
    w_lo = activity_weight(1)
    w_hi = activity_weight(100)
    check("权重单调增", w_hi > w_lo)
    check("100 条权重远小于 100 倍", w_hi < 100, f"{w_hi:.2f}")
    days = window_days(3)
    check("窗口 3 天", len(days) == 3 and days[-1] == days[-1])
    db.close()


def test_engine_equal():
    print("[4] 等权抽奖 / 多等次 / 不重复 / 冷却")
    db = new_db()
    store = SettingsStore(db, {})
    gs = store.get("aiocqhttp:1:1")
    cands = [(f"u{i}", f"用户{i}") for i in range(10)]
    res = draw(mode="equal", prizes=[{"name": "一等奖", "count": 2},
                                     {"name": "二等奖", "count": 3}],
               candidates=cands, rng=random.Random(42))
    total = sum(len(t.winners) for t in res.tiers)
    check("总中奖 5 人", total == 5, str(total))
    allw = [u for t in res.tiers for u, _ in t.winners]
    check("无人重复中奖", len(set(allw)) == 5, str(allw))
    # 冷却排除
    cool = {allw[0], allw[1]}
    res2 = draw(mode="equal", prizes=[{"name": "奖", "count": 8}],
                candidates=cands, cooldown_uids=cool, rng=random.Random(1))
    winners2 = {u for t in res2.tiers for u, _ in t.winners}
    check("冷却者未中", not (winners2 & cool), str(winners2))
    check("冷却提示存在", any("冷却" in n for n in res2.notes), str(res2.notes))
    # 名额不足
    res3 = draw(mode="equal", prizes=[{"name": "大奖", "count": 20}],
                candidates=cands, rng=random.Random(1))
    check("名额不足有缺额", res3.tiers[0].shortage == 10, str(res3.tiers[0].shortage))
    # 空池报错
    try:
        draw(mode="equal", prizes=[{"name": "x", "count": 1}], candidates=[])
        check("空池报错", False)
    except DrawError:
        check("空池报错", True)
    db.close()


def test_engine_activity():
    print("[5] 活跃度加权抽奖")
    db = new_db()
    store = SettingsStore(db, {})
    gs = store.get("aiocqhttp:1:1")
    tr = ActivityTracker(db, min_gap_seconds=0, daily_cap=9999)
    import time as _t
    base = _t.time() - 3600
    # u1 高活跃，u2/u3 低活跃
    for i in range(50):
        tr.record_message(gs.umo, "u1", "活跃帝", base + i * 10)
    for i in range(3):
        tr.record_message(gs.umo, "u2", "小透明", base + i * 10)
        tr.record_message(gs.umo, "u3", "路人", base + i * 10)
    gs.update({"activity_window_days": 7, "activity_min_count": 0})
    cands, weights, note = gather_candidates(
        mode=MODE_ACTIVITY, settings=gs, db=db, activity=tr, signup_sid=None)
    check("候选 3 人", len(cands) == 3, note)
    check("u1 权重最高", weights["u1"] > weights["u2"], str(weights))
    # 蒙特卡洛：u1 中奖率应显著高于均值
    rng = random.Random(7)
    wins = {"u1": 0, "u2": 0, "u3": 0}
    for _ in range(600):
        r = draw(mode=MODE_ACTIVITY, prizes=[{"name": "p", "count": 1}],
                 candidates=cands, weights=weights, rng=rng)
        u = r.tiers[0].winners[0][0]
        wins[u] += 1
    check("高活跃中奖率显著高", wins["u1"] > wins["u2"] + wins["u3"], str(wins))
    db.close()


def test_engine_signup():
    print("[6] 报名模式")
    db = new_db()
    store = SettingsStore(db, {})
    gs = store.get("aiocqhttp:1:1")
    gs.update({"mode": MODE_SIGNUP})
    tr = ActivityTracker(db)
    import time as _t
    for i in range(3):
        db.add_signup(gs.umo, 99, f"s{i}", f"报名者{i}", int(_t.time()))
    cands, weights, note = gather_candidates(
        mode=MODE_SIGNUP, settings=gs, db=db, activity=tr, signup_sid=99)
    check("报名候选 3 人", len(cands) == 3, note)
    check("报名模式无权重", weights is None)
    try:
        gather_candidates(mode=MODE_SIGNUP, settings=store.get("aiocqhttp:1:2"),
                          db=db, activity=tr, signup_sid=1)
        check("空报名报错", False)
    except DrawError:
        check("空报名报错", True)
    db.close()


def test_scheduler():
    print("[7] 定时 trigger 与预览")
    nxt = preview_next({"type": SCHED_DAILY, "time": "20:00"}, 2)
    check("每日预览 2 条", len(nxt) == 2, str(nxt))
    check("每日含 20:00", all("20:00" in x for x in nxt), str(nxt))
    nxt = preview_next({"type": SCHED_WEEKLY, "time": "09:00", "weekday": 0}, 1)
    check("每周预览", len(nxt) == 1)
    import datetime
    nxt = preview_next({"type": SCHED_CUSTOM, "cron": "0 20 * * 5"}, 1)
    # cron 周字段 5 = 周五（Monday=0）；用 weekday() 校验，避免 locale 缩写差异
    trig = build_trigger({"type": SCHED_CUSTOM, "cron": "0 20 * * 5"})
    from activity import TZ
    fire = trig.get_next_fire_time(None, datetime.datetime.now(TZ))
    check("cron 周五预览", len(nxt) == 1 and fire.astimezone(TZ).weekday() == 4,
          f"{nxt} weekday={fire.astimezone(TZ).weekday()}")
    check("none 无 trigger", build_trigger({"type": "none"}) is None)
    bad = False
    try:
        build_trigger({"type": SCHED_CUSTOM, "cron": "99 99 * * *"})
    except ValueError:
        bad = True
    except Exception:
        bad = True
    check("非法 cron 报错", bad)


if __name__ == "__main__":
    test_prizes()
    test_settings_store()
    test_activity()
    test_engine_equal()
    test_engine_activity()
    test_engine_signup()
    test_scheduler()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
