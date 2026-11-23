# -*- coding: utf-8 -*-
import asyncio
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402
import settings  # noqa: E402
from simulator import FakeEditor, VirtualClock  # noqa: E402


def cfg(**kw):
    base = dict(ERROR_RATE=0.0, PAUSES=1.0, AUTOCLOSE_WAIT_MS=300, WPM_MIN=40, WPM_MAX=90)
    base.update(kw)
    return settings.build(site="", overrides=base)


def run_typing(text, editor=None, seed=1, **cfg_kw):
    clock = editor.clock if editor else VirtualClock()
    ed = editor or FakeEditor(clock=clock)
    c = cfg(**cfg_kw)
    t = engine.Typist(ed, ed, c, rng=random.Random(seed), sleep=clock.sleep,
                      now=clock.now, log=lambda *_: None)
    stats = asyncio.run(t.type_text(text))
    return ed, stats


HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Тест</title>
    <style>
        body { margin: 0; }
    </style>
</head>
<body>
    <div class="box">
        <p>Привет, <b>мир</b> и <i>текст</i> в одной строке.</p>
        <ul>
            <li>Один</li>
            <li>Два</li>
        </ul>
        <img src="a.png" alt="x"><br>
        <!-- комментарий -->
    </div>
    <script>
        for (let i = 0; i < 3; i++) { console.log(i > 1 ? "a" : 'b'); }
    </script>
