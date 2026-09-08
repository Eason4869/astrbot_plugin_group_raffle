# -*- coding: utf-8 -*-
"""验证 状态/名单 经 _dispatch_command 返回信息卡片图片（含群停用时）。"""
import sys, os, types, asyncio, tempfile

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
    web_pkg.request = None
    web_pkg.json_response = lambda d: d

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


class FakeCtx:
    def register_web_api(self, *a, **k):
        pass

    async def send_message(self, umo, chain):
        pass


class Result:
    def __init__(self, kind, val):
        self.kind = kind
        self.val = val


class FakeEvent:
    def __init__(self, text, umo, admin=True):
        self.message_str = text
        self._umo = umo
        self._admin = admin
        self.plain_sent = []
        self.image_sent = []
        self.stopped = False

    @property
    def unified_msg_origin(self):
        return self._umo

    def get_sender_id(self):
        return "admin1"

    def get_sender_name(self):
        return "管理员"

    def is_admin(self):
        return self._admin

    def get_group_id(self):
        return self._umo.split(":")[-1]

    def plain_result(self, text):
        self.plain_sent.append(text)
        return Result("plain", text)

    def image_result(self, path):
        self.image_sent.append(path)
        return Result("image", path)

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    def set_result(self, *a):
        pass


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


async def run_cmd(p, text):
    ev = FakeEvent(text, "aiocqhttp:1:100")
    async for _ in p._dispatch_command(ev):
        pass
    return ev


async def main():
    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, "t.db")
    p = Plugin(FakeCtx(), {"card_enabled": True, "at_enabled": True}, dbp)
    umo = "aiocqhttp:1:100"
    for i in range(5):
        p.db.upsert_user(umo, f"u{i}", f"成员{i}")
    gs = p.store.get(umo)
    gs.set_enabled(True)

    ev = await run_cmd(p, "抽奖 状态")
    print("状态 -> images:", len(ev.image_sent), "plains:", len(ev.plain_sent))
    assert ev.image_sent, "状态未返回卡片图片"
    assert ev.stopped, "状态未 stop_event，可能被 LLM 接管"

    ev = await run_cmd(p, "抽奖 名单")
    print("名单 -> images:", len(ev.image_sent), "plains:", len(ev.plain_sent))
    assert ev.image_sent, "名单未返回卡片图片"
    assert ev.stopped, "名单未 stop_event，可能被 LLM 接管"

    # 停用群后，只读信息命令仍应返回
    gs.set_enabled(False)
    ev = await run_cmd(p, "抽奖 状态")
    assert ev.image_sent or ev.plain_sent, "停用后 状态 未返回结果"
    assert ev.stopped, "停用后 状态 未 stop_event"
    print("-> 停用后 状态 仍返回:", bool(ev.image_sent or ev.plain_sent))

    print("OK")


asyncio.run(main())
