# -*- coding: utf-8 -*-
"""
Браузерный слой: запуск нового окна Edge или подключение к открытому, поиск поля/редактора,
установка курсора, чтение состояния и итогового текста.

ВАЖНО про Patchright: page.evaluate() по умолчанию выполняется в ИЗОЛИРОВАННОМ контексте,
где глобальной переменной `monaco` нет. Всё, что обращается к window.monaco,
выполняется с isolated_context=False. Остальное (DOM) — в изолированном.
"""

import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request

import settings

# ============================================================
# 1. Драйвер клавиш (CDP)
# ============================================================


class CdpDriver:
    def __init__(self, cdp, debug=False):
        self.cdp, self.debug = cdp, debug

    async def key(self, typ, key, code, vk, modifiers, text="", unmod=""):
        params = {
            "type": typ, "key": key, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
            "modifiers": modifiers,
        }
        if text and typ == "keyDown":
            params["text"] = text
            params["unmodifiedText"] = unmod or text
        if self.debug:
            print(f"  CDP {typ}: key={key!r} text={text!r} mod={modifiers}")
        await self.cdp.send("Input.dispatchKeyEvent", params)


# ============================================================
# 2. Monaco (выполняется в основном контексте страницы)
# ============================================================

JS_LOCATE = """
(a) => {
  if (typeof monaco !== 'object' || !monaco || !monaco.editor) return {error: 'no-monaco'};
  const eds = monaco.editor.getEditors();
  const vis = e => { const n = e.getDomNode();
      return !!(n && n.isConnected && (n.offsetWidth || n.offsetHeight || n.getClientRects().length)); };
  const order = (x, y) => (x.getDomNode().compareDocumentPosition(y.getDomNode()) & 4) ? -1 : 1;
  let ed = null;
  if (a.mode === 'focused') {
    const act = document.activeElement;
    ed = eds.find(e => { const n = e.getDomNode(); return n && act && n.contains(act); })
      || eds.find(e => e.hasTextFocus && e.hasTextFocus());
  } else if (a.mode === 'selector') {
    const el = document.querySelectorAll(a.selector)[a.index];
    if (!el) return {error: 'no-element'};
    ed = eds.find(e => { const n = e.getDomNode(); return n && (n === el || n.contains(el) || el.contains(n)); });
  } else {
    ed = eds.filter(vis).sort(order)[a.index];
  }
  if (!ed) return {error: 'no-editor', count: eds.length};
  Object.defineProperty(window, Symbol.for(a.key), {value: ed, configurable: true, enumerable: false, writable: true});
  return {ok: true};
}
"""

JS_LIST = """
() => {
  if (typeof monaco !== 'object' || !monaco || !monaco.editor) return null;
  const eds = monaco.editor.getEditors();
  const vis = e => { const n = e.getDomNode();
      return !!(n && n.isConnected && (n.offsetWidth || n.offsetHeight || n.getClientRects().length)); };
  const order = (x, y) => (x.getDomNode().compareDocumentPosition(y.getDomNode()) & 4) ? -1 : 1;
  const list = eds.filter(vis).sort(order);
  return {total: eds.length, visible: list.map((e, i) => {
    const m = e.getModel(); const n = e.getDomNode(); const r = n.getBoundingClientRect();
    return {index: i, lang: m && m.getLanguageId ? m.getLanguageId() : '',
            lines: m ? m.getLineCount() : 0,
            first: m ? m.getLineContent(1).slice(0, 60) : '',
            size: Math.round(r.width) + 'x' + Math.round(r.height),
            focused: !!(e.hasTextFocus && e.hasTextFocus()),
            readOnly: !!(e.getOption && e.getOption(monaco.editor.EditorOption.readOnly))};
  })};
}
"""

