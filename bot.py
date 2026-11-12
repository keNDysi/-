"""
Бот замен расписания БГАЭК для группы 33-п.

Что делает:
  1. Берёт с сайта колледжа ссылки на Google-документы с заменами (по одному на день недели).
  2. Из каждого документа достаёт дату и строки только для своей группы.
  3. Накладывает замены на обычное расписание (base_schedule.json).
  4. Если для сегодняшней/завтрашней даты что-то новое — присылает в Telegram.
  5. Вечером (после EVENING_HOUR), если замены на завтра ещё не вывесили,
     присылает обычное расписание на завтра с пометкой.

Запуск:
  python bot.py            — одна проверка (так запускает GitHub Actions)
  python bot.py --loop     — проверять каждые 10 минут (для запуска на своём ПК)
  python bot.py --dry      — ничего не отправлять, просто напечатать сообщения
  python bot.py --debug    — показать, что бот прочитал из каждого документа
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---------------- настройки ----------------
GROUP = "33-п"
SITE_URL = ("https://bgaek.by/2024/09/19/%D1%80%D0%B0%D1%81%D0%BF%D0%B8%D1%81%D0%B0%D0%BD%D0%B8%D0%B5-"
            "%D0%B1%D1%83%D1%85%D0%B3%D0%B0%D0%BB%D1%82%D0%B5%D1%80%D0%B0-%D0%BF%D1%80%D0%BE%D0%B3%D1%80"
            "%D0%B0%D0%BC%D0%BC%D0%B8%D1%81%D1%82%D1%8B/")
# Если сайт не откроется — используем эти документы (пн..сб на момент создания бота)
FALLBACK_DOC_IDS = [
    "1sV5QloMUpfqkyfLLc59QmPTyBXNSUYA1",
    "16BoaCwKzlnWrmTYhuAcH42nhrv-IlOkt",
    "1nSdGG8YjdwCM9_KHVfXj0dk9tflhq_gX",
    "1yCtjpPTl1hb6PhlqJMCbbch9Tbh5NtSQ",
    "1No-WT977T-oS3OTTTG8s5RTfJw4RKUeK",
    "1biAObySxkm-xeFuR_1otpU_xT6YGMKmO",
]
EVENING_HOUR = 20          # с этого часа шлём обычное расписание, если замен на завтра нет
TZ = ZoneInfo("Europe/Minsk")
DIR = Path(__file__).resolve().parent
STATE_FILE = DIR / "state.json"
BASE_FILE = DIR / "base_schedule.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (schedule bot 33-p)"}

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
MONTHS_GEN = {v: k for k, v in MONTHS.items()}
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
CANCEL_WORDS = {"-", "–", "—", "нет", "снято", "отмена", "отменено", "не будет"}
GROUP_RE = re.compile(r"(?<![\w-])(\d{2,3}-[а-яё])(?![\w-])", re.IGNORECASE)
NUM_RE = re.compile(r"^(\d{1,2})\s*\.?$")
DATE_RE = re.compile(r"на\s*[«\"']?\s*(\d{1,2})\s*[»\"']?\s*([а-яё]+)\s*(\d{4})", re.IGNORECASE)


# ---------------- загрузка и разбор ----------------
def get_doc_ids():
    """Ссылки на документы берём с сайта: если колледж заменит документ — бот это подхватит."""
    try:
        html = requests.get(SITE_URL, headers=HEADERS, timeout=30).text
        ids = re.findall(r"docs\.google\.com/document/d/([\w-]+)/pub", html)
        ids = list(dict.fromkeys(ids))  # убрать повторы, сохранить порядок
        if ids:
            return ids
    except requests.RequestException as e:
        print("Сайт колледжа не открылся:", e)
    return FALLBACK_DOC_IDS


def clean(text):
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def cell_text(td):
    """Текст ячейки; строки внутри ячейки (абзацы) склеиваем через ' / '."""
    paragraphs = td.find_all("p")
    if paragraphs:
        parts = [clean(p.get_text("")) for p in paragraphs]
    else:
        parts = [clean(td.get_text(" "))]
    return " / ".join(p for p in parts if p)


def expand_row(tr):
    """Список ячеек строки с учётом объединённых (colspan): [(колонка, текст), ...]."""
    cells, col = [], 0
    for td in tr.find_all(["td", "th"], recursive=False):
        span = int(td.get("colspan", 1) or 1)
        cells.append((col, cell_text(td)))
        col += span
    return cells


def parse_doc(html, group=GROUP):
    """Возвращает (дата или None, {номер_урока: (предмет, преподаватель, кабинет)})."""
    soup = BeautifulSoup(html, "html.parser")
    full_text = clean(soup.get_text(" "))

    doc_date = None
    m = DATE_RE.search(full_text)
    if m and m.group(2).lower() in MONTHS:
        try:
            doc_date = date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            pass

    changes = {}
    for table in soup.find_all("table"):
        tbody = table.find("tbody") or table
        rows = tbody.find_all("tr", recursive=False)
        # Где в таблице блок нашей группы: (колонка, с которой он начинается) или None
        block_start = None
        for tr in rows:
            cells = expand_row(tr)
            if not cells:
                continue
            first = cells[0][1]
            num = NUM_RE.match(first) if cells[0][0] == 0 else None

            if not num:
                # Строка-заголовок: ищем в ней названия групп
                found = [(c, GROUP_RE.search(t).group(1).lower()) for c, t in cells if GROUP_RE.search(t)]
                if found:
                    block_start = None
                    for c, g in found:
                        if g == group.lower():
                            # левый блок начинается с колонки 1, правый — с 4
                            block_start = 1 if c < 4 else 4
                continue

            if block_start is None:
                continue
            lesson = int(num.group(1))
            by_col = {}
            for c, t in cells:
                by_col[c] = t
            subj = by_col.get(block_start, "")
            teacher = by_col.get(block_start + 1, "")
            room = by_col.get(block_start + 2, "")
            if subj or teacher or room:
                changes[lesson] = (subj, teacher, room)
    return doc_date, changes


def fetch_docs():
    result = []
    for doc_id in get_doc_ids():
        url = f"https://docs.google.com/document/d/{doc_id}/pub?embedded=true"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            print("Не открылся документ", doc_id, e)
            continue
        d, ch = parse_doc(r.text)
        result.append((doc_id, d, ch))
    return result


# ---------------- сборка расписания ----------------
def norm_room(room):
    return re.sub(r"[\s,;/]+", "/", room.lower()).strip("/")


def merge(base_day, changes, teachers=None):
    """Итоговое расписание: {урок: (предмет, кабинет, преподаватель, пометка)}; пометка: '' / 'new' / 'room' / 'cancel'."""
    teachers = teachers or {}
    lessons = {}
    for k, v in base_day.items():
        if v:
            lessons[int(k)] = (v[0], v[1], teachers.get(v[0], ""), "")
    for n, (subj, teacher, room) in changes.items():
        base = lessons.get(n)
        if subj and clean(subj).lower() in CANCEL_WORDS:
            lessons[n] = (base[0] if base else "—", "", "", "cancel")
        elif subj:
            lessons[n] = (subj, room, teacher, "new")
        elif room:
            if base and norm_room(room) == norm_room(base[1]):
                continue  # в заменах просто продублирован тот же кабинет
            lessons[n] = (base[0] if base else "?", room, teacher or (base[2] if base else ""), "room")
        elif teacher and base:
            lessons[n] = (base[0], base[1], teacher, "new")
    return dict(sorted(lessons.items()))


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Сокращения предметов: (часть названия в нижнем регистре, сокращение)
SHORT_NAMES = [
    ("алгоритмизац", "ОАиП"),
    ("арифметико", "АЛОВТ"),
]


def short_name(name):
    low = name.lower()
    for key, short in SHORT_NAMES:
        if key in low:
            return short + (" (к.)" if "(к.)" in low else "")
    return name


def lesson_line(subj, room, teacher):
    parts = [esc(short_name(subj))]
    if room:
        parts.append(esc(room))
    if teacher:
        parts.append(esc(teacher))
    return " — ".join(parts)


def build_message(day, merged, has_doc, update=False):
    head = f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS_GEN[day.month]}"
    real = {n: v for n, v in merged.items() if v[3]}
    lines = []
    if update:
        lines.append("<b>Замены обновились</b>")
    lines.append(f"<b>{head}</b> — {GROUP}")
    lines.append("")

    if not has_doc:
        lines.append("Замены ещё не вывесили, это обычное расписание.")
    elif real:
        lines.append("<b>Замены:</b>")
        for n, (subj, room, teacher, mark) in real.items():
            if mark == "cancel":
                lines.append(f"{n} урок — {esc(short_name(subj))} (урок снят)")
            elif mark == "room":
                lines.append(f"{n} урок — {esc(short_name(subj))}, кабинет {esc(room)}")
            else:
                lines.append(f"{n} урок — {lesson_line(subj, room, teacher)}")
    else:
        lines.append("Замен нет.")
    lines.append("")

    lines.append("<b>Расписание:</b>")
    shown = [(n, v) for n, v in merged.items() if v[3] != "cancel"]
    if not shown:
        lines.append("Занятий нет.")
    for n, (subj, room, teacher, mark) in shown:
        text = lesson_line(subj, room, teacher)
        lines.append(f"{n}. <b>{text}</b> (замена)" if mark else f"{n}. {text}")
    return "\n".join(lines)


# ---------------- Telegram и состояние ----------------
def send(text, dry=False):
    if dry or not TG_TOKEN or not TG_CHAT_ID:
        print("----- сообщение -----\n" + text + "\n---------------------")
        return
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=30)
    if not r.ok:
        print("Telegram ошибка:", r.status_code, r.text)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sent": {}, "base_sent": []}


def save_state(state, today):
    # чистим старые даты, чтобы файл не рос
    keep = (today - timedelta(days=3)).isoformat()
    state["sent"] = {d: h for d, h in state["sent"].items() if d >= keep}
    state["base_sent"] = [d for d in state["base_sent"] if d >= keep]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def check_once(dry=False, debug=False):
    base_json = json.loads(BASE_FILE.read_text(encoding="utf-8"))
    base = base_json["days"]
    teachers = base_json.get("teachers", {})
    now = datetime.now(TZ)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    state = load_state()

    docs = fetch_docs()
    seen_dates = set()
    for doc_id, d, changes in docs:
        if debug:
            print(f"{doc_id}: дата={d}, строки {GROUP}: {changes}")
        if not d or d < today or d.weekday() == 6:
            continue
        seen_dates.add(d)
        key = d.isoformat()
        digest = hashlib.md5(json.dumps(sorted(changes.items()), ensure_ascii=False).encode()).hexdigest()
        if state["sent"].get(key) == digest:
            continue  # уже присылали ровно это
        update = key in state["sent"] or key in state["base_sent"]
        merged = merge(base.get(str(d.isoweekday()), {}), changes, teachers)
        send(build_message(d, merged, has_doc=True, update=update), dry)
        state["sent"][key] = digest

    # Вечером: замен на завтра нет — присылаем обычное расписание
    t_key = tomorrow.isoformat()
    if (now.hour >= EVENING_HOUR and tomorrow.weekday() != 6 and tomorrow not in seen_dates
            and t_key not in state["base_sent"] and t_key not in state["sent"]):
        merged = merge(base.get(str(tomorrow.isoweekday()), {}), {}, teachers)
        send(build_message(tomorrow, merged, has_doc=False), dry)
        state["base_sent"].append(t_key)

    if not dry:
        save_state(state, today)


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--loop" in args:
        while True:
            try:
                check_once(dry="--dry" in args)
            except Exception as e:  # чтобы бот не падал из-за одного сбоя сети
                print(datetime.now(TZ), "ошибка:", e)
            time.sleep(600)
    else:
        check_once(dry="--dry" in args, debug="--debug" in args)
