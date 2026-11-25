"""Self-check for the login bot's photo pipeline, rotation and screen rendering.

Runs against a fake Pyrogram client that enforces the *same* contract as the real one:
send_photo / edit_message_media accept a file_id (str) or a file-like object (io.IOBase)
with .name set - raw bytes are rejected, which is the bug this build fixes.
No network, no Telegram credentials.
"""
import asyncio
import io
import json
import os
import re
import sys
import tempfile
from types import SimpleNamespace

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tests/ lives inside the project
os.environ.update(
    BOT_TOKEN="12345:AAHtesttoken",
    API_ID="12345",
    API_HASH="a" * 32,
    ROTATION_STATE_FILE=os.path.join(tempfile.mkdtemp(), "rotation.json"),
)
sys.path.insert(0, BOT_DIR)
import main  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


class FakePhoto:
    def __init__(self, file_id):
        self.file_id = file_id
        self.width = self.height = 800


class FakeMsg:
    def __init__(self, mid, chat_id, photo=None, text=None):
        self.id = mid
        self.chat = SimpleNamespace(id=chat_id)
        self.photo = [FakePhoto(p) for p in (photo or [])]
        self.text = text
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeBot:
    """Mirrors pyrogram's file handling closely enough to catch the real bug."""

    def __init__(self):
        self.n = 0
        self.photos = []          # (chat_id, caption, source_type, markup)
        self.messages = []        # (chat_id, text, markup)
        self.edits = 0
        self.deletes = 0
        self.fail_media = False
        self.fail_forever = False

    def _validate(self, source):
        if isinstance(source, bytes):
            raise ValueError("Invalid file. Expected a file path as string or a binary file pointer")
        if isinstance(source, str):
            return
        if isinstance(source, io.IOBase) and getattr(source, "name", None):
            return
        raise ValueError("file-like object needs a .name")

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        if self.fail_media or self.fail_forever:
            raise ValueError("Invalid file")
        self._validate(photo)
        self.n += 1
        self.photos.append((chat_id, caption, type(photo).__name__, reply_markup))
        return FakeMsg(self.n, chat_id, photo=[f"fid{self.n}"])

    async def send_message(self, chat_id, text, reply_markup=None):
        self.n += 1
        self.messages.append((chat_id, text, reply_markup))
        return FakeMsg(self.n, chat_id, text=text)

    async def edit_message_media(self, chat_id, mid, media, reply_markup=None):
        self._validate(media.media)
        self.n += 1
        self.edits += 1
        return FakeMsg(mid, chat_id, photo=[f"fid{self.n}"])

    async def edit_message_caption(self, chat_id, mid, caption=None, reply_markup=None):
        self.n += 1
        return FakeMsg(mid, chat_id, photo=["fid"])

    async def edit_message_text(self, chat_id, mid, text=None, reply_markup=None):
        self.n += 1
        return FakeMsg(mid, chat_id, text=text)

    async def delete_messages(self, chat_id, message_ids):
        self.deletes += 1


