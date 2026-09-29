import os
import io
import asyncio
import logging
import json
import urllib.request
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile
from openai import OpenAI
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

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY.strip(),
    timeout=60.0
)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

async def translate_blocks(blocks_text: list) -> list:
    if not blocks_text:
        return []
    
    prompt = "Translate the following JSON array of English texts into a JSON array of Arabic texts. Return ONLY a valid JSON array. No explanations.\n"
    prompt += json.dumps(blocks_text)

    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="openrouter/free",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
        )
        raw_res = response.choices[0].message.content.strip()
        
        if raw_res.startswith("```json"): raw_res = raw_res[7:]
        if raw_res.startswith("```"): raw_res = raw_res[3:]
        if raw_res.endswith("```"): raw_res = raw_res[:-3]
            
        return json.loads(raw_res.strip())
    except Exception as e:
        logging.error(f"Translation Error: {e}")
        return []

def process_pdf_interlinear(pdf_bytes: bytes, max_pages: int = 5) -> io.BytesIO:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_limit = min(len(doc), max_pages)

    for page_idx in range(pages_limit):
        page = doc[page_idx]
        blocks = page.get_text("blocks")
        
        if os.path.exists(FONT_PATH):
            page.insert_font(fontname="amiri", fontfile=FONT_PATH)
            font_to_use = "amiri"
        else:
            font_to_use = "helv"

        text_blocks = []
        valid_coords = []
        for b in blocks:
            if b[6] == 0:
                txt = b[4].strip()
                if len(txt) > 3 and not txt.isdigit():
                    text_blocks.append(txt.replace("\n", " "))
                    valid_coords.append((b[0], b[1], b[2], b[3]))

        if not text_blocks:
            continue

        try:
            translations = asyncio.run(translate_blocks(text_blocks))
        except Exception:
            continue

        if isinstance(translations, list):
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
                insert_point = fitz.Point(x0, min(y1 + 10, page.rect.height - 10))

                try:
                    page.insert_text(
                        insert_point,
                        bidi_text,
                        fontname=font_to_use,
                        fontsize=8.5,
                        color=(0.8, 0.1, 0.1),
                        rotate=0
                    )
                except Exception as e:
                    logging.error(f"Error inserting text: {e}")

    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    return output

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    await message.answer(
        "👋 مرحباً بك في **المترجم الأكاديمي السحابي**!\n\n"
        "أرسل ملف المحاضرة (PDF) وستتم طباعة الترجمة أسفل كل سطر بخط عربي واضح 📄✨."
    )

@dp.message(F.document)
async def handle_pdf(message: types.Message):
    doc_info = message.document
    if not doc_info.file_name.lower().endswith(".pdf"):
        await message.answer("⚠️ يرجى إرسال ملف بصيغة PDF فقط.")
        return

    status_msg = await message.answer("📥 جاري الترجمة وتضمين الخطوط العربية في الملف...")

    try:
        pdf_io = io.BytesIO()
        await bot.download(doc_info, destination=pdf_io)
        pdf_bytes = pdf_io.getvalue()

        loop = asyncio.get_running_loop()
        processed_pdf_io = await loop.run_in_executor(
            None,
            lambda: process_pdf_interlinear(pdf_bytes, max_pages=5)
        )

        out_name = f"مترجم_{doc_info.file_name}"
        to_send = BufferedInputFile(processed_pdf_io.getvalue(), filename=out_name)

        await status_msg.delete()
        await message.answer_document(
            document=to_send,
            caption="✅ تمت الترجمة بنجاح وبدون أي أخطاء!"
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
    logging.info("🚀 البوت يعمل الآن...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
