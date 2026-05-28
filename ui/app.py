"""
Gradio web UI for the LLM ETL configurator (RU).
4 tabs:
  1. Разбор данных
  2. Обучающие примеры
  3. Дообучение (запуск)
  4. Настройки и статус
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from io import StringIO
from pathlib import Path

import gradio as gr
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.data_parser import DataParser  # noqa: E402
from core.db_writer import DBWriter  # noqa: E402
from core.llm_client import LLMClient  # noqa: E402
from training.dataset_builder import add_example, example_count  # noqa: E402
from training.annotation import validate_records  # noqa: E402


# ── Lazy singletons ──────────────────────────────────────────────────────────
_client: LLMClient | None = None
_parser: DataParser | None = None
_writer: DBWriter | None = None


def get_parser() -> DataParser:
    global _client, _parser
    if _parser is None:
        _client = LLMClient()
        _parser = DataParser(_client)
    return _parser


def get_writer() -> DBWriter:
    global _writer
    if _writer is None:
        _writer = DBWriter()
    return _writer


# ── Вкладка 1: Разбор данных ─────────────────────────────────────────────────

def ui_parse(text: str, file, write_to_db: bool):
    if file is not None:
        try:
            with open(file.name, "rb") as f:
                raw = f.read()
            records = get_parser().parse_bytes(raw, filename=file.name)
        except Exception as e:
            return f"ОШИБКА: {e}\n\n{traceback.format_exc()}", ""
    elif text and text.strip():
        try:
            records = get_parser().parse_text(text)
        except Exception as e:
            return f"ОШИБКА: {e}\n\n{traceback.format_exc()}", ""
    else:
        return "Введите данные или загрузите файл.", ""

    records_json = json.dumps(records, ensure_ascii=False, indent=2)
    db_msg = ""
    if write_to_db:
        try:
            stats = get_writer().write(records)
            db_msg = (
                f"Записано в БД: {stats['inserted']}    "
                f"Пропущено (дубликаты): {stats['skipped']}    "
                f"Ошибок: {len(stats['errors'])}\n"
                + ("\n".join(map(str, stats['errors'][:10])) if stats['errors'] else "")
            )
        except Exception as e:
            db_msg = f"ОШИБКА БД: {e}"
    return records_json, db_msg


# ── Вкладка 2: Обучающие примеры ─────────────────────────────────────────────

def ui_add_example(raw_input: str, parsed_output_json: str):
    if not raw_input.strip() or not parsed_output_json.strip():
        return "❗ Заполните оба поля.", example_count()
    try:
        parsed = json.loads(parsed_output_json)
        if not isinstance(parsed, list):
            return "❗ Результат должен быть массивом JSON.", example_count()
        ok, errs = validate_records(parsed)
        if not ok:
            return "❗ Ошибки в данных:\n" + "\n".join(errs[:5]), example_count()
        path = add_example(raw_input, parsed)
        return f"✅ Пример сохранён в {path.name}. Всего примеров: {example_count()}", example_count()
    except json.JSONDecodeError as e:
        return f"❗ Невалидный JSON: {e}", example_count()


def ui_auto_label(raw_input: str):
    if not raw_input.strip():
        return ""
    try:
        records = get_parser().parse_text(raw_input)
        return json.dumps(records, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"ОШИБКА: {e}"


# ── Вкладка 3: Дообучение ────────────────────────────────────────────────────

_training_thread: threading.Thread | None = None
_training_log = StringIO()


def ui_start_training():
    global _training_thread, _training_log
    if _training_thread is not None and _training_thread.is_alive():
        return "⚠ Обучение уже идёт."
    if example_count() < 5:
        return f"❗ Нужно минимум 5 примеров (сейчас {example_count()}). Соберите примеры на вкладке 'Обучающие примеры'."

    _training_log = StringIO()

    def _run():
        from training.finetune import run_training
        sys.stdout = _training_log
        try:
            run_training()
        except Exception:
            _training_log.write(traceback.format_exc())
        finally:
            sys.stdout = sys.__stdout__

    _training_thread = threading.Thread(target=_run, daemon=True)
    _training_thread.start()
    return f"▶ Обучение запущено. В работе {example_count()} примеров."


def ui_training_log():
    return _training_log.getvalue() or "(пока пусто)"


# ── Вкладка 4: Настройки ─────────────────────────────────────────────────────

def ui_db_status():
    try:
        w = get_writer()
        counts = w.row_counts()
        lines = [f"{name:40s} {n}" for name, n in sorted(counts.items())]
        total = sum(counts.values())
        return f"Всего записей в БД: {total}\n\n" + "\n".join(lines)
    except Exception as e:
        return f"ОШИБКА: {e}"


def ui_show_config(name: str) -> str:
    path = ROOT / "config" / name
    if not path.exists():
        return f"(файл не найден: {path})"
    return path.read_text(encoding="utf-8")


def ui_save_config(name: str, contents: str):
    path = ROOT / "config" / name
    try:
        yaml.safe_load(contents)
        path.write_text(contents, encoding="utf-8")
        return f"✅ {name} сохранён."
    except yaml.YAMLError as e:
        return f"❗ Невалидный YAML: {e}"


def ui_test_llm():
    try:
        ok = LLMClient().test_connection()
        return "✅ LLM отвечает." if ok else "❗ LLM не отвечает. Проверьте что Ollama запущена."
    except Exception as e:
        return f"❗ {e}"


# ── Layout ────────────────────────────────────────────────────────────────────

INTRO = """
# LLM ETL — конфигуратор парсера данных

