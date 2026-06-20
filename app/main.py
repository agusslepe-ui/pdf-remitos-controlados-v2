import asyncio
import io
import logging
import re
import shutil
import subprocess
import time
import unicodedata
import uuid
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from os import getenv
from pathlib import Path

import fitz
import pytesseract
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pdf2image import convert_from_path
from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError
from pypdf import PdfReader, PdfWriter


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = Path(getenv("UPLOADS_DIR", BASE_DIR / "uploads"))
OUTPUTS_DIR = Path(getenv("OUTPUTS_DIR", BASE_DIR / "outputs"))
STATIC_DIR = BASE_DIR / "app" / "static"
TEMPLATES_DIR = BASE_DIR / "app" / "templates"
LOCAL_APP_DATA = Path.home() / "AppData" / "Local"
CLEANUP_INTERVAL_SECONDS = 300
CLEANUP_MAX_AGE_SECONDS = 300
MAX_UPLOAD_SIZE_BYTES = 100 * 1024 * 1024
TESSERACT_LANG = "spa+eng"
TESSERACT_CONFIG = "--psm 6"
EE_DETECTION_REGEX = r"\bentrega\s+entrante\b\D{0,250}(18\d{6,10})"

UPLOADS_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

active_jobs: set[str] = set()


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    cleanup_task = asyncio.create_task(cleanup_worker())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Separador de remitos PDF", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@dataclass
class DependencyStatus:
    name: str
    command: str
    installed: bool
    detail: str
    executable: str | None = None


@dataclass
class PageData:
    page_index: int
    text: str
    zone_text: str | None
    source: str
    current_page: int | None
    total_pages: int | None
    purchase_document: str | None
    inbound_delivery: str | None
    external_id: str | None


@dataclass
class ReceiptGroup:
    pages: list[int]
    purchase_document: str | None
    inbound_delivery: str | None
    external_id: str | None


PROCESSING_MODE_STANDARD = "standard"
PROCESSING_MODE_VELADERO_BLOCKS = "veladero_blocks"


@dataclass
class ReadStats:
    pymupdf_seconds: float = 0.0
    pypdf_seconds: float = 0.0
    detection_seconds: float = 0.0
    ocr_zone_seconds: float = 0.0
    ocr_full_seconds: float = 0.0
    pymupdf_pages: int = 0
    pypdf_pages: int = 0
    ocr_zone_pages: int = 0
    ocr_full_pages: int = 0


@dataclass
class ZipStats:
    split_seconds: float = 0.0
    zip_seconds: float = 0.0


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    dependencies = check_dependencies()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "dependencies": dependencies,
            "missing_dependencies": [dep for dep in dependencies if not dep.installed],
        },
    )


@app.get("/health")
def health():
    dependencies = check_dependencies()
    return {
        "ok": all(dep.installed for dep in dependencies),
        "dependencies": [
            {
                "name": dep.name,
                "command": dep.command,
                "installed": dep.installed,
                "detail": dep.detail,
                "executable": dep.executable,
            }
            for dep in dependencies
        ],
    }


