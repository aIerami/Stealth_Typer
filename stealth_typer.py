# -*- coding: utf-8 -*-

import asyncio
import random
import re
import sys
from pathlib import Path

from patchright.async_api import async_playwright

import config as cfg


# ============================================================
# 1. Таблицы символов
# ============================================================

_RU_LAYOUT = "йцукенгшщзхъфывапролджэячсмитьбю"
_US_CODES = [
    "KeyQ", "KeyW", "KeyE", "KeyR", "KeyT", "KeyY", "KeyU", "KeyI",
    "KeyO", "KeyP", "BracketLeft", "BracketRight",
    "KeyA", "KeyS", "KeyD", "KeyF", "KeyG", "KeyH", "KeyJ", "KeyK",
    "KeyL", "Semicolon", "Quote",
    "KeyZ", "KeyX", "KeyC", "KeyV", "KeyB", "KeyN", "KeyM",
    "Comma", "Period", "Slash",
]
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
    CYRILLIC_MAP[ch] = (code, vk, 0)
    CYRILLIC_MAP[ch.upper()] = (code, vk, 8)

for ch in "abcdefghijklmnopqrstuvwxyz":
    CYRILLIC_MAP[ch] = (f"Key{ch.upper()}", ord(ch.upper()), 0)
for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    CYRILLIC_MAP[ch] = (f"Key{ch}", ord(ch), 8)
for d in "0123456789":
    CYRILLIC_MAP[d] = (f"Digit{d}", ord(d), 0)

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
# 2. HTML-токенизация
# ============================================================

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


def _split_tokens(line: str):
    """
    Разбивает строку HTML на токены:
      ('open_tag', '<div>')
      ('self_tag', '<meta charset="UTF-8">')
      ('close_tag', '</div>')
      ('text', 'Hello')
    """
    tokens = []
    i, n = 0, len(line)
    while i < n:
        if line[i] == "<":
            j = line.find(">", i)
            if j == -1:
                tokens.append(("text", line[i:]))
                break
            inner = line[i + 1 : j]
            if inner.startswith("/"):
                tokens.append(("close_tag", line[i : j + 1]))
            elif inner.endswith("/") or inner.split()[0].lower() in VOID_TAGS:
                tokens.append(("self_tag", line[i : j + 1]))
            else:
                tokens.append(("open_tag", line[i : j + 1]))
            i = j + 1
        else:
            j = line.find("<", i)
            if j == -1:
                tokens.append(("text", line[i:]))
                break
            tokens.append(("text", line[i:j]))
            i = j
    return tokens


def _next_line_closing_only(lines, i):
    """True, если следующая непустая строка — только закрывающие теги."""
    for k in range(i + 1, len(lines)):
        s = lines[k].strip()
        if not s:
            continue
        tokens = _split_tokens(s)
        return bool(tokens) and all(kind == "close_tag" for kind, _ in tokens)
    return False


# ============================================================
# 3. Хелперы
# ============================================================

def load_text() -> str:
    path = Path(cfg.TEXT_FILE)
    if path.is_file():
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").strip()
        if text:
            return text
        print(f"[!] Файл {cfg.TEXT_FILE} пуст, использую TEXT_INLINE.")
    return cfg.TEXT_INLINE.strip()


def _typing_delay(wpm: int) -> float:
    base = 12.0 / wpm
    return random.uniform(base * 0.5, base * 1.8)


def _pick_error_char(ch: str) -> str:
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
# 4. CDP
# ============================================================

async def _cdp_key(cdp, key, code, vk, text="", modifiers=0, event_type="keyDown"):
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
        print(f"  CDP {event_type}: key={key!r} text={text!r}")
    await cdp.send("Input.dispatchKeyEvent", params)


async def _cdp_press_char(cdp, ch):
    if ch not in CYRILLIC_MAP:
        print(f"[!] Нет маппинга для {ch!r}")
        return
    code, vk, mod = CYRILLIC_MAP[ch]
    await _cdp_key(cdp, ch, code, vk, text=ch, modifiers=mod, event_type="keyDown")
    await _cdp_key(cdp, ch, code, vk, modifiers=mod, event_type="keyUp")


async def _cdp_backspace(cdp):
    await _cdp_key(cdp, "Backspace", "Backspace", 8, event_type="rawKeyDown")
    await _cdp_key(cdp, "Backspace", "Backspace", 8, event_type="keyUp")


async def _cdp_enter(cdp):
    await _cdp_key(cdp, "Enter", "Enter", 13, text="\r", event_type="keyDown")
    await _cdp_key(cdp, "Enter", "Enter", 13, event_type="keyUp")


