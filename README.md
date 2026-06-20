# Separador de remitos PDF

Web app simple en Python + FastAPI para subir un PDF escaneado con muchos remitos,
leer cada pagina con OCR local, agrupar paginas por remito y descargar un ZIP con
un PDF separado por cada remito.

No usa APIs externas.

## Produccion con Docker y Easypanel

Configuracion prevista:

- Repositorio GitHub: `pdf-remitos-controlados-v2`
- Visibilidad: publico
- Deploy: Easypanel con Docker
- Puerto interno: `8000`
- Comando de arranque:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

El `Dockerfile` ya instala las dependencias necesarias del sistema:

```text
poppler-utils
tesseract-ocr
tesseract-ocr-spa
```

### Deploy en Easypanel

1. Subir este proyecto a un repositorio privado de GitHub llamado
   `pdf-remitos-controlados`.
2. En Easypanel, crear una nueva app desde GitHub.
3. Seleccionar el repositorio `pdf-remitos-controlados`.
4. Elegir deploy con Dockerfile.
5. Configurar el puerto interno de la app:

```text
8000
```

6. Por ahora usar la URL generada por Easypanel.

### Volumenes persistentes en Easypanel

La app usa estas carpetas dentro del contenedor:

```text
/app/uploads
/app/outputs
```

En Easypanel conviene crear volumenes persistentes para ambas rutas:

```text
/app/uploads
/app/outputs
```

Aunque la app borra archivos viejos automaticamente cada 5 minutos, los volumenes
evitan problemas de permisos y dejan esas carpetas disponibles de forma estable.

### Limite de subida

El tamano maximo permitido por la app es:

```text
100 MB por PDF
```

Si Easypanel o un proxy delante de la app tiene un limite menor, tambien hay que
subir ese limite en la configuracion del proxy/app.

### Archivos fuera de GitHub

No se deben subir al repositorio:

```text
.venv/
uploads/
outputs/
tools/poppler/
*.pdf
*.zip
```

En Linux/Docker no hace falta `tools/poppler`, porque Poppler se instala dentro
del contenedor con `apt`.

## Requisitos en Windows

La app necesita dos programas externos disponibles en el `PATH`:

- Poppler: usado por `pdf2image` para leer y convertir paginas del PDF.
- Tesseract OCR: usado por `pytesseract` para leer texto en las imagenes.

Si aparece este error:

```text
Unable to get page count. Is poppler installed and in PATH?
```

significa que falta Poppler o que Windows no encuentra sus ejecutables en el `PATH`.

### Opcion A: instalar con winget

Abrir PowerShell como usuario normal y ejecutar:

```powershell
winget install oschwartz10612.Poppler
winget install UB-Mannheim.TesseractOCR
```

Cerrar y volver a abrir PowerShell para recargar el `PATH`.

Verificar:

```powershell
pdfinfo -v
pdftoppm -v
tesseract --version
```

Los tres comandos deben responder sin error.

### Opcion B: instalar Poppler manualmente

1. Descargar Poppler para Windows desde:
   `https://github.com/oschwartz10612/poppler-windows/releases`
2. Descargar el archivo `.zip` mas reciente.
3. Descomprimirlo, por ejemplo en:

```text
C:\poppler
```

4. Buscar la carpeta `bin`. Normalmente queda parecida a:

```text
C:\poppler\Library\bin
```

5. Agregar esa carpeta al `PATH` de Windows:
   - Inicio -> buscar "variables de entorno".
   - Abrir "Editar las variables de entorno del sistema".
   - Boton "Variables de entorno".
   - En "Variables de usuario", seleccionar `Path`.
   - Boton "Editar".
   - Boton "Nuevo".
   - Agregar `C:\poppler\Library\bin`.
   - Aceptar todo.

6. Cerrar y volver a abrir PowerShell.

Verificar:

```powershell
pdfinfo -v
pdftoppm -v
```

### Opcion C: instalar Tesseract manualmente

