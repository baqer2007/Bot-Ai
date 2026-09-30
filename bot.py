import os
import io
import asyncio
import logging
import urllib.request
import re
import sqlite3
import time
import aiohttp
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
    from pptx.enum.shapes import MSO_SHAPE_TYPE
except ImportError:
    Presentation = None
    MSO_SHAPE_TYPE = None

try:
    from docx import Document as DocxDocument
except ImportError:
    DocxDocument = None

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_USER_IDS = [832023205, 832023272, 832023243, 832023294, 832023401]

KEYS_STRING = os.environ.get("OPENROUTER_API_KEYS", os.environ.get("OPENROUTER_API_KEY", ""))
API_KEYS = [k.strip() for k in KEYS_STRING.split(",") if k.strip()]

FONT_PATH = "Amiri-Regular.ttf"
FONT_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf"

def ensure_font_downloaded():
    if not os.path.exists(FONT_PATH) or os.path.getsize(FONT_PATH) < 50000:
        try:
            logging.info("Downloading Amiri font...")
            opener = urllib.request.build_opener()
            opener.addheaders = [('User-agent', 'Mozilla/5.0')]
            urllib.request.install_opener(opener)
            urllib.request.urlretrieve(FONT_URL, FONT_PATH)
            logging.info("Amiri font downloaded successfully.")
        except Exception as e:
            logging.error(f"فشل تحميل الخط: {e}")

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
    waiting_for_search_limit = State()
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

def clean_math_text(text: str) -> str:
    if not text: return ""
    replacements = [
        (r'\\frac\{([^}]+)\}\{([^}]+)\}', r'(\1 / \2)'),
        (r'\\In\b', 'ln'),
        (r'\\ln\b', 'ln'),
        (r'\\log_\{10\}', 'log10'),
        (r'\\tag\{[^}]+\}', ''),
        (r'\\Delta\b', 'Δ'),
        (r'\\mu\b', 'μ'),
        (r'\\rho\b', 'ρ'),
        (r'\\phi\b', 'φ'),
        (r'\\pi\b', 'π'),
        (r'\\approx\b', '≈'),
        (r'\\times\b', '×'),
        (r'\\pm\b', '±'),
        (r'\\circ', '°'),
        (r'\^\{([^}]+)\}', r'^\1'),
        (r'_\{([^}]+)\}', r'_\1'),
        (r'\\[(\[\]\)]', ''),
        (r'\$', ''),
        (r'\\text\{([^}]+)\}', r'\1'),
    ]
    cleaned = text
    for pattern, repl in replacements:
        cleaned = re.sub(pattern, repl, cleaned)
    return cleaned

def format_arabic(text: str) -> str:
    """تشكيل وعكس النص العربي ليعرض بشكل طبيعي وسليم في PDF"""
    if not text: return ""
    reshaped = arabic_reshaper.reshape(clean_math_text(text))
    return get_display(reshaped)

def get_font_object():
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 50000:
        try:
            return fitz.Font(fontfile=FONT_PATH)
        except Exception:
            pass
    return fitz.Font("helv")

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
                f"🔄 المعالجة الأكاديمية: `[{bar_frame}]`\n\n"
                f"💡 جاري كتابة وضبط التنسيق الهندسي المعتمد..."
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
        [InlineKeyboardButton(text="🔍 بحث في الأرشيف الأكاديمي", callback_data="cmd_search_menu"),
         InlineKeyboardButton(text="📖 قاموس هندسة النفط", callback_data="cmd_dict")],
        [InlineKeyboardButton(text="🧮 حاسبة ومحول وحدات النفط", callback_data="cmd_calc"),
         InlineKeyboardButton(text="📐 مفسر المعادلات والرموز", callback_data="cmd_formula")],
        [InlineKeyboardButton(text="📝 إنشاء تقرير مختبر أكاديمي", callback_data="cmd_lab"),
         InlineKeyboardButton(text="🔄 تحويل PowerPoint إلى PDF كامل", callback_data="cmd_convert")],
        [InlineKeyboardButton(text="📅 الجدول والتبليغات الرسمية", callback_data="cmd_schedule"),
         InlineKeyboardButton(text="ℹ حول المنصة", callback_data="cmd_about")]
    ]
    if is_admin(user_id):
        keyboard.insert(0, [InlineKeyboardButton(text="👑 لوحة تحكم المشرف (Admin)", callback_data="cmd_admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_pdf_actions():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 ترجمة (بالصندوق الأزرق المتناسق)", callback_data="action_translate")],
        [InlineKeyboardButton(text="📑 تلخيص أكاديمي شامل", callback_data="action_summarize")],
        [InlineKeyboardButton(text="📄 استخراج النصوص", callback_data="action_extract"),
         InlineKeyboardButton(text="💾 أرشفة في مواد القسم", callback_data="action_archive")]
    ])