async def _cdp_end(cdp):
    await _cdp_key(cdp, "End", "End", 35, event_type="rawKeyDown")
    await _cdp_key(cdp, "End", "End", 35, event_type="keyUp")


async def _cdp_down(cdp):
    await _cdp_key(cdp, "ArrowDown", "ArrowDown", 40, event_type="rawKeyDown")
    await _cdp_key(cdp, "ArrowDown", "ArrowDown", 40, event_type="keyUp")


async def _cdp_right(cdp, n=1):
    for _ in range(n):
        await _cdp_key(cdp, "ArrowRight", "ArrowRight", 39, event_type="rawKeyDown")
        await _cdp_key(cdp, "ArrowRight", "ArrowRight", 39, event_type="keyUp")


# ============================================================
# 5. Monaco API
# ============================================================

async def _monaco_eval(page, js_body: str):
    return await page.evaluate(f"""
        () => {{
            if (typeof monaco !== 'object' || !monaco.editor) return null;
            const eds = monaco.editor.getEditors();
            const t = eds.find(e => {{
                const n = e.getDomNode && e.getDomNode();
                return n && n.hasAttribute && n.hasAttribute('data-stealth-target');
            }});
            if (!t) return null;
            {js_body}
        }}
    """)


async def _find_autoclose_pos(page, closing_tag: str):
    """
    Ищет ближайший closing_tag в модели ПОСЛЕ текущей позиции курсора.
    Возвращает dict { curLine, targetLine } или None.
    """
    tag_js = closing_tag.replace("\\", "\\\\").replace("'", "\\'")
    return await _monaco_eval(page, f"""
        const model = t.getModel();
        const pos = t.getPosition();
        const total = model.getLineCount();
        let targetLine = -1;
        for (let ln = pos.lineNumber; ln <= total; ln++) {{
            const text = model.getLineContent(ln);
            const startCol = ln === pos.lineNumber ? pos.column - 1 : 0;
            const idx = text.indexOf('{tag_js}', startCol);
            if (idx !== -1) {{
                targetLine = ln;
                break;
            }}
        }}
        return {{ curLine: pos.lineNumber, targetLine }};
    """)


# ============================================================
# 6. Ввод с autoclose
# ============================================================

async def _type_raw(cdp, s: str, wpm: int, allow_typos: bool = True):
    for ch in s:
        if allow_typos and ch not in '"\'([{<>&`' and random.random() < cfg.ERROR_RATE:
            wrong = _pick_error_char(ch)
            await _cdp_press_char(cdp, wrong)
            await asyncio.sleep(_typing_delay(wpm) * 0.6)
            await _cdp_backspace(cdp)
            await asyncio.sleep(_typing_delay(wpm) * 0.4)
        await _cdp_press_char(cdp, ch)
        await asyncio.sleep(_typing_delay(wpm))


async def _navigate_to_autoclose(page, cdp, closing_tag: str):
    """Пропускает закрывающий тег: перемещает курсор за autoclose-инстанс."""
    info = await _find_autoclose_pos(page, closing_tag)
    if not info or info.get("targetLine", -1) == -1:
        # autoclose не найден — печатаем closing_tag вручную
        await _type_raw(cdp, closing_tag, 100, allow_typos=False)
        return
    down_count = info["targetLine"] - info["curLine"]
    for _ in range(max(0, down_count)):
        await _cdp_down(cdp)
    await _cdp_end(cdp)


async def type_line(page, cdp, line: str, wpm: int):
    tokens = _split_tokens(line)
    for kind, value in tokens:
        if kind == "text":
            await _type_raw(cdp, value, wpm)
        elif kind == "open_tag":
            # печатаем <tag ...> — Monaco HTML создаст </tag>
            await _type_raw(cdp, value, wpm, allow_typos=False)
        elif kind == "self_tag":
            await _type_raw(cdp, value, wpm, allow_typos=False)
        elif kind == "close_tag":
            if cfg.USE_HTML_AUTOCLOSE:
                await _navigate_to_autoclose(page, cdp, value)
            else:
                await _type_raw(cdp, value, wpm, allow_typos=False)


async def human_type_html(page, cdp, text: str, wpm: int):
    lines = text.split("\n")
    for i, line in enumerate(lines):
        is_last = (i == len(lines) - 1)
        if line.strip() == "":
            if not is_last:
                await _cdp_enter(cdp)
                await asyncio.sleep(_typing_delay(wpm))
            continue

        await type_line(page, cdp, line, wpm)

        if is_last:
            break

        if cfg.USE_HTML_AUTOCLOSE and _next_line_closing_only(lines, i):
            # следующая строка — только </tag>, не нажимаем Enter:
            # курсор уже на строке с autoclose ниже.
            continue

        await _cdp_enter(cdp)
        await asyncio.sleep(_typing_delay(wpm) * 1.2)
        if random.random() < 0.02:
            await asyncio.sleep(random.uniform(0.6, 1.5))


