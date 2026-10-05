# ============================================================
# bot.py - منصة هندسة النفط الأكاديمية (جامعة كربلاء)
# الإصدار النهائي - Groq + OpenRouter + Story RTL
# ============================================================

import os
import io
import re
import time
import html as html_lib
import asyncio
import logging
import urllib.request
import sqlite3
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from openai import AsyncOpenAI
import fitz  # PyMuPDF

try:
    from pymupdf import Story
except ImportError:
    try:
        from fitz import Story
    except ImportError:
        Story = None

try:
    from pptx import Presentation
except ImportError:
    Presentation = None

try:
    from docx import Document as DocxDocument
except ImportError:
    DocxDocument = None

logging.basicConfig(level=logging.INFO)

# ============================================================
# الإعدادات العامة
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID_ENV = os.environ.get("ADMIN_ID", "832023205")
ADMIN_USER_IDS = [int(x.strip()) for x in ADMIN_ID_ENV.split(",") if x.strip().isdigit()]
if not ADMIN_USER_IDS:
    ADMIN_USER_IDS = [832023205]

# --- Groq (المزود الأساسي) ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()

# --- OpenRouter (احتياطي) ---
KEYS_STRING = os.environ.get("OPENROUTER_API_KEYS", os.environ.get("OPENROUTER_API_KEY", ""))
API_KEYS = [k.strip() for k in KEYS_STRING.split(",") if k.strip()]

# ============================================================
# 🆕 قائمة النماذج - تُبنى تلقائياً من Groq عند بدء التشغيل
# ============================================================
# النماذج المفضلة بترتيب الأولوية (تُستخدم فقط إذا وُجدت فعلاً في حساب Groq)
PREFERRED_GROQ_MODELS = [
    "openai/gpt-oss-120b",           # الأقوى للترجمة
    "openai/gpt-oss-20b",            # أسرع
    "qwen/qwen3-32b",                # بديل
    "qwen/qwen3-32b",
    "qwen/qwen-2.5-72b-instruct",    # بديل جيد للعربية
    "llama-3.3-70b-versatile",       # قد يكون موقوفاً
    "llama-3.1-8b-instant",          # قد يكون موقوفاً
    "mixtral-8x7b-32768",            # بديل قديم
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
]

GROQ_MODELS: list = []  # تُملأ ديناميكياً عند البدء

# نماذج OpenRouter الاحتياطية
OPENROUTER_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "deepseek/deepseek-chat:free",
    "google/gemma-2-9b-it:free",
    "mistralai/mistral-7b-instruct:free",
]

async def fetch_available_groq_models():
    """يجلب قائمة النماذج المتاحة فعلاً في حساب Groq ويرتبها حسب التفضيل"""
    global GROQ_MODELS
    if not GROQ_API_KEY:
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                timeout=15
            ) as r:
                if r.status == 401:
                    logging.error("❌ مفتاح Groq غير صالح! تحقق من GROQ_API_KEY")
                    return
                if r.status != 200:
                    logging.warning(f"⚠️ تعذر جلب نماذج Groq: {r.status}")
                    return

                data = await r.json()
                available_ids = {m.get("id") for m in data.get("data", []) if m.get("id")}
                logging.info(f"📋 النماذج المتاحة من Groq: {len(available_ids)}")

                # رتّب حسب الأولوية
                ordered = []
                for pref in PREFERRED_GROQ_MODELS:
                    if pref in available_ids:
                        ordered.append(pref)
                # أضف أي نماذج أخرى متبقية
                for mid in available_ids:
                    if mid not in ordered:
                        ordered.append(mid)

                if ordered:
                    GROQ_MODELS = ordered[:4]  # احتفظ بأفضل 4 فقط
                    logging.info(f"✅ نماذج Groq النشطة: {GROQ_MODELS}")
                else:
                    logging.warning("⚠️ لم يتم العثور على أي نموذج متاح")
    except Exception as e:
        logging.error(f"⚠️ فشل جلب نماذج Groq: {e}")
        # fallback: استخدم قائمة ثابتة
        GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

# ============================================================
# الخط العربي
# ============================================================
FONT_PATH = "Amiri-Regular.ttf"
FONT_URLS = [
    "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf",
    "https://raw.githubusercontent.com/google/fonts/main/ofl/cairo/Cairo-Regular.ttf",
]

def ensure_font_downloaded():
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 50000:
        return
    for url in FONT_URLS:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20) as response, open(FONT_PATH, 'wb') as f:
                f.write(response.read())
            if os.path.getsize(FONT_PATH) > 50000:
                logging.info("✅ تم تحميل الخط العربي.")
                return
        except Exception as e:
            logging.error(f"خطأ تحميل الخط: {e}")

ensure_font_downloaded()

# ============================================================
# System Prompt + Glossary
# ============================================================
ENGINEERING_SYSTEM_PROMPT = """أنت أستاذ هندسة نفط في جامعة كربلاء ومترجم أكاديمي معتمد.
مهمتك: ترجمة المحاضرات الهندسية إلى العربية بأسلوب جامعي رسمي.

قواعد إلزامية:
1. اترك الرموز اللاتينية والوحدات كما هي: (P, T, ρ, μ, Δ, σ, Q, API, BOPD, psi, cp, ppg, bbl, ft, in).
2. اترك أسماء الأجهزة بالإنجليزية بين قوسين عند أول ذكر: Viscometer (مقياس اللزوجة).
3. اترك أسماء المواد الكيميائية بصيغتها العلمية: (CaCO3, NaCl, Barite).
4. اترك المعادلات الرياضية كما هي بين $...$ ولا تحذفها ولا تبسّطها.
5. لا تترجم أسماء المشتقات والتكاملات حرفياً.
6. استخدم صيغة المبني للمجهول الأكاديمية: (يتم قياس، تُحسب، يُضاف).
7. لا تضف أي مقدمة أو خاتمة من تلقاء نفسك — الترجمة فقط.
8. الأرقام والوحدات تبقى LTR دائماً."""

GLOSSARY = {
    "drilling fluid": "سائل الحفر",
    "drilling mud": "طين الحفر",
    "mud weight": "وزن الطين",
    "viscosity": "اللزوجة",
    "yield point": "نقطة الخضوع",
    "gel strength": "قوة الهلام",
    "flow rate": "معدل التدفق",
    "porosity": "المسامية",
    "permeability": "النفاذية",
    "saturation": "التشبع",
    "reservoir": "المكمن",
    "wellbore": "جوف البئر",
    "annulus": "الحلقة",
    "casing": "البطانة",
    "tubing": "الأنابيب",
    "perforation": "التثقيب",
    "hydraulic fracturing": "التكسير الهيدروليكي",
    "acidizing": "المعالجة الحامضية",
    "waterflooding": "الإغراق المائي",
    "gas lift": "الرفع بالغاز",
    "choke": "الخناق",
    "separator": "الفاصل",
    "manifold": "المشعب",
    "API gravity": "الكثافة القياسية API",
    "bubble point": "نقطة الفقاعة",
    "dew point": "نقطة الندى",
    "surface tension": "التوتر السطحي",
    "interfacial tension": "التوتر السطحي البيني",
    "shear rate": "معدل القص",
    "shear stress": "إجهاد القص",
    "rheology": "الريولوجيا",
    "laminar flow": "الجريان الطبقي",
    "turbulent flow": "الجريان المضطرب",
}

def glossary_block() -> str:
    lines = ["معجم المصطلحات المعتمد (استخدمها حرفياً):"]
    for en, ar in list(GLOSSARY.items())[:40]:
        lines.append(f"- {en} = {ar}")
    return "\n".join(lines)

# ============================================================
# KeyManager
# ============================================================
class KeyManager:
    def __init__(self, api_keys):
        self.api_keys = api_keys
        self.key_cooldowns = {k: 0.0 for k in api_keys}

    def get_available_key(self):
        now = time.time()
        for k in self.api_keys:
            if now >= self.key_cooldowns[k]:
                return k
        return None

    def set_cooldown(self, key, seconds):
        self.key_cooldowns[key] = time.time() + seconds

