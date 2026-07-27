#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AEGIS AGENT
======================
Kod odaklı, dosya kontrolüne sahip, çoklu API anahtarlı (kota rotasyonlu) bir
Gemini sohbet ajanı. Tek bir masaüstü GUI uygulaması olarak çalışır (terminal
modu yok, tek dosya).

Kurulum (temel):
    pip install google-generativeai customtkinter pillow opencv-python numpy

    (Tarayıcı kontrolü opsiyoneldir: pip install playwright && playwright install)
    (Panodan resim yapıştırma özelliği Pillow'un ImageGrab modülünü kullanır;
     bu modül en iyi Windows ve macOS'ta çalışır.)
    (Canlı tarayıcı izleme + sanal imleç overlay için opencv-python ve numpy gerekir;
     kurulu değilse tarayıcı yine de çalışır, sadece canlı görüntü penceresi açılmaz.)

Kurulum (ekstra özellikler - hepsi opsiyonel, kurulu olmayan özellik sessizce devre dışı kalır):
    pip install SpeechRecognition pyttsx3 pyaudio     # sesli komut + TTS yanıt (altyapı hazır)
    pip install watchdog                              # dosya izleme (altyapı hazır)
    (Git entegrasyonu ve otomatik test/lint için sistemde 'git' ve
     istersen 'pytest'/'flake8' kurulu olması yeterli, ek pip paketi gerekmez)
    (PR açma için GitHub CLI: https://cli.github.com/)

Çalıştırma:
    python gemini_agent.py

Özellikler:
    - Kod odaklı sistem promtu (dosya oluşturma/düzenleme/silme, terminal komutu çalıştırma)
    - Sınırsız sayıda API anahtarı arasında otomatik kota rotasyonu
    - Kotası biten anahtarlar için arka planda saatlik "test call" ile otomatik canlanma
    - Ayarlar penceresinde anahtar/model/dil/tarayıcı modu tek merkezden yönetimi
    - Dosya ekleme (görsel, PDF, kod, metin, ses, video...) sürükle-bırak yerine dosya seçici ile
    - Gerçek zamanlı tarayıcı izleme + sanal imleç, kendi tarayıcı profilini kullanabilme
    - Git entegrasyonu (agent kendi isteğiyle commit/branch/diff/PR açabilir)
    - Anahtar başına token/maliyet takibi, geçmiş sohbetlerden hafıza araması,
      paralel görev çalıştırma, plugin sistemi (~/.gemini_agent/plugins/) — backend
      hazır, GUI'den tetiklenmesi ileride eklenebilir.
"""

import os
import re
import math
import sys
import json
import time
import queue
import shutil
import signal
import atexit
import base64
import mimetypes
import tempfile
import importlib.util
import threading
import subprocess
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# Klavye dinleme (Enter: iptal, E: düzenle) için platforma özgü modüller
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform.startswith("darwin")
IS_LINUX = sys.platform.startswith("linux")
if IS_WINDOWS:
    import msvcrt
else:
    try:
        import termios
        import tty
        import select
        KEY_LISTEN_AVAILABLE = True
    except ImportError:
        KEY_LISTEN_AVAILABLE = False

try:
    import google.generativeai as genai
    from google.generativeai.types import HarmCategory, HarmBlockThreshold
except ImportError:
    print("HATA: 'google-generativeai' paketi kurulu değil.")
    print("Kurmak için: pip install google-generativeai rich pillow")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.prompt import Prompt
    from rich.live import Live
    from rich.text import Text
    from rich.rule import Rule
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

# Görsel işleme (pano yapıştırma + dosya gezgini) - opsiyonel bağımlılık
try:
    from PIL import Image, ImageGrab
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Canlı tarayıcı izleme + sanal imleç overlay - opsiyonel bağımlılık
# Kurulum: pip install opencv-python numpy
try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

# Sesli komut + TTS yanıt - opsiyonel bağımlılık
# Kurulum: pip install SpeechRecognition pyttsx3 pyaudio
try:
    import speech_recognition as sr
    SPEECH_RECOGNITION_AVAILABLE = True
except ImportError:
    SPEECH_RECOGNITION_AVAILABLE = False
try:
    import pyttsx3
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False

# Dosya izleme modu - opsiyonel bağımlılık (yoksa basit polling'e düşülür)
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

# GUI (CustomTkinter) - opsiyonel bağımlılık; kurulu değilse net bir hata ile çıkılır
# çünkü bu dosya artık SADECE GUI olarak çalışıyor (terminal modu kaldırıldı).
try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import filedialog, messagebox
    CUSTOMTKINTER_AVAILABLE = True
except ImportError:
    CUSTOMTKINTER_AVAILABLE = False

# --------------------------------------------------------------------------
# Sabitler / Yollar
# --------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".gemini_agent"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
SESSIONS_DIR = CONFIG_DIR / "sessions"
UPLOADS_DIR = CONFIG_DIR / "uploads"
TEMP_DIR = CONFIG_DIR / "temp"
PLUGINS_DIR = CONFIG_DIR / "plugins"
USAGE_FILE = CONFIG_DIR / "usage.json"
BROWSER_PROFILE_DIR = CONFIG_DIR / "browser_profile"
DEFAULT_MODEL = "gemini-3.5-flash"
AVAILABLE_MODELS = {
    "lite": "gemini-3.1-flash-lite",
    "flash": "gemini-3.5-flash",
    "pro": "gemini-3.1-pro-preview",
}
# Yaklaşık USD fiyatları (milyon token başına). Sadece /kullanim panelinde
# TAHMİNİ maliyet göstermek içindir; Google'ın güncel fiyatlandırmasını
# yansıtmayabilir, kesin rakamlar için Google AI Studio'ya bakın.
MODEL_PRICING_PER_MILLION = {
    "gemini-3.1-flash-lite": {"input": 0.05, "output": 0.20},
    "gemini-3.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-3.1-pro-preview": {"input": 2.50, "output": 10.00},
}
RETRY_TEST_INTERVAL_SECONDS = 60 * 60  # 1 saat
DEFAULT_KEY_COUNT = 5  # ilk kurulumda önerilen varsayılan sayı; /settings'ten değiştirilebilir

# Bir araç (ör. tarayıcı seçimi, CAPTCHA bekleme) kullanıcıdan input() ile
# doğrudan girdi istediğinde, arka plandaki Enter/E dinleyicisinin stdin'e
# karışmaması için bu event set edilir.
key_listener_paused = threading.Event()

console = Console() if RICH_AVAILABLE else None


def cprint(*args, **kwargs):
    """rich varsa onunla, yoksa normal print ile yazdırır."""
    if RICH_AVAILABLE:
        console.print(*args, **kwargs)
    else:
        # rich Panel/Markdown gibi objeleri düz metne çevirmeye çalış
        plain = []
        for a in args:
            plain.append(str(a))
        print(" ".join(plain))


# --------------------------------------------------------------------------
# Konfigürasyon Yönetimi
# --------------------------------------------------------------------------

def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)


def load_config():
    ensure_config_dir()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Eski/artık desteklenmeyen model kayıtlarını otomatik güncelle
            deprecated_models = {
                "gemini-2.0-flash", "gemini-pro", "gemini-1.5-flash", "gemini-1.5-pro",
                "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite",
            }
            if cfg.get("model") in deprecated_models:
                cfg["model"] = DEFAULT_MODEL
                save_config(cfg)
            return cfg
        except Exception:
            pass
    return {}


def save_config(cfg):
    ensure_config_dir()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Sohbet Oturumları (kaydet / listele / yükle) - "localStorage" karşılığı
# olarak diskte JSON dosyaları kullanılır (~/.gemini_agent/sessions/*.json)
# --------------------------------------------------------------------------

def _sessions_dir():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def new_session_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def session_path(session_id):
    return _sessions_dir() / f"{session_id}.json"


def save_session_data(session_id, title, transcript, artifacts):
    data = {
        "id": session_id,
        "title": title or "(başlıksız sohbet)",
        "updated_at": datetime.now().isoformat(),
        "transcript": transcript,
        "artifacts": artifacts,
    }
    try:
        with open(session_path(session_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_session_data(session_id):
    p = session_path(session_id)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_sessions():
    """Tüm kayıtlı sohbetleri, son güncellenme tarihine göre azalan sırada döner."""
    _sessions_dir()
    sessions = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            sessions.append(data)
        except Exception:
            continue
    sessions.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    return sessions


def delete_session(session_id):
    p = session_path(session_id)
    if p.exists():
        p.unlink()


# --------------------------------------------------------------------------
# Hafıza / RAG (geçmiş sohbetlerden ilgili bağlamı bulma)
# --------------------------------------------------------------------------
# Basit ama etkili bir yaklaşım: harici embedding servisine bağımlı olmadan,
# kelime/kök örtüşmesine dayalı bir alaka skoru ile geçmiş sohbetlerdeki
# mesajları tarar. Büyük bir vektör veritabanı gerektirmez, tamamen yerel çalışır.

_TR_STOPWORDS = {
    "bir", "ve", "ile", "bu", "şu", "o", "de", "da", "mi", "mı", "mu", "mü",
    "için", "gibi", "ama", "fakat", "çok", "az", "ne", "nasıl", "neden",
    "the", "a", "an", "is", "are", "to", "of", "in", "on", "and", "or",
}


def _tokenize(text):
    words = re.findall(r"[a-zA-ZğüşıöçĞÜŞİÖÇ0-9]+", text.lower())
    return [w for w in words if w not in _TR_STOPWORDS and len(w) > 2]


def search_memory(query, top_k=5, exclude_session_id=None):
    """
    TÜM kayıtlı sohbetlerdeki mesajları tarar, sorguyla kelime örtüşmesine göre
    puanlar, en alakalı top_k eşleşmeyi döner.
    Dönüş: [{"session_id", "title", "role", "text", "score", "updated_at"}, ...]
    """
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return []

    results = []
    for session in list_sessions():
        if session.get("id") == exclude_session_id:
            continue
        for turn in session.get("transcript", []):
            text = turn.get("text", "")
            if not text:
                continue
            turn_tokens = set(_tokenize(text))
            overlap = query_tokens & turn_tokens
            if not overlap:
                continue
            score = len(overlap) / max(len(query_tokens), 1)
            results.append({
                "session_id": session.get("id"),
                "title": session.get("title", "(başlıksız)"),
                "role": turn.get("role"),
                "text": text,
                "score": score,
                "updated_at": session.get("updated_at", ""),
            })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]


def format_memory_snippet(text, max_len=220):
    text = text.strip().replace("\n", " ")
    return text[:max_len] + ("…" if len(text) > max_len else "")


# --------------------------------------------------------------------------
# Kullanım / Maliyet Takibi (anahtar başına token sayacı)
# --------------------------------------------------------------------------

_usage_lock = threading.Lock()


def load_usage():
    ensure_config_dir()
    if USAGE_FILE.exists():
        try:
            with open(USAGE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_usage(data):
    ensure_config_dir()
    with _usage_lock:
        try:
            with open(USAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def record_usage(key_index, model_name, input_tokens, output_tokens):
    """Bir API çağrısının token kullanımını anahtar bazında biriktirir."""
    with _usage_lock:
        data = load_usage()
        k = str(key_index)
        entry = data.setdefault(k, {})
        m = entry.setdefault(model_name, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
        m["input_tokens"] += int(input_tokens or 0)
        m["output_tokens"] += int(output_tokens or 0)
        m["calls"] += 1
        data[k] = entry
    save_usage(data)


def estimate_cost_usd(model_name, input_tokens, output_tokens):
    price = MODEL_PRICING_PER_MILLION.get(model_name)
    if not price:
        return None
    return (input_tokens / 1_000_000) * price["input"] + (output_tokens / 1_000_000) * price["output"]


def summarize_usage():
    """Tüm anahtarlar için toplam token/maliyet özetini döner (tabloya basılacak satır listesi)."""
    data = load_usage()
    rows = []
    grand_total_cost = 0.0
    for key_idx_str, models in sorted(data.items(), key=lambda kv: int(kv[0])):
        for model_name, m in models.items():
            cost = estimate_cost_usd(model_name, m["input_tokens"], m["output_tokens"])
            if cost:
                grand_total_cost += cost
            rows.append({
                "key": int(key_idx_str) + 1,
                "model": model_name,
                "calls": m["calls"],
                "input_tokens": m["input_tokens"],
                "output_tokens": m["output_tokens"],
                "cost_usd": cost,
            })
    return rows, grand_total_cost


# --------------------------------------------------------------------------
# API Anahtarı Yönetimi (Kota Rotasyonu + Arka Plan Test)
# --------------------------------------------------------------------------

class KeyManager:
    """
    5 API anahtarını yönetir.
    - Her açılışta 1. anahtardan başlar.
    - Bir anahtar kota hatası (429 / ResourceExhausted) verirse sıradaki
      anahtara geçilir.
    - Kotası dolan anahtarlar arka planda her saat başı test edilir;
      başarılı olursa tekrar 'aktif' listesine döner.
    - Bütün anahtarlar tükenirse kullanıcıya bilgi verilir.
    """

    QUOTA_ERROR_PATTERNS = [
        "429", "resourceexhausted", "resource_exhausted", "quota",
        "rate limit", "exceeded", "permission_denied", "unavailable",
    ]

    def __init__(self, keys, model_name, on_switch_callback=None):
        self.keys = keys
        self.model_name = model_name
        self.on_switch_callback = on_switch_callback  # switch olduğunda çağrılır
        self.lock = threading.Lock()

        # her key için durum: "active" | "exhausted"
        self.status = {i: "active" for i in range(len(keys))}
        self.exhausted_since = {}
        self.current_index = 0  # her açılışta 1. anahtardan başla

        self._stop_event = threading.Event()
        self._bg_thread = threading.Thread(target=self._background_loop, daemon=True)
        self._bg_thread.start()

    # ---- yardımcılar -------------------------------------------------

    def _is_quota_error(self, err: Exception) -> bool:
        msg = str(err).lower()
        return any(p in msg for p in self.QUOTA_ERROR_PATTERNS)

    def current_key(self):
        with self.lock:
            return self.keys[self.current_index], self.current_index

    def mark_exhausted(self, index):
        with self.lock:
            self.status[index] = "exhausted"
            self.exhausted_since[index] = datetime.now()

    def _next_active_index(self, start_after):
        """start_after'dan sonraki ilk aktif anahtarı bulur (döngüsel)."""
        n = len(self.keys)
        for offset in range(1, n + 1):
            idx = (start_after + offset) % n
            if self.status[idx] == "active":
                return idx
        return None

    def advance_to_next(self):
        """
        Mevcut anahtarı tükenmiş işaretler ve sıradaki aktif anahtara geçer.
        Geçiş başarılı olursa (new_index, key) döner, hiçbiri aktif değilse None.
        """
        with self.lock:
            old_index = self.current_index
            self.status[old_index] = "exhausted"
            self.exhausted_since[old_index] = datetime.now()

            next_idx = self._next_active_index(old_index)
            if next_idx is None:
                return None

            self.current_index = next_idx

        if self.on_switch_callback:
            self.on_switch_callback(old_index, next_idx, reason="quota")
        return old_index, self.current_index

    def all_exhausted(self):
        with self.lock:
            return all(v == "exhausted" for v in self.status.values())

    def status_table(self):
        with self.lock:
            rows = []
            for i, k in enumerate(self.keys):
                masked = k[:6] + "..." + k[-4:] if len(k) > 12 else "***"
                state = self.status[i]
                is_current = " (AKTİF KULLANIMDA)" if i == self.current_index else ""
                rows.append((i + 1, masked, state, is_current))
            return rows

    # ---- arka plan test döngüsü --------------------------------------

    def _test_key(self, index):
        """Kotası biten bir anahtara minik bir test isteği gönderir."""
        try:
            genai.configure(api_key=self.keys[index])
            model = genai.GenerativeModel(self.model_name)
            resp = model.generate_content(
                "ping",
                generation_config={"max_output_tokens": 5},
            )
            # cevap geldiyse (hata fırlatmadıysa) anahtar tekrar kullanılabilir demektir
            return True
        except Exception as e:
            return False

    def _background_loop(self):
        while not self._stop_event.wait(RETRY_TEST_INTERVAL_SECONDS):
            with self.lock:
                exhausted_indices = [i for i, s in self.status.items() if s == "exhausted"]
            for idx in exhausted_indices:
                ok = self._test_key(idx)
                if ok:
                    with self.lock:
                        self.status[idx] = "active"
                    if self.on_switch_callback:
                        self.on_switch_callback(None, idx, reason="revived")
            # test bitince mevcut aktif anahtarı tekrar configure et (güvenlik için)
            with self.lock:
                cur = self.current_index
            genai.configure(api_key=self.keys[cur])

    def stop(self):
        self._stop_event.set()


# --------------------------------------------------------------------------
# Araçlar (Tools) - Dosya / Terminal Kontrolü
# --------------------------------------------------------------------------

def tool_run_command(command: str, cwd: str = None, timeout: int = 60, on_line=None) -> dict:
    """
    Verilen shell komutunu çalıştırır ve çıktısını döner.
    on_line verilirse, çıktı satır satır üretildikçe on_line(line) ile bildirilir
    (canlı panel gösterimi için kullanılır).
    """
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd or os.getcwd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )
        lines = []
        start = time.time()
        while True:
            line = proc.stdout.readline()
            if line:
                lines.append(line.rstrip("\n"))
                if on_line:
                    on_line(line.rstrip("\n"))
            if proc.poll() is not None and not line:
                break
            if time.time() - start > timeout:
                proc.kill()
                return {"error": f"Komut {timeout} saniye içinde bitmedi (timeout).",
                        "stdout": "\n".join(lines)[-8000:]}
        full_output = "\n".join(lines)[-8000:]
        return {"returncode": proc.returncode, "stdout": full_output, "stderr": ""}
    except Exception as e:
        return {"error": str(e)}


def run_command_live(command: str, cwd: str = None, timeout: int = 60):
    """
    tool_run_command'ı, çıktı satır satır üretildikçe düz (panelsiz) şekilde
    terminale yazdırarak çalıştırır.
    """
    cprint(f"[dim]⚙ çalıştırılıyor:[/dim] {command}" if RICH_AVAILABLE else f"⚙ çalıştırılıyor: {command}")

    def on_line(line):
        if RICH_AVAILABLE:
            console.print(line, style="grey62", markup=False, highlight=False)
        else:
            print(line)

    return tool_run_command(command, cwd=cwd, timeout=timeout, on_line=on_line)


def tool_create_file(path: str, content: str = "") -> dict:
    """Yeni bir dosya oluşturur. Zaten varsa hata döner."""
    p = Path(path).expanduser()
    if p.exists():
        return {"error": f"Dosya zaten var: {path}. Düzenlemek için edit_file kullanın."}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"success": True, "path": str(p)}
    except Exception as e:
        return {"error": str(e)}


