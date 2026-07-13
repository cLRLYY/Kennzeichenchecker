import json
import os
import logging
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

BASE_DIR = Path(__file__).resolve().parent

BASE_URL = "https://wkz.landkreis-peine.de/wkz/?renderer=responsive"
APL_URL = "https://www.apl.de/neuwagen/bmw/m2-coup-/basis/angebot/1748-133"
APL_LIST_PRICE = 85960.0

CONFIG_FILE = BASE_DIR / "config.yaml"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "logs.txt"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

    logging.info("Logging initialisiert. Logdatei: %s", LOG_FILE)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Konfigurationsdatei nicht gefunden: {path}. "
            f"Bitte config.yaml anlegen."
        )

    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError("config.yaml ist ungültig.")

    for key in ("check_interval_minutes", "license_plates", "telegram"):
        if key not in config:
            raise ValueError(f"Pflichtfeld fehlt in config.yaml: {key}")

    telegram = config.get("telegram", {})

    bot_token = str(
        telegram.get("bot_token")
        or os.getenv("TELEGRAM_BOT_TOKEN", "")
    ).strip()

    chat_id = str(
        telegram.get("chat_id")
        or os.getenv("TELEGRAM_CHAT_ID", "")
    ).strip()

    telegram["bot_token"] = bot_token
    telegram["chat_id"] = chat_id
    config["telegram"] = telegram

    if not telegram["bot_token"] or not telegram["chat_id"]:
        raise ValueError(
            "Telegram-Daten fehlen. Bitte telegram.bot_token / telegram.chat_id "
            "in config.yaml oder die Umgebungsvariablen "
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID setzen."
        )

    plates = config.get("license_plates", [])
    if not plates or not isinstance(plates, list):
        raise ValueError("license_plates muss eine nicht-leere Liste sein.")

    return config


def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logging.warning("state.json konnte nicht gelesen werden, starte leer: %s", exc)
        return {}


def save_state(path: Path, state: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def random_delay(min_seconds: float = 0.8, max_seconds: float = 2.2) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def normalize_plate(raw_plate: str) -> dict[str, str]:
    cleaned = re.sub(r"[^A-Za-z0-9?]", "", raw_plate).upper()

    if not cleaned.startswith("PE"):
        raise ValueError(
            f"Kennzeichen '{raw_plate}' ist ungültig. "
            f"Für Landkreis Peine muss es mit 'PE' beginnen."
        )

    rest = cleaned[2:]

    match = re.fullmatch(r"([A-Z?]{1,2})([0-9?]{1,4})", rest)
    if not match:
        raise ValueError(
            f"Kennzeichen '{raw_plate}' hat kein unterstütztes Format. "
            f"Erlaubt sind Buchstaben/Ziffern sowie '?' als Platzhalter, z. B. PE?M2 oder PEA?23."
        )

    letters, numbers = match.groups()
    return {
        "raw": raw_plate,
        "compact": cleaned,
        "district": "PE",
        "letters": letters,
        "numbers": numbers,
        "pretty": f"PE {letters} {numbers}",
    }


def send_telegram_message(bot_token: str, chat_id: str, message: str, timeout: int = 20) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": str(chat_id).strip(),
            "text": message,
        },
        timeout=timeout,
    )

    if not response.ok:
        logging.error("Telegram API Antwort: %s", response.text)

    response.raise_for_status()


def parse_euro_amount(value: str) -> float:
    cleaned = value.replace("€", "").replace(".", "").replace(",", ".")
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    if not cleaned:
        raise ValueError(f"Kein Euro-Betrag erkennbar: {value!r}")
    return float(cleaned)


def format_euro(value: float) -> str:
    formatted = f"{value:,.2f}"
    formatted = formatted.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} €"


