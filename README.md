# Pacer_Time

Registro de participantes, cronometraje y resultados de carreras. **Todo local** (sin internet): Python + PySide6 + SQLite.

Diseñado para no "trabarse" el día de la carrera: la captura de lecturas corre en un **proceso aparte** de la interfaz, las lecturas crudas **nunca se modifican** y los resultados siempre se **recalculan** a partir de ellas.

## Instalación

### Requisitos
- **Python 3.10 o superior** (3.11–3.13 probados). Comprobar con `python --version`.
- **Git** (para descargar el proyecto).
- Windows 10/11, Linux o macOS. No necesita internet para funcionar; solo para instalar.
- *Opcional:* un lector UHF por red compatible con LLRP (p. ej. Impinj) para captura real.

### 1. Descargar el proyecto
```bash
git clone https://github.com/ANT-RX/Pacer_Time.git
cd Pacer_Time
```

### 2. Crear el entorno virtual e instalar

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
```
> Si PowerShell bloquea el script de activación: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, o use `cmd` y `.venv\Scripts\activate.bat`.

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

Extras opcionales (se pueden combinar, p. ej. `pip install -e ".[dev,llrp]"`):

| Extra | Para qué | Comando |
|---|---|---|
| `dev` | Correr las pruebas (`pytest`) | `pip install -e ".[dev]"` |
| `llrp` | Lectores UHF por red (instala `sllurp`) | `pip install -e ".[llrp]"` |

### 3. Verificar la instalación
```bash
pytest                  # solo si instaló el extra "dev"; debe terminar en "passed"
pacer-sim seed --db demo.db --runners 100
pacer-time demo.db      # abre la app con una carrera de demostración
```
Sin datos propios todavía, `pacer-time` a secas muestra una bienvenida con **Nuevo evento / Abrir evento / Probar con datos de demostración**.

### Dónde se guardan los datos
Cada evento es **un solo archivo `.db`** (por defecto en `~/PacerTime/`, o `C:\Users\<usuario>\PacerTime\` en Windows). Los respaldos automáticos van a una carpeta `<evento>.respaldos/` junto al archivo. Para pasar un evento a otra computadora basta copiar ese `.db` (con la app cerrada).

### Linux: librerías de Qt
Si al abrir la app aparece un error como `Could not load the Qt platform plugin "xcb"`, instale las dependencias del sistema:
```bash
sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1
```
Para correr sin pantalla (servidores, pruebas): `QT_QPA_PLATFORM=offscreen pytest`.

### Problemas frecuentes
| Síntoma | Solución |
|---|---|
| `pacer-time` no se reconoce como comando | Active el entorno virtual (`.venv`) o use `python -m pacer_time.ui.app` |
| `No matching distribution found for PySide6` | Python demasiado antiguo o de 32 bits: use Python 3.10+ de 64 bits |
| `ModuleNotFoundError: sllurp` al usar un lector LLRP | `pip install -e ".[llrp]"` |
| La captura "se queda en SIN SEÑAL" | El proceso de captura se cerró; en *Carrera en vivo* pulse **Iniciar captura** de nuevo (las lecturas ya guardadas no se pierden) |

### Actualizar
```bash
git pull
pip install -e .
```

## Uso rápido

```bash
pacer-time                         # abre la app (bienvenida: nuevo evento / abrir / demostración)
pacer-time ~/PacerTime/mi_evento.db