def get_search_lang_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇶 بحث باللغة العربية", callback_data="search_ar"),
         InlineKeyboardButton(text="🇬🇧 Search in English", callback_data="search_en")]
    ])

def get_search_limit_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="عرض 3 ملفات", callback_data="limit_3"),
         InlineKeyboardButton(text="عرض 5 ملفات", callback_data="limit_5"),
         InlineKeyboardButton(text="عرض 10 ملفات", callback_data="limit_10")]
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
        "ترجم العبارات الأكاديمية التالية إلى العربية بدقة علمية مخصصة لطلاب كلية الهندسة.\n"
        "حافظ على الرموز والمعادلات الرياضية. التزم بصيغة الترقيم بالضبط (رقم|| النص المترجم).\n\n"
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
                translated_results[int(num_str)] = clean_math_text(parts[1].strip())
    return translated_results

def split_text_to_fit(text, max_length=80):
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

def add_academic_cover(doc: fitz.Document, filename: str):
    doc.insert_page(0, width=595, height=842)
    page = doc[0]
    font_obj = get_font_object()
    
    border_rect = fitz.Rect(25, 25, 570, 817)
    page.draw_rect(border_rect, color=(0.1, 0.22, 0.45), width=2)
    
    texts = [
        ("جامعة كربلاء - كلية الهندسة", 22, 140),
        ("قسم هندسة النفط", 18, 180),
        ("الترجمة والتنسيق الأكاديمي المعتمد", 22, 380),
        (f"المحاضرة: {filename[:45]}", 13, 440),
        ("إعداد وتطوير: دفعة هندسة النفط - جامعة كربلاء", 14, 730)
    ]
    for text, size, y in texts:
        bidi_text = format_arabic(text)
        t_len = font_obj.text_length(bidi_text, fontsize=size)
        x = (595 - t_len) / 2
        page.insert_text(fitz.Point(x, y), bidi_text, fontfile=FONT_PATH, fontsize=size, color=(0.08, 0.2, 0.45))