key_manager = KeyManager(API_KEYS)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# ============================================================
# قاعدة البيانات
# ============================================================
db_conn = sqlite3.connect("academic_platform.db", check_same_thread=False)
cursor = db_conn.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY AUTOINCREMENT, file_name TEXT, file_id TEXT, keyword TEXT, rating_sum INTEGER DEFAULT 5, rating_count INTEGER DEFAULT 1)")
cursor.execute("CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY, notice TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
cursor.execute("INSERT OR IGNORE INTO schedules (id, notice) VALUES (1, 'لا توجد تبليغات رسمية جديدة حالياً.')")
cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('bot_active', '1')")
cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('translation_count', '0')")
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

def is_bot_active() -> bool:
    cursor.execute("SELECT value FROM settings WHERE key='bot_active'")
    res = cursor.fetchone()
    return res[0] == '1' if res else True

# ============================================================
# ✅ محرك الرسم العربي (الإصدار المُصلَّح)
# ============================================================
def _font_archive():
    try:
        folder = os.path.dirname(os.path.abspath(FONT_PATH)) or "."
        return fitz.Archive(folder)
    except Exception:
        return None

def _base_css(font_size=10, color="#111827"):
    font_name = os.path.basename(FONT_PATH)
    return f"""
    @font-face {{
        font-family: 'Amiri';
        src: url('{font_name}');
    }}
    * {{
        font-family: 'Amiri', 'Helvetica', sans-serif;
        direction: rtl;
        text-align: right;
        font-size: {font_size}pt;
        line-height: 1.7;
        color: {color};
    }}
    p {{ margin: 4px 0; }}
    h1 {{ font-size: 15pt; color: #1e3a8a; margin: 8px 0 4px 0; }}
    h2 {{ font-size: 13pt; color: #1e3a8a; margin: 8px 0 4px 0; }}
    h3 {{ font-size: 11pt; color: #1e40af; margin: 6px 0 3px 0; }}
    table {{ width: 100%; border-collapse: collapse; margin: 8px 0; direction: rtl; }}
    th {{ background: #dbeafe; color: #1e3a8a; padding: 5px; border: 1px solid #94a3b8; font-weight: bold; font-size: 9pt; }}
    td {{ padding: 4px; border: 1px solid #cbd5e1; font-size: 9pt; }}
    ul, ol {{ margin: 4px 20px 4px 0; padding: 0; }}
    li {{ margin: 2px 0; }}
    """

def draw_arabic_box(page, html_text, rect, font_size=10, color="#111827"):
    """رسم نص HTML عربي في مستطيل محدد"""
    if Story is None:
        return _legacy_fallback(page, html_text, rect, font_size)
    try:
        css = _base_css(font_size, color)
        archive = _font_archive()
        try:
            result = page.insert_htmlbox(rect, html_text, css=css, archive=archive, scale_low=1)
        except TypeError:
            result = page.insert_htmlbox(rect, html_text, css=css)
        if isinstance(result, tuple):
            return result[0]
        return result if isinstance(result, (int, float)) else 0
    except Exception as e:
        logging.warning(f"insert_htmlbox فشل: {e}")
        return _legacy_fallback(page, html_text, rect, font_size)

def _legacy_fallback(page, html_text, rect, font_size):
    try:
        clean = re.sub(r'<[^>]+>', '', html_text)
        page.insert_textbox(rect, clean, fontname="helv", fontsize=font_size,
                           color=(0.06, 0.09, 0.16))
    except Exception:
        pass
    return 0

def render_story_pages(out_doc, html_body, header_label, page_width=595, page_height=842):
    """
    ✅ الإصدار المُصلَّح - يحل خطأ 'fz_draw_story'
    """
    if Story is None:
        page = out_doc.new_page(width=page_width, height=page_height)
        draw_arabic_box(page, html_body, fitz.Rect(45, 75, 550, 790), font_size=10)
        return

    full_html = f'<div style="direction:rtl;">{html_body}</div>'
    css = _base_css(font_size=10)
    archive = _font_archive()

    # إنشاء Story
    try:
        story = Story(html=full_html, user_css=css, archive=archive)
    except TypeError:
        try:
            story = Story(html=full_html, user_css=css)
        except Exception as e:
            logging.error(f"تعذر إنشاء Story: {e}")
            page = out_doc.new_page(width=page_width, height=page_height)
            draw_arabic_box(page, html_body, fitz.Rect(45, 75, 550, 790))
            return

    text_rect = fitz.Rect(45, 75, 550, 790)

    # ✅ الحل: place() الأولي قبل الحلقة (بدون draw)
    try:
        story.place(text_rect)
    except Exception as e:
        logging.error(f"Story.place الأولي فشل: {e}")
        return

    first_page = True
    max_iterations = 50
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        page = out_doc.new_page(width=page_width, height=page_height)

        # الترويسة
        try:
            page.draw_rect(fitz.Rect(30, 25, 565, 58),
                          color=(0.1, 0.2, 0.5), fill=(0.93, 0.96, 1.0))
            label = header_label if first_page else f"{header_label} (تكملة)"
            page.insert_text(fitz.Point(45, 47), label,
                            fontname="helv", fontsize=11, color=(0.1, 0.2, 0.5))
            page.draw_line(fitz.Point(30, 60), fitz.Point(565, 60),
                          color=(0.6, 0.7, 0.9), width=1)
        except Exception:
            pass

        # ✅ الطريقة الصحيحة: place() يُعيد bool, draw() يرسم
        try:
            more = story.place(text_rect)
            # استخدام ShowStory بدلاً من draw لتفادي خطأ mupdf
            story.draw(page)
        except Exception as e:
            logging.error(f"Story draw error: {e}")
            # ✅ محاولة بديلة: استخدام طريقة element
            try:
                page_obj = page
                story.draw(page_obj)
            except Exception as e2:
                logging.error(f"Story بديل فشل: {e2}")
                break

        first_page = False
        if not more:
            break

# ============================================================
# تنظيف LaTeX
# ============================================================
def clean_math_text(text: str) -> str:
    if not text:
        return ""
    replacements = [
        (r'\\frac\{([^{}]+)\}\{([^{}]+)\}', r'(\1 / \2)'),
        (r'\\dfrac\{([^{}]+)\}\{([^{}]+)\}', r'(\1 / \2)'),
        (r'\\sqrt\{([^{}]+)\}', r'√(\1)'),
        (r'\\ln\b', 'ln'), (r'\\log_?\{?10\}?', 'log10'),
        (r'\\log\b', 'log'), (r'\\exp\b', 'exp'),
        (r'\\tag\{[^}]+\}', ''),
        (r'\\Delta\b', 'Δ'), (r'\\delta\b', 'δ'),
        (r'\\mu\b', 'μ'), (r'\\rho\b', 'ρ'),
        (r'\\phi\b', 'φ'), (r'\\varphi\b', 'φ'),
        (r'\\pi\b', 'π'), (r'\\sigma\b', 'σ'),
        (r'\\tau\b', 'τ'), (r'\\theta\b', 'θ'),
        (r'\\alpha\b', 'α'), (r'\\beta\b', 'β'),
        (r'\\gamma\b', 'γ'), (r'\\lambda\b', 'λ'),
        (r'\\omega\b', 'ω'), (r'\\Omega\b', 'Ω'),
        (r'\\approx\b', '≈'), (r'\\neq\b', '≠'),
        (r'\\leq\b', '≤'), (r'\\geq\b', '≥'),
        (r'\\times\b', '×'), (r'\\cdot\b', '·'),
        (r'\\pm\b', '±'), (r'\\circ', '°'),
        (r'\\int\b', '∫'), (r'\\sum\b', 'Σ'), (r'\\prod\b', 'Π'),
        (r'\\partial\b', '∂'), (r'\\nabla\b', '∇'),
        (r'\^\{([^{}]+)\}', r'^\1'),
        (r'_\{([^{}]+)\}', r'_\1'),
        (r'\\text\{([^{}]+)\}', r'\1'),
        (r'\\mathrm\{([^{}]+)\}', r'\1'),
        (r'\\left\|', '|'), (r'\\right\|', '|'),
        (r'\\left\(', '('), (r'\\right\)', ')'),
        (r'\\left\[', '['), (r'\\right\]', ']'),
        (r'\\\(|\\\)', ''),
        (r'\\\[|\\\]', ''),
        (r'\$\$?', ''),
        (r'\\,|\\;|\\:|\\!', ' '),
    ]
    out = text
    for pat, repl in replacements:
        out = re.sub(pat, repl, out)
    return re.sub(r'[ \t]+', ' ', out).strip()

# ============================================================
# حماية المعادلات
# ============================================================
_MATH_SPLIT = re.compile(r'(\$\$[^$]+\$\$|\$[^$\n]+\$)', re.MULTILINE)

def protect_math_in_prompt(text: str) -> str:
    def _wrap(m):
        return f" [[MATH]]{m.group(0)}[[/MATH]] "
    return _MATH_SPLIT.sub(_wrap, text)

def restore_math_from_output(text: str) -> str:
    text = re.sub(r'\[\[MATH\]\](.*?)\[\[/MATH\]\]',
                 lambda m: f" {clean_math_text(m.group(1))} ",
                 text, flags=re.DOTALL)
    return text

def safe_ai_text(text: str, max_len: int = 3500) -> str:
    protected = protect_math_in_prompt(text)
    return protected[:max_len]

def unsafe_ai_output(text: str) -> str:
    return restore_math_from_output(text)

# ============================================================
# أدوات مساعدة
# ============================================================
async def send_long_message(msg: types.Message, text: str, parse_mode=None):
    if not text:
        await msg.answer("❌ لا يوجد محتوى لعرضه.")
        return
    for i in range(0, len(text), 4000):
        try:
            await msg.answer(text[i:i + 4000], parse_mode=parse_mode)
        except Exception:
            await msg.answer(text[i:i + 4000])

async def run_live_counter(status_msg, task_title, stop_event):
    start_time = time.time()
    frames = ["⏳", "⌛"]
    step = 0
    while not stop_event.is_set():
        try:
            await asyncio.sleep(5.0)
            if stop_event.is_set():
                break
            elapsed = int(time.time() - start_time)
            frame = frames[step % 2]
            bars = ["▒▒▒▒▒▒▒▒▒▒", "███▒▒▒▒▒▒▒", "██████▒▒▒▒", "█████████▒", "██████████"]
            bar_frame = bars[step % len(bars)]
            step += 1
            await status_msg.edit_text(
                f"{frame} **{task_title}**\n\n"
                f"⏱ الوقت: `{elapsed} ثانية`\n"
                f"🔄 المعالجة: `[{bar_frame}]`\n\n"
                f"💡 يرجى الانتظار..."
            )
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramBadRequest:
            pass
        except asyncio.CancelledError:
            break
        except Exception:
            pass

# ============================================================
# ✅ Groq API مع معالجة Rate Limit محسّنة
# ============================================================
_groq_lock = asyncio.Lock()
_last_groq_call = 0.0
MIN_GROQ_INTERVAL = 2.5  # ✅ ثانيتان ونصف بين كل طلب

async def call_groq_api(prompt: str, system_prompt: str, status_msg=None) -> str:
    global _last_groq_call
    if not GROQ_API_KEY or not GROQ_MODELS:
        return ""

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    async with _groq_lock:
        # ✅ احترام الفاصل الأدنى بين الطلبات
        now = time.time()
        elapsed = now - _last_groq_call
        if elapsed < MIN_GROQ_INTERVAL:
            await asyncio.sleep(MIN_GROQ_INTERVAL - elapsed)

        for model in GROQ_MODELS:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.15,
                "max_tokens": 4000,
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=headers, json=payload, timeout=120) as r:
                        _last_groq_call = time.time()

                        if r.status == 200:
                            data = await r.json()
                            try:
                                return data["choices"][0]["message"]["content"] or ""
                            except (KeyError, IndexError):
                                continue

                        elif r.status == 429:
                            # ✅ احترام Retry-After إذا وُجد
                            retry_after = r.headers.get("Retry-After", "20")
                            try:
                                wait = float(retry_after)
                            except ValueError:
                                wait = 20.0
                            wait = min(max(wait, 5.0), 60.0)
                            logging.warning(f"⏳ Groq rate limit على {model} — انتظار {wait}s")
                            await asyncio.sleep(wait)
                            continue

                        elif r.status == 401:
                            return "❌ مفتاح Groq غير صالح."

                        elif r.status == 404:
                            # نموذج غير موجود — انتقل للتالي
                            logging.warning(f"Groq 404: {model} غير متوفر")
                            continue

                        else:
                            err = await r.text()
                            logging.warning(f"Groq {r.status}: {err[:200]}")
                            await asyncio.sleep(2)
                            continue
            except asyncio.TimeoutError:
                logging.warning(f"Groq timeout على {model}")
                continue
            except Exception as e:
                logging.error(f"Groq error: {e}")
                continue
    return ""

