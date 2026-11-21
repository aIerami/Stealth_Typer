# -*- coding: utf-8 -*-

import asyncio
import random
import sys
from pathlib import Path

from patchright.async_api import async_playwright

import config as cfg

# ============================================================
# 1. Таблица кириллицы: символ -> (code, windowsVirtualKeyCode)
# ============================================================

_RU_LAYOUT = "йцукенгшщзхъфывапролджэячсмитьбю"
# Физические клавиши US-раскладки в том же порядке, что и буквы ЙЦУКЕН
_US_CODES = [
    "KeyQ", "KeyW", "KeyE", "KeyR", "KeyT", "KeyY", "KeyU", "KeyI",
    "KeyO", "KeyP", "BracketLeft", "BracketRight",
    "KeyA", "KeyS", "KeyD", "KeyF", "KeyG", "KeyH", "KeyJ", "KeyK",
    "KeyL", "Semicolon", "Quote",
    "KeyZ", "KeyX", "KeyC", "KeyV", "KeyB", "KeyN", "KeyM",
    "Comma", "Period", "Slash",
]

# VK-коды для этих физических клавиш (латиница/цифры/пунктуация)
_VK_MAP = {
    "KeyQ": 81, "KeyW": 87, "KeyE": 69, "KeyR": 82, "KeyT": 84, "KeyY": 89,
    "KeyU": 85, "KeyI": 73, "KeyO": 79, "KeyP": 80,
    "BracketLeft": 219, "BracketRight": 221,
    "KeyA": 65, "KeyS": 83, "KeyD": 68, "KeyF": 70, "KeyG": 71, "KeyH": 72,
    "KeyJ": 74, "KeyK": 75, "KeyL": 76,
    "Semicolon": 186, "Quote": 222,
    "KeyZ": 90, "KeyX": 88, "KeyC": 67, "KeyV": 86, "KeyB": 66, "KeyN": 78,
    "KeyM": 77,
    "Comma": 188, "Period": 190, "Slash": 191,
}

CYRILLIC_MAP = {}
for ch, code in zip(_RU_LAYOUT, _US_CODES):
    vk = _VK_MAP[code]
    CYRILLIC_MAP[ch] = (code, vk, 0)               # строчная, без Shift
    CYRILLIC_MAP[ch.upper()] = (code, vk, 8)       # заглавная, Shift

# Латинские буквы и цифры (нижний регистр)
for ch in "abcdefghijklmnopqrstuvwxyz":
    code = f"Key{ch.upper()}"
    CYRILLIC_MAP[ch] = (code, ord(ch.upper()), 0)
for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    code = f"Key{ch}"
    CYRILLIC_MAP[ch] = (code, ord(ch), 8)

for d in "0123456789":
    CYRILLIC_MAP[d] = (f"Digit{d}", ord(d), 0)

# Пунктуация, которая требует Shift на US-раскладке
_SHIFT_SYMBOLS = {
    "!": ("Digit1", 49), "@": ("Digit2", 50), "#": ("Digit3", 51),
    "$": ("Digit4", 52), "%": ("Digit5", 53), "^": ("Digit6", 54),
    "&": ("Digit7", 55), "*": ("Digit8", 56), "(": ("Digit9", 57),
    ")": ("Digit0", 48),
    "_": ("Minus", 189), "+": ("Equal", 187),
    "{": ("BracketLeft", 219), "}": ("BracketRight", 221),
    "|": ("Backslash", 220), ":": ("Semicolon", 186), '"': ("Quote", 222),
    "<": ("Comma", 188), ">": ("Period", 190), "?": ("Slash", 191),
    "~": ("Backquote", 192),
}
for sym, (code, vk) in _SHIFT_SYMBOLS.items():
    CYRILLIC_MAP[sym] = (code, vk, 8)

# Символы без Shift
_NORMAL_SYMBOLS = {
    " ": ("Space", 32), "-": ("Minus", 189), "=": ("Equal", 187),
    "[": ("BracketLeft", 219), "]": ("BracketRight", 221),
    "\\": ("Backslash", 220), ";": ("Semicolon", 186), "'": ("Quote", 222),
    ",": ("Comma", 188), ".": ("Period", 190), "/": ("Slash", 191),
    "`": ("Backquote", 192),
}
for sym, (code, vk) in _NORMAL_SYMBOLS.items():
    CYRILLIC_MAP[sym] = (code, vk, 0)

# ============================================================
# 2. Вспомогательные функции
# ============================================================

