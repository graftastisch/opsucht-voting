#!/usr/bin/env python3
"""Täglicher Vote für den Minecraft-Server auf mc-liste.de (läuft im GitHub-Actions-Workflow).

Ablauf:
  1. (nur bei geplantem Lauf) bis zu einem Zufallszeitpunkt zwischen 00:05 und 00:25 Uhr
     (Europe/Zurich) warten
  2. Einloggen -> Dashboard
  3. Vote-Seite öffnen, Username prüfen/eintragen, "Vote absenden" klicken
  4. Bei Fehler: KEIN erneuter Versuch, stattdessen Discord-Benachrichtigungen + Exit-Code 1

Benötigte Umgebungsvariablen (im Workflow aus Secrets/Variables gesetzt):
  LOGIN_USERNAME, LOGIN_PASSWORD, DISCORD_WEBHOOK_LOGS, DISCORD_WEBHOOK_FAIL, DISCORD_PING_ID
"""

from __future__ import annotations

import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
LOGIN_URL = "https://www.mc-liste.de/login"
VOTE_URL = "https://www.mc-liste.de/server/66/vote#serverVotePanel"
VOTE_PATH = "/server/66/vote"
VOTE_USERNAME = "GrafTastisch_"

TZ = ZoneInfo("Europe/Zurich")
WINDOW_START_MINUTE = 5      # frühester Vote: 00:05
WINDOW_LENGTH_MINUTES = 20   # spätester Vote: 00:25
MAX_WAIT_SECONDS = 50 * 60   # Sicherheitsnetz: länger warten wir nie

ACTION_TIMEOUT_MS = 30_000
DEBUG_DIR = Path("debug")

ALERT_SELECTOR = '.alert, .invalid-feedback, .text-danger, [role="alert"]'


class VoteError(Exception):
    """Erwarteter Fehler beim Login oder Voting (Meldung wird 1:1 nach Discord gesendet)."""


