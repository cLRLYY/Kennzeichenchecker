import json
import logging
import os
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


def build_summary_message(results: list[dict[str, Any]]) -> str:
    lines = ["📋 Wunschkennzeichen-Prüfung abgeschlossen", ""]

    for item in results:
        pretty_plate = item["pretty_plate"]
        status = item["status"]
        matches = item.get("matches", [])

        if status == "available":
            icon = "✅"
            text = "verfügbar"
        elif status == "unavailable":
            icon = "❌"
            text = "nicht verfügbar"
        else:
            icon = "❓"
            text = "unbekannt / Fehler"

        if status == "available" and matches:
            lines.append(f"{icon} {pretty_plate} — {text}: {', '.join(matches)}")
        else:
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


def normalize_found_plate(text: str) -> str | None:
    cleaned = " ".join(text.replace("\xa0", " ").replace("-", " - ").split())
    match = re.search(r"PE\s*-\s*([A-Z]{1,2})\s*([0-9]{1,4})", cleaned)
    if not match:
        return None

    letters, numbers = match.groups()
    return f"PE {letters} {numbers}"


def extract_available_options(page: Page) -> list[str]:
    candidates: list[str] = []

    select_locator = page.locator("#wkzresultlist_wkz")

    try:
        if select_locator.count() > 0:
            options = select_locator.evaluate(
                """el => Array.from(el.options).map(opt => ({
                    text: (opt.textContent || '').trim(),
                    value: (opt.value || '').trim()
                }))"""
            )

            for opt in options:
                for raw in (opt.get("text", ""), opt.get("value", "")):
                    plate = normalize_found_plate(raw)
                    if plate:
                        candidates.append(plate)
    except Exception as exc:
        logging.warning("Optionen aus #wkzresultlist_wkz konnten nicht direkt gelesen werden: %s", exc)

    if not candidates:
        fallback_selectors = [
            "#wkzresultlist_wkz-button .ui-selectmenu-button-text",
            ".ui-menu-item",
            ".ui-menu-item-wrapper",
            "[role='option']",
            "select option",
            "li",
        ]

        for selector in fallback_selectors:
            locator = page.locator(selector)

            try:
                count = locator.count()
            except Exception:
                count = 0

            for i in range(count):
                try:
                    text = locator.nth(i).inner_text().strip()
                except Exception:
                    continue

                plate = normalize_found_plate(text)
                if plate:
                    candidates.append(plate)

    if not candidates:
        try:
            body_text = extract_page_text(page)
            for raw_match in re.findall(r"PE\s*-\s*[A-Z]{1,2}\s*[0-9]{1,4}", body_text):
                plate = normalize_found_plate(raw_match)
                if plate:
                    candidates.append(plate)
        except Exception:
            pass

    unique_candidates: list[str] = []
    seen: set[str] = set()

    for item in candidates:
        if item not in seen:
            seen.add(item)
            unique_candidates.append(item)

    return unique_candidates


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


def check_plate_once(context: BrowserContext, raw_plate: str) -> dict[str, Any]:
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
        matches: list[str] = []

        if status == "available" and "?" in raw_plate:
            try:
                matches = extract_available_options(page)
                if matches:
                    logging.info(
                        "Gefundene Platzhalter-Treffer für %s: %s",
                        plate["pretty"],
                        ", ".join(matches),
                    )
                else:
                    logging.info(
                        "Für %s wurde zwar 'verfügbar' erkannt, aber keine Trefferliste ausgelesen.",
                        plate["pretty"],
                    )
            except Exception as exc:
                logging.warning(
                    "Trefferliste für %s konnte nicht ausgelesen werden: %s",
                    plate["pretty"],
                    exc,
                )

        if status == "unknown":
            html_snapshot = page.content()[:5000]
            logging.warning(
                "Status für %s konnte nicht eindeutig erkannt werden. HTML-Auszug im Log.",
                plate["pretty"],
            )
            logging.debug("HTML-Auszug für %s:\n%s", plate["pretty"], html_snapshot)

        logging.info("Erkanntes Ergebnis für %s: %s", plate["pretty"], status)

        return {
            "status": status,
            "matches": matches,
        }

    finally:
        page.close()


def check_plate_with_retry(context: BrowserContext, raw_plate: str, retries: int = 3) -> dict[str, Any]:
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
    cycle_results: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = build_browser(playwright)
        context = build_context(browser)

        try:
            for raw_plate in config["license_plates"]:
                plate = normalize_plate(raw_plate)
                compact = plate["compact"]
                pretty = plate["pretty"]

                try:
                    result = check_plate_with_retry(context, raw_plate, retries=3)
                    status = result["status"]
                    matches = result.get("matches", [])

                    updated_state[compact] = status
                    save_state(STATE_FILE, updated_state)

                    cycle_results.append(
                        {
                            "compact_plate": compact,
                            "pretty_plate": pretty,
                            "status": status,
                            "matches": matches,
                        }
                    )

                except Exception as exc:
                    logging.error("Prüfung für %s fehlgeschlagen: %s", pretty, exc)

                    cycle_results.append(
                        {
                            "compact_plate": compact,
                            "pretty_plate": pretty,
                            "status": "unknown",
                            "matches": [],
                        }
                    )

                random_delay(2.0, 5.0)

        finally:
            context.close()
            browser.close()

    summary_message = build_summary_message(cycle_results)
    send_telegram_message(
        bot_token=config["telegram"]["bot_token"],
        chat_id=config["telegram"]["chat_id"],
        message=summary_message,
    )
    logging.info("Zusammenfassende Telegram-Benachrichtigung gesendet.")

    return updated_state


def run_once() -> int:
    try:
        config = load_config(CONFIG_FILE)
        state = load_state(STATE_FILE)
    except Exception as exc:
        logging.error("Start fehlgeschlagen: %s", exc)
        return 1

    try:
        run_cycle(config, state)
        return 0
    except Exception as exc:
        logging.exception("Unerwarteter Fehler im Prüfzyklus: %s", exc)

        if "Executable doesn't exist" in str(exc):
            logging.error(
                "Playwright-Browser fehlen. Bitte ausführen: python -m playwright install chromium"
            )

        return 1


def main() -> None:
    setup_logging()

    github_actions = os.getenv("GITHUB_ACTIONS", "").lower() == "true"

    if github_actions:
        logging.info("Kennzeichenwächter gestartet im GitHub-Actions-Modus.")
        sys.exit(run_once())

    try:
        config = load_config(CONFIG_FILE)
    except Exception as exc:
        logging.error("Start fehlgeschlagen: %s", exc)
        sys.exit(1)

    interval_minutes = int(config["check_interval_minutes"])
    logging.info("Kennzeichenwächter gestartet. Intervall: %s Minuten", interval_minutes)

    while True:
        exit_code = run_once()
        if exit_code != 0:
            logging.error("Prüfzyklus mit Fehler beendet.")

        sleep_seconds = interval_minutes * 60
        logging.info("Warte %s Sekunden bis zum nächsten Zyklus.", sleep_seconds)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
