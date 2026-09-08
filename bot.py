import asyncio, base64, io, json, logging, math, os, random, secrets, time, zipfile
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo
import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update, LabeledPrice, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, PreCheckoutQueryHandler, filters
from shopbot.config import Settings, ADMIN_IDS
from shopbot.database import Database
from shopbot.services import ShopService, StorageManager
from shopbot.integrations import ImageService, GitHubStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx includes request URLs in INFO logs; Telegram Bot API URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
log=logging.getLogger(__name__)

def active_custom_buttons(context):
    try:
        with service(context).db.connect() as c:
            return c.execute("SELECT id,label,kind,target FROM custom_buttons WHERE is_active=1 ORDER BY id").fetchall()
    except Exception: return []

def main_menu(is_admin=False, customs=None):
    rows=[[B("المنتجات",callback_data="products")],[B("الخدمات",callback_data="services")],[B("شحن Credits",callback_data="topup"),B("حسابي",callback_data="account")]]
    for item in (customs or []):
        if item['kind'] == 'url' and item['target']:
            rows.append([B(item['label'],url=item['target'])])
        else:
            rows.append([B(item['label'],callback_data=f"custombtn:{item['id']}")])
    if is_admin: rows.append([B("لوحة الإدارة",callback_data="admin")])
    return M(rows)
def back_home(): return M([[B("رجوع",callback_data="home")]])
def service(context): return context.application.bot_data['service']
def settings(context): return context.application.bot_data['settings']

def admin_back(): return M([[B("رجوع للوحة الإدارة", callback_data="admin")]])

def format_time(value):
    if not value: return '-'
    try:
        moment=datetime.fromisoformat(value).replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Africa/Cairo'))
        return moment.strftime('%Y-%m-%d %I:%M %p').replace('AM','ص').replace('PM','م')
    except ValueError: return value

def subscriptions(context):
    with service(context).db.connect() as c:return c.execute("SELECT * FROM subscriptions WHERE is_active=1 ORDER BY is_required DESC,id").fetchall()

def api_chat_id(chat_id):
    # القنوات الخاصة تُخزن برقم — وتيليجرام يتوقع الرقم int لا نص
    try:
        s=str(chat_id).strip()
        if s.lstrip('-').isdigit(): return int(s)
    except Exception: pass
    return chat_id

def normalize_channel_id(raw):
    """يحول أي صيغة ID (4451031016 أو -1004451031016) إلى الرقم الصحيح -100..."""
    s=(raw or '').strip()
    if not s: return None
    if s.startswith('-'):
        return int(s) if s[1:].isdigit() else None
    if s.isdigit() and len(s) >= 5:
        if s.startswith('100') and len(s) > 10:
            return int('-'+s)
        return int('-100'+s)
    return None

async def is_subscribed(context, user_id, subscription):
    try:
        member=await context.bot.get_chat_member(api_chat_id(subscription['chat_id']),user_id)
        return member.status not in {'left','kicked'}
    except Exception:
        log.warning('Subscription verification failed',exc_info=True)
        return False

async def pending_required_subscriptions(context, user_id):
    pending=[]
    for subscription in subscriptions(context):
        if subscription['is_required'] and not await is_subscribed(context,user_id,subscription): pending.append(subscription)
    return pending

async def send_sub_prompt(chat_id, context, items, blocking):
    """يرسل رسائل الاشتراك (مع صور إن وُجدت) + زر تحقق للإجباري. يرجع IDs للمسح لاحقاً."""
    ids=[]
    use_photos=any(item['image_url'] for item in items)
    if use_photos:
        for item in items:
            cap=f"<b>{escape(item['title'])}</b>\n{escape(item['description'])}"
            kb=M([[B(f"📢 {item['title'][:35]}",url=item['url'])]])
            try:
                if item['image_url']:
                    m=await context.bot.send_photo(chat_id,item['image_url'],caption=cap,parse_mode=ParseMode.HTML,reply_markup=kb)
                else:
                    m=await context.bot.send_message(chat_id,cap,parse_mode=ParseMode.HTML,reply_markup=kb)
            except Exception:
                m=await context.bot.send_message(chat_id,cap,parse_mode=ParseMode.HTML,reply_markup=kb)
            ids.append(m.message_id)
    else:
        title='🔐 الاشتراك الإجباري' if blocking else '📢 قنوات اختيارية'
        text=f"<b>{title}</b>\n\n"+'\n\n'.join(f"<b>{escape(item['title'])}</b>\n{escape(item['description'])}" for item in items)
        buttons=[[B(f"📢 {item['title'][:35]}",url=item['url'])] for item in items]
        if blocking:
            text+='\n\n<b>بعد الاشتراك في كل القنوات اضغط زر التحقق بالأسفل.</b>'
            buttons.append([B("✅ تحققت من الاشتراك",callback_data="check_sub")])
        m=await context.bot.send_message(chat_id,text,parse_mode=ParseMode.HTML,reply_markup=M(buttons))
        ids.append(m.message_id)
    if blocking and use_photos:
        v=await context.bot.send_message(chat_id,"<b>بعد الاشتراك في كل القنوات اضغط زر التحقق بالأسفل.</b>",parse_mode=ParseMode.HTML,reply_markup=M([[B("✅ تحققت من الاشتراك",callback_data="check_sub")]]))
        ids.append(v.message_id)
    return ids

async def show_subscriptions(update, context, items, blocking):
    ids=await send_sub_prompt(update.effective_chat.id,context,items,blocking)
    if blocking:
        old=context.user_data.get('subscription_prompt_ids',[])
        context.user_data['subscription_prompt_ids']=old+ids

async def clear_subscription_prompt(update, context):
    ids=context.user_data.pop('subscription_prompt_ids',[])
    prompt_id=context.user_data.pop('subscription_prompt_id',None)
    if prompt_id: ids.append(prompt_id)
    for message_id in ids:
        try: await context.bot.delete_message(update.effective_chat.id,message_id)
        except Exception: pass

def normalize_support_url(target):
    target=(target or '').strip()
    if not target: return ''
    if target.startswith("@"): return f"https://t.me/{target[1:]}"
    if target.startswith(("https://","http://","tg://")): return target
    return f"https://t.me/{target}"

def ban_text(context):
    return service(context).setting("ban_message","⛔ حسابك محظور. تواصل مع خدمة العملاء للمراجعة.")

def db_file_path():
    return os.getenv("DATABASE_PATH","data.db")

def backup_database(dst_path):
    import sqlite3
    src=sqlite3.connect(db_file_path(),timeout=30)
    try:
        dst=sqlite3.connect(dst_path)
        try: src.backup(dst)
        finally: dst.close()
    finally: src.close()

async def edit_or_send(query, context, text, keyboard):
    """يحرر الرسالة الحالية (نص أو caption) وإلا يرسل جديدة — بطاقة المنتج تظل موجودة دائماً."""
    if getattr(query.message,'photo',None):
        # الزر مضغوط من بطاقة صور — نُبقي البطاقة ونرسل القائمة كرسالة جديدة
        await context.bot.send_message(query.message.chat_id,text,parse_mode=ParseMode.HTML,reply_markup=keyboard); return
    try:
        await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=keyboard); return
    except Exception: pass
    try:
        await query.edit_message_caption(caption=text,parse_mode=ParseMode.HTML,reply_markup=keyboard); return
    except Exception: pass
    try: await query.delete_message()
    except Exception: pass
    await context.bot.send_message(query.message.chat_id,text,parse_mode=ParseMode.HTML,reply_markup=keyboard)

def product_deep_link(bot_username, pid):
    return f"https://t.me/{bot_username}?start=product_{pid}"

def gift_deep_link(bot_username, code):
    return f"https://t.me/{bot_username}?start=gift_{code}"

async def show_home(update, context, edit=False):
    user=update.effective_user; markup=main_menu(user.id in ADMIN_IDS,active_custom_buttons(context))
    with service(context).db.connect() as c:
        u=c.execute("SELECT full_name,username,credits,registered_at FROM users WHERE telegram_id=?",(user.id,)).fetchone()
        refs=c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?",(user.id,)).fetchone()[0] if u else 0
    if u:
        username=f"@{u['username']}" if u['username'] else "لا يوجد"
        text=(f"👋 <b>أهلاً بك في المتجر</b>\n"
              f"━━━━━━━━━━━━━━\n"
              f"1️⃣ <b>الاسم:</b> {escape(u['full_name'] or '-')}\n"
              f"2️⃣ <b>اليوزر:</b> {escape(username)}\n"
              f"3️⃣ <b>الايدي:</b> <code>{user.id}</code>\n"
              f"💰 <b>الكريديت:</b> {u['credits']}\n"
              f"👥 <b>الإحالات:</b> {refs}\n"
              f"📅 <b>تاريخ التسجيل:</b> {format_time(u['registered_at'])}\n"
              f"━━━━━━━━━━━━━━\n"
              f"اختر الخدمة المطلوبة 👇")
    else:
        text="مرحباً بك في المتجر. اختر الخدمة المطلوبة."
    if edit: await update.callback_query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=markup)
    else: await update.effective_message.reply_text(text,parse_mode=ParseMode.HTML,reply_markup=markup)

async def reward_referral_if_ready(context, user_id):
    """يصرف مكافأة الإحالة بعد تحقق الاشتراكات الإجبارية + إشعار الطرفين. يرجع True لو صُرفت."""
    if await pending_required_subscriptions(context,user_id):
        return False
    with service(context).db.transaction() as c:
        res=service(context).try_reward_referral(c,user_id)
    if not res: return False
    referrer,giver,joiner=res
    with service(context).db.connect() as c:
        newcomer=c.execute("SELECT username,full_name FROM users WHERE telegram_id=?",(user_id,)).fetchone()
    name=(f"@{newcomer['username']}" if newcomer and newcomer['username'] else (newcomer['full_name'] if newcomer and newcomer['full_name'] else str(user_id)))
    if giver > 0:
        try:
            await context.bot.send_message(referrer,f"🎉 <b>تم استلام {giver} Credits</b>\n👤 عن طريق المستخدم: {escape(name)}",parse_mode=ParseMode.HTML)
        except Exception:
            log.warning("Could not notify referrer %s",referrer)
    if joiner > 0:
        try:
            await context.bot.send_message(user_id,f"🎁 تمت إضافة <b>{joiner} Credits</b> إلى رصيدك من رابط الإحالة. أهلاً بك!",parse_mode=ParseMode.HTML)
        except Exception: pass
    else:
        try:
            await context.bot.send_message(user_id,"✅ تم تفعيل رابط الإحالة الذي دخلت به. أهلاً بك في المتجر!")
        except Exception: pass
    return True

async def claim_gift(update, context, code):
    with service(context).db.transaction() as c:
        link=c.execute("SELECT * FROM redeem_links WHERE code=?",(code,)).fetchone()
        if not link or not link['is_active']:
            await update.effective_message.reply_text("⚠️ رابط الهدية غير صالح أو تم إيقافه."); return
        if link['used_count'] >= link['max_uses']:
            await update.effective_message.reply_text("⚠️ تم استنفاد هذا الرابط (اكتمل عدد المستخدمين)."); return
        if c.execute("SELECT 1 FROM redeem_claims WHERE link_id=? AND user_id=?",(link['id'],update.effective_user.id)).fetchone():
            await update.effective_message.reply_text("⚠️ لقد استخدمت هذا الرابط من قبل."); return
        c.execute("INSERT INTO redeem_claims(link_id,user_id) VALUES(?,?)",(link['id'],update.effective_user.id))
        c.execute("UPDATE redeem_links SET used_count=used_count+1 WHERE id=?",(link['id'],))
        service(context).credit(c,update.effective_user.id,link['credits'],"gift_link","gift_link",code,None)
    await update.effective_message.reply_text(f"🎁 تم تفعيل هديتك: <b>+{link['credits']} Credits</b>!",parse_mode=ParseMode.HTML,reply_markup=M([[B("🛍️ قائمة المنتجات",callback_data="products")]]))

async def start(update, context):
    raw=context.args[0] if context.args else None
    referral=None; product_payload=None; gift_payload=None
    if raw:
        if raw.startswith("product_") and raw[len("product_"):].isdigit(): product_payload=int(raw[len("product_"):])
        elif raw.startswith("gift_") and len(raw) > 5: gift_payload=raw[len("gift_"):].strip()
        elif raw.isdigit(): referral=raw
    service(context).ensure_user(update.effective_user,referral)
    if update.effective_user.id not in ADMIN_IDS:
        if is_user_banned(context,update.effective_user.id):
            await update.effective_message.reply_text(ban_text(context))
            return
        pending=await pending_required_subscriptions(context,update.effective_user.id)
        if pending: await show_subscriptions(update,context,pending,True); return
        await clear_subscription_prompt(update,context)
        optional=[item for item in subscriptions(context) if not item['is_required']]
        if optional: await show_subscriptions(update,context,optional,False)
    await reward_referral_if_ready(context,update.effective_user.id)
    if gift_payload:
        await claim_gift(update,context,gift_payload)
        if product_payload is None: return
    if product_payload is not None:
        p=service(context).product(product_payload)
        if not p:
            await update.effective_message.reply_text("⚠️ المنتج غير متاح.")
            await show_home(update,context); return
        await send_product_card(update.effective_chat.id,context,p,page=1)
        return
    await show_home(update,context)

def build_product_text(p, for_caption=False):
    is_free=p['product_type']=='free'
    type_label="🎁 مجاني" if is_free else "💳 مدفوع"
    price_label="مجاني" if is_free else f"{p['price']} Credits"
    title=p['title'] or ''
    short=p['short_description'] or ''
    description=p['description'] or ''
    if for_caption:
        # سقف آمن حتى لا يتجاوز الـcaption حد 1024 حرف بدون قص tags مكسورة
        title=title[:80]; short=short[:150]
        if len(description) > 350:
            description=description[:350].rstrip()+'…'
    lines=[(f"<b><u>{escape(p['title'])}</u></b>\n\n"
            f"<blockquote>{escape(p['short_description'])}</blockquote>\n\n"
            f"{escape(description)}\n\n"
            f"🏷️ النوع: <b>{type_label}</b>\n"
            f"💳 السعر: <b>{price_label}</b>")]
    if p['stock_quantity'] is not None:
        lines.append(f"📦 الكمية: {p['stock_quantity']}")
    return "\n".join(lines)

async def attach_buttons(chat_id, context, message_id, keyboard):
    """يثبت الأزرار تحت الألبوم/الصورة — قالب واحد (رسالة واحدة)."""
    try:
        await context.bot.edit_message_reply_markup(chat_id=chat_id,message_id=message_id,reply_markup=keyboard)
        return True
    except Exception:
        log.warning("Could not attach buttons to album message",exc_info=True)
        return False

def forget_product_messages(context, chat_id=None):
    data=context.user_data.pop('product_msg_ids',None)
    if data and chat_id is not None and data.get('chat_id') != chat_id:
        context.user_data['product_msg_ids']=data

async def cleanup_product_messages(context, chat_id):
    data=context.user_data.pop('product_msg_ids',None)
    if not data:
        return
    if data.get('chat_id') != chat_id:
        context.user_data['product_msg_ids']=data
        return
    for message_id in data.get('message_ids',[]):
        try: await context.bot.delete_message(chat_id,message_id)
        except Exception: pass

async def show_products(query, context, page=1, shuffle=False):
    # shuffle=True فقط عند الضغط على زر "المنتجات" من الرئيسية.
    # التنقل بين الصفحات أو الرجوع من منتج يحافظ على نفس الترتيب المختلط.
    stored=context.user_data.get('product_msg_ids')
    fresh_send=False
    if stored and stored.get('chat_id') == query.message.chat_id and query.message.message_id in stored.get('message_ids',[]):
        # راجع من بطاقة منتج — البطاقة تظل موجودة، نرسل القائمة كرسالة جديدة مباشرة
        forget_product_messages(context,query.message.chat_id)
        fresh_send=True
    else:
        forget_product_messages(context,query.message.chat_id)
    if shuffle or 'shuffled_products' not in context.user_data:
        pids=service(context).all_active_product_ids()
        random.shuffle(pids)
        context.user_data['shuffled_products']=pids
    else:
        pids=list(context.user_data['shuffled_products'])
        # إسقاط أي منتجات محذوفة من القائمة المحفوظة بدون إعادة خلط
        valid_ids=set(service(context).all_active_product_ids())
        pids=[pid for pid in pids if pid in valid_ids]
        # إضافة أي منتجات جديدة في النهاية بدون إعادة خلط القديم
        for pid in valid_ids:
            if pid not in set(pids): pids.append(pid)
        context.user_data['shuffled_products']=pids
    total=len(pids)
    if total==0:
        try: await query.edit_message_text("لا توجد منتجات متاحة حالياً.",reply_markup=back_home())
        except Exception:
            try: await query.delete_message()
            except Exception: pass
            await context.bot.send_message(query.message.chat_id,"لا توجد منتجات متاحة حالياً.",reply_markup=back_home())
        return
    pages=max(1,math.ceil(total/10)); page=max(1,min(page,pages))
    page_pids=pids[(page-1)*10:page*10]
    rows=service(context).active_products_by_ids(page_pids)
    keyboard=[]
    for r in rows:
        is_free=r['product_type']=='free'
        name_btn=B(f"{r['title'][:30]}",callback_data=f"product:{r['id']}:{page}")
        price_btn=B("🎁 مجاني",callback_data=f"product:{r['id']}:{page}") if is_free else B(f"💳 {r['price']} Credits",callback_data=f"product:{r['id']}:{page}")
        keyboard.append([name_btn,price_btn])
    keyboard.append([B("‹",callback_data=f"products:{page-1}" if page>1 else "noop"),B(f"{page} من {pages}",callback_data="noop"),B("›",callback_data=f"products:{page+1}" if page<pages else "noop")])
    keyboard.append([B("رجوع",callback_data="home")])
    header=("<b>🔥 المنتجات المدفوعة والمجانية — تسليم فوري بضغطة زر ⚡</b>\n"
            "<i>اختر المنتج المناسب لك: المجاني استلمه فوراً، والمدفوع اشترِه بالـCredits وسيصلك ملفه مباشرة.</i>")
    if fresh_send:
        await context.bot.send_message(query.message.chat_id,header,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))
        return
    try:
        await query.edit_message_text(header,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))
    except Exception:
        # غالباً رسالة المنتج السابقة كانت صورة وتم حذفها — نرسل القائمة كرسالة جديدة
        try: await query.delete_message()
        except Exception: pass
        await context.bot.send_message(query.message.chat_id,header,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))

DEFAULT_SHARE_TEMPLATE="شاهد هذا المنتج: {{title}}"

def render_share_text(context, title, link):
    # الرابط يُمرر في حقل url الخاص بالمشاركة — لا تكرره داخل النص افتراضياً
    template=service(context).setting("share_template",DEFAULT_SHARE_TEMPLATE)
    return template.replace("{{link}}",link).replace("{{title}}",title or "")

def build_share_url(deep, text):
    # لو النص فيه الرابط أصلاً (قالب مخصص فيه {{link}}) لا نمرره في حقل url حتى لا يتكرر مرتين
    if deep and deep in text:
        return f"https://t.me/share/url?text={quote(text)}"
    return f"https://t.me/share/url?url={quote(deep)}&text={quote(text)}"

def render_no_credits(context, credits, price):
    template=service(context).setting("no_credits_message","⚠️ <b>عدد الـCredits لا يكفي</b> لإتمام الشراء.\n💳 رصيدك الحالي: <b>{{credits}}</b> Credits\n💰 سعر المنتج: <b>{{price}}</b> Credits")
    return template.replace("{{credits}}",str(credits)).replace("{{price}}",str(price))

async def send_product_card(chat_id, context, p, page=1):
    """بطاقة المنتج: كل الصور + العنوان والوصف في الألبوم، والأزرار في رسالة ملاصقة بنفس القالب."""
    is_free=p['product_type']=='free'
    purchase_label="استلام مجاني" if is_free else "شراء"
    deep=product_deep_link(context.bot.username,p['id'])
    share_url=build_share_url(deep,render_share_text(context,p['title'],deep))
    keyboard=M([[B(purchase_label,callback_data=f"buy:{p['id']}:{page}")],[B("📤 مشاركة المنتج",url=share_url)],[B("رجوع",callback_data=f"products:{page}")]])
    image_urls=json.loads(p['image_urls'] or '[]')
    msg_ids=[]
    if len(image_urls) == 1:
        # صورة واحدة: رسالة واحدة حقيقية (صورة + وصف + أزرار)
        try:
            sent=await context.bot.send_photo(chat_id,image_urls[0],caption=build_product_text(p,for_caption=True),parse_mode=ParseMode.HTML,reply_markup=keyboard)
            context.user_data['product_msg_ids']={'chat_id':chat_id,'message_ids':[sent.message_id]}
            return
        except Exception:
            log.warning("Unable to send product photo, falling back",exc_info=True)
    elif len(image_urls) > 1:
        # كل الصور في ألبوم واحد + الأزرار مثبتة تحته = قالب واحد
        try:
            caption=build_product_text(p,for_caption=True)
            media=[InputMediaPhoto(url,caption=caption if index == 0 else None,parse_mode=ParseMode.HTML if index == 0 else None) for index,url in enumerate(image_urls[:10])]
            sent_group=await context.bot.send_media_group(chat_id,media)
            msg_ids=[message.message_id for message in sent_group]
            if await attach_buttons(chat_id,context,msg_ids[0],keyboard):
                context.user_data['product_msg_ids']={'chat_id':chat_id,'message_ids':msg_ids}
                return
        except Exception:
            log.warning("Unable to send product media group, falling back",exc_info=True)
            msg_ids=[]
    if not msg_ids:
        sent=await context.bot.send_message(chat_id,build_product_text(p),parse_mode=ParseMode.HTML,reply_markup=keyboard)
        context.user_data['product_msg_ids']={'chat_id':chat_id,'message_ids':[sent.message_id]}
        return
    head=await context.bot.send_message(chat_id,f"<b>{escape(p['title'])}</b>\n👇 اختر الإجراء:",parse_mode=ParseMode.HTML,reply_markup=keyboard)
    msg_ids.append(head.message_id)
    context.user_data['product_msg_ids']={'chat_id':chat_id,'message_ids':msg_ids}