def tool_edit_file(path: str, content: str = None, find: str = None, replace: str = None,
                    mode: str = "overwrite") -> dict:
    """
    Var olan bir dosyayı düzenler.
    mode="overwrite" -> content ile tamamen değiştirir
    mode="append"     -> content'i sona ekler
    mode="find_replace"-> find metnini replace ile değiştirir
    """
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"Dosya bulunamadı: {path}"}
    try:
        if mode == "overwrite":
            p.write_text(content or "", encoding="utf-8")
        elif mode == "append":
            with open(p, "a", encoding="utf-8") as f:
                f.write(content or "")
        elif mode == "find_replace":
            text = p.read_text(encoding="utf-8")
            if find not in text:
                return {"error": "find metni dosyada bulunamadı."}
            text = text.replace(find, replace or "")
            p.write_text(text, encoding="utf-8")
        else:
            return {"error": f"Bilinmeyen mode: {mode}"}
        return {"success": True, "path": str(p)}
    except Exception as e:
        return {"error": str(e)}


def tool_delete_file(path: str) -> dict:
    """Bir dosyayı veya klasörü siler."""
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"Bulunamadı: {path}"}
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return {"success": True, "path": str(p)}
    except Exception as e:
        return {"error": str(e)}


def tool_read_file(path: str) -> dict:
    """Bir dosyanın içeriğini okur."""
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"Bulunamadı: {path}"}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        return {"content": content[:20000]}
    except Exception as e:
        return {"error": str(e)}


def tool_list_dir(path: str = ".") -> dict:
    """Bir klasördeki dosya/klasörleri listeler."""
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": f"Bulunamadı: {path}"}
    try:
        items = []
        for item in sorted(p.iterdir()):
            items.append(("DIR " if item.is_dir() else "FILE") + " " + item.name)
        return {"items": items}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------
# Otomatik Test/Lint (dosya düzenlemesinden sonra)
# ---------------------------------------------------------------------

def _find_test_command(cwd: str) -> str:
    """cwd içinde pytest/flake8 gibi araçlar kullanılabilir mi diye bakar, komut önerir."""
    cwd_p = Path(cwd or os.getcwd())
    if shutil.which("pytest") and (any(cwd_p.rglob("test_*.py")) or any(cwd_p.rglob("*_test.py"))):
        return "pytest -q"
    if (cwd_p / "package.json").exists() and shutil.which("npm"):
        return "npm test --silent"
    return ""


def run_auto_test_and_lint(cwd: str = None, changed_path: str = None) -> dict:
    """
    Bir dosya düzenlemesinden sonra otomatik olarak (mevcutsa) pytest ve flake8/ruff
    çalıştırır. Sonuçlar modele geri beslenmesi için özetlenmiş biçimde döner.
    Hiçbiri kurulu değilse/tespit edilemezse boş sonuç döner (sessizce atlanır).
    """
    results = {}
    cwd = cwd or os.getcwd()

    test_cmd = _find_test_command(cwd)
    if test_cmd:
        r = tool_run_command(test_cmd, cwd=cwd, timeout=90)
        results["test"] = {"command": test_cmd, "output": (r.get("stdout") or r.get("error") or "")[-2000:]}

    linter = None
    if shutil.which("ruff"):
        linter = "ruff check ."
    elif shutil.which("flake8"):
        linter = "flake8 ."
    if linter and changed_path and changed_path.endswith(".py"):
        r = tool_run_command(linter, cwd=cwd, timeout=30)
        results["lint"] = {"command": linter, "output": (r.get("stdout") or r.get("error") or "")[-2000:]}

    return results


# ---------------------------------------------------------------------
# Git Entegrasyonu
# ---------------------------------------------------------------------

def _git(args, cwd=None, timeout=30):
    if not shutil.which("git"):
        return {"error": "'git' sistemde kurulu değil."}
    return tool_run_command("git " + args, cwd=cwd, timeout=timeout)


def tool_git_status(cwd: str = None) -> dict:
    """`git status` çıktısını döner."""
    return _git("status --short --branch", cwd=cwd)


def tool_git_diff(cwd: str = None, staged: bool = False) -> dict:
    """Mevcut (ya da stage edilmiş) değişikliklerin diff'ini döner."""
    return _git(f"diff{' --staged' if staged else ''}", cwd=cwd)


def tool_git_commit(message: str, cwd: str = None, add_all: bool = True) -> dict:
    """Değişiklikleri commit'ler. add_all=True ise önce `git add -A` çalıştırır."""
    if add_all:
        add_result = _git("add -A", cwd=cwd)
        if add_result.get("error"):
            return add_result
    safe_msg = message.replace('"', '\\"')
    return _git(f'commit -m "{safe_msg}"', cwd=cwd)


def tool_git_branch(name: str = None, cwd: str = None, create: bool = False) -> dict:
    """Branch listeler (name verilmezse), ya da create=True ile yeni branch açıp geçer."""
    if not name:
        return _git("branch", cwd=cwd)
    if create:
        return _git(f"checkout -b {name}", cwd=cwd)
    return _git(f"checkout {name}", cwd=cwd)


def tool_git_push(cwd: str = None, remote: str = "origin", branch: str = None) -> dict:
    """Mevcut branch'i (ya da belirtileni) uzak depoya gönderir."""
    if branch:
        return _git(f"push {remote} {branch}", cwd=cwd, timeout=60)
    return _git(f"push {remote}", cwd=cwd, timeout=60)


def tool_git_pr_create(title: str, body: str = "", cwd: str = None) -> dict:
    """GitHub CLI (`gh`) kuruluysa mevcut branch için bir pull request açar."""
    if not shutil.which("gh"):
        return {"error": "GitHub CLI ('gh') kurulu değil. Kurulum: https://cli.github.com/"}
    safe_title = title.replace('"', '\\"')
    safe_body = (body or "").replace('"', '\\"')
    return tool_run_command(f'gh pr create --title "{safe_title}" --body "{safe_body}"', cwd=cwd, timeout=60)


TOOL_FUNCTIONS = {
    "run_command": tool_run_command,
    "create_file": tool_create_file,
    "edit_file": tool_edit_file,
    "delete_file": tool_delete_file,
    "read_file": tool_read_file,
    "list_dir": tool_list_dir,
    "git_status": tool_git_status,
    "git_diff": tool_git_diff,
    "git_commit": tool_git_commit,
    "git_branch": tool_git_branch,
    "git_push": tool_git_push,
    "git_pr_create": tool_git_pr_create,
}

# ---------------------------------------------------------------------
# Plugin Sistemi (~/.gemini_agent/plugins/*.py)
# ---------------------------------------------------------------------
# Kullanıcı, PLUGINS_DIR altına aşağıdaki formatta bir .py dosyası koyarak
# kendi aracını ekleyebilir:
#
#   TOOL_SCHEMA = {
#       "name": "benim_aracim",
#       "description": "Ne işe yaradığını açıkla.",
#       "parameters": {"type": "object", "properties": {...}, "required": [...]},
#   }
#   def run(**kwargs):
#       return {"success": True, ...}
#
# Agent açılışta bu klasörü tarar, her plugin'i TOOL_FUNCTIONS ve GEMINI_TOOLS'a
# otomatik olarak ekler.

PLUGIN_TOOL_SCHEMAS = []


