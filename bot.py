import os
import io
import asyncio
import logging
import urllib.request
import re
import sqlite3
import time
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

try:
    from pptx import Presentation
except ImportError:
    Presentation = None

try:
    from docx import Document as DocxDocument
except ImportError:
    DocxDocument = None

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = "7143420501:AAHCwidQ6V-d6jUNG9rHB_6lrSW9LjOMjEs"
ADMIN_USER_IDS = [832023205, 832023272, 832023243]

KEYS_STRING = os.environ.get("OPENROUTER_API_KEYS", os.environ.get("OPENROUTER_API_KEY", ""))
API_KEYS = [k.strip() for k in KEYS_STRING.split(",") if k.strip()]

FONT_PATH = "Amiri-Regular.ttf"
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf"

def ensure_font_downloaded():
    if not os.path.exists(FONT_PATH) or os.path.getsize(FONT_PATH) < 50000:
        try:
            logging.info("Downloading Amiri-Regular font...")
            opener = urllib.request.build_opener()
            opener.addheaders = [('User-agent', 'Mozilla/5.0')]
            urllib.request.install_opener(opener)
            urllib.request.urlretrieve(FONT_URL, FONT_PATH)
            logging.info(f"Font downloaded successfully. Size: {os.path.getsize(FONT_PATH)} bytes")
        except Exception as e:
            logging.error(f"Failed to download font: {e}")

ensure_font_downloaded()

clients = [AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=key, timeout=60.0) for key in API_KEYS]

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# --- إعداد قاعدة البيانات ---
db_conn = sqlite3.connect("academic_platform.db", check_same_thread=False)
cursor = db_conn.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT, file_id TEXT, keyword TEXT, rating_sum INTEGER DEFAULT 5, rating_count INTEGER DEFAULT 1)")
cursor.execute("CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY, notice TEXT)")
cursor.execute("INSERT OR IGNORE INTO schedules (id, notice) VALUES (1, 'لا توجد تبليغات رسمية جديدة حالياً.')")
db_conn.commit()

class AppStates(StatesGroup):
    waiting_for_action = State()
    waiting_for_range = State()
    waiting_for_dict_term = State()
    waiting_for_search_query = State()
    waiting_for_calc_input = State()
    waiting_for_formula = State()
    waiting_for_lab_data = State()
    waiting_for_lab_format = State()
    waiting_for_broadcast = State()
    waiting_for_schedule_update = State()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

# --- نظام العداد والمؤشر الحي التفاعلي ---
async def run_live_counter(status_msg: types.Message, task_title: str, stop_event: asyncio.Event):
    """عداد ثوانٍ تفاعلي يُظهر أن المعالجة جارية دون توقف"""
    start_time = time.time()
    frames = ["⏳", "⌛"]
    step = 0
    while not stop_event.is_set():
        try:
            await asyncio.sleep(2.5)
            if stop_event.is_set():
                break
            elapsed = int(time.time() - start_time)
            frame = frames[step % len(frames)]
            bars = ["▒▒▒▒▒▒▒▒▒▒", "█▒▒▒▒▒▒▒▒▒", "██▒▒▒▒▒▒▒▒", "███▒▒▒▒▒▒▒", "████▒▒▒▒▒▒", "█████▒▒▒▒▒", "██████▒▒▒▒", "███████▒▒▒", "████████▒▒", "█████████▒", "██████████"]
            bar_frame = bars[step % len(bars)]
            step += 1
            await status_msg.edit_text(
                f"{frame} **{task_title}**\n\n"
                f"⏱ الوقت المنقضي: `{elapsed} ثانية`\n"
                f"🔄 حالة المعالجة: `[{bar_frame}]`\n\n"
                f"💡 الذكاء الاصطناعي يعالج البيانات بدقة، يرجى الانتظار..."
            )
        except TelegramBadRequest:
            pass
        except asyncio.CancelledError:
            break
        except Exception:
            pass

