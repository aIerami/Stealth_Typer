# -*- coding: utf-8 -*-
"""
Пример своей запускалки под конкретный сайт.

Скопируйте этот файл, например в run_my_site.py, и поменяйте имя профиля SITE
на имя из config.py (раздел SITES). Имя профиля пишите ЛАТИНИЦЕЙ (my_site).

Запуск:  python run_my_site.py        (или тем Python из venv, которым вы пользуетесь)

Скрипт вызывает программу напрямую, без командной строки Windows, поэтому кодировка
не портится. Можно добавить любые параметры: "--text", "texts/page2.html", "--wpm", "50-80" ...
"""

import sys

import stealth_typer

SITE = "my_site"

if __name__ == "__main__":
    sys.exit(stealth_typer.main(["--site", SITE] + sys.argv[1:]))
