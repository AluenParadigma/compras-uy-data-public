import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests


BASE_URL = (
    "https://www.comprasestatales.gub.uy/"
    "comprasenlinea/jboss/generarReporte"
)

OUTPUT_JSON = Path("latest.json")
OUTPUT_CSV = Path("latest.csv")
OUTPUT_METADATA = Path("metadata.json")

# Para la primera extracción hacemos un backfill amplio.
# Luego podremos optimizarlo.
START_DATE = datetime(2024, 1, 1)

# ARCE admite ventanas de hasta 10 días.
WINDOW_DAYS = 10

TIMEOUT = 60


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
    """Elimina namespace XML si existe."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def element_to_dict(element):
    """
    Convierte recursivamente un nodo XML en un diccionario.
    Esto nos permite trabajar aunque ARCE cambie o amplíe campos.
    """
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
    """
    Intenta detectar automáticamente qué nodos representan compras.

    Primero busca etiquetas comunes. Si no encuentra ninguna,
    toma los hijos directos del nodo raíz que tengan estructura.
    """
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

    children = [child for child in list(root) if list(child)]

    if children:
        return children

    return []


def flatten(data, parent_key="", separator="_"):
    """
    Aplana diccionarios para poder exportarlos fácilmente a CSV.
    """
    items = {}

    if isinstance(data, dict):
        for key, value in data.items():
            new_key = (
                f"{parent_key}{separator}{key}"
                if parent_key
                else key
            )

            if isinstance(value, dict):
                items.update(flatten(value, new_key, separator))

            elif isinstance(value, list):
                # Para listas complejas conservamos JSON.
                items[new_key] = json.dumps(
                    value,
                    ensure_ascii=False
                )

            else:
                items[new_key] = value

    else:
        items[parent_key] = data

    return items


def make_record_key(record):
    """
    Intenta construir una clave estable para deduplicar compras.
    Si encuentra un ID oficial, lo prioriza.
    """

    flattened = {
        str(k).lower(): str(v).strip()
        for k, v in flatten(record).items()
        if v not in (None, "")
    }

    id_candidates = [
        "id_compra",
        "idcompra",
        "id",
        "numero_compra",
        "nro_compra",
        "numero",
    ]

    for candidate in id_candidates:
        for key, value in flattened.items():
            if key.endswith(candidate) and value:
                return f"{candidate}:{value}"

    # Si no encontramos ID explícito, construimos una clave
    # combinando campos frecuentes.
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
                fallback_values.append(f"{keyword}:{value}")
                break

    if fallback_values:
        return "|".join(fallback_values)

    # Último recurso: representación completa.
    return json.dumps(record, sort_keys=True, ensure_ascii=False)


def download_window(session, start_date, end_date):
    params = build_params(start_date, end_date)

    print(
        f"Consultando {start_date:%d/%m/%Y} "
        f"- {end_date:%d/%m/%Y}"
    )

    response = session.get(
        BASE_URL,
        params=params,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    content = response.content

    if not content:
        return []

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        preview = response.text[:500]

        raise RuntimeError(
            "ARCE no devolvió XML válido.\n"
            f"Respuesta inicial:\n{preview}"
        ) from exc

    nodes = find_record_nodes(root)

    records = []

    for node in nodes:
        record = element_to_dict(node)

        if isinstance(record, dict):
            records.append(record)

    return records


def generate_windows(start_date, end_date):
    current = start_date

    while current <= end_date:
        window_end = min(
            current + timedelta(days=WINDOW_DAYS - 1),
            end_date,
        )

        yield current, window_end

        current = window_end + timedelta(days=1)


def write_json(records):
    payload = {
        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "source": "Compras Estatales Uruguay / ARCE",
        "publication_type": "Llamados vigentes",
        "total_records": len(records),
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


def write_csv(records):
    flat_records = [flatten(record) for record in records]

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
        rows = list(csv.reader(file))

    if len(rows) <= 1:
        return 0

    return len(rows) - 1


def write_metadata(
    windows_processed,
    raw_records,
    unique_records,
    failed_windows,
):
    json_records = unique_records
    csv_records = count_csv_records()

    coverage_complete = (
        failed_windows == 0
        and unique_records > 0
        and json_records == csv_records
    )

    metadata = {
        "extraction_timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "source": "Compras Estatales Uruguay / ARCE",
        "source_endpoint": BASE_URL,
        "publication_type": "Llamados vigentes",
        "start_date": START_DATE.strftime("%Y-%m-%d"),
        "windows_processed": windows_processed,
        "failed_windows": failed_windows,
        "xml_records_raw": raw_records,
        "unique_records": unique_records,
        "json_records": json_records,
        "csv_records": csv_records,
        "coverage_complete": coverage_complete,
        "portal_reconciliation": False,
        "validation_status": (
            "OK"
            if coverage_complete
            else "ERROR"
        ),
        "note": (
            "Cobertura XML completa de todas las ventanas "
            "consultadas. La reconciliación contra el total "
            "del portal web se incorporará en una segunda etapa."
        ),
    }

    OUTPUT_METADATA.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


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
                "(compatible; ComprasUYData/1.0)"
            ),
            "Accept": (
                "application/xml,text/xml,"
                "application/xhtml+xml,*/*"
            ),
        }
    )

    all_records = []

    windows_processed = 0
    failed_windows = 0

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

            all_records.extend(records)
            windows_processed += 1

        except Exception as exc:
            failed_windows += 1

            print(
                f"ERROR ventana "
                f"{start:%d/%m/%Y} - "
                f"{end:%d/%m/%Y}: {exc}",
                file=sys.stderr,
            )

    print(f"Registros XML crudos: {len(all_records)}")

    unique = {}

    for record in all_records:
        key = make_record_key(record)
        unique[key] = record

    records = list(unique.values())

    print(f"Registros únicos: {len(records)}")

    write_json(records)
    write_csv(records)

    write_metadata(
        windows_processed=windows_processed,
        raw_records=len(all_records),
        unique_records=len(records),
        failed_windows=failed_windows,
    )

    print("")
    print("Archivos generados:")
    print(f"- {OUTPUT_JSON}")
    print(f"- {OUTPUT_CSV}")
    print(f"- {OUTPUT_METADATA}")

    if failed_windows:
        print(
            f"ATENCIÓN: fallaron "
            f"{failed_windows} ventanas."
        )

        sys.exit(1)

    if not records:
        print(
            "ERROR: no se obtuvieron registros.",
            file=sys.stderr,
        )

        sys.exit(1)

    print("Extracción finalizada correctamente.")


if __name__ == "__main__":
    main()
