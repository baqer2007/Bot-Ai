import os
import io
import asyncio
import logging
import urllib.request
import re
import sqlite3
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from openai import AsyncOpenAI
import fitz  # PyMuPDF
import arabic_reshaper
from bidi.algorithm import get_display

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = "7143420501:AAHCwidQ6V-d6jUNG9rHB_6lrSW9LjOMjEs"
ADMIN_USER_ID = 832023205  # معرف الأدمن الافتراضي للإدارة

KEYS_STRING = os.environ.get("OPENROUTER_API_KEYS", os.environ.get("OPENROUTER_API_KEY", ""))
API_KEYS = [k.strip() for k in KEYS_STRING.split(",") if k.strip()]

FONT_PATH = "Amiri-Regular.ttf"
if not os.path.exists(FONT_PATH) or os.path.getsize(FONT_PATH) < 50000:
    try:
        logging.info("جاري تحميل الخط العربي الرسمي...")
        urllib.request.urlretrieve("https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf", FONT_PATH)
    except Exception as e:
        logging.error(f"خطأ أثناء تحميل الخط: {e}")

clients = [AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=key, timeout=60.0) for key in API_KEYS]

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# --- إعداد قاعدة البيانات الشاملة (SQLite) ---
db_conn = sqlite3.connect("academic_platform.db", check_same_thread=False)
cursor = db_conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT,
    file_id TEXT,
    keyword TEXT,
    downloads INTEGER DEFAULT 0,
    rating_sum INTEGER DEFAULT 5,
    rating_count INTEGER DEFAULT 1
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY,
    notice TEXT
)
""")
cursor.execute("INSERT OR IGNORE INTO schedules (id, notice) VALUES (1, 'لا توجد تبليغات رسمية جديدة حالياً.')")
db_conn.commit()

# --- إدارة الحالات (FSM) ---
class AppStates(StatesGroup):
    waiting_for_action = State()
    waiting_for_range = State()
    waiting_for_dict_term = State()
    waiting_for_search_query = State()
    waiting_for_calc_input = State()
    waiting_for_formula = State()
    waiting_for_lab_data = State()
    waiting_for_broadcast = State()
    waiting_for_schedule_update = State()

# --- القوائم التفاعلية ---
def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 بحث في الأرشيف الأكاديمي", callback_data="cmd_search"),
         InlineKeyboardButton(text="📖 قاموس هندسة النفط", callback_data="cmd_dict")],
        [InlineKeyboardButton(text="🧮 حاسبة ومحول وحدات النفط", callback_data="cmd_calc"),
         InlineKeyboardButton(text="📐 مفسر المعادلات", callback_data="cmd_formula")],
        [InlineKeyboardButton(text="📝 مساعد تقارير المختبر", callback_data="cmd_lab"),
         InlineKeyboardButton(text="📅 جدول المحاضرات والتبليغات", callback_data="cmd_schedule")],
        [InlineKeyboardButton(text="ℹ️ حول المنصة", callback_data="cmd_about")]
    ])

def get_pdf_actions():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 ترجمة (مع علامة مائية وغلاف)", callback_data="action_translate")],
        [InlineKeyboardButton(text="📑 تلخيص أكاديمي ذكي", callback_data="action_summarize"),
         InlineKeyboardButton(text="📄 استخراج النصوص", callback_data="action_extract")],
        [InlineKeyboardButton(text="💾 حفظ وتوثيق في الأرشيف", callback_data="action_archive")]
    ])

def get_rating_keyboard(file_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐ 1", callback_data=f"rate_{file_id}_1"),
            InlineKeyboardButton(text="⭐⭐ 2", callback_data=f"rate_{file_id}_2"),
            InlineKeyboardButton(text="⭐⭐⭐ 3", callback_data=f"rate_{file_id}_3"),
            InlineKeyboardButton(text="⭐⭐⭐⭐ 4", callback_data=f"rate_{file_id}_4"),
            InlineKeyboardButton(text="⭐⭐⭐⭐⭐ 5", callback_data=f"rate_{file_id}_5"),
        ]
    ])

# --- دوال المعالجة والذكاء الاصطناعي ---
async def send_long_message(msg: types.Message, text: str, parse_mode=None):
    if not text:
        await msg.answer("❌ لا يوجد محتوى لعرضه.")
        return
    for i in range(0, len(text), 4000):
        await msg.answer(text[i:i+4000], parse_mode=parse_mode)

async def ai_request(prompt: str) -> str:
    if not clients: return "لم يتم ضبط مفاتيح OpenRouter بنجاح."
    for client in clients:
        try:
            response = await client.chat.completions.create(
                model="openrouter/free",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            return response.choices[0].message.content or ""
        except Exception:
            continue
    return "تعذر الاتصال بمحرك الذكاء الاصطناعي حالياً، يرجى المحاولة لاحقاً."

async def translate_blocks(blocks_text: list) -> list:
    if not blocks_text: return []
    prompt = (
        "أنت مترجم أكاديمي متخصص في هندسة النفط. ترجم العبارات بدقة هندسية إلى العربية.\n"
        "حافظ على وسوم <br> ولا تمسحها. أعد النتيجة بنفس الترقيم (رقم|| النص).\n\n"
    )
    for i, text in enumerate(blocks_text):
        prompt += f"{i}|| {text}\n"
    
    content = await ai_request(prompt)
    translated_results = ["" for _ in range(len(blocks_text))]
    for line in content.split('\n'):
        if '||' in line:
            parts = line.split('||', 1)
            num_str = parts[0].strip()
            if num_str.isdigit() and 0 <= int(num_str) < len(blocks_text):
                translated_results[int(num_str)] = parts[1].strip()
    return translated_results

def add_academic_cover(doc: fitz.Document, filename: str):
    doc.insert_page(0, width=595, height=842)
    page = doc[0]
    font_key = "arab"
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 50000:
        page.insert_font(fontname=font_key, fontfile=FONT_PATH)
    else:
        font_key = "helv"
        
    texts = [
        ("جامعة كربلاء - كلية الهندسة", 24, 160),
        ("قسم هندسة النفط", 20, 210),
        ("المنصة الأكاديمية الذكية للأرشفة والترجمة", 16, 420),
        (f"المحاضرة: {filename}", 12, 470),
        ("إعداد وتطوير: دفعة هندسة النفط", 14, 720)
    ]
    
    for text, size, y in texts:
        reshaped = arabic_reshaper.reshape(text)
        bidi_text = get_display(reshaped)
        text_length = fitz.get_text_length(bidi_text, fontname=font_key, fontsize=size)
        x = (595 - text_length) / 2
        page.insert_text(fitz.Point(x, y), bidi_text, fontname=font_key, fontsize=size, color=(0.08, 0.2, 0.45))

def apply_watermark(page: fitz.Page, font_key: str):
    # علامة مائية شفافة ومائلة في منتصف الصفحة
    wm_text = "قسم هندسة النفط - جامعة كربلاء"
    reshaped = arabic_reshaper.reshape(wm_text)
    bidi_text = get_display(reshaped)
    rect = page.rect
    point = fitz.Point(rect.width * 0.15, rect.height * 0.5)
    try:
        page.insert_text(point, bidi_text, fontname=font_key, fontsize=24, color=(0.82, 0.82, 0.82), rotate=35)
    except Exception:
        pass

def split_text_to_fit(text, max_length=85):
    words = text.split()
    lines, current_line = [], ""
    for word in words:
        if len(current_line) + len(word) + 1 <= max_length:
            current_line += (word + " ")
        else:
            lines.append(current_line.strip())
            current_line = word + " "
    if current_line: lines.append(current_line.strip())
    return lines

async def process_pdf(pdf_bytes: bytes, filename: str, start_page: int, end_page: int, status_msg: types.Message) -> io.BytesIO:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_to_keep = [i for i in range(len(doc)) if start_page <= i <= end_page]
    if pages_to_keep:
        doc.select(pages_to_keep)

    add_academic_cover(doc, filename)
    total_pages = len(doc) - 1
    font_key = "arab"
    valid_font = os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 50000

    for page_idx in range(1, len(doc)):
        try:
            percent = int((page_idx / total_pages) * 100)
            bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
            if page_idx % 2 == 0 or page_idx == total_pages:
                await status_msg.edit_text(f"⏳ **جاري الترجمة والحفظ بالحقوق الأكاديمية...**\n\n[{bar}] {percent}%\nصفحة {page_idx} من {total_pages}")
        except TelegramBadRequest:
            pass

        page = doc[page_idx]
        if valid_font:
            page.insert_font(fontname=font_key, fontfile=FONT_PATH)
        else:
            font_key = "helv"

        apply_watermark(page, font_key)

        blocks = page.get_text("blocks")
        text_blocks, valid_coords = [], []
        
        for b in blocks:
            if b[6] == 0:
                txt = b[4].strip()
                if len(txt) > 5 and re.search('[a-zA-Z]{3,}', txt):
                    text_blocks.append(txt.replace("\n", " <br> "))
                    valid_coords.append((b[0], b[1], b[2], b[3]))

        if not text_blocks: continue
        translations = await translate_blocks(text_blocks)

        for i, coord in enumerate(valid_coords):
            if i >= len(translations): break
            ar_text = str(translations[i]).strip()
            if not ar_text or ar_text == text_blocks[i]: continue

            try:
                x0, y0, x1, y1 = coord
                y_offset = y1 + 5
                block_width = x1 - x0
                
                ar_segments = ar_text.split('<br>')
                for segment in ar_segments:
                    segment = segment.strip()
                    if not segment: continue
                    wrapped_lines = split_text_to_fit(segment, max_length=85)
                    for w_line in wrapped_lines:
                        reshaped = arabic_reshaper.reshape(w_line)
                        bidi_text = get_display(reshaped)
                        
                        t_len = fitz.get_text_length(bidi_text, fontname=font_key, fontsize=8.0)
                        centered_x = x0 + (block_width - t_len) / 2
                        insert_x = centered_x if centered_x > x0 else x0
                        
                        page.insert_text(fitz.Point(insert_x, min(y_offset, page.rect.height - 5)), 
                                         bidi_text, fontname=font_key, fontsize=8.0, color=(0.1, 0.2, 0.6))
                        y_offset += 10
            except Exception as e:
                logging.error(f"خطأ إدراج سطر: {e}")
        await asyncio.sleep(0.4)

    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    return output

# ==========================================
# الأحداث والقوائم التفاعلية
# ==========================================

@dp.message(CommandStart())
async def handle_start(message: types.Message, state: FSMContext):
    await state.clear()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", 
                   (message.from_user.id, message.from_user.username or ""))
    db_conn.commit()
    
    text = (
        "👋 مرحباً بك في **منصة هندسة النفط الأكاديمية الشاملة** (جامعة كربلاء).\n\n"
        "أرسل أي ملف محاضرة PDF للبدء المباشر، أو اختر من الخدمات المتقدمة أدناه:"
    )
    await message.answer(text, reply_markup=get_main_menu())

@dp.callback_query(F.data == "cmd_about")
async def cb_about(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "ℹ️️ **حول المنصة الأكاديمية:**\n\n"
        "مشروع تخصصي مطور لخدمة طلبة قسم هندسة النفط - جامعة كربلاء.\n"
        "يقدم أدوات ذكية لترجمة وتلخيص المناهج، حل وشرح المعادلات، صياغة تقارير المختبر، وأرشفة المواد الدراسية مع الحفاظ على حقوق القسم."
    )
    await callback.message.edit_text(text, reply_markup=get_main_menu())
    await callback.answer()

# --- القاموس ومحرك البحث ---
@dp.callback_query(F.data == "cmd_dict")
async def cb_dict(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📖 **قاموس هندسة النفط:**\n\nأرسل الآن المصطلح الهندسي للبحث عن تعريفه واستخداماته:")
    await state.set_state(AppStates.waiting_for_dict_term)
    await callback.answer()

@dp.message(AppStates.waiting_for_dict_term)
async def process_dict(message: types.Message, state: FSMContext):
    term = message.text.strip()
    msg = await message.answer("🔍 جاري جلب الشرح الأكاديمي...")
    prompt = f"اشرح المصطلح الهندسي النفطي '{term}' شرحاً دقيقاً لطلاب هندسة النفط، مع توضيح أهميته الميدانية والمصطلحات المرتبطة."
    res = await ai_request(prompt)
    await send_long_message(msg, f"📘 **المصطلح:** `{term}`\n\n{res}")
    await message.answer("الرجوع للقائمة:", reply_markup=get_main_menu())
    await state.clear()

@dp.callback_query(F.data == "cmd_search")
async def cb_search(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔍 **البحث في الأرشيف الجامعي:**\n\nاكتب اسم المادة أو جزء من اسم الملزمة:")
    await state.set_state(AppStates.waiting_for_search_query)
    await callback.answer()

@dp.message(AppStates.waiting_for_search_query)
async def process_search(message: types.Message, state: FSMContext):
    query = message.text.strip().lower()
    cursor.execute("SELECT id, file_name, file_id, rating_sum, rating_count FROM files WHERE keyword LIKE ?", (f"%{query}%",))
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("❌ لم يتم العثور على ملازم تطابق هذا الاسم.", reply_markup=get_main_menu())
    else:
        await message.answer(f"✅ تم العثور على {len(rows)} ملف. جاري التحميل...")
        for row in rows[:4]:
            f_id, name, telegram_fid, r_sum, r_cnt = row
            avg_rate = round(r_sum / max(1, r_cnt), 1)
            caption = f"📁 **{name}**\n⭐ التقييم الأكاديمي: {avg_rate}/5"
            await message.answer_document(telegram_fid, caption=caption, reply_markup=get_rating_keyboard(f_id))
        await message.answer("يمكنك تقييم جودة الملازم عبر النجوم أعلاه 👆", reply_markup=get_main_menu())
    await state.clear()

# --- تقييم الملازم ---
@dp.callback_query(F.data.startswith("rate_"))
async def handle_rating(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    file_record_id = int(parts[1])
    score = int(parts[2])
    
    cursor.execute("UPDATE files SET rating_sum = rating_sum + ?, rating_count = rating_count + 1 WHERE id = ?", (score, file_record_id))
    db_conn.commit()
    await callback.answer(f"تم تسجيل تقييمك ({score} نجوم). شكراً لدعمك!", show_alert=True)

# --- الحاسبة ومحول الوحدات ---
@dp.callback_query(F.data == "cmd_calc")
async def cb_calc(callback: types.CallbackQuery, state: FSMContext):
    text = (
        "🧮 **حاسبة هندسة النفط ومحول الوحدات:**\n\n"
        "أرسل المعطيات أو المسألة الحسابية أو التحويل الذي تريده، مثال:\n"
        "- `احسب الضغط الهيدروستاتيكي لعمق 8000 قدم بكثافة طين 10 ppg`\n"
        "- `حول 4500 psi إلى Bar`\n"
        "- `احسب API Gravity لكثافة نوعية 0.85`\n\n"
        "اكتب مسألتك الآن بالتفصيل:"
    )
    await callback.message.edit_text(text)
    await state.set_state(AppStates.waiting_for_calc_input)
    await callback.answer()

@dp.message(AppStates.waiting_for_calc_input)
async def process_calc(message: types.Message, state: FSMContext):
    q = message.text.strip()
    msg = await message.answer("⚙️ جاري الحساب وتطبيق القوانين...")
    prompt = f"حل هذه المسألة الهندسية النفطية بخطوات رياضية مفصلة واذكر القوانين المستخدمة والناتج النهائي بالوحدات الصحيحة:\n\n{q}"
    res = await ai_request(prompt)
    await send_long_message(msg, f"🧮 **خطوات الحل والناتج:**\n\n{res}")
    await message.answer("القائمة الرئيسية:", reply_markup=get_main_menu())
    await state.clear()

# --- مفسر المعادلات ---
@dp.callback_query(F.data == "cmd_formula")
async def cb_formula(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📐 **مفسر المعادلات وقوانين النفط:**\n\nاكتب اسم القانون أو اكتب صياغة المعادلة (مثال: `Darcy's Law` أو `P = rho * g * h`):")
    await state.set_state(AppStates.waiting_for_formula)
    await callback.answer()

@dp.message(AppStates.waiting_for_formula)
async def process_formula(message: types.Message, state: FSMContext):
    form = message.text.strip()
    msg = await message.answer("🔍 جاري تفكيك رموز المعادلة...")
    prompt = f"اشرح المعادلة التالية بالتفصيل: '{form}'. وضح دلالة كل رمز، وحدات القياس الحقلية والدولية (SI)، والفرضيات التطبيقية لها في هندسة النفط."
    res = await ai_request(prompt)
    await send_long_message(msg, f"📐 **تفسير القانون والرموز:**\n\n{res}")
    await message.answer("القائمة الرئيسية:", reply_markup=get_main_menu())
    await state.clear()

# --- مساعد تقارير المختبر ---
@dp.callback_query(F.data == "cmd_lab")
async def cb_lab(callback: types.CallbackQuery, state: FSMContext):
    text = (
        "📝 **مساعد كتابة تقارير المختبر الأكاديمية:**\n\n"
        "أرسل اسم التجربة (مثلاً: Core Porosity Measurement أو Flash Point Test) وأي معطيات أو نتائج تريد صياغتها، "
        "وسيقوم البوت بإنشاء مقدمة علمية، مناقشة نتائج (Discussion)، وخاتمة أكاديمية جاهزة."
    )
    await callback.message.edit_text(text)
    await state.set_state(AppStates.waiting_for_lab_data)
    await callback.answer()

@dp.message(AppStates.waiting_for_lab_data)
async def process_lab(message: types.Message, state: FSMContext):
    lab_info = message.text.strip()
    msg = await message.answer("✍️ جاري صياغة التقرير الهندسي...")
    prompt = f"قم بصياغة تقرير مختبري جامعي احترافي بالإنجليزية والعربية للتجربة التالية: {lab_info}. ركز على صياغة Introduction, Discussion, Conclusion بطريقة هندسية مقبولة في الجامعات."
    res = await ai_request(prompt)
    await send_long_message(msg, f"📑 **مسودة التقرير الأكاديمي:**\n\n{res}")
    await message.answer("القائمة الرئيسية:", reply_markup=get_main_menu())
    await state.clear()

# --- الجدول الدراسي والتبليغات ---
@dp.callback_query(F.data == "cmd_schedule")
async def cb_schedule(callback: types.CallbackQuery):
    cursor.execute("SELECT notice FROM schedules WHERE id = 1")
    notice = cursor.fetchone()[0]
    await callback.message.edit_text(f"📅 **جدول المحاضرات والتبليغات الرسمية:**\n\n{notice}", reply_markup=get_main_menu())
    await callback.answer()

# ==========================================
# معالجة ملفات المحاضرات (PDF)
# ==========================================

@dp.message(F.document)
async def handle_document(message: types.Message, state: FSMContext):
    if not message.document.file_name.lower().endswith(".pdf"):
        await message.answer("⚠️️ يرجى إرسال ملفات بصيغة PDF فقط.")
        return
    
    await state.update_data(file_id=message.document.file_id, file_name=message.document.file_name)
    await message.answer("📥 تم استلام المحاضرة. حدد الإجراء المطلوب:", reply_markup=get_pdf_actions())
    await state.set_state(AppStates.waiting_for_action)

@dp.callback_query(AppStates.waiting_for_action)
async def process_pdf_action(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data
    data = await state.get_data()
    file_id = data.get("file_id")
    file_name = data.get("file_name")
    
    if action == "action_archive":
        cursor.execute("INSERT INTO files (file_name, file_id, keyword) VALUES (?, ?, ?)", 
                      (file_name, file_id, file_name.lower()))
        db_conn.commit()
        await callback.message.edit_text("✅ تم أرشفة الملف بنجاح وإتاحته لكافة زملائك في البحث.")
        await state.clear()
        
    elif action == "action_translate":
        await callback.message.edit_text("📄 هل تريد ترجمة المحاضرة بالكامل أم صفحات معينة؟\n\n- أرسل كلمة `الكل` لترجمتها كاملة.\n- أو حدد الصفحات (مثال: `1-5`)")
        await state.set_state(AppStates.waiting_for_range)
        
    elif action == "action_summarize":
        await callback.message.edit_text("⏳ جاري استخراج المحتوى وتلخيصه استناداً لأهم قوانين ومفاهيم النفط...")
        try:
            file = await bot.get_file(file_id)
            pdf_io = io.BytesIO()
            await bot.download_file(file.file_path, destination=pdf_io)
            doc = fitz.open(stream=pdf_io.getvalue(), filetype="pdf")
            
            text_acc = ""
            for i in range(min(6, len(doc))):
                text_acc += f"\n{doc[i].get_text()}"
                
            prompt = f"لخص هذه المحاضرة في هندسة النفط باللغة العربية مع إبراز: القوانين الرئيسية، التعاريف الهامة، والأسئلة الامتحانية المتوقعة:\n\n{text_acc[:3500]}"
            summary = await ai_request(prompt)
            await send_long_message(callback.message, f"📑 **الملخص الهندسي الشامل ({file_name}):**\n\n{summary}")
        except Exception as e:
            logging.error(f"خطأ في التلخيص: {e}")
            await callback.message.answer("❌ تعذر تلخيص الملف.")
        finally:
            await state.clear()
            
    elif action == "action_extract":
        await callback.message.edit_text("⏳ جاري استخراج النصوص...")
        try:
            file = await bot.get_file(file_id)
            pdf_io = io.BytesIO()
            await bot.download_file(file.file_path, destination=pdf_io)
            doc = fitz.open(stream=pdf_io.getvalue(), filetype="pdf")
            
            extracted = "".join([f"\n--- صفحة {i+1} ---\n{doc[i].get_text()}" for i in range(min(5, len(doc)))])
            await send_long_message(callback.message, extracted[:3800])
        except Exception as e:
            logging.error(f"خطأ استخراج: {e}")
            await callback.message.answer("❌ تعذر استخراج النصوص.")
        finally:
            await state.clear()

@dp.message(AppStates.waiting_for_range)
async def run_translation(message: types.Message, state: FSMContext):
    data = await state.get_data()
    file_id = data.get("file_id")
    file_name = data.get("file_name")
    user_text = message.text.strip()
    
    start_p, end_p = 0, 9999
    if user_text != "الكل":
        try:
            parts = user_text.split("-")
            start_p = int(parts[0]) - 1
            end_p = int(parts[1]) - 1
        except Exception:
            await message.answer("❌ يرجى كتابة النطاق بشكل صحيح مثل 1-5 أو كلمة 'الكل'.")
            return

    status_msg = await message.answer("📥 جاري تهيئة المستند وتطبيق معايير القسم...")
    try:
        file = await bot.get_file(file_id)
        pdf_io = io.BytesIO()
        await bot.download_file(file.file_path, destination=pdf_io)
        pdf_bytes = pdf_io.getvalue()

        processed_pdf = await process_pdf(pdf_bytes, file_name, start_p, end_p, status_msg)

        out_name = f"مترجم_{file_name}"
        to_send = BufferedInputFile(processed_pdf.getvalue(), filename=out_name)

        await status_msg.delete()
        await message.answer_document(
            document=to_send, 
            caption="✅ تمت الترجمة بنجاح مع إضافة صفحة الغلاف الرسمية والعلامة المائية لحفظ الحقوق!",
            reply_markup=get_main_menu()
        )
    except Exception as e:
        logging.error(f"خطأ الترجمة: {e}")
        await message.answer("❌ حدث خطأ أثناء المعالجة.")
    finally:
        await state.clear()

# ==========================================
# لوحة تحكم المشرف (Admin Panel & Broadcast)
# ==========================================

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM files")
    total_files = cursor.fetchone()[0]
    
    text = (
        f"👑 **لوحة تحكم إدارة منصة هندسة النفط:**\n\n"
        f"👥 إجمالي الطلاب المشتركين: `{total_users}`\n"
        f"📚 إجمالي الملازم المؤرشفة: `{total_files}`\n\n"
        f"الأوامر المتاحة:\n"
        f"- لإرسال تبليغ رسمي للكل: اكتب `/broadcast نص التبليغ`\n"
        f"- لتحديث جدول المحاضرات: اكتب `/set_schedule نص الجدول الجديد`"
    )
    await message.answer(text)

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    broadcast_msg = message.text.replace("/broadcast", "").strip()
    if not broadcast_msg:
        await message.answer("⚠️ اكتب نص الإذاعة بعد الأمر.")
        return
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    sent = 0
    for (u_id,) in users:
        try:
            await bot.send_message(u_id, f"📢 **تبليغ رسمي من ممثلية القسم:**\n\n{broadcast_msg}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.answer(f"✅ تم إرسال الإذاعة بنجاح إلى {sent} طالب.")

@dp.message(Command("set_schedule"))
async def cmd_set_schedule(message: types.Message):
    if message.from_user.id != ADMIN_USER_ID:
        return
    new_schedule = message.text.replace("/set_schedule", "").strip()
    if not new_schedule:
        await message.answer("⚠️ اكتب محتوى الجدول بعد الأمر.")
        return
    cursor.execute("UPDATE schedules SET notice = ? WHERE id = 1", (new_schedule,))
    db_conn.commit()
    await message.answer("✅ تم تحديث الجدول الدراسي بنجاح.")

# ==========================================
# خادم الويب (Web Server لـ Render)
# ==========================================
async def handle_ping(request):
    return web.Response(text="Academic Bot Platform is Live!")

async def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await start_web_server()
    logging.info("🚀 المنصة الأكاديمية الشاملة تعمل الآن بكامل أدوات هندسة النفط...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
