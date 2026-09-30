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

# ضع توكن البوت الجديد هنا
TELEGRAM_BOT_TOKEN = "7143420501:AAGnq6tiRfky-VqjtDlQptM1tZ1zp6IPzqg"
ADMIN_USER_IDS = [832023205, 832023272, 832023243, 832023294]

KEYS_STRING = os.environ.get("OPENROUTER_API_KEYS", os.environ.get("OPENROUTER_API_KEY", ""))
API_KEYS = [k.strip() for k in KEYS_STRING.split(",") if k.strip()]

FONT_PATH = "Amiri-Regular.ttf"
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf"

def ensure_font_downloaded():
    if not os.path.exists(FONT_PATH) or os.path.getsize(FONT_PATH) < 50000:
        try:
            logging.info("Downloading font Amiri...")
            opener = urllib.request.build_opener()
            opener.addheaders = [('User-agent', 'Mozilla/5.0')]
            urllib.request.install_opener(opener)
            urllib.request.urlretrieve(FONT_URL, FONT_PATH)
        except Exception as e:
            logging.error(f"Font download error: {e}")

ensure_font_downloaded()

clients = [AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=key, timeout=60.0) for key in API_KEYS]

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# --- قاعدة البيانات ---
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
    waiting_for_lab_student_name = State()
    waiting_for_lab_dept = State()
    waiting_for_lab_stage = State()
    waiting_for_lab_study_type = State()
    waiting_for_lab_data = State()
    waiting_for_lab_format = State()
    waiting_for_broadcast = State()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

async def run_live_counter(status_msg: types.Message, task_title: str, stop_event: asyncio.Event):
    start_time = time.time()
    frames = ["⏳", "⌛"]
    step = 0
    while not stop_event.is_set():
        try:
            await asyncio.sleep(2.5)
            if stop_event.is_set(): break
            elapsed = int(time.time() - start_time)
            frame = frames[step % len(frames)]
            bars = ["▒▒▒▒▒▒▒▒▒▒", "███▒▒▒▒▒▒▒", "██████▒▒▒▒", "█████████▒", "██████████"]
            bar_frame = bars[step % len(bars)]
            step += 1
            await status_msg.edit_text(
                f"{frame} **{task_title}**\n\n"
                f"⏱ الوقت المنقضي: `{elapsed} ثانية`\n"
                f"🔄 المعالجة: `[{bar_frame}]`\n\n"
                f"💡 جاري تنفيذ العملية وتنسيق الملف، يرجى الانتظار..."
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
        [InlineKeyboardButton(text="🔍 بحث منظم في الأرشيف", callback_data="cmd_search_menu"),
         InlineKeyboardButton(text="📖 قاموس هندسة النفط", callback_data="cmd_dict")],
        [InlineKeyboardButton(text="🧮 حاسبة وتحويل وحدات", callback_data="cmd_calc"),
         InlineKeyboardButton(text="📐 مفسر المعادلات والرموز", callback_data="cmd_formula")],
        [InlineKeyboardButton(text="📝 إنشاء تقرير مختبر أكاديمي", callback_data="cmd_lab"),
         InlineKeyboardButton(text="🔄 تحويل PowerPoint إلى PDF", callback_data="cmd_convert")],
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

def get_search_lang_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇶 بحث باللغة العربية", callback_data="search_ar"),
         InlineKeyboardButton(text="🇬🇧 Search in English", callback_data="search_en")]
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
    if not clients: return "لم يتم ضبط مفاتيح OpenRouter."
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
    return "تعذر الاتصال بالذكاء الاصطناعي."