@app.post("/process")
async def process_pdf(
    request: Request,
    file: UploadFile = File(...),
    processing_mode: str = Form(PROCESSING_MODE_STANDARD),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Subi un archivo PDF.")

    total_start = time.perf_counter()
    job_id = uuid.uuid4().hex
    job_dir = OUTPUTS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = UPLOADS_DIR / f"{job_id}.pdf"
    active_jobs.add(job_id)

    try:
        upload_start = time.perf_counter()
        with input_path.open("wb") as buffer:
            copy_upload_with_limit(file, buffer)
        upload_seconds = time.perf_counter() - upload_start

        read_start = time.perf_counter()
        page_data, read_stats = read_pdf_pages(
            input_path,
            prefer_full_ocr=processing_mode == PROCESSING_MODE_VELADERO_BLOCKS,
        )
        read_seconds = time.perf_counter() - read_start
        if not page_data:
            raise HTTPException(status_code=400, detail="No se encontraron paginas en el PDF.")

        save_ee_missing_diagnostic_pages(input_path, page_data, job_dir / "revision_ee_sin_detectar")

        grouping_start = time.perf_counter()
        if processing_mode == PROCESSING_MODE_VELADERO_BLOCKS:
            groups = group_veladero_blocks(page_data)
        else:
            groups = group_receipts(page_data)
        grouping_seconds = time.perf_counter() - grouping_start
        if not groups:
            if processing_mode == PROCESSING_MODE_VELADERO_BLOCKS:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "No se detecto ninguna pagina con 'Lista de Recibos'. "
                        "Revisa el modo seleccionado o la calidad del OCR."
                    ),
                )
            raise HTTPException(status_code=422, detail="No se pudieron detectar remitos.")

        zip_path = job_dir / "remitos_separados.zip"
        zip_stats = create_zip_from_pdf(input_path, groups, zip_path, processing_mode)
        total_seconds = time.perf_counter() - total_start

        logger.info("TOTAL subida/guardado PDF: %.3fs", upload_seconds)
        logger.info("TOTAL PyMuPDF: %.3fs (%s paginas)", read_stats.pymupdf_seconds, read_stats.pymupdf_pages)
        logger.info("TOTAL pypdf: %.3fs (%s paginas)", read_stats.pypdf_seconds, read_stats.pypdf_pages)
        logger.info("TOTAL deteccion de campos: %.3fs", read_stats.detection_seconds)
        logger.info(
            "TOTAL OCR zona superior derecha: %.3fs (%s paginas)",
            read_stats.ocr_zone_seconds,
            read_stats.ocr_zone_pages,
        )
        logger.info(
            "TOTAL OCR pagina completa: %.3fs (%s paginas)",
            read_stats.ocr_full_seconds,
            read_stats.ocr_full_pages,
        )
        logger.info(
            "TOTAL OCR fallback: %.3fs",
            read_stats.ocr_zone_seconds + read_stats.ocr_full_seconds,
        )
        logger.info("TOTAL agrupado remitos: %.3fs", grouping_seconds)
        logger.info("TOTAL separacion PDFs: %.3fs", zip_stats.split_seconds)
        logger.info("TOTAL creacion ZIP: %.3fs", zip_stats.zip_seconds)
        logger.info("TOTAL lectura/deteccion completa: %.3fs", read_seconds)
        logger.info("TOTAL proceso completo: %.3fs", total_seconds)

        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename="remitos_separados.zip",
        )
    except HTTPException as exc:
        return error_page(request, exc.detail, exc.status_code)
    except PDFInfoNotInstalledError as exc:
        return error_page(
            request,
            (
                "No se pudo leer el PDF porque falta Poppler. Instala Poppler o agrega "
                "su carpeta bin al PATH. En Windows suele ser C:\\poppler\\Library\\bin."
            ),
            500,
        )
    except PDFPageCountError as exc:
        return error_page(
            request,
            (
                "No se pudo contar las paginas del PDF. Revisa que Poppler este instalado "
                "y que pdfinfo y pdftoppm funcionen en PowerShell."
            ),
            500,
        )
    except pytesseract.TesseractNotFoundError as exc:
        return error_page(
            request,
            (
                "No se pudo ejecutar OCR porque falta Tesseract. Instala Tesseract o "
                "agrega C:\\Program Files\\Tesseract-OCR al PATH."
            ),
            500,
        )
    except Exception as exc:
        return error_page(request, f"Error procesando PDF: {exc}", 500)
    finally:
        active_jobs.discard(job_id)
        await file.close()


def copy_upload_with_limit(file: UploadFile, buffer) -> None:
    copied = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break

        copied += len(chunk)
        if copied > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="El PDF supera el tamano maximo permitido de 100 MB.",
            )

        buffer.write(chunk)


async def cleanup_worker() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        cleanup_old_files()


def cleanup_old_files() -> None:
    now = time.time()
    cleanup_uploads(now)
    cleanup_outputs(now)


def cleanup_uploads(now: float) -> None:
    for path in UPLOADS_DIR.iterdir():
        if path.name == ".gitkeep" or not path.is_file():
            continue
        if path.stem in active_jobs:
            continue
        delete_if_old(path, now)


def cleanup_outputs(now: float) -> None:
    for path in OUTPUTS_DIR.iterdir():
        if path.name == ".gitkeep":
            continue
        if path.is_dir() and path.name in active_jobs:
            continue
        delete_if_old(path, now)


def delete_if_old(path: Path, now: float) -> None:
    try:
        age = now - path.stat().st_mtime
        if age < CLEANUP_MAX_AGE_SECONDS:
            return

        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def check_dependencies() -> list[DependencyStatus]:
    return [
        check_command(
            name="Poppler pdfinfo",
            command="pdfinfo",
            args=["pdfinfo", "-v"],
            help_text="Falta Poppler en PATH. pdf2image lo necesita para contar paginas del PDF.",
        ),
        check_command(
            name="Poppler pdftoppm",
            command="pdftoppm",
            args=["pdftoppm", "-v"],
            help_text="Falta Poppler en PATH. pdf2image lo necesita para convertir paginas a imagen.",
        ),
        check_command(
            name="Tesseract OCR",
            command="tesseract",
            args=["tesseract", "--version"],
            help_text="Falta Tesseract en PATH. pytesseract lo necesita para leer texto.",
        ),
    ]


