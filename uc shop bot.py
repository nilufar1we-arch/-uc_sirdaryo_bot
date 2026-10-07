"""
PUBG UC savdo boti (qo'lda to'lov tasdiqlash bilan).

Jarayon:
  mijoz paket tanlaydi -> PUBG ID kiritadi -> kartaga to'laydi -> chek yuboradi
  -> admin "To'lov tasdiqlandi" bosadi -> UC'ni o'zi yuboradi -> "UC yuborildi" bosadi
"""

import asyncio
import html
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------------------
# SOZLAMALAR (.env faylidan olinadi)
# ----------------------------------------------------------------------------
BOT_TOKEN = "8845139822:AAFeDJ7_ZKAo3xFmwoQNwMCV0-XPba80el4"
ADMIN_IDS = {7722748791}
CARD_NUMBER = os.getenv("CARD_NUMBER", "9860170110768")
CARD_OWNER = os.getenv("CARD_OWNER", "Xaydarov Naim")
CONTACT = os.getenv("CONTACT", "@admin_username")
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Boshlang'ich paketlar (UC, narx so'mda). Keyin /addpack bilan o'zgartirasiz.
DEFAULT_PACKS = [(60, 12000), (325, 60000), (660, 120000), (1800, 300000), (3850, 600000)]

# Tugma matnlari
BTN_BUY = "🛒 UC sotib olish"
BTN_ORDERS = "📦 Buyurtmalarim"
BTN_CONTACT = "☎️ Aloqa"
BTN_CANCEL = "❌ Bekor qilish"

STATUS_TEXT = {
    "awaiting_receipt": "⏳ Chek kutilmoqda",
    "pending": "🕓 To'lov tekshirilmoqda",
    "paid": "💳 To'lov tasdiqlandi, UC yuborilmoqda",
    "done": "✅ Bajarildi",
    "rejected": "❌ Rad etildi",
    "cancelled": "🚫 Bekor qilindi",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("uc_bot")


# ----------------------------------------------------------------------------
# BAZA (SQLite)
# ----------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_one(sql: str, params: tuple = ()):
    with closing(_conn()) as conn:
        return conn.execute(sql, params).fetchone()


def db_all(sql: str, params: tuple = ()):
    with closing(_conn()) as conn:
        return conn.execute(sql, params).fetchall()


def db_insert(sql: str, params: tuple = ()) -> int:
    with closing(_conn()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def db_update(sql: str, params: tuple = ()) -> int:
    """O'zgargan qatorlar sonini qaytaradi."""
    with closing(_conn()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    with closing(_conn()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                tg_id     INTEGER PRIMARY KEY,
                username  TEXT,
                full_name TEXT,
                joined_at TEXT
            );
            CREATE TABLE IF NOT EXISTS products(
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                uc    INTEGER UNIQUE NOT NULL,
                price INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders(
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id           INTEGER NOT NULL,
                pubg_id         TEXT NOT NULL,
                uc              INTEGER NOT NULL,
                price           INTEGER NOT NULL,
                status          TEXT NOT NULL,
                receipt_file_id TEXT,
                created_at      TEXT,
                updated_at      TEXT
            );
            """
        )
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
            conn.executemany("INSERT INTO products(uc, price) VALUES(?, ?)", DEFAULT_PACKS)
        conn.commit()


def upsert_user(m: Message) -> None:
    u = m.from_user
    db_update(
        """INSERT INTO users(tg_id, username, full_name, joined_at) VALUES(?,?,?,?)
           ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
        (u.id, u.username, u.full_name, now()),
    )


def get_order(order_id: int):
    return db_one("SELECT * FROM orders WHERE id=?", (order_id,))


def set_status(order_id: int, new: str, expected: str) -> bool:
    """Holatni faqat hozirgi holat `expected` bo'lsa o'zgartiradi (ikki marta bosilishdan himoya)."""
    return (
        db_update(
            "UPDATE orders SET status=?, updated_at=? WHERE id=? AND status=?",
            (new, now(), order_id, expected),
        )
        > 0
    )


# ----------------------------------------------------------------------------
# YORDAMCHI FUNKSIYALAR
# ----------------------------------------------------------------------------
def fmt_price(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " so'm"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_BUY)], [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_CONTACT)]],
        resize_keyboard=True,
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)


def admin_kb(order_id: int, stage: str):
    if stage == "pending":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ To'lov tasdiqlandi", callback_data=f"adm:ok:{order_id}"),
                    InlineKeyboardButton(text="❌ Rad etish", callback_data=f"adm:no:{order_id}"),
                ]
            ]
        )
    if stage == "paid":
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📤 UC yuborildi", callback_data=f"adm:done:{order_id}")]]
        )
    return None