1. Descargar el instalador de Tesseract para Windows desde:
   `https://github.com/UB-Mannheim/tesseract/wiki`
2. Durante la instalacion, incluir el idioma Spanish si el instalador lo ofrece.
3. Instalarlo normalmente. La ruta comun es:

```text
C:\Program Files\Tesseract-OCR
```

4. Agregar esa carpeta al `PATH` si el instalador no lo hizo:
   - Inicio -> buscar "variables de entorno".
   - Abrir "Editar las variables de entorno del sistema".
   - Boton "Variables de entorno".
   - En "Variables de usuario", seleccionar `Path`.
   - Boton "Editar".
   - Boton "Nuevo".
   - Agregar `C:\Program Files\Tesseract-OCR`.
   - Aceptar todo.

5. Cerrar y volver a abrir PowerShell.

Verificar:

```powershell
tesseract --version
tesseract --list-langs
```

En la lista de idiomas deberia aparecer `spa`. Si no aparece, instalar el paquete
de idioma Spanish o copiar `spa.traineddata` en la carpeta `tessdata` de Tesseract.

### Ejecutar en Windows

Crear entorno virtual:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Ejecutar:

```powershell
uvicorn app.main:app --reload
```

Abrir:

```text
http://127.0.0.1:8000
```

La pagina inicial avisa si falta `pdfinfo`, `pdftoppm` o `tesseract`.
Tambien se puede revisar el estado en:

```text
http://127.0.0.1:8000/health
```

Importante: si instalaste Poppler o Tesseract mientras la app ya estaba abierta,
detene Uvicorn con `Ctrl+C`, cerra PowerShell, abri una terminal nueva y volve a
ejecutar `uvicorn app.main:app --reload`. Windows no actualiza el `PATH` dentro
de procesos que ya estaban abiertos.

La app tambien intenta detectar automaticamente estas rutas comunes aunque no esten
en el `PATH`:

```text
C:\Users\<usuario>\...\veladero_pdf\tools\poppler\Library\bin
C:\poppler\Library\bin
C:\poppler\bin
C:\Program Files\Tesseract-OCR
```

En este proyecto tambien se puede copiar Poppler dentro de `tools/poppler`. Si esa
carpeta existe, la app la usa antes que el `PATH`.

## Requisitos en Ubuntu

