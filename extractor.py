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

PAGE_PAUSE_SECONDS = 0.20
DETAIL_PAUSE_SECONDS = 0.15

WINDOW_DAYS = 14

MAX_RESULTS_PER_WINDOW = 800
MAX_PAGES_PER_WINDOW = 100

HISTORY_START = datetime(2010, 1, 1)

# Catch-up previo a detalles
CATCHUP_DAYS = 45
MAX_CATCHUP_ROUNDS = 3

# Reconciliacion posterior a detalles.
# Si cambia el conjunto de llamados mientras descargamos
# detalles, volvemos a obtener el universo vigente.
MAX_FINAL_RECONCILIATION_ROUNDS = 3

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

            if attempt < MAX_HTTP_ATTEMPTS:

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


async def refresh_general_total(
    page,
    previous_total,
):

    log("")
    log(
        "Revalidando contador general ARCE..."
    )

    current = (
        await get_general_portal_total(
            page
        )
    )

    log(
        "Contador anterior:",
        previous_total,
        "| contador actual:",
        current,
    )

    if current != previous_total:

        await asyncio.sleep(
            RECONCILIATION_WAIT_SECONDS
        )

        confirmation = (
            await get_general_portal_total(
                page
            )
        )

        log(
            "Confirmacion contador:",
            confirmation,
        )

        return confirmation

    return current


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
            f"{after}/{expected_total}"
        )

        if after == expected_total:
            break

        if after > expected_total:

            raise RuntimeError(
                "La pagina recupero mas IDs "
                "que el contador de la ventana: "
                f"{after} > {expected_total}"
            )

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

    return window_links


