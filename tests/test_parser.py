from pprint import pprint

from app.parser import parse_job

text = """
تم إضافة مشروع جديد على منصة نفذلي

عنوان المشروع : إعادة بناء ملفات PDF احترافية داخل Microsoft Word

تفاصيل المشروع : أبحث عن خبير Microsoft Word محترف جدًا لديه خبرة حقيقية في تصميم المستندات الاحترافية وإعادة بنائها داخل Word.

المشروع ليس تحويل PDF إلى Word.

المطلوب هو إعادة بناء المستند بالكامل داخل Word.

الميزانية : 10 - 25 دولار

رابط المشروع : https://nafezly.com/project/52103
"""

pprint(parse_job("Nafezly - نفذلي", text))
