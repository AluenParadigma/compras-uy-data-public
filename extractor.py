import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright


# ============================================================
# CONFIGURACION
# ============================================================

BASE_URL = "https://www.comprasestatales.gub.uy"

GENERAL_VIG_URL = (
    f"{BASE_URL}/consultas/buscar/"
    "reset/1/tipo-pub/VIG"
)

OUTPUT_JSON = Path("latest.json")
OUTPUT_CSV = Path("latest.csv")
OUTPUT_METADATA = Path("metadata.json")

TEMP_JSON = Path("latest.tmp.json")
TEMP_CSV = Path("latest.tmp.csv")
TEMP_METADATA = Path("metadata.tmp.json")

NAV_TIMEOUT = 30000
CONTENT_TIMEOUT = 45000

MAX_HTTP_ATTEMPTS = 2

# Pausas conservadoras para no golpear ARCE.
PAGE_PAUSE_SECONDS = 0.25
DETAIL_PAUSE_SECONDS = 0.20

# Cada ventana usa como máximo este tamaño.
WINDOW_DAYS = 14

# Primero buscamos desde 2024.
# Si no alcanzamos el total general, ampliamos hacia atrás.
HISTORY_BLOCKS = [
    (
        datetime(2024, 1, 1),
        None,
    ),
    (
        datetime(2020, 1, 1),
        datetime(2023, 12, 31),
    ),
    (
        datetime(2010, 1, 1),
        datetime(2019, 12, 31),
    ),
]

MAX_PAGES_PER_WINDOW = 100


# ============================================================
# LOG
# ============================================================

def log(*args):
    print(*args, flush=True)


def log_error(*args):
    print(*args, file=sys.stderr, flush=True)


# ============================================================
# HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def absolute_url(href):
    return urljoin(
        BASE_URL,
        href,
    )


def extract_detail_id(url):
    """
    ARCE usa IDs numericos y alfanumericos:
    1302291
    i504650
    """

    if not url:
        return None

    match = re.search(
        r"/id/([^/?#]+)",
        url,
        re.I,
    )

    if match:
        return match.group(1)

    match = re.search(
        r"[?&]id=([^&#]+)",
        url,
        re.I,
    )

    if match:
        return match.group(1)

    return None


def extract_regex(text, pattern):
    match = re.search(
        pattern,
        text,
        flags=re.I | re.S,
    )

    if not match:
        return ""

    return clean_text(
        match.group(1)
    )


# ============================================================
# URLS DE RANGO
# ============================================================

def encode_portal_datetime(
    date_value,
    end_of_day=False,
):
    date_text = date_value.strftime(
        "%Y-%m-%d"
    )

    if end_of_day:
        return (
            f"{date_text}"
            "%2B23%3A59%3A59"
        )

    return (
        f"{date_text}"
        "%2B00%3A00%3A00"
    )


def build_window_url(
    start_date,
    end_date,
    page_number=1,
):
    """
    Ejemplo de estructura usada actualmente por ARCE:

    /consultas/buscar/
    reset/1/
    tipo-pub/VIG/
    tipo-fecha/MOD/
    orden/ORD_MOD/
    tipo-orden/DESC/
    rango-fecha/<inicio>_<fin>/
    page/1
    """

    start_encoded = (
        encode_portal_datetime(
            start_date,
            False,
        )
    )

    end_encoded = (
        encode_portal_datetime(
            end_date,
            True,
        )
    )

    return (
        f"{BASE_URL}/consultas/buscar/"
        "reset/1/"
        "tipo-pub/VIG/"
        "tipo-fecha/MOD/"
        "orden/ORD_MOD/"
        "tipo-orden/DESC/"
        f"rango-fecha/"
        f"{start_encoded}_"
        f"{end_encoded}/"
        f"page/{page_number}"
    )


def generate_windows(
    start_date,
    end_date,
):
    current = start_date

    while current <= end_date:

        window_end = min(
            current
            + timedelta(
                days=WINDOW_DAYS - 1
            ),
            end_date,
        )

        yield (
            current,
            window_end,
        )

        current = (
            window_end
            + timedelta(days=1)
        )


