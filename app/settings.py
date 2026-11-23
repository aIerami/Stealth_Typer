# -*- coding: utf-8 -*-
"""Загрузка и проверка настроек: общие + профиль сайта + параметры командной строки."""

import sys
from pathlib import Path
from types import SimpleNamespace

# Структура проекта:  <корень>/app (код), <корень>/config (config.py), <корень>/texts, <корень>/data
ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT_DIR          # старое имя, оставлено для совместимости
sys.path.insert(0, str(ROOT_DIR / "config"))

import config as _cfg  # noqa: E402

_SKIP = {"SITES", "ACTIVE_SITE"}


class ConfigError(Exception):
    pass


def defaults() -> dict:
    return {n: getattr(_cfg, n) for n in dir(_cfg) if n.isupper() and n not in _SKIP}


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT_DIR / path


def _check(d: dict) -> None:
    def bad(msg):
        raise ConfigError(msg)

    if d["BROWSER_MODE"] not in ("launch", "attach"):
        bad('BROWSER_MODE должен быть "launch" или "attach"')
    if d["MONACO_MODE"] not in ("natural", "literal"):
        bad('MONACO_MODE должен быть "natural" или "literal"')
    if d["AUTOCLOSE_MODE"] not in ("skip", "delete"):
        bad('AUTOCLOSE_MODE должен быть "skip" или "delete"')
    if d["CURSOR_MODE"] not in ("auto", "manual"):
        bad('CURSOR_MODE должен быть "auto" или "manual"')
    if not (0 < d["WPM_MIN"] <= d["WPM_MAX"]):
        bad("Нужно 0 < WPM_MIN <= WPM_MAX")
    for name in ("VARIABILITY", "FATIGUE"):
        if not (0 <= d[name] <= 1):
            bad(f"{name} должен быть от 0 до 1")
    for name in ("ERROR_RATE", "SWAP_SHARE", "DOUBLE_SHARE"):
        if not (0 <= d[name] <= 1):
            bad(f"{name} должен быть от 0 до 1")
    if d["SWAP_SHARE"] + d["DOUBLE_SHARE"] > 1:
        bad("SWAP_SHARE + DOUBLE_SHARE не должны быть больше 1")
    if d["PAUSES"] < 0 or d["FATIGUE_MINUTES"] <= 0:
        bad("PAUSES >= 0 и FATIGUE_MINUTES > 0")
    lo, hi = d["KEY_HOLD_MS"]
    if not (0 < lo <= hi):
        bad("KEY_HOLD_MS = (от, до), 0 < от <= до")
    cl = d["CURSOR_LINE"]
    if not (cl in ("end", "start") or (isinstance(cl, int) and cl >= 1)):
        bad('CURSOR_LINE: "end", "start" или номер строки (1, 2, ...)')
    cc = d["CURSOR_COLUMN"]
    if not (cc == "end" or (isinstance(cc, int) and cc >= 1)):
        bad('CURSOR_COLUMN: "end" или номер колонки (1, 2, ...)')
    if d["FIELD_INDEX"] < 0:
        bad("FIELD_INDEX не может быть отрицательным")


def mojibake_candidates(text: str):
    """Варианты «починки» строки, испорченной Windows при запуске из .bat / os.system:
    UTF-8 или cp1251, прочитанные как cp866/cp437/cp1251/cp1252. Первым идёт исходный вариант."""
    seen, out = set(), []

    def add(v):
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    add(text)
    for wrong in ("cp866", "cp1251", "cp437", "cp850", "cp1252", "latin-1"):
        for right in ("utf-8", "cp1251"):
            if wrong == right:
                continue
            try:
                add(text.encode(wrong).decode(right))
            except (UnicodeError, LookupError):
                pass
    return out


def resolve_site_name(name: str, known) -> str:
    """Найти профиль по имени. Терпимо к регистру и к «кракозябрам» из-за кодировки Windows."""
    known = list(known)
    if name in known:
        return name
    for cand in mojibake_candidates(name):
        for k in known:
            if cand == k or cand.casefold() == k.casefold():
                return k
    return name


def build(site=None, overrides=None) -> SimpleNamespace:
    """Собрать настройки. site=None → ACTIVE_SITE из config.py."""
    d = defaults()
    name = site if site is not None else _cfg.ACTIVE_SITE
    if name:
        name = resolve_site_name(name, _cfg.SITES)
        if name not in _cfg.SITES:
            known = ", ".join(_cfg.SITES) or "(пусто)"
            hint = ""
            if not name.isascii():
                hint = (" Похоже, кириллица в имени испортилась при запуске (так бывает в .bat-файлах и os.system). "
                        "Назовите профиль латиницей, например my_site, и запускайте с ним.")
            raise ConfigError(f"Профиль сайта «{name}» не найден. Есть: {known}.{hint}")
        for k, v in _cfg.SITES[name].items():
            if k not in d:
                raise ConfigError(f"В профиле «{name}» неизвестная настройка «{k}» (опечатка?)")
            d[k] = v
    for k, v in (overrides or {}).items():
        if v is not None:
            d[k] = v
    _check(d)
    ns = SimpleNamespace(**d)
    ns.SITE_NAME = name or ""
    return ns