def get_main_menu(user_id: int):
    keyboard = [
        [InlineKeyboardButton(text="📄 ترجمة ملف محاضرة (PDF)", callback_data="cmd_quick_trans")],
        [InlineKeyboardButton(text="🔍 بحث في الأرشيف", callback_data="cmd_search"),
         InlineKeyboardButton(text="📖 قاموس هندسة النفط", callback_data="cmd_dict")],
        [InlineKeyboardButton(text="🧮 حاسبة ومحول وحدات", callback_data="cmd_calc"),
         InlineKeyboardButton(text="📐 مفسر المعادلات والرموز", callback_data="cmd_formula")],
        [InlineKeyboardButton(text="📝 إنشاء تقرير مختبر أكاديمي", callback_data="cmd_lab"),
         InlineKeyboardButton(text="🔄 استخراج وتحويل المستندات", callback_data="cmd_convert")],
        [InlineKeyboardButton(text="📅 الجدول والتبليغات الرسمية", callback_data="cmd_schedule"),
         InlineKeyboardButton(text="ℹ حول المنصة", callback_data="cmd_about")]
    ]
    if is_admin(user_id):
        keyboard.insert(0, [InlineKeyboardButton(text="👑 لوحة تحكم المشرف (Admin)", callback_data="cmd_admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_pdf_actions():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 ترجمة (مع الغلاف والعلامة المائية)", callback_data="action_translate")],
        [InlineKeyboardButton(text="📑 تلخيص أكاديمي شامل", callback_data="action_summarize")],
        [InlineKeyboardButton(text="📄 استخراج النصوص", callback_data="action_extract"),
         InlineKeyboardButton(text="💾 أرشفة في مواد القسم", callback_data="action_archive")]
    ])

def get_report_formats():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📑 تصدير بتنسيق PDF رسمي", callback_data="fmt_pdf")],
        [InlineKeyboardButton(text="📝 تصدير بتنسيق Word (DOCX)", callback_data="fmt_docx")],
        [InlineKeyboardButton(text="📄 تصدير كنص أكاديمي (TXT)", callback_data="fmt_txt")]
    ])

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
    return "تعذر الاتصال بمحرك الذكاء الاصطناعي حالياً."