def order_caption(order, status_line: str | None = None) -> str:
    user = db_one("SELECT * FROM users WHERE tg_id=?", (order["tg_id"],))
    name = html.escape(user["full_name"] or "—") if user else "—"
    uname = f"@{html.escape(user['username'])}" if user and user["username"] else "username yo'q"
    text = (
        f"🧾 <b>Buyurtma #{order['id']}</b>\n"
        f"👤 {name} ({uname})\n"
        f"🆔 Telegram ID: <code>{order['tg_id']}</code>\n"
        f"🎮 PUBG ID: <code>{html.escape(order['pubg_id'])}</code>\n"
        f"💎 {order['uc']} UC\n"
        f"💰 {fmt_price(order['price'])}"
    )
    if status_line:
        text += f"\n\n{status_line}"
    return text


async def safe_send(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text)
        return True
    except Exception as e:  # foydalanuvchi botni bloklagan bo'lishi mumkin
        log.warning("Xabar yuborilmadi (%s): %s", chat_id, e)
        return False


async def safe_edit(cb: CallbackQuery, text: str, kb) -> None:
    try:
        await cb.message.edit_caption(caption=text, reply_markup=kb)
    except TelegramBadRequest as e:
        log.warning("Caption o'zgartirilmadi: %s", e)


async def reset_flow(state: FSMContext) -> None:
    """Yarim qolgan buyurtmani bekor qilib, holatni tozalaydi."""
    data = await state.get_data()
    order_id = data.get("order_id")
    if order_id:
        set_status(order_id, "cancelled", "awaiting_receipt")
    await state.clear()


class OrderFlow(StatesGroup):
    pubg_id = State()
    receipt = State()


# ----------------------------------------------------------------------------
# ADMIN QISMI
# ----------------------------------------------------------------------------
admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


@admin_router.message(Command("admin"))
async def admin_help(m: Message):
    await m.answer(
        "🛠 <b>Admin buyruqlari</b>\n\n"
        "/orders — kutayotgan buyurtmalar\n"
        "/stats — statistika\n"
        "/packs — paketlar ro'yxati\n"
        "/addpack <code>UC narx</code> — paket qo'shish/narxini o'zgartirish\n"
        "   masalan: <code>/addpack 660 125000</code>\n"
        "/delpack <code>UC</code> — paketni o'chirish\n"
        "/broadcast <code>matn</code> — hammaga xabar yuborish"
    )


@admin_router.message(Command("packs"))
async def admin_packs(m: Message):
    rows = db_all("SELECT uc, price FROM products ORDER BY uc")
    if not rows:
        return await m.answer("Paket yo'q. /addpack bilan qo'shing.")
    await m.answer("💎 <b>Paketlar</b>\n\n" + "\n".join(f"{r['uc']} UC — {fmt_price(r['price'])}" for r in rows))


@admin_router.message(Command("addpack"))
async def admin_addpack(m: Message, command: CommandObject):
    try:
        uc, price = (int(x) for x in (command.args or "").split())
        assert uc > 0 and price > 0
    except Exception:
        return await m.answer("Format: <code>/addpack 660 120000</code>")
    db_update(
        "INSERT INTO products(uc, price) VALUES(?,?) ON CONFLICT(uc) DO UPDATE SET price=excluded.price",
        (uc, price),
    )
    await m.answer(f"✅ {uc} UC — {fmt_price(price)} saqlandi.")


@admin_router.message(Command("delpack"))
async def admin_delpack(m: Message, command: CommandObject):
    try:
        uc = int((command.args or "").strip())
    except ValueError:
        return await m.answer("Format: <code>/delpack 660</code>")
    n = db_update("DELETE FROM products WHERE uc=?", (uc,))
    await m.answer("🗑 O'chirildi." if n else "Bunday paket topilmadi.")


@admin_router.message(Command("stats"))
async def admin_stats(m: Message):
    users = db_one("SELECT COUNT(*) AS c FROM users")["c"]
    by_status = {r["status"]: r["c"] for r in db_all("SELECT status, COUNT(*) AS c FROM orders GROUP BY status")}
    revenue = db_one("SELECT COALESCE(SUM(price),0) AS s FROM orders WHERE status='done'")["s"]
    lines = [f"{STATUS_TEXT[s]}: {by_status.get(s, 0)}" for s in STATUS_TEXT]
    await m.answer(
        f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {users}\n\n" + "\n".join(lines) + f"\n\n💰 Jami savdo: {fmt_price(revenue)}"
    )


@admin_router.message(Command("orders"))
async def admin_orders(m: Message):
    rows = db_all("SELECT * FROM orders WHERE status IN ('pending','paid') ORDER BY id")
    if not rows:
        return await m.answer("Kutayotgan buyurtma yo'q ✅")
    for o in rows:
        stage = "pending" if o["status"] == "pending" else "paid"
        await m.answer(order_caption(o, STATUS_TEXT[o["status"]]), reply_markup=admin_kb(o["id"], stage))