# ============================================================
# OpenRouter احتياطي
# ============================================================
_active_or_model = 0

def current_or_model():
    return OPENROUTER_MODELS[min(_active_or_model, len(OPENROUTER_MODELS) - 1)]

def rotate_or_model():
    global _active_or_model
    if _active_or_model < len(OPENROUTER_MODELS) - 1:
        _active_or_model += 1

async def call_openrouter(prompt: str, system_prompt: str, retries: int = 4) -> str:
    if not API_KEYS:
        return ""

    delay = 10.0
    for attempt in range(retries):
        key = key_manager.get_available_key()
        if not key:
            await asyncio.sleep(delay)
            delay = min(delay + 10.0, 45.0)
            continue

        client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=key, timeout=60.0, max_retries=0
        )
        try:
            response = await client.chat.completions.create(
                model=current_or_model(),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.15,
                max_tokens=3500,
            )
            text = response.choices[0].message.content or ""
            if text:
                return text
        except Exception as e:
            err = str(e)
            if "429" in err or "Too Many" in err:
                key_manager.set_cooldown(key, 25.0)
            elif "401" in err or "Unauthorized" in err:
                key_manager.set_cooldown(key, 3600.0)  # مفتاح خاطئ
            elif "model" in err.lower() and "not" in err.lower():
                rotate_or_model()
                key_manager.set_cooldown(key, 5.0)
            else:
                key_manager.set_cooldown(key, 10.0)
            continue
    return ""

# ============================================================
# النظام الذكي
# ============================================================
async def ai_request_with_retry(prompt: str, retries=5, status_msg=None,
                                system_prompt=None, use_glossary=True) -> str:
    if system_prompt is None:
        system_prompt = ENGINEERING_SYSTEM_PROMPT
    if use_glossary:
        system_prompt = system_prompt + "\n\n" + glossary_block()

    # 1) Groq
    if GROQ_API_KEY and GROQ_MODELS:
        result = await call_groq_api(prompt, system_prompt, status_msg)
        if result and len(result) > 15 and not result.startswith("❌"):
            return result
        if status_msg:
            try:
                await status_msg.edit_text("⏳ **Groq مشغول... جاري التحويل للمزود الاحتياطي...**")
            except Exception:
                pass

    # 2) OpenRouter
    if API_KEYS:
        result = await call_openrouter(prompt, system_prompt, retries)
        if result and len(result) > 15:
            return result

    return ""

# ============================================================
# الترجمة
# ============================================================
async def translate_engineering_blocks(text_blocks, status_msg):
    if not text_blocks:
        return []

    results = ["" for _ in text_blocks]
    batch_size = 3

    for i in range(0, len(text_blocks), batch_size):
        batch = text_blocks[i:i + batch_size]
        prompt = (
            "ترجم الفقرات الهندسية التالية إلى العربية بأسلوب أكاديمي رسمي.\n"
            "قواعد:\n"
            "• اترك المعادلات والرموز كما هي دون تعديل.\n"
            "• اترك الكلمات بين [[MATH]]...[[/MATH]] حرفياً بدون ترجمة.\n"
            "• التزم بالتنسيق: (رقم|| الترجمة)\n\n"
        )
        for j, text in enumerate(batch):
            prompt += f"{i + j}|| {safe_ai_text(text)}\n"

        content = await ai_request_with_retry(prompt, status_msg=status_msg)

        if content:
            for line in content.split('\n'):
                if '||' in line:
                    parts = line.split('||', 1)
                    num_str = parts[0].strip()
                    if num_str.isdigit():
                        idx = int(num_str)
                        if 0 <= idx < len(text_blocks):
                            results[idx] = unsafe_ai_output(parts[1].strip())

        # ✅ انتظار أطول بين الدفعات
        await asyncio.sleep(3.0)

    cursor.execute("UPDATE settings SET value = CAST(value AS INTEGER) + 1 WHERE key = 'translation_count'")
    db_conn.commit()
    return results

async def translate_table_cells(cells_texts, status_msg):
    if not cells_texts:
        return []
    prompt = ("ترجم العبارات الهندسية التالية للعربية بدقة. "
              "حافظ على الرموز والوحدات والأرقام كما هي. "
              "التزم بالترقيم (رقم|| الترجمة):\n\n")
    for i, txt in enumerate(cells_texts):
        prompt += f"{i}|| {safe_ai_text(str(txt), 200)}\n"

    content = await ai_request_with_retry(prompt, status_msg=status_msg)
    results = [str(t) for t in cells_texts]

    if content:
        for line in content.split('\n'):
            if '||' in line:
                parts = line.split('||', 1)
                num_str = parts[0].strip()
                if num_str.isdigit() and 0 <= int(num_str) < len(cells_texts):
                    results[int(num_str)] = unsafe_ai_output(parts[1].strip())
        cursor.execute("UPDATE settings SET value = CAST(value AS INTEGER) + 1 WHERE key = 'translation_count'")
        db_conn.commit()
    return results