def check_command(
    name: str,
    command: str,
    args: list[str],
    help_text: str,
) -> DependencyStatus:
    executable = find_executable(command)
    if not executable:
        return DependencyStatus(name=name, command=command, installed=False, detail=help_text)

    try:
        command_args = [executable, *args[1:]]
        result = subprocess.run(
            command_args,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return DependencyStatus(
            name=name,
            command=command,
            installed=False,
            detail=f"No se pudo ejecutar {command}: {exc}",
            executable=executable,
        )

    output = (result.stdout or result.stderr).strip().splitlines()
    detail = output[0] if output else f"{command} encontrado en {executable}"
    return DependencyStatus(
        name=name,
        command=command,
        installed=result.returncode == 0,
        detail=detail if result.returncode == 0 else f"{command} respondio con error: {detail}",
        executable=executable,
    )


def find_executable(command: str) -> str | None:
    executable = shutil.which(command)
    if executable:
        return executable

    candidates = windows_executable_candidates(command)
    for candidate in candidates:
        try:
            exists = candidate.exists()
        except PermissionError:
            continue
        if exists:
            return str(candidate)
    return None


def windows_executable_candidates(command: str) -> list[Path]:
    executable_name = command if command.endswith(".exe") else f"{command}.exe"
    program_files = [Path("C:/Program Files"), Path("C:/Program Files (x86)")]

    if command in {"pdfinfo", "pdftoppm"}:
        candidates = [
            BASE_DIR / "tools" / "poppler" / "Library" / "bin" / executable_name,
            Path("C:/poppler/Library/bin") / executable_name,
            Path("C:/poppler/bin") / executable_name,
        ]
        winget_packages = LOCAL_APP_DATA / "Microsoft" / "WinGet" / "Packages"
        candidates.append(
            winget_packages
            / "oschwartz10612.Poppler_Microsoft.Winget.Source_8wekyb3d8bbwe"
            / "poppler-25.07.0"
            / "Library"
            / "bin"
            / executable_name
        )
        candidates.extend(winget_packages.glob(f"oschwartz10612.Poppler*/**/{executable_name}"))
        for base in program_files:
            candidates.extend(base.glob(f"poppler*/Library/bin/{executable_name}"))
            candidates.extend(base.glob(f"poppler*/bin/{executable_name}"))
        return candidates

    if command == "tesseract":
        return [
            Path("C:/Program Files/Tesseract-OCR") / executable_name,
            Path("C:/Program Files (x86)/Tesseract-OCR") / executable_name,
        ]

    return []


def get_poppler_path() -> str | None:
    pdfinfo = find_executable("pdfinfo")
    pdftoppm = find_executable("pdftoppm")
    if not pdfinfo or not pdftoppm:
        return None

    pdfinfo_dir = Path(pdfinfo).parent
    if pdfinfo_dir == Path(pdftoppm).parent:
        return str(pdfinfo_dir)
    return None


def configure_tesseract() -> None:
    tesseract = find_executable("tesseract")
    if tesseract:
        pytesseract.pytesseract.tesseract_cmd = tesseract


def error_page(request: Request, message: str, status_code: int = 500) -> HTMLResponse:
    dependencies = check_dependencies()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "error": message,
            "dependencies": dependencies,
            "missing_dependencies": [dep for dep in dependencies if not dep.installed],
        },
        status_code=status_code,
    )


def read_pdf_pages(pdf_path: Path, prefer_full_ocr: bool = False) -> tuple[list[PageData], ReadStats]:
    pymupdf_doc = fitz.open(pdf_path)
    pypdf_reader = PdfReader(str(pdf_path))
    pages: list[PageData] = []
    stats = ReadStats()
    try_zone_ocr = not prefer_full_ocr

    try:
        for index in range(len(pymupdf_doc)):
            page = try_text_method(
                index=index,
                method_name="PyMuPDF",
                source="pymupdf",
                extractor=lambda page_index: pymupdf_doc[page_index].get_text("text"),
                stats=stats,
            )
            if page:
                stats.pymupdf_pages += 1
                pages.append(page)
                continue

            page = try_text_method(
                index=index,
                method_name="pypdf",
                source="pypdf",
                extractor=lambda page_index: pypdf_reader.pages[page_index].extract_text() or "",
                stats=stats,
            )
            if page:
                stats.pypdf_pages += 1
                pages.append(page)
                continue

            if try_zone_ocr:
                logger.info("Pagina %s: PyMuPDF y pypdf insuficientes; inicia OCR por zona", index + 1)
            else:
                logger.info("Pagina %s: PyMuPDF y pypdf insuficientes; salta OCR por zona y usa pagina completa", index + 1)

            ocr_page, zone_failed = ocr_page_with_fallback(pdf_path, index, stats, try_zone_ocr)
            if zone_failed:
                try_zone_ocr = False
                logger.info("OCR por zona desactivado para las siguientes paginas porque no alcanzo en pagina %s", index + 1)
            pages.append(ocr_page)
    finally:
        pymupdf_doc.close()

    add_top_right_zone_ocr(pdf_path, pages, stats)
    return pages, stats


def add_top_right_zone_ocr(pdf_path: Path, pages: list[PageData], stats: ReadStats) -> None:
    configure_tesseract()
    for page in pages:
        zone_start = time.perf_counter()
        images = convert_from_path(
            pdf_path,
            dpi=200,
            first_page=page.page_index + 1,
            last_page=page.page_index + 1,
            poppler_path=get_poppler_path(),
        )
        zone_text = pytesseract.image_to_string(
            crop_top_right_zone(images[0]),
            lang=TESSERACT_LANG,
            config=TESSERACT_CONFIG,
        )
        zone_elapsed = time.perf_counter() - zone_start
        stats.ocr_zone_seconds += zone_elapsed
        page.zone_text = normalize_text(zone_text)
        logger.info(
            "pagina fisica %s: texto OCR zona superior derecha='%s'",
            page.page_index + 1,
            diagnostic_text_sample_length(page.zone_text, 1000),
        )