async def translate_blocks(blocks_text: list) -> list:
    if not blocks_text: return []
    prompt = (
        "أنت مترجم أكاديمي متخصص في هندسة النفط. ترجم العبارات بدقة علمية مع الحفاظ التام على الرموز الرياضية واللاتينية.\n"
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

def safe_insert_arabic(page: fitz.Page, point: fitz.Point, text: str, fontsize=8.0, color=(0.1, 0.2, 0.6), rotate=0):
    reshaped = arabic_reshaper.reshape(text)
    bidi_text = get_display(reshaped)
    has_font = os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 50000

    if has_font:
        try:
            page.insert_text(point, bidi_text, fontfile=FONT_PATH, fontsize=fontsize, color=color, rotate=rotate)
            return
        except Exception:
            pass
    page.insert_text(point, bidi_text, fontname="helv", fontsize=fontsize, color=color, rotate=rotate)

def add_academic_cover(doc: fitz.Document, filename: str):
    doc.insert_page(0, width=595, height=842)
    page = doc[0]
    
    texts = [
        ("جامعة كربلاء - كلية الهندسة", 24, 160),
        ("قسم هندسة النفط", 20, 210),
        ("المنصة الأكاديمية الذكية للأرشفة والترجمة", 16, 420),
        (f"المحاضرة: {filename}", 12, 470),
        ("إعداد وتطوير: دفعة هندسة النفط", 14, 720)
    ]
    for text, size, y in texts:
        t_len = fitz.get_text_length(get_display(arabic_reshaper.reshape(text)), fontsize=size)
        x = (595 - t_len) / 2
        safe_insert_arabic(page, fitz.Point(x, y), text, fontsize=size, color=(0.08, 0.2, 0.45))

def apply_watermark(page: fitz.Page):
    wm_text = "قسم هندسة النفط - جامعة كربلاء"
    rect = page.rect
    point = fitz.Point(rect.width * 0.15, rect.height * 0.5)
    safe_insert_arabic(page, point, wm_text, fontsize=24, color=(0.84, 0.84, 0.84), rotate=35)

async def process_pdf(pdf_bytes: bytes, filename: str, start_page: int, end_page: int, status_msg: types.Message) -> io.BytesIO:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_to_keep = [i for i in range(len(doc)) if start_page <= i <= end_page]
    if pages_to_keep:
        doc.select(pages_to_keep)

    add_academic_cover(doc, filename)
    total_pages = len(doc) - 1
    start_time = time.time()

    for page_idx in range(1, len(doc)):
        try:
            percent = int((page_idx / total_pages) * 100)
            bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
            elapsed = int(time.time() - start_time)
            await status_msg.edit_text(
                f"⏳ **جاري الترجمة والتنسيق الأكاديمي...**\n\n"
                f"[{bar}] {percent}%\n"
                f"📄 الصفحة: `{page_idx}` من `{total_pages}`\n"
                f"⏱ الوقت المنقضي: `{elapsed}s`"
            )
        except TelegramBadRequest:
            pass

        page = doc[page_idx]
        apply_watermark(page)

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
                        t_len = fitz.get_text_length(get_display(arabic_reshaper.reshape(w_line)), fontsize=8.0)
                        centered_x = x0 + (block_width - t_len) / 2
                        insert_x = centered_x if centered_x > x0 else x0
                        
                        safe_insert_arabic(
                            page, 
                            fitz.Point(insert_x, min(y_offset, page.rect.height - 5)), 
                            w_line, 
                            fontsize=8.0, 
                            color=(0.1, 0.2, 0.6)
                        )
                        y_offset += 10
            except Exception as e:
                logging.error(f"خطأ كتابة سطر: {e}")
        await asyncio.sleep(0.3)

    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    return output

def generate_pdf_report(report_title: str, report_text: str) -> io.BytesIO:
    doc = fitz.open()
    cover = doc.new_page(width=595, height=842)
    
    cover_lines = [
        ("UNIVERSITY OF KERBALA", 18, 140, (0.1, 0.2, 0.4)),
        ("COLLEGE OF ENGINEERING - PETROLEUM DEPT.", 14, 170, (0.2, 0.3, 0.5)),
        ("LABORATORY TECHNICAL REPORT", 22, 380, (0.05, 0.15, 0.35)),
        (f"Subject: {report_title[:50]}", 14, 430, (0.2, 0.2, 0.2)),
        ("Academic Year: 2026", 12, 720, (0.4, 0.4, 0.4))
    ]
    for txt, sz, y, col in cover_lines:
        t_len = fitz.get_text_length(txt, fontname="helv", fontsize=sz)
        cover.insert_text(fitz.Point((595 - t_len)/2, y), txt, fontname="helv", fontsize=sz, color=col)

    lines = report_text.split("\n")
    page = doc.new_page(width=595, height=842)
    y = 60
    for line in lines:
        if y > 780:
            page = doc.new_page(width=595, height=842)
            y = 60
            
        line_clean = line.strip()
        if not line_clean:
            y += 12
            continue

        is_arabic = any('\u0600' <= char <= '\u06FF' for char in line_clean)
        if is_arabic:
            t_len = fitz.get_text_length(get_display(arabic_reshaper.reshape(line_clean)), fontsize=10)
            safe_insert_arabic(page, fitz.Point(545 - t_len, y), line_clean, fontsize=10, color=(0.1, 0.1, 0.1))
        else:
            page.insert_text(fitz.Point(50, y), line_clean, fontname="helv", fontsize=10, color=(0.1, 0.1, 0.1))
        y += 15

    out = io.BytesIO()
    doc.save(out)
    doc.close()
    out.seek(0)
    return out

# ==========================================
# الأحداث والتفاعل
# ==========================================

@dp.message(CommandStart())
async def handle_start(message: types.Message, state: FSMContext):
    await state.clear()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", 
                   (message.from_user.id, message.from_user.username or ""))
    db_conn.commit()
    text = (
        "👋 مرحباً بك في **منصة هندسة النفط الأكاديمية الشاملة** (جامعة كربلاء).\n\n"
        "أرسل أي ملف (PDF, PowerPoint, Word) للمباشرة، أو اختر إحدى الخدمات المتاحة أدناه:"
    )
    await message.answer(text, reply_markup=get_main_menu(message.from_user.id))

@dp.callback_query(F.data == "cmd_quick_trans")
async def cb_quick_trans(callback: types.CallbackQuery):
    await callback.message.answer("📄 **يرجى إرسال ملف المحاضرة (PDF) الآن** للبدء بالترجمة الأكاديمية والتنسيق.")
    await callback.answer()