def build_summary_message(
    results: list[dict[str, str]],
    apl_data: dict[str, float] | None = None,
) -> str:
    lines = ["📋 Wunschkennzeichen-Prüfung abgeschlossen", ""]

    if apl_data:
        lines.extend(
            [
                "🚗 APL-Angebot BMW M2 Coupé",
                f"Listenpreis: {format_euro(apl_data['list_price'])}",
                f"Ihr Endpreis: {format_euro(apl_data['end_price'])}",
                f"Ersparnis: {apl_data['savings_percent']:.2f} %",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "🚗 APL-Angebot BMW M2 Coupé",
                "Listenpreis: nicht verfügbar",
                "Ihr Endpreis: nicht verfügbar",
                "Ersparnis: nicht verfügbar",
                "",
            ]
        )

    for item in results:
        pretty_plate = item["pretty_plate"]
        status = item["status"]

        if status == "available":
            icon = "✅"
            text = "verfügbar"
        elif status == "unavailable":
            icon = "❌"
            text = "nicht verfügbar"
        else:
            icon = "❓"
            text = "unbekannt / Fehler"

        lines.append(f"{icon} {pretty_plate} — {text}")

    lines.extend(
        [
            "",
            "Legende:",
            "✅ verfügbar",
            "❌ nicht verfügbar",
            "❓ unbekannt / Fehler",
        ]
    )

    return "\n".join(lines)


