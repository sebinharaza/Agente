# 🤖 Agente Reclutador Conversacional

Agente conversacional desarrollado en **FastAPI** que permite consultar una base de trabajadores
utilizando **lenguaje natural**, combinando:

- Reglas estructuradas (filtros, operadores)
- Búsqueda semántica mediante **embeddings**
- Prompts reutilizables
- Persistencia en **SQLite**
- Integración con **OpenAI API**

El proyecto está orientado a demostrar el uso práctico de **IA Generativa aplicada** en un backend real.

---

## 🎯 Objetivo del proyecto

Permitir que un usuario consulte información de trabajadores (cargo, unidad, títulos, años en puesto, etc.)
mediante preguntas en lenguaje natural, por ejemplo:

- “Dame 3 analistas de sistemas”
- “Personas con más de 5 años en el cargo en la Gerencia X”
- “Busca perfiles relacionados con auditoría”
- “Consulta directa vía SQL a la base local”

---

## 🚀 Tecnologías utilizadas

- **Python 3.13**
- **FastAPI**
- **Uvicorn**
- **Pandas**
- **SQLite**
- **OpenAI API (LLM + Embeddings)**

---

## 📂 Estructura del repositorio

Agente/
│
├── app/
│ ├── main.py # API principal FastAPI
│ ├── prompts/ # Prompts reutilizables
│ │ ├── system_role.txt
│ │ ├── rules.txt
│ │ ├── tone.txt
│ │ ├── query_builder.txt
│ │ ├── clarify.txt
│ │ └── greeting.txt
│ └── storage/ # Base SQLite
│
├── kb/ # Base de conocimiento (textos para embeddings)
├── ui/ # Interfaz HTML simple (opcional)
├── index_files/ # Archivos estáticos
│
├── trabajadores.xlsx # Fuente de datos base
├── .env.example # Variables de entorno de ejemplo
├── .gitignore
└── README.md


---

## ⚙️ Configuración inicial

### 1️⃣ Crear entorno virtual (opcional pero recomendado)

```bash
python -m venv venv
source venv/bin/activate       # Linux / Mac
venv\Scripts\activate          # Windows
2️⃣ Instalar dependencias
pip install -r requirements.txt
(Si no tienes requirements.txt, instala al menos: fastapi, uvicorn, pandas, openai, python-dotenv)

🔐 Variables de entorno
Crea un archivo .env a partir de .env.example:

OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
EXCEL_PATH=trabajadores.xlsx
SQLITE_PATH=app/storage/trabajadores.db
SQLITE_TABLE=trabajadores
▶️ Ejecutar el proyecto
Desde la raíz del repositorio:

uvicorn app.main:app --reload
La API quedará disponible en:

📍 API: http://127.0.0.1:8000

📘 Swagger: http://127.0.0.1:8000/docs

🧠 Endpoints principales
🔹 Healthcheck
GET /health
🔹 Chat conversacional (JSON)
POST /chat
Ejemplo de body:

{
  "question": "Dame 3 personas con más de 5 años en el cargo",
  "include_rows": true
}
🔹 Chat en texto plano
POST /chat_text
📚 Búsqueda semántica (Embeddings)
El proyecto incluye una base de conocimiento (kb/) que se utiliza para recuperación semántica.
Los textos se vectorizan mediante embeddings y se inyectan como contexto al LLM.

Esto permite responder preguntas conceptuales o de dominio, no solo estructuradas.

🗄️ Módulo SQL (SQLite)
El sistema incluye un módulo adicional que permite ejecutar consultas SQL de solo lectura.

Ver esquema
GET /sql/schema
Ejecutar consulta
POST /sql
Ejemplo:

{
  "query": "SELECT Nombre, Cargo FROM trabajadores WHERE Gerencia = :g",
  "params": { "g": "Gerencia de Finanzas" }
}
⚠️ Seguridad: solo se permiten consultas SELECT.

🧩 Prompts reutilizables
Los prompts están desacoplados del código y organizados en archivos de texto,
permitiendo fácil mantenimiento y reutilización:

system_role.txt → rol del asistente

rules.txt → reglas de negocio

query_builder.txt → construcción de filtros

clarify.txt → manejo de ambigüedad

tone.txt → tono de respuesta

greeting.txt → saludo inicial