# ============================================================
# VENTANA CON SUBDIVISION
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
            "el filtro de fecha. "
            "Una ventana de un solo dia "
            f"devuelve {window_total} "
            f"y el total general es "
            f"{general_total}."
        )

    if (
        window_total
        > MAX_RESULTS_PER_WINDOW
    ):

        if (
            start_date.date()
            == end_date.date()
        ):

            raise RuntimeError(
                "Una ventana de un solo dia "
                "supera el maximo previsto: "
                f"{window_total}"
            )

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

        log(
            "Ventana demasiado grande:",
            window_total,
        )

        log(
            "Subdividiendo:"
        )

        log(
            " A:",
            start_date.strftime(
                "%Y-%m-%d"
            ),
            "a",
            left_end.strftime(
                "%Y-%m-%d"
            ),
        )

        log(
            " B:",
            right_start.strftime(
                "%Y-%m-%d"
            ),
            "a",
            end_date.strftime(
                "%Y-%m-%d"
            ),
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

    links = (
        await extract_window_simple(
            page,
            start_date,
            end_date,
            window_total,
        )
    )

    if (
        len(links)
        != window_total
    ):

        raise RuntimeError(
            "La ventana no coincide "
            "con su contador: "
            f"{len(links)} "
            f"!= {window_total}"
        )

    return links


# ============================================================
# CATCH-UP RECIENTE
# ============================================================

async def run_recent_catchup(
    page,
    all_links,
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

    catchup_start = (
        today
        - timedelta(
            days=CATCHUP_DAYS - 1
        )
    )

    log("")
    log(
        "===================================="
    )
    log(
        "CATCH-UP RECIENTE"
    )
    log(
        "===================================="
    )

    added_total = 0

    for (
        window_start,
        window_end,
    ) in generate_windows(
        catchup_start,
        today,
        WINDOW_DAYS,
    ):

        links = (
            await extract_window(
                page,
                window_start,
                window_end,
                portal_total,
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

        added = (
            after - before
        )

        added_total += added

        log(
            "Catch-up:",
            after,
            "/",
            portal_total,
            "| nuevos:",
            added,
        )

    return added_total


# ============================================================
# PRIMER UNIVERSO DE IDS
# ============================================================

async def collect_initial_universe(
    page,
    portal_total_start,
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

    current_target = (
        portal_total_start
    )

    log("")
    log(
        "===================================="
    )
    log(
        "EXTRACCION HISTORICA POR VENTANAS"
    )
    log(
        "===================================="
    )

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
                current_target,
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
            "/",
            current_target,
            "| nuevos:",
            after - before,
        )

    current_target = (
        await refresh_general_total(
            page,
            current_target,
        )
    )

    catchup_rounds = 0

    for round_number in range(
        1,
        MAX_CATCHUP_ROUNDS + 1,
    ):

        if (
            len(all_links)
            == current_target
        ):

            return (
                all_links,
                windows_processed,
                current_target,
                catchup_rounds,
            )

        if (
            len(all_links)
            > current_target
        ):

            break

        catchup_rounds = round_number

        log("")
        log(
            f"CATCH-UP RONDA "
            f"{round_number}/"
            f"{MAX_CATCHUP_ROUNDS}"
        )

        log(
            "Faltantes:",
            current_target
            - len(all_links),
        )

        await run_recent_catchup(
            page,
            all_links,
            current_target,
        )

        current_target = (
            await refresh_general_total(
                page,
                current_target,
            )
        )

        log(
            "Despues del catch-up:",
            len(all_links),
            "/",
            current_target,
        )

        if (
            len(all_links)
            == current_target
        ):

            return (
                all_links,
                windows_processed,
                current_target,
                catchup_rounds,
            )

    return (
        all_links,
        windows_processed,
        current_target,
        catchup_rounds,
    )


# ============================================================
# OBTENER UNIVERSO ACTUAL COMPLETO
# ============================================================

async def collect_fresh_universe(
    page,
    portal_total,
):
    """
    Hace una nueva pasada completa de IDs vigentes.

    Se usa al final para detectar:
    - altas nuevas
    - llamados que dejaron de estar vigentes

    No descarga nuevamente los detalles.
    """

    today_date = (
        datetime.now().date()
    )

    today = datetime(
        today_date.year,
        today_date.month,
        today_date.day,
    )

    fresh_links = {}

    log("")
    log(
        "===================================="
    )
    log(
        "RELECTURA DEL UNIVERSO VIGENTE"
    )
    log(
        "===================================="
    )

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
                portal_total,
            )
        )

        fresh_links.update(
            links
        )

        log(
            "Relectura global:",
            len(fresh_links),
            "/",
            portal_total,
        )

    return fresh_links


# ============================================================
# DETALLE INDIVIDUAL
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

        locator = (
            page.locator(
                selector
            )
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
# DESCARGAR DETALLES FALTANTES
# ============================================================

async def download_missing_details(
    context,
    detail_links,
    records_by_id,
):
    """
    Descarga solamente los detalles que aun
    no existen en records_by_id.
    """

    missing_ids = [
        detail_id
        for detail_id
        in detail_links.keys()
        if detail_id
        not in records_by_id
    ]

    if not missing_ids:
        return []

    log("")
    log(
        "Detalles nuevos a descargar:",
        len(missing_ids),
    )

    page = (
        await context.new_page()
    )

    failures = []

    total = len(
        missing_ids
    )

    for index, detail_id in enumerate(
        missing_ids,
        start=1,
    ):

        seed = detail_links[
            detail_id
        ]

        log(
            f"Detalle nuevo "
            f"{index}/{total} "
            f"- {detail_id}"
        )

        try:

            record = (
                await extract_detail(
                    page,
                    seed,
                )
            )

            records_by_id[
                detail_id
            ] = record

        except Exception as exc:

            failures.append(
                {
                    "id":
                        detail_id,

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
                detail_id,
                ":",
                exc,
            )

        await asyncio.sleep(
            DETAIL_PAUSE_SECONDS
        )

    await page.close()

    return failures


# ============================================================
# RECONCILIACION FINAL
# ============================================================

async def reconcile_after_details(
    page,
    context,
    detail_links,
    records_by_id,
    portal_total,
):
    """
    Vuelve a obtener el universo vigente después
    de descargar los detalles.

    Si cambió:
    - agrega llamados nuevos
    - descarga únicamente sus detalles
    - elimina llamados que ya no están vigentes

    Se repite hasta estabilizar.
    """

    all_failures = []

    reconciliation_rounds = 0

    for round_number in range(
        1,
        MAX_FINAL_RECONCILIATION_ROUNDS + 1,
    ):

        reconciliation_rounds = (
            round_number
        )

        log("")
        log(
            "===================================="
        )
        log(
            f"RECONCILIACION FINAL "
            f"{round_number}/"
            f"{MAX_FINAL_RECONCILIATION_ROUNDS}"
        )
        log(
            "===================================="
        )

        total_now = (
            await refresh_general_total(
                page,
                portal_total,
            )
        )

        fresh_links = (
            await collect_fresh_universe(
                page,
                total_now,
            )
        )

        fresh_ids = set(
            fresh_links.keys()
        )

        old_ids = set(
            detail_links.keys()
        )

        added_ids = (
            fresh_ids - old_ids
        )

        removed_ids = (
            old_ids - fresh_ids
        )

        log(
            "Portal actual:",
            total_now,
        )

        log(
            "IDs reextraidos:",
            len(
                fresh_ids
            ),
        )

        log(
            "Altas detectadas:",
            len(
                added_ids
            ),
        )

        log(
            "Bajas detectadas:",
            len(
                removed_ids
            ),
        )

        if (
            len(fresh_ids)
            != total_now
        ):

            log(
                "La relectura todavia no "
                "coincide con el contador. "
                "Se hara otra ronda."
            )

            portal_total = (
                total_now
            )

            detail_links = (
                fresh_links
            )

            await asyncio.sleep(
                RECONCILIATION_WAIT_SECONDS
            )

            continue

        # ----------------------------------------------------
        # ELIMINAR LOS QUE DEJARON DE ESTAR VIGENTES
        # ----------------------------------------------------

        for removed_id in (
            removed_ids
        ):

            records_by_id.pop(
                removed_id,
                None,
            )

        # ----------------------------------------------------
        # ACTUALIZAR UNIVERSO
        # ----------------------------------------------------

        detail_links = (
            fresh_links
        )

        # ----------------------------------------------------
        # DESCARGAR SOLO ALTAS NUEVAS
        # ----------------------------------------------------

        failures = (
            await download_missing_details(
                context,
                detail_links,
                records_by_id,
            )
        )

        all_failures.extend(
            failures
        )

        if failures:

            log(
                "Hubo fallos descargando "
                "detalles nuevos."
            )

            portal_total = (
                total_now
            )

            continue

        # ----------------------------------------------------
        # CONTROL DE CANTIDAD
        # ----------------------------------------------------

        if (
            len(records_by_id)
            != total_now
        ):

            log(
                "Los detalles disponibles "
                "todavia no coinciden con "
                "el contador:"
            )

            log(
                len(records_by_id),
                "!=",
                total_now,
            )

            portal_total = (
                total_now
            )

            continue

        # ----------------------------------------------------
        # CONFIRMACION DEL CONTADOR
        # ----------------------------------------------------

        await asyncio.sleep(
            RECONCILIATION_WAIT_SECONDS
        )

        confirmation_total = (
            await get_general_portal_total(
                page
            )
        )

        log(
            "Confirmacion final:",
            confirmation_total,
        )

        if (
            confirmation_total
            == total_now
        ):

            log(
                "Universo final estabilizado."
            )

            return (
                detail_links,
                records_by_id,
                confirmation_total,
                reconciliation_rounds,
                all_failures,
            )

        log(
            "El contador volvio a cambiar: "
            f"{total_now} -> "
            f"{confirmation_total}"
        )

        portal_total = (
            confirmation_total
        )

    raise RuntimeError(
        "El universo de llamados vigentes "
        "no logro estabilizarse despues de "
        f"{MAX_FINAL_RECONCILIATION_ROUNDS} "
        "rondas de reconciliacion final."
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
    portal_total_start,
    portal_total_final,
    windows_processed,
    catchup_rounds,
    final_reconciliation_rounds,
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

    unique_ids = set(
        records_by_id.keys()
    )

    detail_unique = len(
        unique_ids
    )

    file_integrity = (
        detail_unique
        == len(records)
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

    portal_reconciliation = (
        detail_unique
        == portal_total_final
    )

    coverage_complete = (
        len(failures) == 0
        and
        portal_reconciliation
        and
        file_integrity
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

        "extraction_method":
            (
                "PLAYWRIGHT_DATE_WINDOWS"
                "_PLUS_FINAL_SET_RECONCILIATION"
            ),

        "publication_type":
            "VIG",

        "date_filter_type":
            "MOD",

        "window_days":
            WINDOW_DAYS,

        "catchup_days":
            CATCHUP_DAYS,

        "catchup_rounds":
            catchup_rounds,

        "final_reconciliation_rounds":
            final_reconciliation_rounds,

        "history_start":
            HISTORY_START.strftime(
                "%Y-%m-%d"
            ),

        "windows_processed":
            windows_processed,

        "portal_total_start":
            portal_total_start,

        "portal_total_final":
            portal_total_final,

        "portal_total":
            portal_total_final,

        "portal_changed_during_run":
            (
                portal_total_start
                != portal_total_final
            ),

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
            portal_reconciliation,

        "content_quality_complete":
            content_quality_complete,

        "coverage_complete":
            coverage_complete,

        "coverage_basis":
            (
                "PORTAL_DYNAMIC_TOTAL"
                "_PLUS_DATE_WINDOWS"
                "_PLUS_FINAL_ID_SET_RECONCILIATION"
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
                "El universo de llamados vigentes "
                "se relee despues de descargar los "
                "detalles. Las altas ocurridas "
                "durante la corrida se incorporan "
                "sin repetir los detalles ya "
                "descargados y las bajas dejan "
                "de formar parte del snapshot. "
                "El archivo solo se publica "
                "cuando el conjunto final de IDs "
                "y el contador del portal "
                "se encuentran reconciliados."
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
            # TOTAL INICIAL
            # ------------------------------------------------

            log("")
            log(
                "===================================="
            )
            log(
                "TOTAL GENERAL INICIAL"
            )
            log(
                "===================================="
            )

            total_start = (
                await get_general_portal_total(
                    page
                )
            )

            log(
                "Portal inicial:",
                total_start,
            )

            # ------------------------------------------------
            # UNIVERSO INICIAL
            # ------------------------------------------------

            (
                detail_links,
                windows_processed,
                target_before_details,
                catchup_rounds,
            ) = await collect_initial_universe(
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
                "IDs recuperados:",
                len(
                    detail_links
                ),
            )

            log(
                "Portal:",
                target_before_details,
            )

            if (
                len(detail_links)
                != target_before_details
            ):

                raise RuntimeError(
                    "El universo inicial no "
                    "coincide con el contador "
                    "vigente antes de detalles: "
                    f"{len(detail_links)} "
                    f"!= {target_before_details}"
                )

            # ------------------------------------------------
            # DETALLES INICIALES
            # ------------------------------------------------

            records_by_id = {}

            initial_failures = (
                await download_missing_details(
                    context,
                    detail_links,
                    records_by_id,
                )
            )

            if initial_failures:

                raise RuntimeError(
                    "Fallaron "
                    f"{len(initial_failures)} "
                    "detalles durante la "
                    "descarga inicial."
                )

            log("")
            log(
                "Detalles iniciales descargados:",
                len(
                    records_by_id
                ),
            )

            # ------------------------------------------------
            # RECONCILIACION POST-DETALLE
            # ------------------------------------------------

            (
                final_links,
                records_by_id,
                total_final,
                final_reconciliation_rounds,
                final_failures,
            ) = await reconcile_after_details(
                page,
                context,
                detail_links,
                records_by_id,
                target_before_details,
            )

            all_failures = (
                initial_failures
                + final_failures
            )

            log("")
            log(
                "===================================="
            )
            log(
                "RESULTADO FINAL"
            )
            log(
                "===================================="
            )

            log(
                "Portal inicial:",
                total_start,
            )

            log(
                "Portal antes de detalles:",
                target_before_details,
            )

            log(
                "Portal final:",
                total_final,
            )

            log(
                "IDs finales:",
                len(
                    final_links
                ),
            )

            log(
                "Detalles finales:",
                len(
                    records_by_id
                ),
            )

            # ------------------------------------------------
            # ARCHIVOS
            # ------------------------------------------------

            metadata = (
                write_temp_files(
                    records_by_id,
                    total_start,
                    total_final,
                    windows_processed,
                    catchup_rounds,
                    final_reconciliation_rounds,
                    all_failures,
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
                f"{total_final}/"
                f"{total_final} "
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