def click_first_available(page: Page, selectors: list[str], timeout_ms: int = 5000) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            locator.first.wait_for(state="attached", timeout=timeout_ms)
            locator.first.click(force=True, timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


def extract_page_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return page.content()


def infer_status_from_text(text: str) -> str:
    normalized = " ".join(text.lower().split())

    unavailable_patterns = [
        "es konnten keine freien kennzeichen gefunden werden",
        "keine freien kennzeichen gefunden",
        "kein freies kennzeichen gefunden",
        "nicht verfügbar",
        "ist nicht verfügbar",
        "leider nicht verfügbar",
        "bereits vergeben",
        "schon vergeben",
        "nicht frei",
        "ungültig",
        "unzulässig",
    ]

    available_patterns = [
        "1 gefundenes freies kennzeichen",
        "gefundenes freies kennzeichen",
        "gefundene freie kennzeichen",
        "bitte wählen sie ihr wunschkennzeichen",
    ]

    for pattern in unavailable_patterns:
        if pattern in normalized:
            return "unavailable"

    for pattern in available_patterns:
        if pattern in normalized:
            return "available"

    return "unknown"


def open_fresh_page(context: BrowserContext) -> Page:
    page = context.new_page()
    page.set_default_timeout(20000)
    return page


def prepare_start_page(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")

    selector = "button#action_infopage_next, button[name='ACTION_INFOPAGE_NEXT']"
    page.wait_for_selector(selector, state="attached", timeout=15000)

    random_delay(1.0, 2.0)
    page.locator(selector).first.click(force=True)
    page.wait_for_load_state("domcontentloaded")
    random_delay(1.0, 2.0)


def continue_plate_type_page(page: Page) -> None:
    weiter_selectors = [
        "button:has-text('Weiter')",
        "input[type='submit'][value='Weiter']",
        "button[name*='NEXT']",
    ]

    clicked = click_first_available(page, weiter_selectors, timeout_ms=10000)
    if not clicked:
        raise RuntimeError("Auf der Kennzeichenart-Seite wurde der 'Weiter'-Button nicht gefunden.")

    page.wait_for_load_state("domcontentloaded")
    random_delay(1.0, 2.0)


def try_fill_plate_form(page: Page, plate: dict[str, str]) -> None:
    text_inputs = page.locator("input[type='text']")
    count = text_inputs.count()

    if count < 2:
        raise RuntimeError(
            f"Es wurden nur {count} Textfelder gefunden, erwartet werden mindestens 2."
        )

    letters_input = text_inputs.nth(0)
    numbers_input = text_inputs.nth(1)

    letters_input.fill(plate["letters"])
    random_delay(0.3, 0.8)

    numbers_input.fill(plate["numbers"])
    random_delay(0.3, 0.8)


def submit_plate_check(page: Page) -> None:
    clicked = click_first_available(
        page,
        [
            "button:has-text('Suchen')",
            "input[type='submit'][value='Suchen']",
            "button[name*='SUCH']",
            "button[name*='SEARCH']",
            "button[name*='NEXT']",
        ],
        timeout_ms=10000,
    )

    if not clicked:
        raise RuntimeError("Such-Button wurde nicht gefunden.")

    page.wait_for_load_state("domcontentloaded")
    random_delay(1.0, 2.4)


def fetch_apl_offer_data(context: BrowserContext) -> dict[str, float]:
    page = open_fresh_page(context)

    try:
        logging.info("Prüfe APL-Angebot: %s", APL_URL)

        page.goto(APL_URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        random_delay(1.0, 2.0)

        consent_selectors = [
            "button:has-text('Akzeptieren')",
            "button:has-text('Zustimmen')",
            "button:has-text('Alle akzeptieren')",
            "button:has-text('Einverstanden')",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        ]
        click_first_available(page, consent_selectors, timeout_ms=3000)

        price_input = page.locator("#txtEingabePreis")
        price_input.wait_for(state="visible", timeout=15000)

        price_input.click()
        random_delay(0.2, 0.4)
        price_input.press("Control+A")
        price_input.press("Delete")
        random_delay(0.2, 0.4)
        price_input.type("85960", delay=100)

        page.evaluate(
            """
            () => {
                document.querySelector('#kdEingabePreis').value = '85960';
            }
            """
        )

        price_input.dispatch_event("input")
        price_input.dispatch_event("change")
        price_input.press("Tab")
        random_delay(0.8, 1.5)

        tarif_caption = page.locator(".kTarif .caption.entry span")
        try:
            caption_text = tarif_caption.inner_text(timeout=3000).strip().lower()
        except Exception:
            caption_text = ""

        if "privatkunden" not in caption_text:
            page.locator(".kTarif").click()
            random_delay(0.5, 1.0)

            privat_option = page.locator("text=für Privatkunden").last
            privat_option.wait_for(state="visible", timeout=10000)
            privat_option.click()
            random_delay(1.0, 2.0)

        page.wait_for_function(
            """() => {
                const hidden = document.querySelector('#kdEingabePreis');
                return hidden && hidden.value === '85960';
            }""",
            timeout=10000,
        )

        page.wait_for_function(
            """() => {
                const lp = document.querySelector('#kdListenpreis');
                const ep = document.querySelector('#kdEndpreis');
                if (!lp || !ep) return false;
                const lpText = (lp.textContent || '').trim();
                const epText = (ep.textContent || '').trim();
                return /€/.test(lpText) && /€/.test(epText) && !lpText.includes('wait.gif') && !epText.includes('wait.gif');
            }""",
            timeout=15000,
        )

        listenpreis_text = page.locator("#kdListenpreis").inner_text().strip()
        rabatt_text = page.locator("#kdRabatt").inner_text().strip()
        kaufpreis_text = page.locator("#kdKaufpreis").inner_text().strip()
        ufb_text = page.locator("#kdUFB").inner_text().strip()
        endpreis_text = page.locator("#kdEndpreis").inner_text().strip()

        logging.info(
            "APL DOM-Werte | Listenpreis=%s | Rabatt=%s | APL-Preis=%s | UFB=%s | Endpreis=%s",
            listenpreis_text,
            rabatt_text,
            kaufpreis_text,
            ufb_text,
            endpreis_text,
        )

        listen_price = parse_euro_amount(listenpreis_text)
        end_price = parse_euro_amount(endpreis_text)
        savings_percent = ((listen_price - end_price) / listen_price) * 100

        logging.info(
            "APL erkannt: Ihr Endpreis=%s | Ersparnis=%.2f%%",
            format_euro(end_price),
            savings_percent,
        )

        return {
            "list_price": listen_price,
            "end_price": end_price,
            "savings_percent": savings_percent,
        }

    finally:
        page.close()


def check_plate_once(context: BrowserContext, raw_plate: str) -> str:
    plate = normalize_plate(raw_plate)
    page = open_fresh_page(context)

    try:
        logging.info("Prüfe Kennzeichen: %s", plate["pretty"])

        prepare_start_page(page)
        continue_plate_type_page(page)
        try_fill_plate_form(page, plate)
        submit_plate_check(page)

        text = extract_page_text(page)
        status = infer_status_from_text(text)

        if status == "unknown":
            html_snapshot = page.content()[:5000]
            logging.warning(
                "Status für %s konnte nicht eindeutig erkannt werden. HTML-Auszug im Log.",
                plate["pretty"],
            )
            logging.debug("HTML-Auszug für %s:\n%s", plate["pretty"], html_snapshot)

        logging.info("Erkanntes Ergebnis für %s: %s", plate["pretty"], status)
        return status

    finally:
        page.close()


def check_plate_with_retry(context: BrowserContext, raw_plate: str, retries: int = 3) -> str:
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            return check_plate_once(context, raw_plate)
        except (requests.RequestException, PlaywrightTimeoutError, RuntimeError) as exc:
            last_error = exc
            logging.warning(
                "Fehler bei Prüfung von %s, Versuch %s/%s: %s",
                raw_plate,
                attempt,
                retries,
                exc,
            )
            if attempt < retries:
                sleep_seconds = random.uniform(5, 12)
                logging.info("Warte %.1f Sekunden vor erneutem Versuch.", sleep_seconds)
                time.sleep(sleep_seconds)

    raise RuntimeError(f"Prüfung für {raw_plate} nach {retries} Versuchen fehlgeschlagen: {last_error}")


def build_browser(playwright: Playwright) -> Browser:
    return playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )


def build_context(browser: Browser) -> BrowserContext:
    return browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="de-DE",
        timezone_id="Europe/Berlin",
    )


def run_cycle(config: dict[str, Any], state: dict[str, str]) -> dict[str, str]:
    updated_state = dict(state)
    cycle_results: list[dict[str, str]] = []
    apl_data: dict[str, float] | None = None

    with sync_playwright() as playwright:
        browser = build_browser(playwright)
        context = build_context(browser)

        try:
            try:
                apl_data = fetch_apl_offer_data(context)
            except Exception as exc:
                logging.error("APL-Angebot konnte nicht gelesen werden: %s", exc)

            random_delay(2.0, 4.0)

            for raw_plate in config["license_plates"]:
                plate = normalize_plate(raw_plate)
                compact = plate["compact"]
                pretty = plate["pretty"]

                try:
                    status = check_plate_with_retry(context, raw_plate, retries=3)
                    updated_state[compact] = status
                    save_state(STATE_FILE, updated_state)

                    cycle_results.append(
                        {
                            "compact_plate": compact,
                            "pretty_plate": pretty,
                            "status": status,
                        }
                    )

                except Exception as exc:
                    logging.error("Prüfung für %s fehlgeschlagen: %s", pretty, exc)

                    cycle_results.append(
                        {
                            "compact_plate": compact,
                            "pretty_plate": pretty,
                            "status": "unknown",
                        }
                    )

                random_delay(2.0, 5.0)

        finally:
            context.close()
            browser.close()

    summary_message = build_summary_message(cycle_results, apl_data=apl_data)
    send_telegram_message(
        bot_token=config["telegram"]["bot_token"],
        chat_id=config["telegram"]["chat_id"],
        message=summary_message,
    )
    logging.info("Zusammenfassende Telegram-Benachrichtigung gesendet.")

    return updated_state


def main() -> None:
    setup_logging()

    try:
        config = load_config(CONFIG_FILE)
        state = load_state(STATE_FILE)
    except Exception as exc:
        logging.error("Start fehlgeschlagen: %s", exc)
        sys.exit(1)

    logging.info("Kennzeichenwächter gestartet. Einzelner Prüfzyklus für GitHub Actions.")

    try:
        run_cycle(config, state)
    except Exception as exc:
        logging.exception("Unerwarteter Fehler im Prüfzyklus: %s", exc)

        if "Executable doesn't exist" in str(exc):
            logging.error(
                "Playwright-Browser fehlen. Bitte ausführen: python -m playwright install chromium"
            )

        sys.exit(1)


if __name__ == "__main__":
    main()