pacer-sim seed --db demo.db --runners 300                    # base de demostración con carrera terminada y anomalías
pacer-sim seed --db demo.db --runners 300 --elapsed-min 12   # carrera EN CURSO
pacer-capture --db demo.db --race 1 --source sim --speed 10  # captura simulada (proceso independiente)
pacer-capture --db evento.db --race 1 --source llrp --host 192.168.1.50 --point-id 3 --antennas 1,2
```

### Flujo en la app
1. **Participantes**: alta manual, edición, **importar CSV** (columnas en español o inglés; ver abajo), estado DNS/DNF/DQ con motivo.
2. **Check-in**: busca por dorsal/documento/chip, escanea el chip, marca el kit entregado.
3. **Carrera en vivo**: *Disparo de salida* → *Iniciar captura* (simulador o lector LLRP) → lecturas en tiempo real, salud de cada lector, paso manual de respaldo.
4. **Resultados**: general, por género y por categoría; pistola (gun) y neto (chip); ritmo; parciales; exporta CSV y HTML.
5. **Calidad y correcciones**: anomalías detectadas (sin meta, sin salida, parcial faltante, orden imposible, chips desconocidos…), lecturas por dorsal, fijar tiempos, anular lecturas, cambiar estado, ajustar el disparo. **Cada corrección exige motivo y operador** y queda en un historial que no se puede editar ni borrar.

### CSV de inscripciones
Delimitador `,` `;` o tabulador. Columnas reconocidas (mayúsculas/acentos indiferentes): `nombre`, `apellido(s)`, `documento`, `fecha_nacimiento` (AAAA-MM-DD o DD/MM/AAAA), `genero/sexo`, `club`, `email`, `telefono`, `contacto_emergencia`, `telefono_emergencia`, `dorsal`, `chip`, `categoria`, `pagado`, `kit`. Cada fila es atómica: una fila con error no afecta a las demás; los duplicados (mismo documento, o mismo nombre y nacimiento) se omiten.

## Cómo funciona

```
UI (PySide6) ── lee ──▶ SQLite (WAL, un archivo por evento) ◀── escribe ── proceso de captura
   │ cálculo en hilo aparte                 ▲                                  (LLRP / simulador)
   └──▶ motor de resultados (función pura) ─┘
```

- **Lecturas crudas inmutables** (`raw_read`, con triggers que impiden UPDATE/DELETE) y **correcciones inmutables** (`correction`, con operador y motivo obligatorios). Resultados = función pura de (lecturas + chips + correcciones): mismas entradas, mismo resultado, sin importar el orden de llegada.
- **Duplicados**: las lecturas del mismo chip en el mismo punto separadas por ≤ 5 s (configurable) son un solo paso; se usa la primera. Pasos posteriores se reportan como informativos.
- **Tiempos**: pistola = meta − disparo. Neto = meta − max(paso por salida, disparo). Sin lectura de salida: neto = pistola (con aviso).
- **Premiación** configurable por carrera: todo por pistola, todo por neto, o híbrido (general por pistola, categorías por neto).
- **Durabilidad**: `journal_mode=WAL`, `synchronous=FULL`, lotes de escritura cada ~200 ms con reintentos. Probado con `kill -9` al proceso de captura: la base queda íntegra y lo confirmado no se pierde.
- **Aislamiento UI/captura**: la UI solo lee la BD y pide detener marcando `stop_requested`; si la UI se cierra o se cuelga, la captura sigue. El proceso publica un latido; sin latido > 5 s la UI avisa "SIN SEÑAL".
- **Respaldo**: copia en caliente (API de backup de SQLite) cada 5 min y al cerrar, en `<evento>.respaldos/` (últimas 30).
- **Migraciones**: esquema versionado (`PRAGMA user_version`); una BD de una versión más nueva se rechaza en lugar de dañarse.

## Estructura

```
src/pacer_time/
  schema.py db.py repo.py       esquema, conexión/migraciones/respaldo, reglas de negocio
  engine.py                     motor de cálculo puro (resultados + anomalías)
  importers.py exporters.py     CSV de inscripciones/lecturas; resultados CSV/HTML
  capture/                      writer, runner (proceso), cli, simulated, llrp, launcher
  simulator.py                  simulador con ruido (duplicados, pérdidas, chips ajenos…)
  ui/                           app, ventana principal y pestañas
tests/                          pytest (núcleo, motor, captura, UI sin pantalla)
```

## Pruebas

```bash
pytest            # ~50 pruebas; la UI corre sin pantalla (QT_QPA_PLATFORM=offscreen)
```

Con 2 000 corredores y ~35 000 lecturas, recalcular todos los resultados toma ~0,2 s.

## Limitaciones conocidas / pendiente

- **LLRP sin validar con un lector físico**: probado contra un cliente simulado y con la configuración real de `sllurp`, pero falta una prueba con hardware (antes de una carrera real).
- Aún no hay: salidas por oleadas con horarios distintos, PDF/diplomas, resultados en vivo por red/kiosco, varias PCs en red, importación desde formatos propietarios de decodificadores (solo CSV `chip,hora`).
- Empaquetado (PyInstaller/`pyside6-deploy`) y pruebas en Windows pendientes.
- La base contiene datos personales: **no se versionan** (`*.db` está en `.gitignore`).

## Licencias
PySide6 se distribuye bajo LGPL; `sllurp` (opcional) bajo su propia licencia. Revise los avisos si va a redistribuir la aplicación.