async def show_product(query, context, product_id, page):
    p=service(context).product(product_id)
    if not p: await query.answer("المنتج غير متاح",show_alert=True); return
    chat_id=query.message.chat_id
    forget_product_messages(context,chat_id)
    try: await query.delete_message()
    except Exception: pass
    await send_product_card(chat_id,context,p,page)

async def account(query, context):
    with service(context).db.connect() as c:u=c.execute("SELECT * FROM users WHERE telegram_id=?",(query.from_user.id,)).fetchone(); refs=c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?",(query.from_user.id,)).fetchone()[0]
    text=f"<b>حسابي</b>\nالايدي: <code>{u['telegram_id']}</code>\nاليوزر: @{u['username'] or '-'}\nالاسم: {u['full_name']}\nالكريديت: {u['credits']}\nالإحالات: {refs}\nتاريخ التسجيل: {format_time(u['registered_at'])}"
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M([[B("إحالاتي",callback_data="referrals"),B("سجلاتي",callback_data="history")],[B("رجوع",callback_data="home")]]))

async def referrals(query, context):
    me=query.from_user.id
    with service(context).db.connect() as c:u=c.execute("SELECT referral_earned FROM users WHERE telegram_id=?",(me,)).fetchone(); count=c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?",(me,)).fetchone()[0]
    giver,joiner=service(context).referral_rewards()
    link=f"https://t.me/{context.bot.username}?start={me}"
    text=(f"👥 <b>الإحالات</b>\n"
          f"━━━━━━━━━━━━━━\n"
          f"🔗 <b>رابطك الخاص:</b>\n<b>{link}</b>\n"
          f"━━━━━━━━━━━━━━\n"
          f"👤 <b>الأشخاص:</b> {count}\n"
          f"🎁 <b>إجمالي مكافآتي:</b> {u['referral_earned']} كريديت\n"
          f"💰 <b>مكافأة كل صديق:</b> لك {giver} + له {joiner}\n"
          f"━━━━━━━━━━━━━━\n"
          f"<b>📋 التعليمات:</b>\n"
          f"• شارك رابطك واكسب {giver} كريديت عن كل عضو جديد.\n"
          f"• تُحسب الإحالة بعد اشتراك العضو في القنوات الإجبارية (إن وُجدت).\n"
          f"• ممنوع إحالة نفسك أو تكرار نفس الشخص.\n"
          f"• التحايل أو بوتات الرشق = إيقاف الحساب.")
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M([[B("رجوع",callback_data="account")]]))

HISTORY_ICONS={'registration':'👋','starting_credits':'🎉','purchase':'🛍️','free_product':'🎁','delivery':'📦','refund':'↩️','stars_payment':'⭐','gift_link':'🎁','referral':'🤝','admin_adjustment':'⚖️','ban':'⛔','unban':'✅'}

async def history(query, context):
    with service(context).db.connect() as c:
        total=c.execute("SELECT COUNT(*) FROM history WHERE user_id=?",(query.from_user.id,)).fetchone()[0]
        rows=c.execute("SELECT * FROM history WHERE user_id=? ORDER BY id DESC LIMIT 20",(query.from_user.id,)).fetchall()
    if not rows:
        text=("<b>📜 سجل عملياتي</b>\n"
              "🧾 الإجمالي: <b>0</b> عملية\n"
              "━━━━━━━━━━━━━━\n"
              "<i>لا توجد عمليات بعد.</i>\n\n"
              "📌 أي شراء أو شحن أو استلام سيظهر هنا تلقائياً.")
    else:
        lines=["<b>📜 سجل عملياتي</b>",f"🧾 الإجمالي: <b>{total}</b> عملية — الأحدث أولاً","━━━━━━━━━━━━━━",""]
        for i,r in enumerate(rows,1):
            icon=HISTORY_ICONS.get(r['event_type'],'📌')
            msg=escape(r['message'] or '-')
            lines.append(f"<b>{i}. {icon} {msg}</b>\n🕒 <i>{format_time(r['created_at'])}</i>\n━━━━━━━━━━━━━━")
        text="\n".join(lines)
    try:
        await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M([[B("رجوع",callback_data="account")]]))
    except Exception:
        try: await query.delete_message()
        except Exception: pass
        await context.bot.send_message(query.message.chat_id,text,parse_mode=ParseMode.HTML,reply_markup=M([[B("رجوع",callback_data="account")]]))

def all_support_contacts(context):
    with service(context).db.connect() as c:
        rows=c.execute("SELECT label,target FROM support_contacts WHERE is_active=1 ORDER BY id").fetchall()
    contacts=[(r['label'],normalize_support_url(r['target'])) for r in rows if normalize_support_url(r['target'])]
    if not contacts:
        legacy=service(context).setting("support_target",settings(context).support_url)
        if normalize_support_url(legacy):
            contacts=[("الشحن عن طريق خدمة العملاء",normalize_support_url(legacy))]
    return contacts

async def topup(query,context):
    rows=[]
    for label,target in all_support_contacts(context):
        rows.append([B(label or "خدمة العملاء",url=target)])
    rows += [[B("الشحن عن طريق نجوم Telegram",callback_data="stars")],[B("رجوع",callback_data="home")]]
    await query.edit_message_text("اختر طريقة الشحن:",reply_markup=M(rows))

async def stars(query,context):
    context.user_data['awaiting_credits']=True
    await query.edit_message_text("كم عدد الـCredits التي تريد شحنها؟ أرسل رقماً صحيحاً.",reply_markup=back_home())