# --- معالجة وترجمة الـ PDF عبر صناديق TextBox الآمنة ---
async def process_pdf(pdf_bytes: bytes, filename: str, start_page: int, end_page: int, status_msg: types.Message) -> io.BytesIO:
    src_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_to_keep = [i for i in range(len(src_doc)) if start_page <= i <= end_page]
    
    out_doc = fitz.open()
    add_academic_cover(out_doc, filename)
    
    total_pages = len(pages_to_keep)
    start_time = time.time()
    font_obj = get_font_object()

    for idx, page_num in enumerate(pages_to_keep, 1):
        try:
            percent = int((idx / max(1, total_pages)) * 100)
            bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
            elapsed = int(time.time() - start_time)
            await status_msg.edit_text(
                f"⏳ **جاري ترجمة وتنسيق المحاضرة أكاديمياً...**\n\n"
                f"[{bar}] {percent}%\n"
                f"📄 الصفحة: `{idx}` من `{total_pages}`\n"
                f"⏱ الوقت: `{elapsed}s`"
            )
        except TelegramBadRequest:
            pass

        src_page = src_doc[page_num]
        blocks = src_page.get_text("blocks")
        
        text_blocks = []
        for b in blocks:
            if b[6] == 0:
                txt = b[4].strip()
                if len(txt) > 3 and re.search('[a-zA-Z]{2,}', txt):
                    text_blocks.append(clean_math_text(txt.replace("\n", " ")))

        translations = await translate_blocks(text_blocks) if text_blocks else []
        
        new_page = out_doc.new_page(width=595, height=842)
        
        # الترويسة والتذييل
        new_page.draw_line(fitz.Point(40, 40), fitz.Point(555, 40), color=(0.7, 0.7, 0.7), width=0.8)
        new_page.insert_text(fitz.Point(45, 35), f"Petroleum Engineering Dept | Page {idx}", fontname="helv", fontsize=8, color=(0.4, 0.4, 0.4))
        new_page.draw_line(fitz.Point(40, 805), fitz.Point(555, 805), color=(0.7, 0.7, 0.7), width=0.8)
        
        wm_txt = format_arabic("قسم هندسة النفط - جامعة كربلاء")
        t_len = font_obj.text_length(wm_txt, fontsize=9)
        new_page.insert_text(fitz.Point((595 - t_len) / 2, 820), wm_txt, fontfile=FONT_PATH, fontsize=9, color=(0.6, 0.6, 0.6))

        y = 65
        t_idx = 0
        for b in blocks:
            if b[6] == 0:
                txt = clean_math_text(b[4].strip())
                if not txt: continue
                
                # طباعة النص الإنجليزي
                for en_line in split_text_to_fit(txt.replace("\n", " "), max_length=85):
                    if y > 760:
                        new_page = out_doc.new_page(width=595, height=842)
                        y = 65
                    new_page.insert_text(fitz.Point(45, y), en_line, fontname="helv", fontsize=9.0, color=(0.12, 0.12, 0.12))
                    y += 13

                # طباعة الترجمة العربية داخل صندوق TextBox آمن مع تظليل أزرق
                if t_idx < len(translations) and translations[t_idx]:
                    ar_raw = translations[t_idx].strip()
                    if ar_raw and ar_raw != txt:
                        ar_lines = split_text_to_fit(ar_raw, max_length=60)
                        box_height = len(ar_lines) * 14 + 10
                        
                        if y + box_height > 760:
                            new_page = out_doc.new_page(width=595, height=842)
                            y = 65
                            
                        # رسم صندوق التظليل الأزرق الشفاف
                        box_rect = fitz.Rect(45, y - 2, 545, y + box_height - 2)
                        new_page.draw_rect(box_rect, color=(0.82, 0.88, 0.96), fill=(0.94, 0.97, 1.0))
                        new_page.draw_line(fitz.Point(545, y - 2), fitz.Point(545, y + box_height - 2), color=(0.18, 0.38, 0.75), width=3.0)
                        
                        # إدراج كل سطر عربي بمكانه السليم داخل الصندوق
                        cur_y = y + 10
                        for a_l in ar_lines:
                            bidi_line = format_arabic(a_l)
                            line_len = font_obj.text_length(bidi_line, fontsize=8.5)
                            x_target = 535 - line_len  # محاذاة من اليمين بمسافة أمان داخل الصندوق
                            new_page.insert_text(fitz.Point(x_target, cur_y), bidi_line, fontfile=FONT_PATH, fontsize=8.5, color=(0.08, 0.22, 0.58))
                            cur_y += 14
                            
                        y += (box_height + 4)
                    t_idx += 1
                y += 6
        await asyncio.sleep(0.3)

    output = io.BytesIO()
    out_doc.save(output)
    out_doc.close()
    src_doc.close()
    output.seek(0)
    return output

