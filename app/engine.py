# -*- coding: utf-8 -*-
"""
Движок печати: раскладки клавиш, модель скорости (разброс + усталость),
опечатки, и синхронизация с редактором (удаление лишнего, что вставил сайт).

Ничего не знает про браузер: события клавиш уходят в «драйвер» (см. browser.CdpDriver),
а состояние редактора читается через «пробу» (probe). Поэтому всё тестируется без браузера.
"""

import asyncio
import math
import random
import re
import time
from dataclasses import dataclass

# ============================================================
# 1. Клавиши
# ============================================================

SHIFT_MOD = 8


@dataclass(frozen=True)
class Key:
    key: str
    code: str
    vk: int
    shift: bool = False
    text: str = ""
    unmod: str = ""


_EN_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
_RU_ROWS = ["йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю"]
_ROW_CODES = [
    ["KeyQ", "KeyW", "KeyE", "KeyR", "KeyT", "KeyY", "KeyU", "KeyI", "KeyO", "KeyP",
     "BracketLeft", "BracketRight"],
    ["KeyA", "KeyS", "KeyD", "KeyF", "KeyG", "KeyH", "KeyJ", "KeyK", "KeyL", "Semicolon", "Quote"],
    ["KeyZ", "KeyX", "KeyC", "KeyV", "KeyB", "KeyN", "KeyM", "Comma", "Period"],
]
_VK = {"BracketLeft": 219, "BracketRight": 221, "Semicolon": 186, "Quote": 222,
       "Comma": 188, "Period": 190, "Slash": 191, "Minus": 189, "Equal": 187,
       "Backslash": 220, "Backquote": 192, "Space": 32}

# символ -> (physical code, символ без Shift)
_SHIFTED = {
    "!": ("Digit1", "1"), "@": ("Digit2", "2"), "#": ("Digit3", "3"), "$": ("Digit4", "4"),
    "%": ("Digit5", "5"), "^": ("Digit6", "6"), "&": ("Digit7", "7"), "*": ("Digit8", "8"),
    "(": ("Digit9", "9"), ")": ("Digit0", "0"), "_": ("Minus", "-"), "+": ("Equal", "="),
    "{": ("BracketLeft", "["), "}": ("BracketRight", "]"), "|": ("Backslash", "\\"),
    ":": ("Semicolon", ";"), '"': ("Quote", "'"), "<": ("Comma", ","), ">": ("Period", "."),
    "?": ("Slash", "/"), "~": ("Backquote", "`"),
}
_PLAIN = {
    "`": "Backquote", "-": "Minus", "=": "Equal", "[": "BracketLeft", "]": "BracketRight",
    "\\": "Backslash", ";": "Semicolon", "'": "Quote", ",": "Comma", ".": "Period",
    "/": "Slash", " ": "Space",
}


def _vk(code: str) -> int:
    if code.startswith("Key"):
        return ord(code[3])
    if code.startswith("Digit"):
        return ord(code[5])
    return _VK[code]


def _build_keymap() -> dict:
    m = {}
    for rows in (_EN_ROWS, _RU_ROWS):
        for row, codes in zip(rows, _ROW_CODES):
            for ch, code in zip(row, codes):
                m[ch] = Key(ch, code, _vk(code), False, ch, ch)
                m[ch.upper()] = Key(ch.upper(), code, _vk(code), True, ch.upper(), ch)
    m["ё"] = Key("ё", "Backquote", 192, False, "ё", "ё")
    m["Ё"] = Key("Ё", "Backquote", 192, True, "Ё", "ё")
    for d in "0123456789":
        m[d] = Key(d, f"Digit{d}", ord(d), False, d, d)
    for ch, (code, base) in _SHIFTED.items():
        m[ch] = Key(ch, code, _vk(code), True, ch, base)
    for ch, code in _PLAIN.items():
        m[ch] = Key(ch, code, _vk(code), False, ch, ch)
    m["\n"] = Key("Enter", "Enter", 13, False, "\r", "\r")
    m["\t"] = Key("Tab", "Tab", 9, False, "\t", "\t")
    return m