def load_text() -> str:
    path = Path(cfg.TEXT_FILE)
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
        print(f"[!] Файл {cfg.TEXT_FILE} пуст, использую TEXT_INLINE.")
    return cfg.TEXT_INLINE.strip()


def _typing_delay(wpm: int) -> float:
    """Средняя задержка на символ, сек (5 символов = слово)."""
    base = 12.0 / wpm          # 60 WPM -> 0.2 с
    return random.uniform(base * 0.5, base * 1.8)


def _pick_error_char(ch: str) -> str:
    """Случайный символ из той же раскладки, не равный исходному."""
    if ch in "йцукенгшщзхъфывапролджэячсмитьбю":
        pool = "йцукенгшщзхъфывапролджэячсмитьбю"
    elif ch in "abcdefghijklmnopqrstuvwxyz":
        pool = "abcdefghijklmnopqrstuvwxyz"
    elif ch.isdigit():
        pool = "0123456789"
    else:
        return random.choice("abcdefghijklmnopqrstuvwxyz")
    return random.choice([c for c in pool if c != ch])


# ============================================================
# 3. CDP-ввод
# ============================================================

async def _cdp_key(
    cdp,
    key: str,
    code: str,
    vk: int,
    text: str = "",
    modifiers: int = 0,
    event_type: str = "keyDown",
) -> None:
    params = {
        "type": event_type,
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
        "modifiers": modifiers,
    }
    if text and event_type == "keyDown":
        params["text"] = text
        params["unmodifiedText"] = text

    if cfg.CDP_DEBUG:
        print(f"  CDP {event_type}: key={key!r} code={code!r} vk={vk} text={text!r} mod={modifiers}")

    await cdp.send("Input.dispatchKeyEvent", params)


async def _cdp_press_char(cdp, ch: str) -> None:
    """Отправить keyDown + keyUp для печатного символа."""
    if ch not in CYRILLIC_MAP:
        print(f"[!] Нет маппинга для символа {ch!r} — пропускаю.")
        return
    code, vk, mod = CYRILLIC_MAP[ch]
    await _cdp_key(cdp, ch, code, vk, text=ch, modifiers=mod, event_type="keyDown")
    await _cdp_key(cdp, ch, code, vk, modifiers=mod, event_type="keyUp")


async def _cdp_backspace(cdp) -> None:
    await _cdp_key(cdp, "Backspace", "Backspace", 8, event_type="rawKeyDown")
    await _cdp_key(cdp, "Backspace", "Backspace", 8, event_type="keyUp")


async def _cdp_enter(cdp) -> None:
    await _cdp_key(cdp, "Enter", "Enter", 13, text="\r", event_type="keyDown")
    await _cdp_key(cdp, "Enter", "Enter", 13, event_type="keyUp")


async def human_type_cdp(cdp, text: str, wpm: int) -> None:
    """Посимвольный ввод через CDP с задержками и опечатками."""
    for ch in text:
        if ch == "\n":
            await _cdp_enter(cdp)
            await asyncio.sleep(_typing_delay(wpm) * 1.5)
            continue

        # Опечатка
        if random.random() < cfg.ERROR_RATE:
            wrong = _pick_error_char(ch)
            await _cdp_press_char(cdp, wrong)
            await asyncio.sleep(_typing_delay(wpm) * 0.6)
            await _cdp_backspace(cdp)
            await asyncio.sleep(_typing_delay(wpm) * 0.4)

        await _cdp_press_char(cdp, ch)
        await asyncio.sleep(_typing_delay(wpm))

        # Редкая «задумчивая» пауза
        if random.random() < 0.01:
            await asyncio.sleep(random.uniform(0.8, 2.0))


# ============================================================
# 4. Адаптер поля: фокусировка и позиционирование
# ============================================================