# --- تحويل PowerPoint الشامل إلى PDF ---
def convert_pptx_to_formatted_pdf(pptx_io: io.BytesIO, filename: str) -> io.BytesIO:
    prs = Presentation(pptx_io)
    doc = fitz.open()
    font_obj = get_font_object()
    
    cover = doc.new_page(width=792, height=612)
    cover_lines = [
        ("UNIVERSITY OF KERBALA - COLLEGE OF ENGINEERING", 18, 160, (0.1, 0.2, 0.5)),
        ("DEPARTMENT OF PETROLEUM ENGINEERING", 15, 200, (0.2, 0.3, 0.6)),
        (f"Lecture Presentation: {filename[:45]}", 22, 320, (0.05, 0.15, 0.35)),
        ("Converted with Complete Deep Content Preservation", 13, 370, (0.3, 0.3, 0.3)),
        ("Academic Year: 2026", 12, 540, (0.4, 0.4, 0.4))
    ]
    for txt, sz, y, col in cover_lines:
        t_len = fitz.get_text_length(txt, fontname="helv", fontsize=sz)
        cover.insert_text(fitz.Point((792 - t_len)/2, y), txt, fontname="helv", fontsize=sz, color=col)

    for idx, slide in enumerate(prs.slides):
        page = doc.new_page(width=792, height=612)
        
        rect = fitz.Rect(30, 30, 762, 582)
        page.draw_rect(rect, color=(0.15, 0.25, 0.55), width=1.5)
        page.insert_text(fitz.Point(45, 60), f"Slide {idx + 1}", fontname="helv", fontsize=14, color=(0.15, 0.25, 0.55))
        
        y_cursor = 95
        
        def extract_recursive(shp):
            nonlocal y_cursor
            if y_cursor > 550: return
            
            if hasattr(shp, "shapes"):
                for sub in shp.shapes:
                    extract_recursive(sub)
                return

            if hasattr(shp, "text") and shp.text.strip():
                for line in shp.text.strip().split("\n"):
                    clean_line = clean_math_text(line.strip())
                    if not clean_line or y_cursor > 550: continue
                    is_ar = any('\u0600' <= char <= '\u06FF' for char in clean_line)
                    if is_ar:
                        b_txt = format_arabic(clean_line)
                        t_len = font_obj.text_length(b_txt, fontsize=9.5)
                        page.insert_text(fitz.Point(740 - t_len, y_cursor), b_txt, fontfile=FONT_PATH, fontsize=9.5, color=(0.1, 0.1, 0.1))
                    else:
                        page.insert_text(fitz.Point(50, y_cursor), f"• {clean_line[:105]}", fontname="helv", fontsize=9.5, color=(0.15, 0.15, 0.15))
                    y_cursor += 16
                y_cursor += 4
                
            if hasattr(shp, "has_table") and shp.has_table:
                for row in shp.table.rows:
                    row_txt = " | ".join([clean_math_text(cell.text.strip()) for cell in row.cells if cell.text.strip()])
                    if row_txt and y_cursor <= 550:
                        page.insert_text(fitz.Point(50, y_cursor), f"[Table Data] {row_txt[:100]}", fontname="helv", fontsize=8.5, color=(0.2, 0.3, 0.5))
                        y_cursor += 15

        for shape in slide.shapes:
            extract_recursive(shape)

    out = io.BytesIO()
    doc.save(out)
    doc.close()
    out.seek(0)
    return out

