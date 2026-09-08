# -*- coding: utf-8 -*-
"""端到端验证：模拟开奖会发送卡片图片且不写库。直接用 Pillow 兜底渲染。"""
import sys, os, types, asyncio, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None


def _install_stubs():
    import importlib

    def make_pkg(name):
        m = types.ModuleType(name)
        m.__path__ = []
        sys.modules[name] = m
        return m

    astrbot = make_pkg("astrbot")
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
    event_pkg = make_pkg("astrbot.api.event")
    star_pkg = make_pkg("astrbot.api.star")
    web_pkg = make_pkg("astrbot.api.web")
    core = make_pkg("astrbot.core")
    make_pkg("astrbot.core.message")
    comp_pkg = make_pkg("astrbot.core.message.components")

    # filter
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

    event_pkg.filter = _Filter()
    # astrbot.api.event.filter 子模块
    filter_mod = types.ModuleType("astrbot.api.event.filter")
    filter_mod.filter = _Filter()
    filter_mod.EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group")
    sys.modules["astrbot.api.event.filter"] = filter_mod

    # astrbot.core.star.filter.event_message_type 兜底导入
    make_pkg("astrbot.core.star.filter")
    emt_mod = types.ModuleType("astrbot.core.star.filter.event_message_type")
    emt_mod.event_message_type = lambda *a, **k: (lambda f: f)
    emt_mod.EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group")
    sys.modules["astrbot.core.star.filter.event_message_type"] = emt_mod
    perm_mod = types.ModuleType("astrbot.core.star.filter.permission")
    perm_mod.PermissionType = types.SimpleNamespace(ADMIN="admin")
    perm_mod.permission_type = lambda *a, **k: (lambda f: f)
    sys.modules["astrbot.core.star.filter.permission"] = perm_mod

    def register_star(*a, **k):
        return lambda cls: cls

    class Star:
        def __init__(self, *a, **k):
            pass

    star_pkg.register_star = register_star
    star_pkg.Star = Star
    star_pkg.Context = object

    web_pkg.request = None
    web_pkg.json_response = lambda d: d

    class _Plain:
        def __init__(self, text=""):
            self.text = text

    class _Image:
        @staticmethod
        def fromFileSystem(path):
            return _Image(path)

        def __init__(self, *a, **k):
            self.path = a[0] if a else k.get("file")

    comp_pkg.Plain = _Plain
    comp_pkg.Image = _Image
    make_pkg("astrbot.core.star")
    sm = make_pkg("astrbot.core.star.star_handler")
    sm.star_handlers_registry = types.SimpleNamespace(
        register=lambda *a, **k: None, get_handlers_by_event_type=lambda *a, **k: []
    )
    et = make_pkg("astrbot.core.star.event_type")
    et.EventType = types.SimpleNamespace(AdapterMessageEvent=1)


_install_stubs()

import main as M  # noqa: E402
from db import Database  # noqa: E402
from settings import SettingsStore  # noqa: E402
from activity import ActivityTracker  # noqa: E402


class FakeCtx:
    def __init__(self):
        self.sent = []

    def register_web_api(self, *a, **k):
        pass

    async def send_message(self, umo, chain):
        self.sent.append((umo, list(chain)))


class Plugin(M.GroupRafflePlugin):
    def __init__(self, ctx, cfg, dbp):
        self.context = ctx
        self.config = cfg
        self.db = Database(dbp)
        self.store = SettingsStore(self.db, cfg)
        self.tracker = ActivityTracker(self.db)
        self.sched = types.SimpleNamespace(
            sync_group=lambda *a, **k: None, remove_group=lambda *a, **k: None
        )
        self._signup_sid = {}
        self.html_render = None  # 强制走 Pillow 兜底


async def main():
    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, "t.db")
    cfg = {"card_enabled": True, "at_enabled": True}
    ctx = FakeCtx()
    p = Plugin(ctx, cfg, dbp)
    umo = "aiocqhttp:1:100"
    for i in range(6):
        p.db.upsert_user(umo, f"u{i}", f"成员{i}")
    gs = p.store.get(umo)
    gs.set_enabled(True)

    err = await p.do_draw(umo, simulate=True,
                          prizes_override=[{"name": "模拟奖", "count": 3}])
    assert err is None, f"do_draw 返回错误: {err}"
    assert ctx.sent, "未发送任何消息"
    _, chain = ctx.sent[0]
    names = [type(c).__name__ for c in chain]
    has_img = any("Image" in n for n in names)
    print("发送链组件:", names)
    assert has_img, "模拟开奖未发送卡片图片！"
    assert p.db.count_winners(umo) == 0, "模拟不应写中奖记录"
    print("✅ 模拟开奖已发送卡片图片，且未写中奖记录")


asyncio.run(main())
