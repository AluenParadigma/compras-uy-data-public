import csv
import json
import os
import sys
import time
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

OUTPUT_JSON = Path("latest.json")
OUTPUT_CSV = Path("latest.csv")
OUTPUT_METADATA = Path("metadata.json")

TEMP_JSON = Path("latest.tmp.json")
TEMP_CSV = Path("latest.tmp.csv")
TEMP_METADATA = Path("metadata.tmp.json")

START_DATE = datetime(2024, 1, 1)

WINDOW_DAYS = 10

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60

# Solo dos intentos por ventana.
MAX_WINDOW_ATTEMPTS = 2

# Espera entre intento 1 y 2.
RETRY_WAIT_SECONDS = 8

# Pequeña pausa entre consultas para no golpear ARCE.
REQUEST_PAUSE_SECONDS = 0.5

csv.field_size_limit(sys.maxsize)


# ============================================================
# LOG
# ============================================================

def log(*args):
    print(*args, flush=True)


def log_error(*args):
    print(*args, file=sys.stderr, flush=True)


# ============================================================
# SESION HTTP
# ============================================================

def create_session():
    session = requests.Session()

    # IMPORTANTE:
    # No usamos Retry/HTTPAdapter.
    # Así evitamos reintentos ocultos que antes multiplicaban
    # enormemente el tiempo total de ejecución.

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/130.0 Safari/537.36"
            ),
            "Accept": (
                "application/xml,"
                "text/xml,"
                "application/xhtml+xml,"
                "*/*;q=0.8"
            ),
            "Accept-Language": "es-UY,es;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }
    )

    return session


# ============================================================
# PARAMETROS ARCE
# ============================================================

def build_params(start_date, end_date, publication_type):
    return {
        "tipo_publicacion": publication_type,
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


# ============================================================
# XML
# ============================================================

def normalize_tag(tag):
    if "}" in tag:
        return tag.split("}", 1)[1]

    return tag


def xml_element_to_dict(element):
    result = {}

    for key, value in element.attrib.items():
        result[normalize_tag(key)] = value

    text = (element.text or "").strip()

    if text:
        result["_text"] = text

    for child in list(element):
        key = normalize_tag(child.tag)

        child_value = xml_element_to_dict(child)

        if not child_value:
            child_value = None

        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]

            result[key].append(child_value)

        else:
            result[key] = child_value

    return result


def find_compra_nodes(root):
    compras = []

    for element in root.iter():
        tag = normalize_tag(element.tag).lower()

        if tag == "compra":
            compras.append(element)

    return compras


# ============================================================
# FLATTEN
# ============================================================

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
                        separator,
                    )
                )

            elif isinstance(value, list):

                items[new_key] = json.dumps(
                    value,
                    ensure_ascii=False,
                )

            elif value is None:

                items[new_key] = ""

            else:

                items[new_key] = value

    else:

        items[parent_key] = data

    return items


# ============================================================
# CONTROL DE CONTENIDO
# ============================================================

def count_non_empty_scalar_fields(record):
    flattened = flatten(record)

    count = 0

    for value in flattened.values():

        if value is None:
            continue

        text = str(value).strip()

        if not text:
            continue

        if text in ("[]", "{}", "null"):
            continue

        count += 1

    return count


def record_has_real_content(record):
    return count_non_empty_scalar_fields(record) >= 3