# ============================================================
# NAVEGACION
# ============================================================

async def goto_with_retry(
    page,
    url,
    label,
):
    last_error = None

    for attempt in range(
        1,
        MAX_HTTP_ATTEMPTS + 1,
    ):

        try:

            response = await page.goto(
                url,
                wait_until="commit",
                timeout=NAV_TIMEOUT,
            )

            if response is None:
                raise RuntimeError(
                    "Navegacion sin respuesta HTTP"
                )

            status = response.status

            if status in (
                403,
                429,
                500,
                502,
                503,
                504,
            ):
                raise RuntimeError(
                    f"HTTP {status}"
                )

            if status >= 400:
                raise RuntimeError(
                    f"HTTP {status}"
                )

            await page.wait_for_selector(
                "body",
                timeout=CONTENT_TIMEOUT,
            )

            return response

        except Exception as exc:

            last_error = exc

            log_error(
                f"{label}: intento "
                f"{attempt}/"
                f"{MAX_HTTP_ATTEMPTS} "
                f"fallo: {exc}"
            )

            if (
                attempt
                < MAX_HTTP_ATTEMPTS
            ):

                await asyncio.sleep(
                    3 * attempt
                )

    raise RuntimeError(
        f"{label}: fallo luego de "
        f"{MAX_HTTP_ATTEMPTS} intentos. "
        f"Ultimo error: {last_error}"
    )


async def wait_for_search_content(page):
    await page.wait_for_function(
        """
        () => {
            const text =
                document.body
                ? document.body.innerText
                : '';

            return (
                text.includes('Se encontraron') ||
                text.includes(
                    'La búsqueda no produjo'
                ) ||
                text.includes(
                    'La busqueda no produjo'
                ) ||
                text.includes(
                    'Llamados vigentes'
                )
            );
        }
        """,
        timeout=CONTENT_TIMEOUT,
    )


async def wait_for_results_stable(page):
    selector = (
        "a[href*='/consultas/detalle/'], "
        "a[href*='mostrar-llamado']"
    )

    last_count = -1
    stable_rounds = 0

    for _ in range(15):

        count = await page.locator(
            selector
        ).count()

        if (
            count == last_count
            and
            count > 0
        ):
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 2:
            return count

        last_count = count

        await page.wait_for_timeout(
            500
        )

    return max(
        last_count,
        0,
    )


# ============================================================
# CONTADOR DEL PORTAL
# ============================================================

async def get_result_count(page):
    body = clean_text(
        await page.locator(
            "body"
        ).inner_text()
    )

    match = re.search(
        r"Se encontraron\s+"
        r"([\d\.]+)\s+resultados",
        body,
        re.I,
    )

    if match:

        return int(
            match.group(1)
            .replace(".", "")
        )

    if (
        "La búsqueda no produjo"
        in body
        or
        "La busqueda no produjo"
        in body
    ):
        return 0

    raise RuntimeError(
        "No se pudo determinar "
        "el contador de resultados."
    )


# ============================================================
# LINKS DE DETALLE
# ============================================================

async def collect_detail_links_from_page(
    page,
):
    links = {}

    locator = page.locator(
        "a[href*='/consultas/detalle/'], "
        "a[href*='mostrar-llamado']"
    )

    count = await locator.count()

    for i in range(count):

        element = locator.nth(i)

        href = await element.get_attribute(
            "href"
        )

        if not href:
            continue

        url = absolute_url(
            href
        )

        detail_id = extract_detail_id(
            url
        )

        if not detail_id:
            continue

        title = clean_text(
            await element.inner_text()
        )

        if not title:

            try:

                title = clean_text(
                    await element.locator(
                        "xpath=.."
                    ).inner_text()
                )

            except Exception:

                title = ""

        if not title:
            title = (
                f"Llamado {detail_id}"
            )

        links[detail_id] = {
            "detail_id":
                detail_id,

            "detail_url":
                url,

            "titulo_listado":
                title,
        }

    return links


# ============================================================
# HEALTH CHECK + TOTAL GENERAL
# ============================================================