def load_plugins():
    """PLUGINS_DIR altındaki .py dosyalarını yükler, TOOL_FUNCTIONS'a ekler."""
    ensure_config_dir()
    loaded = []
    for py_file in sorted(PLUGINS_DIR.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(f"aegis_plugin_{py_file.stem}", py_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            schema = getattr(module, "TOOL_SCHEMA", None)
            run_fn = getattr(module, "run", None)
            if not schema or not callable(run_fn):
                cprint(f"[yellow]⚠ Plugin atlandı (TOOL_SCHEMA/run eksik): {py_file.name}[/yellow]"
                       if RICH_AVAILABLE else f"Plugin atlandı: {py_file.name}")
                continue
            name = schema.get("name")
            if not name:
                continue
            TOOL_FUNCTIONS[name] = run_fn
            PLUGIN_TOOL_SCHEMAS.append({
                "name": name,
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
            })
            loaded.append(name)
        except Exception as e:
            cprint(f"[red]Plugin yüklenemedi ({py_file.name}): {e}[/red]"
                   if RICH_AVAILABLE else f"Plugin yüklenemedi ({py_file.name}): {e}")
    return loaded

# ---------------------------------------------------------------------
# Tarayıcı Kontrolü (Playwright) - opsiyonel bağımlılık
# Kurulum: pip install playwright  &&  playwright install
# ---------------------------------------------------------------------

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


def detect_installed_browsers():
    """
    Sistemde GERÇEKTEN kurulu olan tarayıcıları tespit eder.
    Kurulu olmayanları listeye eklemez (ör. Firefox kurulu değilse önerilmez).
    Dönüş: [(playwright_channel, görünen_isim), ...]
    """
    found = []

    def add(channel, label):
        if not any(c == channel for c, _ in found):
            found.append((channel, label))

    if IS_WINDOWS:
        import winreg

        # 1) Windows App Paths registry'sinden kesin yol kontrolü
        app_path_map = [
            ("chrome.exe", "chrome", "Google Chrome"),
            ("msedge.exe", "msedge", "Microsoft Edge"),
            ("firefox.exe", "firefox", "Mozilla Firefox"),
        ]
        for exe_name, channel, label in app_path_map:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
                    with winreg.OpenKey(hive, key_path) as key:
                        path_val, _ = winreg.QueryValueEx(key, "")
                        if path_val and Path(path_val).exists():
                            add(channel, label)
                except OSError:
                    continue

        # 2) Bilinen kurulum yollarını da kontrol et (registry'de yoksa yedek)
        env_dirs = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                    os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                    os.environ.get("LocalAppData", "")]
        fallback_paths = [
            ("chrome", "Google Chrome", ["Google\\Chrome\\Application\\chrome.exe"]),
            ("msedge", "Microsoft Edge", ["Microsoft\\Edge\\Application\\msedge.exe"]),
            ("firefox", "Mozilla Firefox", ["Mozilla Firefox\\firefox.exe"]),
        ]
        for channel, label, subpaths in fallback_paths:
            for base in env_dirs:
                if not base:
                    continue
                for sub in subpaths:
                    if (Path(base) / sub).exists():
                        add(channel, label)
    elif IS_MAC:
        mac_apps = [
            ("chrome", "Google Chrome", "/Applications/Google Chrome.app"),
            ("msedge", "Microsoft Edge", "/Applications/Microsoft Edge.app"),
            ("firefox", "Mozilla Firefox", "/Applications/Firefox.app"),
        ]
        for channel, label, app_path in mac_apps:
            if Path(app_path).exists():
                add(channel, label)
    else:
        # Linux: PATH üzerinde binary arıyoruz
        linux_bins = [
            ("chrome", "Google Chrome", ["google-chrome", "google-chrome-stable"]),
            ("msedge", "Microsoft Edge", ["microsoft-edge", "microsoft-edge-stable"]),
            ("firefox", "Mozilla Firefox", ["firefox"]),
        ]
        for channel, label, bins in linux_bins:
            for b in bins:
                if shutil.which(b):
                    add(channel, label)
                    break

    # Playwright'ın dahili Chromium'u her zaman yedek olarak sunulur
    add("chromium", "Chromium (Playwright dahili, her zaman kullanılabilir)")
    return found


def _chrome_family_user_data_dir(channel):
    """Chrome/Edge için 'User Data' kök klasörünü (profilleri içeren) döner, yoksa None."""
    if channel == "chrome":
        if IS_WINDOWS:
            base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
        elif IS_MAC:
            base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
        else:
            base = Path.home() / ".config" / "google-chrome"
    elif channel == "msedge":
        if IS_WINDOWS:
            base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
        elif IS_MAC:
            base = Path.home() / "Library" / "Application Support" / "Microsoft Edge"
        else:
            base = Path.home() / ".config" / "microsoft-edge"
    else:
        return None
    return str(base) if base.exists() else None


def detect_browser_profiles(channel):
    """
    Verilen tarayıcı kanalı (chrome/msedge) için gerçek kullanıcı profillerini tespit eder.
    Dönüş: (user_data_dir, [(profil_klasörü, görünen_isim), ...]) ya da (None, []).
    """
    user_data_dir = _chrome_family_user_data_dir(channel)
    if not user_data_dir:
        return None, []
    local_state = Path(user_data_dir) / "Local State"
    profiles = []
    if local_state.exists():
        try:
            data = json.loads(local_state.read_text(encoding="utf-8"))
            info_cache = data.get("profile", {}).get("info_cache", {})
            for folder, info in info_cache.items():
                profiles.append((folder, info.get("name") or folder))
        except Exception:
            pass
    if not profiles:
        # Local State okunamadıysa da en azından "Default" profili varsayalım
        if (Path(user_data_dir) / "Default").exists():
            profiles = [("Default", "Varsayılan Profil")]
    return user_data_dir, profiles


class BrowserController:
    """Tek bir kalıcı tarayıcı oturumunu yönetir (lazy-init, tekil)."""

    LIVE_WINDOW_NAME = "Tarayici (canli izleme) - turuncu imlec = yapay zekanin dokunusu"
    MAX_FPS = 60
    MIN_FPS = 30  # sadece hedef aralığı belgelemek için (gerçek fps sayfa yenileme hızına bağlıdır)

    def __init__(self):
        self._playwright = None
        self.browser = None
        self.page = None
        self.channel = None
        self._is_persistent = False  # True ise self.browser aslında bir BrowserContext (kendi profil modu)

        # Canlı izleme (CDP screencast) durumu
        self._cdp = None
        self._page_size = (1280, 800)
        self.cursor_pos = (640, 400)          # sanal (yapay zeka) imlecinin sayfa koordinatı
        self._frame_queue = queue.Queue(maxsize=2)
        self._live_thread = None
        self._live_stop = threading.Event()
        self._frame_callback = None  # GUI tarafından atanır: fn(frame_bgr_ndarray) -> None

    def set_frame_callback(self, fn):
        """GUI'nin her yeni kareyi (sanal imleç zaten çizilmiş, BGR ndarray) almak için
        kaydettiği fonksiyon. Ayarlandığında ayrı bir cv2 penceresi açılmaz — kareler
        sadece bu callback'e gönderilir (sohbet balonu içindeki gömülü görünüm için)."""
        self._frame_callback = fn

    def user_click(self, x, y):
        """Kullanıcının gömülü tarayıcı görünümüne tıklamasıyla tetiklenir; yapay zekanın
        sanal imlecinden bağımsız olarak sayfada gerçek bir fare tıklaması yapar."""
        if not self.page:
            return
        try:
            self.page.mouse.click(x, y)
        except Exception:
            pass

    def user_type(self, text):
        """Kullanıcının gömülü tarayıcı yazma kutusundan girdiği metni, o an odaklı
        elemana yazar (Enter dahil değildir, çağıran taraf isterse ayrıca gönderir)."""
        if not self.page:
            return
        try:
            self.page.keyboard.type(text)
        except Exception:
            pass

    def current_page_size(self):
        return self._page_size

    def _detect_channels(self):
        """Sistemde GERÇEKTEN kurulu olan tarayıcı kanallarını döner."""
        return detect_installed_browsers()

    def _ask_choice(self, title, options):
        """Genel amaçlı: (value, label) listesinden kullanıcıya seçtirir. stdin çakışmasını
        önlemek için arka plandaki Enter/E dinleyicisini geçici olarak duraklatır."""
        key_listener_paused.set()
        try:
            if len(options) == 1:
                return options[0][0]
            cprint(f"\n[bold cyan]{title}[/bold cyan]" if RICH_AVAILABLE else f"\n{title}")
            for i, (_, label) in enumerate(options, 1):
                cprint(f"  {i}) {label}")
            while True:
                raw = input("Seçim (numara): ").strip()
                if raw.isdigit() and 1 <= int(raw) <= len(options):
                    return options[int(raw) - 1][0]
                cprint("[red]Geçersiz seçim, tekrar deneyin.[/red]")
        finally:
            key_listener_paused.clear()

    def _ask_which_browser(self, only_real=False):
        """Kullanıcıya hangi tarayıcının kullanılacağını sorar."""
        candidates = self._detect_channels()
        if only_real:
            candidates = [c for c in candidates if c[0] != "chromium"]
        if not candidates:
            return None
        return self._ask_choice("Bilgisayarınızda bulunan tarayıcılar:", candidates)

    def ensure_started(self, ask_if_multiple=True):
        if self.page:
            return True, None
        if not PLAYWRIGHT_AVAILABLE:
            return False, ("Tarayıcı kontrolü için 'playwright' kurulu değil. "
                            "Kurmak için: pip install playwright && playwright install")
        try:
            self._playwright = sync_playwright().start()
            ok, err = self._start_integrated_browser()
            if not ok:
                return False, err

            self.page = self.browser.pages[0] if self.browser.pages else self.browser.new_page()

            try:
                vp = self.page.viewport_size
                if vp and vp.get("width") and vp.get("height"):
                    self._page_size = (vp["width"], vp["height"])
            except Exception:
                pass
            self.cursor_pos = (self._page_size[0] // 2, self._page_size[1] // 2)
            self._start_live_view()
            return True, None
        except Exception as e:
            return False, str(e)

    def _start_integrated_browser(self):
        """
        AEGIS'in KENDİ dahili tarayıcısını başlatır — sistemdeki Chrome/Edge'e hiç
        dokunmaz, kullanıcıya hiçbir şey sormaz. Playwright'ın kendi Chromium'unu,
        AEGIS'e ait kalıcı bir profille (~/.gemini_agent/browser_profile) açar; bu
        sayede girdiğin siteler, çerezler ve oturumlar (ör. bir siteye giriş yapman)
        AEGIS kapatılıp açılsa bile hatırlanır. Otomasyon tespiti yapan sitelerde
        engellenmemek için birkaç 'gerçek tarayıcı gibi görünme' ayarı da eklenir.
        """
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        try:
            context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=False,
                args=launch_args,
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            )
        except Exception as e:
            return False, (f"AEGIS'in dahili tarayıcısı başlatılamadı — profil klasörü başka bir "
                            f"AEGIS penceresi tarafından kullanılıyor olabilir ({e})")

        try:
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        except Exception:
            pass

        self.browser = context
        self._is_persistent = True
        self.channel = "AEGIS dahili tarayıcı (kalıcı profil)"
        return True, None

    # ---- Canlı izleme (CDP screencast) + sanal imleç ------------------

    def _start_live_view(self):
        """
        Chrome DevTools Protocol üzerinden sayfanın canlı ekran akışını (screencast)
        başlatır ve ayrı bir pencerede (OpenCV) 30-60 FPS civarında gösterir.
        Kullanıcının gerçek fare imleci DEĞİL; yapay zekanın tıkladığı/doldurduğu
        yeri gösteren turuncu bir sanal imleç, her karenin üzerine çizilir.
        """
        if not CV2_AVAILABLE:
            cprint("[dim]Canlı tarayıcı izleme için 'opencv-python' ve 'numpy' kurulu değil "
                   "(pip install opencv-python numpy). Tarayıcı yine de normal şekilde çalışacak.[/dim]"
                   if RICH_AVAILABLE else
                   "Canlı tarayıcı izleme için opencv-python ve numpy kurulu değil.")
            return
        # GUI modunda ayrı bir masaüstü penceresi (cv2.imshow) yerine kareler doğrudan
        # sohbet balonu içindeki gömülü görünüme (frame_callback) gönderilir.
        try:
            self._cdp = self.page.context.new_cdp_session(self.page)
        except Exception as e:
            cprint(f"[dim]Canlı izleme başlatılamadı (CDP): {e}[/dim]")
            return

        def on_frame(params):
            # Chrome, bir sonraki kareyi göndermeden önce bu karenin onaylanmasını
            # (ack) bekler; onaylanmazsa akış durur.
            try:
                self._cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:
                pass
            try:
                if self._frame_queue.full():
                    try:
                        self._frame_queue.get_nowait()
                    except Exception:
                        pass
                self._frame_queue.put_nowait(params["data"])
            except Exception:
                pass

        try:
            self._cdp.on("Page.screencastFrame", on_frame)
            self._cdp.send("Page.startScreencast", {
                "format": "jpeg",
                "quality": 80,
                "maxWidth": 1280,
                "maxHeight": 800,
                "everyNthFrame": 1,
            })
        except Exception as e:
            cprint(f"[dim]Screencast başlatılamadı: {e}[/dim]")
            return

        self._live_stop.clear()
        self._live_thread = threading.Thread(target=self._live_view_loop, daemon=True)
        self._live_thread.start()
        cprint("[dim]🖥 Canlı tarayıcı penceresi açıldı (turuncu daire = yapay zekanın sanal imleci).[/dim]"
               if RICH_AVAILABLE else "Canlı tarayıcı penceresi açıldı.")

    def _draw_virtual_cursor(self, frame, x, y):
        """Kullanıcının gerçek imlecinden ayırt edilebilir, turuncu bir sanal imleç çizer."""
        color = (0, 165, 255)  # BGR: turuncu
        cv2.circle(frame, (x, y), 14, color, 2, lineType=cv2.LINE_AA)
        cv2.circle(frame, (x, y), 3, color, -1, lineType=cv2.LINE_AA)
        cv2.line(frame, (x - 20, y), (x - 8, y), color, 2, lineType=cv2.LINE_AA)
        cv2.line(frame, (x + 8, y), (x + 20, y), color, 2, lineType=cv2.LINE_AA)
        cv2.line(frame, (x, y - 20), (x, y - 8), color, 2, lineType=cv2.LINE_AA)
        cv2.line(frame, (x, y + 8), (x, y + 20), color, 2, lineType=cv2.LINE_AA)
        cv2.putText(frame, "AI", (x + 18, y - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    color, 2, cv2.LINE_AA)

    def _live_view_loop(self):
        min_interval = 1.0 / self.MAX_FPS
        last_show = 0.0
        while not self._live_stop.is_set():
            try:
                b64data = self._frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            now = time.time()
            if now - last_show < min_interval:
                continue
            last_show = now
            try:
                raw = base64.b64decode(b64data)
                arr = np.frombuffer(raw, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                h, w = frame.shape[:2]
                page_w, page_h = self._page_size
                cx, cy = self.cursor_pos
                if page_w and page_h:
                    px = int(cx * (w / page_w))
                    py = int(cy * (h / page_h))
                else:
                    px, py = int(cx), int(cy)
                px = max(0, min(w - 1, px))
                py = max(0, min(h - 1, py))
                self._draw_virtual_cursor(frame, px, py)
                if self._frame_callback:
                    try:
                        self._frame_callback(frame)
                    except Exception:
                        pass
                else:
                    cv2.imshow(self.LIVE_WINDOW_NAME, frame)
                    cv2.waitKey(1)
            except Exception:
                continue
        if not self._frame_callback:
            try:
                cv2.destroyWindow(self.LIVE_WINDOW_NAME)
            except Exception:
                pass

    def _stop_live_view(self):
        self._live_stop.set()
        try:
            if self._cdp:
                self._cdp.send("Page.stopScreencast")
        except Exception:
            pass
        if self._live_thread:
            self._live_thread.join(timeout=1.5)
        self._live_thread = None
        self._cdp = None

    def move_cursor_to(self, x, y, duration=0.3, fps=60):
        """
        Sanal imleci mevcut konumundan (x, y) hedefine, yumuşak (ease-out) bir
        yörüngeyle, ~60 FPS'e kadar aralıklarla kaydırarak hareket ettirir.
        Canlı izleme penceresi her karede self.cursor_pos'u okuduğu için bu,
        gerçek bir "imleç kayması" animasyonu olarak görünür.
        """
        steps = max(int(duration * fps), 1)
        start_x, start_y = self.cursor_pos
        for i in range(1, steps + 1):
            t = i / steps
            eased = 1 - (1 - t) ** 2  # ease-out: hızlı başlar, yavaşlayarak durur
            ix = start_x + (x - start_x) * eased
            iy = start_y + (y - start_y) * eased
            self.cursor_pos = (ix, iy)
            time.sleep(1.0 / fps)
        self.cursor_pos = (x, y)

    def close(self):
        self._stop_live_view()
        try:
            if self.browser:
                self.browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self.browser = None
        self.page = None
        self._playwright = None
        self._is_persistent = False


_browser = BrowserController()


def tool_browser_open(url: str) -> dict:
    """Bir URL'yi tarayıcıda açar (tarayıcı henüz başlamadıysa önce başlatır)."""
    ok, err = _browser.ensure_started()
    if not ok:
        return {"error": err}
    try:
        if not url.startswith("http"):
            url = "https://" + url
        _browser.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {"success": True, "url": _browser.page.url, "title": _browser.page.title()}
    except Exception as e:
        return {"error": str(e)}


def tool_browser_click(selector: str = None, text: str = None) -> dict:
    """Bir elementi CSS seçici veya görünür metniyle tıklar (önce sanal imleç hedefe taşınır)."""
    if not _browser.page:
        return {"error": "Tarayıcı açık değil. Önce browser_open kullanın."}
    try:
        if text and not selector:
            locator = _browser.page.get_by_text(text, exact=False).first
        else:
            locator = _browser.page.locator(selector).first
        try:
            box = locator.bounding_box(timeout=5000)
            if box:
                cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                _browser.move_cursor_to(cx, cy)
        except Exception:
            pass
        locator.click(timeout=10000)
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


def tool_browser_fill(selector: str, text: str) -> dict:
    """Bir input/textarea alanına CSS seçiciyle metin yazar (önce sanal imleç hedefe taşınır)."""
    if not _browser.page:
        return {"error": "Tarayıcı açık değil. Önce browser_open kullanın."}
    try:
        locator = _browser.page.locator(selector).first
        try:
            box = locator.bounding_box(timeout=5000)
            if box:
                cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                _browser.move_cursor_to(cx, cy)
        except Exception:
            pass
        locator.fill(text, timeout=10000)
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


def tool_browser_get_text(selector: str = "body") -> dict:
    """Sayfadaki (veya bir elementin) görünür metnini döner."""
    if not _browser.page:
        return {"error": "Tarayıcı açık değil. Önce browser_open kullanın."}
    try:
        content = _browser.page.inner_text(selector, timeout=10000)
        return {"content": content[:6000]}
    except Exception as e:
        return {"error": str(e)}


def tool_browser_wait_for_user(message: str = "Lütfen doğrulamayı (CAPTCHA vb.) tamamlayın.") -> dict:
    """
    CAPTCHA veya benzeri bir doğrulama algılandığında çağrılır: kullanıcıdan
    tarayıcı penceresinde işlemi tamamlamasını ister, Enter'a basınca devam eder.
    """
    key_listener_paused.set()
    try:
        cprint(f"\n[bold yellow]⏸ {message}[/bold yellow]\n[dim]Tamamladıktan sonra buraya dönüp Enter'a basın...[/dim]"
               if RICH_AVAILABLE else f"\n{message}\nTamamladıktan sonra Enter'a basın...")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        return {"success": True, "note": "Kullanıcı işlemi tamamladığını bildirdi."}
    finally:
        key_listener_paused.clear()


def tool_browser_screenshot(path: str = None) -> dict:
    """
    Mevcut sayfanın ekran görüntüsünü alır. Masaüstüne değil, geçici bir klasöre
    kaydedilir; model görüntüye baktıktan hemen sonra dosya otomatik olarak silinir
    (bu yüzden 'success' sonucuna görüntünün base64 verisi de eklenir; asıl silme
    işlemi bu fonksiyonu çağıran _execute_tool içinde yapılır).
    """
    if not _browser.page:
        return {"error": "Tarayıcı açık değil. Önce browser_open kullanın."}
    ensure_config_dir()
    tmp_path = TEMP_DIR / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    try:
        _browser.page.screenshot(path=str(tmp_path))
        return {"success": True, "path": str(tmp_path), "_temp_image": True}
    except Exception as e:
        return {"error": str(e)}


def tool_browser_close(dummy: str = None) -> dict:
    """Tarayıcı oturumunu kapatır."""
    _browser.close()
    return {"success": True}


TOOL_FUNCTIONS.update({
    "browser_open": tool_browser_open,
    "browser_click": tool_browser_click,
    "browser_fill": tool_browser_fill,
    "browser_get_text": tool_browser_get_text,
    "browser_wait_for_user": tool_browser_wait_for_user,
    "browser_screenshot": tool_browser_screenshot,
    "browser_close": tool_browser_close,
})

# Gemini function-calling şeması
GEMINI_TOOLS = [
    {
        "function_declarations": [
            {
                "name": "run_command",
                "description": "Verilen shell/terminal komutunu çalıştırır ve stdout/stderr/returncode döner.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Çalıştırılacak shell komutu."},
                        "cwd": {"type": "string", "description": "Çalışma dizini (opsiyonel)."},
                        "timeout": {"type": "integer", "description": "Saniye cinsinden zaman aşımı."},
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "create_file",
                "description": "Yeni bir dosya oluşturur. Dosya zaten varsa hata döner (onun yerine edit_file kullanılmalı).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "edit_file",
                "description": "Var olan bir dosyayı düzenler: overwrite (tamamen değiştir), append (sona ekle) veya find_replace (metin değiştir).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "find": {"type": "string"},
                        "replace": {"type": "string"},
                        "mode": {"type": "string", "enum": ["overwrite", "append", "find_replace"]},
                    },
                    "required": ["path", "mode"],
                },
            },
            {
                "name": "delete_file",
                "description": "Bir dosyayı veya klasörü siler.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "read_file",
                "description": "Bir dosyanın içeriğini okur.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "list_dir",
                "description": "Bir klasördeki dosya ve klasörleri listeler.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
            {
                "name": "browser_open",
                "description": "Kullanıcının bilgisayarındaki gerçek bir tarayıcıda bir URL açar. "
                                "Tarayıcı henüz başlatılmadıysa, GERÇEKTEN kurulu tarayıcılar arasından "
                                "kullanıcıya hangisini kullanmak istediğini sorar.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
            {
                "name": "browser_click",
                "description": "Açık sayfada bir elementi CSS seçici (selector) veya görünür metin (text) ile tıklar.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string", "description": "CSS seçici (opsiyonel)."},
                        "text": {"type": "string", "description": "Tıklanacak görünür metin (opsiyonel)."},
                    },
                },
            },
            {
                "name": "browser_fill",
                "description": "Açık sayfadaki bir input/textarea alanına CSS seçiciyle metin yazar.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "selector": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["selector", "text"],
                },
            },
            {
                "name": "browser_get_text",
                "description": "Açık sayfadaki (veya belirtilen elementteki) görünür metni okur. "
                                "Sayfa içeriğini anlamak/kontrol etmek için kullanılır.",
                "parameters": {
                    "type": "object",
                    "properties": {"selector": {"type": "string"}},
                },
            },
            {
                "name": "browser_wait_for_user",
                "description": "CAPTCHA veya benzeri bir insan doğrulaması algılandığında çağrılır. "
                                "Kullanıcıdan tarayıcı penceresinde işlemi tamamlamasını ister ve "
                                "kullanıcı onaylayana kadar bekler.",
                "parameters": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                },
            },
            {
                "name": "browser_screenshot",
                "description": "Açık sayfanın ekran görüntüsünü alır ve doğrudan modele (sana) gösterir. "
                                "Görüntü geçici bir dosyaya kaydedilir ve sen baktıktan hemen sonra "
                                "otomatik olarak silinir; kullanıcının masaüstünde birikmez.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "browser_close",
                "description": "Açık tarayıcı oturumunu kapatır.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "git_status",
                "description": "`git status` çalıştırır, değişen/staged/untracked dosyaları listeler.",
                "parameters": {
                    "type": "object",
                    "properties": {"cwd": {"type": "string"}},
                },
            },
            {
                "name": "git_diff",
                "description": "Mevcut değişikliklerin (ya da staged=true ise stage edilmiş olanların) diff çıktısını gösterir.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cwd": {"type": "string"},
                        "staged": {"type": "boolean"},
                    },
                },
            },
            {
                "name": "git_commit",
                "description": "Değişiklikleri commit eder. add_all=true (varsayılan) ise önce tüm değişiklikleri stage eder.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "cwd": {"type": "string"},
                        "add_all": {"type": "boolean"},
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "git_branch",
                "description": "Branch listeler (name verilmezse), ya da create=true ile yeni bir branch açıp ona geçer, "
                                "create=false ile var olan bir branch'e geçer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "cwd": {"type": "string"},
                        "create": {"type": "boolean"},
                    },
                },
            },
            {
                "name": "git_push",
                "description": "Mevcut (ya da belirtilen) branch'i uzak depoya (varsayılan: origin) gönderir.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cwd": {"type": "string"},
                        "remote": {"type": "string"},
                        "branch": {"type": "string"},
                    },
                },
            },
            {
                "name": "git_pr_create",
                "description": "GitHub CLI ('gh') kuruluysa, mevcut branch için bir pull request açar.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "cwd": {"type": "string"},
                    },
                    "required": ["title"],
                },
            },
        ]
    }
]


