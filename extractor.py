import csv
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests


# ============================================================
# CONFIGURACION
# ============================================================

BASE_URL = (
    "https://www.comprasestatales.gub.uy/"
    "comprasenlinea/jboss/generarReporte"
)

PORTAL_URL = (
    "https://www.comprasestatales.gub.uy/"
    "consultas/index/tipo-pub/VIG"
)

OUTPUT_JSON = Path("latest.json")
OUTPUT_CSV = Path("latest.csv")
OUTPUT_METADATA = Path("metadata.json")

# Backfill amplio para capturar llamados publicados o modificados
# anteriormente que todavía puedan estar vigentes.
START_DATE = datetime(2024, 1, 1)

WINDOW_DAYS = 10
TIMEOUT = 60


# ============================================================
# UTILIDADES XML
# ============================================================

def build_params(start_date, end_date):
    return {
        "tipo_publicacion": "lv",
        "tipo_compra": "",
        "anio_inicial": start_date.strftime("%Y"),
        "mes_inicial": start_date.strftime("%m"),
        "dia_inicial": start_date.strftime("%d"),
        "anio_final": end_date.strftime("%Y"),
        "mes_final": end_date.strftime("%m"),
        "dia_final": end_date.strftime("%d"),
        "hora_inicial": "00",
        "hora_final": "23",
    }


def normalize_tag(tag):
    if "}" in tag:
        return tag.split("}", 1)[1]

    return tag


def element_to_dict(element):
    children = list(element)

    if not children:
        return (element.text or "").strip()

    result = {}

    for child in children:
        key = normalize_tag(child.tag)
        value = element_to_dict(child)

        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]

            result[key].append(value)

        else:
            result[key] = value

    return result


def find_record_nodes(root):
    possible_names = {
        "compra",
        "llamado",
        "publicacion",
        "item",
        "registro",
    }

    candidates = []

    for element in root.iter():
        tag = normalize_tag(element.tag).lower()

        if tag in possible_names and list(element):
            candidates.append(element)

    if candidates:
        return candidates

    children = [
        child
        for child in list(root)
        if list(child)
    ]

    if children:
        return children

    return []


def flatten(data, parent_key="", separator="_"):
    items = {}

    if isinstance(data, dict):

        for key, value in data.items():

            new_key = (
                f"{parent_key}{separator}{key}"
                if parent_key
                else key
            )

            if isinstance(value, dict):

                items.update(
                    flatten(
                        value,
                        new_key,
                        separator
                    )
                )

            elif isinstance(value, list):

                items[new_key] = json.dumps(
                    value,
                    ensure_ascii=False
                )

            else:
                items[new_key] = value

    else:
        items[parent_key] = data

    return items


# ============================================================
# IDENTIFICACION / DEDUPLICACION
# ============================================================

def make_record_key(record):
    flattened = {
        str(k).lower(): str(v).strip()
        for k, v in flatten(record).items()
        if v not in (None, "")
    }

    id_candidates = [
        "id_compra",
        "idcompra",
        "id_llamado",
        "idllamado",
        "numero_compra",
        "nro_compra",
    ]

    for candidate in id_candidates:

        for key, value in flattened.items():

            if key.endswith(candidate) and value:

                return f"{candidate}:{value}"

    fallback_values = []

    keywords = [
        "tipo_compra",
        "numero",
        "anio",
        "inciso",
        "unidad_ejecutora",
    ]

    for keyword in keywords:

        for key, value in flattened.items():

            if keyword in key and value:

                fallback_values.append(
                    f"{keyword}:{value}"
                )

                break

    if fallback_values:
        return "|".join(fallback_values)

    return json.dumps(
        record,
        sort_keys=True,
        ensure_ascii=False
    )


# ============================================================
# DESCARGA XML
# ============================================================