KEYMAP = _build_keymap()
BACKSPACE = Key("Backspace", "Backspace", 8)
DELETE = Key("Delete", "Delete", 46)
END = Key("End", "End", 35)
HOME = Key("Home", "Home", 36)
RIGHT = Key("ArrowRight", "ArrowRight", 39)
DOWN = Key("ArrowDown", "ArrowDown", 40)
SHIFT_KEY = Key("Shift", "ShiftLeft", 16)


def key_for(ch: str) -> Key:
    """Клавиша для символа. Для символов вне раскладки (—, «», №, эмодзи...)
    отправляется событие «только текст», как это делают IME и сторонние раскладки."""
    k = KEYMAP.get(ch)
    return k if k else Key(ch, "", 0, False, ch, ch)


def neighbors(ch: str) -> list:
    """Соседние по раскладке клавиши (для правдоподобных опечаток)."""
    low = ch.lower()
    for rows in (_EN_ROWS, _RU_ROWS):
        for r, row in enumerate(rows):
            i = row.find(low)
            if i < 0:
                continue
            cand = []
            for di in (-1, 1):
                if 0 <= i + di < len(row):
                    cand.append(row[i + di])
            for dr in (-1, 1):
                rr = r + dr
                if 0 <= rr < len(rows):
                    for di in ((0, 1) if dr < 0 else (-1, 0)):
                        j = i + di
                        if 0 <= j < len(rows[rr]):
                            cand.append(rows[rr][j])
            return [c.upper() for c in cand] if ch.isupper() else cand
    return []


# ============================================================
# 2. Модель скорости: разброс + усталость
# ============================================================

class SpeedModel:
    """
    Текущая скорость (WPM) плавно гуляет между WPM_MIN и WPM_MAX.

    - x ∈ [0, 1] — положение внутри диапазона; тянется к «целевому уровню» (процесс
      Орнштейна–Уленбека) с случайным шумом (VARIABILITY).
    - Уровень у каждого запуска свой (кто-то печатает быстрее, кто-то медленнее).
    - Усталость растёт со временем и снижает целевой уровень (FATIGUE, FATIGUE_MINUTES);
      паузы-«отдыхи» её частично сбрасывают.
    - Иногда случаются «разгон» (поток) и «просадка» (отвлёкся) на несколько секунд.
    """

    TIME_CONST_S = 10.0

    def __init__(self, cfg, rng: random.Random):
        self.cfg, self.rng = cfg, rng
        self.base = rng.uniform(0.40, 0.70)
        self.x = min(1.0, max(0.0, self.base + rng.uniform(-0.1, 0.1)))
        self.fatigue = 0.0
        self.last_t = 0.0
        self.mood = 0.0
        self.mood_until = 0.0

    def update(self, t: float) -> None:
        dt = t - self.last_t
        if dt <= 0:
            return
        self.last_t = t
        c = self.cfg
        # усталость
        tau = c.FATIGUE_MINUTES * 60.0
        self.fatigue += (1.0 - self.fatigue) * (1.0 - math.exp(-dt / tau))
        # настроение: разгон/просадка
        if t >= self.mood_until:
            self.mood = 0.0
            if self.rng.random() < min(1.0, dt * 0.02 * c.VARIABILITY):
                self.mood = self.rng.choice((-0.25, 0.25))
                self.mood_until = t + self.rng.uniform(4.0, 12.0)
        target = self.base * (1.0 - c.FATIGUE * 0.8 * self.fatigue) + self.mood
        a = 1.0 - math.exp(-dt / self.TIME_CONST_S)
        sigma = c.VARIABILITY * 0.22 * math.sqrt(1.0 - math.exp(-2.0 * dt / self.TIME_CONST_S))
        self.x += a * (target - self.x) + sigma * self.rng.gauss(0.0, 1.0)
        self.x = min(1.0, max(0.0, self.x))

    def rest(self) -> None:
        """Человек отдохнул — усталость частично уходит."""
        self.fatigue *= 0.55

    def wpm(self) -> float:
        c = self.cfg
        return c.WPM_MIN + self.x * (c.WPM_MAX - c.WPM_MIN)


# ============================================================
# 3. Печатальщик
# ============================================================

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "param", "source", "track", "wbr"}
_OPEN_TAG_END = re.compile(r"<([A-Za-z][\w:-]*)(?:\s[^<>]*)?>$")
_CLOSE_TAG = re.compile(r"</([A-Za-z][\w:-]*)>")
_ONLY_CLOSERS = re.compile(r"(?:\s|</[\w:-]+>|[)\]}\"'`])*")
_LEAD_WS = re.compile(r"[ \t]*")
_PAIRS = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'", "`": "`"}
_CLOSER_CHARS = set(")]}\"'`")
_TYPO_SAFE_LOOKAHEAD = set(" .,;:-")
_SYNC_CHARS = set(">\n()[]{}\"'`")