@admin_router.message(Command("broadcast"))
async def admin_broadcast(m: Message, command: CommandObject, bot: Bot):
    text = command.args
    if not text:
        return await m.answer("Format: <code>/broadcast Xabar matni</code>")
    ok = fail = 0
    for u in db_all("SELECT tg_id FROM users"):
        if await safe_send(bot, u["tg_id"], text):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)  # Telegram limitiga tushmaslik uchun
    await m.answer(f"📣 Yuborildi: {ok}, yetib bormadi: {fail}")


@admin_router.callback_query(F.data.startswith("adm:"))
async def admin_action(cb: CallbackQuery, bot: Bot):
    _, action, oid_raw = cb.data.split(":")
    order_id = int(oid_raw)
    order = get_order(order_id)
    if not order:
        return await cb.answer("Buyurtma topilmadi", show_alert=True)

    admin_name = html.escape(cb.from_user.full_name)
    stale = "Bu buyurtma allaqachon ko'rib chiqilgan"

    if action == "ok":
        if not set_status(order_id, "paid", "pending"):
            return await cb.answer(stale, show_alert=True)
        await safe_send(bot, order["tg_id"], f"✅ Buyurtma #{order_id}: to'lovingiz tasdiqlandi. UC tez orada yuboriladi.")
        await safe_edit(
            cb,
            order_caption(order, f"💳 To'lov tasdiqlandi ({admin_name}).\nUC'ni yuboring va pastdagi tugmani bosing."),
            admin_kb(order_id, "paid"),
        )

    elif action == "no":
        if not set_status(order_id, "rejected", "pending"):
            return await cb.answer(stale, show_alert=True)
        await safe_send(
            bot,
            order["tg_id"],
            f"❌ Buyurtma #{order_id}: to'lov tasdiqlanmadi.\nMuammo bo'lsa, {CONTACT} bilan bog'laning.",
        )
        await safe_edit(cb, order_caption(order, f"❌ Rad etildi ({admin_name})"), None)

    elif action == "done":
        if not set_status(order_id, "done", "paid"):
            return await cb.answer(stale, show_alert=True)
        await safe_send(
            bot,
            order["tg_id"],
            f"🎉 Buyurtma #{order_id}: <b>{order['uc']} UC</b> PUBG ID <code>{html.escape(order['pubg_id'])}</code> ga yuborildi!\n"
            "Xaridingiz uchun rahmat!",
        )
        await safe_edit(cb, order_caption(order, f"✅ Bajarildi ({admin_name})"), None)

    await cb.answer("Bajarildi")


# ----------------------------------------------------------------------------
# FOYDALANUVCHI QISMI
# ----------------------------------------------------------------------------
user_router = Router()


@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await reset_flow(state)
    upsert_user(m)
    await m.answer(
        f"Salom, <b>{html.escape(m.from_user.first_name or 'do‘st')}</b>! 👋\n"
        "Bu yerda PUBG Mobile UC'ni tez va qulay narxda sotib olishingiz mumkin.",
        reply_markup=main_menu(),
    )


# Bekor qilish — holatdan qat'i nazar ishlashi uchun state handlerlardan oldin turadi
@user_router.message(Command("cancel"))
@user_router.message(F.text == BTN_CANCEL)
async def cmd_cancel(m: Message, state: FSMContext):
    await reset_flow(state)
    await m.answer("Bekor qilindi.", reply_markup=main_menu())


@user_router.message(F.text == BTN_CONTACT)
async def btn_contact(m: Message, state: FSMContext):
    await reset_flow(state)
    await m.answer(f"Savollar bo'yicha: {CONTACT}", reply_markup=main_menu())


@user_router.message(F.text == BTN_ORDERS)
async def btn_orders(m: Message, state: FSMContext):
    await reset_flow(state)
    rows = db_all(
        "SELECT * FROM orders WHERE tg_id=? AND status!='cancelled' ORDER BY id DESC LIMIT 10",
        (m.from_user.id,),
    )
    if not rows:
        return await m.answer("Sizda hali buyurtma yo'q.", reply_markup=main_menu())
    text = "📦 <b>So'nggi buyurtmalaringiz</b>\n\n" + "\n\n".join(
        f"<b>#{o['id']}</b> — {o['uc']} UC ({fmt_price(o['price'])})\n{STATUS_TEXT[o['status']]}" for o in rows
    )
    await m.answer(text, reply_markup=main_menu())