def build_gemini_tools():
    """
    GEMINI_TOOLS + yüklenen plugin'lerin şemalarını birleştirir. Plugin'ler
    açılışta load_plugins() ile PLUGIN_TOOL_SCHEMAS listesine eklenir; bu
    fonksiyon her ChatEngine._build_model() çağrısında çağrılarak modele
    güncel araç listesinin gitmesini sağlar.
    """
    if not PLUGIN_TOOL_SCHEMAS:
        return GEMINI_TOOLS
    merged = json.loads(json.dumps(GEMINI_TOOLS))  # derin kopya
    merged[0]["function_declarations"].extend(PLUGIN_TOOL_SCHEMAS)
    return merged


# --------------------------------------------------------------------------
# Dosya Yükleme (/resim komutu) - dosya gezgini veya panodan (Ctrl+V)
# Sadece görsel değil; PDF, ses, video, kod/metin dosyaları dahil HER TÜR
# dosya desteklenir. Dosya türüne göre otomatik olarak doğru şekilde
# (binary inline_data ya da düz metin) Gemini'ye gönderilir.
# --------------------------------------------------------------------------

# Gemini'nin binary (inline_data) olarak doğrudan kabul ettiği türler.
# Bunların dışındaki her şey, mümkünse METİN olarak okunup gönderilir
# (kod dosyaları, .html, .json, .csv, .md, .log, vb. için doğru yaklaşım budur;
# aksi halde "Unable to process input image" gibi hatalar alınır).
_BINARY_MIME_PREFIXES = ("image/", "audio/", "video/")
_BINARY_EXACT_MIME = {"application/pdf"}

# uzantıdan mime tahmini isabetsiz/eksik olursa (mimetypes bazı uzantıları
# bilmeyebilir) burada elle tamamlıyoruz.
_EXT_MIME_OVERRIDES = {
    ".py": "text/x-python",
    ".ts": "text/plain",
    ".tsx": "text/plain",
    ".jsx": "text/plain",
    ".md": "text/markdown",
    ".yml": "text/plain",
    ".yaml": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/plain",
    ".js": "text/javascript",
    ".c": "text/plain",
    ".cpp": "text/plain",
    ".h": "text/plain",
    ".java": "text/plain",
    ".sh": "text/plain",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".xml": "text/xml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".toml": "text/plain",
}

# Metin olarak okunacak dosyalar için üst boyut sınırı (çok büyük dosyalar
# prompt'u şişirip modeli yavaşlatır/hata verdirir).
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024   # 2 MB
MAX_BINARY_FILE_BYTES = 20 * 1024 * 1024  # 20 MB


def _guess_mime_type(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in _EXT_MIME_OVERRIDES:
        return _EXT_MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _pick_file_via_file_dialog():
    """Tkinter dosya gezgini penceresi açar, seçilen dosyanın yolunu döner (iptalse None)."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        return None, "Bu sistemde tkinter kurulu değil, dosya gezgini açılamıyor."
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Bir dosya seçin (görsel, PDF, kod, metin, ses, video... her tür desteklenir)",
            filetypes=[
                ("Tüm dosyalar", "*.*"),
                ("Görsel dosyalar", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                ("Belgeler", "*.pdf *.txt *.md *.csv *.json"),
                ("Kod dosyaları", "*.py *.js *.ts *.html *.css *.java *.c *.cpp *.sh"),
            ],
        )
        root.destroy()
        if not path:
            return None, None
        return path, None
    except Exception as e:
        return None, str(e)


def _pick_file_via_clipboard():
    """
    Panodaki içeriği alır: bir görsel kopyalandıysa geçici dosyaya kaydeder;
    dosya gezgininde bir/birden çok dosya kopyalandıysa (Ctrl+C) doğrudan
    o dosyanın yolunu döner (herhangi bir dosya türü olabilir).
    """
    if not PIL_AVAILABLE:
        return None, "Pillow kurulu değil. Kurmak için: pip install pillow"
    try:
        img = ImageGrab.grabclipboard()
    except Exception as e:
        return None, f"Pano okunamadı: {e}"

    if img is None:
        return None, "Panoda görsel ya da dosya bulunamadı. Önce bir görsel/dosya kopyalayın (Ctrl+C)."

    # Dosya gezgininde kopyalanan dosya(lar) -> yol listesi olarak gelir (her tür dosya olabilir)
    if isinstance(img, list):
        if not img:
            return None, "Panoda dosya bulunamadı."
        first = img[0]
        if isinstance(first, str) and Path(first).exists():
            return first, None
        return None, "Panodaki içerik desteklenmiyor."

    # Doğrudan görsel kopyalanmışsa (ör. bir web sayfasından/paint'ten)
    try:
        ensure_config_dir()
        out_path = UPLOADS_DIR / f"pano_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
        img.save(out_path, "PNG")
        return str(out_path), None
    except Exception as e:
        return None, str(e)


def prompt_resim_command():
    """
    /resim (ve /dosya) komutunun akışını yönetir: kaynağı sorar (dosya/pano),
    dosyayı alır, kullanıcıdan bununla ilgili bir mesaj/soru ister.
    Dönüş: (file_path, caption) ya da (None, None) iptal durumunda.
    """
    key_listener_paused.set()
    try:
        cprint("\n[bold cyan]Dosya kaynağı seçin (görsel, PDF, kod, metin, ses, video... her şey olur):[/bold cyan]"
               if RICH_AVAILABLE else "\nDosya kaynağı seçin (her tür dosya desteklenir):")
        cprint("  1) Dosya gezgini (bilgisayardan seç)")
        cprint("  2) Pano (görsel için Ctrl+C, ya da gezginde dosya kopyalayın)")
        cprint("  0) İptal")
        choice = input("Seçim: ").strip()

        if choice == "1":
            path, err = _pick_file_via_file_dialog()
        elif choice == "2":
            path, err = _pick_file_via_clipboard()
        else:
            return None, None

        if err:
            cprint(f"[red]Hata: {err}[/red]" if RICH_AVAILABLE else f"Hata: {err}")
            return None, None
        if not path:
            cprint("[dim]İptal edildi.[/dim]")
            return None, None

        cprint(f"[green]Dosya alındı:[/green] {path}" if RICH_AVAILABLE else f"Dosya alındı: {path}")
        caption = input("Dosyayla ilgili mesajınız (boş bırakabilirsiniz): ").strip()
        if not caption:
            caption = "Bu dosyayı incele."
        return path, caption
    finally:
        key_listener_paused.clear()


def build_image_parts(file_path: str, caption: str):
    """
    Bir dosyadan + metinden Gemini'ye gönderilecek Content parçalarını oluşturur.
    Görsel/ses/video/PDF ise binary (inline_data) olarak, kod/metin dosyası ise
    (html, py, js, json, csv, md, txt, log, vb.) içeriği okunup düz metin
    olarak gönderilir; bu şekilde "Unable to process input image" gibi hatalar
    önlenmiş olur ve model kod dosyalarını daha iyi analiz edebilir.
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"Dosya bulunamadı: {file_path}")

    mime = _guess_mime_type(file_path)
    size = p.stat().st_size
    is_binary_type = mime.startswith(_BINARY_MIME_PREFIXES) or mime in _BINARY_EXACT_MIME

    if is_binary_type:
        if size > MAX_BINARY_FILE_BYTES:
            raise ValueError(
                f"Dosya çok büyük ({size // (1024*1024)} MB). "
                f"Limit: {MAX_BINARY_FILE_BYTES // (1024*1024)} MB."
            )
        with open(p, "rb") as f:
            data = f.read()
        return [
            genai.protos.Part(text=caption),
            genai.protos.Part(inline_data=genai.protos.Blob(mime_type=mime, data=data)),
        ]

    # Metin/kod dosyası olarak dene
    if size > MAX_TEXT_FILE_BYTES:
        raise ValueError(
            f"Dosya çok büyük ({size // (1024*1024)} MB), metin olarak okunamaz. "
            f"Limit: {MAX_TEXT_FILE_BYTES // (1024*1024)} MB."
        )
    try:
        text_content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise ValueError(f"Dosya okunamadı (desteklenmeyen ikili format olabilir): {e}")

    combined_text = (
        f"{caption}\n\n"
        f"[Ekli dosya: {p.name}]\n"
        f"```\n{text_content}\n```"
    )
    return [genai.protos.Part(text=combined_text)]


# --------------------------------------------------------------------------
# Sistem Promtu
# --------------------------------------------------------------------------

BASE_SYSTEM_PROMPT = """Sen, kullanıcının bilgisayarında çalışan, kod odaklı bir terminal ajanısın.
Görevin: kullanıcının isteklerini yerine getirmek için gerektiğinde terminal komutları
çalıştırmak, dosya oluşturmak/okumak/düzenlemek/silmek ve programlama görevlerinde
(hata ayıklama, kod yazma, kütüphane kurma, test çalıştırma vb.) proaktif şekilde
yardımcı olmak.

Kurallar:
- Kullanıcı bir kodlama/dosya/terminal işlemi istediğinde doğrudan ilgili aracı (tool) çağır,
  sadece ne yapman gerektiğini anlatma.
- Yıkıcı işlemlerden (dosya/klasör silme, sistem genelinde değişiklik) önce kısaca ne yapacağını
  belirt, ardından uygula.
- Cevaplarını kısa ve teknik tut; gereksiz uzun açıklamalardan kaçın.
- Kod bloklarını her zaman uygun dil etiketiyle (```python, ```bash, vb.) ver.
- Bir aracı kullandıktan sonra sonucu kullanıcıya özetle.
- Kullanıcı bir web sitesinde işlem yapmanı istediğinde (sipariş verme, form doldurma, arama vb.)
  browser_open/browser_click/browser_fill/browser_get_text araçlarını kullan. Sayfada bir CAPTCHA
  veya kimlik doğrulama adımıyla karşılaşırsan asla kendin çözmeye çalışma; browser_wait_for_user
  aracını çağırıp kullanıcının tamamlamasını bekle.
- browser_screenshot çağırdığında ekran görüntüsü sana ayrı bir görsel parçası olarak iletilir;
  görüntüyü inceledikten hemen sonra geçici dosya otomatik silinir, bu yüzden dosya yolunu
  kullanıcıya söylemene gerek yok.
- Tarayıcı ayarlarına göre (kullanıcının /settings üzerinden seçtiği moda bağlı olarak) kullanıcının
  KENDİ varsayılan tarayıcı profili (giriş yapılmış hesaplar, kayıtlı sepet/adres bilgileriyle)
  kullanılabilir. Bu sayede "yemeksepetinden şunu sepete ekle" gibi istekleri, kullanıcı zaten o
  siteye giriş yapmışsa doğrudan yerine getirebilirsin; sadece gerekli ürünü bulup sepete ekleme
  adımlarını (arama, ürün kartına tıklama, "sepete ekle" butonuna tıklama) browser_click ile uygula.
"""

LANGUAGE_INSTRUCTIONS = {
    "tr": "Kullanıcıyla HER ZAMAN Türkçe konuş (kod, komut satırı çıktıları ve teknik terimler hariç).",
    "en": "Always respond in English, regardless of the language the user writes in.",
    "auto": "",  # kullanıcı hangi dilde yazarsa o dilde yanıt ver (varsayılan model davranışı)
}


