import asyncio

from app.logger import initialize_workbook
from app.message_processor import process_message


class FakeChat:
    title = "Nafezly - نفذلي"


class FakeMessage:
    buttons = None


class FakeEvent:
    id = 999999999
    raw_text = """
تم إضافة مشروع جديد على منصة نفذلي

عنوان المشروع:
تنظيف وتحضير بيانات مبيعات متجر إلكتروني

تفاصيل المشروع:
مطلوب خبير تحليل بيانات لتنظيف ملف مبيعات يحتوي على أكثر من 5,000 سجل، ثم بناء لوحة معلومات تفاعلية (Power BI) تتضمن تحليل المبيعات، وتشمل المشاكل التالية:
القيم المفقودة (Null values)، أسماء المنتجات غير الموحدة، التكرارات، تنسيق التواريخ، وصيغ النصوص، وقيم سالبة غير منطقية في خانة الكميات.

الميزانية: 10 - 25 دولار

رابط المشروع:
https://nafezly.com/project/52081
"""

    chat = FakeChat()
    message = FakeMessage()


initialize_workbook()

asyncio.run(process_message(FakeEvent()))
