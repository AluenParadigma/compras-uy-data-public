import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

from playwright.async_api import async_playwright


# ============================================================
# CONFIGURACION
# ============================================================

BASE_URL = "https://www.comprasestatales.gub.uy"

START_URL = (
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

PAGE_PAUSE_SECONDS = 0.40
DETAIL_PAUSE_SECONDS = 0.25

MAX_ATTEMPTS = 2

# Reintentos extra para una página que devuelve
# los mismos IDs que la página anterior.
DUPLICATE_PAGE_RETRIES = 3
DUPLICATE_PAGE_WAIT_SECONDS = 4

MAX_SEARCH_PAGES = 200


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

    return re.sub(r"\s+", " ", value).strip()


def absolute_url(href):
    return urljoin(BASE_URL, href)


def extract_detail_id(url):
    """
    Acepta IDs numericos y alfanumericos.
    """

    if not url:
        return None

    patterns = [
        r"/id/([^/?#]+)",
        r"[?&]id=([^&#]+)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            url,
            re.I,
        )

        if match:
            return match.group(1)

    return None


def parse_datetime(text):
    if not text:
        return ""

    return clean_text(
        text.replace("hs", "")
    )


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


def build_page_url(page_number):
    if page_number <= 1:
        return START_URL

    return (
        f"{BASE_URL}/consultas/buscar/"
        f"page/{page_number}/"
        "reset/1/tipo-pub/VIG"
    )


def add_cache_buster(url):
    """
    Agrega un parametro timestamp para evitar respuestas cacheadas.
    """

    parsed = urlparse(url)

    query = dict(
        parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
    )

    query["_ts"] = str(
        int(time.time() * 1000)
    )

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query),
            parsed.fragment,
        )
    )


# ============================================================
# NAVEGACION
# ============================================================

async def goto_with_retry(page, url, label):
    last_error = None

    for attempt in range(
        1,
        MAX_ATTEMPTS + 1,
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
                f"{attempt}/{MAX_ATTEMPTS} "
                f"fallo: {exc}"
            )

            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(
                    3 * attempt
                )

    raise RuntimeError(
        f"{label}: fallo luego de "
        f"{MAX_ATTEMPTS} intentos. "
        f"Ultimo error: {last_error}"
    )