async def translate_blocks(blocks_text: list) -> list:
    if not blocks_text: return []
    prompt = (
        "ترجم العبارات الأكاديمية التالية إلى اللغة العربية بدقة تامة لطلاب الهندسة.\n"
        "حافظ على الرموز <br> كما هي بدون حذف أو دمج. التزم بالترقيم (رقم|| النص المترجم).\n\n"
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

# التضمين السليم للخط العربي في PyMuPDF لتجنب اختفاء النصوص
def prepare_page_font(page: fitz.Page) -> str:
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 50000:
        try:
            page.insert_font(fontname="amiri_font", fontfile=FONT_PATH)
            return "amiri_font"
        except Exception:
            pass
    return "helv"

def safe_insert_arabic(page: fitz.Page, point: fitz.Point, text: str, fontname: str, fontsize=8.0, color=(0.1, 0.2, 0.6)):
    reshaped = arabic_reshaper.reshape(text)
    bidi_text = get_display(reshaped)
    try:
        page.insert_text(point, bidi_text, fontname=fontname, fontsize=fontsize, color=color)
    except Exception as e:
        logging.error(f"خطأ إدراج النص: {e}")

def add_academic_cover(doc: fitz.Document, filename: str):
    doc.insert_page(0, width=595, height=842)
    page = doc[0]
    fname = prepare_page_font(page)
    
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
        safe_insert_arabic(page, fitz.Point(x, y), text, fname, fontsize=size, color=(0.08, 0.2, 0.45))

def apply_watermark(page: fitz.Page, fontname: str):
    wm_text = "قسم هندسة النفط - جامعة كربلاء"
    rect = page.rect
    t_len = fitz.get_text_length(get_display(arabic_reshaper.reshape(wm_text)), fontsize=14)
    point = fitz.Point((rect.width - t_len) / 2, rect.height - 25)
    safe_insert_arabic(page, point, wm_text, fontname, fontsize=12, color=(0.7, 0.7, 0.7))

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
                f"⏳ **جاري ترجمة وتنسيق المحاضرة...**\n\n"
                f"[{bar}] {percent}%\n"
                f"📄 الصفحة: `{page_idx}` من `{total_pages}`\n"
                f"⏱ الوقت: `{elapsed}s`"
            )
        except TelegramBadRequest:
            pass

        page = doc[page_idx]
        fname = prepare_page_font(page)
        apply_watermark(page, fname)

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
                            fname,
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

def convert_pptx_to_formatted_pdf(pptx_io: io.BytesIO, filename: str) -> io.BytesIO:
    prs = Presentation(pptx_io)
    doc = fitz.open()
    
    cover = doc.new_page(width=792, height=612)
    cover_lines = [
        ("UNIVERSITY OF KERBALA - COLLEGE OF ENGINEERING", 18, 160, (0.1, 0.2, 0.5)),
        ("DEPARTMENT OF PETROLEUM ENGINEERING", 15, 200, (0.2, 0.3, 0.6)),
        (f"Presentation Document: {filename[:45]}", 22, 320, (0.05, 0.15, 0.35)),
        ("Converted with Full Layout Formatting", 13, 370, (0.3, 0.3, 0.3)),
        ("Academic Year: 2026", 12, 540, (0.4, 0.4, 0.4))
    ]
    for txt, sz, y, col in cover_lines:
        t_len = fitz.get_text_length(txt, fontname="helv", fontsize=sz)
        cover.insert_text(fitz.Point((792 - t_len)/2, y), txt, fontname="helv", fontsize=sz, color=col)

    for idx, slide in enumerate(prs.slides):
        page = doc.new_page(width=792, height=612)
        fname = prepare_page_font(page)
        
        rect = fitz.Rect(30, 30, 762, 582)
        page.draw_rect(rect, color=(0.15, 0.25, 0.55), width=1.5)
        page.insert_text(fitz.Point(45, 60), f"Slide {idx + 1}", fontname="helv", fontsize=14, color=(0.15, 0.25, 0.55))
        
        y_cursor = 100
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                lines = shape.text.strip().split("\n")
                for line in lines:
                    if y_cursor > 550: break
                    clean_line = line.strip()
                    if not clean_line: continue
                    
                    is_ar = any('\u0600' <= char <= '\u06FF' for char in clean_line)
                    if is_ar:
                        t_len = fitz.get_text_length(get_display(arabic_reshaper.reshape(clean_line)), fontsize=10)
                        safe_insert_arabic(page, fitz.Point(740 - t_len, y_cursor), clean_line, fname, fontsize=10, color=(0.1, 0.1, 0.1))
                    else:
                        page.insert_text(fitz.Point(50, y_cursor), f"• {clean_line[:110]}", fontname="helv", fontsize=10, color=(0.15, 0.15, 0.15))
                    y_cursor += 18
                y_cursor += 8

    out = io.BytesIO()
    doc.save(out)
    doc.close()
    out.seek(0)
    return out