async def credit_amount(update,context):
    text=update.message.text.strip()
    if update.effective_user.id not in ADMIN_IDS and is_user_banned(context,update.effective_user.id):
        await update.message.reply_text(ban_text(context))
        return
    if await handle_dex_text(update,context): return
    edit=context.user_data.get('edit_product')
    if edit:
        pid=edit['product_id']; field=edit['field']; page=edit.get('page',1)
        if field in ('title','short','description'):
            if not text: await update.message.reply_text("القيمة لا يمكن أن تكون فارغة."); return
            column={'title':'title','short':'short_description','description':'description'}[field]
            with service(context).db.transaction() as c:
                row=c.execute("SELECT id FROM products WHERE id=?",(pid,)).fetchone()
                if not row: context.user_data.pop('edit_product',None); await update.message.reply_text("المنتج غير موجود."); return
                c.execute(f"UPDATE products SET {column}=? WHERE id=?",(text,pid))
            context.user_data.pop('edit_product',None)
            await update.message.reply_text("✅ تم حفظ التعديل بنجاح.",reply_markup=M([[B("رجوع للمنتج",callback_data=f"admin_product:{pid}:{page}")]])); return
        if field == 'price':
            try: price=float(text)
            except ValueError: await update.message.reply_text("أرسل سعراً رقمياً صحيحاً."); return
            if price < 0: await update.message.reply_text("السعر لا يمكن أن يكون سالباً."); return
            with service(context).db.transaction() as c:c.execute("UPDATE products SET price=? WHERE id=?",(price,pid))
            context.user_data.pop('edit_product',None)
            await update.message.reply_text(f"✅ تم تحديث السعر إلى {price} Credits.",reply_markup=M([[B("رجوع للمنتج",callback_data=f"admin_product:{pid}:{page}")]])); return
        if field == 'quantity':
            try: quantity=int(text)
            except ValueError: await update.message.reply_text("أرسل عدداً صحيحاً للقطع، أو اضغط «كمية غير محدودة»."); return
            if quantity < 0: await update.message.reply_text("الكمية لا يمكن أن تكون سالبة."); return
            with service(context).db.transaction() as c:c.execute("UPDATE products SET stock_quantity=? WHERE id=?",(quantity,pid))
            context.user_data.pop('edit_product',None)
            await update.message.reply_text(f"✅ تم تحديث الكمية إلى {quantity}.",reply_markup=M([[B("رجوع للمنتج",callback_data=f"admin_product:{pid}:{page}")]])); return
        if field == 'file':
            if edit.get('step') == 'choice':
                await update.message.reply_text("اختر أولاً: هل المحتوى رابط ولا ملف؟",reply_markup=M([[B("🔗 رابط",callback_data="admin_edit_is_link"),B("📁 ملف",callback_data="admin_edit_is_file")]])); return
            if edit.get('step') == 'link_msg':
                edit['link_message']=text[:500]; edit['step']='awaiting'
                await update.message.reply_text("تمام. أرسل الرابط الجديد وسيُحفظ في ملف link.txt:",reply_markup=M([[B("إلغاء",callback_data=f"admin_product:{pid}:{page}")]])); return
            if edit.get('expect_link',True):
                url=text.strip()
                if not url.startswith(("https://","http://","tg://")):
                    await update.message.reply_text("أرسل رابطاً صحيحاً يبدأ بـ https://"); return
                filename, payload='link.txt', url.encode('utf-8')
                done_msg=f"✅ تم تحديث ملف المنتج #{pid} إلى رابط."
            else:
                if len(text) > 4000:
                    await update.message.reply_text("النص طويل جداً. أرسله كملف بدلاً من ذلك."); return
                with service(context).db.connect() as c:
                    row=c.execute("SELECT title FROM products WHERE id=?",(pid,)).fetchone()
                filename, payload=pack_upload(((row['title'] if row else 'product')[:40])+'.txt',text.encode('utf-8'))
                done_msg=f"✅ تم تحديث محتوى المنتج #{pid} بنجاح."
            try:
                await update.message.reply_text("جارٍ رفع المحتوى الجديد إلى التخزين…")
                await context.application.bot_data['storage'].upload_product(pid,payload,filename=filename)
                with service(context).db.transaction() as c:
                    c.execute("UPDATE products SET link_message=? WHERE id=?",(edit.get('link_message','') if edit.get('expect_link',True) else '',pid))
                context.user_data.pop('edit_product',None)
                await update.message.reply_text(done_msg,reply_markup=M([[B("رجوع للمنتج",callback_data=f"admin_product:{pid}:{page}")]]))
            except Exception:
                log.exception("Product content edit upload failed")
                await update.message.reply_text("تعذر رفع المحتوى. تأكد من صلاحيات GitHub ثم أعد المحاولة.")
            return
    addition=context.user_data.get('add_product')
    if addition:
        stage=addition['stage']
        if stage == 'title':
            if not text: await update.message.reply_text("العنوان لا يمكن أن يكون فارغاً."); return
            addition['title']=text; addition['stage']='short'; await update.message.reply_text("أرسل الوصف القصير للمنتج."); return
        if stage == 'short':
            if not text: await update.message.reply_text("الوصف القصير لا يمكن أن يكون فارغاً."); return
            addition['short']=text; addition['stage']='description'; await update.message.reply_text("أرسل الوصف الكامل للمنتج."); return
        if stage == 'description':
            if not text: await update.message.reply_text("الوصف الكامل لا يمكن أن يكون فارغاً."); return
            addition['description']=text
            if addition.get('product_type')=='free':
                addition['price']=0; addition['stage']='quantity'
                await update.message.reply_text("منتج مجاني 🎁 — تم ضبط السعر 0 تلقائياً.\n\nأرسل عدد القطع المتاحة، أو اختر تخطي لجعل الكمية غير محدودة.",reply_markup=M([[B("تخطي — كمية غير محدودة",callback_data="admin_add_quantity_skip")]])); return
            addition['stage']='price'; await update.message.reply_text("أرسل سعر المنتج بعدد الـCredits (مثال: 10 أو 10.5). "); return
        if stage == 'price':
            try: price=float(text)
            except ValueError: await update.message.reply_text("أرسل سعراً رقمياً صحيحاً."); return
            if price < 0: await update.message.reply_text("السعر لا يمكن أن يكون سالباً."); return
            addition['price']=price; addition['stage']='quantity'
            await update.message.reply_text("أرسل عدد القطع المتاحة، أو اختر تخطي لجعل الكمية غير محدودة.",reply_markup=M([[B("تخطي — كمية غير محدودة",callback_data="admin_add_quantity_skip")]])); return
        if stage == 'quantity':
            try: quantity=int(text)
            except ValueError: await update.message.reply_text("أرسل عدداً صحيحاً للقطع، أو اضغط تخطي."); return
            if quantity < 0: await update.message.reply_text("الكمية لا يمكن أن تكون سالبة."); return
            addition['quantity']=quantity; addition['images']=[]; addition['stage']='images'
            await update.message.reply_text("أرسل صور المنتج الآن (اختياري — يمكن التخطي). عند الانتهاء اضغط الزر التالي.",reply_markup=M([[B("انتهيت من إرسال الصور",callback_data="admin_add_images_done")]])); return
        if stage == 'file_choice':
            await update.message.reply_text("اختر أولاً: هل المحتوى رابط ولا ملف؟",reply_markup=M([[B("🔗 رابط",callback_data="admin_add_is_link"),B("📁 ملف",callback_data="admin_add_is_file")]])); return
        if stage == 'link_msg':
            addition['link_message']=text[:500]; addition['stage']='file'
            await update.message.reply_text("تمام. أرسل الرابط الآن (https://...) وسيُحفظ في ملف link.txt:",reply_markup=admin_back()); return
        if stage == 'file':
            # نص عادي → يتحول لـtxt، أو رابط → link.txt
            if addition.get('expect_link'):
                url=text.strip()
                if not url.startswith(("https://","http://","tg://")):
                    await update.message.reply_text("أرسل رابطاً صحيحاً يبدأ بـ https://"); return
                filename, payload='link.txt', url.encode('utf-8')
            else:
                if len(text) > 4000:
                    await update.message.reply_text("النص طويل جداً كمحتوى. أرسله كملف بدلاً من ذلك."); return
                safe_title=(addition.get('title') or 'product')[:40]
                filename, payload=pack_upload(safe_title+'.txt',text.encode('utf-8'))
            try:
                await update.message.reply_text("جارٍ رفع محتوى المنتج إلى التخزين…")
                product_id=await create_product_with_file(context,addition,filename,payload,update.effective_user.id)
                context.user_data.pop('add_product',None)
                await update.message.reply_text(f"تمت إضافة المنتج #{product_id} ورفع محتواه بنجاح.",reply_markup=M([[B("إدارة المنتجات",callback_data="admin_products:1"),B("لوحة الإدارة",callback_data="admin")]]))
            except Exception:
                log.exception("Product text upload failed")
                await update.message.reply_text("تعذر رفع محتوى المنتج. تأكد من صلاحيات GitHub ثم أعد المحاولة.")
            return
    adjustment=context.user_data.get('admin_credit_adjustment')
    if adjustment:
        try: amount=float(text)
        except ValueError: await update.message.reply_text("أرسل رقماً موجباً فقط."); return
        if amount <= 0: await update.message.reply_text("أرسل رقماً موجباً فقط."); return
        delta=amount if adjustment['operation'] == 'add' else -amount
        try:
            with service(context).db.transaction() as c: service(context).credit(c,adjustment['user_id'],delta,"admin_adjustment","admin_adjustment",None,update.effective_user.id)
        except ValueError: await update.message.reply_text("لا يمكن خصم هذا المبلغ لأن الرصيد غير كافٍ."); return
        target_id=adjustment['user_id']
        context.user_data.pop('admin_credit_adjustment',None)
        await update.message.reply_text(f"تم {'إضافة' if delta > 0 else 'خصم'} {amount} Credits بنجاح.",reply_markup=admin_back())
        try:
            if delta > 0:
                await context.bot.send_message(target_id,f"✅ تمت إضافة <b>{amount} Credits</b> إلى حسابك بواسطة الإدارة.",parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(target_id,f"➖ تم خصم <b>{amount} Credits</b> من حسابك بواسطة الإدارة.",parse_mode=ParseMode.HTML)
        except Exception:
            log.warning("Could not notify user %s about credit adjustment",target_id)
        return
    notification=context.user_data.get('admin_notification')
    if notification:
        if notification['stage'] == 'recipient':
            with service(context).db.connect() as c:
                if text.startswith('@'): row=c.execute("SELECT telegram_id FROM users WHERE lower(username)=lower(?)",(text[1:],)).fetchone()
                else: row=c.execute("SELECT telegram_id FROM users WHERE telegram_id=?",(text,)).fetchone() if text.isdigit() else None
            if not row: await update.message.reply_text("لم يتم العثور على المستخدم. أرسل Telegram ID أو @username صحيحاً."); return
            notification.update(stage='body',recipient=row['telegram_id']); await update.message.reply_text("أرسل نص الإشعار الآن."); return
        if notification['stage'] == 'body':
            with service(context).db.transaction() as c:
                recipients=[notification['recipient']] if notification['mode']=='direct' else [r[0] for r in c.execute("SELECT telegram_id FROM users WHERE is_active=1 AND telegram_id NOT IN (?,?)",tuple(ADMIN_IDS)).fetchall()]
                cur=c.execute("INSERT INTO notifications(sender_id,body,audience,status) VALUES(?,?,?,'Pending')",(update.effective_user.id,text,notification['mode']))
                notification_id=cur.lastrowid
                c.executemany("INSERT INTO notification_deliveries(notification_id,user_id) VALUES(?,?)",[(notification_id,user_id) for user_id in recipients])
            context.user_data.pop('admin_notification',None)
            asyncio.create_task(deliver_notification(context.application,notification_id))
            await update.message.reply_text(f"تم وضع الإشعار في الطابور لـ {len(recipients)} مستخدم.",reply_markup=admin_back()); return
    support_setup=context.user_data.get('admin_support_setup')
    if support_setup:
        if support_setup['stage'] == 'label':
            if not text: await update.message.reply_text("أرسل اسماً غير فارغ."); return
            support_setup.update(stage='target',label=text)
            await update.message.reply_text("أرسل الآن الوجهة: @username أو رابط قناة/مجموعة أو رابط كامل.",reply_markup=admin_back()); return
        if support_setup['stage'] == 'target':
            if not normalize_support_url(text):
                await update.message.reply_text("أرسل وجهة صحيحة (@username أو رابط)."); return
            with service(context).db.transaction() as c:
                c.execute("INSERT INTO support_contacts(label,target) VALUES(?,?)",(support_setup['label'],text.strip()))
            context.user_data.pop('admin_support_setup',None)
            await update.message.reply_text("✅ تمت إضافة جهة خدمة العملاء.",reply_markup=M([[B("خدمة العملاء",callback_data="admin_support")],[B("لوحة الإدارة",callback_data="admin")]])); return
    custom_setup=context.user_data.get('admin_custom_setup')
    if custom_setup:
        if custom_setup['stage'] == 'label':
            if not text or len(text) > 40: await update.message.reply_text("أرسل اسماً غير فارغ (بحد أقصى 40 حرفاً)."); return
            custom_setup.update(stage='kind',label=text)
            await update.message.reply_text("اختر نوع الزر:",reply_markup=M([
                [B("🔗 رابط خارجي",callback_data="admin_custom_kind:url")],
                [B("↩️ وجهة داخلية",callback_data="admin_custom_kind:internal")],
                [B("💬 رسالة منبثقة",callback_data="admin_custom_kind:dialog")]])); return
        if custom_setup['stage'] == 'target':
            if not text.startswith(("https://","http://","tg://")):
                await update.message.reply_text("أرسل رابطاً يبدأ بـ https://"); return
            with service(context).db.transaction() as c:
                cur=c.execute("INSERT INTO custom_buttons(label,kind,target,is_active) VALUES(?, 'url', ?, 1)",(custom_setup['label'],text.strip()))
                bid=cur.lastrowid
            context.user_data.pop('admin_custom_setup',None)
            await update.message.reply_text("✅ تم إنشاء الزر.",reply_markup=M([[B("عرض الزر",callback_data=f"admin_custom_item:{bid}")],[B("لوحة الإدارة",callback_data="admin")]])); return
        if custom_setup['stage'] == 'body':
            if not text: await update.message.reply_text("أرسل نصاً غير فارغ."); return
            if len(text) > 200:
                await update.message.reply_text(f"⚠️ رسالة الديالوج في تيليجرام حدها 200 حرف فقط (أرسلت {len(text)}). أرسل نصاً أقصر.",reply_markup=M([[B("رجوع",callback_data="admin_custom")]])); return
            with service(context).db.transaction() as c:
                cur=c.execute("INSERT INTO custom_buttons(label,kind,body,is_active) VALUES(?, 'dialog', ?, 1)",(custom_setup['label'],text))
                bid=cur.lastrowid
            context.user_data.pop('admin_custom_setup',None)
            await update.message.reply_text("✅ تم إنشاء الزر.",reply_markup=M([[B("عرض الزر",callback_data=f"admin_custom_item:{bid}")],[B("لوحة الإدارة",callback_data="admin")]])); return
    if context.user_data.get('admin_share_setup'):
        if "{{link}}" not in text:
            await update.message.reply_text("يجب أن يحتوي النص على المتغير {{link}}. أعد الإرسال."); return
        service(context).set_setting('share_template',text.strip())
        context.user_data.pop('admin_share_setup',None)
        await update.message.reply_text("✅ تم حفظ نص المشاركة.",reply_markup=M([[B("المنتجات",callback_data="admin_products:1"),B("لوحة الإدارة",callback_data="admin")]])); return
    referral_setup=context.user_data.get('admin_referral_setup')
    if referral_setup:
        try: amount=float(text)
        except ValueError: await update.message.reply_text("أرسل رقماً صحيحاً (ويمكن 0)."); return
        if amount < 0 or amount > 100000: await update.message.reply_text("أرسل رقماً بين 0 و 100000."); return
        key='referral_giver_reward' if referral_setup['field'] == 'giver' else 'referral_joiner_reward'
        service(context).set_setting(key,str(amount))
        context.user_data.pop('admin_referral_setup',None)
        giver,joiner=(amount,service(context).referral_rewards()[1]) if referral_setup['field'] == 'giver' else (service(context).referral_rewards()[0],amount)
        await update.message.reply_text(f"✅ تم الحفظ.\n🎁 مكافأة المُحيل: {giver}\n🎉 مكافأة المنضم: {joiner}",reply_markup=M([[B("مكافآت الإحالة",callback_data="admin_referral_cfg"),B("لوحة الإدارة",callback_data="admin")]])); return
    if context.user_data.get('admin_nocredits_setup'):
        if not text: await update.message.reply_text("أرسل نصاً غير فارغ."); return
        service(context).set_setting('no_credits_message',text.strip())
        context.user_data.pop('admin_nocredits_setup',None)
        await update.message.reply_text("✅ تم حفظ رسالة نفاد الكريديتس.",reply_markup=M([[B("المنتجات",callback_data="admin_products:1"),B("لوحة الإدارة",callback_data="admin")]])); return
    if context.user_data.get('admin_banmsg_setup'):
        if not text: await update.message.reply_text("أرسل نصاً غير فارغ."); return
        service(context).set_setting('ban_message',text.strip())
        context.user_data.pop('admin_banmsg_setup',None)
        await update.message.reply_text("✅ تم حفظ رسالة الحظر.",reply_markup=M([[B("المستخدمون",callback_data="admin_users:1"),B("لوحة الإدارة",callback_data="admin")]])); return
    gift_setup=context.user_data.get('admin_gift_setup')
    if gift_setup:
        if gift_setup['stage'] == 'credits':
            try: credits=float(text)
            except ValueError: await update.message.reply_text("أرسل رقماً صحيحاً لعدد الـCredits."); return
            if credits <= 0: await update.message.reply_text("أرسل رقماً موجباً فقط."); return
            gift_setup.update(stage='max_uses',credits=credits)
            await update.message.reply_text("أرسل عدد الأشخاص المسموح لهم باستخدام الرابط (مثال: 10):",reply_markup=M([[B("رجوع",callback_data="admin_gifts:1")]])); return
        if gift_setup['stage'] == 'max_uses':
            try: max_uses=int(text)
            except ValueError: await update.message.reply_text("أرسل عدداً صحيحاً."); return
            if max_uses <= 0 or max_uses > 100000: await update.message.reply_text("أرسل عدداً بين 1 و 100000."); return
            code=secrets.token_hex(4)
            with service(context).db.transaction() as c:
                cur=c.execute("INSERT INTO redeem_links(code,credits,max_uses,created_by) VALUES(?,?,?,?)",(code,gift_setup['credits'],max_uses,update.effective_user.id))
                link_id=cur.lastrowid
            context.user_data.pop('admin_gift_setup',None)
            await update.message.reply_text(f"✅ تم إنشاء رابط الهدية.",reply_markup=M([[B("عرض الرابط",callback_data=f"admin_gift:{link_id}:1")],[B("لوحة الإدارة",callback_data="admin")]])); return
    dex_edit=context.user_data.get('admin_dex_edit')
    if dex_edit:
        sid=dex_edit['service_id']; page=dex_edit.get('page',1)
        if dex_edit['field'] in ('title','description'):
            if dex_edit['field'] == 'title' and not text:
                await update.message.reply_text("أرسل اسماً غير فارغ."); return
            column='title' if dex_edit['field'] == 'title' else 'description'
            with service(context).db.transaction() as c:c.execute(f"UPDATE services SET {column}=? WHERE id=?",(text,sid))
            context.user_data.pop('admin_dex_edit',None)
            await update.message.reply_text("✅ تم الحفظ.",reply_markup=M([[B("رجوع للخدمة",callback_data=f"admin_dex_item:{sid}:{page}")]])); return
        if dex_edit['field'] == 'credits':
            try: credits=float(text)
            except ValueError: await update.message.reply_text("أرسل رقماً صحيحاً."); return
            if credits < 0: await update.message.reply_text("لا يمكن أن يكون سالباً."); return
            with service(context).db.transaction() as c:c.execute("UPDATE services SET credits=? WHERE id=?",(credits,sid))
            context.user_data.pop('admin_dex_edit',None)
            await update.message.reply_text(f"✅ تم تحديث الكريديتس إلى {credits}.",reply_markup=M([[B("رجوع للخدمة",callback_data=f"admin_dex_item:{sid}:{page}")]])); return
    subscription_setup=context.user_data.get('admin_subscription_setup')
    if subscription_setup:
        stage=subscription_setup['stage']
        if stage=='title':
            if not text: await update.message.reply_text("أرسل عنواناً غير فارغ."); return
            subscription_setup.update(stage='description',title=text); await update.message.reply_text("أرسل وصف رسالة الاشتراك."); return
        if stage=='description':
            if not text: await update.message.reply_text("أرسل وصفاً غير فارغ."); return
            subscription_setup.update(stage='url',description=text); await update.message.reply_text(SUB_URL_PROMPT,parse_mode=ParseMode.HTML); return
        if stage=='url':
            fwd=forwarded_channel(update)
            raw=text.strip()
            if 't.me/+' in raw or 't.me/joinchat' in raw:
                # رابط دعوة وليس معرفاً — لا يمكن استخدامه للإضافة مباشرة
                await update.message.reply_text("⚠️ هذا <b>رابط دعوة</b> وليس معرف القناة.\n\nأرسل الـ <b>ID الرقمي</b> للقناة (مثل <code>4451031016</code>) أو حوّل رسالة من القناة.",parse_mode=ParseMode.HTML); return
            numeric_id=None
            if fwd is not None:
                numeric_id=fwd.id
            else:
                # ID رقمي بأي صيغة (مع أو بدون -100) — يعمل للعامة والخاصة بدون يوزر
                numeric_id=normalize_channel_id(raw)
            if numeric_id is not None:
                ok,reason=await verify_bot_admin(context,numeric_id)
                if not ok:
                    await update.message.reply_text(f"⚠️ {reason}"); return
                try:
                    invite=await context.bot.export_chat_invite_link(api_chat_id(numeric_id))
                except Exception:
                    invite=None
                    log.warning("Auto invite export failed for %s",numeric_id,exc_info=True)
                subscription_setup.update(chat_id=str(numeric_id),url=invite or '')
                if not invite:
                    subscription_setup['stage']='invite'
                    await update.message.reply_text("✅ تم التحقق من القناة (البوت مدير ويقرأ الأعضاء).\n\nتعذر إنشاء رابط دعوة تلقائياً (يحتاج صلاحية دعوة الأعضاء) — أرسل رابط الدعوة يدوياً (https://t.me/+...)."); return
                subscription_setup['stage']='photo'
                await update.message.reply_text("✅ تم التحقق من القناة وتم إنشاء رابط الدعوة.\n\nهل تريد إرفاق صورة تظهر مع رسالة الاشتراك؟ أرسل الصورة الآن أو اضغط تخطي.",reply_markup=M([[B("تخطي ⏩",callback_data="admin_sub_skip_photo")],[B("رجوع",callback_data="admin_required_sub")]])); return
            username=raw.rsplit('/',1)[-1].lstrip('@') if 't.me/' in raw else raw.lstrip('@')
            if not username or any(char in username for char in ' ?#'):
                await update.message.reply_text(SUB_URL_PROMPT,parse_mode=ParseMode.HTML); return
            subscription_setup.update(chat_id=f"@{username}",url=f"https://t.me/{username}")
            ok,reason=await verify_bot_admin(context,subscription_setup['chat_id'])
            if not ok:
                await update.message.reply_text(f"⚠️ {reason}"); return
            subscription_setup['stage']='photo'
            await update.message.reply_text("✅ تم التحقق من القناة.\n\nهل تريد إرفاق صورة تظهر مع رسالة الاشتراك؟ أرسل الصورة الآن أو اضغط تخطي.",reply_markup=M([[B("تخطي ⏩",callback_data="admin_sub_skip_photo")],[B("رجوع",callback_data="admin_required_sub")]])); return
        if stage=='invite':
            invite=text.strip()
            if not invite.startswith("https://t.me/"):
                await update.message.reply_text("أرسل رابط دعوة صحيحاً (https://t.me/+...)."); return
            subscription_setup.update(url=invite,stage='photo')
            await update.message.reply_text("تمام. هل تريد إرفاق صورة تظهر مع رسالة الاشتراك؟ أرسل الصورة الآن أو اضغط تخطي.",reply_markup=M([[B("تخطي ⏩",callback_data="admin_sub_skip_photo")],[B("رجوع",callback_data="admin_required_sub")]])); return
        if stage=='photo':
            await update.message.reply_text("أرسل صورة أو اضغط تخطي.",reply_markup=M([[B("تخطي ⏩",callback_data="admin_sub_skip_photo")]])); return
    if not context.user_data.get('awaiting_credits'): return
    try: credits=int(update.message.text)
    except ValueError: await update.message.reply_text("أدخل عدداً صحيحاً أكبر من صفر."); return
    if not 0<credits<=100000: await update.message.reply_text("العدد غير مسموح."); return
    context.user_data.pop('awaiting_credits',None); s=settings(context); stars=credits*s.stars_per_credit; payment_id=service(context).create_payment(update.effective_user.id,credits,stars); payload=f"pay:{payment_id}:{update.effective_user.id}:{credits}:{stars}"
    await context.bot.send_invoice(chat_id=update.effective_chat.id,title="شحن Credits",description=f"شحن {credits} Credits",payload=payload,provider_token="",currency="XTR",prices=[LabeledPrice(f"{credits} Credits",stars)])

def payment_data(payload):
    parts=payload.split(":")
    if len(parts)!=5 or parts[0]!="pay": raise ValueError
    return parts[1],int(parts[2]),int(parts[3]),int(parts[4])
async def precheckout(update,context):
    q=update.pre_checkout_query
    try: pid,uid,credits,stars=payment_data(q.invoice_payload); ok=uid==q.from_user.id and service(context).validate_payment(pid,uid,credits,stars)
    except (ValueError,TypeError): ok=False
    await q.answer(ok=ok,error_message="بيانات الدفع غير صالحة أو انتهت صلاحيتها." if not ok else None)
async def successful_payment(update,context):
    payment=update.message.successful_payment
    try: pid,uid,credits,stars=payment_data(payment.invoice_payload); ok=uid==update.effective_user.id and payment.currency=="XTR" and payment.total_amount==stars and service(context).settle_payment(pid,uid,payment.telegram_payment_charge_id)
    except Exception: log.exception("payment processing failed"); ok=False
    if ok:
        try: await update.message.delete()
        except Exception: pass
        await context.bot.send_message(update.effective_chat.id,f"✅ تم إضافة <b>{credits} Credits</b> إلى حسابك بنجاح ⭐️",parse_mode=ParseMode.HTML,reply_markup=M([[B("🛍️ قائمة المنتجات",callback_data="products")]]))
    else:
        await update.message.reply_text("تم استلام الدفع مسبقاً أو تعذر التحقق منه. تواصل مع الدعم.")

async def deliver_product_file(chat_id, context, storage, product, data):
    """تسليم ذكي: ملف link.txt يُرسل كنص/رابط بدون ملف، وغيره كمستند بامتداده الأصلي."""
    storage_name=(product['storage_path'] or '').strip()
    if storage_name == 'link.txt':
        # يستخرج الرابط من الملف ويبعته كزر عنوانه اسم الخدمة فقط
        url=data.decode('utf-8',errors='replace').strip()
        title=(product['title'] or 'الخدمة')[:60]
        try: extra=(product['link_message'] or '').strip()
        except Exception: extra=''
        lines=[f"<b>{escape(title)}</b>"]
        if extra: lines.append(escape(extra))
        if url.startswith(("https://","http://","tg://")):
            await context.bot.send_message(chat_id,"\n\n".join(lines),parse_mode=ParseMode.HTML,reply_markup=M([[B(title,url=url)]]))
        else:
            lines.append(escape(url))
            await context.bot.send_message(chat_id,"\n\n".join(lines),parse_mode=ParseMode.HTML)
        return
    filename=storage_name or storage.archive_name(product['title'],product['id'])
    await context.bot.send_document(chat_id,io.BytesIO(data),filename=filename)

TELEGRAM_BOT_DOWNLOAD_LIMIT=20*1024*1024

def fmt_mb(size):
    try: return f"{size/1024/1024:.1f} MB"
    except Exception: return "?"

def pack_upload(filename, raw):
    """يجهز بايتات الرفع: أي ملف (apk/zip/غيره) يُرفع بأصله كما هو بدون تغيير."""
    fn=StorageManager.file_name('',0,filename or 'product_file')
    return fn, raw

async def download_document_bytes(context, document):
    size=document.file_size or 0
    if size > TELEGRAM_BOT_DOWNLOAD_LIMIT:
        raise ValueError(f"TOO_BIG_FOR_TELEGRAM:{size}")
    remote=await context.bot.get_file(document.file_id)
    try:
        return bytes(await remote.download_as_bytearray())
    except Exception as exc:
        # غالباً ملف أكبر من حد تيليجرام (20MB) رغم أن الحجم المعلن أصغر
        raise ValueError(f"TELEGRAM_DOWNLOAD_FAILED:{exc}")

def storage_error_text(exc):
    msg=str(exc)
    if "NO_STORAGE_REPOSITORY" in msg:
        return "⚠️ لا توجد مساحة تخزين كافية في مستودعات GitHub. أنشئ مساحة أو احذف منتجات قديمة ثم أعد المحاولة."
    if "TOO_BIG_FOR_TELEGRAM:" in msg:
        size=int(msg.split(":",1)[1] or 0)
        return (f"⚠️ حجم الملف ({fmt_mb(size)}) أكبر من حد تيليجرام للبوتات ({fmt_mb(TELEGRAM_BOT_DOWNLOAD_LIMIT)}).\n"
                "تيليجرام نفسه لا يوصّل الملفات الأكبر من ذلك للبوت، لذا لا يمكن رفعه.\n"
                "الحل: قسّم الملف لأجزاء أصغر من 20MB ثم ارفعها.")
    if "TELEGRAM_DOWNLOAD_FAILED:" in msg:
        return ("⚠️ تعذر تحميل الملف من تيليجرام (غالباً تجاوز حد الـ20MB للبوتات).\n"
                "قسّم الملف لأجزاء أصغر ثم أعد الإرسال.")
    detail=msg[:150]
    return (f"تعذر رفع ملف المنتج. تأكد أن GitHub token لديه صلاحية Contents: Read and write، ثم أعد إرسال الملف. المنتج غير منشور حتى ينجح الرفع.\n<i>السبب: {escape(detail)}</i>")

async def create_product_with_file(context, addition, filename, data, admin_id):
    with service(context).db.transaction() as c:
        cur=c.execute("INSERT INTO products(title,short_description,description,price,product_type,stock_quantity,image_urls,link_message,is_active,created_by) VALUES(?,?,?,?,?,?,?,?,0,?)",(addition['title'],addition['short'],addition['description'],addition['price'],addition.get('product_type','paid'),addition.get('quantity'),json.dumps(addition['images']),addition.get('link_message',''),admin_id))
        product_id=cur.lastrowid
    await context.application.bot_data['storage'].upload_product(product_id,data,filename=filename)
    return product_id

async def buy(query,context,pid,page):
    lock_key=f'buying:{pid}'
    if context.user_data.get(lock_key):
        await query.answer("⏳ طلبك قيد التجهيز بالفعل، انتظر قليلاً…",show_alert=True); return
    context.user_data[lock_key]=True
    try:
        try: purchase_id,p=service(context).purchase(query.from_user.id,pid)
        except ValueError as exc:
            if "insufficient" in str(exc).lower():
                with service(context).db.connect() as c:
                    brow=c.execute("SELECT credits FROM users WHERE telegram_id=?",(query.from_user.id,)).fetchone()
                    prow=c.execute("SELECT price FROM products WHERE id=?",(pid,)).fetchone()
                balance=brow['credits'] if brow else 0
                price=prow['price'] if prow else '?'
                await context.bot.send_message(query.message.chat_id,render_no_credits(context,balance,price),parse_mode=ParseMode.HTML,reply_markup=M([[B("💳 شحن Credits",callback_data="topup")]]))
            else:
                await query.answer(str(exc),show_alert=True)
            return
        # بطاقة المنتج تظل كما هي — رسالة "جارٍ التجهيز" تُرسل جديدة دائماً
        preparing_message_id=None
        try:
            sent=await context.bot.send_message(query.message.chat_id,"✅ تم تأكيد طلبك.\n\n⏳ جارٍ تجهيز منتجك…")
            preparing_message_id=sent.message_id
        except Exception:
            preparing_message_id=None
        storage=context.application.bot_data['storage']
        try:
            last_error=None; fresh=p; data=None
            for attempt in range(5):
                try:
                    # إعادة قراءة المنتج قبل كل محاولة حتى نلتقط أي إصلاح ذاتي لمكان الملف
                    fresh=service(context).product(pid) or p
                    data=await storage.download_product(fresh)
                    last_error=None; break
                except Exception as exc:
                    last_error=exc
                    log.warning("Delivery attempt %d for product %s failed: %s",attempt+1,pid,exc)
                    if attempt < 4: await asyncio.sleep(2 ** attempt)
            if last_error is not None: raise last_error
            await deliver_product_file(query.message.chat_id,context,storage,fresh,data)
            with service(context).db.transaction() as c:c.execute("UPDATE purchases SET status='Completed',updated_at=CURRENT_TIMESTAMP WHERE id=?",(purchase_id,)); service(context).history(c,query.from_user.id,"delivery","تم تسليم المنتج",{"purchase_id":purchase_id})
            if preparing_message_id:
                try: await context.bot.delete_message(query.message.chat_id,preparing_message_id)
                except Exception: pass
            context.user_data.pop('product_msg_ids',None)
            await context.bot.send_message(query.message.chat_id,"تم تسليم المنتج بنجاح.",reply_markup=main_menu(query.from_user.id in ADMIN_IDS,active_custom_buttons(context)))
        except Exception:
            log.exception("delivery failed"); service(context).refund(purchase_id,"فشل تسليم المنتج وتم استرداد الرصيد تلقائياً")
            if preparing_message_id:
                try: await context.bot.delete_message(query.message.chat_id,preparing_message_id)
                except Exception: pass
            context.user_data.pop('product_msg_ids',None)
            await context.bot.send_message(query.message.chat_id,"تعذر التسليم، وتم استرداد Credits تلقائياً. حاول مرة أخرى لاحقاً.",reply_markup=main_menu(query.from_user.id in ADMIN_IDS,active_custom_buttons(context)))
    finally:
        context.user_data.pop(lock_key,None)

async def admin_dashboard(query, context):
    await query.edit_message_text("<b>لوحة الإدارة</b>\nاختر القسم الذي تريد إدارته.",parse_mode=ParseMode.HTML,reply_markup=M([
        [B("المنتجات",callback_data="admin_products:1"),B("المستخدمون",callback_data="admin_users:1")],
        [B("الإشعارات",callback_data="admin_notifications"),B("الإحصاءات",callback_data="admin_statistics")],
        [B("إدارة التخزين",callback_data="admin_storage"),B("خدمة العملاء",callback_data="admin_support")],
        [B("🎁 روابط الهدايا",callback_data="admin_gifts:1"),B("🔘 الأزرار المخصصة",callback_data="admin_custom")],
        [B("الاشتراك الإجباري",callback_data="admin_required_sub"),B("🎁 مكافآت الإحالة",callback_data="admin_referral_cfg")],
        [B("الخدمات",callback_data="admin_dex:1")],
        [B("رجوع للرئيسية",callback_data="home")],
    ]))

def product_row_tag(r):
    if not r['is_active']: return "⏸️"
    return "🎁" if r['product_type']=='free' else "💳"

async def admin_products(query, context, page=1):
    rows,total=service(context).all_products(page); pages=max(1,math.ceil(total/10)); page=max(1,min(page,pages)); rows,total=service(context).all_products(page)
    keyboard=[]
    for r in rows:
        keyboard.append([B(f"{product_row_tag(r)} {r['title'][:30]}",callback_data=f"admin_product:{r['id']}:{page}")])
    keyboard.append([B("‹",callback_data=f"admin_products:{page-1}" if page>1 else "noop"),B(f"{page} من {pages}",callback_data="noop"),B("›",callback_data=f"admin_products:{page+1}" if page<pages else "noop")])
    keyboard.append([B("➕ إضافة منتج",callback_data="admin_product_add")])
    keyboard.append([B("⏸️ المنتجات الموقوفة",callback_data="admin_products_stopped:1"),B("📊 الإحصائيات",callback_data="admin_products_stats")])
    keyboard.append([B("🏆 الأكثر شراءً",callback_data="admin_products_top")])
    keyboard.append([B("✏️ نص المشاركة",callback_data="admin_share_template"),B("✏️ رسالة الرصيد",callback_data="admin_nocredits")])
    keyboard.append([B("رجوع للوحة الإدارة",callback_data="admin")])
    await edit_or_send(query,context,"<b>إدارة جميع المنتجات</b>\nاختر منتجاً أو أضف منتجاً جديداً.",M(keyboard))

async def admin_products_stopped(query, context, page=1):
    with service(context).db.connect() as c:
        total=c.execute("SELECT COUNT(*) FROM products WHERE is_active=0").fetchone()[0]
        pages=max(1,math.ceil(total/10)); page=max(1,min(page,pages))
        rows=c.execute("SELECT * FROM products WHERE is_active=0 ORDER BY id DESC LIMIT 10 OFFSET ?",((page-1)*10,)).fetchall()
    keyboard=[[B(f"⏸️ {r['title'][:30]}",callback_data=f"admin_product:{r['id']}:{page}")] for r in rows]
    if not rows:
        await edit_or_send(query,context,"<b>⏸️ المنتجات الموقوفة</b>\n\nلا توجد منتجات موقوفة.",M([[B("رجوع للمنتجات",callback_data="admin_products:1")]])); return
    keyboard.append([B("‹",callback_data=f"admin_products_stopped:{page-1}" if page>1 else "noop"),B(f"{page} من {pages}",callback_data="noop"),B("›",callback_data=f"admin_products_stopped:{page+1}" if page<pages else "noop")])
    keyboard.append([B("رجوع للمنتجات",callback_data="admin_products:1")])
    await edit_or_send(query,context,"<b>⏸️ المنتجات الموقوفة</b>\nاضغط على أي منتج لتفعيله مجدداً.",M(keyboard))

async def admin_products_stats(query, context):
    with service(context).db.connect() as c:
        total=c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        paid=c.execute("SELECT COUNT(*) FROM products WHERE is_active=1 AND product_type='paid'").fetchone()[0]
        free=c.execute("SELECT COUNT(*) FROM products WHERE is_active=1 AND product_type='free'").fetchone()[0]
        stopped=c.execute("SELECT COUNT(*) FROM products WHERE is_active=0").fetchone()[0]
        purchases=c.execute("SELECT COUNT(*) FROM purchases WHERE status='Completed'").fetchone()[0]
    text=(f"<b>📊 إحصائيات المنتجات</b>\n\n"
          f"📦 إجمالي المنتجات: {total}\n"
          f"💳 مدفوعة (نشطة): {paid}\n"
          f"🎁 مجانية (نشطة): {free}\n"
          f"⏸️ موقوفة: {stopped}\n"
          f"✅ عمليات تسليم مكتملة: {purchases}")
    await edit_or_send(query,context,text,M([[B("رجوع للمنتجات",callback_data="admin_products:1")]]))

async def admin_products_top(query, context):
    with service(context).db.connect() as c:
        rows=c.execute("SELECT p.id,p.title,COUNT(pu.id) AS cnt FROM products p LEFT JOIN purchases pu ON pu.product_id=p.id AND pu.status='Completed' GROUP BY p.id ORDER BY cnt DESC,p.id DESC LIMIT 10").fetchall()
    if not any(r['cnt'] for r in rows):
        await edit_or_send(query,context,"<b>🏆 الأكثر شراءً</b>\n\nلا توجد مبيعات بعد.",M([[B("رجوع للمنتجات",callback_data="admin_products:1")]])); return
    text="<b>🏆 الأكثر شراءً</b>\n\n"+"\n".join(f"{i+1}. {escape(r['title'][:30])} — <b>{r['cnt']}</b> عملية" for i,r in enumerate(rows))
    keyboard=[[B(f"{r['title'][:28]} ({r['cnt']})",callback_data=f"admin_product:{r['id']}:1")] for r in rows]
    keyboard.append([B("رجوع للمنتجات",callback_data="admin_products:1")])
    await edit_or_send(query,context,text,M(keyboard))

async def admin_users(query, context, page):
    with service(context).db.connect() as c:
        admin_ids=tuple(ADMIN_IDS)
        total=c.execute("SELECT COUNT(*) FROM users WHERE telegram_id NOT IN (?,?)",admin_ids).fetchone()[0]
        pages=max(1,math.ceil(total/10)); page=max(1,min(page,pages))
        rows=c.execute("SELECT telegram_id,username,full_name,credits FROM users WHERE telegram_id NOT IN (?,?) ORDER BY registered_at DESC LIMIT 10 OFFSET ?",(*admin_ids,(page-1)*10)).fetchall()
    keyboard=[[B(f"👤 {r['full_name'][:18]}",callback_data=f"admin_user:{r['telegram_id']}:{page}"),B(f"💳 {r['credits']}",callback_data=f"admin_user:{r['telegram_id']}:{page}")] for r in rows]
    keyboard += [[B("‹",callback_data=f"admin_users:{page-1}" if page>1 else "noop"),B(f"{page} من {pages}",callback_data="noop"),B("›",callback_data=f"admin_users:{page+1}" if page<pages else "noop")]]
    keyboard += [[B("📥 نسخة قاعدة البيانات",callback_data="admin_db_backup")],[B("✏️ رسالة الحظر",callback_data="admin_banmsg")],[B("رجوع",callback_data="admin")]]
    await query.edit_message_text("<b>المستخدمون</b>",parse_mode=ParseMode.HTML,reply_markup=M(keyboard))

async def admin_user(query,context,user_id,page):
    with service(context).db.connect() as c:
        u=c.execute("SELECT * FROM users WHERE telegram_id=?",(user_id,)).fetchone()
        refs=c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?",(user_id,)).fetchone()[0]
    if not u: await query.answer("المستخدم غير موجود",show_alert=True); return
    banned=not u['is_active']
    status_label='⛔ محظور' if banned else '✅ نشط'
    text=(f"<b>{escape(u['full_name'] or '-')}</b>\n"
          f"ID: <code>{u['telegram_id']}</code>\n"
          f"Username: @{escape(u['username']) if u['username'] else '-'}\n"
          f"Credits: {u['credits']}\nالإحالات: {refs}\nأرباح الإحالات: {u['referral_earned']}\n"
          f"الحالة: {status_label}\nآخر نشاط: {format_time(u['last_activity_at'])}\nتاريخ التسجيل: {format_time(u['registered_at'])}")
    ban_btn=B("✅ فك الحظر",callback_data=f"admin_unban:{user_id}:{page}") if banned else B("⛔ حظر المستخدم",callback_data=f"admin_ban:{user_id}:{page}")
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M([[B("➕ إضافة Credits",callback_data=f"admin_credit_add:{user_id}:{page}"),B("➖ خصم Credits",callback_data=f"admin_credit_subtract:{user_id}:{page}")],[ban_btn],[B("رجوع",callback_data=f"admin_users:{page}")]]))

