# إعداد Telegram وRailway

## إنشاء البوت والمشرف

1. أنشئ البوت من الحساب الموثق `@BotFather` واحفظ token في مكان آمن.
2. أرسل رسالة للبوت، ثم استخرج رقم حساب المشرف مرة واحدة عبر `getUpdates` قبل
   تسجيل Webhook.
3. خزّن القيمتين في Railway باسم `TELEGRAM_BOT_TOKEN` و`TELEGRAM_ADMIN_CHAT_ID`.
4. لا تضع token في Git أو صورة أو سجل تشغيل.

## Webhook الإنتاجي

أنشئ قيمتين عشوائيتين مختلفتين وطويلتين:

```dotenv
PUBLIC_BASE_URL=https://YOUR-SERVICE.up.railway.app
TELEGRAM_WEBHOOK_SECRET=...
INTERNAL_API_SECRET=...
```

عند بدء FastAPI يسجّل `/telegram/webhook` تلقائيًا مع Telegram. كل تحديث يجب أن
يحمل header السري الرسمي، وإلا يعيد الخادم 401. لا تشغّل Long Polling بالتزامن مع
Webhook.

## تجربة الطالب

1. يضغط `/start` مرة واحدة.
2. يختار الجامعة والكلية والتخصص وسنة التخرج بالأزرار.
3. يختار عدة أنواع فرص ثم يضغط **تم الاختيار**.
4. **فرصي الآن** يعرض قاعدة الفرص الموثقة فورًا دون بحث خارجي بطيء.
5. **تأكيد أهليتي** يسأل فقط عن الشروط الناقصة للفرص الفعلية.
6. يستطيع حفظ الفرصة أو إخفاءها أو فتح صفحة التقديم الرسمية.

## الفحص

- `/health` يجب أن يعيد 200.
- `/readiness` يجب أن يظهر `database_ready` و`webhook_configured` بقيمة `true`.
- من GitHub Actions شغّل `Central Opportunity Discovery` بوضع `fast`.
- افحص أن `signals` و`opportunities` ظهرت في metrics.
- شغّل وضع `deliver` بعد وجود طالب وفرصة مطابقة.

إذا لم تصل رسالة، راجع Telegram token وWebhook secret، ثم حالة رابط التقديم. نبأ
يؤجل الإرسال عمدًا عندما لا يستطيع إعادة فتح الرابط الرسمي.