async def get_general_portal_total(page):
    log("")
    log(
        "===================================="
    )
    log(
        "TOTAL GENERAL LLAMADOS VIGENTES"
    )
    log(
        "===================================="
    )

    await goto_with_retry(
        page,
        GENERAL_VIG_URL,
        "total-general",
    )

    await wait_for_search_content(
        page
    )

    total = await get_result_count(
        page
    )

    if total <= 0:
        raise RuntimeError(
            "El portal general informa "
            "cero llamados vigentes."
        )

    log(
        "Total general informado por ARCE:",
        total,
    )

    return total


# ============================================================
# EXTRAER UNA VENTANA
# ============================================================

async def extract_window(
    page,
    start_date,
    end_date,
):
    first_url = build_window_url(
        start_date,
        end_date,
        1,
    )

    log("")
    log(
        "------------------------------------"
    )

    log(
        "Ventana:",
        start_date.strftime(
            "%Y-%m-%d"
        ),
        "a",
        end_date.strftime(
            "%Y-%m-%d"
        ),
    )

    await goto_with_retry(
        page,
        first_url,
        "ventana-inicial",
    )

    await wait_for_search_content(
        page
    )

    window_total = (
        await get_result_count(
            page
        )
    )

    log(
        "Resultados informados:",
        window_total,
    )

    if window_total == 0:
        return {}

    window_links = {}

    previous_ids = None

    page_number = 1

    while True:

        if (
            page_number
            > MAX_PAGES_PER_WINDOW
        ):

            raise RuntimeError(
                "La ventana excedio "
                "MAX_PAGES_PER_WINDOW."
            )

        url = build_window_url(
            start_date,
            end_date,
            page_number,
        )

        await goto_with_retry(
            page,
            url,
            (
                f"ventana-"
                f"{start_date:%Y%m%d}-"
                f"pagina-{page_number}"
            ),
        )

        await wait_for_search_content(
            page
        )

        await wait_for_results_stable(
            page
        )

        current_links = (
            await collect_detail_links_from_page(
                page
            )
        )

        current_ids = set(
            current_links.keys()
        )

        if not current_ids:

            raise RuntimeError(
                "La pagina no contiene "
                "IDs interpretables pese "
                "a que el contador de "
                "la ventana es mayor que cero."
            )

        if (
            previous_ids is not None
            and
            current_ids
            == previous_ids
        ):

            raise RuntimeError(
                "La paginacion de una ventana "
                "devolvio los mismos IDs "
                "que la pagina anterior."
            )

        before = len(
            window_links
        )

        window_links.update(
            current_links
        )

        after = len(
            window_links
        )

        nuevos = (
            after - before
        )

        log(
            f"  pagina {page_number}: "
            f"{len(current_ids)} IDs | "
            f"acumulados ventana "
            f"{after}/{window_total}"
        )

        if after >= window_total:
            break

        if nuevos == 0:
            raise RuntimeError(
                "La pagina no agrego "
                "resultados nuevos."
            )

        previous_ids = (
            current_ids
        )

        page_number += 1

        await asyncio.sleep(
            PAGE_PAUSE_SECONDS
        )

    if (
        len(window_links)
        != window_total
    ):

        raise RuntimeError(
            "La ventana no coincide "
            "con su propio contador: "
            f"{len(window_links)} "
            f"!= {window_total}"
        )

    return window_links


# ============================================================
# EXTRAER TODAS LAS VENTANAS
# ============================================================