JS_SETUP = """
(a) => {
  const ed = window[Symbol.for(a.key)]; const m = ed.getModel();
  const saved = {};
  if (a.disable) {
    const raw = typeof ed.getRawOptions === 'function' ? ed.getRawOptions() : {};
    for (const k of Object.keys(a.opts)) saved[k] = (k in raw) ? raw[k] : undefined;
    ed.updateOptions(a.opts);
  }
  window[Symbol.for(a.key + 'saved')] = saved;
  if (a.cursor) {
    const last = m.getLineCount();
    let ln = a.line === 'end' ? last : a.line === 'start' ? 1 : Math.min(Math.max(1, a.line), last);
    let col = a.column === 'end' ? m.getLineMaxColumn(ln) : Math.min(a.column, m.getLineMaxColumn(ln));
    ed.setPosition({lineNumber: ln, column: col});
    ed.revealPositionInCenter({lineNumber: ln, column: col});
  } else {
    ed.setPosition(ed.getPosition());      // снять выделение, оставить курсор
  }
  ed.focus();
  const p = ed.getPosition(); const last = m.getLineCount();
  const R = (l1, c1, l2, c2) => ({startLineNumber: l1, startColumn: c1, endLineNumber: l2, endColumn: c2});
  return {
    eol: m.getEOL().length, lines: last, line: p.lineNumber, column: p.column,
    before: m.getValueInRange(R(1, 1, p.lineNumber, p.column), 1),
    after: m.getValueInRange(R(p.lineNumber, p.column, last, m.getLineMaxColumn(last)), 1),
    readOnly: !!(ed.getOption && ed.getOption(monaco.editor.EditorOption.readOnly)),
  };
}
"""

JS_STATE = """
(k) => { const ed = window[Symbol.for(k)]; const m = ed.getModel();
         return [m.getOffsetAt(ed.getPosition()), m.getValueLength()]; }
"""

JS_PEEK = """
(a) => { const ed = window[Symbol.for(a.key)]; const m = ed.getModel();
  const p = ed.getPosition(); const e = m.getPositionAt(m.getOffsetAt(p) + a.n);
  return m.getValueInRange({startLineNumber: p.lineNumber, startColumn: p.column,
                            endLineNumber: e.lineNumber, endColumn: e.column}, 1); }
"""

JS_LINE = """
(k) => { const ed = window[Symbol.for(k)]; const m = ed.getModel(); const p = ed.getPosition();
  return [m.getValueInRange({startLineNumber: p.lineNumber, startColumn: 1,
                             endLineNumber: p.lineNumber, endColumn: p.column}, 1),
          m.getLineContent(p.lineNumber)]; }
"""

JS_READ = "(k) => window[Symbol.for(k)].getModel().getValue(1)"

JS_FINISH = """
(a) => { const ed = window[Symbol.for(a.key)]; if (!ed) return;
  if (a.restore) { const s = window[Symbol.for(a.key + 'saved')]; if (s) ed.updateOptions(s); }
  delete window[Symbol.for(a.key)]; delete window[Symbol.for(a.key + 'saved')]; }
"""

# Подсказки/автодополнение/форматирование мешают вводу (Enter принял бы подсказку) — отключаем всегда
MONACO_SUGGEST_OFF = {
    "formatOnType": False,
    "formatOnPaste": False,
    "quickSuggestions": False,
    "suggestOnTriggerCharacters": False,
    "acceptSuggestionOnEnter": "off",
    "acceptSuggestionOnCommitCharacter": False,
    "snippetSuggestions": "none",
    "tabCompletion": "off",
    "parameterHints": {"enabled": False},
    "linkedEditing": False,
}
# Помощь редактора со структурой — отключаем только в режиме MONACO_MODE = "literal"
MONACO_STRUCTURE_OFF = {
    "autoIndent": "none",
    "autoClosingBrackets": "never",
    "autoClosingQuotes": "never",
    "autoClosingOvertype": "never",
    "autoClosingDelete": "never",
    "autoClosingComments": "never",
    "autoSurround": "never",
    "useTabStops": False,
    "trimAutoWhitespace": False,
}


class MonacoTarget:
    kind = "monaco"

    def __init__(self, frame, key):
        self.frame, self.key = frame, key
        self.eol_len = 1
        self.before = self.after = ""

    async def _ev(self, js, arg):
        return await self.frame.evaluate(js, arg, isolated_context=False)

    async def prepare(self, cfg, manual):
        opts = dict(MONACO_SUGGEST_OFF)
        if cfg.MONACO_MODE == "literal":
            opts.update(MONACO_STRUCTURE_OFF)
        r = await self._ev(JS_SETUP, {
            "key": self.key, "disable": bool(cfg.MONACO_DISABLE_AUTOFEATURES), "opts": opts,
            "cursor": not manual, "line": cfg.CURSOR_LINE, "column": cfg.CURSOR_COLUMN,
        })
        self.eol_len = r["eol"]
        self.before, self.after = r["before"], r["after"]
        if r.get("readOnly"):
            raise RuntimeError("Редактор доступен только для чтения (readOnly).")
        return r

    async def state(self):
        return await self._ev(JS_STATE, self.key)

    async def peek(self, n):
        """Текст справа от курсора (n знаков), переводы строк как \\n."""
        return await self._ev(JS_PEEK, {"key": self.key, "n": int(n)})

    async def line_info(self):
        """(текст строки до курсора, вся строка)"""
        r = await self._ev(JS_LINE, self.key)
        return r[0], r[1]

    async def read_text(self):
        return await self._ev(JS_READ, self.key)

    async def finish(self, cfg):
        try:
            await self._ev(JS_FINISH, {"key": self.key, "restore": bool(cfg.MONACO_RESTORE_OPTIONS)})
        except Exception:
            pass