Программа берёт «сырые» файлы (выгрузки из 1С, чеки ОФД, накладные ЭДО,
прайс-листы поставщиков и т.п.) и через локальную нейросеть раскладывает
их по таблицам базы данных.

**С чего начать:**
1. Откройте вкладку **«🔍 Разбор данных»** — вставьте пример файла → нажмите «Разобрать».
2. Если разобрало неправильно — поправьте JSON на вкладке **«📝 Обучающие примеры»** и сохраните.
3. Когда наберётся 20-50 примеров — соберите архив (кнопка `УПАКОВАТЬ_ДЛЯ_ОБУЧЕНИЯ.bat`),
   перенесите на GPU-машину и обучите модель.
4. Привезите обученную модель обратно (кнопка `ИМПОРТ_ОБУЧЕННОЙ_МОДЕЛИ.bat`) — точность станет выше.
"""

with gr.Blocks(title="LLM ETL — конфигуратор", theme=gr.themes.Soft()) as app:
    gr.Markdown(INTRO)

    # ── Вкладка 1 ────────────────────────────────────────────────────────────
    with gr.Tab("🔍 Разбор данных"):
        gr.Markdown(
            "**Что делает эта вкладка:** даёт нейросети сырые данные → она "
            "распознаёт формат и раскладывает по таблицам БД.\n\n"
            "Можно вставить текст или загрузить файл (XML, JSON, CSV, бинарный, что угодно)."
        )
        with gr.Row():
            with gr.Column():
                raw_in = gr.Textbox(
                    label="Сырые данные (текст)",
                    lines=12,
                    placeholder="Вставьте сюда содержимое файла…\n\nПример: чек ОФД, ТТН ЕГАИС, выгрузка 1С",
                )
                file_in = gr.File(label="...или загрузите файл", file_count="single")
                to_db = gr.Checkbox(
                    label="Сразу записать результат в базу данных",
                    value=False,
                    info="Без галочки — только показать результат. С галочкой — сохранить в SQLite.",
                )
                parse_btn = gr.Button("▶ Разобрать", variant="primary", size="lg")
            with gr.Column():
                gr.Markdown("**Результат — записи для базы данных:**")
                out_json = gr.Code(label="", language="json", lines=20)
                db_status = gr.Textbox(label="Результат записи в БД", lines=4)
        parse_btn.click(ui_parse, [raw_in, file_in, to_db], [out_json, db_status])

    # ── Вкладка 2 ────────────────────────────────────────────────────────────
    with gr.Tab("📝 Обучающие примеры"):
        gr.Markdown(
            "**Зачем эта вкладка:** здесь вы «показываете» нейросети как ПРАВИЛЬНО "
            "разбирать определённый формат данных. Чем больше пар (вход → правильный ответ) — "
            "тем точнее будет работать модель после обучения.\n\n"
            "**Как:**\n"
            "1. Слева вставьте кусок реальных данных (например, фрагмент выгрузки 1С).\n"
            "2. Нажмите **«Авторазметка»** — нейросеть сама предложит черновой ответ.\n"
            "3. Справа исправьте JSON если есть ошибки.\n"
            "4. Нажмите **«Сохранить пример»**.\n\n"
            "Когда наберёте 20+ примеров — переходите к обучению (кнопка `УПАКОВАТЬ_ДЛЯ_ОБУЧЕНИЯ.bat`)."
        )
        with gr.Row():
            with gr.Column():
                ex_in = gr.Textbox(
                    label="Сырой ввод (данные для разбора)",
                    lines=14,
                    placeholder="Вставьте сюда фрагмент данных формата, которому хотите научить модель…",
                )
                auto_btn = gr.Button("🤖 Авторазметка нейросетью", variant="secondary")
            with gr.Column():
                ex_out = gr.Code(
                    label="Правильный ответ (исправьте здесь если нужно)",
                    language="json",
                    lines=14,
                )
        save_btn = gr.Button("💾 Сохранить пример", variant="primary", size="lg")
        save_status = gr.Textbox(label="Статус сохранения", lines=2)
        ex_count = gr.Number(label="Всего сохранено примеров", value=example_count(), interactive=False)
        auto_btn.click(ui_auto_label, [ex_in], [ex_out])
        save_btn.click(ui_add_example, [ex_in, ex_out], [save_status, ex_count])

    # ── Вкладка 3 ────────────────────────────────────────────────────────────
    with gr.Tab("🧠 Дообучение (только на GPU-машине!)"):
        gr.Markdown(
            "⚠ **Эта вкладка работает только на машине с видеокартой NVIDIA** "
            "(в идеале RTX 30/40/50-серия). На обычном офисном компьютере обучение НЕ запустится.\n\n"
            "**Если у вас есть отдельная GPU-машина:**\n"
            "Не запускайте обучение через эту вкладку, а вместо этого:\n"
            "1. На текущем (этом) компьютере запустите файл `УПАКОВАТЬ_ДЛЯ_ОБУЧЕНИЯ.bat` — "
            "получите архив `llm_etl_for_training.zip`.\n"
            "2. Перенесите архив на GPU-машину, распакуйте, откройте файл `ПРОЧТИ_МЕНЯ.txt` "
            "внутри — там понятная инструкция.\n"
            "3. После обучения привезите обратно два файла (.gguf и Modelfile) и запустите "
            "`ИМПОРТ_ОБУЧЕННОЙ_МОДЕЛИ.bat`.\n\n"
            "Кнопка ниже — для тех случаев когда вы открыли эту программу прямо на GPU-машине."
        )
        train_btn = gr.Button("▶ Запустить обучение здесь", variant="stop")
        train_status = gr.Textbox(label="Статус", lines=2)
        log_box = gr.Code(label="Лог обучения (обновляется кнопкой)", language="shell", lines=20)
        refresh_btn = gr.Button("🔄 Обновить лог")
        train_btn.click(ui_start_training, [], [train_status])
        refresh_btn.click(ui_training_log, [], [log_box])

    # ── Вкладка 4 ────────────────────────────────────────────────────────────
    with gr.Tab("⚙ Настройки и статус"):
        gr.Markdown("### Состояние системы")
        with gr.Row():
            with gr.Column():
                gr.Markdown("**Сколько записей в базе данных:**")
                db_box = gr.Code(label="", lines=15)
                gr.Button("Обновить").click(ui_db_status, [], [db_box])
            with gr.Column():
                gr.Markdown("**Связь с нейросетью:**")
                llm_status = gr.Textbox(label="", lines=2)
                gr.Button("Проверить связь с Ollama").click(ui_test_llm, [], [llm_status])

        gr.Markdown(
            "### Расширенные настройки\n"
            "⚠ Меняйте только если понимаете что делаете. Неверная правка может сломать программу."
        )
        cfg_selector = gr.Dropdown(
            ["model.yaml", "schema.yaml", "training.yaml"],
            value="model.yaml",
            label="Файл настроек",
        )
        cfg_box = gr.Code(label="Содержимое", language="yaml", lines=25, value=ui_show_config("model.yaml"))
        cfg_status = gr.Textbox(label="Статус сохранения")
        cfg_selector.change(ui_show_config, [cfg_selector], [cfg_box])
        gr.Button("💾 Сохранить файл настроек").click(ui_save_config, [cfg_selector, cfg_box], [cfg_status])


def launch(share: bool = False, port: int = 7860):
    app.launch(server_name="127.0.0.1", server_port=port, share=share, inbrowser=False)


if __name__ == "__main__":
    launch()
