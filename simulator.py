# -*- coding: utf-8 -*-
"""
Симулятор Monaco-подобного редактора и виртуальные часы.
Нужен для тестов и для режима --dry-run (проверить логику и оценить время без браузера).
"""

import re

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
PAIRS = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'", "`": "`"}
OPEN_TAG_END = re.compile(r"<([A-Za-z][\w:-]*)(?:\s[^<>]*)?>$")
TAB = 4


class VirtualClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class FakeEditor:
    """
    Упрощённая модель Monaco. Умеет то, что «помогает» (и мешает) при вводе:
      autoclose_tags   — после «>» вставляет </tag> справа от курсора (можно с задержкой);
      autoclose_pairs  — автозакрытие скобок и кавычек + overtype (печать закрывающего знака
                         «проходит» через уже стоящий автоматический);
      indent           — None / "copy" (копирует отступ) / "smart" (+1 уровень после открывающего
                         тега или скобки); autoindent=True == "copy";
      outdent          — Enter между <tag> и </tag> (или {}, (), []) делает пустую строку с отступом,
                         а закрывающий тег уходит на следующую строку;
      tab_stops        — Backspace в начале строки стирает до предыдущей табуляции (как useTabStops);
    Одновременно является драйвером (key) и пробой (state/peek/line_info).
    """

    def __init__(self, text="", cursor=None, clock=None, eol="\n",
                 autoclose_tags=True, autoclose_delay=0.0,
                 autoclose_pairs=False, autoindent=False, indent=None,
                 outdent=False, tab_stops=True):
        self.text = text
        self.pos = len(text) if cursor is None else cursor
        self.clock = clock or VirtualClock()
        self.eol = eol
        self.autoclose_tags = autoclose_tags
        self.autoclose_delay = autoclose_delay
        self.autoclose_pairs = autoclose_pairs
        self.indent = indent or ("copy" if autoindent else None)
        self.outdent = outdent or self.indent == "smart"
        self.tab_stops = tab_stops
        self.pending = []        # (due, pos, string)
        self.events = []
        self._auto = {}          # сколько автозакрытых знаков каждого вида ещё стоит справа

    # ---- вспомогательное ----
    def _eolc(self, s):
        return s.replace("\n", self.eol)

    def _flush(self):
        due = [p for p in self.pending if p[0] <= self.clock.t]
        self.pending = [p for p in self.pending if p[0] > self.clock.t]
        for _, pos, s in due:
            self.text = self.text[:pos] + s + self.text[pos:]
            if pos < self.pos:
                self.pos += len(s)

    def _insert(self, s):
        self.text = self.text[:self.pos] + s + self.text[self.pos:]
        self.pending = [(d, p + len(s) if p > self.pos else p, st) for d, p, st in self.pending]
        self.pos += len(s)

    def _line_start(self):
        return self.text.rfind("\n", 0, self.pos) + 1

    def _opens(self, before):
        b = before.rstrip(" \t")
        m = OPEN_TAG_END.search(b)
        if m and m.group(1).lower() not in VOID and not b.endswith("/>"):
            return True
        return b[-1:] in ("{", "(", "[")

    def _enter(self):
        ls = self._line_start()
        line = self.text[ls:self.pos]
        ind = line[:len(line) - len(line.lstrip(" \t"))]
        before, after = self.text[:self.pos], self.text[self.pos:]
        opens = self._opens(before)
        if self.indent is None:
            newind = ""
        elif self.indent == "smart":
            newind = ind + (" " * TAB if opens else "")
        else:
            newind = ind
        between = self.outdent and opens and (after.startswith("</") or after[:1] in (")", "]", "}"))
        if between:
            self._insert("\n" + ind + " " * TAB)
            self.text = self.text[:self.pos] + "\n" + ind + self.text[self.pos:]
        else:
            self._insert("\n" + newind)

    # ---- драйвер ----
    async def key(self, typ, key, code, vk, modifiers, text, unmod=""):
        self._flush()
        self.events.append((typ, key, modifiers))
        if key == "Shift":
            return
        if typ == "keyDown" and text:
            if text == "\r":
                self._enter()
            elif text == "\t":
                self._insert("\t")
            else:
                nxt = self.text[self.pos:self.pos + 1]
                if self.autoclose_pairs and text in ")]}\"'`" and nxt == text and self._auto.get(text, 0) > 0:
                    self.pos += 1                                  # overtype
                    self._auto[text] -= 1
                    return
                prev = self.text[self.pos - 1:self.pos]
                self._insert(text)
                if self.autoclose_pairs and text in PAIRS:
                    nxt = self.text[self.pos:self.pos + 1]
                    word_before = prev.isalnum() and text in "\"'`"
                    if (not nxt or nxt in " \n)]}") and not word_before:
                        self.text = self.text[:self.pos] + PAIRS[text] + self.text[self.pos:]
                        self._auto[PAIRS[text]] = self._auto.get(PAIRS[text], 0) + 1
                if self.autoclose_tags and text == ">":
                    before = self.text[:self.pos]
                    m = OPEN_TAG_END.search(before)
                    if m and m.group(1).lower() not in VOID and not before.endswith("/>"):
                        junk = f"</{m.group(1)}>"
                        if self.autoclose_delay <= 0:
                            self.text = self.text[:self.pos] + junk + self.text[self.pos:]
                        else:
                            self.pending.append((self.clock.t + self.autoclose_delay, self.pos, junk))
        elif typ == "rawKeyDown":
            if key == "Backspace" and self.pos > 0:
                ls = self._line_start()
                pre = self.text[ls:self.pos]
                n = 1
                if self.tab_stops and pre and pre.strip(" ") == "" and len(pre) > 0:
                    n = len(pre) % TAB or TAB
                    n = min(n, len(pre))
                self.text = self.text[:self.pos - n] + self.text[self.pos:]
                self.pos -= n
            elif key == "Delete" and self.pos < len(self.text):
                self.text = self.text[:self.pos] + self.text[self.pos + 1:]
            elif key == "ArrowRight" and self.pos < len(self.text):
                self.pos += 1
            elif key == "End":
                nl = self.text.find("\n", self.pos)
                self.pos = len(self.text) if nl < 0 else nl
            elif key == "Home":
                ls = self._line_start()
                line_end = self.text.find("\n", ls)
                line = self.text[ls:len(self.text) if line_end < 0 else line_end]
                first = len(line) - len(line.lstrip(" \t"))
                self.pos = ls if self.pos - ls == first else ls + first
            elif key == "ArrowDown":
                ls = self._line_start()
                col = self.pos - ls
                nl = self.text.find("\n", self.pos)
                if nl >= 0:
                    nxt_end = self.text.find("\n", nl + 1)
                    nxt_len = (len(self.text) if nxt_end < 0 else nxt_end) - (nl + 1)
                    self.pos = nl + 1 + min(col, nxt_len)

    # ---- проба ----
    async def state(self):
        self._flush()
        off = len(self._eolc(self.text[:self.pos]))
        total = len(self._eolc(self.text))
        return off, total

    async def peek(self, n):
        self._flush()
        s = self._eolc(self.text[self.pos:])[:n]
        if s.endswith("\r"):
            s = s[:-1]
        return s.replace("\r\n", "\n")

    async def line_info(self):
        self._flush()
        ls = self._line_start()
        le = self.text.find("\n", ls)
        return self.text[ls:self.pos], self.text[ls:len(self.text) if le < 0 else le]