# ============================================================
# 7. focus_and_position
# ============================================================

async def focus_and_position(page, field, cdp):
    tag = await field.evaluate("el => el.tagName.toLowerCase()")
    is_ce = await field.evaluate("el => el.isContentEditable")
    is_monaco = await field.evaluate("el => !!el.closest('.monaco-editor')")

    if is_monaco:
        print("[i] Тип поля: Monaco Editor")
        await field.evaluate("""
            el => {
                const ed = el.closest('.monaco-editor');
                if (ed) ed.setAttribute('data-stealth-target', '1');
            }
        """)

        result = await page.evaluate(f"""
            () => {{
                if (typeof monaco !== 'object' || !monaco.editor) return {{ status: 'no-monaco-api' }};
                const eds = monaco.editor.getEditors();
                const target = eds.find(e => {{
                    const n = e.getDomNode && e.getDomNode();
                    return n && n.hasAttribute && n.hasAttribute('data-stealth-target');
                }});
                if (!target) return {{ status: 'target-not-found' }};
                target.updateOptions({{
                    autoIndent: 'none',
                    formatOnType: false,
                    formatOnPaste: false,
                    autoClosingBrackets: 'never',
                    autoClosingQuotes: 'never',
                    autoClosingDelete: 'never',
                    autoClosingOvertype: 'auto',
                    quickSuggestions: false,
                    suggestOnTriggerCharacters: false,
                    snippetSuggestions: 'none',
                    tabCompletion: 'off',
                }});
                target.setPosition({{ lineNumber: {cfg.TARGET_LINE}, column: 1 }});
                target.revealPositionInCenter({{ lineNumber: {cfg.TARGET_LINE}, column: 1 }});
                target.focus();
                return {{ status: 'ok' }};
            }}
        """)
        print(f"[i] Monaco API: {result}")

        await asyncio.sleep(0.2)
        try:
            await field.focus(timeout=3000)
        except Exception:
            pass

        active_ok = await page.evaluate("""
            () => {
                const a = document.activeElement;
                return a && a.classList && a.classList.contains('inputarea');
            }
        """)
        if not active_ok:
            print("[!] textarea не в фокусе — force-клик.")
            try:
                await field.click(force=True, timeout=5000)
            except Exception as e:
                print(f"[!] Force-клик не удался: {e}")
        return "monaco"

    if tag in ("input", "textarea"):
        await field.click()
        await page.keyboard.press("Control+End")
        print(f"[i] Тип поля: {tag}")
        return tag

    if is_ce:
        await field.click()
        await page.keyboard.press("Control+End")
        print("[i] Тип поля: contenteditable")
        return "contenteditable"

    await field.click()
    print(f"[i] Тип поля: неизвестен ({tag}).")
    return "unknown"


async def cleanup_label(page):
    await page.evaluate("""
        () => {
            const el = document.querySelector('.monaco-editor[data-stealth-target]');
            if (el) el.removeAttribute('data-stealth-target');
        }
    """)


# ============================================================
# 8. main
# ============================================================

async def main() -> None:
    text = load_text()
    if not text:
        print("[!] Нет текста.")
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
                input("\n>>> Войдите вручную, затем Enter…")

            print(f"[i] Ищу поле: {cfg.FIELD_SELECTOR} (index={cfg.FIELD_INDEX})")
            try:
                field = page.locator(cfg.FIELD_SELECTOR).nth(cfg.FIELD_INDEX)
                await field.wait_for(state="visible", timeout=15_000)
            except Exception as e:
                print(f"[!] Поле не найдено: {e}")
                await page.screenshot(path="error_field.png")
                return

            cdp = await context.new_cdp_session(page)
            field_type = await focus_and_position(page, field, cdp)
            print(f"[i] Поле готово ({field_type}). Начинаю CDP-ввод…")

            try:
                if field_type == "monaco" and cfg.USE_HTML_AUTOCLOSE:
                    await human_type_html(page, cdp, text, wpm)
                else:
                    # fallback — обычный посимвольный ввод через CDP
                    await _type_raw(cdp, text.replace("\n", ""), wpm)
                print("[✓] Ввод завершён.")
            except Exception as e:
                print(f"[!] Ошибка во время ввода: {e}")
                await page.screenshot(path="error_typing.png")
                return
            finally:
                await cdp.detach()
                await cleanup_label(page)

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