# -*- coding: utf-8 -*-
"""
Stealth Typer — посимвольный ввод текста в браузере (Edge) с человеческим ритмом.

Запуск:   python stealth_typer.py [--site ИМЯ] [--text ФАЙЛ] [--mode launch|attach]
                                  [--cursor auto|manual] [--wpm 50-80] [--dry-run] [--list]
Все настройки — в config.py.
"""

import argparse
import asyncio
import random
import sys
import time
import traceback
from pathlib import Path

import settings

LOG_PATH = None


def log(msg: str = "") -> None:
    print(msg, flush=True)
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
        except OSError:
            pass


# ------------------------------------------------------------
# Текст
# ------------------------------------------------------------

def load_text(cfg, path_override=None) -> str:
    path = settings.resolve_path(path_override or cfg.TEXT_FILE)
    text = ""
    if path.is_file():
        text = path.read_text(encoding="utf-8-sig")
    elif path_override:
        raise settings.ConfigError(f"Файл с текстом не найден: {path}")
    if not text.strip():
        if text == "" and not path.is_file():
            log(f"[i] Файла {path.name} нет — использую TEXT_INLINE.")
        text = cfg.TEXT_INLINE
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if cfg.TRIM_TRAILING_NEWLINES:
        text = text.rstrip("\n")
    bad = sorted({c for c in text if ord(c) < 32 and c not in "\n\t"})
    if bad:
        log(f"[!] В тексте есть управляющие символы {[hex(ord(c)) for c in bad]} — они будут удалены.")
        text = "".join(c for c in text if c not in bad)
    return text


# ------------------------------------------------------------
# Dry-run: без браузера, на симуляторе
# ------------------------------------------------------------

def dry_run(cfg, text: str) -> int:
    from engine import Typist
    from simulator import FakeEditor, VirtualClock

    clock = VirtualClock()
    ed = FakeEditor(clock=clock, autoclose_pairs=True, indent="smart")
    t = Typist(ed, ed, cfg, rng=random.Random(cfg.RANDOM_SEED), sleep=clock.sleep, now=clock.now, log=log)
    stats = asyncio.run(t.type_text(text))
    ok = ed.text == text
    mins = stats["duration"] / 60
    avg = stats["wpm_sum"] / max(1, stats["chars"])
    log(f"[dry-run] знаков: {len(text)}, ожидаемое время: {int(mins)} мин {int(stats['duration'] % 60)} с")
    log(f"[dry-run] скорость: от {stats['wpm_min']:.0f} до {stats['wpm_max']:.0f}, в среднем {avg:.0f} WPM")
    log(f"[dry-run] опечаток исправлено: {stats['typos']}, лишнего удалено у редактора: {stats['junk_removed']}")
    log(f"[dry-run] результат на симуляторе {'совпал' if ok else 'НЕ СОВПАЛ'} с исходным текстом")
    return 0 if ok else 1


# ------------------------------------------------------------
# Основной сценарий
# ------------------------------------------------------------

async def list_mode(cfg) -> int:
    from patchright.async_api import async_playwright
    import browser as br

    async with async_playwright() as p:
        sess = await br.open_session(p, cfg, log)
        try:
            if cfg.CONFIRM_BEFORE_START:
                await br.ask("Откройте страницу с редакторами и нажмите Enter… ")
            found = await br.list_editors(sess.page)
            if not found:
                log("Monaco-редакторов на странице не найдено (или window.monaco недоступен).")
            for fr, r in found:
                log(f"Кадр: {fr.url[:80]} — всего редакторов {r['total']}, видимых {len(r['visible'])}")
                for e in r["visible"]:
                    log(f"  FIELD_INDEX={e['index']}: язык={e['lang'] or '?'}, строк={e['lines']}, "
                        f"размер={e['size']}, первая строка={e['first']!r}"
                        f"{', ФОКУС' if e['focused'] else ''}{', только чтение' if e['readOnly'] else ''}")
            log("Для обычных полей (textarea / contenteditable) используйте FIELD_SELECTOR.")
        finally:
            if sess.owned:
                await sess.context.close()
    return 0