def admin_product_keyboard(product, page, context=None):
    pid=product['id']
    toggle=B("⏸️ إيقاف المنتج",callback_data=f"admin_product_stop:{pid}:{page}") if product['is_active'] else B("▶️ تفعيل المنتج",callback_data=f"admin_product_activate:{pid}:{page}")
    rows=[[B("📦 معاينة المنتج",callback_data=f"admin_product_preview:{pid}:{page}"),toggle],
          [B("✏️ تعديل العنوان",callback_data=f"admin_edit_title:{pid}:{page}"),B("✏️ الوصف القصير",callback_data=f"admin_edit_short:{pid}:{page}")],
          [B("✏️ الوصف الكامل",callback_data=f"admin_edit_desc:{pid}:{page}")]]
    if product['product_type'] != 'free':
        rows.append([B("💳 تعديل السعر",callback_data=f"admin_edit_price:{pid}:{page}"),B("📦 تعديل الكمية",callback_data=f"admin_edit_qty:{pid}:{page}")])
    else:
        rows.append([B("📦 تعديل الكمية",callback_data=f"admin_edit_qty:{pid}:{page}")])
    rows.append([B("🖼️ تعديل الصور",callback_data=f"admin_edit_images:{pid}:{page}"),B("📁 تعديل الملف",callback_data=f"admin_edit_file:{pid}:{page}")])
    if context is not None:
        rows.append([B("📤 مشاركة المنتج",url=admin_share_url(context,product))])
    rows.append([B("🗑️ حذف نهائي",callback_data=f"admin_product_del:{pid}:{page}")])
    rows.append([B("رجوع للمنتجات",callback_data=f"admin_products:{page}")])
    return M(rows)

def admin_share_url(context, product):
    deep=product_deep_link(context.bot.username,product['id'])
    return build_share_url(deep,render_share_text(context,product['title'],deep))

async def admin_product(query, context, product_id, page):
    with service(context).db.connect() as c:
        product=c.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone()
    if not product:
        await query.answer("المنتج غير موجود.",show_alert=True); return
    await cleanup_admin_product_media(context, query.message.chat_id)
    try: await query.delete_message()
    except Exception: pass
    chat_id=query.message.chat_id
    image_urls=json.loads(product['image_urls'] or '[]')
    base=build_product_text(product,for_caption=bool(image_urls))
    state_line="\n⏸️ الحالة: موقوف" if not product['is_active'] else ""
    text=f"{base}{state_line}\n🆔 المعرّف: {product['id']}"
    keyboard=admin_product_keyboard(product,page,context)
    # قالب واحد: كل الصور + الوصف + أزرار الإدارة معاً
    msg_ids=[]
    if len(image_urls) == 1:
        try:
            sent=await context.bot.send_photo(chat_id,image_urls[0],caption=text,parse_mode=ParseMode.HTML,reply_markup=keyboard)
            context.user_data['admin_product_media']={'chat_id':chat_id,'message_ids':[sent.message_id]}
            return
        except Exception:
            log.warning("Unable to send admin product photo, falling back",exc_info=True)
    elif len(image_urls) > 1:
        try:
            media=[InputMediaPhoto(url,caption=text if index == 0 else None,parse_mode=ParseMode.HTML if index == 0 else None) for index,url in enumerate(image_urls[:10])]
            sent_group=await context.bot.send_media_group(chat_id,media)
            msg_ids=[message.message_id for message in sent_group]
            if await attach_buttons(chat_id,context,msg_ids[0],keyboard):
                context.user_data['admin_product_media']={'chat_id':chat_id,'message_ids':msg_ids}
                return
        except Exception:
            log.warning("Unable to send admin product media group, falling back",exc_info=True)
            msg_ids=[]
    if not msg_ids:
        sent=await context.bot.send_message(chat_id,f"<b>إدارة المنتج</b>\n\n{text}",parse_mode=ParseMode.HTML)
        msg_ids=[sent.message_id]
    manage=await context.bot.send_message(chat_id,"<b>إدارة المنتج</b> — اختر الإجراء:",parse_mode=ParseMode.HTML,reply_markup=keyboard)
    msg_ids.append(manage.message_id)
    context.user_data['admin_product_media']={'chat_id':chat_id,'message_ids':msg_ids}

async def cleanup_admin_product_media(context, chat_id):
    data=context.user_data.pop('admin_product_media',None)
    if not data or data['chat_id'] != chat_id: return
    for message_id in data['message_ids']:
        try: await context.bot.delete_message(chat_id,message_id)
        except Exception: pass

async def ask_admin_edit(query, context, text, keyboard):
    # رسالة المنتج في الأدمن قد تكون صورة (caption) — نتعامل مع الحالتين
    try:
        await query.edit_message_text(text,reply_markup=keyboard)
        return
    except Exception: pass
    try:
        await query.edit_message_caption(caption=text,reply_markup=keyboard)
        return
    except Exception: pass
    try: await query.delete_message()
    except Exception: pass
    await context.bot.send_message(query.message.chat_id,text,reply_markup=keyboard)

async def admin_product_preview(query, context, product_id, page):
    with service(context).db.connect() as c:
        product=c.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone()
    if not product or not product['storage_path']:
        await query.answer("ملف المنتج غير جاهز للمعاينة.",show_alert=True); return
    status_msg=await context.bot.send_message(query.message.chat_id,"⏳ جارٍ تجهيز معاينة المنتج…")
    status_id=status_msg.message_id
    try:
        last_error=None
        data=None
        for attempt in range(5):
            try:
                with service(context).db.connect() as c:
                    fresh=c.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone() or product
                data=await context.application.bot_data['storage'].download_product(fresh)
                product=fresh; last_error=None; break
            except Exception as exc:
                last_error=exc
                log.warning("Preview attempt %d for product %s failed: %s",attempt+1,product_id,exc)
                if attempt < 4: await asyncio.sleep(2 ** attempt)
        if last_error is not None: raise last_error
        storage=context.application.bot_data['storage']
        await deliver_product_file(query.message.chat_id,context,storage,product,data)
        try: await context.bot.delete_message(query.message.chat_id,status_id)
        except Exception: pass
        await admin_product(query,context,product_id,page)
    except Exception:
        log.exception("Admin product preview failed")
        try: await context.bot.delete_message(query.message.chat_id,status_id)
        except Exception: pass
        await context.bot.send_message(query.message.chat_id,"تعذر إرسال معاينة المنتج. تحقق من Release Asset وإعدادات GitHub ثم أعد المحاولة.",reply_markup=M([[B("🔄 إعادة المحاولة",callback_data=f"admin_product_preview:{product_id}:{page}")],[B("رجوع للمنتج",callback_data=f"admin_product:{product_id}:{page}")]]))

async def admin_statistics(query,context):
    with service(context).db.connect() as c:
        admins=tuple(ADMIN_IDS)
        users=c.execute("SELECT COUNT(*) FROM users").fetchone()[0]; products=c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        purchases=c.execute("SELECT COUNT(*) FROM purchases WHERE status='Completed'").fetchone()[0]; credits=c.execute("SELECT COALESCE(SUM(credits),0) FROM users WHERE telegram_id NOT IN (?,?)",admins).fetchone()[0]
    await query.edit_message_text(f"<b>الإحصاءات</b>\nالمستخدمون: {users}\nالمنتجات النشطة: {products}\nالمشتريات المكتملة: {purchases}\nإجمالي Credits لدى المستخدمين: {credits}",parse_mode=ParseMode.HTML,reply_markup=admin_back())

async def admin_storage(query,context):
    storage=context.application.bot_data['storage']
    connected,detail=await storage.connection_status()
    try: added=await storage.sync_repositories()
    except Exception: added=0; log.warning("Storage sync failed",exc_info=True)
    with service(context).db.connect() as c: rows=c.execute("SELECT repo_name,used_bytes,reserved_bytes,safe_bytes,is_active FROM storage_repositories ORDER BY id").fetchall()
    def mb(value): return f"{value / 1024 / 1024:.1f} MB"
    status=(f"🟢 <b>متصل بالتخزين</b> — {escape(detail)}" if connected else f"🔴 <b>غير متصل بالتخزين</b> — {escape(detail)}")
    text=("<b>🗄️ إدارة التخزين</b>\n\n"+status+"\n"+
          ("\n\n".join(f"<b>🔒 {escape(r['repo_name'])}</b>\nالمستخدم: {mb(r['used_bytes'])} من {mb(r['safe_bytes'])}\nالحالة: {'نشط' if r['is_active'] else 'متوقف'}" for r in rows) if rows else "<b>لا توجد مستودعات تخزين بعد.</b>"))
    if added: text+="\n\n🔄 تم استيراد %d مستودع من GitHub." % added
    keyboard=[[B(f"🔗 {r['repo_name']}",url=f"https://github.com/{settings(context).github_owner}/{r['repo_name']}")] for r in rows]
    keyboard += [[B("🔄 مزامنة المستودعات من GitHub",callback_data="admin_storage_sync")],[B("🗑️ حذف كل بيانات التخزين",callback_data="admin_storage_delete_confirm")],[B("رجوع للوحة الإدارة",callback_data="admin")]]
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))

async def admin_notifications(query,context):
    with service(context).db.connect() as c:
        general=c.execute("SELECT COUNT(*) FROM notifications WHERE audience='all'").fetchone()[0]
        direct=c.execute("SELECT COUNT(*) FROM notifications WHERE audience='direct'").fetchone()[0]
        pending=c.execute("SELECT COUNT(*) FROM notification_deliveries WHERE status='Pending'").fetchone()[0]
    text=(f"<b>📣 الإشعارات</b>\n\n"
          f"📢 الإشعارات العامة: {general}\n"
          f"👤 الإشعارات الخاصة: {direct}\n"
          f"📨 في الانتظار: {pending}")
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M([
        [B("📢 الإشعار العام",callback_data="admin_notif_general")],
        [B("👤 الإشعار الخاص",callback_data="admin_notif_direct:1")],
        [B("رجوع",callback_data="admin")],
    ]))

async def admin_notif_general(query,context):
    with service(context).db.connect() as c:
        count=c.execute("SELECT COUNT(*) FROM notifications WHERE audience='all'").fetchone()[0]
        last=c.execute("SELECT created_at FROM notifications WHERE audience='all' ORDER BY id DESC LIMIT 1").fetchone()
    text=f"<b>📢 الإشعار العام</b>\n\nتم إرسال إشعار عام <b>{count}</b> مرة."
    if last: text+=f"\n\nآخر إشعار: {format_time(last['created_at'])}"
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M([[B("➕ إرسال إشعار عام جديد",callback_data="admin_broadcast")],[B("رجوع",callback_data="admin_notifications")]]))

async def admin_notif_direct(query,context,page):
    with service(context).db.connect() as c:
        admins=tuple(ADMIN_IDS)
        total=c.execute("SELECT COUNT(DISTINCT u.telegram_id) FROM notification_deliveries nd JOIN notifications n ON n.id=nd.notification_id AND n.audience='direct' JOIN users u ON u.telegram_id=nd.user_id").fetchone()[0]
        pages=max(1,math.ceil(total/10)); page=max(1,min(page,pages))
        rows=c.execute("SELECT u.telegram_id,u.username,u.full_name,COUNT(*) AS cnt FROM notification_deliveries nd JOIN notifications n ON n.id=nd.notification_id AND n.audience='direct' JOIN users u ON u.telegram_id=nd.user_id GROUP BY u.telegram_id ORDER BY cnt DESC,u.telegram_id LIMIT 10 OFFSET ?",((page-1)*10,)).fetchall()
    text="<b>👤 الإشعار الخاص</b>\n\nالمستخدمون الذين أرسلت لهم إشعارات خاصة:\n\n" if rows else "<b>👤 الإشعار الخاص</b>\n\nلا توجد إشعارات خاصة بعد."
    if rows: text+= '\n'.join(f"👤 @{r['username'] or r['telegram_id']} — <b>{r['cnt']}</b> إشعار" for r in rows)
    keyboard=[[B(f"👤 {r['full_name'][:20]}",callback_data=f"admin_notif_pick:{r['telegram_id']}:{page}")] for r in rows]
    keyboard.append([B("‹",callback_data=f"admin_notif_direct:{page-1}" if page>1 else "noop"),B(f"{page} من {pages}",callback_data="noop"),B("›",callback_data=f"admin_notif_direct:{page+1}" if page<pages else "noop")])
    keyboard += [[B("✍️ إرسال لمستخدم (يدوي)",callback_data="admin_direct")],[B("رجوع",callback_data="admin_notifications")]]
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))

async def admin_required_sub(query, context):
    items=subscriptions(context)
    if items:
        text="<b>🔐 الاشتراكات</b>\n\n"+'\n\n'.join(f"<b>{'إجباري' if item['is_required'] else 'اختياري'} — {escape(item['title'])}</b>{' 🖼️' if item['image_url'] else ''}\n{escape(item['chat_id'])}" for item in items)
        buttons=[[B(f"🗑️ حذف: {item['title'][:25]}",callback_data=f"admin_sub_delete:{item['id']}")] for item in items]
    else:
        text="<b>🔐 الاشتراكات</b>\n\nلا توجد قنوات مضافة حالياً."
        buttons=[]
    buttons += [[B("➕ إضافة اشتراك إجباري",callback_data="admin_sub_type:1"),B("➕ إضافة اشتراك اختياري",callback_data="admin_sub_type:0")],[B("رجوع",callback_data="admin")]]
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M(buttons))

async def handle_custom_button(update, context, button_id, pre_answered=False):
    query=update.callback_query
    with service(context).db.connect() as c:
        btn=c.execute("SELECT * FROM custom_buttons WHERE id=?",(button_id,)).fetchone()
    if not btn or not btn['is_active']:
        if pre_answered: await context.bot.send_message(query.message.chat_id,"هذا الزر غير متاح حالياً.")
        else: await query.answer("هذا الزر غير متاح حالياً.",show_alert=True)
        return
    kind=btn['kind']
    if kind == 'internal':
        if not pre_answered:
            try: await query.answer()
            except Exception: pass
        target=(btn['target'] or 'home').strip()
        if target == 'products': await show_products(query,context,1,shuffle=True)
        elif target == 'account': await account(query,context)
        elif target == 'topup': await topup(query,context)
        elif target == 'referrals': await referrals(query,context)
        elif target == 'history': await history(query,context)
        else: await show_home(update,context,True)
        return
    body=btn['body'] or ''
    if not pre_answered and len(body) <= 200:
        await query.answer(body,show_alert=True)
    else:
        if not pre_answered:
            try: await query.answer()
            except Exception: pass
        await context.bot.send_message(query.message.chat_id,escape(body) if body else "—")

async def admin_support(query, context):
    with service(context).db.connect() as c:
        rows=c.execute("SELECT * FROM support_contacts ORDER BY id").fetchall()
    if rows:
        text="<b>🎧 خدمة العملاء</b>\n\n"+'\n'.join(f"{'✅' if r['is_active'] else '⏸️'} <b>{escape(r['label'])}</b>\n<code>{escape(r['target'])}</code>" for r in rows)
        keyboard=[[B(f"{'🗑️' if r['is_active'] else '▶️'} {r['label'][:25]}",callback_data=f"admin_support_del:{r['id']}"),B("⏸️" if r['is_active'] else "▶️",callback_data=f"admin_support_toggle:{r['id']}")] for r in rows]
    else:
        legacy=service(context).setting('support_target',settings(context).support_url) or 'غير محددة'
        text=f"<b>🎧 خدمة العملاء</b>\n\nلا توجد جهات مضافة. (القديمة: <code>{escape(legacy)}</code>)"
        keyboard=[]
    keyboard.append([B("➕ إضافة خدمة عملاء",callback_data="admin_support_add")])
    keyboard.append([B("رجوع",callback_data="admin")])
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))

