# النشر والتشغيل — دليل عملي

الهدف: رابط شغّال على الإنترنت تقدر تفتحه قدام تاجر في أقل من ساعة.

---

## 0. قبل أي نشر — 3 حاجات إجبارية

| المتغير | ليه | مثال |
| --- | --- | --- |
| `GEMINI_API_KEY` | الاستخراج | من <https://aistudio.google.com/apikey> |
| `APP_PASSWORD` أو `APP_USERS` | من غيرها هيتولّد باسورد عشوائي كل مرة يقوم فيها السيرفر | `APP_USERS=ahmed:Str0ngPass,mona:An0ther` |
| `APP_SESSION_SECRET` | من غيرها كل restart بيطفّش كل الجلسات | `python -c "import secrets;print(secrets.token_urlsafe(48))"` |

وكمان:

```
SESSION_HTTPS_ONLY=true          # لما تكون على https
ORDERS_DB_PATH=/app/data/orders.db   # لازم يبقى على volume دائم
```

> ⚠️ **`ORDERS_DB_PATH` أهم سطر في الملف ده.** لو الـ SQLite قاعد على قرص مؤقت،
> كل الطلبات المعتمدة بتتمسح مع أول redeploy. ركّب volume.

---

## 1. Railway (أسرع طريق — موصى به للـ pilot الأول)

```bash
npm i -g @railway/cli
railway login
railway init                      # اختر الريبو
railway volume add --mount-path /app/data
railway variables set \
  GEMINI_API_KEY=... \
  APP_USERS=ahmed:StrongPass \
  APP_SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))") \
  SESSION_HTTPS_ONLY=true \
  ORDERS_DB_PATH=/app/data/orders.db
railway up
railway domain                    # بيطلعلك الرابط العام
```

`railway.json` موجود في الريبو ومظبوط على `Dockerfile` و health check على
`/api/health`.

---

## 2. Fly.io

```bash
fly launch --no-deploy            # بيقرا fly.toml الموجود
fly volumes create wop_data --size 1 --region cdg
fly secrets set \
  GEMINI_API_KEY=... \
  APP_USERS=ahmed:StrongPass \
  APP_SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
fly deploy
fly open
```

---

## 3. Docker على أي VPS

```bash
docker build -t wop .
docker volume create wop_data
docker run -d --name wop -p 8000:8000 \
  -v wop_data:/app/data \
  -e GEMINI_API_KEY=... \
  -e APP_USERS='ahmed:StrongPass' \
  -e APP_SESSION_SECRET='...' \
  -e ORDERS_DB_PATH=/app/data/orders.db \
  --restart unless-stopped \
  wop
```

حُط nginx / Caddy قدامه للـ TLS، وبعدين `SESSION_HTTPS_ONLY=true`.

---

## 4. تشغيل محلي للديمو (من غير إنترنت عام)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

لو هتعمل ديمو من لابتوبك لحد في مكان تاني، أسرع نفق مؤقت:

```bash
# اختر أي واحد
cloudflared tunnel --url http://localhost:8000
ssh -R 80:localhost:8000 serveo.net
```

---

## 5. تشيك ليست قبل ما تفتح الرابط لأي حد

```bash
curl -s https://YOUR_DOMAIN/api/health
```

لازم ترجع `"status":"healthy"` و `"auth_enforced":true` و `catalog_loaded > 0`.

- [ ] `/` بيوديك على `/login` وأنت مش داخل
- [ ] `curl https://YOUR_DOMAIN/api/orders` بيرجّع **401**
- [ ] الباسورد مش الافتراضي ومش في أي commit
- [ ] `ORDERS_DB_PATH` على volume دائم — اعمل redeploy وتأكد إن الطلبات لسه موجودة
- [ ] `SESSION_HTTPS_ONLY=true`
- [ ] رفعت كتالوج التاجر (مش الكتالوج التجريبي) قبل الاجتماع
- [ ] جربت رسالة حقيقية من رسائله ومشيت الدورة كاملة لحد التصدير

---

## 6. النسخ الاحتياطي (سطر واحد، شغّله يومياً)

```bash
sqlite3 /app/data/orders.db ".backup '/app/data/backup-$(date +%F).db'"
```

الـ SQLite شغّال WAL، فالأمر ده آمن والسيرفر شغّال.

---

## 7. حدود معروفة — قولها للعميل بصراحة

| الحد | التفاصيل |
| --- | --- |
| **مستأجر واحد** | نسخة واحدة = تاجر واحد = كتالوج واحد. تاني عميل = نسخة تانية. |
| **SQLite** | ممتازة لعشرات آلاف الطلبات على نسخة واحدة. مش للتوزيع على أكتر من سيرفر. |
| **الهوية** | باسورد مشترك أو حسابات باسم/كلمة مرور. مفيش SSO ولا 2FA. |
| **حصة Gemini** | الطبقة المجانية بتترمي 429 تحت الضغط. النظام بيعمل retry مع backoff، بس تحت حِمل حقيقي هتحتاج حساب مدفوع. |
| **مفيش واتساب مباشر** | لسه بالنسخ واللصق. الربط بواتساب خطوة لاحقة — ومتعملهاش قبل ما عميل يدفع. |

---

## 8. المتغيرات كلها

| المتغير | افتراضي | الوصف |
| --- | --- | --- |
| `GEMINI_API_KEY` | – | مفتاح الاستخراج |
| `GEMINI_MODELS` | `gemini-3.6-flash,…` | سلسلة الموديلات بالترتيب |
| `APP_PASSWORD` | عشوائي | باسورد مشترك |
| `APP_USERS` | – | `name:pass,name:pass` (له الأولوية) |
| `APP_SESSION_SECRET` | عشوائي | توقيع الكوكي |
| `APP_SESSION_MAX_AGE` | `43200` | عمر الجلسة بالثواني |
| `SESSION_HTTPS_ONLY` | `false` | خلّيها `true` على https |
| `ORDERS_DB_PATH` | `./orders.db` | مسار قاعدة البيانات |
| `CATALOG_PATH` | `./catalog.csv` | كتالوج البذرة (أول تشغيل بس) |
| `LOG_LEVEL` | `INFO` | مستوى السجل |
| `RR_K_AUTO_ACCEPT_ENABLED` | `false` | خامل: أي قيمة بتتسجّل وتتجاهل |