async def focus_and_position(page, field, cdp) -> str:
    """
    Определяет тип поля, кликает, позиционирует курсор.
    Возвращает строку-тип: 'input' | 'textarea' | 'contenteditable' | 'monaco'.
    """
    tag = await field.evaluate("el => el.tagName.toLowerCase()")
    is_ce = await field.evaluate("el => el.isContentEditable")
    # Проверка на Monaco: ищем предка с классом monaco-editor
    is_monaco = await field.evaluate(
        "el => !!el.closest('.monaco-editor')"
    )

    if is_monaco:
        print("[i] Тип поля: Monaco Editor")
        # Клик по видимой области редактора
        view_lines = page.locator(".monaco-editor .view-lines").first
        if await view_lines.count():
            await view_lines.click(position={"x": 10, "y": 10})
        else:
            await field.click()
        await asyncio.sleep(0.3)

        # Отключаем авто-закрытие через API, если доступно
        if cfg.DISABLE_MONACO_AUTO_CLOSE:
            try:
                await page.evaluate("""
                    () => {
                        if (typeof monaco === 'object' && monaco.editor) {
                            const eds = monaco.editor.getEditors();
                            if (eds.length) {
                                eds[0].updateOptions({
                                    autoClosingBrackets: 'never',
                                    autoClosingQuotes: 'never',
                                    autoClosingOvertype: 'never'
                                });
                            }
                        }
                    }
                """)
                print("[i] Авто-закрытие Monaco отключено через API.")
            except Exception as e:
                print(f"[!] Не удалось отключить авто-закрытие Monaco: {e}")

        # Позиционирование на TARGET_LINE
        try:
            await page.evaluate(f"""
                () => {{
                    if (typeof monaco === 'object' && monaco.editor) {{
                        const eds = monaco.editor.getEditors();
                        if (eds.length) {{
                            eds[0].setPosition({{ lineNumber: {cfg.TARGET_LINE}, column: 1 }});
                            eds[0].focus();
                        }}
                    }}
                }}
            """)
            print(f"[i] Курсор установлен на строку {cfg.TARGET_LINE}.")
        except Exception as e:
            print(f"[!] Не удалось позиционировать курсор Monaco: {e}")

        return "monaco"

    if tag in ("input", "textarea"):
        await field.click()
        await page.keyboard.press("Control+End")
        print(f"[i] Тип поля: {tag}")
        return tag

    if is_ce:
        await field.click()
        # Ctrl+End для contenteditable
        await page.keyboard.press("Control+End")
        print("[i] Тип поля: contenteditable")
        return "contenteditable"

    # Fallback
    await field.click()
    print(f"[i] Тип поля: неизвестен (тег {tag}), клик выполнен.")
    return "unknown"


# ============================================================
# 5. main
# ============================================================

async def main() -> None:
    text = load_text()
    if not text:
        print("[!] Нет текста для ввода.")
        return

    wpm = random.randint(cfg.WPM_MIN, cfg.WPM_MAX)
    print(f"[i] Целевая скорость: {wpm} WPM")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=cfg.USER_DATA_DIR,
            channel=cfg.BROWSER_CHANNEL,
            headless=cfg.HEADLESS,
            no_viewport=cfg.NO_VIEWPORT,
            args=cfg.BROWSER_ARGS,
        )

        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(cfg.PAGE_LOAD_TIMEOUT_MS)

            print(f"[i] Переход на {cfg.TARGET_URL} …")
            try:
                await page.goto(cfg.TARGET_URL, wait_until="domcontentloaded")
            except Exception as e:
                print(f"[!] Ошибка загрузки: {e}")
                await page.screenshot(path="error_page.png")
                return

            if cfg.WAIT_FOR_MANUAL_LOGIN:
                input("\n>>> Войдите вручную, затем нажмите Enter…")
                print("[i] Продолжаем.")

            print(f"[i] Ищу поле: {cfg.FIELD_SELECTOR}")
            try:
                field = page.locator(cfg.FIELD_SELECTOR).first
                await field.wait_for(state="visible", timeout=15_000)
            except Exception as e:
                print(f"[!] Поле не найдено: {e}")
                await page.screenshot(path="error_field.png")
                return

            # Создаём CDP-сессию
            cdp = await context.new_cdp_session(page)

            # Фокусировка и позиционирование
            field_type = await focus_and_position(page, field, cdp)
            print(f"[i] Поле готово ({field_type}). Начинаю CDP-ввод…")

            try:
                await human_type_cdp(cdp, text, wpm)
                print("[✓] Ввод завершён. Итоговый текст должен быть без ошибок.")
            except Exception as e:
                print(f"[!] Ошибка во время ввода: {e}")
                await page.screenshot(path="error_typing.png")
                return
            finally:
                await cdp.detach()

            if cfg.CLOSE_BROWSER_AFTER:
                print("[i] Закрываю браузер…")
            else:
                print("[i] Браузер оставлен открытым. Enter для выхода.")
                input()

        finally:
            if cfg.CLOSE_BROWSER_AFTER:
                await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[i] Прервано (Ctrl+C).")
        sys.exit(0)
    except Exception as exc:
        print(f"[!] Непредвиденная ошибка: {exc}")
        sys.exit(1)