def build_system_prompt(extra_notes=None, language="tr"):
    prompt = BASE_SYSTEM_PROMPT
    lang_instr = LANGUAGE_INSTRUCTIONS.get(language, "")
    if lang_instr:
        prompt += "\n\n[DİL AYARI]\n" + lang_instr
    if extra_notes:
        prompt += "\n\n[SİSTEM NOTLARI]\n" + "\n".join(extra_notes)
    return prompt


# --------------------------------------------------------------------------
# Sohbet Motoru
# --------------------------------------------------------------------------

class ChatEngine:
    MODEL_ERROR_PATTERNS = [
        "no longer available", "not found", "404", "notfound", "invalid model",
    ]

    def __init__(self, key_manager: KeyManager, model_name: str, session_id=None, title=None,
                 transcript=None, artifacts=None, response_language="tr"):
        self.key_manager = key_manager
        self.model_name = model_name
        self.response_language = response_language
        self.system_notes = []  # anahtar değişimi gibi notlar burada tutulur
        self.history = []  # gemini formatında [{'role':..,'parts':[...]}]
        self.session_id = session_id or new_session_id()
        self.title = title
        self.transcript = transcript or []   # [{"role": "user"/"assistant", "text": "..."}]
        self.artifacts = artifacts or []      # [{"path":..., "action":..., "time":...}]
        self.auto_test_enabled = False        # /otomatiktest ile açılır kapanır
        self._configure_current_key()
        self._build_model()

    @classmethod
    def from_session(cls, key_manager, model_name, session_data, response_language="tr"):
        """Kayıtlı bir sohbeti (transcript) yükleyip Gemini geçmişine dönüştürür."""
        engine = cls(
            key_manager, model_name,
            session_id=session_data.get("id"),
            title=session_data.get("title"),
            transcript=session_data.get("transcript", []),
            artifacts=session_data.get("artifacts", []),
            response_language=response_language,
        )
        gemini_history = []
        for turn in engine.transcript:
            role = "user" if turn.get("role") == "user" else "model"
            gemini_history.append(genai.protos.Content(
                role=role, parts=[genai.protos.Part(text=turn.get("text", ""))]
            ))
        engine.history = gemini_history
        engine._build_model()
        return engine

    def save(self):
        title = self.title
        if not title and self.transcript:
            first_user = next((t["text"] for t in self.transcript if t["role"] == "user"), "")
            title = (first_user[:50] + "…") if len(first_user) > 50 else first_user
            self.title = title
        save_session_data(self.session_id, self.title, self.transcript, self.artifacts)

    # ---- kurulum -------------------------------------------------

    def _configure_current_key(self):
        key, idx = self.key_manager.current_key()
        genai.configure(api_key=key)
        return idx

    def _build_model(self):
        # KRİTİK: API anahtarı/model değişince model yeniden kurulur, ama önceki
        # self.chat nesnesinin GERÇEKTEN biriktirdiği konuşma geçmişini
        # (self.history değil, self.chat.history'yi) senkronize etmezsek
        # sohbet burada sıfırlanır ve "eski mesajları hatırlamama" bug'ı oluşur.
        if getattr(self, "chat", None) is not None:
            try:
                self.history = list(self.chat.history)
            except Exception:
                pass
        self.model = genai.GenerativeModel(
            model_name=self.model_name,
            system_instruction=build_system_prompt(self.system_notes, self.response_language),
            tools=build_gemini_tools(),
            safety_settings={
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            },
        )
        self.chat = self.model.start_chat(history=self.history)

    def note_key_switch(self, old_idx, new_idx, reason):
        if reason == "quota":
            note = (f"[{datetime.now().strftime('%H:%M:%S')}] API anahtarı #{old_idx+1} kota "
                    f"sınırına ulaştığı için API anahtarı #{new_idx+1}'e geçildi. "
                    f"Sohbet aynı bağlamla devam ediyor.")
        else:
            note = (f"[{datetime.now().strftime('%H:%M:%S')}] API anahtarı #{new_idx+1} "
                    f"tekrar aktif hale geldi (test çağrısı başarılı).")
        self.system_notes.append(note)
        # modeli yeni system_instruction ile yeniden kur ama geçmişi koru
        self._configure_current_key()
        self._build_model()
        return note

    # ---- mesaj gönderme -------------------------------------------

    def _is_model_error(self, err: Exception) -> bool:
        msg = str(err).lower()
        return any(p in msg for p in self.MODEL_ERROR_PATTERNS)

    def send(self, user_message, on_tool_call=None, on_text_delta=None, on_key_switch=None,
              cancel_event=None, image_parts=None):
        """
        Kullanıcı mesajını STREAM halinde gönderir (metin parça parça geldikçe
        on_text_delta ile bildirilir), gerekiyorsa tool-call döngüsünü çalıştırır,
        kota hatası durumunda anahtar rotasyonu, model bulunamama hatasında ise
        otomatik olarak 'lite' modele düşer.
        image_parts verilirse (ör. /resim komutundan), mesaj görselle birlikte
        çok parçalı (multimodal) olarak gönderilir.
        cancel_event set edilirse (kullanıcı Enter/E'ye bastıysa), akış o anda
        (bir sonraki metin parçası/tool turu beklenmeden) sessizce kesilir.
        Dönüş: (metin, interrupted:bool)
        """
        if image_parts:
            outgoing = genai.protos.Content(role="user", parts=image_parts)
            transcript_text = user_message  # zaten caption olarak parça içinde var
        else:
            outgoing = user_message
            transcript_text = user_message

        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None, True
            try:
                reply, interrupted = self._send_streaming_round(
                    outgoing, on_tool_call, on_text_delta, cancel_event
                )
                if not interrupted and (cancel_event is None or not cancel_event.is_set()):
                    self.transcript.append({"role": "user", "text": transcript_text})
                    self.transcript.append({"role": "assistant", "text": reply})
                    self.save()
                return reply, interrupted
            except Exception as e:
                if self.key_manager._is_quota_error(e):
                    result = self.key_manager.advance_to_next()
                    if result is None:
                        raise RuntimeError(
                            "Tüm API anahtarlarının kotası doldu. "
                            "Anahtarlar arka planda saatlik olarak test edilecek."
                        )
                    continue
                elif self._is_model_error(e) and self.model_name != AVAILABLE_MODELS["lite"]:
                    old_model = self.model_name
                    self.model_name = AVAILABLE_MODELS["lite"]
                    self.key_manager.model_name = self.model_name
                    note = (f"[{datetime.now().strftime('%H:%M:%S')}] Model '{old_model}' bu hesap/anahtar için "
                            f"kullanılamıyor, otomatik olarak '{self.model_name}' moduna geçildi.")
                    self.system_notes.append(note)
                    self._build_model()
                    if on_key_switch:
                        on_key_switch(note)
                    continue
                else:
                    raise

    def _execute_tool(self, fn_name, fn_args):
        """Bir fonksiyon çağrısını çalıştırır ve (function_response_part, image_bytes, image_mime) döner."""
        if fn_name == "run_command":
            result = run_command_live(
                fn_args.get("command", ""),
                cwd=fn_args.get("cwd"),
                timeout=fn_args.get("timeout", 60),
            )
        else:
            func = TOOL_FUNCTIONS.get(fn_name)
            if func:
                try:
                    result = func(**fn_args)
                except Exception as e:
                    result = {"error": str(e)}
            else:
                result = {"error": f"Bilinmeyen araç: {fn_name}"}

        if fn_name in ("create_file", "edit_file", "delete_file") and result.get("success"):
            self.artifacts.append({
                "path": result.get("path", fn_args.get("path")),
                "action": fn_name,
                "time": datetime.now().isoformat(),
            })
            # Otomatik test/lint (kullanıcı /otomatiktest ile açtıysa)
            if fn_name in ("create_file", "edit_file") and getattr(self, "auto_test_enabled", False):
                changed_path = result.get("path", fn_args.get("path"))
                auto_results = run_auto_test_and_lint(
                    cwd=fn_args.get("cwd") or os.getcwd(), changed_path=changed_path
                )
                if auto_results:
                    result = dict(result)
                    result["auto_test_lint"] = auto_results

        # browser_screenshot özel durumu: görüntüyü ayrı bir inline_data
        # parçası olarak modele ekle, sonra geçici dosyayı hemen sil.
        image_bytes = None
        image_mime = None
        if fn_name == "browser_screenshot" and result.get("_temp_image") and result.get("path"):
            shot_path = result["path"]
            try:
                with open(shot_path, "rb") as f:
                    image_bytes = f.read()
                image_mime = _guess_mime_type(shot_path)
            except Exception:
                image_bytes = None
            finally:
                try:
                    os.remove(shot_path)
                except Exception:
                    pass
            result = {"success": True, "note": "Ekran görüntüsü alındı ve incelendikten sonra silindi."}

        fn_part = genai.protos.Part(
            function_response=genai.protos.FunctionResponse(
                name=fn_name,
                response={"result": result},
            )
        )
        return fn_part, image_bytes, image_mime

    def _send_streaming_round(self, outgoing, on_tool_call, on_text_delta, cancel_event=None):
        """
        İlk mesajı (ve gerekiyorsa tool-call sonrası devam mesajlarını) stream
        halinde gönderir. Her metin parçası geldiğinde on_text_delta çağrılır ve
        HER PARÇADAN SONRA cancel_event kontrol edilir; set edilmişse akış o anda
        (satır ortasında bile) sessizce kesilir.
        Dönüş: (birikmiş_metin, interrupted:bool)
        """
        final_text_parts = []
        interrupted = False
        current_send = outgoing
        max_rounds = 12
        rounds = 0

        while rounds < max_rounds:
            rounds += 1
            if cancel_event is not None and cancel_event.is_set():
                interrupted = True
                break

            stream = self.chat.send_message(current_send, stream=True)
            fn_calls = []
            round_text_parts = []
            last_usage = None

            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    interrupted = True
                    break
                usage = getattr(chunk, "usage_metadata", None)
                if usage:
                    last_usage = usage
                try:
                    candidate = chunk.candidates[0]
                    parts = candidate.content.parts
                except (IndexError, AttributeError):
                    continue
                for part in parts:
                    fc = getattr(part, "function_call", None)
                    if fc and fc.name:
                        fn_calls.append(fc)
                    else:
                        text = getattr(part, "text", None)
                        if text:
                            round_text_parts.append(text)
                            if on_text_delta:
                                on_text_delta(text)

            if last_usage is not None:
                try:
                    _, key_idx = self.key_manager.current_key()
                    record_usage(
                        key_idx, self.model_name,
                        getattr(last_usage, "prompt_token_count", 0) or 0,
                        getattr(last_usage, "candidates_token_count", 0) or 0,
                    )
                except Exception:
                    pass

            if round_text_parts:
                final_text_parts.append("".join(round_text_parts))

            if interrupted:
                break
            if not fn_calls:
                break

            # tool çağrılarını çalıştır
            tool_response_parts = []
            for fc in fn_calls:
                if cancel_event is not None and cancel_event.is_set():
                    interrupted = True
                    break
                fn_name = fc.name
                fn_args = dict(fc.args) if fc.args else {}
                if on_tool_call:
                    on_tool_call(fn_name, fn_args)

                fn_part, image_bytes, image_mime = self._execute_tool(fn_name, fn_args)
                tool_response_parts.append(fn_part)
                if image_bytes:
                    tool_response_parts.append(
                        genai.protos.Part(
                            inline_data=genai.protos.Blob(mime_type=image_mime, data=image_bytes)
                        )
                    )

            if interrupted or (cancel_event is not None and cancel_event.is_set()):
                interrupted = True
                break

            current_send = genai.protos.Content(parts=tool_response_parts)

        return "\n".join(final_text_parts).strip(), interrupted




# --------------------------------------------------------------------------
# Paralel Görev Çalıştırma (/paralel)
# --------------------------------------------------------------------------
# Her alt görev, ANA sohbetten bağımsız, kendi geçici ChatEngine'i (aynı anahtar
# havuzunu paylaşan ama ayrı bir 'chat' oturumu) üzerinden çalıştırılır; böylece
# görevler birbirini bloklamadan gerçekten paralel ilerler. Sonuçlar tamamlanınca
# tek tek özetlenip ekrana basılır.

def _run_single_parallel_task(key_manager, model_name, response_language, task_text, index, results, lock):
    label = f"Görev {index+1}"
    try:
        sub_engine = ChatEngine(key_manager, model_name, response_language=response_language,
                                 title=f"[paralel] {task_text[:40]}")
        reply, interrupted = sub_engine.send(task_text)
        with lock:
            results[index] = {"task": task_text, "reply": reply, "error": None}
    except Exception as e:
        with lock:
            results[index] = {"task": task_text, "reply": None, "error": str(e)}


def run_parallel_tasks(key_manager, model_name, response_language, tasks):
    """
    Verilen görev listesini (her biri ayrı bir string) eşzamanlı olarak çalıştırır.
    Her görev kendi thread'inde, kendi geçici sohbet oturumunda ilerler.
    Dönüş: [{"task", "reply", "error"}, ...] (tasks ile aynı sırada)
    """
    results = [None] * len(tasks)
    lock = threading.Lock()
    threads = []
    for i, t in enumerate(tasks):
        th = threading.Thread(
            target=_run_single_parallel_task,
            args=(key_manager, model_name, response_language, t, i, results, lock),
            daemon=True,
        )
        threads.append(th)
        th.start()
    for th in threads:
        th.join()
    return results


def prompt_parallel_tasks():
    """Kullanıcıdan art arda görev satırları alır (boş satır = bitir)."""
    key_listener_paused.set()
    try:
        cprint("\n[bold cyan]Paralel çalıştırılacak görevleri gir (her satır bir görev, "
               "bitirmek için boş satır):[/bold cyan]" if RICH_AVAILABLE else
               "\nParalel görevleri gir (boş satır = bitir):")
        tasks = []
        while True:
            line = input(f"  Görev {len(tasks)+1}: ").strip()
            if not line:
                break
            tasks.append(line)
        return tasks
    finally:
        key_listener_paused.clear()


# --------------------------------------------------------------------------
# Sesli Komut + TTS Yanıt (/ses)
# --------------------------------------------------------------------------

class VoiceController:
    """Mikrofon ile komut alma (SpeechRecognition) + TTS ile yanıt okuma (pyttsx3)."""

    def __init__(self):
        self.enabled = False
        self._tts_engine = None
        self._recognizer = None
        if SPEECH_RECOGNITION_AVAILABLE:
            self._recognizer = sr.Recognizer()
        if TTS_AVAILABLE:
            try:
                self._tts_engine = pyttsx3.init()
            except Exception:
                self._tts_engine = None

    def available(self):
        return SPEECH_RECOGNITION_AVAILABLE and self._recognizer is not None

    def listen_once(self, language="tr-TR", timeout=8):
        """Mikrofondan tek bir komut dinler, metne çevirip döner (hata olursa None)."""
        if not self.available():
            return None, "SpeechRecognition/pyaudio kurulu değil. Kurulum: pip install SpeechRecognition pyaudio"
        try:
            with sr.Microphone() as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=0.5)
                cprint("[dim]🎤 Dinliyorum...[/dim]" if RICH_AVAILABLE else "🎤 Dinliyorum...")
                audio = self._recognizer.listen(source, timeout=timeout, phrase_time_limit=20)
            text = self._recognizer.recognize_google(audio, language=language)
            return text, None
        except sr.WaitTimeoutError:
            return None, "Zaman aşımı, ses algılanmadı."
        except sr.UnknownValueError:
            return None, "Ses anlaşılamadı."
        except Exception as e:
            return None, str(e)

    def speak(self, text):
        """Verilen metni TTS ile seslendirir (kuruluysa)."""
        if not TTS_AVAILABLE or not self._tts_engine or not text:
            return False
        try:
            # Kod bloklarını ve markdown işaretlerini seslendirmeden önce temizle
            clean = re.sub(r"```.*?```", " kod bloğu ", text, flags=re.DOTALL)
            clean = re.sub(r"[*_#`]", "", clean)
            self._tts_engine.say(clean[:2000])
            self._tts_engine.runAndWait()
            return True
        except Exception:
            return False