async def run():
    print("\n[1] photo store")
    main.load_photos()
    check("all 24 photos load (webp transcoded)", len(main.PHOTOS) == 24, len(main.PHOTOS))
    check("every photo is jpeg/png", all(p.kind in ("jpeg", "png") for p in main.PHOTOS))
    check("login and nature sets are balanced", sum(p.category == "login" for p in main.PHOTOS) == 12)
    src = main.media_source(main.PHOTOS[0])
    check("media source is BytesIO, never raw bytes", isinstance(src, io.BytesIO), type(src).__name__)
    check("media source carries a filename for Pyrogram", bool(getattr(src, "name", None)), src.name)
    check("two calls give independent buffers", main.media_source(main.PHOTOS[0]) is not src)
    # Regression test against the installed library's own upload branch.
    from pyrogram.types import InputMediaPhoto
    im = InputMediaPhoto(main.media_source(main.PHOTOS[0]), caption="x")
    check("pyrogram's upload branch accepts our object",
          isinstance(im.media, io.BytesIO) or os.path.isfile(im.media))
    async def upload_probe(value):
        try:
            await main.bot.save_file(value)
            return "uploaded"
        except ValueError as e:
            return f"ValueError: {e}"
        except AttributeError:
            return "accepted"          # passes validation, stops only because the client is offline
    raw = await upload_probe(main.PHOTOS[0].data)
    ok = await upload_probe(main.media_source(main.PHOTOS[0]))
    check("raw bytes still raise in pyrogram's uploader (original bug)", raw.startswith("ValueError"), raw)
    check("the new media source passes pyrogram's uploader", ok == "accepted", ok)
    print("\n[2] rotation")
    uid = 1001
    picks = [main.PHOTOS.index(main.next_photo(uid)) for _ in range(24)]
    check("first round shows every picture once", len(set(picks)) == 24, len(set(picks)))
    check("no picture repeats back to back", all(a != b for a, b in zip(picks, picks[1:])))
    more = [main.PHOTOS.index(main.next_photo(uid)) for _ in range(48)]
    seq = picks + more
    check("no consecutive duplicates over 72 screens", all(a != b for a, b in zip(seq, seq[1:])))
    cats = [main.PHOTOS[i].category for i in seq[:24]]
    check("login and nature alternate", all(a != b for a, b in zip(cats, cats[1:])))
    other = [main.PHOTOS.index(main.next_photo(2002)) for _ in range(10)]
    check("each user rotates independently", other[:5] != picks[:5])
    print("\n[3] caption splitting")
    session = "A" * 400
    text = main.txt_result_session(session)
    head, rest = main.split_html(text, main.CAPTION_LIMIT)
    check("split keeps the caption within 1024", len(head) <= 1024, len(head))
    check("session string survives the split intact", session in (head + rest))
    check("no code tag is cut in half", head.count("<code>") == head.count("</code>"))
    long_text = main.txt_help()
    head2, rest2 = main.split_html(long_text, main.CAPTION_LIMIT)
    check("a long screen yields caption + continuation", bool(rest2) and len(head2) <= 1024)
    print("\n[4] sending screens")
    fake = FakeBot()
    main.bot = fake
    msg = await main.send_screen(uid, main.txt_home(SimpleNamespace(first_name="Test", username="t")),
                                main.home_markup())
    check("welcome screen attaches a real photo", bool(msg.photo), msg.photo)
    check("caption is within Telegram limits", all(len(c) <= 1024 for _, c, _, _ in fake.photos))
    check("keyboard travels with the photo", fake.photos[-1][3] is not None)
    before = len(fake.photos)
    for _ in range(4):
        await main.send_screen(uid, main.txt_login_menu(), main.login_markup())
    check("repeated screens keep rotating the picture", len(fake.photos) - before == 4)
    fids = [sent.photo[0].file_id for sent in [
        await main.send_screen(uid, main.txt_login_menu(), main.login_markup()) for _ in range(4)]]
    check("consecutive screens are distinct messages", len(set(fids)) == 4, fids)
    print("\n[5] session result is never clipped")
    fake = FakeBot()
    main.bot = fake
    await main.send_result(uid, main.txt_result_head(SimpleNamespace(first_name="Test"), "login", False), session)
    check("result screen sent as photo + text", len(fake.photos) == 1 and len(fake.messages) == 1)
    check("full session string delivered uncut", session in fake.messages[0][1])
    check("keyboard sits on the session message", fake.messages[0][2] is not None)
    print("\n[6] failure handling")
    fake = FakeBot()
    fake.fail_media = True
    main.bot = fake
    main.PHOTO_BLACKLIST.clear()
    for p in main.PHOTOS:
        p.failed = False
    out = await main.send_screen(uid, "hello world", main.nav_markup())
    check("media rejection falls back to text", len(fake.photos) == 0 and len(fake.messages) == 1)
    check("text fallback keeps the message body", out.text == "hello world")
    check("three rejected pictures were blacklisted", len(main.PHOTO_BLACKLIST) == 3, len(main.PHOTO_BLACKLIST))
    check("blacklisted pictures leave rotation", all(
        main.PHOTOS.index(main.next_photo(3003)) not in main.PHOTO_BLACKLIST for _ in range(20)))
    main.PHOTO_BLACKLIST.clear()
    for p in main.PHOTOS:
        p.failed = False
    print("\n[7] in-place edits and keypad")
    fake = FakeBot()
    main.bot = fake
    photo_msg = FakeMsg(50, uid, photo=["fid-old"])
    await main.swap_screen(photo_msg, uid, main.txt_guide_menu(), main.guide_markup())
    check("photo message is edited in place", fake.edits == 1 and not photo_msg.deleted)
    text_msg = FakeMsg(51, uid)
    fake.edits = 0
    await main.swap_screen(text_msg, uid, main.txt_guide_menu(), main.guide_markup())
    check("text-only message is replaced by a photo screen", fake.edits == 0 and text_msg.deleted)
    main.STATE[uid] = {"step": "code", "digits": "12", "kp": 60, "kp_photo": True, "ts": main.time.time()}
    main.LOGIN[uid] = {"where": "test", "next": "", "rs_ts": 0, "hash": "h", "phone": "+1", "client": None}
    fake.edits = 0
    await main.show_code_screen(uid, rotate=True)
    check("keypad screen rotates the photo in place", fake.edits == 1)
    main.STATE.clear()
    main.LOGIN.clear()
    print("\n[8] rotation state across restarts")
    await asyncio.sleep(1.4)                       # let the debounced write land
    path = main.ROTATION_FILE
    saved = json.load(open(path)) if os.path.exists(path) else {}
    check("rotation state written to disk", str(uid) in saved, list(saved)[:3])
    last_rel = main.PHOTOS[main.BAGS[uid]["last"]].rel
    check("state holds the last picture shown", saved.get(str(uid)) == last_rel, saved.get(str(uid)))
    main.BAGS.clear()
    main.ROTATION.clear()
    main.load_rotation()
    first_after_restart = main.PHOTOS.index(main.next_photo(uid))
    check("restart does not repeat the last picture", main.PHOTOS[first_after_restart].rel != last_rel)
    print("\n[9] concurrency")
    fake = FakeBot()
    main.bot = fake
    users = [4001, 4002, 4003]
    results = await asyncio.gather(*[
        main.send_screen(users[i % 3], main.txt_login_menu(), main.login_markup()) for i in range(30)
    ])
    check("30 concurrent screens all attach a photo", all(r.photo for r in results), sum(1 for r in results if r.photo))
    per_user = {}
    for chat_id, _, _, _ in fake.photos:
        per_user.setdefault(chat_id, []).append(1)
    check("every user got their own screens", sorted(per_user) == users, sorted(per_user))
    print("\n[10] html safety")
    check("user names are escaped", "&lt;" in main.txt_home(SimpleNamespace(first_name="<b>x</b>", username=None)))
    check("error screens expose no traceback", "Traceback" not in main.txt_error(RuntimeError("boom")))
    check("error screens stay generic", "boom" not in main.txt_error(RuntimeError("boom")))