Instalar dependencias del sistema:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip tesseract-ocr tesseract-ocr-spa poppler-utils
```

Crear entorno virtual e instalar dependencias Python:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Ejecutar la app:

```bash
uvicorn app.main:app --reload
```

Abrir:

```text
http://127.0.0.1:8000
```

## Como funciona

1. El usuario sube un PDF desde la web.
2. `PyMuPDF` intenta leer primero la capa de texto seleccionable del PDF.
3. Si PyMuPDF no obtiene texto util, `pypdf` intenta leer la misma pagina como
   segundo metodo.
4. Si ambos metodos fallan o no detectan los campos necesarios, `pdf2image`
   convierte solo esa pagina a imagen a 200 DPI.
5. `pytesseract` aplica OCR primero sobre la zona superior derecha de la pagina,
   donde suelen estar los datos principales.
6. Si el OCR por zona no detecta lo necesario, recien ahi aplica OCR sobre la
   pagina completa.
7. La app busca en cada pagina:
   - `Pagina 1 of 1`, `Pagina 1 of 2`, `Pagina 2 of 2`, etc.
   - `Documento de compras`
   - `Entrega entrante`
   - `Identificacion externa`
8. Si la pagina dice que es pagina 1, empieza un nuevo remito.
9. Las paginas siguientes se agregan al remito actual hasta completar el total detectado.
10. `pypdf` crea un PDF separado por cada grupo.
11. Se genera un ZIP final con todos los PDFs.

La app registra logs por pagina indicando si uso texto directo o OCR fallback y
cuanto tardo cada metodo.

Tambien registra tiempos totales del proceso:

```text
TOTAL subida/guardado PDF: Xs
TOTAL PyMuPDF: Xs
TOTAL pypdf: Xs
TOTAL deteccion de campos: Xs
TOTAL OCR zona superior derecha: Xs
TOTAL OCR pagina completa: Xs
TOTAL OCR fallback: Xs
TOTAL agrupado remitos: Xs
TOTAL separacion PDFs: Xs
TOTAL creacion ZIP: Xs
TOTAL proceso completo: Xs
```

Para reducir escrituras innecesarias, los PDFs separados se generan en memoria y
se agregan directo al ZIP final. No se guardan PDFs individuales intermedios en
`outputs/`.

## Modos de separacion

La interfaz permite elegir entre dos modos.

### Separacion actual por remito

Es el modo por defecto. Mantiene la logica original:

- detecta `Pagina 1 of 1`, `Pagina 1 of 2`, `Pagina 2 of 2`, etc.;
- agrupa paginas que pertenecen al mismo remito;
- nombra los PDFs con Documento de compras, Entrega entrante e Identificacion externa.

### Separar por bloques Veladero

Este modo sirve para PDFs ordenados fisicamente asi:

```text
Lista de Recibos Veladero
Remito proveedor
Lista de Recibos Veladero
Remito proveedor
```

La regla es:

- cada bloque empieza en una pagina que contiene `Lista de Recibos`;
- el bloque incluye esa pagina y todas las siguientes;
- el bloque termina justo antes de la proxima pagina que contenga `Lista de Recibos`;
- el remito del proveedor queda dentro del mismo PDF final;
- si una `Lista de Recibos` tiene varias hojas, se mantienen dentro del mismo bloque.

Ejemplo:

```text
Pagina 1: Lista de Recibos Veladero
Pagina 2: Remito proveedor
Pagina 3: Lista de Recibos Veladero
Pagina 4: Remito proveedor
```

Salida:

```text
PDF 1: paginas 1, 2
PDF 2: paginas 3, 4
```

El nombre del PDF en este modo se toma desde la primera pagina del bloque:

```text
OC_4501391063_EE_181817164_REMITO_0020-00009068.pdf
```

Los logs muestran las paginas incluidas en cada bloque:

```text
Bloque Veladero 1: paginas 1, 2; OC=4501391063; EE=181817164; REMITO=0020-00009068
```

Si no se detecta ninguna pagina con `Lista de Recibos`, la app muestra un error
claro y no genera un ZIP vacio.

## Limpieza automatica

La app guarda archivos temporales en estas carpetas del proyecto:

```text
uploads/
outputs/
```

Si la app corre en tu computadora, esas carpetas estan en tu computadora.
Si la app corre en una VPS, esas carpetas estan en el disco de la VPS.

Para evitar que se llene el disco, FastAPI ejecuta una limpieza automatica cada
5 minutos. Borra archivos subidos y resultados generados que tengan mas de
5 minutos de antiguedad. Los trabajos que todavia se estan procesando no se borran.

## Nombres de salida

Cada PDF se renombra con este formato:

```text
001_<Documento de compras>_<Entrega entrante>_<Identificacion externa>.pdf
```

Si algun dato no se pudo leer, se usa:

```text
sin_documento_compras
sin_entrega_entrante
sin_identificacion_externa
```

## Ajustes habituales

Los patrones de lectura estan en `app/main.py`:

- `find_page_match`: detecta pagina actual y total.
- `find_field`: detecta campos por etiqueta.
- `group_receipts`: agrupa paginas en remitos.

Si el OCR devuelve etiquetas con otra forma, agregar variantes en la lista de labels
de `read_pdf_pages` / `build_page_data`.

## Estructura

```text
app/
  main.py
  static/styles.css
  templates/index.html
uploads/
outputs/
requirements.txt
README.md
```

`uploads/` y `outputs/` se usan como carpetas de trabajo local.
