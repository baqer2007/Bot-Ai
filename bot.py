import os
import io
import asyncio
import logging
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

# الكود الآن سيبحث عن المفتاح في مكان سري داخل Render ولن يتم فضحه في GitHub
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY.strip(),
    timeout=35.0
)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

SYSTEM_TRANSLATE_PROMPT = """
أنت مترجم أكاديمي تخصصي للمحاضرات العلمية.
ترجم كل فقرة بدقة إلى العربية وضع علامة ||| حصراً بين كل ترجمة والتي تليها دون أي إضافات.
"""

async def translate_blocks(blocks_text: list) -> list:
    if not blocks_text:
        return []
    prompt = "ترجم كل فقرة مما يلي إلى العربية وافصل بينها بـ ||| فقط:\n\n" + "\n---SPLIT---\n".join(blocks_text)

    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="openrouter/free",
                messages=[
                    {"role": "system", "content": SYSTEM_TRANSLATE_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
            )
        )
        raw_res = response.choices[0].message.content
        return [t.strip() for t in raw_res.split("|||")]
    except Exception as e:
        logging.error(f"خطأ أثناء الترجمة: {e}")
        return []

def process_pdf_interlinear(pdf_bytes: bytes, max_pages: int = 5) -> io.BytesIO:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_limit = min(len(doc), max_pages)

    for page_idx in range(pages_limit):
        page = doc[page_idx]
        blocks = page.get_text("blocks")

        text_blocks = []
        valid_coords = []
        for b in blocks:
            if b[6] == 0:
                txt = b[4].strip()
                if len(txt) > 3:
                    text_blocks.append(txt.replace("\n", " "))
                    valid_coords.append((b[0], b[1], b[2], b[3]))

        if not text_blocks:
            continue

        try:
            translations = asyncio.run(translate_blocks(text_blocks))
        except Exception as e:
            logging.error(f"خطأ صفحة {page_idx}: {e}")
            continue

        for i, coord in enumerate(valid_coords):
            if i >= len(translations):
                break

            ar_text = translations[i].strip()
            if not ar_text:
                continue

            try:
                reshaped = arabic_reshaper.reshape(ar_text)
                bidi_text = get_display(reshaped)
            except Exception:
                bidi_text = ar_text

            x0, y0, x1, y1 = coord
            insert_point = fitz.Point(x0, min(y1 + 8, page.rect.height - 10))

            page.insert_text(
                insert_point,
                bidi_text,
                fontsize=7.5,
                color=(0.75, 0.05, 0.05),
                rotate=0
            )

    output = io.BytesIO()
    doc.save(output)
    doc.close()
    output.seek(0)
    return output

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    await message.answer(
        "👋 مرحباً بك في **المترجم الأكاديمي السحابي**!\n\n"
        "أرسل ملف المحاضرة (PDF) وستتم طباعة الترجمة أسفل كل سطر مع الحفاظ على التصميم 📄✨."
    )

@dp.message(F.document)
async def handle_pdf(message: types.Message):
    doc_info = message.document
    if not doc_info.file_name.lower().endswith(".pdf"):
        await message.answer("⚠️ يرجى إرسال ملف بصيغة PDF فقط.")
        return

    status_msg = await message.answer("📥 جاري تحليل المحاضرة وإدراج الترجمة بين السطور...")

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
            caption="✅ تم إدراج الترجمة أسفل كل سطر بنجاح!"
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
    logging.info("🚀 البوت يعمل الآن في السحاب بنجاح...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