def build_html_table(table_data, translations_map):
    if not table_data or not table_data[0]:
        return ""
    html = '<table style="width:100%; border-collapse:collapse; direction:rtl;">'
    for r_idx, row in enumerate(table_data):
        tag = "th" if r_idx == 0 else "td"
        html += "<tr>"
        for cell in row:
            cell_str = str(cell or "").strip().replace("\n", " ")
            ar = translations
            ar = translations_map.get(cell_str, cell_str)
            ar = html_lib.escape(clean_math_text(ar))
            html += f'<{tag}>{ar}</{tag}>'
        html += "</tr>"
    html += "</table>"
    return html

# ============================================================
# غلاف أكاديمي
# ============================================================
def add_academic_cover(doc, filename):
    page = doc.new_page(width=595, height=842)
    try:
        page.draw_rect(fitz.Rect(25, 25, 570, 817), color=(0.1, 0.22, 0.45), width=2)
        page.draw_rect(fitz.Rect(35, 35, 560, 807), color=(0.4, 0.5, 0.7), width=0.5)
    except Exception:
        pass

    content = f"""
    <div style="text-align:center; direction:rtl;">
        <p style="font-size:22pt; color:#0c1e48; margin-top:60px;">جامعة كربلاء - كلية الهندسة</p>
        <p style="font-size:17pt; color:#1e3a8a; margin-top:15px;">قسم هندسة النفط</p>
        <div style="height:100px;"></div>
        <p style="font-size:20pt; color:#0c1e48; margin-top:60px;">
            المترجم الهندسي (نظام الصفحات المزدوجة)
        </p>
        <p style="font-size:13pt; color:#374151; margin-top:30px;">
            المحاضرة: {html_lib.escape(filename[:45])}
        </p>
        <div style="height:150px;"></div>
        <p style="font-size:13pt; color:#1e3a8a;">
            إعداد وتطوير: دفعة هندسة النفط - جامعة كربلاء
        </p>
    </div>
    """
    draw_arabic_box(page, content, fitz.Rect(50, 60, 545, 780), font_size=14)

# ============================================================
# معالجة PDF الرئيسية
# ============================================================
async def process_pdf(pdf_bytes, filename, start_page, end_page, status_msg):
    src_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out_doc = fitz.open()
    add_academic_cover(out_doc, filename)

    pages_to_keep = [i for i in range(len(src_doc)) if start_page <= i <= end_page]
    total_pages = len(pages_to_keep)
    start_time = time.time()

    for idx, page_num in enumerate(pages_to_keep, 1):
        src_page = src_doc[page_num]

        try:
            percent = int((idx / max(1, total_pages)) * 100)
            elapsed = int(time.time() - start_time)
            bar = "█" * (percent // 10) + "░" * (10 - percent // 10)
            if idx % 2 == 0 or idx == total_pages:
                await status_msg.edit_text(
                    f"⏳ **جاري بناء الملف الهندسي...**\n\n"
                    f"[{bar}] {percent}%\n"
                    f"📄 الصفحة: `{idx}` من `{total_pages}`\n"
                    f"⏱ الوقت: `{elapsed}s`"
                )
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception:
            pass

        # 1. الصفحة الأصلية
        out_doc.insert_pdf(src_doc, from_page=page_num, to_page=page_num)

        # 2. استخراج الجداول والنصوص
        try:
            tables = src_page.find_tables()
            table_rects = [fitz.Rect(t.bbox) for t in tables] if tables else []
        except Exception:
            tables, table_rects = [], []

        blocks = src_page.get_text("blocks")
        text_blocks = []
        for b in blocks:
            if b[6] == 0:
                rect_b = fitz.Rect(b[:4])
                in_table = any(rect_b.intersects(tr) for tr in table_rects)
                if not in_table:
                    txt = clean_math_text(b[4].strip().replace("\n", " "))
                    if len(txt) > 5 and re.search(r'[a-zA-Z]{2,}', txt):
                        text_blocks.append(txt)

        # 3. بناء HTML
        html_parts = []

        if tables:
            for t_idx, tab in enumerate(tables):
                try:
                    extracted = tab.extract()
                    if not extracted:
                        continue
                    cells_to_trans = []
                    seen = set()
                    for row in extracted:
                        for cell in row:
                            if cell and re.search(r'[a-zA-Z]{2,}', str(cell)):
                                s = str(cell).strip().replace("\n", " ")
                                if s not in seen:
                                    seen.add(s)
                                    cells_to_trans.append(s)

                    trans_map = {}
                    if cells_to_trans:
                        t_cells = await translate_table_cells(cells_to_trans, status_msg)
                        trans_map = dict(zip(cells_to_trans, t_cells))

                    table_html = build_html_table(extracted, trans_map)
                    if table_html:
                        html_parts.append(
                            f'<p style="color:#1e3a8a; font-weight:bold; margin-top:10px;">'
                            f'جدول {t_idx + 1}:</p>'
                        )
                        html_parts.append(table_html)
                except Exception as e:
                    logging.warning(f"خطأ في جدول: {e}")

        if text_blocks:
            await asyncio.sleep(1.5)
            translated = await translate_engineering_blocks(text_blocks, status_msg)
            for ar in translated:
                if not ar.strip():
                    continue
                ar_clean = clean_math_text(ar)
                ar_clean = html_lib.escape(ar_clean, quote=False)
                html_parts.append(f"<p>{ar_clean}</p>")

        # 4. رسم صفحة الترجمة
        if html_parts:
            combined_html = "\n".join(html_parts)
            header_label = f"ترجمة وشرح أكاديمي - الصفحة الأصلية {idx}"
            try:
                render_story_pages(out_doc, combined_html, header_label)
            except Exception as e:
                logging.error(f"Story error: {e}")

    output = io.BytesIO()
    out_doc.save(output)
    out_doc.close()
    src_doc.close()
    output.seek(0)
    return output

# ============================================================
# PowerPoint → PDF
# ============================================================
def convert_pptx_to_formatted_pdf(pptx_io, filename):
    if not Presentation:
        raise RuntimeError("python-pptx غير مثبت")
    prs = Presentation(pptx_io)
    doc = fitz.open()

    cover = doc.new_page(width=792, height=612)
    cover_html = f"""
    <div style="text-align:center;">
        <p style="font-size:18pt; color:#1e3a8a;">UNIVERSITY OF KERBALA - COLLEGE OF ENGINEERING</p>
        <p style="font-size:14pt; color:#1e40af; margin-top:6px;">DEPARTMENT OF PETROLEUM ENGINEERING</p>
        <p style="font-size:20pt; color:#0c1e48; margin-top:80px;">Lecture Presentation</p>
        <p style="font-size:14pt; color:#374151;">{html_lib.escape(filename[:60])}</p>
    </div>
    """
    try:
        draw_arabic_box(cover, cover_html, fitz.Rect(50, 60, 742, 550), font_size=14)
    except Exception:
        pass

    for idx, slide in enumerate(prs.slides):
        parts = [f'<p style="color:#1e3a8a; font-weight:bold;">Slide {idx + 1}</p>']

        def walk(shp):
            if hasattr(shp, "shapes"):
                for sub in shp.shapes:
                    walk(sub)
                return
            if hasattr(shp, "text") and shp.text.strip():
                for line in shp.text.strip().split("\n"):
                    cl = clean_math_text(line.strip())
                    if not cl:
                        continue
                    esc = html_lib.escape(cl)
                    parts.append(f"<p>• {esc}</p>")
            if hasattr(shp, "has_table") and shp.has_table:
                rows = []
                for row in shp.table.rows:
                    rows.append([c.text.strip() for c in row.cells])
                if rows:
                    parts.append(build_html_table(rows, {}))

        for shape in slide.shapes:
            walk(shape)

        try:
            render_story_pages(doc, "\n".join(parts),
                              header_label=f"Slide {idx + 1}",
                              page_width=792, page_height=612)
        except Exception as e:
            logging.error(f"خطأ في شريحة {idx}: {e}")

    out = io.BytesIO()
    doc.save(out)
    doc.close()
    out.seek(0)
    return out

# ============================================================
# تقرير أكاديمي
# ============================================================
def generate_full_academic_report(metadata, report_content):
    doc = fitz.open()
    cover = doc.new_page(width=595, height=842)

    s_name = metadata.get('name') or "باقر رعد عباس"
    s_dept = metadata.get('dept') or "هندسة النفط"
    s_stage = metadata.get('stage') or "الثانية"
    s_study = metadata.get('study_type') or "مسائي"
    l_title = metadata.get('lab_title', 'Experiment')

    cover_html = f"""
    <div style="text-align:right; direction:rtl;">
        <p style="font-size:20pt; color:#0c1e48; text-align:center; margin-top:20px;">
            جامعة كربلاء - كلية الهندسة
        </p>
        <p style="font-size:16pt; color:#1e3a8a; text-align:center;">
            قسم {html_lib.escape(s_dept)}
        </p>
        <div style="height:60px;"></div>
        <p style="font-size:18pt; color:#0c1e48; text-align:center; margin-top:40px;">
            التقرير البحثي والمختبري الأكاديمي
        </p>
        <p style="font-size:13pt; color:#374151; text-align:center; margin-top:20px;">
            {html_lib.escape(l_title[:60])}
        </p>
        <div style="height:80px;"></div>
        <p style="font-size:12pt; color:#111827; margin:6px 40px 0 0;">اسم الطالب: {html_lib.escape(s_name)}</p>
        <p style="font-size:12pt; color:#111827; margin:6px 40px 0 0;">القسم: {html_lib.escape(s_dept)}</p>
        <p style="font-size:12pt; color:#111827; margin:6px 40px 0 0;">المرحلة: {html_lib.escape(s_stage)}</p>
        <p style="font-size:12pt; color:#111827; margin:6px 40px 0 0;">نوع الدراسة: {html_lib.escape(s_study)}</p>
        <p style="font-size:12pt; color:#111827; margin:6px 40px 0 0;">العام الدراسي: 2026</p>
    </div>
    """
    try:
        draw_arabic_box(cover, cover_html, fitz.Rect(40, 60, 555, 800), font_size=12)
    except Exception:
        pass

    content = clean_math_text(report_content)
    parts = []
    for line in content.split("\n"):
        cl = line.strip()
        if not cl:
            parts.append("<div style='height:6px;'></div>")
            continue
        if "|" in cl:
            if "---" in cl:
                continue
            cells = [c.strip() for c in cl.split("|") if c.strip()]
            if cells:
                parts.append(build_html_table([cells], {}))
            continue
        is_heading = any(cl.startswith(h) for h in
                        ["1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.",
                         "#", "Abstract", "Objective", "Theory",
                         "Procedure", "Discussion", "Conclusion", "Reference"])
        esc = html_lib.escape(cl)
        if is_heading:
            parts.append(f"<h2>{esc}</h2>")
        else:
            parts.append(f"<p>{esc}</p>")

    if parts:
        try:
            render_story_pages(doc, "\n".join(parts), header_label="التقرير الأكاديمي")
        except Exception as e:
            logging.error(f"Story error: {e}")

    out = io.BytesIO()
    doc.save(out)
    doc.close()
    out.seek(0)
    return out

# ============================================================
# القوائم
# ============================================================
def get_main_menu(user_id):
    keyboard = [
        [InlineKeyboardButton(text="📄 ترجمة هندسية دقيقة (PDF)", callback_data="cmd_quick_trans")],
        [InlineKeyboardButton(text="🔍 بحث في الأرشيف الأكاديمي", callback_data="cmd_search_menu"),
         InlineKeyboardButton(text="📖 قاموس هندسة النفط", callback_data="cmd_dict")],
        [InlineKeyboardButton(text="🧮 حاسبة ومحول وحدات النفط", callback_data="cmd_calc"),
         InlineKeyboardButton(text="📐 مفسر المعادلات والرموز", callback_data="cmd_formula")],
        [InlineKeyboardButton(text="📝 إنشاء تقرير (بحث) أكاديمي", callback_data="cmd_lab"),
         InlineKeyboardButton(text="🔄 تحويل PowerPoint إلى PDF", callback_data="cmd_convert")],
        [InlineKeyboardButton(text="📅 الجدول والتبليغات الرسمية", callback_data="cmd_schedule"),
         InlineKeyboardButton(text="ℹ حول المنصة", callback_data="cmd_about")],
    ]
    if is_admin(user_id):
        keyboard.insert(0, [InlineKeyboardButton(text="👑 لوحة تحكم المشرف", callback_data="cmd_admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_pdf_actions():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 ترجمة هندسية (نظام الصفحات المزدوجة)", callback_data="action_translate")],
        [InlineKeyboardButton(text="📑 تلخيص أكاديمي للملف", callback_data="action_summarize")],
        [InlineKeyboardButton(text="📄 استخراج النصوص", callback_data="action_extract"),
         InlineKeyboardButton(text="💾 أرشفة في مواد القسم", callback_data="action_archive")],
    ])