async def collect_all_vig_links(
    page,
    portal_total,
):
    today_date = (
        datetime.now().date()
    )

    today = datetime(
        today_date.year,
        today_date.month,
        today_date.day,
    )

    all_links = {}

    windows_processed = 0
    blocks_processed = []

    for (
        block_start,
        block_end,
    ) in HISTORY_BLOCKS:

        effective_end = (
            today
            if block_end is None
            else min(
                block_end,
                today,
            )
        )

        if block_start > today:
            continue

        log("")
        log(
            "===================================="
        )
        log(
            "BLOQUE HISTORICO:",
            block_start.strftime(
                "%Y-%m-%d"
            ),
            "a",
            effective_end.strftime(
                "%Y-%m-%d"
            ),
        )
        log(
            "===================================="
        )

        block_windows = 0

        for (
            window_start,
            window_end,
        ) in generate_windows(
            block_start,
            effective_end,
        ):

            links = await extract_window(
                page,
                window_start,
                window_end,
            )

            before = len(
                all_links
            )

            all_links.update(
                links
            )

            after = len(
                all_links
            )

            windows_processed += 1
            block_windows += 1

            log(
                "Global:",
                after,
                "/",
                portal_total,
                "| nuevos:",
                after - before,
            )

            # Ya encontramos todo el universo
            # que informa la consulta general.
            if after == portal_total:

                blocks_processed.append(
                    {
                        "start":
                            block_start.strftime(
                                "%Y-%m-%d"
                            ),

                        "end":
                            effective_end.strftime(
                                "%Y-%m-%d"
                            ),

                        "windows":
                            block_windows,
                    }
                )

                return (
                    all_links,
                    windows_processed,
                    blocks_processed,
                )

            if after > portal_total:

                raise RuntimeError(
                    "La union de ventanas "
                    "supera el total general "
                    f"del portal: "
                    f"{after} > {portal_total}"
                )

        blocks_processed.append(
            {
                "start":
                    block_start.strftime(
                        "%Y-%m-%d"
                    ),

                "end":
                    effective_end.strftime(
                        "%Y-%m-%d"
                    ),

                "windows":
                    block_windows,
            }
        )

        log("")
        log(
            "Fin del bloque. IDs globales:",
            len(all_links),
            "/",
            portal_total,
        )

    return (
        all_links,
        windows_processed,
        blocks_processed,
    )


# ============================================================
# DETALLE DE CADA LLAMADO
# ============================================================