def _frames(page):
    return [page.main_frame] + [f for f in page.frames if f != page.main_frame]


async def find_monaco(page, mode, selector="", index=0):
    key = "stx" + secrets.token_hex(5)
    last_err = None
    for fr in _frames(page):
        try:
            r = await fr.evaluate(JS_LOCATE, {"mode": mode, "selector": selector, "index": index, "key": key},
                                  isolated_context=False)
        except Exception as e:  # кадр мог закрыться/быть недоступен
            last_err = e
            continue
        if r and r.get("ok"):
            return MonacoTarget(fr, key)
    return None


async def list_editors(page):
    out = []
    for fr in _frames(page):
        try:
            r = await fr.evaluate(JS_LIST, None, isolated_context=False)
        except Exception:
            continue
        if r:
            out.append((fr, r))
    return out


# ============================================================
# 3. Обычные поля: input / textarea / contenteditable
# ============================================================

JS_DOM_INFO = """
(el) => ({tag: el.tagName.toLowerCase(), type: el.type || '', ce: !!el.isContentEditable,
          hasValue: typeof el.value === 'string'})
"""

JS_DOM_PREPARE = """
(el, a) => {
  el.focus();
  if (typeof el.value === 'string') {
    const v = el.value; let pos;
    if (a.manual) { pos = el.selectionStart == null ? v.length : el.selectionStart; }
    else {
      const lines = v.split('\\n');
      let ln = a.line === 'end' ? lines.length : a.line === 'start' ? 1 : Math.min(Math.max(1, a.line), lines.length);
      let start = 0; for (let i = 0; i < ln - 1; i++) start += lines[i].length + 1;
      const len = lines[ln - 1].length;
      pos = start + (a.column === 'end' ? len : Math.min(a.column - 1, len));
    }
    try { el.setSelectionRange(pos, pos); } catch (e) {}
    return {before: v.slice(0, pos), after: v.slice(pos), editable: true};
  }
  if (!a.manual) {
    const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
    const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
  }
  return {before: '', after: '', editable: false};
}
"""

JS_DOM_STATE = "(el) => (typeof el.value === 'string' && el.selectionStart != null) ? [el.selectionStart, el.value.length] : null"
JS_DOM_READ = "(el) => typeof el.value === 'string' ? el.value.replace(/\\r\\n/g, '\\n') : el.innerText"


class DomTarget:
    def __init__(self, locator, info):
        self.loc, self.info = locator, info
        self.kind = info["tag"] if info["hasValue"] else "contenteditable"
        self.eol_len = 1
        self.before = self.after = ""
        self.strict = info["hasValue"]       # для contenteditable сверка нестрогая
        self._state_ok = True

    async def prepare(self, cfg, manual):
        r = await self.loc.evaluate(JS_DOM_PREPARE, {"manual": manual, "line": cfg.CURSOR_LINE,
                                                      "column": cfg.CURSOR_COLUMN})
        self.before, self.after = r["before"], r["after"]
        return r

    async def state(self):
        if not self._state_ok:
            return None
        try:
            st = await self.loc.evaluate(JS_DOM_STATE)
        except Exception:
            st = None
        if st is None:
            self._state_ok = False
        return st

    async def read_text(self):
        return await self.loc.evaluate(JS_DOM_READ)

    async def finish(self, cfg):
        pass


async def find_dom(page, selector, index, manual, timeout_ms):
    sel = ":focus" if manual else selector
    loc = None
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
    while True:
        for fr in _frames(page):
            l = fr.locator(sel).nth(0 if manual else index)
            try:
                if await l.count() and await l.is_visible():
                    loc = l
                    break
            except Exception:
                continue
        if loc or asyncio.get_event_loop().time() > deadline:
            break
        await asyncio.sleep(0.3)
    if not loc:
        return None
    info = await loc.evaluate(JS_DOM_INFO)
    if not (info["hasValue"] or info["ce"]):
        return None
    return DomTarget(loc, info)


