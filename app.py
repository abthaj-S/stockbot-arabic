
from flask import Flask, request
import json
import os

app = Flask(__name__)

# ملف المخزون
INVENTORY_FILE = "inventory.json"

# تحميل المخزون
def load_inventory():
    if os.path.exists(INVENTORY_FILE):
        with open(INVENTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

# حفظ المخزون
def save_inventory(inventory):
    with open(INVENTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2)

# معالجة الرسائل
def process_message(message):
    inventory = load_inventory()
    message = message.strip()

    # كم باقي من المنتج؟
    if message.startswith("كم"):
        product = message.replace("كم باقي من", "").replace("كم", "").strip()
        if product in inventory:
            qty = inventory[product]
            if qty <= 5:
                return f"⚠️ {product}: {qty} فقط — يحتاج طلب عاجل"
            return f"✅ {product}: {qty} قطعة"
        return f"❌ {product} غير موجود في المخزون"

    # إضافة مخزون
    elif "أضف" in message or "اضف" in message:
        parts = message.replace("أضف", "").replace("اضف", "").strip().split()
        if len(parts) >= 2:
            qty = int(parts[0])
            product = " ".join(parts[1:])
            inventory[product] = inventory.get(product, 0) + qty
            save_inventory(inventory)
            return f"✅ تم إضافة {qty} من {product}. المجموع: {inventory[product]}"
        return "❌ الصيغة الصحيحة: اضف 10 أرز"

    # تقرير المخزون الكامل
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

@app.route("/webhook", methods=["POST"])
def webhook():
    message = request.form.get("Body", "")
    response = process_message(message)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message>{response}</Message></Response>"""

@app.route("/test", methods=["GET"])
def test():
    msg = request.args.get("msg", "تقرير")
    return process_message(msg)

if __name__ == "__main__":
    app.run(debug=True, port=5000)