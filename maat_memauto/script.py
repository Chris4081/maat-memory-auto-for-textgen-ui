# extensions/maat_memauto/script.py
# MAAT MemAuto – Automatic Memory Extension for Text-Generation-WebUI
# Copyright (C) 2025  Chris4081
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.
# See <https://www.gnu.org/licenses/>.
# MAAT Memory (auto)
# - input_modifier: injiziert Zeit/Datum + passende Erinnerungen (sichtbar)
# - custom_generate_chat_prompt: injiziert dieselben Infos in den HIDDEN-Kontext
# - output_modifier: speichert automatisch, wenn das Modell "save: (...)" etc. ausgibt
# - ui(): Verwaltung (EN/DE), inkl. Guide-Editor & Diagnostik
# Storage: user_data/maat_memauto/memories.json

import os, io, re, json, threading, html, hashlib, shutil
from datetime import datetime
import gradio as gr

# ─────────────────────────────────────────────────────────────────────────────
# Pfade & Konstanten
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.join("user_data", "maat_memauto")
MEM_PATH   = os.path.join(BASE_DIR, "memories.json")
SCHEMA_VERSION = 1
SUPPORTED_LANGS = ["en", "de", "es", "fr", "pt", "it", "pl", "cs"]
# Session-Flags (once-per-session)
_SESSION = {"guide_injected": False}
_GUIDE_MARKER = "[MAAT-MEMORY-GUIDE v1]"

# Heuristiken zum Filtern schlechter Memories (Optional, anpassbar)
MIN_MEMORY_LEN = 18
BAN_PHRASES = [
    "we need to ask", "we will ask", "we cannot because",
    "after we know what to remember", "so not"
]
ALLOW_SENTENCE_END = True

# Laufzeit-Diagnostik (für den UI-Diagnostics-Tab)
last_injected_memories = []
last_injected_chars    = 0

# ─────────────────────────────────────────────────────────────────────────────
# Defaults / Params
# ─────────────────────────────────────────────────────────────────────────────
DEFAULTS = {
    "version": SCHEMA_VERSION,
    "timecontext": True,
    "datecontext": True,
    "debug": False,
    "max_context_chars": 1200,
    "max_show_memories": 8,

    # Einträge: {memory:str, keywords:str, always:bool, created_at:iso}
    "pairs": [],

    # Guide-Injection
    "inject_guide": True,
    "guide_lang": "en",         # "en" | "de"
    "guide_once": True,         # 1x pro Session
    "guide_mode": "trigger",    # "trigger" | "always"
    "hint_on_triggers": True,
    "guide_triggers": [
        "merke", "merk dir", "erinnere", "speichere",
        "remember", "store", "save this", "note this"
    ],
    "guide_custom": { "de": "", "en": "" },

    # Modell darf per "save:" schreiben?
    "allow_model_saves": True,

    # UI-Sprache
    "ui_lang": "en"
}

_params  = dict(DEFAULTS)
_IO_LOCK = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_storage():
    os.makedirs(BASE_DIR, exist_ok=True)
    if not os.path.exists(MEM_PATH):
        with io.open(MEM_PATH, "w", encoding="utf-8") as f:
            json.dump({"pairs": []}, f, ensure_ascii=False, indent=2)

def _debug(*a):
    if _params.get("debug"):
        print("[maat_memauto]", *a, flush=True)

def _coerce_bool(v, default=False):
    if isinstance(v, bool): return v
    if isinstance(v, str):  return v.strip().lower() in ("1","true","yes","y","on")
    return default

def _sanitize(data: dict):
    out = dict(DEFAULTS)
    if not isinstance(data, dict): return out
    out["version"]     = int(data.get("version", SCHEMA_VERSION))
    out["timecontext"] = _coerce_bool(data.get("timecontext", DEFAULTS["timecontext"]))
    out["datecontext"] = _coerce_bool(data.get("datecontext", DEFAULTS["datecontext"]))
    out["debug"]       = _coerce_bool(data.get("debug", DEFAULTS["debug"]))
    out["ui_lang"]     = (data.get("ui_lang") or "en").lower()

    for k in ("max_context_chars", "max_show_memories"):
        try: out[k] = max(0, int(data.get(k, DEFAULTS[k])))
        except Exception: out[k] = DEFAULTS[k]

    # Guide-Felder
    out["inject_guide"]   = _coerce_bool(data.get("inject_guide", DEFAULTS["inject_guide"]))
    out["guide_lang"]     = (data.get("guide_lang") or DEFAULTS["guide_lang"]).lower()
    out["guide_once"]     = _coerce_bool(data.get("guide_once", DEFAULTS["guide_once"]))
    out["guide_mode"]     = (data.get("guide_mode") or DEFAULTS["guide_mode"]).lower()
    out["hint_on_triggers"]= _coerce_bool(data.get("hint_on_triggers", DEFAULTS["hint_on_triggers"]))
    out["guide_triggers"] = [w.strip() for w in (data.get("guide_triggers") or DEFAULTS["guide_triggers"]) if w.strip()]
    out["guide_custom"]   = data.get("guide_custom") or {"de":"", "en":""}

    out["allow_model_saves"] = _coerce_bool(data.get("allow_model_saves", DEFAULTS["allow_model_saves"]))

    # Pairs
    clean, seen = [], set()
    for p in (data.get("pairs") or []):
        if not isinstance(p, dict): continue
        mem = str(p.get("memory","")).strip()
        if not mem: continue
        kws = str(p.get("keywords","")).strip()
        alw = _coerce_bool(p.get("always", False))
        key = (mem, kws, alw)
        if key in seen: continue
        seen.add(key)
        clean.append({
            "memory": mem,
            "keywords": kws,
            "always": alw,
            "created_at": p.get("created_at") or datetime.now().isoformat(timespec="seconds")
        })
    out["pairs"] = clean
    return out