_voice = VoiceController()


# --------------------------------------------------------------------------
# Dosya İzleme Modu (/izle)
# --------------------------------------------------------------------------
# watchdog kuruluysa gerçek zamanlı OS event'leri kullanılır; kurulu değilse
# basit bir polling (mtime karşılaştırma) döngüsüne düşülür. İkisi de aynı
# arayüzü (FileWatchSession) sunar.

class FileWatchSession:
    """Bir klasördeki değişiklikleri izler, her değişiklikte on_change(path, kind) çağırır."""

    def __init__(self, path, on_change, patterns=None):
        self.path = str(Path(path).expanduser())
        self.on_change = on_change
        self.patterns = patterns  # ör. ['*.py'] - None ise hepsi
        self._observer = None
        self._poll_thread = None
        self._stop_event = threading.Event()
        self._mtimes = {}

    def _matches(self, filename):
        if not self.patterns:
            return True
        return any(Path(filename).match(p) for p in self.patterns)

    def start(self):
        if WATCHDOG_AVAILABLE:
            handler_cls = FileSystemEventHandler
            outer = self

            class Handler(handler_cls):
                def on_modified(self, event):
                    if not event.is_directory and outer._matches(event.src_path):
                        outer.on_change(event.src_path, "değişti")

                def on_created(self, event):
                    if not event.is_directory and outer._matches(event.src_path):
                        outer.on_change(event.src_path, "oluşturuldu")

                def on_deleted(self, event):
                    if not event.is_directory and outer._matches(event.src_path):
                        outer.on_change(event.src_path, "silindi")

            self._observer = Observer()
            self._observer.schedule(Handler(), self.path, recursive=True)
            self._observer.start()
        else:
            # Basit polling: her 2 saniyede bir mtime'ları karşılaştır
            for p in Path(self.path).rglob("*"):
                if p.is_file() and self._matches(str(p)):
                    try:
                        self._mtimes[str(p)] = p.stat().st_mtime
                    except OSError:
                        pass

            def poll_loop():
                while not self._stop_event.wait(2.0):
                    try:
                        current = {}
                        for p in Path(self.path).rglob("*"):
                            if p.is_file() and self._matches(str(p)):
                                try:
                                    current[str(p)] = p.stat().st_mtime
                                except OSError:
                                    continue
                        for fp, mtime in current.items():
                            if fp not in self._mtimes:
                                self.on_change(fp, "oluşturuldu")
                            elif mtime != self._mtimes[fp]:
                                self.on_change(fp, "değişti")
                        for fp in list(self._mtimes):
                            if fp not in current:
                                self.on_change(fp, "silindi")
                        self._mtimes = current
                    except Exception:
                        pass

            self._poll_thread = threading.Thread(target=poll_loop, daemon=True)
            self._poll_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass
        self._observer = None


# ============================================================================
# GUI (CustomTkinter) — tek başına giriş noktası
# ============================================================================

# ============================================================================
# Renk / Tasarım Sabitleri
# ============================================================================

if CUSTOMTKINTER_AVAILABLE:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
else:
    print("HATA: 'customtkinter' kurulu değil. Kurmak için: pip install customtkinter")

# customtkinter kurulu değilse bile modül import edilebilsin (class tanımları
# NameError vermesin) diye taban sınıfları güvenli şekilde seçiyoruz.
_BaseFrame = ctk.CTkFrame if CUSTOMTKINTER_AVAILABLE else object
_BaseApp = ctk.CTk if CUSTOMTKINTER_AVAILABLE else object

COL_BG = "#0d0f16"
COL_SIDEBAR = "#12141e"
COL_SURFACE = "#1a1d2b"
COL_SURFACE_HOVER = "#232739"
COL_BUBBLE_USER = "#4453b0"
COL_BUBBLE_ASSISTANT = "#1c2032"
COL_BORDER = "#252a3d"
COL_ACCENT = "#818cf8"
COL_ACCENT_HOVER = "#6670e0"
COL_TEXT = "#eef0f7"
COL_TEXT_DIM = "#868ca8"
COL_TEXT_FAINT = "#5c6180"
COL_SUCCESS = "#34d399"
COL_WARN = "#fbbf5e"
COL_ERROR = "#f2596e"
FONT_FAMILY = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"

MODEL_DESCRIPTIONS = {
    "lite": "Günlük konuşma ve basit sorular için ideal; en hızlı yanıt verir.",
    "flash": "Kod yazma ve genel görevler için hız/kalite dengesi sunar (varsayılan).",
    "pro": "Karmaşık problemler ve derin analiz için en güçlü seçenek; daha yavaştır.",
}


# ============================================================================
# Vektörel ikon çizimi (emoji YOK, hepsi Canvas'ta çiziliyor)
# ============================================================================

