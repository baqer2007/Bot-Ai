import os
import io
import asyncio
import logging
import urllib.request
import re
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from openai import AsyncOpenAI
import fitz  # PyMuPDF
import arabic_reshaper
from bidi.algorithm import get_display

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = "7143420501:AAHCwidQ6V-d6jUNG9rHB_6lrSW9LjOMjEs"

# قراءة جميع المفاتيح من Render
KEYS_STRING = os.environ.get("OPENROUTER_API_KEYS", os.environ.get("OPENROUTER_API_KEY", ""))
API_KEYS = [k.strip() for k in KEYS_STRING.split(",") if k.strip()]

FONT_PATH = "Amiri-Regular.ttf"
if not os.path.exists(FONT_PATH):
    try:
        logging.info("Downloading Arabic Font...")
        urllib.request.urlretrieve("https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf", FONT_PATH)
    except Exception as e:
        logging.error(f"Font download error: {e}")

# إنشاء عملاء OpenRouter بناءً على المفاتيح المتوفرة
clients = []
for key in API_KEYS:
    clients.append(AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=key,
        timeout=60.0
    ))

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

def get_main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 كيفية الترجمة؟", callback_data="how_to")],
            [
                InlineKeyboardButton(text="ℹ️️ حول البوت", callback_data="about"),
                InlineKeyboardButton(text="⚙️ الإعدادات", callback_data="settings")
            ]
        ]
    )

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    welcome_text = (
        "👋 مرحباً بك في **المترجم الأكاديمي الاحترافي**!\n\n"
        "أنا هنا لمساعدتك في ترجمة المحاضرات الهندسية والعلمية (PDF) ووضع الترجمة العربية بشكل أنيق تحت الأسطر الإنجليزية.\n\n"
        "👇 اختر من القائمة أدناه أو أرسل ملف PDF للبدء فوراً:"
    )
    await message.answer(welcome_text, reply_markup=get_main_menu())

@dp.callback_query(F.data == "how_to")
async def cb_how_to(callback: types.CallbackQuery):
    text = "📄 **كيفية الترجمة:**\n\nفقط قم بإرسال أي ملف PDF للمحاضرة في هذه الدردشة. سأقوم بتحليله، ترجمة النصوص بدقة، وإرسال نسخة PDF جديدة لك تحتوي على الترجمة العربية باللون الأحمر أسفل كل سطر."
    await callback.message.edit_text(text, reply_markup=get_main_menu())
    await callback.answer()

@dp.callback_query(F.data == "about")
async def cb_about(callback: types.CallbackQuery):
    text = "ℹ️ **حول البوت:**\n\nتم تطوير هذا البوت لطلاب الجامعات والمهندسين. يعتمد على الذكاء الاصطناعي لترجمة المصطلحات الأكاديمية ووضعها بشكل متناسق داخل ملف الـ PDF الأصلي دون تخريب تصميمه."
    await callback.message.edit_text(text, reply_markup=get_main_menu())
    await callback.answer()

@dp.callback_query(F.data == "settings")
async def cb_settings(callback: types.CallbackQuery):
    text = f"⚙️ **الإعدادات:**\n\n🔹 **النموذج:** OpenRouter Free (الأكثر دقة)\n🔹 **حجم الخط العربي:** 8.0\n🔹 **المفاتيح النشطة:** {len(clients)} مفتاح للتبديل التلقائي\n\n*(البوت يعمل بنظام التبديل الذكي لضمان الاستقرار)*"
    await callback.message.edit_text(text, reply_markup=get_main_menu())
    await callback.answer()

