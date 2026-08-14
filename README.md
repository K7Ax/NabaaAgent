# NabaaAgent — نبأ

[![Quality Gate](https://github.com/K7Ax/NabaaAgent/actions/workflows/quality.yml/badge.svg)](https://github.com/K7Ax/NabaaAgent/actions/workflows/quality.yml)

نبأ منصة Telegram عربية تكتشف فرص الطلاب مركزيًا، تتحقق من رابط التقديم والأدلة،
ثم تطابق الفرصة برمجيًا مع ملف كل طالب. تبدأ المنصة بجامعة الملك سعود، وتدعم
طلاب الجامعات الأخرى والفرص المتاحة في السعودية أو عن بُعد.

## ما الذي تغير في الإصدار 0.2

- البحث مركزي مرة واحدة بدل تكراره لكل طالب.
- دعم Telegram Webhook مع بقاء Long Polling للتطوير المحلي.
- Workflow مركزي يدعم الفحص السريع والعميق وإعادة التحقق؛ الجدولة السحابية تبقى
  متوقفة حتى تتوفر قاعدة دائمة و`NABAA_API_URL` و`INTERNAL_API_SECRET`.
- سجل مصادر، دورات فحص، إشارات خام، نسخ فرص، أدلة، صحة مصادر وميزانيات استخدام.
- 12 فئة: التدريب الصيفي والتعاوني، برامج الخريجين، الوظائف الجزئية والمبتدئة،
  الدورات، المعسكرات، المنح، المسابقات، الهاكاثونات، الفعاليات والتطوع.
- تصنيف KSU بإصدار مستقل، مع اختيار الجامعة والكلية والتخصص بالأزرار.
- اختيار أنواع متعددة من الفرص وأسئلة أهلية تدريجية بالأزرار.
- مطابقة حتمية لا تعتمد على LLM ودرجة مفهومة من 100.
- إشعارات فورية للفرص الأقوى وملخص يومي للبقية.
- إعادة فتح صفحة التقديم قبل الإرسال؛ الصفحة المغلقة أو غير المتاحة لا تُرسل.
- Tavily وGroq وOpenRouter للاكتشاف والتحليل فقط، وليس لاتخاذ قرار الأهلية.
- حد Tavily شهري مركزي يفشل بأمان قبل تجاوز الرصيد المجاني.
- HMAC مع مدة صلاحية خمس دقائق لكل واجهة تشغيل داخلية.
- ترحيل إضافي يحافظ على الطلاب والمحـفوظات والتسليمات من الإصدار السابق.

## البنية

```mermaid
flowchart LR
    O["مصادر رسمية وواجهات عامة"] --> C["جامع مركزي محلي أو في GitHub Actions"]
    X["بحث وإشارات مفهرسة"] --> C
    C --> V["تحقق حتمي + LangGraph عند الغموض"]
    V -->|"دفعة موقعة"| A["FastAPI على Railway"]
    A --> D[("SQLite WAL")]
    D --> M["محرك المطابقة"]
    M --> Q["طابور فوري + يومي"]
    Q --> T["Telegram Webhook"]
```

لا يُعد منشور LinkedIn أو X ولا موقع تجميعي دليلًا كافيًا. يمكن استخدامه كإشارة
للعثور على صفحة تقديم رسمية فقط. الفرصة غير المكتملة تبقى مخفية.

## التشغيل محليًا

يتطلب Python 3.11 أو أحدث:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

ضع الأسرار في `.env` محليًا ولا ترسلها في المحادثات أو Git. يمكن تشغيل الواجهة:

```powershell
.venv\Scripts\python -m uvicorn opportunity_sentinel.api:app --reload
```

يظل `opportunity-bot` متاحًا للتطوير المحلي باستخدام Long Polling، لكنه لا يشغل
بحثًا دوريًا لكل طالب. بيئة الإنتاج تستخدم Webhook فقط.

## نشر Railway

أنشئ خدمة واحدة من Dockerfile واربط Volume بالمسار `/app/data`. أضف المتغيرات:

```dotenv
APP_ENV=production
DATA_DB_PATH=/app/data/opportunity_sentinel.db
CHECKPOINT_DB_PATH=/app/data/opportunity_checkpoints.sqlite
PUBLIC_BASE_URL=https://YOUR-SERVICE.up.railway.app
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_CHAT_ID=...
TELEGRAM_WEBHOOK_SECRET=RANDOM_LONG_VALUE
INTERNAL_API_SECRET=ANOTHER_RANDOM_LONG_VALUE
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
TAVILY_API_KEY=...
TAVILY_MONTHLY_CREDIT_LIMIT=900
```

عند بدء الخدمة يُسجّل Webhook تلقائيًا. يعرض `/readiness` حالة المكونات وأعداد
السجلات دون كشف أي سر.

## أسرار GitHub Actions

أضف إلى Repository Secrets:

- `NABAA_API_URL`: رابط خدمة Railway.
- `INTERNAL_API_SECRET`: نفس القيمة الموجودة في Railway.
- `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`.

Workflow المسمى `Central Opportunity Discovery` ينفذ:

- `fast`: موصلات طويق، مهارات المستقبل، بوابة خريجي KSU، الأكاديمية المالية،
  ولوحات الشركات الرسمية Ashby/Lever/Greenhouse؛ بلا Tavily أو LLM، كل 30 دقيقة.
- `deep`: سبعة استعلامات في كل دورة ضمن مصفوفة دوارة تغطي 12 فئة خلال اليوم،
  مع إشارات LinkedIn وX كل 8 ساعات، باستخدام Tavily Basic حتى 20 نتيجة.
- `revalidate`: انتهاء المواعيد وإعادة فتح الروابط يوميًا.
- `deliver`: توزيع طابور المطابقات بعد كل تشغيل ناجح.

يمكن تشغيل أي وضع يدويًا من GitHub دون تغيير الكود.
الجدولة التلقائية ليست مفعلة حاليًا؛ تفعيلها قبل وجود API وقاعدة بيانات دائمين
سيصنع تشغيلات مؤقتة لا تخدم مستخدمي البوت.

## قياس التغطية

```powershell
.venv\Scripts\python scripts\coverage_report.py
```

يفصل التقرير بين المصادر الفعلية والإشارات والمصادر المخطط لها، ويعرض توزيع
الفرص والسجلات القديمة. لا يدّعي Recall من عدد المخزون؛ لا تُحسب النسبة إلا بعد
ملء `benchmarks/gold_opportunities.json` بمراجعة مستقلة للمصادر الرسمية.

## ضمان الجودة

```powershell
.venv\Scripts\python -m ruff check src tests scripts
.venv\Scripts\python -m pytest --cov=opportunity_sentinel --cov-fail-under=70
.venv\Scripts\python scripts\capstone_demo.py
```

الاختبارات تغطي الترحيل القديم، التوثيق، منع التكرار، المطابقة الواسعة والضيقة،
الأهلية التدريجية، HMAC، حدود Tavily، LangGraph، الأمن، Telegram وواجهات الخدمة.

## قواعد النشر غير القابلة للتجاوز

1. رابط تقديم رسمي قابل للفتح.
2. دليل رسمي على الجهة والموقع والأهلية.
3. موعد مستقبلي أو دليل صريح على أن التسجيل مفتوح.
4. الدورة التي توصف بالمجانية تحتاج دليلًا على عدم وجود رسوم.
5. الشرط الإلزامي المجهول ينتج سؤالًا للطالب، لا افتراضًا.
6. فشل LLM أو Tavily لا يخفض مستوى الأدلة المطلوب.
7. الفرصة المغلقة أو غير القابلة لإعادة التحقق لا تُرسل.

## حدود صريحة

لا توجد وسيلة قانونية أو تقنية تضمن اكتشاف كل إعلان في الإنترنت. نبأ يقيس تغطية
قائمة مصادر معلنة ويستخدم البحث لسد الفجوات. Railway Free ومزودو النماذج المجانية
لا يقدمون ضمان توفر؛ عند وصول الاستخدام إلى الحد يتوقف البحث العميق بأمان، بينما
تبقى البيانات وقواعد التوثيق دون تخفيض.

راجع [معمارية الإنتاج](docs/production-architecture.md)،
[إعداد Telegram](docs/telegram-setup.md)، و[سياسة الأمن](docs/security.md).