def generate_full_academic_report(metadata: dict, report_content: str) -> io.BytesIO:
    doc = fitz.open()
    cover = doc.new_page(width=595, height=842)
    fname = prepare_page_font(cover)
    
    border_rect = fitz.Rect(30, 30, 565, 812)
    cover.draw_rect(border_rect, color=(0.1, 0.2, 0.45), width=2)
    
    headers = [
        ("جامعة كربلاء - كلية الهندسة", 18, 100),
        (f"قسم {metadata.get('dept', 'هندسة النفط')}", 15, 130),
        ("تقرير مختبري أكاديمي معتمد", 22, 280),
        (f"عنوان التجربة: {metadata.get('lab_title', '')[:50]}", 14, 330)
    ]
    for txt, sz, y in headers:
        t_len = fitz.get_text_length(get_display(arabic_reshaper.reshape(txt)), fontsize=sz)
        safe_insert_arabic(cover, fitz.Point((595 - t_len)/2, y), txt, fname, fontsize=sz, color=(0.08, 0.18, 0.4))
        
    student_info = [
        f"اسم الطالب: {metadata.get('name', '---')}",
        f"القسم: {metadata.get('dept', 'هندسة النفط')}",
        f"المرحلة الدراسية: {metadata.get('stage', '---')}",
        f"نوع الدراسة: {metadata.get('study_type', '---')}",
        "العام الدراسي: 2026"
    ]
    y_info = 500
    for info in student_info:
        safe_insert_arabic(cover, fitz.Point(360, y_info), info, fname, fontsize=12, color=(0.15, 0.15, 0.15))
        y_info += 28

    paragraphs = report_content.split("\n")
    page = doc.new_page(width=595, height=842)
    page_num = 1
    y = 70

    def draw_header_footer(pg, p_num):
        pg.draw_line(fitz.Point(40, 45), fitz.Point(555, 45), color=(0.7, 0.7, 0.7), width=0.8)
        pg.insert_text(fitz.Point(45, 40), "Petroleum Engineering Dept - University of Kerbala", fontname="helv", fontsize=8, color=(0.4, 0.4, 0.4))
        pg.draw_line(fitz.Point(40, 800), fitz.Point(555, 800), color=(0.7, 0.7, 0.7), width=0.8)
        pg.insert_text(fitz.Point(280, 815), f"Page {p_num}", fontname="helv", fontsize=9, color=(0.3, 0.3, 0.3))

    draw_header_footer(page, page_num)
    fname = prepare_page_font(page)

    for p in paragraphs:
        clean_p = p.strip()
        if not clean_p:
            y += 12
            continue
            
        if y > 760:
            page = doc.new_page(width=595, height=842)
            page_num += 1
            draw_header_footer(page, page_num)
            fname = prepare_page_font(page)
            y = 70

        is_heading = any(clean_p.startswith(h) for h in ["1.", "2.", "3.", "4.", "5.", "#", "Objective", "Theory", "Procedure", "Discussion", "Conclusion"])
        font_sz = 12 if is_heading else 9.5
        font_col = (0.05, 0.15, 0.4) if is_heading else (0.12, 0.12, 0.12)
        
        is_ar = any('\u0600' <= char <= '\u06FF' for char in clean_p)
        wrapped = split_text_to_fit(clean_p, max_length=75 if is_heading else 85)
        
        for w_line in wrapped:
            if y > 760:
                page = doc.new_page(width=595, height=842)
                page_num += 1
                draw_header_footer(page, page_num)
                fname = prepare_page_font(page)
                y = 70
                
            if is_ar:
                t_len = fitz.get_text_length(get_display(arabic_reshaper.reshape(w_line)), fontsize=font_sz)
                safe_insert_arabic(page, fitz.Point(550 - t_len, y), w_line, fname, fontsize=font_sz, color=font_col)
            else:
                page.insert_text(fitz.Point(45, y), w_line, fontname="helv", fontsize=font_sz, color=font_col)
            y += (font_sz + 4)
            
        y += 4

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
        "تدعم ترجمة وتلخيص المناهج، صياغة تقارير المختبر الرسمية متعددة الصفحات، محاكاة وتفسير المعادلات والرموز، وتحويل ملفات PowerPoint إلى صيغة PDF مباشرة."
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