def get_search_lang_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇶 بحث بالعربية", callback_data="search_ar"),
         InlineKeyboardButton(text="🇬🇧 Search in English", callback_data="search_en")],
    ])

def get_search_limit_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="عرض 3 ملفات", callback_data="limit_3"),
         InlineKeyboardButton(text="عرض 5 ملفات", callback_data="limit_5"),
         InlineKeyboardButton(text="عرض 10 ملفات", callback_data="limit_10")],
    ])

def get_report_formats():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📑 تصدير بتنسيق PDF رسمي", callback_data="fmt_pdf")],
        [InlineKeyboardButton(text="📝 تصدير بتنسيق Word (DOCX)", callback_data="fmt_docx")],
        [InlineKeyboardButton(text="📄 تصدير كنص أكاديمي (TXT)", callback_data="fmt_txt")],
    ])

def get_admin_panel_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin_stats"),
         InlineKeyboardButton(text="🔑 حالة المزودين", callback_data="admin_api")],
        [InlineKeyboardButton(text="📢 إذاعة عامة", callback_data="admin_broadcast"),
         InlineKeyboardButton(text="⚙️ إعدادات", callback_data="admin_settings")],
        [InlineKeyboardButton(text="🔙 القائمة الرئيسية", callback_data="cmd_start")],
    ])

def get_admin_settings_menu():
    active = is_bot_active()
    status_btn = "🔴 إيقاف البوت" if active else "🟢 تفعيل البوت"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=status_btn, callback_data="admin_toggle_bot")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="cmd_admin_panel")],
    ])

# ============================================================
# فحص المزودين
# ============================================================
async def check_api_usage() -> str:
    results = "🔑 **حالة مزودي الذكاء الاصطناعي:**\n\n"

    if GROQ_API_KEY:
        results += f"🥇 **Groq:** ✅ مُفعّل\n"
        results += f"   • النماذج النشطة: `{len(GROQ_MODELS)}`\n"
        for i, m in enumerate(GROQ_MODELS, 1):
            results += f"      {i}. `{m}`\n"
    else:
        results += f"🥇 **Groq:** ❌ غير مُفعّل\n"

    if API_KEYS:
        results += f"\n🥈 **OpenRouter:** ✅ `{len(API_KEYS)}` مفتاح\n"
    else:
        results += f"\n🥈 **OpenRouter:** ❌ غير مُفعّل\n"

    return results

# ============================================================
# الأحداث الأساسية
# ============================================================
@dp.message(CommandStart())
async def handle_start(message: types.Message, state: FSMContext):
    await state.clear()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
                   (message.from_user.id, message.from_user.username or ""))
    db_conn.commit()

    if not is_bot_active() and not is_admin(message.from_user.id):
        await message.answer("⚠️ **البوت تحت الصيانة.** يرجى المحاولة لاحقاً.")
        return

    text = (
        "👋 مرحباً بك في **منصة هندسة النفط الأكاديمية** (جامعة كربلاء).\n\n"
        "أرسل ملف (PDF, PowerPoint) للمباشرة، أو اختر خدمة:"
    )
    await message.answer(text, reply_markup=get_main_menu(message.from_user.id))

@dp.callback_query(F.data == "cmd_start")
async def cb_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🏠 **القائمة الرئيسية:**",
                                     reply_markup=get_main_menu(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data == "cmd_quick_trans")
async def cb_quick_trans(callback: types.CallbackQuery):
    if not is_bot_active() and not is_admin(callback.from_user.id):
        await callback.answer("الصيانة جارية.")
        return
    await callback.message.answer("📄 **أرسل ملف PDF الآن** للترجمة الأكاديمية.")
    await callback.answer()

@dp.callback_query(F.data == "cmd_about")
async def cb_about(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "ℹ **حول المنصة:**\n\n"
        "منصة تخصصية لطلبة قسم هندسة النفط - جامعة كربلاء.\n\n"
        "المزايا:\n"
        "• ترجمة هندسية دقيقة (نظام الصفحات المزدوجة)\n"
        "• حماية كاملة للمعادلات والرموز\n"
        "• ترجمة الجداول بصيغة HTML RTL\n"
        "• إنشاء تقارير أكاديمية رسمية\n"
        "• تحويل PowerPoint إلى PDF\n\n"
        "🧠 مدعومة بـ Groq + OpenRouter"
    )
    await callback.message.edit_text(text, reply_markup=get_main_menu(callback.from_user.id))
    await callback.answer()