def try_text_method(
    index: int,
    method_name: str,
    source: str,
    extractor,
    stats: ReadStats,
) -> PageData | None:
    extract_start = time.perf_counter()
    text = normalize_text(extractor(index) or "")
    extract_elapsed = time.perf_counter() - extract_start

    if source == "pymupdf":
        stats.pymupdf_seconds += extract_elapsed
    elif source == "pypdf":
        stats.pypdf_seconds += extract_elapsed

    detection_start = time.perf_counter()
    page = build_page_data(index, text, source)
    detection_elapsed = time.perf_counter() - detection_start
    stats.detection_seconds += detection_elapsed

    if direct_text_is_usable(page):
        logger.info(
            "Pagina %s: uso %s en %.3fs; deteccion en %.3fs",
            index + 1,
            method_name,
            extract_elapsed,
            detection_elapsed,
        )
        return page

    logger.info(
        "Pagina %s: %s insuficiente en %.3fs; deteccion en %.3fs",
        index + 1,
        method_name,
        extract_elapsed,
        detection_elapsed,
    )
    return None


def build_page_data(index: int, text: str, source: str) -> PageData:
    normalized = normalize_text(text)
    return PageData(
        page_index=index,
        text=normalized,
        zone_text=None,
        source=source,
        current_page=parse_current_page(normalized),
        total_pages=parse_total_pages(normalized),
                purchase_document=find_field(
                    normalized,
                    [
                        "Documento de compras",
                        "Documento compras",
                        "Doc. compras",
                        "OC",
                    ],
                ),
        inbound_delivery=find_field(
            normalized,
            [
                "Entrega entrante",
                "Entrega entrada",
            ],
        ),
                external_id=find_field(
                    normalized,
                    [
                        "Identificacion externa",
                        "Remito proveedor",
                        "Remito",
                        "ID externa",
                    ],
                ),
    )


def direct_text_is_usable(page: PageData) -> bool:
    has_text = bool(page.text.strip())
    has_page_numbers = page.current_page is not None and page.total_pages is not None

    if not has_text or not has_page_numbers:
        return False

    return True


def ocr_page_with_fallback(
    pdf_path: Path,
    page_index: int,
    stats: ReadStats,
    try_zone_ocr: bool,
) -> tuple[PageData, bool]:
    configure_tesseract()
    images = convert_from_path(
        pdf_path,
        dpi=200,
        first_page=page_index + 1,
        last_page=page_index + 1,
        poppler_path=get_poppler_path(),
    )
    image = images[0]

    if try_zone_ocr:
        zone_start = time.perf_counter()
        zone_text = pytesseract.image_to_string(
            crop_top_right_zone(image),
            lang=TESSERACT_LANG,
            config=TESSERACT_CONFIG,
        )
        zone_elapsed = time.perf_counter() - zone_start
        stats.ocr_zone_seconds += zone_elapsed

        detection_start = time.perf_counter()
        zone_page = build_page_data(page_index, zone_text, "ocr_zona_superior_derecha")
        detection_elapsed = time.perf_counter() - detection_start
        stats.detection_seconds += detection_elapsed

        if ocr_zone_is_complete(zone_page):
            stats.ocr_zone_pages += 1
            logger.info(
                "Pagina %s: uso OCR zona superior derecha en %.3fs; deteccion en %.3fs",
                page_index + 1,
                zone_elapsed,
                detection_elapsed,
            )
            return zone_page, False

        logger.info(
            "Pagina %s: OCR zona superior derecha insuficiente en %.3fs; deteccion en %.3fs; faltan: %s; inicia OCR pagina completa",
            page_index + 1,
            zone_elapsed,
            detection_elapsed,
            ", ".join(missing_critical_fields(zone_page)),
        )

    full_start = time.perf_counter()
    full_text = pytesseract.image_to_string(image, lang=TESSERACT_LANG, config=TESSERACT_CONFIG)
    full_elapsed = time.perf_counter() - full_start
    stats.ocr_full_seconds += full_elapsed
    stats.ocr_full_pages += 1

    detection_start = time.perf_counter()
    full_page = build_page_data(page_index, full_text, "ocr_pagina_completa")
    detection_elapsed = time.perf_counter() - detection_start
    stats.detection_seconds += detection_elapsed
    logger.info(
        "Pagina %s: uso OCR pagina completa en %.3fs; deteccion en %.3fs",
        page_index + 1,
        full_elapsed,
        detection_elapsed,
    )
    return full_page, try_zone_ocr


def ocr_zone_is_complete(page: PageData) -> bool:
    return not missing_critical_fields(page)