async def admin_custom(query, context):
    with service(context).db.connect() as c:
        rows=c.execute("SELECT * FROM custom_buttons ORDER BY id").fetchall()
    kind_label={'url':'🔗 رابط','internal':'↩️ داخلي','dialog':'💬 رسالة'}
    if rows:
        text="<b>🔘 الأزرار المخصصة</b>\n\nتظهر كزر عريض تحت (حسابي / شحن Credits) في القائمة الرئيسية.\n\n"+'\n'.join(f"{'✅' if r['is_active'] else '⏸️'} <b>{escape(r['label'])}</b> — {kind_label.get(r['kind'],r['kind'])}" for r in rows)
        keyboard=[[B(f"🔘 {r['label'][:22]}",callback_data=f"admin_custom_item:{r['id']}")] for r in rows]
    else:
        text="<b>🔘 الأزرار المخصصة</b>\n\nلا توجد أزرار بعد. أضف زراً وسمِّه كما تشاء."
        keyboard=[]
    keyboard.append([B("➕ إضافة زر",callback_data="admin_custom_add")])
    keyboard.append([B("رجوع",callback_data="admin")])
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))

async def admin_custom_item(query, context, button_id):
    with service(context).db.connect() as c:
        btn=c.execute("SELECT * FROM custom_buttons WHERE id=?",(button_id,)).fetchone()
    if not btn: await query.answer("الزر غير موجود.",show_alert=True); return
    kind_label={'url':'🔗 رابط خارجي','internal':'↩️ وجهة داخلية','dialog':'💬 رسالة منبثقة'}
    detail=btn['target'] if btn['kind'] in ('url','internal') else (btn['body'][:100] if btn['body'] else '-')
    text=(f"<b>🔘 {escape(btn['label'])}</b>\n\nالنوع: {kind_label.get(btn['kind'],btn['kind'])}\n"
          f"التفاصيل: {escape(detail)}\nالحالة: {'✅ مفعّل' if btn['is_active'] else '⏸️ موقوف'}")
    toggle_label="⏸️ إيقاف" if btn['is_active'] else "▶️ تفعيل"
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M([
        [B(toggle_label,callback_data=f"admin_custom_toggle:{button_id}"),B("🗑️ حذف",callback_data=f"admin_custom_del:{button_id}")],
        [B("رجوع",callback_data="admin_custom")]]))

async def admin_referral_cfg(query, context):
    giver,joiner=service(context).referral_rewards()
    text=(f"<b>🎁 مكافآت الإحالة</b>\n\n"
          f"👤 كل شخص يعطي إحالته ويُسجل بها عضو جديد يأخذ: <b>{giver} Credits</b>\n"
          f"🎉 كل شخص يدخل عبر رابط إحالة يأخذ: <b>{joiner} Credits</b>\n\n"
          f"<i>تُصرف المكافآت بعد اشتراك العضو الجديد في القنوات الإجبارية (إن وُجدت).</i>")
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M([
        [B("✏️ مكافأة المُحيل",callback_data="admin_referral_giver"),B("✏️ مكافأة المنضم",callback_data="admin_referral_joiner")],
        [B("رجوع",callback_data="admin")]]))

async def admin_dex(query, context, page=1):
    with service(context).db.connect() as c:
        total=c.execute("SELECT COUNT(*) FROM services").fetchone()[0]
        pages=max(1,math.ceil(total/10)); page=max(1,min(page,pages))
        rows=c.execute("SELECT * FROM services ORDER BY id DESC LIMIT 10 OFFSET ?",((page-1)*10,)).fetchall()
        orders=c.execute("SELECT COUNT(*) FROM service_orders WHERE status='Done'").fetchone()[0]
    text=f"<b>الخدمات</b> — عمليات مكتملة: {orders}\nاختر خدمة لتعديل اسمها ووصفها ورصيدها:"
    keyboard=[[B(f"{'✅' if r['is_active'] else '⏸️'} {r['title'][:26]} ({r['credits']})",callback_data=f"admin_dex_item:{r['id']}:{page}")] for r in rows]
    if rows:
        keyboard.append([B("‹",callback_data=f"admin_dex:{page-1}" if page>1 else "noop"),B(f"{page} من {pages}",callback_data="noop"),B("›",callback_data=f"admin_dex:{page+1}" if page<pages else "noop")])
    keyboard.append([B("رجوع للوحة الإدارة",callback_data="admin")])
    try:
        await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))
    except Exception:
        try: await query.delete_message()
        except Exception: pass
        await context.bot.send_message(query.message.chat_id,text,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))

async def admin_dex_item(query, context, service_id, page):
    with service(context).db.connect() as c:
        s=c.execute("SELECT * FROM services WHERE id=?",(service_id,)).fetchone()
        done=c.execute("SELECT COUNT(*) FROM service_orders WHERE service_id=? AND status='Done'",(service_id,)).fetchone()[0]
    if not s: await query.answer("الخدمة غير موجودة.",show_alert=True); return
    desc=(s['description'] or '').strip()
    text=(f"<b>{escape(s['title'])}</b>\n\n"
          + (f"{escape(desc)}\n\n" if desc else "") +
          f"💳 الكريديتس المطلوبة: <b>{s['credits']}</b>\n"
          f"الحالة: {'✅ نشطة' if s['is_active'] else '⏸️ موقوفة'}\n✅ عمليات مكتملة: {done}")
    toggle="⏸️ إلغاء التفعيل" if s['is_active'] else "▶️ تفعيل"
    kb=M([[B("✏️ تعديل الاسم",callback_data=f"admin_dex_title:{service_id}:{page}"),B("💳 تعديل الكريديتس",callback_data=f"admin_dex_credits:{service_id}:{page}")],
          [B("📝 تعديل الوصف",callback_data=f"admin_dex_desc:{service_id}:{page}")],
          [B(toggle,callback_data=f"admin_dex_toggle:{service_id}:{page}")],
          [B("رجوع للخدمات",callback_data=f"admin_dex:{page}")]])
    try:
        await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=kb)
    except Exception:
        try: await query.delete_message()
        except Exception: pass
        await context.bot.send_message(query.message.chat_id,text,parse_mode=ParseMode.HTML,reply_markup=kb)

async def admin_gifts(query, context, page=1):
    with service(context).db.connect() as c:
        total=c.execute("SELECT COUNT(*) FROM redeem_links").fetchone()[0]
        pages=max(1,math.ceil(total/10)); page=max(1,min(page,pages))
        rows=c.execute("SELECT * FROM redeem_links ORDER BY id DESC LIMIT 10 OFFSET ?",((page-1)*10,)).fetchall()
    if not rows:
        await query.edit_message_text("<b>🎁 روابط الهدايا</b>\n\nلا توجد روابط بعد. أنشئ رابطاً يمنح Credits لمن يدخل منه.",parse_mode=ParseMode.HTML,reply_markup=M([[B("➕ إنشاء رابط هدية",callback_data="admin_gift_add")],[B("رجوع",callback_data="admin")]])); return
    text="<b>🎁 روابط الهدايا</b>\n\n"+'\n\n'.join(f"{'✅' if r['is_active'] else '⏸️'} <code>{r['code']}</code> — <b>{r['credits']}</b> Credits — {r['used_count']}/{r['max_uses']}" for r in rows)
    keyboard=[[B(f"🎁 {r['code']} ({r['used_count']}/{r['max_uses']})",callback_data=f"admin_gift:{r['id']}:{page}")] for r in rows]
    keyboard.append([B("‹",callback_data=f"admin_gifts:{page-1}" if page>1 else "noop"),B(f"{page} من {pages}",callback_data="noop"),B("›",callback_data=f"admin_gifts:{page+1}" if page<pages else "noop")])
    keyboard.append([B("➕ إنشاء رابط هدية",callback_data="admin_gift_add")])
    keyboard.append([B("رجوع",callback_data="admin")])
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))

async def admin_gift_detail(query, context, link_id, page):
    with service(context).db.connect() as c:
        link=c.execute("SELECT * FROM redeem_links WHERE id=?",(link_id,)).fetchone()
    if not link: await query.answer("الرابط غير موجود.",show_alert=True); return
    deep=gift_deep_link(context.bot.username,link['code'])
    share_url=f"https://t.me/share/url?url={quote(deep)}&text={quote('هدية '+str(link['credits'])+' Credits! اضغط الرابط لاستلامها')}"
    text=(f"<b>هدية <code>{link['code']}</code></b>\n\n"
          f"💳 الكريديتس: <b>{link['credits']}</b>\n👥 الاستخدام: <b>{link['used_count']}/{link['max_uses']}</b>\n"
          f"الحالة: {'✅ مفعّل' if link['is_active'] else '⏸️ موقوف'}\n\n🔗 الرابط:\n{deep}")
    toggle_label="⏸️ إيقاف" if link['is_active'] else "▶️ تفعيل"
    await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M([
        [B("📤 مشاركة الرابط",url=share_url)],
        [B(toggle_label,callback_data=f"admin_gift_toggle:{link_id}:{page}"),B("🗑️ حذف",callback_data=f"admin_gift_del:{link_id}:{page}")],
        [B("رجوع",callback_data=f"admin_gifts:{page}")]]))

def forwarded_channel(update):
    """يستخرج القناة من رسالة مُحوّلة منها (تدعم القنوات الخاصة بدون يوزر)."""
    msg=update.message
    if not msg: return None
    origin=getattr(msg,'forward_origin',None)
    if origin and getattr(origin,'type',None) == 'channel' and getattr(origin,'chat',None):
        return origin.chat
    legacy=getattr(msg,'forward_from_chat',None)
    if legacy is not None and getattr(legacy,'type',None) == 'channel':
        return legacy
    return None

async def verify_bot_admin(context, chat_id):
    """يفحص أن البوت مدير ويستطيع قراءة الأعضاء. يرجع (ناجح, سبب_الفشل)."""
    try:
        me=await context.bot.get_me()
        bot_member=await context.bot.get_chat_member(api_chat_id(chat_id),me.id)
        if bot_member.status in {'administrator','creator'}: return True, ''
        if bot_member.status in {'left','kicked'}:
            return False, "البوت ليس عضواً في القناة — أضفه أولاً ثم أعد المحاولة."
        return False, "البوت عضو عادي وليس مديراً — ارفعه لمدير (مع صلاحية قراءة الأعضاء) ثم أعد المحاولة."
    except Exception as exc:
        msg=str(exc)
        log.warning("Bot admin verify failed for %s: %s",chat_id,msg)
        if "chat not found" in msg.lower() or "peer_id_invalid" in msg.lower():
            return False, "لم أجد القناة بهذا المعرف — تأكد من الـID الرقمي (يبدأ بـ -100) وأن البوت داخل القناة."
        if "not a member" in msg.lower() or "bot was kicked" in msg.lower() or "forbidden" in msg.lower():
            return False, "البوت ليس داخل القناة — أضفه عضواً ثم ارفعه مديراً."
        return False, f"تعذر الفحص ({msg[:120]}). تأكد أن البوت مدير في القناة."

SUB_URL_PROMPT=("أرسل أحد التالي:\n"
    "• رابط القناة العامة أو @username\n"
    "• الـ <b>ID الرقمي</b> للقناة (مثل <code>4451031016</code> — انسخه كما هو)\n"
    "• أو حوّل أي رسالة من القناة إلى هنا مباشرة\n"
    "يجب أن يكون البوت مديراً في القناة.")

def save_subscription(context, setup, image_url, admin_id):
    with service(context).db.transaction() as c:
        try: c.execute("INSERT INTO subscriptions(title,description,chat_id,url,is_required,image_url) VALUES(?,?,?,?,?,?)",(setup['title'],setup['description'],setup['chat_id'],setup['url'],setup['is_required'],image_url or ''))
        except Exception:
            c.execute("INSERT INTO subscriptions(title,description,chat_id,url,is_required) VALUES(?,?,?,?,?)",(setup['title'],setup['description'],setup['chat_id'],setup['url'],setup['is_required']))

def save_edit_images(context, pid):
    edit=context.user_data.get('edit_product')
    working=edit.get('images',[]) if edit else []
    with service(context).db.transaction() as c:
        c.execute("UPDATE products SET image_urls=? WHERE id=?",(json.dumps(working),pid))

async def cleanup_edit_previews(context, chat_id):
    edit=context.user_data.get('edit_product')
    if not edit: return
    for message_id in edit.pop('preview_ids',[]):
        try: await context.bot.delete_message(chat_id,message_id)
        except Exception: pass

async def render_image_manager(query, context, pid, page):
    edit=context.user_data.get('edit_product')
    working=edit.get('images',[]) if edit else []
    if working:
        text="<b>🖼️ صور المنتج الحالية</b>\n\nاضغط على أي صورة لاستبدالها أو حذفها:"
    else:
        text="<b>🖼️ صور المنتج</b>\n\nلا توجد صور حالياً. أضف صوراً جديدة أو اضغط تم."
    rows=[[B(f"🖼️ صورة {i+1}",callback_data=f"admin_edit_img:{pid}:{i}:{page}")] for i in range(len(working))]
    rows.append([B("➕ إضافة صورة جديدة",callback_data=f"admin_edit_img_add:{pid}:{page}")])
    rows.append([B("✅ تم",callback_data=f"admin_edit_images_done:{pid}:{page}"),B("إلغاء",callback_data=f"admin_edit_images_cancel:{pid}:{page}")])
    await ask_admin_edit(query,context,text,M(rows))

async def receive_product_photo(update, context):
    sub_setup=context.user_data.get('admin_subscription_setup')
    if sub_setup and sub_setup.get('stage') == 'photo':
        try:
            photo=update.message.photo[-1]
            image_file=await context.bot.get_file(photo.file_id)
            url=await context.application.bot_data['images'].upload(bytes(await image_file.download_as_bytearray()))
            save_subscription(context,sub_setup,url,update.effective_user.id)
            context.user_data.pop('admin_subscription_setup',None)
            await update.message.reply_text(f"✅ تم إضافة الاشتراك {'الإجباري' if sub_setup['is_required'] else 'الاختياري'} مع صورة.",reply_markup=admin_back())
        except Exception:
            log.exception("Subscription image upload failed")
            await update.message.reply_text("تعذر رفع الصورة. تحقق من IMGBB_API_KEY ثم أعد إرسالها أو اضغط تخطي.",reply_markup=M([[B("تخطي ⏩",callback_data="admin_sub_skip_photo")]]))
        return
    edit=context.user_data.get('edit_product')
    if edit and edit.get('field') == 'images':
        try:
            photo=update.message.photo[-1]
            image_file=await context.bot.get_file(photo.file_id)
            url=await context.application.bot_data['images'].upload(bytes(await image_file.download_as_bytearray()))
            working=edit.get('images',[])
            if edit.get('replace_idx') is not None:
                idx=edit.pop('replace_idx')
                if 0 <= idx < len(working): working[idx]=url
                save_edit_images(context,edit['product_id'])
                await update.message.reply_text(f"✅ تم استبدال صورة {idx+1}.",reply_markup=M([[B("رجوع لصور المنتج",callback_data=f"admin_edit_images_back:{edit['product_id']}:{edit.get('page',1)}")]]))
            else:
                working.append(url)
                save_edit_images(context,edit['product_id'])
                await update.message.reply_text(f"✅ تمت إضافة الصورة (الإجمالي: {len(working)}). أرسل المزيد أو اضغط تم.",reply_markup=M([[B("✅ تم",callback_data=f"admin_edit_images_done:{edit['product_id']}:{edit.get('page',1)}")]]))
        except Exception:
            log.exception("Image upload failed")
            await update.message.reply_text("تعذر رفع الصورة. تحقق من IMGBB_API_KEY ثم أعد إرسالها.")
        return
    addition=context.user_data.get('add_product')
    if not addition or addition.get('stage') != 'images': return
    try:
        photo=update.message.photo[-1]
        image_file=await context.bot.get_file(photo.file_id)
        url=await context.application.bot_data['images'].upload(bytes(await image_file.download_as_bytearray()))
        addition['images'].append(url)
        await update.message.reply_text(f"تم رفع الصورة رقم {len(addition['images'])}. أرسل صورة أخرى أو اضغط «انتهيت من إرسال الصور».",reply_markup=M([[B("انتهيت من إرسال الصور",callback_data="admin_add_images_done")]]))
    except Exception:
        log.exception("Image upload failed")
        await update.message.reply_text("تعذر رفع الصورة. تحقق من IMGBB_API_KEY ثم أعد إرسالها.")

async def receive_product_file(update, context):
    if await handle_dex_document(update,context): return
    edit=context.user_data.get('edit_product')
    if edit and edit.get('field') == 'file':
        edit['step']='awaiting'; edit['expect_link']=False
        document=update.message.document
        if not document: return
        pid=edit['product_id']; page=edit.get('page',1)
        try:
            raw=await download_document_bytes(context,document)
            filename, payload=pack_upload(document.file_name or 'product_file',raw)
            await update.message.reply_text(f"جارٍ رفع الملف ({fmt_mb(len(payload))}) إلى التخزين…")
            await context.application.bot_data['storage'].upload_product(pid,payload,filename=filename)
            with service(context).db.transaction() as c:
                c.execute("UPDATE products SET link_message='' WHERE id=?",(pid,))
            context.user_data.pop('edit_product',None)
            await update.message.reply_text(f"✅ تم تحديث ملف المنتج #{pid} بنجاح.",reply_markup=M([[B("رجوع للمنتج",callback_data=f"admin_product:{pid}:{page}")]]))
        except Exception as exc:
            log.exception("Product file edit upload failed")
            await update.message.reply_text(storage_error_text(exc),parse_mode=ParseMode.HTML)
        return
    addition=context.user_data.get('add_product')
    if not addition or addition.get('stage') not in ('file','file_choice'): return
    addition['stage']='file'; addition['expect_link']=False
    document=update.message.document
    if not document: return
    try:
        raw=await download_document_bytes(context,document)
        filename, payload=pack_upload(document.file_name or 'product_file',raw)
        await update.message.reply_text(f"جارٍ رفع ملف المنتج ({fmt_mb(len(payload))}) إلى التخزين…")
        product_id=await create_product_with_file(context,addition,filename,payload,update.effective_user.id)
        context.user_data.pop('add_product',None)
        await update.message.reply_text(f"تمت إضافة المنتج #{product_id} ورفع ملفه بنجاح.",reply_markup=M([[B("إدارة المنتجات",callback_data="admin_products:1"),B("لوحة الإدارة",callback_data="admin")]]))
    except Exception as exc:
        log.exception("Product file upload failed")
        await update.message.reply_text(storage_error_text(exc),parse_mode=ParseMode.HTML)

async def deliver_notification(application, notification_id):
    db=application.bot_data['service'].db
    with db.connect() as c:
        deliveries=c.execute("SELECT id,user_id FROM notification_deliveries WHERE notification_id=? AND status='Pending'",(notification_id,)).fetchall()
        notification=c.execute("SELECT body,audience FROM notifications WHERE id=?",(notification_id,)).fetchone()
    if not notification: return
    body,audience=notification['body'],notification['audience']
    for delivery in deliveries:
        error=None
        for attempt in range(3):
            try:
                sent=await application.bot.send_message(delivery['user_id'],f"<b>{escape(body)}</b>",parse_mode=ParseMode.HTML)
                if audience == 'all':
                    try: await application.bot.pin_chat_message(delivery['user_id'],sent.message_id,disable_notification=True)
                    except Exception: log.warning('Could not pin general notification for user %s',delivery['user_id'])
                with db.transaction() as c:c.execute("UPDATE notification_deliveries SET status='Sent',attempts=attempts+1,sent_at=CURRENT_TIMESTAMP WHERE id=?",(delivery['id'],))
                error=None; break
            except Exception as exc:
                error=exc
                if attempt < 2: await asyncio.sleep(2 ** attempt)
        if error:
            with db.transaction() as c:c.execute("UPDATE notification_deliveries SET status='Failed',attempts=attempts+3,error=? WHERE id=?",(str(error)[:500],delivery['id']))
        await asyncio.sleep(0.05)
    with db.transaction() as c:c.execute("UPDATE notifications SET status='Sent' WHERE id=?",(notification_id,))

DEX2C_MAX_APK=20*1024*1024
DEX2C_MAX_FILTER=10000

def dex_config():
    token=os.getenv("DEX2C_GITHUB_TOKEN","")
    owner=os.getenv("DEX2C_GITHUB_OWNER","")
    repo=os.getenv("DEX2C_GITHUB_REPO","")
    if not token or not owner or not repo: return None
    return {"token":token,"owner":owner,"repo":repo,
        "branch":os.getenv("DEX2C_GITHUB_BRANCH","main"),
        "workflow":os.getenv("DEX2C_WORKFLOW",".github/workflows/telegram-build.yml"),
        "apps":os.getenv("DEX2C_APPS_FOLDER","telegram_apps")}

