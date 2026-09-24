#!/bin/bash
# دبل كليك على هذا الملف لتشغيل التطبيق على ماك
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ بايثون غير مثبت — ثبّته من python.org ثم شغّل الملف مرة ثانية"
  open "https://www.python.org/downloads/"
  read -r -p "اضغط Enter للإغلاق"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "⏳ أول تشغيل: نجهّز التطبيق، انتظر شوي..."
  python3 -m venv .venv || { read -r -p "❌ فشل التجهيز. اضغط Enter"; exit 1; }
fi
.venv/bin/python -m pip install -q -U --disable-pip-version-check -r requirements.txt \
  || { read -r -p "❌ فشل تثبيت المكتبات، تأكد من الإنترنت. اضغط Enter"; exit 1; }

OPEN_BROWSER=1 .venv/bin/python app.py