async def run(cfg, text: str) -> int:
    from patchright.async_api import async_playwright
    import browser as br
    from engine import Typist

    if not text:
        log("[!] Нет текста для ввода.")
        return 1

    rng = random.Random(cfg.RANDOM_SEED)
    log(f"[i] Режим браузера: {cfg.BROWSER_MODE}, курсор: {cfg.CURSOR_MODE}, "
        f"скорость {cfg.WPM_MIN}–{cfg.WPM_MAX} WPM, знаков: {len(text)}")

    async with async_playwright() as p:
        sess = await br.open_session(p, cfg, log)
        page = sess.page
        target = None
        ok = False
        try:
            # --- подготовка ---
            if cfg.CURSOR_MODE == "manual":
                await br.ask("\n>>> Кликните в редактор там, где нужно печатать (курсор должен мигать), "
                             "затем вернитесь сюда и нажмите Enter… ")
            elif cfg.CONFIRM_BEFORE_START:
                await br.ask("\n>>> Страница готова (вход выполнен, редактор виден)? Нажмите Enter… ")

            target = await br.resolve_target(page, cfg, log)
            if target.kind == "input" and "\n" in text:
                raise RuntimeError("Текст многострочный, а поле однострочное (input): "
                                   "Enter может отправить форму. Выберите textarea/редактор.")

            await page.bring_to_front()
            cdp = await sess.context.new_cdp_session(page)
            try:
                await cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            except Exception:
                pass

            await target.prepare(cfg, manual=(cfg.CURSOR_MODE == "manual"))
            if cfg.START_DELAY_S > 0:
                log(f"[i] Начинаю через {cfg.START_DELAY_S} с — не трогайте мышь и клавиатуру в окне браузера.")
                await asyncio.sleep(cfg.START_DELAY_S)

            # --- ввод ---
            typist = Typist(br.CdpDriver(cdp), target, cfg, rng=rng, log=log)
            typist.eol_len = target.eol_len
            try:
                stats = await typist.type_text(text)
            finally:
                try:
                    await cdp.detach()
                except Exception:
                    pass
            avg = stats["wpm_sum"] / max(1, stats["chars"])
            log(f"[✓] Ввод завершён за {int(stats['duration'] // 60)} мин {int(stats['duration'] % 60)} с. "
                f"Скорость {stats['wpm_min']:.0f}–{stats['wpm_max']:.0f} (в среднем {avg:.0f}) WPM, "
                f"опечаток исправлено: {stats['typos']}.")
            if stats["anomalies"]:
                log(f"[!] Редактор неожиданно менял текст {stats['anomalies']} раз(а) — смотрите проверку ниже.")

            # --- проверка ---
            await asyncio.sleep(0.5)
            if cfg.VERIFY_RESULT:
                ok, msg = await br.verify(target, text)
                log(("[✓] " if ok else "[!] ") + msg)
            else:
                ok = True
        except Exception as e:
            log(f"[!] Ошибка: {e}")
            log(traceback.format_exc())
            if cfg.SCREENSHOT_ON_ERROR:
                try:
                    shot = settings.SCRIPT_DIR / "error_screenshot.png"
                    await page.screenshot(path=str(shot))
                    log(f"[i] Скриншот: {shot}")
                except Exception:
                    pass
            ok = False
        finally:
            if target is not None:
                await target.finish(cfg)

        # --- завершение ---
        if sess.owned:
            if cfg.KEEP_BROWSER_OPEN or not ok:
                await br.ask("\nОкно оставлено открытым. Нажмите Enter, чтобы закрыть браузер и выйти… ")
            await sess.context.close()
        else:
            log("[i] Ваше окно Edge не закрываю.")
        return 0 if ok else 1


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Stealth Typer — человекоподобный ввод текста")
    ap.add_argument("--site", help="имя профиля сайта из config.py (SITES)")
    ap.add_argument("--text", help="файл с текстом (вместо TEXT_FILE)")
    ap.add_argument("--mode", choices=["launch", "attach"], help="новое окно Edge или уже открытое")
    ap.add_argument("--cursor", choices=["auto", "manual"], help="курсор ставит скрипт или вы")
    ap.add_argument("--wpm", help="диапазон скорости, например 50-80")
    ap.add_argument("--seed", type=int, help="число для повторяемого «почерка»")
    ap.add_argument("--dry-run", action="store_true", help="без браузера: проверка логики и оценка времени")
    ap.add_argument("--list", action="store_true", help="показать Monaco-редакторы на странице и выйти")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    global LOG_PATH
    args = parse_args(argv)
    ov = {"BROWSER_MODE": args.mode, "CURSOR_MODE": args.cursor, "RANDOM_SEED": args.seed}
    if args.wpm:
        try:
            lo, hi = args.wpm.replace(" ", "").split("-")
            ov["WPM_MIN"], ov["WPM_MAX"] = int(lo), int(hi)
        except ValueError:
            print("[!] --wpm нужно в формате 50-80")
            return 2
    try:
        cfg = settings.build(site=args.site, overrides=ov)
        if cfg.LOG_FILE:
            LOG_PATH = settings.resolve_path(cfg.LOG_FILE)
        if cfg.SITE_NAME:
            log(f"[i] Профиль сайта: {cfg.SITE_NAME}")
        if args.list:
            return asyncio.run(list_mode(cfg))
        text = load_text(cfg, args.text)
        if args.dry_run:
            return dry_run(cfg, text)
        return asyncio.run(run(cfg, text))
    except settings.ConfigError as e:
        log(f"[!] Ошибка настроек: {e}")
        return 2
    except KeyboardInterrupt:
        log("\n[i] Прервано (Ctrl+C).")
        return 130
    except ModuleNotFoundError as e:
        if "patchright" in str(e):
            log("[!] Не установлен patchright. Выполните:  pip install patchright")
            return 3
        raise
    except Exception as e:  # noqa: BLE001
        log(f"[!] Непредвиденная ошибка: {e}")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