def _opened_tag(recent: str):
    """Имя тега, если recent заканчивается открывающим тегом, который сайт может автозакрыть."""
    m = _OPEN_TAG_END.search(recent)
    if m and not recent.endswith("/>") and m.group(1).lower() not in _VOID:
        return m.group(1)
    return None


def _closer_at(text: str, i: int):
    """Закрывающий тег </x> или закрывающая скобка )]} в позиции i исходного текста."""
    m = _CLOSE_TAG.match(text, i)
    if m:
        return m.group(0)
    if i < len(text) and text[i] in ")]}":
        return text[i]
    return None


class _FlushingProbe:
    """Обёртка над редактором: перед каждым чтением его состояния дожидается отправленных нажатий."""

    def __init__(self, probe, flush):
        self._probe, self._flush = probe, flush

    def __getattr__(self, name):
        attr = getattr(self._probe, name)
        if name in ("state", "peek", "line_info", "read_text") and callable(attr):
            async def wrapper(*a, **kw):
                await self._flush()
                return await attr(*a, **kw)
            return wrapper
        return attr


class Typist:
    """
    Печатает текст «как человек, у которого редактор помогает»:

    * автозакрытые сайтом теги и скобки/кавычки редактора не удаляются — скрипт печатает внутри
      и затем перепрыгивает через закрывающий знак (End / стрелка / «проходная» печать);
    * после Enter принимает автоотступ редактора и подгоняет его под исходный текст
      (лишнее — Backspace, недостающее — допечатывает);
    * если Enter между тегами (или скобками) создал пустую строку, то вместо второго Enter
      выходит из неё вниз, к закрывающему знаку.
    """

    def __init__(self, driver, probe, cfg, rng=None, sleep=None, now=None, log=print):
        """
        driver: async key(type, key, code, vk, modifiers, text, unmod)
        probe:  async state() -> (offset, total) | None; по возможности ещё
                async peek(n) -> текст справа от курсора, async line_info() -> (до курсора, вся строка)
        """
        self.drv, self.cfg = driver, cfg
        self.probe = _FlushingProbe(probe, self._flush) if probe is not None else None
        self.rng = rng or random.Random(cfg.RANDOM_SEED)
        self._sleep_fn = sleep or asyncio.sleep
        self.now = now or time.monotonic
        self.log = log
        self.speed = SpeedModel(cfg, self.rng)
        self.eol_len = 1
        self.stack = []          # символы слева от курсора, за которые мы отвечаем (свои + автоотступ)
        self.cur_len = 0         # их длина в единицах редактора
        self.pend = ""           # то, что редактор вставил справа от курсора (теги, скобки, переносы)
        self.skipped = 0         # сколько знаков справа мы «перепрыгнули» (в единицах редактора)
        self.base_off = 0
        self.tail0 = 0
        self.in_indent = True
        self.t0 = 0.0
        self._since_sync = 0
        self._pending = []             # отправленные, но ещё не подтверждённые нажатия (турбо)
        self._due = None               # «расписание»: когда должен начаться следующий знак
        self._ac_seen = False          # сайт хоть раз вставлял закрывающий тег
        self._ac_misses = 0            # сколько раз подряд ждали тег, а его не было
        self.stats = dict(typos=0, junk_removed=0, indent_fixed=0, anomalies=0, skipped_tags=0,
                          chars=0, wpm_sum=0.0, wpm_min=1e9, wpm_max=0.0, duration=0.0)
        self._last_progress = 0.0

    # ---------- режимы ----------

    @property
    def skip(self) -> bool:
        return (getattr(self.cfg, "AUTOCLOSE_MODE", "skip") == "skip"
                and self.probe is not None and hasattr(self.probe, "peek"))

    @property
    def natural(self) -> bool:
        return (getattr(self.cfg, "MONACO_MODE", "natural") == "natural"
                and self.probe is not None and hasattr(self.probe, "line_info"))

    # ---------- время ----------

    def _scale(self) -> float:
        """Фиксированные «человеческие» задержки (пауза на Shift, реакция на опечатку, взгляд вниз...)
        ужимаются при высокой скорости: на 70 WPM и ниже они как есть, на 500 WPM — примерно в 7 раз короче."""
        return min(1.0, 70.0 / max(1.0, self.speed.wpm()))

    @property
    def turbo(self) -> bool:
        """Быстрый режим: на высокой скорости нажатия уходят в браузер «пачкой», без ожидания ответа на каждое."""
        lim = getattr(self.cfg, "TURBO_FROM_WPM", 150)
        return bool(lim) and self.speed.wpm() >= lim

    async def _wait(self, s: float) -> None:
        """Одна реальная задержка. Короткие (< 20 мс) на Windows asyncio.sleep округляет до ~15 мс,
        поэтому их отсчитываем «вращением» цикла — точнее, ценой небольшой нагрузки на процессор."""
        if s <= 0:
            return
        if self._sleep_fn is asyncio.sleep and s < 0.02:
            end = time.perf_counter() + s
            while time.perf_counter() < end:
                await asyncio.sleep(0)
        else:
            await self._sleep_fn(s)

    async def _sleep(self, s: float) -> None:
        """Человеческая пауза, масштабируется под скорость и сдвигает расписание."""
        s *= self._scale()
        if s > 0:
            if self._due is not None:
                self._due += s
            await self._wait(s)

    async def _raw(self, s: float, shift_due: bool = True) -> None:
        """Точная задержка (удержание клавиши, ожидание сайта). Сдвигает расписание, если shift_due."""
        if s > 0:
            if shift_due and self._due is not None:
                self._due += s
            await self._wait(s)

    async def _until_due(self, interval: float) -> None:
        """Ждать до момента «начала следующего знака». Время, потраченное на сетевые вызовы, не накапливается."""
        now = self.now()
        if self._due is None or self._due < now - 0.25:
            self._due = now                      # сильно отстали (или пауза) — расписание с чистого листа
        self._due += interval
        await self._wait(self._due - now)

    async def _flush(self) -> None:
        if self._pending:
            tasks, self._pending = self._pending, []
            await asyncio.gather(*tasks)

    def _noise(self, sigma: float) -> float:
        return math.exp(self.rng.gauss(-sigma * sigma / 2.0, sigma))

    # ---------- низкоуровневые нажатия ----------

    async def _key(self, typ, k: Key, mods=0, text=""):
        coro = self.drv.key(typ, k.key, k.code, k.vk, mods, text, k.unmod)
        if self.turbo:
            self._pending.append(asyncio.ensure_future(coro))   # порядок сохраняется, ответ не ждём
            if len(self._pending) >= 64:
                await self._flush()
        else:
            await self._flush()
            await coro

    async def _press(self, k: Key, hold: float) -> None:
        mods = SHIFT_MOD if k.shift else 0
        if k.shift:
            await self._key("rawKeyDown", SHIFT_KEY, SHIFT_MOD)
            if not self.turbo:
                await self._sleep(self.rng.uniform(0.02, 0.07))
        typ = "keyDown" if k.text else "rawKeyDown"
        await self._key(typ, k, mods, k.text)
        if not self.turbo:
            await self._raw(hold, shift_due=False)
        await self._key("keyUp", k, mods)
        if k.shift:
            if not self.turbo:
                await self._sleep(self.rng.uniform(0.01, 0.05))
            await self._key("keyUp", SHIFT_KEY, 0)

    def _hold(self, interval: float) -> float:
        lo, hi = self.cfg.KEY_HOLD_MS
        h = self.rng.uniform(lo, hi) / 1000.0
        return min(h, max(0.012, interval * 0.85))

    async def _tap(self, k: Key) -> None:
        """Служебная клавиша (стрелка, Home, End, Backspace...) в спокойном темпе."""
        await self._press(k, self.rng.uniform(0.03, 0.08) * self._scale())
        await self._sleep(self.rng.uniform(0.03, 0.09))

    def _units(self, ch: str) -> int:
        return self.eol_len if ch == "\n" else 1

    def _u(self, s: str) -> int:
        return len(s) + s.count("\n") * (self.eol_len - 1)

    def _anomaly(self, msg: str) -> None:
        self.stats["anomalies"] += 1
        if self.stats["anomalies"] <= 3:
            self.log("[!] " + msg)

    # ---------- синхронизация с редактором ----------

    async def _state(self):
        await self._flush()                 # сначала дожидаемся всех отправленных нажатий
        if self.probe is None:
            return None
        return await self.probe.state()

    async def _delete_until(self, target_tail: int, stat: str) -> None:
        guard = 0
        while guard < 400:
            off, total = await self._state()
            if total - off <= target_tail:
                break
            await self._press(DELETE, 0.02)
            await self._sleep(0.01)
            guard += 1
            self.stats[stat] += 1

    async def _sync(self, wait_growth: bool = False, after_newline: bool = False,
                    opened=None, pair=None, closer: bool = False) -> None:
        st = await self._state()
        if st is None:
            return
        off, total = st
        tail = total - off
        exp_tail = self.tail0 + self._u(self.pend)

        # 0) мы напечатали закрывающий знак: редактор либо «проглотил» (overtype) уже стоящий, либо вставил свой
        if closer and self.pend:
            if tail == exp_tail - 1:
                self.pend = self.pend[1:]
            elif tail == exp_tail:
                await self._press(DELETE, 0.02)            # остался лишний автозакрытый знак
                await self._sleep(0.01)
                self.pend = self.pend[1:]
                self.stats["junk_removed"] += 1
            off, total = await self._state()
            tail = total - off
            exp_tail = self.tail0 + self._u(self.pend)

        if (wait_growth and self.cfg.AUTOCLOSE_WAIT_MS > 0 and tail <= exp_tail
                and (self._ac_seen or self._ac_misses < 3)):
            waited = 0
            while waited < self.cfg.AUTOCLOSE_WAIT_MS and tail <= exp_tail:
                await self._raw(0.025)
                waited += 25
                off, total = await self._state()
                tail = total - off
            if tail > exp_tail:
                self._ac_seen, self._ac_misses = True, 0
            else:
                self._ac_misses += 1       # у сайта нет автозакрытия тегов — после 3 раз ждать перестаём

        # 1) справа от курсора появилось лишнее
        if tail > exp_tail:
            g = tail - exp_tail
            if self.skip:
                P = None
                if opened:
                    P = await self.probe.peek(g)
                    ok = P == f"</{opened}>"
                elif pair:
                    ok = g == 1
                    if ok:
                        P = await self.probe.peek(g)
                        ok = P == _PAIRS[pair]
                elif after_newline:
                    P = await self.probe.peek(g)
                    ok = P.strip() == ""
                else:
                    ok = False
                if ok:
                    self.pend = P + self.pend
                    g = 0
            if g:
                await self._delete_until(exp_tail, "junk_removed")
            off, total = await self._state()
            tail = total - off

        # 2) лишнее слева сразу после Enter — автоотступ редактора
        exp = self.base_off + self.cur_len + self.skipped
        if after_newline and off > exp:
            if self.natural:
                extra = off - exp
                pre, _ = await self.probe.line_info()
                if len(pre) == extra and pre.strip(" \t") == "":
                    for c in pre:                           # принимаем: теперь это «наши» символы слева
                        self.stack.append(c)
                        self.cur_len += 1
                    exp = self.base_off + self.cur_len + self.skipped
            else:
                guard = 0
                while off > exp and guard < 100:
                    await self._press(BACKSPACE, 0.02)
                    await self._sleep(0.01)
                    guard += 1
                    self.stats["indent_fixed"] += 1
                    off, total = await self._state()
                tail = total - off
        exp_tail = self.tail0 + self._u(self.pend)
        if off != exp or tail != exp_tail:
            self._anomaly(f"Редактор изменил текст неожиданно "
                          f"(курсор {off}, ожидался {exp}; справа {tail}, ожидалось {exp_tail})")

    # ---------- прыжки через автозакрытое ----------

    async def _skip_closer(self, text: str, i: int) -> int:
        """Дальше в тексте </tag>, а редактор уже вставил его справа — перепрыгнуть вместо печати.
        Возвращает, сколько знаков исходного текста обработано (0 — не получилось)."""
        m = _CLOSE_TAG.match(text, i)
        if not m or not self.pend:
            return 0
        target, p = m.group(0), self.pend
        idx = p.find(target)
        if idx < 0 or not _ONLY_CLOSERS.fullmatch(p[:idx]):
            return 0
        full = await self.probe.peek(self._u(p) + 1)
        if full[:len(p)] != p:
            self._anomaly("Справа от курсора не то, что ожидалось — печатаю тег руками.")
            return 0
        rest = p[idx:]
        after_ch = full[len(p):len(p) + 1]
        if idx:     # убрать то, что стоит перед нужным тегом (незакрытые в исходнике теги/скобки, пустая строка)
            await self._sleep(self.rng.uniform(0.05, 0.15))
            await self._delete_until(self.tail0 + self._u(rest), "junk_removed")
        await self._sleep(self.rng.uniform(0.04, 0.16))
        want = self.base_off + self.cur_len + self.skipped + len(target)
        if rest == target and after_ch in ("", "\n"):
            await self._press(END, self._hold(0.1))
        else:
            for _ in range(len(target)):
                await self._press(RIGHT, self.rng.uniform(0.02, 0.05))
                await self._sleep(self.rng.uniform(0.02, 0.07))
        off, total = await self._state()
        guard = 0
        while off < want and guard < 40:                  # End мог остановиться раньше (перенос строки)
            await self._press(RIGHT, 0.03)
            guard += 1
            off, total = await self._state()
        self.skipped += len(target)
        self.pend = rest[len(target):]
        if off != want:
            self._anomaly("После прыжка через тег курсор не там, где ожидалось.")
        self.stats["skipped_tags"] += 1
        return len(target)

    async def _fix_line_indent(self, ws: str) -> None:
        """Курсор где-то на строке с закрывающим знаком: привести её отступ к ws и встать в начало текста строки."""
        pre, line = await self.probe.line_info()
        cur_ws = line[:len(line) - len(line.lstrip(" \t"))]
        guard = 0
        while pre != cur_ws and guard < 3:               # Home: в начало текста строки (после отступа)
            await self._tap(HOME)
            pre, line = await self.probe.line_info()
            cur_ws = line[:len(line) - len(line.lstrip(" \t"))]
            guard += 1
        if pre != cur_ws:
            self._anomaly("Не удалось встать в начало строки для выравнивания отступа.")
            return
        guard = 0
        while not ws.startswith(pre) and guard < 30:     # редактор поставил не такой отступ — стираем
            await self._tap(BACKSPACE)
            pre, _ = await self.probe.line_info()
            self.stats["indent_fixed"] += 1
            guard += 1
        for c in ws[len(pre):]:                           # недостающее допечатываем
            await self._press(key_for(c), self.rng.uniform(0.02, 0.05))
            await self._sleep(self.rng.uniform(0.02, 0.06))
            self.stats["indent_fixed"] += 1

    async def _jump_over_newline(self, tgt: str, ws: str) -> bool:
        """Редактор уже сделал нужный перенос и поставил закрывающий знак ниже: Enter не нажимаем,
        а уходим вниз к закрывающему знаку и ставим ему нужный отступ."""
        P = self.pend
        idx = P.find(tgt)
        if idx < 0:
            return False
        pre = P[:idx]
        if "\n" not in pre or pre.strip(" \t\n") != "":
            return False
        full = await self.probe.peek(self._u(P) + 1)
        if full[:len(P)] != P:
            self._anomaly("Справа от курсора не то, что ожидалось — нажимаю Enter как обычно.")
            return False
        rest = P[idx + len(tgt):]
        after_ch = full[len(P):len(P) + 1]
        line_end = (rest == "" and after_ch in ("", "\n")) or rest.startswith("\n")
        await self._sleep(self.rng.uniform(0.15, 0.45))             # человек смотрит вниз
        for _ in range(pre.count("\n")):
            await self._tap(DOWN)
        await self._fix_line_indent(ws)
        if line_end:
            await self._tap(END)
        else:
            for _ in range(len(tgt)):
                await self._press(RIGHT, self.rng.uniform(0.02, 0.05))
                await self._sleep(self.rng.uniform(0.02, 0.07))
        self.pend = rest
        st = await self._state()
        self.skipped = st[0] - self.base_off - self.cur_len
        self.stats["skipped_tags"] += 1
        await self._sync()
        return True

    async def _reconcile_indent(self, ws: str) -> None:
        """Сразу после Enter: отступ, который поставил редактор, должен стать ws."""
        guard = 0
        while guard < 30:
            pre, _ = await self.probe.line_info()
            if pre.strip(" \t") != "":
                self._anomaly("После Enter в начале строки не только пробелы — отступ не трогаю.")
                return
            if ws.startswith(pre):
                break
            await self._tap(BACKSPACE)                    # лишний/другой отступ — стираем (Backspace)
            newpre, _ = await self.probe.line_info()
            for _ in range(max(0, len(pre) - len(newpre))):
                ch = self.stack.pop()
                self.cur_len -= self._units(ch)
            self.stats["indent_fixed"] += 1
            guard += 1
        for c in ws[len(pre):]:                           # недостающий отступ допечатываем
            await self._type_char(c, "\n")

    async def _enter(self, text: str, i: int) -> int:
        """Обработка символа «\\n» на позиции i. Возвращает, сколько знаков исходного текста потрачено."""
        ws = _LEAD_WS.match(text, i + 1).group()
        rest_i = i + 1 + len(ws)
        prev = self.stack[-1] if self.stack else ""
        if self.skip and self.pend:
            tgt = _closer_at(text, rest_i)
            if tgt and await self._jump_over_newline(tgt, ws):
                self.in_indent = False
                return 1 + len(ws) + len(tgt)
        await self._type_char("\n", prev)
        await self._reconcile_indent(ws)
        return 1 + len(ws)

    # ---------- паузы ----------

    async def _pause_after(self, ch: str, prev: str) -> None:
        r, p = self.rng, self.cfg.PAUSES
        if p <= 0:
            return
        if ch == " " and prev not in (" ", "\n", "\t", ""):
            if r.random() < 0.12 * p:
                await self._sleep(r.uniform(0.10, 0.35))
        elif ch in ".,;:!?":
            if r.random() < 0.25 * p:
                await self._sleep(r.uniform(0.10, 0.40))
        elif ch == "\n":
            if r.random() < 0.5:
                await self._sleep(r.uniform(0.2, 0.9) * min(p, 2.0))
            if r.random() < 0.06 * p:       # задумался
                await self._sleep(r.uniform(1.0, 3.5))
            if r.random() < 0.015 * p:      # отдохнул / отвлёкся
                await self._sleep(r.uniform(3.0, 8.0))
                self.speed.rest()

    # ---------- один символ ----------

    async def _type_char(self, ch: str, prev: str) -> None:
        self.speed.update(self.now() - self.t0)
        wpm = self.speed.wpm()
        base = 12.0 / wpm
        if ch == " ":
            f = 0.9
        elif ch == "\n":
            f = 1.5
        elif ch.isdigit():
            f = 1.25
        elif ch.isalpha():
            f = 1.3 if ch.isupper() else 1.0
        else:
            f = 1.3
        if self.in_indent and ch in " \t" and self.cfg.INDENT_FAST:
            f *= 0.3
        interval = base * f * self._noise(0.30)
        hold = self._hold(interval)

        # ждёт ли редактор от этого знака «проходной» печати (закрывающая скобка/кавычка поверх автозакрытой)
        closer = bool(self.pend) and ch in _CLOSER_CHARS and ch == self.pend[0] and self.skip

        await self._press(key_for(ch), hold)
        self.stack.append(ch)
        self.cur_len += self._units(ch)

        opened = _opened_tag("".join(self.stack[-200:])) if ch == ">" else None
        pair = ch if (ch in _PAIRS and not closer) else None
        # состояние редактора читаем там, где он может что-то вставить сам (скобки, кавычки, «>», Enter),
        # и раз в 20 знаков для страховки; на обычных буквах лишних запросов к браузеру нет
        self._since_sync += 1
        if ch in _SYNC_CHARS or closer or self._since_sync >= 20:
            self._since_sync = 0
            await self._sync(wait_growth=bool(opened), after_newline=(ch == "\n"),
                             opened=opened, pair=pair, closer=closer)
        await self._until_due(interval)

        if ch == "\n":
            self.in_indent = True
        elif ch not in " \t":
            self.in_indent = False

        st = self.stats
        st["chars"] += 1
        st["wpm_sum"] += wpm
        st["wpm_min"] = min(st["wpm_min"], wpm)
        st["wpm_max"] = max(st["wpm_max"], wpm)

    async def _backspace(self, n: int) -> None:
        for _ in range(n):
            ch = self.stack.pop()
            self.cur_len -= self._units(ch)
            hold = self.rng.uniform(0.03, 0.07)
            await self._press(BACKSPACE, hold * self._scale())
            await self._sleep(self.rng.uniform(0.04, 0.10))
        await self._sync()
        line = "".join(self.stack).rsplit("\n", 1)[-1]
        self.in_indent = line.strip(" \t") == ""

    # ---------- опечатки ----------

    def _plan_typo(self, text: str, i: int):
        """Вернуть (wrong, consumed, lookahead) или None."""
        c, r = self.cfg, self.rng
        ch = text[i]
        if c.ERROR_RATE <= 0 or not ch.isalpha() or self.in_indent:
            return None
        if r.random() >= c.ERROR_RATE:
            return None
        nxt = text[i + 1] if i + 1 < len(text) else ""
        roll = r.random()
        if roll < c.SWAP_SHARE and nxt.isalpha() and nxt != ch:
            wrong, consumed = nxt + ch, 2
        elif roll < c.SWAP_SHARE + c.DOUBLE_SHARE:
            wrong, consumed = ch + ch, 1
        else:
            nb = neighbors(ch)
            if not nb:
                return None
            wrong, consumed = r.choice(nb), 1
        look = 0
        want = r.randint(0, max(0, c.TYPO_NOTICE_MAX))
        while look < want and i + consumed + look < len(text):
            nc = text[i + consumed + look]
            if not (nc.isalnum() or nc in _TYPO_SAFE_LOOKAHEAD):
                break
            look += 1
        return wrong, consumed, look

    # ---------- основной цикл ----------

    async def type_text(self, text: str) -> dict:
        st = await self._state()
        if st is not None:
            self.base_off, total = st[0], st[1]
            self.tail0 = total - st[0]
        self.t0 = self.now()
        self._due = self.t0
        self._last_progress = self.t0
        self.in_indent = True
        i, n = 0, len(text)
        prev = ""
        while i < n:
            ch = text[i]
            if ch == "\n" and self.natural:
                k = await self._enter(text, i)
                i += k
                prev = "\n"
                await self._pause_after("\n", prev)
                self._progress(i, n)
                continue
            if ch == "<" and self.pend and text.startswith("</", i):
                n_skip = await self._skip_closer(text, i)
                if n_skip:
                    i += n_skip
                    prev = ">"
                    self.in_indent = False
                    await self._sleep(self.rng.uniform(0.03, 0.12))
                    self._progress(i, n)
                    continue
            plan = self._plan_typo(text, i)
            if plan:
                wrong, consumed, look = plan
                self.stats["typos"] += 1
                for w in wrong:
                    await self._type_char(w, prev)
                    prev = w
                for k in range(look):
                    ch2 = text[i + consumed + k]
                    await self._type_char(ch2, prev)
                    prev = ch2
                await self._sleep(self.rng.uniform(0.15, 0.55))      # заметил ошибку
                await self._backspace(len(wrong) + look)
                await self._sleep(self.rng.uniform(0.08, 0.25))
                prev = self.stack[-1] if self.stack else ""
            await self._type_char(ch, prev)
            await self._pause_after(ch, prev)
            prev = ch
            i += 1
            self._progress(i, n)
        # финальный «добор»: сайт мог вставить закрывающий тег с задержкой
        if self.cfg.AUTOCLOSE_WAIT_MS > 0:
            await self._raw(min(0.4, self.cfg.AUTOCLOSE_WAIT_MS / 1000.0))
            await self._sync()
        if self.pend:                                   # теги/скобки, которые исходный текст не закрывал
            await self._delete_until(self.tail0, "junk_removed")
            self.pend = ""
        await self._flush()
        self.stats["duration"] = self.now() - self.t0
        return self.stats

    def _progress(self, i: int, n: int) -> None:
        t = self.now()
        if t - self._last_progress >= 15.0 and i < n:
            self._last_progress = t
            el = t - self.t0
            left = el / i * (n - i) if i else 0
            self.log(f"[i] {100 * i // n}% | ≈{self.speed.wpm():.0f} WPM | "
                     f"осталось ≈{int(left // 60)}:{int(left % 60):02d}")