# ============================================================
# 4. Сессия браузера: launch / attach
# ============================================================

def _scheme_candidates(cfg):
    """Значения color_scheme для Patchright/Playwright. «system» = снять принудительную светлую тему.
    Разные версии называют «сброс» по-разному ("null" / "no-override"), поэтому пробуем по очереди."""
    return ["null", "no-override", None]


async def _try_variants(fn, variants, what, log):
    last = None
    for v in variants:
        try:
            return await fn(v)
        except Exception as e:      # неподходящее значение отвергается до запуска браузера
            last = e
    log(f"[!] Не удалось применить {what}: {last}")
    return None


class Session:
    def __init__(self, context, page, owned, browser=None):
        self.context, self.page, self.owned, self.browser = context, page, owned, browser


async def ask(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


def _edge_path():
    cands = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        "/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return shutil.which("msedge") or shutil.which("microsoft-edge")


def _port_open(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5):
            return True
    except Exception:
        return False


def _start_edge(cfg, log):
    exe = _edge_path()
    if not exe:
        raise RuntimeError("Edge не найден. Запустите его сами: start_edge_debug.bat")
    profile = settings.resolve_path("data/edge_debug_profile")
    args = [exe, f"--remote-debugging-port={cfg.DEBUG_PORT}", f"--user-data-dir={profile}", "--no-first-run"]
    log(f"[i] Запускаю Edge с отладочным портом {cfg.DEBUG_PORT} (профиль: {profile})")
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


async def _pick_tab(pages, cfg, log):
    pages = [p for p in pages if not p.url.startswith(("devtools://", "chrome-extension://"))]
    if not pages:
        raise RuntimeError("В Edge нет открытых вкладок.")
    if cfg.ATTACH_TAB:
        needle = cfg.ATTACH_TAB.lower()
        for p in pages:
            try:
                title = (await p.title()).lower()
            except Exception:
                title = ""
            if needle in p.url.lower() or needle in title:
                return p
        raise RuntimeError(f"Вкладка с «{cfg.ATTACH_TAB}» не найдена среди {len(pages)} открытых.")
    if len(pages) == 1:
        return pages[0]
    visible = []
    for p in pages:
        try:
            if await p.evaluate("document.visibilityState") == "visible":
                visible.append(p)
        except Exception:
            pass
    if len(visible) == 1:
        return visible[0]
    log("[?] Открыто несколько вкладок:")
    for i, p in enumerate(pages):
        try:
            title = (await p.title())[:50]
        except Exception:
            title = ""
        log(f"   {i}: {title} — {p.url[:80]}")
    ans = (await ask("Номер вкладки: ")).strip()
    return pages[int(ans)]


async def open_session(p, cfg, log=print) -> Session:
    if cfg.BROWSER_MODE == "launch":
        async def _launch(scheme):
            kw = dict(user_data_dir=str(settings.resolve_path(cfg.USER_DATA_DIR)),
                      channel=cfg.BROWSER_CHANNEL, headless=False, no_viewport=True, args=[])
            if scheme is not None:
                kw["color_scheme"] = scheme
            return await p.chromium.launch_persistent_context(**kw)

        # По умолчанию Playwright принудительно включает СВЕТЛУЮ тему; мы это снимаем (тема всегда системная)
        ctx = await _try_variants(_launch, _scheme_candidates(cfg), "запуск браузера", log)
        if ctx is None:
            raise RuntimeError("Не удалось запустить браузер.")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(cfg.PAGE_LOAD_TIMEOUT_MS)
        if cfg.TARGET_URL:
            log(f"[i] Открываю {cfg.TARGET_URL} …")
            await page.goto(cfg.TARGET_URL, wait_until="domcontentloaded")
        return Session(ctx, page, owned=True)

    # attach
    url = f"http://127.0.0.1:{cfg.DEBUG_PORT}"
    if not _port_open(cfg.DEBUG_PORT):
        if not cfg.AUTO_START_EDGE:
            raise RuntimeError(f"Edge с портом {cfg.DEBUG_PORT} не найден. Запустите start_edge_debug.bat "
                               f"(или включите AUTO_START_EDGE).")
        _start_edge(cfg, log)
        for _ in range(40):
            if _port_open(cfg.DEBUG_PORT):
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError(
                "Порт отладки не открылся. Если Edge уже запущен с этим же профилем — закройте его "
                "и используйте start_edge_debug.bat. В новых версиях Edge порт не работает "
                "с основным профилем, поэтому нужен отдельный (его создаёт start_edge_debug.bat).")
        log("[i] Edge запущен. Откройте нужную страницу и войдите в аккаунт, если требуется.")
    browser = await p.chromium.connect_over_cdp(url)
    if not browser.contexts:
        raise RuntimeError("Не удалось получить окно браузера.")
    ctx = browser.contexts[0]
    page = await _pick_tab(ctx.pages, cfg, log)
    page.set_default_timeout(cfg.PAGE_LOAD_TIMEOUT_MS)
    # при подключении Playwright тоже может навязать светлую тему — возвращаем системную (или заданную)
    await _try_variants(lambda v: page.emulate_media(color_scheme=v), _scheme_candidates(cfg),
                        "настройку темы", log)
    if cfg.ATTACH_GOTO and cfg.TARGET_URL:
        await page.goto(cfg.TARGET_URL, wait_until="domcontentloaded")
    log(f"[i] Подключился к вкладке: {page.url[:90]}")
    return Session(ctx, page, owned=False, browser=browser)


# ============================================================
# 5. Выбор цели ввода
# ============================================================

async def resolve_target(page, cfg, log=print):
    manual = cfg.CURSOR_MODE == "manual"
    if manual:
        t = await find_monaco(page, "focused")
        if t:
            log("[i] Поле: Monaco (где стоит курсор)")
            return t
        t = await find_dom(page, "", 0, True, 2000)
        if t:
            log(f"[i] Поле: {t.kind} (где стоит курсор)")
            return t
        raise RuntimeError("Не нашёл поле с курсором. Кликните в редактор/поле и повторите.")

    deadline = asyncio.get_event_loop().time() + cfg.FIELD_WAIT_TIMEOUT_MS / 1000.0
    while True:
        if cfg.FIELD_SELECTOR:
            t = await find_monaco(page, "selector", cfg.FIELD_SELECTOR, cfg.FIELD_INDEX)
            if t:
                log(f"[i] Поле: Monaco ({cfg.FIELD_SELECTOR} #{cfg.FIELD_INDEX})")
                return t
            t = await find_dom(page, cfg.FIELD_SELECTOR, cfg.FIELD_INDEX, False, 1500)
            if t:
                log(f"[i] Поле: {t.kind} ({cfg.FIELD_SELECTOR} #{cfg.FIELD_INDEX})")
                return t
        else:
            t = await find_monaco(page, "index", "", cfg.FIELD_INDEX)
            if t:
                log(f"[i] Поле: Monaco-редактор №{cfg.FIELD_INDEX}")
                return t
        if asyncio.get_event_loop().time() > deadline:
            break
        await asyncio.sleep(0.5)
    hint = ("Проверьте FIELD_SELECTOR/FIELD_INDEX. Посмотреть редакторы на странице: "
            "python app/stealth_typer.py --list")
    if not cfg.FIELD_SELECTOR:
        hint = ("Monaco-редактор не найден. Для обычных полей укажите FIELD_SELECTOR. " + hint)
    raise RuntimeError("Поле не найдено. " + hint)


# ============================================================
# 6. Проверка результата
# ============================================================

def diff_report(expected: str, actual: str) -> str:
    n = min(len(expected), len(actual))
    i = next((k for k in range(n) if expected[k] != actual[k]), n)
    line = expected.count("\n", 0, i) + 1
    col = i - (expected.rfind("\n", 0, i) + 1) + 1
    lo = max(0, i - 25)
    return (f"первое расхождение: строка {line}, колонка {col}\n"
            f"   ожидалось: {expected[lo:i + 25]!r}\n"
            f"   в поле:    {actual[lo:i + 25]!r}\n"
            f"   длина: ожидалось {len(expected)}, в поле {len(actual)}")


async def verify(target, typed: str) -> tuple:
    """(ok, сообщение)"""
    actual = (await target.read_text()).replace("\r\n", "\n")
    if target.kind == "contenteditable":
        norm = lambda s: "".join(s.split())   # noqa: E731
        ok = norm(typed) in norm(actual)
        return ok, ("Текст найден в поле (нестрогая проверка)." if ok
                    else "Текст в поле не найден — проверьте глазами.")
    expected = target.before + typed + target.after
    if actual == expected:
        return True, "Текст в поле совпал с исходным на 100%."
    return False, "Текст в поле ОТЛИЧАЕТСЯ от исходного: " + diff_report(expected, actual)
