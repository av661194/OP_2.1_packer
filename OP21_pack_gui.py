#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Графический упаковщик архивов для S.T.A.L.K.E.R. ОП-2.1 (.db)
Запуск без аргументов открывает окно tkinter; с аргументами работает в CLI-режиме.
"""

import argparse
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from OP21_packer_core import pack_op21_archive


MIN_VOL_MB = 32
MAX_VOL_MB = 1920
DEFAULT_VOL_MB = 640


def parse_volume_mb(raw_value: str) -> int:
    """Возвращает размер тома в МБ: от 32 до 1920, иначе 640 МБ по умолчанию."""
    text = raw_value.strip()
    if not text:
        return DEFAULT_VOL_MB
    try:
        val = int(text)
    except ValueError:
        return DEFAULT_VOL_MB
    if val < MIN_VOL_MB or val > MAX_VOL_MB:
        return DEFAULT_VOL_MB
    return val


class OP21PackerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Упаковщик архивов S.T.A.L.K.E.R. ОП-2.1 (.db)")
        self.root.geometry("720x460")
        self.root.minsize(640, 400)

        self.src_var = tk.StringVar()
        self.dst_var = tk.StringVar()
        self.split_var = tk.StringVar(value=str(DEFAULT_VOL_MB))

        self.worker_thread = None
        self.cancel_requested = False
        self.msg_queue: queue.Queue = queue.Queue()

        self._build_ui()
        self.root.after(100, self._poll_queue)

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # --- Блок выбора путей и размера тома ---
        paths_frame = ttk.LabelFrame(self.root, text="Пути упаковки", padding=10)
        paths_frame.pack(fill="x", **pad)
        paths_frame.columnconfigure(1, weight=1)

        ttk.Label(paths_frame, text="Папка с файлами (gamedata):").grid(
            row=0, column=0, sticky="w", pady=4
        )
        ttk.Entry(paths_frame, textvariable=self.src_var).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4
        )
        ttk.Button(paths_frame, text="Обзор...", command=self._browse_src).grid(
            row=0, column=2, pady=4
        )

        ttk.Label(paths_frame, text="Выходной файл архива:").grid(
            row=1, column=0, sticky="w", pady=4
        )
        ttk.Entry(paths_frame, textvariable=self.dst_var).grid(
            row=1, column=1, sticky="ew", padx=6, pady=4
        )
        ttk.Button(paths_frame, text="Сохранить как...", command=self._browse_dst).grid(
            row=1, column=2, pady=4
        )

        vol_row = ttk.Frame(paths_frame)
        vol_row.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 2))
        ttk.Label(
            vol_row,
            text="Размер тома, МБ (от 32 до 1920) если пусто или вне пределов - 640 МБ по умолчанию:",
        ).pack(side="left")
        ttk.Entry(vol_row, textvariable=self.split_var, width=8).pack(
            side="left", padx=8
        )

        # --- Прогресс и кнопки ---
        ctrl_frame = ttk.Frame(self.root)
        ctrl_frame.pack(fill="x", **pad)

        self.progress = ttk.Progressbar(ctrl_frame, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 6))

        btn_row = ttk.Frame(ctrl_frame)
        btn_row.pack(fill="x")

        self.status_lbl = ttk.Label(btn_row, text="Готов к работе")
        self.status_lbl.pack(side="left")

        self.cancel_btn = ttk.Button(
            btn_row, text="Отмена", command=self._cancel_pack, state="disabled"
        )
        self.cancel_btn.pack(side="right", padx=(6, 0))

        self.start_btn = ttk.Button(
            btn_row, text="Упаковать архив", command=self._start_pack
        )
        self.start_btn.pack(side="right")

        # --- Лог ---
        log_frame = ttk.LabelFrame(self.root, text="Журнал упаковки", padding=6)
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_box = scrolledtext.ScrolledText(
            log_frame, height=12, state="disabled", font=("Consolas", 9)
        )
        self.log_box.pack(fill="both", expand=True)

    def _log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _browse_src(self):
        folder = filedialog.askdirectory(title="Выберите папку gamedata для упаковки")
        if folder:
            self.src_var.set(os.path.normpath(folder))
            if not self.dst_var.get().strip():
                parent = os.path.dirname(os.path.normpath(folder))
                self.dst_var.set(os.path.join(parent, "gamedata.dbz"))

    def _browse_dst(self):
        filetypes = [
            ("X-Ray archives", "*.db*"),
            ("Все файлы (*.*)", "*.*"),
        ]
        path = filedialog.asksaveasfilename(
            title="Сохранить архив как",
            defaultextension=".dbz",
            filetypes=filetypes,
        )
        if path:
            self.dst_var.set(os.path.normpath(path))

    def _start_pack(self):
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Ошибка", "Укажите существующую исходную папку!")
            return
        if not dst:
            messagebox.showerror("Ошибка", "Укажите имя выходного файла архива!")
            return

        max_mb = parse_volume_mb(self.split_var.get())
        self.split_var.set(str(max_mb))

        self.cancel_requested = False
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress["value"] = 0

        self._log(f"=== Старт упаковки: {src} -> {dst} ===")
        self._log(f"Размер тома: {max_mb} МБ")

        self.worker_thread = threading.Thread(
            target=self._worker,
            args=(src, dst, max_mb),
            daemon=True,
        )
        self.worker_thread.start()

    def _cancel_pack(self):
        self.cancel_requested = True
        self.cancel_btn.configure(state="disabled")
        self._log("[!] Запрошена отмена операции...")

    def _worker(self, src: str, dst: str, max_mb: int):
        t0 = time.time()
        try:
            def on_prog(cur: int, total: int, rel_path: str):
                self.msg_queue.put(("progress", (cur, total, rel_path)))

            created = pack_op21_archive(
                src_dir=src,
                out_file=dst,
                mode="op21_dbz",
                max_volume_mb=max_mb,
                progress_cb=on_prog,
                cancel_flag=lambda: self.cancel_requested,
                log_cb=self._log,
            )
            elapsed = time.time() - t0
            if self.cancel_requested:
                self.msg_queue.put(("cancelled", elapsed))
            else:
                self.msg_queue.put(("done", (created, elapsed)))
        except Exception as exc:
            self.msg_queue.put(("error", str(exc)))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "progress":
                    cur, total, rel = payload
                    pct = (cur / total * 100.0) if total else 0
                    self.progress["value"] = pct
                    self.status_lbl.configure(text=f"Файл {cur} из {total}: {rel}")
                elif kind == "done":
                    created, elapsed = payload
                    self.progress["value"] = 100
                    self.status_lbl.configure(
                        text=f"Готово за {elapsed:.2f} сек. Создано файлов: {len(created)}"
                    )
                    for p in created:
                        sz_mb = os.path.getsize(p) / (1024 * 1024)
                        self._log(f"[+] Создан архив: {p} ({sz_mb:.2f} МБ)")
                    self._reset_buttons()
                elif kind == "cancelled":
                    self.status_lbl.configure(text="Операция отменена пользователем")
                    self._log("[!] Упаковка прервана.")
                    self._reset_buttons()
                elif kind == "error":
                    self.status_lbl.configure(text="Ошибка упаковки")
                    self._log(f"[ОШИБКА] {payload}")
                    self._reset_buttons()
                    messagebox.showerror("Ошибка", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _reset_buttons(self):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")


def run_cli():
    parser = argparse.ArgumentParser(
        description="Упаковщик архивов для S.T.A.L.K.E.R. ОП-2.1 (.dbz)"
    )
    parser.add_argument("src", help="Путь к исходной папке gamedata")
    parser.add_argument("dst", help="Путь к выходному файлу архива (.dbz)")
    parser.add_argument(
        "--split",
        type=str,
        default=str(DEFAULT_VOL_MB),
        help="Размер тома в МБ (от 32 до 1920, по умолчанию 640)",
    )
    args = parser.parse_args()
    max_mb = parse_volume_mb(args.split)

    created = pack_op21_archive(
        src_dir=args.src,
        out_file=args.dst,
        mode="op21_dbz",
        max_volume_mb=max_mb,
        progress_cb=lambda c, t, r: print(f"[{c}/{t}] {r}"),
    )
    for p in created:
        print(f"Готово: {p}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli()
    else:
        root = tk.Tk()
        app = OP21PackerGUI(root)
        root.mainloop()