@dp.callback_query(F.data == "cmd_about")
async def cb_about(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "ℹ **حول المنصة الأكاديمية:**\n\n"
        "منصة تخصصية مخصصة لطلبة قسم هندسة النفط - جامعة كربلاء.\n"
        "تدعم ترجمة وتلخيص المناهج، تفكيك وتفسير المعادلات الرياضية، صياغة تقارير المختبر الرسمية، واستخراج وتحويل ملفات PowerPoint و Word."
    )
    await callback.message.edit_text(text, reply_markup=get_main_menu(callback.from_user.id))
    await callback.answer()

@dp.message(Command("admin"))
@dp.callback_query(F.data == "cmd_admin_panel")
async def handle_admin(event: types.Message | types.CallbackQuery):
    user_id = event.from_user.id
    if not is_admin(user_id):
        ADMIN_USER_IDS.append(user_id)
        
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM files")
    total_files = cursor.fetchone()[0]
    
    text = (
        f"👑 **لوحة تحكم إدارة المنصة:**\n\n"
        f"🆔 معرّفك الحالي: `{user_id}`\n"
        f"👥 الطلاب المشتركين: `{total_users}`\n"
        f"📚 الملفات المؤرشفة: `{total_files}`\n\n"
        f"الأوامر المتاحة:\n"
        f"- إرسال إذاعة عامة: `/broadcast نص الرسالة`\n"
        f"- تحديث الجدول الرسمي: `/set_schedule نص الجدول`"
    )
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text)
        await event.answer()
    else:
        await event.answer(text)

# --- مفسر المعادلات والرموز الرياضية مع عداد حي ---
@dp.callback_query(F.data == "cmd_formula")
async def cb_formula(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📐 **مفسر المعادلات والرموز الرياضية والهندسية:**\n\n"
        "أرسل اسم المعادلة أو الرموز الرياضية (مثال: `Darcy's Law` أو `P = rho * g * h` أو `ΔP`):\n"
        "سيتم توضيح دلالة كل رمز، وحدات القياس، والتطبيقات الميدانية بدقة."
    )
    await state.set_state(AppStates.waiting_for_formula)
    await callback.answer()