</body>
</html>"""


class TestKeymap(unittest.TestCase):
    def test_ascii_and_cyrillic_have_keys(self):
        for ch in "abcXYZ019 !@#$%^&*()_+{}|:\"<>?~`-=[]\\;',./йцукенгшщзхъфывапролджэячсмитьбюёЁЙ":
            self.assertIn(ch, engine.KEYMAP, ch)

    def test_unmapped_passthrough(self):
        k = engine.key_for("—")
        self.assertEqual(k.text, "—")
        self.assertEqual(engine.key_for("😀").text, "😀")

    def test_uppercase_needs_shift(self):
        self.assertTrue(engine.key_for("A").shift)
        self.assertFalse(engine.key_for("a").shift)
        self.assertEqual(engine.key_for("A").unmod, "a")

    def test_neighbors(self):
        self.assertIn("w", engine.neighbors("q"))
        self.assertIn("Ц", engine.neighbors("Й"))
        self.assertEqual(engine.neighbors("1"), [])


class TestSpeed(unittest.TestCase):
    def sample(self, t_from, t_to, **kw):
        c = cfg(**kw)
        m = engine.SpeedModel(c, random.Random(5))
        vals = []
        t = 0.0
        while t < t_to:
            t += 0.2
            m.update(t)
            if t >= t_from:
                vals.append(m.wpm())
        return vals

    def test_within_bounds_and_varies(self):
        v = self.sample(0, 600, VARIABILITY=0.8)
        self.assertGreaterEqual(min(v), 40)
        self.assertLessEqual(max(v), 90)
        self.assertGreater(max(v) - min(v), 10)

    def test_fatigue_slows_down(self):
        tired, fresh = [], []
        for seed in range(30):
            for lst, fat in ((tired, 1.0), (fresh, 0.0)):
                c = cfg(FATIGUE=fat, VARIABILITY=0.3, FATIGUE_MINUTES=5)
                m = engine.SpeedModel(c, random.Random(seed))
                m.base = 0.6
                t = 0.0
                while t < 1800:
                    t += 1.0
                    m.update(t)
                lst.append(m.wpm())
        self.assertLess(sum(tired) / 30, sum(fresh) / 30 - 8)

    def test_variability_zero_is_calm(self):
        v = self.sample(120, 600, VARIABILITY=0.0, FATIGUE=0.0)
        self.assertLess(max(v) - min(v), 3)


class TestTyping(unittest.TestCase):
    def test_html_sync_autoclose(self):
        ed, st = run_typing(HTML, AUTOCLOSE_MODE="delete")
        self.assertEqual(ed.text, HTML)
        self.assertEqual(st["anomalies"], 0)
        self.assertGreater(st["junk_removed"], 0)
        self.assertEqual(st["skipped_tags"], 0)

    def test_html_delayed_autoclose_within_wait(self):
        clock = VirtualClock()
        ed = FakeEditor(clock=clock, autoclose_delay=0.15)
        ed, st = run_typing(HTML, editor=ed)
        self.assertEqual(ed.text, HTML)
        self.assertEqual(st["anomalies"], 0)

    def test_autoclose_too_late_is_detected(self):
        clock = VirtualClock()
        ed = FakeEditor(clock=clock, autoclose_delay=1.5)
        ed, st = run_typing("<p>a <b>x</b> y</p>", editor=ed)
        self.assertNotEqual(ed.text, "<p>a <b>x</b> y</p>")   # сломалось...
        self.assertGreater(st["anomalies"], 0)                  # ...и скрипт это заметил

    def test_autoclose_pairs_and_autoindent(self):
        ed = FakeEditor(autoclose_pairs=True, autoindent=True)
        ed, st = run_typing(HTML, editor=ed)
        self.assertEqual(ed.text, HTML)
        self.assertEqual(st["anomalies"], 0)

    def test_text_in_the_middle_is_preserved(self):
        before, after = "<body>\n    ", "\n</body>\n</html>"
        ed = FakeEditor(text=before + after, cursor=len(before))
        ed, st = run_typing("<div>\n    <p>x</p>\n</div>", editor=ed)
        self.assertEqual(ed.text, before + "<div>\n    <p>x</p>\n</div>" + after)
        self.assertEqual(st["anomalies"], 0)

    def test_crlf_editor(self):
        ed = FakeEditor(eol="\r\n")
        c = cfg()
        clock = ed.clock
        t = engine.Typist(ed, ed, c, rng=random.Random(2), sleep=clock.sleep, now=clock.now,
                          log=lambda *_: None)
        t.eol_len = 2
        asyncio.run(t.type_text("<div>\n  <p>a</p>\n</div>"))
        self.assertEqual(ed.text, "<div>\n  <p>a</p>\n</div>")
        self.assertEqual(t.stats["anomalies"], 0)

    def test_typos_are_always_fixed(self):
        text = "Привет, мир! Это проверка опечаток: hello world, 123.\n    Вторая строка (с отступом)."
        for seed in range(8):
            ed, st = run_typing(text, seed=seed, ERROR_RATE=0.3, TYPO_NOTICE_MAX=3)
            self.assertEqual(ed.text, text, f"seed={seed}")
            self.assertEqual(st["anomalies"], 0)
        self.assertGreater(st["typos"], 3)

    def test_typos_inside_html(self):
        for seed in range(6):
            ed, st = run_typing(HTML, seed=seed, ERROR_RATE=0.15)
            self.assertEqual(ed.text, HTML, f"seed={seed}")

    def test_cyrillic_tabs_unicode_indent(self):
        text = "\tтаб\n  — «кавычки» № ёЁ 😀\n\t\tглубже"
        ed, st = run_typing(text)
        self.assertEqual(ed.text, text)

    def test_no_probe_still_types(self):
        clock = VirtualClock()
        ed = FakeEditor(clock=clock, autoclose_tags=False)
        c = cfg()
        t = engine.Typist(ed, None, c, rng=random.Random(1), sleep=clock.sleep, now=clock.now,
                          log=lambda *_: None)
        asyncio.run(t.type_text("abc\n  def"))
        self.assertEqual(ed.text, "abc\n  def")

    def test_hold_time_and_shift_events(self):
        ed, _ = run_typing("Ab")
        kinds = [(e[0], e[1]) for e in ed.events]
        self.assertEqual(kinds[0], ("rawKeyDown", "Shift"))     # Shift нажимается отдельно
        self.assertIn(("keyUp", "Shift"), kinds)

    def test_speed_varies_over_long_text(self):
        text = ("слово " * 40 + "\n") * 40
        ed, st = run_typing(text, WPM_MIN=40, WPM_MAX=100, VARIABILITY=0.8)
        self.assertGreater(st["wpm_max"] - st["wpm_min"], 8)
        self.assertGreaterEqual(st["wpm_min"], 40)
        self.assertLessEqual(st["wpm_max"], 100)

    def test_duration_roughly_matches_wpm(self):
        text = "а" * 3000
        ed, st = run_typing(text, WPM_MIN=60, WPM_MAX=60, PAUSES=0, VARIABILITY=0, FATIGUE=0)
        expected = 3000 / (60 * 5 / 60)       # секунд
        self.assertAlmostEqual(st["duration"] / expected, 1.0, delta=0.25)


class TestSkipMode(unittest.TestCase):
    """AUTOCLOSE_MODE = "skip": печатаем внутри автозакрытого тега и перепрыгиваем через него."""

    def check(self, src, editor=None, seed=1, **kw):
        ed, st = run_typing(src, editor=editor, seed=seed, AUTOCLOSE_MODE="skip", **kw)
        self.assertEqual(ed.text, src)
        self.assertEqual(st["anomalies"], 0)
        return st

    def test_default_is_skip(self):
        self.assertEqual(cfg().AUTOCLOSE_MODE, "skip")

    def test_html_sample(self):
        st = self.check(HTML)
        self.assertGreater(st["skipped_tags"], 10)
        self.assertEqual(st["junk_removed"], 0)       # ничего не удаляли — только перепрыгивали

    def test_nested_inline_same_line(self):
        for src in ("<p>a <b>x</b> y</p>", "<p><b>Bold</b> and text</p>",
                    "<p><b><i>deep</i></b> text <u>u</u></p>"):
            st = self.check(src)
            self.assertGreater(st["skipped_tags"], 1)

    def test_outdent_empty_line_after_enter(self):
        # Enter между <div> и </div> даёт пустую строку с отступом, </div> уходит ниже
        ed = FakeEditor(outdent=True, autoindent=True)
        self.check("<div>\n  <p>x</p>\n</div>", editor=ed)
        ed = FakeEditor(outdent=True, autoindent=True)
        self.check(HTML, editor=ed)

    def test_delayed_autoclose(self):
        ed = FakeEditor(autoclose_delay=0.15)
        self.check(HTML, editor=ed)

    def test_unclosed_tags_in_source(self):
        self.check("<ul>\n<li>One\n<li>Two\n</ul>")
        self.check("<p>text without closing")
        self.check("<div><p>a</div>")

    def test_stray_closing_tag_typed_literally(self):
        self.check("a</b>b")

    def test_void_and_selfclosing(self):
        self.check('<img src="a.png"><br><input type="text"/><hr>\n<p>x</p>')

    def test_text_in_the_middle(self):
        before, after = "<body>\n    ", "\n</body>\n</html>"
        src = "<div>\n    <p>x</p>\n</div>"
        ed = FakeEditor(text=before + after, cursor=len(before))
        ed, st = run_typing(src, editor=ed, AUTOCLOSE_MODE="skip")
        self.assertEqual(ed.text, before + src + after)
        self.assertEqual(st["anomalies"], 0)

    def test_with_typos_and_pairs(self):
        for seed in range(6):
            ed = FakeEditor(autoclose_pairs=True, autoindent=True, outdent=True)
            self.check(HTML, editor=ed, seed=seed, ERROR_RATE=0.15)

    def test_crlf(self):
        ed = FakeEditor(eol="\r\n")
        clock = ed.clock
        c = cfg()
        t = engine.Typist(ed, ed, c, rng=random.Random(2), sleep=clock.sleep, now=clock.now,
                          log=lambda *_: None)
        t.eol_len = 2
        asyncio.run(t.type_text(HTML))
        self.assertEqual(ed.text, HTML)
        self.assertEqual(t.stats["anomalies"], 0)

    def test_skip_is_faster_than_delete(self):
        _, a = run_typing(HTML, AUTOCLOSE_MODE="skip", PAUSES=0)
        _, b = run_typing(HTML, AUTOCLOSE_MODE="delete", PAUSES=0)
        self.assertLess(a["duration"], b["duration"])

    def test_too_late_autoclose_still_detected(self):
        ed = FakeEditor(autoclose_delay=1.5)
        ed, st = run_typing("<p>a <b>x</b> y</p>", editor=ed, AUTOCLOSE_MODE="skip")
        self.assertGreater(st["anomalies"], 0)

    def test_no_autoclose_on_site(self):
        ed = FakeEditor(autoclose_tags=False)
        self.check(HTML, editor=ed)


class TestNatural(unittest.TestCase):
    """MONACO_MODE = "natural": помощь редактора включена, скрипт подстраивается под неё."""

    def smart(self, **kw):
        base = dict(indent="smart", autoclose_pairs=True)
        base.update(kw)
        return FakeEditor(**base)

    def check(self, src, editor=None, seed=1, **kw):
        ed = editor or self.smart()
        ed, st = run_typing(src, editor=ed, seed=seed, MONACO_MODE="natural", **kw)
        self.assertEqual(ed.text, src)
        self.assertEqual(st["anomalies"], 0)
        return ed, st

    def test_default_is_natural(self):
        self.assertEqual(cfg().MONACO_MODE, "natural")

    def test_user_example_div_enter(self):
        # <div>|</div> + Enter -> пустая строка; текст внутри; потом выход вниз без второго Enter
        src = '<div>\n"Курсор"\n</div>'
        ed, st = self.check(src)
        enters = [e for e in ed.events if e[0] == "keyDown" and e[1] == "Enter"]
        self.assertEqual(len(enters), 1)                 # Enter нажат один раз: второй перенос уже есть
        self.assertGreaterEqual(st["skipped_tags"], 1)

    def test_html_two_space_indent_vs_editor_four(self):
        self.check(HTML)

    def test_html_tab_indent_in_source(self):
        self.check(HTML.replace("    ", "\t"))

    def test_css_braces(self):
        css = ("body {\n  margin: 0;\n  font: 12px \"Arial\", 'Helvetica';\n}\n"
               ".a, .b {\n  color: red;\n}\n@media (max-width: 600px) {\n  .a {\n    color: blue;\n  }\n}")
        self.check(css)

    def test_js_brackets_quotes_templates(self):
        js = ("function f(a, b) {\n  const x = [1, 2, {k: \"v\"}];\n  if (a > b) {\n    "
              "console.log(`sum: ${a + b}`, 'ok');\n  }\n  return (a + b) * 2;\n}\n"
              "const o = {\n  a: [\n    1,\n    2\n  ],\n  b: () => { return 1; }\n};")
        self.check(js)

    def test_html_with_attrs_and_quotes(self):
        self.check('<a href="x.html" class=\'big\' title="a (b) [c]">link</a>\n<img src="a.png" alt="x">')

    def test_apostrophes_and_unbalanced_quote(self):
        self.check("It's a test: don't (fail) and \"quote\" it's fine.\nsecond \"line")
        self.check("a ' b\n(c")

    def test_blank_lines_and_trailing_spaces_in_source(self):
        self.check("<div>\n\n  <p>x</p>\n\n</div>\n\n<p>y</p>")
        self.check("<div>\n  <p>x</p>  \n</div>")

    def test_empty_element_then_close(self):
        self.check("<div>\n</div>")
        self.check("<ul>\n  <li>\n  </li>\n</ul>")

    def test_unclosed_tags(self):
        self.check("<ul>\n  <li>One\n  <li>Two\n</ul>")

    def test_sites_with_no_helpers(self):
        self.check(HTML, editor=FakeEditor(autoclose_tags=False))
        self.check(HTML, editor=FakeEditor(autoclose_tags=True, indent=None))

    def test_copy_indent_only(self):
        self.check(HTML, editor=FakeEditor(indent="copy", autoclose_pairs=True))

    def test_typos_everywhere(self):
        for seed in range(6):
            self.check(HTML, seed=seed, ERROR_RATE=0.12)
        for seed in range(4):
            self.check("function f(a) {\n  return \"abc\" + (a * 2);\n}", seed=seed, ERROR_RATE=0.15)

    def test_text_in_the_middle(self):
        before, after = "<body>\n    ", "\n</body>\n</html>"
        src = '<div class="x">\n    <p>"q" (z)</p>\n</div>'
        ed = self.smart(text=before + after, cursor=len(before))
        ed, st = run_typing(src, editor=ed, MONACO_MODE="natural")
        self.assertEqual(ed.text, before + src + after)
        self.assertEqual(st["anomalies"], 0)

    def test_crlf(self):
        ed = self.smart(eol="\r\n")
        clock = ed.clock
        t = engine.Typist(ed, ed, cfg(), rng=random.Random(2), sleep=clock.sleep, now=clock.now,
                          log=lambda *_: None)
        t.eol_len = 2
        asyncio.run(t.type_text(HTML + "\n<script>\nf(\"a\", [1]);\n</script>"))
        self.assertEqual(ed.text, HTML + "\n<script>\nf(\"a\", [1]);\n</script>")
        self.assertEqual(t.stats["anomalies"], 0)

    def test_delayed_tag_autoclose(self):
        self.check(HTML, editor=self.smart(autoclose_delay=0.15))

    def test_literal_mode_still_works(self):
        ed, st = run_typing(HTML, editor=FakeEditor(indent="copy", autoclose_pairs=True), MONACO_MODE="literal")
        self.assertEqual(ed.text, HTML)

    def test_delete_mode_with_natural_indent(self):
        ed, st = run_typing(HTML, editor=self.smart(), MONACO_MODE="natural", AUTOCLOSE_MODE="delete")
        self.assertEqual(ed.text, HTML)

    def test_fewer_keystrokes_than_literal(self):
        e1, e2 = self.smart(), self.smart()
        run_typing(HTML, editor=e1, MONACO_MODE="natural", PAUSES=0)
        run_typing(HTML, editor=e2, MONACO_MODE="literal", PAUSES=0)
        n1 = sum(1 for e in e1.events if e[0] == "keyDown")
        n2 = sum(1 for e in e2.events if e[0] == "keyDown")
        self.assertLess(n1, n2)


class TestMojibake(unittest.TestCase):
    NAME = "мой_сайт"

    def setUp(self):
        import config
        config.SITES[self.NAME] = {"WPM_MIN": 51}
        self.addCleanup(config.SITES.pop, self.NAME, None)

    def test_utf8_read_as_oem_codepages(self):
        for enc in ("cp866", "cp1251", "cp437", "cp850"):
            garbled = self.NAME.encode("utf-8").decode(enc, errors="replace")
            self.assertEqual(settings.build(site=garbled).WPM_MIN, 51, enc)

    def test_ansi_bat_read_as_cp866(self):
        garbled = self.NAME.encode("cp1251").decode("cp866")
        self.assertEqual(settings.build(site=garbled).SITE_NAME, self.NAME)

    def test_case_insensitive_and_unknown(self):
        self.assertEqual(settings.build(site=self.NAME.upper()).SITE_NAME, self.NAME)
        with self.assertRaises(settings.ConfigError):
            settings.build(site="╨╜╨╡╤В")


class TestSettings(unittest.TestCase):
    def test_site_profile(self):
        c = settings.build(site="example_manual_cursor")
        self.assertEqual(c.CURSOR_MODE, "manual")
        self.assertEqual(c.WPM_MAX, 110)

    def test_site_name_mojibake_is_repaired(self):
        import config
        config.SITES["мой_сайт"] = {"WPM_MAX": 99}
        try:
            for enc in ("cp866", "cp1251", "cp437"):
                garbled = "мой_сайт".encode("utf-8").decode(enc, errors="strict") \
                    if enc != "cp437" else None
                if garbled is None:
                    continue
                self.assertEqual(settings.build(site=garbled).WPM_MAX, 99, enc)
            self.assertEqual(settings.build(site="МОЙ_САЙТ").WPM_MAX, 99)    # регистр
        finally:
            del config.SITES["мой_сайт"]

    def test_unknown_site_garbled_hint(self):
        with self.assertRaises(settings.ConfigError) as cm:
            settings.build(site="╨╝╨╛╨╣_x")
        self.assertIn("латиницей", str(cm.exception))

    def test_unknown_site(self):
        with self.assertRaises(settings.ConfigError):
            settings.build(site="нет_такого")

    def test_monaco_mode_validation(self):
        with self.assertRaises(settings.ConfigError):
            settings.build(site="", overrides={"MONACO_MODE": "x"})

    def test_autoclose_mode_validation(self):
        with self.assertRaises(settings.ConfigError):
            settings.build(site="", overrides={"AUTOCLOSE_MODE": "x"})

    def test_validation(self):
        with self.assertRaises(settings.ConfigError):
            settings.build(site="", overrides={"WPM_MIN": 100, "WPM_MAX": 50})
        with self.assertRaises(settings.ConfigError):
            settings.build(site="", overrides={"CURSOR_LINE": 0})


if __name__ == "__main__":
    unittest.main()
