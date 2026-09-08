# -*- coding: utf-8 -*-
"""模拟开奖冒烟测试：直接调用引擎链路，验证多轮模拟不落库。

运行：python tests/test_simulate.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from db import Database
from settings import SettingsStore, MODE_ACTIVITY
from activity import ActivityTracker
from engine import draw, gather_candidates

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


tmp = os.path.join(tempfile.mkdtemp(prefix="gr_sim_"), "t.db")
db = Database(tmp)
store = SettingsStore(db, {"default_mode": "equal", "card_enabled": False})
gs = store.get("aiocqhttp:1:100")
tr = ActivityTracker(db, min_gap_seconds=0, daily_cap=9999)

# 造 8 个活跃成员
t0 = time.time() - 7200
for i in range(8):
    uid = f"u{i}"
    for j in range(3 + i):  # 活跃度递增
        tr.record_message(gs.umo, uid, f"成员{i}", t0 + i * 1000 + j * 10)

prizes = [{"name": "一等奖", "count": 1}, {"name": "二等奖", "count": 2}]
winners_before = set()

# 模拟 5 轮，全部不写库
for r in range(5):
    cands, weights, note = gather_candidates(
        mode=MODE_ACTIVITY, settings=gs, db=db, activity=tr, signup_sid=None)
    res = draw(mode=MODE_ACTIVITY, prizes=prizes, candidates=cands, weights=weights)
    total = sum(len(t.winners) for t in res.tiers)
    check(f"第{r+1}轮抽出 3 人", total == 3, str(total))
    alw = [u for t in res.tiers for u, _ in t.winners]
    check(f"第{r+1}轮无重复", len(set(alw)) == 3, str(alw))

# 关键：模拟后 winners 表应为空（未写库）
check("模拟后中奖记录表为空（未落库）",
      len(db.recent_winner_uids(gs.umo, 0)) == 0)

# 真实开奖写库对照
db.add_winners(gs.umo, [("u0", "成员0", "一等奖")], int(time.time()))
check("真实写库后能查到中奖者", "u0" in db.recent_winner_uids(gs.umo, int(time.time()) - 10))

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