async def translate_blocks(blocks_text: list) -> list:
    if not blocks_text or not clients:
        return []
    
    prompt = "ترجم النصوص التالية إلى اللغة العربية بدقة أكاديمية. أعد كتابة الترجمة بنفس الترقيم بالضبط (رقم|| النص المترجم). لا تكتب أي مقدمات أو شروحات إضافية.\n\n"
    for i, text in enumerate(blocks_text):
        prompt += f"{i}|| {text}\n"

    # المحاولة باستخدام المفاتيح المتوفرة
    for client in clients:
        try:
            response = await client.chat.completions.create(
                model="openrouter/free",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            
            content = response.choices[0].message.content
            if not content:
                continue
                
            raw_res = content.strip()
            translated_results = ["" for _ in range(len(blocks_text))]
            
            for line in raw_res.split('\n'):
                if '||' in line:
                    parts = line.split('||', 1)
                    num_str = parts[0].strip()
                    if num_str.isdigit():
                        idx = int(num_str)
                        if 0 <= idx < len(blocks_text):
                            translated_results[idx] = parts[1].strip()
            
            # إذا تمت الترجمة بنجاح وتم إرجاع قائمة مناسبة
            return translated_results
        
        except Exception as e:
            logging.warning(f"فشل المفتاح الحالي، جاري الانتقال للمفتاح التالي. الخطأ: {e}")
            continue
            
    logging.error("استنفدت جميع المفاتيح المتاحة.")
    return ["" for _ in blocks_text]

def split_text_to_fit(text, max_length=95):
    words = text.split()
    lines = []
    current_line = ""
    for word in words:
        if len(current_line) + len(word) + 1 <= max_length:
            current_line += (word + " ")
        else:
            lines.append(current_line.strip())
            current_line = word + " "
    if current_line:
        lines.append(current_line.strip())
    return lines

async def process_pdf_interlinear(pdf_bytes: bytes) -> io.BytesIO:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    if os.path.exists(FONT_PATH):
        font_to_use = "amiri"
    else:
        font_to_use = "helv"

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        if font_to_use == "amiri":
            page.insert_font(fontname="amiri", fontfile=FONT_PATH)

        blocks = page.get_text("blocks")
        text_blocks = []
        valid_coords = []
        
        for b in blocks:
            if b[6] == 0:
                txt = b[4].strip()
                if len(txt) > 5 and re.search('[a-zA-Z]{3,}', txt):
                    text_blocks.append(txt.replace("\n", " "))
                    valid_coords.append((b[0], b[1], b[2], b[3]))

        if not text_blocks:
            continue

        translations = await translate_blocks(text_blocks)

        for i, coord in enumerate(valid_coords):
            if i >= len(translations):
                break

            ar_text = str(translations[i]).strip()
            if not ar_text or ar_text == text_blocks[i]:
                continue

            try:
                wrapped_lines = split_text_to_fit(ar_text)
                x0, y0, x1, y1 = coord
                y_offset = y1 + 9 # زيادة المسافة قليلاً لتحسين الوضوح
                
                for line in wrapped_lines:
                    reshaped = arabic_reshaper.reshape(line)
                    bidi_text = get_display(reshaped)
                    
                    insert_point = fitz.Point(x0, min(y_offset, page.rect.height - 5))
                    
                    try:
                        page.insert_text(
                            insert_point,
                            bidi_text,
                            fontname=font_to_use,
                            fontsize=8.0, # تكبير الخط قليلاً ليكون مقروءاً بشكل أفضل
                            color=(0.7, 0.1, 0.1),
                            rotate=0
                        )
                    except Exception as e:
                        logging.error(f"Error inserting text: {e}")
                    
                    y_offset += 11 

            except Exception as e:
                 logging.error(f"Error processing arabic text: {e}")

        await asyncio.sleep(1.0)

    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    return output

@dp.message(F.document)
async def handle_pdf(message: types.Message):
    doc_info = message.document
    if not doc_info.file_name.lower().endswith(".pdf"):
        await message.answer("⚠️ يرجى إرسال ملف بصيغة PDF فقط.")
        return

    status_msg = await message.answer("📥 استلمت الملف... جاري الترجمة وتنسيق الأسطر بأعلى دقة ⏳")

    try:
        pdf_io = io.BytesIO()
        await bot.download(doc_info, destination=pdf_io)
        pdf_bytes = pdf_io.getvalue()

        processed_pdf_io = await process_pdf_interlinear(pdf_bytes)

        out_name = f"مترجم_{doc_info.file_name}"
        to_send = BufferedInputFile(processed_pdf_io.getvalue(), filename=out_name)

        await status_msg.delete()
        await message.answer_document(
            document=to_send,
            caption="✅ تمت الترجمة والتنسيق بنجاح! احتفظ بهذا الملف.",
            reply_markup=get_main_menu()
        )

    except Exception as e:
        logging.error(f"خطأ أثناء المعالجة: {e}")
        try:
            await status_msg.edit_text(f"❌ حدث خطأ أثناء المعالجة: {e}")
        except TelegramBadRequest:
            await message.answer(f"❌ حدث خطأ أثناء المعالجة: {e}")

async def handle_ping(request):
    return web.Response(text="Bot is running alive!")

async def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await start_web_server()
    logging.info(f"🚀 البوت يعمل الآن بقوة {len(clients)} مفتاح(مفاتيح) من OpenRouter...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
