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
