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

PORTAL_HOME_URL = f"{BASE_URL}/consultas/"

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


# ============================================================
# TIMEOUTS / REINTENTOS
# ============================================================

NAV_TIMEOUT = 90000
CONTENT_TIMEOUT = 60000

MAX_HTTP_ATTEMPTS = 4
RETRY_BASE_WAIT_SECONDS = 5


# ============================================================
# EXTRACCION
# ============================================================

PAGE_PAUSE_SECONDS = 0.20
DETAIL_PAUSE_SECONDS = 0.15

WINDOW_DAYS = 14

MAX_RESULTS_PER_WINDOW = 800
MAX_PAGES_PER_WINDOW = 100

HISTORY_START = datetime(2010, 1, 1)


# ============================================================
# CATCH-UP
# ============================================================

CATCHUP_DAYS = 45
MAX_CATCHUP_ROUNDS = 3


# ============================================================
# ESTABILIZACION FINAL
# ============================================================

# Dos lecturas consecutivas del mismo conjunto de IDs.
MAX_STABILITY_ROUNDS = 4

STABILITY_WAIT_SECONDS = 5

# Si el contador difiere del conjunto estable,
# no abortamos exclusivamente por eso.
# La diferencia queda registrada en metadata.
RECONCILIATION_WAIT_SECONDS = 3


# ============================================================
# LOG
# ============================================================

def log(*args):
    print(*args, flush=True)


def log_error(*args):
    print(
        *args,
        file=sys.stderr,
        flush=True,
    )


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
    Acepta IDs numericos y alfanumericos.

    Ejemplos:
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
# FECHAS / URLS
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
            "%2023%3A59%3A59"
        )

    return (
        f"{date_text}"
        "%2000%3A00%3A00"
    )


def build_window_url(
    start_date,
    end_date,
    page_number=1,
):
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
        f"page/{page_number}/"
        "tipo-pub/VIG/"
        "tipo-fecha/MOD/"
        "orden/ORD_MOD/"
        "tipo-orden/DESC/"
        "rango-fecha/"
        f"{start_encoded}_"
        f"{end_encoded}/"
        "reset/1"
    )