async def extract_detail(
    page,
    seed,
):
    url = seed[
        "detail_url"
    ]

    await goto_with_retry(
        page,
        url,
        f"detalle-{seed['detail_id']}",
    )

    await page.wait_for_selector(
        "body",
        timeout=CONTENT_TIMEOUT,
    )

    body_text = (
        await page.locator(
            "body"
        ).inner_text()
    )

    body_text = body_text.replace(
        "\xa0",
        " ",
    )

    lines = [
        clean_text(line)
        for line
        in body_text.splitlines()
        if clean_text(line)
    ]

    # --------------------------------------------------------
    # TITULO
    # --------------------------------------------------------

    title = ""

    for selector in (
        "h1",
        "h2",
        "h3",
    ):

        locator = page.locator(
            selector
        )

        if await locator.count():

            candidate = clean_text(
                await locator.first.inner_text()
            )

            if candidate:
                title = candidate
                break

    if not title:

        title = seed[
            "titulo_listado"
        ]

    # --------------------------------------------------------
    # OBJETO
    # --------------------------------------------------------

    objeto = extract_regex(
        body_text,
        r"(?:Objeto|Descripci[oó]n)"
        r"\s*:?\s*(.+?)"
        r"(?=\n[A-ZÁÉÍÓÚÑ]"
        r"[^\n]{0,80}:|\Z)",
    )

    # --------------------------------------------------------
    # FECHAS
    # --------------------------------------------------------

    cierre = extract_regex(
        body_text,
        r"Recepci[oó]n de ofertas hasta:"
        r"\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"(?:\s+[0-9]{2}:[0-9]{2})?)",
    )

    publicacion = extract_regex(
        body_text,
        r"Fecha Publicaci[oó]n:"
        r"\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"(?:\s+[0-9]{2}:[0-9]{2})?)",
    )

    if not publicacion:

        publicacion = extract_regex(
            body_text,
            r"Publicado:"
            r"\s*"
            r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
            r"(?:\s+[0-9]{2}:[0-9]{2})?)",
        )

    ultima_modificacion = (
        extract_regex(
            body_text,
            r"[UÚ]ltima Modificaci[oó]n:"
            r"\s*"
            r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
            r"(?:\s+[0-9]{2}:[0-9]{2})?)",
        )
    )

    acto_apertura = extract_regex(
        body_text,
        r"Acto de Apertura:"
        r"\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"(?:\s+[0-9]{2}:[0-9]{2})?)",
    )

    # --------------------------------------------------------
    # ITEMS
    # --------------------------------------------------------

    items = []

    item_pattern = re.compile(
        r"(?:Ítem|Item)"
        r"\s*N?[º°]?\s*(\d+)\s+(.+?)"
        r"(?:\s+\(C[oó]d\."
        r"\s*Art[ií]culo\s*([^)]+)\))?$",
        re.I,
    )

    for line in lines:

        if not line.lower().startswith(
            ("ítem", "item")
        ):
            continue

        match = item_pattern.search(
            line
        )

        if match:

            items.append(
                {
                    "numero":
                        clean_text(
                            match.group(1)
                        ),

                    "descripcion":
                        clean_text(
                            match.group(2)
                        ),

                    "codigo_articulo":
                        clean_text(
                            match.group(3)
                            or ""
                        ),
                }
            )

    # --------------------------------------------------------
    # ADJUNTOS
    # --------------------------------------------------------

    attachments = []

    anchors = page.locator(
        "a[href]"
    )

    anchor_count = (
        await anchors.count()
    )

    for i in range(
        anchor_count
    ):

        anchor = anchors.nth(
            i
        )

        href = await anchor.get_attribute(
            "href"
        )

        if not href:
            continue

        text = clean_text(
            await anchor.inner_text()
        )

        full_url = absolute_url(
            href
        )

        lower_href = href.lower()

        if (
            ".pdf" in lower_href
            or
            ".doc" in lower_href
            or
            ".docx" in lower_href
            or
            ".xls" in lower_href
            or
            ".xlsx" in lower_href
            or
            ".zip" in lower_href
            or
            "/pliegos/" in lower_href
            or
            "/aclaraciones/" in lower_href
        ):

            attachments.append(
                {
                    "texto":
                        text,

                    "url":
                        full_url,
                }
            )

    unique_attachments = []
    seen = set()

    for attachment in attachments:

        if attachment["url"] in seen:
            continue

        seen.add(
            attachment["url"]
        )

        unique_attachments.append(
            attachment
        )

    # --------------------------------------------------------
    # SEMANTIC TEXT
    # --------------------------------------------------------

    semantic_parts = [
        title,
        objeto,
    ]

    semantic_parts.extend(
        item["descripcion"]
        for item in items
        if item.get("descripcion")
    )

    semantic_text = clean_text(
        " | ".join(
            x
            for x in semantic_parts
            if x
        )
    )

    return {
        "id":
            seed["detail_id"],

        "titulo":
            title,

        "objeto":
            objeto,

        "fecha_publicacion":
            publicacion,

        "ultima_modificacion":
            ultima_modificacion,

        "recepcion_ofertas_hasta":
            cierre,

        "acto_apertura":
            acto_apertura,

        "items":
            items,

        "adjuntos":
            unique_attachments,

        "semantic_text":
            semantic_text,

        "url_oficial":
            url,
    }


async def extract_all_details(
    context,
    detail_links,
):
    log("")
    log(
        "===================================="
    )
    log(
        "DETALLE DE TODOS LOS LLAMADOS"
    )
    log(
        "===================================="
    )

    page = await context.new_page()

    records = []
    failures = []

    seeds = list(
        detail_links.values()
    )

    total = len(seeds)

    for index, seed in enumerate(
        seeds,
        start=1,
    ):

        log(
            f"Detalle {index}/{total} "
            f"- {seed['detail_id']}"
        )

        try:

            record = await extract_detail(
                page,
                seed,
            )

            records.append(
                record
            )

        except Exception as exc:

            failures.append(
                {
                    "id":
                        seed[
                            "detail_id"
                        ],

                    "url":
                        seed[
                            "detail_url"
                        ],

                    "error":
                        str(exc),
                }
            )

            log_error(
                "ERROR detalle",
                seed["detail_id"],
                ":",
                exc,
            )

        await asyncio.sleep(
            DETAIL_PAUSE_SECONDS
        )

    await page.close()

    return (
        records,
        failures,
    )