# --- بناء وتنسيق تقارير المختبر ---
def generate_full_academic_report(metadata: dict, report_content: str) -> io.BytesIO:
    doc = fitz.open()
    cover = doc.new_page(width=595, height=842)
    font_obj = get_font_object()
    
    border_rect = fitz.Rect(30, 30, 565, 812)
    cover.draw_rect(border_rect, color=(0.1, 0.2, 0.45), width=2)
    
    s_name = metadata.get('name') if metadata.get('name') and metadata.get('name') != '.' else "باقر رعد عباس"
    s_dept = metadata.get('dept') if metadata.get('dept') and metadata.get('dept') != '.' else "هندسة النفط"
    s_stage = metadata.get('stage') if metadata.get('stage') and metadata.get('stage') != '.' else "الثانية"
    s_study = metadata.get('study_type') if metadata.get('study_type') and metadata.get('study_type') != '.' else "مسائي"
    l_title = metadata.get('lab_title', 'Point VAP Experiment')
    
    headers = [
        ("جامعة كربلاء - كلية الهندسة", 18, 110),
        (f"قسم {s_dept}", 15, 140),
        ("تقرير مختبري أكاديمي معتمد", 22, 290),
        (f"عنوان التجربة: {l_title[:50]}", 14, 340)
    ]
    for txt, sz, y in headers:
        b_txt = format_arabic(txt)
        t_len = font_obj.text_length(b_txt, fontsize=sz)
        cover.insert_text(fitz.Point((595 - t_len)/2, y), b_txt, fontfile=FONT_PATH, fontsize=sz, color=(0.08, 0.18, 0.4))
        
    student_info = [
        f"اسم الطالب: {s_name}",
        f"القسم: {s_dept}",
        f"المرحلة الدراسية: {s_stage}",
        f"نوع الدراسة: {s_study}",
        "العام الدراسي: 2026"
    ]
    y_info = 500
    for info in student_info:
        b_info = format_arabic(info)
        cover.insert_text(fitz.Point(360, y_info), b_info, fontfile=FONT_PATH, fontsize=12, color=(0.15, 0.15, 0.15))
        y_info += 28

    def create_content_page(p_num):
        pg = doc.new_page(width=595, height=842)
        pg.draw_line(fitz.Point(40, 45), fitz.Point(555, 45), color=(0.7, 0.7, 0.7), width=0.8)
        pg.insert_text(fitz.Point(45, 40), "Petroleum Engineering Dept - University of Kerbala", fontname="helv", fontsize=8, color=(0.4, 0.4, 0.4))
        pg.draw_line(fitz.Point(40, 800), fitz.Point(555, 800), color=(0.7, 0.7, 0.7), width=0.8)
        pg.insert_text(fitz.Point(280, 815), f"Page {p_num}", fontname="helv", fontsize=9, color=(0.3, 0.3, 0.3))
        return pg

    page_num = 1
    page = create_content_page(page_num)
    y = 70

    cleaned_content = clean_math_text(report_content)
    lines = cleaned_content.split("\n")
    
    in_table = False
    for line in lines:
        clean_l = line.strip()
        if not clean_l:
            y += 8
            continue

        if "|" in clean_l:
            if "---" in clean_l: continue
            cells = [c.strip() for c in clean_l.split("|") if c.strip()]
            if cells:
                if y > 760:
                    page_num += 1
                    page = create_content_page(page_num)
                    y = 70
                    
                col_width = 500 / max(1, len(cells))
                if not in_table:
                    in_table = True
                    page.draw_rect(fitz.Rect(45, y - 2, 550, y + 16), color=(0.75, 0.8, 0.9), fill=(0.90, 0.93, 0.98))
                    for col_idx, cell_txt in enumerate(cells):
                        page.insert_text(fitz.Point(50 + col_idx * col_width, y + 10), cell_txt[:25], fontname="helv", fontsize=9.0, color=(0.1, 0.2, 0.4))
                    y += 20
                else:
                    page.draw_line(fitz.Point(45, y + 14), fitz.Point(550, y + 14), color=(0.85, 0.85, 0.85), width=0.5)
                    for col_idx, cell_txt in enumerate(cells):
                        page.insert_text(fitz.Point(50 + col_idx * col_width, y + 10), cell_txt[:25], fontname="helv", fontsize=8.5, color=(0.15, 0.15, 0.15))
                    y += 18
                continue
        else:
            in_table = False

        is_heading = any(clean_l.startswith(h) for h in ["1.", "2.", "3.", "4.", "5.", "#", "Abstract", "Objective", "Theory", "Procedure", "Discussion", "Conclusion", "Reference", "Table"])
        font_sz = 11.0 if is_heading else 9.0
        font_col = (0.08, 0.18, 0.45) if is_heading else (0.15, 0.15, 0.15)
        
        is_ar = any('\u0600' <= char <= '\u06FF' for char in clean_l)
        wrapped = split_text_to_fit(clean_l, max_length=70 if is_heading else 85)
        
        for w_line in wrapped:
            if y > 760:
                page_num += 1
                page = create_content_page(page_num)
                y = 70
                
            if is_ar:
                b_line = format_arabic(w_line)
                t_len = font_obj.text_length(b_line, fontsize=font_sz)
                page.insert_text(fitz.Point(545 - t_len, y), b_line, fontfile=FONT_PATH, fontsize=font_sz, color=font_col)
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
        "تدعم ترجمة وتلخيص المناهج، صياغة تقارير المختبر الرسمية، محاكاة وتفسير المعادلات والرموز، وتحويل ملفات PowerPoint إلى صيغة PDF مباشرة."
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

