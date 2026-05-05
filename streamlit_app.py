"""
Steam Publisher Revenue Calculator
==================================

Streamlit-приложение, которое для заданного Steam AppID:
  1. Тянет региональные цены через Steam Store API.
  2. Применяет inclusive-VAT по таблице Steam tax FAQ.
  3. Вычитает комиссию дистрибьютора.
  4. Конвертирует всё в USD по текущим FX-курсам.

Запуск локально:
    pip install -r requirements.txt
    streamlit run streamlit_app.py

Деплой на Streamlit Cloud:
    push в GitHub → connect repo на share.streamlit.io.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# Static data
# ----------------------------------------------------------------------------

# VAT-таблица. Источник: https://partner.steamgames.com/doc/finance/taxfaq
# (раздел "Current Tax Rates", только Inclusive — то есть налог уже сидит в цене).
# Снимок на май 2026.
VAT_TABLE: dict[str, tuple[float, str]] = {
    "AE": (0.050, "United Arab Emirates"),
    "AT": (0.200, "Austria"),
    "AU": (0.100, "Australia"),
    "BD": (0.150, "Bangladesh"),
    "BE": (0.210, "Belgium"),
    "BG": (0.200, "Bulgaria"),
    "BS": (0.100, "Bahamas"),
    "BY": (0.200, "Belarus"),
    "CH": (0.081, "Switzerland"),
    "CL": (0.190, "Chile"),
    "CO": (0.190, "Colombia"),
    "CY": (0.190, "Cyprus"),
    "CZ": (0.210, "Czech Republic"),
    "DE": (0.190, "Germany"),
    "DK": (0.250, "Denmark"),
    "EE": (0.240, "Estonia"),
    "EG": (0.140, "Egypt"),
    "ES": (0.210, "Spain"),
    "FI": (0.255, "Finland"),
    "FR": (0.200, "France"),
    "GB": (0.200, "United Kingdom"),
    "GR": (0.240, "Greece"),
    "HR": (0.250, "Croatia"),
    "HU": (0.270, "Hungary"),
    "ID": (0.110, "Indonesia"),
    "IE": (0.230, "Ireland"),
    "IM": (0.200, "Isle of Man"),
    "IN": (0.180, "India"),
    "IS": (0.240, "Iceland"),
    "IT": (0.220, "Italy"),
    "JP": (0.100, "Japan"),
    "KR": (0.100, "Korea, Republic of"),
    "KZ": (0.160, "Kazakhstan"),
    "LT": (0.210, "Lithuania"),
    "LU": (0.170, "Luxembourg"),
    "LV": (0.210, "Latvia"),
    "MA": (0.200, "Morocco"),
    "MC": (0.200, "Monaco"),
    "MD": (0.200, "Moldova"),
    "MT": (0.180, "Malta"),
    "MX": (0.160, "Mexico"),
    "MY": (0.080, "Malaysia"),
    "NL": (0.210, "Netherlands"),
    "NO": (0.250, "Norway"),
    "NZ": (0.150, "New Zealand"),
    "PE": (0.180, "Peru"),
    "PH": (0.120, "Philippines"),
    "PL": (0.230, "Poland"),
    "PT": (0.230, "Portugal"),
    "RO": (0.210, "Romania"),
    "RS": (0.200, "Serbia"),
    "RU": (0.220, "Russian Federation"),
    "SA": (0.150, "Saudi Arabia"),
    "SE": (0.250, "Sweden"),
    "SG": (0.090, "Singapore"),
    "SI": (0.220, "Slovenia"),
    "SK": (0.230, "Slovakia"),
    "TH": (0.070, "Thailand"),
    "TR": (0.200, "Turkey"),
    "TW": (0.050, "Taiwan"),
    "UA": (0.200, "Ukraine"),
    "UZ": (0.120, "Uzbekistan"),
    "ZA": (0.150, "South Africa"),
}

# Доп. страны с уникальными Steam-валютами, у которых VAT не collected'ится Steam'ом.
# Их добавляем чтобы цены в этих валютах тоже попали в таблицу. VAT = 0.
EXTRA_COUNTRIES: dict[str, str] = {
    "US": "United States",
    "CA": "Canada",
    "BR": "Brazil",
    "AR": "Argentina",
    "IL": "Israel",
    "HK": "Hong Kong",
    "VN": "Vietnam",
    "CR": "Costa Rica",
    "UY": "Uruguay",
    "KW": "Kuwait",
    "QA": "Qatar",
    "CN": "China",
}

STEAM_API = "https://store.steampowered.com/api/appdetails"
FX_API = "https://open.er-api.com/v6/latest/USD"


def all_countries() -> dict[str, tuple[float, str]]:
    """Возвращает {cc: (vat_rate, country_name)} для всех стран в выборке."""
    out: dict[str, tuple[float, str]] = {}
    for cc, (rate, name) in VAT_TABLE.items():
        out[cc] = (rate, name)
    for cc, name in EXTRA_COUNTRIES.items():
        if cc not in out:
            out[cc] = (0.0, name)
    return out


# ----------------------------------------------------------------------------
# Network calls (cached)
# ----------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_app_meta(appid: str) -> dict | None:
    """Получить базовую инфу про приложение (имя)."""
    try:
        r = requests.get(
            STEAM_API,
            params={"appids": appid, "filters": "basic", "l": "en"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json() or {}
        node = data.get(appid) or {}
        if node.get("success"):
            return node.get("data") or {}
    except Exception:
        pass
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_steam_price(appid: str, cc: str) -> dict | None:
    """Получить price_overview для (appid, cc). None если игра free / не продаётся в регионе."""
    try:
        r = requests.get(
            STEAM_API,
            params={
                "appids": appid,
                "cc": cc,
                "filters": "price_overview",
                "l": "en",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json() or {}
        node = data.get(appid) or {}
        if not node.get("success"):
            return None
        po = (node.get("data") or {}).get("price_overview")
        if not po:
            return None
        return {
            "currency": po.get("currency"),
            "final": po.get("final"),                # минорные единицы
            "initial": po.get("initial"),
            "discount_percent": po.get("discount_percent", 0),
            "final_formatted": po.get("final_formatted", ""),
        }
    except Exception:
        return None


@st.cache_data(ttl=900, show_spinner=False)
def fetch_fx_rates() -> tuple[dict[str, float], str]:
    """USD-based курсы. Возвращает (rates, last_update)."""
    try:
        r = requests.get(FX_API, timeout=15)
        r.raise_for_status()
        data = r.json() or {}
        rates = data.get("rates") or {}
        last = data.get("time_last_update_utc") or ""
        return rates, last
    except Exception:
        return {}, ""


# ----------------------------------------------------------------------------
# Math
# ----------------------------------------------------------------------------

def steam_minor_to_major(amount_minor: int | float | None) -> float | None:
    """Steam Store API всегда возвращает цену в /100 единицах (включая JPY, KRW)."""
    if amount_minor is None:
        return None
    try:
        return float(amount_minor) / 100.0
    except (TypeError, ValueError):
        return None


def compute_row(
    cc: str,
    country_name: str,
    vat_rate: float,
    price_info: dict | None,
    fx_rates: dict[str, float],
    distributor_fee_pct: float,
) -> dict:
    """Рассчитать строку таблицы для одной страны."""
    base = {
        "Регион": f"{cc} — {country_name}",
        "Валюта": "—",
        "Цена в локальной валюте": None,
        "Цена в USD по курсу": None,
        "VAT %": f"{vat_rate * 100:.1f}%" if vat_rate > 0 else "0%",
        "Цена в USD без VAT": None,
        "Доход издателя (локальная)": None,
        "Доход издателя (USD)": None,
        "Note": "",
    }

    if not price_info:
        base["Note"] = "no price (free / not for sale в этом регионе)"
        return base

    currency = price_info.get("currency") or "—"
    local_price = steam_minor_to_major(price_info.get("final"))
    if local_price is None or local_price <= 0:
        base["Note"] = "no price"
        base["Валюта"] = currency
        return base

    local_ex_vat = local_price / (1 + vat_rate) if vat_rate > 0 else local_price
    publisher_local = local_ex_vat * (1 - distributor_fee_pct / 100.0)

    rate = fx_rates.get(currency)
    if rate and rate > 0:
        usd_gross = local_price / rate
        usd_ex_vat = local_ex_vat / rate
        publisher_usd = publisher_local / rate
    else:
        usd_gross = usd_ex_vat = publisher_usd = None
        base["Note"] = f"no FX rate for {currency}"

    base.update({
        "Валюта": currency,
        "Цена в локальной валюте": round(local_price, 2),
        "Цена в USD по курсу": round(usd_gross, 2) if usd_gross is not None else None,
        "Цена в USD без VAT": round(usd_ex_vat, 2) if usd_ex_vat is not None else None,
        "Доход издателя (локальная)": round(publisher_local, 2),
        "Доход издателя (USD)": round(publisher_usd, 2) if publisher_usd is not None else None,
    })
    return base


# ----------------------------------------------------------------------------
# Table builder
# ----------------------------------------------------------------------------

def build_pricing_table(
    appid: str,
    distributor_fee_pct: float,
    progress_cb=None,
) -> tuple[pd.DataFrame, dict[str, float], str]:
    """Главный пайплайн. Возвращает (df, fx_rates, fx_last_update)."""
    countries = all_countries()
    fx_rates, fx_last = fetch_fx_rates()

    # Параллельные запросы к Steam Store API (он терпит ~4 параллельных).
    results: dict[str, dict | None] = {}
    total = len(countries)
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        future_to_cc = {ex.submit(fetch_steam_price, appid, cc): cc for cc in countries}
        for fut in as_completed(future_to_cc):
            cc = future_to_cc[fut]
            try:
                results[cc] = fut.result()
            except Exception:
                results[cc] = None
            done += 1
            if progress_cb:
                progress_cb(done / total)

    rows = []
    for cc, (vat_rate, name) in countries.items():
        rows.append(compute_row(
            cc=cc,
            country_name=name,
            vat_rate=vat_rate,
            price_info=results.get(cc),
            fx_rates=fx_rates,
            distributor_fee_pct=distributor_fee_pct,
        ))

    df = pd.DataFrame(rows)
    return df, fx_rates, fx_last


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="Steam Publisher Revenue Calculator",
        page_icon="💰",
        layout="wide",
    )

    st.title("Steam Publisher Revenue Calculator")
    st.caption(
        "Считает доход издателя по региональным ценам Steam с учётом VAT и "
        "комиссии дистрибьютора. Расчёт под продажу через CD-keys "
        "(без 30% Steam revenue share)."
    )

    with st.sidebar:
        st.header("Параметры")

        mode = st.radio(
            "Режим расчёта",
            options=["По AppID (фактические цены)", "По базовой USD-цене (FX-конверсия)"],
            help=(
                "AppID — берём текущие региональные цены конкретной игры из Steam Store API.\n\n"
                "Base USD — конвертируем введённую базовую цену в локальные валюты по FX "
                "(приблизительно; не учитывает Steam suggested pricing tiers)."
            ),
        )

        appid = ""
        base_usd = 0.0
        if mode.startswith("По AppID"):
            appid = st.text_input(
                "Steam AppID",
                value="730",
                help="Например, 730 = Counter-Strike 2",
            ).strip()
        else:
            base_usd = st.number_input(
                "Базовая цена в USD",
                min_value=0.0,
                max_value=999.99,
                value=29.99,
                step=1.0,
                format="%.2f",
            )

        distributor_fee = st.number_input(
            "Комиссия дистрибьютора, %",
            min_value=0.0,
            max_value=99.0,
            value=20.0,
            step=0.5,
            help="Процент, который удерживает дистрибьютор/реселлер с продажи ключа.",
        )

        st.markdown("---")
        run = st.button("Рассчитать", type="primary", use_container_width=True)

    if not run:
        st.info(
            "👈 Заполни параметры слева и нажми **Рассчитать**.\n\n"
            "Подсказка: для CS2 → AppID `730`, для Dota 2 → `570`."
        )
        st.stop()

    # ---- Validation ----
    if mode.startswith("По AppID"):
        if not appid.isdigit():
            st.error("AppID должен быть числом, например `730`.")
            st.stop()
    else:
        if base_usd <= 0:
            st.error("Базовая USD-цена должна быть больше нуля.")
            st.stop()

    # ---- Mode A: AppID-driven ----
    if mode.startswith("По AppID"):
        meta = fetch_app_meta(appid)
        app_name = (meta or {}).get("name") or f"AppID {appid}"

        progress_bar = st.progress(0.0, text="Получаем цены из Steam Store API…")
        df, fx_rates, fx_last = build_pricing_table(
            appid=appid,
            distributor_fee_pct=distributor_fee,
            progress_cb=lambda p: progress_bar.progress(p, text=f"Получаем цены… {int(p*100)}%"),
        )
        progress_bar.empty()

        st.subheader(f"{app_name}")
        st.markdown(
            f"**AppID:** `{appid}` · "
            f"**Комиссия дистрибьютора:** {distributor_fee}% · "
            f"**FX update:** {fx_last or 'unknown'}"
        )

        valid_df = df[df["Note"] == ""].drop(columns=["Note"]).reset_index(drop=True)
        skipped_df = df[df["Note"] != ""][["Регион", "Валюта", "Note"]].reset_index(drop=True)

        st.dataframe(
            valid_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Цена в локальной валюте": st.column_config.NumberColumn(format="%.2f"),
                "Цена в USD по курсу": st.column_config.NumberColumn(format="$%.2f"),
                "Цена в USD без VAT": st.column_config.NumberColumn(format="$%.2f"),
                "Доход издателя (локальная)": st.column_config.NumberColumn(format="%.2f"),
                "Доход издателя (USD)": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

        with st.expander(f"Регионы без данных ({len(skipped_df)})"):
            if skipped_df.empty:
                st.write("Нет — цены получены везде ✓")
            else:
                st.dataframe(skipped_df, use_container_width=True, hide_index=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        st.download_button(
            "💾 Скачать CSV",
            data=valid_df.to_csv(index=False).encode("utf-8"),
            file_name=f"steam_pricing_{appid}_{ts}.csv",
            mime="text/csv",
        )

    # ---- Mode B: Base USD via FX ----
    else:
        fx_rates, fx_last = fetch_fx_rates()
        if not fx_rates:
            st.error("Не удалось получить FX-курсы. Попробуй чуть позже.")
            st.stop()

        # Минимальный mapping country → currency для Mode B.
        # Берём актуальные на 2026 значения. RU/AR/TR/UA Steam перевёл на USD.
        country_currency_map = {
            "AE": "AED", "AT": "EUR", "AU": "AUD", "BE": "EUR",
            "BG": "EUR", "BR": "BRL", "CA": "CAD", "CH": "CHF",
            "CL": "CLP", "CN": "CNY", "CO": "COP", "CR": "CRC",
            "CY": "EUR", "CZ": "EUR", "DE": "EUR", "DK": "EUR",
            "EE": "EUR", "ES": "EUR", "FI": "EUR", "FR": "EUR",
            "GB": "GBP", "GR": "EUR", "HK": "HKD", "HR": "EUR",
            "HU": "EUR", "ID": "IDR", "IE": "EUR", "IL": "ILS",
            "IM": "GBP", "IN": "INR", "IS": "EUR", "IT": "EUR",
            "JP": "JPY", "KR": "KRW", "LT": "EUR", "LU": "EUR",
            "LV": "EUR", "MC": "EUR", "MT": "EUR", "MX": "MXN",
            "MY": "MYR", "NL": "EUR", "NO": "EUR", "NZ": "NZD",
            "PE": "PEN", "PH": "PHP", "PL": "PLN", "PT": "EUR",
            "RO": "EUR", "RS": "EUR", "SA": "SAR", "SE": "EUR",
            "SG": "SGD", "SI": "EUR", "SK": "EUR", "TH": "THB",
            "TW": "TWD", "US": "USD", "UY": "UYU", "VN": "VND",
            "ZA": "ZAR",
            # Steam перевёл эти регионы на USD-биллинг:
            "RU": "USD", "UA": "USD", "TR": "USD", "AR": "USD",
            "BY": "USD", "KZ": "USD", "MD": "USD",
            "BD": "USD", "BS": "USD", "EG": "USD", "MA": "USD",
            "UZ": "USD", "KW": "USD", "QA": "USD",
        }

        rows = []
        countries = all_countries()
        for cc, (vat_rate, name) in countries.items():
            currency = country_currency_map.get(cc, "USD")
            rate = fx_rates.get(currency)
            if not rate or rate <= 0:
                rows.append({
                    "Регион": f"{cc} — {name}",
                    "Валюта": currency,
                    "Note": f"no FX rate for {currency}",
                    "Цена в локальной валюте": None,
                    "Цена в USD по курсу": None,
                    "VAT %": f"{vat_rate * 100:.1f}%",
                    "Цена в USD без VAT": None,
                    "Доход издателя (локальная)": None,
                    "Доход издателя (USD)": None,
                })
                continue

            local_price = base_usd * rate  # USD → local
            local_ex_vat = local_price / (1 + vat_rate) if vat_rate > 0 else local_price
            publisher_local = local_ex_vat * (1 - distributor_fee / 100.0)

            usd_gross = local_price / rate            # = base_usd
            usd_ex_vat = local_ex_vat / rate
            publisher_usd = publisher_local / rate

            rows.append({
                "Регион": f"{cc} — {name}",
                "Валюта": currency,
                "Цена в локальной валюте": round(local_price, 2),
                "Цена в USD по курсу": round(usd_gross, 2),
                "VAT %": f"{vat_rate * 100:.1f}%",
                "Цена в USD без VAT": round(usd_ex_vat, 2),
                "Доход издателя (локальная)": round(publisher_local, 2),
                "Доход издателя (USD)": round(publisher_usd, 2),
                "Note": "",
            })

        df = pd.DataFrame(rows)

        st.subheader(f"Базовая цена: ${base_usd:.2f}")
        st.markdown(
            f"**Комиссия дистрибьютора:** {distributor_fee}% · "
            f"**FX update:** {fx_last or 'unknown'}"
        )
        st.warning(
            "⚠️ Mode B делает прямую FX-конверсию USD → локальная валюта. "
            "Это не совпадает с **Steam suggested pricing tiers** Valve, "
            "которые учитывают психологическое округление и paritetic pricing. "
            "Используй Mode A для точных текущих цен."
        )

        valid_df = df[df["Note"] == ""].drop(columns=["Note"]).reset_index(drop=True)
        st.dataframe(
            valid_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Цена в локальной валюте": st.column_config.NumberColumn(format="%.2f"),
                "Цена в USD по курсу": st.column_config.NumberColumn(format="$%.2f"),
                "Цена в USD без VAT": st.column_config.NumberColumn(format="$%.2f"),
                "Доход издателя (локальная)": st.column_config.NumberColumn(format="%.2f"),
                "Доход издателя (USD)": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        st.download_button(
            "💾 Скачать CSV",
            data=valid_df.to_csv(index=False).encode("utf-8"),
            file_name=f"steam_basecase_{base_usd:.2f}_{ts}.csv",
            mime="text/csv",
        )

    # ---- Footer ----
    st.markdown("---")
    st.caption(
        "**Источники:** "
        "[Steam Store API](https://store.steampowered.com/api/appdetails) · "
        "[Steam tax FAQ](https://partner.steamgames.com/doc/finance/taxfaq) · "
        "[open.er-api.com](https://open.er-api.com) (FX). "
        "VAT применяется только к странам, где Steam собирает inclusive-налог. "
        "Steam revenue share (30%) НЕ учитывается — расчёт под продажу через дистрибьютора (CD-keys)."
    )


if __name__ == "__main__":
    main()