def generate_windows(
    start_date,
    end_date,
    window_days=WINDOW_DAYS,
):
    current = start_date

    while current <= end_date:

        window_end = min(
            current
            + timedelta(
                days=window_days - 1
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
# DETECCION DE CONTENIDO UTIL
# ============================================================

async def page_has_useful_content(page):

    try:

        body = page.locator(
            "body"
        )

        if await body.count() == 0:
            return False

        text = clean_text(
            await body.inner_text(
                timeout=5000,
            )
        )

        if not text:
            return False

        indicators = [
            "Se encontraron",
            "Llamados vigentes",
            "La búsqueda no produjo",
            "La busqueda no produjo",
            "Compras Estatales",
        ]

        return any(
            indicator in text
            for indicator in indicators
        )

    except Exception:
        return False


# ============================================================
# NAVEGACION ROBUSTA
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

            log(
                f"{label}: navegacion "
                f"{attempt}/"
                f"{MAX_HTTP_ATTEMPTS}"
            )

            response = await page.goto(
                url,
                wait_until="commit",
                timeout=NAV_TIMEOUT,
            )

            if response is None:

                if await page_has_useful_content(
                    page
                ):
                    return None

                raise RuntimeError(
                    "Navegacion sin respuesta HTTP"
                )

            status = response.status

            log(
                f"{label}: HTTP {status}"
            )

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

            try:

                await page.wait_for_selector(
                    "body",
                    timeout=CONTENT_TIMEOUT,
                )

            except Exception:

                if not await page_has_useful_content(
                    page
                ):
                    raise

            return response

        except Exception as exc:

            last_error = exc

            if await page_has_useful_content(
                page
            ):

                log(
                    f"{label}: contenido util "
                    "detectado pese al error "
                    "de navegacion."
                )

                return None

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

                wait_seconds = (
                    RETRY_BASE_WAIT_SECONDS
                    * attempt
                )

                log(
                    f"{label}: esperando "
                    f"{wait_seconds}s..."
                )

                await asyncio.sleep(
                    wait_seconds
                )

                try:

                    await page.goto(
                        "about:blank",
                        wait_until="commit",
                        timeout=10000,
                    )

                except Exception:
                    pass

    raise RuntimeError(
        f"{label}: fallo luego de "
        f"{MAX_HTTP_ATTEMPTS} intentos. "
        f"Ultimo error: {last_error}"
    )


# ============================================================
# WARM-UP
# ============================================================

async def warm_up_portal(page):

    log("")
    log(
        "===================================="
    )
    log(
        "WARM-UP PORTAL ARCE"
    )
    log(
        "===================================="
    )

    try:

        await goto_with_retry(
            page,
            PORTAL_HOME_URL,
            "warm-up",
        )

        await page.wait_for_timeout(
            1500
        )

        log(
            "Warm-up completado."
        )

    except Exception as exc:

        log(
            "AVISO: warm-up fallido:",
            exc,
        )


# ============================================================
# ESPERA
# ============================================================

async def wait_for_search_content(page):

    try:

        await page.wait_for_function(
            """
            () => {
                const text =
                    document.body
                    ? document.body.innerText
                    : '';

                return (
                    text.includes(
                        'Se encontraron'
                    ) ||
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

    except Exception:

        if not await page_has_useful_content(
            page
        ):
            raise


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

        if (
            count > 0
            and
            count == last_count
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
# CONTADORES
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


async def get_general_portal_total(page):

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

    return total


# ============================================================
# LINKS
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

        href = (
            await element.get_attribute(
                "href"
            )
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
# VENTANA SIMPLE
# ============================================================

async def extract_window_simple(
    page,
    start_date,
    end_date,
    expected_total,
):

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
                f"{end_date:%Y%m%d}-"
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
                "IDs interpretables."
            )

        if (
            previous_ids is not None
            and
            current_ids == previous_ids
        ):

            raise RuntimeError(
                "La paginacion devolvio "
                "los mismos IDs que "
                "la pagina anterior."
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
            f"{after}/{expected_total}"
        )

        if after == expected_total:
            break

        if after > expected_total:

            # No abortamos automáticamente.
            # El contador de la ventana también
            # puede quedar temporalmente atrasado.
            log(
                "AVISO: IDs de ventana "
                "superan contador:",
                after,
                ">",
                expected_total,
            )

            break

        if nuevos == 0:
            break

        previous_ids = (
            current_ids
        )

        page_number += 1

        await asyncio.sleep(
            PAGE_PAUSE_SECONDS
        )

    return window_links


# ============================================================
# VENTANA
# ============================================================

async def extract_window(
    page,
    start_date,
    end_date,
    general_total,
):

    first_url = build_window_url(
        start_date,
        end_date,
        1,
    )

    log("")
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

    number_of_days = (
        end_date.date()
        - start_date.date()
    ).days + 1

    if (
        number_of_days == 1
        and
        window_total >= general_total
    ):

        raise RuntimeError(
            "ARCE parece estar ignorando "
            "el filtro de fecha."
        )

    if (
        window_total
        > MAX_RESULTS_PER_WINDOW
        and
        start_date.date()
        != end_date.date()
    ):

        days = (
            end_date.date()
            - start_date.date()
        ).days

        midpoint = (
            start_date
            + timedelta(
                days=days // 2
            )
        )

        left_end = midpoint

        right_start = (
            midpoint
            + timedelta(days=1)
        )

        left_links = (
            await extract_window(
                page,
                start_date,
                left_end,
                general_total,
            )
        )

        right_links = (
            await extract_window(
                page,
                right_start,
                end_date,
                general_total,
            )
        )

        merged = {}

        merged.update(
            left_links
        )

        merged.update(
            right_links
        )

        return merged

    return await extract_window_simple(
        page,
        start_date,
        end_date,
        window_total,
    )


# ============================================================
# EXTRAER UNIVERSO COMPLETO DE IDS
# ============================================================

async def collect_full_universe(
    page,
    general_total,
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

    for (
        window_start,
        window_end,
    ) in generate_windows(
        HISTORY_START,
        today,
        WINDOW_DAYS,
    ):

        links = (
            await extract_window(
                page,
                window_start,
                window_end,
                general_total,
            )
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

        log(
            "Global:",
            after,
            "| nuevos:",
            after - before,
        )

    return (
        all_links,
        windows_processed,
    )


# ============================================================
# ESTABILIZACION POR DOBLE CONJUNTO
# ============================================================

async def get_stable_universe(
    page,
    initial_counter,
):

    previous_links = None

    last_counter = (
        initial_counter
    )

    total_windows = 0

    for round_number in range(
        1,
        MAX_STABILITY_ROUNDS + 1,
    ):

        log("")
        log(
            "===================================="
        )
        log(
            f"LECTURA DE ESTABILIDAD "
            f"{round_number}/"
            f"{MAX_STABILITY_ROUNDS}"
        )
        log(
            "===================================="
        )

        counter_before = (
            await get_general_portal_total(
                page
            )
        )

        (
            current_links,
            windows_processed,
        ) = await collect_full_universe(
            page,
            counter_before,
        )

        total_windows += (
            windows_processed
        )

        current_ids = set(
            current_links.keys()
        )

        counter_after = (
            await get_general_portal_total(
                page
            )
        )

        log("")
        log(
            "Contador antes:",
            counter_before,
        )

        log(
            "IDs encontrados:",
            len(current_ids),
        )

        log(
            "Contador despues:",
            counter_after,
        )

        if previous_links is not None:

            previous_ids = set(
                previous_links.keys()
            )

            added = (
                current_ids
                - previous_ids
            )

            removed = (
                previous_ids
                - current_ids
            )

            log(
                "Cambios vs lectura anterior:"
            )

            log(
                "  altas:",
                len(added),
            )

            log(
                "  bajas:",
                len(removed),
            )

            if (
                current_ids
                == previous_ids
            ):

                log("")
                log(
                    "CONJUNTO DE IDS ESTABLE "
                    "EN DOS LECTURAS CONSECUTIVAS."
                )

                return {
                    "links":
                        current_links,

                    "stable_id_count":
                        len(
                            current_ids
                        ),

                    "portal_total":
                        counter_after,

                    "portal_counter_match":
                        (
                            len(
                                current_ids
                            )
                            == counter_after
                        ),

                    "windows_processed":
                        total_windows,

                    "stability_rounds":
                        round_number,
                }

        previous_links = (
            current_links
        )

        last_counter = (
            counter_after
        )

        if (
            round_number
            < MAX_STABILITY_ROUNDS
        ):

            log(
                f"Esperando "
                f"{STABILITY_WAIT_SECONDS}s "
                "antes de repetir..."
            )

            await asyncio.sleep(
                STABILITY_WAIT_SECONDS
            )

    raise RuntimeError(
        "No se obtuvo el mismo conjunto "
        "de IDs en dos lecturas consecutivas "
        f"despues de "
        f"{MAX_STABILITY_ROUNDS} rondas. "
        f"Ultimo contador: {last_counter}"
    )


# ============================================================
# DETALLE
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

    # TITULO
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

    # OBJETO
    objeto = extract_regex(
        body_text,
        r"(?:Objeto|Descripci[oó]n)"
        r"\s*:?\s*(.+?)"
        r"(?=\n[A-ZÁÉÍÓÚÑ]"
        r"[^\n]{0,80}:|\Z)",
    )

    # FECHAS
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

    # ITEMS
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

        match = (
            item_pattern.search(
                line
            )
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

    # ADJUNTOS
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

        anchor = anchors.nth(i)

        href = (
            await anchor.get_attribute(
                "href"
            )
        )

        if not href:
            continue

        text = clean_text(
            await anchor.inner_text()
        )

        full_url = absolute_url(
            href
        )

        lower_href = (
            href.lower()
        )

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

        if (
            attachment["url"]
            in seen
        ):
            continue

        seen.add(
            attachment["url"]
        )

        unique_attachments.append(
            attachment
        )

    # TEXTO SEMANTICO
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
            for value in semantic_parts
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


# ============================================================
# TODOS LOS DETALLES
# ============================================================

async def extract_all_details(
    context,
    detail_links,
):

    page = (
        await context.new_page()
    )

    records_by_id = {}

    failures = []

    seeds = list(
        detail_links.values()
    )

    total = len(
        seeds
    )

    log("")
    log(
        "===================================="
    )
    log(
        "DESCARGA DE DETALLES"
    )
    log(
        "===================================="
    )

    for index, seed in enumerate(
        seeds,
        start=1,
    ):

        log(
            f"Detalle "
            f"{index}/{total} "
            f"- {seed['detail_id']}"
        )

        try:

            record = await extract_detail(
                page,
                seed,
            )

            records_by_id[
                seed["detail_id"]
            ] = record

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
        records_by_id,
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
    records_by_id,
    stability_info,
    failures,
):

    timestamp = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    records = list(
        records_by_id.values()
    )

    stable_id_count = (
        stability_info[
            "stable_id_count"
        ]
    )

    portal_total = (
        stability_info[
            "portal_total"
        ]
    )

    portal_counter_match = (
        stability_info[
            "portal_counter_match"
        ]
    )

    file_integrity = (
        len(records_by_id)
        == len(records)
        == stable_id_count
    )

    detail_validation_complete = (
        len(failures) == 0
        and
        len(records)
        == stable_id_count
    )

    content_quality_complete = (
        len(records) > 0
        and
        all(
            record.get(
                "titulo"
            )
            and
            record.get(
                "url_oficial"
            )
            for record in records
        )
    )

    # IMPORTANTE:
    # la cobertura se basa ahora en:
    # - doble conjunto estable
    # - todos los detalles
    # - integridad JSON/CSV
    #
    # El contador puede diferir transitoriamente.
    coverage_complete = (
        stable_id_count > 0
        and
        file_integrity
        and
        detail_validation_complete
        and
        content_quality_complete
    )

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

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

            row["adjuntos"] = json.dumps(
                row["adjuntos"],
                ensure_ascii=False,
            )

            writer.writerow(
                row
            )

    # --------------------------------------------------------
    # METADATA
    # --------------------------------------------------------

    metadata = {
        "extraction_timestamp":
            timestamp,

        "source":
            "Compras Estatales Uruguay / ARCE",

        "source_url":
            f"{BASE_URL}/consultas/",

        "publication_type":
            "VIG",

        "extraction_method":
            (
                "PLAYWRIGHT_DATE_WINDOWS"
                "_PLUS_DOUBLE_STABLE_ID_SET"
            ),

        "date_filter_type":
            "MOD",

        "window_days":
            WINDOW_DAYS,

        "history_start":
            HISTORY_START.strftime(
                "%Y-%m-%d"
            ),

        "stability_rounds":
            stability_info[
                "stability_rounds"
            ],

        "stable_id_set":
            True,

        "stable_id_count":
            stable_id_count,

        "portal_total":
            portal_total,

        "portal_counter_match":
            portal_counter_match,

        "portal_counter_difference":
            (
                stable_id_count
                - portal_total
            ),

        "windows_processed":
            stability_info[
                "windows_processed"
            ],

        "list_unique_records":
            stable_id_count,

        "detail_unique_records":
            len(
                records_by_id
            ),

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

        "detail_validation_complete":
            detail_validation_complete,

        "content_quality_complete":
            content_quality_complete,

        "coverage_complete":
            coverage_complete,

        "coverage_basis":
            (
                "DOUBLE_STABLE_ID_SET"
                "_PLUS_DETAIL_VALIDATION"
                "_PLUS_FILE_INTEGRITY"
            ),

        "validation_status":
            (
                "OK"
                if coverage_complete
                else "ERROR"
            ),

        "note":
            (
                "La cobertura se valida mediante "
                "dos lecturas consecutivas que "
                "producen exactamente el mismo "
                "conjunto de IDs vigentes. "
                "El contador visible del portal "
                "se conserva como control adicional "
                "pero no invalida por si solo un "
                "conjunto estable, debido a posibles "
                "desfasajes temporales entre el "
                "contador y el listado."
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

            page = (
                await context.new_page()
            )

            # ------------------------------------------------
            # WARM-UP
            # ------------------------------------------------

            await warm_up_portal(
                page
            )

            # ------------------------------------------------
            # CONTADOR INICIAL
            # ------------------------------------------------

            initial_total = (
                await get_general_portal_total(
                    page
                )
            )

            log("")
            log(
                "Contador inicial ARCE:",
                initial_total,
            )

            # ------------------------------------------------
            # DOBLE LECTURA ESTABLE DE IDS
            # ------------------------------------------------

            stability_info = (
                await get_stable_universe(
                    page,
                    initial_total,
                )
            )

            detail_links = (
                stability_info[
                    "links"
                ]
            )

            log("")
            log(
                "===================================="
            )
            log(
                "UNIVERSO ESTABLE"
            )
            log(
                "===================================="
            )

            log(
                "IDs estables:",
                stability_info[
                    "stable_id_count"
                ],
            )

            log(
                "Contador portal:",
                stability_info[
                    "portal_total"
                ],
            )

            log(
                "Coincidencia contador:",
                stability_info[
                    "portal_counter_match"
                ],
            )

            # ------------------------------------------------
            # DESCARGA DE DETALLES
            # ------------------------------------------------

            (
                records_by_id,
                failures,
            ) = await extract_all_details(
                context,
                detail_links,
            )

            # ------------------------------------------------
            # VERIFICAR QUE TODOS LOS IDS TENGAN DETALLE
            # ------------------------------------------------

            stable_ids = set(
                detail_links.keys()
            )

            detail_ids = set(
                records_by_id.keys()
            )

            missing_details = (
                stable_ids
                - detail_ids
            )

            if missing_details:

                raise RuntimeError(
                    "Faltan detalles para "
                    f"{len(missing_details)} "
                    "IDs del conjunto estable."
                )

            # ------------------------------------------------
            # ARCHIVOS
            # ------------------------------------------------

            metadata = (
                write_temp_files(
                    records_by_id,
                    stability_info,
                    failures,
                )
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
                "Stable ID set:",
                metadata[
                    "stable_id_set"
                ],
            )

            log(
                "Stable IDs:",
                metadata[
                    "stable_id_count"
                ],
            )

            log(
                "Portal total:",
                metadata[
                    "portal_total"
                ],
            )

            log(
                "Portal counter match:",
                metadata[
                    "portal_counter_match"
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

            # ------------------------------------------------
            # PUBLICAR
            # ------------------------------------------------

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
                "Universo estable:",
                metadata[
                    "stable_id_count"
                ],
                "llamados vigentes."
            )

            if not metadata[
                "portal_counter_match"
            ]:

                log(
                    "AVISO: contador visible ARCE:",
                    metadata[
                        "portal_total"
                    ],
                    "| conjunto estable:",
                    metadata[
                        "stable_id_count"
                    ],
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