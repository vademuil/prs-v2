# Steam Publisher Revenue Calculator

Streamlit-приложение для расчёта дохода издателя по региональным ценам Steam с учётом VAT и комиссии дистрибьютора.

## Что делает

На вход:
- **AppID Steam** (например, `730` для CS2) — получает фактические текущие цены через Steam Store API.
- ИЛИ **базовая цена в USD** — конвертирует в локальные валюты по FX (mode B, приблизительный).
- **Комиссия дистрибьютора, %** — процент, удерживаемый реселлером/площадкой.

На выход — таблица по ~70 странам:

| Регион | Валюта | Цена в локальной | Цена в USD по курсу | VAT % | Цена в USD без VAT | Доход издателя (локальная) | Доход издателя (USD) |

Можно экспортировать как CSV.

## Логика расчёта (для одной страны)

```
local_price        = Steam Store API → final / 100
local_price_ex_vat = local_price / (1 + vat_rate)        # только если VAT > 0 в Steam tax FAQ
publisher_local    = local_price_ex_vat * (1 - distributor_fee/100)
publisher_usd      = publisher_local / fx_rate(currency, USD)
```

## Источники данных

- **Цены:** [Steam Store API](https://store.steampowered.com/api/appdetails) — публичный, без ключей.
- **VAT:** [Steam tax FAQ → Current Tax Rates](https://partner.steamgames.com/doc/finance/taxfaq) — захардкожен снимок (62 страны). Обновлять вручную при изменении.
- **FX:** [open.er-api.com](https://open.er-api.com) — бесплатно, без ключей, ECB-based, обновляется ежедневно.

## Запуск локально

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Откроется на `http://localhost:8501`.

## Деплой на Streamlit Cloud

1. Залить `streamlit_app.py`, `requirements.txt`, `README.md` в публичный GitHub-репо.
2. Зайти на [share.streamlit.io](https://share.streamlit.io), connect GitHub, выбрать репо.
3. Указать main file: `streamlit_app.py`.
4. Deploy. Готово.

## Известные ограничения / TODO для v2

- **Steam Store API rate limit** — 200 запросов / 5 минут с одного IP. Кешируем по `(appid, cc)` на 1 час, но при шквале AppID из разных IP можно словить 429. Можно добавить экспоненциальный backoff.
- **Mode B (Base USD)** — простая FX-конверсия. Не учитывает [Steam suggested pricing tiers](https://partner.steamgames.com/doc/store/pricing) с psychological rounding (`.99`, `.49`). Для точных рекомендаций нужно подгружать таблицу Valve (login-gated) или хардкодить снимок.
- **Country → currency map в Mode B** — захардкожен. Steam периодически меняет регионы (RU/AR/TR перевели на USD). Снимок на май 2026.
- **VAT-таблица** — снимок Steam tax FAQ, обновлять вручную. Можно добавить периодический скрейп страницы tax FAQ + diff.
- **Steam revenue share (30%)** — не вычитается. Расчёт оптимизирован под продажу CD-keys через дистрибьютора, где Valve не получает свою долю. Если нужен Mode "Steam Store sales", добавить отдельный flag и вычитать 30% после VAT.
- **Discount (текущая скидка на Steam)** — отображаем `final_formatted` цену, т.е. со скидкой если она активна. Добавить опциональный toggle "use original price (initial)".
- **Steam China (XC код)** — отдельная экосистема (Perfect World), сюда не включена. CN в выборке = глобальный Steam с CNY-pricing.

## Структура файлов

```
streamlit_app.py    # вся логика и UI в одном файле
requirements.txt    # зависимости
README.md           # этот файл
```