async def wait_for_search_content(page):
    await page.wait_for_function(
        """
        () => {
            const text = document.body
                ? document.body.innerText
                : '';

            return (
                text.includes('Se encontraron') ||
                text.includes('Llamados vigentes')
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

    for _ in range(20):
        count = await page.locator(
            selector
        ).count()

        log(
            "  enlaces de llamados visibles:",
            count,
        )

        if count > 0 and count == last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 3:
            return count

        last_count = count

        await page.wait_for_timeout(
            1000
        )

    if last_count <= 0:
        raise RuntimeError(
            "El listado no termino de cargar "
            "ningun llamado."
        )

    return last_count


# ============================================================
# HEALTH CHECK
# ============================================================

async def health_check(page):
    log("")
    log("====================================")
    log("HEALTH CHECK NUEVA WEB ARCE")
    log("====================================")

    log(
        "Probando:",
        START_URL,
    )

    await goto_with_retry(
        page,
        START_URL,
        "health-check",
    )

    await wait_for_search_content(
        page
    )

    await wait_for_results_stable(
        page
    )

    final_url = page.url

    log(
        "URL final:",
        final_url,
    )

    if "/consultas/rss/" in final_url:
        raise RuntimeError(
            "ARCE redirigio el health check "
            "a RSS: "
            f"{final_url}"
        )

    if "/consultas/buscar/" not in final_url:
        raise RuntimeError(
            "La URL final no corresponde "
            "a /consultas/buscar/: "
            f"{final_url}"
        )

    if "/tipo-pub/VIG" not in final_url:
        raise RuntimeError(
            "La URL final no corresponde "
            "a llamados vigentes: "
            f"{final_url}"
        )

    log(
        "Nueva interfaz ARCE accesible."
    )

    return final_url


# ============================================================
# TOTAL DEL PORTAL
# ============================================================

async def get_portal_total(page):
    body = clean_text(
        await page.locator(
            "body"
        ).inner_text()
    )

    match = re.search(
        r"Se encontraron\s+([\d\.]+)\s+resultados",
        body,
        re.I,
    )

    if not match:
        raise RuntimeError(
            "No se pudo encontrar el contador "
            "'Se encontraron X resultados'."
        )

    return int(
        match.group(1).replace(".", "")
    )


# ============================================================
# LINKS DE DETALLE
# ============================================================

async def collect_detail_links_from_page(page):
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
# LEER PAGINA CON CONTROL ANTI-CACHE
# ============================================================

async def load_page_ids(
    page,
    page_number,
    page_url,
):
    await goto_with_retry(
        page,
        page_url,
        f"pagina-{page_number}",
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

    return (
        current_links,
        current_ids,
    )


async def load_page_with_duplicate_retry(
    page,
    page_number,
    page_url,
    previous_page_ids,
):
    """
    Si ARCE devuelve los mismos IDs que la pagina anterior,
    reintenta con cache-busting antes de abortar.
    """

    last_links = {}
    last_ids = set()

    for attempt in range(
        1,
        DUPLICATE_PAGE_RETRIES + 1,
    ):

        if attempt == 1:
            url = page_url
        else:
            url = add_cache_buster(
                page_url
            )

        log(
            f"  carga pagina {page_number} "
            f"intento {attempt}/"
            f"{DUPLICATE_PAGE_RETRIES}"
        )

        current_links, current_ids = (
            await load_page_ids(
                page,
                page_number,
                url,
            )
        )

        last_links = current_links
        last_ids = current_ids

        if not current_ids:
            log(
                "  pagina sin IDs, se reintentara."
            )

        elif (
            previous_page_ids is None
            or
            current_ids != previous_page_ids
        ):
            return (
                current_links,
                current_ids,
            )

        log(
            "  ARCE devolvio los mismos IDs "
            "que la pagina anterior."
        )

        if (
            attempt
            < DUPLICATE_PAGE_RETRIES
        ):
            log(
                f"  esperando "
                f"{DUPLICATE_PAGE_WAIT_SECONDS}s "
                "y reintentando sin cache..."
            )

            await asyncio.sleep(
                DUPLICATE_PAGE_WAIT_SECONDS
            )

    raise RuntimeError(
        f"La pagina {page_number} devolvio "
        "los mismos IDs que la pagina anterior "
        f"en {DUPLICATE_PAGE_RETRIES} intentos. "
        "Posible cache/inconsistencia temporal de ARCE."
    )


# ============================================================
# CRAWL DE TODAS LAS PAGINAS
# ============================================================

async def crawl_all_search_pages(
    page,
    start_url,
):
    log("")
    log("====================================")
    log("PAGINACION DE LLAMADOS VIGENTES")
    log("====================================")

    await goto_with_retry(
        page,
        start_url,
        "pagina-inicial",
    )

    await wait_for_search_content(
        page
    )

    await wait_for_results_stable(
        page
    )

    portal_total = (
        await get_portal_total(
            page
        )
    )

    log(
        "Total informado por ARCE:",
        portal_total,
    )

    detail_links = {}

    previous_page_ids = None

    page_number = 1

    while True:

        if page_number > MAX_SEARCH_PAGES:
            raise RuntimeError(
                "Se alcanzo MAX_SEARCH_PAGES "
                "sin completar el total."
            )

        page_url = build_page_url(
            page_number
        )

        log("")
        log(
            f"Pagina de resultados "
            f"{page_number}: "
            f"{page_url}"
        )

        (
            current_links,
            current_ids,
        ) = await load_page_with_duplicate_retry(
            page,
            page_number,
            page_url,
            previous_page_ids,
        )

        log(
            "  IDs unicos en esta pagina:",
            len(current_ids),
        )

        if not current_ids:
            raise RuntimeError(
                f"La pagina {page_number} "
                "no contiene llamados interpretables."
            )

        before = len(
            detail_links
        )

        detail_links.update(
            current_links
        )

        after = len(
            detail_links
        )

        nuevos = (
            after - before
        )

        log(
            "  llamados nuevos:",
            nuevos,
            "| acumulados:",
            after,
            "/",
            portal_total,
        )

        if after == portal_total:
            log(
                "  total del portal alcanzado."
            )
            break

        if after > portal_total:
            raise RuntimeError(
                "Se encontraron mas llamados "
                "que el total informado: "
                f"{after} > {portal_total}"
            )

        if nuevos == 0:
            raise RuntimeError(
                f"La pagina {page_number} "
                "no agrego ningun llamado nuevo."
            )

        previous_page_ids = (
            current_ids
        )

        page_number += 1

        await asyncio.sleep(
            PAGE_PAUSE_SECONDS
        )

    return (
        portal_total,
        detail_links,
        page_number,
    )


# ============================================================
# EXTRAER DETALLE
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

    clean_body = clean_text(
        body_text
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

        if await locator.count() > 0:
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
        r"(?:Objeto|Descripci[oó]n)\s*:?\s*(.+?)"
        r"(?=\n[A-ZÁÉÍÓÚÑ][^\n]{0,80}:|\Z)",
    )

    if not objeto:
        try:
            index = lines.index(
                title
            )

            for line in lines[
                index + 1:
                index + 12
            ]:
                lower = line.lower()

                ignored = [
                    "recepción de ofertas",
                    "recepcion de ofertas",
                    "fecha publicación",
                    "fecha publicacion",
                    "acto de apertura",
                    "última modificación",
                    "ultima modificacion",
                ]

                if any(
                    value in lower
                    for value in ignored
                ):
                    continue

                objeto = line
                break

        except ValueError:
            pass

    # --------------------------------------------------------
    # FECHAS
    # --------------------------------------------------------

    cierre = extract_regex(
        body_text,
        r"Recepci[oó]n de ofertas hasta:\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"(?:\s+[0-9]{2}:[0-9]{2})?)",
    )

    publicacion = extract_regex(
        body_text,
        r"Fecha Publicaci[oó]n:\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"(?:\s+[0-9]{2}:[0-9]{2})?)",
    )

    if not publicacion:
        publicacion = extract_regex(
            body_text,
            r"Publicado:\s*"
            r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
            r"(?:\s+[0-9]{2}:[0-9]{2})?)",
        )

    ultima_modificacion = extract_regex(
        body_text,
        r"[UÚ]ltima Modificaci[oó]n:\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"(?:\s+[0-9]{2}:[0-9]{2})?)",
    )

    acto_apertura = extract_regex(
        body_text,
        r"Acto de Apertura:\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"(?:\s+[0-9]{2}:[0-9]{2})?)",
    )

    # --------------------------------------------------------
    # ITEMS
    # --------------------------------------------------------

    items = []

    item_pattern = re.compile(
        r"(?:Ítem|Item)\s*N?[º°]?\s*(\d+)\s+(.+?)"
        r"(?:\s+\(C[oó]d\.\s*Art[ií]culo\s*([^)]+)\))?$",
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

        href_lower = href.lower()

        if (
            ".pdf" in href_lower
            or
            ".doc" in href_lower
            or
            ".docx" in href_lower
            or
            ".xls" in href_lower
            or
            ".xlsx" in href_lower
            or
            ".zip" in href_lower
            or
            "/pliegos/" in href_lower
            or
            "/aclaraciones/" in href_lower
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
    seen_attachments = set()

    for attachment in attachments:
        key = attachment[
            "url"
        ]

        if key in seen_attachments:
            continue

        seen_attachments.add(
            key
        )

        unique_attachments.append(
            attachment
        )

    # --------------------------------------------------------
    # APERTURA ELECTRONICA
    # --------------------------------------------------------

    apertura_electronica = (
        "apertura electrónica"
        in clean_body.lower()
        or
        "apertura electronica"
        in clean_body.lower()
    )

    # --------------------------------------------------------
    # TEXTO SEMANTICO
    # --------------------------------------------------------

    semantic_parts = [
        title,
        objeto,
    ]

    semantic_parts.extend(
        item["descripcion"]
        for item in items
        if item.get(
            "descripcion"
        )
    )

    semantic_text = clean_text(
        " | ".join(
            value
            for value
            in semantic_parts
            if value
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
            parse_datetime(
                publicacion
            ),

        "ultima_modificacion":
            parse_datetime(
                ultima_modificacion
            ),

        "recepcion_ofertas_hasta":
            parse_datetime(
                cierre
            ),

        "acto_apertura":
            parse_datetime(
                acto_apertura
            ),

        "apertura_electronica":
            apertura_electronica,

        "items":
            items,

        "adjuntos":
            unique_attachments,

        "semantic_text":
            semantic_text,

        "url_oficial":
            url,
    }


# ============================================================
# EXTRAER TODOS LOS DETALLES
# ============================================================

async def extract_all_details(
    context,
    detail_links,
):
    log("")
    log("====================================")
    log("DETALLE DE CADA LLAMADO")
    log("====================================")

    page = await context.new_page()

    records = []
    failures = []

    seeds = list(
        detail_links.values()
    )

    total = len(
        seeds
    )

    for index, seed in enumerate(
        seeds,
        start=1,
    ):

        log(
            f"Detalle {index}/{total} "
            f"- ID {seed['detail_id']}"
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
            log_error(
                f"ERROR detalle "
                f"{seed['detail_id']}: "
                f"{exc}"
            )

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

        await asyncio.sleep(
            DETAIL_PAUSE_SECONDS
        )

    await page.close()

    return (
        records,
        failures,
    )


# ============================================================
# VALIDACION
# ============================================================

def validate_records(
    portal_total,
    detail_links,
    records,
    failures,
):
    unique_ids = {
        record["id"]
        for record
        in records
    }

    list_unique = len(
        detail_links
    )

    detail_unique = len(
        unique_ids
    )

    list_matches_portal = (
        list_unique
        == portal_total
    )

    details_match_portal = (
        detail_unique
        == portal_total
    )

    no_detail_failures = (
        len(failures)
        == 0
    )

    all_have_title = all(
        bool(
            record.get(
                "titulo"
            )
        )
        for record
        in records
    )

    all_have_url = all(
        bool(
            record.get(
                "url_oficial"
            )
        )
        for record
        in records
    )

    content_records = sum(
        1
        for record
        in records
        if (
            record.get(
                "titulo"
            )
            or
            record.get(
                "objeto"
            )
            or
            record.get(
                "items"
            )
        )
    )

    content_quality_complete = (
        content_records
        == detail_unique
        and
        detail_unique > 0
    )

    coverage_complete = all(
        [
            portal_total > 0,
            list_matches_portal,
            details_match_portal,
            no_detail_failures,
            all_have_title,
            all_have_url,
            content_quality_complete,
        ]
    )

    return {
        "portal_total":
            portal_total,

        "list_unique_records":
            list_unique,

        "detail_unique_records":
            detail_unique,

        "detail_failures":
            len(failures),

        "list_matches_portal":
            list_matches_portal,

        "details_match_portal":
            details_match_portal,

        "content_records":
            content_records,

        "content_quality_complete":
            content_quality_complete,

        "coverage_complete":
            coverage_complete,
    }


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
    validation,
    search_pages_count,
    failures,
):
    timestamp = datetime.now(
        timezone.utc
    ).isoformat()

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

    csv_columns = [
        "id",
        "titulo",
        "objeto",
        "fecha_publicacion",
        "ultima_modificacion",
        "recepcion_ofertas_hasta",
        "acto_apertura",
        "apertura_electronica",
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
            fieldnames=csv_columns,
        )

        writer.writeheader()

        for record in records:
            row = record.copy()

            row["items"] = json.dumps(
                row["items"],
                ensure_ascii=False,
            )

            row["adjuntos"] = json.dumps(
                row["adjuntos"],
                ensure_ascii=False,
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
            "PLAYWRIGHT_PORTAL_HTML",

        "publication_type":
            "VIG",

        "portal_total":
            validation[
                "portal_total"
            ],

        "search_pages_processed":
            search_pages_count,

        "list_unique_records":
            validation[
                "list_unique_records"
            ],

        "detail_unique_records":
            validation[
                "detail_unique_records"
            ],

        "json_records":
            len(records),

        "csv_records":
            len(records),

        "detail_failures":
            validation[
                "detail_failures"
            ],

        "list_matches_portal":
            validation[
                "list_matches_portal"
            ],

        "details_match_portal":
            validation[
                "details_match_portal"
            ],

        "portal_reconciliation":
            (
                validation[
                    "list_matches_portal"
                ]
                and
                validation[
                    "details_match_portal"
                ]
            ),

        "file_integrity":
            (
                len(records)
                ==
                validation[
                    "detail_unique_records"
                ]
            ),

        "content_records":
            validation[
                "content_records"
            ],

        "content_quality_complete":
            validation[
                "content_quality_complete"
            ],

        "coverage_complete":
            validation[
                "coverage_complete"
            ],

        "coverage_basis":
            (
                "PORTAL_HTML_DIRECT_PAGE"
                "_PAGINATION_PLUS_DETAIL_VALIDATION"
            ),

        "validation_status":
            (
                "OK"
                if validation[
                    "coverage_complete"
                ]
                else "ERROR"
            ),

        "failed_details":
            failures,

        "note":
            (
                "Cobertura validada contra "
                "el contador visible del portal "
                "ARCE. Las paginas se recorren "
                "con reintentos anti-cache ante "
                "duplicados temporales."
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


# ============================================================
# PUBLICACION
# ============================================================

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

            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features="
                    "AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            context = await browser.new_context(
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

            page = await context.new_page()

            start_url = await health_check(
                page
            )

            (
                portal_total,
                detail_links,
                search_pages_count,
            ) = await crawl_all_search_pages(
                page,
                start_url,
            )

            log("")
            log(
                "Portal informa:",
                portal_total,
            )

            log(
                "IDs unicos encontrados:",
                len(
                    detail_links
                ),
            )

            if (
                len(detail_links)
                != portal_total
            ):
                raise RuntimeError(
                    "La paginacion no coincide "
                    "con el contador del portal: "
                    f"{len(detail_links)} "
                    f"!= {portal_total}"
                )

            (
                records,
                failures,
            ) = await extract_all_details(
                context,
                detail_links,
            )

            validation = validate_records(
                portal_total,
                detail_links,
                records,
                failures,
            )

            metadata = write_temp_files(
                records,
                validation,
                search_pages_count,
                failures,
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
                "Portal total:",
                metadata[
                    "portal_total"
                ],
            )

            log(
                "Listado:",
                metadata[
                    "list_unique_records"
                ],
            )

            log(
                "Detalles:",
                metadata[
                    "detail_unique_records"
                ],
            )

            log(
                "Fallos detalle:",
                metadata[
                    "detail_failures"
                ],
            )

            log(
                "Contenido valido:",
                metadata[
                    "content_records"
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
                    "de cobertura fallo."
                )

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
                f"{metadata['portal_total']}/"
                f"{metadata['portal_total']} "
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