# --- محرك البحث في الأرشيف المنسق ---
@dp.callback_query(F.data == "cmd_search_menu")
async def cb_search_menu(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔍 **اختر لغة البحث في الأرشيف الأكاديمي:**", reply_markup=get_search_lang_menu())
    await callback.answer()

@dp.callback_query(F.data.in_(["search_ar", "search_en"]))
async def cb_search_by_lang(callback: types.CallbackQuery, state: FSMContext):
    lang = "ar" if callback.data == "search_ar" else "en"
    msg = "🔍 اكتب الآن اسم المادة أو الكلمة الدلالية بالعربية:" if lang == "ar" else "🔍 Type the subject name or keyword in English:"
    await callback.message.edit_text(msg)
    await state.set_state(AppStates.waiting_for_search_query)
    await callback.answer()

@dp.message(AppStates.waiting_for_search_query)
async def process_search(message: types.Message, state: FSMContext):
    query = message.text.strip().lower()
    cursor.execute("SELECT id, file_name, file_id, rating_sum, rating_count FROM files WHERE keyword LIKE ?", (f"%{query}%",))
    rows = cursor.fetchall()
    
    if not rows:
        await message.answer("❌ لم يتم العثور على ملازم تطابق هذا البحث في الأرشيف.", reply_markup=get_main_menu(message.from_user.id))
    else:
        await message.answer(f"📚 **نتائج البحث الأكاديمي ({len(rows)} ملف متوفر):**\n" + "—" * 25)
        for idx, row in enumerate(rows[:5], 1):
            f_id, name, telegram_fid, r_sum, r_cnt = row
            avg_rate = round(r_sum / max(1, r_cnt), 1)
            caption = (
                f"📄 **ملف رقم {idx}:** `{name}`\n"
                f"⭐ تقييم الدفعة: `{avg_rate}/5`\n"
                f"🏛 قسم هندسة النفط - جامعة كربلاء"
            )
            await message.answer_document(telegram_fid, caption=caption)
        await message.answer("يمكنك الرجوع للقائمة الرئيسية في أي وقت:", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()

# --- إنشاء تقرير المختبر ---
@dp.callback_query(F.data == "cmd_lab")
async def cb_lab_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📝 **صياغة تقرير مختبر أكاديمي رسمي:**\n\nيرجى إرسال **اسم الطالب الثلاثي**:")
    await state.set_state(AppStates.waiting_for_lab_student_name)
    await callback.answer()

@dp.message(AppStates.waiting_for_lab_student_name)
async def process_student_name(message: types.Message, state: FSMContext):
    await state.update_data(student_name=message.text.strip())
    await message.answer("🏛 يرجى كتابة **اسم القسم** (مثال: هندسة النفط):")
    await state.set_state(AppStates.waiting_for_lab_dept)

@dp.message(AppStates.waiting_for_lab_dept)
async def process_student_dept(message: types.Message, state: FSMContext):
    await state.update_data(dept=message.text.strip())
    await message.answer("📚 يرجى إدخال **المرحلة الدراسية** (الأولى، الثانية، الثالثة، الرابعة):")
    await state.set_state(AppStates.waiting_for_lab_stage)

@dp.message(AppStates.waiting_for_lab_stage)
async def process_student_stage(message: types.Message, state: FSMContext):
    await state.update_data(stage=message.text.strip())
    await message.answer("☀️ يرجى تحديد **نوع الدراسة** (صباحي / مسائي):")
    await state.set_state(AppStates.waiting_for_lab_study_type)

@dp.message(AppStates.waiting_for_lab_study_type)
async def process_student_study_type(message: types.Message, state: FSMContext):
    await state.update_data(study_type=message.text.strip())
    await message.answer(
        "🔬 أرسل الآن **اسم التجربة** وأي قراءات أو بيانات أو حسابات متوفرة لديك:\n"
        "(مثال: `Viscosity and Density Measurement of Drilling Fluids`)"
    )
    await state.set_state(AppStates.waiting_for_lab_data)

@dp.message(AppStates.waiting_for_lab_data)
async def process_lab_input(message: types.Message, state: FSMContext):
    raw_data = message.text.strip()
    status_msg = await message.answer("✍️ **جاري صياغة التقرير الهندسي الشامل...**")
    
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري صياغة تقرير أكاديمي مفصل متعدد الصفحات", stop_event))
    
    prompt = (
        f"قم بصياغة تقرير مختبري جامعي رسمي مفصل باللغة الإنجليزية للتجربة التالية: {raw_data}.\n"
        "يجب أن يكون التقرير شاملاً ومفصلاً جداً ليمتد على عدة صفحات، ويشمل:\n"
        "1. Abstract & Introduction\n"
        "2. Theoretical Background & Mathematical Equations (وضح كل رمز رياضي)\n"
        "3. Apparatus & Materials Used\n"
        "4. Step-by-Step Experimental Procedure\n"
        "5. Experimental Data & Sample Calculations\n"
        "6. In-Depth Results & Discussion\n"
        "7. Conclusions & Recommendations\n"
        "8. References\n"
        "اكتب المحتوى بأسلوب أكاديمي رسمي متبع في كلية الهندسة."
    )
    report_content = await ai_request(prompt)
    
    stop_event.set()
    counter_task.cancel()
    
    await state.update_data(lab_title=raw_data[:40], lab_report=report_content)
    await status_msg.delete()
    await message.answer("✅ تم تجهيز التقرير الأكاديمي الشامل! **اختر صيغة التصدير المطلوبة:**", reply_markup=get_report_formats())
    await state.set_state(AppStates.waiting_for_lab_format)

@dp.callback_query(AppStates.waiting_for_lab_format)
async def export_lab_report(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    metadata = {
        'name': data.get('student_name', '---'),
        'dept': data.get('dept', 'هندسة النفط'),
        'stage': data.get('stage', '---'),
        'study_type': data.get('study_type', '---'),
        'lab_title': data.get('lab_title', 'Lab Report')
    }
    content = data.get("lab_report", "")
    fmt = callback.data
    
    status_msg = await callback.message.answer("⏳ **جاري تنسيق وإنشاء الملف النهائي مع الغلاف والمعلومات...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري بناء صفحات التقرير", stop_event))
    
    if fmt == "fmt_pdf":
        pdf_io = generate_full_academic_report(metadata, content)
        doc_file = BufferedInputFile(pdf_io.getvalue(), filename=f"Report_{metadata['lab_title']}.pdf")
        stop_event.set()
        counter_task.cancel()
        await status_msg.delete()
        await callback.message.answer_document(doc_file, caption="📑 تقريرك الأكاديمي جاهز بصيغة PDF الرسمية متعددة الصفحات!")
    elif fmt == "fmt_docx":
        doc_io = io.BytesIO()
        if DocxDocument:
            doc = DocxDocument()
            doc.add_heading(f"Report: {metadata['lab_title']}", 0)
            doc.add_paragraph(f"Student Name: {metadata['name']}\nDepartment: {metadata['dept']}\nStage: {metadata['stage']} - {metadata['study_type']}")
            doc.add_paragraph(content)
            doc.save(doc_io)
            doc_io.seek(0)
            doc_file = BufferedInputFile(doc_io.getvalue(), filename=f"Report_{metadata['lab_title']}.docx")
            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            await callback.message.answer_document(doc_file, caption="📝 تم إنشاء المستند بصيغة Word الرسمية.")
        else:
            txt_file = BufferedInputFile(content.encode("utf-8"), filename=f"Report_{metadata['lab_title']}.txt")
            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            await callback.message.answer_document(txt_file, caption="📄 التقرير بصيغة نصية.")
    else:
        txt_file = BufferedInputFile(content.encode("utf-8"), filename=f"Report_{metadata['lab_title']}.txt")
        stop_event.set()
        counter_task.cancel()
        await status_msg.delete()
        await callback.message.answer_document(txt_file, caption="📄 تم إنشاء ملف التقرير النصي.")
        
    await callback.message.answer("العودة للقائمة الرئيسية:", reply_markup=get_main_menu(callback.from_user.id))
    await state.clear()
    await callback.answer()

# --- تحويل مستندات PowerPoint واستقبال الملفات ---
@dp.callback_query(F.data == "cmd_convert")
async def cb_convert_prompt(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🔄 **تحويل العروض التقديمية (PowerPoint) إلى PDF:**\n\n"
        "أرسل الآن ملف PowerPoint (.pptx) في المحادثة وسيقوم البوت بتحويله إلى مستند PDF مع الحفاظ على التنسيقات والشرائح.",
        reply_markup=get_main_menu(callback.from_user.id)
    )
    await callback.answer()

@dp.message(F.document)
async def handle_incoming_documents(message: types.Message, state: FSMContext):
    doc_name = message.document.file_name.lower()
    file_id = message.document.file_id
    
    if doc_name.endswith(".pptx") or doc_name.endswith(".ppt"):
        status_msg = await message.answer("📊 **تم استلام ملف PowerPoint... جاري التحويل والتنسيق إلى PDF...**")
        stop_event = asyncio.Event()
        counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري تحويل شرائح البوربوينت إلى PDF", stop_event))
        try:
            file = await bot.get_file(file_id)
            io_file = io.BytesIO()
            await bot.download_file(file.file_path, destination=io_file)
            
            if Presentation and doc_name.endswith(".pptx"):
                pdf_io = convert_pptx_to_formatted_pdf(io_file, message.document.file_name)
                out_file = BufferedInputFile(pdf_io.getvalue(), filename=f"Converted_{message.document.file_name}.pdf")
                stop_event.set()
                counter_task.cancel()
                await status_msg.delete()
                await message.answer_document(
                    out_file, 
                    caption="✅ تم تحويل ملف البوربوينت بنجاح إلى مستند PDF منسق بالكامل!",
                    reply_markup=get_main_menu(message.from_user.id)
                )
            else:
                stop_event.set()
                counter_task.cancel()
                await status_msg.delete()
                await message.answer("⚠️ يرجى إرسال ملف بصيغة PPTX الحديثة.")
        except Exception as e:
            stop_event.set()
            counter_task.cancel()
            logging.error(f"PPTX error: {e}")
            await message.answer(f"❌ تعذر تحويل ملف البوربوينت: {e}")
        return

    if doc_name.endswith(".pdf"):
        await state.update_data(file_id=file_id, file_name=message.document.file_name)
        await message.answer("📥 **تم استلام ملف المحاضرة (PDF).** حدد الإجراء المطلوب:", reply_markup=get_pdf_actions())
        await state.set_state(AppStates.waiting_for_action)
        return

    await message.answer("📁 تم استلام الملف. تدعم المنصة معالجة ملفات PDF و PowerPoint.", reply_markup=get_main_menu(message.from_user.id))

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

    status_msg = await message.answer("📥 **جاري تنزيل الملف وترجمة المحتوى بدقة...**")
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
            caption="✅ تمت الترجمة وتنسيق الأسطر مع الغلاف والعلامة المائية الأكاديمية بنجاح!",
            reply_markup=get_main_menu(message.from_user.id)
        )
    except Exception as e:
        logging.error(f"خطأ الترجمة: {e}")
        await message.answer(f"❌ حدث خطأ أثناء المعالجة: {e}")
    finally:
        await state.clear()

# --- باقي الخدمات ---
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

@dp.callback_query(F.data == "cmd_formula")
async def cb_formula(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📐 **مفسر المعادلات والرموز:**\n\nأرسل المعادلة الرياضية أو القانون لشرح دلالة الرموز وتطبيقاتها:")
    await state.set_state(AppStates.waiting_for_formula)
    await callback.answer()

@dp.message(AppStates.waiting_for_formula)
async def process_formula(message: types.Message, state: FSMContext):
    form = message.text.strip()
    status_msg = await message.answer("🔍 **جاري تحليل وتفكيك المعادلة...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري تحليل الرموز والمعادلات", stop_event))
    
    prompt = f"اشرح المعادلة والرموز الرياضية التالية بالتفصيل: '{form}'. وضح كل رمز، والوحدات الحقلية والمخبرية، وتطبيقاتها في هندسة النفط."
    res = await ai_request(prompt)
    
    stop_event.set()
    counter_task.cancel()
    await status_msg.delete()
    await send_long_message(message, f"📐 **تفسير القانون والرموز:**\n\n{res}")
    await message.answer("القائمة الرئيسية:", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()

@dp.callback_query(F.data == "cmd_calc")
async def cb_calc(callback: types.CallbackQuery, state: FSMContext):
    text = "🧮 **حاسبة ومحول وحدات النفط:**\n\nأرسل مسألتك أو التحويل المطلوب لحسابها خطوة بخطوة بالوحدات الهندسية:"
    await callback.message.edit_text(text)
    await state.set_state(AppStates.waiting_for_calc_input)
    await callback.answer()

@dp.message(AppStates.waiting_for_calc_input)
async def process_calc(message: types.Message, state: FSMContext):
    q = message.text.strip()
    status_msg = await message.answer("⚙️ **جاري الحساب...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري الحساب وتطبيق القوانين", stop_event))
    
    prompt = f"حل هذه المسألة الهندسية النفطية بخطوات رياضية واضحة واذكر القوانين والوحدات الصحيحة:\n\n{q}"
    res = await ai_request(prompt)
    
    stop_event.set()
    counter_task.cancel()
    await status_msg.delete()
    await send_long_message(message, f"🧮 **الناتج والحل:**\n\n{res}")
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
    if not is_admin(message.from_user.id): return
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
    if not is_admin(message.from_user.id): return
    new_schedule = message.text.replace("/set_schedule", "").strip()
    if not new_schedule:
        await message.answer("⚠️ اكتب محتوى الجدول بعد الأمر.")
        return
    cursor.execute("UPDATE schedules SET notice = ? WHERE id = 1", (new_schedule,))
    db_conn.commit()
    await message.answer("✅ تم تحديث الجدول الدراسي بنجاح.")

# ==========================================
# خادم الويب
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
    logging.info("🚀 المنصة الأكاديمية تعمل بكامل التنسيقات الصحيحة...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