def download_window(
    session,
    start_date,
    end_date
):
    params = build_params(
        start_date,
        end_date
    )

    print(
        f"Consultando XML "
        f"{start_date:%d/%m/%Y} "
        f"- {end_date:%d/%m/%Y}"
    )

    response = session.get(
        BASE_URL,
        params=params,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    if not response.content:
        return []

    try:

        root = ET.fromstring(
            response.content
        )

    except ET.ParseError as exc:

        preview = response.text[:500]

        raise RuntimeError(
            "ARCE no devolvio XML valido.\n"
            f"Respuesta inicial:\n{preview}"
        ) from exc

    nodes = find_record_nodes(root)

    records = []

    for node in nodes:

        record = element_to_dict(node)

        if isinstance(record, dict):
            records.append(record)

    return records


def generate_windows(
    start_date,
    end_date
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

        yield current, window_end

        current = (
            window_end
            + timedelta(days=1)
        )


# ============================================================
# RECONCILIACION CONTRA PORTAL WEB
# ============================================================

def get_portal_total(session):
    """
    Intenta recuperar el total de llamados vigentes
    informado por el buscador oficial de Compras Estatales.

    Devuelve None si no puede verificarse.
    """

    print("")
    print(
        "Intentando reconciliacion "
        "contra portal web..."
    )

    urls_to_try = [
        (
            "https://www.comprasestatales.gub.uy/"
            "consultas/index/tipo-pub/VIG"
        ),
        (
            "https://www.comprasestatales.gub.uy/"
            "consultas/buscar/tipo-pub/VIG"
        ),
        (
            "https://www.comprasestatales.gub.uy/"
            "consultas/index/page/1/tipo-pub/VIG"
        ),
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/130.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": (
            "es-UY,es;q=0.9,en;q=0.8"
        ),
        "Referer": (
            "https://www.comprasestatales.gub.uy/"
        ),
    }

    patterns = [
        r"Se\s+encontraron\s+([\d\.\,]+)\s+resultados",
        r"Se\s+encontró\s+([\d\.\,]+)\s+resultado",
        r"([\d\.\,]+)\s+resultados",
    ]

    for url in urls_to_try:

        print("Probando portal:", url)

        try:

            response = session.get(
                url,
                headers=headers,
                timeout=TIMEOUT,
                allow_redirects=True,
            )

            print(
                "HTTP:",
                response.status_code,
                "URL final:",
                response.url,
            )

            if response.status_code != 200:
                continue

            html = response.text

            for pattern in patterns:

                match = re.search(
                    pattern,
                    html,
                    flags=re.IGNORECASE,
                )

                if not match:
                    continue

                raw_total = match.group(1)

                normalized = (
                    raw_total
                    .replace(".", "")
                    .replace(",", "")
                )

                total = int(normalized)

                print(
                    "Total informado por portal:",
                    total,
                )

                return total

        except Exception as exc:

            print(
                "Error consultando",
                url,
                ":",
                exc,
            )

    print(
        "No fue posible recuperar "
        "el total del portal."
    )

    return None


# ============================================================
# SALIDA JSON
# ============================================================

def write_json(records):
    payload = {
        "generated_at": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
        "source": (
            "Compras Estatales Uruguay / ARCE"
        ),
        "publication_type": (
            "Llamados vigentes"
        ),
        "total_records": (
            len(records)
        ),
        "data": records,
    }

    OUTPUT_JSON.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# SALIDA CSV
# ============================================================

def write_csv(records):
    flat_records = [
        flatten(record)
        for record in records
    ]

    if not flat_records:

        OUTPUT_CSV.write_text(
            "sin_registros\n",
            encoding="utf-8",
        )

        return

    columns = sorted(
        {
            key
            for record in flat_records
            for key in record.keys()
        }
    )

    with OUTPUT_CSV.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=columns,
        )

        writer.writeheader()

        for record in flat_records:
            writer.writerow(record)


def count_csv_records():
    if not OUTPUT_CSV.exists():
        return 0

    with OUTPUT_CSV.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        rows = list(
            csv.reader(file)
        )

    if len(rows) <= 1:
        return 0

    return len(rows) - 1


# ============================================================
# METADATA / VALIDACION
# ============================================================

def write_metadata(
    windows_processed,
    raw_records,
    unique_records,
    failed_windows,
    portal_total,
):
    json_records = unique_records

    csv_records = (
        count_csv_records()
    )

    xml_coverage_complete = (
        failed_windows == 0
        and unique_records > 0
        and json_records == csv_records
    )

    portal_reconciliation = (
        portal_total is not None
        and portal_total == unique_records
    )

    coverage_complete = (
        xml_coverage_complete
        and portal_reconciliation
    )

    if coverage_complete:

        validation_status = "OK"

        note = (
            "Cobertura 100% verificada: "
            "todas las ventanas XML fueron "
            "procesadas y el total de registros "
            "unicos coincide con el total "
            "informado por el portal."
        )

    elif xml_coverage_complete:

        validation_status = (
            "PARTIAL_VALIDATION"
        )

        if portal_total is None:

            note = (
                "Extraccion XML completa e "
                "internamente consistente, pero "
                "no fue posible recuperar el "
                "total del portal web. "
                "No se certifica cobertura 100%."
            )

        else:

            note = (
                "Extraccion XML completa, pero "
                "el total XML no coincide con "
                "el total informado por el portal. "
                "No se certifica cobertura 100%."
            )

    else:

        validation_status = "ERROR"

        note = (
            "La extraccion XML presenta errores "
            "o inconsistencias."
        )

    metadata = {
        "extraction_timestamp": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
        "source": (
            "Compras Estatales Uruguay / ARCE"
        ),
        "source_endpoint": BASE_URL,
        "portal_url": PORTAL_URL,
        "publication_type": (
            "Llamados vigentes"
        ),
        "start_date": (
            START_DATE.strftime(
                "%Y-%m-%d"
            )
        ),
        "windows_processed": (
            windows_processed
        ),
        "failed_windows": (
            failed_windows
        ),
        "xml_records_raw": (
            raw_records
        ),
        "unique_records": (
            unique_records
        ),
        "json_records": (
            json_records
        ),
        "csv_records": (
            csv_records
        ),
        "portal_total": (
            portal_total
        ),
        "xml_coverage_complete": (
            xml_coverage_complete
        ),
        "portal_reconciliation": (
            portal_reconciliation
        ),
        "coverage_complete": (
            coverage_complete
        ),
        "validation_status": (
            validation_status
        ),
        "note": note,
    }

    OUTPUT_METADATA.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# MAIN
