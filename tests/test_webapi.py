# -*- coding: utf-8 -*-
"""验证 WebUI 分群配置保存 + 立即开奖：update_config 真实落库、draw_now 触发真实开奖。"""
import sys, os, types, asyncio, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _install_stubs():
    def make_pkg(name):
        m = types.ModuleType(name)
        m.__path__ = []
        sys.modules[name] = m
        return m

    make_pkg("astrbot")
    api = make_pkg("astrbot.api")

    class _Log:
        def _p(self, *a):
            pass
        info = warning = error = debug = _p

    class AstrBotConfig(dict):
        def save_config(self):
            pass

    api.logger = _Log()
    api.AstrBotConfig = AstrBotConfig

    make_pkg("astrbot.api.event")

    class _Filter:
        PermissionType = types.SimpleNamespace(ADMIN="admin")

        def command_group(self, *a, **k):
            def deco(f):
                f.command = lambda *aa, **kk: (lambda g: g)
                f.group = lambda *aa, **kk: (lambda g: g)
                return f
            return deco

        def event_message_type(self, *a, **k):
            return lambda f: f

        def permission_type(self, *a, **k):
            return lambda f: f

        def regex(self, *a, **k):
            return lambda f: f

        def command(self, *a, **k):
            return lambda f: f

        def on_decorating_result(self, *a, **k):
            return lambda f: f

        def after_message_sent(self, *a, **k):
            return lambda f: f

    _f = _Filter()
    fmod = types.ModuleType("astrbot.api.event.filter")
    fmod.command_group = _f.command_group
    fmod.event_message_type = _f.event_message_type
    fmod.permission_type = _f.permission_type
    fmod.regex = _f.regex
    fmod.command = _f.command
    fmod.PermissionType = _Filter.PermissionType
    fmod.EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="g")
    sys.modules["astrbot.api.event.filter"] = fmod

    star_pkg = make_pkg("astrbot.api.star")
    star_pkg.register_star = (lambda *a, **k: (lambda cls: cls))
    star_pkg.register = star_pkg.register_star

    class Star:
        def __init__(self, *a, **k):
            pass

    star_pkg.Star = Star
    star_pkg.Context = object

    web_pkg = make_pkg("astrbot.api.web")
    web_pkg.json_response = lambda d: d
    # request 稍后由测试注入

    make_pkg("astrbot.core")
    make_pkg("astrbot.core.message")
    comp = make_pkg("astrbot.core.message.components")

    class _Plain:
        def __init__(self, text=""):
            self.text = text

    class _Image:
        @staticmethod
        def fromFileSystem(p):
            return _Image(p)

        def __init__(self, *a, **k):
            self.path = a[0] if a else k.get("file")

    comp.Plain = _Plain
    comp.Image = _Image
    mer_mod = make_pkg("astrbot.core.message.message_event_result")

    class _MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain) if chain is not None else []

    mer_mod.MessageChain = _MessageChain
    make_pkg("astrbot.core.star")
    sm = make_pkg("astrbot.core.star.star_handler")
    sm.star_handlers_registry = types.SimpleNamespace(
        register=lambda *a, **k: None, get_handlers_by_event_type=lambda *a, **k: [])
    et = make_pkg("astrbot.core.star.event_type")
    et.EventType = types.SimpleNamespace(AdapterMessageEvent=1)
    make_pkg("astrbot.core.star.filter")
    em = types.ModuleType("astrbot.core.star.filter.event_message_type")
    em.event_message_type = lambda *a, **k: (lambda f: f)
    em.EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="g")
    sys.modules["astrbot.core.star.filter.event_message_type"] = em
    pm = types.ModuleType("astrbot.core.star.filter.permission")
    pm.PermissionType = types.SimpleNamespace(ADMIN="admin")
    pm.permission_type = lambda *a, **k: (lambda f: f)
    sys.modules["astrbot.core.star.filter.permission"] = pm


_install_stubs()
import main as M  # noqa: E402
from db import Database  # noqa: E402
from settings import SettingsStore  # noqa: E402
from activity import ActivityTracker  # noqa: E402
from web_api import WebApiHandler  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.sent = []

    def register_web_api(self, *a, **k):
        pass

    async def send_message(self, umo, chain):
        comps = chain.chain if hasattr(chain, "chain") else chain
        self.sent.append((umo, list(comps)))


class Plugin(M.GroupRafflePlugin):
    def __init__(self, ctx, cfg, dbp):
        self.context = ctx
        self.config = cfg
        self.db = Database(dbp)
        self.store = SettingsStore(self.db, cfg)
        self.tracker = ActivityTracker(self.db)
        self.sched = types.SimpleNamespace(
            sync_group=lambda *a, **k: None, remove_group=lambda *a, **k: None)
        self._signup_sid = {}
        self.html_render = None


class StubReq:
    """模拟 astrbot.api.web.request"""
    def __init__(self, body, query=None):
        self._body = body
        self._query = query or {}

    @property
    def query(self):
        return self._query

    async def json(self, default=None):
        return self._body or {}


async def main():
    import astrbot.api.web as webmod
    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, "t.db")
    ctx = FakeCtx()
    p = Plugin(ctx, {"card_enabled": False, "at_enabled": False,
                     "default_mode": "equal"}, dbp)
    umo = "aiocqhttp:1:100"
    for i in range(5):
        p.db.upsert_user(umo, f"u{i}", f"成员{i}")
    gs = p.store.get(umo)
    gs.set_enabled(True)

    api = WebApiHandler(p)

    # 1) update_config：分群保存（prizes / mode / card_enabled）——验证真实落库
    webmod.request = StubReq({
        "umo": umo,
        "patch": {
            "mode": "activity",
            "prizes": "一等奖:1,二等奖:2",
            "card_enabled": True,
            "cooldown_days": 3,
        },
    })
    r = await api.update_config()
    print("update_config status:", r.get("status"), "msg:", r.get("message"))
    assert r.get("status") == "ok", r
    data = r.get("data") or {}
    assert data.get("mode") == "activity", data.get("mode")
    assert data.get("prizes") == [
        {"name": "一等奖", "count": 1}, {"name": "二等奖", "count": 2}
    ], data.get("prizes")
    assert data.get("card_enabled") is True
    assert data.get("cooldown_days") == 3

    # 重新 store.get 验证确实落库（非表面生效）
    gs2 = p.store.get(umo)
    assert gs2.mode == "activity"
    assert gs2.card_enabled is True
    assert gs2.cooldown_days == 3
    print("-> 落库确认: mode=activity, card=True, cooldown=3")

    # 2) draw_now：触发真实开奖（成员充足）
    webmod.request = StubReq({"umo": umo})
    r2 = await api.draw_now()
    print("draw_now status:", r2.get("status"), "msg:", r2.get("message"))
    assert r2.get("status") == "ok", r2
    assert ctx.sent, "立即开奖未向群发送消息"
    # 中奖已记录（真实开奖）
    assert p.db.count_winners(umo) > 0, "真实开奖未写中奖记录"
    print("-> 立即开奖已向群发送消息并记录中奖，中奖人数:",
          [w["name"] for w in p.db.list_winners(umo, 10)])

    # 3) draw_now 缺参应报错
    webmod.request = StubReq({})
    r3 = await api.draw_now()
    assert r3.get("status") == "error", r3
    print("-> 缺参时正确返回错误:", r3.get("message"))

    print("OK")


asyncio.run(main())