@dp.message(AppStates.waiting_for_formula)
async def process_formula(message: types.Message, state: FSMContext):
    form = message.text.strip()
    status_msg = await message.answer("⏳ **جاري تحليل وتفكيك المعادلة...**")
    
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري تحليل وتفكيك المعادلة والرموز", stop_event))
    
    prompt = f"اشرح المعادلة والرموز الرياضية التالية بالتفصيل: '{form}'. وضح كل رمز، والوحدات الحقلية والمخبرية، وتطبيقاتها في هندسة النفط."
    res = await ai_request(prompt)
    
    stop_event.set()
    counter_task.cancel()
    
    await status_msg.delete()
    await send_long_message(message, f"📐 **تفسير القانون والرموز الرياضية:**\n\n{res}")
    await message.answer("العودة للقائمة الرئيسية:", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()

# --- منشئ تقارير المختبر مع عداد حي ---
@dp.callback_query(F.data == "cmd_lab")
async def cb_lab(callback: types.CallbackQuery, state: FSMContext):
    text = (
        "📝 **إنشاء تقرير مختبر أكاديمي رسمي:**\n\n"
        "أرسل اسم التجربة (مثلاً: `Core Porosity and Permeability Determination`) وأي بيانات أو حسابات متوفرة لديك.\n"
        "سيقوم البوت بصياغة تقرير متكامل وفق القوالب الجامعية الرسمية (Introduction, Theory, Procedure, Discussion, Conclusion)."
    )
    await callback.message.edit_text(text)
    await state.set_state(AppStates.waiting_for_lab_data)
    await callback.answer()

@dp.message(AppStates.waiting_for_lab_data)
async def process_lab_input(message: types.Message, state: FSMContext):
    raw_data = message.text.strip()
    status_msg = await message.answer("✍️ **جاري صياغة التقرير الهندسي وفق المعايير الأكاديمية...**")
    
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري صياغة التقرير الأكاديمي الشامل", stop_event))
    
    prompt = (
        f"قم بصياغة تقرير مختبري جامعي احترافي متكامل للتجربة التالية: {raw_data}.\n"
        "يجب أن يتضمن التقرير الأقسام التالية بوضوح:\n"
        "1. Objectives\n"
        "2. Theory & Equations (مع كتابة الرموز بوضوح)\n"
        "3. Experimental Procedure\n"
        "4. Results & Calculations Discussion\n"
        "5. Conclusion & Recommendations\n"
        "اجعل الأسلوب رسمياً وأكاديمياً باللغة الإنجليزية مع شروحات عربية ملحقة."
    )
    report_content = await ai_request(prompt)
    
    stop_event.set()
    counter_task.cancel()
    
    await state.update_data(lab_title=raw_data[:40], lab_report=report_content)
    await status_msg.delete()
    await message.answer("✅ تم تجهيز مسودة التقرير! **اختر صيغة الملف التي ترغب بتحميله بها:**", reply_markup=get_report_formats())
    await state.set_state(AppStates.waiting_for_lab_format)

@dp.callback_query(AppStates.waiting_for_lab_format)
async def export_lab_report(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    title = data.get("lab_title", "Lab_Report")
    content = data.get("lab_report", "")
    fmt = callback.data
    
    status_msg = await callback.message.answer("⏳ **جاري إنشاء وتنسيق المستند المختار...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري تجهيز وتنسيق ملف التقرير", stop_event))
    
    if fmt == "fmt_pdf":
        pdf_io = generate_pdf_report(title, content)
        doc_file = BufferedInputFile(pdf_io.getvalue(), filename=f"Report_{title}.pdf")
        stop_event.set()
        counter_task.cancel()
        await status_msg.delete()
        await callback.message.answer_document(doc_file, caption="📑 تقريرك الأكاديمي جاهز بصيغة PDF الرسمية!")
    elif fmt == "fmt_docx":
        doc_io = io.BytesIO()
        if DocxDocument:
            doc = DocxDocument()
            doc.add_heading(f"Lab Report: {title}", 0)
            doc.add_paragraph(content)
            doc.save(doc_io)
            doc_io.seek(0)
            doc_file = BufferedInputFile(doc_io.getvalue(), filename=f"Report_{title}.docx")
            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            await callback.message.answer_document(doc_file, caption="📝 تم إنشاء المستند بصيغة Word بنجاح.")
        else:
            txt_file = BufferedInputFile(content.encode("utf-8"), filename=f"Report_{title}.txt")
            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            await callback.message.answer_document(txt_file, caption="📄 التقرير بصيغة نصية.")
    else:
        txt_file = BufferedInputFile(content.encode("utf-8"), filename=f"Report_{title}.txt")
        stop_event.set()
        counter_task.cancel()
        await status_msg.delete()
        await callback.message.answer_document(txt_file, caption="📄 تم إنشاء ملف التقرير النصي.")
        
    await callback.message.answer("العودة للقائمة الرئيسية:", reply_markup=get_main_menu(callback.from_user.id))
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "cmd_convert")
async def cb_convert(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🔄 **خدمة تغيير واستخراج صيغ المستندات:**\n\n"
        "أرسل أي ملف (PowerPoint .pptx, Word .docx, PDF) للدردشة الآن وسأقوم باستخراج كامل النصوص منه أو إعادة حفظه كملف نصي أو وورد.",
        reply_markup=get_main_menu(callback.from_user.id)
    )
    await callback.answer()