@user_router.message(F.text == BTN_BUY)
async def btn_buy(m: Message, state: FSMContext):
    await reset_flow(state)
    upsert_user(m)
    packs = db_all("SELECT id, uc, price FROM products ORDER BY uc")
    if not packs:
        return await m.answer("Hozircha paketlar mavjud emas.", reply_markup=main_menu())
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💎 {p['uc']} UC — {fmt_price(p['price'])}", callback_data=f"pack:{p['id']}")]
            for p in packs
        ]
    )
    await m.answer("Qancha UC kerak? Paketni tanlang 👇", reply_markup=kb)


@user_router.callback_query(F.data.startswith("pack:"))
async def pick_pack(cb: CallbackQuery, state: FSMContext):
    product = db_one("SELECT * FROM products WHERE id=?", (int(cb.data.split(":")[1]),))
    if not product:
        return await cb.answer("Bu paket endi mavjud emas", show_alert=True)
    await reset_flow(state)
    await state.update_data(uc=product["uc"], price=product["price"])
    await state.set_state(OrderFlow.pubg_id)
    await cb.message.answer(
        f"Tanlandi: <b>{product['uc']} UC</b> — {fmt_price(product['price'])}\n\n"
        "Endi <b>PUBG Mobile ID</b> raqamingizni yuboring (faqat raqamlar).",
        reply_markup=cancel_menu(),
    )
    await cb.answer()


@user_router.message(OrderFlow.pubg_id, F.text)
async def got_pubg_id(m: Message, state: FSMContext):
    pid = m.text.strip()
    if not (pid.isdigit() and 5 <= len(pid) <= 15):
        return await m.answer("❗ ID faqat raqamlardan iborat bo'lishi kerak. Qayta yuboring.")
    data = await state.get_data()
    order_id = db_insert(
        "INSERT INTO orders(tg_id, pubg_id, uc, price, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        (m.from_user.id, pid, data["uc"], data["price"], "awaiting_receipt", now(), now()),
    )
    await state.update_data(order_id=order_id)
    await state.set_state(OrderFlow.receipt)
    await m.answer(
        f"🧾 Buyurtma <b>#{order_id}</b> yaratildi\n\n"
        f"💎 {data['uc']} UC\n"
        f"🎮 PUBG ID: <code>{pid}</code>\n"
        f"💰 To'lov summasi: <b>{fmt_price(data['price'])}</b>\n\n"
        f"💳 Karta: <code>{CARD_NUMBER}</code>\n"
        f"👤 {html.escape(CARD_OWNER)}\n\n"
        "To'lovni qilgach, <b>chek rasmini (screenshot)</b> shu yerga yuboring.",
        reply_markup=cancel_menu(),
    )


@user_router.message(OrderFlow.receipt, F.photo | F.document)
async def got_receipt(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    order_id = data.get("order_id")
    file_id = m.photo[-1].file_id if m.photo else m.document.file_id

    updated = db_update(
        "UPDATE orders SET status='pending', receipt_file_id=?, updated_at=? WHERE id=? AND status='awaiting_receipt'",
        (file_id, now(), order_id),
    )
    if not updated:
        await state.clear()
        return await m.answer("Buyurtma topilmadi. Qaytadan boshlang.", reply_markup=main_menu())

    order = get_order(order_id)
    caption = order_caption(order, "🕓 To'lov tekshirilishi kutilmoqda")
    for admin_id in ADMIN_IDS:
        try:
            if m.photo:
                await bot.send_photo(admin_id, file_id, caption=caption, reply_markup=admin_kb(order_id, "pending"))
            else:
                await bot.send_document(admin_id, file_id, caption=caption, reply_markup=admin_kb(order_id, "pending"))
        except Exception as e:
            log.warning("Adminga yuborilmadi (%s): %s", admin_id, e)

    await state.clear()
    await m.answer(
        f"✅ Chek qabul qilindi. Buyurtma <b>#{order_id}</b> tekshirilmoqda.\n"
        "Tasdiqlangach sizga xabar beramiz.",
        reply_markup=main_menu(),
    )


@user_router.message(OrderFlow.receipt)
async def receipt_wrong_type(m: Message):
    await m.answer("Iltimos, to'lov chekini <b>rasm</b> ko'rinishida yuboring (yoki ❌ Bekor qilish).")


@user_router.message(OrderFlow.pubg_id)
async def pubg_id_wrong_type(m: Message):
    await m.answer("PUBG ID'ni matn (raqam) ko'rinishida yuboring.")


@user_router.message()
async def fallback(m: Message):
    await m.answer("Quyidagi menyudan foydalaning 👇", reply_markup=main_menu())


# ----------------------------------------------------------------------------
# ISHGA TUSHIRISH
# ----------------------------------------------------------------------------
async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env faylini to'ldiring.")
    if not ADMIN_IDS:
        raise SystemExit("ADMIN_IDS topilmadi. .env faylida o'z Telegram ID'ingizni yozing.")

    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)  # admin buyruqlari birinchi tekshiriladi
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Bot ishga tushdi")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
