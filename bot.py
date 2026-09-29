import os
import io
import asyncio
import logging
import urllib.request
import re
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile
from openai import AsyncOpenAI
import fitz  # PyMuPDF
import arabic_reshaper
from bidi.algorithm import get_display

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = "7143420501:AAHCwidQ6V-d6jUNG9rHB_6lrSW9LjOMjEs"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

FONT_PATH = "Amiri-Regular.ttf"
if not os.path.exists(FONT_PATH):
    try:
        logging.info("Downloading Arabic Font...")
        urllib.request.urlretrieve("https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf", FONT_PATH)
    except Exception as e:
        logging.error(f"Font download error: {e}")

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY.strip(),
    timeout=40.0
)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

async def translate_blocks(blocks_text: list) -> list:
    if not blocks_text:
        return []
    
    prompt = "ترجم النصوص التالية إلى اللغة العربية. أعد كتابة الترجمة بنفس الترقيم بالضبط (رقم|| النص المترجم). لا تكتب أي مقدمات أو شروحات إضافية.\n\n"
    for i, text in enumerate(blocks_text):
        prompt += f"{i}|| {text}\n"

    try:
        # العودة للموجه التلقائي المستقر (لن يعطي 404 أبداً)
        response = await client.chat.completions.create(
            model="openrouter/free",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        
        content = response.choices[0].message.content
        if not content:
            return ["" for _ in blocks_text]
            
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
        
        return translated_results
    except Exception as e:
        logging.error(f"Translation Error: {e}")
        return ["" for _ in blocks_text]

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
                # الفلترة الذكية: أخذ النصوص التي تحتوي حروف إنجليزية فقط
                if len(txt) > 2 and re.search('[a-zA-Z]', txt):
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
                reshaped = arabic_reshaper.reshape(ar_text)
                bidi_text = get_display(reshaped)
            except Exception:
                bidi_text = ar_text

            x0, y0, x1, y1 = coord
            insert_point = fitz.Point(x0, min(y1 + 4, page.rect.height - 5))

            try:
                page.insert_text(
                    insert_point,
                    bidi_text,
                    fontname=font_to_use,
                    fontsize=6.5,
                    color=(0.7, 0.1, 0.1),
                    rotate=0
                )
            except Exception as e:
                logging.error(f"Error inserting text: {e}")

        await asyncio.sleep(1.0)

    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    return output

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    await message.answer(
        "👋 مرحباً بك في **المترجم الأكاديمي السريع**!\n\n"
        "أرسل ملف المحاضرة (PDF) وسأترجمه بسرعة متجاهلاً الأرقام والرموز للحفاظ على التنسيق 📄✨."
    )

@dp.message(F.document)
async def handle_pdf(message: types.Message):
    doc_info = message.document
    if not doc_info.file_name.lower().endswith(".pdf"):
        await message.answer("⚠️ يرجى إرسال ملف بصيغة PDF فقط.")
        return

    status_msg = await message.answer("📥 جاري الترجمة الدقيقة (تم تجاوز الجداول والأرقام لتسريع العملية)...")

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
            caption="✅ تمت الترجمة بالكامل بنجاح!"
        )

    except Exception as e:
        logging.error(f"خطأ أثناء المعالجة: {e}")
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
    logging.info("🚀 البوت يعمل الآن بالنسخة المستقرة والسريعة...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