# ============================================================
# ARCHIVOS
# ============================================================

def cleanup_temp():

    for path in (
        TEMP_JSON,
        TEMP_CSV,
        TEMP_METADATA,
    ):

        try:

            if path.exists():
                path.unlink()

        except Exception:
            pass


def write_temp_files(
    records,
    portal_total,
    windows_processed,
    blocks_processed,
    failures,
    total_start,
    total_end,
):
    timestamp = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    unique_ids = {
        record["id"]
        for record in records
    }

    detail_unique = len(
        unique_ids
    )

    file_integrity = (
        detail_unique
        == len(records)
    )

    portal_total_stable = (
        total_start
        == total_end
    )

    coverage_complete = (
        portal_total_stable
        and
        len(failures) == 0
        and
        detail_unique == total_end
        and
        file_integrity
    )

    payload = {
        "generated_at":
            timestamp,

        "source":
            "Compras Estatales Uruguay / ARCE",

        "source_url":
            f"{BASE_URL}/consultas/",

        "publication_type":
            "Llamados vigentes",

        "total_records":
            len(records),

        "data":
            records,
    }

    TEMP_JSON.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    columns = [
        "id",
        "titulo",
        "objeto",
        "fecha_publicacion",
        "ultima_modificacion",
        "recepcion_ofertas_hasta",
        "acto_apertura",
        "items",
        "adjuntos",
        "semantic_text",
        "url_oficial",
    ]

    with TEMP_CSV.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=columns,
        )

        writer.writeheader()

        for record in records:

            row = record.copy()

            row["items"] = json.dumps(
                row["items"],
                ensure_ascii=False,
            )

            row["adjuntos"] = (
                json.dumps(
                    row["adjuntos"],
                    ensure_ascii=False,
                )
            )

            writer.writerow(
                row
            )

    metadata = {
        "extraction_timestamp":
            timestamp,

        "source":
            "Compras Estatales Uruguay / ARCE",

        "source_url":
            f"{BASE_URL}/consultas/",

        "extraction_method":
            (
                "PLAYWRIGHT_DATE_WINDOWS"
            ),

        "publication_type":
            "VIG",

        "date_filter_type":
            "MOD",

        "window_days":
            WINDOW_DAYS,

        "windows_processed":
            windows_processed,

        "history_blocks":
            blocks_processed,

        "portal_total_start":
            total_start,

        "portal_total_end":
            total_end,

        "portal_total":
            total_end,

        "portal_total_stable":
            portal_total_stable,

        "list_unique_records":
            detail_unique,

        "detail_unique_records":
            detail_unique,

        "json_records":
            len(records),

        "csv_records":
            len(records),

        "detail_failures":
            len(failures),

        "failed_details":
            failures,

        "file_integrity":
            file_integrity,

        "portal_reconciliation":
            (
                detail_unique
                == total_end
            ),

        "content_quality_complete":
            (
                len(records)
                > 0
                and
                all(
                    record.get("titulo")
                    and
                    record.get(
                        "url_oficial"
                    )
                    for record
                    in records
                )
            ),

        "coverage_complete":
            coverage_complete,

        "coverage_basis":
            (
                "PORTAL_TOTAL_PLUS_DATE_WINDOWS"
                "_PLUS_DETAIL_VALIDATION"
            ),

        "validation_status":
            (
                "OK"
                if coverage_complete
                else "ERROR"
            ),

        "note":
            (
                "Los llamados vigentes se "
                "recuperan por ventanas de "
                "fecha para evitar limites "
                "de paginacion del portal. "
                "Los IDs se deduplican y la "
                "union final se reconcilia "
                "contra el contador general "
                "de llamados vigentes."
            ),
    }

    TEMP_METADATA.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return metadata