def log(message: str) -> None:
    print(f"[{datetime.now(TZ):%H:%M:%S}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# Zeitfenster (00:05 - 00:25 Uhr Europe/Zurich, Sommer-/Winterzeit-sicher)
# --------------------------------------------------------------------------- #
def plan_wait(now_utc: datetime, cron_schedule: str) -> tuple[float, datetime] | None:
    """Berechnet, wie lange gewartet werden muss.

    Der Workflow hat zwei Cron-Einträge (21:40 und 22:40 UTC), weil GitHub nur UTC kennt.
    Pro Jahreszeit passt genau einer davon zu 23:40 Uhr Ortszeit; der andere liefert None
    und wird übersprungen. Danach wird bis zu einem Zufallszeitpunkt zwischen 00:05 und
    00:25 Uhr gewartet. Kam der Lauf verspätet an, wird sofort gevotet.
    """
    now = now_utc.astimezone(TZ)
    offset_hours = int(now.utcoffset().total_seconds() // 3600)  # Sommer: 2, Winter: 1
    expected_utc_hour = (23 - offset_hours) % 24                 # 23:40 Ortszeit in UTC

    try:
        fired_utc_hour = int(cron_schedule.split()[1])
    except (IndexError, ValueError):
        fired_utc_hour = expected_utc_hour  # unbekannter Auslöser -> nicht blockieren
    if fired_utc_hour != expected_utc_hour:
        return None

    base_day = now if now.hour < 12 else now + timedelta(days=1)
    midnight = base_day.replace(hour=0, minute=0, second=0, microsecond=0)
    target = midnight + timedelta(
        minutes=WINDOW_START_MINUTE,
        seconds=random.randint(0, WINDOW_LENGTH_MINUTES * 60),
    )
    wait = (target.astimezone(timezone.utc) - now_utc).total_seconds()
    if wait > MAX_WAIT_SECONDS:
        return None
    return max(wait, 0.0), target


# --------------------------------------------------------------------------- #
# Browser-Schritte
# --------------------------------------------------------------------------- #
def alert_text(page: Page) -> str:
    texts = [t.strip() for t in page.locator(ALERT_SELECTOR).all_inner_texts() if t.strip()]
    return " | ".join(texts)


def dismiss_cookie_banner(page: Page) -> None:
    """Best effort: schließt einen evtl. vorhandenen Cookie-Banner, damit er keine Klicks blockiert."""
    try:
        page.get_by_role(
            "button", name=re.compile(r"alle akzeptieren|akzeptieren|zustimmen", re.I)
        ).first.click(timeout=2_000)
        log("Cookie-Banner geschlossen.")
    except PlaywrightTimeoutError:
        pass


def login(page: Page, username: str, password: str) -> None:
    log("Öffne Login-Seite ...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)

    page.locator('input[name="login"]').fill(username)
    page.locator('input[name="password"]').fill(password)
    page.locator("button.btn-auth-submit").click()

    try:
        page.wait_for_url(re.compile(r"[?&]page=dashboard"))
    except PlaywrightTimeoutError:
        hint = alert_text(page)
        raise VoteError(
            "Login fehlgeschlagen: keine Weiterleitung zum Dashboard."
            + (f" Meldung der Seite: {hint}" if hint else "")
        ) from None
    log("Login erfolgreich (Dashboard erreicht).")


def submit_vote(page: Page) -> str:
    log("Öffne Vote-Seite ...")
    page.goto(VOTE_URL, wait_until="domcontentloaded")

    form = page.locator("form").filter(has=page.get_by_role("button", name="Vote absenden")).first
    name_input = form.locator('input[name="username"]')
    try:
        name_input.wait_for(state="visible")
    except PlaywrightTimeoutError:
        hint = alert_text(page)
        raise VoteError(
            "Vote-Formular nicht gefunden."
            + (f" Meldung der Seite: {hint}" if hint else "")
        ) from None

    current = name_input.input_value().strip()
    if VOTE_USERNAME in current:
        log("Username ist bereits eingetragen.")
    else:
        log(f"Username-Feld enthält {current!r} -> trage {VOTE_USERNAME!r} ein.")
        name_input.fill(VOTE_USERNAME)

    log("Sende Vote ab ...")
    try:
        with page.expect_navigation(wait_until="domcontentloaded") as navigation:
            form.get_by_role("button", name="Vote absenden").click()
    except PlaywrightTimeoutError:
        hint = alert_text(page)
        raise VoteError(
            "Vote nicht bestätigt: nach dem Absenden gab es keine Weiterleitung."
            + (f" Meldung der Seite: {hint}" if hint else "")
        ) from None

    response = navigation.value
    if response is not None and not response.ok:
        raise VoteError(f"Vote fehlgeschlagen: Server antwortete mit HTTP {response.status}.")

    if VOTE_PATH in urlparse(page.url).path:
        # Wir sind noch auf der Vote-Seite -> die Seite zeigt eine Fehlermeldung.
        hint = alert_text(page)
        raise VoteError(f"Vote abgelehnt. Meldung der Seite: {hint or 'keine Meldung gefunden'}")

    message = alert_text(page) or page.title()
    log(f"Weitergeleitet auf {urlparse(page.url).path} - Vote erfolgreich.")
    return message


def save_debug_screenshot(page: Page) -> None:
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / "failure.png"), full_page=True)
        log("Screenshot gespeichert: debug/failure.png")
    except Exception:  # noqa: BLE001 - Screenshot ist nur Zusatz
        pass


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise VoteError(f"Umgebungsvariable {name} ist nicht gesetzt.")
    return value


def run_vote() -> str:
    username = require_env("LOGIN_USERNAME")
    password = require_env("LOGIN_PASSWORD")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Zurich",
            viewport={"width": 1280, "height": 900},
        )
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        page = context.new_page()  # ein einziger Tab für den ganzen Ablauf
        try:
            login(page, username, password)
            return submit_vote(page)
        except Exception:
            save_debug_screenshot(page)
            raise
        finally:
            context.close()
            browser.close()


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def with_query(url: str, **params: str) -> str:
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query.update(params)
    return urlunparse(parts._replace(query=urlencode(query)))


