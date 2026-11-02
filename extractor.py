import asyncio
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


BASE_URL = "https://www.comprasestatales.gub.uy"

START_URLS = [
    f"{BASE_URL}/consultas/buscar/reset/1/tipo-pub/VIG",
    f"{BASE_URL}/consultas/buscar",
]

OUTPUT_JSON = Path("latest.json")
OUTPUT_CSV = Path("latest.csv")
OUTPUT_METADATA = Path("metadata.json")

TEMP_JSON = Path("latest.tmp.json")
TEMP_CSV = Path("latest.tmp.csv")
TEMP_METADATA = Path("metadata.tmp.json")

NAV_TIMEOUT = 60000

# Pausa pequeña para no sobrecargar ARCE.
PAGE_PAUSE_SECONDS = 0.35
DETAIL_PAUSE_SECONDS = 0.30

# Cantidad máxima de intentos por página.
MAX_ATTEMPTS = 3


def log(*args):
    print(*args, flush=True)


def log_error(*args):
    print(*args, file=sys.stderr, flush=True)


def clean_text(value):
    if value is None:
        return ""

    return re.sub(r"\s+", " ", value).strip()


def absolute_url(href):
    return urljoin(BASE_URL, href)


def extract_detail_id(url):
    match = re.search(
        r"/consultas/detalle/mostrar-llamado/\d+/id/(\d+)",
        url,
        re.I,
    )

    return match.group(1) if match else None


def parse_datetime(text):
    """
    Devuelve el texto normalizado.
    No convertimos la fecha para evitar errores de timezone.
    """

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

    return clean_text(match.group(1))


async def goto_with_retry(page, url, label):
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):

        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT,
            )

            if response is None:
                raise RuntimeError(
                    "Navegacion sin respuesta HTTP"
                )

            status = response.status

            if status in (403, 429, 500, 502, 503, 504):
                raise RuntimeError(
                    f"HTTP {status}"
                )

            if status >= 400:
                raise RuntimeError(
                    f"HTTP {status}"
                )

            await page.wait_for_timeout(500)

            return response

        except Exception as exc:
            last_error = exc

            log_error(
                f"{label}: intento "
                f"{attempt}/{MAX_ATTEMPTS} fallo: {exc}"
            )

            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(
                    2 * attempt
                )

    raise RuntimeError(
        f"{label}: fallo luego de "
        f"{MAX_ATTEMPTS} intentos. "
        f"Ultimo error: {last_error}"
    )


async def find_working_start_url(page):
    log("")
    log("====================================")
    log("HEALTH CHECK NUEVA WEB ARCE")
    log("====================================")

    errors = []

    for url in START_URLS:

        try:
            log("Probando:", url)

            await goto_with_retry(
                page,
                url,
                "health-check",
            )

            body = clean_text(
                await page.locator("body").inner_text()
            )

            if (
                "Llamados vigentes" not in body
                and "Se encontraron" not in body
            ):
                raise RuntimeError(
                    "La pagina no parece ser "
                    "la consulta de llamados vigentes."
                )

            log("Nueva interfaz ARCE accesible.")

            return page.url

        except Exception as exc:
            errors.append(
                f"{url}: {exc}"
            )

    raise RuntimeError(
        "Ninguna URL de consulta ARCE funciono. "
        + " | ".join(errors)
    )