def _load():
    _ensure_storage()
    with _IO_LOCK:
        try:
            with io.open(MEM_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    _params.update(_sanitize(raw))
    _debug("loaded:", {"pairs": len(_params["pairs"])})

def _save():
    _ensure_storage()
    data = {
        "version": SCHEMA_VERSION,
        "timecontext": _params["timecontext"],
        "datecontext": _params["datecontext"],
        "debug": _params["debug"],
        "ui_lang": _params.get("ui_lang","en"),
        "max_context_chars": _params["max_context_chars"],
        "max_show_memories": _params.get("max_show_memories", 8),

        "inject_guide": _params.get("inject_guide", True),
        "guide_lang": _params.get("guide_lang","en"),
        "guide_once": _params.get("guide_once", True),
        "guide_mode": _params.get("guide_mode","trigger"),
        "hint_on_triggers": _params.get("hint_on_triggers", True),
        "guide_triggers": _params.get("guide_triggers", []),
        "guide_custom": _params.get("guide_custom", {"de":"","en":""}),

        "allow_model_saves": _params.get("allow_model_saves", True),
        "pairs": _params["pairs"],
    }
    with _IO_LOCK:
        with io.open(MEM_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    _debug("saved")

# ─────────────────────────────────────────────────────────────────────────────
# Guide-Text (EN/DE) + Editor-API
# ─────────────────────────────────────────────────────────────────────────────
GUIDE_EN_DEFAULT = (
    "You can store memories by adding one command line to your reply:\n\n"
    "• JSON (preferred)\n"
    "  save: {\"memory\":\"<content>\",\"keywords\":\"kw1,kw2\",\"always\":false}\n\n"
    "• Key–Value (fallback)\n"
    "  save: memory=<text>, keywords=kw1,kw2, always=true\n\n"
    "• Short form\n"
    "  save: (short memory text)\n\n"
    "Rules:\n"
    "- Save only stable, helpful info (preferences, recurring goals, constraints).\n"
    "- No sensitive data without consent.\n"
    "- Keep it short & precise (≤ 1–5 sentences) per memory.\n"
    "- keywords: 1–5 focused triggers; use always=true only if broadly useful.\n\n"
    "Good examples:\n"
    "save: {\"memory\":\"User wants concise answers (≤5 sentences).\",\"keywords\":\"concise,short\",\"always\":true}\n"
    "save: {\"memory\":\"Project=Helios; Stack=Next.js+Supabase.\",\"keywords\":\"helios,project\"}\n"
    "save: memory=No emojis, keywords=emoji, always=true"
)

GUIDE_DE_DEFAULT = (
    "Du kannst Erinnerungen speichern, indem du eine einzelne Befehlszeile in deine Antwort einfügst:\n\n"
    "• JSON (bevorzugt)\n"
    "  save: {\"memory\":\"<Inhalt>\",\"keywords\":\"kw1,kw2\",\"always\":false}\n\n"
    "• Key–Value (Fallback)\n"
    "  save: memory=<text>, keywords=kw1,kw2, always=true\n\n"
    "• Kurzform\n"
    "  save: (kurzer Erinnerungstext)\n\n"
    "Regeln:\n"
    "- Speichere nur stabile, hilfreiche Informationen (Vorlieben, wiederkehrende Ziele, Randbedingungen).\n"
    "- Keine sensiblen Daten ohne Zustimmung.\n"
    "- Kurz & präzise (≤ 1–5 Sätze) je Memory.\n"
    "- keywords: 1–5 präzise Triggerwörter, always=true nur wenn global sinnvoll.\n\n"
    "Gute Beispiele:\n"
    "save: {\"memory\":\"User wünscht kurze Antworten (≤5 Sätze).\",\"keywords\":\"kurz,prägnant\",\"always\":true}\n"
    "save: {\"memory\":\"Projekt=Helios; Stack=Next.js+Supabase.\",\"keywords\":\"helios,projekt\"}\n"
    "save: memory=Keine Emojis verwenden, keywords=emoji, always=true"
)

GUIDE_ES_DEFAULT = (
    "Puedes guardar memorias añadiendo una sola línea de comando a tu respuesta:\n\n"
    "• JSON (preferido)\n"
    "  save: {\"memory\":\"<contenido>\",\"keywords\":\"kw1,kw2\",\"always\":false}\n\n"
    "• Clave–Valor (alternativa)\n"
    "  save: memory=<texto>, keywords=kw1,kw2, always=true\n\n"
    "• Forma corta\n"
    "  save: (texto corto de memoria)\n\n"
    "Reglas:\n"
    "- Guarda solo información estable y útil (preferencias, metas recurrentes, restricciones).\n"
    "- No guardes datos sensibles sin consentimiento.\n"
    "- Manténlo breve y preciso (≤ 1–5 frases) por memoria.\n"
    "- keywords: 1–5 disparadores precisos; usa always=true solo si es ampliamente útil.\n\n"
    "Buenos ejemplos:\n"
    "save: {\"memory\":\"El usuario quiere respuestas concisas (≤5 frases).\",\"keywords\":\"conciso,corto\",\"always\":true}\n"
    "save: {\"memory\":\"Proyecto=Helios; Stack=Next.js+Supabase.\",\"keywords\":\"helios,proyecto\"}\n"
    "save: memory=Sin emojis, keywords=emoji, always=true"
)

GUIDE_PT_DEFAULT = (
    "Você pode armazenar memórias adicionando uma única linha de comando à sua resposta:\n\n"
    "• JSON (preferido)\n"
    "  save: {\"memory\":\"<conteúdo>\",\"keywords\":\"kw1,kw2\",\"always\":false}\n\n"
    "• Chave–Valor (alternativa)\n"
    "  save: memory=<texto>, keywords=kw1,kw2, always=true\n\n"
    "• Forma curta\n"
    "  save: (texto curto da memória)\n\n"
    "Regras:\n"
    "- Salve apenas informações estáveis e úteis (preferências, metas recorrentes, restrições).\n"
    "- Não salve dados sensíveis sem consentimento.\n"
    "- Mantenha curto e preciso (≤ 1–5 frases) por memória.\n"
    "- keywords: 1–5 gatilhos precisos; use always=true apenas se for amplamente útil.\n\n"
    "Bons exemplos:\n"
    "save: {\"memory\":\"Usuário deseja respostas concisas (≤5 frases).\",\"keywords\":\"conciso,curto\",\"always\":true}\n"
    "save: {\"memory\":\"Projeto=Helios; Stack=Next.js+Supabase.\",\"keywords\":\"helios,projeto\"}\n"
    "save: memory=Sem emojis, keywords=emoji, always=true"
)

GUIDE_FR_DEFAULT = (
    "Vous pouvez enregistrer des mémoires en ajoutant une seule ligne de commande à votre réponse :\n\n"
    "• JSON (recommandé)\n"
    "  save: {\"memory\":\"<contenu>\",\"keywords\":\"kw1,kw2\",\"always\":false}\n\n"
    "• Clé–Valeur (solution de repli)\n"
    "  save: memory=<texte>, keywords=kw1,kw2, always=true\n\n"
    "• Forme courte\n"
    "  save: (texte court de mémoire)\n\n"
    "Règles :\n"
    "- N’enregistrez que des informations stables et utiles (préférences, objectifs récurrents, contraintes).\n"
    "- Pas de données sensibles sans consentement.\n"
    "- Gardez le tout bref et précis (≤ 1–5 phrases) par mémoire.\n"
    "- keywords : 1 à 5 déclencheurs précis ; utilisez always=true uniquement si c’est largement utile.\n\n"
    "Bons exemples :\n"
    "save: {\"memory\":\"L’utilisateur souhaite des réponses concises (≤5 phrases).\",\"keywords\":\"concis,court\",\"always\":true}\n"
    "save: {\"memory\":\"Projet=Helios; Stack=Next.js+Supabase.\",\"keywords\":\"helios,projet\"}\n"
    "save: memory=Pas d’emojis, keywords=emoji, always=true"
)

GUIDE_IT_DEFAULT = (
    "Puoi salvare le memorie aggiungendo una singola riga di comando alla tua risposta:\n\n"
    "• JSON (preferito)\n"
    "  save: {\"memory\":\"<contenuto>\",\"keywords\":\"kw1,kw2\",\"always\":false}\n\n"
    "• Chiave–Valore (alternativa)\n"
    "  save: memory=<testo>, keywords=kw1,kw2, always=true\n\n"
    "• Forma breve\n"
    "  save: (breve testo della memoria)\n\n"
    "Regole:\n"
    "- Salva solo informazioni stabili e utili (preferenze, obiettivi ricorrenti, vincoli).\n"
    "- Non salvare dati sensibili senza consenso.\n"
    "- Mantieni il testo breve e preciso (≤ 1–5 frasi) per ogni memoria.\n"
    "- keywords: 1–5 parole chiave mirate; usa always=true solo se ampiamente utile.\n\n"
    "Esempi validi:\n"
    "save: {\"memory\":\"L’utente desidera risposte concise (≤5 frasi).\",\"keywords\":\"conciso,breve\",\"always\":true}\n"
    "save: {\"memory\":\"Progetto=Helios; Stack=Next.js+Supabase.\",\"keywords\":\"helios,progetto\"}\n"
    "save: memory=Nessuna emoji, keywords=emoji, always=true"
)

GUIDE_PL_DEFAULT = (
    "Możesz zapisywać wspomnienia, dodając jedną linię polecenia do swojej odpowiedzi:\n\n"
    "• JSON (preferowane)\n"
    "  save: {\"memory\":\"<treść>\",\"keywords\":\"kw1,kw2\",\"always\":false}\n\n"
    "• Klucz–Wartość (alternatywa)\n"
    "  save: memory=<tekst>, keywords=kw1,kw2, always=true\n\n"
    "• Krótka forma\n"
    "  save: (krótki tekst pamięci)\n\n"
    "Zasady:\n"
    "- Zapisuj tylko stabilne i przydatne informacje (preferencje, powtarzające się cele, ograniczenia).\n"
    "- Nie zapisuj danych wrażliwych bez zgody.\n"
    "- Zachowaj zwięzłość i precyzję (≤ 1–5 zdań) na jedną pamięć.\n"
    "- keywords: 1–5 dokładnych słów kluczowych; always=true używaj tylko, jeśli jest to szeroko przydatne.\n\n"
    "Dobre przykłady:\n"
    "save: {\"memory\":\"Użytkownik chce zwięzłych odpowiedzi (≤5 zdań).\",\"keywords\":\"zwięzłe,krótkie\",\"always\":true}\n"
    "save: {\"memory\":\"Projekt=Helios; Stack=Next.js+Supabase.\",\"keywords\":\"helios,projekt\"}\n"
    "save: memory=Bez emotikonów, keywords=emoji, always=true"
)

GUIDE_CS_DEFAULT = (
    "Můžete ukládat vzpomínky přidáním jediného příkazového řádku do své odpovědi:\n\n"
    "• JSON (preferované)\n"
    "  save: {\"memory\":\"<obsah>\",\"keywords\":\"kw1,kw2\",\"always\":false}\n\n"
    "• Klíč–Hodnota (alternativa)\n"
    "  save: memory=<text>, keywords=kw1,kw2, always=true\n\n"
    "• Krátká forma\n"
    "  save: (krátký text paměti)\n\n"
    "Pravidla:\n"
    "- Ukládejte pouze stabilní a užitečné informace (preferencí, opakující se cíle, omezení).\n"
    "- Neukládejte citlivá data bez souhlasu.\n"
    "- Držte to krátké a přesné (≤ 1–5 vět) na jednu paměť.\n"
    "- keywords: 1–5 přesných spouštěčů; always=true použijte jen, pokud je to obecně užitečné.\n\n"
    "Dobré příklady:\n"
    "save: {\"memory\":\"Uživatel chce stručné odpovědi (≤5 vět).\",\"keywords\":\"stručné,krátké\",\"always\":true}\n"
    "save: {\"memory\":\"Projekt=Helios; Stack=Next.js+Supabase.\",\"keywords\":\"helios,projekt\"}\n"
    "save: memory=Bez emotikonů, keywords=emoji, always=true"
)

_GUIDE_SUPPORTED = ["en","de","es","fr","pt","it","pl","cs"]

def _get_guide_text(lang: str = "en") -> str:
    lang = (lang or "en").lower()
    # 1) Benutzerdefinierter Text (falls gesetzt)
    custom_map = (_params.get("guide_custom") or {})
    custom_txt = (custom_map.get(lang) or "").strip()
    guide_body = custom_txt if custom_txt else _guide_default_for(lang)
    # 2) Marker vorschalten, um Doppel-Injection zu vermeiden
    return f"{_GUIDE_MARKER}\n{guide_body}".strip()

def _set_guide_text(lang: str, txt: str):
    lang = (lang or "en").lower()
    gc = dict.fromkeys(_GUIDE_SUPPORTED, "")
    gc.update(_params.get("guide_custom") or {})
    gc[lang] = (txt or "").strip()
    _params["guide_custom"] = gc
    _save()

def _guide_default_for(lang: str) -> str:
    lang = (lang or "en").lower()
    return {
        "en": GUIDE_EN_DEFAULT,
        "de": GUIDE_DE_DEFAULT,
        "es": GUIDE_ES_DEFAULT,
        "fr": GUIDE_FR_DEFAULT,
        "pt": GUIDE_PT_DEFAULT,
        "it": GUIDE_IT_DEFAULT,
        "pl": GUIDE_PL_DEFAULT,
        "cs": GUIDE_CS_DEFAULT,
    }.get(lang, GUIDE_EN_DEFAULT)

def _reset_guide(lang: str):
    # Auf Default zurücksetzen: einfach den Custom-Text leeren
    _set_guide_text(lang, "")

# ─────────────────────────────────────────────────────────────────────────────
# Matching / Utilities
# ─────────────────────────────────────────────────────────────────────────────
def _split_keywords(s: str):
    if not s: return []
    return [x.strip().lower() for x in re.split(r"[,\n]+", s) if x.strip()]

def _matches(text_lower: str, kw: str):
    if kw.startswith("r/") and kw.endswith("/") and len(kw) > 3:
        try:
            return re.search(kw[2:-1], text_lower, flags=re.IGNORECASE) is not None
        except re.error:
            return False
    return kw in text_lower

def _backup_memories():
    """Schreibt eine Sicherung der aktuellen memories.json."""
    try:
        _ensure_storage()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = os.path.join(BASE_DIR, f"memories.backup-{ts}.json")
        with _IO_LOCK:
            shutil.copy2(MEM_PATH, bak)
        _debug(f"backup written: {bak}")
        return bak
    except Exception as e:
        _debug(f"backup failed: {e}")
        return None

def _delete_all_memories():
    """Löscht alle Memory-Einträge (mit vorherigem Backup)."""
    bak = _backup_memories()
    _params["pairs"] = []
    _save()
    return bak

def _cap(text: str, max_chars: int):
    if max_chars <= 0 or len(text) <= max_chars: return text
    head = text[:max(0, max_chars-60)].rstrip()
    return head + "\n… [truncated context]"

def _cap_context_block(text: str, max_chars: int) -> str:
    try:
        return _cap(text, max_chars)
    except NameError:
        return text if max_chars <= 0 or len(text) <= max_chars else text[:max(0, max_chars - 1)].rstrip() + "…"

def _collect_memories_for(text: str, return_indices: bool = False):
    user_lower = (text or "").lower()
    picked, seen = [], set()
    for i, p in enumerate(_params.get("pairs", [])):
        kws = _split_keywords(p.get("keywords", ""))
        if p.get("always") or any(_matches(user_lower, kw) for kw in kws):
            m = (p.get("memory") or "").strip()
            if m and m not in seen:
                seen.add(m)
                picked.append((i, m) if return_indices else m)
    return picked

def _normalize_memory_text(s: str) -> str:
    s = html.unescape(s or "").strip()
    # umschließende Anführungszeichen oder Backticks löschen
    if (len(s) >= 2) and ((s[0] == s[-1] == '"') or
                          (s[0] == s[-1] == "'") or
                          (s[0] == s[-1] == '`')):
        s = s[1:-1].strip()
    return re.sub(r"\s+", " ", s)

def _is_relevant_memory(s: str) -> bool:
    s = (s or "").strip()
    if len(s) < 12:   # etwas großzügiger, z. B. min. 12 Zeichen
        return False

    low = s.lower()
    verbot = [
        "we need to ask", "we will ask", "we cannot because",
        "after we know what to remember", "so not"
    ]
    if any(p in low for p in verbot):
        return False

    # akzeptiere, wenn es ein Satzende hat ODER mind. 3 Wörter enthält
    words = [w for w in re.split(r"\s+", s) if w]
    if re.search(r"[.!?…]$", s) or len(words) >= 3:
        return True

    return False

def _append_memory(memory: str, keywords: str = "", always: bool = False):
    # HTML-Entities entfernen, Whitespace normalisieren
    memory = _normalize_memory_text(html.unescape(memory))

    # Wenn es nach JSON aussieht: versuchen, daraus memory/keywords/always zu holen
    try:
        if memory.startswith("{") and memory.endswith("}"):
            obj = json.loads(memory)
            if isinstance(obj, dict) and obj.get("memory"):
                memory = _normalize_memory_text(str(obj.get("memory", "")))
                if not keywords:
                    keywords = str(obj.get("keywords", "")).strip()
                if not always:
                    av = obj.get("always", False)
                    always = av if isinstance(av, bool) else str(av).strip().lower() in ("1","true","yes","y","on")
    except Exception:
        pass

    if not memory:
        return False, "⚠️ Empty memory."
    if not _is_relevant_memory(memory):
        return False, "⚠️ Filtered (short/irrelevant)."

    entry = {
        "memory": memory,
        "keywords": (keywords or "").strip(),
        "always": bool(always),
        "created_at": datetime.now().isoformat(timespec="seconds")
    }

    for p in _params.get("pairs", []):
        if (p.get("memory","").strip() == entry["memory"]
            and p.get("keywords","").strip().lower() == entry["keywords"].lower()
            and bool(p.get("always", False)) == entry["always"]):
            return False, "ℹ️ Already exists."

    _params.setdefault("pairs", []).append(entry)
    _save()
    return True, f"✅ Memory saved ({datetime.now().strftime('%H:%M')})"

    def _key(p):
        return (
            re.sub(r"\s+", " ", p.get("memory","").strip().lower()),
            p.get("keywords","").strip().lower(),
            bool(p.get("always", False))
        )

    new_key = _key(entry)
    for p in _params.get("pairs", []):
        if _key(p) == new_key:
            return False, "ℹ️ Already exists."

    _params.setdefault("pairs", []).append(entry)
    _save()
    ts = datetime.now().strftime("%H:%M")
    return True, f"✅ Memory saved ({ts})"

def _has_trigger(user_text: str, words=None) -> bool:
    s = (user_text or "").lower()
    words = words or _params.get("guide_triggers", [])
    for w in words:
        w = (w or "").strip().lower()
        if not w: continue
        try:
            if re.search(rf"\b{re.escape(w)}\b", s): return True
        except re.error:
            if w in s: return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# Regex für "save:"-Befehle (werden in PART 2 genutzt)
# ─────────────────────────────────────────────────────────────────────────────
_SAVE_SHORT_RE = re.compile(
    r"save\s*:\s*\((?P<memory>.*?)\)\s*(?:\[keywords=(?P<keywords>[^\]]*)\])?\s*(?:\[always=(?P<always>[^\]]*)\])?",
    re.IGNORECASE | re.DOTALL
)
_SAVE_JSON_RE  = re.compile(r"save\s*:\s*(\{.*?\})", re.IGNORECASE | re.DOTALL)
_SAVE_KV_RE    = re.compile(r"save\s*:\s*memory\s*=\s*(?P<memory>[^,\n]+?)\s*,\s*keywords\s*=\s*(?P<keywords>[^,\n]+?)\s*,\s*always\s*=\s*(?P<always>\S+)", re.IGNORECASE)

# ─────────────────────────────────────────────────────────────────────────────
# WebUI Hooks
# ─────────────────────────────────────────────────────────────────────────────
def input_modifier(user_input: str):
    global last_injected_memories, last_injected_chars

    blocks = []
    # 1) Zeit/Datum
    if _params.get("timecontext"):
        blocks.append(f"Current time: {datetime.now().strftime('%H:%M')}")
    if _params.get("datecontext"):
        blocks.append(f"Current date: {datetime.now().strftime('%B %d, %Y')}")

    # 2) Erinnerungen sammeln + sichtbar listen
    max_show = int(_params.get("max_show_memories", 8))
    mems = _collect_memories_for(user_input)
    if mems:
        header = f"[Memories loaded ({len(mems)})]"
        if len(mems) <= max_show:
            listed = "\n".join(f"- {m.strip()}" for m in mems)
            blocks.append(f"{header}\n{listed}")
        else:
            head = "\n".join(f"- {m.strip()}" for m in mems[:max_show])
            blocks.append(f"{header}\n{head}\n… (+{len(mems)-max_show} more)")

        if _params.get("debug"):
            print("[maat_memauto] matched memories:")
            for i, m in enumerate(mems, 1):
                print(f"  {i:02d}. {m}")
            print("[maat_memauto] end matched memories")

    # 3) Optionaler Guide (bei Trigger/once-per-session)
    if _params.get("hint_on_triggers", True) and _params.get("inject_guide", True):
        if _has_trigger(user_input, _params.get("guide_triggers", [])):
            if not (_params.get("guide_once", True) and _SESSION.get("guide_injected")):
                guide_text = _get_guide_text(_params.get("guide_lang","en"))
                blocks.append("[Memory Guide]\n" + guide_text)
                _SESSION["guide_injected"] = True
                if _params.get("debug"):
                    print("🧩 [maat_memauto] injected memory guide (trigger detected)")

    # 4) Zusammenbauen + limitieren
    if blocks:
        inj = _cap("\n\n".join(blocks).strip(), _params.get("max_context_chars", 1200))
        last_injected_memories = mems[:]   # für Diagnostics
        last_injected_chars = len(inj)
        if _params.get("debug"):
            print(f"🔵 [maat_memauto] injected {len(inj)} chars")
        return f"{inj}\n\n{user_input}"

    # Reset Diagnostics wenn nichts injiziert wurde
    last_injected_memories, last_injected_chars = [], 0
    return user_input

def custom_generate_chat_prompt(user_input, state, **kwargs):
    # Optional: Guide in HIDDEN-Kontext – je nach Modus
    try:
        if _params.get("inject_guide", True):
            mode = (_params.get("guide_mode") or "trigger").lower()
            inject_now = (mode == "always") or _has_trigger(user_input, _params.get("guide_triggers", []))
            if inject_now and not (_params.get("guide_once", True) and _SESSION.get("guide_injected")):
                guide_text = _get_guide_text(_params.get("guide_lang","en"))
                ctx = (state.get("context","") or "")
                if _GUIDE_MARKER not in ctx:
                    state["context"] = f"{guide_text}\n\n{ctx}".strip()
                    _SESSION["guide_injected"] = True
                    _debug("Guide injected into hidden context")
    except Exception as e:
        _debug("Guide inject error:", e)

    # Zeit/Datum + Memories in HIDDEN-Kontext
    lines = []
    if _params.get("timecontext"):
        lines.append(f"Current time: {datetime.now().strftime('%H:%M')}")
    if _params.get("datecontext"):
        lines.append(f"Current date: {datetime.now().strftime('%B %d, %Y')}")
    ms = _collect_memories_for(user_input)
    if ms:
        lines.append("[Memories]")
        lines.extend(ms)

    if lines:
        block = _cap_context_block("\n".join(lines).strip(), _params.get("max_context_chars", 1200))
        state["context"] = f"{block}\n\n{state.get('context','')}".strip()
        _debug("inject/context:", {"chars": len(block)})

    return None

# ─────────────────────────────────────────────────────────────────────────────
# Public actions
# ─────────────────────────────────────────────────────────────────────────────
def reload_memories_into_ki():
    _load()

# ─────────────────────────────────────────────────────────────────────────────
# Output-Postprocessing: "save: ..." finden, speichern, Tag aus Antwort entfernen
# ─────────────────────────────────────────────────────────────────────────────
_LAST_SAVE_FINGERPRINT = set()

def _parse_save_payload(raw: str):
    """
    Versucht mehrere Formate:
      1) JSON: {"memory": "...", "keywords": "a,b", "always": true}
      2) Key-Value: memory=..., keywords=a,b, always=true
      3) Plain: der gesamte Text ist memory
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    # WICHTIG: HTML-Entities entfernen (z.B. &quot;)
    raw = html.unescape(raw)

    # 1) JSON
    if raw.startswith("{") and raw.endswith("}"):
        try:
            obj = json.loads(raw)
            return {
                "memory": str(obj.get("memory", "")).strip(),
                "keywords": str(obj.get("keywords", "")).strip(),
                "always": bool(obj.get("always", False)),
            }
        except Exception:
            pass

    # 2) key=value, key=value ...
    if "=" in raw and "," in raw:
        parts = [p.strip() for p in raw.split(",")]
        kv = {}
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                kv[k.strip().lower()] = v.strip()
        if "memory" in kv or "keywords" in kv or "always" in kv:
            return {
                "memory": kv.get("memory", ""),
                "keywords": kv.get("keywords", ""),
                "always": str(kv.get("always", "")).strip().lower() in ("1", "true", "yes", "y", "on"),
            }

    # 3) plain memory
    return {"memory": raw, "keywords": "", "always": False}

def _fingerprint(save_dict):
    s = json.dumps(save_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

# Alle Save-Pattern, die wir entfernen/auswerten wollen
_SAVE_PATTERNS = [
    re.compile(r'(?is)\bsave\s*:\s*\((.*?)\)\s*'),
    re.compile(r'(?is)\bsave\s*:\s*\[(.*?)\]\s*'),
    re.compile(r'(?is)\bsave\s*:\s*({.*?})\s*'),
    re.compile(r'(?is)\bsave\s*:\s*(.+?)(?:\n|$)'),
]

def output_modifier(string):
    """
    Called after model output.
    Looks for 'save: (...) / [..] / {..}' and stores memory.
    Preserves keywords/always if provided as trailing [keywords=...] [always=...].
    """
    if not _params.get("allow_model_saves", True):
        return string

    original = string
    modified = string
    saves = []

    # Collect matches with their spans so we can examine trailing flags
    matches = []
    for pat in _SAVE_PATTERNS:
        for m in pat.finditer(modified):
            matches.append((pat, m.span(), m.group(1)))

    if not matches:
        return original

    # We will remove the save-tags as we go; do it from the end to keep spans valid
    matches.sort(key=lambda x: x[1][0], reverse=True)

    # Helper: extract trailing [keywords=...] and [always=...] after the match
    kw_re   = re.compile(r'\[\s*keywords\s*=\s*([^\]]+)\]', re.IGNORECASE)
    alw_re  = re.compile(r'\[\s*always\s*=\s*([^\]]+)\]', re.IGNORECASE)

    collected = []
    for pat, (start, end), payload in matches:
        # Look ahead a small window after the match for suffix flags
        tail = modified[end:end+200]  # should be plenty
        tail_kw  = None
        tail_alw = None

        mkw = kw_re.search(tail)
        if mkw:
            tail_kw = mkw.group(1).strip()
        malw = alw_re.search(tail)
        if malw:
            tail_alw = malw.group(1).strip().lower() in ("1","true","yes","y","on")

        collected.append((start, end, payload, tail_kw, tail_alw))

        # Remove the matched block including immediate trailing flag brackets if present
        cut_end = end
        # extend cut_end to include any immediate [keywords=...] / [always=...] blocks
        for mflag in re.finditer(r'\s*\[(?:keywords|always)\s*=\s*[^\]]+\]', tail, re.IGNORECASE):
            cut_end = end + mflag.end()
        pre = modified[:start]
        post = modified[cut_end:]
        # clean surrounding blank lines
        if pre.endswith("\n") and post.lstrip().startswith("\n"):
            post = post.lstrip()
        modified = (pre + post).strip()

    # Now process collected saves (in reverse we stripped; order doesn’t matter for saving)
    any_found = False
    for start, end, raw, tail_kw, tail_alw in reversed(collected):
        parsed = _parse_save_payload(raw)
        if not parsed:
            continue

        # If parser didn’t provide keywords/always, fill from tail flags
        if tail_kw and not parsed.get("keywords"):
            parsed["keywords"] = tail_kw
        if tail_alw is not None and "always" in parsed and parsed["always"] is False:
            parsed["always"] = bool(tail_alw)

        fp = _fingerprint(parsed)
        if fp in _LAST_SAVE_FINGERPRINT:
            continue
        _LAST_SAVE_FINGERPRINT.add(fp)

        ok, msg = _append_memory(
            memory=parsed.get("memory", ""),
            keywords=parsed.get("keywords", ""),
            always=parsed.get("always", False),
        )
        any_found = True
        if _params.get("debug", True):
            status = "✅" if ok else "ℹ️"
            print(f"{status} [Maat-Memory/save] {msg} :: {parsed}", flush=True)

    return modified if any_found else original

# ─────────────────────────────────────────────────────────────────────────────
# UI-Strings (EN/DE) + kleiner i18n-Helper
# ─────────────────────────────────────────────────────────────────────────────
UI_TXT = {
    "en": {
        "title": "## 🧠 MAAT Memory (auto)\nSave memories from the model with `save: ( ... )` and inject them into prompts.\nStorage: `user_data/maat_memauto/memories.json`",
        "tab_settings": "⚙️ Settings", "tab_guide": "📘 Guide", "tab_add": "➕ Add",
        "tab_list": "📋 List", "tab_edit": "✏️ Edit", "tab_delete": "🗑️ Delete", "tab_diag": "🩺 Diagnostics",
        "ui_lang": "UI language",
        "append_time": "Append current time", "append_date": "Append current date", "debug_logs": "Debug logs",
        "max_injected": "Max injected chars", "max_listed": "Max memories listed in prompt",
        "inject_guide": "Inject memory guide into context", "once_per_session": "Once per session", "guide_lang": "Guide language",
        "allow_model_save": "Allow model to save memories via `save:`",
        "triggers": "Trigger words (comma-separated)", "triggers_ph": "remember, memorize, note, remind me, merke, speichere, erinnere, ...",
        "reload_disk": "Reload from disk",
        "guide_edit_lang": "Edit language", "guide_text": "Guide text",
        "guide_save": "💾 Save guide", "guide_reset_curr": "↩ Reset this language to default", "guide_reset_both": "↩ Reset BOTH languages to default",
        "add_memory": "Memory", "add_keywords": "Keywords (comma-separated, or regex r/<pattern>/)", "add_always": "Always inject", "add_save": "Save",
        "add_saved_ok": "✅ Saved.", "add_need_mem": "⚠️ Please enter a memory.", "add_need_kw": "⚠️ Provide keywords or enable 'Always inject'.",
        "list_refresh": "Refresh", "list_headers": ["Memory","Keywords","Always"],
        "edit_select": "Select entry", "edit_memory": "Memory", "edit_keywords": "Keywords", "edit_always": "Always",
        "edit_apply": "Apply", "edit_updated": "✅ Updated.", "edit_need_select": "⚠️ Select an entry first.", "edit_reload_choices": "Reload choices",
        "del_select": "Select entry", "del_delete": "Delete", "del_deleted": "✅ Deleted.", "del_need_select": "⚠️ Select an entry first.",
        "del_invalid_idx": "⚠️ Invalid index.", "del_reload_choices": "Reload choices",
        "diag_injected": "Injected chars (last turn)", "diag_matched": "Matched memories (last turn)",
        "diag_refresh": "Refresh diagnostics", "diag_test_label": "Test match (type a user message to see which memories would match)",
        "diag_run_test": "Run test", "diag_last_mem_hdr": ["Last injected memories (this turn)"]
    },
    "de": {
        "title": "## 🧠 MAAT Memory (auto)\nSpeichere Erinnerungen mit `save: ( ... )` und injiziere sie in Prompts.\nAblage: `user_data/maat_memauto/memories.json`",
        "tab_settings": "⚙️ Einstellungen", "tab_guide": "📘 Anleitung", "tab_add": "➕ Hinzufügen",
        "tab_list": "📋 Liste", "tab_edit": "✏️ Bearbeiten", "tab_delete": "🗑️ Löschen", "tab_diag": "🩺 Diagnose",
        "ui_lang": "UI-Sprache",
        "append_time": "Aktuelle Zeit anhängen", "append_date": "Aktuelles Datum anhängen", "debug_logs": "Debug-Logs",
        "max_injected": "Max. injizierte Zeichen", "max_listed": "Max. Erinnerungen im Prompt auflisten",
        "inject_guide": "Memory-Guide in den Kontext injizieren", "once_per_session": "Einmal pro Sitzung", "guide_lang": "Guide-Sprache",
        "allow_model_save": "Modell darf via `save:` Erinnerungen speichern",
        "triggers": "Trigger-Wörter (kommagetrennt)", "triggers_ph": "remember, memorize, note, remind me, merke, speichere, erinnere, ...",
        "reload_disk": "Von Datenträger neu laden",
        "guide_edit_lang": "Sprache bearbeiten", "guide_text": "Guide-Text",
        "guide_save": "💾 Guide speichern", "guide_reset_curr": "↩ Diese Sprache auf Standard zurücksetzen", "guide_reset_both": "↩ BEIDE Sprachen auf Standard zurücksetzen",
        "add_memory": "Erinnerung", "add_keywords": "Schlüsselwörter (kommagetrennt oder Regex r/<pattern>/)", "add_always": "Immer injizieren", "add_save": "Speichern",
        "add_saved_ok": "✅ Gespeichert.", "add_need_mem": "⚠️ Bitte eine Erinnerung eingeben.", "add_need_kw": "⚠️ Keywords angeben oder 'Immer injizieren' aktivieren.",
        "list_refresh": "Aktualisieren", "list_headers": ["Erinnerung","Keywords","Immer"],
        "edit_select": "Eintrag wählen", "edit_memory": "Erinnerung", "edit_keywords": "Keywords", "edit_always": "Immer",
        "edit_apply": "Übernehmen", "edit_updated": "✅ Aktualisiert.", "edit_need_select": "⚠️ Bitte zuerst einen Eintrag wählen.", "edit_reload_choices": "Auswahl neu laden",
        "del_select": "Eintrag wählen", "del_delete": "Löschen", "del_deleted": "✅ Gelöscht.", "del_need_select": "⚠️ Bitte Eintrag wählen.",
        "del_invalid_idx": "⚠️ Ungültiger Index.", "del_reload_choices": "Auswahl neu laden",
        "diag_injected": "Injizierte Zeichen (letzte Runde)", "diag_matched": "Gematchte Erinnerungen (letzte Runde)",
        "diag_refresh": "Diagnose aktualisieren", "diag_test_label": "Test-Match (Text eingeben, um passende Erinnerungen zu sehen)",
        "diag_run_test": "Test ausführen", "diag_last_mem_hdr": ["Zuletzt injizierte Erinnerungen (diese Runde)"]
    }
}

UI_TXT["en"].update({
    "del_all_title":        "### 🧨 Delete ALL memories",
    "del_all_confirm":      "I confirm I want to delete ALL memories.",
    "del_all_button":       "🧨 Delete ALL now",
    "del_all_done":         "✅ All memories deleted.",
    "del_all_need_confirm": "⚠️ Please tick the confirmation first.",
    "del_all_backup":       "Backup created"
})

UI_TXT["de"].update({
    "del_all_title":        "### 🧨 Alle Erinnerungen löschen",
    "del_all_confirm":      "Ich bestätige, dass ich ALLE Erinnerungen löschen möchte.",
    "del_all_button":       "🧨 Jetzt ALLES löschen",
    "del_all_done":         "✅ Alle Erinnerungen wurden gelöscht.",
    "del_all_need_confirm": "⚠️ Bitte zuerst die Bestätigung anhaken.",
    "del_all_backup":       "Backup erstellt"
})

UI_TXT["es"] = {
    "title": "## 🧠 MAAT Memory (auto)\nGuarda recuerdos del modelo con `save: ( ... )` y añádelos a los mensajes.\nArchivo: `user_data/maat_memauto/memories.json`",
    "tab_settings": "⚙️ Ajustes", "tab_guide": "📘 Guía", "tab_add": "➕ Añadir",
    "tab_list": "📋 Lista", "tab_edit": "✏️ Editar", "tab_delete": "🗑️ Eliminar", "tab_diag": "🩺 Diagnóstico",
    "ui_lang": "Idioma de la interfaz",
    "append_time": "Añadir hora actual", "append_date": "Añadir fecha actual", "debug_logs": "Registros de depuración",
    "max_injected": "Máx. caracteres inyectados", "max_listed": "Máx. recuerdos listados en el prompt",
    "inject_guide": "Inyectar guía de memoria en el contexto", "once_per_session": "Una vez por sesión", "guide_lang": "Idioma de la guía",
    "allow_model_save": "Permitir que el modelo guarde recuerdos con `save:`",
    "triggers": "Palabras clave (separadas por comas)", "triggers_ph": "remember, memorize, note, remind me, ...",
    "reload_disk": "Recargar desde disco",
    "guide_edit_lang": "Editar idioma", "guide_text": "Texto de la guía",
    "guide_save": "💾 Guardar guía", "guide_reset_curr": "↩ Restablecer este idioma", "guide_reset_both": "↩ Restablecer AMBOS idiomas",
    "add_memory": "Recuerdo", "add_keywords": "Palabras clave (separadas por comas o regex r/<pattern>/)",
    "add_always": "Inyectar siempre", "add_save": "Guardar",
    "add_saved_ok": "✅ Guardado.", "add_need_mem": "⚠️ Por favor ingresa un recuerdo.", "add_need_kw": "⚠️ Indica palabras clave o activa 'Inyectar siempre'.",
    "list_refresh": "Actualizar", "list_headers": ["Recuerdo","Palabras clave","Siempre"],
    "edit_select": "Seleccionar entrada", "edit_memory": "Recuerdo", "edit_keywords": "Palabras clave",
    "edit_always": "Siempre", "edit_apply": "Aplicar", "edit_updated": "✅ Actualizado.", "edit_need_select": "⚠️ Selecciona una entrada primero.", "edit_reload_choices": "Recargar opciones",
    "del_select": "Seleccionar entrada", "del_delete": "Eliminar", "del_deleted": "✅ Eliminado.", "del_need_select": "⚠️ Selecciona una entrada primero.",
    "del_invalid_idx": "⚠️ Índice inválido.", "del_reload_choices": "Recargar opciones",
    "diag_injected": "Caracteres inyectados (última vez)", "diag_matched": "Recuerdos coincidentes (última vez)",
    "diag_refresh": "Actualizar diagnóstico", "diag_test_label": "Probar coincidencia (escribe un mensaje para ver qué recuerdos coinciden)",
    "diag_run_test": "Probar", "diag_last_mem_hdr": ["Últimos recuerdos inyectados (esta vez)"],
    "del_all_title": "### 🧨 Borrar TODOS los recuerdos",
    "del_all_confirm": "Confirmo que deseo borrar TODOS los recuerdos.",
    "del_all_button": "🧨 Borrar TODO ahora",
    "del_all_done": "✅ Todos los recuerdos han sido borrados.",
    "del_all_need_confirm": "⚠️ Marca la casilla de confirmación primero.",
    "del_all_backup": "Copia de seguridad creada"
}

UI_TXT["fr"] = {
    "title": "## 🧠 MAAT Memory (auto)\nEnregistrez des souvenirs du modèle avec `save: ( ... )` et injectez-les dans les invites.\nStockage : `user_data/maat_memauto/memories.json`",
    "tab_settings": "⚙️ Paramètres", "tab_guide": "📘 Guide", "tab_add": "➕ Ajouter",
    "tab_list": "📋 Liste", "tab_edit": "✏️ Éditer", "tab_delete": "🗑️ Supprimer", "tab_diag": "🩺 Diagnostic",
    "ui_lang": "Langue de l'interface",
    "append_time": "Ajouter l'heure actuelle", "append_date": "Ajouter la date actuelle", "debug_logs": "Journaux de débogage",
    "max_injected": "Caractères injectés max.", "max_listed": "Souvenirs max. listés dans l'invite",
    "inject_guide": "Injecter le guide de mémoire dans le contexte", "once_per_session": "Une fois par session", "guide_lang": "Langue du guide",
    "allow_model_save": "Autoriser le modèle à enregistrer des souvenirs via `save:`",
    "triggers": "Mots déclencheurs (séparés par des virgules)", "triggers_ph": "remember, memorize, note, remind me, ...",
    "reload_disk": "Recharger depuis le disque",
    "guide_edit_lang": "Modifier la langue", "guide_text": "Texte du guide",
    "guide_save": "💾 Enregistrer le guide", "guide_reset_curr": "↩ Réinitialiser cette langue", "guide_reset_both": "↩ Réinitialiser LES DEUX langues",
    "add_memory": "Souvenir", "add_keywords": "Mots-clés (séparés par des virgules ou regex r/<pattern>/)",
    "add_always": "Toujours injecter", "add_save": "Enregistrer",
    "add_saved_ok": "✅ Enregistré.", "add_need_mem": "⚠️ Veuillez saisir un souvenir.", "add_need_kw": "⚠️ Fournissez des mots-clés ou activez « Toujours injecter ».",
    "list_refresh": "Actualiser", "list_headers": ["Souvenir","Mots-clés","Toujours"],
    "edit_select": "Sélectionner une entrée", "edit_memory": "Souvenir", "edit_keywords": "Mots-clés",
    "edit_always": "Toujours", "edit_apply": "Appliquer", "edit_updated": "✅ Mis à jour.", "edit_need_select": "⚠️ Sélectionnez d'abord une entrée.", "edit_reload_choices": "Recharger les choix",
    "del_select": "Sélectionner une entrée", "del_delete": "Supprimer", "del_deleted": "✅ Supprimé.", "del_need_select": "⚠️ Sélectionnez d'abord une entrée.",
    "del_invalid_idx": "⚠️ Index invalide.", "del_reload_choices": "Recharger les choix",
    "diag_injected": "Caractères injectés (dernier tour)", "diag_matched": "Souvenirs correspondants (dernier tour)",
    "diag_refresh": "Actualiser le diagnostic", "diag_test_label": "Tester la correspondance (entrez un message pour voir les souvenirs correspondants)",
    "diag_run_test": "Lancer le test", "diag_last_mem_hdr": ["Derniers souvenirs injectés (ce tour)"],
    "del_all_title": "### 🧨 Supprimer TOUS les souvenirs",
    "del_all_confirm": "Je confirme vouloir supprimer TOUS les souvenirs.",
    "del_all_button": "🧨 Supprimer TOUT maintenant",
    "del_all_done": "✅ Tous les souvenirs ont été supprimés.",
    "del_all_need_confirm": "⚠️ Veuillez d'abord cocher la confirmation.",
    "del_all_backup": "Sauvegarde créée"
}

UI_TXT["it"] = {
    "title": "## 🧠 MAAT Memory (auto)\nSalva i ricordi del modello con `save: ( ... )` e inseriscili nei prompt.\nArchivio: `user_data/maat_memauto/memories.json`",
    "tab_settings": "⚙️ Impostazioni", "tab_guide": "📘 Guida", "tab_add": "➕ Aggiungi",
    "tab_list": "📋 Elenco", "tab_edit": "✏️ Modifica", "tab_delete": "🗑️ Elimina", "tab_diag": "🩺 Diagnostica",
    "ui_lang": "Lingua interfaccia",
    "append_time": "Aggiungi ora corrente", "append_date": "Aggiungi data corrente", "debug_logs": "Log di debug",
    "max_injected": "Max caratteri iniettati", "max_listed": "Max ricordi elencati nel prompt",
    "inject_guide": "Inietta la guida memoria nel contesto", "once_per_session": "Una volta per sessione", "guide_lang": "Lingua guida",
    "allow_model_save": "Consenti al modello di salvare ricordi tramite `save:`",
    "triggers": "Parole chiave (separate da virgola)", "triggers_ph": "remember, memorizza, nota, ricordami, ...",
    "reload_disk": "Ricarica da disco",
    "guide_edit_lang": "Modifica lingua", "guide_text": "Testo guida",
    "guide_save": "💾 Salva guida", "guide_reset_curr": "↩ Reimposta questa lingua", "guide_reset_both": "↩ Reimposta ENTRAMBE le lingue",
    "add_memory": "Ricordo", "add_keywords": "Parole chiave (separate da virgola o regex r/<pattern>/)",
    "add_always": "Inietta sempre", "add_save": "Salva",
    "add_saved_ok": "✅ Salvato.", "add_need_mem": "⚠️ Inserisci un ricordo.", "add_need_kw": "⚠️ Fornisci parole chiave o attiva 'Inietta sempre'.",
    "list_refresh": "Aggiorna", "list_headers": ["Ricordo","Parole chiave","Sempre"],
    "edit_select": "Seleziona voce", "edit_memory": "Ricordo", "edit_keywords": "Parole chiave",
    "edit_always": "Sempre", "edit_apply": "Applica", "edit_updated": "✅ Aggiornato.", "edit_need_select": "⚠️ Seleziona prima una voce.", "edit_reload_choices": "Ricarica scelte",
    "del_select": "Seleziona voce", "del_delete": "Elimina", "del_deleted": "✅ Eliminato.", "del_need_select": "⚠️ Seleziona prima una voce.",
    "del_invalid_idx": "⚠️ Indice non valido.", "del_reload_choices": "Ricarica scelte",
    "diag_injected": "Caratteri iniettati (ultimo turno)", "diag_matched": "Ricordi corrispondenti (ultimo turno)",
    "diag_refresh": "Aggiorna diagnostica", "diag_test_label": "Test corrispondenza (digita un messaggio per vedere i ricordi corrispondenti)",
    "diag_run_test": "Esegui test", "diag_last_mem_hdr": ["Ultimi ricordi iniettati (questo turno)"],
    "del_all_title": "### 🧨 Elimina TUTTI i ricordi",
    "del_all_confirm": "Confermo di voler eliminare TUTTI i ricordi.",
    "del_all_button": "🧨 Elimina TUTTO ora",
    "del_all_done": "✅ Tutti i ricordi sono stati eliminati.",
    "del_all_need_confirm": "⚠️ Spunta prima la conferma.",
    "del_all_backup": "Backup creato"
}

UI_TXT["pt"] = {
    "title": "## 🧠 MAAT Memory (auto)\nSalve memórias do modelo com `save: ( ... )` e injete-as nos prompts.\nArmazenamento: `user_data/maat_memauto/memories.json`",
    "tab_settings": "⚙️ Configurações", "tab_guide": "📘 Guia", "tab_add": "➕ Adicionar",
    "tab_list": "📋 Lista", "tab_edit": "✏️ Editar", "tab_delete": "🗑️ Excluir", "tab_diag": "🩺 Diagnóstico",
    "ui_lang": "Idioma da interface",
    "append_time": "Anexar hora atual", "append_date": "Anexar data atual", "debug_logs": "Logs de depuração",
    "max_injected": "Máx. caracteres injetados", "max_listed": "Máx. memórias listadas no prompt",
    "inject_guide": "Injetar guia de memória no contexto", "once_per_session": "Uma vez por sessão", "guide_lang": "Idioma do guia",
    "allow_model_save": "Permitir que o modelo salve memórias via `save:`",
    "triggers": "Palavras de gatilho (separadas por vírgulas)", "triggers_ph": "remember, memorizar, anotar, lembre-me, ...",
    "reload_disk": "Recarregar do disco",
    "guide_edit_lang": "Editar idioma", "guide_text": "Texto do guia",
    "guide_save": "💾 Salvar guia", "guide_reset_curr": "↩ Redefinir este idioma", "guide_reset_both": "↩ Redefinir AMBOS os idiomas",
    "add_memory": "Memória", "add_keywords": "Palavras-chave (separadas por vírgulas ou regex r/<pattern>/)",
    "add_always": "Sempre injetar", "add_save": "Salvar",
    "add_saved_ok": "✅ Salvo.", "add_need_mem": "⚠️ Insira uma memória.", "add_need_kw": "⚠️ Forneça palavras-chave ou ative 'Sempre injetar'.",
    "list_refresh": "Atualizar", "list_headers": ["Memória","Palavras-chave","Sempre"],
    "edit_select": "Selecionar entrada", "edit_memory": "Memória", "edit_keywords": "Palavras-chave",
    "edit_always": "Sempre", "edit_apply": "Aplicar", "edit_updated": "✅ Atualizado.", "edit_need_select": "⚠️ Selecione uma entrada primeiro.", "edit_reload_choices": "Recarregar opções",
    "del_select": "Selecionar entrada", "del_delete": "Excluir", "del_deleted": "✅ Excluído.", "del_need_select": "⚠️ Selecione uma entrada primeiro.",
    "del_invalid_idx": "⚠️ Índice inválido.", "del_reload_choices": "Recarregar opções",
    "diag_injected": "Caracteres injetados (última rodada)", "diag_matched": "Memórias correspondentes (última rodada)",
    "diag_refresh": "Atualizar diagnóstico", "diag_test_label": "Testar correspondência (digite uma mensagem para ver quais memórias corresponderiam)",
    "diag_run_test": "Executar teste", "diag_last_mem_hdr": ["Últimas memórias injetadas (esta rodada)"],
    "del_all_title": "### 🧨 Excluir TODAS as memórias",
    "del_all_confirm": "Confirmo que desejo excluir TODAS as memórias.",
    "del_all_button": "🧨 Excluir TUDO agora",
    "del_all_done": "✅ Todas as memórias foram excluídas.",
    "del_all_need_confirm": "⚠️ Marque a confirmação primeiro.",
    "del_all_backup": "Backup criado"
}

UI_TXT["cs"] = {
    "title": "## 🧠 MAAT Memory (auto)\nUkládejte vzpomínky modelu pomocí `save: ( ... )` a vkládejte je do promptů.\nUložení: `user_data/maat_memauto/memories.json`",
    "tab_settings": "⚙️ Nastavení", "tab_guide": "📘 Průvodce", "tab_add": "➕ Přidat",
    "tab_list": "📋 Seznam", "tab_edit": "✏️ Upravit", "tab_delete": "🗑️ Smazat", "tab_diag": "🩺 Diagnostika",
    "ui_lang": "Jazyk rozhraní",
    "append_time": "Připojit aktuální čas", "append_date": "Připojit aktuální datum", "debug_logs": "Ladicí záznamy",
    "max_injected": "Max. počet vložených znaků", "max_listed": "Max. počet vzpomínek v promptu",
    "inject_guide": "Vložit průvodce do kontextu", "once_per_session": "Jednou za relaci", "guide_lang": "Jazyk průvodce",
    "allow_model_save": "Povolit modelu ukládat vzpomínky přes `save:`",
    "triggers": "Spouštěcí slova (oddělená čárkou)", "triggers_ph": "remember, zapamatovat, uložit, připomeň, ...",
    "reload_disk": "Načíst z disku",
    "guide_edit_lang": "Upravit jazyk", "guide_text": "Text průvodce",
    "guide_save": "💾 Uložit průvodce", "guide_reset_curr": "↩ Obnovit tento jazyk", "guide_reset_both": "↩ Obnovit OBA jazyky",
    "add_memory": "Vzpomínka", "add_keywords": "Klíčová slova (čárkami oddělená nebo regex r/<pattern>/)",
    "add_always": "Vkládat vždy", "add_save": "Uložit",
    "add_saved_ok": "✅ Uloženo.", "add_need_mem": "⚠️ Zadejte prosím vzpomínku.", "add_need_kw": "⚠️ Zadejte klíčová slova nebo zapněte 'Vkládat vždy'.",
    "list_refresh": "Obnovit", "list_headers": ["Vzpomínka","Klíčová slova","Vždy"],
    "edit_select": "Vyberte položku", "edit_memory": "Vzpomínka", "edit_keywords": "Klíčová slova",
    "edit_always": "Vždy", "edit_apply": "Použít", "edit_updated": "✅ Aktualizováno.", "edit_need_select": "⚠️ Nejprve vyberte položku.", "edit_reload_choices": "Znovu načíst volby",
    "del_select": "Vyberte položku", "del_delete": "Smazat", "del_deleted": "✅ Smazáno.", "del_need_select": "⚠️ Nejprve vyberte položku.",
    "del_invalid_idx": "⚠️ Neplatný index.", "del_reload_choices": "Znovu načíst volby",
    "diag_injected": "Vložené znaky (poslední kolo)", "diag_matched": "Odpovídající vzpomínky (poslední kolo)",
    "diag_refresh": "Obnovit diagnostiku", "diag_test_label": "Test shody (napište zprávu pro zobrazení odpovídajících vzpomínek)",
    "diag_run_test": "Spustit test", "diag_last_mem_hdr": ["Naposledy vložené vzpomínky (toto kolo)"],
    "del_all_title": "### 🧨 Smazat VŠECHNY vzpomínky",
    "del_all_confirm": "Potvrzuji, že chci smazat VŠECHNY vzpomínky.",
    "del_all_button": "🧨 Smazat VŠE nyní",
    "del_all_done": "✅ Všechny vzpomínky byly smazány.",
    "del_all_need_confirm": "⚠️ Nejprve zaškrtněte potvrzení.",
    "del_all_backup": "Záloha vytvořena"
}

UI_TXT["pl"] = {
    "title": "## 🧠 MAAT Memory (auto)\nZapisuj wspomnienia modelu za pomocą `save: ( ... )` i wstawiaj je do promptów.\nPrzechowywanie: `user_data/maat_memauto/memories.json`",
    "tab_settings": "⚙️ Ustawienia", "tab_guide": "📘 Przewodnik", "tab_add": "➕ Dodaj",
    "tab_list": "📋 Lista", "tab_edit": "✏️ Edytuj", "tab_delete": "🗑️ Usuń", "tab_diag": "🩺 Diagnostyka",
    "ui_lang": "Język interfejsu",
    "append_time": "Dołącz bieżący czas", "append_date": "Dołącz bieżącą datę", "debug_logs": "Logi debugowania",
    "max_injected": "Maks. wstrzykniętych znaków", "max_listed": "Maks. liczba wspomnień w promptcie",
    "inject_guide": "Wstrzyknij przewodnik pamięci do kontekstu", "once_per_session": "Raz na sesję", "guide_lang": "Język przewodnika",
    "allow_model_save": "Pozwól modelowi zapisywać wspomnienia przez `save:`",
    "triggers": "Słowa wyzwalające (oddzielone przecinkami)", "triggers_ph": "remember, zapamiętaj, notuj, przypomnij, ...",
    "reload_disk": "Przeładuj z dysku",
    "guide_edit_lang": "Edytuj język", "guide_text": "Tekst przewodnika",
    "guide_save": "💾 Zapisz przewodnik", "guide_reset_curr": "↩ Przywróć ten język", "guide_reset_both": "↩ Przywróć OBA języki",
    "add_memory": "Wspomnienie", "add_keywords": "Słowa kluczowe (oddzielone przecinkami lub regex r/<pattern>/)",
    "add_always": "Zawsze wstrzykuj", "add_save": "Zapisz",
    "add_saved_ok": "✅ Zapisano.", "add_need_mem": "⚠️ Wpisz wspomnienie.", "add_need_kw": "⚠️ Podaj słowa kluczowe lub włącz 'Zawsze wstrzykuj'.",
    "list_refresh": "Odśwież", "list_headers": ["Wspomnienie","Słowa kluczowe","Zawsze"],
    "edit_select": "Wybierz wpis", "edit_memory": "Wspomnienie", "edit_keywords": "Słowa kluczowe",
    "edit_always": "Zawsze", "edit_apply": "Zastosuj", "edit_updated": "✅ Zaktualizowano.", "edit_need_select": "⚠️ Najpierw wybierz wpis.", "edit_reload_choices": "Przeładuj opcje",
    "del_select": "Wybierz wpis", "del_delete": "Usuń", "del_deleted": "✅ Usunięto.", "del_need_select": "⚠️ Najpierw wybierz wpis.",
    "del_invalid_idx": "⚠️ Nieprawidłowy indeks.", "del_reload_choices": "Przeładuj opcje",
    "diag_injected": "Wstrzyknięte znaki (ostatnia tura)", "diag_matched": "Pasujące wspomnienia (ostatnia tura)",
    "diag_refresh": "Odśwież diagnostykę", "diag_test_label": "Test dopasowania (wpisz wiadomość, aby zobaczyć pasujące wspomnienia)",
    "diag_run_test": "Uruchom test", "diag_last_mem_hdr": ["Ostatnio wstrzyknięte wspomnienia (ta tura)"],
    "del_all_title": "### 🧨 Usuń WSZYSTKIE wspomnienia",
    "del_all_confirm": "Potwierdzam, że chcę usunąć WSZYSTKIE wspomnienia.",
    "del_all_button": "🧨 Usuń WSZYSTKO teraz",
    "del_all_done": "✅ Wszystkie wspomnienia zostały usunięte.",
    "del_all_need_confirm": "⚠️ Najpierw zaznacz potwierdzenie.",
    "del_all_backup": "Utworzono kopię zapasową"
}


def _t(key: str) -> str:
    lang = (_params.get("ui_lang") or "en").lower()
    return UI_TXT.get(lang, UI_TXT["en"]).get(key, key)

def _save_guide(lang, text):
    _set_guide_text(lang or "en", text or "")
    return gr.update()

def _reset_curr(lang):
    _reset_guide(lang or "en")
    return _get_guide_text(lang or "en")

def _reset_both():
    for code in _GUIDE_SUPPORTED:
        _reset_guide(code)
    return _get_guide_text((dd_g_lang.value or "en") if 'dd_g_lang' in globals() else "en")
    
# ─────────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────────
def _rows():
    return [[p.get("memory",""), p.get("keywords",""), bool(p.get("always"))]
            for p in _params.get("pairs", [])]

def ui():
    _load()
    gr.Markdown(_t("title"))

    with gr.Tabs():
        # SETTINGS
        with gr.Tab(_t("tab_settings")):
            with gr.Row():
                dd_ui_lang = gr.Dropdown(
                    choices=["en","de","es","fr","pt","it","pl","cs"],
                    value=_params.get("ui_lang","en"),
                    label="UI language  (💾 restart server to apply)"
                )
                cb_time = gr.Checkbox(value=_params.get("timecontext", True),  label=_t("append_time"))
                cb_date = gr.Checkbox(value=_params.get("datecontext", True), label=_t("append_date"))
                cb_dbg  = gr.Checkbox(value=_params.get("debug", False),      label=_t("debug_logs"))
            with gr.Row():
                sl_max = gr.Slider(0, 4000, step=50, value=_params.get("max_context_chars", 1200),
                                   label=_t("max_injected"))
                sl_max_show = gr.Slider(1, 30, step=1, value=_params.get("max_show_memories", 8),
                                        label=_t("max_listed"))
            with gr.Row():
                cb_guide = gr.Checkbox(value=_params.get("inject_guide", True), label=_t("inject_guide"))
                cb_once  = gr.Checkbox(value=_params.get("guide_once", True),   label=_t("once_per_session"))
                dd_lang = gr.Dropdown(choices=["en","de","es","fr","pt","it","pl","cs"],
                                        value=_params.get("guide_lang", "en"),
                                        label=_t("guide_lang"))
            with gr.Row():
                cb_allow = gr.Checkbox(value=_params.get("allow_model_saves", True),
                                       label=_t("allow_model_save"))
                trigger_tb = gr.Textbox(
                    value=", ".join(_params.get("guide_triggers", [])),
                    label=_t("triggers"), placeholder=_t("triggers_ph")
                )

            def _apply_settings(ui, t, d, dbg, m, maxshow, g, o, glang, allow, trig_txt):
                _params["ui_lang"]            = ui or "en"
                _params["timecontext"]        = bool(t)
                _params["datecontext"]        = bool(d)
                _params["debug"]              = bool(dbg)
                _params["max_context_chars"]  = int(m)
                _params["max_show_memories"]  = int(maxshow)
                _params["inject_guide"]       = bool(g)
                _params["guide_once"]         = bool(o)
                _params["guide_lang"]         = glang or "en"
                _params["allow_model_saves"]  = bool(allow)
                _params["guide_triggers"]     = [w.strip() for w in (trig_txt or "").split(",") if w.strip()]
                _save()
                return {
                    cb_time:     gr.update(label=_t("append_time")),
                    cb_date:     gr.update(label=_t("append_date")),
                    cb_dbg:      gr.update(label=_t("debug_logs")),
                    sl_max:      gr.update(label=_t("max_injected")),
                    sl_max_show: gr.update(label=_t("max_listed")),
                    cb_guide:    gr.update(label=_t("inject_guide")),
                    cb_once:     gr.update(label=_t("once_per_session")),
                    dd_lang:     gr.update(label=_t("guide_lang")),
                    cb_allow:    gr.update(label=_t("allow_model_save")),
                    trigger_tb:  gr.update(label=_t("triggers"), placeholder=_t("triggers_ph")),
                }

            for comp in (dd_ui_lang, cb_time, cb_date, cb_dbg, cb_guide, cb_once, dd_lang, cb_allow, trigger_tb):
                comp.change(
                    _apply_settings,
                    [dd_ui_lang, cb_time, cb_date, cb_dbg, sl_max, sl_max_show, cb_guide, cb_once, dd_lang, cb_allow, trigger_tb],
                    outputs=[cb_time, cb_date, cb_dbg, sl_max, sl_max_show, cb_guide, cb_once, dd_lang, cb_allow, trigger_tb]
                )
            sl_max.release(_apply_settings, [dd_ui_lang, cb_time, cb_date, cb_dbg, sl_max, sl_max_show, cb_guide, cb_once, dd_lang, cb_allow, trigger_tb],
                           outputs=[cb_time, cb_date, cb_dbg, sl_max, sl_max_show, cb_guide, cb_once, dd_lang, cb_allow, trigger_tb])
            sl_max_show.release(_apply_settings, [dd_ui_lang, cb_time, cb_date, cb_dbg, sl_max, sl_max_show, cb_guide, cb_once, dd_lang, cb_allow, trigger_tb],
                                outputs=[cb_time, cb_date, cb_dbg, sl_max, sl_max_show, cb_guide, cb_once, dd_lang, cb_allow, trigger_tb])

            gr.Button(_t("reload_disk")).click(lambda: (_load(), None), outputs=[])

        # GUIDE
        with gr.Tab(_t("tab_guide")):
            dd_g_lang = gr.Dropdown(
                choices=["en","de","es","fr","pt","it","pl","cs"],
                value=_params.get("guide_lang", "en"),
                label=_t("guide_edit_lang")
            )
            tb_guide  = gr.Textbox(value=_get_guide_text(_params.get("guide_lang","en")), lines=18, label=_t("guide_text"))
            with gr.Row():
                btn_save_guide  = gr.Button(_t("guide_save"))
                btn_reset_curr  = gr.Button(_t("guide_reset_curr"))
                btn_reset_both  = gr.Button(_t("guide_reset_both"))

            def _load_guide(lang): return _get_guide_text(lang or "en")
            def _save_guide(lang, text): _set_guide_text(lang or "en", text or ""); return gr.update()
            def _reset_curr(lang): _reset_guide(lang or "en"); return _get_guide_text(lang or "en")
            def _reset_both(): _reset_guide("en"); _reset_guide("de"); return _get_guide_text(dd_g_lang.value or "en")

            dd_g_lang.change(_load_guide, [dd_g_lang], [tb_guide])
            btn_save_guide.click(_save_guide, [dd_g_lang, tb_guide], outputs=[])
            btn_reset_curr.click(_reset_curr, [dd_g_lang], [tb_guide])
            btn_reset_both.click(_reset_both, [], [tb_guide])

        # ADD
        with gr.Tab(_t("tab_add")):
            tb_mem = gr.Textbox(label=_t("add_memory"), lines=3, placeholder="Short memory text…")
            tb_kw  = gr.Textbox(label=_t("add_keywords"))
            cb_alw = gr.Checkbox(label=_t("add_always"), value=False)
            btn_add = gr.Button(_t("add_save"))
            out_add = gr.Markdown(visible=False)

            def _add(mem, kw, alw):
                mem = (mem or "").strip()
                if not mem:
                    return gr.update(value=_t("add_need_mem"), visible=True)
                if not kw and not alw:
                    return gr.update(value=_t("add_need_kw"), visible=True)
                if not any(mem == x.get("memory") for x in _params.get("pairs", [])):
                    _params.setdefault("pairs", []).append({
                        "memory": mem,
                        "keywords": (kw or "").strip(),
                        "always": bool(alw),
                        "created_at": datetime.now().isoformat(timespec="seconds")
                    })
                    _save()
                return gr.update(value=_t("add_saved_ok"), visible=True)

            btn_add.click(_add, [tb_mem, tb_kw, cb_alw], [out_add])

        # LIST
        with gr.Tab(_t("tab_list")):
            grid = gr.Dataframe(value=_rows(), headers=_t("list_headers"),
                                datatype=["str","str","bool"], interactive=False, wrap=True)
            gr.Button(_t("list_refresh")).click(lambda: _rows(), outputs=[grid])

        # EDIT
        with gr.Tab(_t("tab_edit")):
            def _choices():
                items = []
                for i, p in enumerate(_params.get("pairs", [])):
                    m = p.get("memory","")
                    label = (m[:48] + "…") if len(m) > 50 else m
                    items.append(f"{i}: {label}")
                return items

            dd = gr.Dropdown(choices=_choices(), label=_t("edit_select"))
            ed_mem = gr.Textbox(label=_t("edit_memory"), lines=3)
            ed_kw  = gr.Textbox(label=_t("edit_keywords"))
            ed_alw = gr.Checkbox(label=_t("edit_always"), value=False)
            btn_apply = gr.Button(_t("edit_apply"))
            out_edit  = gr.Markdown(visible=False)
            gr.Button(_t("edit_reload_choices")).click(lambda: gr.update(choices=_choices(), value=None), outputs=[dd])

            def _fill(sel):
                if not sel: return "", "", False
                idx = int(sel.split(":")[0])
                p = _params["pairs"][idx]
                return p.get("memory",""), p.get("keywords",""), bool(p.get("always"))

            def _upd(sel, m, k, a):
                if not sel:
                    return gr.update(value=_t("edit_need_select"), visible=True)
                idx = int(sel.split(":")[0])
                _params["pairs"][idx] = {
                    "memory": (m or "").strip(),
                    "keywords": (k or "").strip(),
                    "always": bool(a),
                    "created_at": _params["pairs"][idx].get("created_at") or datetime.now().isoformat(timespec="seconds")
                }
                _save()
                return gr.update(value=_t("edit_updated"), visible=True)

            dd.change(_fill, [dd], [ed_mem, ed_kw, ed_alw])
            btn_apply.click(_upd, [dd, ed_mem, ed_kw, ed_alw], [out_edit])

        # DELETE
        with gr.Tab(_t("tab_delete")):
            def _choices_del():
                items = []
                for i, p in enumerate(_params.get("pairs", [])):
                    m = p.get("memory","")
                    label = (m[:48] + "…") if len(m) > 50 else m
                    items.append(f"{i}: {label}")
                return items

            dd_del = gr.Dropdown(choices=_choices_del(), label=_t("del_select"))
            btn_del = gr.Button(_t("del_delete"))
            out_del = gr.Markdown(visible=False)
            gr.Button(_t("del_reload_choices")).click(
                lambda: gr.update(choices=_choices_del(), value=None),
                outputs=[dd_del]
            )

            def _delete(sel):
                if not sel:
                    return gr.update(value=_t("del_need_select"), visible=True)
                idx = int(sel.split(":")[0])
                if 0 <= idx < len(_params.get("pairs", [])):
                    del _params["pairs"][idx]
                    _save()
                    return gr.update(value=_t("del_deleted"), visible=True)
                return gr.update(value=_t("del_invalid_idx"), visible=True)

            btn_del.click(_delete, [dd_del], [out_del])

            # ─── Delete ALL memories ───
            gr.Markdown(_t("del_all_title"))
            confirm_all = gr.Checkbox(label=_t("del_all_confirm"))
            btn_del_all = gr.Button(_t("del_all_button"))
            out_del_all = gr.Markdown(visible=False)

            def _delete_all(confirm):
                if not confirm:
                    return (
                        gr.update(value=_t("del_all_need_confirm"), visible=True),
                        gr.update(choices=_choices_del(), value=None),
                    )
                bak = _backup_memories()        # schreibt optional ein Backup
                _params["pairs"] = []
                _save()
                msg = _t("del_all_done")
                if bak:
                    msg += f"  \n{_t('del_all_backup')}: `{os.path.basename(bak)}`"
                return (
                    gr.update(value=msg, visible=True),
                    gr.update(choices=_choices_del(), value=None),
                )

            btn_del_all.click(_delete_all, [confirm_all], [out_del_all, dd_del])

        # DIAGNOSTICS
        with gr.Tab(_t("tab_diag")):
            md_stats = gr.Markdown()
            df_last  = gr.Dataframe(headers=_t("diag_last_mem_hdr"), datatype=["str"], interactive=False, wrap=True)
            tb_test  = gr.Textbox(label=_t("diag_test_label"))
            out_test = gr.Dataframe(headers=["Would match"], datatype=["str"], interactive=False, wrap=True)
            btn_run  = gr.Button(_t("diag_run_test"))

            def _stats():
                return (f"**{_t('diag_injected')}:** {last_injected_chars}  \n"
                        f"**{_t('diag_matched')}:** {len(last_injected_memories)}")

            def _load_diag():
                return _stats(), [[m] for m in last_injected_memories]

            def _test_match(s):
                ms = _collect_memories_for(s or "")
                return [[m] for m in ms]

            gr.Button(_t("diag_refresh")).click(_load_diag, outputs=[md_stats, df_last])
            btn_run.click(_test_match, [tb_test], [out_test])

# ─────────────────────────────────────────────────────────────────────────────
# Auto init
# ─────────────────────────────────────────────────────────────────────────────
def _init():
    try:
        _load()
        print("[maat_memauto] ready ✓")
    except Exception as e:
        print(f"[maat_memauto] init error: {e}")

_init()