# ============================================================

def main():
    today = datetime.now().date()

    end_date = datetime(
        today.year,
        today.month,
        today.day,
    )

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/130 Safari/537.36"
            ),
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "*/*;q=0.8"
            ),
            "Accept-Language": (
                "es-UY,es;q=0.9,en;q=0.8"
            ),
        }
    )

    all_records = []

    windows_processed = 0
    failed_windows = 0

    # --------------------------------------------------------
    # EXTRACCION XML
    # --------------------------------------------------------

    for start, end in generate_windows(
        START_DATE,
        end_date,
    ):

        try:

            records = download_window(
                session,
                start,
                end,
            )

            all_records.extend(
                records
            )

            windows_processed += 1

        except Exception as exc:

            failed_windows += 1

            print(
                "ERROR ventana "
                f"{start:%d/%m/%Y} - "
                f"{end:%d/%m/%Y}: "
                f"{exc}",
                file=sys.stderr,
            )

    print("")
    print(
        "Registros XML crudos:",
        len(all_records),
    )

    # --------------------------------------------------------
    # DEDUPLICACION
    # --------------------------------------------------------

    unique = {}

    for record in all_records:

        key = make_record_key(record)

        unique[key] = record

    records = list(
        unique.values()
    )

    print(
        "Registros unicos:",
        len(records),
    )

    # --------------------------------------------------------
    # ARCHIVOS
    # --------------------------------------------------------

    write_json(records)
    write_csv(records)

    # --------------------------------------------------------
    # PORTAL
    # --------------------------------------------------------

    portal_total = (
        get_portal_total(session)
    )

    # --------------------------------------------------------
    # METADATA
    # --------------------------------------------------------

    write_metadata(
        windows_processed=(
            windows_processed
        ),
        raw_records=(
            len(all_records)
        ),
        unique_records=(
            len(records)
        ),
        failed_windows=(
            failed_windows
        ),
        portal_total=(
            portal_total
        ),
    )

    # --------------------------------------------------------
    # RESULTADO
    # --------------------------------------------------------

    print("")
    print(
        "================================="
    )

    print(
        "RESUMEN DE VALIDACION"
    )

    print(
        "================================="
    )

    print(
        "Ventanas procesadas:",
        windows_processed,
    )

    print(
        "Ventanas fallidas:",
        failed_windows,
    )

    print(
        "Registros XML crudos:",
        len(all_records),
    )

    print(
        "Registros unicos:",
        len(records),
    )

    print(
        "Total portal:",
        portal_total,
    )

    print("")
    print(
        "Archivos generados:"
    )

    print(
        "- latest.json"
    )

    print(
        "- latest.csv"
    )

    print(
        "- metadata.json"
    )

    if failed_windows:

        print("")
        print(
            "ERROR: hubo ventanas "
            "XML fallidas."
        )

        sys.exit(1)

    if not records:

        print(
            "ERROR: no se obtuvieron "
            "registros.",
            file=sys.stderr,
        )

        sys.exit(1)

    print("")
    print(
        "Extraccion finalizada."
    )

    if (
        portal_total is not None
        and portal_total == len(records)
    ):

        print(
            "COBERTURA 100% VERIFICADA"
        )

    elif portal_total is None:

        print(
            "XML completo. "
            "Reconciliacion web no disponible."
        )

    else:

        print(
            "ATENCION: XML y portal "
            "no coinciden."
        )


if __name__ == "__main__":
    main()