def dex_headers(cfg):
    return {"Authorization":f"Bearer {cfg['token']}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}

def dex_api(cfg):
    return f"https://api.github.com/repos/{cfg['owner']}/{cfg['repo']}"

DEX2C_WORKFLOW_YML='''name: Telegram Dex2C Build

run-name: Telegram Dex2C - ${{ inputs.app_name }}

on:
  workflow_dispatch:
    inputs:
      app_path:
        description: "Path of APK"
        required: true
        type: string
      filter_path:
        description: "Path of filter"
        required: true
        type: string
      app_name:
        description: "Application name"
        required: true
        type: string

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      - name: Set up Java 17
        uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: "17"
      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Set up Android SDK
        uses: android-actions/setup-android@v3
      - name: Install Android SDK tools
        run: |
          sdkmanager "platform-tools" "build-tools;35.0.0" "ndk;29.0.14206865"
      - name: Verify NDK
        run: |
          NDK_DIR="$ANDROID_HOME/ndk/29.0.14206865"
          if [ ! -f "$NDK_DIR/ndk-build" ]; then
            exit 1
          fi
      - name: Prepare apksigner
        run: |
          mkdir -p tools
          APKSIGNER=$(find "$ANDROID_HOME/build-tools" -path "*/lib/apksigner.jar" | sort -V | tail -n 1)
          if [ -z "$APKSIGNER" ]; then
            exit 1
          fi
          cp "$APKSIGNER" tools/apksigner.jar
      - name: Prepare Telegram input
        env:
          APP_PATH: ${{ inputs.app_path }}
          FILTER_PATH: ${{ inputs.filter_path }}
        run: |
          if [ ! -f "$APP_PATH" ] || [ ! -f "$FILTER_PATH" ]; then
            exit 1
          fi
          mkdir -p input
          cp "$APP_PATH" input/app.apk
          cp "$FILTER_PATH" filter.txt
      - name: Configure dxx.cfg
        run: |
          python3 - <<'PY'
          import json, os
          path = "dxx.cfg"
          ndk_dir = os.path.join(os.environ["ANDROID_HOME"], "ndk", "29.0.14206865")
          with open(path, "r", encoding="utf-8") as f:
              data = json.load(f)
          data["ndk_dir"] = ndk_dir
          with open(path, "w", encoding="utf-8") as f:
              json.dump(data, f, indent=4)
          PY
      - name: Build Dex2C
        run: |
          chmod +x java/build.sh
          sh java/build.sh
          if [ ! -f dex2cxx.jar ]; then
            exit 1
          fi
      - name: Run Dex2C
        run: |
          mkdir -p output
          java -jar dex2cxx.jar -a input/app.apk -o output/app_protected.apk --filter filter.txt
          if [ ! -f output/app_protected.apk ]; then
            exit 1
          fi
      - name: Create Protection README
        run: |
          cat > output/README.md <<'EOF'
          # Dex2C Protected Application
          This application has been protected using Dex2C.
          EOF
      - name: Add README inside APK
        run: |
          rm -rf apk_tmp && mkdir -p apk_tmp
          unzip -q output/app_protected.apk -d apk_tmp
          mkdir -p apk_tmp/assets
          cp output/README.md apk_tmp/assets/README.md
          cd apk_tmp && zip -qr ../app_protected_with_readme.apk . && cd ..
          mv app_protected_with_readme.apk output/app_protected.apk
      - name: Zipalign APK
        run: |
          ZIPALIGN="$ANDROID_HOME/build-tools/35.0.0/zipalign"
          "$ZIPALIGN" -f -p 4 output/app_protected.apk output/app_protected_aligned.apk
          mv output/app_protected_aligned.apk output/app_protected.apk
      - name: Generate signing keystore
        run: |
          keytool -genkeypair -v -keystore tools/0dex-release.keystore -alias 0dex -keyalg RSA -keysize 2048 -validity 10000 -storepass 0dex123456 -keypass 0dex123456 -dname "CN=0Dex, OU=0Dex, O=0Dex, L=Unknown, ST=Unknown, C=US"
      - name: Sign APK
        run: |
          java -jar tools/apksigner.jar sign --ks tools/0dex-release.keystore --ks-key-alias 0dex --ks-pass pass:0dex123456 --key-pass pass:0dex123456 --out output/app_protected_signed.apk output/app_protected.apk
          mv output/app_protected_signed.apk output/app_protected.apk
      - name: Upload APK
        uses: actions/upload-artifact@v4
        with:
          name: dex2c_app
          path: |
            output/app_protected.apk
            output/README.md
          if-no-files-found: error
          retention-days: 1
'''

async def dex_gh(client, method, url, **kwargs):
    r=await client.request(method,url,**kwargs)
    return r

async def dex_ensure_workflow(client, cfg):
    api=dex_api(cfg)
    r=await dex_gh(client,"GET",f"{api}/contents/{cfg['workflow']}",params={"ref":cfg['branch']})
    if r.status_code == 200: return False
    if r.status_code != 404: raise RuntimeError(f"Workflow check error: {r.status_code}")
    encoded=base64.b64encode(DEX2C_WORKFLOW_YML.encode()).decode()
    r=await dex_gh(client,"PUT",f"{api}/contents/{cfg['workflow']}",json={"message":"System update","content":encoded,"branch":cfg['branch']})
    if r.status_code not in (200,201): raise RuntimeError(f"Workflow creation failed: {r.status_code}")
    return True

async def dex_next_app_number(client, cfg):
    api=dex_api(cfg)
    r=await dex_gh(client,"GET",f"{api}/contents/{cfg['apps']}",params={"ref":cfg['branch']})
    if r.status_code == 404: return 1
    if r.status_code != 200: raise RuntimeError(f"App fetch failed: {r.status_code}")
    data=r.json(); items=data if isinstance(data,list) else []
    numbers=[int(it["name"][3:]) for it in items if it.get("type")=="dir" and it.get("name","").startswith("app") and it["name"][3:].isdigit()]
    return max(numbers)+1 if numbers else 1

async def dex_upload_file(client, cfg, path, data, message):
    api=dex_api(cfg)
    url=f"{api}/contents/{path}"
    check=await dex_gh(client,"GET",url,params={"ref":cfg['branch']})
    payload={"message":message,"content":base64.b64encode(data).decode(),"branch":cfg['branch']}
    if check.status_code == 200: payload["sha"]=check.json()["sha"]
    r=await dex_gh(client,"PUT",url,json=payload)
    if r.status_code not in (200,201): raise RuntimeError(f"Upload failed: {r.status_code}")
    return r.json()

async def dex_dispatch(client, cfg, app_path, filter_path, app_name):
    api=dex_api(cfg)
    wf=cfg['workflow'].split("/")[-1]
    r=await dex_gh(client,"POST",f"{api}/actions/workflows/{wf}/dispatches",json={"ref":cfg['branch'],"inputs":{"app_path":app_path,"filter_path":filter_path,"app_name":app_name}})
    if r.status_code != 204: raise RuntimeError(f"Dispatch failed: {r.status_code}")

async def dex_wait_for_run(client, cfg, started_ts, app_name):
    import time as _t
    api=dex_api(cfg)
    wf=cfg['workflow'].split("/")[-1]
    start_iso=datetime.fromtimestamp(started_ts,timezone.utc)
    for _ in range(40):
        r=await dex_gh(client,"GET",f"{api}/actions/workflows/{wf}/runs",params={"per_page":50})
        if r.status_code != 200: raise RuntimeError(f"Runs fetch failed: {r.status_code}")
        cands=[]
        for run in r.json().get("workflow_runs",[]):
            if run.get("event") != "workflow_dispatch": continue
            name=run.get("name","")
            if not (name.startswith("Telegram Dex2C") or app_name in name): continue
            try: created=datetime.fromisoformat(run["created_at"].replace("Z","+00:00"))
            except Exception: continue
            if created >= start_iso: cands.append(run)
        if cands:
            cands.sort(key=lambda x: x.get("created_at",""),reverse=True)
            return cands[0]["id"]
        await asyncio.sleep(1.5)
    raise TimeoutError("Build server initialization timeout.")

async def dex_wait_completion(client, cfg, run_id, status_cb=None):
    api=dex_api(cfg)
    for _ in range(600):
        r=await dex_gh(client,"GET",f"{api}/actions/runs/{run_id}")
        if r.status_code != 200: raise RuntimeError(f"Status check failed: {r.status_code}")
        data=r.json(); status,conclusion=data.get("status"),data.get("conclusion")
        if status_cb: await status_cb(status,conclusion)
        if status == "completed":
            if conclusion != "success": raise RuntimeError(f"Build failed with status: {conclusion}")
            return data
        await asyncio.sleep(5)
    raise TimeoutError("Build timeout.")

async def dex_download_artifact(client, cfg, run_id):
    api=dex_api(cfg)
    r=await dex_gh(client,"GET",f"{api}/actions/runs/{run_id}/artifacts")
    if r.status_code != 200: raise RuntimeError("Artifact retrieval failed.")
    target=next((a for a in r.json().get("artifacts",[]) if a.get("name")=="dex2c_app"),None)
    if not target: raise RuntimeError("Output artifact not found.")
    r1=await client.get(target["archive_download_url"],follow_redirects=False)
    url=r1.headers.get("Location") if r1.status_code in (301,302,307,308) else target["archive_download_url"]
    async with httpx.AsyncClient(timeout=300) as plain:
        res=await plain.get(url)
    if res.status_code != 200: raise RuntimeError("Artifact download failed.")
    return res.content

def dex_sessions(application):
    return application.bot_data.setdefault('dex_sessions',{})

def dex_session(context, user_id):
    try: return context.application.bot_data.get('dex_sessions',{}).get(user_id)
    except Exception: return None

def dex_forget(application, user_id, order_id=None):
    try:
        sessions=application.bot_data.get('dex_sessions',{})
        if order_id is None or (sessions.get(user_id) or {}).get('order_id') == order_id:
            sessions.pop(user_id,None)
    except Exception: pass

async def show_dex_services(query, context):
    with service(context).db.connect() as c:
        rows=c.execute("SELECT * FROM services WHERE is_active=1 ORDER BY id").fetchall()
    if not rows:
        try: await query.edit_message_text("لا توجد خدمات متاحة حالياً.",reply_markup=back_home())
        except Exception:
            try: await query.delete_message()
            except Exception: pass
            await context.bot.send_message(query.message.chat_id,"لا توجد خدمات متاحة حالياً.",reply_markup=back_home())
        return
    keyboard=[]
    for r in rows:
        keyboard.append([B(f"{r['title'][:50]}",callback_data=f"dex:{r['id']}"),B(f"💳 {r['credits']}",callback_data=f"dex:{r['id']}")])
    keyboard.append([B("رجوع",callback_data="home")])
    text=("<b>🔥 الخدمات الاحترافية — نفّذ طلبك بضغطة زر ⚡</b>\n"
          "<i>اختر الخدمة المناسبة لك: ادفع بالـCredits مرة واحدة وتابع التنفيذ واستلم النتيجة مباشرة داخل المحادثة.</i>")
    try:
        await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))
    except Exception:
        try: await query.delete_message()
        except Exception: pass
        await context.bot.send_message(query.message.chat_id,text,parse_mode=ParseMode.HTML,reply_markup=M(keyboard))

async def show_dex_service(query, context, service_id):
    with service(context).db.connect() as c:
        s=c.execute("SELECT * FROM services WHERE id=? AND is_active=1",(service_id,)).fetchone()
    if not s: await query.answer("الخدمة غير متاحة",show_alert=True); return
    desc=(s['description'] or '').strip()
    text=(f"<b><u>{escape(s['title'])}</u></b>\n\n"
          + (f"{escape(desc)}\n\n" if desc else "") +
          f"💳 التكلفة: <b>{s['credits']} Credits</b>\n\n"
          f"<i>اضغط بدء الخدمة لخصم الرصيد أولاً ثم أرسل ملف APK.</i>")
    buttons=[[B(f"🚀 بدء الخدمة ({s['credits']} Credits)",callback_data=f"dex_start:{s['id']}")]]
    session=dex_session(context,query.from_user.id)
    if session and session.get('service_id') == s['id'] and session.get('stage') != 'building':
        buttons.append([B("إلغاء الخدمة الجارية",callback_data=f"dex_cancel:{s['id']}")])
    buttons.append([B("رجوع للخدمات",callback_data="services")])
    kb=M(buttons)
    try:
        await query.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=kb)
    except Exception:
        try: await query.delete_message()
        except Exception: pass
        await context.bot.send_message(query.message.chat_id,text,parse_mode=ParseMode.HTML,reply_markup=kb)

async def dex_start_service(query, context, service_id):
    with service(context).db.connect() as c:
        s=c.execute("SELECT * FROM services WHERE id=? AND is_active=1",(service_id,)).fetchone()
    if not s: await query.answer("الخدمة غير متاحة",show_alert=True); return
    if dex_session(context,query.from_user.id):
        await query.answer("لديك خدمة قيد التنفيذ بالفعل. أكملها أو ألغها أولاً.",show_alert=True); return
    if dex_config() is None:
        await query.answer("الخدمة غير مُعدة بعد. تواصل مع الإدارة.",show_alert=True); return
    try:
        with service(context).db.transaction() as c:
            # الطلب أولاً حتى يكون مرجع الخصم فريداً (order_id) ولا يتكرر UNIQUE أبداً
            cur=c.execute("INSERT INTO service_orders(user_id,service_id,credits,status) VALUES(?,?,?,'Building')",(query.from_user.id,s['id'],s['credits']))
            order_id=cur.lastrowid
            service(context).credit(c,query.from_user.id,-s['credits'],"dex_service","dex_service",str(order_id),None)
            service(context).history(c,query.from_user.id,"dex_service",f"بدء خدمة {s['title']} ({s['credits']} Credits)",{"order_id":order_id})
    except ValueError:
        with service(context).db.connect() as c:
            brow=c.execute("SELECT credits FROM users WHERE telegram_id=?",(query.from_user.id,)).fetchone()
        balance=brow['credits'] if brow else 0
        await context.bot.send_message(query.message.chat_id,render_no_credits(context,balance,s['credits']),parse_mode=ParseMode.HTML,reply_markup=M([[B("💳 شحن Credits",callback_data="topup")]]))
        return
    except Exception:
        log.exception("dex_start failed")
        await query.answer("حدث خطأ أثناء بدء الخدمة. حاول مجدداً.",show_alert=True)
        return
    dex_sessions(context.application)[query.from_user.id]={'service_id':s['id'],'order_id':order_id,'stage':'waiting_apk','apk':None,'apk_name':None,'created':time.time()}
    log.info("dex order %s started: user=%s service=%s cost=%s",order_id,query.from_user.id,s['id'],s['credits'])
    await context.bot.send_message(query.message.chat_id,(f"✅ تم خصم <b>{s['credits']} Credits</b> وبدء خدمة <b>{escape(s['title'])}</b>.\n\n"
        "📦 أرسل الآن ملف <b>APK</b> المطلوب حمايته (بحد أقصى 20MB)."),parse_mode=ParseMode.HTML,
        reply_markup=M([[B("إلغاء الخدمة",callback_data=f"dex_cancel:{s['id']}")]]))

async def handle_dex_document(update, context):
    """يستقبل APK أثناء جلسة خدمة. يرجع True لو استهلك الرسالة."""
    session=dex_session(context,update.effective_user.id)
    if not session or session.get('stage') != 'waiting_apk': return False
    document=update.message.document
    if not document: return False
    if not document.file_name or not document.file_name.lower().endswith(".apk"):
        await update.message.reply_text("الملف غير صالح — أرسل ملف بصيغة APK فقط."); return True
    if (document.file_size or 0) > DEX2C_MAX_APK:
        await update.message.reply_text(f"حجم الملف ({fmt_mb(document.file_size)}) يتجاوز الحد الأقصى ({fmt_mb(DEX2C_MAX_APK)})."); return True
    if (document.file_size or 0) > TELEGRAM_BOT_DOWNLOAD_LIMIT:
        await update.message.reply_text(f"⚠️ حجم الملف ({fmt_mb(document.file_size)}) أكبر من حد تيليجرام للبوتات ({fmt_mb(TELEGRAM_BOT_DOWNLOAD_LIMIT)}). أرسل نسخة أصغر."); return True
    status=await update.message.reply_text("جاري استلام الملف والتحقق من الحجم...")
    try:
        remote=await context.bot.get_file(document.file_id)
        apk_data=bytes(await remote.download_as_bytearray())
        if len(apk_data) > DEX2C_MAX_APK:
            await status.edit_text("حجم الملف يتجاوز الحد الأقصى المسموح به."); return True
        session['apk']=apk_data; session['apk_name']=document.file_name; session['stage']='waiting_filter'
        await status.edit_text("تم استلام التطبيق بنجاح.\n\nيرجى إرسال قائمة الكلاسات (Filter Classes) المطلوب تحويلها لـ Native C++.\n\nأمثلة:\ncom.example.MainActivity\ncom.example.security.*\n\nضع كل كلاس أو باكيج في سطر مستقل.")
    except Exception as exc:
        log.exception("Dex APK receive failed")
        await status.edit_text(f"حدث خطأ أثناء رفع الملف: {str(exc)[:200]}")
    return True

async def handle_dex_text(update, context):
    """يستقبل الفلتر أثناء جلسة خدمة ويطلق البناء. يرجع True لو استهلك الرسالة."""
    session=dex_session(context,update.effective_user.id)
    if not session or session.get('stage') != 'waiting_filter': return False
    text=(update.message.text or '').strip()
    if not text or len(text) > DEX2C_MAX_FILTER:
        await update.message.reply_text("الفلتر المدخل غير صالح أو يتجاوز الحجم المسموح."); return True
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if not lines or any("/" in l or "\\" in l for l in lines):
        await update.message.reply_text("صيغة الفلتر غير صحيحة، يرجى إدخال أسماء الكلاسات فقط."); return True
    session['stage']='building'
    filter_data=("\n".join(lines)+"\n").encode()
    status=await update.message.reply_text("جاري تهيئة بيئة البناء...")
    asyncio.create_task(run_dex_build(update.effective_chat.id,update.effective_user.id,session,filter_data,status.message_id,context.application))
    return True