# ------------------------------------------------------------------------------------------------------------
#  Rotation hardening: who is blamed for a failed send, file ids with real Pyrogram objects, give-back, recency,
#  identical pictures, even spread across folders.
# ------------------------------------------------------------------------------------------------------------
import logging
import shutil
import struct


class PyroPhoto:
    """What real Pyrogram puts in message.photo: ONE Photo object (not a list of sizes)."""

    def __init__(self, file_id):
        self.file_id = file_id
        self.width = self.height = 800


def rpc(name, id_):
    """A Telegram RPC error the way the library raises it: a class with a name and an ID."""
    return type(name, (Exception,), {"ID": id_})(id_)


class ScriptBot(FakeBot):
    """FakeBot with scripted failures; message.photo is ONE Photo object, like in real Pyrogram."""

    def __init__(self, single=True):
        super().__init__()
        self.single = single
        self.send_plan, self.edit_plan, self.sources = [], [], []
        self.dead_ids = set()
        self.text_fail = False

    def _photo_msg(self, mid, chat_id):
        m = FakeMsg(mid, chat_id)
        m.photo = PyroPhoto(f"fid{self.n}") if self.single else [FakePhoto(f"fid{self.n}")]
        return m

    def _scripted(self, plan, source):
        if plan:
            err = plan.pop(0)
            if err is not None:
                raise err
        if isinstance(source, str) and source in self.dead_ids:
            raise rpc("FileIdInvalid", "FILE_ID_INVALID")

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.sources.append(type(photo).__name__)
        self._scripted(self.send_plan, photo)
        self._validate(photo)
        self.n += 1
        self.photos.append((chat_id, caption, type(photo).__name__, reply_markup))
        return self._photo_msg(self.n, chat_id)

    async def edit_message_media(self, chat_id, mid, media, reply_markup=None):
        self.sources.append(type(media.media).__name__)
        self._scripted(self.edit_plan, media.media)
        self._validate(media.media)
        self.n += 1
        self.edits += 1
        return self._photo_msg(mid, chat_id)

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.text_fail:
            raise RuntimeError("text send failed")
        return await super().send_message(chat_id, text, reply_markup)