@dp.callback_query(F.data == "cmd_search_menu")
async def cb_search_menu(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔍 **اختر لغة البحث في الأرشيف الأكاديمي:**", reply_markup=get_search_lang_menu())
    await callback.answer()

@dp.callback_query(F.data.in_(["search_ar", "search_en"]))
async def cb_search_by_lang(callback: types.CallbackQuery, state: FSMContext):
    lang = "ar" if callback.data == "search_ar" else "en"
    await state.update_data(search_lang=lang)
    msg = "🔍 اكتب الآن اسم المادة أو الكلمة الدلالية بالعربية:" if lang == "ar" else "🔍 Type the subject name or keyword in English:"
    await callback.message.edit_text(msg)
    await state.set_state(AppStates.waiting_for_search_query)
    await callback.answer()

@dp.message(AppStates.waiting_for_search_query)
async def process_search_query(message: types.Message, state: FSMContext):
    await state.update_data(search_query=message.text.strip().lower())
    await message.answer("📄 **حدد عدد الملفات التي ترغب بعرضها في النتائج:**", reply_markup=get_search_limit_menu())
    await state.set_state(AppStates.waiting_for_search_limit)

@dp.callback_query(AppStates.waiting_for_search_limit)
async def process_search_with_limit(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    query = data.get("search_query", "")
    limit = int(callback.data.replace("limit_", ""))
    
    cursor.execute("SELECT id, file_name, file_id, rating_sum, rating_count FROM files WHERE keyword LIKE ? OR file_name LIKE ? LIMIT ?", 
                   (f"%{query}%", f"%{query}%", limit))
    rows = cursor.fetchall()
    
    if not rows:
        await callback.message.answer(
            f"❌ لم يتم العثور على ملازم تطابق '{query}'.\n"
            "💡 تأكد من حفظ وأرشفة الملف أولاً عبر خيار (أرشفة في مواد القسم) بعد رفع المحاضرة.",
            reply_markup=get_main_menu(callback.from_user.id)
        )
    else:
        await callback.message.answer(f"📚 **نتائج البحث الأكاديمي المنظمة ({len(rows)} ملف):**\n" + "—" * 28)
        for idx, row in enumerate(rows, 1):
            f_id, name, telegram_fid, r_sum, r_cnt = row
            avg_rate = round(r_sum / max(1, r_cnt), 1)
            caption = (
                f"📁 **المستند رقم {idx}:** `{name}`\n"
                f"⭐ التقييم المعتمد: `{avg_rate}/5`\n"
                f"🏛 قسم هندسة النفط - جامعة كربلاء"
            )
            await callback.message.answer_document(telegram_fid, caption=caption)
        await callback.message.answer("يمكنك الرجوع للقائمة الرئيسية في أي وقت:", reply_markup=get_main_menu(callback.from_user.id))
    await state.clear()
    await callback.answer()

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
    status_msg = await message.answer("✍️ **جاري صياغة التقرير الهندسي الشامل وتنظيم الجداول...**")
    
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري صياغة تقرير أكاديمي مفصل متعدد الصفحات", stop_event))
    
    prompt = (
        f"قم بصياغة تقرير مختبري جامعي رسمي مفصل باللغة الإنجليزية للتجربة التالية: {raw_data}.\n"
        "مهم جداً: اكتب المعادلات الرياضية بصيغة نصية واضحة وتجنب تماماً استخدام رموز LaTeX مثل \\frac و \\tag و $$ و [ ] لتكون مقروءة ومرتبة.\n"
        "نظم بيانات النتائج في جداول Markdown منسقة واضحة.\n"
        "يجب أن يكون التقرير شاملاً ومفصلاً جداً ليمتد على عدة صفحات، ويشمل:\n"
        "1. Abstract & Introduction\n"
        "2. Theoretical Background & Mathematical Equations (مع توضيح الرموز)\n"
        "3. Apparatus & Materials Used\n"
        "4. Step-by-Step Experimental Procedure\n"
        "5. Experimental Data & Sample Calculations (في جدول منسق)\n"
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
        'name': data.get('student_name') or "باقر رعد عباس",
        'dept': data.get('dept') or "هندسة النفط",
        'stage': data.get('stage') or "الثانية",
        'study_type': data.get('study_type') or "مسائي",
        'lab_title': data.get('lab_title', 'Point VAP Experiment')
    }
    content = data.get("lab_report", "")
    fmt = callback.data
    
    status_msg = await callback.message.answer("⏳ **جاري تنسيق وإنشاء الملف النهائي مع الغلاف والجداول...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري بناء صفحات التقرير المتعددة", stop_event))
    
    if fmt == "fmt_pdf":
        pdf_io = generate_full_academic_report(metadata, content)
        doc_file = BufferedInputFile(pdf_io.getvalue(), filename=f"Report_{metadata['lab_title']}.pdf")
        stop_event.set()
        counter_task.cancel()
        await status_msg.delete()
        await callback.message.answer_document(doc_file, caption="📑 تقريرك الأكاديمي جاهز بصيغة PDF الرسمية مع الجداول المنسقة!")
    elif fmt == "fmt_docx":
        doc_io = io.BytesIO()
        if DocxDocument:
            doc = DocxDocument()
            doc.add_heading(f"Report: {metadata['lab_title']}", 0)
            doc.add_paragraph(f"Student Name: {metadata['name']}\nDepartment: {metadata['dept']}\nStage: {metadata['stage']} - {metadata['study_type']}")
            doc.add_paragraph(clean_math_text(content))
            doc.save(doc_io)
            doc_io.seek(0)
            doc_file = BufferedInputFile(doc_io.getvalue(), filename=f"Report_{metadata['lab_title']}.docx")
            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            await callback.message.answer_document(doc_file, caption="📝 تم إنشاء المستند بصيغة Word الرسمية.")
        else:
            txt_file = BufferedInputFile(clean_math_text(content).encode("utf-8"), filename=f"Report_{metadata['lab_title']}.txt")
            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            await callback.message.answer_document(txt_file, caption="📄 التقرير بصيغة نصية.")
    else:
        txt_file = BufferedInputFile(clean_math_text(content).encode("utf-8"), filename=f"Report_{metadata['lab_title']}.txt")
        stop_event.set()
        counter_task.cancel()
        await status_msg.delete()
        await callback.message.answer_document(txt_file, caption="📄 تم إنشاء ملف التقرير النصي.")
        
    await callback.message.answer("العودة للقائمة الرئيسية:", reply_markup=get_main_menu(callback.from_user.id))
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "cmd_convert")
async def cb_convert_prompt(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🔄 **تحويل العروض التقديمية (PowerPoint) إلى PDF بالكامل:**\n\n"
        "أرسل الآن ملف PowerPoint (.pptx) في المحادثة وسيقوم البوت بتحويل كافة النصوص والمسائل والجداول العميقة إلى مستند PDF منسق.",
        reply_markup=get_main_menu(callback.from_user.id)
    )
    await callback.answer()