def draw_icon(parent, kind, size=22, color=COL_TEXT, bg=None):
    """
    Verilen türde küçük bir ikonu bir CTkCanvas üzerine çizip döner.
    kind: 'shield' | 'clip' | 'gear' | 'plus' | 'arrow_up' | 'stop' | 'dot'
    """
    bg = bg or (parent.cget("fg_color") if hasattr(parent, "cget") else COL_BG)
    if isinstance(bg, (list, tuple)):
        bg = bg[-1]
    c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
    cx = cy = size / 2

    if kind == "shield":
        pts = [
            cx, cy - size * 0.42,
            cx + size * 0.34, cy - size * 0.28,
            cx + size * 0.34, cy + size * 0.06,
            cx, cy + size * 0.42,
            cx - size * 0.34, cy + size * 0.06,
            cx - size * 0.34, cy - size * 0.28,
        ]
        c.create_polygon(pts, fill=color, outline="", smooth=True)
        c.create_line(cx - size * 0.14, cy - size * 0.02, cx - size * 0.02, cy + size * 0.12,
                       cx + size * 0.18, cy - size * 0.14, fill=bg, width=max(2, size // 10),
                       capstyle="round", joinstyle="round")

    elif kind == "clip":
        r = size * 0.16
        c.create_arc(cx - r * 1.6, cy - size * 0.32, cx + r * 1.6, cy + r * 0.6,
                      start=200, extent=200, style="arc", outline=color, width=max(2, size // 11))
        c.create_line(cx + r * 1.55, cy - size * 0.05, cx - r * 0.2, cy + size * 0.30,
                       fill=color, width=max(2, size // 11), capstyle="round")
        c.create_arc(cx - r * 1.1, cy - size * 0.06, cx + r * 1.1, cy + size * 0.34,
                      start=20, extent=200, style="arc", outline=color, width=max(2, size // 11))

    elif kind == "gear":
        outer_r = size * 0.30
        inner_r = size * 0.13
        tooth_len = size * 0.10
        teeth = 8
        for i in range(teeth):
            angle = (2 * math.pi / teeth) * i
            x1 = cx + outer_r * math.cos(angle)
            y1 = cy + outer_r * math.sin(angle)
            x2 = cx + (outer_r + tooth_len) * math.cos(angle)
            y2 = cy + (outer_r + tooth_len) * math.sin(angle)
            c.create_line(x1, y1, x2, y2, fill=color, width=max(2, size // 8), capstyle="round")
        c.create_oval(cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r,
                       outline=color, width=max(2, size // 11), fill=bg)
        c.create_oval(cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r, fill=color, outline="")

    elif kind == "plus":
        arm = size * 0.28
        w = max(2, size // 7)
        c.create_line(cx - arm, cy, cx + arm, cy, fill=color, width=w, capstyle="round")
        c.create_line(cx, cy - arm, cx, cy + arm, fill=color, width=w, capstyle="round")

    elif kind == "arrow_up":
        s = size * 0.30
        c.create_polygon(cx, cy - s, cx + s, cy + s * 0.5, cx + s * 0.4, cy + s * 0.5,
                          cx + s * 0.4, cy + s, cx - s * 0.4, cy + s, cx - s * 0.4, cy + s * 0.5,
                          cx - s, cy + s * 0.5, fill=color, outline="", smooth=False)

    elif kind == "stop":
        s = size * 0.26
        c.create_rectangle(cx - s, cy - s, cx + s, cy + s, fill=color, outline="")

    elif kind == "dot":
        r = size * 0.5
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")

    elif kind == "expand":
        s = size * 0.30
        w = max(2, size // 9)
        for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            x0 = cx + dx * s
            y0 = cy + dy * s
            c.create_line(x0, y0, x0 - dx * s * 0.5, y0, fill=color, width=w, capstyle="round")
            c.create_line(x0, y0, x0, y0 - dy * s * 0.5, fill=color, width=w, capstyle="round")

    elif kind == "shrink":
        s = size * 0.30
        w = max(2, size // 9)
        for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            x0 = cx + dx * s * 0.5
            y0 = cy + dy * s * 0.5
            c.create_line(x0, y0, x0 + dx * s * 0.5, y0, fill=color, width=w, capstyle="round")
            c.create_line(x0, y0, x0, y0 + dy * s * 0.5, fill=color, width=w, capstyle="round")

    elif kind == "mic":
        w = size * 0.20
        h = size * 0.34
        c.create_rectangle(cx - w / 2, cy - h, cx + w / 2, cy + h * 0.15,
                            fill=color, outline="", width=0)
        c.create_arc(cx - w, cy - h * 0.2, cx + w, cy + h * 0.9,
                     start=200, extent=140, style="arc", outline=color, width=max(2, size // 11))
        c.create_line(cx, cy + h * 0.85, cx, cy + h * 1.15, fill=color, width=max(2, size // 11))
        c.create_line(cx - w * 0.7, cy + h * 1.15, cx + w * 0.7, cy + h * 1.15,
                       fill=color, width=max(2, size // 11), capstyle="round")

    return c


class IconButton(_BaseFrame):
    """Canvas ile çizilmiş bir ikonu daire arkaplan üzerinde buton gibi davranan widget."""

    def __init__(self, master, kind, command=None, size=40, icon_size=18,
                 fg_color=COL_SURFACE, hover_color=COL_SURFACE_HOVER, icon_color=COL_TEXT, **kwargs):
        super().__init__(master, width=size, height=size, fg_color=fg_color,
                          corner_radius=size // 2, **kwargs)
        self.grid_propagate(False)
        self.command = command
        self.kind = kind
        self.icon_color = icon_color
        self._normal_fg = fg_color
        self._hover_fg = hover_color

        self.canvas = draw_icon(self, kind, size=icon_size, color=icon_color, bg=fg_color)
        self.canvas.place(relx=0.5, rely=0.5, anchor="center")

        for widget in (self, self.canvas):
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

    def _on_click(self, _event):
        if self.command:
            self.command()

    def _on_enter(self, _event):
        self.configure(fg_color=self._hover_fg)
        self.canvas.configure(bg=self._hover_fg)

    def _on_leave(self, _event):
        self.configure(fg_color=self._normal_fg)
        self.canvas.configure(bg=self._normal_fg)

    def set_kind(self, kind, icon_color=None):
        self.canvas.destroy()
        self.icon_color = icon_color or self.icon_color
        self.canvas = draw_icon(self, kind, size=18, color=self.icon_color, bg=self.cget("fg_color"))
        self.canvas.place(relx=0.5, rely=0.5, anchor="center")
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)


# ============================================================================
# Sohbet balonu widget'ı
# ============================================================================

class ChatBubble(_BaseFrame):
    def __init__(self, master, role, text="", **kwargs):
        is_user = role == "user"
        is_system = role == "system"
        super().__init__(master, fg_color="transparent", **kwargs)

        bubble_color = COL_BUBBLE_USER if is_user else (COL_SURFACE if is_system else COL_BUBBLE_ASSISTANT)
        anchor_side = "e" if is_user else "w"
        text_color = COL_TEXT if not is_system else COL_TEXT_DIM

        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.pack(fill="x", padx=10, pady=4)

        label_name = {"user": "Sen", "assistant": "AEGIS", "system": "Sistem"}.get(role, role)
        if not is_system:
            ctk.CTkLabel(
                outer, text=label_name, font=(FONT_FAMILY, 11, "bold"),
                text_color=COL_ACCENT if is_user else COL_TEXT_DIM,
            ).pack(anchor=anchor_side, padx=14)

        self.bubble = ctk.CTkFrame(outer, fg_color=bubble_color, corner_radius=14,
                                    border_width=1, border_color=COL_BORDER)
        self.bubble.pack(anchor=anchor_side, padx=4, pady=(0, 2))

        self.text_var = text
        self.label = ctk.CTkLabel(
            self.bubble, text=text or "...", justify="left", anchor="w",
            font=(FONT_FAMILY, 13, "italic" if is_system else "normal"),
            text_color=text_color, wraplength=560,
        )
        self.label.pack(padx=14, pady=(9, 9) if is_system else (10, 10))

    def update_text(self, text):
        self.text_var = text
        self.label.configure(text=text or "...")


class BrowserBubble(_BaseFrame):
    """
    Sohbet akışının içine gömülü, canlı tarayıcı görünümü.
    - BrowserController'dan gelen her kareyi (yapay zekanın turuncu sanal imleci
      zaten çizilmiş halde) gösterir.
    - Kullanıcı görüntüye tıklarsa, gösterilen piksel konumunu gerçek sayfa
      koordinatına çevirip controller.user_click(x, y) çağırır (gerçek bir
      fare tıklaması olarak sayfaya gider — yapay zekanın sanal imlecinden ayrı).
    - Sağ üstteki buton görünümü ayrı bir tam ekran pencerede açar/kapatır.
    """

    DISPLAY_W = 520
    DISPLAY_H = 325  # 16:10 varsayılan yer tutucu oranı

    def __init__(self, master, controller, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.controller = controller
        self._ctk_image = None
        self._fs_ctk_image = None
        self._fs_window = None
        self._fs_image_label = None
        self._last_frame_size = (1280, 800)
        self._got_first_frame = False

        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.pack(fill="x", padx=10, pady=4)

        ctk.CTkLabel(outer, text="AEGIS — Tarayıcı", font=(FONT_FAMILY, 11, "bold"),
                     text_color=COL_TEXT_DIM).pack(anchor="w", padx=14)

        card = ctk.CTkFrame(outer, fg_color=COL_BUBBLE_ASSISTANT, corner_radius=14,
                            border_width=1, border_color=COL_BORDER)
        card.pack(anchor="w", padx=4, pady=(0, 2))

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(8, 0))
        self.status_label = ctk.CTkLabel(header, text="Bağlanılıyor...", font=(FONT_FAMILY, 11),
                                          text_color=COL_TEXT_DIM)
        self.status_label.pack(side="left")
        self.fs_button = IconButton(header, "expand", command=self.toggle_fullscreen,

                                     size=26, icon_size=14)
        self.fs_button.pack(side="right")

        self.image_label = ctk.CTkLabel(
            card, text="Görüntü bekleniyor...", cursor="hand2",
            width=self.DISPLAY_W, height=self.DISPLAY_H,
            fg_color=COL_SURFACE, corner_radius=8,
            font=(FONT_FAMILY, 12), text_color=COL_TEXT_DIM,
        )
        self.image_label.pack(padx=10, pady=8)
        self.image_label.bind("<Button-1>", self._on_click)

        type_row = ctk.CTkFrame(card, fg_color="transparent")
        type_row.pack(fill="x", padx=10, pady=(0, 10))
        self.type_entry = ctk.CTkEntry(type_row, placeholder_text="Sayfaya yazmak için buraya yaz, Enter'a bas...",
                                        fg_color=COL_SURFACE, border_color=COL_BORDER)
        self.type_entry.pack(fill="x")
        self.type_entry.bind("<Return>", self._on_type_enter)

        if controller is not None:
            controller.set_frame_callback(self.push_frame)
            if not PLAYWRIGHT_AVAILABLE:
                self.status_label.configure(text="Kurulum eksik", text_color=COL_ERROR)
                self.image_label.configure(
                    text="Tarayıcı kontrolü için 'playwright' gerekiyor.\n"
                         "Kurulum: pip install playwright && playwright install\n(Sonra AEGIS'i yeniden başlat.)",
                )
            elif not CV2_AVAILABLE:
                self.status_label.configure(text="Kurulum eksik", text_color=COL_ERROR)
                self.image_label.configure(
                    text="Canlı görüntü için 'opencv-python' ve 'numpy' gerekiyor.\n"
                         "Kurulum: pip install opencv-python numpy\n(Sonra AEGIS'i yeniden başlat.)",
                )
            else:
                self.after(5000, self._check_no_frame_timeout)

    def _check_no_frame_timeout(self):
        if not self._got_first_frame:
            self.status_label.configure(text="Görüntü gelmedi", text_color=COL_WARN)
            self.image_label.configure(
                text="Tarayıcıdan henüz görüntü gelmedi.\n"
                     "Tarayıcı gerçekten açıldı mı ve sayfa yüklendi mi kontrol et\n"
                     "(playwright kurulu mu: pip install playwright && playwright install).",
            )

    # -- kare akışı -----------------------------------------------------
    def push_frame(self, bgr_frame):
        """BrowserController'ın canlı izleme iş parçacığından çağrılır (arka planda).
        Tkinter'a güvenli şekilde ana iş parçacığından ulaşmak için `after` kullanılır."""
        try:
            h, w = bgr_frame.shape[:2]
            self._last_frame_size = (w, h)
            rgb = bgr_frame[:, :, ::-1]
            pil_img = Image.fromarray(rgb)
            disp_h = int(self.DISPLAY_W * h / w) if w else self.DISPLAY_H
            self.after(0, lambda: self._render(pil_img, disp_h))
        except Exception:
            pass

    def _render(self, pil_img, disp_h):
        try:
            self._got_first_frame = True
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img,
                                    size=(self.DISPLAY_W, disp_h))
            self._ctk_image = ctk_img
            self.image_label.configure(image=ctk_img, text="", width=self.DISPLAY_W, height=disp_h)
            self.status_label.configure(text="Canlı — turuncu imleç yapay zekaya ait", text_color=COL_TEXT_DIM)
            if self._fs_window is not None and self._fs_window.winfo_exists():
                fw = self._fs_window.winfo_width() or self.DISPLAY_W * 2
                fh = int(fw * disp_h / self.DISPLAY_W)
                fs_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(fw, fh))
                self._fs_ctk_image = fs_img
                self._fs_image_label.configure(image=fs_img)
        except Exception:
            pass

    # -- kullanıcı etkileşimi --------------------------------------------
    def _map_to_page(self, widget_x, widget_y, widget_w):
        page_w, page_h = self._last_frame_size
        scale = page_w / widget_w if widget_w else 1
        return widget_x * scale, widget_y * scale

    def _on_click(self, event):
        if not self.controller:
            return
        px, py = self._map_to_page(event.x, event.y, self.DISPLAY_W)
        threading.Thread(target=self.controller.user_click, args=(px, py), daemon=True).start()

    def _on_type_enter(self, event):
        if not self.controller:
            return
        text = self.type_entry.get()
        self.type_entry.delete(0, "end")
        if text:
            def _send():
                self.controller.user_type(text)
                self.controller.user_type("\n")
            threading.Thread(target=_send, daemon=True).start()

    # -- tam ekran --------------------------------------------------------
    def toggle_fullscreen(self):
        if self._fs_window is not None and self._fs_window.winfo_exists():
            self._fs_window.destroy()
            self._fs_window = None
            self._fs_image_label = None
            return

        win = ctk.CTkToplevel(self)
        win.title("AEGIS — Tarayıcı (tam ekran)")
        win.configure(fg_color=COL_BG)
        win.geometry("1200x800")
        self._fs_window = win

        top = ctk.CTkFrame(win, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(top, text="Canlı tarayıcı", font=(FONT_FAMILY, 13, "bold"),
                     text_color=COL_TEXT).pack(side="left")
        IconButton(top, "shrink", command=self.toggle_fullscreen, size=28, icon_size=14).pack(side="right")

        self._fs_image_label = ctk.CTkLabel(win, text="")
        self._fs_image_label.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._fs_image_label.bind("<Button-1>", self._on_fs_click)

        def _on_close():
            self._fs_window = None
            self._fs_image_label = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _on_fs_click(self, event):
        if not self.controller or self._fs_window is None:
            return
        widget_w = self._fs_image_label.winfo_width() or self.DISPLAY_W * 2
        px, py = self._map_to_page(event.x, event.y, widget_w)
        threading.Thread(target=self.controller.user_click, args=(px, py), daemon=True).start()


# ============================================================================
# Ana Uygulama
# ============================================================================

class AegisApp(_BaseApp):
    def __init__(self):
        super().__init__()
        self.title("AEGIS AGENT")
        self.geometry("1200x780")
        self.minsize(880, 580)
        self.configure(fg_color=COL_BG)

        self.cfg = load_config()
        if not self.cfg.get("api_keys"):
            self._first_time_setup_gui()

        self.key_manager = KeyManager(
            self.cfg["api_keys"], self.cfg.get("model", DEFAULT_MODEL),
            on_switch_callback=self._on_key_switch,
        )
        self.engine = ChatEngine(
            self.key_manager, self.cfg.get("model", DEFAULT_MODEL),
            response_language=self.cfg.get("response_language", "tr"),
        )

        self._ui_queue = queue.Queue()
        self._cancel_event = None
        self._busy = False
        self._pending_image_parts = None
        self._browser_bubble_shown = False
        self._steer_pending_text = None

        self._build_layout()
        self._refresh_key_summary()
        self._refresh_session_list()
        self.after(80, self._poll_queue)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # İlk kurulum
    # ------------------------------------------------------------------

    def _first_time_setup_gui(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("İlk Kurulum")
        dialog.geometry("480x420")
        dialog.grab_set()

        ctk.CTkLabel(dialog, text="AEGIS AGENT - İlk Kurulum",
                     font=(FONT_FAMILY, 16, "bold")).pack(pady=(18, 4))
        ctk.CTkLabel(dialog, text="Kaç adet Gemini API anahtarı gireceksin?",
                     text_color=COL_TEXT_DIM).pack(pady=(0, 10))

        count_var = ctk.StringVar(value=str(DEFAULT_KEY_COUNT))
        ctk.CTkEntry(dialog, textvariable=count_var, width=80, justify="center").pack(pady=(0, 10))

        keys_frame = ctk.CTkScrollableFrame(dialog, width=420, height=240)
        keys_frame.pack(fill="both", expand=True, padx=16, pady=10)
        entries = []

        def rebuild_fields():
            for w in keys_frame.winfo_children():
                w.destroy()
            entries.clear()
            try:
                n = max(1, int(count_var.get()))
            except ValueError:
                n = DEFAULT_KEY_COUNT
            for i in range(n):
                e = ctk.CTkEntry(keys_frame, placeholder_text=f"API Anahtarı #{i+1}", width=380)
                e.pack(pady=4)
                entries.append(e)

        ctk.CTkButton(dialog, text="Alan sayısını uygula", command=rebuild_fields,
                      fg_color=COL_SURFACE, hover_color=COL_ACCENT_HOVER, width=180).pack(pady=(0, 6))
        rebuild_fields()

        result = {"done": False}

        def finish():
            keys = [e.get().strip() for e in entries if e.get().strip()]
            if not keys:
                messagebox.showerror("Hata", "En az bir API anahtarı girmelisin.")
                return
            cfg = {
                "api_keys": keys, "model": DEFAULT_MODEL, "response_language": "tr",
                "browser_mode": "own", "created_at": datetime.now().isoformat(),
            }
            save_config(cfg)
            self.cfg = cfg
            result["done"] = True
            dialog.destroy()

        ctk.CTkButton(dialog, text="Kaydet ve Başla", command=finish,
                      fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER).pack(pady=12)
        self.wait_window(dialog)
        if not result["done"]:
            sys.exit(0)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ---- Sidebar (sade: logo, yeni sohbet, geçmiş, ayarlar) ----
        sidebar = ctk.CTkFrame(self, width=250, fg_color=COL_SIDEBAR, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nswe")
        sidebar.grid_propagate(False)

        header = ctk.CTkFrame(sidebar, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(20, 4))
        draw_icon(header, "shield", size=26, color=COL_ACCENT, bg=COL_SIDEBAR).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(header, text="AEGIS AGENT", font=(FONT_FAMILY, 18, "bold"),
                     text_color=COL_TEXT).pack(side="left")

        ctk.CTkLabel(sidebar, text="Kod odaklı yapay zeka ajanı", font=(FONT_FAMILY, 11),
                     text_color=COL_TEXT_DIM).pack(pady=(0, 18), padx=18, anchor="w")

        new_chat_row = ctk.CTkFrame(sidebar, fg_color=COL_ACCENT, corner_radius=10, height=40)
        new_chat_row.pack(fill="x", padx=18, pady=(0, 18))
        new_chat_row.pack_propagate(False)
        draw_icon(new_chat_row, "plus", size=16, color="#ffffff", bg=COL_ACCENT).place(x=16, rely=0.5, anchor="w")
        chat_lbl = ctk.CTkLabel(new_chat_row, text="Yeni Sohbet", font=(FONT_FAMILY, 13, "bold"), text_color="#ffffff")
        chat_lbl.place(relx=0.5, rely=0.5, anchor="center")
        for w in (new_chat_row, chat_lbl):
            w.bind("<Button-1>", lambda e: self._new_chat())

        ctk.CTkLabel(sidebar, text="GEÇMİŞ SOHBETLER", font=(FONT_FAMILY, 10, "bold"),
                     text_color=COL_TEXT_FAINT).pack(anchor="w", padx=18, pady=(2, 6))
        self.sessions_frame = ctk.CTkScrollableFrame(sidebar, fg_color=COL_SURFACE, corner_radius=10)
        self.sessions_frame.pack(fill="both", expand=True, padx=18, pady=(0, 14))

        # Alt bilgi: özet anahtar durumu + ayarlar butonu
        bottom_bar = ctk.CTkFrame(sidebar, fg_color="transparent")
        bottom_bar.pack(fill="x", padx=18, pady=(0, 18))
        self.key_summary_label = ctk.CTkLabel(bottom_bar, text="", font=(FONT_FAMILY, 11),
                                               text_color=COL_TEXT_DIM, anchor="w")
        self.key_summary_label.pack(fill="x", pady=(0, 8))

        settings_row = ctk.CTkFrame(bottom_bar, fg_color=COL_SURFACE, corner_radius=10, height=42)
        settings_row.pack(fill="x")
        settings_row.pack_propagate(False)
        draw_icon(settings_row, "gear", size=18, color=COL_TEXT_DIM, bg=COL_SURFACE).place(x=14, rely=0.5, anchor="w")
        settings_lbl = ctk.CTkLabel(settings_row, text="Ayarlar", font=(FONT_FAMILY, 13), text_color=COL_TEXT_DIM)
        settings_lbl.place(relx=0.5, rely=0.5, anchor="center")
        for w in (settings_row, settings_lbl):
            w.bind("<Button-1>", lambda e: self._open_settings())
            w.bind("<Enter>", lambda e: settings_row.configure(fg_color=COL_SURFACE_HOVER))
            w.bind("<Leave>", lambda e: settings_row.configure(fg_color=COL_SURFACE))

        # ---- Ana sohbet alanı ----
        main = ctk.CTkFrame(self, fg_color=COL_BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nswe")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)

        self.chat_scroll = ctk.CTkScrollableFrame(main, fg_color=COL_BG)
        self.chat_scroll.grid(row=0, column=0, sticky="nswe", padx=8, pady=(10, 0))
        self.chat_scroll.grid_columnconfigure(0, weight=1)

        info_row = ctk.CTkFrame(main, fg_color="transparent")
        info_row.grid(row=1, column=0, sticky="we", padx=18, pady=(6, 0))
        info_row.grid_columnconfigure(0, weight=1)
        self.model_desc_label = ctk.CTkLabel(info_row, text="", font=(FONT_FAMILY, 11),
                                              text_color=COL_TEXT_FAINT, anchor="w")
        self.model_desc_label.grid(row=0, column=0, sticky="w")
        self.status_label = ctk.CTkLabel(info_row, text="Hazır", font=(FONT_FAMILY, 11),
                                          text_color=COL_TEXT_DIM, anchor="e")
        self.status_label.grid(row=0, column=1, sticky="e")

        # Giriş çubuğu: [ataç] [textbox] [model] [gönder-ok]
        input_bar = ctk.CTkFrame(main, fg_color=COL_SURFACE, corner_radius=16,
                                  border_width=1, border_color=COL_BORDER)
        input_bar.grid(row=2, column=0, sticky="we", padx=14, pady=12)
        input_bar.grid_columnconfigure(1, weight=1)

        attach_holder = ctk.CTkFrame(input_bar, fg_color="transparent", width=40, height=40)
        attach_holder.grid(row=0, column=0, padx=(8, 2), pady=8)
        attach_holder.grid_propagate(False)
        self.attach_btn = IconButton(attach_holder, "clip", command=self._attach_file,
                                      size=38, icon_size=16, fg_color=COL_SURFACE,
                                      hover_color=COL_SURFACE_HOVER, icon_color=COL_TEXT_DIM)
        self.attach_btn.place(relx=0.5, rely=0.5, anchor="center")

        self.input_box = ctk.CTkTextbox(
            input_bar, height=42, fg_color="transparent", border_width=0,
            font=(FONT_FAMILY, 13), wrap="word",
        )
        self.input_box.grid(row=0, column=1, sticky="we", pady=8)
        self.input_box.bind("<Return>", self._on_enter_pressed)
        self.input_box.bind("<Shift-Return>", lambda e: None)

        model_holder = ctk.CTkFrame(input_bar, fg_color="transparent")
        model_holder.grid(row=0, column=2, padx=(4, 4), pady=8)
        self.model_menu = ctk.CTkOptionMenu(
            model_holder, values=["lite", "flash", "pro"], command=self._on_model_change,
            fg_color=COL_BG, button_color=COL_BG, button_hover_color=COL_SURFACE_HOVER,
            width=90, font=(FONT_FAMILY, 12),
        )
        self.model_menu.pack()
        self._sync_model_menu()

        send_holder = ctk.CTkFrame(input_bar, fg_color="transparent", width=44, height=44)
        send_holder.grid(row=0, column=3, padx=(2, 8), pady=8)
        send_holder.grid_propagate(False)
        self.send_btn = IconButton(send_holder, "arrow_up", command=self._send_message,
                                    size=42, icon_size=18, fg_color=COL_ACCENT,
                                    hover_color=COL_ACCENT_HOVER, icon_color="#ffffff")
        self.send_btn.place(relx=0.5, rely=0.5, anchor="center")

        self.attachment_bar = ctk.CTkLabel(main, text="", font=(FONT_FAMILY, 11),
                                            text_color=COL_ACCENT, anchor="w")
        self.attachment_bar.grid(row=3, column=0, sticky="we", padx=18, pady=(0, 8))

        self._add_bubble("system", "AEGIS Agent'a hoş geldin. Bir mesaj yaz ya da ataç ikonuyla dosya ekle.")
        self._update_model_description()

    # ------------------------------------------------------------------
    # Yardımcılar
    # ------------------------------------------------------------------

    def _add_bubble(self, role, text=""):
        bubble = ChatBubble(self.chat_scroll, role, text)
        bubble.pack(fill="x", expand=True)
        self._scroll_to_bottom()
        return bubble

    def _add_browser_bubble(self):
        bubble = BrowserBubble(self.chat_scroll, _browser)
        bubble.pack(fill="x", expand=True)
        self._scroll_to_bottom()
        return bubble

    def _scroll_to_bottom(self):
        def do_scroll():
            self.chat_scroll.update_idletasks()
            try:
                self.chat_scroll._parent_canvas.yview_moveto(1.0)
            except Exception:
                pass
        self.after(20, do_scroll)

    def _sync_model_menu(self):
        rev = {v: k for k, v in AVAILABLE_MODELS.items()}
        self.model_menu.set(rev.get(self.engine.model_name, "flash"))

    def _update_model_description(self):
        rev = {v: k for k, v in AVAILABLE_MODELS.items()}
        key = rev.get(self.engine.model_name, "flash")
        desc = MODEL_DESCRIPTIONS.get(key, "")
        self.model_desc_label.configure(text=f"{key}: {desc}")

    def _set_status(self, text, color=None):
        self.status_label.configure(text=text, text_color=color or COL_TEXT_DIM)

    def _set_busy(self, busy):
        self._busy = busy
        self.send_btn.set_kind("stop" if busy else "arrow_up", icon_color="#ffffff")
        self.send_btn.configure(fg_color=COL_ERROR if busy else COL_ACCENT)
        self.send_btn._normal_fg = COL_ERROR if busy else COL_ACCENT
        self.send_btn._hover_fg = COL_ERROR if busy else COL_ACCENT_HOVER

    # ------------------------------------------------------------------
    # Mesaj gönderme
    # ------------------------------------------------------------------

    def _on_enter_pressed(self, event):
        if event.state & 0x0001:
            return
        self._send_message()
        return "break"

    def _send_message(self):
        text = self.input_box.get("1.0", "end").strip()

        if self._busy:
            if self._cancel_event:
                self._cancel_event.set()
                self._set_status("Durduruldu, yeni talimat işleniyor...", COL_WARN)
            if text:
                # Kullanıcı, işlem sürerken (ör. tarayıcı bir şey yaparken) yeni bir
                # mesaj yazıp gönderdi: mevcut turu durdurup, hemen bu talimatla
                # devam edilecek şekilde kuyruğa alıyoruz (aynı sohbet bağlamında).
                self._steer_pending_text = text
                self.input_box.delete("1.0", "end")
            return

        if self._steer_pending_text is not None:
            text = self._steer_pending_text
            self._steer_pending_text = None
        if not text and not self._pending_image_parts:
            return
        self.input_box.delete("1.0", "end")

        self._add_bubble("user", text or "(dosya gönderildi)")
        assistant_bubble = self._add_bubble("assistant", "")

        image_parts = self._pending_image_parts
        self._pending_image_parts = None
        self.attachment_bar.configure(text="")

        self._cancel_event = threading.Event()
        self._set_busy(True)
        self._set_status("Yanıt bekleniyor...", COL_ACCENT)

        cancel_event = self._cancel_event
        acc = {"text": ""}

        def on_text_delta(chunk):
            acc["text"] += chunk
            self._ui_queue.put(("delta", assistant_bubble, acc["text"]))

        def on_tool_call(name, args):
            self._ui_queue.put(("tool", name, args))

        def on_key_switch(note):
            self._ui_queue.put(("note", note))

        def worker():
            try:
                reply, interrupted = self.engine.send(
                    text, on_tool_call=on_tool_call, on_text_delta=on_text_delta,
                    on_key_switch=on_key_switch, cancel_event=cancel_event,
                    image_parts=image_parts,
                )
                if interrupted:
                    self._ui_queue.put(("cancelled", assistant_bubble))
                else:
                    self._ui_queue.put(("done", assistant_bubble, reply))
            except Exception as e:
                self._ui_queue.put(("error", assistant_bubble, str(e), traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                item = self._ui_queue.get_nowait()
                kind = item[0]
                if kind == "delta":
                    _, bubble, text = item
                    bubble.update_text(text)
                    self._scroll_to_bottom()
                elif kind == "tool":
                    _, name, args = item
                    arg_str = json.dumps(args, ensure_ascii=False)[:120]
                    self._add_bubble("system", f"Araç çalıştırıldı: {name}({arg_str})")
                    if name.startswith("browser_") and not self._browser_bubble_shown:
                        self._browser_bubble_shown = True
                        self._add_browser_bubble()
                elif kind == "note":
                    _, note = item
                    self._add_bubble("system", note)
                    self._refresh_key_summary()
                elif kind == "done":
                    _, bubble, reply = item
                    bubble.update_text(reply or "(boş yanıt)")
                    self._set_busy(False)
                    self._set_status("Hazır", COL_TEXT_DIM)
                    self._refresh_session_list()
                    self._scroll_to_bottom()
                elif kind == "cancelled":
                    _, bubble = item
                    bubble.update_text((bubble.text_var or "") + "\n\n[kesildi]")
                    self._set_busy(False)
                    if self._steer_pending_text is not None:
                        self._set_status("Yeni talimatla devam ediliyor...", COL_ACCENT)
                        self.after(50, self._send_message)
                    else:
                        self._set_status("İptal edildi", COL_WARN)
                elif kind == "error":
                    _, bubble, msg, tb = item
                    bubble.update_text(f"Hata: {msg}")
                    print(tb)
                    self._set_busy(False)
                    self._set_status(f"Hata: {msg}", COL_ERROR)
        except queue.Empty:
            pass
        self.after(60, self._poll_queue)

    # ------------------------------------------------------------------
    # Dosya/görsel ekleme
    # ------------------------------------------------------------------

    def _attach_file(self):
        path = filedialog.askopenfilename(title="Dosya seç (görsel, PDF, kod, metin...)")
        if not path:
            return
        try:
            self._pending_image_parts = build_image_parts(path, "")
            self.attachment_bar.configure(text=f"Eklendi: {Path(path).name} (mesajınla birlikte gönderilecek)")
        except Exception as e:
            messagebox.showerror("Dosya Hatası", str(e))

    # ------------------------------------------------------------------
    # Anahtar durumu / oturumlar
    # ------------------------------------------------------------------

    def _on_key_switch(self, old_idx, new_idx, reason):
        note = self.engine.note_key_switch(old_idx, new_idx, reason)
        self._ui_queue.put(("note", f"{note}"))

    def _refresh_key_summary(self):
        rows = self.key_manager.status_table()
        active = sum(1 for r in rows if r[2] == "active")
        total = len(rows)
        color = COL_SUCCESS if active == total else (COL_WARN if active > 0 else COL_ERROR)
        self.key_summary_label.configure(text=f"API: {active}/{total} aktif", text_color=color)

    def _refresh_session_list(self):
        for w in self.sessions_frame.winfo_children():
            w.destroy()
        for s in list_sessions()[:30]:
            title = s.get("title") or "(başlıksız)"
            btn = ctk.CTkButton(
                self.sessions_frame, text=title[:32], anchor="w", height=28,
                fg_color="transparent", hover_color=COL_SURFACE_HOVER,
                font=(FONT_FAMILY, 11), text_color=COL_TEXT_DIM,
                command=lambda sid=s.get("id"): self._load_session(sid),
            )
            btn.pack(fill="x", pady=1)

    def _load_session(self, session_id):
        data = load_session_data(session_id)
        if not data:
            return
        self.engine = ChatEngine.from_session(
            self.key_manager, self.engine.model_name, data,
            response_language=self.engine.response_language,
        )
        for w in self.chat_scroll.winfo_children():
            w.destroy()
        for turn in self.engine.transcript:
            self._add_bubble(turn.get("role", "assistant"), turn.get("text", ""))
        self._set_status(f"Sohbet yüklendi: {data.get('title')}", COL_TEXT_DIM)
        self._sync_model_menu()
        self._update_model_description()

    def _new_chat(self):
        if self.engine.transcript:
            self.engine.save()
        self.engine = ChatEngine(
            self.key_manager, self.engine.model_name, response_language=self.engine.response_language,
        )
        for w in self.chat_scroll.winfo_children():
            w.destroy()
        self._add_bubble("system", "Yeni sohbet başlatıldı.")
        self._refresh_session_list()

    # ------------------------------------------------------------------
    # Model / dil değişimi
    # ------------------------------------------------------------------

    def _on_model_change(self, choice):
        real_model = AVAILABLE_MODELS.get(choice, choice)
        self.engine.model_name = real_model
        self.engine.key_manager.model_name = real_model
        self.engine._build_model()
        self.cfg["model"] = real_model
        save_config(self.cfg)
        self._update_model_description()
        self._add_bubble("system", f"Model değiştirildi: {real_model}")

    def _on_lang_change(self, choice):
        self.engine.response_language = choice
        self.engine._build_model()
        self.cfg["response_language"] = choice
        save_config(self.cfg)

    # ------------------------------------------------------------------
    # Ayarlar penceresi (çoğu ayar burada toplanıyor)
    # ------------------------------------------------------------------

    def _open_settings(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Ayarlar")
        dialog.geometry("440x620")
        dialog.grab_set()

        scroll = ctk.CTkScrollableFrame(dialog, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=4, pady=4)

        header = ctk.CTkFrame(scroll, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 10))
        draw_icon(header, "gear", size=20, color=COL_ACCENT, bg=COL_SURFACE if False else dialog.cget("fg_color")).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(header, text="Ayarlar", font=(FONT_FAMILY, 16, "bold")).pack(side="left")

        # Yanıt dili
        ctk.CTkLabel(scroll, text="Yanıt Dili", font=(FONT_FAMILY, 12, "bold")).pack(anchor="w", padx=20, pady=(6, 2))
        lang_menu = ctk.CTkOptionMenu(scroll, values=["tr", "en", "auto"], command=self._on_lang_change,
                                       fg_color=COL_SURFACE, button_color=COL_SURFACE,
                                       button_hover_color=COL_ACCENT_HOVER)
        lang_menu.set(self.engine.response_language)
        lang_menu.pack(fill="x", padx=20, pady=(0, 14))

        # Modeller (açıklamalı)
        ctk.CTkLabel(scroll, text="Modeller", font=(FONT_FAMILY, 12, "bold")).pack(anchor="w", padx=20, pady=(0, 2))
        for key, desc in MODEL_DESCRIPTIONS.items():
            row = ctk.CTkFrame(scroll, fg_color=COL_SURFACE, corner_radius=8)
            row.pack(fill="x", padx=20, pady=3)
            ctk.CTkLabel(row, text=key, font=(FONT_FAMILY, 12, "bold"), text_color=COL_ACCENT).pack(
                anchor="w", padx=12, pady=(6, 0))
            ctk.CTkLabel(row, text=desc, font=(FONT_FAMILY, 11), text_color=COL_TEXT_DIM,
                         wraplength=360, justify="left").pack(anchor="w", padx=12, pady=(0, 8))

        # API anahtarları
        ctk.CTkLabel(scroll, text="API Anahtarları", font=(FONT_FAMILY, 12, "bold")).pack(
            anchor="w", padx=20, pady=(14, 2))
        rows = self.key_manager.status_table()
        status_frame = ctk.CTkFrame(scroll, fg_color=COL_SURFACE, corner_radius=8)
        status_frame.pack(fill="x", padx=20, pady=(0, 6))
        for i, k, state, current in rows:
            r = ctk.CTkFrame(status_frame, fg_color="transparent")
            r.pack(fill="x", padx=10, pady=3)
            color = COL_SUCCESS if state == "active" else COL_ERROR
            draw_icon(r, "dot", size=10, color=color, bg=COL_SURFACE).pack(side="left", padx=(2, 8))
            label_text = f"#{i} {k}" + (" (aktif)" if current else "")
            ctk.CTkLabel(r, text=label_text, font=(FONT_FAMILY, 11),
                        text_color=COL_TEXT if current else COL_TEXT_DIM).pack(side="left")

        keys_box = ctk.CTkTextbox(scroll, height=110)
        keys_box.pack(fill="x", padx=20, pady=(4, 4))
        keys_box.insert("1.0", "\n".join(self.cfg.get("api_keys", [])))
        ctk.CTkLabel(scroll, text="(her satıra bir anahtar; kaydedince uygulanır)",
                    font=(FONT_FAMILY, 10), text_color=COL_TEXT_FAINT).pack(anchor="w", padx=20)

        def save_keys():
            keys = [line.strip() for line in keys_box.get("1.0", "end").splitlines() if line.strip()]
            if not keys:
                messagebox.showerror("Hata", "En az bir anahtar olmalı.")
                return
            self.cfg["api_keys"] = keys
            save_config(self.cfg)
            self.key_manager.stop()
            self.key_manager = KeyManager(keys, self.engine.model_name, on_switch_callback=self._on_key_switch)
            self.engine.key_manager = self.key_manager
            self.engine._configure_current_key()
            self.engine._build_model()
            self._refresh_key_summary()
            messagebox.showinfo("Tamam", "Anahtarlar güncellendi, sohbet bağlamı korundu.")

        ctk.CTkButton(scroll, text="Anahtarları Kaydet", command=save_keys,
                     fg_color=COL_ACCENT, hover_color=COL_ACCENT_HOVER).pack(pady=(6, 16))

        # Tarayıcı
        ctk.CTkLabel(scroll, text="Tarayıcı", font=(FONT_FAMILY, 12, "bold")).pack(anchor="w", padx=20)
        ctk.CTkLabel(
            scroll, justify="left", anchor="w", wraplength=420, text_color=COL_TEXT_DIM,
            text=("AEGIS kendi dahili tarayıcısını kullanır (sistem Chrome/Edge'e dokunmaz). "
                  "Girdiğin siteler ve oturumlar (girişler dahil) kalıcı profilde saklanır, "
                  "AEGIS'i kapatıp açsan bile hatırlanır."),
        ).pack(fill="x", padx=20, pady=(4, 4))

        def reset_browser_profile():
            if _browser.page:
                _browser.close()
            try:
                shutil.rmtree(BROWSER_PROFILE_DIR, ignore_errors=True)
                messagebox.showinfo("Tamam", "Tarayıcı profili sıfırlandı (tüm girişler/çerezler silindi).")
            except Exception as e:
                messagebox.showerror("Hata", str(e))

        ctk.CTkButton(scroll, text="Tarayıcı Profilini Sıfırla", command=reset_browser_profile,
                     fg_color=COL_SURFACE, hover_color=COL_ACCENT_HOVER).pack(pady=(2, 14))

        # Otomatik test/lint
        auto_test_var = ctk.BooleanVar(value=self.engine.auto_test_enabled)

        def toggle_auto_test():
            self.engine.auto_test_enabled = auto_test_var.get()

        ctk.CTkCheckBox(
            scroll, text="Dosya düzenlemesinden sonra otomatik test/lint çalıştır",
            variable=auto_test_var, command=toggle_auto_test,
        ).pack(anchor="w", padx=20, pady=(4, 14))

        ctk.CTkLabel(
            scroll, text="Not: /paralel, /git, /ses, /izle, /hafiza, /plugins\n"
                        "komutları şu an sadece terminal versiyonunda mevcut.",
            font=(FONT_FAMILY, 10), text_color=COL_TEXT_FAINT, justify="left",
        ).pack(anchor="w", padx=20, pady=(6, 16))

    # ------------------------------------------------------------------

    def _on_close(self):
        try:
            if self.engine.transcript:
                self.engine.save()
            self.key_manager.stop()
        except Exception:
            pass
        self.destroy()


def main():
    if not CUSTOMTKINTER_AVAILABLE:
        print("HATA: 'customtkinter' kurulu değil. Kurmak için: pip install customtkinter")
        sys.exit(1)
    app = AegisApp()
    app.mainloop()


if __name__ == "__main__":
    main()