def publish_snapshot():

    os.replace(
        TEMP_JSON,
        OUTPUT_JSON,
    )

    os.replace(
        TEMP_CSV,
        OUTPUT_CSV,
    )

    os.replace(
        TEMP_METADATA,
        OUTPUT_METADATA,
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    cleanup_temp()

    browser = None

    try:

        async with async_playwright() as p:

            browser = (
                await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
            )

            context = (
                await browser.new_context(
                    locale="es-UY",

                    timezone_id=
                        "America/Montevideo",

                    viewport={
                        "width": 1440,
                        "height": 1000,
                    },

                    user_agent=(
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; "
                        "Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/130.0.0.0 "
                        "Safari/537.36"
                    ),
                )
            )

            page = await context.new_page()

            # -----------------------------------------------
            # TOTAL ANTES DE EMPEZAR
            # -----------------------------------------------

            total_start = (
                await get_general_portal_total(
                    page
                )
            )

            # -----------------------------------------------
            # EXTRAER POR VENTANAS
            # -----------------------------------------------

            (
                detail_links,
                windows_processed,
                blocks_processed,
            ) = await collect_all_vig_links(
                page,
                total_start,
            )

            log("")
            log(
                "===================================="
            )
            log(
                "RECONCILIACION PRE-DETALLE"
            )
            log(
                "===================================="
            )

            log(
                "Portal inicial:",
                total_start,
            )

            log(
                "IDs recuperados:",
                len(detail_links),
            )

            if (
                len(detail_links)
                != total_start
            ):

                raise RuntimeError(
                    "Las ventanas no recuperaron "
                    "todo el universo de vigentes: "
                    f"{len(detail_links)} "
                    f"!= {total_start}"
                )

            # -----------------------------------------------
            # DETALLES
            # -----------------------------------------------

            (
                records,
                failures,
            ) = await extract_all_details(
                context,
                detail_links,
            )

            # -----------------------------------------------
            # TOTAL AL FINAL
            # -----------------------------------------------

            total_end = (
                await get_general_portal_total(
                    page
                )
            )

            log("")
            log(
                "Portal al inicio:",
                total_start,
            )

            log(
                "Portal al final:",
                total_end,
            )

            if total_start != total_end:

                raise RuntimeError(
                    "El total de vigentes cambio "
                    "durante la extraccion. "
                    "Se aborta para evitar "
                    "publicar un snapshot "
                    "inconsistente."
                )

            # -----------------------------------------------
            # ARCHIVOS TEMPORALES
            # -----------------------------------------------

            metadata = write_temp_files(
                records,
                total_end,
                windows_processed,
                blocks_processed,
                failures,
                total_start,
                total_end,
            )

            log("")
            log(
                "===================================="
            )
            log(
                "VALIDACION FINAL"
            )
            log(
                "===================================="
            )

            log(
                "Portal:",
                metadata[
                    "portal_total"
                ],
            )

            log(
                "JSON:",
                metadata[
                    "json_records"
                ],
            )

            log(
                "CSV:",
                metadata[
                    "csv_records"
                ],
            )

            log(
                "Fallos detalle:",
                metadata[
                    "detail_failures"
                ],
            )

            log(
                "Coverage complete:",
                metadata[
                    "coverage_complete"
                ],
            )

            if not metadata[
                "coverage_complete"
            ]:

                raise RuntimeError(
                    "La validacion final "
                    "no fue satisfactoria."
                )

            # -----------------------------------------------
            # PUBLICAR
            # -----------------------------------------------

            publish_snapshot()

            log("")
            log(
                "===================================="
            )
            log(
                "SNAPSHOT PUBLICADO OK"
            )
            log(
                "===================================="
            )

            log(
                f"{total_end}/{total_end} "
                "llamados vigentes."
            )

            await browser.close()

    except Exception as exc:

        cleanup_temp()

        log_error("")
        log_error(
            "===================================="
        )
        log_error(
            "EXTRACCION ABORTADA"
        )
        log_error(
            "===================================="
        )

        log_error(
            str(exc)
        )

        log_error("")
        log_error(
            "El ultimo snapshot valido "
            "queda intacto."
        )

        if browser is not None:

            try:
                await browser.close()

            except Exception:
                pass

        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