@dp.message(F.document)
async def handle_incoming_documents(message: types.Message, state: FSMContext):
    doc_name = message.document.file_name.lower()
    file_id = message.document.file_id
    
    if doc_name.endswith(".pptx") or doc_name.endswith(".ppt"):
        status_msg = await message.answer("📊 **تم استلام ملف PowerPoint... جاري قراءة كامل الشرائح والمسائل والجداول...**")
        stop_event = asyncio.Event()
        counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري تحويل كامل محتوى البوربوينت إلى PDF", stop_event))
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
                    caption="✅ تم تحويل ملف البوربوينت بنجاح إلى PDF مع الحفاظ التام على المسائل والمحتوى!",
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
            await send_long_message(callback.message, f"📑 **الملخص الأكاديمي الشامل ({file_name}):**\n\n{clean_math_text(summary)}")
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
            
            extracted = "".join([f"\n--- صفحة {i+1} ---\n{clean_math_text(doc[i].get_text())}" for i in range(min(8, len(doc)))])
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

    status_msg = await message.answer("📥 **جاري تنزيل الملف وترجمة المحتوى بالصندوق المتناسق الجديد...**")
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
            caption="✅ تمت الترجمة وتنسيق الأسطر داخل الصندوق الأزرق بهوامش محكمة تمنع خروج النص!",
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
    await send_long_message(message, f"📘 **المصطلح:** `{term}`\n\n{clean_math_text(res)}")
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
    
    prompt = f"اشرح المعادلة والرموز الرياضية التالية بالتفصيل بصيغة نصية واضحة بدون رموز لاتكس مشوهة: '{form}'. وضح كل رمز، والوحدات الحقلية والمخبرية، وتطبيقاتها في هندسة النفط."
    res = await ai_request(prompt)
    
    stop_event.set()
    counter_task.cancel()
    await status_msg.delete()
    await send_long_message(message, f"📐 **تفسير القانون والرموز:**\n\n{clean_math_text(res)}")
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
    status_msg = await message.answer("⚙ **جاري الحساب...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(run_live_counter(status_msg, "جاري الحساب وتطبيق القوانين", stop_event))
    
    prompt = f"حل هذه المسألة الهندسية النفطية بخطوات رياضية واضحة واذكر القوانين والوحدات الصحيحة بنص مقروء ومرتب: {q}"
    res = await ai_request(prompt)
    
    stop_event.set()
    counter_task.cancel()
    await status_msg.delete()
    await send_long_message(message, f"🧮 **الناتج والحل:**\n\n{clean_math_text(res)}")
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
# خادم الويب ومنع نوم السيرفر (Self Ping)
# ==========================================
async def handle_ping(request):
    return web.Response(text="Academic Bot Platform is Live and Awake!")

async def keep_awake_loop():
    port = int(os.environ.get("PORT", 8080))
    url = f"http://127.0.0.1:{port}/"
    await asyncio.sleep(15)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    pass
        except Exception:
            pass
        await asyncio.sleep(480)

async def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    asyncio.create_task(keep_awake_loop())

async def main():
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🚀 المنصة الأكاديمية تعمل مع الصندوق الأزرق والجداول المنسقة...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