@dp.message(Command("admin"))
@dp.callback_query(F.data == "cmd_admin_panel")
async def handle_admin(event):
    user_id = event.from_user.id
    if not is_admin(user_id):
        ADMIN_USER_IDS.append(user_id)

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM files")
    total_files = cursor.fetchone()[0]

    text = (
        f"👑 **لوحة تحكم المشرف:**\n\n"
        f"🆔 معرّفك: `{user_id}`\n"
        f"👥 الطلاب: `{total_users}`\n"
        f"📚 الملفات المؤرشفة: `{total_files}`\n\n"
        f"الأوامر:\n"
        f"• `/broadcast نص`\n"
        f"• `/set_schedule نص`"
    )
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=get_admin_panel_menu())
        await event.answer()
    else:
        await event.answer(text, reply_markup=get_admin_panel_menu())

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: types.CallbackQuery):
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM files")
    total_files = cursor.fetchone()[0]
    cursor.execute("SELECT value FROM settings WHERE key='translation_count'")
    res = cursor.fetchone()
    total_trans = res[0] if res else 0

    text = (
        f"📊 **الإحصائيات:**\n\n"
        f"👥 الطلاب: `{total_users}`\n"
        f"📚 الملفات: `{total_files}`\n"
        f"📝 الترجمات: `{total_trans}`\n"
    )
    await callback.message.edit_text(text, reply_markup=get_admin_panel_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_api")
async def cb_admin_api(callback: types.CallbackQuery):
    await callback.message.edit_text("⏳ جاري الفحص...")
    res = await check_api_usage()
    await callback.message.edit_text(res, reply_markup=get_admin_panel_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_settings")
async def cb_admin_settings(callback: types.CallbackQuery):
    await callback.message.edit_text("⚙️ **الإعدادات:**", reply_markup=get_admin_settings_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_toggle_bot")
async def cb_admin_toggle(callback: types.CallbackQuery):
    new_val = '0' if is_bot_active() else '1'
    cursor.execute("UPDATE settings SET value = ? WHERE key = 'bot_active'", (new_val,))
    db_conn.commit()
    await callback.message.edit_text("✅ تم التحديث.", reply_markup=get_admin_settings_menu())
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📢 **أرسل الرسالة:**")
    await state.set_state(AppStates.waiting_for_broadcast)
    await callback.answer()

@dp.message(AppStates.waiting_for_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    broadcast_msg = message.text.strip()
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    sent = 0
    await message.answer("⏳ جاري الإرسال...")
    for (u_id,) in users:
        try:
            await bot.send_message(u_id, f"📢 **تبليغ رسمي:**\n\n{broadcast_msg}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.answer(f"✅ تم الإرسال إلى {sent} طالب.", reply_markup=get_admin_panel_menu())
    await state.clear()

# ============================================================
# البحث في الأرشيف
# ============================================================
@dp.callback_query(F.data == "cmd_search_menu")
async def cb_search_menu(callback: types.CallbackQuery, state: FSMContext):
    if not is_bot_active() and not is_admin(callback.from_user.id):
        await callback.answer("الصيانة.")
        return
    await callback.message.edit_text("🔍 **اختر لغة البحث:**", reply_markup=get_search_lang_menu())
    await callback.answer()

@dp.callback_query(F.data.in_(["search_ar", "search_en"]))
async def cb_search_by_lang(callback: types.CallbackQuery, state: FSMContext):
    lang = "ar" if callback.data == "search_ar" else "en"
    await state.update_data(search_lang=lang)
    msg = "🔍 اكتب الكلمة المفتاحية:" if lang == "ar" else "🔍 Type the keyword:"
    await callback.message.edit_text(msg)
    await state.set_state(AppStates.waiting_for_search_query)
    await callback.answer()

@dp.message(AppStates.waiting_for_search_query)
async def process_search_query(message: types.Message, state: FSMContext):
    await state.update_data(search_query=message.text.strip().lower())
    await message.answer("📄 **حدد عدد النتائج:**", reply_markup=get_search_limit_menu())
    await state.set_state(AppStates.waiting_for_search_limit)

@dp.callback_query(AppStates.waiting_for_search_limit)
async def process_search_with_limit(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    query = data.get("search_query", "")
    limit = int(callback.data.replace("limit_", ""))

    cursor.execute(
        "SELECT id, file_name, file_id, rating_sum, rating_count FROM files WHERE keyword LIKE ? OR file_name LIKE ? LIMIT ?",
        (f"%{query}%", f"%{query}%", limit)
    )
    rows = cursor.fetchall()

    if not rows:
        await callback.message.answer(f"❌ لا توجد نتائج لـ '{query}'.",
                                     reply_markup=get_main_menu(callback.from_user.id))
    else:
        await callback.message.answer(f"📚 **النتائج ({len(rows)}):**\n" + "—" * 28)
        for idx, row in enumerate(rows, 1):
            _, name, tg_fid, r_sum, r_cnt = row
            avg_rate = round(r_sum / max(1, r_cnt), 1)
            try:
                await callback.message.answer_document(
                    tg_fid,
                    caption=f"📁 **{idx}:** `{name}`\n⭐ `{avg_rate}/5`"
                )
            except Exception:
                pass
        await callback.message.answer("القائمة:", reply_markup=get_main_menu(callback.from_user.id))
    await state.clear()
    await callback.answer()

# ============================================================
# إنشاء التقرير الأكاديمي
# ============================================================
@dp.callback_query(F.data == "cmd_lab")
async def cb_lab_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_bot_active() and not is_admin(callback.from_user.id):
        await callback.answer("الصيانة.")
        return
    await callback.message.edit_text("📝 **أرسل اسم الطالب الثلاثي:**")
    await state.set_state(AppStates.waiting_for_lab_student_name)
    await callback.answer()

@dp.message(AppStates.waiting_for_lab_student_name)
async def process_student_name(message: types.Message, state: FSMContext):
    await state.update_data(student_name=message.text.strip())
    await message.answer("🏛 **اسم القسم:**")
    await state.set_state(AppStates.waiting_for_lab_dept)

@dp.message(AppStates.waiting_for_lab_dept)
async def process_student_dept(message: types.Message, state: FSMContext):
    await state.update_data(dept=message.text.strip())
    await message.answer("📚 **المرحلة:**")
    await state.set_state(AppStates.waiting_for_lab_stage)

@dp.message(AppStates.waiting_for_lab_stage)
async def process_student_stage(message: types.Message, state: FSMContext):
    await state.update_data(stage=message.text.strip())
    await message.answer("☀️ **نوع الدراسة (صباحي/مسائي):**")
    await state.set_state(AppStates.waiting_for_lab_study_type)

@dp.message(AppStates.waiting_for_lab_study_type)
async def process_student_study_type(message: types.Message, state: FSMContext):
    await state.update_data(study_type=message.text.strip())
    await message.answer("🔬 **اسم التجربة والبيانات:**")
    await state.set_state(AppStates.waiting_for_lab_data)

@dp.message(AppStates.waiting_for_lab_data)
async def process_lab_input(message: types.Message, state: FSMContext):
    raw_data = message.text.strip()
    status_msg = await message.answer("✍️ **جاري صياغة التقرير...**")

    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(
        run_live_counter(status_msg, "صياغة بحث أكاديمي", stop_event)
    )

    prompt = (
        f"اكتب تقريراً بحثياً مختبرياً جامعياً مفصلاً بالإنجليزية للتجربة: {raw_data}.\n"
        "اكتب المعادلات بصيغة نصية واضحة وتجنب LaTeX.\n"
        "يجب أن يشمل 4 صفحات على الأقل:\n"
        "1. Abstract & Introduction\n"
        "2. Theoretical Background & Equations\n"
        "3. Apparatus & Materials\n"
        "4. Experimental Procedure\n"
        "5. Data & Calculations (Markdown table)\n"
        "6. Results & Discussion\n"
        "7. Conclusions\n"
        "8. References"
    )
    report_content = await ai_request_with_retry(prompt, status_msg=status_msg)

    stop_event.set()
    counter_task.cancel()

    if not report_content:
        await status_msg.delete()
        await message.answer("❌ تعذر إنشاء التقرير.")
        await state.clear()
        return

    await state.update_data(lab_title=raw_data[:40], lab_report=report_content)
    await status_msg.delete()
    await message.answer("✅ **اختر الصيغة:**", reply_markup=get_report_formats())
    await state.set_state(AppStates.waiting_for_lab_format)

@dp.callback_query(AppStates.waiting_for_lab_format)
async def export_lab_report(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    metadata = {
        'name': data.get('student_name') or "طالب",
        'dept': data.get('dept') or "هندسة النفط",
        'stage': data.get('stage') or "الثانية",
        'study_type': data.get('study_type') or "مسائي",
        'lab_title': data.get('lab_title', 'Experiment')
    }
    content = data.get("lab_report", "")
    fmt = callback.data

    status_msg = await callback.message.answer("⏳ **جاري إنشاء الملف...**")

    try:
        if fmt == "fmt_pdf":
            pdf_io = generate_full_academic_report(metadata, content)
            doc_file = BufferedInputFile(pdf_io.getvalue(),
                                        filename=f"Report_{metadata['lab_title']}.pdf")
            await status_msg.delete()
            await callback.message.answer_document(doc_file, caption="📑 تقريرك PDF.")
        elif fmt == "fmt_docx":
            doc_io = io.BytesIO()
            if DocxDocument:
                doc = DocxDocument()
                doc.add_heading(f"Report: {metadata['lab_title']}", 0)
                doc.add_paragraph(
                    f"Student: {metadata['name']}\nDept: {metadata['dept']}\n"
                    f"Stage: {metadata['stage']} - {metadata['study_type']}"
                )
                doc.add_paragraph(clean_math_text(content))
                doc.save(doc_io)
                doc_io.seek(0)
                doc_file = BufferedInputFile(doc_io.getvalue(),
                                            filename=f"Report_{metadata['lab_title']}.docx")
                await status_msg.delete()
                await callback.message.answer_document(doc_file, caption="📝 تقرير Word.")
            else:
                txt = BufferedInputFile(clean_math_text(content).encode("utf-8"),
                                       filename=f"Report.txt")
                await status_msg.delete()
                await callback.message.answer_document(txt, caption="📄 TXT (Word غير متوفر).")
        else:
            txt = BufferedInputFile(clean_math_text(content).encode("utf-8"),
                                   filename=f"Report.txt")
            await status_msg.delete()
            await callback.message.answer_document(txt, caption="📄 تقرير نصي.")
    except Exception as e:
        logging.error(f"Report error: {e}")
        await status_msg.delete()
        await callback.message.answer(f"❌ خطأ: {e}")

    await callback.message.answer("القائمة:", reply_markup=get_main_menu(callback.from_user.id))
    await state.clear()
    await callback.answer()

# ============================================================
# PowerPoint
# ============================================================
@dp.callback_query(F.data == "cmd_convert")
async def cb_convert_prompt(callback: types.CallbackQuery):
    if not is_bot_active() and not is_admin(callback.from_user.id):
        await callback.answer("الصيانة.")
        return
    await callback.message.edit_text(
        "🔄 **أرسل ملف .pptx:**",
        reply_markup=get_main_menu(callback.from_user.id)
    )
    await callback.answer()

# ============================================================
# استقبال المستندات
# ============================================================
@dp.message(F.document)
async def handle_incoming_documents(message: types.Message, state: FSMContext):
    if not is_bot_active() and not is_admin(message.from_user.id):
        return
    doc_name = message.document.file_name.lower()
    file_id = message.document.file_id

    if doc_name.endswith(".pptx") or doc_name.endswith(".ppt"):
        status_msg = await message.answer("📊 **جاري تحويل PowerPoint...**")
        stop_event = asyncio.Event()
        counter_task = asyncio.create_task(
            run_live_counter(status_msg, "تحويل المحتوى", stop_event)
        )
        try:
            file = await bot.get_file(file_id)
            io_file = io.BytesIO()
            await bot.download_file(file.file_path, destination=io_file)

            if Presentation and doc_name.endswith(".pptx"):
                pdf_io = convert_pptx_to_formatted_pdf(io_file, message.document.file_name)
                out_file = BufferedInputFile(
                    pdf_io.getvalue(),
                    filename=f"Converted_{message.document.file_name}.pdf"
                )
                stop_event.set()
                counter_task.cancel()
                await status_msg.delete()
                await message.answer_document(
                    out_file,
                    caption="✅ تم التحويل بنجاح!",
                    reply_markup=get_main_menu(message.from_user.id)
                )
            else:
                stop_event.set()
                counter_task.cancel()
                await status_msg.delete()
                await message.answer("⚠️ يرجى إرسال ملف PPTX.")
        except Exception as e:
            stop_event.set()
            counter_task.cancel()
            logging.error(f"PPTX error: {e}")
            await message.answer(f"❌ فشل التحويل: {e}")
        return

    if doc_name.endswith(".pdf"):
        await state.update_data(file_id=file_id, file_name=message.document.file_name)
        await message.answer("📥 **تم استلام PDF.** حدد الإجراء:",
                            reply_markup=get_pdf_actions())
        await state.set_state(AppStates.waiting_for_action)
        return

    await message.answer("📁 يدعم البوت PDF و PowerPoint.",
                        reply_markup=get_main_menu(message.from_user.id))


# ============================================================
# إجراءات PDF
# ============================================================
@dp.callback_query(AppStates.waiting_for_action)
async def process_pdf_action(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data
    data = await state.get_data()
    file_id = data.get("file_id")
    file_name = data.get("file_name")

    if action == "action_archive":
        cursor.execute(
            "INSERT INTO files (file_name, file_id, keyword) VALUES (?, ?, ?)",
            (file_name, file_id, file_name.lower())
        )
        db_conn.commit()
        await callback.message.edit_text("✅ تم أرشفة الملف.")
        await state.clear()

    elif action == "action_translate":
        await callback.message.edit_text(
            "📄 هل تريد ترجمة الملف كاملاً أم صفحات معينة؟\n\n"
            "- أرسل `الكل` للترجمة الكاملة.\n"
            "- أو حدد الصفحات (مثال: `1-5`)"
        )
        await state.set_state(AppStates.waiting_for_range)

    elif action == "action_summarize":
        status_msg = await callback.message.answer("⏳ **جاري التلخيص...**")
        stop_event = asyncio.Event()
        counter_task = asyncio.create_task(
            run_live_counter(status_msg, "تحليل المحتوى", stop_event)
        )
        try:
            file = await bot.get_file(file_id)
            pdf_io = io.BytesIO()
            await bot.download_file(file.file_path, destination=pdf_io)
            doc = fitz.open(stream=pdf_io.getvalue(), filetype="pdf")

            text_acc = "".join([
                f"\n{doc[i].get_text()}" for i in range(min(6, len(doc)))
            ])
            prompt = (
                f"لخص هذه المحاضرة في هندسة النفط بالعربية مع إبراز: "
                f"القوانين والمعادلات، التعاريف الهامة، والأسئلة الامتحانية المتوقعة:\n\n"
                f"{text_acc[:3500]}"
            )
            summary = await ai_request_with_retry(prompt, status_msg=status_msg)

            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            if summary:
                await send_long_message(
                    callback.message,
                    f"📑 **الملخص الأكاديمي ({file_name}):**\n\n{clean_math_text(summary)}"
                )
            else:
                await callback.message.answer("❌ تعذر التلخيص.")
        except Exception as e:
            stop_event.set()
            counter_task.cancel()
            logging.error(f"Summarize error: {e}")
            await callback.message.answer("❌ فشل التلخيص.")
        finally:
            await state.clear()

    elif action == "action_extract":
        status_msg = await callback.message.answer("⏳ **جاري الاستخراج...**")
        stop_event = asyncio.Event()
        counter_task = asyncio.create_task(
            run_live_counter(status_msg, "استخراج النصوص", stop_event)
        )
        try:
            file = await bot.get_file(file_id)
            pdf_io = io.BytesIO()
            await bot.download_file(file.file_path, destination=pdf_io)
            doc = fitz.open(stream=pdf_io.getvalue(), filetype="pdf")

            extracted = "".join([
                f"\n--- صفحة {i+1} ---\n{clean_math_text(doc[i].get_text())}"
                for i in range(min(8, len(doc)))
            ])
            txt_file = BufferedInputFile(
                extracted.encode("utf-8"),
                filename=f"Text_{file_name}.txt"
            )
            stop_event.set()
            counter_task.cancel()
            await status_msg.delete()
            await callback.message.answer_document(txt_file,
                                                  caption="📄 تم استخراج النصوص.")
        except Exception as e:
            stop_event.set()
            counter_task.cancel()
            logging.error(f"Extract error: {e}")
            await callback.message.answer("❌ فشل الاستخراج.")
        finally:
            await state.clear()

    await callback.answer()


# ============================================================
# الترجمة الهندسية
# ============================================================
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
            await message.answer("❌ اكتب النطاق بشكل صحيح مثل `1-5` أو `الكل`.")
            return

    status_msg = await message.answer(
        "📥 **جاري تنزيل الملف وبدء الترجمة...**\n"
        "🛡️ نظام الصفحات المزدوجة + حماية المعادلات"
    )
    try:
        file = await bot.get_file(file_id)
        pdf_io = io.BytesIO()
        await bot.download_file(file.file_path, destination=pdf_io)
        pdf_bytes = pdf_io.getvalue()

        processed_pdf = await process_pdf(pdf_bytes, file_name, start_p, end_p, status_msg)

        out_name = f"مترجم_هندسي_{file_name}"
        to_send = BufferedInputFile(processed_pdf.getvalue(), filename=out_name)

        await status_msg.delete()
        await message.answer_document(
            document=to_send,
            caption=(
                "✅ **تمت الترجمة الهندسية بنجاح!**\n\n"
                "💡 تم الاحتفاظ بالصفحات الإنجليزية، وإضافة شرح عربي منظم بجانبها، "
                "مع ترجمة الجداول والمعادلات بشكل دقيق."
            ),
            reply_markup=get_main_menu(message.from_user.id)
        )
    except Exception as e:
        logging.error(f"Translation error: {e}")
        await message.answer(f"❌ خطأ: {e}")
    finally:
        await state.clear()


# ============================================================
# القاموس
# ============================================================
@dp.callback_query(F.data == "cmd_dict")
async def cb_dict(callback: types.CallbackQuery, state: FSMContext):
    if not is_bot_active() and not is_admin(callback.from_user.id):
        await callback.answer("الصيانة.")
        return
    await callback.message.edit_text("📖 **أرسل المصطلح:**")
    await state.set_state(AppStates.waiting_for_dict_term)
    await callback.answer()

@dp.message(AppStates.waiting_for_dict_term)
async def process_dict(message: types.Message, state: FSMContext):
    term = message.text.strip()
    status_msg = await message.answer("🔍 **جاري البحث...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(
        run_live_counter(status_msg, f"البحث عن '{term}'", stop_event)
    )

    prompt = (
        f"اشرح المصطلح الهندسي النفطي '{term}' شرحاً دقيقاً لطلاب هندسة النفط، "
        f"مع أهميته الميدانية والمصطلحات المرتبطة."
    )
    res = await ai_request_with_retry(prompt, status_msg=status_msg)

    stop_event.set()
    counter_task.cancel()
    await status_msg.delete()
    if res:
        await send_long_message(
            message,
            f"📘 **المصطلح:** `{term}`\n\n{clean_math_text(res)}"
        )
    else:
        await message.answer("❌ تعذر جلب الشرح.")
    await message.answer("القائمة:", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()


# ============================================================
# مفسر المعادلات
# ============================================================
@dp.callback_query(F.data == "cmd_formula")
async def cb_formula(callback: types.CallbackQuery, state: FSMContext):
    if not is_bot_active() and not is_admin(callback.from_user.id):
        await callback.answer("الصيانة.")
        return
    await callback.message.edit_text("📐 **أرسل المعادلة:**")
    await state.set_state(AppStates.waiting_for_formula)
    await callback.answer()

@dp.message(AppStates.waiting_for_formula)
async def process_formula(message: types.Message, state: FSMContext):
    form = message.text.strip()
    status_msg = await message.answer("🔍 **جاري التحليل...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(
        run_live_counter(status_msg, "تحليل المعادلة", stop_event)
    )

    prompt = (
        f"اشرح المعادلة والرموز الرياضية التالية بالتفصيل بصيغة نصية واضحة: '{form}'. "
        f"وضح كل رمز، والوحدات الحقلية، وتطبيقاتها في هندسة النفط."
    )
    res = await ai_request_with_retry(prompt, status_msg=status_msg)

    stop_event.set()
    counter_task.cancel()
    await status_msg.delete()
    if res:
        await send_long_message(
            message,
            f"📐 **تفسير المعادلة:**\n\n{clean_math_text(res)}"
        )
    else:
        await message.answer("❌ تعذر التحليل.")
    await message.answer("القائمة:", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()


# ============================================================
# الحاسبة
# ============================================================
@dp.callback_query(F.data == "cmd_calc")
async def cb_calc(callback: types.CallbackQuery, state: FSMContext):
    if not is_bot_active() and not is_admin(callback.from_user.id):
        await callback.answer("الصيانة.")
        return
    await callback.message.edit_text("🧮 **أرسل المسألة:**")
    await state.set_state(AppStates.waiting_for_calc_input)
    await callback.answer()

@dp.message(AppStates.waiting_for_calc_input)
async def process_calc(message: types.Message, state: FSMContext):
    q = message.text.strip()
    status_msg = await message.answer("⚙ **جاري الحساب...**")
    stop_event = asyncio.Event()
    counter_task = asyncio.create_task(
        run_live_counter(status_msg, "الحساب", stop_event)
    )

    prompt = f"حل هذه المسألة الهندسية النفطية بخطوات واضحة واذكر القوانين والوحدات: {q}"
    res = await ai_request_with_retry(prompt, status_msg=status_msg)

    stop_event.set()
    counter_task.cancel()
    await status_msg.delete()
    if res:
        await send_long_message(message, f"🧮 **الحل:**\n\n{clean_math_text(res)}")
    else:
        await message.answer("❌ تعذر الحل.")
    await message.answer("القائمة:", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()


# ============================================================
# الجدول والتبليغات
# ============================================================
@dp.callback_query(F.data == "cmd_schedule")
async def cb_schedule(callback: types.CallbackQuery):
    cursor.execute("SELECT notice FROM schedules WHERE id = 1")
    notice = cursor.fetchone()[0]
    await callback.message.edit_text(
        f"📅 **الجدول والتبليغات الرسمية:**\n\n{notice}",
        reply_markup=get_main_menu(callback.from_user.id)
    )
    await callback.answer()


# ============================================================
# الأوامر الإدارية
# ============================================================
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    broadcast_msg = message.text.replace("/broadcast", "").strip()
    if not broadcast_msg:
        await message.answer("⚠️ اكتب نص الإذاعة.")
        return
    cursor.execute("SELECT user_id FROM users")
    users = cursor.fetchall()
    sent = 0
    for (u_id,) in users:
        try:
            await bot.send_message(u_id, f"📢 **تبليغ رسمي:**\n\n{broadcast_msg}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.answer(f"✅ تم الإرسال إلى {sent} طالب.")

@dp.message(Command("set_schedule"))
async def cmd_set_schedule(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    new_schedule = message.text.replace("/set_schedule", "").strip()
    if not new_schedule:
        await message.answer("⚠️ اكتب محتوى الجدول.")
        return
    cursor.execute("UPDATE schedules SET notice = ? WHERE id = 1", (new_schedule,))
    db_conn.commit()
    await message.answer("✅ تم تحديث الجدول.")


# ============================================================
# catch_all_text
# ============================================================
@dp.message(F.text)
async def catch_all_text(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        text = message.text.strip()
        if text == "الكل" or re.match(r'^\d+-\d+$', text):
            await message.answer(
                "⚠️ عذراً، تم فقدان الجلسة.\n\n"
                "يرجى إرسال ملف PDF من جديد لترجمته."
            )


# ============================================================
# Web Server (Keep-Alive)
# ============================================================
async def handle_ping(request):
    return web.Response(text="Engineering Bot is Live!")

async def keep_awake_loop():
    port = int(os.environ.get("PORT", 8080))
    url = f"http://127.0.0.1:{port}/"
    await asyncio.sleep(20)
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


# ============================================================
# Main
# ============================================================
async def main():
    if not GROQ_API_KEY and not API_KEYS:
        logging.error("❌ لا يوجد أي مزود AI مُفعّل! الرجاء إضافة GROQ_API_KEY")
        return

    if GROQ_API_KEY:
        await fetch_available_groq_models()
        if not GROQ_MODELS:
            logging.warning("⚠️ لم يتم العثور على نماذج Groq")

    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)

    providers = []
    if GROQ_API_KEY and GROQ_MODELS:
        providers.append(f"Groq ✅ ({len(GROQ_MODELS)})")
    if API_KEYS:
        providers.append(f"OpenRouter ✅ ({len(API_KEYS)})")
    if not providers:
        providers.append("⚠️ لا يوجد مزود!")

    logging.info(f"🚀 البوت يعمل — المزودون: {', '.join(providers)}")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
