#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ядро упаковщика архивов для S.T.A.L.K.E.R. ОП-2.1 (.dbz)
Использует xrCore.dll, расположенный в одной папке со скриптом/программой.
"""

import ctypes
import os
import struct
import sys
import zlib
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple


DB_CHUNK_DATA_STD = 0x00000000
DB_CHUNK_HEADER_STD = 0x00000001
DB_CHUNK_OP_2_1_DATA = 0x00000309
DB_CHUNK_OP_2_1_HEADER = 0x0000030A

# Точный список расширений, которые шифруются в архивах ОП-2.1 (извлечён из xrCore.dll)
OP21_ENCRYPTED_EXTS = {
    ".ltx",
    ".xml",
    ".script",
    ".cfg",
    ".ps",
    ".s",
    ".vs",
    "._s",
    ".h",
}

def should_encrypt_op21_file(rel_path: str) -> bool:
    """
    Точная копия логики strrchr(fname, '.') из xrCore.dll ОП-2.1:
    в отличие от os.path.splitext('.s'), корректно распознаёт файлы вида 'shaders\\r1\\.s'.
    """
    base_name = os.path.basename(rel_path)
    dot_idx = base_name.rfind(".")
    if dot_idx == -1:
        return False
    ext = base_name[dot_idx:]  # регистрозависимо, как memcmp в xrCore.dll
    return ext in OP21_ENCRYPTED_EXTS


def get_app_dir() -> str:
    """Возвращает папку, в которой находится скрипт или собранный .exe."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_volume_suffix(idx: int) -> str:
    """Возвращает символ номера тома: 0..9, затем a..z."""
    if idx < 10:
        return chr(ord("0") + idx)
    if idx < 36:
        return chr(ord("a") + idx - 10)
    raise ValueError("Превышено максимальное число томов (36: 0-9, a-z).")


def make_volume_path(out_file: str, vol_idx: int, total_vols: int) -> str:
    """
    Формирует имя тома по стандарту X-Ray:
      gamedata.db  -> gamedata.db0, gamedata.db1 ... gamedata.dba, gamedata.dbb
      gamedata.dbz -> gamedata.dbz0, gamedata.dbz1 ... gamedata.dbza, gamedata.dbzb
    """
    if total_vols == 1:
        return out_file

    suffix = get_volume_suffix(vol_idx)
    lower = out_file.lower()

    # Если пользователь уже указал имя вида gamedata.db0 или gamedata.dbz0, убираем '0' на конце
    if lower.endswith(".db0") or lower.endswith(".dbz0"):
        base = out_file[:-1]
        return f"{base}{suffix}"

    if lower.endswith(".db") or lower.endswith(".dbz"):
        return f"{out_file}{suffix}"

    root_name, ext = os.path.splitext(out_file)
    return f"{root_name}{ext}{suffix}"


class NativeXRScrambler:
    """Обёртка над нативным xray_re::xr_scrambler(config=2) из локального xrCore.dll."""

    def __init__(self):
        if struct.calcsize("P") * 8 != 32:
            raise RuntimeError(
                "Требуется 32-битный Python (запускайте через: py -3-32 OP_2_1_pack_gui.py)."
            )

        app_dir = get_app_dir()
        dll_path = os.path.join(app_dir, "xrCore.dll")
        if not os.path.isfile(dll_path):
            raise FileNotFoundError(
                f"Не найден файл xrCore.dll рядом со скриптом:\n{dll_path}"
            )

        os.add_dll_directory(app_dir)
        os.environ["PATH"] = app_dir + os.pathsep + os.environ.get("PATH", "")
        self.dll = ctypes.WinDLL(dll_path)

        VirtualAlloc = ctypes.windll.kernel32.VirtualAlloc
        VirtualAlloc.restype = ctypes.c_void_p

        def make_thiscall(fn_addr: int, argtypes, restype=None):
            code = b"\x58\x59\x50\xB8" + fn_addr.to_bytes(4, "little") + b"\xFF\xE0"
            buf = VirtualAlloc(0, len(code), 0x3000, 0x40)
            ctypes.memmove(buf, code, len(code))
            return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(buf)

        init_addr = ctypes.cast(
            getattr(self.dll, "?init@xr_scrambler@xray_re@@QAEXW4cipher_config@12@@Z"),
            ctypes.c_void_p,
        ).value
        enc_addr = ctypes.cast(
            getattr(self.dll, "?encrypt@xr_scrambler@xray_re@@QBEXPAEPBEI@Z"),
            ctypes.c_void_p,
        ).value

        self._init_fn = make_thiscall(init_addr, [ctypes.c_int])
        self._enc_fn = make_thiscall(
            enc_addr, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        )
        self._obj = ctypes.create_string_buffer(1024)
        self._init_fn(self._obj, 2)

    def encrypt_bytes(self, data: bytes) -> bytes:
        if not data:
            return b""
        enc_buf = ctypes.create_string_buffer(len(data))
        self._enc_fn(self._obj, enc_buf, data, len(data))
        return enc_buf.raw