async def dex_stale_sweeper(application, timeout_seconds=1800, interval_seconds=300):
    """يلغي الجلسات العالقة (APK/فلتر لم يكتمل) مع استرداد الرصيد — الفشل فقط."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            svc=application.bot_data['service']
            now=time.time()
            for uid,sess in list(application.bot_data.get('dex_sessions',{}).items()):
                if not isinstance(sess,dict) or sess.get('stage') == 'building': continue
                if now - sess.get('created',now) < timeout_seconds: continue
                order_id=sess.get('order_id')
                try:
                    with svc.db.transaction() as c:
                        row=c.execute("SELECT * FROM service_orders WHERE id=?",(order_id,)).fetchone()
                        if row and row['status'] == 'Building':
                            svc.credit(c,uid,row['credits'],"dex_refund","dex_service",str(order_id),None)
                            c.execute("UPDATE service_orders SET status='Cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=?",(order_id,))
                            svc.history(c,uid,"dex_refund","انتهت مهلة الخدمة وتم استرداد الرصيد تلقائياً",{"order_id":order_id})
                            log.info("dex order %s stale-cancelled with refund: user=%s",order_id,uid)
                    dex_forget(application,uid,order_id)
                    try: await application.bot.send_message(uid,"⏳ انتهت مهلة الخدمة لعدم إكمال الخطوات، وتم استرداد رصيدك تلقائياً.")
                    except Exception: pass
                except Exception:
                    log.warning("dex stale sweep failed for order %s",order_id,exc_info=True)
        except Exception:
            log.warning("dex stale sweeper cycle failed",exc_info=True)

async def run_dex_build(chat_id, user_id, session, filter_data, status_id, application):
    bot=application.bot; svc=application.bot_data['service']
    order_id=session['order_id']; service_id=session['service_id']
    lock=application.bot_data.setdefault('dex_lock',asyncio.Lock())
    hourglass_id=None
    log.info("dex order %s building: user=%s service=%s",order_id,user_id,service_id)
    async def edit(text):
        try: await bot.edit_message_text(text,chat_id=chat_id,message_id=status_id,parse_mode=ParseMode.HTML)
        except Exception: pass
    def build_status_label(status, conclusion):
        if status == 'queued': return "⏳ <b>في الانتظار</b>"
        if status == 'in_progress': return "⚙️ <b>جارٍ التنفيذ</b>"
        if status == 'completed': return "✅ <b>مكتمل</b>" if conclusion == 'success' else "❌ <b>انتهى بفشل</b>"
        return f"⏳ <b>{escape(status or '...')}</b>"
    async with lock:
        try:
            cfg=dex_config()
            if cfg is None: raise RuntimeError("خدمة البناء غير مُعدة. تواصل مع الإدارة.")
            await edit("فحص مكونات الخدمة...")
            log.info("dex order %s: checking workflow on %s/%s",order_id,cfg['owner'],cfg['repo'])
            async with httpx.AsyncClient(timeout=120,headers=dex_headers(cfg)) as client:
                await dex_ensure_workflow(client,cfg)
                with svc.db.connect() as c:
                    srow=c.execute("SELECT title FROM services WHERE id=?",(service_id,)).fetchone()
                app_name_base=(srow['title'] if srow else 'app')
                num=await dex_next_app_number(client,cfg)
                app_name=f"app{num}"
                folder=f"{cfg['apps']}/{app_name}"
                await edit("جاري رفع واستخراج البيانات في بيئة المعالجة...")
                await dex_upload_file(client,cfg,f"{folder}/app.apk",session['apk'],f"Process {app_name}")
                await dex_upload_file(client,cfg,f"{folder}/filter.txt",filter_data,f"Filter {app_name}")
                log.info("dex order %s: files uploaded as %s",order_id,app_name)
                started=time.time()
                await edit("جاري تشغيل محرك البناء...")
                await dex_dispatch(client,cfg,f"{folder}/app.apk",f"{folder}/filter.txt",app_name)
                run_id=await dex_wait_for_run(client,cfg,started,app_name)
                log.info("dex order %s: dispatched run %s",order_id,run_id)
                try:
                    hg=await bot.send_message(chat_id,"⏳")
                    hourglass_id=hg.message_id
                except Exception: pass
                await edit("⚙️ <b>بيئة المعالجة نشطة الآن</b>\n\n✅ تجهيز Java و Android SDK\n⏳ تشغيل محرك البناء والترجمة والتوقيع\n\n<i>قد يستغرق دقائق قليلة...</i>")
                async def cb(st,co): await edit(f"{build_status_label(st,co)}\n\n<i>قد يستغرق دقائق قليلة...</i>")
                await dex_wait_completion(client,cfg,run_id,cb)
                await edit("✅ <b>تم اكتمال البناء بنجاح</b>\n<i>جاري تجميع الملف النهائي...</i>")
                result=await dex_download_artifact(client,cfg,run_id)
            await edit("📦 <b>تم تجهيز الملف — جارٍ الإرسال...</b>")
            await bot.send_document(chat_id,io.BytesIO(result),filename="Protected_App_Dex2C.zip",caption=f"تمت معالجة <b>{escape(app_name_base)}</b> بنجاح.",parse_mode=ParseMode.HTML)
            with svc.db.transaction() as c:
                c.execute("UPDATE service_orders SET status='Done',updated_at=CURRENT_TIMESTAMP WHERE id=?",(order_id,))
                svc.history(c,user_id,"dex_service","اكتملت الخدمة وتم التسليم",{"order_id":order_id})
            log.info("dex order %s delivered successfully: user=%s",order_id,user_id)
            await edit("اكتملت جميع العمليات بنجاح ✅")
        except Exception as exc:
            log.warning("dex order %s failed, refunding: user=%s error=%s",order_id,user_id,exc)
            log.exception("Dex build failed")
            try:
                with svc.db.transaction() as c:
                    row=c.execute("SELECT * FROM service_orders WHERE id=?",(order_id,)).fetchone()
                    if row and row['status'] == 'Building':
                        svc.credit(c,user_id,row['credits'],"dex_refund","dex_service",str(order_id),None)
                        c.execute("UPDATE service_orders SET status='Refunded',updated_at=CURRENT_TIMESTAMP WHERE id=?",(order_id,))
                        svc.history(c,user_id,"dex_refund","فشل البناء وتم استرداد الرصيد تلقائياً",{"order_id":order_id})
                        log.info("dex order %s refunded %s credits to user=%s",order_id,row['credits'],user_id)
                await edit(f"فشلت العملية وتم استرداد رصيدك تلقائياً.\n<i>{escape(str(exc)[:200])}</i>")
                try: await bot.send_message(chat_id,"تم استرداد Credits تلقائياً.",reply_markup=main_menu(user_id in ADMIN_IDS,[]))
                except Exception: pass
            except Exception:
                log.exception("Dex refund failed")
                await edit(f"فشلت العملية: {escape(str(exc)[:200])}")
        finally:
            if hourglass_id:
                try: await bot.delete_message(chat_id,hourglass_id)
                except Exception: pass
            # مسح الجلسة حتى تعمل الخدمة مجدداً بدل التوقف بعد أول استخدام
            dex_forget(application,user_id,order_id)

def is_user_banned(context, user_id):
    try:
        with service(context).db.connect() as c:
            row=c.execute("SELECT is_active FROM users WHERE telegram_id=?",(user_id,)).fetchone()
            return row is not None and not row['is_active']
    except Exception: return False

async def callbacks(update,context):
    q=update.callback_query; data=q.data
    if data.startswith("custombtn:"):
        # قبل أي answer عام — حتى تعمل الرسالة المنبثقة (Dialog) من أول رد
        await handle_custom_button(update,context,int(data.split(":")[1]))
        return
    if data=="check_sub":
        pending=await pending_required_subscriptions(context,q.from_user.id)
        if pending:
            await q.answer("⚠️ لم تشترك بعد في كل القنوات المطلوبة.",show_alert=True)
            return
        await q.answer("✅ تم التحقق من اشتراكك، أهلاً بك!")
        await clear_subscription_prompt(update,context)
        await reward_referral_if_ready(context,q.from_user.id)
        try:
            await show_home(update,context,True)
        except Exception:
            try: await q.delete_message()
            except Exception: pass
            await show_home(update,context,False)
        return
    await q.answer()
    if (data == "admin" or data.startswith("admin_")) and q.from_user.id not in ADMIN_IDS:
        await q.answer("ليس لديك صلاحية الإدارة.", show_alert=True)
        return
    if data=="noop": return
    if q.from_user.id not in ADMIN_IDS:
        if is_user_banned(context,q.from_user.id):
            try: await q.answer()
            except Exception: pass
            await context.bot.send_message(q.message.chat_id,ban_text(context))
            return
        pending=await pending_required_subscriptions(context,q.from_user.id)
        if pending:
            # مستخدم قديم غير مشترك: نعرض القنوات + زر تحقق، ولا يعمل شيء حتى يشترك
            if not context.user_data.get('subscription_prompt_ids'):
                ids=await send_sub_prompt(q.message.chat_id,context,pending,True)
                context.user_data['subscription_prompt_ids']=ids
            await q.answer("🔐 اشترك في القنوات المطلوبة ثم اضغط (✅ تحققت من الاشتراك).",show_alert=True)
            return
    if data=="services": await show_dex_services(q,context)
    elif data.startswith("dex:"):
        await show_dex_service(q,context,int(data.split(":")[1]))
    elif data.startswith("dex_start:"):
        await dex_start_service(q,context,int(data.split(":")[1]))
    elif data.startswith("dex_cancel:"):
        sid=int(data.split(":")[1])
        session=dex_session(context,q.from_user.id)
        if not session or session.get('service_id') != sid:
            await q.answer("لا توجد عملية نشطة.",show_alert=True); return
        if session.get('stage') == 'building':
            await q.answer("لا يمكن الإلغاء أثناء البناء.",show_alert=True); return
        order_id=session.get('order_id')
        try:
            with service(context).db.transaction() as c:
                row=c.execute("SELECT * FROM service_orders WHERE id=?",(order_id,)).fetchone()
                if row and row['status'] == 'Building':
                    service(context).credit(c,q.from_user.id,row['credits'],"dex_refund","dex_service",str(order_id),None)
                    c.execute("UPDATE service_orders SET status='Cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=?",(order_id,))
        except Exception:
            log.exception("Dex cancel refund failed")
        log.info("dex order %s cancelled by user=%s",session.get('order_id'),q.from_user.id)
        dex_forget(context.application,q.from_user.id,session.get('order_id'))
        await q.answer("تم إلغاء الخدمة واسترداد رصيدك.")
        await show_dex_services(q,context)
    elif data=="home": await show_home(update,context,True)
    elif data=="products": await show_products(q,context,1,shuffle=True)
    elif data.startswith("products:"):
        _,page=data.split(":"); await show_products(q,context,int(page),shuffle=False)
    elif data.startswith("product:"):
        _,pid,page=data.split(":"); await show_product(q,context,int(pid),int(page))
    elif data.startswith("buy:"):
        _,pid,page=data.split(":"); await buy(q,context,int(pid),int(page))
    elif data=="account": await account(q,context)
    elif data=="referrals": await referrals(q,context)
    elif data=="history" or data.startswith("history:"): await history(q,context)
    elif data=="topup": await topup(q,context)
    elif data=="stars": await stars(q,context)
    elif data=="admin": await admin_dashboard(q,context)
    elif data.startswith("admin_products:"):
        _,page=data.split(":"); await admin_products(q,context,int(page))
    elif data.startswith("admin_users:"): await admin_users(q,context,int(data.split(":")[1]))
    elif data.startswith("admin_user:"):
        _,user_id,page=data.split(":"); await admin_user(q,context,int(user_id),int(page))
    elif data=="admin_banmsg":
        current=ban_text(context)
        context.user_data['admin_banmsg_setup']=True
        await q.edit_message_text(f"<b>✏️ رسالة الحظر</b>\n\nالحالية:\n{escape(current)}\n\nأرسل النص الجديد الذي يظهر للمحظور:",parse_mode=ParseMode.HTML,reply_markup=M([[B("رجوع",callback_data="admin_users:1")]]))
    elif data=="admin_db_backup":
        await q.answer("⏳ جارٍ تجهيز النسخة…")
        try:
            tmp=os.path.join(os.path.dirname(os.path.abspath(db_file_path())) or ".",f"backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db")
            if not os.path.isabs(tmp): tmp=os.path.abspath(tmp)
            backup_database(tmp)
            stamp=datetime.now().strftime('%Y-%m-%d %H:%M')
            with open(tmp,'rb') as fh:
                await context.bot.send_document(q.message.chat_id,fh,filename=f"users_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db",caption=f"📥 نسخة قاعدة البيانات — {stamp}")
            try: os.remove(tmp)
            except Exception: pass
        except Exception:
            log.exception("DB backup failed")
            await q.answer("تعذر إنشاء النسخة.",show_alert=True)
    elif data.startswith("admin_ban:"):
        _,user_id,page=data.split(":"); user_id=int(user_id)
        with service(context).db.transaction() as c:
            c.execute("UPDATE users SET is_active=0 WHERE telegram_id=?",(user_id,))
            service(context).history(c,user_id,"ban","⛔ تم حظر الحساب بواسطة الإدارة")
        await q.answer("تم حظر المستخدم.")
        await admin_user(q,context,user_id,int(page))
        return
    elif data.startswith("admin_unban:"):
        _,user_id,page=data.split(":"); user_id=int(user_id)
        with service(context).db.transaction() as c:
            c.execute("UPDATE users SET is_active=1 WHERE telegram_id=?",(user_id,))
            service(context).history(c,user_id,"unban","✅ تم فك الحظر عن الحساب")
        await q.answer("تم فك الحظر.")
        await admin_user(q,context,user_id,int(page))
        return
    elif data.startswith("admin_product:"):
        _,product_id,page=data.split(":"); await admin_product(q,context,int(product_id),int(page))
    elif data.startswith("admin_product_stop:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        with service(context).db.transaction() as c:c.execute("UPDATE products SET is_active=0 WHERE id=?",(pid,))
        context.user_data.pop('shuffled_products',None)
        await q.answer("تم إيقاف المنتج.")
        await admin_product(q,context,pid,page)
    elif data.startswith("admin_product_activate:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        with service(context).db.transaction() as c:c.execute("UPDATE products SET is_active=1 WHERE id=?",(pid,))
        context.user_data.pop('shuffled_products',None)
        await q.answer("تم تفعيل المنتج.")
        await admin_product(q,context,pid,page)
    elif data.startswith("admin_products_stopped:"):
        await admin_products_stopped(q,context,int(data.split(":")[1]))
    elif data=="admin_products_stats": await admin_products_stats(q,context)
    elif data=="admin_products_top": await admin_products_top(q,context)
    elif data=="custombtn_noop": pass
    elif data.startswith("admin_product_preview:"):
        _,product_id,page=data.split(":"); await admin_product_preview(q,context,int(product_id),int(page))
    elif data.startswith("admin_edit_title:"):
        _,pid,page=data.split(":"); context.user_data['edit_product']={'field':'title','product_id':int(pid),'page':int(page)}
        await ask_admin_edit(q,context,"أرسل العنوان الجديد للمنتج:",M([[B("إلغاء",callback_data=f"admin_product:{pid}:{page}")]]))
    elif data.startswith("admin_edit_short:"):
        _,pid,page=data.split(":"); context.user_data['edit_product']={'field':'short','product_id':int(pid),'page':int(page)}
        await ask_admin_edit(q,context,"أرسل الوصف القصير الجديد:",M([[B("إلغاء",callback_data=f"admin_product:{pid}:{page}")]]))
    elif data.startswith("admin_edit_desc:"):
        _,pid,page=data.split(":"); context.user_data['edit_product']={'field':'description','product_id':int(pid),'page':int(page)}
        await ask_admin_edit(q,context,"أرسل الوصف الكامل الجديد:",M([[B("إلغاء",callback_data=f"admin_product:{pid}:{page}")]]))
    elif data.startswith("admin_edit_price:"):
        _,pid,page=data.split(":"); context.user_data['edit_product']={'field':'price','product_id':int(pid),'page':int(page)}
        await ask_admin_edit(q,context,"أرسل السعر الجديد بعدد الـCredits (مثال: 10 أو 10.5):",M([[B("إلغاء",callback_data=f"admin_product:{pid}:{page}")]]))
    elif data.startswith("admin_edit_qty:"):
        _,pid,page=data.split(":"); context.user_data['edit_product']={'field':'quantity','product_id':int(pid),'page':int(page)}
        await ask_admin_edit(q,context,"أرسل الكمية الجديدة (عدد صحيح)، أو اختر كمية غير محدودة:",M([[B("♾️ كمية غير محدودة",callback_data=f"admin_edit_qty_none:{pid}:{page}")],[B("إلغاء",callback_data=f"admin_product:{pid}:{page}")]]))
    elif data.startswith("admin_edit_qty_none:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        with service(context).db.transaction() as c:c.execute("UPDATE products SET stock_quantity=NULL WHERE id=?",(pid,))
        context.user_data.pop('edit_product',None)
        await q.answer("تم جعل الكمية غير محدودة.")
        await admin_product(q,context,pid,page)
    elif data.startswith("admin_edit_images:") and not data.startswith("admin_edit_images_done:") and not data.startswith("admin_edit_images_back:") and not data.startswith("admin_edit_images_cancel:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        with service(context).db.connect() as c:
            row=c.execute("SELECT image_urls FROM products WHERE id=?",(pid,)).fetchone()
        working=list(json.loads(row['image_urls'] or '[]')) if row else []
        context.user_data['edit_product']={'field':'images','product_id':pid,'page':page,'images':working,'preview_ids':[]}
        if working:
            try:
                sent_group=await context.bot.send_media_group(q.message.chat_id,[InputMediaPhoto(url) for url in working[:10]])
                context.user_data['edit_product']['preview_ids']=[m.message_id for m in sent_group]
            except Exception:
                log.warning("Could not preview product images",exc_info=True)
        await render_image_manager(q,context,pid,page)
    elif data.startswith("admin_edit_img_add:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        edit=context.user_data.get('edit_product')
        if not edit or edit.get('field') != 'images' or edit.get('product_id') != pid:
            await q.answer("لا توجد عملية تعديل صور نشطة.",show_alert=True); return
        edit['replace_idx']=None
        await ask_admin_edit(q,context,"أرسل الصور الجديدة (كل صورة تُضاف للقائمة):",M([[B("✅ تم",callback_data=f"admin_edit_images_done:{pid}:{page}")],[B("رجوع",callback_data=f"admin_edit_images_back:{pid}:{page}")]]))
    elif data.startswith("admin_edit_img_replace:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        edit=context.user_data.get('edit_product')
        if not edit or edit.get('field') != 'images' or edit.get('product_id') != pid or 'sel_idx' not in edit:
            await q.answer("لا توجد عملية تعديل صور نشطة.",show_alert=True); return
        edit['replace_idx']=edit.pop('sel_idx')
        await ask_admin_edit(q,context,f"أرسل الصورة البديلة لصورة {edit['replace_idx']+1} الآن:",M([[B("رجوع",callback_data=f"admin_edit_images_back:{pid}:{page}")]]))
    elif data.startswith("admin_edit_img_del:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        edit=context.user_data.get('edit_product')
        if not edit or edit.get('field') != 'images' or edit.get('product_id') != pid or 'sel_idx' not in edit:
            await q.answer("لا توجد عملية تعديل صور نشطة.",show_alert=True); return
        idx=edit.pop('sel_idx')
        if 0 <= idx < len(edit.get('images',[])): edit['images'].pop(idx)
        save_edit_images(context,pid)
        await q.answer(f"تم حذف صورة {idx+1}.")
        await render_image_manager(q,context,pid,page)
    elif data.startswith("admin_edit_img:"):
        _,pid,idx,page=data.split(":"); pid=int(pid); idx=int(idx); page=int(page)
        edit=context.user_data.get('edit_product')
        if not edit or edit.get('field') != 'images' or edit.get('product_id') != pid:
            await q.answer("لا توجد عملية تعديل صور نشطة.",show_alert=True); return
        if idx < 0 or idx >= len(edit.get('images',[])):
            await q.answer("الصورة غير موجودة.",show_alert=True); return
        edit['sel_idx']=idx
        await ask_admin_edit(q,context,f"🖼️ <b>صورة {idx+1}</b> — اختر الإجراء:",M([
            [B("🔄 استبدال الصورة",callback_data=f"admin_edit_img_replace:{pid}:{page}")],
            [B("🗑️ حذف الصورة",callback_data=f"admin_edit_img_del:{pid}:{page}")],
            [B("رجوع",callback_data=f"admin_edit_images_back:{pid}:{page}")]]))
    elif data.startswith("admin_edit_images_back:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        edit=context.user_data.get('edit_product')
        if not edit or edit.get('field') != 'images' or edit.get('product_id') != pid:
            await q.answer("لا توجد عملية تعديل صور نشطة.",show_alert=True); return
        edit.pop('replace_idx',None); edit.pop('sel_idx',None)
        await render_image_manager(q,context,pid,page)
    elif data.startswith("admin_edit_images_cancel:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        await cleanup_edit_previews(context,q.message.chat_id)
        context.user_data.pop('edit_product',None)
        await admin_product(q,context,pid,page)
    elif data.startswith("admin_edit_images_done:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        edit=context.user_data.get('edit_product')
        if not edit or edit.get('field') != 'images' or edit.get('product_id') != pid:
            await q.answer("لا توجد عملية تعديل صور نشطة.",show_alert=True); return
        save_edit_images(context,pid)
        await cleanup_edit_previews(context,q.message.chat_id)
        context.user_data.pop('edit_product',None)
        await q.answer("تم حفظ الصور.")
        await admin_product(q,context,pid,page)
    elif data.startswith("admin_edit_file:"):
        _,pid,page=data.split(":"); context.user_data['edit_product']={'field':'file','step':'choice','product_id':int(pid),'page':int(page)}
        await ask_admin_edit(q,context,"محتوى المنتج الجديد: هل هو <b>رابط</b> ولا <b>ملف</b>؟",M([[B("🔗 رابط",callback_data="admin_edit_is_link"),B("📁 ملف",callback_data="admin_edit_is_file")],[B("إلغاء",callback_data=f"admin_product:{pid}:{page}")]]))
    elif data=="admin_statistics": await admin_statistics(q,context)
    elif data=="admin_storage": await admin_storage(q,context)
    elif data=="admin_notifications": await admin_notifications(q,context)
    elif data=="admin_notif_general": await admin_notif_general(q,context)
    elif data.startswith("admin_notif_direct:"): await admin_notif_direct(q,context,int(data.split(":")[1]))
    elif data.startswith("admin_notif_pick:"):
        _,user_id,page=data.split(":")
        context.user_data['admin_notification']={'mode':'direct','stage':'body','recipient':int(user_id)}
        await q.edit_message_text("أرسل نص الإشعار الآن.",reply_markup=admin_back())
    elif data=="admin_support": await admin_support(q,context)
    elif data=="admin_support_add":
        context.user_data['admin_support_setup']={'stage':'label'}
        await q.edit_message_text("أرسل اسم جهة خدمة العملاء (مثال: الدعم الفني):",reply_markup=admin_back())
    elif data.startswith("admin_support_del:"):
        with service(context).db.transaction() as c:c.execute("DELETE FROM support_contacts WHERE id=?",(int(data.split(':')[1]),))
        await q.answer("تم حذف جهة الدعم.")
        await admin_support(q,context)
    elif data.startswith("admin_support_toggle:"):
        sid=int(data.split(':')[1])
        with service(context).db.transaction() as c:
            row=c.execute("SELECT is_active FROM support_contacts WHERE id=?",(sid,)).fetchone()
            if row: c.execute("UPDATE support_contacts SET is_active=? WHERE id=?",(0 if row['is_active'] else 1,sid))
        await admin_support(q,context)
    elif data=="admin_custom": await admin_custom(q,context)
    elif data=="admin_custom_add":
        context.user_data['admin_custom_setup']={'stage':'label'}
        await q.edit_message_text("أرسل اسم الزر الجديد (سيظهر للمستخدمين):",reply_markup=M([[B("رجوع",callback_data="admin_custom")]]))
    elif data.startswith("admin_custom_item:"):
        await admin_custom_item(q,context,int(data.split(':')[1]))
    elif data.startswith("admin_custom_toggle:"):
        bid=int(data.split(':')[1])
        with service(context).db.transaction() as c:
            row=c.execute("SELECT is_active FROM custom_buttons WHERE id=?",(bid,)).fetchone()
            if row: c.execute("UPDATE custom_buttons SET is_active=? WHERE id=?",(0 if row['is_active'] else 1,bid))
        await q.answer("تم التحديث.")
        await admin_custom_item(q,context,bid)
    elif data.startswith("admin_custom_del:"):
        with service(context).db.transaction() as c:c.execute("DELETE FROM custom_buttons WHERE id=?",(int(data.split(':')[1]),))
        await q.answer("تم حذف الزر.")
        await admin_custom(q,context)
    elif data.startswith("admin_custom_kind:"):
        setup=context.user_data.get('admin_custom_setup')
        if not setup or setup.get('stage') != 'kind':
            await q.answer("لا توجد عملية إنشاء زر نشطة.",show_alert=True); return
        kind=data.split(':')[1]
        setup['kind']=kind
        if kind == 'url':
            setup['stage']='target'
            await q.edit_message_text("أرسل الرابط الخارجي (https://...):",reply_markup=M([[B("رجوع",callback_data="admin_custom")]]))
        elif kind == 'internal':
            setup['stage']='internal'
            await q.edit_message_text("اختر الوجهة الداخلية:",reply_markup=M([
                [B("🛍️ المنتجات",callback_data="admin_custom_go:products"),B("👤 حسابي",callback_data="admin_custom_go:account")],
                [B("💳 الشحن",callback_data="admin_custom_go:topup"),B("👥 إحالاتي",callback_data="admin_custom_go:referrals")],
                [B("📜 سجلاتي",callback_data="admin_custom_go:history")],
                [B("رجوع",callback_data="admin_custom")]]))
        else:
            setup['stage']='body'
            await q.edit_message_text("أرسل نص الرسالة المنبثقة (Dialog) التي ستظهر للمستخدم:",reply_markup=M([[B("رجوع",callback_data="admin_custom")]]))
    elif data.startswith("admin_custom_go:"):
        setup=context.user_data.get('admin_custom_setup')
        if not setup or setup.get('stage') != 'internal':
            await q.answer("لا توجد عملية إنشاء زر نشطة.",show_alert=True); return
        with service(context).db.transaction() as c:
            cur=c.execute("INSERT INTO custom_buttons(label,kind,target,is_active) VALUES(?, 'internal', ?, 1)",(setup['label'],data.split(':')[1]))
            bid=cur.lastrowid
        context.user_data.pop('admin_custom_setup',None)
        await q.answer("تم إنشاء الزر.")
        await admin_custom_item(q,context,bid)
    elif data.startswith("admin_gifts:"):
        await admin_gifts(q,context,int(data.split(':')[1]))
    elif data=="admin_gift_add":
        context.user_data['admin_gift_setup']={'stage':'credits'}
        await q.edit_message_text("أرسل عدد الـCredits للهدية (مثال: 5):",reply_markup=M([[B("رجوع",callback_data="admin_gifts:1")]]))
    elif data.startswith("admin_gift:"):
        _,gid,page=data.split(":"); await admin_gift_detail(q,context,int(gid),int(page))
    elif data.startswith("admin_gift_toggle:"):
        _,gid,page=data.split(":"); gid=int(gid); page=int(page)
        with service(context).db.transaction() as c:
            row=c.execute("SELECT is_active FROM redeem_links WHERE id=?",(gid,)).fetchone()
            if row: c.execute("UPDATE redeem_links SET is_active=? WHERE id=?",(0 if row['is_active'] else 1,gid))
        await admin_gift_detail(q,context,gid,page)
    elif data.startswith("admin_gift_del:"):
        _,gid,page=data.split(":"); gid=int(gid)
        with service(context).db.transaction() as c:
            c.execute("DELETE FROM redeem_claims WHERE link_id=?",(gid,)); c.execute("DELETE FROM redeem_links WHERE id=?",(gid,))
        await q.answer("تم حذف الرابط.")
        await admin_gifts(q,context,int(page))
    elif data=="admin_nocredits":
        current=service(context).setting("no_credits_message","⚠️ <b>عدد الـCredits لا يكفي</b> لإتمام الشراء.\n💳 رصيدك الحالي: <b>{{credits}}</b> Credits\n💰 سعر المنتج: <b>{{price}}</b> Credits")
        context.user_data['admin_nocredits_setup']=True
        await q.edit_message_text(f"<b>✏️ رسالة نفاد الكريديتس</b>\n\nالحالية:\n{current}\n\nأرسل النص الجديد. المتغيرات:\n<code>{{{{credits}}}}</code> = رصيد المستخدم\n<code>{{{{price}}}}</code> = سعر المنتج",parse_mode=ParseMode.HTML,reply_markup=M([[B("رجوع للمنتجات",callback_data="admin_products:1")]]))
    elif data=="admin_share_template":
        current=service(context).setting("share_template",DEFAULT_SHARE_TEMPLATE)
        context.user_data['admin_share_setup']=True
        await q.edit_message_text(f"<b>✏️ نص المشاركة</b>\n\nالحالي:\n{escape(current)}\n\nأرسل النص الجديد. المتغيرات:\n<code>{{{{link}}}}</code> = رابط المنتج\n<code>{{{{title}}}}</code> = اسم المنتج",parse_mode=ParseMode.HTML,reply_markup=M([[B("رجوع للمنتجات",callback_data="admin_products:1")]]))
    elif data.startswith("admin_product_del:"):
        _,pid,page=data.split(":")
        await q.edit_message_text(f"⚠️ <b>حذف نهائي للمنتج #{pid}؟</b>\n\nسيُحذف المنتج وسجل مشترياته وملفاته من GitHub نهائياً ولا يمكن التراجع.",parse_mode=ParseMode.HTML,reply_markup=M([[B("✅ نعم، احذف نهائياً",callback_data=f"admin_product_del_yes:{pid}:{page}")],[B("إلغاء",callback_data=f"admin_product:{pid}:{page}")]]))
    elif data.startswith("admin_product_del_yes:"):
        _,pid,page=data.split(":"); pid=int(pid); page=int(page)
        await q.edit_message_text("⏳ جارٍ حذف المنتج وكل بياناته وملفاته…")
        with service(context).db.connect() as c:
            product=c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
        if product is not None:
            # مسح ملفات GitHub (asset + release) وإرجاع المساحة قبل مسح الصف
            await context.application.bot_data['storage'].delete_product_storage(product)
            with service(context).db.transaction() as c:
                c.execute("DELETE FROM purchases WHERE product_id=?",(pid,))
                c.execute("DELETE FROM products WHERE id=?",(pid,))
        context.user_data.pop('shuffled_products',None)
        await q.answer("تم حذف المنتج وكل بياناته وملفاته نهائياً.")
        await admin_products(q,context,page)
    elif data=="admin_add_link" or data=="admin_add_is_link":
        addition=context.user_data.get('add_product')
        if not addition or addition.get('stage') not in ('file','file_choice','link_msg'):
            await q.answer("لا توجد عملية إضافة منتج نشطة.",show_alert=True); return
        addition['stage']='link_msg'; addition['expect_link']=True
        await q.edit_message_text("هل تريد كتابة رسالة تظهر تحت عنوان المنتج وفوق زر الرابط؟\n(أو اضغط تخطي)",reply_markup=M([[B("تخطي ⏩",callback_data="admin_add_linkmsg_skip")],[B("رجوع للوحة الإدارة",callback_data="admin")]]))
    elif data=="admin_add_linkmsg_skip":
        addition=context.user_data.get('add_product')
        if not addition or addition.get('stage') != 'link_msg':
            await q.answer("لا توجد عملية إضافة منتج نشطة.",show_alert=True); return
        addition['link_message']=''; addition['stage']='file'
        await q.edit_message_text("أرسل الرابط الآن (https://...) وسيُحفظ في ملف link.txt:",reply_markup=admin_back())
    elif data=="admin_add_is_file":
        addition=context.user_data.get('add_product')
        if not addition or addition.get('stage') not in ('file','file_choice'):
            await q.answer("لا توجد عملية إضافة منتج نشطة.",show_alert=True); return
        addition['stage']='file'; addition['expect_link']=False
        await q.edit_message_text("أرسل الآن الملف (ZIP أو TXT) أو نصاً عادياً وسيُحوَّل لملف TXT تلقائياً:",reply_markup=admin_back())
    elif data=="admin_edit_link" or data=="admin_edit_is_link":
        edit=context.user_data.get('edit_product')
        if not edit or edit.get('field') != 'file':
            await q.answer("لا توجد عملية تعديل نشطة.",show_alert=True); return
        edit['step']='link_msg'; edit['expect_link']=True
        await ask_admin_edit(q,context,"هل تريد كتابة رسالة تظهر تحت العنوان وفوق زر الرابط؟ (أو تخطي)",M([[B("تخطي ⏩",callback_data="admin_edit_linkmsg_skip")],[B("إلغاء",callback_data=f"admin_product:{edit['product_id']}:{edit.get('page',1)}")]]))
    elif data=="admin_edit_linkmsg_skip":
        edit=context.user_data.get('edit_product')
        if not edit or edit.get('field') != 'file':
            await q.answer("لا توجد عملية تعديل نشطة.",show_alert=True); return
        edit['step']='awaiting'; edit['expect_link']=True; edit['link_message']=''
        await ask_admin_edit(q,context,"أرسل الرابط الجديد وسيُحفظ في ملف link.txt:",M([[B("إلغاء",callback_data=f"admin_product:{edit['product_id']}:{edit.get('page',1)}")]]))
    elif data=="admin_edit_is_file":
        edit=context.user_data.get('edit_product')
        if not edit or edit.get('field') != 'file':
            await q.answer("لا توجد عملية تعديل نشطة.",show_alert=True); return
        edit['step']='awaiting'; edit['expect_link']=False
        await ask_admin_edit(q,context,"أرسل الملف الجديد (ZIP/TXT) أو نصاً عادياً وسيُحوَّل لـTXT:",M([[B("إلغاء",callback_data=f"admin_product:{edit['product_id']}:{edit.get('page',1)}")]]))
    elif data.startswith("admin_dex:"):
        await admin_dex(q,context,int(data.split(":")[1]))
    elif data.startswith("admin_dex_item:"):
        _,sid,page=data.split(":"); await admin_dex_item(q,context,int(sid),int(page))
    elif data.startswith("admin_dex_title:"):
        _,sid,page=data.split(":")
        context.user_data['admin_dex_edit']={'field':'title','service_id':int(sid),'page':int(page)}
        await q.edit_message_text("أرسل الاسم الجديد للخدمة:",reply_markup=M([[B("إلغاء",callback_data=f"admin_dex_item:{sid}:{page}")]]))
    elif data.startswith("admin_dex_credits:"):
        _,sid,page=data.split(":")
        context.user_data['admin_dex_edit']={'field':'credits','service_id':int(sid),'page':int(page)}
        await q.edit_message_text("أرسل عدد الكريديتس المطلوب الجديد:",reply_markup=M([[B("إلغاء",callback_data=f"admin_dex_item:{sid}:{page}")]]))
    elif data.startswith("admin_dex_desc:"):
        _,sid,page=data.split(":")
        context.user_data['admin_dex_edit']={'field':'description','service_id':int(sid),'page':int(page)}
        await q.edit_message_text("أرسل الوصف الجديد للخدمة:",reply_markup=M([[B("إلغاء",callback_data=f"admin_dex_item:{sid}:{page}")]]))
    elif data.startswith("admin_dex_toggle:"):
        _,sid,page=data.split(":"); sid=int(sid); page=int(page)
        with service(context).db.transaction() as c:
            row=c.execute("SELECT is_active FROM services WHERE id=?",(sid,)).fetchone()
            if row: c.execute("UPDATE services SET is_active=? WHERE id=?",(0 if row['is_active'] else 1,sid))
        await q.answer("تم إلغاء التفعيل." if row and row['is_active'] else "تم التفعيل.")
        await admin_dex_item(q,context,sid,page)
    elif data=="admin_referral_cfg": await admin_referral_cfg(q,context)
    elif data=="admin_referral_giver":
        context.user_data['admin_referral_setup']={'field':'giver'}
        await q.edit_message_text("أرسل مكافأة المُحيل (عدد الـCredits لكل عضو جديد يسجل برابطه، ويمكن 0):",reply_markup=M([[B("رجوع",callback_data="admin_referral_cfg")]]))
    elif data=="admin_referral_joiner":
        context.user_data['admin_referral_setup']={'field':'joiner'}
        await q.edit_message_text("أرسل مكافأة المنضم (عدد الـCredits لكل عضو يدخل عبر رابط إحالة، ويمكن 0):",reply_markup=M([[B("رجوع",callback_data="admin_referral_cfg")]]))
    elif data=="admin_required_sub": await admin_required_sub(q,context)
    elif data=="admin_product_add":
        await q.edit_message_text("<b>➕ إضافة منتج جديد</b>\n\nاختر نوع المنتج:",parse_mode=ParseMode.HTML,reply_markup=M([[B("🎁 منتج مجاني",callback_data="admin_product_add:free"),B("💳 منتج مدفوع",callback_data="admin_product_add:paid")],[B("رجوع للمنتجات",callback_data="admin_products:1")]]))
    elif data.startswith("admin_product_add:"):
        product_type=data.split(":")[1]
        context.user_data['add_product']={'stage':'title','product_type':product_type}
        label="مجاني 🎁" if product_type=='free' else "مدفوع 💳"
        await q.edit_message_text(f"إضافة منتج جديد ({label})\n\nأرسل عنوان المنتج أولاً.",reply_markup=admin_back())

    elif data=="admin_storage_delete_confirm":
        await q.edit_message_text("⚠️ سيُحذف كل مستودعات التخزين الخاصة والـRelease Assets والروابط المخزنة نهائياً. هل أنت متأكد؟",reply_markup=M([[B("نعم، احذف كل التخزين",callback_data="admin_storage_delete_execute")],[B("إلغاء",callback_data="admin_storage")]]))
    elif data=="admin_storage_sync":
        try:
            added=await context.application.bot_data['storage'].sync_repositories()
            await q.answer(f"تمت المزامنة: {added} مستودع.")
        except Exception:
            log.exception('Storage sync failed')
            await q.answer("تعذرت المزامنة. تحقق من إعدادات GitHub.",show_alert=True)
        await admin_storage(q,context)
    elif data=="admin_storage_delete_execute":
        await q.edit_message_text("⏳ جارٍ حذف بيانات التخزين…")
        try:
            await context.application.bot_data['storage'].delete_all_storage()
            await q.edit_message_text("✅ تم حذف كل بيانات التخزين ومستودعات GitHub الخاصة.",reply_markup=admin_back())
        except Exception:
            log.exception('Storage deletion failed')
            await q.edit_message_text("تعذر حذف بعض بيانات التخزين. راجع صلاحيات GitHub ثم أعد المحاولة.",reply_markup=admin_back())
    elif data=="admin_add_quantity_skip":
        addition=context.user_data.get('add_product')
        if not addition or addition.get('stage') != 'quantity':
            await q.answer("لا توجد عملية إضافة منتج نشطة.",show_alert=True); return
        addition['quantity']=None; addition['images']=[]; addition['stage']='images'
        await q.edit_message_text("أرسل صور المنتج الآن (اختياري — يمكن التخطي). عند الانتهاء اضغط الزر التالي.",reply_markup=M([[B("انتهيت من إرسال الصور",callback_data="admin_add_images_done")]]))
    elif data=="admin_add_images_done":
        addition=context.user_data.get('add_product')
        if not addition or addition.get('stage') != 'images':
            await q.answer("لا توجد عملية إضافة منتج نشطة.",show_alert=True); return
        # الصور اختيارية — يمكن إتمام المنتج بدون صور وستُرسل البطاقة نصية
        addition['stage']='file_choice'
        addition.pop('expect_link',None)
        await q.edit_message_text("محتوى المنتج: هل هو <b>رابط</b> ولا <b>ملف</b>؟",parse_mode=ParseMode.HTML,reply_markup=M([[B("🔗 رابط",callback_data="admin_add_is_link"),B("📁 ملف",callback_data="admin_add_is_file")],[B("رجوع للوحة الإدارة",callback_data="admin")]]))
    elif data.startswith("admin_credit_add:") or data.startswith("admin_credit_subtract:"):
        operation='add' if data.startswith("admin_credit_add:") else 'subtract'
        _,user_id,page=data.split(":")
        context.user_data['admin_credit_adjustment']={'operation':operation,'user_id':int(user_id)}
        await q.edit_message_text(f"أرسل عدد الـCredits الذي تريد {'إضافته' if operation=='add' else 'خصمه'}.",reply_markup=M([[B("رجوع للمستخدم",callback_data=f"admin_user:{user_id}:{page}")]]))
    elif data=="admin_broadcast":
        context.user_data['admin_notification']={'mode':'all','stage':'body'}
        await q.edit_message_text("أرسل نص الإشعار العام الآن.",reply_markup=admin_back())
    elif data=="admin_direct":
        context.user_data['admin_notification']={'mode':'direct','stage':'recipient'}
        await q.edit_message_text("أرسل Telegram ID أو @username للمستخدم.",reply_markup=admin_back())
    elif data=="admin_support_set":
        context.user_data['admin_support_setup']={'stage':'label'}
        await q.edit_message_text("أرسل اسم جهة خدمة العملاء (مثال: الدعم الفني):",reply_markup=admin_back())
    elif data.startswith("admin_sub_type:"):
        required=bool(int(data.split(":")[1]))
        context.user_data['admin_subscription_type']=required
        await q.edit_message_text("اختر نص الرسالة:",reply_markup=M([[B("استخدام النص الافتراضي",callback_data="admin_sub_default")],[B("كتابة نص مخصص",callback_data="admin_sub_custom")],[B("رجوع",callback_data="admin_required_sub")]]))
    elif data=="admin_sub_default":
        required=context.user_data.pop('admin_subscription_type',True)
        context.user_data['admin_subscription_setup']={'stage':'url','is_required':required,'title':'🔐 الاشتراك في القناة','description':'يرجى الاشتراك في القناة التالية ثم اضغط زر التحقق لإكمال الدخول.' if required else 'يمكنك الاشتراك في القناة التالية للحصول على آخر التحديثات.'}
        await q.edit_message_text(SUB_URL_PROMPT,parse_mode=ParseMode.HTML,reply_markup=admin_back())
    elif data=="admin_sub_custom":
        required=context.user_data.pop('admin_subscription_type',True)
        context.user_data['admin_subscription_setup']={'stage':'title','is_required':required}
        await q.edit_message_text("أرسل عنوان رسالة الاشتراك.",reply_markup=admin_back())
    elif data.startswith("admin_sub_delete:"):
        subscription_id=int(data.split(":")[1])
        with service(context).db.transaction() as c:c.execute("DELETE FROM subscriptions WHERE id=?",(subscription_id,))
        await q.answer("تم حذف الاشتراك.")
        await admin_required_sub(q,context)
    elif data=="admin_sub_skip_photo":
        setup=context.user_data.get('admin_subscription_setup')
        if not setup or setup.get('stage') != 'photo':
            await q.answer("لا توجد عملية إضافة اشتراك نشطة.",show_alert=True); return
        save_subscription(context,setup,'',q.from_user.id)
        context.user_data.pop('admin_subscription_setup',None)
        await q.answer("تمت الإضافة بدون صورة.")
        await admin_required_sub(q,context)

async def storage_watchdog(application, interval_seconds=300):
    """فحص دوري لاتصال GitHub — إشعار الأدمنز عند فقدان الاتصال أو عودته."""
    await asyncio.sleep(60)
    while True:
        try:
            storage=application.bot_data.get('storage')
            if storage is None: return
            connected,detail=await storage.connection_status()
            previous=application.bot_data.get('storage_online',True)
            application.bot_data['storage_online']=connected
            if previous and not connected:
                for admin_id in ADMIN_IDS:
                    try: await application.bot.send_message(admin_id,f"⚠️ <b>فقد الاتصال بتخزين GitHub</b>\n{escape(detail)}\n\nلن تعمل المعاينة والتسليم حتى عودة الاتصال.",parse_mode=ParseMode.HTML)
                    except Exception: pass
                log.warning("GitHub storage connection lost: %s",detail)
            elif not previous and connected:
                for admin_id in ADMIN_IDS:
                    try: await application.bot.send_message(admin_id,f"✅ <b>عاد الاتصال بتخزين GitHub</b>\n{escape(detail)}",parse_mode=ParseMode.HTML)
                    except Exception: pass
                log.info("GitHub storage connection restored")
        except Exception:
            log.warning("Storage watchdog check failed",exc_info=True)
        await asyncio.sleep(interval_seconds)

async def database_backup_task(application, interval_seconds=28800):
    """نسخة احتياطية من قاعدة البيانات تُرسل للأدمنز عبر البوت فقط — كل 8 ساعات فقط."""
    await asyncio.sleep(interval_seconds)
    while True:
        try:
            tmp=os.path.abspath(f"auto_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db")
            backup_database(tmp)
            stamp=datetime.now().strftime('%Y-%m-%d %H:%M')
            with open(tmp,'rb') as fh:
                data=fh.read()
            for admin_id in ADMIN_IDS:
                try:
                    await application.bot.send_document(admin_id,io.BytesIO(data),filename=f"auto_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db",caption=f"🗄️ نسخة تلقائية — {stamp}")
                except Exception: pass
            try: os.remove(tmp)
            except Exception: pass
            log.info("Automatic database backup sent to admins")
        except Exception:
            log.warning("Automatic database backup failed",exc_info=True)
        await asyncio.sleep(interval_seconds)

async def on_storage_startup(application):
    storage=application.bot_data['storage']
    try:
        added=await storage.sync_repositories()
        log.info("Storage sync registered %d existing GitHub repos",added)
    except Exception:
        log.warning("Storage sync failed during startup",exc_info=True)
    try:
        connected,_=await storage.connection_status()
        application.bot_data['storage_online']=connected
    except Exception:
        application.bot_data['storage_online']=False
    asyncio.create_task(storage_watchdog(application))
    asyncio.create_task(database_backup_task(application))
    asyncio.create_task(dex_stale_sweeper(application))

def run():
    load_dotenv(); s=Settings.from_env(); db=Database(os.getenv("DATABASE_PATH","data.db")); db.initialize()
    with db.transaction() as c: c.execute("UPDATE storage_repositories SET max_bytes=?, safe_bytes=?",(s.storage_max_bytes,s.storage_safe_bytes))
    with db.transaction() as c:
        c.execute("INSERT INTO services(title,description,credits,is_active,created_by) SELECT 'حماية التطبيقات', 'حماية ملف APK وتحويل الكلاسات المحددة إلى Native C++.', 10, 1, 0 WHERE NOT EXISTS (SELECT 1 FROM services)")
    app=ApplicationBuilder().token(s.bot_token).post_init(on_storage_startup).build()
    app.bot_data.update(settings=s,service=ShopService(db),images=ImageService(s.imgbb_api_key),storage=StorageManager(db,GitHubStorage(s.github_token,s.github_owner),s))
    app.add_handler(CommandHandler("start",start)); app.add_handler(PreCheckoutQueryHandler(precheckout)); app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT,successful_payment)); app.add_handler(CallbackQueryHandler(callbacks)); app.add_handler(MessageHandler(filters.PHOTO,receive_product_photo)); app.add_handler(MessageHandler(filters.Document.ALL,receive_product_file)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,credit_amount)); app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=="__main__": run()
