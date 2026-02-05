# main.py
# Agente Reclutador Conversacional (Excel) - FastAPI + Pandas + OpenAI
# Incluye:
# - prompts externos en /prompts
# - saludo inicial 1 vez por session_id (greeting.txt) SIN comerse la 1a pregunta
# - /chat      -> JSON (answer + rows opcional + query_spec siempre)
# - /chat_text -> SOLO TEXTO (text/plain)
# - /schema, /health
# - memoria por session_id
# - modo clarify/refine
# - Fix NaN en JSON: ORJSONResponse (NaN -> null)
# - Limpieza de NaN también en /chat_text (evita imprimir "nan")
# - Operadores: equals/contains/between/in + comparadores numéricos
# - Limpieza de filtros inválidos
# - ATAJO RUT: busca directo sin LLM si detecta un RUT
# - ATAJO RUT inteligente: si el usuario pide una columna específica, la devuelve
# - FALLBACK "inteligente": si spec.filters queda vacío => keyword search con scoring
# - NUEVO: strategy semantic/structured
# - NUEVO: semantic search ponderado (2 pasadas) priorizando títulos/cargo/org
# - NUEVO: respeta número pedido por el usuario (ej "dame 3", "necesito 10")

from __future__ import annotations

import json
import math
import os
import re
import sqlite3               # ✅ FIX: faltaba
import threading             # ✅ FIX: faltaba
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse, PlainTextResponse
from openai import OpenAI
from pydantic import BaseModel, Field

# ============================================================
# ✅ FIX 1: Definir RUT_REGEX (antes de usarlo)
# ============================================================
# Soporta:
#  - 12.345.678-9
#  - 12345678-9
#  - DV numérico o K/k
RUT_REGEX = re.compile(r"\b(\d{1,2}(?:\.\d{3}){2}-[\dkK]|\d{7,8}-[\dkK])\b")


# ---------------------------
# Config
# ---------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
EXCEL_PATH = os.getenv("EXCEL_PATH", "trabajadores.xlsx").strip()

if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY no está definido. /chat y /chat_text fallarán hasta configurarlo en .env")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

app = FastAPI(
    title="Agente Reclutador - Conversacional",
    version="3.5.0",
    default_response_class=ORJSONResponse,
)

# ✅ CORS: permite servir UI desde http://127.0.0.1:5500 (python http.server)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------
# Prompts externos
# ---------------------------
PROMPTS_PATH = Path("prompts")

def load_prompt(filename: str) -> str:
    path = PROMPTS_PATH / filename
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()

DEFAULT_GREETING = "Hola 👋 Soy Reclutin. ¿En qué te puedo ayudar hoy?"

# ---------------------------
# Carga Excel
# ---------------------------
try:
    df = pd.read_excel(EXCEL_PATH, engine="openpyxl")
except Exception as e:
    df = None
    EXCEL_LOAD_ERROR = f"No pude leer el Excel '{EXCEL_PATH}'. Error: {e}"
else:
    EXCEL_LOAD_ERROR = None
    df.columns = [str(c).strip() for c in df.columns]
    df = df.where(pd.notnull(df), None)

# ---------------------------
# Operadores permitidos
# ---------------------------
ALLOWED_OPS = {
    "equals", "contains", "between", "in",
    "is_true", "is_false", "is_null", "not_null",
    "greater_than", "less_than", "greater_or_equal", "less_or_equal",
}

# ---------------------------
# Semantic search weighting (tu base real)
# ---------------------------
PROFILE_COLS_HIGH = [
    "Título Profesional 1","Título Profesional 2","Título Profesional 3","Título Profesional 4",
    "Título Técnico 1","Título Técnico 2","Título Técnico 3","Título Técnico 4",
    "Efobech","Efobech Centro de Formación",
]
PROFILE_COLS_MED = [
    "Cargo Actual","Categoria Puesto","Nivel",
    "Unidad Organizativa","Subgerencia","Gerencia","División","Nombre Planta",
]
PROFILE_COLS_LOW = [
    "Nombre",
]

WEIGHTS: Dict[str, float] = {}
for c in PROFILE_COLS_HIGH:
    WEIGHTS[c] = 4.0 if "Título" in c else 3.0  # títulos 4x, efobech 3x
for c in PROFILE_COLS_MED:
    WEIGHTS[c] = 3.0 if c in {"Cargo Actual","Categoria Puesto","Nivel"} else 2.0
for c in PROFILE_COLS_LOW:
    WEIGHTS[c] = 1.5

# ---------------------------
# Sesiones (memoria simple)
# ---------------------------
sessions: Dict[str, Dict[str, Any]] = {}