def missing_critical_fields(page: PageData) -> list[str]:
    missing: list[str] = []

    if page.current_page is None or page.total_pages is None:
        missing.append("pagina actual/total")

    if page.current_page in {None, 1} and detect_receipt_start_inbound_delivery(page) is None:
        missing.append("Entrega entrante")

    return missing


def crop_top_right_zone(image):
    width, height = image.size
    left = int(width * 0.40)
    top = 0
    right = width
    bottom = int(height * 0.38)
    return image.crop((left, top, right, bottom))


def normalize_text(text: str) -> str:
    text = text.replace("\x0c", "\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_current_page(text: str) -> int | None:
    match = find_page_match(text)
    if not match:
        return None
    return int(match.group("current"))


def parse_total_pages(text: str) -> int | None:
    match = find_page_match(text)
    if not match:
        return None
    return int(match.group("total"))


def find_page_match(text: str) -> re.Match[str] | None:
    patterns = [
        r"pagina\s*(?P<current>\d+)\s*(?:of|de|/)\s*(?P<total>\d+)",
        r"page\s*(?P<current>\d+)\s*(?:of|de|/)\s*(?P<total>\d+)",
        r"\b(?P<current>\d+)\s*/\s*(?P<total>\d+)\b",
    ]
    searchable_text = strip_accents(text)
    for pattern in patterns:
        match = re.search(pattern, searchable_text, flags=re.IGNORECASE)
        if match:
            return match
    return None


def find_field(text: str, labels: list[str]) -> str | None:
    lines = text.splitlines()
    for label in labels:
        label_pattern = re.escape(strip_accents(label))
        for line in lines:
            searchable_line = strip_accents(line)
            match = re.search(
                rf"{label_pattern}\s*:?\s*([A-Z0-9][A-Z0-9\-_/\.]*)",
                searchable_line,
                flags=re.IGNORECASE,
            )
            if match:
                return cleanup_value(match.group(1))

    joined_text = strip_accents(" ".join(lines))
    for label in labels:
        label_pattern = re.escape(strip_accents(label))
        match = re.search(
            rf"{label_pattern}\s*:?\s*([A-Z0-9][A-Z0-9\-_/\.]*)",
            joined_text,
            flags=re.IGNORECASE,
        )
        if match:
            return cleanup_value(match.group(1))

    return None


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def cleanup_value(value: str) -> str | None:
    value = value.strip(" :;,.")
    return value if value else None


def group_receipts(pages: list[PageData]) -> list[ReceiptGroup]:
    groups: list[ReceiptGroup] = []
    current_group: ReceiptGroup | None = None

    for page in pages:
        zone_text = page.zone_text or ""
        zone_inbound_delivery = detect_inbound_delivery_from_text(
            zone_text,
            page.page_index,
            "zona_superior_derecha",
            require_lista_recibos=True,
        )
        full_inbound_delivery = detect_receipt_start_inbound_delivery(page)
        zone_can_start = contains_lista_de_recibos(zone_text) and zone_inbound_delivery is not None
        full_can_start = contains_lista_de_recibos(page.text) and full_inbound_delivery is not None
        detected_inbound_delivery = zone_inbound_delivery if zone_can_start else full_inbound_delivery
        detection_source = "zona_superior_derecha" if zone_can_start else "pagina_completa"
        can_start_block = zone_can_start or full_can_start
        starts_new = (
            can_start_block
            and (
                current_group is None
                or detected_inbound_delivery != current_group.inbound_delivery
            )
        )
        logger.info(
            "pagina %s: inicio_bloque=%s; EE_detectada=%s",
            page.page_index + 1,
            starts_new,
            detected_inbound_delivery or "sin dato",
        )
        logger.info(
            "Pagina fisica %s: texto_zona='%s'; EE_zona=%s; EE_pagina_completa=%s; fuente_corte=%s; inicio_bloque=%s",
            page.page_index + 1,
            diagnostic_text_sample_length(zone_text, 1000),
            zone_inbound_delivery or "sin dato",
            full_inbound_delivery or "sin dato",
            detection_source if can_start_block else "sin_corte",
            starts_new,
        )
        log_missing_ee_diagnostic(page, detected_inbound_delivery)

        if starts_new:
            if current_group is not None:
                logger.info(
                    "Cierra bloque %s: paginas %s; cantidad_paginas=%s",
                    len(groups),
                    format_page_list(current_group.pages),
                    len(current_group.pages),
                )

            current_group = ReceiptGroup(
                pages=[page.page_index],
                purchase_document=None,
                inbound_delivery=detected_inbound_delivery,
                external_id=None,
            )
            groups.append(current_group)
            logger.info(
                "Abre bloque %s en pagina fisica %s; entrega_entrante=%s",
                len(groups),
                page.page_index + 1,
                detected_inbound_delivery or "sin dato",
            )
            continue

        if current_group is None:
            logger.info(
                "Pagina fisica %s ignorada: todavia no se detecto inicio de bloque valido",
                page.page_index + 1,
            )
            continue

        current_group.pages.append(page.page_index)

    if current_group is not None:
        logger.info(
            "Cierra bloque %s: paginas %s; cantidad_paginas=%s",
            len(groups),
            format_page_list(current_group.pages),
            len(current_group.pages),
        )

    return groups


def is_receipt_start_page(page: PageData) -> bool:
    return contains_lista_de_recibos(page.text) and detect_receipt_start_inbound_delivery(page) is not None


def has_receipt_start_pagination(text: str) -> bool:
    searchable_text = strip_accents(text)
    return find_receipt_start_pagination_match(searchable_text) is not None


def find_receipt_start_pagination_match(text: str) -> re.Match[str] | None:
    patterns = [
        r"\b(?:pagina|page)?\s*1\s*[o0]\s*f\s*[A-Z0-9]*\b",
        r"\b(?:pagina|page)\s*[1Il]\s*[o0]\s*f\s*[A-Z0-9]*\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, strip_accents(text), flags=re.IGNORECASE)
        if match:
            return match
    return None


def detect_receipt_start_inbound_delivery(page: PageData) -> str | None:
    return detect_inbound_delivery_from_text(
        page.text,
        page.page_index,
        "pagina_completa",
        require_lista_recibos=True,
        parsed_inbound_delivery=page.inbound_delivery,
    )


def detect_inbound_delivery_from_text(
    text: str,
    page_index: int,
    source: str,
    require_lista_recibos: bool,
    parsed_inbound_delivery: str | None = None,
) -> str | None:
    searchable_text = strip_accents(" ".join(text.splitlines()))
    match = re.search(EE_DETECTION_REGEX, searchable_text, flags=re.IGNORECASE)
    if match:
        inbound_delivery = cleanup_value(match.group(1))
        logger.info(
            "EE detectada por modo normal pagina %s fuente=%s: %s",
            page_index + 1,
            source,
            inbound_delivery or "sin dato",
        )
        return inbound_delivery

    if parsed_inbound_delivery and re.fullmatch(r"18\d{6,10}", parsed_inbound_delivery):
        logger.info(
            "EE detectada por campo parseado pagina %s fuente=%s: %s",
            page_index + 1,
            source,
            parsed_inbound_delivery,
        )
        return parsed_inbound_delivery

    recovered_inbound_delivery = recover_inbound_delivery_from_lista_recibos_text(
        text,
        page_index,
        source,
        require_lista_recibos,
    )
    if recovered_inbound_delivery:
        logger.info(
            "EE detectada por recuperacion OCR pagina %s fuente=%s: %s",
            page_index + 1,
            source,
            recovered_inbound_delivery,
        )
        return recovered_inbound_delivery

    return None


def recover_inbound_delivery_from_lista_recibos(page: PageData) -> str | None:
    return recover_inbound_delivery_from_lista_recibos_text(
        page.text,
        page.page_index,
        "pagina_completa",
        True,
    )


def recover_inbound_delivery_from_lista_recibos_text(
    text: str,
    page_index: int,
    source: str,
    require_lista_recibos: bool,
) -> str | None:
    if require_lista_recibos and not contains_lista_de_recibos(text):
        return None

    searchable_text = strip_accents(" ".join(text.splitlines()))
    candidates = find_nearby_inbound_delivery_candidates(
        searchable_text,
        [r"entrante", r"entrega", r"lista\s+de\s+recibos", r"lista\s+recibos"],
        250,
    )
    unique_candidates = sorted(set(candidates))
    logger.info(
        "Recuperacion OCR EE pagina %s fuente=%s: candidatos=%s",
        page_index + 1,
        source,
        unique_candidates or "sin candidatos",
    )

    if len(unique_candidates) == 1:
        return unique_candidates[0]

    return None


def find_nearby_inbound_delivery_candidates(
    text: str,
    anchor_patterns: list[str],
    radius: int,
) -> list[str]:
    candidates: list[str] = []
    for anchor_pattern in anchor_patterns:
        for anchor_match in re.finditer(anchor_pattern, text, flags=re.IGNORECASE):
            start = max(0, anchor_match.start() - radius)
            end = min(len(text), anchor_match.end() + radius)
            nearby_text = text[start:end]
            candidates.extend(re.findall(r"\b18\d{6,8}\b", nearby_text))
    return candidates


def log_missing_ee_diagnostic(page: PageData, detected_inbound_delivery: str | None) -> None:
    if not contains_lista_de_recibos(page.text) or detected_inbound_delivery:
        return

    searchable_text = strip_accents(" ".join(page.text.splitlines()))
    contains_entrante = re.search(r"entrante", searchable_text, flags=re.IGNORECASE) is not None
    logger.warning(
        "Diagnostico EE sin detectar pagina %s: lista_recibos=True; EE_detectada=sin dato; contiene_entrante=%s; regex='%s'; texto_1000='%s'; contexto_entrante='%s'",
        page.page_index + 1,
        contains_entrante,
        EE_DETECTION_REGEX,
        diagnostic_text_sample_length(page.text, 1000),
        text_context_around_pattern(page.text, r"entrante", 150),
    )


def save_ee_missing_diagnostic_pages(
    input_path: Path,
    pages: list[PageData],
    output_dir: Path,
) -> None:
    problem_pages = [
        page
        for page in pages
        if contains_lista_de_recibos(page.text) and detect_receipt_start_inbound_delivery(page) is None
    ]
    if not problem_pages:
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(input_path))
    summary_writer = PdfWriter()

    for page in problem_pages:
        writer = PdfWriter()
        writer.add_page(reader.pages[page.page_index])
        summary_writer.add_page(reader.pages[page.page_index])

        page_path = output_dir / f"pagina_{page.page_index + 1:04d}.pdf"
        with page_path.open("wb") as buffer:
            writer.write(buffer)
        logger.warning(
            "Pagina %s guardada para revision EE sin detectar: %s",
            page.page_index + 1,
            page_path,
        )

    summary_path = output_dir / "revision_ee_sin_detectar.pdf"
    with summary_path.open("wb") as buffer:
        summary_writer.write(buffer)
    logger.warning(
        "Revision EE sin detectar: %s paginas guardadas en %s",
        len(problem_pages),
        output_dir,
    )


def diagnostic_text_sample_length(text: str, max_length: int) -> str:
    return " ".join(text.split())[:max_length]


def text_context_around_pattern(text: str, pattern: str, radius: int) -> str:
    compact_text = " ".join(text.split())
    match = re.search(pattern, compact_text, flags=re.IGNORECASE)
    if not match:
        return "sin coincidencia"

    start = max(0, match.start() - radius)
    end = min(len(compact_text), match.end() + radius)
    return compact_text[start:end]


def format_detected_pagination(page: PageData) -> str:
    raw_match = find_receipt_start_pagination_match(page.text)
    if raw_match:
        return " ".join(raw_match.group(0).split())
    if page.current_page is None or page.total_pages is None:
        return "sin dato"
    return f"{page.current_page} of {page.total_pages}"


def group_veladero_blocks(pages: list[PageData]) -> list[ReceiptGroup]:
    return group_receipts(pages)


def extract_veladero_block_filename_fields(page: PageData) -> ReceiptGroup:
    return ReceiptGroup(
        pages=[page.page_index],
        purchase_document=find_veladero_filename_field(
            page.text,
            ["Documento de compras"],
            r"(\d{6,})",
        ),
        inbound_delivery=find_veladero_filename_field(
            page.text,
            ["Entrega entrante"],
            r"(\d{6,})",
        ),
        external_id=find_veladero_filename_field(
            page.text,
            ["Identificacion externa", "Identificación externa"],
            r"([A-Z0-9]{2,}[-_/\.][A-Z0-9\-_/\.]+)",
        ),
    )


def find_veladero_filename_field(text: str, labels: list[str], value_pattern: str) -> str | None:
    lines = [strip_accents(line) for line in text.splitlines()]
    label_patterns = [re.escape(strip_accents(label)) for label in labels]

    for label_pattern in label_patterns:
        same_line_pattern = rf"{label_pattern}\s*:?\s*{value_pattern}"
        for line in lines:
            match = re.search(same_line_pattern, line, flags=re.IGNORECASE)
            if match:
                return cleanup_value(match.group(1))

    joined_text = " ".join(lines)
    for label_pattern in label_patterns:
        next_line_pattern = rf"{label_pattern}\s*:?\s+{value_pattern}"
        match = re.search(next_line_pattern, joined_text, flags=re.IGNORECASE)
        if match:
            return cleanup_value(match.group(1))

    return None


def extract_veladero_block_filename_fields(page: PageData) -> ReceiptGroup:
    return ReceiptGroup(
        pages=[page.page_index],
        purchase_document=find_number_after_veladero_label(page.text, ["Documento de compras"], 6, 12),
        inbound_delivery=find_veladero_inbound_delivery(page.text),
        external_id=find_veladero_external_id(page.text),
    )


def find_number_after_veladero_label(
    text: str,
    labels: list[str],
    min_digits: int,
    max_digits: int,
) -> str | None:
    searchable_text = strip_accents(" ".join(text.splitlines()))
    value_pattern = rf"(\d{{{min_digits},{max_digits}}})"

    for label in labels:
        label_pattern = re.escape(strip_accents(label))
        match = re.search(
            rf"{label_pattern}\D{{0,40}}{value_pattern}",
            searchable_text,
            flags=re.IGNORECASE,
        )
        if match:
            return cleanup_value(match.group(1))

    return None


def find_veladero_inbound_delivery(text: str) -> str | None:
    searchable_text = strip_accents(" ".join(text.splitlines()))
    match = re.search(r"\bentrante\b\D{0,80}(\d{8,12})", searchable_text, flags=re.IGNORECASE)
    if match:
        return cleanup_value(match.group(1))
    return None


def find_veladero_external_id(text: str) -> str | None:
    searchable_text = strip_accents(" ".join(text.splitlines()))
    label_match = re.search(
        r"Identificacion\s+externa\D{0,80}(\d{4}-\d{8})",
        searchable_text,
        flags=re.IGNORECASE,
    )
    if label_match:
        return cleanup_value(label_match.group(1))

    remito_match = re.search(r"\b(\d{4}-\d{8})\b", searchable_text)
    if remito_match:
        return cleanup_value(remito_match.group(1))

    return None


def filename_text_sample(text: str) -> str:
    return " ".join(text.split())[:300]


def is_veladero_page(page: PageData) -> bool:
    if contains_lista_de_recibos(page.text):
        return True

    if page.purchase_document or page.inbound_delivery or page.external_id:
        return True

    return veladero_indicator_count(page.text) > 0


def has_veladero_start_signal(page: PageData) -> bool:
    if contains_lista_de_recibos(page.text):
        return True

    if page.purchase_document or page.inbound_delivery or page.external_id:
        return True

    # Proveedor + Material solos tambien pueden aparecer en un remito proveedor.
    # Para abrir bloque por indicadores, exigimos al menos una senal fuerte.
    return veladero_indicator_count(page.text) >= 2 and veladero_strong_indicator_count(page.text) >= 1


def veladero_indicator_count(text: str) -> int:
    normalized = strip_accents(text).lower()
    indicators = [
        "documento de compras",
        "documento compras",
        "doc. compras",
        "entrega entrante",
        "identificacion externa",
        "identificacion",
        "almacen",
        "proveedor",
        "posicion",
        "material",
        "recepcion",
        "recibos",
        "recibo",
    ]
    return sum(1 for indicator in indicators if indicator in normalized)


def veladero_strong_indicator_count(text: str) -> int:
    normalized = strip_accents(text).lower()
    strong_indicators = [
        "almacen",
        "posicion",
        "recepcion",
        "recibos",
        "recibo",
    ]
    return sum(1 for indicator in strong_indicators if indicator in normalized)


def contains_lista_de_recibos(text: str) -> bool:
    normalized = strip_accents(text).lower()
    compact = normalized.replace(" ", "")
    return (
        "lista de recibos" in normalized
        or "lista recibos" in normalized
        or "lista de recibo" in normalized
        or "listaderecibos" in compact
        or "listarecibos" in compact
    )


def diagnostic_text_sample(text: str) -> str:
    return " ".join(text.split())[:200]


def format_page_list(page_indexes: list[int]) -> str:
    if not page_indexes:
        return ""

    ranges: list[str] = []
    start = previous = page_indexes[0] + 1

    for page_index in page_indexes[1:]:
        page_number = page_index + 1
        if page_number == previous + 1:
            previous = page_number
            continue

        ranges.append(format_page_range(start, previous))
        start = previous = page_number

    ranges.append(format_page_range(start, previous))
    return ", ".join(ranges)


def format_page_range(start: int, end: int) -> str:
    if start == end:
        return str(start)
    return f"{start}-{end}"


def create_zip_from_pdf(
    input_path: Path,
    groups: list[ReceiptGroup],
    zip_path: Path,
    processing_mode: str = PROCESSING_MODE_STANDARD,
) -> ZipStats:
    reader = PdfReader(str(input_path))
    stats = ZipStats()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
        used_filenames: set[str] = set()
        for number, group in enumerate(groups, start=1):
            split_start = time.perf_counter()
            writer = PdfWriter()
            for page_index in group.pages:
                writer.add_page(reader.pages[page_index])

            pdf_buffer = io.BytesIO()
            writer.write(pdf_buffer)
            stats.split_seconds += time.perf_counter() - split_start

            zip_start = time.perf_counter()
            output_filename = unique_output_filename(
                build_output_filename(group, number, processing_mode),
                used_filenames,
            )
            logger.info(
                "PDF generado bloque %s: archivo=%s; cantidad_paginas=%s",
                number,
                output_filename,
                len(group.pages),
            )
            archive.writestr(output_filename, pdf_buffer.getvalue())
            stats.zip_seconds += time.perf_counter() - zip_start

    return stats


def unique_output_filename(filename: str, used_filenames: set[str]) -> str:
    if filename not in used_filenames:
        used_filenames.add(filename)
        return filename

    stem, dot, suffix = filename.rpartition(".")
    if not dot:
        stem = filename
        suffix = ""

    counter = 2
    while True:
        candidate = f"{stem}_{counter:03d}.{suffix}" if suffix else f"{stem}_{counter:03d}"
        if candidate not in used_filenames:
            used_filenames.add(candidate)
            return candidate
        counter += 1


def build_output_filename(group: ReceiptGroup, number: int, processing_mode: str) -> str:
    if processing_mode == PROCESSING_MODE_VELADERO_BLOCKS:
        return build_veladero_block_filename(group, number)
    return build_receipt_filename(group, number)


def build_veladero_block_filename(group: ReceiptGroup, number: int) -> str:
    return build_receipt_filename(group, number)


def build_receipt_filename(group: ReceiptGroup, number: int) -> str:
    inbound_delivery = safe_filename(group.inbound_delivery or f"sin_ee_{number:03d}")
    return f"EE_{inbound_delivery}.pdf"


def safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("._-")
    return value[:80] or "sin_dato"