async def get_portal_total(page):
    body = clean_text(
        await page.locator("body").inner_text()
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

    number = match.group(1).replace(".", "")

    return int(number)


async def collect_detail_links_from_page(page):
    links = {}

    locator = page.locator(
        "a[href*='/consultas/detalle/mostrar-llamado/']"
    )

    count = await locator.count()

    for i in range(count):
        element = locator.nth(i)

        href = await element.get_attribute("href")

        if not href:
            continue

        url = absolute_url(href)

        detail_id = extract_detail_id(url)

        if not detail_id:
            continue

        title = clean_text(
            await element.inner_text()
        )

        if not title:
            continue

        # El mismo link puede aparecer más de una vez.
        links[detail_id] = {
            "detail_id": detail_id,
            "detail_url": url,
            "titulo_listado": title,
        }

    return links


async def collect_pagination_links(page):
    """
    No suponemos cuántos resultados entran por página.
    Seguimos los links de paginación que expone ARCE.
    """

    result = set()

    locator = page.locator("a[href]")

    count = await locator.count()

    for i in range(count):
        element = locator.nth(i)

        href = await element.get_attribute("href")

        if not href:
            continue

        if "/page/" not in href:
            continue

        if "/consultas/" not in href:
            continue

        url = absolute_url(href)

        parsed = urlparse(url)

        if parsed.netloc != urlparse(BASE_URL).netloc:
            continue

        # Evitamos páginas de detalle.
        if "/detalle/" in url:
            continue

        result.add(url)

    return result


async def crawl_all_search_pages(page, start_url):
    log("")
    log("====================================")
    log("PAGINACION DE LLAMADOS VIGENTES")
    log("====================================")

    await goto_with_retry(
        page,
        start_url,
        "pagina-inicial",
    )

    portal_total = await get_portal_total(page)

    log(
        "Total informado por ARCE:",
        portal_total,
    )

    queue = [page.url]
    visited = set()

    detail_links = {}

    while queue:
        current = queue.pop(0)

        if current in visited:
            continue

        visited.add(current)

        log(
            f"Pagina de resultados "
            f"{len(visited)}: {current}"
        )

        await goto_with_retry(
            page,
            current,
            "pagina-resultados",
        )

        current_links = (
            await collect_detail_links_from_page(
                page
            )
        )

        before = len(detail_links)

        detail_links.update(
            current_links
        )

        after = len(detail_links)

        log(
            "  llamados nuevos:",
            after - before,
            "| acumulados:",
            after,
            "/",
            portal_total,
        )

        pagination = (
            await collect_pagination_links(
                page
            )
        )

        for url in sorted(pagination):
            if (
                url not in visited
                and url not in queue
            ):
                queue.append(url)

        await asyncio.sleep(
            PAGE_PAUSE_SECONDS
        )

    return (
        portal_total,
        detail_links,
        visited,
    )


def first_meaningful_line_after(lines, target):
    try:
        index = lines.index(target)
    except ValueError:
        return ""

    for line in lines[index + 1:]:
        value = clean_text(line)

        if not value:
            continue

        if value.startswith(
            "Recepción de ofertas hasta:"
        ):
            return ""

        return value

    return ""


async def extract_detail(page, seed):
    url = seed["detail_url"]

    await goto_with_retry(
        page,
        url,
        f"detalle-{seed['detail_id']}",
    )

    body_text = await page.locator(
        "body"
    ).inner_text()

    body_text = body_text.replace(
        "\xa0",
        " ",
    )

    lines = [
        clean_text(line)
        for line in body_text.splitlines()
        if clean_text(line)
    ]

    # -------------------------------------------------------
    # TITULO
    # -------------------------------------------------------

    title = ""

    h2 = page.locator("h2")

    if await h2.count() > 0:
        title = clean_text(
            await h2.first.inner_text()
        )

    if not title:
        title = seed[
            "titulo_listado"
        ]

    # -------------------------------------------------------
    # OBJETO
    # -------------------------------------------------------

    objeto = first_meaningful_line_after(
        lines,
        title,
    )

    # -------------------------------------------------------
    # FECHAS
    # -------------------------------------------------------

    cierre = extract_regex(
        body_text,
        r"Recepci[oó]n de ofertas hasta:\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"\s+[0-9]{2}:[0-9]{2})",
    )

    publicacion = extract_regex(
        body_text,
        r"Fecha Publicaci[oó]n:\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"\s+[0-9]{2}:[0-9]{2})",
    )

    if not publicacion:
        publicacion = extract_regex(
            body_text,
            r"Publicado:\s*"
            r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
            r"\s+[0-9]{2}:[0-9]{2})",
        )

    ultima_modificacion = extract_regex(
        body_text,
        r"[UÚ]ltima Modificaci[oó]n:\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"\s+[0-9]{2}:[0-9]{2})",
    )

    acto_apertura = extract_regex(
        body_text,
        r"Acto de Apertura:\s*"
        r"([0-9]{2}/[0-9]{2}/[0-9]{4}"
        r"\s+[0-9]{2}:[0-9]{2})",
    )

    # -------------------------------------------------------
    # ITEMS
    # -------------------------------------------------------

    items = []

    item_pattern = re.compile(
        r"Ítem\s*N[º°]?\s*(\d+)\s+(.+?)"
        r"(?:\s+\(C[oó]d\.\s*Art[ií]culo\s*"
        r"([^)]+)\))?$",
        re.I,
    )

    for line in lines:
        if not line.lower().startswith(
            ("ítem", "item")
        ):
            continue

        match = item_pattern.search(line)

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
                            match.group(3) or ""
                        ),
                }
            )

    # -------------------------------------------------------
    # ADJUNTOS
    # -------------------------------------------------------

    attachments = []

    anchors = page.locator(
        "a[href]"
    )

    anchor_count = await anchors.count()

    for i in range(anchor_count):
        anchor = anchors.nth(i)

        href = await anchor.get_attribute(
            "href"
        )

        if not href:
            continue

        text = clean_text(
            await anchor.inner_text()
        )

        full_url = absolute_url(href)

        href_lower = href.lower()

        if (
            ".pdf" in href_lower
            or "/pliegos/" in href_lower
            or "/aclaraciones/" in href_lower
        ):
            attachments.append(
                {
                    "texto": text,
                    "url": full_url,
                }
            )

    # eliminar duplicados
    unique_attachments = []
    attachment_seen = set()

    for item in attachments:
        key = item["url"]

        if key in attachment_seen:
            continue

        attachment_seen.add(key)

        unique_attachments.append(
            item
        )

    # -------------------------------------------------------
    # APERTURA ELECTRONICA
    # -------------------------------------------------------

    apertura_electronica = (
        "Apertura electrónica"
        in body_text
        or
        "Apertura electronica"
        in body_text
    )

    # -------------------------------------------------------
    # INFORMACION PARA ANALISIS SEMANTICO
    # -------------------------------------------------------

    semantic_parts = [
        title,
        objeto,
    ]

    semantic_parts.extend(
        item["descripcion"]
        for item in items
    )

    semantic_text = clean_text(
        " | ".join(
            value
            for value in semantic_parts
            if value
        )
    )

    return {
        "id": seed["detail_id"],
        "titulo": title,
        "objeto": objeto,
        "fecha_publicacion":
            parse_datetime(publicacion),
        "ultima_modificacion":
            parse_datetime(
                ultima_modificacion
            ),
        "recepcion_ofertas_hasta":
            parse_datetime(cierre),
        "acto_apertura":
            parse_datetime(
                acto_apertura
            ),
        "apertura_electronica":
            apertura_electronica,
        "items": items,
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
    log("====================================")
    log("DETALLE DE CADA LLAMADO")
    log("====================================")

    # Una sola página evita bombardear el servidor.
    page = await context.new_page()

    records = []

    seeds = list(
        detail_links.values()
    )

    total = len(seeds)

    failures = []

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

            records.append(record)

        except Exception as exc:
            log_error(
                f"ERROR detalle "
                f"{seed['detail_id']}: "
                f"{exc}"
            )

            failures.append(
                {
                    "id":
                        seed["detail_id"],
                    "url":
                        seed["detail_url"],
                    "error":
                        str(exc),
                }
            )

        await asyncio.sleep(
            DETAIL_PAUSE_SECONDS
        )

    await page.close()

    return records, failures


def validate_records(
    portal_total,
    detail_links,
    records,
    failures,
):
    unique_ids = {
        record["id"]
        for record in records
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
        for record in records
    )

    all_have_url = all(
        bool(
            record.get(
                "url_oficial"
            )
        )
        for record in records
    )

    coverage_complete = all(
        [
            portal_total > 0,
            list_matches_portal,
            details_match_portal,
            no_detail_failures,
            all_have_title,
            all_have_url,
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
        "all_have_title":
            all_have_title,
        "all_have_url":
            all_have_url,
        "coverage_complete":
            coverage_complete,
    }


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

            writer.writerow(row)

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
            validation["portal_total"],

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

        "content_quality_complete":
            (
                validation[
                    "all_have_title"
                ]
                and
                validation[
                    "all_have_url"
                ]
            ),

        "coverage_complete":
            validation[
                "coverage_complete"
            ],

        "coverage_basis":
            (
                "PORTAL_HTML_PAGINATION"
                "_PLUS_DETAIL_VALIDATION"
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
                "Cobertura validada contra el "
                "contador visible de llamados "
                "vigentes del portal ARCE. "
                "Se recorrieron todas las paginas "
                "de resultados y se recupero el "
                "detalle de cada identificador."
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


def cleanup_temp():
    for path in (
        TEMP_JSON,
        TEMP_CSV,
        TEMP_METADATA,
    ):
        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass


async def main():
    cleanup_temp()

    try:
        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )

            context = await browser.new_context(
                locale="es-UY",
                timezone_id="America/Montevideo",
                viewport={
                    "width": 1440,
                    "height": 1000,
                },
                user_agent=(
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/130.0.0.0 "
                    "Safari/537.36"
                ),
            )

            page = await context.new_page()

            start_url = (
                await find_working_start_url(
                    page
                )
            )

            (
                portal_total,
                detail_links,
                visited_pages,
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
                len(detail_links),
            )

            # Si la lista ya no coincide, no hacemos
            # cientos de requests de detalle.
            if (
                len(detail_links)
                != portal_total
            ):
                raise RuntimeError(
                    "La paginacion no coincide con "
                    "el contador del portal: "
                    f"{len(detail_links)} "
                    f"!= {portal_total}"
                )

            records, failures = (
                await extract_all_details(
                    context,
                    detail_links,
                )
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
                len(visited_pages),
                failures,
            )

            log("")
            log("====================================")
            log("VALIDACION FINAL")
            log("====================================")

            log(
                "Portal total:",
                metadata["portal_total"],
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
                "Coverage complete:",
                metadata[
                    "coverage_complete"
                ],
            )

            if not metadata[
                "coverage_complete"
            ]:
                raise RuntimeError(
                    "La validacion de cobertura "
                    "del nuevo portal ARCE fallo."
                )

            publish_snapshot()

            log("")
            log("====================================")
            log("SNAPSHOT PUBLICADO OK")
            log("====================================")

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
        log_error(str(exc))
        log_error("")
        log_error(
            "El ultimo snapshot valido "
            "queda intacto."
        )

        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