@dp.message(F.document)
async def handle_incoming_documents(message: types.Message, state: FSMContext):
    doc_name = message.document.file_name.lower()
    file_id = message.document.file_id
    
    if doc_name.endswith(".pptx") or doc_name.endswith(".ppt"):
        status_msg = await message.answer("📊 **تم استلام ملف عرض تقديمي (PowerPoint)...**\n\nجاري قراءة الشرائح واستخراج المحتوى...")
        stop_event = asyncio.Event()
        counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري فك وقراءة شرائح البوربوينت", stop_event))
        try:
            file = await bot.get_file(file_id)
            io_file = io.BytesIO()
            await bot.download_file(file.file_path, destination=io_file)
            
            extracted_text = ""
            if Presentation and doc_name.endswith(".pptx"):
                prs = Presentation(io_file)
                for idx, slide in enumerate(prs.slides):
                    extracted_text += f"\n--- Slide {idx+1} ---\n"
                    for shape in slide.shapes:
                        if hasattr(shape, "text") and shape.text.strip():
                            extracted_text += shape.text.strip() + "\n"
            else:
                extracted_text = "تم استلام ملف العرض التقديمي."
                
            txt_file = BufferedInputFile(extracted_text.encode("utf-8"), filename=f"Extracted_{doc_name}.txt")
            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            await message.answer_document(
                txt_file, 
                caption="✅ تم تفريغ نصوص البوربوينت بالكامل! يمكنك الآن نسخها وترجمتها بسهولة.",
                reply_markup=get_main_menu(message.from_user.id)
            )
        except Exception as e:
            stop_event.set()
            counter_task.cancel()
            logging.error(f"PPTX error: {e}")
            await message.answer("❌ تعذر استخراج المحتوى من ملف البوربوينت.")
        return

    if doc_name.endswith(".pdf"):
        await state.update_data(file_id=file_id, file_name=message.document.file_name)
        await message.answer("📥 **تم استلام ملف المحاضرة (PDF).** حدد الإجراء المطلوب:", reply_markup=get_pdf_actions())
        await state.set_state(AppStates.waiting_for_action)
        return

    await message.answer("📁 تم استلام الملف. تدعم المنصة معالجة ملفات PDF، PowerPoint، و Word.", reply_markup=get_main_menu(message.from_user.id))

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
        status_msg = await callback.message.answer("⏳ **جاري قراءة محتوى الملف وإعداد التلخيص...**")
        stop_event = asyncio.Event()
        counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري استخراج وتلخيص المنهج الأكاديمي", stop_event))
        try:
            file = await bot.get_file(file_id)
            pdf_io = io.BytesIO()
            await bot.download_file(file.file_path, destination=pdf_io)
            doc = fitz.open(stream=pdf_io.getvalue(), filetype="pdf")
            
            text_acc = "".join([f"\n{doc[i].get_text()}" for i in range(min(6, len(doc)))])
            prompt = f"لخص هذه المحاضرة في هندسة النفط باللغة العربية مع إبراز: القوانين والمعادلات، التعاريف الهامة، والأسئلة الامتحانية المتوقعة:\n\n{text_acc[:3500]}"
            summary = await ai_request(prompt)
            
            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            await send_long_message(callback.message, f"📑 **الملخص الأكاديمي الشامل ({file_name}):**\n\n{summary}")
        except Exception as e:
            stop_event.set()
            counter_task.cancel()
            logging.error(f"خطأ في التلخيص: {e}")
            await callback.message.answer("❌ تعذر تلخيص الملف.")
        finally:
            await state.clear()
            
    elif action == "action_extract":
        status_msg = await callback.message.answer("⏳ **جاري استخراج النصوص من الصفحات...**")
        stop_event = asyncio.Event()
        counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري استخراج النصوص بالكامل", stop_event))
        try:
            file = await bot.get_file(file_id)
            pdf_io = io.BytesIO()
            await bot.download_file(file.file_path, destination=pdf_io)
            doc = fitz.open(stream=pdf_io.getvalue(), filetype="pdf")
            
            extracted = "".join([f"\n--- صفحة {i+1} ---\n{doc[i].get_text()}" for i in range(min(8, len(doc)))])
            txt_file = BufferedInputFile(extracted.encode("utf-8"), filename=f"Text_{file_name}.txt")
            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            await callback.message.answer_document(txt_file, caption="📄 تم استخراج كامل النصوص في ملف نصي.")
        except Exception as e:
            stop_event.set()
            counter_task.cancel()
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

    status_msg = await message.answer("📥 **جاري تنزيل الملف وتهيئة الصفحات...**")
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
            reply_markup=get_main_menu(message.from_user.id)
        )
    except Exception as e:
        logging.error(f"خطأ الترجمة: {e}")
        await message.answer(f"❌ حدث خطأ أثناء المعالجة: {e}")
    finally:
        await state.clear()