def get_session(session_id: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    if not session_id:
        session_id = str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = {
            "messages": [],
            "last_spec": None,
            "pending_clarify": None,
            "greeted": False,
        }
    return session_id, sessions[session_id]

# ---------------------------
# Modelos API
# ---------------------------
class ChatRequest(BaseModel):
    question: str = Field(..., description="Pregunta en lenguaje natural (español)")
    session_id: Optional[str] = Field(default=None, description="ID de sesión para mantener conversación")
    limit: Optional[int] = Field(default=None, description="Opcional: fuerza el límite de resultados")
    include_rows: bool = Field(default=True, description="Devuelve filas además del texto (útil para UI)")

# ---------------------------
# Helpers
# ---------------------------
def _ensure_loaded():
    if df is None:
        raise HTTPException(status_code=500, detail=EXCEL_LOAD_ERROR or "Excel no cargado")

def _ensure_parent_dir(path: str) -> None:
    """✅ FIX: esta función faltaba y la usa SQLite_PATH (storage/...)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

def _to_bool(x: Any) -> Optional[bool]:
    if x is None:
        return None
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in {"si", "sí", "s", "true", "1", "x"}:
        return True
    if s in {"no", "false", "0"}:
        return False
    return None

def clean_value(v: Any) -> Any:
    """Evita 'nan' y 'inf' en text/plain y también en rows devueltos."""
    try:
        if isinstance(v, float) and (pd.isna(v) or math.isinf(v)):
            return None
    except Exception:
        pass
    return v

def clean_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        out.append({k: clean_value(v) for k, v in r.items()})
    return out

# ---------- Normalización de texto ----------
def normalize_text(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).lower().strip()
    s = "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^\w\s\.\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# ---------- Respeta número pedido ----------
LIMIT_PATTERNS = [
    re.compile(r"\b(?:dame|trae|muestra|quiero|necesito|busco|listar|listame)\s+(\d{1,3})\b", re.I),
    re.compile(r"\b(\d{1,3})\s+(?:personas|personas|trabajadores|trabajador|perfiles|resultados|nombres)\b", re.I),
    re.compile(r"\bsolo\s+(\d{1,3})\b", re.I),
    re.compile(r"\bsolo\s+(?:un|una)\b", re.I),
]

def infer_user_limit(question: str, default_limit: int = 3, min_limit: int = 1, max_limit: int = 100) -> int:
    if not question:
        return default_limit
    q = question.strip()

    # Soporta "solo un/una" (sin número explícito)
    if re.search(r"\bsolo\s+(?:un|una)\b", q, re.I):
        return 1

    for pat in LIMIT_PATTERNS:
        m = pat.search(q)
        if m:
            try:
                n = int(m.group(1))
                return max(min_limit, min(max_limit, n))
            except Exception:
                pass

    return default_limit

# ============================================================
# ✅ FIX 2: extract_rut_from_text consistente con la regex
# ============================================================
def extract_rut_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = RUT_REGEX.search(text)
    return m.group(1).strip() if m else None

def normalize_rut(rut: Any) -> str:
    """✅ safe: soporta float/NaN/None."""
    if rut is None:
        return ""
    try:
        if isinstance(rut, float) and pd.isna(rut):
            return ""
    except Exception:
        pass
    s = str(rut).strip().replace(".", "").replace(" ", "")
    return s.lower()

# ---------------------------
# Selección inteligente de columnas pedidas por el usuario
# ---------------------------
COLUMN_ALIASES = {
    # títulos / formación
    "titulo profesional": ["Título Profesional 1", "Título Profesional 2", "Título Profesional 3", "Título Profesional 4"],
    "título profesional": ["Título Profesional 1", "Título Profesional 2", "Título Profesional 3", "Título Profesional 4"],
    "casa estudio profesional": ["Casa Estudio Profesional 1", "Casa Estudio Profesional 2", "Casa Estudio Profesional 3", "Casa Estudio Profesional 4"],
    "titulo tecnico": ["Título Técnico 1", "Título Técnico 2", "Título Técnico 3", "Título Técnico 4"],
    "título técnico": ["Título Técnico 1", "Título Técnico 2", "Título Técnico 3", "Título Técnico 4"],
    "casa estudio tecnico": ["Casa Estudio Técnico 1", "Casa Estudio Técnico 2", "Casa EstudioTécnico 3", "Casa Estudio Técnico 4"],

    # sed
    "sed a-2024": ["Sed A-2024"],
    "sed s-2024": ["Sed S-2024"],
    "sed a 2024": ["Sed A-2024"],
    "sed s 2024": ["Sed S-2024"],
    "sed a-2023": ["Sed A-2023"],
    "sed s-2023": ["Sed S-2023"],
    "sed a-2022": ["Sed A-2022"],

    # ranking
    "ranking 2 sem 2024": ["Ranking2°Sem2024"],
    "ranking 1 sem 2024": ["Ranking1°Sem2024"],
    "ranking 2 sem 2023": ["Ranking2°Sem2023"],
    "ranking 1 sem 2023": ["Ranking1°Sem2023"],

    # sanción
    "sancion vigente": ["Sancion Vigente", "Fecha término sanción vigente"],
    "sanción vigente": ["Sancion Vigente", "Fecha término sanción vigente"],

    # org
    "unidad": ["Unidad Organizativa"],
    "subgerencia": ["Subgerencia"],
    "gerencia": ["Gerencia"],
    "division": ["División"],
    "planta": ["Nombre Planta"],

    # otros
    "ausentismo": ["Ausentismo", "Tipo Ausentismo", "Fecha Fin Ausentismo"],
    "dias licencia": ["Días Licencia Desde Enero Anterior"],
    "años en puesto": ["Años En Puesto"],
    "tiempo cargo": ["Tiempo Cargo"],
    "correo": ["Correo Electronico"],
    "fono": ["Fonos"],
}

def _pick_existing(cols: List[str], allowed_cols: set) -> List[str]:
    return [c for c in cols if c in allowed_cols]

def infer_requested_columns(question: str, allowed_cols: set) -> List[str]:
    qn = normalize_text(question)
    picks: List[str] = []

    # 1) alias
    for alias, cols in COLUMN_ALIASES.items():
        if normalize_text(alias) in qn:
            picks.extend(_pick_existing(cols, allowed_cols))

    # 2) mención directa (si el usuario pegó nombre real)
    allowed_norm = {normalize_text(c): c for c in allowed_cols}
    for norm_name, real_name in allowed_norm.items():
        if len(norm_name) >= 6 and norm_name in qn:
            picks.append(real_name)

    # dedup
    seen = set()
    out = []
    for c in picks:
        if c not in seen:
            out.append(c)
            seen.add(c)
    return out

# ============================================================
# ✅ FIX 3: parse_user_requested_select faltaba
# ============================================================
def parse_user_requested_select(question: str, allowed_cols: set) -> List[str]:
    """
    Detecta si el usuario pidió columnas/campos específicos.
    Si no hay señales, devuelve [].
    """
    if not question:
        return []

    qn = normalize_text(question)

    # señales típicas de petición explícita de campos
    wants = bool(re.search(r"\b(solo|únicamente|unicamente|campos|columnas|dato|datos|info de|informacion de|información de|muestra|muéstrame|dame|trae)\b", qn, re.I))
    if not wants:
        return []

    # reusa tu mapeo inteligente
    return infer_requested_columns(question, allowed_cols)

def direct_rut_lookup(question: str, data: pd.DataFrame, limit: int) -> Optional[pd.DataFrame]:
    rut = extract_rut_from_text(question)
    if not rut:
        return None
    if "Rut" not in data.columns:
        return None

    allowed_cols = set(data.columns)

    rut_norm = normalize_rut(rut)
    series_norm = data["Rut"].map(normalize_rut)
    out = data[series_norm == rut_norm].head(limit)

    if out.empty:
        return out

    requested = infer_requested_columns(question, allowed_cols)

    base = [c for c in ["Rut", "Nombre"] if c in allowed_cols]
    if requested:
        cols = [c for c in requested if c in allowed_cols]
        if cols:
            return out[cols]
        return out[base] if base else out
    else:
        cols = [c for c in ["Rut", "Nombre", "Cargo Actual", "Gerencia", "Años En Puesto", "Título Profesional 1"] if c in allowed_cols]
        return out[cols]

# ---------- Keywords + fallback global (INTELIGENTE) ----------
STOPWORDS_ES = {
    "el","la","los","las","un","una","unos","unas",
    "de","del","al","a","en","y","o","u",
    "con","sin","para","por","sobre","entre",
    "que","quiero","necesito","busco","dame","muestra","trae",
    "trabajador","trabajadores","persona","personas","perfil","perfiles",
    "info","información","datos","dato","me","puedes","podrias","podrías",
    "buscar","busca","encuentra","mostrar","lista","listame",
}

def singularize_es(word: str) -> str:
    w = word.strip()
    if len(w) > 4 and w.endswith("es"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s"):
        return w[:-1]
    return w

def extract_keywords(text: str) -> List[str]:
    if not text:
        return []
    t = normalize_text(text)
    raw = [w.strip() for w in t.split() if w.strip()]

    kws: List[str] = []
    for w in raw:
        if len(w) < 3:
            continue
        if w in STOPWORDS_ES:
            continue
        w = singularize_es(w)
        if len(w) < 3:
            continue
        kws.append(w)

    seen = set()
    out: List[str] = []
    for w in kws:
        if w not in seen:
            out.append(w)
            seen.add(w)

    return out[:10]

def fallback_keyword_search(data: pd.DataFrame, question: str, limit: int) -> pd.DataFrame:
    kws = extract_keywords(question)
    if not kws:
        return data.head(0)

    cols = list(data.columns)

    score = pd.Series(0, index=data.index, dtype="int64")
    matched_kws = pd.Series(0, index=data.index, dtype="int64")

    norm_cols_cache: Dict[str, pd.Series] = {}
    for c in cols:
        norm_cols_cache[c] = data[c].astype(str).map(normalize_text)

    for kw in kws:
        kw_hit_any = pd.Series(False, index=data.index)
        for c in cols:
            hit = norm_cols_cache[c].str.contains(kw, na=False)
            kw_hit_any = kw_hit_any | hit
            score = score + hit.astype("int64")
        matched_kws = matched_kws + kw_hit_any.astype("int64")

    min_hits = max(1, min(3, math.ceil(len(kws) * 0.4)))

    out = data[matched_kws >= min_hits].copy()
    if len(out) == 0:
        return out

    out["_score"] = score[matched_kws >= min_hits]
    out = out.sort_values(by="_score", ascending=False).drop(columns=["_score"])
    return out.head(limit)

# ---------- Semantic search ponderado (2 pasadas) ----------
def semantic_search_weighted(
    data: pd.DataFrame,
    semantic_query: Dict[str, Any],
    limit: int,
    allowed_cols: set,
) -> pd.DataFrame:
    query = (semantic_query.get("query") or "").strip()
    must_have = semantic_query.get("must_have") or []
    should_have = semantic_query.get("should_have") or []
    synonyms = semantic_query.get("synonyms") or []
    avoid = semantic_query.get("avoid") or []

    cols_pass1 = [c for c in WEIGHTS.keys() if c in allowed_cols]
    if not cols_pass1:
        return data.head(0)

    def _run(cols: List[str], base_weight_for_others: float = 0.8) -> pd.DataFrame:
        norm_cols = {c: data[c].astype(str).map(normalize_text) for c in cols}
        score = pd.Series(0.0, index=data.index)

        def add_term(term: str, base_points: float):
            t = normalize_text(term)
            if not t:
                return
            for c in cols:
                w = WEIGHTS.get(c, base_weight_for_others)
                hit = norm_cols[c].str.contains(t, na=False)
                score.loc[hit] += base_points * w

        if must_have:
            mask = pd.Series(True, index=data.index)
            for t in must_have:
                tt = normalize_text(t)
                if not tt:
                    continue
                any_hit = pd.Series(False, index=data.index)
                for c in cols:
                    any_hit = any_hit | norm_cols[c].str.contains(tt, na=False)
                mask = mask & any_hit

            subset = data[mask].copy()
            if subset.empty:
                return subset

            idx = subset.index
            norm_cols2 = {c: norm_cols[c].loc[idx] for c in cols}
            score2 = pd.Series(0.0, index=idx)

            def add_term_subset(term: str, base_points: float):
                t = normalize_text(term)
                if not t:
                    return
                for c in cols:
                    w = WEIGHTS.get(c, base_weight_for_others)
                    hit = norm_cols2[c].str.contains(t, na=False)
                    score2.loc[hit] += base_points * w

            for t in should_have:
                add_term_subset(t, 1.0)
            for t in synonyms:
                add_term_subset(t, 1.5)
            if query:
                add_term_subset(query, 0.8)

            if avoid:
                avoid_mask = pd.Series(False, index=idx)
                for t in avoid:
                    tt = normalize_text(t)
                    if not tt:
                        continue
                    any_hit = pd.Series(False, index=idx)
                    for c in cols:
                        any_hit = any_hit | norm_cols2[c].str.contains(tt, na=False)
                    avoid_mask = avoid_mask | any_hit
                score2.loc[avoid_mask] -= 10.0

            out = subset.copy()
            out["_score"] = score2
            out = out.sort_values("_score", ascending=False).drop(columns=["_score"])
            return out.head(limit)

        for t in should_have:
            add_term(t, 1.0)
        for t in synonyms:
            add_term(t, 1.5)
        if query:
            add_term(query, 0.8)

        if avoid:
            avoid_mask = pd.Series(False, index=data.index)
            for t in avoid:
                tt = normalize_text(t)
                if not tt:
                    continue
                any_hit = pd.Series(False, index=data.index)
                for c in cols:
                    any_hit = any_hit | norm_cols[c].str.contains(tt, na=False)
                avoid_mask = avoid_mask | any_hit
            score.loc[avoid_mask] -= 10.0

        out = data.copy()
        out["_score"] = score
        out = out.sort_values("_score", ascending=False).drop(columns=["_score"])
        return out.head(limit)

    out1 = _run(cols_pass1)
    if not out1.empty:
        return out1

    cols_pass2 = [c for c in allowed_cols if c != "Rut"]
    return _run(cols_pass2, base_weight_for_others=0.6)

# ---------- Limpieza spec ----------
def clean_spec(spec: Dict[str, Any], allowed_cols: set) -> Dict[str, Any]:
    def clean_item(item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None

        if "items" in item:
            logic = str(item.get("logic", "AND")).upper()
            items = item.get("items", [])
            if not isinstance(items, list):
                return None

            cleaned = []
            for it in items:
                c = clean_item(it)
                if c:
                    cleaned.append(c)

            if not cleaned:
                return None

            return {"logic": logic if logic in {"AND", "OR"} else "AND", "items": cleaned}

        col = item.get("column", None)
        op = item.get("op", None)

        if col is None:
            return None
        col = str(col).strip()
        if not col or col not in allowed_cols:
            return None

        if op is None:
            return None
        op = str(op).strip()
        if op not in ALLOWED_OPS:
            return None

        out = {"column": col, "op": op}
        if "value" in item:
            out["value"] = item["value"]
        return out

    filters_raw = spec.get("filters")
    if filters_raw is None or not isinstance(filters_raw, list):
        spec["filters"] = []
    else:
        cleaned_filters = []
        for f in filters_raw:
            cleaned = clean_item(f)
            if cleaned:
                cleaned_filters.append(cleaned)
        spec["filters"] = cleaned_filters

    spec.setdefault("mode", "new")
    spec.setdefault("strategy", "structured")
    spec.setdefault("select", ["Rut", "Nombre", "Cargo Actual", "Unidad Organizativa", "Gerencia", "Años En Puesto", "Título Profesional 1"])
    spec.setdefault("order_by", [])
    spec.setdefault("limit", 10)

    spec.setdefault("semantic_query", {})
    if not isinstance(spec["semantic_query"], dict):
        spec["semantic_query"] = {}
    spec["semantic_query"].setdefault("query", "")
    spec["semantic_query"].setdefault("must_have", [])
    spec["semantic_query"].setdefault("should_have", [])
    spec["semantic_query"].setdefault("synonyms", [])
    spec["semantic_query"].setdefault("avoid", [])
    spec["semantic_query"].setdefault("notes", "")

    sel = spec.get("select") or []
    if isinstance(sel, list):
        spec["select"] = [c for c in sel if isinstance(c, str) and c in allowed_cols]
    else:
        spec["select"] = []

    try:
        spec["limit"] = int(spec.get("limit", 10))
        if spec["limit"] < 1:
            spec["limit"] = 10
    except Exception:
        spec["limit"] = 10

    return spec

def enforce_default_select(spec: Dict[str, Any], question: str, allowed_cols: set) -> Dict[str, Any]:
    qn = normalize_text(question)
    wants_full = bool(re.search(r"\b(detalle|completo|completa|todo|toda|toda la informacion|toda la información|ficha completa|full)\b", qn, re.I))
    if wants_full:
        return spec

    explicit = bool(parse_user_requested_select(question, allowed_cols))
    if explicit:
        return spec

    compact = [
        "Rut", "Nombre", "Cargo Actual", "Años En Puesto",
        "Unidad Organizativa", "Subgerencia", "Gerencia", "División",
        "Título Profesional 1"
    ]
    compact = [c for c in compact if c in allowed_cols]
    if compact:
        spec["select"] = compact
    return spec

def eval_filter(data: pd.DataFrame, f: Dict[str, Any], allowed_cols: set) -> pd.DataFrame:
    col = f.get("column")
    op = f.get("op")
    val = f.get("value", None)

    if col not in allowed_cols:
        raise ValueError(f"Columna no permitida o inexistente: {col}")
    if op not in ALLOWED_OPS:
        raise ValueError(f"Operador no permitido: {op}")

    series = data[col]

    if op == "equals":
        return data[series.astype(str).str.strip().str.lower() == str(val).strip().lower()]

    if op == "contains":
        return data[series.astype(str).str.contains(str(val), case=False, na=False)]

    if op == "between":
        lo, hi = val
        nums = pd.to_numeric(series, errors="coerce")
        return data[nums.between(float(lo), float(hi))]

    if op == "in":
        allowed_vals = [str(x).strip().lower() for x in val]
        return data[series.astype(str).str.strip().str.lower().isin(allowed_vals)]

    if op == "is_true":
        b = series.map(_to_bool)
        return data[b == True]  # noqa: E712

    if op == "is_false":
        b = series.map(_to_bool)
        return data[b == False]  # noqa: E712

    if op == "is_null":
        return data[series.isna() | (series.astype(str).str.strip() == "")]

    if op == "not_null":
        return data[~(series.isna() | (series.astype(str).str.strip() == ""))]

    if op in {"greater_than", "less_than", "greater_or_equal", "less_or_equal"}:
        nums = pd.to_numeric(series, errors="coerce")
        try:
            num_val = float(val)
        except Exception:
            raise ValueError(f"El valor para {op} debe ser numérico. Recibido: {val}")

        if op == "greater_than":
            return data[nums > num_val]
        if op == "less_than":
            return data[nums < num_val]
        if op == "greater_or_equal":
            return data[nums >= num_val]
        if op == "less_or_equal":
            return data[nums <= num_val]

    raise ValueError(f"Operador no implementado: {op}")

def eval_group(data: pd.DataFrame, group: Dict[str, Any], allowed_cols: set) -> pd.DataFrame:
    logic = str(group.get("logic", "AND")).upper()
    items = group.get("items", [])

    if logic not in {"AND", "OR"}:
        logic = "AND"
    if not items:
        return data

    if logic == "AND":
        out = data
        for it in items:
            out = eval_group(out, it, allowed_cols) if ("items" in it) else eval_filter(out, it, allowed_cols)
        return out

    frames = []
    for it in items:
        frames.append(eval_group(data, it, allowed_cols) if ("items" in it) else eval_filter(data, it, allowed_cols))
    return pd.concat(frames, ignore_index=True).drop_duplicates()

def apply_query_spec(data: pd.DataFrame, spec: Dict[str, Any], allowed_cols: set) -> pd.DataFrame:
    out = data.copy()

    for item in spec.get("filters", []):
        out = eval_group(out, item, allowed_cols) if ("items" in item) else eval_filter(out, item, allowed_cols)

    for ob in spec.get("order_by", []):
        col = ob.get("column")
        direction = str(ob.get("direction", "asc")).lower()
        if col in allowed_cols:
            out = out.sort_values(by=col, ascending=(direction != "desc"), na_position="last")

    select_cols = spec.get("select") or ["Rut", "Nombre", "Cargo Actual", "Unidad Organizativa", "Gerencia", "Años En Puesto", "Título Profesional 1"]
    select_cols = [c for c in select_cols if c in allowed_cols]
    if select_cols:
        out = out[select_cols]

    limit = int(spec.get("limit", 10))
    return out.head(limit)

# ---------------------------
# Prompt / LLM
# ---------------------------
def build_system_prompt(columns: List[str]) -> str:
    cols = ", ".join(columns)

    role = load_prompt("system_role.txt")
    tone = load_prompt("tone.txt")
    rules = load_prompt("rules.txt")
    query_builder = load_prompt("query_builder.txt")
    clarify = load_prompt("clarify.txt")

    extra = """
DATOS IMPORTANTES DEL DOMINIO:
- "Sed A-2024" y "Sed S-2024" (y otras SED) son numéricas entre 0 y 5 (pueden tener decimales).
- Rankings suelen ser numéricos 1..5, pero pueden contener el texto "Exc. x plazo fijo".
  Si el usuario pide comparadores numéricos (>=, <=), ignora textos no numéricos.
  Si el usuario pide "Exc. x plazo fijo", usa contains/equals con ese texto.
- Para búsquedas de perfiles por títulos/cargos, usa strategy="semantic" y semantic_query.
""".strip()

    return f"""
{role}

{tone}

{rules}

{query_builder}

{clarify}

{extra}

Columnas disponibles (exactas):
{cols}

REGLAS IMPORTANTES:
- Nunca inventes columnas.
- Si no estás seguro de qué columna representa lo pedido, usa mode="clarify" o strategy="semantic".
""".strip()

def llm_to_query_spec(user_question: str, columns: List[str], last_spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if client is None:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY no configurada (.env)")

    system = build_system_prompt(columns)
    context = {
        "role": "system",
        "content": f"CONTEXTO_ANTERIOR (puede ser null): {json.dumps(last_spec, ensure_ascii=False)}"
    }

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            context,
            {"role": "user", "content": user_question},
        ],
    )

    return json.loads(resp.choices[0].message.content)

# ---------------------------
# Conversación (texto)
# ---------------------------
def fmt(v: Any) -> str:
    v = clean_value(v)
    return "N/D" if v is None else str(v)

def rows_to_text(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No encontré resultados con esos criterios. ¿Quieres ajustar (ej: título, cargo, unidad/gerencia, SED, ranking, sanción, años)?"

    msg = [f"Encontré {len(rows)} resultado(s):\n"]
    for i, r in enumerate(rows, start=1):
        parts = []
        for k, v in r.items():
            v = clean_value(v)
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            parts.append(f"{k}: {fmt(v)}")
        msg.append(f"{i}) " + " | ".join(parts))

    msg.append("\n¿Quieres refinar por otra condición (SED, ranking, sanción, años, gerencia/unidad, etc.)?")
    return "\n".join(msg)

def build_clarify_answer(spec: Dict[str, Any]) -> str:
    q = (spec.get("clarifying_question") or "").strip()
    opts = spec.get("suggested_options") or []
    if opts:
        return q + "\n\nOpciones sugeridas:\n- " + "\n- ".join([str(o) for o in opts])
    return q or "¿Me puedes dar un poco más de detalle para poder buscar?"

def validate_result_matches_keywords(question: str, rows: List[Dict[str, Any]]) -> bool:
    kws = extract_keywords(question)
    if not kws:
        return True
    joined = normalize_text(" ".join([str(v) for r in rows for v in r.values() if v is not None]))
    return any(kw in joined for kw in kws)

# ============================================================
# SQLite (para consultas SQL rápidas desde el frontend)
# Endpoints:
#   - GET  /sql/schema  -> columnas y conteo
#   - POST /sql/reload  -> reconstruye la DB desde el Excel (df)
#   - POST /sql         -> ejecuta SELECT (solo lectura) con params
# ============================================================
SQLITE_PATH = os.getenv("SQLITE_PATH", "storage/trabajadores.db").strip()
SQLITE_TABLE = os.getenv("SQLITE_TABLE", "trabajadores").strip()
_db_lock = threading.Lock()

def _get_conn():
    _ensure_parent_dir(SQLITE_PATH)
    # check_same_thread=False: FastAPI puede atender en hilos
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)

def rebuild_sqlite_from_df() -> Dict[str, Any]:
    _ensure_loaded()
    with _db_lock:
        conn = _get_conn()
        try:
            # Reemplaza tabla
            df.to_sql(SQLITE_TABLE, conn, if_exists="replace", index=False)
            cur = conn.cursor()
            cur.execute(f'SELECT COUNT(*) FROM "{SQLITE_TABLE}"')
            n = int(cur.fetchone()[0])
            return {"ok": True, "sqlite_path": SQLITE_PATH, "table": SQLITE_TABLE, "rows": n}
        finally:
            conn.close()

def _sqlite_table_exists(conn) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (SQLITE_TABLE,))
    return cur.fetchone() is not None

def _safe_select_only(q: str) -> None:
    q2 = (q or "").strip()
    if not q2:
        raise HTTPException(status_code=400, detail="query vacío")
    # prohibimos múltiples statements
    if ";" in q2:
        raise HTTPException(status_code=400, detail="Solo se permite un statement (sin ';').")
    head = re.sub(r"^\s+", "", q2, flags=re.M).lower()
    if not (head.startswith("select") or head.startswith("with")):
        raise HTTPException(status_code=400, detail="Solo se permite SQL de lectura (SELECT/WITH).")
    # bloqueos simples
    forbidden = ["insert", "update", "delete", "drop", "alter", "create", "pragma", "attach", "detach"]
    for kw in forbidden:
        if re.search(rf"\b{kw}\b", head):
            raise HTTPException(status_code=400, detail=f"Keyword no permitido: {kw}")

class SQLRequest(BaseModel):
    query: str
    params: Dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=200, ge=1, le=2000)

@app.on_event("startup")
def _startup_sqlite():
    # No hacemos crash si falla; pero si no existe DB, intentamos crearla
    try:
        if df is None:
            return
        with _db_lock:
            conn = _get_conn()
            try:
                if not _sqlite_table_exists(conn):
                    df.to_sql(SQLITE_TABLE, conn, if_exists="replace", index=False)
            finally:
                conn.close()
    except Exception as e:
        print(f"WARNING: SQLite init falló: {e}")

@app.get("/sql/schema")
def sql_schema():
    _ensure_loaded()
    with _db_lock:
        conn = _get_conn()
        try:
            if not _sqlite_table_exists(conn):
                df.to_sql(SQLITE_TABLE, conn, if_exists="replace", index=False)
            cur = conn.cursor()
            cur.execute(f'PRAGMA table_info("{SQLITE_TABLE}")')
            cols = [{"cid": r[0], "name": r[1], "type": r[2]} for r in cur.fetchall()]
            cur.execute(f'SELECT COUNT(*) FROM "{SQLITE_TABLE}"')
            n = int(cur.fetchone()[0])
            return {"sqlite_path": SQLITE_PATH, "table": SQLITE_TABLE, "rows": n, "columns": cols}
        finally:
            conn.close()

@app.post("/sql/reload")
def sql_reload():
    return rebuild_sqlite_from_df()

@app.post("/sql")
def sql_query(req: SQLRequest):
    _ensure_loaded()
    _safe_select_only(req.query)

    q = req.query.strip()
    params = dict(req.params or {})
    if re.search(r"\blimit\b", q, flags=re.I) is None:
        q = f"SELECT * FROM ({q}) AS subq LIMIT :_limit"
        params["_limit"] = int(req.limit)

    with _db_lock:
        conn = _get_conn()
        conn.row_factory = sqlite3.Row
        try:
            if not _sqlite_table_exists(conn):
                df.to_sql(SQLITE_TABLE, conn, if_exists="replace", index=False)
            cur = conn.cursor()
            cur.execute(q, params)
            rows = [dict(r) for r in cur.fetchall()]
            return {"rows": clean_rows(rows), "count": len(rows)}
        except sqlite3.Error as e:
            raise HTTPException(status_code=400, detail=f"SQL error: {e}")
        finally:
            conn.close()

# ---------------------------
# Endpoints
# ---------------------------
@app.get("/health")
def health():
    return {
        "status": "ok" if df is not None else "error",
        "excel_path": EXCEL_PATH,
        "rows_loaded": int(len(df)) if df is not None else 0,
        "excel_error": EXCEL_LOAD_ERROR,
        "openai_key_configured": bool(OPENAI_API_KEY),
        "version": "3.5.0",
    }

@app.get("/schema")
def schema():
    _ensure_loaded()
    return {"rows_loaded": int(len(df)), "columns": list(df.columns)}

@app.post("/chat")
def chat(req: ChatRequest):
    _ensure_loaded()
    session_id, sess = get_session(req.session_id)

    greeting = load_prompt("greeting.txt") or DEFAULT_GREETING
    first_turn = not sess.get("greeted", False)
    if first_turn:
        sess["greeted"] = True

    pending = sess.get("pending_clarify")
    if pending:
        question = f"Pregunta original: {pending['question']}\nRespuesta del usuario: {req.question}"
        sess["pending_clarify"] = None
    else:
        question = (req.question or "").strip()

    if not question:
        if first_turn:
            payload: Dict[str, Any] = {
                "session_id": session_id,
                "answer": greeting,
                "count_returned": 0,
                "query_spec": {"mode": "greeting"},
            }
            if req.include_rows:
                payload["rows"] = []
            return ORJSONResponse(content=payload)
        raise HTTPException(status_code=400, detail="question vacío")

    limit = int(req.limit) if req.limit is not None else infer_user_limit(question, default_limit=3, max_limit=100)

    hit = direct_rut_lookup(question, df, limit)
    if hit is not None:
        rows = clean_rows(hit.to_dict(orient="records"))
        answer = rows_to_text(rows) if rows else "No encontré ese RUT en la base."
        if first_turn:
            answer = greeting + "\n\n" + answer
        spec = {"mode": "rut_lookup", "rut_detected": extract_rut_from_text(question), "select": list(hit.columns) if not hit.empty else [], "limit": limit}

        sess["last_spec"] = spec
        sess["messages"].append({"role": "user", "content": req.question})
        sess["messages"].append({"role": "assistant", "content": answer})

        payload: Dict[str, Any] = {
            "session_id": session_id,
            "answer": answer,
            "count_returned": int(len(rows)),
            "query_spec": spec,
        }
        if req.include_rows:
            payload["rows"] = rows
        return ORJSONResponse(content=payload)

    allowed_cols = set(df.columns)
    last_spec = sess.get("last_spec")

    try:
        spec = llm_to_query_spec(question, sorted(list(allowed_cols)), last_spec)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error LLM: {e}")

    spec["limit"] = limit
    spec = clean_spec(spec, allowed_cols)
    spec = enforce_default_select(spec, question, allowed_cols)

    if spec.get("mode") == "clarify":
        answer = build_clarify_answer(spec)
        if first_turn:
            answer = greeting + "\n\n" + answer
        sess["pending_clarify"] = {"question": req.question, "spec": spec}
        sess["messages"].append({"role": "user", "content": req.question})
        sess["messages"].append({"role": "assistant", "content": answer})
        payload = {
            "session_id": session_id,
            "answer": answer,
            "needs_clarification": True,
            "count_returned": 0,
            "query_spec": spec,
        }
        if req.include_rows:
            payload["rows"] = []
        return ORJSONResponse(content=payload)

    if spec.get("mode") == "refine" and isinstance(last_spec, dict):
        merged = dict(last_spec)
        merged["filters"] = list(last_spec.get("filters", [])) + list(spec.get("filters", []))
        merged["select"] = spec.get("select") or merged.get("select")
        merged["order_by"] = spec.get("order_by") or merged.get("order_by")
        merged["limit"] = limit
        merged["strategy"] = spec.get("strategy", merged.get("strategy", "structured"))
        merged["semantic_query"] = spec.get("semantic_query", merged.get("semantic_query", {}))
        spec = clean_spec(merged, allowed_cols)

    if spec.get("strategy") == "semantic":
        sem = semantic_search_weighted(df, spec.get("semantic_query", {}), limit, allowed_cols)

        select_cols = spec.get("select") or ["Rut","Nombre","Cargo Actual","Unidad Organizativa","Gerencia","Años En Puesto","Título Profesional 1"]
        select_cols = [c for c in select_cols if c in allowed_cols]
        if select_cols and not sem.empty:
            sem = sem[select_cols]

        rows = clean_rows(sem.to_dict(orient="records"))
        answer = rows_to_text(rows) if rows else "No encontré coincidencias claras. ¿Qué título/cargo exacto buscas o en qué gerencia/unidad?"
        if first_turn:
            answer = greeting + "\n\n" + answer

        payload: Dict[str, Any] = {
            "session_id": session_id,
            "answer": answer,
            "count_returned": int(len(rows)),
            "query_spec": {"mode": spec.get("mode", "new"), "strategy": "semantic", "semantic_query": spec.get("semantic_query", {}), "select": select_cols, "limit": limit},
        }
        if req.include_rows:
            payload["rows"] = rows

        sess["last_spec"] = payload["query_spec"]
        sess["messages"].append({"role": "user", "content": req.question})
        sess["messages"].append({"role": "assistant", "content": answer})
        return ORJSONResponse(content=payload)

    if not spec.get("filters"):
        fb = fallback_keyword_search(df, question, limit)
        rows = clean_rows(fb.to_dict(orient="records"))
        answer = rows_to_text(rows) if rows else "No encontré coincidencias en la base. Prueba con más detalle (ej: título, cargo, gerencia, SED, ranking)."
        if first_turn:
            answer = greeting + "\n\n" + answer

        fbspec = {"mode": spec.get("mode", "new"), "strategy": "fallback_keyword_search", "keywords": extract_keywords(question), "limit": limit}

        payload: Dict[str, Any] = {
            "session_id": session_id,
            "answer": answer,
            "count_returned": int(len(rows)),
            "query_spec": fbspec,
        }
        if req.include_rows:
            payload["rows"] = rows

        sess["last_spec"] = fbspec
        sess["messages"].append({"role": "user", "content": req.question})
        sess["messages"].append({"role": "assistant", "content": answer})
        return ORJSONResponse(content=payload)

    try:
        result = apply_query_spec(df, spec, allowed_cols)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error aplicando filtros: {e}")

    rows = clean_rows(result.to_dict(orient="records"))

    if rows and not validate_result_matches_keywords(question, rows):
        fb = fallback_keyword_search(df, question, limit)
        rows = clean_rows(fb.to_dict(orient="records"))
        spec = {"mode": spec.get("mode", "new"), "strategy": "fallback_keyword_search_after_validation", "keywords": extract_keywords(question), "limit": limit}

    answer = rows_to_text(rows)
    if first_turn:
        answer = greeting + "\n\n" + answer

    sess["last_spec"] = spec
    sess["messages"].append({"role": "user", "content": req.question})
    sess["messages"].append({"role": "assistant", "content": answer})

    payload: Dict[str, Any] = {
        "session_id": session_id,
        "answer": answer,
        "count_returned": int(len(rows)),
        "query_spec": spec,
    }
    if req.include_rows:
        payload["rows"] = rows

    return ORJSONResponse(content=payload)

@app.post("/chat_text", response_class=PlainTextResponse)
def chat_text(req: ChatRequest):
    _ensure_loaded()
    session_id, sess = get_session(req.session_id)

    greeting = load_prompt("greeting.txt") or DEFAULT_GREETING
    first_turn = not sess.get("greeted", False)
    if first_turn:
        sess["greeted"] = True

    pending = sess.get("pending_clarify")
    if pending:
        question = f"Pregunta original: {pending['question']}\nRespuesta del usuario: {req.question}"
        sess["pending_clarify"] = None
    else:
        question = (req.question or "").strip()

    if not question:
        if first_turn:
            return PlainTextResponse(greeting)
        raise HTTPException(status_code=400, detail="question vacío")

    limit = int(req.limit) if req.limit is not None else infer_user_limit(question, default_limit=3, max_limit=100)

    hit = direct_rut_lookup(question, df, limit)
    if hit is not None:
        rows = clean_rows(hit.to_dict(orient="records"))
        answer = rows_to_text(rows) if rows else "No encontré ese RUT en la base."
        if first_turn:
            answer = greeting + "\n\n" + answer
        sess["last_spec"] = {"mode": "rut_lookup", "strategy": "structured", "rut_detected": extract_rut_from_text(question), "select": list(hit.columns) if not hit.empty else [], "limit": limit}
        sess["messages"].append({"role": "user", "content": req.question})
        sess["messages"].append({"role": "assistant", "content": answer})
        return PlainTextResponse(answer)

    allowed_cols = set(df.columns)
    last_spec = sess.get("last_spec")

    try:
        spec = llm_to_query_spec(question, sorted(list(allowed_cols)), last_spec)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error LLM: {e}")

    spec["limit"] = limit
    spec = clean_spec(spec, allowed_cols)
    spec = enforce_default_select(spec, question, allowed_cols)

    if spec.get("mode") == "clarify":
        answer = build_clarify_answer(spec)
        if first_turn:
            answer = greeting + "\n\n" + answer
        sess["pending_clarify"] = {"question": req.question, "spec": spec}
        sess["messages"].append({"role": "user", "content": req.question})
        sess["messages"].append({"role": "assistant", "content": answer})
        return PlainTextResponse(answer)

    if spec.get("mode") == "refine" and isinstance(last_spec, dict):
        merged = dict(last_spec)
        merged["filters"] = list(last_spec.get("filters", [])) + list(spec.get("filters", []))
        merged["select"] = spec.get("select") or merged.get("select")
        merged["order_by"] = spec.get("order_by") or merged.get("order_by")
        merged["limit"] = limit
        merged["strategy"] = spec.get("strategy", merged.get("strategy", "structured"))
        merged["semantic_query"] = spec.get("semantic_query", merged.get("semantic_query", {}))
        spec = clean_spec(merged, allowed_cols)

    if spec.get("strategy") == "semantic":
        sem = semantic_search_weighted(df, spec.get("semantic_query", {}), limit, allowed_cols)

        select_cols = spec.get("select") or ["Rut","Nombre","Cargo Actual","Unidad Organizativa","Gerencia","Años En Puesto","Título Profesional 1"]
        select_cols = [c for c in select_cols if c in allowed_cols]
        if select_cols and not sem.empty:
            sem = sem[select_cols]

        rows = clean_rows(sem.to_dict(orient="records"))
        answer = rows_to_text(rows) if rows else "No encontré coincidencias claras. ¿Qué título/cargo exacto buscas o en qué gerencia/unidad?"
        if first_turn:
            answer = greeting + "\n\n" + answer

        sess["last_spec"] = {"mode": spec.get("mode", "new"), "strategy": "semantic", "semantic_query": spec.get("semantic_query", {}), "select": select_cols, "limit": limit}
        sess["messages"].append({"role": "user", "content": req.question})
        sess["messages"].append({"role": "assistant", "content": answer})
        return PlainTextResponse(answer)

    if not spec.get("filters"):
        fb = fallback_keyword_search(df, question, limit)
        rows = clean_rows(fb.to_dict(orient="records"))
        if not rows:
            answer = "No encontré coincidencias en la base. Prueba con más detalle (ej: título, cargo, gerencia, SED, ranking)."
            if first_turn:
                answer = greeting + "\n\n" + answer
            return PlainTextResponse(answer)
        answer = rows_to_text(rows)
        if first_turn:
            answer = greeting + "\n\n" + answer
        return PlainTextResponse(answer)

    try:
        result = apply_query_spec(df, spec, allowed_cols)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error aplicando filtros: {e}")

    rows = clean_rows(result.to_dict(orient="records"))

    if rows and not validate_result_matches_keywords(question, rows):
        fb = fallback_keyword_search(df, question, limit)
        rows = clean_rows(fb.to_dict(orient="records"))

    answer = rows_to_text(rows)
    if first_turn:
        answer = greeting + "\n\n" + answer

    sess["last_spec"] = spec
    sess["messages"].append({"role": "user", "content": req.question})
    sess["messages"].append({"role": "assistant", "content": answer})

    return PlainTextResponse(answer)

# Ejecutar:
# uvicorn main:app --reload