@dataclass
class ArchiveEntry:
    rel_path: str
    full_path: str
    real_size: int
    comp_size: int
    crc32: int
    offset: int


def collect_files(src_dir: str) -> List[Tuple[str, str]]:
    result: List[Tuple[str, str]] = []
    base = os.path.abspath(src_dir)
    for root, _, files in os.walk(base):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base).replace("/", "\\")
            result.append((rel, full))
    result.sort(key=lambda pair: next(iter(pair)).lower())
    return result


def build_header_table(entries: List[ArchiveEntry]) -> bytes:
    buf = bytearray()
    for e in entries:
        path_bytes = e.rel_path.encode("cp1251", errors="replace")
        entry_size = 16 + len(path_bytes)
        buf.extend(struct.pack("<HIII", entry_size, e.real_size, e.comp_size, e.crc32))
        buf.extend(path_bytes)
        buf.extend(struct.pack("<I", e.offset))
    return bytes(buf)


def pack_op21_archive(
    src_dir: str,
    out_file: str,
    mode: str = "op21_dbz",
    max_volume_mb: int = 640,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_flag: Optional[Callable[[], bool]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> List[str]:
    files = collect_files(src_dir)
    if not files:
        raise ValueError("Выбранная папка пуста — нет файлов для упаковки.")

    scrambler = NativeXRScrambler() if mode == "op21_dbz" else None

    total_files = len(files)
    max_bytes = max_volume_mb * 1024 * 1024 if max_volume_mb > 0 else 0
    created_files: List[str] = []

    volumes: List[List[Tuple[str, str]]] = []
    cur_vol: List[Tuple[str, str]] = []
    cur_size = 0
    for rel, full in files:
        fsize = os.path.getsize(full)
        if max_bytes > 0 and cur_vol and (cur_size + fsize > max_bytes):
            volumes.append(cur_vol)
            cur_vol = []
            cur_size = 0
        cur_vol.append((rel, full))
        cur_size += fsize
    if cur_vol:
        volumes.append(cur_vol)

    total_vols = len(volumes)
    processed_count = 0

    for vol_idx, vol_files in enumerate(volumes):
        if cancel_flag and cancel_flag():
            break

        vol_path = make_volume_path(out_file, vol_idx, total_vols)
        os.makedirs(os.path.dirname(os.path.abspath(vol_path)), exist_ok=True)
        entries: List[ArchiveEntry] = []

        with open(vol_path, "wb") as f:
            data_chunk_id = (
                DB_CHUNK_OP_2_1_DATA if mode == "op21_dbz" else DB_CHUNK_DATA_STD
            )
            f.write(struct.pack("<II", data_chunk_id, 0))
            data_start = f.tell()

            for rel, full in vol_files:
                if cancel_flag and cancel_flag():
                    break

                offset = f.tell()
                with open(full, "rb") as src_f:
                    raw_data = src_f.read()

                real_size = len(raw_data)
                crc = zlib.crc32(raw_data) & 0xFFFFFFFF

                if mode == "op21_dbz" and scrambler is not None and should_encrypt_op21_file(rel):
                    written_data = scrambler.encrypt_bytes(raw_data)
                else:
                    written_data = raw_data

                f.write(written_data)

                entries.append(
                    ArchiveEntry(
                        rel_path=rel,
                        full_path=full,
                        real_size=real_size,
                        comp_size=real_size,
                        crc32=crc,
                        offset=offset,
                    )
                )

                processed_count += 1
                if progress_cb:
                    progress_cb(processed_count, total_files, rel)

            data_end = f.tell()
            data_payload_size = data_end - data_start

            f.seek(0)
            f.write(struct.pack("<II", data_chunk_id, data_payload_size))
            f.seek(data_end)

            raw_table = build_header_table(entries)

            if mode == "op21_dbz":
                f.write(struct.pack("<II", DB_CHUNK_OP_2_1_HEADER, len(raw_table)))
                f.write(raw_table)
            else:
                f.write(struct.pack("<II", DB_CHUNK_HEADER_STD, len(raw_table)))
                f.write(raw_table)

        created_files.append(vol_path)

    return created_files
