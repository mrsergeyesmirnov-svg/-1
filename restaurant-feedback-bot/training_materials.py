"""
Обучающие материалы точки: папки и файлы (Telegram file_id).
Доступ сотруднику — после хотя бы одного отклика на смену (привязка uid → chat_id).
"""
from __future__ import annotations

import secrets
from datetime import datetime
from html import escape
from typing import Any


def record_staff_link(data: dict[str, Any], user_id: int, chat_id: int) -> None:
    links = data.setdefault("staff_restaurant_links", {})
    if not isinstance(links, dict):
        links = {}
        data["staff_restaurant_links"] = links
    links[str(user_id)] = str(chat_id)


def staff_chat_id(data: dict[str, Any], user_id: int) -> int | None:
    raw = (data.get("staff_restaurant_links") or {}).get(str(user_id))
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _root(rec: dict[str, Any]) -> dict[str, Any]:
    raw = rec.get("training_materials")
    if not isinstance(raw, dict):
        raw = {"folders": [], "files": []}
        rec["training_materials"] = raw
    raw.setdefault("folders", [])
    raw.setdefault("files", [])
    return raw


def list_folders(rec: dict[str, Any]) -> list[dict[str, Any]]:
    root = _root(rec)
    folders = root.get("folders")
    if not isinstance(folders, list):
        return []
    out = [f for f in folders if isinstance(f, dict) and f.get("id")]
    return sorted(out, key=lambda x: int(x.get("order", 0)))


def list_files(rec: dict[str, Any], folder_id: str | None = None) -> list[dict[str, Any]]:
    root = _root(rec)
    files = root.get("files")
    if not isinstance(files, list):
        return []
    out: list[dict[str, Any]] = []
    for f in files:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        if folder_id is None or str(f.get("folder_id")) == str(folder_id):
            out.append(f)
    return sorted(out, key=lambda x: str(x.get("title", "")).lower())


def add_folder(rec: dict[str, Any], name: str) -> tuple[bool, str | None, str | None]:
    name = name.strip()
    if len(name) < 2 or len(name) > 60:
        return False, "Название папки: 2–60 символов.", None
    folders = list_folders(rec)
    if len(folders) >= 15:
        return False, "Не больше 15 папок.", None
    fid = "tf_" + secrets.token_hex(3)
    folders.append({"id": fid, "name": name, "order": len(folders)})
    _root(rec)["folders"] = folders
    return True, None, fid


def delete_folder(rec: dict[str, Any], folder_id: str) -> bool:
    root = _root(rec)
    folders = [f for f in list_folders(rec) if f["id"] != folder_id]
    files = [f for f in list_files(rec) if str(f.get("folder_id")) != folder_id]
    root["folders"] = folders
    root["files"] = files
    return True


def add_file(
    rec: dict[str, Any],
    *,
    folder_id: str,
    title: str,
    file_id: str,
    file_type: str,
    by_uid: int,
) -> tuple[bool, str | None]:
    title = title.strip() or "Материал"
    if len(title) > 120:
        title = title[:120]
    if not any(f["id"] == folder_id for f in list_folders(rec)):
        return False, "Папка не найдена."
    files = list_files(rec)
    if len(files) >= 50:
        return False, "Не больше 50 файлов на точку."
    files.append(
        {
            "id": "tm_" + secrets.token_hex(4),
            "folder_id": folder_id,
            "title": title,
            "file_id": file_id,
            "file_type": file_type,
            "by_uid": by_uid,
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    _root(rec)["files"] = files
    return True, None


def delete_file(rec: dict[str, Any], file_id: str) -> bool:
    root = _root(rec)
    root["files"] = [f for f in list_files(rec) if f["id"] != file_id]
    return True


def format_staff_menu(rec: dict[str, Any], chat_title: str) -> str:
    folders = list_folders(rec)
    if not folders:
        return (
            f"<b>📚 Обучение</b> · {escape(chat_title)}\n\n"
            "<i>Материалы пока не добавлены.</i>"
        )
    lines = [
        f"<b>📚 Обучение</b> · {escape(chat_title)}",
        "",
        "Выберите папку:",
    ]
    for f in folders:
        n = len(list_files(rec, f["id"]))
        lines.append(f"• {escape(f['name'])} — <b>{n}</b> файлов")
    return "\n".join(lines)


def format_folder_files(rec: dict[str, Any], folder_id: str, chat_title: str) -> str:
    folder = next((f for f in list_folders(rec) if f["id"] == folder_id), None)
    if not folder:
        return "Папка не найдена."
    files = list_files(rec, folder_id)
    lines = [
        f"<b>{escape(folder['name'])}</b> · {escape(chat_title)}",
        "",
    ]
    if files:
        for i, f in enumerate(files, 1):
            lines.append(f"{i}. {escape(f.get('title', 'Файл'))}")
    else:
        lines.append("<i>В папке пока нет файлов.</i>")
    return "\n".join(lines)


def format_manager_menu(rec: dict[str, Any], chat_title: str) -> str:
    folders = list_folders(rec)
    files_n = len(list_files(rec))
    return (
        f"<b>📚 Материалы</b> · {escape(chat_title)}\n\n"
        f"Папок: <b>{len(folders)}</b> · файлов: <b>{files_n}</b>\n\n"
        "Сотрудники, которые хотя бы раз отметили смену, видят материалы "
        "своей точки в кнопке «📚 Обучение»."
    )


def staff_folders_keyboard(chat_id: int, rec: dict[str, Any]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    for f in list_folders(rec):
        short = f["name"][:28] + ("…" if len(f["name"]) > 28 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📁 {short}",
                    callback_data=f"tr:sf:{cid}:{f['id']}"[:64],
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def staff_files_keyboard(chat_id: int, folder_id: str, rec: dict[str, Any]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = []
    for f in list_files(rec, folder_id):
        short = f.get("title", "Файл")[:30]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📄 {short}",
                    callback_data=f"tr:fd:{cid}:{f['id']}"[:64],
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="← Папки", callback_data=f"tr:sm:{cid}"[:64])]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def manager_menu_keyboard(chat_id: int, rec: dict[str, Any]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    rows: list[list] = [
        [InlineKeyboardButton(text="➕ Папка", callback_data=f"tr:mfadd:{cid}"[:64])],
    ]
    for f in list_folders(rec):
        short = f["name"][:26] + ("…" if len(f["name"]) > 26 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📁 {short}",
                    callback_data=f"tr:mfold:{cid}:{f['id']}"[:64],
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def manager_folder_keyboard(chat_id: int, folder_id: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = str(chat_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Загрузить файл",
                    callback_data=f"tr:mup:{cid}:{folder_id}"[:64],
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить папку",
                    callback_data=f"tr:mdel:{cid}:{folder_id}"[:64],
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data=f"tr:mm:{cid}"[:64])],
        ]
    )