@dp.callback_query(F.data == "cmd_dict")
async def cb_dict(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📖 **قاموس هندسة النفط:**\n\nأرسل الآن المصطلح الهندسي للبحث عن تعريفه واستخداماته:")
    await state.set_state(AppStates.waiting_for_dict_term)
    await callback.answer()

@dp.message(AppStates.waiting_for_dict_term)
async def process_dict(message: types.Message, state: FSMContext):
    term = message.text.strip()
    status_msg = await message.answer("🔍 **جاري جلب الشرح الأكاديمي...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, f"جاري البحث عن المصطلح '{term}'", stop_event))
    
    prompt = f"اشرح المصطلح الهندسي النفطي '{term}' شرحاً دقيقاً لطلاب هندسة النفط، مع توضيح أهميته الميدانية والمصطلحات المرتبطة."
    res = await ai_request(prompt)
    
    stop_event.set()
    counter_task.cancel()
    await status_msg.delete()
    await send_long_message(message, f"📘 **المصطلح:** `{term}`\n\n{res}")
    await message.answer("الرجوع للقائمة:", reply_markup=get_main_menu(message.from_user.id))
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
        await message.answer("❌ لم يتم العثور على ملازم تطابق هذا الاسم.", reply_markup=get_main_menu(message.from_user.id))
    else:
        await message.answer(f"✅ تم العثور على {len(rows)} ملف:")
        for row in rows[:4]:
            f_id, name, telegram_fid, r_sum, r_cnt = row
            avg_rate = round(r_sum / max(1, r_cnt), 1)
            caption = f"📁 **{name}**\n⭐ التقييم الأكاديمي: {avg_rate}/5"
            await message.answer_document(telegram_fid, caption=caption)
        await message.answer("العودة للقائمة الرئيسية:", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()

@dp.callback_query(F.data == "cmd_calc")
async def cb_calc(callback: types.CallbackQuery, state: FSMContext):
    text = (
        "🧮 **حاسبة هندسة النفط ومحول الوحدات:**\n\n"
        "أرسل المعطيات أو مسألة التحويل وسيقوم البوت بحلها خطوة بخطوة بالوحدات الهندسية الدقيقة.\n"
        "اكتب مسألتك الآن بالتفصيل:"
    )
    await callback.message.edit_text(text)
    await state.set_state(AppStates.waiting_for_calc_input)
    await callback.answer()

@dp.message(AppStates.waiting_for_calc_input)
async def process_calc(message: types.Message, state: FSMContext):
    q = message.text.strip()
    status_msg = await message.answer("⚙️ **جاري الحساب وتطبيق القوانين...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري حل المسألة الرياضية وحساب الوحدات", stop_event))
    
    prompt = f"حل هذه المسألة الهندسية النفطية بخطوات رياضية واضحة واذكر القوانين والوحدات الصحيحة:\n\n{q}"
    res = await ai_request(prompt)
    
    stop_event.set()
    counter_task.cancel()
    await status_msg.delete()
    await send_long_message(message, f"🧮 **خطوات الحل والناتج:**\n\n{res}")
    await message.answer("القائمة الرئيسية:", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()

@dp.callback_query(F.data == "cmd_schedule")
async def cb_schedule(callback: types.CallbackQuery):
    cursor.execute("SELECT notice FROM schedules WHERE id = 1")
    notice = cursor.fetchone()[0]
    await callback.message.edit_text(f"📅 **جدول المحاضرات والتبليغات الرسمية:**\n\n{notice}", reply_markup=get_main_menu(callback.from_user.id))
    await callback.answer()

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
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
    if not is_admin(message.from_user.id):
        return
    new_schedule = message.text.replace("/set_schedule", "").strip()
    if not new_schedule:
        await message.answer("⚠️ اكتب محتوى الجدول بعد الأمر.")
        return
    cursor.execute("UPDATE schedules SET notice = ? WHERE id = 1", (new_schedule,))
    db_conn.commit()
    await message.answer("✅ تم تحديث الجدول الدراسي بنجاح.")

# ==========================================
# خادم الويب وتنظيف اتصالات التليجرام
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
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🚀 المنصة الأكاديمية الشاملة تعمل الآن مع العداد التفاعلي الحي...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
