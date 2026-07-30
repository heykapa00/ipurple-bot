"""
Monitor produktow iPurple.eu (PrestaShop) dla danej frazy wyszukiwania.

Wykrywa DWA zdarzenia:
  1. NOWY PRODUKT   - pojawia sie w wynikach wyszukiwania, ktorego wczesniej nie bylo.
  2. RESTOCK        - produkt, ktory byl "Out-of-Stock", staje sie dostepny.

Przy kazdym wykryciu wysyla powiadomienie systemowe (plyer) ORAZ wiadomosc
na Discorda przez webhook (wystarczy dodac URL webhooka w DISCORD_WEBHOOK_URL
ponizej - zero bota, zero tokenow logowania).
Stan (lista znanych produktow + ich dostepnosc) trzymany jest w pliku JSON,
wiec dziala poprawnie miedzy kolejnymi uruchomieniami skryptu.

Instalacja zaleznosci:
    pip install requests beautifulsoup4 plyer --break-system-packages
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from plyer import notification
    DESKTOP_NOTIFICATIONS_AVAILABLE = True
except Exception:
    # plyer moze nie dzialac w srodowisku bez GUI (np. GitHub Actions) - to normalne, pomijamy.
    DESKTOP_NOTIFICATIONS_AVAILABLE = False

# ----------------------- KONFIGURACJA -----------------------

# Fraza wyszukiwania (to co wpisujesz w search sklepu, np. "Arirang")
SEARCH_QUERY = "Arirang"

# Bazowy URL wyszukiwania (jezyk gb = English, mozesz zmienic na pl/fr/itd.)
BASE_SEARCH_URL = "https://ipurple.eu/gb/module/ambjolisearch/jolisearch"

# Co ile sekund sprawdzac strone (900 = 15 minut - rozsadny odstep, zeby nie zapetlac zapytan)
CHECK_INTERVAL_SECONDS = 900

# Maksymalna liczba stron wynikow do przejrzenia (paginacja)
MAX_PAGES = 5

# Plik, w ktorym zapisywany jest stan miedzy uruchomieniami
STATE_FILE = Path(__file__).parent / "ipurple_state.json"

# URL webhooka Discorda. Brany ze zmiennej srodowiskowej DISCORD_WEBHOOK_URL
# (na GitHub - z sekretu repozytorium; lokalnie - ustaw zmienna srodowiskowa
# albo tymczasowo wpisz URL bezposrednio zamiast os.environ.get(...)).
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# --------------------------------------------------------------


def build_page_url(page: int) -> str:
    if page <= 1:
        return f"{BASE_SEARCH_URL}?s={SEARCH_QUERY}"
    return f"{BASE_SEARCH_URL}?page={page}&s={SEARCH_QUERY}"


def extract_product_id(container, link_href: str) -> str:
    """Probuje wyciagnac unikalny ID produktu - najpierw z atrybutu data-id-product
    (standard PrestaShop), a jesli go brak, z numeru w nawiasach kwadratowych
    w adresie URL, np. .../nazwa-produktu-11200-.html -> 11200."""
    pid = container.get("data-id-product")
    if pid:
        return pid
    match = re.search(r"-(\d+)-?\.html", link_href)
    if match:
        return match.group(1)
    # ostatecznosc: caly URL jako identyfikator
    return link_href


def find_product_containers(soup: BeautifulSoup):
    """PrestaShop (szablon classic) najczesciej uzywa jednej z tych klas
    dla kontenera pojedynczego produktu na liscie/wynikach wyszukiwania."""
    for selector in [
        "article.product-miniature",
        "div.product-miniature",
        "div.js-product-miniature",
    ]:
        containers = soup.select(selector)
        if containers:
            return containers
    return []


def is_out_of_stock(container) -> bool:
    classes = " ".join(container.get("class", []))
    if "out-of-stock" in classes.lower():
        return True
    text = container.get_text(" ", strip=True).lower()
    return "out-of-stock" in text or "out of stock" in text


def fetch_products() -> dict:
    """Zwraca slownik {product_id: {"name": str, "url": str, "in_stock": bool}}"""
    products = {}

    for page in range(1, MAX_PAGES + 1):
        url = build_page_url(page)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[BLAD] Nie udalo sie pobrac strony {page}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        containers = find_product_containers(soup)

        if not containers:
            # brak produktow = koniec paginacji (albo zmienil sie layout strony)
            break

        for c in containers:
            link_tag = c.select_one("h3 a, h2 a, .product-title a") or c.find("a", href=True)
            if not link_tag:
                continue
            href = link_tag.get("href", "")
            name = link_tag.get_text(strip=True)
            if not href or not name:
                continue

            pid = extract_product_id(c, href)
            products[pid] = {
                "name": name,
                "url": href,
                "in_stock": not is_out_of_stock(c),
            }

        time.sleep(1)  # uprzejma pauza miedzy stronami

    return products


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def notify_discord(title: str, message: str) -> None:
    """Wysyla wiadomosc na Discorda przez webhook."""
    if not DISCORD_WEBHOOK_URL:
        print("[OSTRZEZENIE] Nie ustawiono DISCORD_WEBHOOK_URL - pomijam wysylke na Discorda.")
        return
    try:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": f"**{title}**\n{message}"},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[BLAD] Nie udalo sie wyslac wiadomosci na Discorda: {e}")


def notify(title: str, message: str) -> None:
    print(f"[POWIADOMIENIE] {title}: {message}")
    if DESKTOP_NOTIFICATIONS_AVAILABLE:
        try:
            notification.notify(title=title, message=message[:250], timeout=15)
        except Exception as e:
            # np. brak wspieranego backendu powiadomien na danym systemie
            print(f"[BLAD] Nie udalo sie wyslac powiadomienia systemowego: {e}")
    notify_discord(title, message)


def seed_state() -> None:
    """Zapisuje aktualna liste produktow jako punkt odniesienia, BEZ wysylania
    jakichkolwiek powiadomien. Uzyj tego raz na starcie (albo po dodaniu nowej
    frazy wyszukiwania), zeby bot nie zaspamil Cie wiadomosciami o wszystkich
    juz istniejacych produktach."""
    new_state = fetch_products()
    if not new_state:
        print("[OSTRZEZENIE] Nie znaleziono zadnych produktow - nic nie zapisano.")
        return
    save_state(new_state)
    print(f"[OK] Zapisano stan poczatkowy: {len(new_state)} produktow. "
          f"Od teraz powiadomienia beda przychodzic tylko dla realnych zmian.")


def check_once() -> None:
    old_state = load_state()
    new_state = fetch_products()

    if not new_state:
        print("[OSTRZEZENIE] Nie znaleziono zadnych produktow - "
              "mozliwe, ze selektory HTML wymagaja aktualizacji (sprawdz strone recznie).")
        return

    for pid, info in new_state.items():
        old_info = old_state.get(pid)

        if old_info is None:
            notify(
                "Nowy produkt!",
                f"{info['name']}\n{info['url']}",
            )
        elif (not old_info.get("in_stock", True)) and info["in_stock"]:
            notify(
                "Produkt ponownie dostepny!",
                f"{info['name']}\n{info['url']}",
            )

    save_state(new_state)


def main():
    if "--seed" in sys.argv:
        seed_state()
        return

    if "--once" in sys.argv:
        # Tryb jednorazowy - uzywany przez harmonogram GitHub Actions
        # (to on odpowiada za cykliczne uruchamianie, skrypt tylko sprawdza raz i konczy dzialanie).
        check_once()
        return

    print(f"Uruchomiono monitorowanie frazy '{SEARCH_QUERY}' na ipurple.eu")
    print(f"Sprawdzanie co {CHECK_INTERVAL_SECONDS} sekund. Ctrl+C aby zatrzymac.\n")
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"[BLAD] Niespodziewany blad podczas sprawdzania: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
