# 🤖 Agente Reclutador Conversacional

Agente conversacional desarrollado en **FastAPI** que permite consultar una base de trabajadores
mediante **lenguaje natural**, combinando reglas estructuradas, búsqueda semántica y modelos de
lenguaje (LLM) usando **OpenAI API**.

El proyecto demuestra el uso práctico de **IA Generativa aplicada**, integrando prompts
reutilizables, embeddings y persistencia en SQLite.

---

## 🎯 Objetivo del proyecto

Permitir consultas en lenguaje natural sobre una base de trabajadores, por ejemplo:

- “Dame 3 analistas de sistemas”
- “Personas con más de 5 años en el cargo”
- “Trabajadores relacionados con auditoría”
- “Consulta directa vía SQL a la base local”

---

## 🚀 Tecnologías utilizadas

- Python 3.13  
- FastAPI  
- Uvicorn  
- Pandas  
- SQLite  
- OpenAI API  

---

## 📂 Estructura del repositorio

```text
Agente/
│
├── app/
│   ├── main.py                # API principal FastAPI
│   ├── prompts/               # Prompts reutilizables
│   │   ├── system_role.txt
│   │   ├── rules.txt
│   │   ├── tone.txt
│   │   ├── query_builder.txt
│   │   ├── clarify.txt
│   │   └── greeting.txt
│   └── storage/               # Base de datos SQLite
│
├── kb/                         # Base de conocimiento (textos para embeddings)
├── ui/                         # Interfaz HTML simple (opcional)
├── index_files/                # Archivos estáticos
│
├── trabajadores.xlsx           # Fuente de datos base
├── .env.example                # Variables de entorno de ejemplo
├── .gitignore
└── README.md
```

---

## ⚙️ Configuración inicial

### 1️⃣ Crear entorno virtual (opcional pero recomendado)

```bash
python -m venv venv
```

Activar entorno virtual:

```bash
# Linux / Mac
source venv/bin/activate
```

```bash
# Windows
venv\Scripts\activate
```

---

### 2️⃣ Instalar dependencias

```bash
pip install -r requirements.txt
```

Si no existe `requirements.txt`, instalar al menos:

```bash
pip install fastapi uvicorn pandas openai python-dotenv
```

---

## 🔐 Variables de entorno

Crear un archivo `.env` a partir de `.env.example`:

```env
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
EXCEL_PATH=trabajadores.xlsx
SQLITE_PATH=app/storage/trabajadores.db
SQLITE_TABLE=trabajadores
```

---

## ▶️ Ejecutar el proyecto

Desde la raíz del repositorio:

```bash
uvicorn app.main:app --reload
```

La API quedará disponible en:

- API: http://127.0.0.1:8000  
- Swagger UI: http://127.0.0.1:8000/docs  

---

## 🧠 Endpoints principales

### 🔹 Healthcheck

```http
GET /health
```

---

### 🔹 Chat conversacional (JSON)

```http
POST /chat
```

Ejemplo de body:

```json
{
  "question": "Dame 3 personas con más de 5 años en el cargo",
  "include_rows": true
}
```

---

### 🔹 Chat en texto plano

```http
POST /chat_text
```

---

## 📚 Búsqueda semántica (Embeddings)

El sistema utiliza una **base de conocimiento (`kb/`)** que se vectoriza mediante embeddings,
permitiendo responder preguntas conceptuales además de consultas estructuradas.

---

## 🗄️ Módulo SQL (SQLite)

El proyecto incluye un módulo para ejecutar **consultas SQL de solo lectura** sobre la base local.

### Ver esquema de la base

```http
GET /sql/schema
```

---

### Ejecutar consulta SQL

```http
POST /sql
```

Ejemplo:

```json
{
  "query": "SELECT Nombre, Cargo FROM trabajadores WHERE Gerencia = :g",
  "params": {
    "g": "Gerencia de Finanzas"
  }
}
```

> ⚠️ Por seguridad, solo se permiten consultas `SELECT`.

---

## 🧩 Prompts reutilizables

Los prompts están desacoplados del código y organizados en archivos de texto:

- `system_role.txt` → Rol del asistente  
- `rules.txt` → Reglas de negocio  
- `query_builder.txt` → Construcción de filtros  
- `clarify.txt` → Manejo de ambigüedad  
- `tone.txt` → Tono de respuesta  
- `greeting.txt` → Saludo inicial  

Esto permite modificar el comportamiento del agente sin cambiar el código.

---

## 📌 Estado del proyecto

- FastAPI operativo  
- Integración OpenAI (LLM + Embeddings)  
- Base de conocimiento semántica  
- Prompts reutilizables  
- Módulo SQL con SQLite  
- Repositorio documentado en GitHub  

---

## 👤 Autores

Proyecto desarrollado por **Jonathan Salinas - Sebastián Leiva **  
Curso: *Prompt Engineering / IA Generativa aplicada*

## Punto 2 – Modelo LLM API-Based

El endpoint POST /chat utiliza el modelo gpt-4o-mini vía OpenAI API.

Flujo:
1. Usuario envía pregunta en lenguaje natural.
2. El sistema construye un prompt dinámico.
3. Se llama a OpenAI Chat Completions.
4. El modelo devuelve un JSON estructurado (query_spec).
5. El sistema ejecuta la estrategia correspondiente (structured / semantic / fallback).
6. Se responde al usuario.

La respuesta incluye:
- answer (texto generado)
- query_spec (cómo se resolvió)
- strategy (structured / semantic / fallback)
