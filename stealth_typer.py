# -*- coding: utf-8 -*-

import asyncio
import random
import sys
from pathlib import Path

from patchright.async_api import async_playwright
from humantyping import HumanTyper

try:
    import humantyping.config as ht_config
except ImportError:
    ht_config = None

import config as cfg


def load_text() -> str:
    """Читает текст из файла или возвращает TEXT_INLINE."""
    path = Path(cfg.TEXT_FILE)
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
        print(f"[!] Файл {cfg.TEXT_FILE} пуст, использую TEXT_INLINE.")
    return cfg.TEXT_INLINE.strip()


def apply_error_rates() -> None:
    """Переопределяет вероятности ошибок в конфиге HumanTyping, если возможно."""
    if ht_config is None:
        print("[!] Не удалось импортировать humantyping.config — "
              "вероятности ошибок останутся стандартными (4% / 1.5%).")
        return

    # В Python-версии HumanTyping имена могут отличаться.
    # Пробуем установить оба известных варианта.
    try:
        if hasattr(ht_config, "PROB_ERROR"):
            ht_config.PROB_ERROR = cfg.ERROR_RATE
        elif hasattr(ht_config, "ERROR_RATE"):
            ht_config.ERROR_RATE = cfg.ERROR_RATE
        else:
            print("[!] В humantyping.config не найдена переменная вероятности опечаток.")

        if hasattr(ht_config, "PROB_SWAP_ERROR"):
            ht_config.PROB_SWAP_ERROR = cfg.SWAP_ERROR_RATE
        elif hasattr(ht_config, "SWAP_ERROR_RATE"):
            ht_config.SWAP_ERROR_RATE = cfg.SWAP_ERROR_RATE
        else:
            print("[!] В humantyping.config не найдена переменная вероятности перестановок.")
    except Exception as e:
        print(f"[!] Не удалось переопределить вероятности ошибок: {e}")


async def main() -> None:
    text = load_text()
    if not text:
        print("[!] Нет текста для ввода. Проверьте input_text.txt или TEXT_INLINE.")
        return

    # Применяем вероятности ошибок до создания HumanTyper
    apply_error_rates()

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
                print(f"[!] Ошибка загрузки страницы: {e}")
                await page.screenshot(path="error_page.png")
                print("[i] Скриншот сохранён в error_page.png")
                return

            if cfg.WAIT_FOR_MANUAL_LOGIN:
                input("\n>>> Войдите вручную в открытом окне, затем нажмите Enter здесь…")
                print("[i] Продолжаем после ручного входа.")

            print(f"[i] Ищу поле: {cfg.FIELD_SELECTOR}")
            try:
                field = page.locator(cfg.FIELD_SELECTOR).first
                await field.wait_for(state="visible", timeout=15_000)
            except Exception as e:
                print(f"[!] Поле не найдено: {e}")
                await page.screenshot(path="error_field.png")
                print("[i] Скриншот сохранён в error_field.png")
                return

            await field.click()
            print("[i] Клик по полю выполнен. Начинаю посимвольный ввод…")

            # HumanTyper принимает wpm и layout; вероятности ошибок заданы выше.
            typer = HumanTyper(wpm=wpm, layout=cfg.LAYOUT)

            try:
                await typer.type(field, text)
                print("[✓] Ввод завершён. Итоговый текст должен быть без ошибок.")
            except Exception as e:
                print(f"[!] Ошибка во время ввода: {e}")
                await page.screenshot(path="error_typing.png")
                print("[i] Скриншот сохранён в error_typing.png")
                return

            if cfg.CLOSE_BROWSER_AFTER:
                print("[i] Закрываю браузер…")
            else:
                print("[i] Браузер оставлен открытым. Нажмите Enter для выхода.")
                input()

        finally:
            if cfg.CLOSE_BROWSER_AFTER:
                await context.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[i] Прервано пользователем (Ctrl+C).")
        sys.exit(0)
    except Exception as exc:
        print(f"[!] Непредвиденная ошибка: {exc}")
        sys.exit(1)