def fresh_state():
    main.PHOTO_BLACKLIST.clear()
    for p in main.PHOTOS:
        p.failed, p.file_id = False, None
    main.BAGS.clear()
    main.ROTATION.clear()


def force_bag(uid, indices):
    main.BAGS[uid] = {"bag": list(indices), "last": None}


async def rotation_hardening():
    from pyrogram import errors as tg
    main.load_photos()
    fresh_state()

    print("\n[11] who is to blame for a failed send")
    table = [
        ("ValueError from the uploader, no cached id", ValueError("Invalid file"), False, "picture"),
        ("ValueError from the file_id decoder", ValueError("Failed to decode"), True, "stale_id"),
        ("struct.error while decoding a cached id", struct.error("bad"), True, "stale_id"),
        ("FileIdInvalid with a cached id", rpc("FileIdInvalid", "FILE_ID_INVALID"), True, "stale_id"),
        ("FileReferenceExpired with a cached id", rpc("FileReferenceExpired", "FILE_REFERENCE_EXPIRED"), True, "stale_id"),
        ("MediaEmpty with a cached id", rpc("MediaEmpty", "MEDIA_EMPTY"), True, "stale_id"),
        ("FileIdInvalid on a fresh upload", rpc("FileIdInvalid", "FILE_ID_INVALID"), False, "picture"),
        ("PhotoInvalidDimensions", rpc("PhotoInvalidDimensions", "PHOTO_INVALID_DIMENSIONS"), False, "picture"),
        ("PhotoSaveFileInvalid", rpc("PhotoSaveFileInvalid", "PHOTO_SAVE_FILE_INVALID"), True, "picture"),
        ("ImageProcessFailed", rpc("ImageProcessFailed", "IMAGE_PROCESS_FAILED"), False, "picture"),
        ("FilePartsInvalid", rpc("FilePartsInvalid", "FILE_PARTS_INVALID"), False, "picture"),
        ("UserIsBlocked", rpc("UserIsBlocked", "USER_IS_BLOCKED"), False, "other"),
        ("ChatWriteForbidden", rpc("ChatWriteForbidden", "CHAT_WRITE_FORBIDDEN"), True, "other"),
        ("PeerIdInvalid", rpc("PeerIdInvalid", "PEER_ID_INVALID"), False, "other"),
        ("InputUserDeactivated", rpc("InputUserDeactivated", "INPUT_USER_DEACTIVATED"), False, "other"),
        ("MessageIdInvalid", rpc("MessageIdInvalid", "MESSAGE_ID_INVALID"), False, "other"),
        ("InternalServerError", rpc("InternalServerError", "INTERNAL_SERVER_ERROR"), False, "other"),
        ("ConnectionError", ConnectionError("lost"), False, "other"),
        ("TimeoutError", TimeoutError("slow"), True, "other"),
        ("asyncio.TimeoutError", asyncio.TimeoutError(), False, "other"),
        ("a bug in our own code (AttributeError)", AttributeError("x"), False, "other"),
        ("a bug in our own code (KeyError)", KeyError("x"), True, "other"),
    ]
    for name, err, used, want in table:
        got = main.photo_error_kind(err, used)
        check(f"verdict: {name} -> {want}", got == want, got)

    print("\n[12] file ids with real Pyrogram objects")
    check("one Photo object gives its file id", main._largest_file_id(SimpleNamespace(photo=PyroPhoto("abc"))) == "abc")
    sizes = [SimpleNamespace(file_id="small", width=90, height=90), SimpleNamespace(file_id="big", width=800, height=800)]
    check("a list of sizes still gives the largest", main._largest_file_id(SimpleNamespace(photo=sizes)) == "big")
    check("no photo, empty list, empty id -> None", all(main._largest_file_id(SimpleNamespace(photo=v)) is None
                                                          for v in (None, [], PyroPhoto(None), PyroPhoto(""))))
    check("a message without the attribute -> None", main._largest_file_id(SimpleNamespace()) is None)
    uid = 7001
    bot = ScriptBot(single=True)
    main.bot = bot
    for _ in range(24):
        await main.send_screen(uid, main.txt_login_menu(), main.login_markup())
    check("first round: every picture is uploaded once", bot.sources == ["BytesIO"] * 24, set(bot.sources))
    check("every picture cached its file id from a single-Photo message", all(p.file_id for p in main.PHOTOS))
    for _ in range(24):
        await main.send_screen(uid, main.txt_login_menu(), main.login_markup())
    check("second round: no upload at all, only cached file ids", bot.sources[24:] == ["str"] * 24, set(bot.sources[24:]))
    main.STATE[uid] = {"step": "code", "digits": "", "ts": main.time.time()}
    main.LOGIN[uid] = {"where": "test", "next": "", "rs_ts": 0, "hash": "h", "phone": "+1", "client": None}
    await main.show_code_screen(uid)
    check("the keypad message is known to carry a photo", main.STATE[uid].get("kp_photo") is True, main.STATE[uid])
    main.STATE.clear()
    main.LOGIN.clear()

    print("\n[13] a dead cached file id is dropped, the picture is not")
    for how, plan, dead in (("FileIdInvalid", [], True), ("ValueError", [ValueError("Failed to decode")], False)):
        fresh_state()
        bot = ScriptBot()
        main.bot = bot
        p = main.PHOTOS[3]
        p.file_id = "dead"
        if dead:
            bot.dead_ids = {"dead"}
        bot.send_plan = plan
        force_bag(7002, [3, 4, 5])
        msg = await main.send_screen(7002, "hello", main.nav_markup())
        check(f"{how}: the screen still goes out with a photo", bool(msg.photo) and len(bot.photos) == 1)
        check(f"{how}: the same picture is uploaded again", bot.sources == ["str", "BytesIO"], bot.sources)
        check(f"{how}: the picture is not blacklisted and has a fresh id",
              not main.PHOTO_BLACKLIST and p.file_id not in (None, "dead"), (main.PHOTO_BLACKLIST, p.file_id))

    print("\n[14] only a bad picture leaves the rotation")
    fresh_state()
    bot = ScriptBot()
    main.bot = bot
    bot.send_plan = [rpc("PhotoInvalidDimensions", "PHOTO_INVALID_DIMENSIONS")]
    force_bag(7003, [6, 7, 8])
    msg = await main.send_screen(7003, "hello", main.nav_markup())
    check("a refused picture is blacklisted", main.PHOTO_BLACKLIST == {6}, main.PHOTO_BLACKLIST)
    check("the next picture takes its place on the same screen", bool(msg.photo) and len(bot.photos) == 1)

    print("\n[15] problems that are not the picture never cost a picture")
    outages = [rpc("UserIsBlocked", "USER_IS_BLOCKED"), rpc("ChatWriteForbidden", "CHAT_WRITE_FORBIDDEN"),
               rpc("PeerIdInvalid", "PEER_ID_INVALID"), ConnectionError("lost"), asyncio.TimeoutError(),
               rpc("InternalServerError", "INTERNAL_SERVER_ERROR"), tg.FloodWait(5)]
    for err in outages:
        fresh_state()
        bot = ScriptBot()
        main.bot = bot
        bot.send_plan = [err]
        force_bag(7004, [9, 10, 11])
        before = len(bot.messages)
        out = await main.send_screen(7004, "hello", main.nav_markup())
        name = type(err).__name__
        check(f"{name}: the screen goes out as text", len(bot.messages) == before + 1 and out.text == "hello")
        check(f"{name}: no picture is blacklisted", not main.PHOTO_BLACKLIST, main.PHOTO_BLACKLIST)
        check(f"{name}: the picture comes next time", main.PHOTOS.index(main.next_photo(7004)) == 9)
    fresh_state()
    bot = ScriptBot()
    main.bot = bot
    for n in range(40):                                   # 40 failed sends: the old code blacklisted 3 pictures each time
        bot.send_plan = [rpc("UserIsBlocked", "USER_IS_BLOCKED")]
        await main.send_screen(7100 + n, "hello", main.nav_markup())
    check("40 failed sends leave all 24 pictures in rotation", len(main._usable_photos()) == 24 and not main.PHOTO_BLACKLIST,
          main.PHOTO_BLACKLIST)
    fresh_state()
    bot = ScriptBot()
    main.bot = bot
    bot.text_fail = True
    force_bag(7005, [12, 13, 14])
    raised = None
    try:
        await main.send_screen(7005, main.txt_help(), main.nav_markup())
    except RuntimeError as e:
        raised = e
    check("a failing second message is reported, not blamed on the picture", raised is not None and not main.PHOTO_BLACKLIST)
    check("...and the screen is not sent twice", len(bot.photos) == 1, len(bot.photos))

    print("\n[16] in-place edits follow the same rules")
    fresh_state()
    bot = ScriptBot()
    main.bot = bot
    p = main.PHOTOS[15]
    p.file_id = "dead"
    bot.dead_ids = {"dead"}
    force_bag(7006, [15, 16])
    old = FakeMsg(80, 7006)
    old.photo = PyroPhoto("old")
    out = await main.swap_screen(old, 7006, main.txt_guide_menu(), main.guide_markup())
    check("a dead id in an in-place edit: same picture uploaded, message edited", bot.sources == ["str", "BytesIO"] and bot.edits == 1
          and not old.deleted and out is not None, bot.sources)
    check("...nothing blacklisted", not main.PHOTO_BLACKLIST)
    fresh_state()
    bot = ScriptBot()
    main.bot = bot
    bot.edit_plan = [rpc("MessageIdInvalid", "MESSAGE_ID_INVALID")]
    force_bag(7007, [17, 18])
    old = FakeMsg(81, 7007)
    old.photo = PyroPhoto("old")
    out = await main.swap_screen(old, 7007, main.txt_guide_menu(), main.guide_markup())
    check("an old message: deleted and the screen resent as a photo", old.deleted and len(bot.photos) == 1 and bool(out.photo))
    check("...no picture lost, and the picture that failed to edit is the one resent",
          not main.PHOTO_BLACKLIST and main.BAGS[7007]["last"] == 17, (main.PHOTO_BLACKLIST, main.BAGS[7007]["last"]))
    fresh_state()
    bot = ScriptBot()
    main.bot = bot
    bot.edit_plan = [tg.MessageNotModified()]
    old = FakeMsg(82, 7008)
    old.photo = PyroPhoto("old")
    out = await main.swap_screen(old, 7008, main.txt_guide_menu(), main.guide_markup())
    check("'message not modified' keeps the message as it is", out is old and not old.deleted and not bot.photos)
    fresh_state()
    bot = ScriptBot()
    main.bot = bot
    bot.edit_plan = [rpc("PhotoInvalidDimensions", "PHOTO_INVALID_DIMENSIONS")]
    force_bag(7009, [19, 20, 21])
    old = FakeMsg(83, 7009)
    old.photo = PyroPhoto("old")
    out = await main.swap_screen(old, 7009, main.txt_guide_menu(), main.guide_markup())
    check("a refused picture in an in-place edit: blacklisted, the next one is used", main.PHOTO_BLACKLIST == {19}
          and bot.edits == 1 and not old.deleted)

    fresh_state()
    bot = ScriptBot()
    main.bot = bot
    main.STATE[7013] = {"step": "code", "digits": "1", "kp": 90, "kp_photo": True, "ts": main.time.time()}
    main.LOGIN[7013] = {"where": "test", "next": "", "rs_ts": 0, "hash": "h", "phone": "+1", "client": None}
    await main.show_code_screen(7013, rotate=True)
    check("the code screen rotates its picture in place, nothing is deleted or resent",
          bot.edits == 1 and bot.deletes == 0 and not bot.photos, (bot.edits, bot.deletes, len(bot.photos)))
    bot.edit_plan = [rpc("MessageIdInvalid", "MESSAGE_ID_INVALID")]
    await main.show_code_screen(7013, rotate=True)
    check("...when the edit is impossible the screen is resent, no picture is lost",
          bot.deletes == 1 and len(bot.photos) == 1 and not main.PHOTO_BLACKLIST, (bot.deletes, len(bot.photos)))
    main.STATE.clear()
    main.LOGIN.clear()

    print("\n[17] give_back")
    fresh_state()
    st_uid = 7010
    force_bag(st_uid, [0, 1, 2, 3])
    a = main.next_photo(st_uid)
    b = main.next_photo(st_uid)
    check("two pictures were shown", main.BAGS[st_uid]["last"] == 1 and main.BAGS[st_uid]["bag"] == [2, 3])
    main.give_back(st_uid, b)
    st = main.BAGS[st_uid]
    check("the unseen picture goes to the front of the bag", st["bag"] == [1, 2, 3], st["bag"])
    check("the last picture steps back", st["last"] == 0 and main.ROTATION[str(st_uid)] == a.rel, (st["last"], main.ROTATION))
    main.give_back(st_uid, b)
    check("giving it back twice does not duplicate it", main.BAGS[st_uid]["bag"] == [1, 2, 3], main.BAGS[st_uid]["bag"])
    check("it is the next one shown", main.PHOTOS.index(main.next_photo(st_uid)) == 1)
    force_bag(7012, [0, 1, 2])
    x = main.next_photo(7012)
    main.PHOTO_BLACKLIST.add(0)
    main.give_back(7012, x)
    check("a picture that was blacklisted meanwhile is not given back", main.BAGS[7012]["bag"] == [1, 2], main.BAGS[7012]["bag"])
    main.PHOTO_BLACKLIST.clear()
    main.give_back(424242, a)
    main.give_back(st_uid, main.Photo("/x", "x.jpg", "jpeg", b"", "x"))
    check("an unknown user or picture is ignored", True)
    force_bag(7011, [5])
    only = main.next_photo(7011)
    main.give_back(7011, only)
    check("the very first picture given back leaves no stale 'last'", main.BAGS[7011]["last"] is None and "7011" not in main.ROTATION)

    print("\n[18] the persisted state keeps the most recently active users")
    fresh_state()
    cap = main.MAX_ROTATION_USERS
    main.MAX_ROTATION_USERS = 3
    for u in (1, 2, 3, 1, 4):
        main.next_photo(u)
    check("the least recently active user is dropped, not the oldest one", list(main.ROTATION) == ["3", "1", "4"], list(main.ROTATION))
    main.MAX_ROTATION_USERS = cap
    fresh_state()

    print("\n[19] identical pictures get one turn")
    tmp = tempfile.mkdtemp()
    real_dir = main.PHOTO_DIR
    try:
        for rel, idx in (("login/a.jpg", 0), ("nature/copy_of_a.jpg", 0), ("nature/b.jpg", 1)):
            os.makedirs(os.path.join(tmp, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(tmp, rel), "wb") as f:
                f.write(main.PHOTOS[idx].data)
        records = []

        class Cap(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())

        h = Cap()
        main.log.addHandler(h)
        main.PHOTO_DIR = tmp
        main.load_photos()
        main.log.removeHandler(h)
        check("a copy of a picture is skipped", len(main.PHOTOS) == 2, [p.rel for p in main.PHOTOS])
        check("the copy is reported in the log", any("identical" in r for r in records), records)
        check("the startup line names every folder", any("photos: 2 ready" in r and "1 login" in r and "1 nature" in r for r in records), records)
    finally:
        main.PHOTO_DIR = real_dir
        shutil.rmtree(tmp, ignore_errors=True)
        main.load_photos()
        fresh_state()
    check("the real gallery is back (24 pictures)", len(main.PHOTOS) == 24)

    print("\n[20] every folder is spread evenly through a round")
    real = main.PHOTOS

    def synth(spec):
        out = []
        for cat, n in spec:
            for i in range(n):
                out.append(main.Photo(f"/x/{cat}{i}.jpg", f"{cat}/{i}.jpg", "jpeg", b"", cat))
        return out

    def longest_run(seq, photos):
        best = run_ = 1
        for a, b in zip(seq, seq[1:]):
            run_ = run_ + 1 if photos[a].category == photos[b].category else 1
            best = max(best, run_)
        return best

    try:
        main.PHOTOS = synth([("login", 12), ("nature", 6), ("city", 6)])
        ok_perm = ok_run = ok_window = True
        for _ in range(100):
            seq = main._refill(list(range(24)), None)
            ok_perm &= sorted(seq) == list(range(24))
            ok_run &= longest_run(seq, main.PHOTOS) == 1
            for i in range(0, 17):
                w = [main.PHOTOS[x].category for x in seq[i:i + 8]]
                ok_window &= abs(w.count("login") - 4) <= 1 and abs(w.count("nature") - 2) <= 1 and abs(w.count("city") - 2) <= 1
        check("12/6/6: every picture exactly once per round", ok_perm)
        check("12/6/6: never the same folder twice in a row", ok_run)
        check("12/6/6: every stretch of 8 holds each folder's fair share", ok_window)
        main.PHOTOS = synth([("login", 12), ("nature", 12)])
        alt = all(longest_run(main._refill(list(range(24)), None), main.PHOTOS) == 1 for _ in range(100))
        check("12/12: the two folders alternate exactly (as before)", alt)
        for spec in ([12, 12, 3], [12, 12, 7], [10, 8, 6], [9, 9, 6], [13, 11, 2, 1], [4, 4, 4, 4], [7, 5, 3]):
            main.PHOTOS = synth([(f"f{i}", n) for i, n in enumerate(spec)])
            total = sum(spec)
            rounds = [main._refill(list(range(total)), None) for _ in range(150)]
            check(f"mix {spec}: every picture once, no folder twice in a row",
                  all(sorted(r) == list(range(total)) and longest_run(r, main.PHOTOS) == 1 for r in rounds))
        swept = bad = 0
        for a in range(1, 13):
            for b in range(1, a + 1):
                for c in range(0, b + 1):
                    spec = [a, b] + ([c] if c else [])
                    total = sum(spec)
                    if a * 2 > total + 1:                      # one folder holds more than half: repeats cannot be avoided
                        continue
                    main.PHOTOS = synth([(f"f{i}", x) for i, x in enumerate(spec)])
                    swept += 1
                    for _ in range(4):
                        seq = main._refill(list(range(total)), None)
                        if sorted(seq) != list(range(total)) or longest_run(seq, main.PHOTOS) > 1:
                            bad += 1
                            break
        check(f"{swept} folder mixes swept: every picture once, no folder twice in a row", bad == 0 and swept > 200, (swept, bad))
        swept5 = bad5 = 0
        for a in range(1, 9):
            for b in range(1, a + 1):
                for c in range(1, b + 1):
                    for d in range(0, c + 1):
                        for e in (0, 1, 2):
                            if e and not d:
                                continue
                            spec = [a, b, c] + ([d] if d else []) + ([e] if e else [])
                            total = sum(spec)
                            if a * 2 > total + 1:
                                continue
                            main.PHOTOS = synth([(f"f{i}", x) for i, x in enumerate(spec)])
                            swept5 += 1
                            for _ in range(3):
                                seq = main._refill(list(range(total)), None)
                                if sorted(seq) != list(range(total)) or longest_run(seq, main.PHOTOS) > 1:
                                    bad5 += 1
                                    break
        check(f"{swept5} mixes of 3 to 5 folders swept: no folder twice in a row", bad5 == 0 and swept5 > 1000, (swept5, bad5))
        main.PHOTOS = synth([("a", 16), ("b", 8)])
        gap_sets = set()
        for _ in range(20):
            seq = main._refill(list(range(24)), None)
            pos = [i for i, x in enumerate(seq) if main.PHOTOS[x].category == "b"]
            gap_sets.add(tuple(q - p for p, q in zip(pos, pos[1:])))
        check("16/8: the 8 pictures of the small folder sit at equal distances", gap_sets == {(3,) * 7}, gap_sets)
        main.PHOTOS = synth([("login", 20), ("nature", 2)])
        gaps = []
        for _ in range(100):
            seq = main._refill(list(range(22)), None)
            pos = [i for i, x in enumerate(seq) if main.PHOTOS[x].category == "nature"]
            gaps.append(pos[1] - pos[0])
        check("20/2: the two rare pictures are spread apart", min(gaps) >= 8, min(gaps))
        main.PHOTOS = synth([("a", 1), ("b", 1), ("c", 1), ("d", 1), ("e", 1)])
        check("five folders of one picture each: a clean permutation", all(sorted(main._refill(list(range(5)), None)) == list(range(5)) for _ in range(50)))
        main.PHOTOS = synth([("only", 10)])
        check("a single folder works", sorted(main._refill(list(range(10)), None)) == list(range(10)))
        main.PHOTOS = synth([("login", 3), ("nature", 3)])
        check("a round never opens with the last picture of the previous one",
              all(main._refill(list(range(6)), 2)[0] != 2 for _ in range(200)))
        main.PHOTOS = synth([("login", 12), ("nature", 6), ("city", 6)])
        main.ROTATION_INDEX = {p.rel: i for i, p in enumerate(main.PHOTOS)}
        main.BAGS.clear()
        picks = [main.next_photo(8800) for _ in range(24)]
        check("next_photo over three folders: every picture once per round, none twice in a row",
              len({id(p) for p in picks}) == 24 and all(a is not b for a, b in zip(picks, picks[1:])))
    finally:
        main.PHOTOS = real
        main.load_photos()
        fresh_state()


asyncio.run(run())
asyncio.run(rotation_hardening())
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