# ============================================================
# IDENTIFICADOR
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
        "nro_compra",
        "numero_compra",
        "num_compra",
        "id_publicacion",
    ]

    for candidate in id_candidates:

        if candidate in flattened:

            return (
                f"{candidate}:"
                f"{flattened[candidate]}"
            )

    for candidate in id_candidates:

        for key, value in flattened.items():

            if key.endswith(candidate) and value:

                return (
                    f"{candidate}:"
                    f"{value}"
                )

    fallback_values = []

    keywords = [
        "tipo_compra",
        "nro_compra",
        "numero_compra",
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

    if len(fallback_values) >= 2:

        return "|".join(fallback_values)

    return json.dumps(
        record,
        sort_keys=True,
        ensure_ascii=False,
    )


# ============================================================
# VENTANAS
# ============================================================

def generate_windows(start_date, end_date):

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
# REQUEST
# ============================================================

def request_xml(
    session,
    start_date,
    end_date,
    publication_type,
):
    params = build_params(
        start_date,
        end_date,
        publication_type,
    )

    response = session.get(
        BASE_URL,
        params=params,
        timeout=(
            CONNECT_TIMEOUT,
            READ_TIMEOUT,
        ),
    )

    if response.status_code == 403:
        raise RuntimeError(
            "HTTP 403 Forbidden"
        )

    if response.status_code == 429:
        raise RuntimeError(
            "HTTP 429 Too Many Requests"
        )

    if response.status_code >= 500:
        raise RuntimeError(
            f"HTTP {response.status_code} "
            "Server Error"
        )

    response.raise_for_status()

    if not response.content:
        raise RuntimeError(
            "Respuesta vacia de ARCE"
        )

    try:

        root = ET.fromstring(
            response.content
        )

    except ET.ParseError as exc:

        preview = (
            response.text[:300]
        )

        raise RuntimeError(
            "Respuesta no XML. "
            f"Inicio de respuesta: {preview}"
        ) from exc

    return root


# ============================================================
# HEALTH CHECK
# ============================================================

def health_check(session):
    """
    Antes de recorrer las ~100 ventanas comprobamos
    que ARCE realmente este respondiendo.

    Si ARCE esta bloqueando con 403, timeout, etc.,
    abortamos antes de iniciar la extraccion completa.
    """

    log("")
    log("====================================")
    log("HEALTH CHECK ARCE")
    log("====================================")

    today = datetime.now().date()

    start = datetime(
        today.year,
        today.month,
        today.day,
    )

    for attempt in range(
        1,
        MAX_WINDOW_ATTEMPTS + 1,
    ):

        try:

            root = request_xml(
                session=session,
                start_date=start,
                end_date=start,
                publication_type="lv",
            )

            # No importa si hoy hay 0 compras.
            # Lo importante es haber recibido XML valido.

            nodes = find_compra_nodes(root)

            log(
                "ARCE responde correctamente.",
                "Compras encontradas hoy:",
                len(nodes),
            )

            return True

        except Exception as exc:

            log_error(
                f"Health check intento "
                f"{attempt}/"
                f"{MAX_WINDOW_ATTEMPTS}: "
                f"{exc}"
            )

            if (
                attempt
                < MAX_WINDOW_ATTEMPTS
            ):

                log(
                    f"Esperando "
                    f"{RETRY_WAIT_SECONDS}s..."
                )

                time.sleep(
                    RETRY_WAIT_SECONDS
                )

    raise RuntimeError(
        "ARCE no supera el health check. "
        "Se aborta antes de comenzar "
        "la extraccion completa."
    )


# ============================================================
# DESCARGAR UNA VENTANA
# ============================================================

def download_window(
    session,
    start_date,
    end_date,
    publication_type,
):

    label = (
        "LV"
        if publication_type == "lv"
        else "ALL"
    )

    log(
        f"[{label}] "
        f"{start_date:%d/%m/%Y}"
        " - "
        f"{end_date:%d/%m/%Y}"
    )

    last_error = None

    for attempt in range(
        1,
        MAX_WINDOW_ATTEMPTS + 1,
    ):

        try:

            root = request_xml(
                session,
                start_date,
                end_date,
                publication_type,
            )

            compra_nodes = (
                find_compra_nodes(root)
            )

            records = []

            for node in compra_nodes:

                record = (
                    xml_element_to_dict(
                        node
                    )
                )

                if record:
                    records.append(record)

            log(
                "  compras:",
                len(records),
                f"| intento {attempt}"
            )

            time.sleep(
                REQUEST_PAUSE_SECONDS
            )

            return records

        except (
            requests.Timeout,
            requests.ConnectionError,
            requests.HTTPError,
            RuntimeError,
        ) as exc:

            last_error = exc

            log_error(
                f"  intento "
                f"{attempt}/"
                f"{MAX_WINDOW_ATTEMPTS} "
                f"fallo: {exc}"
            )

            if (
                attempt
                < MAX_WINDOW_ATTEMPTS
            ):

                log(
                    f"  reintento en "
                    f"{RETRY_WAIT_SECONDS}s..."
                )

                time.sleep(
                    RETRY_WAIT_SECONDS
                )

    raise RuntimeError(
        f"Ventana {label} "
        f"{start_date:%Y-%m-%d}"
        " - "
        f"{end_date:%Y-%m-%d} "
        "fallo luego de "
        f"{MAX_WINDOW_ATTEMPTS} intentos. "
        f"Ultimo error: {last_error}"
    )


# ============================================================
# EXTRACCION
# ============================================================

def extract_publication_type(
    session,
    publication_type,
    start_date,
    end_date,
):

    all_records = []

    windows_processed = 0
    windows_with_records = 0

    windows = list(
        generate_windows(
            start_date,
            end_date,
        )
    )

    total_windows = len(windows)

    label = (
        "LV"
        if publication_type == "lv"
        else "ALL"
    )

    for index, (start, end) in enumerate(
        windows,
        start=1,
    ):

        log(
            f"{label} ventana "
            f"{index}/{total_windows}"
        )

        # IMPORTANTE:
        # si una sola ventana falla de manera definitiva,
        # abortamos inmediatamente.
        #
        # No tiene sentido recorrer las restantes porque
        # el snapshot ya no podria certificarse como completo.

        records = download_window(
            session,
            start,
            end,
            publication_type,
        )

        all_records.extend(records)

        windows_processed += 1

        if records:
            windows_with_records += 1

    unique = {}

    for record in all_records:

        key = make_record_key(
            record
        )

        unique[key] = record

    records_with_real_content = sum(
        1
        for record
        in unique.values()
        if record_has_real_content(
            record
        )
    )

    return {
        "raw_records": all_records,
        "unique": unique,
        "windows_processed":
            windows_processed,
        "windows_expected":
            total_windows,
        "windows_with_records":
            windows_with_records,
        "failed_windows": 0,
        "records_with_real_content":
            records_with_real_content,
    }


# ============================================================
# VALIDACION CRUZADA
# ============================================================

def cross_validate(
    lv_unique,
    all_unique,
):

    lv_keys = set(
        lv_unique.keys()
    )

    all_keys = set(
        all_unique.keys()
    )

    missing_in_all = sorted(
        lv_keys - all_keys
    )

    common = (
        lv_keys & all_keys
    )

    return {
        "common": common,
        "missing_in_all":
            missing_in_all,
        "lv_is_subset_of_all":
            len(missing_in_all) == 0,
    }


# ============================================================
# ARCHIVOS TEMPORALES
# ============================================================

def cleanup_temp_files():

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


def write_json_temp(records):

    payload = {
        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),
        "source":
            "Compras Estatales Uruguay / ARCE",
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


def write_csv_temp(records):

    flat_records = [
        flatten(record)
        for record in records
    ]

    if not flat_records:

        raise RuntimeError(
            "No hay registros para CSV"
        )

    columns = sorted(
        {
            key
            for record in flat_records
            for key in record.keys()
        }
    )

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

        for record in flat_records:

            writer.writerow(record)


def count_temp_csv_records():

    with TEMP_CSV.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = csv.reader(file)

        next(reader, None)

        return sum(
            1 for _ in reader
        )


# ============================================================
# METADATA
# ============================================================

def build_metadata(
    lv_result,
    all_result,
    cross_result,
):

    lv_unique_records = len(
        lv_result["unique"]
    )

    all_unique_records = len(
        all_result["unique"]
    )

    json_records = (
        lv_unique_records
    )

    csv_records = (
        count_temp_csv_records()
    )

    lv_xml_complete = (
        lv_result[
            "windows_processed"
        ]
        ==
        lv_result[
            "windows_expected"
        ]
        and
        lv_unique_records > 0
    )

    all_xml_complete = (
        all_result[
            "windows_processed"
        ]
        ==
        all_result[
            "windows_expected"
        ]
        and
        all_unique_records > 0
    )

    file_integrity = (
        json_records
        ==
        csv_records
        ==
        lv_unique_records
    )

    cross_validation_match = (
        cross_result[
            "lv_is_subset_of_all"
        ]
    )

    content_quality_complete = (
        lv_unique_records > 0
        and
        lv_result[
            "records_with_real_content"
        ]
        ==
        lv_unique_records
    )

    xml_double_validation = (
        lv_xml_complete
        and
        all_xml_complete
        and
        cross_validation_match
        and
        file_integrity
    )

    coverage_complete = (
        xml_double_validation
        and
        content_quality_complete
    )

    return {
        "extraction_timestamp":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "source":
            "Compras Estatales Uruguay / ARCE",

        "source_endpoint":
            BASE_URL,

        "start_date":
            START_DATE.strftime(
                "%Y-%m-%d"
            ),

        "window_days":
            WINDOW_DAYS,

        "max_window_attempts":
            MAX_WINDOW_ATTEMPTS,

        "lv_windows_processed":
            lv_result[
                "windows_processed"
            ],

        "lv_windows_expected":
            lv_result[
                "windows_expected"
            ],

        "lv_windows_with_records":
            lv_result[
                "windows_with_records"
            ],

        "lv_failed_windows":
            0,

        "lv_raw_records":
            len(
                lv_result[
                    "raw_records"
                ]
            ),

        "lv_unique_records":
            lv_unique_records,

        "lv_records_with_real_content":
            lv_result[
                "records_with_real_content"
            ],

        "all_windows_processed":
            all_result[
                "windows_processed"
            ],

        "all_windows_expected":
            all_result[
                "windows_expected"
            ],

        "all_windows_with_records":
            all_result[
                "windows_with_records"
            ],

        "all_failed_windows":
            0,

        "all_raw_records":
            len(
                all_result[
                    "raw_records"
                ]
            ),

        "all_unique_records":
            all_unique_records,

        "all_records_with_real_content":
            all_result[
                "records_with_real_content"
            ],

        "lv_records_found_in_all":
            len(
                cross_result[
                    "common"
                ]
            ),

        "lv_records_missing_in_all":
            len(
                cross_result[
                    "missing_in_all"
                ]
            ),

        "lv_is_subset_of_all":
            cross_validation_match,

        "json_records":
            json_records,

        "csv_records":
            csv_records,

        "file_integrity":
            file_integrity,

        "lv_xml_complete":
            lv_xml_complete,

        "all_xml_complete":
            all_xml_complete,

        "cross_validation_match":
            cross_validation_match,

        "xml_double_validation":
            xml_double_validation,

        "content_quality_complete":
            content_quality_complete,

        "portal_total":
            None,

        "portal_reconciliation":
            False,

        "coverage_complete":
            coverage_complete,

        "coverage_basis":
            (
                "DOUBLE_OFFICIAL_XML_EXTRACTION"
                "_PLUS_CONTENT_VALIDATION"
            ),

        "validation_status":
            (
                "OK"
                if coverage_complete
                else "ERROR"
            ),

        "note":
            (
                "Cobertura XML validada por "
                "doble extraccion oficial ARCE. "
                "Todas las ventanas fueron "
                "procesadas correctamente, "
                "los llamados vigentes estan "
                "contenidos en el universo "
                "general, JSON y CSV coinciden "
                "y todos los registros poseen "
                "contenido sustantivo."
                if coverage_complete
                else
                "La validacion no fue "
                "satisfactoria. Se conserva "
                "el ultimo snapshot valido."
            ),
    }


def write_metadata_temp(metadata):

    TEMP_METADATA.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# PUBLICAR
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

def main():

    cleanup_temp_files()

    session = create_session()

    try:

        # ----------------------------------------------------
        # HEALTH CHECK
        # ----------------------------------------------------

        health_check(session)

        today = datetime.now().date()

        end_date = datetime(
            today.year,
            today.month,
            today.day,
        )

        # ----------------------------------------------------
        # LV
        # ----------------------------------------------------

        log("")
        log("====================================")
        log("EXTRACCION A - LLAMADOS VIGENTES")
        log("====================================")

        lv_result = extract_publication_type(
            session=session,
            publication_type="lv",
            start_date=START_DATE,
            end_date=end_date,
        )

        log("")
        log(
            "LV crudos:",
            len(
                lv_result[
                    "raw_records"
                ]
            ),
        )

        log(
            "LV unicos:",
            len(
                lv_result[
                    "unique"
                ]
            ),
        )

        log(
            "LV con contenido real:",
            lv_result[
                "records_with_real_content"
            ],
        )

        # ----------------------------------------------------
        # ALL
        # ----------------------------------------------------

        log("")
        log("====================================")
        log("EXTRACCION B - TODOS LOS LLAMADOS")
        log("====================================")

        all_result = extract_publication_type(
            session=session,
            publication_type="l",
            start_date=START_DATE,
            end_date=end_date,
        )

        log("")
        log(
            "ALL crudos:",
            len(
                all_result[
                    "raw_records"
                ]
            ),
        )

        log(
            "ALL unicos:",
            len(
                all_result[
                    "unique"
                ]
            ),
        )

        # ----------------------------------------------------
        # CROSS VALIDATION
        # ----------------------------------------------------

        cross_result = cross_validate(
            lv_result["unique"],
            all_result["unique"],
        )

        log("")
        log("====================================")
        log("VALIDACION CRUZADA")
        log("====================================")

        log(
            "LV encontrados en ALL:",
            len(
                cross_result[
                    "common"
                ]
            ),
        )

        log(
            "LV faltantes en ALL:",
            len(
                cross_result[
                    "missing_in_all"
                ]
            ),
        )

        if not cross_result[
            "lv_is_subset_of_all"
        ]:

            raise RuntimeError(
                "Existen llamados vigentes "
                "que no aparecen en ALL."
            )

        # ----------------------------------------------------
        # ARCHIVOS TEMPORALES
        # ----------------------------------------------------

        records = list(
            lv_result[
                "unique"
            ].values()
        )

        if not records:

            raise RuntimeError(
                "ARCE devolvio cero "
                "llamados vigentes."
            )

        write_json_temp(records)

        write_csv_temp(records)

        metadata = build_metadata(
            lv_result,
            all_result,
            cross_result,
        )

        write_metadata_temp(
            metadata
        )

        # ----------------------------------------------------
        # VALIDACION FINAL
        # ----------------------------------------------------

        if not metadata[
            "coverage_complete"
        ]:

            raise RuntimeError(
                "La validacion final "
                "del snapshot fallo."
            )

        # ----------------------------------------------------
        # PUBLICACION ATOMICA
        # ----------------------------------------------------

        publish_snapshot()

        log("")
        log("====================================")
        log("EXTRACCION FINALIZADA OK")
        log("====================================")

        log(
            "LV:",
            metadata[
                "lv_unique_records"
            ],
        )

        log(
            "ALL:",
            metadata[
                "all_unique_records"
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
            "Coverage complete:",
            metadata[
                "coverage_complete"
            ],
        )

        log(
            "Validation status:",
            metadata[
                "validation_status"
            ],
        )

        log("")
        log(
            "Snapshot nuevo publicado."
        )

    except Exception as exc:

        cleanup_temp_files()

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

        sys.exit(1)


if __name__ == "__main__":
    main()
