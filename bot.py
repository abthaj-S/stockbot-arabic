import json
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ضعي التوكن هنا
TOKEN = "8677643524:AAFMlP0a-r9Dc0AiORUVk9jbQHw2Ez958cM"

INVENTORY_FILE = "inventory.json"

def load_inventory():
    if os.path.exists(INVENTORY_FILE):
        with open(INVENTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_inventory(inventory):
    with open(INVENTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2)

def process_message(message):
    inventory = load_inventory()
    message = message.strip()

    if message.startswith("كم"):
        product = message.replace("كم باقي من", "").replace("كم", "").strip()
        if product in inventory:
            qty = inventory[product]
            if qty <= 5:
                return f"⚠️ {product}: {qty} فقط — يحتاج طلب عاجل"
            return f"✅ {product}: {qty} قطعة"
        return f"❌ {product} غير موجود في المخزون"

    elif "أضف" in message or "اضف" in message:
        parts = message.replace("أضف", "").replace("اضف", "").strip().split()
        if len(parts) >= 2:
            qty = int(parts[0])
            product = " ".join(parts[1:])
            inventory[product] = inventory.get(product, 0) + qty
            save_inventory(inventory)
            return f"✅ تم إضافة {qty} من {product}. المجموع: {inventory[product]}"
        return "❌ الصيغة الصحيحة: اضف 10 أرز"

    elif message == "تقرير":
        if not inventory:
            return "المخزون فارغ"
        report = "📦 تقرير المخزون:\n"
        for item, qty in inventory.items():
            status = "⚠️" if qty <= 5 else "✅"
            report += f"{status} {item}: {qty}\n"
        return report

    else:
        return "الأوامر المتاحة:\n• كم باقي من [منتج]\n• اضف [كمية] [منتج]\n• تقرير"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text
    response = process_message(message)
    await update.message.reply_text(response)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("البوت شغال...")
    app.run_polling()