def post_webhook(url: str, payload: dict, *, name: str, components: bool = False) -> None:
    if not url:
        log(f"Discord-Webhook {name} ist nicht gesetzt - übersprungen.")
        return
    if components:
        # Ohne diesen Parameter verwirft Discord Buttons bei normalen Webhooks stillschweigend.
        url = with_query(url, with_components="true")
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        log(f"Discord-Nachricht über {name} gesendet.")
    except requests.RequestException as exc:
        status = getattr(exc.response, "status_code", None)
        body = getattr(exc.response, "text", "")[:200]
        log(f"Discord-Webhook {name} fehlgeschlagen ({type(exc).__name__}, HTTP {status}) {body}")


def notify_failure(reason: str) -> None:
    now = datetime.now(TZ).strftime("%d.%m.%Y %H:%M:%S")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_line = f"\nWorkflow-Lauf: <{server}/{repo}/actions/runs/{run_id}>" if repo and run_id else ""

    # 1) Log-Nachricht (ohne Ping)
    post_webhook(
        os.environ.get("DISCORD_WEBHOOK_LOGS", "").strip(),
        {
            "content": (
                f"❌ **Voting fehlgeschlagen** ({now})\n"
                f"```\n{reason[:1400]}\n```{run_line}"
            ),
            "allowed_mentions": {"parse": []},
        },
        name="LOGS",
    )

    # 2) Ping-Nachricht mit Link-Button zur Vote-Seite
    ping_id = os.environ.get("DISCORD_PING_ID", "").strip()
    if ping_id and not ping_id.isdigit():
        log("DISCORD_PING_ID ist keine gültige Discord-ID - es wird nicht gepingt.")
        ping_id = ""
    mention = f"<@{ping_id}> " if ping_id else ""
    post_webhook(
        os.environ.get("DISCORD_WEBHOOK_FAIL", "").strip(),
        {
            "content": f"{mention}Das tägliche Voting für den Minecraft-Server ist fehlgeschlagen. Bitte manuell voten!",
            "allowed_mentions": {"users": [ping_id]} if ping_id else {"parse": []},
            "components": [
                {
                    "type": 1,  # Action Row
                    "components": [
                        {
                            "type": 2,   # Button
                            "style": 5,  # Link-Button
                            "label": "Jetzt manuell voten",
                            "url": VOTE_URL,
                        }
                    ],
                }
            ],
        },
        name="FAIL",
        components=True,
    )

def notify_success(result: str) -> None:
    now = datetime.now(TZ).strftime("%d.%m.%Y %H:%M:%S")

    post_webhook(
        os.environ.get("DISCORD_WEBHOOK_LOGS", "").strip(),
        {
            "content": (
                f"**Minecraft-Voting erfolgreich** ({now})\n"
                f"Username: `{VOTE_USERNAME}`\n"
                f"Status: Vote erfolgreich abgeschickt.\n"
                f"Seitenmeldung: {result[:500]}"
            ),
            "allowed_mentions": {"parse": []},
        },
        name="LOGS",
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        plan = plan_wait(datetime.now(timezone.utc), os.environ.get("CRON_SCHEDULE", ""))
        if plan is None:
            log(
                "Dieser Cron-Eintrag passt nicht zur aktuellen Sommer-/Winterzeit "
                "(oder liegt außerhalb des Zeitfensters) - beende ohne Vote."
            )
            return 0
        wait_seconds, target = plan
        log(f"Vote geplant für {target:%H:%M:%S} Uhr (Europe/Zurich), warte {wait_seconds / 60:.1f} Min.")
        time.sleep(wait_seconds)
    else:
        log("Manueller Start - kein Warten, Vote läuft sofort.")

    try:
        result = run_vote()
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, VoteError):
            reason = str(exc)
        else:
            reason = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        log(f"FEHLER: {reason}")
        notify_failure(reason)  # bewusst kein erneuter Versuch
        return 1

    log(f"Vote erfolgreich. Seitenmeldung: {result}")
    return 0
  
    log(f"Vote erfolgreich. Seitenmeldung: {result}")
    notify_success(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
